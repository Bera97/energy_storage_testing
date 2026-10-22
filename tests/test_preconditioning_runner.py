"""Offline state-machine tests using a fake backend."""

import json
import unittest

import test_preconditioning_plan as plan_tests

from experiments.preconditioning_runner import (
    OutputOffVerificationError,
    PhaseResult,
    PhaseTerminationError,
    run_preconditioning,
)


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.shutdown_count = 0
        self.shutdown_failures = {}
        self.phase_error = None
        self.rest_error = None
        self.charge_reason = "cv_current_termination"
        self.discharge_reason = "voltage_min"
        self.output_off = False
        self.mutate_phase = False

    def shutdown_verified(self):
        self.shutdown_count += 1
        self.calls.append(("shutdown", self.shutdown_count))

        failure = self.shutdown_failures.get(self.shutdown_count)

        if isinstance(failure, BaseException):
            raise failure

        if failure is False:
            self.output_off = False
            return False

        self.output_off = True
        return True

    def charge(self, phase):
        self.calls.append(("charge", phase["phase_index"]))
        self.output_off = False

        if self.mutate_phase:
            phase["target_voltage_v"] = -999

        if self.phase_error is not None:
            raise self.phase_error

        return PhaseResult(self.charge_reason)

    def discharge(self, phase):
        self.calls.append(("discharge", phase["phase_index"]))
        self.output_off = False

        if self.phase_error is not None:
            raise self.phase_error

        return PhaseResult(self.discharge_reason)

    def rest(self, phase):
        self.calls.append(("rest", phase["phase_index"]))

        if not self.output_off:
            raise AssertionError("Rest began without output OFF.")

        if self.rest_error is not None:
            raise self.rest_error


