"""Offline tests for acquisition-and-export orchestration."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from drivers.monitored_acquisition import (
    MonitoredResult,
    StopLimits,
)
from drivers.recorded_acquisition import acquire_and_save
from drivers.spot_acquisition import SMUReading


def example_result():
    record = SMUReading(
        channel=1,
        voltage_v=1.0,
        current_a=-100e-6,
        timestamp_s=0.0,
        status=3201,
    )

    return MonitoredResult(
        readings=(record,),
        stop_reason="voltage_min",
        triggering_reading=record,
        decision_elapsed_s=0.1,
        shutdown_elapsed_s=0.2,
        requested_count=100,
    )


class FakeAcquisition:
    def __init__(self, *, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def acquire(self, limits, **kwargs):
        self.calls.append((limits, kwargs))

        if self.error is not None:
            raise self.error

        return self.result


class RecordedAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def test_returned_result_is_saved_and_arguments_forwarded(self):
        expected = example_result()
        acquisition = FakeAcquisition(result=expected)
        limits = StopLimits(min_voltage_v=1.1)

        result, directory = acquire_and_save(
            acquisition,
            self.base,
            "returned",
            metadata={"test": "offline"},
            limits=limits,
            timeout_s=10.0,
            poll_interval_s=0.02,
        )

        self.assertIs(result, expected)
        self.assertTrue((directory / "COMPLETE").exists())
        self.assertEqual(
            acquisition.calls,
            [
                (
                    limits,
                    {
                        "timeout_s": 10.0,
                        "poll_interval_s": 0.02,
                    },
                )
            ],
        )

    def test_original_acquisition_error_is_saved_and_reraised(self):
        original = TimeoutError("simulated timeout")
        original.partial_readings = example_result().readings
        acquisition = FakeAcquisition(error=original)

        with self.assertRaises(TimeoutError) as caught:
            acquire_and_save(
                acquisition,
                self.base,
                "timeout",
                metadata={},
            )

        self.assertIs(caught.exception, original)
        directory = original.run_directory
        self.assertTrue((directory / "COMPLETE").exists())

        document = json.loads(
            (directory / "metadata.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(document["record_count"], 1)
        self.assertIsNone(
            document["outcome"]["shutdown_verified"]
        )

    def test_export_failure_does_not_replace_acquisition_error(self):
        original = TimeoutError("original timeout")
        original.partial_readings = example_result().readings
        acquisition = FakeAcquisition(error=original)

        with patch(
            "drivers.recorded_acquisition.save_monitored_failure",
            side_effect=OSError("simulated disk failure"),
        ):
            with self.assertRaises(TimeoutError) as caught:
                acquire_and_save(
                    acquisition,
                    self.base,
                    "failed_export",
                    metadata={},
                )

        self.assertIs(caught.exception, original)
        self.assertTrue(
            any(
                "Failure-data export also failed" in note
                for note in original.__notes__
            )
        )
        self.assertEqual(len(original.partial_readings), 1)

    def test_returned_result_remains_available_if_export_fails(self):
        expected = example_result()
        acquisition = FakeAcquisition(result=expected)

        with patch(
            "drivers.recorded_acquisition.save_monitored_result",
            side_effect=OSError("simulated disk failure"),
        ):
            with self.assertRaises(OSError) as caught:
                acquire_and_save(
                    acquisition,
                    self.base,
                    "returned_export_failure",
                    metadata={},
                )

        self.assertIs(
            caught.exception.acquisition_result,
            expected,
        )
        self.assertEqual(len(acquisition.calls), 1)

    def test_keyboard_interrupt_is_not_swallowed(self):
        original = KeyboardInterrupt("simulated interruption")
        original.partial_readings = ()
        acquisition = FakeAcquisition(error=original)

        with self.assertRaises(KeyboardInterrupt) as caught:
            acquire_and_save(
                acquisition,
                self.base,
                "interrupted",
                metadata={},
            )

        self.assertIs(caught.exception, original)
        self.assertTrue(
            (original.run_directory / "COMPLETE").exists()
        )


if __name__ == "__main__":
    unittest.main()