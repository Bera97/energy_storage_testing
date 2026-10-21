"""Offline tests for read-only recorded-run analysis."""

import csv
import json
from pathlib import Path
import tempfile
import unittest

from analysis.analyze_recorded_run import analyze_run
from analysis.validate_recorded_run import FIELDS


class AnalyzeRecordedRunTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

        self.rows = [
            [0, 1, 10.0, 2.0, -0.5, -1.0, 0, False, False],
            [1, 1, 12.0, 2.0, -0.5, -1.0, 0, False, False],
        ]

        self.document = {
            "schema_version": 1,
            "export_mode": "incremental_csv",
            "metadata": {
                "channel": 1,
                "specimen_id": "synthetic-test",
            },
            "record_count": 2,
            "outcome": {
                "state": "returned",
                "stop_reason": "completed",
                "requested_count": 2,
                "retained_count": 2,
                "records_flushed": 2,
                "shutdown_verified": True,
                "triggering_record_index": None,
                "triggering_reading": None,
            },
        }

    def save(self):
        (self.directory / "metadata.json").write_text(
            json.dumps(self.document),
            encoding="utf-8",
        )

        with (self.directory / "measurements.csv").open(
            "w",
            encoding="utf-8",
            newline="",
        ) as handle:
            writer = csv.writer(handle)
            writer.writerow(FIELDS)
            writer.writerows(self.rows)

        marker = self.directory / "COMPLETE"

        if self.document["outcome"]["state"] == "returned":
            marker.write_text(
                "Run files finalized.\n",
                encoding="utf-8",
            )
        elif marker.exists():
            marker.unlink()

    def use_interrupted_outcome(self):
        self.document.pop("record_count", None)
        self.document["outcome"] = {
            "state": "raised",
            "stop_reason": "exception",
            "error_type": "KeyboardInterrupt",
            "retained_in_memory_count": 2,
            "records_flushed": 2,
            "csv_writer_failed": False,
            "csv_close_error": None,
            "shutdown_verified": None,
            "shutdown_evidence": "unknown",
        }

    def test_completed_run_is_analyzed(self):
        self.save()

        result = analyze_run(self.directory)
        summary = result["calculation"]["summary"]

        self.assertTrue(result["validation"]["valid"])
        self.assertEqual(
            result["acquisition_outcome"]["state"],
            "returned",
        )
        self.assertEqual(
            result["acquisition_outcome"]["stop_reason"],
            "completed",
        )
        self.assertEqual(summary["sample_count"], 2)
        self.assertAlmostEqual(summary["recorded_span_s"], 2)
        self.assertAlmostEqual(summary["signed_charge_c"], -1)
        self.assertAlmostEqual(summary["signed_energy_j"], -2)
        self.assertEqual(result["warnings"], [])

    def test_source_metadata_is_preserved(self):
        self.save()

        result = analyze_run(self.directory)

        self.assertEqual(
            result["source_metadata"],
            self.document,
        )
        self.assertEqual(
            result["acquisition_outcome"],
            self.document["outcome"],
        )

    def test_interrupted_run_keeps_outcome_and_warnings(self):
        self.use_interrupted_outcome()
        self.save()

        result = analyze_run(self.directory)

        self.assertTrue(result["validation"]["valid"])
        self.assertEqual(
            result["acquisition_outcome"]["state"],
            "raised",
        )
        self.assertEqual(
            result["acquisition_outcome"]["error_type"],
            "KeyboardInterrupt",
        )
        self.assertTrue(result["validation"]["warnings"])

        for warning in result["validation"]["warnings"]:
            self.assertIn(warning, result["warnings"])

        self.assertAlmostEqual(
            result["calculation"]["summary"]["signed_energy_j"],
            -2,
        )

    def test_writer_failure_keeps_physical_rows_and_warning(self):
        self.use_interrupted_outcome()
        self.document["outcome"]["records_flushed"] = 1
        self.document["outcome"]["csv_writer_failed"] = True
        self.save()

        result = analyze_run(self.directory)

        self.assertEqual(result["validation"]["rows_read"], 2)
        self.assertEqual(
            result["calculation"]["summary"]["sample_count"],
            2,
        )
        self.assertTrue(
            any(
                "CSV I/O failed" in warning
                for warning in result["warnings"]
            )
        )

    def test_invalid_recording_is_rejected(self):
        self.rows[1][5] = 99.0
        self.save()

        with self.assertRaisesRegex(
            ValueError,
            "Recorded-run validation failed",
        ):
            analyze_run(self.directory)

    def test_empty_recording_is_rejected(self):
        self.rows = []
        self.document["record_count"] = 0
        self.document["outcome"].update({
            "stop_reason": "cancelled",
            "retained_count": 0,
            "records_flushed": 0,
        })
        self.save()

        with self.assertRaisesRegex(
            ValueError,
            "no samples",
        ):
            analyze_run(self.directory)

    def test_single_sample_preserves_calculation_warning(self):
        self.rows = self.rows[:1]
        self.document["record_count"] = 1
        self.document["outcome"].update({
            "requested_count": 1,
            "retained_count": 1,
            "records_flushed": 1,
        })
        self.save()

        result = analyze_run(self.directory)

        self.assertTrue(result["validation"]["valid"])
        self.assertEqual(
            result["calculation"]["summary"]["interval_count"],
            0,
        )
        self.assertTrue(result["calculation"]["warnings"])

        for warning in result["calculation"]["warnings"]:
            self.assertIn(warning, result["warnings"])

    def test_flagged_rows_remain_in_calculation(self):
        self.rows[1][7] = True
        self.rows[1][8] = True
        self.save()

        result = analyze_run(self.directory)

        self.assertEqual(
            result["validation"]["compliance_flagged_rows"],
            1,
        )
        self.assertEqual(
            result["validation"]["fault_flagged_rows"],
            1,
        )
        self.assertEqual(
            result["calculation"]["excluded_intervals"],
            [],
        )
        self.assertAlmostEqual(
            result["calculation"]["summary"]["signed_energy_j"],
            -2,
        )
        self.assertTrue(result["warnings"])

    def test_analysis_does_not_change_or_create_files(self):
        self.save()
        before = {
            path.name: path.read_bytes()
            for path in self.directory.iterdir()
        }

        analyze_run(self.directory)

        after = {
            path.name: path.read_bytes()
            for path in self.directory.iterdir()
        }
        self.assertEqual(before, after)

    def test_missing_recording_is_rejected(self):
        with self.assertRaisesRegex(
            ValueError,
            "Recorded-run validation failed",
        ):
            analyze_run(self.directory / "missing")

    def test_result_is_strict_json_compatible(self):
        self.save()

        result = analyze_run(self.directory)
        encoded = json.dumps(result, allow_nan=False)

        self.assertEqual(json.loads(encoded), result)


if __name__ == "__main__":
    unittest.main()