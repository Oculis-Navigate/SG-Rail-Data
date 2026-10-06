"""Top-level orchestration for the rail station data pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Callable

from . import __version__
from .config import PipelineConfig
from .progress import ProgressCallback
from .run import RunContext, RunStatus
from .sources.lta_bus_stops import (
    BusStopsCollectionResult,
    collect_lta_bus_stops,
    load_lta_bus_stops_snapshot,
)
from .sources.lta_gtfs import GtfsCollectionResult, collect_lta_gtfs
from .sources.sgwiki import SgWikiCollectionResult, collect_sgwiki
from .sources.sgwiki_mapdata import (
    SgWikiMapDataCollectionResult,
    collect_sgwiki_mapdata,
)
from .stage1 import load_reviewed_decisions
from .stage1_semantics import build_full_stage1, compare_stage1
from .stage2 import build_stage2, compare_stage2
from .station_catalog import load_station_catalog
from .storage import atomic_write_bytes, atomic_write_json, read_json
from .structured_logging import StructuredLogger


class PipelineValidationError(RuntimeError):
    """Raised when valid source data contains a blocking semantic change."""


@dataclass(frozen=True, slots=True)
class PipelineResult:
    run_id: str
    run_directory: Path
    manifest_path: Path
    log_path: Path
    status: RunStatus
    data_release_created: bool

    @property
    def exit_code(self) -> int:
        if self.status is RunStatus.COMPLETE:
            return 0
        if self.status is RunStatus.REVIEW_REQUIRED:
            return 2
        return 1


def _result(run: RunContext) -> PipelineResult:
    return PipelineResult(
        run_id=run.run_id,
        run_directory=run.path,
        manifest_path=run.manifest_path,
        log_path=run.log_path,
        status=run.status,
        data_release_created=bool(run.manifest["data_release_created"]),
    )


def _sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _replay_root(path: str | Path) -> Path:
    requested = Path(path).expanduser().resolve()
    nested = requested / "sources"
    root = nested if nested.is_dir() else requested
    expected = {
        "lta_gtfs": root / "lta-gtfs" / "active-stations.json",
        "lta_bus_stops": root / "lta-bus-stops" / "snapshot.json",
        "sgwiki": root / "sgwiki" / "snapshot.json",
        "sgwiki_mapdata": root / "sgwiki-mapdata" / "snapshot.json",
    }
    missing = [f"{name}: {source}" for name, source in expected.items() if not source.is_file()]
    if missing:
        raise PipelineValidationError(
            "Replay source bundle is incomplete; missing " + "; ".join(missing)
        )
    return root


def _copy_replay_snapshot(source: Path, destination: Path) -> None:
    destination.parent.mkdir()
    atomic_write_bytes(destination, source.read_bytes())
    atomic_write_json(
        destination.parent / "replay.json",
        {
            "schema_version": 1,
            "kind": "replayed_source_snapshot",
            "source_path": str(source),
            "source_sha256": _sha256(source),
            "snapshot_file": destination.name,
        },
    )


def _source_revisions(dataset: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "sgwiki_station_revisions",
        "stations": [
            {
                "station_id": station["id"],
                "station_name": station["name"],
                "page_id": station["source"]["page_id"],
                "revision_id": station["source"]["revision_id"],
                "revision_url": station["source"]["revision_url"],
            }
            for station in dataset["stations"]
        ],
    }


def run_pipeline(
    *,
    config: PipelineConfig,
    account_key: str | None = None,
    service_date: date | None = None,
    replay_sources: str | Path | None = None,
    baseline: str | Path | None = None,
    stage2_baseline: str | Path | None = None,
    runs_root: str | Path | None = None,
    run_id: str | None = None,
    gtfs_collector: Callable[..., GtfsCollectionResult] = collect_lta_gtfs,
    bus_stops_collector: Callable[..., BusStopsCollectionResult] = collect_lta_bus_stops,
    sgwiki_collector: Callable[..., SgWikiCollectionResult] = collect_sgwiki,
    mapdata_collector: Callable[..., SgWikiMapDataCollectionResult] = collect_sgwiki_mapdata,
    progress: ProgressCallback | None = None,
) -> PipelineResult:
    """Collect or replay sources and build immutable Stage 1 and Stage 2 data."""

    output_root = Path(runs_root) if runs_root is not None else config.paths.runs_dir
    selected_date = service_date or datetime.now(config.locale.zoneinfo).date()
    if type(selected_date) is not date:
        raise TypeError("service_date must be a date")
    selected_stage1_baseline = (
        Path(baseline).expanduser().resolve()
        if baseline is not None
        else config.paths.stage1_approved_baseline
    )
    baseline_metadata = {
        "path": str(selected_stage1_baseline),
        "status": "available" if selected_stage1_baseline.is_file() else "unavailable",
        "sha256": _sha256(selected_stage1_baseline)
        if selected_stage1_baseline.is_file()
        else None,
    }
    selected_stage2_baseline = (
        Path(stage2_baseline).expanduser().resolve()
        if stage2_baseline is not None
        else config.paths.stage2_approved_baseline
    )
    stage2_baseline_metadata = {
        "path": str(selected_stage2_baseline)
        if selected_stage2_baseline is not None
        else None,
        "status": (
            "available"
            if selected_stage2_baseline is not None
            and selected_stage2_baseline.is_file()
            else "unavailable"
        ),
        "sha256": (
            _sha256(selected_stage2_baseline)
            if selected_stage2_baseline is not None
            and selected_stage2_baseline.is_file()
            else None
        ),
    }
    run = RunContext.create(
        output_root,
        run_id=run_id,
        pipeline_mode="full",
        configuration={
            "path": str(config.config_path),
            "project": config.project.name,
            "schema_version": config.project.schema_version,
            "sha256": config.config_sha256,
            "software_version": __version__,
            "service_date": selected_date.isoformat(),
            "source_mode": "replay" if replay_sources is not None else "live",
            "stage1_approved_baseline": baseline_metadata,
            "stage2_approved_baseline": stage2_baseline_metadata,
        },
    )
    active_stage = "collection"
    try:
        with StructuredLogger(run.log_path, context={"run_id": run.run_id}) as logger:
            try:
                logger.info(
                    "pipeline.started",
                    "Wayfinding pipeline started.",
                    source_mode="replay" if replay_sources is not None else "live",
                    service_date=selected_date.isoformat(),
                )
                run.set_stage("collection", "running", "Collecting and validating source snapshots.")
                catalog = load_station_catalog(
                    config.paths.station_catalog,
                    expected_count=config.inventory.expected_station_count,
                )
                decisions = load_reviewed_decisions(config.paths.stage1_reviewed_decisions)

                if replay_sources is None:
                    if account_key is None:
                        raise PipelineValidationError(
                            "Live collection requires an LTA_API_KEY; use replay mode for offline runs"
                        )
                    network = config.network
                    if progress is not None:
                        progress("LTA GTFS", 0, 1, "downloading and validating")
                    gtfs_result = gtfs_collector(
                        endpoint=config.sources.lta_gtfs.endpoint,
                        account_key=account_key,
                        service_date=selected_date,
                        output_directory=run.path / "sources/lta-gtfs",
                        timeout_seconds=network.timeout_seconds,
                        max_attempts=network.max_attempts,
                        initial_backoff_seconds=network.initial_backoff_seconds,
                        maximum_backoff_seconds=network.maximum_backoff_seconds,
                    )
                    if progress is not None:
                        progress("LTA GTFS", 1, 1, "validated")
                    bus_result = bus_stops_collector(
                        endpoint=config.sources.lta_bus_stops.endpoint,
                        account_key=account_key,
                        output_directory=run.path / "sources/lta-bus-stops",
                        timeout_seconds=network.timeout_seconds,
                        max_attempts=network.max_attempts,
                        initial_backoff_seconds=network.initial_backoff_seconds,
                        maximum_backoff_seconds=network.maximum_backoff_seconds,
                        minimum_interval_seconds=network.minimum_interval_seconds,
                        progress=progress,
                    )
                    wiki_result = sgwiki_collector(
                        endpoint=config.sources.sgwiki_api.endpoint,
                        catalog=catalog,
                        output_directory=run.path / "sources/sgwiki",
                        timeout_seconds=network.timeout_seconds,
                        max_attempts=network.max_attempts,
                        initial_backoff_seconds=network.initial_backoff_seconds,
                        maximum_backoff_seconds=network.maximum_backoff_seconds,
                        minimum_interval_seconds=network.minimum_interval_seconds,
                        progress=progress,
                    )
                    mapdata_result = mapdata_collector(
                        endpoint=config.sources.sgwiki_mapdata.endpoint,
                        sgwiki_directory=wiki_result.output_directory,
                        output_directory=run.path / "sources/sgwiki-mapdata",
                        timeout_seconds=network.timeout_seconds,
                        max_attempts=network.max_attempts,
                        initial_backoff_seconds=network.initial_backoff_seconds,
                        maximum_backoff_seconds=network.maximum_backoff_seconds,
                        minimum_interval_seconds=network.minimum_interval_seconds,
                        progress=progress,
                    )
                    gtfs_inventory = gtfs_result.inventory
                    bus_stops = bus_result.records
                    sgwiki_snapshot = read_json(wiki_result.snapshot_path)
                    mapdata_snapshot = read_json(mapdata_result.snapshot_path)
                    source_summary = {
                        "mode": "live",
                        "gtfs_active_station_records": gtfs_inventory["active_station_count"],
                        "lta_bus_stop_records": bus_result.record_count,
                        "sgwiki_station_pages": len(wiki_result.stations),
                        "sgwiki_partial_pages": len(wiki_result.partial_station_names),
                        "sgwiki_embedded_maps": (
                            len(mapdata_result.stations)
                            - len(mapdata_result.no_map_station_names)
                        ),
                        "sgwiki_pages_without_embedded_maps": len(
                            mapdata_result.no_map_station_names
                        ),
                    }
                else:
                    if progress is not None:
                        progress("Replay sources", 0, 1, "copying and validating")
                    source_root = _replay_root(replay_sources)
                    gtfs_source = source_root / "lta-gtfs/active-stations.json"
                    bus_source = source_root / "lta-bus-stops/snapshot.json"
                    wiki_source = source_root / "sgwiki/snapshot.json"
                    mapdata_source = source_root / "sgwiki-mapdata/snapshot.json"
                    gtfs_path = run.path / "sources/lta-gtfs/active-stations.json"
                    bus_path = run.path / "sources/lta-bus-stops/snapshot.json"
                    wiki_path = run.path / "sources/sgwiki/snapshot.json"
                    mapdata_path = run.path / "sources/sgwiki-mapdata/snapshot.json"
                    _copy_replay_snapshot(gtfs_source, gtfs_path)
                    _copy_replay_snapshot(bus_source, bus_path)
                    _copy_replay_snapshot(wiki_source, wiki_path)
                    _copy_replay_snapshot(mapdata_source, mapdata_path)
                    gtfs_inventory = read_json(gtfs_path)
                    bus_stops = load_lta_bus_stops_snapshot(bus_path)
                    sgwiki_snapshot = read_json(wiki_path)
                    mapdata_snapshot = read_json(mapdata_path)
                    source_summary = {
                        "mode": "replay",
                        "gtfs_snapshot_sha256": _sha256(gtfs_path),
                        "lta_bus_stops_snapshot_sha256": _sha256(bus_path),
                        "sgwiki_snapshot_sha256": _sha256(wiki_path),
                        "sgwiki_mapdata_snapshot_sha256": _sha256(mapdata_path),
                    }
                    if progress is not None:
                        progress("Replay sources", 1, 1, "validated")

                if gtfs_inventory.get("service_date") != selected_date.isoformat():
                    raise PipelineValidationError(
                        "GTFS snapshot service_date does not match the requested service date: "
                        f"{gtfs_inventory.get('service_date')!r} != {selected_date.isoformat()!r}"
                    )
                run.set_stage(
                    "collection",
                    "complete",
                    "All source snapshots were retained and passed their source contracts.",
                    summary=source_summary,
                )
                logger.info("collection.completed", "Source collection and validation completed.", **source_summary)

                active_stage = "stage1"
                if progress is not None:
                    progress("Stage 1", 0, 1, "building station semantics")
                run.set_stage("stage1", "running", "Building deterministic Stage 1 semantics.")
                build = build_full_stage1(
                    sgwiki_snapshot=sgwiki_snapshot,
                    gtfs_inventory=gtfs_inventory,
                    bus_stops=bus_stops,
                    catalog=catalog,
                    decisions=decisions,
                )
                baseline_document = (
                    read_json(selected_stage1_baseline)
                    if selected_stage1_baseline.is_file()
                    else None
                )
                changes = compare_stage1(build.dataset, baseline_document)
                atomic_write_json(run.path / "stage1/stations.json", build.dataset)
                atomic_write_json(run.path / "stage1/reviews.json", build.reviews)
                atomic_write_json(run.path / "stage1/changes.json", changes)
                atomic_write_json(run.path / "stage1/source-revisions.json", _source_revisions(build.dataset))
                atomic_write_json(
                    run.path / "stage1/required-bus-stops.json",
                    {
                        "schema_version": 1,
                        "kind": "stage1_required_lta_bus_stops",
                        "count": len(build.required_bus_stops),
                        "bus_stops": [record.as_source_dict() for record in build.required_bus_stops],
                    },
                )
                if progress is not None:
                    progress("Stage 1", 1, 1, "artifacts written")

                if changes["highest_severity"] in {"ERROR", "CRITICAL"}:
                    error = PipelineValidationError(
                        f"Stage 1 comparison contains {changes['highest_severity']} changes; review changes.json"
                    )
                    run.set_stage("stage1", "failed", str(error), summary=build.dataset["summary"])
                    logger.error(
                        "stage1.blocked",
                        str(error),
                        change_count=changes["count"],
                    )
                    run.fail(error)
                    return _result(run)

                stage1_requires_review = (
                    bool(build.reviews["count"])
                    or changes["highest_severity"] == "WARNING"
                )
                stage_status = "review_required" if stage1_requires_review else "complete"
                run.set_stage(
                    "stage1",
                    stage_status,
                    (
                        "Stage 1 artifacts are valid and require human review."
                        if stage1_requires_review
                        else "Stage 1 artifacts are valid and unchanged."
                    ),
                    summary=build.dataset["summary"],
                )
                log_method = logger.warning if stage1_requires_review else logger.info
                log_method(
                    "stage1.review_required" if stage1_requires_review else "stage1.completed",
                    "Stage 1 build completed.",
                    review_group_count=build.reviews["group_count"],
                    review_finding_count=build.reviews["finding_count"],
                    change_count=changes["count"],
                )

                active_stage = "stage2"
                if progress is not None:
                    progress("Stage 2", 0, 1, "resolving exit coordinates")
                run.set_stage(
                    "stage2",
                    "running",
                    "Resolving exit coordinates from Kartographer and reviewed evidence.",
                )
                reviewed_coordinates_path = config.paths.stage2_reviewed_coordinates
                reviewed_ledger_path = config.paths.stage2_coordinate_ledger
                trusted_onemap_path = config.paths.stage2_trusted_onemap_baseline
                supplemental_path = config.paths.stage2_trusted_supplemental_baseline
                reviewed_coordinates = read_json(reviewed_coordinates_path)
                reviewed_ledger = read_json(reviewed_ledger_path)
                trusted_onemap = read_json(trusted_onemap_path)
                if type(reviewed_coordinates) is not dict or type(
                    reviewed_coordinates.get("metadata")
                ) is not dict:
                    raise PipelineValidationError(
                        "Reviewed coordinate evidence has no metadata table"
                    )
                declared_ledger_sha256 = reviewed_coordinates["metadata"].get(
                    "ledger_sha256"
                )
                actual_ledger_sha256 = _sha256(reviewed_ledger_path)
                if declared_ledger_sha256 != actual_ledger_sha256:
                    raise PipelineValidationError(
                        "Reviewed coordinate evidence does not match the configured "
                        "coordinate ledger SHA-256"
                    )
                stage2_build = build_stage2(
                    build.dataset,
                    mapdata_snapshot,
                    reviewed_coordinates,
                    reviewed_ledger,
                    trusted_onemap,
                    read_json(supplemental_path),
                )
                stage2_baseline_document = (
                    read_json(selected_stage2_baseline)
                    if selected_stage2_baseline.is_file()
                    else None
                )
                stage2_changes = compare_stage2(
                    stage2_build.dataset,
                    stage2_baseline_document,
                    movement_threshold_metres=(
                        config.release.coordinate_movement_review_metres
                    ),
                )
                atomic_write_json(
                    run.path / "stage2/stations.json", stage2_build.dataset
                )
                atomic_write_json(
                    run.path / "stage2/exits.geojson", stage2_build.geojson
                )
                atomic_write_json(
                    run.path / "stage2/reviews.json", stage2_build.reviews
                )
                atomic_write_json(
                    run.path / "stage2/changes.json", stage2_changes
                )
                atomic_write_json(
                    run.path / "stage2/coverage.json", stage2_build.coverage
                )
                atomic_write_json(
                    run.path / "stage2/source-revisions.json",
                    {
                        "schema_version": 1,
                        "kind": "stage2_coordinate_source_revisions",
                        "sgwiki_mapdata_snapshot_sha256": _sha256(
                            run.path / "sources/sgwiki-mapdata/snapshot.json"
                        ),
                        "reviewed_coordinates_file": reviewed_coordinates_path.name,
                        "reviewed_coordinates_sha256": _sha256(
                            reviewed_coordinates_path
                        ),
                        "coordinate_ledger_file": reviewed_ledger_path.name,
                        "coordinate_ledger_sha256": actual_ledger_sha256,
                        "trusted_onemap_baseline_file": trusted_onemap_path.name,
                        "trusted_supplemental_baseline_file": supplemental_path.name,
                        "trusted_supplemental_baseline_sha256": _sha256(supplemental_path),
                        "trusted_onemap_baseline_sha256": _sha256(
                            trusted_onemap_path
                        ),
                    },
                )
                if progress is not None:
                    progress("Stage 2", 1, 1, "artifacts written")

                stage2_review_severity = stage2_build.reviews["highest_severity"]
                stage2_change_severity = stage2_changes["highest_severity"]
                if stage2_review_severity == "ERROR" or stage2_change_severity in {
                    "ERROR", "CRITICAL"
                }:
                    error = PipelineValidationError(
                        "Stage 2 contains blocking coordinate findings; review "
                        "stage2/reviews.json and stage2/changes.json"
                    )
                    run.set_stage(
                        "stage2",
                        "failed",
                        str(error),
                        summary=stage2_build.dataset["summary"],
                    )
                    logger.error(
                        "stage2.blocked",
                        str(error),
                        review_finding_count=stage2_build.reviews["finding_count"],
                        change_count=stage2_changes["count"],
                    )
                    run.fail(error)
                    return _result(run)

                unresolved_count = stage2_build.dataset["summary"][
                    "unresolved_exit_count"
                ]
                stage2_requires_review = (
                    stage2_review_severity == "WARNING"
                    or stage2_change_severity == "WARNING"
                    or unresolved_count
                    > config.release.max_unresolved_exit_coordinates
                )
                run.set_stage(
                    "stage2",
                    "review_required" if stage2_requires_review else "complete",
                    (
                        "Stage 2 artifacts are valid and require human review."
                        if stage2_requires_review
                        else "Every Stage 1 exit has an approved coordinate."
                    ),
                    summary=stage2_build.dataset["summary"],
                )
                stage2_log_method = (
                    logger.warning if stage2_requires_review else logger.info
                )
                stage2_log_method(
                    (
                        "stage2.review_required"
                        if stage2_requires_review
                        else "stage2.completed"
                    ),
                    (
                        "Stage 2 coordinate resolution requires human review."
                        if stage2_requires_review
                        else "Stage 2 coordinate resolution completed."
                    ),
                    unresolved_exit_count=unresolved_count,
                    review_finding_count=stage2_build.reviews["finding_count"],
                    change_count=stage2_changes["count"],
                )

                requires_review = stage1_requires_review or stage2_requires_review
                if requires_review:
                    run.require_review()
                else:
                    run.complete(data_release_created=True)
                logger.info(
                    "pipeline.completed",
                    "Wayfinding pipeline finished.",
                    status=run.status.value,
                    data_release_created=not requires_review,
                )
            except BaseException as error:
                if run.status is RunStatus.RUNNING:
                    run.set_stage(active_stage, "failed", f"{type(error).__name__}: {error}")
                    logger.critical(
                        "pipeline.failed",
                        "Pipeline stopped because an input or invariant failed.",
                        stage=active_stage,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                raise
    except BaseException as error:
        if run.status is RunStatus.RUNNING:
            run.fail(error)
        raise
    return _result(run)


__all__ = ["PipelineResult", "PipelineValidationError", "run_pipeline"]
