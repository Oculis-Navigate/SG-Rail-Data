"""Command-line interface for sg-rail-station-data."""

from __future__ import annotations

import argparse
from datetime import date
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .audit import (
    apply_decision,
    audit_status,
    promote_coordinate_draft,
    propose_decision,
    record_decision_action,
    render_signoffs,
    validate_initial_ledger,
)
from .config import DEFAULT_CONFIG_PATH, PipelineConfig, load_config
from .credentials import DEFAULT_ENV_PATH, load_lta_api_key
from .network_geometry import build_network_preview
from .pipeline import run_pipeline
from .progress import TerminalProgress
from .review_bundle import write_review_bundle
from .review_server import ReviewStore, serve_review_app, validate_network_preview
from .sources.lta_bus_stops import collect_lta_bus_stops
from .sources.sgwiki_mapdata import collect_sgwiki_mapdata
from .stage1 import build_stage1_reviewed_links, load_reviewed_decisions
from .storage import atomic_write_json, read_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sg-rail-station-data",
        description="Build deterministic Singapore MRT and LRT station data.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    audit_parser = commands.add_parser("audit", help="Inspect and update the intervention sign-off ledger.")
    audit_commands = audit_parser.add_subparsers(dest="audit_command", required=True)
    audit_validate = audit_commands.add_parser("validate", help="Validate hashes, coverage and recorded sign-offs.")
    _add_config_argument(audit_validate)
    audit_validate.add_argument("--json", action="store_true")
    audit_validate.set_defaults(handler=_validate_audit_command)
    audit_status_parser = audit_commands.add_parser("status", help="Show each decision's effective status.")
    _add_config_argument(audit_status_parser)
    audit_status_parser.add_argument("--json", action="store_true")
    audit_status_parser.set_defaults(handler=_audit_status_command)

    audit_propose = audit_commands.add_parser("propose", help="Record a strict decision file for review.")
    _add_config_argument(audit_propose)
    audit_propose.add_argument("--decision-file", type=Path, required=True)
    audit_propose.add_argument("--recorded-by", required=True)
    audit_propose.add_argument("--note", required=True)
    audit_propose.add_argument("--json", action="store_true")
    audit_propose.set_defaults(handler=_audit_propose_command)

    audit_promote = audit_commands.add_parser(
        "promote-coordinate-draft",
        help="Convert one saved map draft to a strict pending audit decision.",
    )
    _add_config_argument(audit_promote)
    audit_promote.add_argument("--draft-file", type=Path, required=True)
    audit_promote.add_argument("--decision-id", required=True)
    audit_promote.add_argument(
        "--bundle-file", type=Path, required=True,
        help="Exact review bundle used when the draft was saved.",
    )
    audit_promote.add_argument(
        "--stage1-file", type=Path, required=True,
        help="Exact Stage 1 stations.json named by the review bundle.",
    )
    audit_promote.add_argument(
        "--evidence-file",
        type=Path,
        action="append",
        default=[],
        help="Retain and hash a local source file. Repeat for more than one file.",
    )
    audit_promote.add_argument("--recorded-by", required=True)
    audit_promote.add_argument("--note", required=True)
    audit_promote.add_argument("--json", action="store_true")
    audit_promote.set_defaults(handler=_audit_promote_coordinate_draft_command)

    audit_apply = audit_commands.add_parser(
        "apply",
        help="Apply an approved reviewed-coordinate decision with rollback.",
    )
    _add_config_argument(audit_apply)
    audit_apply.add_argument("decision_id")
    audit_apply.add_argument("--recorded-by", required=True)
    audit_apply.add_argument("--note", required=True)
    audit_apply.add_argument("--json", action="store_true")
    audit_apply.set_defaults(handler=_audit_apply_command)

    for operation in ("approve", "reject", "revalidate", "replace", "retire"):
        action = audit_commands.add_parser(operation, help=f"{operation.capitalize()} an audit decision.")
        _add_config_argument(action)
        action.add_argument("decision_id")
        action.add_argument("--recorded-by", required=True)
        action.add_argument("--approved-by", required=True)
        action.add_argument("--authorization-basis", required=True)
        action.add_argument("--authorization-ref", action="append", required=True)
        action.add_argument("--note", required=True)
        if operation == "replace":
            action.add_argument("--with-decision-id", required=True)
        action.add_argument("--json", action="store_true")
        action.set_defaults(handler=_audit_action_command, audit_operation=operation)

    audit_render = audit_commands.add_parser("render", help="Generate dated Markdown from the JSONL ledger.")
    _add_config_argument(audit_render)
    audit_render.add_argument("--json", action="store_true")
    audit_render.set_defaults(handler=_audit_render_command)

    config_parser = commands.add_parser("config", help="Validate pipeline configuration.")
    config_commands = config_parser.add_subparsers(dest="config_command", required=True)
    validate_parser = config_commands.add_parser(
        "validate",
        help="Load the configuration and reject any unknown or invalid value.",
    )
    _add_config_argument(validate_parser)
    validate_parser.add_argument("--json", action="store_true", help="Print JSON output.")
    validate_parser.set_defaults(handler=_validate_config_command)

    credentials_parser = commands.add_parser(
        "credentials", help="Validate credentials without displaying secret values."
    )
    credentials_commands = credentials_parser.add_subparsers(
        dest="credentials_command", required=True
    )
    credentials_validate_parser = credentials_commands.add_parser(
        "validate",
        help="Validate LTA_API_KEY from the environment or repository .env.",
    )
    credentials_validate_parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_PATH,
        help=f"Local fallback file (default: {DEFAULT_ENV_PATH}).",
    )
    credentials_validate_parser.add_argument(
        "--json", action="store_true", help="Print JSON output."
    )
    credentials_validate_parser.set_defaults(handler=_validate_credentials_command)

    sources_parser = commands.add_parser(
        "sources", help="Collect and validate immutable source snapshots."
    )
    source_commands = sources_parser.add_subparsers(
        dest="sources_command", required=True
    )
    bus_stops_parser = source_commands.add_parser(
        "collect-lta-bus-stops",
        help="Download every LTA BusStops page into a new immutable directory.",
    )
    _add_config_argument(bus_stops_parser)
    bus_stops_parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_PATH,
        help=f"Local credential fallback (default: {DEFAULT_ENV_PATH}).",
    )
    bus_stops_parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="New output directory; the command refuses to reuse an existing path.",
    )
    bus_stops_parser.add_argument(
        "--json", action="store_true", help="Print JSON output."
    )
    bus_stops_parser.add_argument(
        "--no-progress", action="store_true", help="Disable terminal progress output."
    )
    bus_stops_parser.set_defaults(handler=_collect_lta_bus_stops_command)

    mapdata_parser = source_commands.add_parser(
        "collect-sgwiki-mapdata",
        help="Download revision-pinned Kartographer overlays from a saved SgWiki collection.",
    )
    _add_config_argument(mapdata_parser)
    mapdata_parser.add_argument(
        "--sgwiki-directory",
        type=Path,
        required=True,
        help="Saved SgWiki source directory containing snapshot.json, collection.json and rendered/.",
    )
    mapdata_parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="New output directory; the command refuses to reuse an existing path.",
    )
    mapdata_parser.add_argument(
        "--json", action="store_true", help="Print JSON output."
    )
    mapdata_parser.add_argument(
        "--no-progress", action="store_true", help="Disable terminal progress output."
    )
    mapdata_parser.set_defaults(handler=_collect_sgwiki_mapdata_command)

    network_parser = commands.add_parser(
        "network", help="Build preview rail-network geometry from LTA GTFS and OSM."
    )
    network_commands = network_parser.add_subparsers(dest="network_command", required=True)
    network_preview = network_commands.add_parser(
        "build-preview",
        help="Create an unapproved Stage 3 candidate and a temporary local map.",
    )
    _add_config_argument(network_preview)
    network_preview.add_argument(
        "--gtfs-directory", type=Path, required=True,
        help="Retained LTA GTFS source directory containing the ZIP and active-stations.json.",
    )
    network_preview.add_argument(
        "--output-dir", type=Path, required=True,
        help="New immutable preview directory; an existing directory is never reused.",
    )
    network_preview.add_argument("--replay-reviewed-osm", action="store_true",
                                 help="Use retained October 2026 OSM responses instead of live collection.")
    network_preview.add_argument("--json", action="store_true")
    network_preview.set_defaults(handler=_build_network_preview_command)

    stage1_parser = commands.add_parser(
        "stage1", help="Build and validate Stage 1 semantic records."
    )
    stage1_commands = stage1_parser.add_subparsers(
        dest="stage1_command", required=True
    )
    reviewed_links_parser = stage1_commands.add_parser(
        "resolve-reviewed-links",
        help="Apply reviewed exit-scope and bus-to-exit decisions to parsed SgWiki rows.",
    )
    _add_config_argument(reviewed_links_parser)
    reviewed_links_parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Parsed SgWiki station input using the strict Stage 1 input contract.",
    )
    reviewed_links_parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New output JSON file; an existing file is never replaced.",
    )
    reviewed_links_parser.add_argument(
        "--json", action="store_true", help="Print a JSON command summary."
    )
    reviewed_links_parser.set_defaults(handler=_resolve_reviewed_links_command)

    review_parser = commands.add_parser(
        "review", help="Build and open a run-specific coordinate review."
    )
    review_commands = review_parser.add_subparsers(dest="review_command", required=True)
    review_build = review_commands.add_parser(
        "build", help="Build an immutable map-review bundle from one pipeline run."
    )
    _add_config_argument(review_build)
    review_build.add_argument("--run-dir", type=Path, required=True)
    review_build.add_argument("--output", type=Path, required=True)
    review_build.add_argument(
        "--approved-stage2", type=Path,
        help="Approved Stage 2 stations.json. Omit it when no approved baseline exists.",
    )
    review_build.add_argument("--json", action="store_true")
    review_build.set_defaults(handler=_build_review_command)

    review_serve = review_commands.add_parser(
        "serve", help="Serve the local map and draft-decision API."
    )
    _add_config_argument(review_serve)
    review_serve.add_argument("--bundle", type=Path, required=True)
    review_serve.add_argument("--drafts-dir", type=Path)
    review_serve.add_argument("--static-dir", type=Path)
    review_serve.add_argument("--network-preview", type=Path, help="Mount a built Stage 3 preview at /stage3/.")
    review_serve.add_argument("--bind", default="127.0.0.1")
    review_serve.add_argument("--port", type=int, default=8000)
    review_serve.add_argument(
        "--check", action="store_true",
        help="Validate the bundle and draft directory, then exit.",
    )
    review_serve.set_defaults(handler=_serve_review_command)

    pipeline_parser = commands.add_parser("pipeline", help="Run the data pipeline.")
    pipeline_commands = pipeline_parser.add_subparsers(
        dest="pipeline_command",
        required=True,
    )
    run_parser = pipeline_commands.add_parser(
        "run",
        help="Collect or replay sources and build deterministic Stage 1 and Stage 2 artifacts.",
    )
    _add_config_argument(run_parser)
    run_parser.add_argument(
        "--runs-dir",
        type=Path,
        help="Override the configured immutable run directory (primarily for testing).",
    )
    run_parser.add_argument(
        "--run-id",
        help="Explicit run ID for reproducible testing; fails if it already exists.",
    )
    run_parser.add_argument(
        "--service-date",
        type=date.fromisoformat,
        help="GTFS service date in YYYY-MM-DD form (default: today in Singapore).",
    )
    run_parser.add_argument(
        "--replay-sources",
        type=Path,
        help="Prior run or sources directory containing the four native snapshots.",
    )
    run_parser.add_argument(
        "--baseline",
        type=Path,
        help=(
            "Override the configured approved Stage 1 stations.json used for "
            "change detection."
        ),
    )
    run_parser.add_argument(
        "--stage2-baseline",
        type=Path,
        help="Override the configured approved Stage 2 baseline used for change detection.",
    )
    run_parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_PATH,
        help=f"Local LTA credential fallback for live collection (default: {DEFAULT_ENV_PATH}).",
    )
    run_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the run result as JSON.",
    )
    run_parser.add_argument(
        "--no-progress", action="store_true", help="Disable terminal progress output."
    )
    run_parser.set_defaults(handler=_run_pipeline_command)
    return parser


