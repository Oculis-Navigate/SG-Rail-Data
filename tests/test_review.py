from datetime import date
import json
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory
import unittest

from mrt_wayfinding.contracts import ContractError
from mrt_wayfinding.pipeline import run_pipeline
from mrt_wayfinding.review_bundle import (
    FROZEN_COORDINATE_EVIDENCE,
    FROZEN_COORDINATE_EVIDENCE_SHA256,
    _frozen_coordinate_candidates,
    build_review_bundle,
    validate_review_bundle,
    write_review_bundle,
)
from mrt_wayfinding.review_server import (
    ReviewServerError,
    ReviewStore,
    StaleReviewState,
    default_basemaps,
)
from tests.helpers import SERVICE_DATE, write_replay_bundle, write_test_project


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FROZEN_EVIDENCE_PATH = PROJECT_ROOT / FROZEN_COORDINATE_EVIDENCE


def install_frozen_evidence(repo_root: Path) -> Path:
    target = repo_root / FROZEN_COORDINATE_EVIDENCE
    target.parent.mkdir(parents=True, exist_ok=True)
    copyfile(FROZEN_EVIDENCE_PATH, target)
    return target


class ReviewBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = write_test_project(self.root / "project")
        install_frozen_evidence(self.config.paths.repo_root)
        replay = write_replay_bundle(self.root / "replay")
        self.run = run_pipeline(
            config=self.config,
            replay_sources=replay,
            service_date=date.fromisoformat(SERVICE_DATE),
            runs_root=self.root / "runs",
            run_id="review-run",
        ).run_directory

    def tearDown(self):
        self.temporary.cleanup()

    def test_candidate_run_is_not_approved_without_explicit_baseline(self):
        bundle = build_review_bundle(run_directory=self.run, config=self.config)
        self.assertEqual(bundle["summary"]["exit_count"], 1)
        self.assertEqual(bundle["summary"]["approved_coordinate_count"], 0)
        exit_row = bundle["exits"][0]
        self.assertIsNone(exit_row["approved_coordinate"])
        self.assertEqual(exit_row["current_coordinate"]["source"], "kartographer")
        self.assertEqual(exit_row["candidates"][0]["source"], "kartographer")
        self.assertTrue(exit_row["candidates"][0]["selected_in_candidate_run"])
        validate_review_bundle(bundle)

    def test_explicit_baseline_is_shown_as_approved(self):
        baseline = self.run / "stage2/stations.json"
        bundle = build_review_bundle(
            run_directory=self.run,
            config=self.config,
            approved_stage2=baseline,
        )
        self.assertEqual(bundle["approved_baseline"]["status"], "available")
        self.assertEqual(bundle["summary"]["approved_coordinate_count"], 1)
        self.assertEqual(
            bundle["exits"][0]["approved_coordinate"]["coordinates"],
            [103.8, 1.3],
        )

    def test_unknown_bundle_field_fails_loudly(self):
        bundle = build_review_bundle(run_directory=self.run, config=self.config)
        bundle["new_field"] = True
        with self.assertRaisesRegex(ContractError, r"extra=\['new_field'\]"):
            validate_review_bundle(bundle)

    def test_frozen_evidence_is_untrusted_and_skips_retired_identities(self):
        stage1 = json.loads((PROJECT_ROOT / "config/stage1-approved-baseline.json").read_text())
        identities = {
            (station["id"], exit_row["id"])
            for station in stage1["stations"]
            for exit_row in station["exits"]
        }
        evidence = json.loads(FROZEN_EVIDENCE_PATH.read_text())
        candidates, summary = _frozen_coordinate_candidates(evidence, identities)
        self.assertEqual(summary["feature_count"], 872)
        self.assertEqual(summary["source_identity_count"], 613)
        self.assertEqual(summary["matched_identity_count"], 596)
        self.assertEqual(summary["matched_feature_count"], 848)
        self.assertEqual(summary["skipped_identity_count"], 17)
        self.assertEqual(summary["skipped_feature_count"], 24)
        self.assertEqual(summary["deduplicated_feature_count"], 4)
        self.assertEqual(summary["imported_candidate_count"], 844)
        self.assertEqual(
            {row["source"] for rows in candidates.values() for row in rows},
            {"kartographer", "onemap", "osm", "lta", "manual"},
        )
        self.assertTrue(all(
            not row["selected_in_candidate_run"]
            for rows in candidates.values()
            for row in rows
        ))

    def test_frozen_evidence_unknown_format_fails_loudly(self):
        evidence = json.loads(FROZEN_EVIDENCE_PATH.read_text())
        evidence["features"][0]["properties"]["new_upstream_field"] = True
        with self.assertRaisesRegex(ContractError, "unexpected fields"):
            _frozen_coordinate_candidates(evidence, {("admiralty", "MRT:A")})

    def test_frozen_evidence_hash_drift_fails_loudly(self):
        path = self.config.paths.repo_root / FROZEN_COORDINATE_EVIDENCE
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaisesRegex(ContractError, "frozen artifact SHA-256 changed"):
            build_review_bundle(run_directory=self.run, config=self.config)

    def test_unavailable_baseline_cannot_claim_approved_coordinates(self):
        bundle = build_review_bundle(run_directory=self.run, config=self.config)
        bundle["exits"][0]["approved_coordinate"] = {
            "coordinates": [103.8, 1.3],
            "source": "kartographer",
        }
        bundle["summary"]["approved_coordinate_count"] = 1
        bundle["summary"]["unapproved_coordinate_count"] = 0
        with self.assertRaisesRegex(ContractError, "unavailable baseline"):
            validate_review_bundle(bundle)

    def test_frozen_evidence_digest_is_pinned(self):
        from hashlib import sha256

        self.assertEqual(sha256(FROZEN_EVIDENCE_PATH.read_bytes()).hexdigest(), FROZEN_COORDINATE_EVIDENCE_SHA256)


