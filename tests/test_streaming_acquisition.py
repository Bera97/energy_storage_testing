"""Offline integration tests for incremental acquisition saving.

No VISA connection or hardware required.
"""

import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from drivers.incremental_csv import IncrementalCSVWriter
from drivers.monitored_acquisition import StopLimits
from drivers.spot_acquisition import SMUReading
from drivers.streaming_acquisition import (
    StreamingMonitoredAcquisition,
)


def reading(timestamp, voltage=1.303, status=3201):
    return SMUReading(
        channel=1,
        voltage_v=voltage,
        current_a=-100e-6,
        timestamp_s=timestamp,
        status=status,
    )


class FakeSMU:
    def __init__(self):
        self.shutdown_calls = 0

    def shutdown(self):
        self.shutdown_calls += 1


class ScriptedStreamingAcquisition(StreamingMonitoredAcquisition):
    """Use the real acquisition loop and parser with scripted I/O."""

    def __init__(
        self,
        writer,
        batches,
        *,
        requested_count,
        idle=False,
    ):
        super().__init__(
            FakeSMU(),
            channel=1,
            writer=writer,
        )

        self._count = requested_count
        self._interval_s = 0.2
        self._trace_ready = True

        self.batches = list(batches)
        self.pending_batch = None
        self.idle = idle
        self.commands = []

    def _preflight(self):
        # Skip instrument configuration queries, but exercise
        # the production CSV preflight.
        self._check_writer()

        if not self._trace_ready:
            raise RuntimeError("Not configured.")

    def _write(self, command):
        self.commands.append(command)

    def _register(self, command):
        return (1 << 4) if self.idle else 0

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
                f"Unexpected fake query: {command}"
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


class StreamingAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "measurements.csv"

    def read_rows(self):
        with self.path.open(
            encoding="utf-8",
            newline="",
        ) as handle:
            return list(csv.DictReader(handle))

    def test_normal_completion_saves_records(self):
        first = reading(0.0)
        second = reading(0.2)

        with IncrementalCSVWriter(self.path) as writer:
            acquisition = ScriptedStreamingAcquisition(
                writer,
                [[first, second]],
                requested_count=2,
                idle=True,
            )

            result = acquisition.acquire(
                timeout_s=1.0,
                poll_interval_s=0.001,
            )

            self.assertEqual(result.stop_reason, "completed")
            self.assertEqual(result.readings, (first, second))
            self.assertEqual(writer.records_flushed, 2)
            self.assertEqual(len(self.read_rows()), 2)
            self.assertEqual(
                acquisition._smu.shutdown_calls,
                1,
            )

    def test_delayed_crossing_saves_all_batches(self):
        first = reading(0.0, voltage=1.30)
        above = reading(0.2, voltage=1.20)
        trigger = reading(0.4, voltage=1.00)

        with IncrementalCSVWriter(self.path) as writer:
            acquisition = ScriptedStreamingAcquisition(
                writer,
                [[first], [above], [trigger]],
                requested_count=100,
            )

            result = acquisition.acquire(
                StopLimits(min_voltage_v=1.10),
                timeout_s=1.0,
                poll_interval_s=0.001,
            )

            self.assertEqual(result.stop_reason, "voltage_min")
            self.assertEqual(result.triggering_reading, trigger)
            self.assertEqual(writer.records_flushed, 3)

            rows = self.read_rows()

            self.assertEqual(
                [row["record_index"] for row in rows],
                ["0", "1", "2"],
            )
            self.assertEqual(
                [float(row["instrument_timestamp_s"]) for row in rows],
                [0.0, 0.2, 0.4],
            )

    def test_final_records_are_saved_after_shutdown_without_duplicates(self):
        trigger = reading(0.0, voltage=1.00)
        final = reading(0.2, voltage=0.99)

        with IncrementalCSVWriter(self.path) as writer:
            acquisition = ScriptedStreamingAcquisition(
                writer,
                [[trigger], [final]],
                requested_count=100,
            )

            result = acquisition.acquire(
                StopLimits(min_voltage_v=1.10),
                timeout_s=1.0,
                poll_interval_s=0.001,
            )

            self.assertEqual(result.readings, (trigger, final))
            self.assertEqual(result.triggering_reading, trigger)
            self.assertEqual(writer.records_flushed, 2)

            rows = self.read_rows()

            self.assertEqual(len(rows), 2)
            self.assertEqual(
                [row["record_index"] for row in rows],
                ["0", "1"],
            )
            self.assertEqual(
                acquisition._smu.shutdown_calls,
                1,
            )

    def test_write_failure_attempts_shutdown_and_retains_partial_records(self):
        record = reading(0.0)

        with IncrementalCSVWriter(self.path) as writer:
            acquisition = ScriptedStreamingAcquisition(
                writer,
                [[record]],
                requested_count=100,
            )

            with patch.object(
                writer,
                "_flush",
                side_effect=OSError("simulated CSV flush failure"),
            ):
                with self.assertRaises(OSError) as caught:
                    acquisition.acquire(
                        timeout_s=1.0,
                        poll_interval_s=0.001,
                    )

            self.assertEqual(
                caught.exception.partial_readings,
                (record,),
            )
            self.assertEqual(acquisition.readings, (record,))
            self.assertEqual(
                acquisition._smu.shutdown_calls,
                1,
            )
            self.assertTrue(writer.failed)
            self.assertEqual(writer.records_flushed, 0)
            self.assertFalse(acquisition._trace_ready)

    def test_closed_writer_fails_before_init_and_attempts_shutdown(self):
        writer = IncrementalCSVWriter(self.path)
        writer.close()

        acquisition = ScriptedStreamingAcquisition(
            writer,
            [[reading(0.0)]],
            requested_count=100,
        )

        with self.assertRaises(RuntimeError):
            acquisition.acquire(
                timeout_s=1.0,
                poll_interval_s=0.001,
            )

        self.assertFalse(
            any(
                command.startswith(":INIT")
                for command in acquisition.commands
            )
        )
        self.assertEqual(acquisition._smu.shutdown_calls, 1)

    def test_fault_record_is_saved_before_fault_termination(self):
        fault = reading(
            0.0,
            status=3201 | (1 << 3),
        )

        with IncrementalCSVWriter(self.path) as writer:
            acquisition = ScriptedStreamingAcquisition(
                writer,
                [[fault]],
                requested_count=100,
            )

            result = acquisition.acquire(
                timeout_s=1.0,
                poll_interval_s=0.001,
            )

            self.assertEqual(
                result.stop_reason,
                "measurement_fault",
            )
            self.assertEqual(result.fault_readings, (fault,))
            self.assertEqual(writer.records_flushed, 1)

            rows = self.read_rows()

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["has_fault"], "True")
            self.assertEqual(int(rows[0]["status"]), fault.status)
            self.assertEqual(
                acquisition._smu.shutdown_calls,
                1,
            )


if __name__ == "__main__":
    unittest.main()