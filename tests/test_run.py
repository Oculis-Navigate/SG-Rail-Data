from datetime import UTC, datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mrt_wayfinding.config import DEFAULT_CONFIG_PATH, load_config
from mrt_wayfinding.contracts import ContractError
from mrt_wayfinding.pipeline import run_pipeline
from mrt_wayfinding.run import RunContext, RunLifecycleError, RunStatus
from mrt_wayfinding.storage import OutputExistsError
from tests.helpers import SERVICE_DATE, write_replay_bundle, write_test_project
from datetime import date


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_pipeline_replays_sources_and_creates_reviewable_stage1(self):
        project = self.root / "project"
        bundle = write_replay_bundle(self.root / "bundle")
        result = run_pipeline(
            config=write_test_project(project),
            service_date=date.fromisoformat(SERVICE_DATE),
            replay_sources=bundle,
            runs_root=self.root / "runs",
            run_id="phase1-test",
        )
        self.assertEqual(result.status, RunStatus.REVIEW_REQUIRED)
        self.assertEqual(result.exit_code, 2)
        self.assertFalse(result.data_release_created)
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "review_required")
        self.assertEqual(manifest["pipeline_mode"], "full")
        self.assertFalse(manifest["data_release_created"])
        self.assertRegex(manifest["configuration"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(manifest["configuration"]["software_version"], "0.1.0")
        self.assertEqual(
            manifest["configuration"]["stage2_approved_baseline"]["status"],
            "unavailable",
        )
        self.assertEqual(set(manifest["stages"]), {"collection", "stage1", "stage2"})
        self.assertEqual(manifest["stages"]["collection"]["status"], "complete")
        self.assertEqual(manifest["stages"]["stage1"]["status"], "review_required")
        self.assertEqual(manifest["stages"]["stage2"]["status"], "review_required")
        self.assertEqual(
            {path.name for path in result.run_directory.iterdir()},
            {"sources", "stage1", "stage2", "manifest.json", "pipeline.jsonl"},
        )
        records = [json.loads(line) for line in result.log_path.read_text().splitlines()]
        self.assertEqual(records[0]["event"], "pipeline.started")
        self.assertEqual(records[-1]["event"], "pipeline.completed")
        self.assertTrue((result.run_directory / "stage1/stations.json").is_file())
        self.assertTrue((result.run_directory / "stage1/reviews.json").is_file())
        self.assertTrue((result.run_directory / "stage1/changes.json").is_file())
        self.assertTrue((result.run_directory / "stage2/stations.json").is_file())
        self.assertTrue((result.run_directory / "stage2/exits.geojson").is_file())
        self.assertTrue((result.run_directory / "stage2/reviews.json").is_file())
        self.assertTrue((result.run_directory / "stage2/changes.json").is_file())
        dataset = json.loads((result.run_directory / "stage1/stations.json").read_text())
        self.assertEqual(dataset["stations"][0]["exits"][0]["destinations"], ["Test Landmark"])

    def test_existing_run_directory_is_never_reused(self):
        config = write_test_project(self.root / "project")
        bundle = write_replay_bundle(self.root / "bundle")
        arguments = dict(
            config=config,
            replay_sources=bundle,
            service_date=date.fromisoformat(SERVICE_DATE),
            runs_root=self.root / "runs",
            run_id="same-run",
        )
        run_pipeline(**arguments)
        with self.assertRaisesRegex(OutputExistsError, "Refusing to reuse"):
            run_pipeline(**arguments)

    def test_approved_unchanged_baselines_create_complete_release(self):
        config = write_test_project(self.root / "project")
        bundle = write_replay_bundle(self.root / "bundle")
        first = run_pipeline(
            config=config,
            replay_sources=bundle,
            service_date=date.fromisoformat(SERVICE_DATE),
            runs_root=self.root / "runs",
            run_id="initial",
        )
        result = run_pipeline(
            config=config,
            replay_sources=bundle,
            baseline=first.run_directory / "stage1/stations.json",
            stage2_baseline=first.run_directory / "stage2/stations.json",
            service_date=date.fromisoformat(SERVICE_DATE),
            runs_root=self.root / "runs",
            run_id="approved",
        )
        self.assertEqual(result.status, RunStatus.COMPLETE)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.data_release_created)
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["configuration"]["stage2_approved_baseline"]["status"],
            "available",
        )
        self.assertRegex(
            manifest["configuration"]["stage2_approved_baseline"]["sha256"],
            r"^[0-9a-f]{64}$",
        )

    def test_unknown_source_shape_fails_and_is_recorded(self):
        config = write_test_project(self.root / "project")
        bundle = write_replay_bundle(self.root / "bundle", unknown_entry_field=True)
        with self.assertRaisesRegex(ContractError, r"extra=\['new_upstream_field'\]"):
            run_pipeline(
                config=config,
                replay_sources=bundle,
                service_date=date.fromisoformat(SERVICE_DATE),
                runs_root=self.root / "runs",
                run_id="bad-source",
            )
        manifest = json.loads(
            (self.root / "runs/bad-source/manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["stages"]["stage1"]["status"], "failed")

    def test_failed_run_records_exception_and_cannot_be_finalized_again(self):
        run = RunContext.create(
            self.root,
            run_id="failed-run",
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        run.fail(RuntimeError("deliberate failure"),
                 finished_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC))
        manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure"], {
            "type": "RuntimeError", "message": "deliberate failure"})
        with self.assertRaises(RunLifecycleError):
            run.complete()

    def test_artifacts_cannot_escape_run_or_overwrite(self):
        run = RunContext.create(self.root, run_id="artifact-run")
        with self.assertRaisesRegex(ValueError, "stay within"):
            run.write_json_artifact("../outside.json", {})
        path = run.write_json_artifact("stage1/example.json", {"value": 1})
        self.assertTrue(path.is_file())
        with self.assertRaises(OutputExistsError):
            run.write_json_artifact("stage1/example.json", {"value": 2})


if __name__ == "__main__":
    unittest.main()