class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = write_test_project(self.root / "project")
        install_frozen_evidence(self.config.paths.repo_root)
        replay = write_replay_bundle(self.root / "replay")
        self.run = run_pipeline(
            config=self.config,
            replay_sources=replay,
            service_date=date.fromisoformat(SERVICE_DATE),
            runs_root=self.root / "runs",
            run_id="review-run",
        ).run_directory
        self.bundle = self.root / "review/bundle.json"
        self.bundle.parent.mkdir()
        write_review_bundle(
            run_directory=self.run,
            output_path=self.bundle,
            config=self.config,
        )
        self.drafts = self.root / "review/drafts"
        self.drafts.mkdir()
        self.store = ReviewStore(self.bundle, self.drafts, basemaps=[])

    def tearDown(self):
        self.temporary.cleanup()

    def request(self, state_hash: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "bundle_sha256": state_hash,
            "station_id": "example",
            "exit_id": "MRT:A",
            "proposed_coordinates": [103.8001, 1.3001],
            "change_category": "source_correction",
            "rationale": "The newer source places the entrance beside the building door.",
            "evidence": [{
                "url": "https://example.com/evidence",
                "title": "Operator notice",
                "note": "The notice labels Exit A beside the door.",
            }],
            "alternatives_rejected": "The older marker points to the road centre.",
        }

    def test_save_creates_draft_only_and_advances_state_hash(self):
        state = self.store.state()
        result = self.store.save(self.request(state["bundle_sha256"]))
        self.assertEqual(result["status"], "created")
        self.assertNotEqual(result["bundle_sha256"], state["bundle_sha256"])
        self.assertEqual(len(list(self.drafts.glob("DRAFT-*.json"))), 1)
        self.assertEqual(json.loads(self.bundle.read_text())["summary"]["exit_count"], 1)
        self.assertEqual(self.store.state()["drafts"][0]["status"], "draft")

    def test_stale_tab_is_rejected(self):
        state_hash = self.store.state()["bundle_sha256"]
        self.store.save(self.request(state_hash))
        with self.assertRaisesRegex(StaleReviewState, "Reload"):
            self.store.save(self.request(state_hash))

    def test_missing_rationale_or_evidence_is_rejected(self):
        request = self.request(self.store.state()["bundle_sha256"])
        request["rationale"] = ""
        with self.assertRaises(ContractError):
            self.store.save(request)
        request = self.request(self.store.state()["bundle_sha256"])
        request["evidence"] = []
        with self.assertRaisesRegex(ReviewServerError, "nonempty"):
            self.store.save(request)

    def test_unknown_request_field_is_rejected(self):
        request = self.request(self.store.state()["bundle_sha256"])
        request["approved"] = True
        with self.assertRaisesRegex(ContractError, r"extra=\['approved'\]"):
            self.store.save(request)

    def test_saved_draft_filename_and_identity_are_revalidated(self):
        result = self.store.save(self.request(self.store.state()["bundle_sha256"]))
        original = self.drafts / f"{result['draft']['decision_id']}.json"
        renamed_id = f"DRAFT-{'0' * 32}"
        renamed = self.drafts / f"{renamed_id}.json"
        original.rename(renamed)
        with self.assertRaisesRegex(ReviewServerError, "filename does not match"):
            self.store.state()

        renamed.rename(original)
        value = json.loads(original.read_text())
        value["station_id"] = "missing-station"
        original.write_text(json.dumps(value))
        with self.assertRaisesRegex(ReviewServerError, "unknown exit identity"):
            self.store.state()

    def test_saved_draft_timestamp_is_revalidated(self):
        result = self.store.save(self.request(self.store.state()["bundle_sha256"]))
        path = self.drafts / f"{result['draft']['decision_id']}.json"
        value = json.loads(path.read_text())
        value["created_at"] = "not-a-date"
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ReviewServerError, "expected ISO 8601"):
            self.store.state()

    def test_default_basemap_is_neutral_and_has_full_colour_fallback(self):
        basemaps = default_basemaps()
        self.assertEqual([row["id"] for row in basemaps], ["neutral-osm", "osm"])
        self.assertTrue(basemaps[0]["is_default"])
        self.assertTrue(basemaps[0]["neutral_style"])
        self.assertFalse(basemaps[1]["is_default"])


if __name__ == "__main__":
    unittest.main()
