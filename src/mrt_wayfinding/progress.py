"""Small terminal progress renderer with no effect on pipeline semantics."""

from __future__ import annotations

from collections.abc import Callable
import sys
from typing import TextIO


ProgressCallback = Callable[[str, int, int | None, str], None]


class TerminalProgress:
    """Render one reusable progress line to stderr when attached to a terminal."""

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        enabled: bool | None = None,
        width: int = 24,
    ) -> None:
        if type(width) is not int or type(width) is bool or width < 10:
            raise ValueError("progress width must be an integer of at least 10")
        if enabled is not None and type(enabled) is not bool:
            raise ValueError("progress enabled must be a boolean or None")
        self.stream = sys.stderr if stream is None else stream
        self.enabled = self.stream.isatty() if enabled is None else enabled
        self.width = width
        self._rendered_width = 0

    def update(
        self,
        task: str,
        completed: int,
        total: int | None,
        message: str = "",
    ) -> None:
        if not self.enabled:
            return
        if type(task) is not str or not task:
            raise ValueError("progress task must be nonempty text")
        if type(completed) is not int or type(completed) is bool or completed < 0:
            raise ValueError("progress completed count must be a non-negative integer")
        if total is not None and (
            type(total) is not int or type(total) is bool or total < 1 or completed > total
        ):
            raise ValueError("progress total must be positive and at least completed")
        if type(message) is not str:
            raise ValueError("progress message must be text")

        if total is None:
            meter = f"{completed} complete"
        else:
            fraction = completed / total
            filled = min(self.width, int(fraction * self.width))
            bar = "#" * filled + "-" * (self.width - filled)
            meter = f"[{bar}] {completed}/{total} {fraction:>6.1%}"
        line = f"{task:<16} {meter}"
        if message:
            line += f"  {message}"
        padding = " " * max(0, self._rendered_width - len(line))
        self.stream.write(f"\r{line}{padding}")
        self.stream.flush()
        self._rendered_width = len(line)
        if total is not None and completed == total:
            self.stream.write("\n")
            self.stream.flush()
            self._rendered_width = 0

    def close(self) -> None:
        if self.enabled and self._rendered_width:
            self.stream.write("\n")
            self.stream.flush()
            self._rendered_width = 0

    def __enter__(self) -> TerminalProgress:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


__all__ = ["ProgressCallback", "TerminalProgress"]
