"""Pure Stage 2 exit-coordinate resolution.

Stage 2 is deliberately conservative.  It resolves only exit identities that
already exist in the approved Stage 1 document, and it has four
authoritative coordinate sources:

* an exact marker from an SgWiki Kartographer overlay collected for the same
  page revision as Stage 1; or
* a checked-in, human-reviewed coordinate backed by the review ledger; or
* an explicitly trusted, frozen OneMap baseline with original provenance; or
* an owner-authorized supplemental baseline of saved LTA, OSM and OneMap points.

New OneMap, LTA, OSM and operator candidates remain evidence for a reviewer.
The frozen baselines are never refreshed automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from math import asin, cos, radians, sin, sqrt
from pathlib import PurePosixPath
import re
from typing import Any
from urllib.parse import quote_plus, urlsplit

from .contracts import ContractError, exact_bool, exact_int, exact_str, fail, require_fields


SINGAPORE_LONGITUDE = (103.58, 104.12)
SINGAPORE_LATITUDE = (1.12, 1.50)
HASH = re.compile(r"[0-9a-f]{64}")
STATION_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
EXIT_ID = re.compile(r"(MRT|LRT|SHARED):([A-Z0-9]+)")
REVIEW_MODES = frozenset({"override", "gap_fill"})
EXACT_OSM_REVIEW_URLS = {
    ("bugis", "MRT:E"): "https://www.openstreetmap.org/node/6898222161",
    ("bugis", "MRT:G"): "https://www.openstreetmap.org/node/13415193362",
    ("expo", "MRT:A"): "https://www.openstreetmap.org/node/5168796839",
    ("farrer-park", "MRT:E"): "https://www.openstreetmap.org/node/2402426984",
    ("raffles-place", "MRT:K"): "https://www.openstreetmap.org/node/10277858575",
    ("raffles-place", "MRT:L"): "https://www.openstreetmap.org/node/10277858574",
    ("raffles-place", "MRT:M"): "https://www.openstreetmap.org/node/10277858573",
    ("tanjong-pagar", "MRT:J"): "https://www.openstreetmap.org/node/6033450179",
    ("tanjong-pagar", "MRT:K"): "https://www.openstreetmap.org/node/1839626477",
}
REVIEW_SELECTION_POLICY = [
    "An exact human-reviewed override replaces Kartographer.",
    "Otherwise an exact Kartographer coordinate is selected.",
    "A human-reviewed gap fill is selected only when Kartographer has no exact coordinate.",
    "Unapproved external candidates never resolve an exit.",
]
TRUSTED_ONEMAP_POLICY = [
    "Imported OneMap positions are trusted deterministic gap fills authorized by the project owner.",
    "Human-reviewed coordinates and exact SgWiki Kartographer markers take precedence.",
    "A changed SgWiki page identity or revision invalidates an imported coordinate until it is vetted again.",
    "Original OneMap response provenance is retained for audit.",
]

STAGE1_FIELDS = {
    "schema_version", "kind", "status", "publication_allowed", "gtfs",
    "reviewed_decisions", "summary", "normalizations", "stations",
}
STAGE1_STATION_FIELDS = {
    "id", "name", "codes", "catalog_status", "operational_status", "gtfs_matches",
    "source", "exit_inventory_status", "exits", "bus_links", "excluded_bus_links",
    "station_connections", "areas", "platforms", "layout_supplements",
    "layout_coverage", "reviews",
}
STAGE1_SOURCE_FIELDS = {
    "kind", "title", "url", "api_url", "revision_url", "page_id", "revision_id",
    "redirects", "contracts_applied",
}
STAGE1_EXIT_FIELDS = {
    "id", "label", "scopes", "inventory_evidence", "destinations", "source_rows",
    "alternative_exits", "accessibility", "operating_status", "operating_hours",
    "status_notes", "bus_stop_codes",
}


@dataclass(frozen=True, slots=True)
class Stage2BuildResult:
    """All deterministic Stage 2 artifacts produced by one resolution pass."""

    dataset: dict[str, object]
    geojson: dict[str, object]
    reviews: dict[str, object]
    coverage: dict[str, object]


def _array(value: object, context: str, *, nonempty: bool = False) -> list[object]:
    if type(value) is not list:
        fail(context, f"expected array, got {type(value).__name__}")
    if nonempty and not value:
        fail(context, "must not be empty")
    return value


def _nullable_hash(value: object, context: str) -> str | None:
    if value is None:
        return None
    return _hash(value, context)


def _hash(value: object, context: str) -> str:
    result = exact_str(value, context)
    if HASH.fullmatch(result) is None:
        fail(context, "must be a lowercase SHA-256 digest")
    return result


def _station_id(value: object, context: str) -> str:
    result = exact_str(value, context)
    if STATION_ID.fullmatch(result) is None:
        fail(context, "must be a lowercase station slug")
    return result


def _exit_id(value: object, context: str) -> tuple[str, str]:
    result = exact_str(value, context)
    match = EXIT_ID.fullmatch(result)
    if match is None:
        fail(context, "must be MRT:, LRT: or SHARED: followed by an uppercase label")
    return result, match.group(2)


def _string_array(
    value: object,
    context: str,
    *,
    nonempty: bool = False,
    allow_empty_strings: bool = False,
) -> list[str]:
    rows = _array(value, context, nonempty=nonempty)
    result = [
        exact_str(item, f"{context}[{index}]", nonempty=not allow_empty_strings)
        for index, item in enumerate(rows)
    ]
    if len(result) != len(set(result)):
        fail(context, "must not contain duplicates")
    return result


def _coordinate(value: object, context: str) -> list[float]:
    values = _array(value, context)
    if len(values) != 2:
        fail(context, "Point coordinates must contain exactly longitude and latitude")
    longitude, latitude = values
    if type(longitude) is not float or type(latitude) is not float:
        fail(context, "longitude and latitude must be exact finite floats")
    if longitude != longitude or latitude != latitude:
        fail(context, "longitude and latitude must be finite")
    if longitude in (float("inf"), float("-inf")) or latitude in (
        float("inf"), float("-inf")
    ):
        fail(context, "longitude and latitude must be finite")
    if not SINGAPORE_LONGITUDE[0] <= longitude <= SINGAPORE_LONGITUDE[1]:
        fail(context, f"longitude is outside Singapore bounds {SINGAPORE_LONGITUDE}")
    if not SINGAPORE_LATITUDE[0] <= latitude <= SINGAPORE_LATITUDE[1]:
        fail(context, f"latitude is outside Singapore bounds {SINGAPORE_LATITUDE}")
    return [longitude, latitude]


def _https_url(value: object, context: str) -> str:
    result = exact_str(value, context)
    parts = urlsplit(result)
    if parts.scheme != "https" or not parts.hostname:
        fail(context, "must be an absolute HTTPS URL")
    if parts.username is not None or parts.password is not None:
        fail(context, "must not contain credentials")
    return result


def _timestamp(value: object, context: str) -> str:
    result = exact_str(value, context)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError:
        fail(context, "must be an ISO-8601 timestamp")
    if parsed.tzinfo is None:
        fail(context, "must include a UTC offset")
    return result


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def reviewed_ledger_entry_hash(entry_without_hash: object) -> str:
    """Return the canonical hash used by the reviewed-coordinate ledger."""

    if type(entry_without_hash) is not dict:
        fail("reviewed ledger hash payload", "expected table")
    if "entry_hash" in entry_without_hash:
        fail("reviewed ledger hash payload", "must not include entry_hash")
    return _canonical_digest(entry_without_hash)


def _stage1_identity_index(
    document: object,
) -> tuple[list[dict[str, object]], dict[tuple[str, str], dict[str, object]]]:
    root = require_fields(document, STAGE1_FIELDS, "stage2.stage1")
    if exact_int(root["schema_version"], "stage2.stage1.schema_version") != 1:
        fail("stage2.stage1.schema_version", "unsupported version")
    if exact_str(root["kind"], "stage2.stage1.kind") != "mrt_wayfinding_stage_1":
        fail("stage2.stage1.kind", "unsupported document kind")
    if exact_str(root["status"], "stage2.stage1.status") != "complete":
        fail("stage2.stage1.status", "Stage 1 must be complete before coordinate resolution")
    if not exact_bool(root["publication_allowed"], "stage2.stage1.publication_allowed"):
        fail("stage2.stage1.publication_allowed", "Stage 1 must allow publication")
    if type(root["gtfs"]) is not dict or type(root["reviewed_decisions"]) is not dict:
        fail("stage2.stage1", "gtfs and reviewed_decisions must be tables")
    if type(root["summary"]) is not dict or type(root["normalizations"]) is not list:
        fail("stage2.stage1", "summary must be a table and normalizations must be an array")

    stations: list[dict[str, object]] = []
    identities: dict[tuple[str, str], dict[str, object]] = {}
    station_ids: set[str] = set()
    for station_index, value in enumerate(
        _array(root["stations"], "stage2.stage1.stations", nonempty=True)
    ):
        context = f"stage2.stage1.stations[{station_index}]"
        row = require_fields(value, STAGE1_STATION_FIELDS, context)
        station_id = _station_id(row["id"], f"{context}.id")
        if station_id in station_ids:
            fail(f"{context}.id", f"duplicate station identity {station_id}")
        station_ids.add(station_id)
        name = exact_str(row["name"], f"{context}.name")
        codes = _string_array(row["codes"], f"{context}.codes", nonempty=True)
        if exact_str(row["operational_status"], f"{context}.operational_status") != "active":
            fail(context, "Stage 2 accepts active stations only")
        source = require_fields(row["source"], STAGE1_SOURCE_FIELDS, f"{context}.source")
        if exact_str(source["kind"], f"{context}.source.kind") != "sgwiki_station_page":
            fail(f"{context}.source.kind", "unsupported source")
        page_id = exact_int(source["page_id"], f"{context}.source.page_id", minimum=1)
        revision_id = exact_int(
            source["revision_id"], f"{context}.source.revision_id", minimum=1
        )
        source_url = _https_url(source["url"], f"{context}.source.url")
        station = {
            "id": station_id,
            "name": name,
            "codes": codes,
            "source": {
                "kind": "sgwiki_station_page",
                "page_id": page_id,
                "revision_id": revision_id,
                "url": source_url,
            },
            "exits": [],
        }
        exit_ids: set[str] = set()
        for exit_index, exit_value in enumerate(
            _array(row["exits"], f"{context}.exits", nonempty=True)
        ):
            exit_context = f"{context}.exits[{exit_index}]"
            exit_row = require_fields(exit_value, STAGE1_EXIT_FIELDS, exit_context)
            identity, identity_label = _exit_id(exit_row["id"], f"{exit_context}.id")
            label = exact_str(exit_row["label"], f"{exit_context}.label")
            if label != identity_label:
                fail(exit_context, "exit label does not match its identity")
            scopes = _string_array(exit_row["scopes"], f"{exit_context}.scopes", nonempty=True)
            if scopes not in (["MRT"], ["LRT"], ["MRT", "LRT"]):
                fail(f"{exit_context}.scopes", "unsupported or non-canonical exit scopes")
            expected_prefix = "SHARED" if len(scopes) == 2 else scopes[0]
            if not identity.startswith(f"{expected_prefix}:"):
                fail(exit_context, "exit identity prefix does not agree with scopes")
            if identity in exit_ids:
                fail(exit_context, f"duplicate exit identity {identity}")
            exit_ids.add(identity)
            exit_record = {"id": identity, "label": label, "scopes": scopes}
            station["exits"].append(exit_record)
            identities[(station_id, identity)] = {
                "station": station,
                "exit": exit_record,
            }
        station["exits"].sort(key=lambda item: str(item["id"]))
        stations.append(station)
    stations.sort(key=lambda item: str(item["id"]))
    return stations, identities


def prepare_kartographer_exits(
    stage1_document: object,
    snapshot: object,
) -> dict[str, object]:
    """Map native collector markers to Stage 1 identities by unique station label.

    A marker label that has zero or multiple matches is retained for review and
    never turned into a new exit identity.
    """

    stage1_stations, _ = _stage1_identity_index(stage1_document)
    stage1_by_name: dict[str, dict[str, object]] = {}
    for station in stage1_stations:
        name = str(station["name"])
        if name in stage1_by_name:
            fail("kartographer adapter", f"duplicate Stage 1 station name {name}")
        stage1_by_name[name] = station

    root = require_fields(
        snapshot, {"schema_version", "kind", "stations", "overlays"},
        "kartographer snapshot",
    )
    if exact_int(root["schema_version"], "kartographer snapshot.schema_version") != 1:
        fail("kartographer snapshot.schema_version", "unsupported version")
    if exact_str(root["kind"], "kartographer snapshot.kind") != "sgwiki_kartographer_snapshot":
        fail("kartographer snapshot.kind", "unsupported document kind")

    downloaded: dict[str, dict[str, object]] = {}
    station_names: set[str] = set()
    base_fields = {
        "station", "codes", "title", "page_id", "revision_id", "rendered_file",
        "rendered_sha256", "sgwiki_snapshot_sha256", "status",
    }
    downloaded_extra = {
        "overlay_id", "mapdata_url", "response_file", "response_sha256", "normalized_file",
        "normalized_sha256", "exit_marker_count",
    }
    for index, value in enumerate(_array(root["stations"], "kartographer snapshot.stations")):
        context = f"kartographer snapshot.stations[{index}]"
        if type(value) is not dict:
            fail(context, "expected table")
        status = exact_str(value.get("status"), f"{context}.status")
        fields = base_fields | (downloaded_extra if status == "downloaded" else set())
        row = require_fields(value, fields, context)
        if status not in {"downloaded", "no_embedded_map"}:
            fail(f"{context}.status", "unsupported collection status")
        station_name = exact_str(row["station"], f"{context}.station")
        if station_name in station_names:
            fail(f"{context}.station", f"duplicate station {station_name}")
        station_names.add(station_name)
        _string_array(row["codes"], f"{context}.codes", nonempty=True)
        exact_str(row["title"], f"{context}.title")
        exact_int(row["page_id"], f"{context}.page_id", minimum=1)
        exact_int(row["revision_id"], f"{context}.revision_id", minimum=1)
        exact_str(row["rendered_file"], f"{context}.rendered_file")
        _hash(row["rendered_sha256"], f"{context}.rendered_sha256")
        _hash(row["sgwiki_snapshot_sha256"], f"{context}.sgwiki_snapshot_sha256")
        if status == "downloaded":
            exact_str(row["overlay_id"], f"{context}.overlay_id")
            _https_url(row["mapdata_url"], f"{context}.mapdata_url")
            exact_str(row["response_file"], f"{context}.response_file")
            _hash(row["response_sha256"], f"{context}.response_sha256")
            exact_str(row["normalized_file"], f"{context}.normalized_file")
            _hash(row["normalized_sha256"], f"{context}.normalized_sha256")
            exact_int(row["exit_marker_count"], f"{context}.exit_marker_count", minimum=0)
            downloaded[station_name] = dict(row)

    prepared_overlays: list[dict[str, object]] = []
    unmatched: list[dict[str, object]] = []
    overlay_names: set[str] = set()
    for overlay_index, value in enumerate(_array(root["overlays"], "kartographer snapshot.overlays")):
        context = f"kartographer snapshot.overlays[{overlay_index}]"
        overlay = require_fields(
            value,
            {
                "schema_version", "kind", "station", "codes", "page", "map",
                "feature_collection", "exit_markers",
            },
            context,
        )
        if exact_int(overlay["schema_version"], f"{context}.schema_version") != 1:
            fail(f"{context}.schema_version", "unsupported version")
        if exact_str(overlay["kind"], f"{context}.kind") != "sgwiki_kartographer_overlay":
            fail(f"{context}.kind", "unsupported overlay kind")
        station_name = exact_str(overlay["station"], f"{context}.station")
        if station_name in overlay_names:
            fail(f"{context}.station", f"duplicate overlay for {station_name}")
        overlay_names.add(station_name)
        codes = _string_array(overlay["codes"], f"{context}.codes", nonempty=True)
        page = require_fields(
            overlay["page"], {"title", "page_id", "revision_id", "overlay_id"},
            f"{context}.page",
        )
        exact_str(page["title"], f"{context}.page.title")
        page_id = exact_int(page["page_id"], f"{context}.page.page_id", minimum=1)
        revision_id = exact_int(
            page["revision_id"], f"{context}.page.revision_id", minimum=1
        )
        overlay_id = exact_str(page["overlay_id"], f"{context}.page.overlay_id")
        map_data = require_fields(
            overlay["map"],
            {
                "rendered", "kind", "zoom", "center", "width", "height", "overlay_id",
                "map_href", "snapshot_url", "mapdata_url",
            },
            f"{context}.map",
        )
        if not exact_bool(map_data["rendered"], f"{context}.map.rendered"):
            fail(f"{context}.map.rendered", "downloaded overlays must be rendered")
        if exact_str(map_data["kind"], f"{context}.map.kind") != "kartographer_osm_basemap_with_sgwiki_overlay":
            fail(f"{context}.map.kind", "unsupported map kind")
        exact_int(map_data["zoom"], f"{context}.map.zoom", minimum=1, maximum=22)
        _coordinate(map_data["center"], f"{context}.map.center")
        exact_int(map_data["width"], f"{context}.map.width", minimum=1)
        exact_int(map_data["height"], f"{context}.map.height", minimum=1)
        if exact_str(map_data["overlay_id"], f"{context}.map.overlay_id") != overlay_id:
            fail(context, "map and page overlay IDs disagree")
        exact_str(map_data["map_href"], f"{context}.map.map_href")
        _https_url(map_data["snapshot_url"], f"{context}.map.snapshot_url")
        source_url = _https_url(map_data["mapdata_url"], f"{context}.map.mapdata_url")
        collection = require_fields(
            overlay["feature_collection"], {"type", "features"},
            f"{context}.feature_collection",
        )
        if exact_str(collection["type"], f"{context}.feature_collection.type") != "FeatureCollection":
            fail(f"{context}.feature_collection.type", "must be FeatureCollection")
        _array(collection["features"], f"{context}.feature_collection.features")

        collection_row = downloaded.get(station_name)
        if collection_row is None:
            fail(context, "overlay has no downloaded station provenance row")
        if (
            collection_row["codes"] != codes
            or collection_row["page_id"] != page_id
            or collection_row["revision_id"] != revision_id
            or collection_row["overlay_id"] != overlay_id
            or collection_row["mapdata_url"] != source_url
        ):
            fail(context, "overlay disagrees with its collection provenance row")

        stage1_station = stage1_by_name.get(station_name)
        if stage1_station is not None and stage1_station["codes"] != codes:
            fail(context, "Kartographer station codes disagree with Stage 1")
        prepared_markers: list[dict[str, object]] = []
        seen_labels: set[str] = set()
        marker_rows = _array(overlay["exit_markers"], f"{context}.exit_markers")
        if collection_row["exit_marker_count"] != len(marker_rows):
            fail(context, "exit marker count disagrees with collection provenance")
        for marker_index, marker_value in enumerate(marker_rows):
            marker_context = f"{context}.exit_markers[{marker_index}]"
            marker = require_fields(
                marker_value,
                {"label", "title", "destination_verbatim", "coordinates", "feature_index"},
                marker_context,
            )
            label = exact_str(marker["label"], f"{marker_context}.label")
            if label in seen_labels:
                fail(marker_context, f"duplicate marker label {label}")
            seen_labels.add(label)
            if exact_str(marker["title"], f"{marker_context}.title") != f"Exit {label}":
                fail(marker_context, "marker title does not agree with its label")
            exact_str(
                marker["destination_verbatim"],
                f"{marker_context}.destination_verbatim",
            )
            coordinates = _coordinate(marker["coordinates"], f"{marker_context}.coordinates")
            feature_index = exact_int(
                marker["feature_index"], f"{marker_context}.feature_index", minimum=0
            )
            matches = [] if stage1_station is None else [
                item for item in stage1_station["exits"] if item["label"] == label
            ]
            if len(matches) != 1:
                unmatched.append({
                    "station_id": None if stage1_station is None else stage1_station["id"],
                    "station_name": station_name,
                    "label": label,
                    "coordinates": coordinates,
                    "source_url": source_url,
                    "source_feature_index": feature_index,
                    "reason": (
                        "kartographer_station_missing_from_stage1"
                        if stage1_station is None
                        else "kartographer_exit_label_missing_from_stage1"
                        if not matches
                        else "kartographer_exit_label_ambiguous_in_stage1"
                    ),
                    "matching_exit_ids": sorted(str(item["id"]) for item in matches),
                })
                continue
            matched_exit = matches[0]
            prepared_markers.append({
                "exit_id": matched_exit["id"],
                "label": label,
                "coordinates": coordinates,
                "source_feature_index": feature_index,
            })
        if stage1_station is not None:
            prepared_overlays.append({
                "station_id": stage1_station["id"],
                "page_id": page_id,
                "revision_id": revision_id,
                "source_url": source_url,
                "markers": prepared_markers,
            })
    if set(downloaded) != overlay_names:
        fail(
            "kartographer snapshot",
            f"downloaded station rows and overlays differ; rows={sorted(downloaded)}, "
            f"overlays={sorted(overlay_names)}",
        )
    return {
        "schema_version": 1,
        "kind": "sgwiki_kartographer_exit_overlays",
        "overlays": prepared_overlays,
        "unmatched_markers": unmatched,
    }


def _kartographer_markers(
    document: object,
) -> tuple[dict[tuple[str, str], dict[str, object]], list[dict[str, object]]]:
    root = require_fields(
        document, {"schema_version", "kind", "overlays", "unmatched_markers"},
        "kartographer",
    )
    if exact_int(root["schema_version"], "kartographer.schema_version") != 1:
        fail("kartographer.schema_version", "unsupported version")
    if exact_str(root["kind"], "kartographer.kind") != "sgwiki_kartographer_exit_overlays":
        fail("kartographer.kind", "unsupported document kind")
    markers: dict[tuple[str, str], dict[str, object]] = {}
    overlay_stations: set[str] = set()
    for overlay_index, value in enumerate(_array(root["overlays"], "kartographer.overlays")):
        context = f"kartographer.overlays[{overlay_index}]"
        row = require_fields(
            value,
            {"station_id", "page_id", "revision_id", "source_url", "markers"},
            context,
        )
        station_id = _station_id(row["station_id"], f"{context}.station_id")
        if station_id in overlay_stations:
            fail(f"{context}.station_id", f"duplicate overlay for {station_id}")
        overlay_stations.add(station_id)
        page_id = exact_int(row["page_id"], f"{context}.page_id", minimum=1)
        revision_id = exact_int(row["revision_id"], f"{context}.revision_id", minimum=1)
        source_url = _https_url(row["source_url"], f"{context}.source_url")
        for marker_index, marker_value in enumerate(
            _array(row["markers"], f"{context}.markers")
        ):
            marker_context = f"{context}.markers[{marker_index}]"
            marker = require_fields(
                marker_value,
                {"exit_id", "label", "coordinates", "source_feature_index"},
                marker_context,
            )
            exit_identity, identity_label = _exit_id(
                marker["exit_id"], f"{marker_context}.exit_id"
            )
            label = exact_str(marker["label"], f"{marker_context}.label")
            if label != identity_label:
                fail(marker_context, "marker label does not match its exit identity")
            key = (station_id, exit_identity)
            if key in markers:
                fail(marker_context, f"duplicate/conflicting marker identity {key}")
            markers[key] = {
                "station_id": station_id,
                "exit_id": exit_identity,
                "label": label,
                "coordinates": _coordinate(marker["coordinates"], f"{marker_context}.coordinates"),
                "source_feature_index": exact_int(
                    marker["source_feature_index"],
                    f"{marker_context}.source_feature_index",
                    minimum=0,
                ),
                "page_id": page_id,
                "revision_id": revision_id,
                "source_url": source_url,
            }
    unmatched: list[dict[str, object]] = []
    for index, value in enumerate(
        _array(root["unmatched_markers"], "kartographer.unmatched_markers")
    ):
        context = f"kartographer.unmatched_markers[{index}]"
        row = require_fields(
            value,
            {
                "station_id", "station_name", "label", "coordinates", "source_url",
                "source_feature_index", "reason", "matching_exit_ids",
            },
            context,
        )
        station_id = row["station_id"]
        if station_id is not None:
            station_id = _station_id(station_id, f"{context}.station_id")
        unmatched.append({
            "station_id": station_id,
            "station_name": exact_str(row["station_name"], f"{context}.station_name"),
            "label": exact_str(row["label"], f"{context}.label"),
            "coordinates": _coordinate(row["coordinates"], f"{context}.coordinates"),
            "source_url": _https_url(row["source_url"], f"{context}.source_url"),
            "source_feature_index": exact_int(
                row["source_feature_index"], f"{context}.source_feature_index", minimum=0
            ),
            "reason": exact_str(row["reason"], f"{context}.reason"),
            "matching_exit_ids": _string_array(
                row["matching_exit_ids"], f"{context}.matching_exit_ids"
            ),
        })
    return markers, unmatched


def _review_ledger(document: object) -> tuple[
    dict[str, dict[str, object]],
    dict[tuple[str, str], dict[str, object]],
    str | None,
    int,
    str,
]:
    root = require_fields(
        document,
        {"schema_version", "kind", "revision", "candidate_sha256", "entries"},
        "reviewed ledger",
    )
    if exact_int(root["schema_version"], "reviewed ledger.schema_version") != 1:
        fail("reviewed ledger.schema_version", "unsupported version")
    if exact_str(root["kind"], "reviewed ledger.kind") != "exit_coordinate_edit_ledger":
        fail("reviewed ledger.kind", "unsupported document kind")
    revision = exact_int(root["revision"], "reviewed ledger.revision", minimum=0)
    candidate_sha256 = _hash(root["candidate_sha256"], "reviewed ledger.candidate_sha256")
    by_hash: dict[str, dict[str, object]] = {}
    latest: dict[tuple[str, str], dict[str, object]] = {}
    previous_hash: str | None = None
    rows = _array(root["entries"], "reviewed ledger.entries")
    if revision != len(rows):
        fail("reviewed ledger.revision", "must equal the entry count")
    for index, value in enumerate(rows):
        context = f"reviewed ledger.entries[{index}]"
        row = require_fields(
            value,
            {
                "sequence", "edit_id", "saved_at", "station_id", "station_name", "exit_id",
                "label", "from", "to_coordinates", "note", "previous_entry_hash", "entry_hash",
            },
            context,
        )
        sequence = exact_int(row["sequence"], f"{context}.sequence", minimum=1)
        if sequence != index + 1:
            fail(f"{context}.sequence", f"expected contiguous sequence {index + 1}")
        edit_id = exact_str(row["edit_id"], f"{context}.edit_id")
        saved_at = _timestamp(row["saved_at"], f"{context}.saved_at")
        station_id = _station_id(row["station_id"], f"{context}.station_id")
        exit_identity, _ = _exit_id(row["exit_id"], f"{context}.exit_id")
        station_name = exact_str(row["station_name"], f"{context}.station_name")
        label = exact_str(row["label"], f"{context}.label")
        if label != exit_identity.split(":", 1)[1]:
            fail(context, "ledger label does not match exit identity")
        source_fields = {"source", "coordinates", "candidate_index"}
        if type(row["from"]) is dict and row["from"].get("source") == "human_reviewed":
            source_fields = {"source", "coordinates", "edit_id"}
        elif type(row["from"]) is dict and row["from"].get("source") == "audit_review_draft":
            source_fields = {
                "source", "coordinates", "decision_id", "draft_sha256",
                "bundle_sha256", "basis_source",
            }
        source = require_fields(row["from"], source_fields, f"{context}.from")
        source_name = exact_str(source["source"], f"{context}.from.source")
        source_coordinates = _coordinate(
            source["coordinates"], f"{context}.from.coordinates"
        )
        if source_name == "human_reviewed":
            source_edit_id = exact_str(source["edit_id"], f"{context}.from.edit_id")
            prior = latest.get((station_id, exit_identity))
            if (
                prior is None
                or source_edit_id != prior["edit_id"]
                or source_coordinates != prior["to_coordinates"]
            ):
                fail(context, "human-reviewed source does not match the prior edit")
        elif source_name == "audit_review_draft":
            decision_id = exact_str(source["decision_id"], f"{context}.from.decision_id")
            if re.fullmatch(r"D-[0-9]{3,}", decision_id) is None:
                fail(context, "audit review source has an invalid decision ID")
            _hash(source["draft_sha256"], f"{context}.from.draft_sha256")
            _hash(source["bundle_sha256"], f"{context}.from.bundle_sha256")
            exact_str(source["basis_source"], f"{context}.from.basis_source")
            if (station_id, exit_identity) in latest:
                fail(context, "a reviewed identity cannot restart from an audit draft")
        else:
            exact_int(source["candidate_index"], f"{context}.from.candidate_index", minimum=0)
            if (station_id, exit_identity) in latest:
                fail(context, "a reviewed identity cannot restart from external evidence")
        entry = {
            "sequence": sequence,
            "edit_id": edit_id,
            "saved_at": saved_at,
            "station_id": station_id,
            "station_name": station_name,
            "exit_id": exit_identity,
            "label": label,
            "from": dict(source),
            "to_coordinates": _coordinate(
                row["to_coordinates"], f"{context}.to_coordinates"
            ),
            "note": exact_str(row["note"], f"{context}.note", nonempty=False),
            "previous_entry_hash": _nullable_hash(
                row["previous_entry_hash"], f"{context}.previous_entry_hash"
            ),
        }
        if entry["previous_entry_hash"] != previous_hash:
            fail(context, "previous_entry_hash does not match the preceding ledger entry")
        entry_hash = _hash(row["entry_hash"], f"{context}.entry_hash")
        if entry_hash != reviewed_ledger_entry_hash(entry):
            fail(context, "entry_hash does not match the canonical ledger entry")
        if entry_hash in by_hash:
            fail(context, "duplicate ledger entry hash")
        entry["entry_hash"] = entry_hash
        prior_for_identity = latest.get((station_id, exit_identity))
        entry["origin_from"] = (
            dict(prior_for_identity["origin_from"])
            if prior_for_identity is not None
            else dict(source)
        )
        by_hash[entry_hash] = entry
        latest[(station_id, exit_identity)] = entry
        previous_hash = entry_hash
    return by_hash, latest, previous_hash, revision, candidate_sha256


def _reviewed_coordinates(
    document: object,
    ledger_document: object,
) -> dict[tuple[str, str], dict[str, object]]:
    by_hash, latest, head_hash, ledger_revision, candidate_sha256 = _review_ledger(
        ledger_document
    )
    root = require_fields(document, {"type", "metadata", "features"}, "reviewed coordinates")
    if exact_str(root["type"], "reviewed coordinates.type") != "FeatureCollection":
        fail("reviewed coordinates.type", "must be FeatureCollection")
    metadata = require_fields(
        root["metadata"],
        {
            "schema_version", "kind", "status", "revision", "feature_count", "ledger_file",
            "ledger_sha256", "ledger_head_entry_hash", "migrated_from", "selection_policy",
        },
        "reviewed coordinates.metadata",
    )
    if exact_int(metadata["schema_version"], "reviewed coordinates.metadata.schema_version") != 1:
        fail("reviewed coordinates.metadata.schema_version", "unsupported version")
    if (
        exact_str(metadata["kind"], "reviewed coordinates.metadata.kind")
        != "stage2_reviewed_exit_coordinates"
    ):
        fail("reviewed coordinates.metadata.kind", "unsupported document kind")
    if exact_str(metadata["status"], "reviewed coordinates.metadata.status") != "approved":
        fail("reviewed coordinates.metadata.status", "coordinates must be approved")
    if exact_int(metadata["revision"], "reviewed coordinates.metadata.revision", minimum=0) != ledger_revision:
        fail("reviewed coordinates.metadata.revision", "does not match the supplied ledger")
    if (
        exact_str(metadata["ledger_file"], "reviewed coordinates.metadata.ledger_file")
        != "config/stage2-coordinate-ledger.json"
    ):
        fail("reviewed coordinates.metadata.ledger_file", "unexpected ledger path")
    _hash(metadata["ledger_sha256"], "reviewed coordinates.metadata.ledger_sha256")
    migrated = require_fields(
        metadata["migrated_from"],
        {"repository", "kind", "release_revision", "ssot_sha256", "candidate_sha256"},
        "reviewed coordinates.metadata.migrated_from",
    )
    if exact_str(migrated["repository"], "reviewed coordinates.metadata.migrated_from.repository") != "mrt-data-v2":
        fail("reviewed coordinates.metadata.migrated_from.repository", "unexpected repository")
    if exact_str(migrated["kind"], "reviewed coordinates.metadata.migrated_from.kind") != "human_reviewed_exit_coordinate_ssot":
        fail("reviewed coordinates.metadata.migrated_from.kind", "unexpected source kind")
    migration_revision = exact_int(
        migrated["release_revision"],
        "reviewed coordinates.metadata.migrated_from.release_revision",
        minimum=0,
    )
    if migration_revision > ledger_revision:
        fail(
            "reviewed coordinates.metadata.migrated_from.release_revision",
            "cannot be newer than the current reviewed ledger",
        )
    _hash(migrated["ssot_sha256"], "reviewed coordinates.metadata.migrated_from.ssot_sha256")
    if _hash(migrated["candidate_sha256"], "reviewed coordinates.metadata.migrated_from.candidate_sha256") != candidate_sha256:
        fail("reviewed coordinates.metadata.migrated_from.candidate_sha256", "does not match ledger")
    if metadata["selection_policy"] != REVIEW_SELECTION_POLICY:
        fail("reviewed coordinates.metadata.selection_policy", "selection policy changed")
    declared_head = _nullable_hash(
        metadata["ledger_head_entry_hash"],
        "reviewed coordinates.metadata.ledger_head_entry_hash",
    )
    if declared_head != head_hash:
        fail("reviewed coordinates.metadata", "ledger head does not match the supplied ledger")
    rows = _array(root["features"], "reviewed coordinates.features")
    feature_count = exact_int(
        metadata["feature_count"], "reviewed coordinates.metadata.feature_count", minimum=0
    )
    if feature_count != len(rows):
        fail("reviewed coordinates.metadata.feature_count", "does not match features")

    reviewed: dict[tuple[str, str], dict[str, object]] = {}
    referenced_hashes: set[str] = set()
    for index, value in enumerate(rows):
        context = f"reviewed coordinates.features[{index}]"
        feature = require_fields(value, {"type", "id", "geometry", "properties"}, context)
        if exact_str(feature["type"], f"{context}.type") != "Feature":
            fail(f"{context}.type", "must be Feature")
        geometry = require_fields(feature["geometry"], {"type", "coordinates"}, f"{context}.geometry")
        if exact_str(geometry["type"], f"{context}.geometry.type") != "Point":
            fail(f"{context}.geometry.type", "must be Point")
        coordinates = _coordinate(geometry["coordinates"], f"{context}.geometry.coordinates")
        properties = require_fields(
            feature["properties"],
            {
                "station_id", "station_name", "station_codes", "exit_id", "label", "scope",
                "mode", "approved_at", "edit_id", "ledger_entry_hash", "based_on_source",
                "based_on_coordinates", "review_note", "source_guard",
            },
            f"{context}.properties",
        )
        station_id = _station_id(properties["station_id"], f"{context}.properties.station_id")
        exit_identity, identity_label = _exit_id(
            properties["exit_id"], f"{context}.properties.exit_id"
        )
        expected_feature_id = f"{station_id}:{exit_identity}"
        if exact_str(feature["id"], f"{context}.id") != expected_feature_id:
            fail(f"{context}.id", f"must be {expected_feature_id!r}")
        station_name = exact_str(
            properties["station_name"], f"{context}.properties.station_name"
        )
        station_codes = _string_array(
            properties["station_codes"], f"{context}.properties.station_codes", nonempty=True
        )
        label = exact_str(properties["label"], f"{context}.properties.label")
        if label != identity_label:
            fail(context, "feature label does not match exit identity")
        scope = exact_str(properties["scope"], f"{context}.properties.scope")
        if scope != exit_identity.split(":", 1)[0]:
            fail(context, "feature scope does not match exit identity")
        mode = exact_str(properties["mode"], f"{context}.properties.mode")
        if mode not in REVIEW_MODES:
            fail(f"{context}.properties.mode", f"must be one of {sorted(REVIEW_MODES)}")
        approved_at = _timestamp(
            properties["approved_at"], f"{context}.properties.approved_at"
        )
        edit_id = exact_str(properties["edit_id"], f"{context}.properties.edit_id")
        based_on_source = exact_str(
            properties["based_on_source"], f"{context}.properties.based_on_source"
        )
        based_on_coordinates = _coordinate(
            properties["based_on_coordinates"],
            f"{context}.properties.based_on_coordinates",
        )
        review_note = exact_str(
            properties["review_note"], f"{context}.properties.review_note", nonempty=False
        )
        guard = require_fields(
            properties["source_guard"],
            {"sgwiki_page_id", "sgwiki_revision_id"},
            f"{context}.properties.source_guard",
        )
        page_id = exact_int(
            guard["sgwiki_page_id"], f"{context}.properties.source_guard.sgwiki_page_id", minimum=1
        )
        revision_id = exact_int(
            guard["sgwiki_revision_id"],
            f"{context}.properties.source_guard.sgwiki_revision_id",
            minimum=1,
        )
        ledger_hash = _hash(
            properties["ledger_entry_hash"], f"{context}.properties.ledger_entry_hash"
        )
        if ledger_hash in referenced_hashes:
            fail(f"{context}.properties.ledger_entry_hash", "ledger entry is referenced twice")
        referenced_hashes.add(ledger_hash)
        entry = by_hash.get(ledger_hash)
        if entry is None:
            fail(context, "feature references an unknown ledger entry")
        if entry is not latest.get((station_id, exit_identity)):
            fail(context, "feature must reference the latest ledger entry for its identity")
        if (
            edit_id != entry["edit_id"]
            or approved_at != entry["saved_at"]
            or station_name != entry["station_name"]
            or label != entry["label"]
            or review_note != entry["note"]
        ):
            fail(context, "feature approval fields do not match the ledger entry")
        if coordinates != entry["to_coordinates"]:
            fail(context, "coordinates do not match the ledger entry")
        source = entry["origin_from"]
        if (
            based_on_source != source["source"]
            or based_on_coordinates != source["coordinates"]
        ):
            fail(context, "feature evidence fields do not match the ledger entry")
        key = (station_id, exit_identity)
        if key in reviewed:
            fail(context, f"duplicate reviewed identity {key}")
        reviewed[key] = {
            "review_id": edit_id,
            "station_id": station_id,
            "station_name": station_name,
            "station_codes": station_codes,
            "exit_id": exit_identity,
            "mode": mode,
            "coordinates": coordinates,
            "page_id": page_id,
            "revision_id": revision_id,
            "evidence_urls": [],
            "review_note": review_note,
            "approved_at": approved_at,
            "original_edit_id": edit_id,
            "original_entry_hash": ledger_hash,
            "entry_hash": ledger_hash,
        }
    if set(reviewed) != set(latest):
        missing = sorted(set(latest) - set(reviewed))
        extra = sorted(set(reviewed) - set(latest))
        fail("reviewed coordinates", f"features and latest ledger decisions differ; missing={missing}, extra={extra}")
    return reviewed


def _trusted_onemap_coordinates(document: object | None) -> dict[tuple[str, str], dict[str, object]]:
    """Validate the separately trusted legacy OneMap coordinate baseline."""

    if document is None:
        return {}
    root = require_fields(document, {"type", "metadata", "features"}, "trusted OneMap")
    if exact_str(root["type"], "trusted OneMap.type") != "FeatureCollection":
        fail("trusted OneMap.type", "must be FeatureCollection")
    metadata = require_fields(
        root["metadata"],
        {
            "schema_version", "kind", "status", "feature_count", "imported_at",
            "source", "trust_policy",
        },
        "trusted OneMap.metadata",
    )
    if exact_int(metadata["schema_version"], "trusted OneMap.metadata.schema_version") != 1:
        fail("trusted OneMap.metadata.schema_version", "unsupported version")
    if exact_str(metadata["kind"], "trusted OneMap.metadata.kind") != "stage2_trusted_onemap_baseline":
        fail("trusted OneMap.metadata.kind", "unsupported kind")
    if exact_str(metadata["status"], "trusted OneMap.metadata.status") != "trusted":
        fail("trusted OneMap.metadata.status", "must be trusted")
    imported_at = _timestamp(metadata["imported_at"], "trusted OneMap.metadata.imported_at")
    source = require_fields(
        metadata["source"],
        {
            "repository", "stage2_path", "stage2_sha256", "candidate_bundle_path",
            "candidate_bundle_sha256",
        },
        "trusted OneMap.metadata.source",
    )
    if exact_str(source["repository"], "trusted OneMap.metadata.source.repository") != "mrt-data-v2":
        fail("trusted OneMap.metadata.source.repository", "unexpected source repository")
    stage2_path = exact_str(source["stage2_path"], "trusted OneMap.metadata.source.stage2_path")
    candidate_path = exact_str(
        source["candidate_bundle_path"],
        "trusted OneMap.metadata.source.candidate_bundle_path",
    )
    for context, path_text in (
        ("trusted OneMap.metadata.source.stage2_path", stage2_path),
        ("trusted OneMap.metadata.source.candidate_bundle_path", candidate_path),
    ):
        path = PurePosixPath(path_text)
        if path.is_absolute() or ".." in path.parts:
            fail(context, "must be a repository-relative path")
    stage2_sha256 = _hash(
        source["stage2_sha256"], "trusted OneMap.metadata.source.stage2_sha256"
    )
    candidate_sha256 = _hash(
        source["candidate_bundle_sha256"],
        "trusted OneMap.metadata.source.candidate_bundle_sha256",
    )
    if metadata["trust_policy"] != TRUSTED_ONEMAP_POLICY:
        fail("trusted OneMap.metadata.trust_policy", "trust policy changed")
    rows = _array(root["features"], "trusted OneMap.features")
    if exact_int(
        metadata["feature_count"], "trusted OneMap.metadata.feature_count", minimum=0
    ) != len(rows):
        fail("trusted OneMap.metadata.feature_count", "does not match features")

    trusted: dict[tuple[str, str], dict[str, object]] = {}
    for index, value in enumerate(rows):
        context = f"trusted OneMap.features[{index}]"
        feature = require_fields(value, {"type", "id", "geometry", "properties"}, context)
        if exact_str(feature["type"], f"{context}.type") != "Feature":
            fail(f"{context}.type", "must be Feature")
        geometry = require_fields(
            feature["geometry"], {"type", "coordinates"}, f"{context}.geometry"
        )
        if exact_str(geometry["type"], f"{context}.geometry.type") != "Point":
            fail(f"{context}.geometry.type", "must be Point")
        coordinates = _coordinate(
            geometry["coordinates"], f"{context}.geometry.coordinates"
        )
        properties = require_fields(
            feature["properties"],
            {
                "station_id", "station_name", "station_codes", "exit_id", "label",
                "scopes", "trust_class", "source_guard", "provenance",
            },
            f"{context}.properties",
        )
        station_id = _station_id(properties["station_id"], f"{context}.properties.station_id")
        exit_identity, identity_label = _exit_id(
            properties["exit_id"], f"{context}.properties.exit_id"
        )
        expected_id = f"{station_id}:{exit_identity}"
        if exact_str(feature["id"], f"{context}.id") != expected_id:
            fail(f"{context}.id", f"must be {expected_id!r}")
        station_name = exact_str(
            properties["station_name"], f"{context}.properties.station_name"
        )
        station_codes = _string_array(
            properties["station_codes"], f"{context}.properties.station_codes", nonempty=True
        )
        label = exact_str(properties["label"], f"{context}.properties.label")
        if label != identity_label:
            fail(context, "feature label does not match exit identity")
        scopes = _string_array(
            properties["scopes"], f"{context}.properties.scopes", nonempty=True
        )
        expected_scopes = (
            ["MRT", "LRT"] if exit_identity.startswith("SHARED:")
            else [exit_identity.split(":", 1)[0]]
        )
        if scopes != expected_scopes:
            fail(context, "feature scopes do not match exit identity")
        if exact_str(
            properties["trust_class"], f"{context}.properties.trust_class"
        ) != "trusted_onemap_import":
            fail(f"{context}.properties.trust_class", "unsupported trust class")
        guard = require_fields(
            properties["source_guard"],
            {"sgwiki_page_id", "sgwiki_revision_id"},
            f"{context}.properties.source_guard",
        )
        page_id = exact_int(
            guard["sgwiki_page_id"],
            f"{context}.properties.source_guard.sgwiki_page_id",
            minimum=1,
        )
        revision_id = exact_int(
            guard["sgwiki_revision_id"],
            f"{context}.properties.source_guard.sgwiki_revision_id",
            minimum=1,
        )
        provenance = require_fields(
            properties["provenance"],
            {
                "source", "original_exit_id", "original_selection_status",
                "candidate_id", "candidate_scope", "candidate_label", "building",
                "query_scope", "response_files",
            },
            f"{context}.properties.provenance",
        )
        if exact_str(provenance["source"], f"{context}.properties.provenance.source") != "onemap":
            fail(f"{context}.properties.provenance.source", "must be onemap")
        original_exit_id = exact_str(
            provenance["original_exit_id"],
            f"{context}.properties.provenance.original_exit_id",
        )
        if (
            re.fullmatch(r"(?:MRT|LRT|UNSCOPED|SHARED):[A-Z0-9]+", original_exit_id) is None
            or original_exit_id.split(":", 1)[1] != label
        ):
            fail(context, "original exit identity does not match the current exit label")
        if exact_str(
            provenance["original_selection_status"],
            f"{context}.properties.provenance.original_selection_status",
        ) != "experimental_selected":
            fail(
                f"{context}.properties.provenance.original_selection_status",
                "unexpected legacy selection status",
            )
        candidate_id = exact_str(
            provenance["candidate_id"], f"{context}.properties.provenance.candidate_id"
        )
        candidate_scope = exact_str(
            provenance["candidate_scope"],
            f"{context}.properties.provenance.candidate_scope",
        )
        candidate_label = exact_str(
            provenance["candidate_label"],
            f"{context}.properties.provenance.candidate_label",
        )
        if candidate_label != label:
            fail(context, "OneMap candidate label does not match the current exit label")
        building = exact_str(
            provenance["building"], f"{context}.properties.provenance.building"
        )
        query_scope = exact_str(
            provenance["query_scope"], f"{context}.properties.provenance.query_scope"
        )
        if candidate_scope not in {"MRT", "LRT"} or query_scope != candidate_scope:
            fail(context, "OneMap candidate and query scopes must agree")
        if candidate_scope not in scopes or re.fullmatch(
            rf"{candidate_scope}:[0-9]+", candidate_id
        ) is None:
            fail(context, "OneMap candidate identity or scope is invalid for this exit")
        response_files: list[dict[str, str]] = []
        for response_index, response_value in enumerate(
            _array(
                provenance["response_files"],
                f"{context}.properties.provenance.response_files",
                nonempty=True,
            )
        ):
            response_context = (
                f"{context}.properties.provenance.response_files[{response_index}]"
            )
            response = require_fields(
                response_value, {"file", "sha256", "fetched_at"}, response_context
            )
            response_file = exact_str(response["file"], f"{response_context}.file")
            response_path = PurePosixPath(response_file)
            if (
                response_path.is_absolute()
                or ".." in response_path.parts
                or not response_file.startswith("sources/")
            ):
                fail(f"{response_context}.file", "must be a safe sources/ path")
            response_files.append({
                "file": response_file,
                "sha256": _hash(response["sha256"], f"{response_context}.sha256"),
                "fetched_at": _timestamp(
                    response["fetched_at"], f"{response_context}.fetched_at"
                ),
            })
        key = (station_id, exit_identity)
        if key in trusted:
            fail(context, f"duplicate trusted OneMap identity {key}")
        trusted[key] = {
            "station_id": station_id,
            "station_name": station_name,
            "station_codes": station_codes,
            "exit_id": exit_identity,
            "coordinates": coordinates,
            "page_id": page_id,
            "revision_id": revision_id,
            "imported_at": imported_at,
            "stage2_sha256": stage2_sha256,
            "candidate_sha256": candidate_sha256,
            "original_exit_id": original_exit_id,
            "candidate_id": candidate_id,
            "candidate_scope": candidate_scope,
            "candidate_label": candidate_label,
            "building": building,
            "query_scope": query_scope,
            "response_files": response_files,
        }
    return trusted


def _validate_supplemental_provenance(value: object, context: str) -> dict[str, object]:
    fields = {
        "kind", "source", "trust_class", "page_id", "revision_id", "imported_at",
        "decision_method", "decision_note", "evidence_urls", "candidate_bundle_sha256",
        "candidate_feature_index", "source_attributes",
    }
    row = require_fields(value, fields, context)
    if row["kind"] != "trusted_supplemental_baseline" or row["trust_class"] != "owner_authorized_import":
        fail(context, "unsupported supplemental trust class")
    provider = exact_str(row["source"], f"{context}.source")
    attribute_fields = {
        "lta_geojson": {"source_feature_index", "source_updated_at", "source_crc", "source_station_name", "source_exit_code"},
        "osm": {"source_url", "source_timestamp", "source_tags_json", "association_json", "label_evidence"},
        "onemap": {"candidate_id", "building", "query_scope", "response_file", "response_sha256", "fetched_at"},
    }
    if provider not in attribute_fields:
        fail(context, "unsupported supplemental provider")
    attrs = require_fields(row["source_attributes"], attribute_fields[provider], f"{context}.source_attributes")
    for key, text in attrs.items():
        exact_str(text, f"{context}.source_attributes.{key}")
    if provider == "osm":
        _https_url(attrs["source_url"], f"{context}.source_attributes.source_url")
        _timestamp(attrs["source_timestamp"], f"{context}.source_attributes.source_timestamp")
        for field, expected_type in (("source_tags_json", dict), ("association_json", list)):
            try:
                decoded = json.loads(attrs[field])
            except ValueError:
                fail(context, f"invalid {field}")
            if type(decoded) is not expected_type:
                fail(context, f"invalid {field} shape")
    elif provider == "onemap":
        _hash(attrs["response_sha256"], f"{context}.source_attributes.response_sha256")
        _timestamp(attrs["fetched_at"], f"{context}.source_attributes.fetched_at")
    for field in ("page_id", "revision_id"):
        exact_int(row[field], f"{context}.{field}", minimum=1)
    _timestamp(row["imported_at"], f"{context}.imported_at")
    _hash(row["candidate_bundle_sha256"], f"{context}.candidate_bundle_sha256")
    exact_int(row["candidate_feature_index"], f"{context}.candidate_feature_index", minimum=0)
    method = exact_str(row["decision_method"], f"{context}.decision_method")
    if method not in {"owner_accepted_candidate", "assistant_evidence_selection"}:
        fail(context, "unsupported decision method")
    exact_str(row["decision_note"], f"{context}.decision_note")
    for url in _string_array(row["evidence_urls"], f"{context}.evidence_urls", nonempty=True):
        _https_url(url, f"{context}.evidence_urls")
    return dict(row)


def _supplemental_coordinates(document: object | None) -> dict[tuple[str, str], dict[str, object]]:
    if document is None:
        return {}
    root = require_fields(document, {"type", "metadata", "features"}, "supplemental baseline")
    if root["type"] != "FeatureCollection":
        fail("supplemental baseline", "expected FeatureCollection")
    meta = require_fields(root["metadata"], {"schema_version", "kind", "status", "feature_count"}, "supplemental metadata")
    if exact_int(meta["schema_version"], "supplemental schema_version") != 1 or meta["kind"] != "stage2_trusted_supplemental_baseline" or meta["status"] != "trusted":
        fail("supplemental baseline", "unsupported metadata")
    features = _array(root["features"], "supplemental features")
    if exact_int(meta["feature_count"], "supplemental feature_count", minimum=0) != len(features):
        fail("supplemental baseline", "feature count mismatch")
    result = {}
    for index, value in enumerate(features):
        ctx = f"supplemental features[{index}]"
        f = require_fields(value, {"type", "id", "geometry", "properties"}, ctx)
        geometry = require_fields(f["geometry"], {"type", "coordinates"}, f"{ctx}.geometry")
        if f["type"] != "Feature" or geometry["type"] != "Point":
            fail(ctx, "expected Point feature")
        props = require_fields(f["properties"], {"station_id", "station_name", "station_codes", "exit_id", "coordinate_source"}, f"{ctx}.properties")
        station_id = _station_id(props["station_id"], f"{ctx}.station_id")
        exit_id, _ = _exit_id(props["exit_id"], f"{ctx}.exit_id")
        key = (station_id, exit_id)
        if key in result or f["id"] != f"{station_id}:{exit_id}":
            fail(ctx, "duplicate or inconsistent identity")
        provenance = _validate_supplemental_provenance(props["coordinate_source"], f"{ctx}.coordinate_source")
        result[key] = {
            "station_name": exact_str(props["station_name"], f"{ctx}.station_name"),
            "station_codes": _string_array(props["station_codes"], f"{ctx}.station_codes", nonempty=True),
            "coordinates": _coordinate(geometry["coordinates"], f"{ctx}.coordinates"),
            "page_id": provenance["page_id"], "revision_id": provenance["revision_id"],
            "provenance": provenance,
        }
    return result


def _review_item(
    severity: str,
    reason: str,
    station_id: str,
    exit_id: str,
    **detail: object,
) -> dict[str, object]:
    return {
        "severity": severity,
        "reason": reason,
        "station_id": station_id,
        "exit_id": exit_id,
        **detail,
    }


def review_evidence_urls(
    station_name: str,
    exit_label: str,
    sgwiki_url: str,
    *,
    station_id: str | None = None,
    exit_id: str | None = None,
) -> list[dict[str, str]]:
    """Build deterministic manual-review links without treating them as data.

    Operator links are intentionally absent because a station cannot be mapped
    to an operator URL safely from its name alone.
    """

    name = exact_str(station_name, "review evidence.station_name")
    label = exact_str(exit_label, "review evidence.exit_label")
    wiki = _https_url(sgwiki_url, "review evidence.sgwiki_url")
    query = quote_plus(f"{name} station exit {label}")
    if (station_id is None) != (exit_id is None):
        fail("review evidence", "station_id and exit_id must be supplied together")
    if station_id is not None and exit_id is not None:
        station_id = _station_id(station_id, "review evidence.station_id")
        exit_id, _identity_label = _exit_id(exit_id, "review evidence.exit_id")
    osm_url = EXACT_OSM_REVIEW_URLS.get(
        (station_id, exit_id),
        f"https://www.openstreetmap.org/search?query={query}",
    )
    return [
        {"source": "sgwiki", "url": wiki},
        {
            "source": "openstreetmap",
            "url": osm_url,
        },
        {
            "source": "onemap_search",
            "url": (
                "https://www.onemap.gov.sg/api/common/elastic/search"
                f"?searchVal={query}&returnGeom=Y&getAddrDetails=Y&pageNum=1"
            ),
        },
        {
            "source": "lta_train_station_exit_dataset",
            "url": (
                "https://data.gov.sg/datasets/"
                "d_b39d3a0871985372d7e1637193335da5/view"
            ),
        },
    ]


def _group_reviews(items: list[dict[str, object]]) -> dict[str, object]:
    severity_order = {"ERROR": 0, "WARNING": 1, "INFO": 2}
    sorted_items = sorted(
        items,
        key=lambda item: (
            severity_order[str(item["severity"])],
            str(item["reason"]),
            str(item.get("station_id", "")),
            str(item.get("exit_id", "")),
            json.dumps(item, sort_keys=True, separators=(",", ":")),
        ),
    )
    groups: list[dict[str, object]] = []
    for severity in ("ERROR", "WARNING", "INFO"):
        reasons = sorted({str(item["reason"]) for item in sorted_items if item["severity"] == severity})
        for reason in reasons:
            members = [
                item for item in sorted_items
                if item["severity"] == severity and item["reason"] == reason
            ]
            groups.append({
                "severity": severity,
                "reason": reason,
                "count": len(members),
                "items": members,
            })
    highest = "ERROR" if any(item["severity"] == "ERROR" for item in items) else (
        "WARNING" if any(item["severity"] == "WARNING" for item in items) else None
    )
    status = "failed" if highest == "ERROR" else (
        "review_required" if highest == "WARNING" else "complete"
    )
    return {
        "schema_version": 1,
        "kind": "stage2_coordinate_reviews",
        "status": status,
        "highest_severity": highest,
        "group_count": len(groups),
        "finding_count": len(items),
        "groups": groups,
    }


def _source_guard_matches(
    station: dict[str, object], source: dict[str, object]
) -> bool:
    station_source = station["source"]
    assert isinstance(station_source, dict)
    return (
        source["page_id"] == station_source["page_id"]
        and source["revision_id"] == station_source["revision_id"]
    )


def _reviewed_provenance(entry: dict[str, object]) -> dict[str, object]:
    return {
        "kind": "human_reviewed",
        "mode": entry["mode"],
        "review_id": entry["review_id"],
        "ledger_entry_hash": entry["entry_hash"],
        "page_id": entry["page_id"],
        "revision_id": entry["revision_id"],
        "evidence_urls": entry["evidence_urls"],
        "review_note": entry["review_note"],
        "approved_at": entry["approved_at"],
        "original_edit_id": entry["original_edit_id"],
        "original_entry_hash": entry["original_entry_hash"],
    }


def _kartographer_provenance(marker: dict[str, object]) -> dict[str, object]:
    return {
        "kind": "sgwiki_kartographer",
        "page_id": marker["page_id"],
        "revision_id": marker["revision_id"],
        "url": marker["source_url"],
        "source_feature_index": marker["source_feature_index"],
    }


def _trusted_onemap_provenance(entry: dict[str, object]) -> dict[str, object]:
    return {
        "kind": "trusted_onemap_baseline",
        "trust_class": "trusted_onemap_import",
        "page_id": entry["page_id"],
        "revision_id": entry["revision_id"],
        "imported_at": entry["imported_at"],
        "legacy_stage2_sha256": entry["stage2_sha256"],
        "legacy_candidate_bundle_sha256": entry["candidate_sha256"],
        "original_exit_id": entry["original_exit_id"],
        "candidate_id": entry["candidate_id"],
        "candidate_scope": entry["candidate_scope"],
        "building": entry["building"],
        "query_scope": entry["query_scope"],
        "response_files": entry["response_files"],
    }


def build_stage2(
    stage1_document: object,
    kartographer_document: object,
    reviewed_coordinates_document: object,
    reviewed_ledger_document: object,
    trusted_onemap_document: object | None = None,
    trusted_supplemental_document: object | None = None,
) -> Stage2BuildResult:
    """Resolve exits from reviewed, Kartographer, then trusted OneMap evidence."""

    stations, identities = _stage1_identity_index(stage1_document)
    if type(kartographer_document) is not dict:
        fail("kartographer", "expected table")
    kartographer_kind = kartographer_document.get("kind")
    prepared_kartographer = (
        prepare_kartographer_exits(stage1_document, kartographer_document)
        if kartographer_kind == "sgwiki_kartographer_snapshot"
        else kartographer_document
    )
    markers, unmatched_markers = _kartographer_markers(prepared_kartographer)
    reviewed = _reviewed_coordinates(reviewed_coordinates_document, reviewed_ledger_document)
    trusted_onemap = _trusted_onemap_coordinates(trusted_onemap_document)
    supplemental = _supplemental_coordinates(trusted_supplemental_document)
    if set(supplemental) & set(trusted_onemap):
        fail("supplemental baseline", "overlaps trusted OneMap baseline; resolve conflicting configuration")
    review_items: list[dict[str, object]] = []

    for marker in unmatched_markers:
        review_items.append({
            "severity": "WARNING",
            "reason": marker["reason"],
            "station_id": marker["station_id"],
            "station_name": marker["station_name"],
            "exit_id": None,
            "exit_label": marker["label"],
            "matching_exit_ids": marker["matching_exit_ids"],
            "coordinates": marker["coordinates"],
            "source_url": marker["source_url"],
            "source_feature_index": marker["source_feature_index"],
        })

    for key in sorted(set(markers) - set(identities)):
        marker = markers[key]
        review_items.append(_review_item(
            "WARNING", "unmatched_kartographer_marker", key[0], key[1],
            coordinates=marker["coordinates"], source_url=marker["source_url"],
            source_feature_index=marker["source_feature_index"],
        ))
    for key in sorted(set(reviewed) - set(identities)):
        entry = reviewed[key]
        review_items.append(_review_item(
            "ERROR", "reviewed_coordinate_identity_missing_from_stage1", key[0], key[1],
            review_id=entry["review_id"], ledger_entry_hash=entry["entry_hash"],
        ))
    for key in sorted(set(trusted_onemap) - set(identities)):
        entry = trusted_onemap[key]
        review_items.append(_review_item(
            "ERROR", "trusted_onemap_identity_missing_from_stage1", key[0], key[1],
            original_exit_id=entry["original_exit_id"],
            candidate_id=entry["candidate_id"],
        ))

    for key in sorted(set(supplemental) - set(identities)):
        review_items.append(_review_item(
            "ERROR", "supplemental_identity_missing_from_stage1", key[0], key[1],
        ))

    source_counts = {"sgwiki_kartographer": 0, "human_reviewed_override": 0,
                     "trusted_supplemental_baseline": 0,
                     "human_reviewed_gap_fill": 0, "trusted_onemap_baseline": 0}
    unresolved_count = 0
    output_stations: list[dict[str, object]] = []
    features: list[dict[str, object]] = []
    coverage_stations: list[dict[str, object]] = []

    for station in stations:
        output_exits: list[dict[str, object]] = []
        unresolved_ids: list[str] = []
        resolved_count = 0
        for exit_record in station["exits"]:
            assert isinstance(exit_record, dict)
            station_id, exit_id = str(station["id"]), str(exit_record["id"])
            key = (station_id, exit_id)
            marker = markers.get(key)
            reviewed_entry = reviewed.get(key)
            trusted_onemap_entry = trusted_onemap.get(key)
            supplemental_entry = supplemental.get(key)
            supplemental_valid = (
                supplemental_entry is not None
                and _source_guard_matches(station, supplemental_entry)
                and supplemental_entry["station_codes"] == station["codes"]
                and supplemental_entry["station_name"] == station["name"]
            )
            reviewed_valid = (
                reviewed_entry is not None
                and _source_guard_matches(station, reviewed_entry)
            )
            marker_valid = marker is not None and _source_guard_matches(station, marker)
            trusted_onemap_valid = (
                trusted_onemap_entry is not None
                and _source_guard_matches(station, trusted_onemap_entry)
                and trusted_onemap_entry["station_codes"] == station["codes"]
                and trusted_onemap_entry["station_name"] == station["name"]
            )

            if reviewed_entry is not None and not reviewed_valid:
                source = station["source"]
                assert isinstance(source, dict)
                review_items.append(_review_item(
                    "ERROR", "reviewed_coordinate_source_guard_changed", station_id, exit_id,
                    review_id=reviewed_entry["review_id"],
                    expected_page_id=reviewed_entry["page_id"],
                    expected_revision_id=reviewed_entry["revision_id"],
                    actual_page_id=source["page_id"],
                    actual_revision_id=source["revision_id"],
                ))
            if marker is not None and not marker_valid:
                source = station["source"]
                assert isinstance(source, dict)
                review_items.append(_review_item(
                    "ERROR", "kartographer_source_guard_mismatch", station_id, exit_id,
                    marker_page_id=marker["page_id"],
                    marker_revision_id=marker["revision_id"],
                    stage1_page_id=source["page_id"],
                    stage1_revision_id=source["revision_id"],
                    source_url=marker["source_url"],
                ))
            if (
                trusted_onemap_entry is not None
                and not trusted_onemap_valid
                and reviewed_entry is None
            ):
                source = station["source"]
                assert isinstance(source, dict)
                review_items.append(_review_item(
                    "ERROR", "trusted_onemap_source_guard_changed", station_id, exit_id,
                    expected_page_id=trusted_onemap_entry["page_id"],
                    expected_revision_id=trusted_onemap_entry["revision_id"],
                    actual_page_id=source["page_id"],
                    actual_revision_id=source["revision_id"],
                    original_exit_id=trusted_onemap_entry["original_exit_id"],
                    candidate_id=trusted_onemap_entry["candidate_id"],
                    expected_station_codes=trusted_onemap_entry["station_codes"],
                    actual_station_codes=station["codes"],
                    expected_station_name=trusted_onemap_entry["station_name"],
                    actual_station_name=station["name"],
                ))

            coordinates: list[float] | None = None
            if supplemental_entry is not None and not supplemental_valid and reviewed_entry is None:
                review_items.append(_review_item(
                    "ERROR", "supplemental_source_guard_changed", station_id, exit_id,
                    expected_page_id=supplemental_entry["page_id"],
                    expected_revision_id=supplemental_entry["revision_id"],
                    actual_page_id=station["source"]["page_id"],
                    actual_revision_id=station["source"]["revision_id"],
                    expected_station_codes=supplemental_entry["station_codes"],
                    actual_station_codes=station["codes"],
                ))
            provenance: dict[str, object] | None = None
            if (
                reviewed_valid and reviewed_entry is not None
                and reviewed_entry["mode"] == "override"
            ):
                coordinates = list(reviewed_entry["coordinates"])
                provenance = _reviewed_provenance(reviewed_entry)
                source_counts["human_reviewed_override"] += 1
            elif marker_valid and marker is not None:
                coordinates = list(marker["coordinates"])
                provenance = _kartographer_provenance(marker)
                source_counts["sgwiki_kartographer"] += 1
                if supplemental_valid:
                    review_items.append(_review_item(
                        "WARNING", "kartographer_supersedes_supplemental_baseline",
                        station_id, exit_id,
                        previous_coordinates=supplemental_entry["coordinates"],
                        coordinates=coordinates,
                    ))
                if (
                    reviewed_valid and reviewed_entry is not None
                    and reviewed_entry["mode"] == "gap_fill"
                ):
                    review_items.append(_review_item(
                        "WARNING", "kartographer_supersedes_reviewed_gap_fill",
                        station_id, exit_id,
                        reviewed_coordinates=reviewed_entry["coordinates"],
                        kartographer_coordinates=marker["coordinates"],
                        review_id=reviewed_entry["review_id"],
                        source_url=marker["source_url"],
                    ))
                if trusted_onemap_valid and trusted_onemap_entry is not None:
                    review_items.append(_review_item(
                        "WARNING", "kartographer_supersedes_trusted_onemap_baseline",
                        station_id, exit_id,
                        trusted_onemap_coordinates=trusted_onemap_entry["coordinates"],
                        kartographer_coordinates=marker["coordinates"],
                        original_exit_id=trusted_onemap_entry["original_exit_id"],
                        candidate_id=trusted_onemap_entry["candidate_id"],
                        source_url=marker["source_url"],
                    ))
            elif (
                reviewed_valid and reviewed_entry is not None
                and reviewed_entry["mode"] == "gap_fill"
            ):
                coordinates = list(reviewed_entry["coordinates"])
                provenance = _reviewed_provenance(reviewed_entry)
                source_counts["human_reviewed_gap_fill"] += 1
            elif (
                trusted_onemap_valid
                and trusted_onemap_entry is not None
                and reviewed_entry is None
            ):
                coordinates = list(trusted_onemap_entry["coordinates"])
                provenance = _trusted_onemap_provenance(trusted_onemap_entry)
                source_counts["trusted_onemap_baseline"] += 1
            elif supplemental_valid and reviewed_entry is None:
                coordinates = list(supplemental_entry["coordinates"])
                provenance = supplemental_entry["provenance"]
                source_counts["trusted_supplemental_baseline"] += 1

            if coordinates is None:
                unresolved_count += 1
                unresolved_ids.append(exit_id)
                source = station["source"]
                assert isinstance(source, dict)
                review_items.append(_review_item(
                    "WARNING", "exit_coordinate_unresolved", station_id, exit_id,
                    station_name=station["name"], exit_label=exit_record["label"],
                    evidence_urls=review_evidence_urls(
                        str(station["name"]),
                        str(exit_record["label"]),
                        str(source["url"]),
                        station_id=station_id,
                        exit_id=exit_id,
                    ),
                ))
                output_exits.append({
                    **exit_record,
                    "coordinate_status": "unresolved",
                    "coordinates": None,
                    "coordinate_source": None,
                })
                continue

            resolved_count += 1
            output_exit = {
                **exit_record,
                "coordinate_status": "resolved",
                "coordinates": coordinates,
                "coordinate_source": provenance,
            }
            output_exits.append(output_exit)
            features.append({
                "type": "Feature",
                "id": f"{station_id}/{exit_id}",
                "geometry": {"type": "Point", "coordinates": coordinates},
                "properties": {
                    "station_id": station_id,
                    "station_name": station["name"],
                    "station_codes": station["codes"],
                    "exit_id": exit_id,
                    "label": exit_record["label"],
                    "scopes": exit_record["scopes"],
                    "coordinate_source": provenance,
                },
            })
        output_stations.append({
            "id": station["id"],
            "name": station["name"],
            "codes": station["codes"],
            "source": station["source"],
            "exits": output_exits,
        })
        coverage_stations.append({
            "station_id": station["id"],
            "station_name": station["name"],
            "exit_count": len(output_exits),
            "resolved_count": resolved_count,
            "unresolved_count": len(unresolved_ids),
            "unresolved_exit_ids": unresolved_ids,
        })

    reviews = _group_reviews(review_items)
    total_count = len(identities)
    resolved_total = total_count - unresolved_count
    publication_allowed = reviews["status"] == "complete" and unresolved_count == 0
    summary = {
        "station_count": len(stations),
        "exit_identity_count": total_count,
        "resolved_exit_count": resolved_total,
        "unresolved_exit_count": unresolved_count,
        "kartographer_exit_count": source_counts["sgwiki_kartographer"],
        "reviewed_override_exit_count": source_counts["human_reviewed_override"],
        "reviewed_gap_fill_exit_count": source_counts["human_reviewed_gap_fill"],
        "trusted_onemap_baseline_exit_count": source_counts["trusted_onemap_baseline"],
        "trusted_supplemental_baseline_exit_count": source_counts["trusted_supplemental_baseline"],
        "review_finding_count": reviews["finding_count"],
    }
    dataset = {
        "schema_version": 1,
        "kind": "mrt_wayfinding_stage_2",
        "status": reviews["status"],
        "publication_allowed": publication_allowed,
        "stage1_sha256": _canonical_digest(stage1_document),
        "summary": summary,
        "stations": output_stations,
    }
    geojson = {
        "type": "FeatureCollection",
        "metadata": {
            "schema_version": 1,
            "kind": "mrt_wayfinding_stage_2_exit_coordinates",
            "status": reviews["status"],
            "feature_count": len(features),
            "stage1_sha256": dataset["stage1_sha256"],
        },
        "features": features,
    }
    coverage = {
        "schema_version": 1,
        "kind": "stage2_coordinate_coverage",
        "status": "complete" if unresolved_count == 0 else "incomplete",
        "summary": summary,
        "stations": coverage_stations,
    }
    return Stage2BuildResult(dataset, geojson, reviews, coverage)


def _validate_stage2_comparison_document(
    document: object, context: str
) -> dict[tuple[str, str], dict[str, object]]:
    root = require_fields(
        document,
        {
            "schema_version", "kind", "status", "publication_allowed", "stage1_sha256",
            "summary", "stations",
        },
        context,
    )
    if exact_int(root["schema_version"], f"{context}.schema_version") != 1:
        fail(f"{context}.schema_version", "unsupported version")
    if exact_str(root["kind"], f"{context}.kind") != "mrt_wayfinding_stage_2":
        fail(f"{context}.kind", "unsupported kind")
    status = exact_str(root["status"], f"{context}.status")
    if status not in {"complete", "review_required", "failed"}:
        fail(f"{context}.status", "unsupported status")
    exact_bool(root["publication_allowed"], f"{context}.publication_allowed")
    _hash(root["stage1_sha256"], f"{context}.stage1_sha256")
    summary_fields = {
            "station_count", "exit_identity_count", "resolved_exit_count",
            "unresolved_exit_count", "kartographer_exit_count",
            "reviewed_override_exit_count", "reviewed_gap_fill_exit_count",
            "trusted_onemap_baseline_exit_count", "review_finding_count",
            "trusted_supplemental_baseline_exit_count",
    }
    # The two exact earlier summary contracts remain readable as baselines.
    legacy_summary = (
        type(root["summary"]) is dict
        and set(root["summary"]) == summary_fields - {"trusted_onemap_baseline_exit_count", "trusted_supplemental_baseline_exit_count"}
    )
    onemap_summary = (
        type(root["summary"]) is dict
        and set(root["summary"]) == summary_fields - {"trusted_supplemental_baseline_exit_count"}
    )
    expected_summary = summary_fields
    if legacy_summary:
        expected_summary = summary_fields - {"trusted_onemap_baseline_exit_count", "trusted_supplemental_baseline_exit_count"}
    elif onemap_summary:
        expected_summary = summary_fields - {"trusted_supplemental_baseline_exit_count"}
    summary = require_fields(
        root["summary"],
        expected_summary,
        f"{context}.summary",
    )
    for field, value in summary.items():
        exact_int(value, f"{context}.summary.{field}", minimum=0)
    identities: dict[tuple[str, str], dict[str, object]] = {}
    station_ids: set[str] = set()
    for station_index, value in enumerate(_array(root["stations"], f"{context}.stations")):
        station_context = f"{context}.stations[{station_index}]"
        station = require_fields(value, {"id", "name", "codes", "source", "exits"}, station_context)
        station_id = _station_id(station["id"], f"{station_context}.id")
        if station_id in station_ids:
            fail(station_context, f"duplicate station {station_id}")
        station_ids.add(station_id)
        exact_str(station["name"], f"{station_context}.name")
        _string_array(station["codes"], f"{station_context}.codes", nonempty=True)
        source = require_fields(
            station["source"], {"kind", "page_id", "revision_id", "url"},
            f"{station_context}.source",
        )
        if exact_str(source["kind"], f"{station_context}.source.kind") != "sgwiki_station_page":
            fail(f"{station_context}.source.kind", "unsupported source")
        exact_int(source["page_id"], f"{station_context}.source.page_id", minimum=1)
        exact_int(source["revision_id"], f"{station_context}.source.revision_id", minimum=1)
        _https_url(source["url"], f"{station_context}.source.url")
        for exit_index, exit_value in enumerate(_array(station["exits"], f"{station_context}.exits")):
            exit_context = f"{station_context}.exits[{exit_index}]"
            exit_row = require_fields(
                exit_value,
                {"id", "label", "scopes", "coordinate_status", "coordinates", "coordinate_source"},
                exit_context,
            )
            exit_identity, identity_label = _exit_id(exit_row["id"], f"{exit_context}.id")
            if exact_str(exit_row["label"], f"{exit_context}.label") != identity_label:
                fail(exit_context, "label does not match exit identity")
            _string_array(exit_row["scopes"], f"{exit_context}.scopes", nonempty=True)
            coordinate_status = exact_str(
                exit_row["coordinate_status"], f"{exit_context}.coordinate_status"
            )
            if coordinate_status == "unresolved":
                if exit_row["coordinates"] is not None or exit_row["coordinate_source"] is not None:
                    fail(exit_context, "unresolved exits cannot contain coordinate data")
            elif coordinate_status == "resolved":
                _coordinate(exit_row["coordinates"], f"{exit_context}.coordinates")
                if type(exit_row["coordinate_source"]) is not dict:
                    fail(f"{exit_context}.coordinate_source", "expected table")
                coordinate_source = exit_row["coordinate_source"]
                source_kind = exact_str(
                    coordinate_source.get("kind"), f"{exit_context}.coordinate_source.kind"
                )
                if source_kind == "sgwiki_kartographer":
                    source_row = require_fields(
                        coordinate_source,
                        {"kind", "page_id", "revision_id", "url", "source_feature_index"},
                        f"{exit_context}.coordinate_source",
                    )
                    exact_int(
                        source_row["page_id"],
                        f"{exit_context}.coordinate_source.page_id",
                        minimum=1,
                    )
                    exact_int(
                        source_row["revision_id"],
                        f"{exit_context}.coordinate_source.revision_id",
                        minimum=1,
                    )
                    _https_url(source_row["url"], f"{exit_context}.coordinate_source.url")
                    exact_int(
                        source_row["source_feature_index"],
                        f"{exit_context}.coordinate_source.source_feature_index",
                        minimum=0,
                    )
                elif source_kind == "human_reviewed":
                    source_row = require_fields(
                        coordinate_source,
                        {
                            "kind", "mode", "review_id", "ledger_entry_hash", "page_id",
                            "revision_id", "evidence_urls", "review_note", "approved_at",
                            "original_edit_id", "original_entry_hash",
                        },
                        f"{exit_context}.coordinate_source",
                    )
                    mode = exact_str(
                        source_row["mode"], f"{exit_context}.coordinate_source.mode"
                    )
                    if mode not in REVIEW_MODES:
                        fail(
                            f"{exit_context}.coordinate_source.mode",
                            f"must be one of {sorted(REVIEW_MODES)}",
                        )
                    exact_str(
                        source_row["review_id"],
                        f"{exit_context}.coordinate_source.review_id",
                    )
                    _hash(
                        source_row["ledger_entry_hash"],
                        f"{exit_context}.coordinate_source.ledger_entry_hash",
                    )
                    exact_int(
                        source_row["page_id"],
                        f"{exit_context}.coordinate_source.page_id",
                        minimum=1,
                    )
                    exact_int(
                        source_row["revision_id"],
                        f"{exit_context}.coordinate_source.revision_id",
                        minimum=1,
                    )
                    for url_index, url in enumerate(
                        _array(
                            source_row["evidence_urls"],
                            f"{exit_context}.coordinate_source.evidence_urls",
                        )
                    ):
                        _https_url(
                            url,
                            f"{exit_context}.coordinate_source.evidence_urls[{url_index}]",
                        )
                    exact_str(
                        source_row["review_note"],
                        f"{exit_context}.coordinate_source.review_note",
                        nonempty=False,
                    )
                    _timestamp(
                        source_row["approved_at"],
                        f"{exit_context}.coordinate_source.approved_at",
                    )
                    exact_str(
                        source_row["original_edit_id"],
                        f"{exit_context}.coordinate_source.original_edit_id",
                    )
                    _hash(
                        source_row["original_entry_hash"],
                        f"{exit_context}.coordinate_source.original_entry_hash",
                    )
                elif source_kind == "trusted_supplemental_baseline":
                    if legacy_summary or onemap_summary:
                        fail(exit_context, "older summary cannot contain supplemental coordinates")
                    _validate_supplemental_provenance(coordinate_source, f"{exit_context}.coordinate_source")
                elif source_kind == "trusted_onemap_baseline":
                    if legacy_summary:
                        fail(exit_context, "legacy summary cannot contain trusted OneMap coordinates")
                    source_row = require_fields(
                        coordinate_source,
                        {
                            "kind", "trust_class", "page_id", "revision_id",
                            "imported_at", "legacy_stage2_sha256",
                            "legacy_candidate_bundle_sha256", "original_exit_id",
                            "candidate_id", "candidate_scope", "building", "query_scope",
                            "response_files",
                        },
                        f"{exit_context}.coordinate_source",
                    )
                    if exact_str(
                        source_row["trust_class"],
                        f"{exit_context}.coordinate_source.trust_class",
                    ) != "trusted_onemap_import":
                        fail(
                            f"{exit_context}.coordinate_source.trust_class",
                            "unsupported trust class",
                        )
                    exact_int(
                        source_row["page_id"],
                        f"{exit_context}.coordinate_source.page_id",
                        minimum=1,
                    )
                    exact_int(
                        source_row["revision_id"],
                        f"{exit_context}.coordinate_source.revision_id",
                        minimum=1,
                    )
                    _timestamp(
                        source_row["imported_at"],
                        f"{exit_context}.coordinate_source.imported_at",
                    )
                    _hash(
                        source_row["legacy_stage2_sha256"],
                        f"{exit_context}.coordinate_source.legacy_stage2_sha256",
                    )
                    _hash(
                        source_row["legacy_candidate_bundle_sha256"],
                        f"{exit_context}.coordinate_source.legacy_candidate_bundle_sha256",
                    )
                    for field in (
                        "original_exit_id", "candidate_id", "candidate_scope", "building",
                        "query_scope",
                    ):
                        exact_str(
                            source_row[field],
                            f"{exit_context}.coordinate_source.{field}",
                        )
                    for response_index, response_value in enumerate(
                        _array(
                            source_row["response_files"],
                            f"{exit_context}.coordinate_source.response_files",
                            nonempty=True,
                        )
                    ):
                        response_context = (
                            f"{exit_context}.coordinate_source.response_files[{response_index}]"
                        )
                        response = require_fields(
                            response_value,
                            {"file", "sha256", "fetched_at"},
                            response_context,
                        )
                        exact_str(response["file"], f"{response_context}.file")
                        _hash(response["sha256"], f"{response_context}.sha256")
                        _timestamp(response["fetched_at"], f"{response_context}.fetched_at")
                else:
                    fail(
                        f"{exit_context}.coordinate_source.kind",
                        "unsupported coordinate source",
                    )
            else:
                fail(f"{exit_context}.coordinate_status", "unsupported status")
            key = (station_id, exit_identity)
            if key in identities:
                fail(exit_context, f"duplicate identity {key}")
            identities[key] = {
                "station": station,
                "exit": exit_row,
            }
    return identities


def _distance_metres(first: list[float], second: list[float]) -> float:
    longitude1, latitude1 = map(radians, first)
    longitude2, latitude2 = map(radians, second)
    delta_longitude = longitude2 - longitude1
    delta_latitude = latitude2 - latitude1
    value = (
        sin(delta_latitude / 2) ** 2
        + cos(latitude1) * cos(latitude2) * sin(delta_longitude / 2) ** 2
    )
    return 6_371_008.8 * 2 * asin(sqrt(value))


def compare_stage2(
    current: object,
    baseline: object | None,
    *,
    movement_threshold_metres: float = 25.0,
) -> dict[str, object]:
    """Compare Stage 2 identities, coordinates and provenance with a baseline."""

    if type(movement_threshold_metres) is not float or movement_threshold_metres <= 0:
        fail("stage2 comparison.movement_threshold_metres", "must be a positive exact float")
    current_by = _validate_stage2_comparison_document(current, "stage2 comparison.current")
    if baseline is None:
        return {
            "schema_version": 1,
            "kind": "stage2_coordinate_changes",
            "status": "baseline_unavailable",
            "highest_severity": "WARNING",
            "count": 1,
            "items": [{"severity": "WARNING", "reason": "approved_stage2_baseline_unavailable"}],
        }
    baseline_by = _validate_stage2_comparison_document(
        baseline, "stage2 comparison.baseline"
    )
    items: list[dict[str, object]] = []
    for key in sorted(set(current_by) - set(baseline_by)):
        items.append(_review_item("ERROR", "exit_identity_added", key[0], key[1]))
    for key in sorted(set(baseline_by) - set(current_by)):
        items.append(_review_item("ERROR", "exit_identity_removed", key[0], key[1]))
    for key in sorted(set(current_by) & set(baseline_by)):
        current_item, baseline_item = current_by[key], baseline_by[key]
        current_exit, baseline_exit = current_item["exit"], baseline_item["exit"]
        current_coordinates = current_exit["coordinates"]
        baseline_coordinates = baseline_exit["coordinates"]
        if baseline_coordinates is not None and current_coordinates is None:
            items.append(_review_item(
                "ERROR", "coordinate_became_unresolved", key[0], key[1],
                previous_coordinates=baseline_coordinates,
            ))
        elif baseline_coordinates is None and current_coordinates is not None:
            items.append(_review_item(
                "WARNING", "coordinate_became_resolved", key[0], key[1],
                coordinates=current_coordinates,
            ))
        elif baseline_coordinates is not None and current_coordinates is not None:
            distance = _distance_metres(baseline_coordinates, current_coordinates)
            if distance > 0:
                severity = "WARNING" if distance > movement_threshold_metres else "INFO"
                items.append(_review_item(
                    severity, "coordinate_moved", key[0], key[1],
                    from_coordinates=baseline_coordinates,
                    to_coordinates=current_coordinates,
                    distance_metres=round(distance, 3),
                    review_threshold_metres=movement_threshold_metres,
                ))
        current_source = current_item["station"]["source"]
        baseline_source = baseline_item["station"]["source"]
        if current_source["page_id"] != baseline_source["page_id"]:
            items.append(_review_item(
                "ERROR", "sgwiki_page_identity_changed", key[0], key[1],
                previous_page_id=baseline_source["page_id"],
                current_page_id=current_source["page_id"],
            ))
        elif current_source["revision_id"] != baseline_source["revision_id"]:
            coordinate_source = current_exit["coordinate_source"]
            if (
                coordinate_source is not None
                and coordinate_source.get("kind") == "sgwiki_kartographer"
            ):
                items.append(_review_item(
                    "WARNING", "kartographer_source_page_revision_changed", key[0], key[1],
                    previous_revision_id=baseline_source["revision_id"],
                    current_revision_id=current_source["revision_id"],
                ))
    grouped = _group_reviews(items)
    return {
        "schema_version": 1,
        "kind": "stage2_coordinate_changes",
        "status": grouped["status"],
        "highest_severity": grouped["highest_severity"],
        "count": len(items),
        "items": [item for group in grouped["groups"] for item in group["items"]],
    }


__all__ = [
    "ContractError",
    "Stage2BuildResult",
    "build_stage2",
    "compare_stage2",
    "prepare_kartographer_exits",
    "review_evidence_urls",
    "reviewed_ledger_entry_hash",
]
