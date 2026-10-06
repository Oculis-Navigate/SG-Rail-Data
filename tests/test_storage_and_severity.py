import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sg_rail_station_data.severity import (
    DEFAULT_SEVERITY_POLICY,
    PipelineStatus,
    Severity,
)
from sg_rail_station_data.storage import OutputExistsError, StorageError, atomic_write_json
from sg_rail_station_data.structured_logging import StructuredLogger


class StorageAndSeverityTests(unittest.TestCase):
    def test_atomic_json_is_stable_and_refuses_overwrite(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "result.json"
            atomic_write_json(path, {"b": 2, "a": 1})
            self.assertEqual(path.read_text(), '{\n  "a": 1,\n  "b": 2\n}\n')
            with self.assertRaises(OutputExistsError):
                atomic_write_json(path, {"a": 3})

    def test_json_rejects_nonfinite_numbers(self):
        with TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(StorageError, "valid JSON"):
                atomic_write_json(Path(temporary) / "bad.json", {"value": float("nan")})

    def test_structured_log_is_jsonl_and_refuses_reserved_fields(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.jsonl"
            with StructuredLogger(path, context={"run_id": "r1"}) as logger:
                record = logger.warning("review.required", "Review this", count=2)
                with self.assertRaisesRegex(ValueError, "reserved"):
                    logger.info("bad", "bad", timestamp="fake")
                with self.assertRaisesRegex(ValueError, "contextual"):
                    logger.info("bad", "bad", run_id="replacement")
            self.assertEqual(json.loads(path.read_text()), record)
            self.assertEqual(record["level"], "WARNING")

    def test_severity_policy_uses_zero_two_one_exit_codes(self):
        policy = DEFAULT_SEVERITY_POLICY
        self.assertEqual(policy.status_for(None), PipelineStatus.COMPLETE)
        self.assertEqual(policy.exit_code_for(Severity.INFO), 0)
        self.assertEqual(policy.exit_code_for(Severity.WARNING), 2)
        self.assertEqual(policy.exit_code_for(Severity.ERROR), 1)
        self.assertEqual(policy.exit_code_for(Severity.CRITICAL), 1)
        self.assertFalse(policy.blocks_release(Severity.WARNING))
        self.assertTrue(policy.blocks_release(Severity.ERROR))
        self.assertTrue(policy.aborts_run(Severity.CRITICAL))


if __name__ == "__main__":
    unittest.main()
