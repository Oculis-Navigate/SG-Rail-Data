import contextlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sg_rail_station_data.cli import main
from tests.helpers import SERVICE_DATE, write_replay_bundle, write_test_project


class CliTests(unittest.TestCase):
    def test_config_validate_json(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["config", "validate", "--json"])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["project"], "sg-rail-station-data")
        self.assertEqual(result["expected_station_count"], 184)

    def test_pipeline_command_and_duplicate_failure(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = write_test_project(root / "project")
            bundle = write_replay_bundle(root / "bundle")
            arguments = [
                "pipeline", "run",
                "--config", str(config.config_path),
                "--runs-dir", str(root / "runs"),
                "--run-id", "cli-test",
                "--service-date", SERVICE_DATE,
                "--replay-sources", str(bundle),
                "--json",
            ]
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(arguments)
            self.assertEqual(code, 2)
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "review_required")
            self.assertEqual(result["exit_code"], 2)
            self.assertFalse(result["data_release_created"])
            self.assertTrue(Path(result["manifest"]).is_file())

            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                duplicate_code = main(arguments)
            self.assertEqual(duplicate_code, 1)
            self.assertIn("Refusing to reuse existing run directory", error.getvalue())

    def test_credentials_validate_uses_environment_without_printing_key(self):
        output = io.StringIO()
        with patch.dict("os.environ", {"LTA_API_KEY": "super-secret-value"}, clear=True):
            with contextlib.redirect_stdout(output):
                code = main(["credentials", "validate", "--json"])
        self.assertEqual(code, 0)
        self.assertNotIn("super-secret-value", output.getvalue())
        result = json.loads(output.getvalue())
        self.assertEqual(result, {
            "credential": "LTA_API_KEY",
            "source": "environment variable LTA_API_KEY",
            "status": "valid",
        })

    def test_lta_bus_stops_collection_command_does_not_print_key(self):
        output = io.StringIO()
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "bus-stops"
            fake_result = SimpleNamespace(
                manifest_path=root / "collection.json",
                output_directory=root,
                page_count=12,
                record_count=5_123,
                snapshot_path=root / "snapshot.json",
            )
            with (
                patch.dict("os.environ", {"LTA_API_KEY": "super-secret-value"}, clear=True),
                patch(
                    "sg_rail_station_data.cli.collect_lta_bus_stops",
                    return_value=fake_result,
                ) as collector,
                contextlib.redirect_stdout(output),
            ):
                code = main(
                    [
                        "sources",
                        "collect-lta-bus-stops",
                        "--output-dir",
                        str(root),
                        "--json",
                    ]
                )
        self.assertEqual(code, 0)
        self.assertNotIn("super-secret-value", output.getvalue())
        result = json.loads(output.getvalue())
        self.assertEqual(result["record_count"], 5_123)
        self.assertEqual(result["page_count"], 12)
        self.assertEqual(
            collector.call_args.kwargs["account_key"], "super-secret-value"
        )

    def test_sgwiki_mapdata_collection_command_uses_saved_station_evidence(self):
        output = io.StringIO()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sgwiki"
            destination = root / "mapdata"
            fake_result = SimpleNamespace(
                output_directory=destination,
                snapshot_path=destination / "snapshot.json",
                manifest_path=destination / "collection.json",
                stations=({"station": "Mapped"}, {"station": "No map"}),
                no_map_station_names=("No map",),
            )
            with (
                patch(
                    "sg_rail_station_data.cli.collect_sgwiki_mapdata",
                    return_value=fake_result,
                ) as collector,
                contextlib.redirect_stdout(output),
            ):
                code = main([
                    "sources", "collect-sgwiki-mapdata",
                    "--sgwiki-directory", str(source),
                    "--output-dir", str(destination),
                    "--json",
                ])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["embedded_map_count"], 1)
        self.assertEqual(result["no_embedded_map_count"], 1)
        self.assertEqual(
            collector.call_args.kwargs["sgwiki_directory"], source
        )


if __name__ == "__main__":
    unittest.main()