class PreconditioningRunnerTests(unittest.TestCase):
    def setUp(self):
        fixture = plan_tests.PreconditioningPlanTests(
            methodName="test_two_cycles_have_eight_ordered_phases"
        )
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)

        self.document = fixture.document
        self.backend = FakeBackend()

    def test_two_cycles_complete_in_order(self):
        report = run_preconditioning(
            self.document, self.backend
        )

        self.assertEqual(report["state"], "completed")
        self.assertEqual(report["completed_phase_count"], 8)
        self.assertIsNone(report["active_phase_index"])
        self.assertTrue(report["shutdown_verified"])
        self.assertIsNone(report["error"])

        operations = [
            name
            for name, _ in self.backend.calls
            if name != "shutdown"
        ]
        self.assertEqual(
            operations,
            ["charge", "rest", "discharge", "rest"] * 2,
        )
        self.assertTrue(
            all(
                phase["state"] == "completed"
                and phase["shutdown_verified"] is True
                for phase in report["phases"]
            )
        )

        # One initial shutdown plus one after every phase.
        self.assertEqual(self.backend.shutdown_count, 9)

    def test_invalid_config_never_calls_backend(self):
        self.document["experiment"]["cycles"] = 0

        with self.assertRaises(ValueError):
            run_preconditioning(self.document, self.backend)

        self.assertEqual(self.backend.calls, [])

    def test_initial_shutdown_failure_prevents_charge(self):
        original = RuntimeError("initial shutdown failed")
        self.backend.shutdown_failures[1] = original

        with self.assertRaises(RuntimeError) as caught:
            run_preconditioning(self.document, self.backend)

        self.assertIs(caught.exception, original)
        self.assertEqual(
            [name for name, _ in self.backend.calls],
            ["shutdown", "shutdown"],
        )
        self.assertTrue(
            original.experiment_report["shutdown_verified"]
        )
        self.assertEqual(
            original.experiment_report["completed_phase_count"],
            0,
        )

    def test_false_shutdown_verification_is_rejected(self):
        self.backend.shutdown_failures[1] = False

        with self.assertRaises(OutputOffVerificationError):
            run_preconditioning(self.document, self.backend)

        self.assertFalse(
            any(
                name == "charge"
                for name, _ in self.backend.calls
            )
        )

    def test_charge_timeout_stops_all_later_phases(self):
        original = TimeoutError("charge timed out")
        self.backend.phase_error = original

        with self.assertRaises(TimeoutError) as caught:
            run_preconditioning(self.document, self.backend)

        self.assertIs(caught.exception, original)
        self.assertEqual(
            [name for name, _ in self.backend.calls],
            ["shutdown", "charge", "shutdown"],
        )
        report = original.experiment_report
        self.assertEqual(report["state"], "raised")
        self.assertEqual(report["completed_phase_count"], 0)
        self.assertEqual(report["phases"][0]["state"], "raised")
        self.assertTrue(report["shutdown_verified"])

    def test_unexpected_charge_reason_stops_sequence(self):
        self.backend.charge_reason = "completed"

        with self.assertRaises(PhaseTerminationError):
            run_preconditioning(self.document, self.backend)

        self.assertFalse(
            any(
                name == "rest"
                for name, _ in self.backend.calls
            )
        )

    def test_unexpected_discharge_reason_stops_sequence(self):
        self.backend.discharge_reason = "compliance"

        with self.assertRaises(PhaseTerminationError) as caught:
            run_preconditioning(self.document, self.backend)

        report = caught.exception.experiment_report
        self.assertEqual(report["completed_phase_count"], 2)
        self.assertEqual(report["active_phase_index"], 2)
        self.assertEqual(len(report["phases"]), 3)

    def test_post_charge_shutdown_failure_prevents_rest(self):
        original = RuntimeError("post-charge shutdown failed")
        self.backend.shutdown_failures[2] = original

        with self.assertRaises(RuntimeError) as caught:
            run_preconditioning(self.document, self.backend)

        self.assertIs(caught.exception, original)
        self.assertFalse(
            any(
                name == "rest"
                for name, _ in self.backend.calls
            )
        )
        report = original.experiment_report
        self.assertEqual(report["completed_phase_count"], 0)
        self.assertEqual(report["state"], "raised")
        self.assertTrue(report["shutdown_verified"])

    def test_interrupt_during_rest_prevents_discharge(self):
        original = KeyboardInterrupt()
        self.backend.rest_error = original

        with self.assertRaises(KeyboardInterrupt) as caught:
            run_preconditioning(self.document, self.backend)

        self.assertIs(caught.exception, original)
        self.assertFalse(
            any(
                name == "discharge"
                for name, _ in self.backend.calls
            )
        )
        self.assertEqual(
            original.experiment_report["completed_phase_count"],
            1,
        )
        self.assertTrue(
            original.experiment_report["shutdown_verified"]
        )

    def test_cleanup_failure_preserves_original_interrupt(self):
        original = KeyboardInterrupt()
        self.backend.phase_error = original
        self.backend.shutdown_failures[2] = RuntimeError(
            "cleanup failed"
        )

        with self.assertRaises(KeyboardInterrupt) as caught:
            run_preconditioning(self.document, self.backend)

        self.assertIs(caught.exception, original)
        report = original.experiment_report
        self.assertFalse(report["shutdown_verified"])
        self.assertEqual(
            report["cleanup_error"]["type"], "RuntimeError"
        )
        self.assertTrue(original.__notes__)

    def test_second_interrupt_does_not_replace_first_error(self):
        original = TimeoutError("charge timeout")
        self.backend.phase_error = original
        self.backend.shutdown_failures[2] = KeyboardInterrupt()

        with self.assertRaises(TimeoutError) as caught:
            run_preconditioning(self.document, self.backend)

        self.assertIs(caught.exception, original)
        self.assertEqual(
            original.experiment_report["cleanup_error"]["type"],
            "KeyboardInterrupt",
        )

    def test_backend_cannot_mutate_stored_plan_phase(self):
        self.backend.mutate_phase = True

        report = run_preconditioning(
            self.document, self.backend
        )

        self.assertEqual(
            report["plan"]["phases"][0]["target_voltage_v"],
            4.1,
        )
        self.assertEqual(
            self.document["charge"]["target_voltage_v"],
            4.1,
        )

    def test_completed_report_is_strict_json_compatible(self):
        report = run_preconditioning(
            self.document, self.backend
        )

        self.assertEqual(
            json.loads(json.dumps(report, allow_nan=False)),
            report,
        )

    def test_failure_report_is_strict_json_compatible(self):
        original = TimeoutError("charge timeout")
        self.backend.phase_error = original

        with self.assertRaises(TimeoutError):
            run_preconditioning(self.document, self.backend)

        report = original.experiment_report
        self.assertEqual(
            json.loads(json.dumps(report, allow_nan=False)),
            report,
        )


if __name__ == "__main__":
    unittest.main()