def _add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Pipeline TOML file (default: {DEFAULT_CONFIG_PATH}).",
    )


def _config_summary(config: PipelineConfig) -> dict[str, object]:
    return {
        "status": "valid",
        "config": str(config.config_path),
        "config_sha256": config.config_sha256,
        "project": config.project.name,
        "schema_version": config.project.schema_version,
        "runs_dir": str(config.paths.runs_dir),
        "station_catalog": str(config.paths.station_catalog),
        "timezone": config.locale.timezone,
        "expected_station_count": config.inventory.expected_station_count,
    }


def _validate_audit_command(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.config)
    summary = validate_initial_ledger(config.config_path.parent.parent)
    if arguments.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        print(f"Initial audit {summary['status']}: {summary['decision_count']} decisions, "
              f"{summary['intervention_count']} intervention records")
        print(f"Approved decisions: {summary['approved_count']}")
        print("Pending: " + (", ".join(summary['pending_decision_ids']) or "none"))
        print("This is an intervention audit, not release approval.")
    return 2 if summary["pending_decision_ids"] else 0


def _audit_status_command(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.config)
    summary = audit_status(config.config_path.parent.parent)
    if arguments.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        print(f"Audit status: {summary['decision_count']} decisions; {summary['event_count']} events")
        for status, decision_ids in summary["effective_states"].items():
            print(f"{status}: " + (", ".join(decision_ids) or "none"))
        print("This is an intervention audit, not release approval.")
    return 0


