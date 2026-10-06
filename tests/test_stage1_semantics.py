import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sg_rail_station_data.contracts import ContractError
from sg_rail_station_data.sources.lta_bus_stops import BusStop
from sg_rail_station_data.stage1 import load_reviewed_decisions
from sg_rail_station_data.stage1_semantics import build_full_stage1, compare_stage1
from sg_rail_station_data.stage1_layout import normalize_layout
from sg_rail_station_data.station_catalog import load_station_catalog
from tests.helpers import gtfs_inventory, sgwiki_snapshot, write_test_project


BUS_STOPS = (BusStop("99999", "Test Road", "Example Station Exit A", 1.3, 103.8),)


class Stage1SemanticsTests(unittest.TestCase):
    def build(self, snapshot=None):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = write_test_project(root)
        catalog = load_station_catalog(config.paths.station_catalog, expected_count=1)
        decisions = load_reviewed_decisions(config.paths.stage1_reviewed_decisions)
        return build_full_stage1(
            sgwiki_snapshot=snapshot or sgwiki_snapshot(),
            gtfs_inventory=gtfs_inventory(),
            bus_stops=BUS_STOPS,
            catalog=catalog,
            decisions=decisions,
        )

    def test_full_build_preserves_wayfinding_semantics_and_exact_bus_join(self):
        result = self.build()
        station = result.dataset["stations"][0]
        self.assertEqual(station["operational_status"], "active")
        self.assertEqual(station["exits"][0]["destinations"], ["Test Landmark"])
        self.assertEqual(station["bus_links"][0]["resolved_exit_ids"], ["MRT:A"])
        self.assertEqual(station["bus_links"][0]["lta"]["BusStopCode"], "99999")
        self.assertEqual(station["platforms"][0]["level"], "B2")
        self.assertEqual(station["platforms"][0]["concourse_levels"], ["B1"])
        self.assertEqual(result.dataset["summary"]["exit_identity_count"], 1)
        self.assertEqual(result.dataset["status"], "complete")

    def test_repeated_label_combines_evidence_without_merging_peer_exits(self):
        snapshot = sgwiki_snapshot()
        source = snapshot["stations"][0]["source"]
        first = copy.deepcopy(source["entries"][0])
        first.update({
            "labels": ["E", "G"],
            "label_cell_verbatim": "E G The Atrium@Orchard",
            "row_verbatim": ["E G", "The Atrium@Orchard"],
            "row": 2,
            "destination_verbatim": "The Atrium@Orchard",
        })
        second = copy.deepcopy(source["entries"][0])
        second.update({
            "labels": ["F", "G"],
            "label_cell_verbatim": "F G Macdonald House",
            "row_verbatim": ["F G", "Macdonald House"],
            "row": 3,
            "destination_verbatim": "Macdonald House",
        })
        source["labels"] = ["E", "F", "G"]
        source["entries"] = [first, second]
        source["reviews"] = [{
            "reason": "repeated_exit_identity_in_source_table",
            "identity": ["MRT", "G"],
            "table": 1,
            "row": 3,
        }]
        source["bus_connections"] = {
            "status": "partial_positive_evidence",
            "labels": ["E"],
            "rows": [{
                "row": 2,
                "bus_stop_code": "99999",
                "stop_verbatim": "99999 Test Road",
                "labels": ["E"],
                "service_verbatim": "1 2 3",
            }],
            "unlinked_rows": [],
            "reviews": [],
        }

        result = self.build(snapshot)
        exits = {
            item["id"]: item
            for item in result.dataset["stations"][0]["exits"]
        }
        self.assertEqual(set(exits), {"MRT:E", "MRT:F", "MRT:G"})
        self.assertEqual(exits["MRT:E"]["destinations"], ["The Atrium@Orchard"])
        self.assertEqual(exits["MRT:F"]["destinations"], ["Macdonald House"])
        self.assertEqual(
            exits["MRT:G"]["destinations"],
            ["The Atrium@Orchard", "Macdonald House"],
        )
        self.assertEqual([row["row"] for row in exits["MRT:E"]["source_rows"]], [2])
        self.assertEqual([row["row"] for row in exits["MRT:F"]["source_rows"]], [3])
        self.assertEqual([row["row"] for row in exits["MRT:G"]["source_rows"]], [2, 3])
        self.assertEqual(result.dataset["normalizations"], [{
            "station": "Example",
            "kind": "repeated_exit_evidence_merged",
            "scopes": ["MRT"],
            "label": "G",
            "source_table": 1,
            "source_row": 3,
        }])

    def test_partial_inventory_survives_as_warning_instead_of_fake_completeness(self):
        snapshot = sgwiki_snapshot()
        source = snapshot["stations"][0]["source"]
        source["status"] = "partial"
        source["evidence_kind"] = "bus_connections_only"
        source["entries"] = []
        source["reviews"] = [{"reason": "exit_table_missing_bus_fallback", "bus_labels": ["A"]}]
        result = self.build(snapshot)
        station = result.dataset["stations"][0]
        self.assertEqual(station["exit_inventory_status"], "partial")
        self.assertEqual(station["exits"][0]["id"], "MRT:A")
        self.assertTrue(any(item["reason"] == "partial_exit_inventory" for item in station["reviews"]))
        self.assertFalse(result.dataset["publication_allowed"])

    def test_reviewed_partial_inventory_becomes_complete_without_partial_warnings(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = write_test_project(root)
        decisions_path = config.paths.stage1_reviewed_decisions
        document = json.loads(decisions_path.read_text(encoding="utf-8"))
        document["exit_inventory_overrides"] = [{
            "station": "Example",
            "page_id": 1,
            "codes": ["NS99"],
            "source_inventory_status": "partial",
            "source_inventory_evidence_kind": "bus_connections_only",
            "source_exits": [{"label": "A", "scopes": ["MRT"]}],
            "resolved_exits": [
                {"label": "A", "scopes": ["MRT"]},
                {"label": "B", "scopes": ["MRT"]},
            ],
            "evidence": ["test-reviewed complete inventory"],
        }]
        decisions_path.write_text(json.dumps(document), encoding="utf-8")
        snapshot = sgwiki_snapshot()
        source = snapshot["stations"][0]["source"]
        source["status"] = "partial"
        source["evidence_kind"] = "bus_connections_only"
        source["entries"] = []
        source["reviews"] = [{"reason": "exit_table_missing_bus_fallback", "bus_labels": ["A"]}]
        catalog = load_station_catalog(config.paths.station_catalog, expected_count=1)
        decisions = load_reviewed_decisions(decisions_path)
        result = build_full_stage1(
            sgwiki_snapshot=snapshot,
            gtfs_inventory=gtfs_inventory(),
            bus_stops=BUS_STOPS,
            catalog=catalog,
            decisions=decisions,
        )
        station = result.dataset["stations"][0]
        self.assertEqual(station["exit_inventory_status"], "complete_reviewed_override")
        self.assertEqual([item["id"] for item in station["exits"]], ["MRT:A", "MRT:B"])
        self.assertEqual(result.dataset["summary"]["complete_exit_inventory_count"], 1)
        self.assertEqual(result.dataset["summary"]["partial_exit_inventory_count"], 0)
        self.assertFalse(any(
            item["reason"] in {"partial_exit_inventory", "sgwiki_source_review"}
            for item in station["reviews"]
        ))

    def test_crossed_wheelchair_evidence_marks_exit_not_accessible(self):
        snapshot = sgwiki_snapshot()
        entry = snapshot["stations"][0]["source"]["entries"][0]
        entry["wheelchair_access"] = "not_accessible"
        result = self.build(snapshot)
        accessibility = result.dataset["stations"][0]["exits"][0]["accessibility"]
        self.assertEqual(accessibility["status"], "not_accessible")
        self.assertEqual(accessibility["evidence"], ["sgwiki_crossed_wheelchair_icon"])
        self.assertEqual(result.dataset["summary"]["not_accessible_exit_identity_count"], 1)

    def test_source_reported_closure_and_operating_hours_are_structured(self):
        snapshot = sgwiki_snapshot()
        source = snapshot["stations"][0]["source"]
        source["exit_statuses"] = [{
            "labels": ["A"],
            "scopes": ["MRT"],
            "destination_verbatim": "Test Landmark",
            "status": "closed",
            "alternative_exits": ["B"],
            "status_verbatim": "(Closed – use Exit B)",
            "status_markup": "small.d-block.mx-auto.fw-normal.tx-center",
        }]
        source["exit_operating_hours"] = [{
            "labels": ["A"],
            "scopes": ["MRT"],
            "days": [
                "monday", "tuesday", "wednesday", "thursday",
                "friday", "saturday", "sunday",
            ],
            "opens": "10:00",
            "closes": "22:00",
            "timezone": "Asia/Singapore",
            "source_verbatim": "Notes: Exit A is open from 10:00 - 22:00 daily.",
            "grammar": "daily_open_interval",
        }]
        result = self.build(snapshot)
        exit_record = result.dataset["stations"][0]["exits"][0]
        self.assertEqual(exit_record["operating_status"], "source_reported_closed")
        self.assertEqual(exit_record["alternative_exits"], ["B"])
        self.assertEqual(exit_record["operating_hours"][0]["opens"], "10:00")
        self.assertEqual(result.dataset["summary"]["closed_exit_identity_count"], 1)

    def test_grouped_source_platforms_become_individual_records(self):
        source = sgwiki_snapshot()["stations"][0]["source"]["layout"]
        source["floors"][2]["platforms"][0]["label"] = "A/B"
        source["reviews"] = [{
            "reason": "combined_platform_label",
            "row": 3,
            "label": "A/B",
        }]
        layout = normalize_layout(source, station="Example", codes=["NS99"])
        self.assertEqual(
            [(item["label"], item["source_group_label"]) for item in layout["platforms"]],
            [("A", "A/B"), ("B", "A/B")],
        )
        self.assertEqual(layout["coverage"]["platform_count"], 2)
        self.assertEqual(layout["coverage"]["source_platform_record_count"], 1)
        self.assertEqual(layout["coverage"]["grouped_source_platform_record_count"], 1)

    def test_revision_pinned_station_complex_connection_retains_unlinked_bus_row(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = write_test_project(root)
        decisions_path = config.paths.stage1_reviewed_decisions
        document = json.loads(decisions_path.read_text(encoding="utf-8"))
        document["station_connection_contracts"] = [{
            "station": "Example",
            "page_id": 1,
            "revision_id": 2,
            "codes": ["NS99"],
            "source_bus_connections": [{
                "row": 3,
                "bus_stop_code": "99998",
                "stop_verbatim": "99998 Example Interchange",
                "service_verbatim": "4 5 6",
            }],
            "connections": [{
                "bus_stop_code": "99998",
                "kind": "bus_interchange",
                "relationship": "inside_station_complex",
                "name": "Example Bus Interchange",
            }],
            "evidence": ["test-reviewed internal station connection"],
        }]
        decisions_path.write_text(json.dumps(document), encoding="utf-8")
        snapshot = sgwiki_snapshot()
        source = snapshot["stations"][0]["source"]
        source["bus_connections"]["unlinked_rows"] = [{
            "row": 3,
            "bus_stop_code": "99998",
            "stop_verbatim": "99998 Example Interchange",
            "service_verbatim": "4 5 6",
        }]
        source["bus_connections"]["reviews"] = [{
            "reason": "bus_stop_without_exit_link",
            "row": 3,
            "stop_verbatim": "99998 Example Interchange",
        }]
        source["reviews"] = [{
            "section": "bus_connections",
            "reason": "bus_stop_without_exit_link",
            "row": 3,
            "stop_verbatim": "99998 Example Interchange",
        }]
        catalog = load_station_catalog(config.paths.station_catalog, expected_count=1)
        bus_stops = BUS_STOPS + (
            BusStop("99998", "Example Road", "Example Interchange", 1.31, 103.81),
        )
        result = build_full_stage1(
            sgwiki_snapshot=snapshot,
            gtfs_inventory=gtfs_inventory(),
            bus_stops=bus_stops,
            catalog=catalog,
            decisions=load_reviewed_decisions(decisions_path),
        )
        station = result.dataset["stations"][0]
        self.assertEqual(station["reviews"], [])
        self.assertEqual(station["station_connections"][0]["kind"], "bus_interchange")
        self.assertEqual(
            station["station_connections"][0]["relationship"],
            "inside_station_complex",
        )
        self.assertEqual(station["station_connections"][0]["resolved_exit_ids"], [])
        self.assertEqual(result.dataset["summary"]["station_connection_count"], 1)

        stale = copy.deepcopy(snapshot)
        stale["stations"][0]["source"]["revision_id"] = 3
        with self.assertRaisesRegex(ContractError, "page revision changed"):
            build_full_stage1(
                sgwiki_snapshot=stale,
                gtfs_inventory=gtfs_inventory(),
                bus_stops=bus_stops,
                catalog=catalog,
                decisions=load_reviewed_decisions(decisions_path),
            )

    def test_reviews_are_grouped_by_reason_without_losing_station_findings(self):
        snapshot = sgwiki_snapshot()
        snapshot["stations"][0]["source"]["layout"]["reviews"] = [
            {"reason": "combined_platform_label", "row": 3, "label": "A/B"},
            {"reason": "combined_platform_label", "row": 4, "label": "C/D"},
        ]
        result = self.build(snapshot)
        self.assertEqual(result.reviews["group_count"], 1)
        self.assertEqual(result.reviews["finding_count"], 2)
        group = result.reviews["groups"][0]
        self.assertEqual(group["detail_reason"], "combined_platform_label")
        self.assertEqual(group["station_names"], ["Example"])
        self.assertEqual(group["station_count"], 1)
        self.assertEqual(group["finding_count"], 2)
        self.assertEqual(len(group["findings"]), 2)

    def test_layout_service_diagram_is_retained_as_evidence_not_warning(self):
        snapshot = sgwiki_snapshot()
        snapshot["stations"][0]["source"]["layout"]["supplementary_tables"] = [
            {"table": 2, "kind": "rail_service_diagram"}
        ]
        result = self.build(snapshot)
        station = result.dataset["stations"][0]
        self.assertEqual(station["layout_supplements"], [
            {"table": 2, "kind": "rail_service_diagram"}
        ])
        self.assertEqual(result.dataset["summary"]["rail_service_diagram_count"], 1)
        self.assertEqual(result.reviews["count"], 0)

    def test_scope_contract_consumes_only_unspecified_scope_warning(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = write_test_project(root)
        decisions_path = config.paths.stage1_reviewed_decisions
        document = json.loads(decisions_path.read_text(encoding="utf-8"))
        document["exit_scope_contracts"] = [{
            "station": "Example",
            "page_id": 1,
            "codes": ["NS99"],
            "exits": [{
                "label": "A",
                "source_scopes": [],
                "resolved_scopes": ["MRT"],
            }],
            "evidence": ["test-reviewed scope"],
        }]
        decisions_path.write_text(json.dumps(document), encoding="utf-8")
        snapshot = sgwiki_snapshot()
        entry = snapshot["stations"][0]["source"]["entries"][0]
        entry["scopes"] = None
        snapshot["stations"][0]["source"]["reviews"] = [{
            "reason": "unspecified_exit_scope",
            "table": 1,
            "row": 2,
            "text": "A Test Landmark",
        }]
        catalog = load_station_catalog(config.paths.station_catalog, expected_count=1)
        result = build_full_stage1(
            sgwiki_snapshot=snapshot,
            gtfs_inventory=gtfs_inventory(),
            bus_stops=BUS_STOPS,
            catalog=catalog,
            decisions=load_reviewed_decisions(decisions_path),
        )
        self.assertEqual(result.dataset["stations"][0]["exits"][0]["scopes"], ["MRT"])
        self.assertEqual(result.reviews["count"], 0)

    def test_revision_pinned_review_acceptance_consumes_exact_finding(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = write_test_project(root)
        decisions_path = config.paths.stage1_reviewed_decisions
        document = json.loads(decisions_path.read_text(encoding="utf-8"))
        document["review_acceptances"] = [{
            "station": "Example",
            "page_id": 1,
            "revision_id": 2,
            "codes": ["NS99"],
            "findings": [{
                "reason": "platform_with_multiple_concourse_levels",
                "count": 1,
            }],
            "evidence": ["test-reviewed two-level concourse"],
        }]
        decisions_path.write_text(json.dumps(document), encoding="utf-8")
        snapshot = sgwiki_snapshot()
        snapshot["stations"][0]["source"]["layout"]["floors"].append({
            "row": 4,
            "floor": "B3",
            "area_verbatim": "NSL Concourse",
            "platforms": [],
        })
        catalog = load_station_catalog(config.paths.station_catalog, expected_count=1)
        result = build_full_stage1(
            sgwiki_snapshot=snapshot,
            gtfs_inventory=gtfs_inventory(),
            bus_stops=BUS_STOPS,
            catalog=catalog,
            decisions=load_reviewed_decisions(decisions_path),
        )
        station = result.dataset["stations"][0]
        self.assertEqual(result.reviews["count"], 0)
        self.assertEqual(station["source"]["contracts_applied"][-1]["kind"], "review_acceptance")

        stale_snapshot = copy.deepcopy(snapshot)
        stale_snapshot["stations"][0]["source"]["revision_id"] = 3
        with self.assertRaisesRegex(ContractError, "page revision changed"):
            build_full_stage1(
                sgwiki_snapshot=stale_snapshot,
                gtfs_inventory=gtfs_inventory(),
                bus_stops=BUS_STOPS,
                catalog=catalog,
                decisions=load_reviewed_decisions(decisions_path),
            )

    def test_unknown_source_field_stops_build(self):
        with self.assertRaisesRegex(ContractError, r"extra=\['new_upstream_field'\]"):
            self.build(sgwiki_snapshot(unknown_entry_field=True))

    def test_revision_and_exit_changes_are_explicit(self):
        baseline = self.build().dataset
        current = copy.deepcopy(baseline)
        current["stations"][0]["source"]["revision_id"] = 3
        current["stations"][0]["exits"].append({
            **current["stations"][0]["exits"][0],
            "id": "MRT:B",
            "label": "B",
        })
        changes = compare_stage1(current, baseline)
        self.assertEqual(changes["highest_severity"], "WARNING")
        self.assertEqual(
            {item["reason"] for item in changes["items"]},
            {"sgwiki_revision_changed", "exit_added", "station_semantics_changed"},
        )

    def test_gtfs_match_change_is_explicit(self):
        baseline = self.build().dataset
        current = copy.deepcopy(baseline)
        current["stations"][0]["gtfs_matches"][0]["route_names"] = ["Changed Line"]
        changes = compare_stage1(current, baseline)
        self.assertEqual(changes["highest_severity"], "WARNING")
        self.assertEqual(changes["items"], [{
            "severity": "WARNING",
            "reason": "station_semantics_changed",
            "station_id": "example",
            "fields": ["gtfs_matches"],
        }])


if __name__ == "__main__":
    unittest.main()
