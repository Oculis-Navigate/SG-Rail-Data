import contextlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sg_rail_station_data.cli import main
from sg_rail_station_data.contracts import ContractError
from sg_rail_station_data.stage1 import build_stage1_reviewed_links, load_reviewed_decisions


REPO_ROOT = Path(__file__).resolve().parents[1]
DECISIONS_PATH = REPO_ROOT / "config/stage1-reviewed-decisions.json"


def shared_station(name="Punggol", page_id=237, revision_id=100):
    return {
        "station": name,
        "codes": ["NE17", "PTC"],
        "page_id": page_id,
        "revision_id": revision_id,
        "source_inventory_status": "complete",
        "source_inventory_evidence_kind": "sgwiki_exit_table",
        "exits": [{"label": label, "scopes": []} for label in "ABCD"],
        "bus_links": [
            {
                "bus_stop_code": "12345",
                "stop_verbatim": "12345 Punggol Station Exit B",
                "exit_labels": ["B"],
            }
        ],
    }


def bukit_panjang_station():
    identities = (
        ("LRT", "A"),
        ("LRT", "B"),
        ("LRT", "C"),
        ("LRT", "D"),
        ("MRT", "A1"),
        ("MRT", "A2"),
        ("MRT", "B"),
        ("MRT", "C"),
        ("MRT", "D"),
    )
    links = (
        ("45009", "45009 Bt Panjang Int Petir Rd", ["A", "B"]),
        ("44029", "44029 Bt Panjang Stn Exit A/LRT Upp Bt Timah Rd", ["A"]),
        ("44021", "44021 Bt Panjang Stn Exit B Upp Bt Timah Rd", ["B"]),
        ("44251", "44251 Bt Panjang Stn/Blk 604 Bt Panjang Rd", ["C"]),
        ("44031", "44031 Aft Bt Panjang Stn Upp Bt Timah Rd", ["A"]),
        ("44259", "44259 Blk 183 Bt Panjang Rd", ["A"]),
        ("44909", "44909 Blk 184 Bt Panjang Rd", ["A"]),
        ("44859", "44859 Opp Bt Panjang Stn Petir Rd", ["A"]),
    )
    return {
        "station": "Bukit Panjang",
        "codes": ["BP6", "DT1"],
        "page_id": 342,
        "revision_id": 36376,
        "source_inventory_status": "complete",
        "source_inventory_evidence_kind": "sgwiki_exit_table",
        "exits": [{"label": label, "scopes": [scope]} for scope, label in identities],
        "bus_links": [
            {"bus_stop_code": code, "stop_verbatim": stop, "exit_labels": labels}
            for code, stop, labels in links
        ],
    }


def cantonment_station():
    return {
        "station": "Cantonment",
        "codes": ["CC31"],
        "page_id": 278,
        "revision_id": 35867,
        "source_inventory_status": "partial",
        "source_inventory_evidence_kind": "bus_connections_only",
        "exits": [
            {"label": label, "scopes": ["MRT"]}
            for label in ("1", "3", "4")
        ],
        "bus_links": [{
            "bus_stop_code": "14071",
            "stop_verbatim": "14071 Keppel Workshop Keppel Rd",
            "exit_labels": ["1"],
        }],
    }


