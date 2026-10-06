"""Severity ordering and the pipeline's process-exit policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum


class Severity(IntEnum):
    """Ordered severity used by validation and change events."""

    INFO = 10
    WARNING = 20
    ERROR = 30
    CRITICAL = 40


class PipelineStatus(str, Enum):
    """Terminal status recorded in a run manifest."""

    COMPLETE = "complete"
    REVIEW_REQUIRED = "review_required"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SeverityPolicy:
    """Map the highest event severity to a manifest status and process exit code."""

    warning_exit_code: int = 2
    failure_exit_code: int = 1
    success_exit_code: int = 0

    def status_for(self, highest: Severity | None) -> PipelineStatus:
        """Return the terminal status for the highest emitted severity."""

        if highest is None or highest <= Severity.INFO:
            return PipelineStatus.COMPLETE
        if highest == Severity.WARNING:
            return PipelineStatus.REVIEW_REQUIRED
        return PipelineStatus.FAILED

    def exit_code_for(self, highest: Severity | None) -> int:
        """Return 0 for success, 2 for review, and 1 for blocking failures."""

        status = self.status_for(highest)
        if status is PipelineStatus.COMPLETE:
            return self.success_exit_code
        if status is PipelineStatus.REVIEW_REQUIRED:
            return self.warning_exit_code
        return self.failure_exit_code

    def blocks_release(self, severity: Severity) -> bool:
        """Return whether an event prevents release promotion."""

        return severity >= Severity.ERROR

    def aborts_run(self, severity: Severity) -> bool:
        """Return whether processing should stop immediately."""

        return severity >= Severity.CRITICAL


DEFAULT_SEVERITY_POLICY = SeverityPolicy()