def _audit_propose_command(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.config)
    result = propose_decision(
        config.config_path.parent.parent,
        arguments.decision_file,
        recorded_by=arguments.recorded_by,
        note=arguments.note,
    )
    summary = {
        "status": "pending_review",
        "decision_id": result["event"]["decision_id"],
        "event_id": result["event"]["event_id"],
        "ledger_head": result["audit"]["ledger_head"],
    }
    if arguments.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        print(f"Decision proposed: {summary['decision_id']} ({summary['event_id']})")
        print("Human sign-off is required.")
    return 0


def _audit_promote_coordinate_draft_command(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.config)
    result = promote_coordinate_draft(
        config.config_path.parent.parent,
        arguments.draft_file,
        arguments.decision_id,
        bundle_file=arguments.bundle_file,
        stage1_file=arguments.stage1_file,
        recorded_by=arguments.recorded_by,
        note=arguments.note,
        evidence_files=arguments.evidence_file,
    )
    summary = {
        "status": "pending_review",
        "decision_id": result["event"]["decision_id"],
        "event_id": result["event"]["event_id"],
        "ledger_head": result["audit"]["ledger_head"],
    }
    if arguments.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        print(f"Coordinate draft promoted: {summary['decision_id']} ({summary['event_id']})")
        print("The draft is pending. This command did not change coordinate configuration.")
    return 0


