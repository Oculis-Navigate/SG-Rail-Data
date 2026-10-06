"""Build the complete Stage 1 semantic dataset from native source snapshots."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from .contracts import ContractError, fail, require_fields
from .sources.lta_bus_stops import BusStop, select_required_bus_stops
from .sources.lta_gtfs import match_catalog_stations
from .sources.sgwiki_parser import rail_scopes
from .stage1 import ReviewedDecisions, build_stage1_reviewed_links
from .stage1_layout import normalize_layout
from .station_catalog import StationCatalog, station_slug


ENTRY_FIELDS = {
    "labels", "scopes", "label_cell_verbatim", "label_note_verbatim",
    "row_verbatim", "table", "row", "destination_verbatim", "label_markup",
    "wheelchair_access", "has_uninterpreted_icon", "destination_missing",
}
STATUS_FIELDS = {
    "labels", "scopes", "destination_verbatim", "status", "alternative_exits",
    "status_verbatim", "status_markup",
}
OPERATING_HOURS_FIELDS = {
    "labels", "scopes", "days", "opens", "closes", "timezone",
    "source_verbatim", "grammar",
}
BUS_FIELDS = {"status", "labels", "rows", "unlinked_rows", "reviews"}
BUS_ROW_FIELDS = {"row", "bus_stop_code", "stop_verbatim", "labels", "service_verbatim"}
UNLINKED_BUS_ROW_FIELDS = {"row", "bus_stop_code", "stop_verbatim", "service_verbatim"}
KNOWN_SOURCE_REVIEWS = {
    "bus_exit_not_in_exit_table",
    "bus_stop_without_exit_link",
    "combined_platform_label",
    "exit_cell_icon_not_interpreted",
    "exit_label_note_not_interpreted",
    "exit_label_rowspan",
    "exit_section_prose_not_interpreted",
    "exit_status_requires_review",
    "exit_table_missing_bus_fallback",
    "layout_floor_rowspan_overruns_table",
    "line_scope_rowspan_overruns_table",
    "repeated_exit_identity_in_source_table",
    "unspecified_exit_scope",
}
LABEL = re.compile(r"(?:[A-Z][0-9]{0,2}|[0-9]{1,2})\Z")
SYSTEM_ORDER = {"MRT": 0, "LRT": 1}


@dataclass(frozen=True, slots=True)
class Stage1BuildResult:
    dataset: dict[str, object]
    reviews: dict[str, object]
    required_bus_stops: tuple[BusStop, ...]


def _string_list(value: object, context: str, *, nonempty: bool = False) -> list[str]:
    if type(value) is not list or (nonempty and not value):
        fail(context, "expected nonempty array" if nonempty else "expected array")
    if not all(type(item) is str for item in value):
        fail(context, "expected strings")
    if len(value) != len(set(value)):
        fail(context, "must not contain duplicates")
    return value


def _scopes(value: object, context: str) -> list[str]:
    if value is None:
        return []
    scopes = _string_list(value, context, nonempty=True)
    if not set(scopes) <= set(SYSTEM_ORDER):
        fail(context, f"unknown scopes {sorted(set(scopes)-set(SYSTEM_ORDER))}")
    return sorted(scopes, key=SYSTEM_ORDER.__getitem__)


def _validate_entry(value: object, context: str) -> dict[str, object]:
    if type(value) is not dict:
        fail(context, "expected object")
    expected = set(ENTRY_FIELDS)
    if "exit_status" in value:
        expected.add("exit_status")
    row = require_fields(value, expected, context)
    labels = _string_list(row["labels"], f"{context}.labels", nonempty=True)
    if not all(LABEL.fullmatch(label) for label in labels):
        fail(f"{context}.labels", "invalid exit label")
    _scopes(row["scopes"], f"{context}.scopes")
    if type(row["table"]) is not int or row["table"] < 1:
        fail(f"{context}.table", "expected positive integer")
    if type(row["row"]) is not int or row["row"] < 2:
        fail(f"{context}.row", "expected integer at least 2")
    for key in (
        "label_cell_verbatim", "label_note_verbatim", "destination_verbatim", "label_markup"
    ):
        if type(row[key]) is not str:
            fail(f"{context}.{key}", "expected string")
    if type(row["row_verbatim"]) is not list or not all(
        type(item) is str for item in row["row_verbatim"]
    ):
        fail(f"{context}.row_verbatim", "expected string array")
    for key in ("has_uninterpreted_icon", "destination_missing"):
        if type(row[key]) is not bool:
            fail(f"{context}.{key}", "expected boolean")
    if row["wheelchair_access"] not in {"not_classified", "not_accessible"}:
        fail(
            f"{context}.wheelchair_access",
            f"unsupported value {row['wheelchair_access']!r}",
        )
    return row


def _validate_status(value: object, context: str) -> dict[str, object]:
    row = require_fields(value, STATUS_FIELDS, context)
    labels = _string_list(row["labels"], f"{context}.labels", nonempty=True)
    if not all(LABEL.fullmatch(label) for label in labels):
        fail(f"{context}.labels", "invalid exit label")
    _scopes(row["scopes"], f"{context}.scopes")
    if row["status"] != "closed":
        fail(f"{context}.status", f"unsupported value {row['status']!r}")
    _string_list(row["alternative_exits"], f"{context}.alternative_exits")
    for key in ("destination_verbatim", "status_verbatim", "status_markup"):
        if type(row[key]) is not str:
            fail(f"{context}.{key}", "expected string")
    return row


def _validate_operating_hours(value: object, context: str) -> dict[str, object]:
    row = require_fields(value, OPERATING_HOURS_FIELDS, context)
    labels = _string_list(row["labels"], f"{context}.labels", nonempty=True)
    if not all(LABEL.fullmatch(label) for label in labels):
        fail(f"{context}.labels", "invalid exit label")
    _scopes(row["scopes"], f"{context}.scopes")
    days = _string_list(row["days"], f"{context}.days", nonempty=True)
    if days != [
        "monday", "tuesday", "wednesday", "thursday",
        "friday", "saturday", "sunday",
    ]:
        fail(f"{context}.days", "only the exact daily schedule is supported")
    for key in ("opens", "closes"):
        if type(row[key]) is not str or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", row[key]) is None:
            fail(f"{context}.{key}", "expected 24-hour HH:MM")
    if row["opens"] >= row["closes"]:
        fail(context, "overnight operating hours need an explicit rule")
    if row["timezone"] != "Asia/Singapore" or row["grammar"] != "daily_open_interval":
        fail(context, "unsupported operating-hours timezone or grammar")
    if type(row["source_verbatim"]) is not str or not row["source_verbatim"]:
        fail(f"{context}.source_verbatim", "expected nonempty string")
    return row


def _validate_bus(value: object, context: str) -> list[dict[str, object]]:
    bus = require_fields(value, BUS_FIELDS, context)
    if bus["status"] not in {"unavailable", "no_service", "partial_positive_evidence"}:
        fail(f"{context}.status", f"unsupported value {bus['status']!r}")
    _string_list(bus["labels"], f"{context}.labels")
    if (
        type(bus["rows"]) is not list
        or type(bus["unlinked_rows"]) is not list
        or type(bus["reviews"]) is not list
    ):
        fail(context, "rows, unlinked_rows and reviews must be arrays")
    rows: list[dict[str, object]] = []
    codes: set[str] = set()
    for index, value in enumerate(bus["rows"]):
        row_context = f"{context}.rows[{index}]"
        row = require_fields(value, BUS_ROW_FIELDS, row_context)
        if type(row["row"]) is not int or row["row"] < 2:
            fail(f"{row_context}.row", "expected integer at least 2")
        code = row["bus_stop_code"]
        if type(code) is not str or re.fullmatch(r"[0-9]{5}", code) is None:
            fail(f"{row_context}.bus_stop_code", "expected five digits")
        if code in codes:
            fail(f"{row_context}.bus_stop_code", f"duplicate code {code}")
        codes.add(code)
        labels = _string_list(row["labels"], f"{row_context}.labels", nonempty=True)
        if not all(LABEL.fullmatch(label) for label in labels):
            fail(f"{row_context}.labels", "invalid exit label")
        if not all(type(row[key]) is str and row[key] for key in ("stop_verbatim", "service_verbatim")):
            fail(row_context, "source text must be nonempty strings")
        rows.append(dict(row))
    for index, value in enumerate(bus["unlinked_rows"]):
        row_context = f"{context}.unlinked_rows[{index}]"
        row = require_fields(value, UNLINKED_BUS_ROW_FIELDS, row_context)
        if type(row["row"]) is not int or row["row"] < 2:
            fail(f"{row_context}.row", "expected integer at least 2")
        code = row["bus_stop_code"]
        if type(code) is not str or re.fullmatch(r"[0-9]{5}", code) is None:
            fail(f"{row_context}.bus_stop_code", "expected five digits")
        if code in codes:
            fail(f"{row_context}.bus_stop_code", f"duplicate code {code}")
        codes.add(code)
        if not all(
            type(row[key]) is str and row[key]
            for key in ("stop_verbatim", "service_verbatim")
        ):
            fail(row_context, "source text must be nonempty strings")
    return rows


def _validate_source(value: object, station: str) -> dict[str, object]:
    if type(value) is not dict:
        fail(f"{station}.source", "expected object")
    expected = {
        "status", "labels", "entries", "sections", "prose_verbatim", "reviews",
        "exit_statuses", "exit_operating_hours", "requires_human_review",
        "bus_connections", "layout", "title",
        "page_id", "revision_id", "redirects", "url", "api_url", "revision_url",
    }
    if value.get("status") == "partial":
        expected.add("evidence_kind")
    source = require_fields(value, expected, f"{station}.source")
    if source["status"] not in {"complete", "partial"}:
        fail(f"{station}.source.status", "expected complete or partial")
    if source["status"] == "partial" and source["evidence_kind"] != "bus_connections_only":
        fail(f"{station}.source.evidence_kind", "unsupported partial evidence")
    labels = _string_list(source["labels"], f"{station}.source.labels", nonempty=True)
    if not all(LABEL.fullmatch(label) for label in labels):
        fail(f"{station}.source.labels", "invalid exit labels")
    if type(source["entries"]) is not list:
        fail(f"{station}.source.entries", "expected array")
    for index, entry in enumerate(source["entries"]):
        _validate_entry(entry, f"{station}.source.entries[{index}]")
    if source["status"] == "partial" and source["entries"]:
        fail(f"{station}.source.entries", "partial bus-only evidence must not contain exit rows")
    for key in ("sections", "prose_verbatim"):
        _string_list(source[key], f"{station}.source.{key}")
    if type(source["reviews"]) is not list:
        fail(f"{station}.source.reviews", "expected array")
    for index, review in enumerate(source["reviews"]):
        if type(review) is not dict or review.get("reason") not in KNOWN_SOURCE_REVIEWS:
            fail(f"{station}.source.reviews[{index}]", "unknown review shape or reason")
    if type(source["exit_statuses"]) is not list:
        fail(f"{station}.source.exit_statuses", "expected array")
    for index, status in enumerate(source["exit_statuses"]):
        _validate_status(status, f"{station}.source.exit_statuses[{index}]")
    if type(source["exit_operating_hours"]) is not list:
        fail(f"{station}.source.exit_operating_hours", "expected array")
    for index, hours in enumerate(source["exit_operating_hours"]):
        _validate_operating_hours(
            hours, f"{station}.source.exit_operating_hours[{index}]"
        )
    if source["requires_human_review"] is not True:
        fail(f"{station}.source.requires_human_review", "expected true")
    _validate_bus(source["bus_connections"], f"{station}.source.bus_connections")
    if type(source["page_id"]) is not int or source["page_id"] < 1:
        fail(f"{station}.source.page_id", "expected positive integer")
    if type(source["revision_id"]) is not int or source["revision_id"] < 1:
        fail(f"{station}.source.revision_id", "expected positive integer")
    for key in ("title", "url", "api_url", "revision_url"):
        if type(source[key]) is not str or not source[key]:
            fail(f"{station}.source.{key}", "expected nonempty string")
    if type(source["redirects"]) is not list:
        fail(f"{station}.source.redirects", "expected array")
    return source


def _snapshot_records(snapshot: object, catalog: StationCatalog) -> list[dict[str, object]]:
    root = require_fields(snapshot, {"schema_version", "kind", "stations"}, "SgWiki snapshot")
    if root["schema_version"] != 1 or type(root["schema_version"]) is not int:
        fail("SgWiki snapshot.schema_version", "expected integer 1")
    if root["kind"] != "sgwiki_station_snapshot":
        fail("SgWiki snapshot.kind", "unsupported value")
    if type(root["stations"]) is not list or len(root["stations"]) != len(catalog.stations):
        fail("SgWiki snapshot.stations", f"expected exactly {len(catalog.stations)} records")
    records: list[dict[str, object]] = []
    for index, (value, configured) in enumerate(zip(root["stations"], catalog.stations)):
        context = f"SgWiki snapshot.stations[{index}]"
        row = require_fields(
            value, {"name", "codes", "catalog_status", "sgwiki_title", "source"}, context
        )
        if (
            row["name"] != configured.name
            or row["codes"] != list(configured.codes)
            or row["catalog_status"] != configured.catalog_status
            or row["sgwiki_title"] != configured.sgwiki_title
        ):
            fail(context, "station identity disagrees with the reviewed registry")
        source = _validate_source(row["source"], configured.name)
        records.append({**row, "source": source})
    return records


def _resolver_input(records: list[dict[str, object]]) -> tuple[dict[str, object], list[dict[str, object]]]:
    stations: list[dict[str, object]] = []
    normalizations: list[dict[str, object]] = []
    for record in records:
        name = str(record["name"])
        source = record["source"]
        exits: list[dict[str, object]] = []
        seen: set[tuple[tuple[str, ...], str]] = set()
        if source["entries"]:
            for entry in source["entries"]:
                scopes = _scopes(entry["scopes"], f"{name}.entry.scopes")
                for label in entry["labels"]:
                    identity = (tuple(scopes), label)
                    if identity in seen:
                        # A source row can list several separate exits, such as
                        # Dhoby Ghaut "E G" and "F G". Combine evidence only
                        # for the exact repeated identity (MRT:G here). Never
                        # collapse the other labels into that exit.
                        normalizations.append({
                            "station": name,
                            "kind": "repeated_exit_evidence_merged",
                            "scopes": scopes,
                            "label": label,
                            "source_table": entry["table"],
                            "source_row": entry["row"],
                        })
                        continue
                    seen.add(identity)
                    exits.append({"label": label, "scopes": scopes})
        else:
            systems = rail_scopes(record["codes"])
            fallback_scopes = systems if len(systems) == 1 else []
            exits = [
                {"label": label, "scopes": fallback_scopes}
                for label in source["labels"]
            ]
        stations.append({
            "station": name,
            "codes": record["codes"],
            "page_id": source["page_id"],
            "revision_id": source["revision_id"],
            "source_inventory_status": source["status"],
            "source_inventory_evidence_kind": (
                source["evidence_kind"]
                if source["status"] == "partial"
                else "sgwiki_exit_table"
            ),
            "exits": exits,
            "bus_links": [
                {
                    "bus_stop_code": row["bus_stop_code"],
                    "stop_verbatim": row["stop_verbatim"],
                    "exit_labels": row["labels"],
                }
                for row in source["bus_connections"]["rows"]
            ],
        })
    return {"schema_version": 1, "stations": stations}, normalizations


def _match_entry_exit(
    exits: list[dict[str, object]], *, label: str, scopes: list[str], station: str
) -> dict[str, object]:
    exact = [item for item in exits if item["label"] == label and item["scopes"] == scopes]
    if len(exact) == 1:
        return exact[0]
    by_label = [item for item in exits if item["label"] == label]
    if len(by_label) == 1:
        return by_label[0]
    fail(station, f"source exit {scopes}:{label} cannot be matched to one resolved identity")


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _review_detail_reason(item: dict[str, object]) -> str:
    detail = item.get("detail")
    if type(detail) is dict and (detail.get("reason") or detail.get("kind")):
        return str(detail.get("reason") or detail.get("kind"))
    return str(item["reason"])


def _group_review_items(
    review_items: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Group atomic findings by reason while retaining every affected station."""

    groups: list[dict[str, object]] = []
    groups_by_key: dict[tuple[str, str, str], dict[str, object]] = {}
    for item in review_items:
        detail_reason = _review_detail_reason(item)
        key = (
            str(item["severity"]),
            str(item["reason"]),
            detail_reason,
        )
        group = groups_by_key.get(key)
        if group is None:
            group = {
                "id": ":".join((key[1], detail_reason)),
                "severity": key[0],
                "reason": key[1],
                "detail_reason": detail_reason,
                "station_count": 0,
                "station_ids": [],
                "station_names": [],
                "finding_count": 0,
                "findings": [],
            }
            groups_by_key[key] = group
            groups.append(group)
        if item["station_id"] not in group["station_ids"]:
            group["station_ids"].append(item["station_id"])
            group["station_names"].append(item["station_name"])
            group["station_count"] += 1
        finding = {
            field: value
            for field, value in item.items()
            if field not in {"severity", "reason"}
        }
        group["findings"].append(finding)
        group["finding_count"] += 1
    return groups


