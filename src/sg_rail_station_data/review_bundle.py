"""Build the strict, run-specific coordinate review contract.

The browser contract is intentionally small and explicit::

    {
      "schema_version": 1,
      "kind": "sg_rail_station_data_review_bundle",
      "run": {"id", "status", "finished_at", "manifest_sha256"},
      "approved_baseline": {"status", "sha256"},
      "inputs": {"...input name...": "<sha256>"},
      "summary": {...integer counts...},
      "exits": [{
        "station_id", "station_name", "station_codes", "exit_id", "label",
        "scopes", "approved_coordinate", "current_coordinate", "candidates",
        "evidence", "findings"
      }],
      "run_reports": {"stage1_changes", "stage1_reviews",
                      "stage2_changes", "stage2_reviews"}
    }

``approved_coordinate`` is non-null only when the caller supplies an approved
Stage 2 baseline.  A candidate run is never inferred to be approved.  Every
other point is a candidate with one of ``kartographer``, ``onemap``, ``osm``,
``lta``, or ``manual`` as its source.  The UI shows candidates only after the
reviewer selects their source filter.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from .config import PipelineConfig
from .contracts import ContractError, exact_int, exact_str, require_fields
from .stage2 import (
    _reviewed_coordinates,
    _stage1_identity_index,
    _supplemental_coordinates,
    _trusted_onemap_coordinates,
    _validate_stage2_comparison_document,
)
from .storage import atomic_write_json, read_json


REPORT_FIELDS = {
    "stage1_changes": {"schema_version", "kind", "status", "count", "highest_severity", "items"},
    "stage1_reviews": {"schema_version", "kind", "status", "count", "finding_count", "group_count", "groups"},
    "stage2_changes": {"schema_version", "kind", "status", "count", "highest_severity", "items"},
    "stage2_reviews": {"schema_version", "kind", "status", "finding_count", "group_count", "groups", "highest_severity"},
}
SOURCE_NAMES = frozenset({"kartographer", "onemap", "osm", "lta", "manual"})
FROZEN_COORDINATE_EVIDENCE = Path("audit/evidence/coordinate-candidates-v1.geojson")
FROZEN_COORDINATE_EVIDENCE_SHA256 = (
    "cccac719c2a641e15df739aa943c1d3260d00d6d48ee09d6ed487b17faf66c0d"
)
FROZEN_SOURCE_NAMES = {
    "sgwiki_kartographer": "kartographer",
    "onemap": "onemap",
    "osm": "osm",
    "lta_geojson": "lta",
    "reviewed_manual": "manual",
}
FROZEN_METADATA_FIELDS = {
    "schema_version", "station_count", "exit_identity_count",
    "coordinate_candidate_count", "unresolved_exit_count", "source_counts",
    "status_counts", "stage2_file", "stage2_sha256", "bootstrap_files",
}
FROZEN_EXPERIMENTAL_FIELDS = {
    "station_id", "station_name", "station_codes", "exit_id", "label", "scope",
    "scopes", "selection_status", "approved", "review_reasons", "source",
    "candidate_index", "selected", "candidate_id", "title", "description",
    "building", "accuracy_metres", "position_kind", "provenance",
    "primary_status", "candidate_count", "source_count", "sources_for_exit",
    "duplicate_within_source", "multiple_candidates", "needs_review",
}
FROZEN_BOOTSTRAP_FIELDS = {
    "station_id", "station_name", "codes", "station_codes", "exit_id", "label",
    "scope", "classification", "candidate_source", "source", "source_record_id",
    "review_status", "approved", "position_kind", "accuracy_metres",
    "candidate_index", "candidate_count", "provenance", "scopes",
    "selection_status", "selected", "review_reasons", "primary_status",
    "source_count", "sources_for_exit", "duplicate_within_source",
    "multiple_candidates", "needs_review",
}
FROZEN_REFRESH_FIELDS = {
    "station_id", "station_name", "station_codes", "exit_id", "label", "scope",
    "scopes", "selection_status", "approved", "review_status", "review_reasons",
    "source", "candidate_index", "selected", "position_kind", "provenance",
    "primary_status", "candidate_count", "source_count", "sources_for_exit",
    "duplicate_within_source", "multiple_candidates", "needs_review",
}


def _sha256_file(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def canonical_sha256(value: object) -> str:
    """Return the digest used for browser stale-state protection."""

    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _coordinates(value: object, context: str) -> list[float]:
    if type(value) is not list or len(value) != 2:
        raise ContractError(f"{context}: expected [longitude, latitude]")
    longitude, latitude = value
    if type(longitude) is not float or type(latitude) is not float:
        raise ContractError(f"{context}: coordinates must be exact floats")
    if not 103.58 <= longitude <= 104.12 or not 1.12 <= latitude <= 1.50:
        raise ContractError(f"{context}: coordinate is outside Singapore")
    return [longitude, latitude]


def _sha256(value: object, context: str) -> str:
    digest = exact_str(value, context)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ContractError(f"{context}: expected SHA-256")
    return digest


def _string_array(value: object, context: str, *, nonempty: bool = False) -> list[str]:
    if type(value) is not list or (nonempty and not value):
        expected = "nonempty array" if nonempty else "array"
        raise ContractError(f"{context}: expected {expected}")
    return [exact_str(item, f"{context}[{index}]") for index, item in enumerate(value)]


def _validate_frozen_provenance(source: str, value: object, context: str) -> None:
    """Validate the source-specific provenance retained in the frozen artifact."""

    if type(value) is not dict:
        raise ContractError(f"{context}: expected object")
    fields = set(value)
    if source == "sgwiki_kartographer":
        full = {
            "directory", "index_sha256", "overlay_file", "overlay_sha256",
            "overlay_id", "revision_id", "mapdata_url",
        }
        refresh = {"file", "sha256", "revision_id", "feature_index", "note"}
        if fields == full:
            for field in ("directory", "overlay_file", "overlay_id"):
                exact_str(value[field], f"{context}.{field}")
            _sha256(value["index_sha256"], f"{context}.index_sha256")
            _sha256(value["overlay_sha256"], f"{context}.overlay_sha256")
            exact_int(value["revision_id"], f"{context}.revision_id", minimum=1)
            if not exact_str(value["mapdata_url"], f"{context}.mapdata_url").startswith("https://"):
                raise ContractError(f"{context}.mapdata_url: expected HTTPS URL")
        elif fields == refresh:
            exact_str(value["file"], f"{context}.file")
            _sha256(value["sha256"], f"{context}.sha256")
            exact_int(value["revision_id"], f"{context}.revision_id", minimum=1)
            exact_int(value["feature_index"], f"{context}.feature_index", minimum=0)
            exact_str(value["note"], f"{context}.note")
        else:
            raise ContractError(f"{context}: unexpected Kartographer provenance fields")
        return
    if source == "onemap":
        onemap = value
        onemap_context = context
        if fields == {"building", "provenance"}:
            exact_str(value["building"], f"{context}.building")
            if type(value["provenance"]) is not dict:
                raise ContractError(f"{context}.provenance: expected object")
            onemap = value["provenance"]
            onemap_context = f"{context}.provenance"
        if set(onemap) == {"query_scope", "response_files"}:
            exact_str(onemap["query_scope"], f"{onemap_context}.query_scope")
            files = onemap["response_files"]
            if type(files) is not list or not files:
                raise ContractError(f"{onemap_context}.response_files: expected nonempty array")
            for index, item in enumerate(files):
                row = require_fields(
                    item, {"file", "sha256", "fetched_at"},
                    f"{onemap_context}.response_files[{index}]",
                )
                exact_str(row["file"], f"{onemap_context}.response_files[{index}].file")
                _sha256(row["sha256"], f"{onemap_context}.response_files[{index}].sha256")
                exact_str(row["fetched_at"], f"{onemap_context}.response_files[{index}].fetched_at")
        else:
            raise ContractError(f"{context}: unexpected OneMap provenance fields")
        return
    if source == "lta_geojson":
        row = require_fields(
            value,
            {"source_feature_index", "source_updated_at", "source_crc", "source_station_name", "source_exit_code"},
            context,
        )
        exact_int(row["source_feature_index"], f"{context}.source_feature_index", minimum=0)
        for field in ("source_updated_at", "source_crc", "source_station_name", "source_exit_code"):
            exact_str(row[field], f"{context}.{field}")
        return
    if source == "osm":
        current = {"source_timestamp", "source_url", "source_tags", "association", "label_evidence"}
        refresh = {"source_record_id", "osm_url", "timestamp", "file", "sha256"}
        if fields == current:
            exact_str(value["source_timestamp"], f"{context}.source_timestamp")
            if not exact_str(value["source_url"], f"{context}.source_url").startswith("https://"):
                raise ContractError(f"{context}.source_url: expected HTTPS URL")
            if type(value["source_tags"]) is not dict or type(value["association"]) is not list:
                raise ContractError(f"{context}: invalid OSM tag or association data")
            exact_str(value["label_evidence"], f"{context}.label_evidence")
        elif fields == refresh:
            exact_str(value["source_record_id"], f"{context}.source_record_id")
            if not exact_str(value["osm_url"], f"{context}.osm_url").startswith("https://"):
                raise ContractError(f"{context}.osm_url: expected HTTPS URL")
            exact_str(value["timestamp"], f"{context}.timestamp")
            exact_str(value["file"], f"{context}.file")
            _sha256(value["sha256"], f"{context}.sha256")
        else:
            raise ContractError(f"{context}: unexpected OSM provenance fields")
        return
    if source == "reviewed_manual":
        row = require_fields(
            value, {"position_kind", "accuracy_metres", "method", "evidence", "notes"}, context
        )
        exact_str(row["position_kind"], f"{context}.position_kind")
        exact_int(row["accuracy_metres"], f"{context}.accuracy_metres", minimum=0)
        exact_str(row["method"], f"{context}.method")
        exact_str(row["notes"], f"{context}.notes")
        if type(row["evidence"]) is not list or not row["evidence"]:
            raise ContractError(f"{context}.evidence: expected nonempty array")
        for index, evidence in enumerate(row["evidence"]):
            if type(evidence) is not dict:
                raise ContractError(f"{context}.evidence[{index}]: expected object")
        return
    raise ContractError(f"{context}: unsupported frozen source {source!r}")


def _frozen_coordinate_candidates(
    value: object,
    current_identities: set[tuple[str, str]],
) -> tuple[dict[tuple[str, str], list[dict[str, object]]], dict[str, object]]:
    """Validate frozen v2 evidence and retain only current exit identities."""

    root = require_fields(value, {"type", "metadata", "features"}, "frozen coordinate evidence")
    if exact_str(root["type"], "frozen coordinate evidence.type") != "FeatureCollection":
        raise ContractError("frozen coordinate evidence.type: expected FeatureCollection")
    metadata = require_fields(root["metadata"], FROZEN_METADATA_FIELDS, "frozen coordinate evidence.metadata")
    if exact_int(metadata["schema_version"], "frozen coordinate evidence.metadata.schema_version") != 1:
        raise ContractError("frozen coordinate evidence.metadata.schema_version: unsupported version")
    for field in ("station_count", "exit_identity_count", "coordinate_candidate_count", "unresolved_exit_count"):
        exact_int(metadata[field], f"frozen coordinate evidence.metadata.{field}", minimum=0)
    source_counts = require_fields(
        metadata["source_counts"], set(FROZEN_SOURCE_NAMES),
        "frozen coordinate evidence.metadata.source_counts",
    )
    for source, count in source_counts.items():
        exact_int(count, f"frozen coordinate evidence.metadata.source_counts.{source}", minimum=0)
    status_counts = require_fields(
        metadata["status_counts"], {"candidate", "duplicate", "multiple_candidates", "needs_review"},
        "frozen coordinate evidence.metadata.status_counts",
    )
    for status, count in status_counts.items():
        exact_int(count, f"frozen coordinate evidence.metadata.status_counts.{status}", minimum=0)
    exact_str(metadata["stage2_file"], "frozen coordinate evidence.metadata.stage2_file")
    _sha256(metadata["stage2_sha256"], "frozen coordinate evidence.metadata.stage2_sha256")
    _string_array(metadata["bootstrap_files"], "frozen coordinate evidence.metadata.bootstrap_files")
    features = root["features"]
    if type(features) is not list:
        raise ContractError("frozen coordinate evidence.features: expected array")
    if metadata["coordinate_candidate_count"] != len(features):
        raise ContractError("frozen coordinate evidence.metadata.coordinate_candidate_count: does not match features")

    by_identity: dict[tuple[str, str], list[dict[str, object]]] = {}
    source_totals: dict[str, int] = {source: 0 for source in FROZEN_SOURCE_NAMES}
    source_identities: set[tuple[str, str]] = set()
    skipped_identities: set[tuple[str, str]] = set()
    skipped_feature_count = 0
    matched_feature_count = 0
    deduplicated_feature_count = 0
    seen: set[tuple[tuple[str, str], str, tuple[float, float]]] = set()
    for index, feature_value in enumerate(features):
        context = f"frozen coordinate evidence.features[{index}]"
        feature = require_fields(feature_value, {"type", "geometry", "properties"}, context)
        if exact_str(feature["type"], f"{context}.type") != "Feature":
            raise ContractError(f"{context}.type: expected Feature")
        geometry = require_fields(feature["geometry"], {"type", "coordinates"}, f"{context}.geometry")
        if exact_str(geometry["type"], f"{context}.geometry.type") != "Point":
            raise ContractError(f"{context}.geometry.type: expected Point")
        coordinates = _coordinates(geometry["coordinates"], f"{context}.geometry.coordinates")
        if type(feature["properties"]) is not dict:
            raise ContractError(f"{context}.properties: expected object")
        properties = feature["properties"]
        source = exact_str(properties.get("source"), f"{context}.properties.source")
        if source not in FROZEN_SOURCE_NAMES:
            raise ContractError(f"{context}.properties.source: unsupported source {source!r}")
        source_totals[source] += 1
        property_fields = set(properties)
        allowed_schemas = {
            "sgwiki_kartographer": {frozenset(FROZEN_EXPERIMENTAL_FIELDS), frozenset(FROZEN_REFRESH_FIELDS)},
            "onemap": {frozenset(FROZEN_EXPERIMENTAL_FIELDS), frozenset(FROZEN_BOOTSTRAP_FIELDS)},
            "osm": {frozenset(FROZEN_BOOTSTRAP_FIELDS), frozenset(FROZEN_REFRESH_FIELDS)},
            "lta_geojson": {frozenset(FROZEN_BOOTSTRAP_FIELDS)},
            "reviewed_manual": {frozenset(FROZEN_BOOTSTRAP_FIELDS)},
        }[source]
        if frozenset(property_fields) not in allowed_schemas:
            raise ContractError(f"{context}.properties: unexpected fields for source {source!r}")
        station_id = exact_str(properties["station_id"], f"{context}.properties.station_id")
        exit_id = exact_str(properties["exit_id"], f"{context}.properties.exit_id")
        if type(properties["station_name"]) is not str or not properties["station_name"]:
            raise ContractError(f"{context}.properties.station_name: expected nonempty string")
        exact_str(properties["label"], f"{context}.properties.label")
        _string_array(properties["station_codes"], f"{context}.properties.station_codes", nonempty=True)
        _string_array(properties["scopes"], f"{context}.properties.scopes")
        _string_array(properties["sources_for_exit"], f"{context}.properties.sources_for_exit", nonempty=True)
        if properties["scope"] is not None:
            exact_str(properties["scope"], f"{context}.properties.scope")
        for field in ("approved", "selected", "duplicate_within_source", "multiple_candidates", "needs_review"):
            if type(properties[field]) is not bool:
                raise ContractError(f"{context}.properties.{field}: expected boolean")
        if properties["approved"]:
            raise ContractError(f"{context}.properties.approved: frozen evidence must not be approved")
        for field in ("candidate_count", "source_count"):
            exact_int(properties[field], f"{context}.properties.{field}", minimum=1)
        if properties["candidate_index"] is not None:
            exact_int(properties["candidate_index"], f"{context}.properties.candidate_index", minimum=0)
        if type(properties["review_reasons"]) is not list:
            raise ContractError(f"{context}.properties.review_reasons: expected array")
        _validate_frozen_provenance(source, properties["provenance"], f"{context}.properties.provenance")
        identity = (station_id, exit_id)
        source_identities.add(identity)
        if identity not in current_identities:
            skipped_identities.add(identity)
            skipped_feature_count += 1
            continue
        matched_feature_count += 1
        mapped_source = FROZEN_SOURCE_NAMES[source]
        dedupe_key = (identity, mapped_source, tuple(coordinates))
        if dedupe_key in seen:
            deduplicated_feature_count += 1
            continue
        seen.add(dedupe_key)
        provenance = {
            "kind": "frozen_mrt_data_v2_candidate",
            "original_source": source,
            "properties": properties,
        }
        by_identity.setdefault(identity, []).append(
            _candidate(mapped_source, coordinates, provenance, selected=False)
        )

    if dict(source_totals) != dict(source_counts):
        raise ContractError("frozen coordinate evidence.metadata.source_counts: does not match features")
    if metadata["exit_identity_count"] != len(source_identities):
        raise ContractError("frozen coordinate evidence.metadata.exit_identity_count: does not match features")
    imported_candidate_count = sum(len(rows) for rows in by_identity.values())
    return by_identity, {
        "kind": "frozen_mrt_data_v2_coordinate_candidates",
        "feature_count": len(features),
        "source_identity_count": len(source_identities),
        "matched_identity_count": len(set(by_identity)),
        "matched_feature_count": matched_feature_count,
        "skipped_identity_count": len(skipped_identities),
        "skipped_feature_count": skipped_feature_count,
        "skipped_reason": "identity_absent_from_current_stage1",
        "deduplicated_feature_count": deduplicated_feature_count,
        "imported_candidate_count": imported_candidate_count,
    }


def _validate_report(value: object, name: str) -> dict[str, object]:
    row = require_fields(value, REPORT_FIELDS[name], f"review bundle.{name}")
    if exact_int(row["schema_version"], f"review bundle.{name}.schema_version") != 1:
        raise ContractError(f"review bundle.{name}.schema_version: unsupported version")
    exact_str(row["kind"], f"review bundle.{name}.kind")
    exact_str(row["status"], f"review bundle.{name}.status")
    if name.endswith("changes"):
        if type(row["items"]) is not list:
            raise ContractError(f"review bundle.{name}.items: expected array")
        if exact_int(row["count"], f"review bundle.{name}.count", minimum=0) != len(row["items"]):
            raise ContractError(f"review bundle.{name}.count: does not match items")
    else:
        if type(row["groups"]) is not list:
            raise ContractError(f"review bundle.{name}.groups: expected array")
        if exact_int(row["group_count"], f"review bundle.{name}.group_count", minimum=0) != len(row["groups"]):
            raise ContractError(f"review bundle.{name}.group_count: does not match groups")
        exact_int(row["finding_count"], f"review bundle.{name}.finding_count", minimum=0)
    return dict(row)


def _source_name(source: object) -> str:
    if type(source) is not dict:
        return "manual"
    kind = source.get("kind")
    if kind == "sgwiki_kartographer":
        return "kartographer"
    if kind == "trusted_onemap_baseline":
        return "onemap"
    if kind == "trusted_supplemental_baseline":
        provider = source.get("source")
        return "lta" if provider == "lta_geojson" else str(provider)
    return "manual"


def _urls(value: object) -> list[str]:
    found: list[str] = []
    if type(value) is dict:
        for key, item in value.items():
            if key in {"url", "source_url", "revision_url"} and type(item) is str and item.startswith("https://"):
                found.append(item)
            elif key == "evidence_urls" and type(item) is list:
                for entry in item:
                    if type(entry) is str and entry.startswith("https://"):
                        found.append(entry)
            else:
                found.extend(_urls(item))
    elif type(value) is list:
        for item in value:
            found.extend(_urls(item))
    return list(dict.fromkeys(found))


def _candidate(
    source: str,
    coordinates: object,
    provenance: object,
    *,
    selected: bool,
) -> dict[str, object]:
    if source not in SOURCE_NAMES:
        raise ContractError(f"review candidate source: unsupported source {source!r}")
    point = _coordinates(coordinates, "review candidate.coordinates")
    return {
        "id": canonical_sha256({"source": source, "coordinates": point, "provenance": provenance})[:16],
        "source": source,
        "coordinates": point,
        "selected_in_candidate_run": selected,
        "evidence_urls": _urls(provenance),
    }


def _report_findings(reports: dict[str, dict[str, object]], station_id: str, exit_id: str) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []

    def visit(value: object, report: str) -> None:
        if type(value) is dict:
            if value.get("station_id") == station_id and value.get("exit_id") == exit_id:
                reason = value.get("reason") or value.get("detail_reason") or "review_finding"
                severity = value.get("severity", "WARNING")
                if type(reason) is str and type(severity) is str:
                    results.append({"report": report, "severity": severity, "reason": reason})
            for child in value.values():
                visit(child, report)
        elif type(value) is list:
            for child in value:
                visit(child, report)

    for name, report in reports.items():
        visit(report, name)
    unique = {(row["report"], row["severity"], row["reason"]): row for row in results}
    return [unique[key] for key in sorted(unique)]


def _add_candidate(
    candidates: list[dict[str, object]], candidate: dict[str, object]
) -> None:
    identity = (candidate["source"], tuple(candidate["coordinates"]))
    if any((row["source"], tuple(row["coordinates"])) == identity for row in candidates):
        if candidate["selected_in_candidate_run"]:
            for row in candidates:
                if (row["source"], tuple(row["coordinates"])) == identity:
                    row["selected_in_candidate_run"] = True
        return
    candidates.append(candidate)


def build_review_bundle(
    *,
    run_directory: Path,
    config: PipelineConfig,
    approved_stage2: Path | None = None,
) -> dict[str, object]:
    """Validate one pipeline run and return its map-review bundle.

    The bundle references the approved baseline separately from the candidate
    run.  Passing no baseline produces null approved coordinates for every exit.
    """

    run_directory = run_directory.resolve()
    required = {
        "manifest": run_directory / "manifest.json",
        "stage1": run_directory / "stage1/stations.json",
        "stage2": run_directory / "stage2/stations.json",
        "stage1_changes": run_directory / "stage1/changes.json",
        "stage1_reviews": run_directory / "stage1/reviews.json",
        "stage2_changes": run_directory / "stage2/changes.json",
        "stage2_reviews": run_directory / "stage2/reviews.json",
    }
    missing = [str(path) for path in required.values() if not path.is_file()]
    if missing:
        raise ContractError(f"review run: missing required files: {missing}")

    manifest_value = read_json(required["manifest"])
    manifest = require_fields(
        manifest_value,
        {"schema_version", "run_id", "started_at", "finished_at", "status", "pipeline_mode", "configuration", "stages", "failure", "data_release_created"},
        "review run.manifest",
    )
    if exact_int(manifest["schema_version"], "review run.manifest.schema_version") != 1:
        raise ContractError("review run.manifest.schema_version: unsupported version")
    run_id = exact_str(manifest["run_id"], "review run.manifest.run_id")
    if run_id != run_directory.name:
        raise ContractError("review run.manifest.run_id: does not match run directory")
    status = exact_str(manifest["status"], "review run.manifest.status")
    finished_at = exact_str(manifest["finished_at"], "review run.manifest.finished_at")

    stage1_document = read_json(required["stage1"])
    _stage1_identity_index(stage1_document)
    current_document = read_json(required["stage2"])
    current = _validate_stage2_comparison_document(current_document, "review run.stage2")
    current_identities = set(_stage1_identity_index(stage1_document)[1])
    if set(current) != current_identities:
        raise ContractError("review run: Stage 1 and Stage 2 exit identities differ")

    frozen_evidence_path = config.paths.repo_root / FROZEN_COORDINATE_EVIDENCE
    if not frozen_evidence_path.is_file():
        raise ContractError(f"review coordinate evidence: missing required file {frozen_evidence_path}")
    frozen_evidence_sha256 = _sha256_file(frozen_evidence_path)
    if frozen_evidence_sha256 != FROZEN_COORDINATE_EVIDENCE_SHA256:
        raise ContractError(
            "review coordinate evidence: frozen artifact SHA-256 changed; "
            f"expected {FROZEN_COORDINATE_EVIDENCE_SHA256}, got {frozen_evidence_sha256}"
        )
    frozen_candidates, frozen_summary = _frozen_coordinate_candidates(
        read_json(frozen_evidence_path), current_identities
    )

    baseline: dict[tuple[str, str], dict[str, object]] = {}
    baseline_sha: str | None = None
    if approved_stage2 is not None:
        approved_path = approved_stage2.resolve()
        baseline_document = read_json(approved_path)
        baseline = _validate_stage2_comparison_document(baseline_document, "review approved baseline")
        baseline_sha = _sha256_file(approved_path)

    reports = {
        name: _validate_report(read_json(required[name]), name)
        for name in REPORT_FIELDS
    }

    reviewed = _reviewed_coordinates(
        read_json(config.paths.stage2_reviewed_coordinates),
        read_json(config.paths.stage2_coordinate_ledger),
    )
    trusted_onemap = _trusted_onemap_coordinates(
        read_json(config.paths.stage2_trusted_onemap_baseline)
    )
    supplemental = _supplemental_coordinates(
        read_json(config.paths.stage2_trusted_supplemental_baseline)
    )

    exits: list[dict[str, object]] = []
    candidate_count = 0
    approved_count = 0
    for key in sorted(current):
        current_identity = current[key]
        station = current_identity["station"]
        exit_row = current_identity["exit"]
        current_coordinate = None
        candidates: list[dict[str, object]] = []
        if exit_row["coordinate_status"] == "resolved":
            source = _source_name(exit_row["coordinate_source"])
            current_coordinate = {
                "coordinates": list(exit_row["coordinates"]),
                "source": source,
            }
            _add_candidate(candidates, _candidate(
                source, exit_row["coordinates"], exit_row["coordinate_source"], selected=True
            ))

        for source, entries in (
            ("manual", reviewed), ("onemap", trusted_onemap),
        ):
            entry = entries.get(key)
            if entry is not None:
                _add_candidate(candidates, _candidate(
                    source, entry["coordinates"], entry, selected=False
                ))
        supplemental_entry = supplemental.get(key)
        if supplemental_entry is not None:
            provider = supplemental_entry["provenance"]["source"]
            source = "lta" if provider == "lta_geojson" else provider
            _add_candidate(candidates, _candidate(
                str(source), supplemental_entry["coordinates"],
                supplemental_entry["provenance"], selected=False,
            ))
        for frozen_candidate in frozen_candidates.get(key, []):
            _add_candidate(candidates, frozen_candidate)

        approved_coordinate = None
        approved_identity = baseline.get(key)
        if approved_identity is not None:
            approved_exit = approved_identity["exit"]
            if approved_exit["coordinate_status"] == "resolved":
                approved_coordinate = {
                    "coordinates": list(approved_exit["coordinates"]),
                    "source": _source_name(approved_exit["coordinate_source"]),
                }
                approved_count += 1

        evidence = [{"url": station["source"]["url"], "title": "SgWiki station page"}]
        candidates.sort(key=lambda row: (str(row["source"]), str(row["id"])))
        candidate_count += len(candidates)
        exits.append({
            "station_id": key[0],
            "station_name": station["name"],
            "station_codes": list(station["codes"]),
            "exit_id": key[1],
            "label": exit_row["label"],
            "scopes": list(exit_row["scopes"]),
            "approved_coordinate": approved_coordinate,
            "current_coordinate": current_coordinate,
            "candidates": candidates,
            "evidence": evidence,
            "findings": _report_findings(reports, key[0], key[1]),
        })

    return {
        "schema_version": 1,
        "kind": "sg_rail_station_data_review_bundle",
        "run": {
            "id": run_id,
            "status": status,
            "finished_at": finished_at,
            "manifest_sha256": _sha256_file(required["manifest"]),
        },
        "approved_baseline": {
            "status": "available" if approved_stage2 is not None else "unavailable",
            "sha256": baseline_sha,
        },
        "inputs": {
            "stage1_sha256": _sha256_file(required["stage1"]),
            "stage2_sha256": _sha256_file(required["stage2"]),
            "stage1_changes_sha256": _sha256_file(required["stage1_changes"]),
            "stage1_reviews_sha256": _sha256_file(required["stage1_reviews"]),
            "stage2_changes_sha256": _sha256_file(required["stage2_changes"]),
            "stage2_reviews_sha256": _sha256_file(required["stage2_reviews"]),
            "reviewed_coordinates_sha256": _sha256_file(config.paths.stage2_reviewed_coordinates),
            "coordinate_ledger_sha256": _sha256_file(config.paths.stage2_coordinate_ledger),
            "trusted_onemap_sha256": _sha256_file(config.paths.stage2_trusted_onemap_baseline),
            "trusted_supplemental_sha256": _sha256_file(config.paths.stage2_trusted_supplemental_baseline),
            "alternate_coordinate_evidence_sha256": frozen_evidence_sha256,
        },
        "alternate_coordinate_evidence": {
            "status": "available",
            "path": FROZEN_COORDINATE_EVIDENCE.as_posix(),
            "sha256": frozen_evidence_sha256,
            **frozen_summary,
        },
        "summary": {
            "exit_count": len(exits),
            "approved_coordinate_count": approved_count,
            "unapproved_coordinate_count": len(exits) - approved_count,
            "candidate_count": candidate_count,
            "finding_count": sum(len(row["findings"]) for row in exits),
        },
        "exits": exits,
        "run_reports": reports,
    }


def write_review_bundle(
    *, run_directory: Path, output_path: Path, config: PipelineConfig,
    approved_stage2: Path | None = None,
) -> Path:
    """Build and atomically write one immutable review bundle."""

    return atomic_write_json(
        output_path,
        build_review_bundle(
            run_directory=run_directory,
            config=config,
            approved_stage2=approved_stage2,
        ),
    )


def validate_review_bundle(value: object) -> dict[str, object]:
    """Validate the complete browser contract and return it unchanged."""

    root = require_fields(
        value,
        {"schema_version", "kind", "run", "approved_baseline", "inputs", "alternate_coordinate_evidence", "summary", "exits", "run_reports"},
        "review bundle",
    )
    if exact_int(root["schema_version"], "review bundle.schema_version") != 1:
        raise ContractError("review bundle.schema_version: unsupported version")
    if exact_str(root["kind"], "review bundle.kind") != "sg_rail_station_data_review_bundle":
        raise ContractError("review bundle.kind: unsupported kind")
    run = require_fields(root["run"], {"id", "status", "finished_at", "manifest_sha256"}, "review bundle.run")
    for field in ("id", "status", "finished_at", "manifest_sha256"):
        exact_str(run[field], f"review bundle.run.{field}")
    if len(run["manifest_sha256"]) != 64 or any(
        character not in "0123456789abcdef" for character in run["manifest_sha256"]
    ):
        raise ContractError("review bundle.run.manifest_sha256: expected SHA-256")
    approved = require_fields(root["approved_baseline"], {"status", "sha256"}, "review bundle.approved_baseline")
    baseline_status = exact_str(approved["status"], "review bundle.approved_baseline.status")
    if baseline_status not in {"available", "unavailable"}:
        raise ContractError("review bundle.approved_baseline.status: unsupported status")
    if (baseline_status == "available") != (type(approved["sha256"]) is str):
        raise ContractError("review bundle.approved_baseline.sha256: does not agree with status")
    if type(approved["sha256"]) is str and (
        len(approved["sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in approved["sha256"])
    ):
        raise ContractError("review bundle.approved_baseline.sha256: expected SHA-256")
    input_fields = {
        "stage1_sha256", "stage2_sha256", "stage1_changes_sha256", "stage1_reviews_sha256",
        "stage2_changes_sha256", "stage2_reviews_sha256", "reviewed_coordinates_sha256",
        "coordinate_ledger_sha256", "trusted_onemap_sha256", "trusted_supplemental_sha256",
        "alternate_coordinate_evidence_sha256",
    }
    inputs = require_fields(root["inputs"], input_fields, "review bundle.inputs")
    for field in input_fields:
        _sha256(inputs[field], f"review bundle.inputs.{field}")
    alternate = require_fields(
        root["alternate_coordinate_evidence"],
        {
            "status", "path", "sha256", "kind", "feature_count", "source_identity_count",
            "matched_identity_count", "matched_feature_count", "skipped_identity_count",
            "skipped_feature_count", "skipped_reason", "deduplicated_feature_count",
            "imported_candidate_count",
        },
        "review bundle.alternate_coordinate_evidence",
    )
    if exact_str(alternate["status"], "review bundle.alternate_coordinate_evidence.status") != "available":
        raise ContractError("review bundle.alternate_coordinate_evidence.status: expected available")
    if exact_str(alternate["path"], "review bundle.alternate_coordinate_evidence.path") != FROZEN_COORDINATE_EVIDENCE.as_posix():
        raise ContractError("review bundle.alternate_coordinate_evidence.path: unexpected path")
    if exact_str(alternate["kind"], "review bundle.alternate_coordinate_evidence.kind") != "frozen_mrt_data_v2_coordinate_candidates":
        raise ContractError("review bundle.alternate_coordinate_evidence.kind: unsupported kind")
    if _sha256(alternate["sha256"], "review bundle.alternate_coordinate_evidence.sha256") != inputs["alternate_coordinate_evidence_sha256"]:
        raise ContractError("review bundle.alternate_coordinate_evidence.sha256: does not match inputs")
    for field in (
        "feature_count", "source_identity_count", "matched_identity_count", "matched_feature_count",
        "skipped_identity_count", "skipped_feature_count", "deduplicated_feature_count",
        "imported_candidate_count",
    ):
        exact_int(alternate[field], f"review bundle.alternate_coordinate_evidence.{field}", minimum=0)
    if exact_str(alternate["skipped_reason"], "review bundle.alternate_coordinate_evidence.skipped_reason") != "identity_absent_from_current_stage1":
        raise ContractError("review bundle.alternate_coordinate_evidence.skipped_reason: unsupported reason")
    if alternate["matched_feature_count"] + alternate["skipped_feature_count"] != alternate["feature_count"]:
        raise ContractError("review bundle.alternate_coordinate_evidence: feature counts do not balance")
    if alternate["imported_candidate_count"] + alternate["deduplicated_feature_count"] != alternate["matched_feature_count"]:
        raise ContractError("review bundle.alternate_coordinate_evidence: imported counts do not balance")
    summary = require_fields(
        root["summary"],
        {"exit_count", "approved_coordinate_count", "unapproved_coordinate_count", "candidate_count", "finding_count"},
        "review bundle.summary",
    )
    for field in summary:
        exact_int(summary[field], f"review bundle.summary.{field}", minimum=0)
    reports = require_fields(root["run_reports"], set(REPORT_FIELDS), "review bundle.run_reports")
    for name in REPORT_FIELDS:
        _validate_report(reports[name], name)
    if type(root["exits"]) is not list:
        raise ContractError("review bundle.exits: expected array")
    identities: set[tuple[str, str]] = set()
    approved_count = candidate_count = finding_count = 0
    for index, value in enumerate(root["exits"]):
        context = f"review bundle.exits[{index}]"
        row = require_fields(
            value,
            {"station_id", "station_name", "station_codes", "exit_id", "label", "scopes", "approved_coordinate", "current_coordinate", "candidates", "evidence", "findings"},
            context,
        )
        station_id = exact_str(row["station_id"], f"{context}.station_id")
        exit_id = exact_str(row["exit_id"], f"{context}.exit_id")
        exact_str(row["station_name"], f"{context}.station_name")
        exact_str(row["label"], f"{context}.label")
        for field in ("station_codes", "scopes"):
            if type(row[field]) is not list or not row[field]:
                raise ContractError(f"{context}.{field}: expected nonempty array")
            for item_index, item in enumerate(row[field]):
                exact_str(item, f"{context}.{field}[{item_index}]")
        identity = (station_id, exit_id)
        if identity in identities:
            raise ContractError(f"{context}: duplicate exit identity {identity}")
        identities.add(identity)
        for field in ("approved_coordinate", "current_coordinate"):
            coordinate = row[field]
            if coordinate is None:
                continue
            coordinate_row = require_fields(coordinate, {"coordinates", "source"}, f"{context}.{field}")
            _coordinates(coordinate_row["coordinates"], f"{context}.{field}.coordinates")
            if exact_str(coordinate_row["source"], f"{context}.{field}.source") not in SOURCE_NAMES:
                raise ContractError(f"{context}.{field}.source: unsupported source")
        if row["approved_coordinate"] is not None:
            approved_count += 1
        if type(row["candidates"]) is not list:
            raise ContractError(f"{context}.candidates: expected array")
        for candidate_index, item in enumerate(row["candidates"]):
            candidate_context = f"{context}.candidates[{candidate_index}]"
            candidate = require_fields(item, {"id", "source", "coordinates", "selected_in_candidate_run", "evidence_urls"}, candidate_context)
            exact_str(candidate["id"], f"{candidate_context}.id")
            if exact_str(candidate["source"], f"{candidate_context}.source") not in SOURCE_NAMES:
                raise ContractError(f"{candidate_context}.source: unsupported source")
            _coordinates(candidate["coordinates"], f"{candidate_context}.coordinates")
            if type(candidate["selected_in_candidate_run"]) is not bool:
                raise ContractError(f"{candidate_context}.selected_in_candidate_run: expected boolean")
            if type(candidate["evidence_urls"]) is not list:
                raise ContractError(f"{candidate_context}.evidence_urls: expected array")
            for url_index, url in enumerate(candidate["evidence_urls"]):
                if not exact_str(url, f"{candidate_context}.evidence_urls[{url_index}]").startswith("https://"):
                    raise ContractError(f"{candidate_context}.evidence_urls[{url_index}]: expected HTTPS URL")
        candidate_count += len(row["candidates"])
        for field in ("evidence", "findings"):
            if type(row[field]) is not list:
                raise ContractError(f"{context}.{field}: expected array")
        for evidence_index, evidence_value in enumerate(row["evidence"]):
            evidence = require_fields(evidence_value, {"url", "title"}, f"{context}.evidence[{evidence_index}]")
            if not exact_str(evidence["url"], f"{context}.evidence[{evidence_index}].url").startswith("https://"):
                raise ContractError(f"{context}.evidence[{evidence_index}].url: expected HTTPS URL")
            exact_str(evidence["title"], f"{context}.evidence[{evidence_index}].title")
        for finding_index, finding_value in enumerate(row["findings"]):
            finding = require_fields(finding_value, {"report", "severity", "reason"}, f"{context}.findings[{finding_index}]")
            for field in finding:
                exact_str(finding[field], f"{context}.findings[{finding_index}].{field}")
        finding_count += len(row["findings"])
    if summary["exit_count"] != len(root["exits"]):
        raise ContractError("review bundle.summary.exit_count: does not match exits")
    if summary["approved_coordinate_count"] != approved_count:
        raise ContractError("review bundle.summary.approved_coordinate_count: does not match exits")
    if baseline_status == "unavailable" and approved_count != 0:
        raise ContractError("review bundle.approved_baseline: unavailable baseline cannot contain approved coordinates")
    if summary["unapproved_coordinate_count"] != len(root["exits"]) - approved_count:
        raise ContractError("review bundle.summary.unapproved_coordinate_count: does not match exits")
    if summary["candidate_count"] != candidate_count or summary["finding_count"] != finding_count:
        raise ContractError("review bundle.summary: candidate or finding count does not match exits")
    return dict(root)