def _audit_apply_command(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.config)
    result = apply_decision(
        config.config_path.parent.parent,
        arguments.decision_id,
        recorded_by=arguments.recorded_by,
        note=arguments.note,
    )
    summary = {
        "status": "applied",
        "decision_id": result["event"]["decision_id"],
        "event_id": result["event"]["event_id"],
        "ledger_head": result["audit"]["ledger_head"],
    }
    if arguments.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        print(f"Decision applied: {summary['decision_id']} ({summary['event_id']})")
        print("Reviewed-coordinate ledger and GeoJSON validation passed.")
    return 0


def _audit_action_command(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.config)
    result = record_decision_action(
        config.config_path.parent.parent,
        arguments.audit_operation,
        arguments.decision_id,
        recorded_by=arguments.recorded_by,
        approved_by=arguments.approved_by,
        authorization_basis=arguments.authorization_basis,
        authorization_refs=arguments.authorization_ref,
        note=arguments.note,
        related_decision_id=getattr(arguments, "with_decision_id", None),
    )
    summary = {
        "status": result["event"]["disposition"],
        "operation": result["event"]["operation"],
        "decision_id": result["event"]["decision_id"],
        "event_id": result["event"]["event_id"],
        "ledger_head": result["audit"]["ledger_head"],
    }
    if arguments.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        print(
            f"Decision {summary['operation']}: {summary['decision_id']} "
            f"({summary['event_id']}, {summary['status']})"
        )
    return 0


def _audit_render_command(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.config)
    summary = render_signoffs(config.config_path.parent.parent)
    if arguments.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        print(f"Sign-off pages generated: {summary['date_count']} date(s)")
        print(f"Output: {summary['output_directory']}")
    return 0


def _validate_config_command(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.config)
    summary = _config_summary(config)
    if arguments.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        print(f"Configuration valid: {summary['config']}")
        print(
            f"Project {summary['project']} schema {summary['schema_version']}; "
            f"runs: {summary['runs_dir']}"
        )
    return 0


