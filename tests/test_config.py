from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sg_rail_station_data.config import ConfigError, DEFAULT_CONFIG_PATH, load_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "config").mkdir()
        self.path = self.root / "config/pipeline.toml"
        self.original = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, content):
        self.path.write_text(content, encoding="utf-8")
        return self.path

    def test_default_contract_loads_and_resolves_repo_owned_runs_path(self):
        config = load_config(self.write(self.original))
        self.assertEqual(config.project.name, "sg-rail-station-data")
        self.assertEqual(config.inventory.expected_station_count, 184)
        self.assertEqual(config.locale.timezone, "Asia/Singapore")
        self.assertEqual(config.paths.runs_dir, (self.root / "runs").resolve())
        self.assertEqual(
            config.paths.station_catalog,
            (self.root / "config/stations.json").resolve(),
        )
        self.assertEqual(
            config.paths.stage1_reviewed_decisions,
            (self.root / "config/stage1-reviewed-decisions.json").resolve(),
        )
        self.assertEqual(
            config.paths.stage1_approved_baseline,
            (self.root / "config/stage1-approved-baseline.json").resolve(),
        )
        self.assertEqual(
            config.paths.stage2_approved_baseline,
            (self.root / "config/stage2-approved-baseline.json").resolve(),
        )
        self.assertEqual(
            config.paths.stage2_reviewed_coordinates,
            (self.root / "config/stage2-reviewed-coordinates.geojson").resolve(),
        )
        self.assertEqual(
            config.paths.stage2_coordinate_ledger,
            (self.root / "config/stage2-coordinate-ledger.json").resolve(),
        )
        self.assertEqual(
            config.paths.stage2_trusted_onemap_baseline,
            (self.root / "config/stage2-trusted-onemap-baseline.geojson").resolve(),
        )
        self.assertEqual(config.sources.sgwiki_api.allowed_hosts,
                         ("sgwiki.miraheze.org",))
        self.assertRegex(config.config_sha256, r"^[0-9a-f]{64}$")

    def test_unknown_field_fails_loudly(self):
        changed = self.original.replace(
            'schema_version = 1', 'schema_version = 1\nunexpected = true', 1)
        with self.assertRaisesRegex(ConfigError, r"extra=\['unexpected'\]"):
            load_config(self.write(changed))

    def test_wrong_exact_type_is_not_coerced(self):
        changed = self.original.replace("expected_station_count = 184",
                                        "expected_station_count = true")
        with self.assertRaisesRegex(ConfigError, "expected integer, got bool"):
            load_config(self.write(changed))

    def test_unknown_schema_version_fails_loudly(self):
        changed = self.original.replace("schema_version = 1", "schema_version = 2", 1)
        with self.assertRaisesRegex(ConfigError, "unsupported version 2"):
            load_config(self.write(changed))

    def test_runs_path_cannot_escape_repository(self):
        changed = self.original.replace('runs_dir = "runs"', 'runs_dir = "../outside"')
        with self.assertRaisesRegex(ConfigError, "must resolve within"):
            load_config(self.write(changed))

    def test_endpoint_must_match_allowlist(self):
        changed = self.original.replace(
            "https://sgwiki.miraheze.org/w/api.php",
            "https://example.invalid/w/api.php",
            1,
        )
        with self.assertRaisesRegex(ConfigError, "not in allowed_hosts"):
            load_config(self.write(changed))


if __name__ == "__main__":
    unittest.main()
