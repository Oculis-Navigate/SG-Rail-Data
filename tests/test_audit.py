from copy import deepcopy
import contextlib
from datetime import date
import io
import json
from hashlib import sha256
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sg_rail_station_data.audit import (
    apply_decision,
    audit_status,
    content_hash,
    promote_coordinate_draft,
    propose_decision,
    record_decision_action,
    render_signoffs,
    validate_initial_ledger,
)
from sg_rail_station_data.cli import main
from sg_rail_station_data.contracts import ContractError
from sg_rail_station_data.pipeline import run_pipeline
from sg_rail_station_data.review_bundle import write_review_bundle
from sg_rail_station_data.review_server import ReviewStore
from tests.helpers import SERVICE_DATE, write_replay_bundle, write_test_project


ROOT = Path(__file__).resolve().parents[1]


class AuditFixture:
    def copy_package(self, root):
        shutil.copytree(ROOT / "audit", root / "audit")
        package = json.loads((ROOT / "audit/initial-package.json").read_text())
        for item in package["files"]:
            dest = root / item["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / item["path"], dest)


class InitialAuditTests(AuditFixture, unittest.TestCase):

    def test_current_audit_has_all_signoffs_and_covers_all_records(self):
        result = validate_initial_ledger(ROOT)
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["decision_count"], 29)
        self.assertEqual(result["intervention_count"], 354)
        self.assertEqual(result["approved_count"], 29)
        self.assertEqual(result["pending_decision_ids"], [])
        self.assertEqual(result["release_approval"], "not_granted")

    def test_changed_live_config_keeps_the_immutable_bootstrap_valid(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self.copy_package(root)
            path = root / "config/stage2-trusted-supplemental-baseline.geojson"
            data = json.loads(path.read_text())
            data["features"][0]["geometry"]["coordinates"][0] += 0.001
            path.write_text(json.dumps(data))
            self.assertEqual(validate_initial_ledger(root)["intervention_count"], 354)

    def test_changed_immutable_bootstrap_copy_fails(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self.copy_package(root)
            package = json.loads((root / "audit/initial-package.json").read_text())
            digest = package["files"][0]["sha256"]
            (root / "audit/bootstrap" / digest).write_bytes(b"tampered")
            with self.assertRaisesRegex(ContractError, "historical content changed"):
                validate_initial_ledger(root)

    def test_ledger_rejects_tampering_missing_signoffs_and_invalid_approval(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self.copy_package(root)
            path = root / "audit/decision-ledger.jsonl"
            original = [json.loads(line) for line in path.read_text().splitlines()]
            cases = []
            changed = deepcopy(original)
            changed[0]["note"] = "tampered"
            cases.append(changed)
            cases.append(original[:12] + original[13:])
            changed = deepcopy(original)
            changed[0]["unknown"] = True
            cases.append(changed)
            changed = deepcopy(original)
            changed[0]["sequence"] = True
            cases.append(changed)
            changed = deepcopy(original)
            changed[0]["authorization_refs"] = []
            changed[0]["event_hash"] = content_hash({k:v for k,v in changed[0].items() if k != "event_hash"})
            cases.append(changed)
            changed = deepcopy(original)
            changed[3]["approved_by"] = "project_owner"
            cases.append(changed)
            for index, rows in enumerate(cases):
                with self.subTest(index=index):
                    path.write_text("".join(json.dumps(row)+"\n" for row in rows))
                    with self.assertRaises(ContractError):
                        validate_initial_ledger(root)

    def test_missing_intervention_is_detected_before_ledger_hash_validation(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self.copy_package(root)
            path = root / "audit/initial-package.json"
            data = json.loads(path.read_text())
            data["decisions"].pop()
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ContractError, "interventions lack decision"):
                validate_initial_ledger(root)

    def test_cli_reports_valid_signed_off_inventory_without_release_approval(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["audit", "validate", "--json"])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["approved_count"], 29)
        self.assertEqual(result["release_approval"], "not_granted")

    def test_geometry_baseline_matches_its_exact_approval(self):
        baseline_path = ROOT / "config/stage3-approved-geometry-baseline.geojson"
        baseline = json.loads(baseline_path.read_text())
        decision = json.loads((ROOT / "audit/decisions/D-028.json").read_text())
        evidence = json.loads(
            (ROOT / "audit/evidence/2026-10-25-network-geometry-baseline-approval.json").read_text()
        )
        target = decision["targets"][0]
        self.assertEqual(target["path"], "config/stage3-approved-geometry-baseline.geojson")
        self.assertEqual(target["before"], baseline)
        self.assertEqual(target["after"], baseline)
        self.assertEqual(target["after_sha256"], content_hash(baseline))
        self.assertEqual(
            evidence["candidate"]["file_sha256"],
            sha256(baseline_path.read_bytes()).hexdigest(),
        )
        self.assertFalse(baseline["metadata"]["publication_allowed"])

    def test_stage2_baseline_matches_its_approval_and_display_order_migration(self):
        baseline_path = ROOT / "config/stage2-approved-baseline.json"
        baseline = json.loads(baseline_path.read_text())
        approval = json.loads((ROOT / "audit/decisions/D-027.json").read_text())
        migration = json.loads((ROOT / "audit/decisions/D-029.json").read_text())
        evidence = json.loads(
            (ROOT / "audit/evidence/2026-11-01-station-code-display-order-migration.json").read_text()
        )
        target = approval["targets"][0]
        self.assertEqual(target["path"], "config/stage2-approved-baseline.json")
        migrated = deepcopy(target["after"])

        def pointer(document, value):
            current = document
            for token in value[1:].split("/"):
                current = current[int(token)] if isinstance(current, list) else current[token]
            return current

        stage2_targets = [
            item for item in migration["targets"]
            if item["path"] == "config/stage2-approved-baseline.json"
        ]
        self.assertEqual(len(stage2_targets), 18)
        for item in stage2_targets:
            codes = pointer(migrated, item["pointer"])
            self.assertEqual(codes, item["before"])
            codes[:] = item["after"]
        self.assertEqual(migrated, baseline)
        self.assertEqual(evidence["migration"]["semantic_change"], "station code array order only")
        stage2_evidence = next(
            item for item in evidence["migration"]["files"]
            if item["path"] == "config/stage2-approved-baseline.json"
        )
        self.assertEqual(
            stage2_evidence["after_file_sha256"],
            sha256(baseline_path.read_bytes()).hexdigest(),
        )


class AuditLifecycleTests(AuditFixture, unittest.TestCase):
    def coordinate_draft_project(self, temp: str):
        root = Path(temp) / "project"
        config = write_test_project(root)
        shutil.copytree(ROOT / "audit", root / "audit")
        replay = write_replay_bundle(Path(temp) / "replay")
        run = run_pipeline(
            config=config,
            replay_sources=replay,
            service_date=date.fromisoformat(SERVICE_DATE),
            runs_root=Path(temp) / "runs",
            run_id="coordinate-review-run",
        ).run_directory
        bundle_path = root / "review-bundle.json"
        write_review_bundle(
            run_directory=run,
            output_path=bundle_path,
            config=config,
            approved_stage2=run / "stage2/stations.json",
        )
        drafts = root / "audit/drafts"
        drafts.mkdir(exist_ok=True)
        store = ReviewStore(bundle_path, drafts, basemaps=[])
        request = {
            "schema_version": 1,
            "bundle_sha256": store.state()["bundle_sha256"],
            "station_id": "example",
            "exit_id": "MRT:A",
            "proposed_coordinates": [103.8001, 1.3001],
            "change_category": "source_correction",
            "rationale": "The retained operator map places the exit beside the doorway.",
            "evidence": [{
                "url": "https://example.test/operator-map",
                "title": "Operator map",
                "note": "Exit A is beside the marked doorway.",
            }],
            "alternatives_rejected": "The old point is in the road.",
        }
        saved = store.save(request)["draft"]
        return (
            root,
            config,
            replay,
            run,
            bundle_path,
            drafts / f"{saved['decision_id']}.json",
        )

    def new_decision(self, root: Path, decision_id: str, *, revalidation: bool = False) -> Path:
        evidence = root / "audit/evidence/new-source.txt"
        evidence.write_text("Retained source evidence.\n")
        target_path = "config/stage2-trusted-supplemental-baseline.geojson"
        config = json.loads((root / target_path).read_text())
        before = config["features"][0]["geometry"]["coordinates"]
        after = before if revalidation else [before[0] + 0.001, before[1] + 0.001]
        decision = {
            "schema_version": 2,
            "kind": "audit_decision",
            "id": decision_id,
            "title": "Test station: Exit A coordinate",
            "change_type": "revision_revalidation" if revalidation else "source_correction",
            "reason": "The retained source places Exit A at the new coordinate.",
            "targets": [{
                "identity": config["features"][0]["id"],
                "path": target_path,
                "pointer": "/features/0/geometry/coordinates",
                "before": before,
                "after": after,
                "before_sha256": content_hash(before),
                "after_sha256": content_hash(after),
            }],
            "evidence": [{
                "description": "Retained source snapshot.",
                "path": "audit/evidence/new-source.txt",
                "sha256": sha256(evidence.read_bytes()).hexdigest(),
            }],
            "source_revisions": ["SgWiki test station revision 2"],
            "limitations": [],
        }
        draft = root / f"{decision_id}.json"
        draft.write_text(json.dumps(decision))
        return draft

    def apply_decision_target(self, root: Path, decision_path: Path) -> None:
        decision = json.loads(decision_path.read_text())
        target = decision["targets"][0]
        config_path = root / target["path"]
        config = json.loads(config_path.read_text())
        config["features"][0]["geometry"]["coordinates"] = target["after"]
        config_path.write_text(json.dumps(config))

    def action(self, root: Path, operation: str, decision_id: str, *, related=None):
        return record_decision_action(
            root,
            operation,
            decision_id,
            recorded_by="maintainer@example.test",
            approved_by="reviewer@example.test",
            authorization_basis="direct_human_signoff",
            authorization_refs=["PR-42"],
            note=f"The reviewer chose to {operation} this exact decision.",
            related_decision_id=related,
        )

    def test_approved_initial_decision_can_be_revalidated(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self.copy_package(root)
            revalidated = self.action(root, "revalidate", "D-004")
            self.assertEqual(revalidated["event"]["event_id"], "S-038")
            result = audit_status(root)
            self.assertEqual(result["event_count"], 38)
            self.assertEqual(result["pending_decision_ids"], [])
            self.assertIn("D-004", result["effective_states"]["approved"])

    def test_propose_approve_and_retire_preserve_exact_decision_hash(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self.copy_package(root)
            draft = self.new_decision(root, "D-100")
            result = propose_decision(
                root, draft, recorded_by="agent@example.test", note="Prepare this change for human review."
            )
            self.assertEqual(result["event"]["disposition"], "pending_review")
            canonical = root / "audit/decisions/D-100.json"
            expected_hash = content_hash(json.loads(canonical.read_text()))
            self.assertEqual(result["event"]["decision_sha256"], expected_hash)
            self.action(root, "approve", "D-100")
            self.action(root, "retire", "D-100")
            status = audit_status(root)
            self.assertIn("D-100", status["effective_states"]["retired"])

    def test_reject_and_replace_have_distinct_valid_transitions(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self.copy_package(root)
            propose_decision(root, self.new_decision(root, "D-100"), recorded_by="agent", note="Review it.")
            self.action(root, "reject", "D-100")
            self.assertIn("D-100", audit_status(root)["effective_states"]["rejected"])

            propose_decision(root, self.new_decision(root, "D-101"), recorded_by="agent", note="Review it.")
            self.action(root, "approve", "D-101")
            self.apply_decision_target(root, root / "audit/decisions/D-101.json")
            propose_decision(root, self.new_decision(root, "D-102"), recorded_by="agent", note="Review it.")
            self.action(root, "replace", "D-101", related="D-102")
            status = audit_status(root)
            self.assertIn("D-101", status["effective_states"]["replaced"])
            self.assertIn("D-102", status["effective_states"]["approved"])
            self.assertNotIn("D-102", status["pending_decision_ids"])
            rendered = render_signoffs(root)
            pages = list(Path(rendered["output_directory"]).glob("*.md"))
            self.assertTrue(any("replacement `D-102` approved atomically" in page.read_text() for page in pages))

    def test_replace_rejects_target_discontinuity_and_direct_overlap(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self.copy_package(root)
            propose_decision(root, self.new_decision(root, "D-100"), recorded_by="agent", note="Review it.")
            self.action(root, "approve", "D-100")
            self.apply_decision_target(root, root / "audit/decisions/D-100.json")
            replacement = self.new_decision(root, "D-101")
            replacement_value = json.loads(replacement.read_text())
            replacement_value["targets"][0]["identity"] += "-wrong"
            replacement.write_text(json.dumps(replacement_value))
            propose_decision(root, replacement, recorded_by="agent", note="Review it.")
            with self.assertRaisesRegex(ContractError, "overlap.*use replace"):
                self.action(root, "approve", "D-101")
            with self.assertRaisesRegex(ContractError, "before value must equal|same strict targets"):
                self.action(root, "replace", "D-100", related="D-101")

    def test_invalid_transition_and_missing_authorization_do_not_change_ledger(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self.copy_package(root)
            ledger = root / "audit/decision-ledger.jsonl"
            original = ledger.read_bytes()
            with self.assertRaisesRegex(ContractError, "invalid approve transition"):
                self.action(root, "approve", "D-001")
            self.assertEqual(ledger.read_bytes(), original)
            with self.assertRaisesRegex(ContractError, "must not be empty"):
                record_decision_action(
                    root, "revalidate", "D-004", recorded_by="agent", approved_by="reviewer",
                    authorization_basis="direct_human_signoff", authorization_refs=[], note="Approve it.",
                )
            self.assertEqual(ledger.read_bytes(), original)

    def test_changed_decision_or_evidence_breaks_later_validation(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self.copy_package(root)
            propose_decision(root, self.new_decision(root, "D-100"), recorded_by="agent", note="Review it.")
            path = root / "audit/decisions/D-100.json"
            value = json.loads(path.read_text())
            value["reason"] = "Changed after the event."
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ContractError, "decision hash mismatch"):
                audit_status(root)

    def test_coordinate_draft_promotes_and_applies_complete_reviewed_override(self):
        with TemporaryDirectory() as temp:
            root, config, replay, run, bundle_path, draft_path = self.coordinate_draft_project(temp)
            retained = root / "operator-map.txt"
            retained.write_text("retained source\n")
            result = promote_coordinate_draft(
                root,
                draft_path,
                "D-100",
                bundle_file=bundle_path,
                stage1_file=run / "stage1/stations.json",
                recorded_by="agent",
                note="Prepared for human review.",
                evidence_files=[retained],
            )
            self.assertEqual(result["event"]["disposition"], "pending_review")
            decision = json.loads((root / "audit/decisions/D-100.json").read_text())
            self.assertEqual(decision["schema_version"], 2)
            self.assertEqual(
                {(target["path"], target["pointer"]) for target in decision["targets"]},
                {
                    ("config/stage2-coordinate-ledger.json", ""),
                    ("config/stage2-reviewed-coordinates.geojson", ""),
                },
            )
            for target in decision["targets"]:
                self.assertEqual(target["before_sha256"], content_hash(target["before"]))
                self.assertEqual(target["after_sha256"], content_hash(target["after"]))
            self.assertEqual(len(decision["evidence"]), 4)
            for evidence in decision["evidence"]:
                evidence_path = root / evidence["path"]
                self.assertTrue(evidence_path.is_file())
                self.assertEqual(sha256(evidence_path.read_bytes()).hexdigest(), evidence["sha256"])
            self.assertEqual(audit_status(root)["pending_decision_ids"][-1], "D-100")
            original_ledger = (root / "config/stage2-coordinate-ledger.json").read_bytes()
            with self.assertRaisesRegex(ContractError, "must be approved"):
                apply_decision(root, "D-100", recorded_by="agent", note="Too early.")
            self.assertEqual(
                (root / "config/stage2-coordinate-ledger.json").read_bytes(),
                original_ledger,
            )
            self.action(root, "approve", "D-100")
            applied = apply_decision(
                root, "D-100", recorded_by="maintainer", note="Applied reviewed documents."
            )
            self.assertEqual(applied["event"]["operation"], "apply")
            self.assertEqual(applied["event"]["disposition"], "approved")
            self.assertIn("D-100", audit_status(root)["effective_states"]["approved"])
            reviewed = json.loads((root / "config/stage2-reviewed-coordinates.geojson").read_text())
            ledger = json.loads((root / "config/stage2-coordinate-ledger.json").read_text())
            self.assertEqual(ledger["revision"], 1)
            self.assertEqual(reviewed["metadata"]["revision"], 1)
            self.assertEqual(reviewed["metadata"]["migrated_from"]["release_revision"], 0)
            self.assertEqual(reviewed["features"][0]["geometry"]["coordinates"], [103.8001, 1.3001])
            self.assertEqual(ledger["entries"][0]["from"]["source"], "audit_review_draft")
            self.assertEqual(ledger["entries"][0]["from"]["decision_id"], "D-100")
            self.assertNotIn("candidate_index", ledger["entries"][0]["from"])

            rerun = run_pipeline(
                config=config,
                replay_sources=replay,
                service_date=date.fromisoformat(SERVICE_DATE),
                runs_root=Path(temp) / "reruns",
                run_id="after-apply",
                stage2_baseline=run / "stage2/stations.json",
            )
            output = json.loads((rerun.run_directory / "stage2/stations.json").read_text())
            output_exit = output["stations"][0]["exits"][0]
            self.assertEqual(output_exit["coordinates"], [103.8001, 1.3001])
            self.assertEqual(output_exit["coordinate_source"]["kind"], "human_reviewed")

    def test_apply_rolls_back_both_documents_when_ledger_append_fails(self):
        with TemporaryDirectory() as temp:
            root, _, _, run, bundle_path, draft_path = self.coordinate_draft_project(temp)
            promote_coordinate_draft(
                root, draft_path, "D-100", bundle_file=bundle_path,
                stage1_file=run / "stage1/stations.json", recorded_by="agent", note="Review."
            )
            self.action(root, "approve", "D-100")
            paths = [
                root / "config/stage2-coordinate-ledger.json",
                root / "config/stage2-reviewed-coordinates.geojson",
            ]
            before = [path.read_bytes() for path in paths]
            with patch("sg_rail_station_data.audit._append_event", side_effect=RuntimeError("stop")):
                with self.assertRaisesRegex(RuntimeError, "stop"):
                    apply_decision(root, "D-100", recorded_by="agent", note="Apply.")
            self.assertEqual([path.read_bytes() for path in paths], before)
            self.assertIn("D-100", audit_status(root)["effective_states"]["approved"])

    def test_apply_rejects_rejected_decision_without_writing_config(self):
        with TemporaryDirectory() as temp:
            root, _, _, run, bundle_path, draft_path = self.coordinate_draft_project(temp)
            promote_coordinate_draft(
                root, draft_path, "D-100", bundle_file=bundle_path,
                stage1_file=run / "stage1/stations.json", recorded_by="agent", note="Review."
            )
            self.action(root, "reject", "D-100")
            path = root / "config/stage2-coordinate-ledger.json"
            before = path.read_bytes()
            with self.assertRaisesRegex(ContractError, "must be approved, got rejected"):
                apply_decision(root, "D-100", recorded_by="agent", note="Must fail.")
            self.assertEqual(path.read_bytes(), before)

    def test_render_builds_dated_markdown_from_ledger(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self.copy_package(root)
            result = render_signoffs(root)
            self.assertEqual(result["event_count"], 37)
            index = (root / "audit/signoffs/README.md").read_text()
            page = (root / "audit/signoffs/2026-10-05.md").read_text()
            self.assertIn("generated from `audit/decision-ledger.jsonl`", index)
            self.assertIn("## Changes", page)
            self.assertIn("## Evidence", page)
            self.assertIn("## Human decisions", page)
            self.assertIn("## Validation", page)
            self.assertIn("D-004", page)
