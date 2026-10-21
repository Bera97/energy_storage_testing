"""Offline tests for the read-only recorded-run validator."""

import csv
import json
from pathlib import Path
import tempfile
import unittest

from analysis.validate_recorded_run import FIELDS, validate_run


class ValidateRecordedRunTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

        self.rows = [
            [0, 1, 0.0, 1.3, -0.0001, -0.00013, 3201, False, False],
            [1, 1, 0.2, 1.2, -0.0001, -0.00012, 3201, False, False],
        ]

        self.document = {
            "schema_version": 1,
            "export_mode": "incremental_csv",
            "metadata": {"channel": 1},
            "outcome": {
                "state": "raised",
                "stop_reason": "exception",
                "error_type": "KeyboardInterrupt",
                "retained_in_memory_count": 2,
                "records_flushed": 2,
                "csv_writer_failed": False,
                "csv_close_error": None,
                "shutdown_verified": None,
                "shutdown_evidence": "unknown",
            },
        }

    def save(self):
        (self.directory / "metadata.json").write_text(
            json.dumps(self.document),
            encoding="utf-8",
        )
        with (self.directory / "measurements.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.writer(handle)
            writer.writerow(FIELDS)
            writer.writerows(self.rows)

    def test_historical_cancellation_is_valid_with_warnings(self):
        self.save()
        result = validate_run(self.directory)

        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["rows_read"], 2)
        self.assertTrue(result["warnings"])

    def test_validation_does_not_modify_files(self):
        self.save()
        before = {
            path.name: path.read_bytes()
            for path in self.directory.iterdir()
        }

        validate_run(self.directory)

        after = {
            path.name: path.read_bytes()
            for path in self.directory.iterdir()
        }
        self.assertEqual(before, after)

    def test_bad_index_is_rejected(self):
        self.rows[1][0] = 3
        self.save()
        self.assertFalse(validate_run(self.directory)["valid"])

    def test_repeated_timestamp_is_rejected(self):
        self.rows[1][2] = 0.0
        self.save()
        self.assertFalse(validate_run(self.directory)["valid"])

    def test_wrong_power_is_rejected(self):
        self.rows[1][5] = 1.0
        self.save()
        self.assertFalse(validate_run(self.directory)["valid"])

    def test_nonfinite_measurement_is_rejected(self):
        self.rows[1][3] = "nan"
        self.save()
        self.assertFalse(validate_run(self.directory)["valid"])

    def test_failed_flush_can_leave_extra_csv_row(self):
        outcome = self.document["outcome"]
        outcome["records_flushed"] = 1
        outcome["csv_writer_failed"] = True
        self.save()

        result = validate_run(self.directory)
        self.assertTrue(result["valid"], result["errors"])
        self.assertTrue(result["warnings"])

    def test_failed_run_with_complete_is_rejected(self):
        self.save()
        (self.directory / "COMPLETE").write_text(
            "unexpected marker\n", encoding="utf-8"
        )
        self.assertFalse(validate_run(self.directory)["valid"])

    def test_returned_completed_run_is_valid(self):
        self.document["record_count"] = 2
        self.document["outcome"] = {
            "state": "returned",
            "stop_reason": "completed",
            "requested_count": 2,
            "retained_count": 2,
            "records_flushed": 2,
            "shutdown_verified": True,
            "triggering_record_index": None,
            "triggering_reading": None,
        }
        self.save()
        (self.directory / "COMPLETE").write_text(
            "Run files finalized.\n", encoding="utf-8"
        )

        result = validate_run(self.directory)
        self.assertTrue(result["valid"], result["errors"])

    def test_shutdown_contradiction_is_rejected(self):
        outcome = self.document["outcome"]
        outcome["shutdown_report"] = {
            "attempted": True,
            "verified": True,
            "evidence": "smu_shutdown_returned",
            "error_type": None,
            "error_message": None,
        }
        self.save()

        self.assertFalse(validate_run(self.directory)["valid"])


if __name__ == "__main__":
    unittest.main()