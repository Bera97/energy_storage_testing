"""Offline tests for streaming-run metadata and finalization."""

import csv
import json
from pathlib import Path
import tempfile
import unittest

from drivers.monitored_acquisition import MonitoredResult
from drivers.spot_acquisition import SMUReading
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


class StreamingRunTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def create_run(self, run_id="test_run"):
        run = StreamingRun(
            self.base,
            run_id,
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

    def test_initial_files_are_unfinalized(self):
        run = self.create_run()
        document = self.document(run)

        self.assertEqual(
            document["outcome"]["state"],
            "unfinalized",
        )
        self.assertIsNone(
            document["outcome"]["shutdown_verified"]
        )
        self.assertEqual(
            document["export_mode"],
            "incremental_csv",
        )
        self.assertTrue(
            (run.directory / "measurements.csv").exists()
        )
        self.assertFalse(
            (run.directory / "COMPLETE").exists()
        )

    def test_completion_preserves_csv_and_creates_marker(self):
        run = self.create_run()
        records = [reading(), reading(0.2)]

        run.writer.write_batch(records)
        run.finish(result_for(records))

        self.assertTrue(run.writer.closed)
        self.assertTrue(
            (run.directory / "COMPLETE").exists()
        )

        document = self.document(run)

        self.assertEqual(document["record_count"], 2)
        self.assertEqual(
            document["outcome"]["stop_reason"],
            "completed",
        )
        self.assertTrue(
            document["outcome"]["shutdown_verified"]
        )

        with (run.directory / "measurements.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            rows = list(csv.DictReader(handle))

        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [row["record_index"] for row in rows],
            ["0", "1"],
        )

    def test_early_stop_records_trigger_index(self):
        run = self.create_run()
        first = reading()
        trigger = reading(0.2, voltage=1.0)

        run.writer.write_batch([first])
        run.writer.write_batch([trigger])
        run.finish(
            result_for([first, trigger], trigger=trigger)
        )

        outcome = self.document(run)["outcome"]

        self.assertEqual(outcome["stop_reason"], "voltage_min")
        self.assertEqual(outcome["triggering_record_index"], 1)
        self.assertEqual(
            outcome["triggering_reading"]["voltage_v"],
            1.0,
        )
        self.assertEqual(outcome["requested_count"], 100)

    def test_failure_has_no_complete_marker(self):
        run = self.create_run()
        record = reading()
        run.writer.write_batch([record])

        error = TimeoutError("simulated acquisition timeout")
        error.partial_readings = (record,)
        error.add_note("example diagnostic note")

        run.fail(error)

        outcome = self.document(run)["outcome"]

        self.assertTrue(run.writer.closed)
        self.assertEqual(outcome["state"], "raised")
        self.assertEqual(outcome["error_type"], "TimeoutError")
        self.assertEqual(outcome["records_flushed"], 1)
        self.assertEqual(outcome["retained_in_memory_count"], 1)
        self.assertIsNone(outcome["shutdown_verified"])
        self.assertEqual(
            outcome["error_notes"],
            ["example diagnostic note"],
        )
        self.assertFalse(
            (run.directory / "COMPLETE").exists()
        )

    def test_count_mismatch_does_not_create_complete_marker(self):
        run = self.create_run()

        with self.assertRaises(RuntimeError):
            run.finish(result_for([reading()]))

        self.assertFalse(
            (run.directory / "COMPLETE").exists()
        )

    def test_existing_run_directory_is_not_reused(self):
        run = self.create_run()

        with self.assertRaises(FileExistsError):
            StreamingRun(
                self.base,
                "test_run",
                metadata={},
            )

        self.assertTrue(
            (run.directory / "metadata.json").exists()
        )


if __name__ == "__main__":
    unittest.main()