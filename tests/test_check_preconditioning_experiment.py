"""Offline tests for the read-only experiment validation CLI."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

import test_preconditioning_experiment as experiment_tests

from analysis.check_preconditioning_experiment import main


class CheckPreconditioningExperimentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = (
            experiment_tests.PreconditioningExperimentTests(
                methodName="test_completed_experiment_is_persisted"
            )
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def invoke(self, *extra):
        output = io.StringIO()
        errors = io.StringIO()

        arguments = [
            str(self.fixture.directory()),
            "--phase-root",
            str(self.fixture.root),
            *extra,
        ]

        with redirect_stdout(output), redirect_stderr(errors):
            exit_code = main(arguments)

        return exit_code, output.getvalue(), errors.getvalue()

    def test_completed_experiment_returns_zero(self):
        self.fixture.run_experiment()

        exit_code, output, errors = self.invoke()

        self.assertEqual(exit_code, 0)
        self.assertIn("Evidence valid: yes", output)
        self.assertIn(
            "Experiment state: completed",
            output,
        )
        self.assertEqual(errors, "")

    def test_json_output_is_machine_readable(self):
        self.fixture.run_experiment()

        exit_code, output, errors = self.invoke("--json")
        document = json.loads(output)

        self.assertEqual(exit_code, 0)
        self.assertTrue(document["valid"])
        self.assertEqual(
            document["state"],
            "completed",
        )
        self.assertEqual(
            document["phase_recordings_checked"],
            4,
        )
        self.assertEqual(errors, "")

    def test_missing_manifest_returns_one(self):
        exit_code, output, _ = self.invoke("--json")
        document = json.loads(output)

        self.assertEqual(exit_code, 1)
        self.assertFalse(document["valid"])
        self.assertTrue(document["errors"])

    def test_invalid_manifest_returns_one(self):
        self.fixture.directory().mkdir()
        path = self.fixture.directory() / "experiment.json"
        path.write_text(
            '{"unfinished":',
            encoding="utf-8",
        )

        exit_code, output, _ = self.invoke("--json")
        document = json.loads(output)

        self.assertEqual(exit_code, 1)
        self.assertFalse(document["valid"])

    def test_consistent_failed_experiment_returns_three(self):
        integration = self.fixture.integration
        integration.failure_phase_index = 0
        integration.phase_error = RuntimeError(
            "synthetic activation failure"
        )

        with self.assertRaises(RuntimeError):
            self.fixture.run_experiment()

        exit_code, output, _ = self.invoke("--json")
        document = json.loads(output)

        self.assertEqual(exit_code, 3)
        self.assertTrue(
            document["valid"],
            document["errors"],
        )
        self.assertEqual(
            document["state"],
            "raised",
        )

    def test_validator_exception_is_reported_as_failure(self):
        with patch(
            (
                "analysis.check_preconditioning_experiment."
                "validate_experiment"
            ),
            side_effect=OSError(
                "synthetic validation read failure"
            ),
        ):
            exit_code, output, _ = self.invoke("--json")

        document = json.loads(output)

        self.assertEqual(exit_code, 1)
        self.assertFalse(document["valid"])
        self.assertTrue(
            any(
                "validation read failure" in message
                for message in document["errors"]
            )
        )

    def test_cli_does_not_modify_recordings(self):
        self.fixture.run_experiment()

        before = {
            str(path.relative_to(self.fixture.root)):
                path.read_bytes()
            for path in self.fixture.root.rglob("*")
            if path.is_file()
        }

        exit_code, _, _ = self.invoke("--json")

        after = {
            str(path.relative_to(self.fixture.root)):
                path.read_bytes()
            for path in self.fixture.root.rglob("*")
            if path.is_file()
        }

        self.assertEqual(exit_code, 0)
        self.assertEqual(after, before)

    def test_missing_arguments_return_usage_error(self):
        output = io.StringIO()
        errors = io.StringIO()

        with redirect_stdout(output), redirect_stderr(errors):
            with self.assertRaises(SystemExit) as caught:
                main([])

        self.assertEqual(caught.exception.code, 2)
        self.assertIn(
            "usage:",
            errors.getvalue().lower(),
        )


if __name__ == "__main__":
    unittest.main()