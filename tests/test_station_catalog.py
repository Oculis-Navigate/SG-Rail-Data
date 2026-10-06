import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sg_rail_station_data.config import load_default_config
from sg_rail_station_data.contracts import ContractError
from sg_rail_station_data.station_catalog import load_station_catalog, station_key


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