def _apply_review_acceptance(
    *,
    station: str,
    codes: list[str],
    source: dict[str, object],
    reviews: list[dict[str, object]],
    decisions: ReviewedDecisions,
) -> tuple[list[dict[str, object]], dict[str, object] | None]:
    acceptance = decisions.review_acceptances.get(station)
    if acceptance is None:
        return reviews, None
    if tuple(codes) != acceptance["codes"]:
        fail(station, "station codes changed from the reviewed finding acceptance")
    if source["page_id"] != acceptance["page_id"]:
        fail(station, "SgWiki page identity changed from the reviewed finding acceptance")
    if source["revision_id"] != acceptance["revision_id"]:
        fail(station, "SgWiki page revision changed from the reviewed finding acceptance")

    observed: dict[str, int] = {}
    for review in reviews:
        reason = _review_detail_reason(review)
        observed[reason] = observed.get(reason, 0) + 1
    for reason, expected_count in acceptance["findings"].items():
        actual_count = observed.get(reason, 0)
        if actual_count != expected_count:
            fail(
                station,
                f"reviewed finding {reason!r} changed; "
                f"expected {expected_count}, got {actual_count}",
            )
    accepted = set(acceptance["findings"])
    remaining = [review for review in reviews if _review_detail_reason(review) not in accepted]
    contract = {
        "kind": "review_acceptance",
        "page_id": acceptance["page_id"],
        "revision_id": acceptance["revision_id"],
        "findings": [
            {"reason": reason, "count": count}
            for reason, count in acceptance["findings"].items()
        ],
        "evidence": list(acceptance["evidence"]),
    }
    return remaining, contract


