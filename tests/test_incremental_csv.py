"""Offline tests for incremental measurement CSV writing."""

import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from drivers.incremental_csv import IncrementalCSVWriter
from drivers.run_data import CSV_FIELDS
from drivers.spot_acquisition import SMUReading


def reading(timestamp, voltage=1.303, status=3201):
    return SMUReading(
        channel=1,
        voltage_v=voltage,
        current_a=-100e-6,
        timestamp_s=timestamp,
        status=status,
    )


class IncrementalCSVTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "measurements.csv"

    def read_rows(self):
        with self.path.open(
            encoding="utf-8",
            newline="",
        ) as handle:
            reader = csv.DictReader(handle)
            self.assertEqual(tuple(reader.fieldnames), CSV_FIELDS)
            return list(reader)

    def test_batches_are_visible_before_close_with_continuous_indices(self):
        fault = reading(
            0.4,
            voltage=1.0,
            status=3201 | 2 | (1 << 3),
        )

        with IncrementalCSVWriter(self.path) as writer:
            self.assertEqual(
                writer.write_batch([reading(0.0)]),
                1,
            )

            # Read the file while the writer remains open.
            rows = self.read_rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["record_index"], "0")

            self.assertEqual(
                writer.write_batch([reading(0.2), fault]),
                2,
            )

            rows = self.read_rows()

            self.assertEqual(
                [row["record_index"] for row in rows],
                ["0", "1", "2"],
            )
            self.assertEqual(
                [float(row["instrument_timestamp_s"]) for row in rows],
                [0.0, 0.2, 0.4],
            )
            self.assertAlmostEqual(
                float(rows[0]["signed_power_w"]),
                -130.3e-6,
            )
            self.assertEqual(rows[2]["in_compliance"], "True")
            self.assertEqual(rows[2]["has_fault"], "True")
            self.assertEqual(int(rows[2]["status"]), fault.status)
            self.assertEqual(writer.records_flushed, 3)

        self.assertTrue(writer.closed)

    def test_empty_batch_leaves_header_only(self):
        with IncrementalCSVWriter(self.path) as writer:
            with patch.object(writer, "_flush") as flush:
                self.assertEqual(writer.write_batch([]), 0)
                flush.assert_not_called()

            self.assertEqual(writer.records_flushed, 0)
            self.assertEqual(self.read_rows(), [])

    def test_existing_file_is_not_overwritten(self):
        self.path.write_text("existing evidence", encoding="utf-8")

        with self.assertRaises(FileExistsError):
            IncrementalCSVWriter(self.path)

        self.assertEqual(
            self.path.read_text(encoding="utf-8"),
            "existing evidence",
        )

    def test_invalid_batch_is_rejected_before_any_rows_are_written(self):
        with IncrementalCSVWriter(self.path) as writer:
            with self.assertRaises(TypeError):
                writer.write_batch([reading(0.0), object()])

            self.assertEqual(self.read_rows(), [])
            self.assertEqual(writer.records_flushed, 0)
            self.assertFalse(writer.failed)

            # Invalid input did not cause a partial CSV batch.
            writer.write_batch([reading(0.0)])
            self.assertEqual(writer.records_flushed, 1)

    def test_flush_failure_propagates_and_blocks_retry(self):
        writer = IncrementalCSVWriter(self.path)
        self.addCleanup(writer.close)

        with patch.object(
            writer,
            "_flush",
            side_effect=OSError("simulated flush failure"),
        ):
            with self.assertRaises(OSError):
                writer.write_batch([reading(0.0)])

        self.assertTrue(writer.failed)
        self.assertEqual(writer.records_flushed, 0)

        with self.assertRaises(RuntimeError):
            writer.write_batch([reading(0.0)])

        # Failed-batch bytes may already exist. No automatic retry
        # or claim about the failed batch's on-disk record count.
        self.assertTrue(self.path.exists())

    def test_context_preserves_original_error_if_close_also_fails(self):
        writer = IncrementalCSVWriter(self.path)
        original = TimeoutError("original acquisition error")

        with patch.object(
            writer,
            "close",
            side_effect=OSError("simulated close failure"),
        ):
            with self.assertRaises(TimeoutError) as caught:
                with writer:
                    writer.write_batch([reading(0.0)])
                    raise original

        self.assertIs(caught.exception, original)
        self.assertTrue(
            any(
                "CSV close also failed" in note
                for note in original.__notes__
            )
        )

        # Close the real handle after removing the simulated failure.
        writer.close()
        self.assertTrue(writer.closed)


if __name__ == "__main__":
    unittest.main()