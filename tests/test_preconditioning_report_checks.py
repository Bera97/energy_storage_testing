"""Offline tests for persisted runner-report consistency."""

from copy import deepcopy
import unittest

import test_preconditioning_runner as runner_tests

from analysis.preconditioning_report_checks import (
    check_runner_report,
)
from experiments.preconditioning_runner import (
    run_preconditioning,
)


class PreconditioningReportChecksTests(unittest.TestCase):
    def setUp(self):
        self.fixture = runner_tests.PreconditioningRunnerTests(
            methodName="test_two_cycles_complete_in_order"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def completed_report(self):
        return run_preconditioning(
            self.fixture.document,
            self.fixture.backend,
        )

    def failed_report(self, *, cleanup_fails=False):
        original = TimeoutError(
            "synthetic phase timeout"
        )
        self.fixture.backend.phase_error = original

        if cleanup_fails:
            self.fixture.backend.shutdown_failures[2] = OSError(
                "synthetic cleanup failure"
            )

        with self.assertRaises(TimeoutError):
            run_preconditioning(
                self.fixture.document,
                self.fixture.backend,
            )

        return original.experiment_report

    def test_actual_completed_report_is_accepted(self):
        report = self.completed_report()

        self.assertEqual(
            check_runner_report(report["plan"], report),
            [],
        )

    def test_actual_failed_report_is_accepted(self):
        report = self.failed_report()

        self.assertEqual(
            check_runner_report(report["plan"], report),
            [],
        )

    def test_actual_cleanup_failure_report_is_accepted(self):
        report = self.failed_report(cleanup_fails=True)

        self.assertEqual(
            check_runner_report(report["plan"], report),
            [],
        )

    def test_completed_phase_requires_correct_stop_reason(self):
        report = self.completed_report()
        report["phases"][0]["stop_reason"] = "completed"

        errors = check_runner_report(
            report["plan"],
            report,
        )

        self.assertTrue(
            any(
                "wrong stop reason" in message
                for message in errors
            )
        )

    def test_completed_count_must_match_outcomes(self):
        report = self.completed_report()
        report["completed_phase_count"] = 7

        errors = check_runner_report(
            report["plan"],
            report,
        )

        self.assertTrue(
            any(
                "completed-phase count" in message
                for message in errors
            )
        )

    def test_failed_report_requires_matching_active_index(self):
        report = self.failed_report()
        report["active_phase_index"] = 2

        errors = check_runner_report(
            report["plan"],
            report,
        )

        self.assertTrue(
            any(
                "active phase" in message
                for message in errors
            )
        )

    def test_failed_shutdown_requires_cleanup_error(self):
        report = self.failed_report(cleanup_fails=True)
        report["cleanup_error"] = None

        errors = check_runner_report(
            report["plan"],
            report,
        )

        self.assertTrue(
            any(
                "cleanup requires" in message
                for message in errors
            )
        )

    def test_phase_identity_boolean_is_not_an_integer_index(self):
        report = self.completed_report()
        report["phases"][0]["phase_index"] = False

        errors = check_runner_report(
            report["plan"],
            report,
        )

        self.assertTrue(
            any(
                "identity differs" in message
                for message in errors
            )
        )

    def test_runner_report_plan_must_match_manifest_plan(self):
        report = self.completed_report()
        different_plan = deepcopy(report["plan"])
        different_plan["phases"][0]["cycle"] = 999

        errors = check_runner_report(
            different_plan,
            report,
        )

        self.assertTrue(
            any(
                "plan differs" in message
                for message in errors
            )
        )


if __name__ == "__main__":
    unittest.main()