def build_full_stage1(
    *,
    sgwiki_snapshot: object,
    gtfs_inventory: object,
    bus_stops: tuple[BusStop, ...],
    catalog: StationCatalog,
    decisions: ReviewedDecisions,
) -> Stage1BuildResult:
    """Join SgWiki semantics, GTFS operational status and exact LTA bus stops."""

    source_records = _snapshot_records(sgwiki_snapshot, catalog)
    gtfs_matches, inactive = match_catalog_stations(catalog, gtfs_inventory)
    resolver_input, normalizations = _resolver_input(source_records)
    resolved = build_stage1_reviewed_links(resolver_input, decisions)
    resolved_by_name = {station["station"]: station for station in resolved["stations"]}

    required_codes = [
        row["bus_stop_code"]
        for record in source_records
        for key in ("rows", "unlinked_rows")
        for row in record["source"]["bus_connections"][key]
    ]
    required_bus_stops = select_required_bus_stops(bus_stops, required_codes)
    bus_stop_by_code = {record.code: record for record in required_bus_stops}
    normalization_by_station: dict[str, list[dict[str, object]]] = {}
    for item in normalizations:
        normalization_by_station.setdefault(str(item["station"]), []).append(item)

    stations: list[dict[str, object]] = []
    review_items: list[dict[str, object]] = []
    for source_record in source_records:
        name = str(source_record["name"])
        codes = source_record["codes"]
        source = source_record["source"]
        identity = resolved_by_name[name]
        exits = [
            {
                **item,
                "inventory_evidence": identity["exit_inventory_status"],
                "destinations": [],
                "source_rows": [],
                "operating_status": "not_stated",
                "operating_hours": [],
                "status_notes": [],
                "alternative_exits": [],
                "accessibility": {
                    "status": "not_classified",
                    "evidence": [],
                    "uninterpreted_source_icon": False,
                },
            }
            for item in identity["exits"]
        ]
        for entry_index, entry in enumerate(source["entries"]):
            _validate_entry(entry, f"{name}.source.entries[{entry_index}]")
            scopes = _scopes(entry["scopes"], f"{name}.source.entries[{entry_index}].scopes")
            for label in entry["labels"]:
                target = _match_entry_exit(exits, label=label, scopes=scopes, station=name)
                _append_unique(target["destinations"], entry["destination_verbatim"])
                target["source_rows"].append({
                    "table": entry["table"],
                    "row": entry["row"],
                    "label_cell_verbatim": entry["label_cell_verbatim"],
                    "row_verbatim": entry["row_verbatim"],
                })
                target["accessibility"]["uninterpreted_source_icon"] |= entry["has_uninterpreted_icon"]
                if entry["wheelchair_access"] == "not_accessible":
                    target["accessibility"]["status"] = "not_accessible"
                    _append_unique(
                        target["accessibility"]["evidence"],
                        "sgwiki_crossed_wheelchair_icon",
                    )
        for status_index, status in enumerate(source["exit_statuses"]):
            _validate_status(status, f"{name}.source.exit_statuses[{status_index}]")
            scopes = _scopes(status["scopes"], f"{name}.source.exit_statuses[{status_index}].scopes")
            for label in status["labels"]:
                target = _match_entry_exit(exits, label=label, scopes=scopes, station=name)
                target["operating_status"] = "source_reported_closed"
                _append_unique(target["status_notes"], status["status_verbatim"])
                for alternative in status["alternative_exits"]:
                    _append_unique(target["alternative_exits"], alternative)
        for hours_index, hours in enumerate(source["exit_operating_hours"]):
            _validate_operating_hours(
                hours, f"{name}.source.exit_operating_hours[{hours_index}]"
            )
            scopes = _scopes(
                hours["scopes"],
                f"{name}.source.exit_operating_hours[{hours_index}].scopes",
            )
            for label in hours["labels"]:
                target = _match_entry_exit(exits, label=label, scopes=scopes, station=name)
                target["operating_hours"].append({
                    "days": hours["days"],
                    "opens": hours["opens"],
                    "closes": hours["closes"],
                    "timezone": hours["timezone"],
                    "source_verbatim": hours["source_verbatim"],
                    "grammar": hours["grammar"],
                })

        raw_bus_by_code = {
            row["bus_stop_code"]: row for row in source["bus_connections"]["rows"]
        }
        bus_links: list[dict[str, object]] = []
        for link in identity["bus_links"]:
            raw = raw_bus_by_code[link["bus_stop_code"]]
            lta = bus_stop_by_code[link["bus_stop_code"]]
            bus_links.append({
                "source_row": raw["row"],
                "bus_stop_code": link["bus_stop_code"],
                "stop_verbatim": link["stop_verbatim"],
                "exit_labels": link["exit_labels"],
                "service_verbatim": raw["service_verbatim"],
                "resolved_exit_ids": link["resolved_exit_ids"],
                "resolution": link["resolution"],
                "evidence": link["evidence"],
                "lta": lta.as_source_dict(),
            })
        excluded_links: list[dict[str, object]] = []
        for link in identity["excluded_bus_links"]:
            raw = raw_bus_by_code[link["bus_stop_code"]]
            lta = bus_stop_by_code[link["bus_stop_code"]]
            excluded_links.append({
                "source_row": raw["row"],
                "bus_stop_code": link["bus_stop_code"],
                "stop_verbatim": link["stop_verbatim"],
                "exit_labels": link["exit_labels"],
                "service_verbatim": raw["service_verbatim"],
                "reason": link["reason"],
                "lta": lta.as_source_dict(),
            })

        unlinked_rows = source["bus_connections"]["unlinked_rows"]
        connection_contract = decisions.station_connection_contracts.get(name)
        station_connections: list[dict[str, object]] = []
        connection_contract_record = None
        if connection_contract is not None:
            if tuple(codes) != connection_contract["codes"]:
                fail(name, "station codes changed from the station connection contract")
            if source["page_id"] != connection_contract["page_id"]:
                fail(name, "SgWiki page identity changed from the station connection contract")
            if source["revision_id"] != connection_contract["revision_id"]:
                fail(name, "SgWiki page revision changed from the station connection contract")
            if unlinked_rows != connection_contract["source_bus_connections"]:
                fail(name, "unlinked bus rows changed from the station connection contract")
            connection_contract_record = {
                "kind": "station_connection_contract",
                "page_id": connection_contract["page_id"],
                "revision_id": connection_contract["revision_id"],
                "evidence": list(connection_contract["evidence"]),
            }
        for raw in unlinked_rows:
            lta = bus_stop_by_code[raw["bus_stop_code"]]
            decision = (
                connection_contract["connections"][raw["bus_stop_code"]]
                if connection_contract is not None
                else None
            )
            station_connections.append({
                "source_row": raw["row"],
                "bus_stop_code": raw["bus_stop_code"],
                "stop_verbatim": raw["stop_verbatim"],
                "service_verbatim": raw["service_verbatim"],
                "kind": decision["kind"] if decision else "unresolved",
                "relationship": decision["relationship"] if decision else "unresolved",
                "name": decision["name"] if decision else None,
                "resolved_exit_ids": [],
                "resolution": (
                    "revision_pinned_station_connection_contract"
                    if decision else "source_bus_row_without_exit"
                ),
                "evidence": list(connection_contract["evidence"]) if decision else [],
                "lta": lta.as_source_dict(),
            })

        layout = normalize_layout(source["layout"], station=name, codes=codes)
        reviews: list[dict[str, object]] = []
        inventory_overridden = identity["exit_inventory_status"] == "complete_reviewed_override"
        scope_contract_applied = any(
            contract["kind"] == "exit_scope_contract"
            for contract in identity["contracts_applied"]
        )
        if source["status"] == "partial" and not inventory_overridden:
            reviews.append({
                "severity": "WARNING",
                "reason": "partial_exit_inventory",
                "evidence_kind": source["evidence_kind"],
            })
        if source_record["catalog_status"] != "v1_baseline" and not inventory_overridden:
            reviews.append({
                "severity": "WARNING",
                "reason": "operator_addition_requires_review",
            })
        if name not in gtfs_matches:
            reviews.append({
                "severity": "WARNING",
                "reason": "not_active_in_lta_gtfs_for_service_date",
                "service_date": gtfs_inventory["service_date"],
            })
        # Layout reviews are carried in both the page-level review ledger and
        # the structured layout object by the parser. Emit them once from the
        # normalized layout after its exact shapes have been validated.
        reviews.extend(
            {"severity": "WARNING", "reason": "sgwiki_source_review", "detail": item}
            for item in source["reviews"]
            if item.get("section") != "layout"
            and not (
                inventory_overridden
                and item.get("reason") == "exit_table_missing_bus_fallback"
            )
            and not (
                scope_contract_applied
                and item.get("reason") == "unspecified_exit_scope"
            )
            and not (
                connection_contract is not None
                and item.get("reason") == "bus_stop_without_exit_link"
            )
        )
        reviews.extend({"severity": "WARNING", "reason": "layout_review", "detail": item}
                       for item in layout["blocking_reviews"])
        reviews.extend({"severity": "WARNING", "reason": value}
                       for value in layout["coverage_reviews"])
        reviews.extend({"severity": "WARNING", "reason": "normalization_applied", "detail": item}
                       for item in normalization_by_station.get(name, []))
        reviews, review_contract = _apply_review_acceptance(
            station=name,
            codes=codes,
            source=source,
            reviews=reviews,
            decisions=decisions,
        )
        contracts_applied = list(identity["contracts_applied"])
        if connection_contract_record is not None:
            contracts_applied.append(connection_contract_record)
        if review_contract is not None:
            contracts_applied.append(review_contract)
        for review in reviews:
            review_items.append({"station_id": station_slug(name), "station_name": name, **review})

        stations.append({
            "id": station_slug(name),
            "name": name,
            "codes": codes,
            "catalog_status": source_record["catalog_status"],
            "operational_status": "active" if name in gtfs_matches else "not_active",
            "gtfs_matches": gtfs_matches.get(name, []),
            "source": {
                "kind": "sgwiki_station_page",
                "title": source["title"],
                "url": source["url"],
                "api_url": source["api_url"],
                "revision_url": source["revision_url"],
                "page_id": source["page_id"],
                "revision_id": source["revision_id"],
                "redirects": source["redirects"],
                "contracts_applied": contracts_applied,
            },
            "exit_inventory_status": identity["exit_inventory_status"],
            "exits": exits,
            "bus_links": bus_links,
            "excluded_bus_links": excluded_links,
            "station_connections": station_connections,
            "areas": layout["areas"],
            "platforms": layout["platforms"],
            "layout_supplements": layout["supplementary_tables"],
            "layout_coverage": layout["coverage"],
            "reviews": reviews,
        })

    review_groups = _group_review_items(review_items)
    summary = {
        "station_count": len(stations),
        "active_station_count": sum(station["operational_status"] == "active" for station in stations),
        "inactive_station_count": len(inactive),
        "complete_exit_inventory_count": sum(
            station["exit_inventory_status"] in {"complete", "complete_reviewed_override"}
            for station in stations
        ),
        "partial_exit_inventory_count": sum(
            station["exit_inventory_status"] == "partial" for station in stations
        ),
        "exit_identity_count": sum(len(station["exits"]) for station in stations),
        "shared_exit_identity_count": sum(
            len(exit_record["scopes"]) > 1 for station in stations for exit_record in station["exits"]
        ),
        "platform_record_count": sum(len(station["platforms"]) for station in stations),
        "area_record_count": sum(len(station["areas"]) for station in stations),
        "rail_service_diagram_count": sum(
            supplement["kind"] == "rail_service_diagram"
            for station in stations
            for supplement in station["layout_supplements"]
        ),
        "empty_layout_formatting_table_count": sum(
            supplement["kind"] == "empty_formatting_table"
            for station in stations
            for supplement in station["layout_supplements"]
        ),
        "bus_link_count": sum(len(station["bus_links"]) for station in stations),
        "excluded_bus_link_count": sum(len(station["excluded_bus_links"]) for station in stations),
        "station_connection_count": sum(
            len(station["station_connections"]) for station in stations
        ),
        "unresolved_station_connection_count": sum(
            connection["resolution"] == "source_bus_row_without_exit"
            for station in stations
            for connection in station["station_connections"]
        ),
        "required_lta_bus_stop_count": len(required_bus_stops),
        "closed_exit_identity_count": sum(
            exit_record["operating_status"] == "source_reported_closed"
            for station in stations for exit_record in station["exits"]
        ),
        "not_accessible_exit_identity_count": sum(
            exit_record["accessibility"]["status"] == "not_accessible"
            for station in stations for exit_record in station["exits"]
        ),
        "station_review_count": sum(bool(station["reviews"]) for station in stations),
        "review_group_count": len(review_groups),
        "review_item_count": len(review_items),
        "normalization_count": len(normalizations),
    }
    dataset = {
        "schema_version": 1,
        "kind": "mrt_wayfinding_stage_1",
        "status": "review_required" if review_items else "complete",
        "publication_allowed": not review_items,
        "gtfs": {
            "service_date": gtfs_inventory["service_date"],
            "source_zip_sha256": gtfs_inventory["source_zip_sha256"],
            "active_station_record_count": gtfs_inventory["active_station_count"],
            "feed_info": gtfs_inventory["feed_info"],
        },
        "reviewed_decisions": {
            "path": str(decisions.path.name),
            "sha256": decisions.sha256,
        },
        "summary": summary,
        "normalizations": normalizations,
        "stations": stations,
    }
    reviews = {
        "schema_version": 1,
        "kind": "stage1_reviews",
        "status": "review_required" if review_items else "complete",
        "count": len(review_groups),
        "group_count": len(review_groups),
        "finding_count": len(review_items),
        "groups": review_groups,
    }
    return Stage1BuildResult(dataset, reviews, required_bus_stops)


