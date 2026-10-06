"""Durable JSON Lines logging for pipeline runs."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from .storage import OutputExistsError, StorageError


_RESERVED_FIELDS = frozenset({"schema_version", "timestamp", "level", "event", "message"})


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(UTC)


def format_timestamp(value: datetime) -> str:
    """Format an aware datetime as ISO 8601 with a ``Z`` UTC suffix."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class StructuredLogger:
    """Append durable, machine-readable events to a newly created JSONL file."""

    def __init__(
        self,
        path: str | Path,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        self.path = Path(path)
        if not self.path.parent.is_dir():
            raise StorageError(
                f"Log parent does not exist or is not a directory: {self.path.parent}"
            )
        context_value = dict(context or {})
        collisions = sorted(_RESERVED_FIELDS.intersection(context_value))
        if collisions:
            raise ValueError(f"Log context uses reserved fields: {collisions}")
        self._context = context_value
        self._context_fields = frozenset(context_value)
        self._lock = threading.Lock()
        try:
            self._stream = self.path.open("x", encoding="utf-8", newline="\n")
        except FileExistsError as error:
            raise OutputExistsError(f"Refusing to overwrite existing log: {self.path}") from error
        self._closed = False

    def log(
        self,
        level: str,
        event: str,
        message: str,
        **fields: Any,
    ) -> dict[str, Any]:
        """Write and return one event, flushing it to disk before returning."""

        normalized_level = level.upper()
        if normalized_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"Unsupported log level: {level}")
        if not event or not isinstance(event, str):
            raise ValueError("Log event must be a non-empty string")
        if not isinstance(message, str):
            raise TypeError("Log message must be a string")
        collisions = sorted((_RESERVED_FIELDS | self._context_fields).intersection(fields))
        if collisions:
            raise ValueError(f"Log fields use reserved or contextual names: {collisions}")
        context_collisions = sorted(set(self._context).intersection(fields))
        if context_collisions:
            raise ValueError(f"Log fields override context fields: {context_collisions}")

        record: dict[str, Any] = {
            "schema_version": 1,
            "timestamp": format_timestamp(utc_now()),
            "level": normalized_level,
            "event": event,
            "message": message,
            **self._context,
            **fields,
        }
        try:
            line = json.dumps(record, ensure_ascii=False, allow_nan=False, sort_keys=True)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Log event is not valid JSON: {error}") from error

        with self._lock:
            if self._closed:
                raise RuntimeError("Cannot write to a closed structured log")
            self._stream.write(f"{line}\n")
            self._stream.flush()
            os.fsync(self._stream.fileno())
        return record

    def debug(self, event: str, message: str, **fields: Any) -> dict[str, Any]:
        return self.log("DEBUG", event, message, **fields)

    def info(self, event: str, message: str, **fields: Any) -> dict[str, Any]:
        return self.log("INFO", event, message, **fields)

    def warning(self, event: str, message: str, **fields: Any) -> dict[str, Any]:
        return self.log("WARNING", event, message, **fields)

    def error(self, event: str, message: str, **fields: Any) -> dict[str, Any]:
        return self.log("ERROR", event, message, **fields)

    def critical(self, event: str, message: str, **fields: Any) -> dict[str, Any]:
        return self.log("CRITICAL", event, message, **fields)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._stream.flush()
                os.fsync(self._stream.fileno())
                self._stream.close()
                self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
