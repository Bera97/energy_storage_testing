"""Offline tests for export-destination preflight."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from drivers.run_preflight import prepare_run_destination


class RunPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name) / "runs"

    def test_valid_destination_checked_but_not_reserved(self):
        destination = prepare_run_destination(
            self.base,
            "valid_run",
            metadata={"instrument": "fake"},
        )

        self.assertEqual(destination, self.base / "valid_run")
        self.assertTrue(self.base.is_dir())
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.base.iterdir()), [])

    def test_existing_run_is_rejected_without_changes(self):
        destination = self.base / "existing"
        destination.mkdir(parents=True)
        evidence = destination / "evidence.txt"
        evidence.write_text("preserve me", encoding="utf-8")

        with self.assertRaises(FileExistsError):
            prepare_run_destination(
                self.base,
                "existing",
                metadata={},
            )

        self.assertEqual(
            evidence.read_text(encoding="utf-8"),
            "preserve me",
        )

    def test_invalid_run_id_rejected_before_directory_creation(self):
        for run_id in ("", "../escape", "a/b", "a\\b"):
            with self.subTest(run_id=run_id):
                with self.assertRaises(ValueError):
                    prepare_run_destination(
                        self.base,
                        run_id,
                        metadata={},
                    )

        self.assertFalse(self.base.exists())

    def test_invalid_metadata_rejected_before_directory_creation(self):
        for metadata in (
            {"invalid": float("nan")},
            {"invalid": object()},
            [],
        ):
            with self.subTest(metadata=metadata):
                with self.assertRaises((TypeError, ValueError)):
                    prepare_run_destination(
                        self.base,
                        "invalid_metadata",
                        metadata=metadata,
                    )

        self.assertFalse(self.base.exists())

    def test_probe_failure_propagates_without_creating_run(self):
        with patch(
            "drivers.run_preflight.tempfile.TemporaryFile",
            side_effect=PermissionError("simulated write failure"),
        ):
            with self.assertRaises(PermissionError):
                prepare_run_destination(
                    self.base,
                    "blocked",
                    metadata={},
                )

        self.assertFalse((self.base / "blocked").exists())


if __name__ == "__main__":
    unittest.main()