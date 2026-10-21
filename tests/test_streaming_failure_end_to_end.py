"""Offline end-to-end failure tests for streaming acquisition.

Uses the production:
- acquisition loop
- trace parser
- incremental CSV writer
- shutdown-reporting helper
- streaming run lifecycle
- streaming orchestration wrapper

Only instrument I/O and deliberate failure points are simulated.
No VISA connection or hardware required.
"""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acquisition.monitored_acquisition import StopLimits
from acquisition.spot_acquisition import SMUReading
from acquisition.streaming_acquisition import (
    StreamingMonitoredAcquisition,
)
from recording.recorded_acquisition import (
    acquire_streaming_and_finalize,
)
from recording.streaming_run import StreamingRun


def reading(timestamp, voltage=1.303):
    return SMUReading(
        channel=1,
        voltage_v=voltage,
        current_a=-100e-6,
        timestamp_s=timestamp,
        status=3201,
    )


class FakeSMU:
    def __init__(self, *, shutdown_error=None):
        self.shutdown_calls = 0
        self.shutdown_error = shutdown_error

    def shutdown(self):
        self.shutdown_calls += 1

        if self.shutdown_error is not None:
            raise self.shutdown_error


class ScriptedStreamingAcquisition(StreamingMonitoredAcquisition):
    """Use real acquisition and parsing with scripted instrument data."""

    def __init__(self, smu, writer, batches):
        super().__init__(
            smu,
            channel=1,
            writer=writer,
        )

        self._count = 100
        self._interval_s = 0.2
        self._trace_ready = True

        self.batches = list(batches)
        self.pending_batch = None
        self.commands = []

    def _preflight(self):
        # Bypass hardware queries but retain CSV readiness checks.
        self._check_writer()

        if not self._trace_ready:
            raise RuntimeError("Not configured.")

    def _write(self, command):
        self.commands.append(command)

    def _register(self, command):
        # Stay active until the deliberately injected failure.
        return 0

    def _check_lost_events(self):
        pass

    def _trace_count(self):
        if self.pending_batch is None:
            self.pending_batch = (
                self.batches.pop(0) if self.batches else []
            )

        return len(self.readings) + len(self.pending_batch)

    def _query(self, command):
        if not command.startswith(":TRAC1:DATA? "):
            raise AssertionError(
                f"Unexpected scripted query: {command}"
            )

        batch = self.pending_batch

        if not batch:
            raise AssertionError("No scripted data available.")

        response = ",".join(
            str(value)
            for record in batch
            for value in (
                record.voltage_v,
                record.current_a,
                record.timestamp_s,
                record.status,
            )
        )

        self.pending_batch = None
        return response


class StreamingFailureEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def create_setup(self, *, shutdown_error=None):
        run = StreamingRun(
            self.base,
            "failure_test",
            metadata={"test": "offline_end_to_end"},
        )
        self.addCleanup(run.close)

        first = reading(0.0)
        second = reading(0.2)

        smu = FakeSMU(shutdown_error=shutdown_error)

        acquisition = ScriptedStreamingAcquisition(
            smu,
            run.writer,
            [[first], [second]],
        )

        return run, acquisition, smu, first, second

    def read_metadata(self, run):
        return json.loads(
            (run.directory / "metadata.json").read_text(
                encoding="utf-8"
            )
        )

    def fail_second_batch_flush(self, writer, error):
        """Return a replacement flush: first succeeds, second raises."""
        real_flush = writer._flush
        calls = 0

        def scripted_flush():
            nonlocal calls
            calls += 1

            if calls == 2:
                raise error

            return real_flush()

        return scripted_flush

    def acquire(self, acquisition, run):
        return acquire_streaming_and_finalize(
            acquisition,
            run,
            limits=StopLimits(
                min_voltage_v=1.10,
                max_voltage_v=1.50,
                min_current_a=-105e-6,
                max_current_a=-95e-6,
            ),
            timeout_s=1.0,
            poll_interval_s=0.001,
        )

    def test_csv_failure_shuts_down_then_records_failure(self):
        run, acquisition, smu, first, second = self.create_setup()
        original = OSError("simulated second-batch flush failure")

        real_fail = run.fail
        shutdown_counts_when_recording_failure = []
        shutdown_reports_when_recording_failure = []

        def observed_fail(error):
            shutdown_counts_when_recording_failure.append(
                smu.shutdown_calls
            )
            shutdown_reports_when_recording_failure.append(
                dict(error.shutdown_report)
            )
            return real_fail(error)

        with patch.object(
            run.writer,
            "_flush",
            side_effect=self.fail_second_batch_flush(
                run.writer,
                original,
            ),
        ):
            with patch.object(
                run,
                "fail",
                side_effect=observed_fail,
            ):
                with self.assertRaises(OSError) as caught:
                    self.acquire(acquisition, run)

        expected_report = {
            "attempted": True,
            "verified": True,
            "evidence": "smu_shutdown_returned",
            "error_type": None,
            "error_message": None,
        }

        self.assertIs(caught.exception, original)
        self.assertEqual(smu.shutdown_calls, 1)
        self.assertEqual(
            shutdown_counts_when_recording_failure,
            [1],
        )
        self.assertEqual(
            shutdown_reports_when_recording_failure,
            [expected_report],
        )
        self.assertEqual(
            original.shutdown_report,
            expected_report,
        )

        self.assertEqual(
            original.partial_readings,
            (first, second),
        )
        self.assertEqual(
            acquisition.readings,
            (first, second),
        )
        self.assertFalse(acquisition._trace_ready)

        self.assertTrue(run.writer.failed)
        self.assertTrue(run.writer.closed)
        self.assertEqual(run.writer.records_flushed, 1)

        self.assertEqual(
            original.run_directory,
            run.directory,
        )
        self.assertFalse(
            (run.directory / "COMPLETE").exists()
        )

        outcome = self.read_metadata(run)["outcome"]

        self.assertEqual(outcome["state"], "raised")
        self.assertEqual(outcome["stop_reason"], "exception")
        self.assertEqual(outcome["error_type"], "OSError")
        self.assertEqual(
            outcome["error_message"],
            str(original),
        )
        self.assertEqual(outcome["records_flushed"], 1)
        self.assertEqual(outcome["retained_in_memory_count"], 2)
        self.assertTrue(outcome["csv_writer_failed"])

        self.assertIs(outcome["shutdown_verified"], True)
        self.assertEqual(
            outcome["shutdown_evidence"],
            "smu_shutdown_returned",
        )
        self.assertEqual(
            outcome["shutdown_report"],
            expected_report,
        )
        self.assertIsNone(outcome["shutdown_report_error"])

        # Do not assert the physical CSV row count here:
        # the failed batch may already have written bytes, and
        # closing the handle can flush buffered bytes afterward.

    def test_shutdown_failure_does_not_replace_csv_failure(self):
        shutdown_error = RuntimeError(
            "simulated shutdown failure"
        )

        run, acquisition, smu, first, second = self.create_setup(
            shutdown_error=shutdown_error
        )
        original = OSError("original CSV flush failure")

        with patch.object(
            run.writer,
            "_flush",
            side_effect=self.fail_second_batch_flush(
                run.writer,
                original,
            ),
        ):
            with self.assertRaises(OSError) as caught:
                self.acquire(acquisition, run)

        expected_report = {
            "attempted": True,
            "verified": False,
            "evidence": "smu_shutdown_raised",
            "error_type": "RuntimeError",
            "error_message": "simulated shutdown failure",
        }

        self.assertIs(caught.exception, original)
        self.assertEqual(smu.shutdown_calls, 1)
        self.assertEqual(
            original.partial_readings,
            (first, second),
        )
        self.assertEqual(
            acquisition.readings,
            (first, second),
        )
        self.assertEqual(
            original.shutdown_report,
            expected_report,
        )
        self.assertFalse(acquisition._trace_ready)

        self.assertTrue(
            any(
                "Shutdown could not be verified" in note
                for note in original.__notes__
            )
        )

        outcome = self.read_metadata(run)["outcome"]

        self.assertEqual(outcome["state"], "raised")
        self.assertEqual(outcome["error_type"], "OSError")
        self.assertEqual(
            outcome["error_message"],
            str(original),
        )
        self.assertEqual(outcome["records_flushed"], 1)
        self.assertEqual(outcome["retained_in_memory_count"], 2)
        self.assertTrue(outcome["csv_writer_failed"])

        self.assertTrue(
            any(
                "Shutdown could not be verified" in note
                for note in outcome["error_notes"]
            )
        )
        self.assertIs(outcome["shutdown_verified"], False)
        self.assertEqual(
            outcome["shutdown_evidence"],
            "smu_shutdown_raised",
        )
        self.assertEqual(
            outcome["shutdown_report"],
            expected_report,
        )
        self.assertIsNone(outcome["shutdown_report_error"])

        self.assertTrue(run.writer.closed)
        self.assertFalse(
            (run.directory / "COMPLETE").exists()
        )

    def test_metadata_failure_does_not_replace_csv_failure(self):
        run, acquisition, smu, first, second = self.create_setup()
        original = OSError("original CSV flush failure")

        with patch.object(
            run.writer,
            "_flush",
            side_effect=self.fail_second_batch_flush(
                run.writer,
                original,
            ),
        ):
            with patch.object(
                run,
                "_replace_metadata",
                side_effect=OSError(
                    "simulated failure-metadata write failure"
                ),
            ):
                with self.assertRaises(OSError) as caught:
                    self.acquire(acquisition, run)

        self.assertIs(caught.exception, original)
        self.assertEqual(smu.shutdown_calls, 1)
        self.assertEqual(
            original.partial_readings,
            (first, second),
        )
        self.assertEqual(
            acquisition.readings,
            (first, second),
        )
        self.assertEqual(
            original.shutdown_report,
            {
                "attempted": True,
                "verified": True,
                "evidence": "smu_shutdown_returned",
                "error_type": None,
                "error_message": None,
            },
        )
        self.assertFalse(acquisition._trace_ready)

        self.assertTrue(
            any(
                "failure metadata could not be finalized" in note
                for note in original.__notes__
            )
        )

        self.assertTrue(run.writer.closed)
        self.assertFalse(
            (run.directory / "COMPLETE").exists()
        )

        # Initial metadata remains when final replacement fails.
        # Do not claim that the shutdown report was saved.
        outcome = self.read_metadata(run)["outcome"]
        self.assertEqual(outcome["state"], "unfinalized")
        self.assertIsNone(outcome["shutdown_verified"])


if __name__ == "__main__":
    unittest.main()