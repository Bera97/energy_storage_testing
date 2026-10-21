"""Offline tests for separate recorded-analysis exports."""

from contextlib import redirect_stderr, redirect_stdout
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analysis.export_recorded_analysis import (
    ANALYSIS_FIELDS,
    export_analysis,
    main,
)
from analysis.validate_recorded_run import FIELDS


class ExportRecordedAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

        self.source = self.root / "recording"
        self.source.mkdir()
        self.output = self.root / "analysis-output"

        self.document = {
            "schema_version": 1,
            "export_mode": "incremental_csv",
            "metadata": {"channel": 1},
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

        self.rows = [
            [0, 1, 10.0, 2.0, -0.5, -1.0, 0, False, False],
            [1, 1, 12.0, 2.0, -0.5, -1.0, 0, False, False],
        ]

        self.save()

    def save(self):
        (self.source / "metadata.json").write_text(
            json.dumps(self.document),
            encoding="utf-8",
        )

        with (self.source / "measurements.csv").open(
            "w",
            encoding="utf-8",
            newline="",
        ) as handle:
            writer = csv.writer(handle)
            writer.writerow(FIELDS)
            writer.writerows(self.rows)

        marker = self.source / "COMPLETE"

        if self.document["outcome"]["state"] == "returned":
            marker.write_text(
                "Run files finalized.\n",
                encoding="utf-8",
            )
        elif marker.exists():
            marker.unlink()

    def test_export_writes_expected_files_and_values(self):
        returned_summary = export_analysis(
            self.source, self.output
        )

        self.assertEqual(
            {path.name for path in self.output.iterdir()},
            {"analysis.csv", "summary.json"},
        )

        with (self.output / "analysis.csv").open(
            encoding="utf-8",
            newline="",
        ) as handle:
            reader = csv.DictReader(handle)
            self.assertEqual(
                tuple(reader.fieldnames), ANALYSIS_FIELDS
            )
            rows = list(reader)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["dt_s"], "")
        self.assertEqual(rows[1]["sample_index"], "1")
        self.assertAlmostEqual(
            float(rows[1]["elapsed_time_s"]), 2
        )
        self.assertAlmostEqual(
            float(rows[1]["cumulative_signed_charge_c"]), -1
        )
        self.assertAlmostEqual(
            float(rows[1]["cumulative_signed_energy_j"]), -2
        )

        saved_summary = json.loads(
            (self.output / "summary.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(saved_summary, returned_summary)
        self.assertNotIn(
            "samples", saved_summary["calculation"]
        )
        self.assertEqual(
            saved_summary["source_metadata"],
            self.document,
        )
        self.assertEqual(
            saved_summary["export"]["analysis_csv_fields"],
            list(ANALYSIS_FIELDS),
        )

    def test_source_recording_is_unchanged(self):
        before = {
            path.name: path.read_bytes()
            for path in self.source.iterdir()
        }

        export_analysis(self.source, self.output)

        after = {
            path.name: path.read_bytes()
            for path in self.source.iterdir()
        }
        self.assertEqual(before, after)

    def test_existing_destination_is_not_overwritten(self):
        self.output.mkdir()
        sentinel = self.output / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")

        with self.assertRaises(FileExistsError):
            export_analysis(self.source, self.output)

        self.assertEqual(
            sentinel.read_text(encoding="utf-8"), "keep"
        )
        self.assertEqual(
            {path.name for path in self.output.iterdir()},
            {"keep.txt"},
        )

    def test_empty_existing_destination_is_rejected(self):
        self.output.mkdir()

        with self.assertRaises(FileExistsError):
            export_analysis(self.source, self.output)

        self.assertEqual(list(self.output.iterdir()), [])

    def test_destination_inside_source_is_rejected(self):
        nested = self.source / "derived"

        with self.assertRaisesRegex(
            ValueError, "outside"
        ):
            export_analysis(self.source, nested)

        self.assertFalse(nested.exists())

        with self.assertRaisesRegex(
            ValueError, "outside"
        ):
            export_analysis(self.source, self.source)

    def test_invalid_source_creates_no_output(self):
        self.rows[1][5] = 99
        self.save()

        with self.assertRaisesRegex(
            ValueError, "validation failed"
        ):
            export_analysis(self.source, self.output)

        self.assertFalse(self.output.exists())

    def test_interrupted_outcome_and_warnings_are_preserved(self):
        self.document.pop("record_count")
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
        self.save()

        summary = export_analysis(
            self.source, self.output
        )

        self.assertEqual(
            summary["acquisition_outcome"]["state"],
            "raised",
        )
        self.assertTrue(summary["warnings"])
        self.assertTrue(summary["validation"]["valid"])

    def test_missing_parent_creates_no_output(self):
        destination = (
            self.root / "missing-parent" / "analysis"
        )

        with self.assertRaises(FileNotFoundError):
            export_analysis(self.source, destination)

        self.assertFalse(destination.parent.exists())

    def test_cli_success(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([
                str(self.source),
                str(self.output),
            ])

        self.assertEqual(code, 0)
        self.assertIn("Samples: 2", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_cli_failure_returns_one(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([
                str(self.root / "missing"),
                str(self.output),
            ])

        self.assertEqual(code, 1)
        self.assertIn(
            "Analysis export failed", stderr.getvalue()
        )
        self.assertFalse(self.output.exists())

    def test_write_failure_returns_one(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch(
            "analysis.export_recorded_analysis.csv.DictWriter",
            side_effect=OSError("simulated write failure"),
        ):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main([
                    str(self.source),
                    str(self.output),
                ])

        self.assertEqual(code, 1)
        self.assertIn(
            "simulated write failure",
            stderr.getvalue(),
        )
        self.assertFalse(
            (self.output / "summary.json").exists()
        )


if __name__ == "__main__":
    unittest.main()