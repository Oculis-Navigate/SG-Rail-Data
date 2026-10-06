"""Stage 1 station semantics and reviewed deterministic decisions.

The SgWiki parser supplies literal exit scopes and bus-connection rows.  This
module turns those rows into stable exit identities, then applies the small set
of human-reviewed decisions that cannot be inferred safely from an exit letter.
Every decision is guarded by the exact source facts it was reviewed against.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

from .contracts import (
    ContractError,
    exact_int,
    exact_str,
    fail,
    require_fields,
    string_list,
)


SYSTEM_ORDER = {"MRT": 0, "LRT": 1}
EXIT_LABEL = re.compile(r"[A-Z0-9]+")
BUS_STOP_CODE = re.compile(r"\d{5}")
REVIEW_ACCEPTANCE_REASONS = frozenset({
    "bus_stop_without_exit_link",
    "combined_platform_label",
    "exit_section_prose_not_interpreted",
    "exit_status_requires_review",
    "layout_floor_rowspan_overruns_table",
    "line_scope_rowspan_overruns_table",
    "platform_with_multiple_concourse_levels",
    "repeated_exit_evidence_merged",
    "repeated_exit_identity_in_source_table",
})


@dataclass(frozen=True, slots=True)
class ReviewedDecisions:
    """Validated Stage 1 decisions indexed by station name."""

    path: Path
    sha256: str
    scope_contracts: dict[str, dict[str, Any]]
    inventory_overrides: dict[str, dict[str, Any]]
    bus_overrides: dict[str, dict[str, Any]]
    station_connection_contracts: dict[str, dict[str, Any]]
    review_acceptances: dict[str, dict[str, Any]]


def _exact_array(value: object, context: str, *, nonempty: bool = False) -> list[object]:
    if type(value) is not list:
        fail(context, f"expected array, got {type(value).__name__}")
    if nonempty and not value:
        fail(context, "must not be empty")
    return value


def _label(value: object, context: str) -> str:
    label = exact_str(value, context)
    if EXIT_LABEL.fullmatch(label) is None:
        fail(context, "must contain only uppercase ASCII letters and digits")
    return label


def _bus_stop_code(value: object, context: str) -> str:
    code = exact_str(value, context)
    if BUS_STOP_CODE.fullmatch(code) is None:
        fail(context, "must be exactly five digits")
    return code


def _scopes(value: object, context: str, *, nonempty: bool = False) -> tuple[str, ...]:
    scopes = string_list(value, context, nonempty=nonempty)
    unknown = sorted(set(scopes) - set(SYSTEM_ORDER))
    if unknown:
        fail(context, f"unknown systems {unknown}")
    return tuple(sorted(scopes, key=SYSTEM_ORDER.__getitem__))


def _exit_id(scopes: tuple[str, ...], label: str) -> str:
    if len(scopes) == 2:
        prefix = "SHARED"
    elif scopes:
        prefix = scopes[0]
    else:
        prefix = "UNSCOPED"
    return f"{prefix}:{label}"


def _source_bus_link(value: object, context: str) -> dict[str, object]:
    row = require_fields(
        value, {"bus_stop_code", "stop_verbatim", "exit_labels"}, context
    )
    return {
        "bus_stop_code": _bus_stop_code(row["bus_stop_code"], f"{context}.bus_stop_code"),
        "stop_verbatim": exact_str(row["stop_verbatim"], f"{context}.stop_verbatim"),
        "exit_labels": list(
            _labels(row["exit_labels"], f"{context}.exit_labels", nonempty=True)
        ),
    }


def _labels(value: object, context: str, *, nonempty: bool = False) -> tuple[str, ...]:
    values = _exact_array(value, context, nonempty=nonempty)
    labels = tuple(_label(item, f"{context}[{index}]") for index, item in enumerate(values))
    if len(labels) != len(set(labels)):
        fail(context, "must not contain duplicates")
    return labels


def _scope_contract(value: object, context: str) -> tuple[str, dict[str, Any]]:
    row = require_fields(value, {"station", "page_id", "codes", "exits", "evidence"}, context)
    station = exact_str(row["station"], f"{context}.station")
    codes = string_list(row["codes"], f"{context}.codes", nonempty=True)
    page_id = exact_int(row["page_id"], f"{context}.page_id", minimum=1)
    evidence = string_list(row["evidence"], f"{context}.evidence", nonempty=True)
    exit_rows = _exact_array(row["exits"], f"{context}.exits", nonempty=True)
    exits: dict[str, dict[str, object]] = {}
    for index, value in enumerate(exit_rows):
        exit_context = f"{context}.exits[{index}]"
        item = require_fields(
            value, {"label", "source_scopes", "resolved_scopes"}, exit_context
        )
        label = _label(item["label"], f"{exit_context}.label")
        if label in exits:
            fail(f"{exit_context}.label", f"duplicate label {label}")
        source_scopes = _scopes(item["source_scopes"], f"{exit_context}.source_scopes")
        resolved_scopes = _scopes(
            item["resolved_scopes"], f"{exit_context}.resolved_scopes", nonempty=True
        )
        exits[label] = {
            "source_scopes": source_scopes,
            "resolved_scopes": resolved_scopes,
        }
    return station, {
        "station": station,
        "page_id": page_id,
        "codes": codes,
        "exits": exits,
        "evidence": evidence,
    }


def _inventory_override(value: object, context: str) -> tuple[str, dict[str, Any]]:
    row = require_fields(
        value,
        {
            "station",
            "page_id",
            "codes",
            "source_inventory_status",
            "source_inventory_evidence_kind",
            "source_exits",
            "resolved_exits",
            "evidence",
        },
        context,
    )
    station = exact_str(row["station"], f"{context}.station")
    page_id = exact_int(row["page_id"], f"{context}.page_id", minimum=1)
    codes = string_list(row["codes"], f"{context}.codes", nonempty=True)
    source_status = exact_str(
        row["source_inventory_status"], f"{context}.source_inventory_status"
    )
    source_evidence_kind = exact_str(
        row["source_inventory_evidence_kind"],
        f"{context}.source_inventory_evidence_kind",
    )
    if source_status != "partial" or source_evidence_kind != "bus_connections_only":
        fail(context, "inventory overrides must guard partial bus-connections-only evidence")

    def exits(key: str) -> list[dict[str, object]]:
        values = _exact_array(row[key], f"{context}.{key}", nonempty=True)
        result: list[dict[str, object]] = []
        identities: set[str] = set()
        for index, value in enumerate(values):
            item_context = f"{context}.{key}[{index}]"
            item = require_fields(value, {"label", "scopes"}, item_context)
            label = _label(item["label"], f"{item_context}.label")
            scopes = _scopes(item["scopes"], f"{item_context}.scopes", nonempty=True)
            identity = _exit_id(scopes, label)
            if identity in identities:
                fail(item_context, f"duplicate exit identity {identity}")
            identities.add(identity)
            result.append({"label": label, "scopes": scopes})
        return result

    source_exits = exits("source_exits")
    resolved_exits = exits("resolved_exits")
    source_ids = {_exit_id(item["scopes"], str(item["label"])) for item in source_exits}
    resolved_ids = {_exit_id(item["scopes"], str(item["label"])) for item in resolved_exits}
    if not source_ids <= resolved_ids:
        fail(context, "resolved exits must contain every source-supported exit")
    evidence = string_list(row["evidence"], f"{context}.evidence", nonempty=True)
    return station, {
        "station": station,
        "page_id": page_id,
        "codes": codes,
        "source_inventory_status": source_status,
        "source_inventory_evidence_kind": source_evidence_kind,
        "source_exits": source_exits,
        "resolved_exits": resolved_exits,
        "evidence": evidence,
    }


def _bus_override(value: object, context: str) -> tuple[str, dict[str, Any]]:
    row = require_fields(
        value,
        {
            "station",
            "page_id",
            "revision_id",
            "codes",
            "expected_exit_ids",
            "source_bus_links",
            "mappings",
            "excluded",
            "evidence",
        },
        context,
    )
    station = exact_str(row["station"], f"{context}.station")
    page_id = exact_int(row["page_id"], f"{context}.page_id", minimum=1)
    revision_id = exact_int(row["revision_id"], f"{context}.revision_id", minimum=1)
    codes = string_list(row["codes"], f"{context}.codes", nonempty=True)
    expected_exit_ids = string_list(
        row["expected_exit_ids"], f"{context}.expected_exit_ids", nonempty=True
    )
    evidence = string_list(row["evidence"], f"{context}.evidence", nonempty=True)

    source_rows = _exact_array(
        row["source_bus_links"], f"{context}.source_bus_links", nonempty=True
    )
    source_bus_links: list[dict[str, object]] = []
    source_codes: set[str] = set()
    for index, value in enumerate(source_rows):
        link = _source_bus_link(value, f"{context}.source_bus_links[{index}]")
        code = str(link["bus_stop_code"])
        if code in source_codes:
            fail(f"{context}.source_bus_links[{index}].bus_stop_code", f"duplicate code {code}")
        source_codes.add(code)
        source_bus_links.append(link)

    mapping_rows = _exact_array(row["mappings"], f"{context}.mappings")
    mappings: dict[str, dict[str, object]] = {}
    for index, value in enumerate(mapping_rows):
        mapping_context = f"{context}.mappings[{index}]"
        item = require_fields(value, {"bus_stop_code", "exit_ids", "evidence"}, mapping_context)
        code = _bus_stop_code(item["bus_stop_code"], f"{mapping_context}.bus_stop_code")
        exit_ids = string_list(item["exit_ids"], f"{mapping_context}.exit_ids", nonempty=True)
        item_evidence = string_list(item["evidence"], f"{mapping_context}.evidence", nonempty=True)
        if code not in source_codes:
            fail(f"{mapping_context}.bus_stop_code", "does not identify a source bus row")
        if code in mappings:
            fail(f"{mapping_context}.bus_stop_code", f"duplicate mapping for {code}")
        unknown_exit_ids = sorted(set(exit_ids) - set(expected_exit_ids))
        if unknown_exit_ids:
            fail(f"{mapping_context}.exit_ids", f"unknown exit identities {unknown_exit_ids}")
        mappings[code] = {"exit_ids": exit_ids, "evidence": item_evidence}

    excluded_rows = _exact_array(row["excluded"], f"{context}.excluded")
    excluded: dict[str, str] = {}
    for index, value in enumerate(excluded_rows):
        excluded_context = f"{context}.excluded[{index}]"
        item = require_fields(value, {"bus_stop_code", "reason"}, excluded_context)
        code = _bus_stop_code(item["bus_stop_code"], f"{excluded_context}.bus_stop_code")
        reason = exact_str(item["reason"], f"{excluded_context}.reason")
        if code not in source_codes:
            fail(f"{excluded_context}.bus_stop_code", "does not identify a source bus row")
        if code in excluded or code in mappings:
            fail(f"{excluded_context}.bus_stop_code", f"duplicate decision for {code}")
        excluded[code] = reason

    decided_codes = set(mappings) | set(excluded)
    if decided_codes != source_codes:
        fail(
            context,
            "every reviewed source bus row must be mapped or excluded; "
            f"missing={sorted(source_codes - decided_codes)}, "
            f"extra={sorted(decided_codes - source_codes)}",
        )
    return station, {
        "station": station,
        "page_id": page_id,
        "revision_id": revision_id,
        "codes": codes,
        "expected_exit_ids": expected_exit_ids,
        "source_bus_links": source_bus_links,
        "mappings": mappings,
        "excluded": excluded,
        "evidence": evidence,
    }


def _review_acceptance(value: object, context: str) -> tuple[str, dict[str, Any]]:
    row = require_fields(
        value,
        {"station", "page_id", "revision_id", "codes", "findings", "evidence"},
        context,
    )
    station = exact_str(row["station"], f"{context}.station")
    page_id = exact_int(row["page_id"], f"{context}.page_id", minimum=1)
    revision_id = exact_int(row["revision_id"], f"{context}.revision_id", minimum=1)
    codes = string_list(row["codes"], f"{context}.codes", nonempty=True)
    evidence = string_list(row["evidence"], f"{context}.evidence", nonempty=True)
    finding_rows = _exact_array(row["findings"], f"{context}.findings", nonempty=True)
    findings: dict[str, int] = {}
    for index, value in enumerate(finding_rows):
        finding_context = f"{context}.findings[{index}]"
        item = require_fields(value, {"reason", "count"}, finding_context)
        reason = exact_str(item["reason"], f"{finding_context}.reason")
        if reason not in REVIEW_ACCEPTANCE_REASONS:
            fail(f"{finding_context}.reason", f"unsupported review reason {reason!r}")
        if reason in findings:
            fail(f"{finding_context}.reason", f"duplicate reason {reason!r}")
        findings[reason] = exact_int(item["count"], f"{finding_context}.count", minimum=1)
    return station, {
        "station": station,
        "page_id": page_id,
        "revision_id": revision_id,
        "codes": codes,
        "findings": findings,
        "evidence": evidence,
    }


def _station_connection_contract(
    value: object, context: str
) -> tuple[str, dict[str, Any]]:
    row = require_fields(
        value,
        {
            "station", "page_id", "revision_id", "codes",
            "source_bus_connections", "connections", "evidence",
        },
        context,
    )
    station = exact_str(row["station"], f"{context}.station")
    page_id = exact_int(row["page_id"], f"{context}.page_id", minimum=1)
    revision_id = exact_int(row["revision_id"], f"{context}.revision_id", minimum=1)
    codes = string_list(row["codes"], f"{context}.codes", nonempty=True)
    evidence = string_list(row["evidence"], f"{context}.evidence", nonempty=True)

    source_rows: list[dict[str, object]] = []
    source_codes: set[str] = set()
    for index, value in enumerate(_exact_array(
        row["source_bus_connections"],
        f"{context}.source_bus_connections",
        nonempty=True,
    )):
        item_context = f"{context}.source_bus_connections[{index}]"
        item = require_fields(
            value,
            {"row", "bus_stop_code", "stop_verbatim", "service_verbatim"},
            item_context,
        )
        source_row = exact_int(item["row"], f"{item_context}.row", minimum=2)
        code = _bus_stop_code(item["bus_stop_code"], f"{item_context}.bus_stop_code")
        if code in source_codes:
            fail(f"{item_context}.bus_stop_code", f"duplicate code {code}")
        source_codes.add(code)
        source_rows.append({
            "row": source_row,
            "bus_stop_code": code,
            "stop_verbatim": exact_str(
                item["stop_verbatim"], f"{item_context}.stop_verbatim"
            ),
            "service_verbatim": exact_str(
                item["service_verbatim"], f"{item_context}.service_verbatim"
            ),
        })

    connections: dict[str, dict[str, str]] = {}
    for index, value in enumerate(_exact_array(
        row["connections"], f"{context}.connections", nonempty=True
    )):
        item_context = f"{context}.connections[{index}]"
        item = require_fields(
            value, {"bus_stop_code", "kind", "relationship", "name"}, item_context
        )
        code = _bus_stop_code(item["bus_stop_code"], f"{item_context}.bus_stop_code")
        kind = exact_str(item["kind"], f"{item_context}.kind")
        relationship = exact_str(item["relationship"], f"{item_context}.relationship")
        name = exact_str(item["name"], f"{item_context}.name")
        if kind != "bus_interchange" or relationship != "inside_station_complex":
            fail(item_context, "unsupported station connection classification")
        if code not in source_codes:
            fail(f"{item_context}.bus_stop_code", "does not identify a source row")
        if code in connections:
            fail(f"{item_context}.bus_stop_code", f"duplicate connection for {code}")
        connections[code] = {
            "kind": kind,
            "relationship": relationship,
            "name": name,
        }
    if set(connections) != source_codes:
        fail(context, "every source bus connection must have a classification")
    return station, {
        "station": station,
        "page_id": page_id,
        "revision_id": revision_id,
        "codes": codes,
        "source_bus_connections": source_rows,
        "connections": connections,
        "evidence": evidence,
    }


def load_reviewed_decisions(path: str | Path) -> ReviewedDecisions:
    """Load the complete reviewed-decision file using an exact schema."""

    source = Path(path).resolve()
    raw = source.read_bytes()
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        fail("stage1 reviewed decisions", f"invalid JSON in {source}: {error}")
    table = require_fields(
        document,
        {
            "schema_version",
            "exit_scope_contracts",
            "exit_inventory_overrides",
            "bus_exit_overrides",
            "station_connection_contracts",
            "review_acceptances",
        },
        "stage1 reviewed decisions",
    )
    version = exact_int(table["schema_version"], "stage1 reviewed decisions.schema_version")
    if version != 1:
        fail("stage1 reviewed decisions.schema_version", f"unsupported version {version}")

    scope_contracts: dict[str, dict[str, Any]] = {}
    scope_rows = _exact_array(
        table["exit_scope_contracts"], "stage1 reviewed decisions.exit_scope_contracts"
    )
    for index, value in enumerate(scope_rows):
        station, contract = _scope_contract(
            value, f"stage1 reviewed decisions.exit_scope_contracts[{index}]"
        )
        if station in scope_contracts:
            fail("stage1 reviewed decisions.exit_scope_contracts", f"duplicate station {station}")
        scope_contracts[station] = contract

    inventory_overrides: dict[str, dict[str, Any]] = {}
    inventory_rows = _exact_array(
        table["exit_inventory_overrides"],
        "stage1 reviewed decisions.exit_inventory_overrides",
    )
    for index, value in enumerate(inventory_rows):
        station, override = _inventory_override(
            value, f"stage1 reviewed decisions.exit_inventory_overrides[{index}]"
        )
        if station in inventory_overrides:
            fail(
                "stage1 reviewed decisions.exit_inventory_overrides",
                f"duplicate station {station}",
            )
        inventory_overrides[station] = override

    bus_overrides: dict[str, dict[str, Any]] = {}
    override_rows = _exact_array(
        table["bus_exit_overrides"], "stage1 reviewed decisions.bus_exit_overrides"
    )
    for index, value in enumerate(override_rows):
        station, override = _bus_override(
            value, f"stage1 reviewed decisions.bus_exit_overrides[{index}]"
        )
        if station in bus_overrides:
            fail("stage1 reviewed decisions.bus_exit_overrides", f"duplicate station {station}")
        bus_overrides[station] = override

    station_connection_contracts: dict[str, dict[str, Any]] = {}
    connection_rows = _exact_array(
        table["station_connection_contracts"],
        "stage1 reviewed decisions.station_connection_contracts",
    )
    for index, value in enumerate(connection_rows):
        station, contract = _station_connection_contract(
            value,
            f"stage1 reviewed decisions.station_connection_contracts[{index}]",
        )
        if station in station_connection_contracts:
            fail(
                "stage1 reviewed decisions.station_connection_contracts",
                f"duplicate station {station}",
            )
        station_connection_contracts[station] = contract

    review_acceptances: dict[str, dict[str, Any]] = {}
    acceptance_rows = _exact_array(
        table["review_acceptances"], "stage1 reviewed decisions.review_acceptances"
    )
    for index, value in enumerate(acceptance_rows):
        station, acceptance = _review_acceptance(
            value, f"stage1 reviewed decisions.review_acceptances[{index}]"
        )
        if station in review_acceptances:
            fail("stage1 reviewed decisions.review_acceptances", f"duplicate station {station}")
        review_acceptances[station] = acceptance

    return ReviewedDecisions(
        path=source,
        sha256=sha256(raw).hexdigest(),
        scope_contracts=scope_contracts,
        inventory_overrides=inventory_overrides,
        bus_overrides=bus_overrides,
        station_connection_contracts=station_connection_contracts,
        review_acceptances=review_acceptances,
    )


def _station_input(value: object, context: str) -> dict[str, Any]:
    row = require_fields(
        value,
        {
            "station",
            "codes",
            "page_id",
            "revision_id",
            "source_inventory_status",
            "source_inventory_evidence_kind",
            "exits",
            "bus_links",
        },
        context,
    )
    station = exact_str(row["station"], f"{context}.station")
    codes = string_list(row["codes"], f"{context}.codes", nonempty=True)
    page_id = exact_int(row["page_id"], f"{context}.page_id", minimum=1)
    revision_id = exact_int(row["revision_id"], f"{context}.revision_id", minimum=1)
    source_inventory_status = exact_str(
        row["source_inventory_status"], f"{context}.source_inventory_status"
    )
    source_inventory_evidence_kind = exact_str(
        row["source_inventory_evidence_kind"],
        f"{context}.source_inventory_evidence_kind",
    )
    if (source_inventory_status, source_inventory_evidence_kind) not in {
        ("complete", "sgwiki_exit_table"),
        ("partial", "bus_connections_only"),
    }:
        fail(context, "unsupported source exit-inventory status/evidence combination")

    exit_rows = _exact_array(row["exits"], f"{context}.exits", nonempty=True)
    exits: list[dict[str, object]] = []
    seen_exit_ids: set[str] = set()
    for index, value in enumerate(exit_rows):
        exit_context = f"{context}.exits[{index}]"
        item = require_fields(value, {"label", "scopes"}, exit_context)
        label = _label(item["label"], f"{exit_context}.label")
        scopes = _scopes(item["scopes"], f"{exit_context}.scopes")
        identity = _exit_id(scopes, label)
        if identity in seen_exit_ids:
            fail(exit_context, f"duplicate exit identity {identity}")
        seen_exit_ids.add(identity)
        exits.append({"label": label, "scopes": scopes})

    link_rows = _exact_array(row["bus_links"], f"{context}.bus_links")
    bus_links: list[dict[str, object]] = []
    seen_bus_codes: set[str] = set()
    for index, value in enumerate(link_rows):
        link = _source_bus_link(value, f"{context}.bus_links[{index}]")
        code = str(link["bus_stop_code"])
        if code in seen_bus_codes:
            fail(f"{context}.bus_links[{index}].bus_stop_code", f"duplicate code {code}")
        seen_bus_codes.add(code)
        bus_links.append(link)
    return {
        "station": station,
        "codes": codes,
        "page_id": page_id,
        "revision_id": revision_id,
        "source_inventory_status": source_inventory_status,
        "source_inventory_evidence_kind": source_inventory_evidence_kind,
        "exits": exits,
        "bus_links": bus_links,
    }


def _resolve_station(station: dict[str, Any], decisions: ReviewedDecisions) -> dict[str, object]:
    name = station["station"]
    codes = station["codes"]
    page_id = station["page_id"]
    revision_id = station["revision_id"]
    scope_contract = decisions.scope_contracts.get(name)
    inventory_override = decisions.inventory_overrides.get(name)
    bus_override = decisions.bus_overrides.get(name)

    if inventory_override is not None and scope_contract is not None:
        fail(name, "inventory and scope overrides cannot both apply to one station")

    effective_source_exits = station["exits"]
    exit_inventory_status = station["source_inventory_status"]
    if inventory_override is not None:
        if codes != inventory_override["codes"]:
            fail(name, "station codes changed from the reviewed exit-inventory override")
        if page_id != inventory_override["page_id"]:
            fail(name, "SgWiki page identity changed from the reviewed exit-inventory override")
        if station["source_inventory_status"] != inventory_override["source_inventory_status"]:
            fail(name, "SgWiki exit-inventory status changed from the reviewed override")
        if (
            station["source_inventory_evidence_kind"]
            != inventory_override["source_inventory_evidence_kind"]
        ):
            fail(name, "SgWiki exit-inventory evidence kind changed from the reviewed override")
        if station["exits"] != inventory_override["source_exits"]:
            fail(name, "SgWiki partial exit inventory changed from the reviewed override")
        effective_source_exits = inventory_override["resolved_exits"]
        exit_inventory_status = "complete_reviewed_override"

    source_scopes_by_label: dict[str, tuple[str, ...]] = {}
    if scope_contract is not None:
        if codes != scope_contract["codes"]:
            fail(name, "station codes changed from the reviewed exit-scope contract")
        if page_id != scope_contract["page_id"]:
            fail(name, "SgWiki page identity changed from the reviewed exit-scope contract")
        for source_exit in effective_source_exits:
            label = str(source_exit["label"])
            if label in source_scopes_by_label:
                fail(name, f"repeated label {label} cannot use the station scope contract")
            source_scopes_by_label[label] = source_exit["scopes"]
        expected = {
            label: item["source_scopes"] for label, item in scope_contract["exits"].items()
        }
        if source_scopes_by_label != expected:
            fail(name, "SgWiki exit inventory/scopes changed from the reviewed contract")

    exits: list[dict[str, object]] = []
    exit_by_id: dict[str, dict[str, object]] = {}
    for source_exit in effective_source_exits:
        label = str(source_exit["label"])
        scopes = source_exit["scopes"]
        if scope_contract is not None:
            scopes = scope_contract["exits"][label]["resolved_scopes"]
        identity = _exit_id(scopes, label)
        if identity in exit_by_id:
            fail(name, f"scope resolution produced duplicate exit identity {identity}")
        output_exit = {
            "id": identity,
            "label": label,
            "scopes": list(scopes),
            "bus_stop_codes": [],
        }
        exits.append(output_exit)
        exit_by_id[identity] = output_exit

    contracts_applied: list[dict[str, object]] = []
    if scope_contract is not None:
        contracts_applied.append({
            "kind": "exit_scope_contract",
            "page_id": scope_contract["page_id"],
            "evidence": list(scope_contract["evidence"]),
        })
    if inventory_override is not None:
        contracts_applied.append({
            "kind": "exit_inventory_override",
            "page_id": inventory_override["page_id"],
            "source_inventory_status": inventory_override["source_inventory_status"],
            "source_inventory_evidence_kind": inventory_override[
                "source_inventory_evidence_kind"
            ],
            "evidence": list(inventory_override["evidence"]),
        })

    resolved_links: list[dict[str, object]] = []
    excluded_links: list[dict[str, object]] = []
    if bus_override is not None:
        if codes != bus_override["codes"]:
            fail(name, "station codes changed from the reviewed bus-exit override")
        if page_id != bus_override["page_id"]:
            fail(name, "SgWiki page identity changed from the reviewed bus-exit override")
        if revision_id != bus_override["revision_id"]:
            fail(
                name,
                "SgWiki page revision changed from the reviewed bus-exit override; review required",
            )
        if set(exit_by_id) != set(bus_override["expected_exit_ids"]):
            fail(name, "exit inventory changed from the reviewed bus-exit override")
        if station["bus_links"] != bus_override["source_bus_links"]:
            fail(name, "SgWiki bus links changed from the reviewed bus-exit override")
        for link in station["bus_links"]:
            code = str(link["bus_stop_code"])
            if code in bus_override["excluded"]:
                excluded_links.append({
                    **link,
                    "reason": bus_override["excluded"][code],
                })
                continue
            mapping = bus_override["mappings"][code]
            resolved_exit_ids = list(mapping["exit_ids"])
            for identity in resolved_exit_ids:
                exit_by_id[identity]["bus_stop_codes"].append(code)
            resolved_links.append({
                **link,
                "resolved_exit_ids": resolved_exit_ids,
                "resolution": "reviewed_deterministic_override",
                "evidence": list(mapping["evidence"]),
            })
        contracts_applied.append({
            "kind": "bus_exit_override",
            "page_id": bus_override["page_id"],
            "revision_id": bus_override["revision_id"],
            "evidence": list(bus_override["evidence"]),
        })
    else:
        for link in station["bus_links"]:
            resolved_exit_ids: list[str] = []
            for label in link["exit_labels"]:
                matches = [item["id"] for item in exits if item["label"] == label]
                if len(matches) != 1:
                    fail(
                        name,
                        f"bus stop {link['bus_stop_code']} label {label} resolves to "
                        f"{len(matches)} exits; a reviewed override is required",
                    )
                identity = str(matches[0])
                if identity not in resolved_exit_ids:
                    resolved_exit_ids.append(identity)
                    exit_by_id[identity]["bus_stop_codes"].append(link["bus_stop_code"])
            resolved_links.append({
                **link,
                "resolved_exit_ids": resolved_exit_ids,
                "resolution": "unique_exit_label",
                "evidence": [],
            })

    return {
        "station": name,
        "codes": list(codes),
        "source": {"kind": "sgwiki_station_page", "page_id": page_id, "revision_id": revision_id},
        "exit_inventory_status": exit_inventory_status,
        "exits": exits,
        "bus_links": resolved_links,
        "excluded_bus_links": excluded_links,
        "contracts_applied": contracts_applied,
    }


def build_stage1_reviewed_links(
    source_document: object, decisions: ReviewedDecisions
) -> dict[str, object]:
    """Resolve reviewed exit scopes and bus links for parsed SgWiki stations."""

    table = require_fields(source_document, {"schema_version", "stations"}, "stage1 input")
    version = exact_int(table["schema_version"], "stage1 input.schema_version")
    if version != 1:
        fail("stage1 input.schema_version", f"unsupported version {version}")
    station_rows = _exact_array(table["stations"], "stage1 input.stations", nonempty=True)
    stations: list[dict[str, object]] = []
    names: set[str] = set()
    for index, value in enumerate(station_rows):
        station = _station_input(value, f"stage1 input.stations[{index}]")
        if station["station"] in names:
            fail(f"stage1 input.stations[{index}].station", "duplicate station")
        names.add(station["station"])
        stations.append(_resolve_station(station, decisions))
    return {
        "schema_version": 1,
        "kind": "stage1_reviewed_exit_links",
        "reviewed_decisions": {
            "path": str(Path(decisions.path.parent.name) / decisions.path.name),
            "sha256": decisions.sha256,
        },
        "stations": stations,
    }


__all__ = [
    "ContractError",
    "ReviewedDecisions",
    "build_stage1_reviewed_links",
    "load_reviewed_decisions",
]
