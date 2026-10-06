"""Small, strict validation helpers shared by pipeline configuration contracts.

The pipeline treats a changed source or configuration shape as evidence that needs
review.  These helpers therefore use exact Python types: ``True`` is not accepted
where an integer is expected, and an integer is not silently coerced to a float.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit


class ContractError(ValueError):
    """Raised when external or configured data violates a declared contract."""


def fail(context: str, message: str) -> NoReturn:
    """Raise a consistently formatted contract error."""

    raise ContractError(f"{context}: {message}")


def require_fields(
    value: object,
    expected: set[str] | frozenset[str],
    context: str,
) -> Mapping[str, object]:
    """Require a string-keyed mapping with exactly ``expected`` fields."""

    if type(value) is not dict:
        fail(context, f"expected table, got {type(value).__name__}")
    mapping = value
    non_string = [key for key in mapping if type(key) is not str]
    if non_string:
        fail(context, "field names must be strings")
    actual = set(mapping)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        fail(context, f"unexpected fields; missing={missing}, extra={extra}")
    return mapping


def exact_str(value: object, context: str, *, nonempty: bool = True) -> str:
    """Return an exact string, optionally rejecting empty or padded values."""

    if type(value) is not str:
        fail(context, f"expected string, got {type(value).__name__}")
    if nonempty and not value:
        fail(context, "must not be empty")
    if value != value.strip():
        fail(context, "must not have surrounding whitespace")
    return value


def exact_int(
    value: object,
    context: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Return an exact integer subject to optional inclusive bounds."""

    if type(value) is not int:
        fail(context, f"expected integer, got {type(value).__name__}")
    if minimum is not None and value < minimum:
        fail(context, f"must be at least {minimum}")
    if maximum is not None and value > maximum:
        fail(context, f"must be at most {maximum}")
    return value


def exact_float(
    value: object,
    context: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """Return an exact finite float subject to optional inclusive bounds."""

    if type(value) is not float:
        fail(context, f"expected float, got {type(value).__name__}")
    if value != value or value in (float("inf"), float("-inf")):
        fail(context, "must be finite")
    if minimum is not None and value < minimum:
        fail(context, f"must be at least {minimum}")
    if maximum is not None and value > maximum:
        fail(context, f"must be at most {maximum}")
    return value


def exact_bool(value: object, context: str) -> bool:
    """Return an exact boolean."""

    if type(value) is not bool:
        fail(context, f"expected boolean, got {type(value).__name__}")
    return value


def string_list(
    value: object,
    context: str,
    *,
    nonempty: bool = False,
    unique: bool = True,
) -> tuple[str, ...]:
    """Return a validated immutable list of strings."""

    if type(value) is not list:
        fail(context, f"expected array, got {type(value).__name__}")
    if nonempty and not value:
        fail(context, "must not be empty")
    result = tuple(exact_str(item, f"{context}[{index}]") for index, item in enumerate(value))
    if unique and len(set(result)) != len(result):
        fail(context, "must not contain duplicates")
    return result


def https_endpoint(value: object, allowed_hosts: tuple[str, ...], context: str) -> str:
    """Validate a credential-free HTTPS endpoint against an explicit host list."""

    endpoint = exact_str(value, context)
    parts = urlsplit(endpoint)
    if parts.scheme != "https":
        fail(context, "must use https")
    if not parts.hostname or parts.hostname not in allowed_hosts:
        fail(context, f"host {parts.hostname!r} is not in allowed_hosts")
    if parts.username is not None or parts.password is not None:
        fail(context, "must not contain credentials")
    try:
        port = parts.port
    except ValueError as exc:
        fail(context, f"has an invalid port: {exc}")
    if port not in (None, 443):
        fail(context, "must use the default HTTPS port")
    if parts.fragment:
        fail(context, "must not contain a URL fragment")
    return endpoint


def safe_repo_path(value: object, repo_root: Path, context: str) -> Path:
    """Resolve a relative path and require it to remain within ``repo_root``."""

    raw = exact_str(value, context)
    configured = Path(raw)
    if configured.is_absolute():
        fail(context, "must be relative to the repository root")
    resolved_root = repo_root.resolve()
    resolved = (resolved_root / configured).resolve()
    if not resolved.is_relative_to(resolved_root):
        fail(context, "must resolve within the repository root")
    if resolved == resolved_root:
        fail(context, "must not resolve to the repository root itself")
    return resolved
