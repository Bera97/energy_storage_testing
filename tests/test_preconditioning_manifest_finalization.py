"""Offline tests for experiment manifest finalization failures.

No instrument connection or real output enabling.
"""

import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_preconditioning_experiment as experiment_tests

from recording.preconditioning_experiment import (
    PreconditioningExperiment,
)


class PreconditioningManifestFinalizationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = (
            experiment_tests.PreconditioningExperimentTests(
                methodName="test_completed_experiment_is_persisted"
            )
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def run_experiment(self):
        return self.fixture.run_experiment()

    def directory(self):
        return self.fixture.directory()

    def manifest(self):
        return self.fixture.manifest()

    def temporary_files(self):
        return list(
            self.directory().glob("experiment_*.tmp")
        )

    def test_final_export_failure_retains_completed_runner_report(self):
        original = OSError(
            "synthetic final manifest failure"
        )
        normal_replace = PreconditioningExperiment._replace

        def replace(experiment, document):
            if document["state"] == "completed":
                raise original

            return normal_replace(experiment, document)

        with patch.object(
            PreconditioningExperiment,
            "_replace",
            new=replace,
        ):
            with self.assertRaises(OSError) as caught:
                self.run_experiment()

        self.assertIs(caught.exception, original)
        self.assertEqual(
            original.experiment_report["state"],
            "completed",
        )
        self.assertEqual(
            original.experiment_report[
                "completed_phase_count"
            ],
            8,
        )
        self.assertTrue(
            original.experiment_report["shutdown_verified"]
        )

        document = self.manifest()

        self.assertEqual(document["state"], "raised")
        self.assertEqual(
            document["experiment_report"]["state"],
            "completed",
        )
        self.assertEqual(
            document["error"]["type"],
            "OSError",
        )
        self.assertTrue(
            any(
                "runner completed" in note.lower()
                for note in original.__notes__
            )
        )

    def test_final_export_and_failure_export_errors_preserve_original(self):
        original = OSError(
            "synthetic final export error"
        )
        normal_replace = PreconditioningExperiment._replace

        def replace(experiment, document):
            if document["state"] == "completed":
                raise original

            if document["state"] == "raised":
                raise OSError(
                    "synthetic failure export error"
                )

            return normal_replace(experiment, document)

        with patch.object(
            PreconditioningExperiment,
            "_replace",
            new=replace,
        ):
            with self.assertRaises(OSError) as caught:
                self.run_experiment()

        self.assertIs(caught.exception, original)
        self.assertEqual(
            original.experiment_report["state"],
            "completed",
        )
        self.assertEqual(
            original.experiment_directory,
            self.directory(),
        )

        document = self.manifest()

        self.assertEqual(document["state"], "running")
        self.assertEqual(
            len(document["backend_returns"]),
            8,
        )
        self.assertTrue(
            any(
                "failure export error" in note
                for note in original.__notes__
            )
        )

    def test_replace_failure_keeps_previous_manifest_until_failure_export(
        self,
    ):
        original = OSError(
            "synthetic atomic replacement failure"
        )
        normal_replace = Path.replace
        observed_previous = []

        def replace(path, target):
            target = Path(target)

            if (
                path.parent == self.directory()
                and path.name.startswith("experiment_")
                and path.suffix == ".tmp"
                and target.name == "experiment.json"
            ):
                staged = json.loads(
                    path.read_text(encoding="utf-8")
                )

                if staged["state"] == "completed":
                    observed_previous.append(
                        json.loads(
                            target.read_text(
                                encoding="utf-8"
                            )
                        )
                    )
                    raise original

            return normal_replace(path, target)

        with patch.object(
            Path,
            "replace",
            new=replace,
        ):
            with self.assertRaises(OSError) as caught:
                self.run_experiment()

        self.assertIs(caught.exception, original)
        self.assertEqual(len(observed_previous), 1)
        self.assertEqual(
            observed_previous[0]["state"],
            "running",
        )
        self.assertEqual(
            len(observed_previous[0]["backend_returns"]),
            8,
        )
        self.assertEqual(
            self.manifest()["state"],
            "raised",
        )
        self.assertEqual(self.temporary_files(), [])

    def test_interrupt_during_final_replace_retains_completed_report(self):
        original = KeyboardInterrupt(
            "synthetic final replacement interruption"
        )
        normal_replace = Path.replace

        def replace(path, target):
            if (
                path.parent == self.directory()
                and path.name.startswith("experiment_")
                and path.suffix == ".tmp"
            ):
                staged = json.loads(
                    path.read_text(encoding="utf-8")
                )

                if staged["state"] == "completed":
                    raise original

            return normal_replace(path, target)

        with patch.object(
            Path,
            "replace",
            new=replace,
        ):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.run_experiment()

        self.assertIs(caught.exception, original)
        self.assertEqual(
            original.experiment_report["state"],
            "completed",
        )
        self.assertTrue(
            original.experiment_report["shutdown_verified"]
        )
        self.assertEqual(
            self.manifest()["state"],
            "raised",
        )
        self.assertEqual(self.temporary_files(), [])

    def test_partial_temporary_write_does_not_corrupt_main_manifest(self):
        original = OSError(
            "synthetic partial temporary write"
        )
        normal_open = Path.open

        class WriteFailureHandle:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                self.handle.close()
                return False

            def write(self, text):
                document = json.loads(text)

                if document["state"] == "completed":
                    self.handle.write(text[:25])
                    raise original

                return self.handle.write(text)

        def open_file(path, *args, **kwargs):
            handle = normal_open(path, *args, **kwargs)
            mode = args[0] if args else kwargs.get("mode", "r")

            if (
                path.parent == self.directory()
                and path.name.startswith("experiment_")
                and path.suffix == ".tmp"
                and mode == "x"
            ):
                return WriteFailureHandle(handle)

            return handle

        with patch.object(
            Path,
            "open",
            new=open_file,
        ):
            with self.assertRaises(OSError) as caught:
                self.run_experiment()

        self.assertIs(caught.exception, original)

        # Reading succeeds only if the authoritative file is still
        # complete JSON rather than the partial temporary content.
        document = self.manifest()

        self.assertEqual(document["state"], "raised")
        self.assertEqual(
            document["experiment_report"]["state"],
            "completed",
        )
        self.assertEqual(self.temporary_files(), [])

    def test_incomplete_completion_report_is_rejected(self):
        experiment = PreconditioningExperiment(
            self.fixture.root,
            "incomplete_report_test",
            plan=(
                self.fixture.executor_fixture.executor.plan
            ),
            metadata={"purpose": "validation test"},
        )

        with self.assertRaises(ValueError):
            experiment.finish(
                {"state": "completed"},
                [],
            )

        self.assertFalse(experiment.finalized)

        document = json.loads(
            (
                experiment.directory / "experiment.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(document["state"], "running")


if __name__ == "__main__":
    unittest.main()