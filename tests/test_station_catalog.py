import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sg_rail_station_data.config import load_default_config
from sg_rail_station_data.contracts import ContractError
from sg_rail_station_data.station_catalog import (
    load_station_catalog,
    station_codes_for_display,
    station_key,
)


class StationCatalogTests(unittest.TestCase):
    def test_repository_registry_has_exact_reviewed_inventory(self):
        config = load_default_config()
        catalog = load_station_catalog(
            config.paths.station_catalog,
            expected_count=config.inventory.expected_station_count,
        )
        self.assertEqual(len(catalog.stations), 184)
        self.assertEqual(len({station.name for station in catalog.stations}), 184)
        self.assertEqual(station_key("ADMIRALTY MRT STATION"), "ADMIRALTY")
        self.assertEqual(station_key("Admiralty station"), "ADMIRALTY")

    def test_station_codes_use_system_map_order_for_display(self):
        self.assertEqual(station_codes_for_display(["CC15", "NS17"]), ["NS17", "CC15"])
        self.assertEqual(
            station_codes_for_display(["CC1", "NE6", "NS24"]),
            ["NS24", "NE6", "CC1"],
        )
        self.assertEqual(
            station_codes_for_display(["TE17", "NE3", "EW16"]),
            ["EW16", "NE3", "TE17"],
        )

    def test_station_code_branches_inherit_parent_line_order(self):
        self.assertEqual(station_codes_for_display(["DT35", "CG1"]), ["CG1", "DT35"])
        self.assertEqual(station_codes_for_display(["DT16", "CE1"]), ["CE1", "DT16"])
        self.assertEqual(station_codes_for_display(["CP2", "TE10"]), ["TE10", "CP2"])

    def test_unknown_station_code_prefix_fails_loudly(self):
        with self.assertRaisesRegex(ContractError, "unsupported station code prefix 'XX'"):
            station_codes_for_display(["NS1", "XX2"])

    def test_mrt_codes_display_before_lrt_codes(self):
        self.assertEqual(station_codes_for_display(["BP6", "DT1"]), ["DT1", "BP6"])
        self.assertEqual(station_codes_for_display(["STC", "NE16"]), ["NE16", "STC"])

    def test_catalog_rejects_non_display_station_code_order(self):
        config = load_default_config()
        document = json.loads(config.paths.station_catalog.read_text(encoding="utf-8"))
        bishan = next(row for row in document["stations"] if row["name"] == "Bishan")
        bishan["codes"] = ["CC15", "NS17"]
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "stations.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "expected system-map display order"):
                load_station_catalog(path, expected_count=184)

    def test_catalog_rejects_unknown_station_code_prefix(self):
        config = load_default_config()
        document = json.loads(config.paths.station_catalog.read_text(encoding="utf-8"))
        document["stations"][0]["codes"] = ["XX10"]
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "stations.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "unsupported station code prefix 'XX'"):
                load_station_catalog(path, expected_count=184)

    def test_unknown_station_field_fails_loudly(self):
        config = load_default_config()
        document = json.loads(config.paths.station_catalog.read_text(encoding="utf-8"))
        document["stations"][0]["new_field"] = True
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "stations.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, r"extra=\['new_field'\]"):
                load_station_catalog(path, expected_count=184)

    def test_station_count_change_fails_loudly(self):
        config = load_default_config()
        with self.assertRaisesRegex(ContractError, "expected exactly 183 records"):
            load_station_catalog(config.paths.station_catalog, expected_count=183)


if __name__ == "__main__":
    unittest.main()
