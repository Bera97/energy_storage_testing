"""Offline tests for persistent experiment-level lifecycle."""

import json
import unittest
from unittest.mock import patch

import test_recorded_preconditioning_integration as integration_tests

from recording.preconditioning_experiment import (
    PreconditioningExperiment,
    run_recorded_preconditioning,
)


class PreconditioningExperimentTests(unittest.TestCase):
    def setUp(self):
        self.integration = (
            integration_tests.RecordedPreconditioningIntegrationTests(
                methodName=(
                    "test_two_cycles_complete_through_concrete_executor"
                )
            )
        )
        self.integration.setUp()
        self.addCleanup(self.integration.doCleanups)

        self.executor_fixture = self.integration.fixture
        self.root = (
            self.executor_fixture.recording_fixture.root
        )
        self.experiment_id = "experiment_manifest_test"

    def run_experiment(self):
        return run_recorded_preconditioning(
            self.executor_fixture.document,
            self.integration.backend,
            self.root,
            self.experiment_id,
            metadata={
                "purpose": "offline integration test",
            },
        )

    def directory(self):
        return self.root / self.experiment_id

    def manifest(self):
        return json.loads(
            (
                self.directory() / "experiment.json"
            ).read_text(encoding="utf-8")
        )

    def test_completed_experiment_is_persisted(self):
        report, directory = self.run_experiment()

        self.assertEqual(directory, self.directory())
        document = self.manifest()

        self.assertEqual(document["state"], "completed")
        self.assertEqual(
            document["experiment_report"], report
        )
        self.assertEqual(
            report["completed_phase_count"], 8
        )
        self.assertEqual(
            len(document["backend_returns"]), 8
        )
        self.assertEqual(
            len(document["backend_evidence"]), 8
        )

    def test_progress_entries_do_not_claim_runner_completion(self):
        self.run_experiment()
        document = self.manifest()

        self.assertTrue(
            all(
                entry["state"] == "backend_returned"
                for entry in document["backend_returns"]
            )
        )
        self.assertEqual(
            [
                entry["phase_index"]
                for entry in document["backend_returns"]
            ],
            list(range(8)),
        )

    def test_second_cycle_failure_is_persisted(self):
        original = OSError(
            "synthetic second-cycle activation failure"
        )
        self.integration.failure_phase_index = 4
        self.integration.phase_error = original

        with self.assertRaises(OSError) as caught:
            self.run_experiment()

        self.assertIs(caught.exception, original)
        self.assertEqual(
            original.experiment_directory,
            self.directory(),
        )

        document = self.manifest()
        self.assertEqual(document["state"], "raised")
        self.assertEqual(
            document["error"]["type"], "OSError"
        )
        self.assertEqual(
            document["experiment_report"][
                "completed_phase_count"
            ],
            4,
        )
        self.assertTrue(
            document["experiment_report"][
                "shutdown_verified"
            ]
        )
        self.assertEqual(
            len(document["backend_returns"]), 4
        )

    def test_progress_write_failure_triggers_runner_cleanup(self):
        original = OSError(
            "synthetic progress write failure"
        )
        normal_replace = PreconditioningExperiment._replace

        def replace(experiment, document):
            if (
                document["state"] == "running"
                and len(document["backend_returns"]) == 1
            ):
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
        self.assertTrue(
            original.experiment_report[
                "shutdown_verified"
            ]
        )
        self.assertEqual(
            self.integration.executed_indices,
            [0],
        )

        document = self.manifest()
        self.assertEqual(document["state"], "raised")

        # The phase recording can be complete even though the
        # overall experiment stopped on progress-export failure.
        self.assertTrue(
            (
                self.integration.directory(0)
                / "COMPLETE"
            ).is_file()
        )

    def test_failure_manifest_error_preserves_original(self):
        original = TimeoutError(
            "synthetic primary activation failure"
        )
        self.integration.failure_phase_index = 0
        self.integration.phase_error = original

        with patch.object(
            PreconditioningExperiment,
            "fail",
            side_effect=OSError(
                "synthetic experiment export failure"
            ),
        ):
            with self.assertRaises(TimeoutError) as caught:
                self.run_experiment()

        self.assertIs(caught.exception, original)
        self.assertTrue(
            any(
                "experiment export failure" in note
                for note in original.__notes__
            )
        )
        self.assertEqual(
            self.manifest()["state"], "running"
        )

    def test_existing_experiment_directory_prevents_backend_execution(
        self,
    ):
        self.directory().mkdir()
        self.integration.executed_indices.clear()
        self.executor_fixture.events.clear()

        with self.assertRaises(FileExistsError):
            self.run_experiment()

        self.assertEqual(
            self.integration.executed_indices, []
        )
        self.assertEqual(
            self.executor_fixture.events, []
        )

    def test_invalid_configuration_creates_no_manifest(self):
        self.executor_fixture.document[
            "experiment"
        ]["cycles"] = 0

        with self.assertRaises(ValueError):
            self.run_experiment()

        self.assertFalse(self.directory().exists())
        self.assertEqual(
            self.integration.executed_indices, []
        )

    def test_completed_manifest_is_strict_json_compatible(self):
        self.run_experiment()
        document = self.manifest()

        self.assertEqual(
            json.loads(
                json.dumps(document, allow_nan=False)
            ),
            document,
        )


if __name__ == "__main__":
    unittest.main()