"""Offline tests for read-only experiment validation."""

import json
import unittest

import test_preconditioning_experiment as experiment_tests

from analysis.validate_preconditioning_experiment import (
    validate_experiment,
)


class ValidatePreconditioningExperimentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = (
            experiment_tests.PreconditioningExperimentTests(
                methodName="test_completed_experiment_is_persisted"
            )
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def create_completed(self):
        self.fixture.run_experiment()

    def manifest_path(self):
        return self.fixture.directory() / "experiment.json"

    def read_manifest(self):
        return json.loads(
            self.manifest_path().read_text(
                encoding="utf-8"
            )
        )

    def write_manifest(self, document):
        self.manifest_path().write_text(
            json.dumps(document, indent=2),
            encoding="utf-8",
        )

    def validate(self):
        return validate_experiment(
            self.fixture.directory(),
            phase_root=self.fixture.root,
        )

    def test_completed_experiment_validates(self):
        self.create_completed()

        result = self.validate()

        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["state"], "completed")
        self.assertEqual(
            result["phase_recordings_checked"], 4
        )
        self.assertEqual(result["warnings"], [])

    def test_validation_does_not_modify_manifest(self):
        self.create_completed()
        before = self.manifest_path().read_bytes()

        self.validate()

        self.assertEqual(
            self.manifest_path().read_bytes(),
            before,
        )

    def test_missing_phase_complete_marker_is_rejected(self):
        self.create_completed()
        document = self.read_manifest()
        reference = document["backend_evidence"][0][
            "run_directory"
        ]

        from pathlib import Path

        (Path(reference) / "COMPLETE").unlink()

        result = self.validate()

        self.assertFalse(result["valid"])
        self.assertTrue(result["errors"])

    def test_duplicate_phase_reference_is_rejected(self):
        self.create_completed()
        document = self.read_manifest()

        document["backend_evidence"][2]["run_directory"] = (
            document["backend_evidence"][0]["run_directory"]
        )
        self.write_manifest(document)

        result = self.validate()

        self.assertFalse(result["valid"])
        self.assertTrue(
            any(
                "duplicates" in message
                for message in result["errors"]
            )
        )

    def test_relative_reference_is_not_guessed(self):
        self.create_completed()
        document = self.read_manifest()
        document["backend_evidence"][0][
            "run_directory"
        ] = "relative_phase_directory"
        self.write_manifest(document)

        result = self.validate()

        self.assertFalse(result["valid"])
        self.assertTrue(
            any(
                "must be absolute" in message
                for message in result["errors"]
            )
        )

    def test_reference_outside_allowed_root_is_rejected(self):
        self.create_completed()

        result = validate_experiment(
            self.fixture.directory(),
            phase_root=self.fixture.directory(),
        )

        self.assertFalse(result["valid"])
        self.assertTrue(
            any(
                "outside the allowed phase root" in message
                for message in result["errors"]
            )
        )

    def test_wrong_stored_phase_is_rejected(self):
        self.create_completed()
        phase_directory = self.fixture.integration.directory(0)
        path = phase_directory / "metadata.json"

        document = json.loads(
            path.read_text(encoding="utf-8")
        )
        document["experiment_phase"]["cycle"] = 999
        path.write_text(
            json.dumps(document, indent=2),
            encoding="utf-8",
        )

        result = self.validate()

        self.assertFalse(result["valid"])
        self.assertTrue(
            any(
                "stored field differs" in message
                for message in result["errors"]
            )
        )

    def test_running_manifest_is_not_reported_as_completed(self):
        integration = self.fixture.integration
        integration.failure_phase_index = 0
        integration.phase_error = RuntimeError(
            "synthetic activation failure"
        )

        from unittest.mock import patch
        from recording.preconditioning_experiment import (
            PreconditioningExperiment,
        )

        with patch.object(
            PreconditioningExperiment,
            "fail",
            side_effect=OSError("manifest unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                self.fixture.run_experiment()

        result = self.validate()

        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["state"], "running")
        self.assertTrue(result["warnings"])


if __name__ == "__main__":
    unittest.main()