def compare_stage1(current: object, baseline: object | None) -> dict[str, object]:
    """Produce a deterministic review ledger against an approved Stage 1 baseline."""

    if baseline is None:
        return {
            "schema_version": 1,
            "kind": "stage1_changes",
            "status": "baseline_unavailable",
            "highest_severity": "WARNING",
            "count": 1,
            "items": [{
                "severity": "WARNING",
                "reason": "approved_stage1_baseline_unavailable",
            }],
        }
    dataset_fields = {
        "schema_version", "kind", "status", "publication_allowed", "gtfs",
        "reviewed_decisions", "summary", "normalizations", "stations",
    }
    station_fields = {
        "id", "name", "codes", "catalog_status", "operational_status", "gtfs_matches",
        "source", "exit_inventory_status", "exits", "bus_links", "excluded_bus_links",
        "station_connections", "areas", "platforms", "layout_supplements",
        "layout_coverage", "reviews",
    }
    source_fields = {
        "kind", "title", "url", "api_url", "revision_url", "page_id", "revision_id",
        "redirects", "contracts_applied",
    }
    for context, document in (("current", current), ("baseline", baseline)):
        root = require_fields(document, dataset_fields, f"stage1 comparison.{context}")
        if root["schema_version"] != 1 or root["kind"] != "mrt_wayfinding_stage_1":
            fail(f"stage1 comparison.{context}", "unsupported Stage 1 document")
        if type(root["stations"]) is not list:
            fail(f"stage1 comparison.{context}.stations", "expected array")
        for index, station in enumerate(root["stations"]):
            row = require_fields(
                station, station_fields, f"stage1 comparison.{context}.stations[{index}]"
            )
            require_fields(
                row["source"],
                source_fields,
                f"stage1 comparison.{context}.stations[{index}].source",
            )
    current_by = {station["id"]: station for station in current["stations"]}
    baseline_by = {station["id"]: station for station in baseline["stations"]}
    if len(current_by) != len(current["stations"]) or len(baseline_by) != len(baseline["stations"]):
        fail("stage1 comparison", "duplicate station IDs")
    items: list[dict[str, object]] = []
    for station_id in sorted(set(current_by) - set(baseline_by)):
        items.append({"severity": "ERROR", "reason": "station_added", "station_id": station_id})
    for station_id in sorted(set(baseline_by) - set(current_by)):
        items.append({"severity": "ERROR", "reason": "station_removed", "station_id": station_id})
    for station_id in sorted(set(current_by) & set(baseline_by)):
        current_station, baseline_station = current_by[station_id], baseline_by[station_id]
        if current_station["codes"] != baseline_station["codes"]:
            items.append({
                "severity": "ERROR",
                "reason": "station_codes_changed",
                "station_id": station_id,
                "from": baseline_station["codes"],
                "to": current_station["codes"],
            })
        if current_station["source"]["page_id"] != baseline_station["source"]["page_id"]:
            items.append({
                "severity": "ERROR",
                "reason": "sgwiki_page_identity_changed",
                "station_id": station_id,
                "from": baseline_station["source"]["page_id"],
                "to": current_station["source"]["page_id"],
            })
        if current_station["source"]["revision_id"] != baseline_station["source"]["revision_id"]:
            items.append({
                "severity": "WARNING",
                "reason": "sgwiki_revision_changed",
                "station_id": station_id,
                "from": baseline_station["source"]["revision_id"],
                "to": current_station["source"]["revision_id"],
            })
        current_exits = {item["id"] for item in current_station["exits"]}
        baseline_exits = {item["id"] for item in baseline_station["exits"]}
        for exit_id in sorted(current_exits - baseline_exits):
            items.append({"severity": "WARNING", "reason": "exit_added", "station_id": station_id,
                          "exit_id": exit_id})
        for exit_id in sorted(baseline_exits - current_exits):
            items.append({"severity": "WARNING", "reason": "exit_removed", "station_id": station_id,
                          "exit_id": exit_id})
        semantic_fields = (
            "catalog_status",
            "operational_status",
            "gtfs_matches",
            "exit_inventory_status",
            "exits",
            "bus_links",
            "excluded_bus_links",
            "station_connections",
            "areas",
            "platforms",
            "layout_supplements",
            "layout_coverage",
            "reviews",
        )
        changed_fields = [
            field for field in semantic_fields
            if current_station[field] != baseline_station[field]
        ]
        if changed_fields:
            items.append({
                "severity": "WARNING",
                "reason": "station_semantics_changed",
                "station_id": station_id,
                "fields": changed_fields,
            })
    severity_order = {"INFO": 0, "WARNING": 1, "ERROR": 2, "CRITICAL": 3}
    highest = max((item["severity"] for item in items), key=severity_order.get, default="INFO")
    return {
        "schema_version": 1,
        "kind": "stage1_changes",
        "status": "complete" if not items else "review_required" if highest == "WARNING" else "failed",
        "highest_severity": highest,
        "count": len(items),
        "items": items,
    }


__all__ = ["Stage1BuildResult", "build_full_stage1", "compare_stage1"]
