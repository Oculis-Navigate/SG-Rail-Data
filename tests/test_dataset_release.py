import json
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

from scripts import dataset


class DatasetReleaseTests(unittest.TestCase):
    def test_version_is_a_date_and_numeric_revision(self):
        self.assertGreater(
            dataset.version("2026.11.01.10"), dataset.version("2026.11.01.2")
        )
        for v in ["../tag", "2026.02.31.1", "2026.10.06.0", "2026.10.06.01"]:
            with self.assertRaises(ValueError):
                dataset.version(v)

    def test_stage3_release_artifact_is_pinned_and_attributed(self):
        import hashlib
        release = dataset.load(dataset.GEOMETRY_RELEASE)
        raw = dataset.geometry_bytes(dataset.load(dataset.GEOMETRY_BASE), release)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), release['asset_sha256'])
        asset = json.loads(raw)
        self.assertIs(asset['metadata']['publication_allowed'], True)
        self.assertIn('OpenStreetMap contributors', asset['metadata']['attribution'])
        self.assertIn('odbl', asset['metadata']['license'])
        with self.assertRaisesRegex(ValueError, 'effective exact-artifact approval'):
            dataset.approved_geometry(Path('.'), release['service_date'], {'effective_states': {'approved': []}})
        with self.assertRaisesRegex(ValueError, 'inventory/service date'):
            dataset.approved_geometry(Path('.'), '2000-01-01', {'effective_states': {'approved': [release['decision_id']]}})

    def test_review_and_gate(self):
        with tempfile.TemporaryDirectory() as tmp, chdir(tmp):

            def commit():
                dataset.git("add", ".")
                dataset.git(
                    "-c",
                    "user.email=test@example.invalid",
                    "-c",
                    "user.name=tests",
                    "commit",
                    "-qm",
                    "test",
                )

            dataset.git("init", "-q")
            Path("config").mkdir()
            Path("audit").mkdir()
            data = {
                "stations": [
                    {"id": "admiralty", "exits": [{"id": "A", "coordinates": [1, 2]}]}
                ]
            }
            for p in dataset.RULES:
                dataset.write(Path(p), data)
            Path(dataset.LEDGER).write_bytes(b"")
            commit()
            base = dataset.git("rev-parse", "HEAD").decode().strip()
            data["stations"][0]["exits"][0]["coordinates"] = [2, 3]
            dataset.write(Path(dataset.BASELINES[1]), data)
            self.assertEqual(
                dataset.changes(base), ["Stage 2 stations/admiralty/A: coordinates"]
            )
            with self.assertRaisesRegex(ValueError, "one new reviewed"):
                dataset.gate(base, False)
            root = Path("changesets/2026.11.01.1")
            root.mkdir(parents=True)
            dataset.write(root / "findings.json", [])
            m = {
                "version": root.name,
                "base": base,
                "run_id": "test",
                "service_date": "2026-11-01",
                "previous": None,
                "findings_sha256": dataset.digest(root / "findings.json"),
            }
            key = dataset.changes(base)[0]
            r = {
                "summary": "Correct the mapped point.",
                "reviewer": "test reviewer",
                "evidence": "Survey in test evidence",
                "signoffs": [],
                "explanations": {key: "Coordinates corrected."},
            }
            dataset.write(root / "review.json", r)
            with self.assertRaisesRegex(ValueError, "new audit sign-off"):
                dataset.notes(root, m)
            event = {
                "operation": "approve",
                "event_id": "S-001",
                "decision_id": "D-001",
                "disposition": "approved",
                "note": "Test sign-off.",
            }
            Path(dataset.LEDGER).write_text(json.dumps(event) + "\n")
            r["signoffs"] = ["S-001"]
            dataset.write(root / "review.json", r)
            with self.assertRaisesRegex(ValueError, "cite the applicable"):
                dataset.notes(root, m)
            r["explanations"][key] += " S-001"
            dataset.write(root / "review.json", r)
            self.assertIn(
                "Stage 2 stations/admiralty/A: coordinates", dataset.notes(root, m)
            )
            dataset.write(root / "candidate.json", m)
            dataset.git("add", ".")
            with patch.object(dataset, "verify") as verify:
                dataset.gate(base, True)
                verify.assert_called_once_with(root.name)
            (root / "review.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "Stage review inputs"):
                dataset.gate(base, True)
            commit()
            newbase = dataset.git("rev-parse", "HEAD").decode().strip()
            (root / "review.json").write_text("{ }")
            with self.assertRaisesRegex(ValueError, "immutable"):
                dataset.gate(newbase, False)
