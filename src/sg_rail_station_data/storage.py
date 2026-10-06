"""Strict filesystem helpers used by the pipeline.

All complete JSON documents are committed atomically.  Callers must opt in to
replacing an existing file; immutable run artifacts use the default
``overwrite=False`` behaviour.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class StorageError(RuntimeError):
    """Base error for persistent pipeline state."""


class OutputExistsError(StorageError):
    """Raised when an immutable output already exists."""


class InvalidJsonError(StorageError):
    """Raised when a JSON document cannot be decoded."""


def _fsync_directory(path: Path) -> None:
    """Best-effort durability for a directory entry update."""

    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Some filesystems do not support fsync on directory descriptors.
        pass
    finally:
        os.close(descriptor)


def atomic_write_bytes(
    path: str | Path,
    content: bytes,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically commit ``content`` to ``path``.

    The destination's parent must already exist.  Requiring callers to create
    directories explicitly prevents a typo from silently creating a new output
    tree.  With ``overwrite=False``, a hard-link commit provides atomic
    create-if-absent semantics without replacing existing evidence.
    """

    destination = Path(path)
    parent = destination.parent
    if not parent.is_dir():
        raise StorageError(f"Output parent does not exist or is not a directory: {parent}")
    if destination.exists() and not overwrite:
        raise OutputExistsError(f"Refusing to overwrite existing output: {destination}")

    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

        if overwrite:
            os.replace(temporary, destination)
        else:
            try:
                os.link(temporary, destination)
            except FileExistsError as error:
                raise OutputExistsError(
                    f"Refusing to overwrite existing output: {destination}"
                ) from error
            temporary.unlink()
        _fsync_directory(parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def atomic_write_text(
    path: str | Path,
    content: str,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically write UTF-8 text."""

    return atomic_write_bytes(path, content.encode("utf-8"), overwrite=overwrite)


def atomic_write_json(
    path: str | Path,
    value: Any,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically write strict, stable JSON followed by a newline."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise StorageError(f"Value is not valid JSON: {error}") from error
    return atomic_write_text(path, f"{encoded}\n", overwrite=overwrite)


def read_json(path: str | Path) -> Any:
    """Load one UTF-8 JSON document, with its path in decode errors."""

    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidJsonError(f"Invalid JSON document {source}: {error}") from error


def require_exact_fields(
    value: Mapping[str, Any],
    expected: set[str] | frozenset[str],
    *,
    context: str,
) -> None:
    """Reject missing and unknown fields in an external data contract."""

    actual = set(value)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        raise StorageError(
            f"{context}: unexpected fields; missing={missing}, extra={extra}"
        )
