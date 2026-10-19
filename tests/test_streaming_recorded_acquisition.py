"""Offline orchestration tests. No VISA or hardware."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from drivers.monitored_acquisition import (
    MonitoredResult,
    StopLimits,
)
from drivers.spot_acquisition import SMUReading
from drivers.streaming_recorded_acquisition import (
    acquire_streaming_and_finalize,
)
from drivers.streaming_run import StreamingRun


def reading(timestamp=0.0, voltage=1.303):
    return SMUReading(
        channel=1,
        voltage_v=voltage,
        current_a=-100e-6,
        timestamp_s=timestamp,
        status=3201,
    )


def result_for(records, *, trigger=None):
    return MonitoredResult(
        readings=tuple(records),
        stop_reason="voltage_min" if trigger is not None else "completed",
        triggering_reading=trigger,
        decision_elapsed_s=0.3,
        shutdown_elapsed_s=0.4,
        requested_count=100 if trigger is not None else len(records),
    )


class FakeAcquisition:
    """Simulate acquire() after its instrument-shutdown handling."""

    def __init__(self, writer, records, *, result=None, error=None):
        self.writer = writer
        self.records = tuple(records)
        self.result = result
        self.error = error
        self.calls = []

    def acquire(self, limits, **kwargs):
        self.calls.append((limits, kwargs))
        self.writer.write_batch(self.records)

        if self.error is not None:
            self.error.partial_readings = self.records
            raise self.error

        return self.result


class StreamingRecordedAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def create_run(self):
        run = StreamingRun(
            self.base,
            "test_run",
            metadata={"instrument": "fake"},
        )
        self.addCleanup(run.close)
        return run

    def document(self, run):
        return json.loads(
            (run.directory / "metadata.json").read_text(
                encoding="utf-8"
            )
        )

    def test_completion_finalizes_existing_streamed_csv(self):
        run = self.create_run()
        records = [reading(), reading(0.2)]
        expected = result_for(records)
        limits = StopLimits(min_voltage_v=1.1)

        acquisition = FakeAcquisition(
            run.writer,
            records,
            result=expected,
        )

        result, directory = acquire_streaming_and_finalize(
            acquisition,
            run,
            limits=limits,
            timeout_s=10.0,
            poll_interval_s=0.02,
        )

        self.assertIs(result, expected)
        self.assertEqual(directory, run.directory)
        self.assertTrue(run.writer.closed)
        self.assertTrue((directory / "COMPLETE").exists())
        self.assertEqual(
            self.document(run)["outcome"]["stop_reason"],
            "completed",
        )
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

    def test_early_stop_preserves_trigger_mapping(self):
        run = self.create_run()
        first = reading()
        trigger = reading(0.2, voltage=1.0)
        records = [first, trigger]

        acquisition = FakeAcquisition(
            run.writer,
            records,
            result=result_for(records, trigger=trigger),
        )

        acquire_streaming_and_finalize(acquisition, run)

        outcome = self.document(run)["outcome"]

        self.assertEqual(outcome["stop_reason"], "voltage_min")
        self.assertEqual(outcome["triggering_record_index"], 1)
        self.assertEqual(outcome["records_flushed"], 2)

    def test_acquisition_error_is_recorded_and_reraised_unchanged(self):
        run = self.create_run()
        original = TimeoutError("original timeout")

        acquisition = FakeAcquisition(
            run.writer,
            [reading()],
            error=original,
        )

        with self.assertRaises(TimeoutError) as caught:
            acquire_streaming_and_finalize(acquisition, run)

        self.assertIs(caught.exception, original)
        self.assertEqual(original.run_directory, run.directory)
        self.assertTrue(run.writer.closed)
        self.assertFalse(
            (run.directory / "COMPLETE").exists()
        )

        outcome = self.document(run)["outcome"]

        self.assertEqual(outcome["state"], "raised")
        self.assertEqual(outcome["records_flushed"], 1)
        self.assertIsNone(outcome["shutdown_verified"])

    def test_failure_metadata_error_does_not_mask_acquisition_error(self):
        run = self.create_run()
        original = TimeoutError("original timeout")

        acquisition = FakeAcquisition(
            run.writer,
            [reading()],
            error=original,
        )

        with patch.object(
            run,
            "fail",
            side_effect=OSError("simulated metadata failure"),
        ):
            with self.assertRaises(TimeoutError) as caught:
                acquire_streaming_and_finalize(acquisition, run)

        self.assertIs(caught.exception, original)
        self.assertTrue(run.writer.closed)
        self.assertTrue(
            any(
                "failure metadata could not be finalized" in note
                for note in original.__notes__
            )
        )

    def test_finalization_error_keeps_returned_result_accessible(self):
        run = self.create_run()
        records = [reading()]
        expected = result_for(records)
        failure = OSError("simulated finalization failure")

        acquisition = FakeAcquisition(
            run.writer,
            records,
            result=expected,
        )

        with patch.object(run, "finish", side_effect=failure):
            with self.assertRaises(OSError) as caught:
                acquire_streaming_and_finalize(acquisition, run)

        self.assertIs(caught.exception, failure)
        self.assertIs(failure.acquisition_result, expected)
        self.assertEqual(failure.partial_readings, tuple(records))
        self.assertTrue(run.writer.closed)
        self.assertFalse(
            (run.directory / "COMPLETE").exists()
        )

    def test_keyboard_interrupt_is_preserved(self):
        run = self.create_run()
        original = KeyboardInterrupt("simulated interruption")

        acquisition = FakeAcquisition(
            run.writer,
            [],
            error=original,
        )

        with self.assertRaises(KeyboardInterrupt) as caught:
            acquire_streaming_and_finalize(acquisition, run)

        self.assertIs(caught.exception, original)
        self.assertTrue(run.writer.closed)
        self.assertFalse(
            (run.directory / "COMPLETE").exists()
        )
        self.assertEqual(
            self.document(run)["outcome"]["error_type"],
            "KeyboardInterrupt",
        )


if __name__ == "__main__":
    unittest.main()