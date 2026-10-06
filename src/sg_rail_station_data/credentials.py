"""Strict, secret-safe credential loading for local and CI runs."""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import re


LTA_API_KEY_NAME = "LTA_API_KEY"
DEFAULT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


class CredentialError(ValueError):
    """Raised when a required credential is absent or malformed."""


def _validate_key(value: str, context: str) -> str:
    if not value:
        raise CredentialError(f"{context}: value is blank")
    if value != value.strip():
        raise CredentialError(f"{context}: value has surrounding whitespace")
    if any(character in value for character in ("\r", "\n", "\x00")):
        raise CredentialError(f"{context}: value contains a forbidden control character")
    if value.startswith(("$(", "${")):
        raise CredentialError(f"{context}: substitutions are not supported")
    if value == "replace-with-your-datamall-account-key":
        raise CredentialError(f"{context}: replace the example placeholder with a real key")
    return value


def _parse_env_file(path: Path) -> str:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CredentialError(
            f"{path}: missing local credential file; copy .env.example to .env "
            f"and set {LTA_API_KEY_NAME}"
        ) from exc
    except OSError as exc:
        raise CredentialError(f"{path}: cannot read credential file: {exc}") from exc

    assignments: list[str] = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.fullmatch(r"LTA_API_KEY=(.*)", line)
        if match is None:
            name = line.split("=", 1)[0].strip() if "=" in line else line.strip()
            raise CredentialError(
                f"{path}:{line_number}: unexpected entry {name!r}; "
                f"only {LTA_API_KEY_NAME} is allowed"
            )
        assignments.append(match.group(1))

    if len(assignments) != 1:
        raise CredentialError(
            f"{path}: expected exactly one {LTA_API_KEY_NAME} assignment; "
            f"found {len(assignments)}"
        )

    value = assignments[0]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    elif value.startswith(("'", '"')) or value.endswith(("'", '"')):
        raise CredentialError(f"{path}: {LTA_API_KEY_NAME} has unmatched quotes")
    return _validate_key(value, f"{path}: {LTA_API_KEY_NAME}")


def load_lta_api_key(
    *,
    environ: Mapping[str, str] | None = None,
    env_path: str | Path = DEFAULT_ENV_PATH,
) -> tuple[str, str]:
    """Load the LTA key from CI/process environment, then the local ``.env``.

    The returned source is safe to log. The key itself must never be logged or
    persisted in pipeline artifacts.
    """

    environment = os.environ if environ is None else environ
    if LTA_API_KEY_NAME in environment:
        value = _validate_key(
            environment[LTA_API_KEY_NAME],
            f"environment variable {LTA_API_KEY_NAME}",
        )
        return value, f"environment variable {LTA_API_KEY_NAME}"

    path = Path(env_path).expanduser().resolve()
    return _parse_env_file(path), str(path)