def _build_network_preview_command(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.config)
    network = config.network
    result = build_network_preview(
        gtfs_directory=arguments.gtfs_directory,
        output_directory=arguments.output_dir,
        repo_root=config.paths.repo_root,
        overpass_endpoint=config.sources.osm_overpass.endpoint,
        api_endpoint=config.sources.osm_api.endpoint,
        timeout_seconds=max(network.timeout_seconds, 120.0),
        max_attempts=network.max_attempts,
        initial_backoff_seconds=network.initial_backoff_seconds,
        maximum_backoff_seconds=network.maximum_backoff_seconds,
        minimum_interval_seconds=network.minimum_interval_seconds,
        approved_baseline_path=config.paths.stage3_approved_geometry_baseline,
        replay_reviewed_osm=arguments.replay_reviewed_osm,
    )
    summary = {
        "status": "review_required",
        "publication_allowed": False,
        "output_directory": str(result.output_directory),
        "preview": str(result.preview_path),
        "geometry": str(result.geojson_path),
        "network": str(result.network_path),
        "coverage": str(result.coverage_path),
        "changes": str(result.changes_path),
        "line_count": result.line_count,
        "station_count": result.station_count,
        "geometry_feature_count": result.feature_count,
        "geometry_change_count": result.change_count,
    }
    if arguments.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        print(
            f"Network geometry preview complete: {result.line_count} lines, "
            f"{result.station_count} stations, {result.feature_count} OSM ways, "
            f"{result.change_count} geometry changes"
        )
        print(f"Preview: {result.preview_path}")
        print("Temporary review output only; publication is not allowed.")
    return 0


def _build_review_command(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.config)
    output = write_review_bundle(
        run_directory=arguments.run_dir,
        output_path=arguments.output,
        config=config,
        approved_stage2=arguments.approved_stage2,
    )
    result = {"status": "complete", "bundle": str(output.resolve())}
    if arguments.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"Review bundle complete: {result['bundle']}")
    return 0


def _serve_review_command(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.config)
    repo_root = config.config_path.parent.parent
    drafts_directory = (
        arguments.drafts_dir.resolve()
        if arguments.drafts_dir is not None
        else (repo_root / "audit/drafts").resolve()
    )
    drafts_directory.mkdir(parents=True, exist_ok=True)
    static_directory = (
        arguments.static_dir.resolve()
        if arguments.static_dir is not None
        else (repo_root / "review_app").resolve()
    )
    if arguments.check:
        if arguments.network_preview:
            validate_network_preview(arguments.network_preview)
        state = ReviewStore(arguments.bundle, drafts_directory).state()
        print(
            f"Review bundle valid: {state['bundle']['summary']['exit_count']} exits; "
            f"{len(state['drafts'])} draft(s)"
        )
        return 0
    print(f"Coordinate review: http://{arguments.bind}:{arguments.port}/")
    print(f"Draft directory: {drafts_directory}")
    try:
        serve_review_app(
            bundle_path=arguments.bundle,
            drafts_directory=drafts_directory,
            static_directory=static_directory,
            network_preview=arguments.network_preview,
            bind=arguments.bind,
            port=arguments.port,
        )
    except KeyboardInterrupt:
        print("Review server stopped.")
    return 0


def _run_pipeline_command(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.config)
    account_key = None
    if arguments.replay_sources is None:
        account_key, _credential_source = load_lta_api_key(env_path=arguments.env_file)
    with TerminalProgress(enabled=False if arguments.no_progress else None) as progress:
        result = run_pipeline(
            config=config,
            account_key=account_key,
            service_date=arguments.service_date,
            replay_sources=arguments.replay_sources,
            baseline=arguments.baseline,
            stage2_baseline=arguments.stage2_baseline,
            runs_root=arguments.runs_dir,
            run_id=arguments.run_id,
            progress=progress.update,
        )
    output = {
        "status": result.status.value,
        "run_id": result.run_id,
        "run_directory": str(result.run_directory.resolve()),
        "manifest": str(result.manifest_path.resolve()),
        "log": str(result.log_path.resolve()),
        "data_release_created": result.data_release_created,
        "stage1": str((result.run_directory / "stage1/stations.json").resolve()),
        "stage1_reviews": str((result.run_directory / "stage1/reviews.json").resolve()),
        "stage1_changes": str((result.run_directory / "stage1/changes.json").resolve()),
        "stage2": str((result.run_directory / "stage2/stations.json").resolve()),
        "stage2_geojson": str((result.run_directory / "stage2/exits.geojson").resolve()),
        "stage2_reviews": str((result.run_directory / "stage2/reviews.json").resolve()),
        "stage2_changes": str((result.run_directory / "stage2/changes.json").resolve()),
        "exit_code": result.exit_code,
    }
    if arguments.json:
        print(json.dumps(output, sort_keys=True))
    else:
        print(f"Pipeline run {result.status.value}: {result.run_id}")
        print(f"Run directory: {output['run_directory']}")
        print(f"Stage 1: {output['stage1']}")
        print(f"Stage 2: {output['stage2']}")
        if result.status.value == "review_required":
            print(
                "Human review required: "
                f"{output['stage1_reviews']}, {output['stage1_changes']}, "
                f"{output['stage2_reviews']} and {output['stage2_changes']}"
            )
    return result.exit_code


