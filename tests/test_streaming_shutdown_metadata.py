"""Offline tests for shutdown evidence in streaming failure metadata.

No VISA connection or hardware required.
"""

import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from drivers.shutdown_reporting import (
    attempt_shutdown_with_report,
)
from acquisition.spot_acquisition import SMUReading
from recording.streaming_run import StreamingRun


class FakeSMU:
    def __init__(self, shutdown_error=None):
        self.shutdown_calls = 0
        self.shutdown_error = shutdown_error

    def shutdown(self):
        self.shutdown_calls += 1

        if self.shutdown_error is not None:
            raise self.shutdown_error


class StreamingShutdownMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

        self.run = StreamingRun(
            self.base,
            "shutdown_metadata",
            metadata={"instrument": "fake"},
        )
        self.addCleanup(self.run.close)

        self.record = SMUReading(
            channel=1,
            voltage_v=1.303,
            current_a=-100e-6,
            timestamp_s=0.0,
            status=3201,
        )

        self.run.writer.write_batch([self.record])

    def document(self):
        return json.loads(
            (self.run.directory / "metadata.json").read_text(
                encoding="utf-8"
            )
        )

    def cancellation(self):
        error = KeyboardInterrupt("operator cancellation")
        error.partial_readings = (self.record,)
        return error

    def assert_failure_export(self, error):
        self.run.fail(error)
        outcome = self.document()["outcome"]

        self.assertEqual(outcome["state"], "raised")
        self.assertEqual(outcome["stop_reason"], "exception")
        self.assertEqual(outcome["error_type"], "KeyboardInterrupt")
        self.assertEqual(outcome["records_flushed"], 1)
        self.assertEqual(outcome["retained_in_memory_count"], 1)
        self.assertTrue(self.run.writer.closed)
        self.assertFalse(
            (self.run.directory / "COMPLETE").exists()
        )

        with (self.run.directory / "measurements.csv").open(
            encoding="utf-8",
            newline="",
        ) as handle:
            rows = list(csv.DictReader(handle))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["record_index"], "0")
        return outcome

    def test_verified_cleanup_is_saved_without_another_shutdown(self):
        error = self.cancellation()
        smu = FakeSMU()

        attempt_shutdown_with_report(smu, error)
        outcome = self.assert_failure_export(error)

        self.assertIs(outcome["shutdown_verified"], True)
        self.assertEqual(
            outcome["shutdown_evidence"],
            "smu_shutdown_returned",
        )
        self.assertEqual(
            outcome["shutdown_report"],
            error.shutdown_report,
        )
        self.assertIsNone(outcome["shutdown_report_error"])
        self.assertEqual(smu.shutdown_calls, 1)

    def test_failed_cleanup_is_saved_without_masking_original(self):
        error = self.cancellation()
        smu = FakeSMU(
            RuntimeError("simulated shutdown verification failure")
        )

        attempt_shutdown_with_report(smu, error)
        original_notes = list(error.__notes__)

        outcome = self.assert_failure_export(error)

        self.assertIs(outcome["shutdown_verified"], False)
        self.assertEqual(
            outcome["shutdown_evidence"],
            "smu_shutdown_raised",
        )
        self.assertEqual(
            outcome["shutdown_report"]["error_type"],
            "RuntimeError",
        )
        self.assertEqual(outcome["error_notes"], original_notes)
        self.assertEqual(
            str(error),
            "operator cancellation",
        )
        self.assertEqual(smu.shutdown_calls, 1)

    def test_missing_report_remains_unknown(self):
        error = self.cancellation()
        outcome = self.assert_failure_export(error)

        self.assertIsNone(outcome["shutdown_verified"])
        self.assertEqual(
            outcome["shutdown_evidence"],
            "unknown",
        )
        self.assertIsNone(outcome["shutdown_report"])
        self.assertIsNone(outcome["shutdown_report_error"])

    def test_interrupted_cleanup_remains_unknown(self):
        error = self.cancellation()
        smu = FakeSMU(
            KeyboardInterrupt("second interrupt during cleanup")
        )

        with self.assertRaises(KeyboardInterrupt):
            attempt_shutdown_with_report(smu, error)

        outcome = self.assert_failure_export(error)

        self.assertIsNone(outcome["shutdown_verified"])
        self.assertEqual(
            outcome["shutdown_evidence"],
            "shutdown_attempt_started",
        )
        self.assertEqual(
            outcome["shutdown_report"],
            error.shutdown_report,
        )
        self.assertIsNone(outcome["shutdown_report_error"])

    def test_malformed_report_does_not_prevent_failure_export(self):
        error = self.cancellation()
        error.shutdown_report = "not a report dictionary"

        outcome = self.assert_failure_export(error)

        self.assertIsNone(outcome["shutdown_verified"])
        self.assertIsNone(outcome["shutdown_report"])
        self.assertIsNotNone(outcome["shutdown_report_error"])

    def test_integer_is_not_accepted_as_verified_boolean(self):
        error = self.cancellation()
        attempt_shutdown_with_report(FakeSMU(), error)
        error.shutdown_report["verified"] = 1

        outcome = self.assert_failure_export(error)

        self.assertIsNone(outcome["shutdown_verified"])
        self.assertEqual(
            outcome["shutdown_evidence"],
            "unknown",
        )
        self.assertIsNotNone(outcome["shutdown_report_error"])

    def test_non_json_report_does_not_prevent_failure_export(self):
        error = self.cancellation()
        attempt_shutdown_with_report(FakeSMU(), error)
        error.shutdown_report["unexpected_object"] = object()

        outcome = self.assert_failure_export(error)

        self.assertIsNone(outcome["shutdown_verified"])
        self.assertIsNone(outcome["shutdown_report"])
        self.assertIsNotNone(outcome["shutdown_report_error"])

    def test_metadata_save_failure_preserves_original_error(self):
        error = self.cancellation()
        attempt_shutdown_with_report(FakeSMU(), error)

        original_report = dict(error.shutdown_report)

        with patch.object(
            self.run,
            "_replace_metadata",
            side_effect=OSError("simulated metadata disk failure"),
        ):
            with self.assertRaises(OSError):
                self.run.fail(error)

        self.assertEqual(
            str(error),
            "operator cancellation",
        )
        self.assertEqual(
            error.shutdown_report,
            original_report,
        )
        self.assertEqual(
            error.partial_readings,
            (self.record,),
        )
        self.assertFalse(
            (self.run.directory / "COMPLETE").exists()
        )


if __name__ == "__main__":
    unittest.main()