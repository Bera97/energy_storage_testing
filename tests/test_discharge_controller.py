"""Offline tests for constant-current discharge decisions."""

from copy import deepcopy
import unittest

from experiments.discharge_controller import (
    DischargeComplianceError,
    DischargeController,
    DischargeFaultError,
    DischargePolarityError,
    DischargeVoltageError,
)


class DischargeControllerTests(unittest.TestCase):
    def setUp(self):
        self.phase = {
            "type": "discharge_cc",
            "current_magnitude_a": 1.0,
            "cutoff_voltage_v": 2.7,
            "maximum_duration_s": 100.0,
        }
        self.controller = DischargeController(self.phase)

    def observe(self, elapsed, voltage, **changes):
        arguments = {
            "elapsed_s": elapsed,
            "voltage_v": voltage,
            "current_a": -1.0,
            "in_compliance": False,
            "has_fault": False,
            **changes,
        }

        return self.controller.update(**arguments)

    def test_above_cutoff_continues(self):
        decision = self.observe(0, 3.8)

        self.assertEqual(decision.state, "discharging")
        self.assertIsNone(decision.stop_reason)

    def test_equal_to_cutoff_completes(self):
        self.observe(0, 3.8)

        decision = self.observe(10, 2.7)

        self.assertEqual(decision.state, "completed")
        self.assertEqual(decision.stop_reason, "voltage_min")

    def test_below_cutoff_completes(self):
        decision = self.observe(0, 2.69)

        self.assertEqual(decision.stop_reason, "voltage_min")

    def test_timeout_takes_priority_over_cutoff(self):
        with self.assertRaises(TimeoutError):
            self.observe(100, 2.7)

        self.assertEqual(self.controller.state, "failed")

    def test_fault_at_cutoff_is_not_success(self):
        with self.assertRaises(DischargeFaultError):
            self.observe(0, 2.7, has_fault=True)

    def test_compliance_at_cutoff_is_not_success(self):
        with self.assertRaises(DischargeComplianceError):
            self.observe(0, 2.7, in_compliance=True)

    def test_positive_current_is_rejected(self):
        with self.assertRaises(DischargePolarityError):
            self.observe(0, 3.8, current_a=0.1)

    def test_negative_voltage_is_rejected(self):
        with self.assertRaises(DischargeVoltageError):
            self.observe(0, -0.01)

    def test_completed_controller_cannot_be_reused(self):
        self.observe(0, 2.7)

        with self.assertRaises(RuntimeError):
            self.observe(1, 2.6)

    def test_failed_controller_cannot_be_reused(self):
        with self.assertRaises(DischargePolarityError):
            self.observe(0, 3.8, current_a=0.1)

        with self.assertRaises(RuntimeError):
            self.observe(1, 3.7)

    def test_elapsed_time_must_increase(self):
        self.observe(1, 3.8)

        with self.assertRaises(ValueError):
            self.observe(1, 3.7)

    def test_invalid_observations_are_rejected(self):
        cases = (
            {"elapsed_s": -1},
            {"elapsed_s": True},
            {"elapsed_s": float("nan")},
            {"voltage_v": float("inf")},
            {"voltage_v": True},
            {"current_a": float("nan")},
            {"current_a": False},
            {"in_compliance": 1},
            {"has_fault": "False"},
        )

        for changes in cases:
            with self.subTest(changes=changes):
                controller = DischargeController(self.phase)
                observation = {
                    "elapsed_s": 0,
                    "voltage_v": 3.8,
                    "current_a": -1.0,
                    "in_compliance": False,
                    "has_fault": False,
                    **changes,
                }

                with self.assertRaises(ValueError):
                    controller.update(**observation)

                self.assertEqual(controller.state, "failed")

    def test_invalid_phase_settings_are_rejected(self):
        cases = (
            {"type": "charge_cc_cv"},
            {"current_magnitude_a": 0},
            {"current_magnitude_a": -1},
            {"cutoff_voltage_v": -0.1},
            {"maximum_duration_s": 0},
            {"maximum_duration_s": float("inf")},
        )

        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    DischargeController({
                        **self.phase,
                        **changes,
                    })

    def test_zero_cutoff_can_be_configured(self):
        controller = DischargeController({
            **self.phase,
            "cutoff_voltage_v": 0.0,
        })

        decision = controller.update(
            elapsed_s=0,
            voltage_v=0.0,
            current_a=-1.0,
        )

        self.assertEqual(decision.stop_reason, "voltage_min")

    def test_phase_is_not_modified(self):
        before = deepcopy(self.phase)

        self.observe(0, 3.8)

        self.assertEqual(self.phase, before)


if __name__ == "__main__":
    unittest.main()