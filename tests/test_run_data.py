"""Offline tests for the standalone run-data exporter."""

import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from drivers.run_data import CSV_FIELDS, save_run
from drivers.spot_acquisition import SMUReading


def reading(
    timestamp=0.0,
    voltage=1.303,
    current=-100e-6,
    status=3201,
):
    return SMUReading(
        channel=1,
        voltage_v=voltage,
        current_a=current,
        timestamp_s=timestamp,
        status=status,
    )


class RunDataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def export(self, records, run_id="test_run", **kwargs):
        return save_run(
            self.base,
            run_id,
            records,
            metadata=kwargs.get(
                "metadata",
                {"instrument": "fake", "set_current_a": -100e-6},
            ),
            outcome=kwargs.get(
                "outcome",
                {
                    "stop_reason": "voltage_min",
                    "shutdown_verified": True,
                },
            ),
        )

    def test_measurements_metadata_and_complete_marker(self):
        records = (
            reading(),
            reading(timestamp=0.2, voltage=1.0),
        )

        directory = self.export(records)

        with (directory / "measurements.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            reader = csv.DictReader(handle)
            self.assertEqual(tuple(reader.fieldnames), CSV_FIELDS)
            rows = list(reader)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["record_index"], "0")
        self.assertEqual(rows[1]["record_index"], "1")
        self.assertEqual(
            float(rows[1]["instrument_timestamp_s"]),
            0.2,
        )
        self.assertEqual(float(rows[0]["current_a"]), -100e-6)
        self.assertAlmostEqual(
            float(rows[0]["signed_power_w"]),
            -130.3e-6,
        )

        document = json.loads(
            (directory / "metadata.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["run_id"], "test_run")
        self.assertEqual(document["record_count"], 2)
        self.assertEqual(
            document["timestamp_basis"],
            "raw_instrument_time",
        )
        self.assertEqual(
            document["outcome"]["stop_reason"],
            "voltage_min",
        )
        self.assertTrue((directory / "COMPLETE").is_file())

    def test_existing_run_is_not_overwritten(self):
        directory = self.export([reading()])
        original = (directory / "measurements.csv").read_bytes()

        with self.assertRaises(FileExistsError):
            self.export([reading(voltage=9.0)])

        self.assertEqual(
            (directory / "measurements.csv").read_bytes(),
            original,
        )

    def test_invalid_run_ids_are_rejected(self):
        for run_id in ("", "../escape", "a/b", "a\\b", "x" * 81):
            with self.subTest(run_id=run_id):
                with self.assertRaises(ValueError):
                    self.export([], run_id=run_id)

        self.assertEqual(list(self.base.iterdir()), [])

    def test_invalid_json_is_rejected_before_directory_creation(self):
        for metadata in (
            {"invalid": float("nan")},
            {"invalid": object()},
        ):
            with self.subTest(metadata=metadata):
                with self.assertRaises((TypeError, ValueError)):
                    self.export([], metadata=metadata)

        self.assertFalse((self.base / "test_run").exists())

    def test_empty_failed_run_can_be_exported(self):
        directory = self.export(
            [],
            outcome={
                "stop_reason": "exception",
                "error_type": "TimeoutError",
                "shutdown_verified": False,
            },
        )

        with (directory / "measurements.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            rows = list(csv.DictReader(handle))

        self.assertEqual(rows, [])

        document = json.loads(
            (directory / "metadata.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(document["record_count"], 0)
        self.assertFalse(
            document["outcome"]["shutdown_verified"]
        )

        # COMPLETE describes export success, not experiment success.
        self.assertTrue((directory / "COMPLETE").exists())

    def test_fault_and_compliance_records_are_preserved(self):
        record = reading(status=3201 | 2 | (1 << 3))
        directory = self.export([record])

        with (directory / "measurements.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            rows = list(csv.DictReader(handle))

        self.assertEqual(int(rows[0]["status"]), record.status)
        self.assertEqual(rows[0]["in_compliance"], "True")
        self.assertEqual(rows[0]["has_fault"], "True")

    def test_io_failure_propagates_without_complete_marker(self):
        with patch.object(
            Path,
            "open",
            side_effect=PermissionError("simulated write failure"),
        ):
            with self.assertRaises(PermissionError):
                self.export([reading()])

        directory = self.base / "test_run"
        self.assertTrue(directory.is_dir())
        self.assertFalse((directory / "COMPLETE").exists())

        # A failed export is not silently reused.
        with self.assertRaises(FileExistsError):
            self.export([reading()])

    def test_metadata_failure_preserves_csv_without_complete_marker(self):
        original_open = Path.open

        def fail_metadata(path, *args, **kwargs):
            if path.name == "metadata.json":
                raise OSError("simulated metadata write failure")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", new=fail_metadata):
            with self.assertRaises(OSError):
                self.export([reading()])

        directory = self.base / "test_run"

        self.assertTrue((directory / "measurements.csv").exists())
        self.assertFalse((directory / "COMPLETE").exists())


if __name__ == "__main__":
    unittest.main()