def _validate_credentials_command(arguments: argparse.Namespace) -> int:
    _key, source = load_lta_api_key(env_path=arguments.env_file)
    summary = {
        "status": "valid",
        "credential": "LTA_API_KEY",
        "source": source,
    }
    if arguments.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        print(f"Credential valid: LTA_API_KEY from {source}")
    return 0


def _collect_lta_bus_stops_command(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.config)
    account_key, _credential_source = load_lta_api_key(env_path=arguments.env_file)
    network = config.network
    with TerminalProgress(enabled=False if arguments.no_progress else None) as progress:
        result = collect_lta_bus_stops(
            endpoint=config.sources.lta_bus_stops.endpoint,
            account_key=account_key,
            output_directory=arguments.output_dir,
            timeout_seconds=network.timeout_seconds,
            max_attempts=network.max_attempts,
            initial_backoff_seconds=network.initial_backoff_seconds,
            maximum_backoff_seconds=network.maximum_backoff_seconds,
            minimum_interval_seconds=network.minimum_interval_seconds,
            progress=progress.update,
        )
    summary = {
        "collection": str(result.manifest_path.resolve()),
        "output_directory": str(result.output_directory.resolve()),
        "page_count": result.page_count,
        "record_count": result.record_count,
        "snapshot": str(result.snapshot_path.resolve()),
        "status": "complete",
    }
    if arguments.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        print(
            f"LTA BusStops collection complete: {result.record_count} records "
            f"across {result.page_count} responses"
        )
        print(f"Output directory: {summary['output_directory']}")
    return 0


def _collect_sgwiki_mapdata_command(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.config)
    network = config.network
    with TerminalProgress(enabled=False if arguments.no_progress else None) as progress:
        result = collect_sgwiki_mapdata(
            endpoint=config.sources.sgwiki_mapdata.endpoint,
            sgwiki_directory=arguments.sgwiki_directory,
            output_directory=arguments.output_dir,
            timeout_seconds=network.timeout_seconds,
            max_attempts=network.max_attempts,
            initial_backoff_seconds=network.initial_backoff_seconds,
            maximum_backoff_seconds=network.maximum_backoff_seconds,
            minimum_interval_seconds=network.minimum_interval_seconds,
            progress=progress.update,
        )
    summary = {
        "status": "complete",
        "output_directory": str(result.output_directory.resolve()),
        "snapshot": str(result.snapshot_path.resolve()),
        "collection": str(result.manifest_path.resolve()),
        "station_count": len(result.stations),
        "embedded_map_count": len(result.stations) - len(result.no_map_station_names),
        "no_embedded_map_count": len(result.no_map_station_names),
    }
    if arguments.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        print(
            "SgWiki MapData collection complete: "
            f"{summary['embedded_map_count']} maps; "
            f"{summary['no_embedded_map_count']} pages without maps"
        )
        print(f"Output directory: {summary['output_directory']}")
    return 0


def _resolve_reviewed_links_command(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.config)
    decisions = load_reviewed_decisions(config.paths.stage1_reviewed_decisions)
    source_document = read_json(arguments.input)
    result = build_stage1_reviewed_links(source_document, decisions)
    output_path = atomic_write_json(arguments.output, result)
    summary = {
        "status": "complete",
        "station_count": len(result["stations"]),
        "output": str(output_path.resolve()),
        "reviewed_decisions": str(decisions.path),
        "reviewed_decisions_sha256": decisions.sha256,
    }
    if arguments.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        print(f"Stage 1 reviewed links complete: {summary['station_count']} stations")
        print(f"Output: {summary['output']}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments and return a process exit code."""

    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except (OSError, RuntimeError, ValueError) as error:
        print(f"sg-rail-station-data: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