class Stage1ReviewedDecisionTests(unittest.TestCase):
    def setUp(self):
        self.decisions = load_reviewed_decisions(DECISIONS_PATH)

    def build(self, *stations):
        return build_stage1_reviewed_links(
            {"schema_version": 1, "stations": list(stations)}, self.decisions
        )

    def test_shared_hub_exits_resolve_to_one_physical_identity(self):
        station = self.build(shared_station())["stations"][0]
        self.assertEqual(
            [exit_record["id"] for exit_record in station["exits"]],
            ["SHARED:A", "SHARED:B", "SHARED:C", "SHARED:D"],
        )
        self.assertTrue(
            all(exit_record["scopes"] == ["MRT", "LRT"] for exit_record in station["exits"])
        )
        self.assertEqual(station["exits"][1]["bus_stop_codes"], ["12345"])
        self.assertEqual(station["contracts_applied"][0]["kind"], "exit_scope_contract")

    def test_choa_chu_kang_only_exit_e_is_shared(self):
        source = {
            "station": "Choa Chu Kang",
            "codes": ["BP1", "JS1", "NS4"],
            "page_id": 109,
            "revision_id": 500,
            "source_inventory_status": "complete",
            "source_inventory_evidence_kind": "sgwiki_exit_table",
            "exits": [
                {"label": label, "scopes": ["MRT"]} for label in "ABCD"
            ] + [{"label": "E", "scopes": ["MRT", "LRT"]}],
            "bus_links": [],
        }
        station = self.build(source)["stations"][0]
        self.assertEqual(
            [exit_record["id"] for exit_record in station["exits"]],
            ["MRT:A", "MRT:B", "MRT:C", "MRT:D", "SHARED:E"],
        )

    def test_bukit_panjang_reviewed_mapping_and_exclusions(self):
        station = self.build(bukit_panjang_station())["stations"][0]
        exits = {item["id"]: item for item in station["exits"]}
        self.assertEqual(exits["LRT:A"]["bus_stop_codes"], ["45009", "44031", "44859"])
        self.assertEqual(exits["LRT:B"]["bus_stop_codes"], ["45009"])
        self.assertEqual(exits["MRT:A1"]["bus_stop_codes"], ["44029"])
        self.assertEqual(exits["MRT:A2"]["bus_stop_codes"], ["44029"])
        self.assertEqual(exits["MRT:B"]["bus_stop_codes"], ["44021"])
        self.assertEqual(exits["MRT:C"]["bus_stop_codes"], ["44251"])
        self.assertEqual(
            [item["bus_stop_code"] for item in station["excluded_bus_links"]],
            ["44259", "44909"],
        )
        self.assertEqual(
            [item["bus_stop_code"] for item in station["bus_links"]],
            ["45009", "44029", "44021", "44251", "44031", "44859"],
        )

    def test_partial_inventory_override_adds_only_reviewed_exits(self):
        station = self.build(cantonment_station())["stations"][0]
        self.assertEqual(
            [item["id"] for item in station["exits"]],
            ["MRT:1", "MRT:2", "MRT:3", "MRT:4"],
        )
        self.assertEqual(station["exit_inventory_status"], "complete_reviewed_override")
        self.assertEqual(station["bus_links"][0]["resolved_exit_ids"], ["MRT:1"])
        self.assertEqual(station["contracts_applied"][0]["kind"], "exit_inventory_override")

    def test_partial_inventory_override_fails_when_source_inventory_changes(self):
        source = cantonment_station()
        source["exits"].append({"label": "2", "scopes": ["MRT"]})
        with self.assertRaisesRegex(ContractError, "partial exit inventory changed"):
            self.build(source)

        source = cantonment_station()
        source["source_inventory_status"] = "complete"
        source["source_inventory_evidence_kind"] = "sgwiki_exit_table"
        with self.assertRaisesRegex(ContractError, "exit-inventory status changed"):
            self.build(source)

    def test_bukit_panjang_revision_pin_is_page_specific(self):
        changed_bukit_panjang = bukit_panjang_station()
        changed_bukit_panjang["revision_id"] += 1
        with self.assertRaisesRegex(ContractError, "page revision changed"):
            self.build(changed_bukit_panjang)

        first = self.build(shared_station(revision_id=100))["stations"][0]
        second = self.build(shared_station(revision_id=101))["stations"][0]
        self.assertEqual(first["exits"], second["exits"])
        self.assertEqual(first["bus_links"], second["bus_links"])
        self.assertEqual(first["source"]["revision_id"], 100)
        self.assertEqual(second["source"]["revision_id"], 101)

    def test_bukit_panjang_bus_row_change_fails_loudly(self):
        source = bukit_panjang_station()
        source["bus_links"][0]["stop_verbatim"] += " changed"
        with self.assertRaisesRegex(ContractError, "SgWiki bus links changed"):
            self.build(source)

    def test_unknown_input_field_fails_loudly(self):
        source = shared_station()
        source["unexpected"] = True
        with self.assertRaisesRegex(ContractError, r"extra=\['unexpected'\]"):
            self.build(source)

    def test_cli_writes_new_output_and_refuses_overwrite(self):
        source = {"schema_version": 1, "stations": [bukit_panjang_station()]}
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "input.json"
            output_path = root / "output.json"
            input_path.write_text(json.dumps(source), encoding="utf-8")
            arguments = [
                "stage1",
                "resolve-reviewed-links",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
                "--json",
            ]
            self.assertEqual(main(arguments), 0)
            result = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(result["stations"][0]["station"], "Bukit Panjang")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(arguments), 1)

    def test_reviewed_decision_file_rejects_unknown_fields(self):
        document = json.loads(DECISIONS_PATH.read_text(encoding="utf-8"))
        document["unexpected"] = True
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "decisions.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, r"extra=\['unexpected'\]"):
                load_reviewed_decisions(path)


if __name__ == "__main__":
    unittest.main()
