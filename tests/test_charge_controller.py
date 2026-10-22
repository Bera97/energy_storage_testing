"""Offline tests for CC-CV charge-termination decisions."""

from copy import deepcopy
import unittest

from experiments.charge_controller import (
    ChargeController,
    ChargeFaultError,
    ChargePolarityError,
)


class ChargeControllerTests(unittest.TestCase):
    def setUp(self):
        self.phase = {
            "type": "charge_cc_cv",
            "current_magnitude_a": 1.0,
            "termination_current_a": 0.1,
            "termination_hold_time_s": 10.0,
            "termination_requires_cv": True,
            "maximum_duration_s": 100.0,
            "maximum_cv_duration_s": 40.0,
        }
        self.controller = ChargeController(self.phase)

    def observe(self, elapsed, current, cv, **changes):
        return self.controller.update(
            elapsed_s=elapsed,
            current_a=current,
            cv_active=cv,
            **changes,
        )

    def test_low_current_in_cc_does_not_terminate(self):
        first = self.observe(0, 0.05, False)
        last = self.observe(20, 0.05, False)

        self.assertEqual(first.state, "cc")
        self.assertEqual(last.state, "cc")
        self.assertEqual(last.qualifying_hold_s, 0)
        self.assertIsNone(last.stop_reason)
        self.assertIsNone(last.first_cv_elapsed_s)

    def test_normal_cc_cv_and_hold(self):
        self.assertEqual(self.observe(0, 1.0, False).state, "cc")
        self.assertEqual(self.observe(10, 0.5, True).state, "cv")

        start = self.observe(20, 0.1, True)
        almost = self.observe(29, 0.09, True)
        complete = self.observe(30, 0.08, True)

        self.assertEqual(start.state, "cv_hold")
        self.assertEqual(start.qualifying_hold_s, 0)
        self.assertIsNone(almost.stop_reason)
        self.assertEqual(complete.state, "completed")
        self.assertEqual(
            complete.stop_reason, "cv_current_termination"
        )
        self.assertEqual(complete.first_cv_elapsed_s, 10)
        self.assertEqual(complete.qualifying_hold_s, 10)

    def test_current_rise_resets_hold(self):
        self.observe(0, 0.09, True)
        self.observe(9, 0.09, True)

        reset = self.observe(10, 0.11, True)
        restarted = self.observe(11, 0.09, True)
        almost = self.observe(20, 0.09, True)
        complete = self.observe(21, 0.09, True)

        self.assertEqual(reset.qualifying_hold_s, 0)
        self.assertEqual(restarted.qualifying_hold_s, 0)
        self.assertIsNone(almost.stop_reason)
        self.assertEqual(complete.state, "completed")

    def test_leaving_cv_resets_hold(self):
        self.observe(0, 0.09, True)
        self.observe(8, 0.09, True)

        reset = self.observe(9, 0.09, False)
        restart = self.observe(10, 0.09, True)

        self.assertEqual(reset.state, "cc")
        self.assertEqual(restart.qualifying_hold_s, 0)
        self.assertEqual(restart.first_cv_elapsed_s, 0)

    def test_cv_timeout_does_not_reset_on_mode_change(self):
        self.observe(0, 1.0, False)
        self.observe(10, 0.5, True)
        self.observe(20, 1.0, False)

        with self.assertRaisesRegex(TimeoutError, "CV"):
            self.observe(50, 1.0, False)

        self.assertEqual(self.controller.state, "failed")

    def test_total_charge_timeout(self):
        self.observe(0, 1.0, False)

        with self.assertRaisesRegex(TimeoutError, "charge"):
            self.observe(100, 1.0, False)

    def test_timeout_has_priority_over_hold_completion(self):
        self.observe(0, 1.0, False)
        self.observe(90, 0.09, True)

        with self.assertRaises(TimeoutError):
            self.observe(100, 0.09, True)

        self.assertEqual(self.controller.state, "failed")

    def test_fault_prevents_termination(self):
        self.observe(0, 0.09, True)

        with self.assertRaises(ChargeFaultError):
            self.observe(10, 0.09, True, has_fault=True)

        self.assertEqual(self.controller.state, "failed")

    def test_negative_current_is_rejected(self):
        with self.assertRaises(ChargePolarityError):
            self.observe(0, -0.1, True)

    def test_completed_controller_cannot_be_reused(self):
        self.observe(0, 0.09, True)
        self.observe(10, 0.09, True)

        with self.assertRaises(RuntimeError):
            self.observe(11, 0.09, True)

    def test_failed_controller_cannot_be_reused(self):
        with self.assertRaises(ChargePolarityError):
            self.observe(0, -0.1, True)

        with self.assertRaises(RuntimeError):
            self.observe(1, 0.1, True)

    def test_elapsed_time_must_increase(self):
        for later in (0, -1):
            with self.subTest(later=later):
                controller = ChargeController(self.phase)
                controller.update(
                    elapsed_s=0,
                    current_a=1,
                    cv_active=False,
                )

                with self.assertRaises(ValueError):
                    controller.update(
                        elapsed_s=later,
                        current_a=1,
                        cv_active=False,
                    )

    def test_invalid_observations_are_rejected(self):
        cases = (
            {"elapsed_s": float("nan")},
            {"elapsed_s": True},
            {"current_a": float("inf")},
            {"current_a": float("nan")},
            {"current_a": True},
            {"cv_active": 1},
            {"has_fault": "False"},
        )

        for changes in cases:
            with self.subTest(changes=changes):
                controller = ChargeController(self.phase)
                observation = {
                    "elapsed_s": 0,
                    "current_a": 1,
                    "cv_active": False,
                    "has_fault": False,
                    **changes,
                }

                with self.assertRaises(ValueError):
                    controller.update(**observation)

                self.assertEqual(controller.state, "failed")

    def test_invalid_phase_settings_are_rejected(self):
        cases = (
            {"termination_requires_cv": False},
            {"termination_current_a": 1.0},
            {"termination_hold_time_s": 0},
            {"maximum_cv_duration_s": 101},
            {"termination_hold_time_s": 40},
            {"type": "discharge_cc"},
        )

        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    ChargeController({
                        **self.phase,
                        **changes,
                    })

    def test_phase_is_not_modified(self):
        before = deepcopy(self.phase)

        self.observe(0, 1, False)

        self.assertEqual(self.phase, before)


if __name__ == "__main__":
    unittest.main()