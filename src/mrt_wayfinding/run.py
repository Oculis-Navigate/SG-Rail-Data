"""Creation and lifecycle management for immutable pipeline run directories."""

from __future__ import annotations

import re
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from .storage import OutputExistsError, StorageError, atomic_write_json
from .structured_logging import format_timestamp, utc_now


RUN_MANIFEST_NAME = "manifest.json"
RUN_LOG_NAME = "pipeline.jsonl"
_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_STAGE_NAMES = ("collection", "stage1", "stage2")


class RunLifecycleError(RuntimeError):
    """Raised for an invalid run status transition."""


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETE = "complete"
    REVIEW_REQUIRED = "review_required"
    FAILED = "failed"


def make_run_id(now: datetime | None = None) -> str:
    """Build a sortable, collision-resistant run identifier."""

    timestamp = (now or utc_now()).astimezone(UTC)
    return f"{timestamp.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:12]}"


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(
            "Run ID must be 1-128 characters using letters, digits, '.', '_' or '-', "
            "and must start with a letter or digit"
        )
    return run_id


@dataclass(slots=True)
class RunContext:
    """A single pipeline run and its guarded manifest lifecycle."""

    run_id: str
    path: Path
    _manifest: dict[str, Any] = field(repr=False)

    @classmethod
    def create(
        cls,
        runs_root: str | Path,
        *,
        run_id: str | None = None,
        started_at: datetime | None = None,
        configuration: dict[str, Any] | None = None,
        pipeline_mode: str = "full",
    ) -> "RunContext":
        root = Path(runs_root)
        if root.exists() and not root.is_dir():
            raise StorageError(f"Runs root exists but is not a directory: {root}")
        root.mkdir(parents=True, exist_ok=True)

        start = started_at or utc_now()
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("Run start timestamp must be timezone-aware")
        identifier = validate_run_id(run_id) if run_id is not None else make_run_id(start)
        run_path = root / identifier
        try:
            run_path.mkdir()
        except FileExistsError as error:
            raise OutputExistsError(
                f"Refusing to reuse existing run directory: {run_path}"
            ) from error

        try:
            for directory in ("sources", "stage1", "stage2"):
                (run_path / directory).mkdir()
            manifest: dict[str, Any] = {
                "schema_version": 1,
                "run_id": identifier,
                "status": RunStatus.RUNNING.value,
                "pipeline_mode": pipeline_mode,
                "started_at": format_timestamp(start),
                "finished_at": None,
                "data_release_created": False,
                "failure": None,
                "configuration": deepcopy(configuration),
                "stages": {
                    "collection": {"status": "pending", "message": "Source collection has not started."},
                    "stage1": {"status": "pending", "message": "Stage 1 has not started."},
                    "stage2": {
                        "status": "pending",
                        "message": "Stage 2 has not started.",
                    },
                },
            }
            atomic_write_json(run_path / RUN_MANIFEST_NAME, manifest)
        except BaseException:
            # Creation did not yield a valid run.  Remove only known empty or
            # partially written bootstrap files from the directory we created.
            for child_name in (RUN_MANIFEST_NAME, "sources", "stage1", "stage2"):
                child = run_path / child_name
                if child.is_file():
                    child.unlink()
                elif child.is_dir():
                    child.rmdir()
            run_path.rmdir()
            raise
        return cls(run_id=identifier, path=run_path, _manifest=manifest)

    @property
    def manifest_path(self) -> Path:
        return self.path / RUN_MANIFEST_NAME

    @property
    def log_path(self) -> Path:
        return self.path / RUN_LOG_NAME

    @property
    def status(self) -> RunStatus:
        return RunStatus(self._manifest["status"])

    @property
    def manifest(self) -> dict[str, Any]:
        """Return a defensive copy of the current manifest."""

        return deepcopy(self._manifest)

    def artifact_path(self, relative_path: str | PurePosixPath) -> Path:
        """Resolve a safe artifact path within this run."""

        relative = PurePosixPath(relative_path)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ValueError(f"Artifact path must stay within the run directory: {relative_path}")
        destination = self.path.joinpath(*relative.parts)
        if destination == self.manifest_path or destination == self.log_path:
            raise ValueError(f"Reserved run path cannot be used as an artifact: {relative_path}")
        return destination

    def write_json_artifact(self, relative_path: str, value: Any) -> Path:
        """Write a new immutable JSON artifact below an existing run subdirectory."""

        self._require_running()
        return atomic_write_json(self.artifact_path(relative_path), value)

    def set_stage(
        self,
        name: str,
        status: str,
        message: str,
        *,
        summary: dict[str, Any] | None = None,
    ) -> None:
        """Atomically record progress for one declared pipeline stage."""

        self._require_running()
        if name not in _STAGE_NAMES:
            raise ValueError(f"Unknown pipeline stage: {name}")
        if status not in {"pending", "running", "complete", "review_required", "failed", "deferred"}:
            raise ValueError(f"Unsupported stage status: {status}")
        if type(message) is not str or not message:
            raise ValueError("Stage message must be a non-empty string")
        updated = deepcopy(self._manifest)
        stage: dict[str, Any] = {"status": status, "message": message}
        if summary is not None:
            if type(summary) is not dict:
                raise TypeError("Stage summary must be a dictionary")
            stage["summary"] = deepcopy(summary)
        updated["stages"][name] = stage
        atomic_write_json(self.manifest_path, updated, overwrite=True)
        self._manifest = updated

    def complete(
        self,
        *,
        finished_at: datetime | None = None,
        data_release_created: bool = False,
    ) -> None:
        """Finalize a successful pipeline execution."""

        self._finalize(
            RunStatus.COMPLETE,
            finished_at=finished_at,
            failure=None,
            data_release_created=data_release_created,
        )

    def require_review(self, *, finished_at: datetime | None = None) -> None:
        """Finalize a valid run whose evidence requires human review."""

        self._finalize(
            RunStatus.REVIEW_REQUIRED,
            finished_at=finished_at,
            failure=None,
            data_release_created=False,
        )

    def fail(
        self,
        error: BaseException,
        *,
        finished_at: datetime | None = None,
    ) -> None:
        """Finalize a failed run with a JSON-safe exception summary."""

        self._finalize(
            RunStatus.FAILED,
            finished_at=finished_at,
            failure={"type": type(error).__name__, "message": str(error)},
            data_release_created=False,
        )

    def _require_running(self) -> None:
        if self.status is not RunStatus.RUNNING:
            raise RunLifecycleError(
                f"Run {self.run_id} is {self.status.value}; expected running"
            )

    def _finalize(
        self,
        status: RunStatus,
        *,
        finished_at: datetime | None,
        failure: dict[str, str] | None,
        data_release_created: bool,
    ) -> None:
        self._require_running()
        if status is RunStatus.RUNNING:
            raise ValueError("Final status cannot be running")
        end = finished_at or utc_now()
        if end.tzinfo is None or end.utcoffset() is None:
            raise ValueError("Run finish timestamp must be timezone-aware")
        updated = deepcopy(self._manifest)
        updated["status"] = status.value
        updated["finished_at"] = format_timestamp(end)
        updated["failure"] = failure
        updated["data_release_created"] = data_release_created
        atomic_write_json(self.manifest_path, updated, overwrite=True)
        self._manifest = updated
