"""Offline integration tests for acquisition-result export."""

import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from drivers.monitored_acquisition import MonitoredResult
from drivers.monitored_run_data import (
    save_monitored_failure,
    save_monitored_result,
)
from drivers.spot_acquisition import SMUReading


def reading(timestamp, voltage, status=3201):
    return SMUReading(
        channel=1,
        voltage_v=voltage,
        current_a=-100e-6,
        timestamp_s=timestamp,
        status=status,
    )


class MonitoredRunDataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def document(self, directory):
        return json.loads(
            (directory / "metadata.json").read_text(
                encoding="utf-8"
            )
        )

    def test_returned_cutoff_result_is_exported(self):
        first = reading(0.0, 1.30)
        trigger = reading(0.2, 1.00)

        result = MonitoredResult(
            readings=(first, trigger),
            stop_reason="voltage_min",
            triggering_reading=trigger,
            decision_elapsed_s=0.28,
            shutdown_elapsed_s=0.36,
            requested_count=150,
        )

        directory = save_monitored_result(
            self.base,
            "cutoff",
            result,
            metadata={"cutoff_v": 1.10},
        )

        document = self.document(directory)
        outcome = document["outcome"]

        self.assertEqual(outcome["acquisition_exit"], "returned")
        self.assertEqual(outcome["stop_reason"], "voltage_min")
        self.assertEqual(outcome["triggering_record_index"], 1)
        self.assertEqual(
            outcome["triggering_reading"]["voltage_v"],
            1.00,
        )
        self.assertEqual(outcome["requested_count"], 150)
        self.assertEqual(outcome["retained_count"], 2)
        self.assertTrue(outcome["shutdown_verified"])
        self.assertTrue((directory / "COMPLETE").exists())

        with (directory / "measurements.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            rows = list(csv.DictReader(handle))

        self.assertEqual(len(rows), 2)
        self.assertEqual(float(rows[1]["current_a"]), -100e-6)

    def test_fault_result_is_not_relabelled_as_completion(self):
        fault = reading(0.0, 1.3, status=3201 | (1 << 3))

        result = MonitoredResult(
            readings=(fault,),
            stop_reason="measurement_fault",
            triggering_reading=fault,
            decision_elapsed_s=0.1,
            shutdown_elapsed_s=0.2,
            requested_count=100,
        )

        directory = save_monitored_result(
            self.base,
            "fault",
            result,
            metadata={},
        )

        outcome = self.document(directory)["outcome"]

        self.assertEqual(
            outcome["stop_reason"],
            "measurement_fault",
        )
        self.assertEqual(outcome["fault_record_indices"], [0])

    def test_failure_preserves_partial_records_and_unknown_shutdown(self):
        error = TimeoutError("simulated acquisition timeout")
        error.partial_readings = (reading(0.0, 1.30),)
        error.add_note("Example diagnostic note")

        directory = save_monitored_failure(
            self.base,
            "timeout",
            error,
            metadata={"requested_count": 150},
        )

        document = self.document(directory)
        outcome = document["outcome"]

        self.assertEqual(document["record_count"], 1)
        self.assertEqual(outcome["acquisition_exit"], "raised")
        self.assertEqual(outcome["error_type"], "TimeoutError")
        self.assertEqual(
            outcome["error_notes"],
            ["Example diagnostic note"],
        )
        self.assertIsNone(outcome["shutdown_verified"])
        self.assertEqual(outcome["shutdown_evidence"], "unknown")

    def test_failure_without_partial_data_can_be_exported(self):
        error = RuntimeError("failure before data retrieval")

        directory = save_monitored_failure(
            self.base,
            "empty_failure",
            error,
            metadata={},
            shutdown_verified=False,
        )

        outcome = self.document(directory)["outcome"]

        self.assertEqual(outcome["retained_count"], 0)
        self.assertFalse(
            outcome["partial_readings_attribute_present"]
        )
        self.assertFalse(outcome["shutdown_verified"])

    def test_missing_trigger_record_is_rejected(self):
        result = MonitoredResult(
            readings=(reading(0.0, 1.3),),
            stop_reason="voltage_min",
            triggering_reading=reading(0.2, 1.0),
            decision_elapsed_s=0.3,
            shutdown_elapsed_s=0.4,
            requested_count=100,
        )

        with self.assertRaises(ValueError):
            save_monitored_result(
                self.base,
                "invalid",
                result,
                metadata={},
            )

        self.assertFalse((self.base / "invalid").exists())

    def test_export_failure_propagates_without_mutating_original_error(self):
        original = TimeoutError("original acquisition failure")
        original.partial_readings = (reading(0.0, 1.3),)

        with patch(
            "drivers.monitored_run_data.save_run",
            side_effect=OSError("simulated disk failure"),
        ):
            with self.assertRaises(OSError):
                save_monitored_failure(
                    self.base,
                    "disk_failure",
                    original,
                    metadata={},
                )

        self.assertEqual(
            str(original),
            "original acquisition failure",
        )
        self.assertEqual(len(original.partial_readings), 1)


if __name__ == "__main__":
    unittest.main()