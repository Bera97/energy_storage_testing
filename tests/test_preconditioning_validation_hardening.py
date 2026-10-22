"""Offline tests for strict experiment evidence parsing."""

import json
import unittest

import test_preconditioning_experiment as experiment_tests

from analysis.validate_preconditioning_experiment import (
    validate_experiment,
)


class PreconditioningValidationHardeningTests(unittest.TestCase):
    def setUp(self):
        self.fixture = (
            experiment_tests.PreconditioningExperimentTests(
                methodName="test_completed_experiment_is_persisted"
            )
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.run_experiment()

    def manifest_path(self):
        return (
            self.fixture.directory()
            / "experiment.json"
        )

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

    def test_duplicate_manifest_key_is_rejected(self):
        text = self.manifest_path().read_text(
            encoding="utf-8"
        )
        text = text.replace(
            "{",
            '{"state": "completed",',
            1,
        )
        self.manifest_path().write_text(
            text,
            encoding="utf-8",
        )

        result = self.validate()

        self.assertFalse(result["valid"])
        self.assertTrue(
            any(
                "Duplicate JSON object key" in message
                for message in result["errors"]
            )
        )

    def test_nonfinite_manifest_value_is_rejected(self):
        document = self.read_manifest()
        document["metadata"]["invalid_value"] = float("nan")
        self.write_manifest(document)

        result = self.validate()

        self.assertFalse(result["valid"])
        self.assertTrue(
            any(
                "Non-finite JSON constant" in message
                for message in result["errors"]
            )
        )

    def test_unknown_planned_phase_is_rejected_without_crashing(self):
        document = self.read_manifest()
        document["plan"]["phases"][0]["type"] = "unknown"
        self.write_manifest(document)

        result = self.validate()

        self.assertFalse(result["valid"])
        self.assertTrue(
            any(
                "order or identity" in message
                for message in result["errors"]
            )
        )

    def test_lost_serialization_evidence_is_not_accepted(self):
        document = self.read_manifest()
        document["backend_evidence_error"] = (
            "ValueError: evidence could not be serialized"
        )
        self.write_manifest(document)

        result = self.validate()

        self.assertFalse(result["valid"])
        self.assertTrue(
            any(
                "lost or invalid evidence" in message
                for message in result["errors"]
            )
        )

    def test_missing_backend_evidence_is_rejected(self):
        document = self.read_manifest()
        document["backend_evidence"] = []
        self.write_manifest(document)

        result = self.validate()

        self.assertFalse(result["valid"])
        self.assertTrue(
            any(
                "missing for recorded backend returns" in message
                for message in result["errors"]
            )
        )

    def test_nonfinite_phase_metadata_is_rejected(self):
        path = (
            self.fixture.integration.directory(0)
            / "metadata.json"
        )
        document = json.loads(
            path.read_text(encoding="utf-8")
        )
        document["invalid_value"] = float("inf")
        path.write_text(
            json.dumps(document, indent=2),
            encoding="utf-8",
        )

        result = self.validate()

        self.assertFalse(result["valid"])
        self.assertTrue(
            any(
                "Non-finite JSON constant" in message
                for message in result["errors"]
            )
        )

    def test_recording_outcome_must_match_transport_trigger(self):
        path = (
            self.fixture.integration.directory(0)
            / "metadata.json"
        )
        document = json.loads(
            path.read_text(encoding="utf-8")
        )
        document["outcome"]["triggering_record_index"] = None
        document["outcome"]["triggering_reading"] = None
        path.write_text(
            json.dumps(document, indent=2),
            encoding="utf-8",
        )

        result = self.validate()

        self.assertFalse(result["valid"])
        self.assertTrue(
            any(
                "trigger evidence differ" in message
                for message in result["errors"]
            )
        )


if __name__ == "__main__":
    unittest.main()