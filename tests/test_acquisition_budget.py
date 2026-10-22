"""Offline tests for finite acquisition sizing."""

from copy import deepcopy
import unittest

import test_preconditioning_plan as plan_tests

from experiments.acquisition_budget import (
    build_acquisition_plan,
    calculate_budget,
)


class AcquisitionBudgetTests(unittest.TestCase):
    def test_four_hours_at_five_hz(self):
        budget = calculate_budget(
            14400,
            0.2,
            maximum_records=100000,
        )

        self.assertEqual(budget.record_count, 72001)
        self.assertAlmostEqual(budget.nominal_span_s, 14400)

    def test_partial_interval_rounds_up(self):
        budget = calculate_budget(
            1.01,
            0.2,
            maximum_records=100,
        )

        self.assertEqual(budget.record_count, 7)
        self.assertGreaterEqual(
            budget.nominal_span_s,
            budget.maximum_duration_s,
        )

    def test_duration_shorter_than_interval_needs_two_samples(self):
        budget = calculate_budget(
            0.1,
            1.0,
            maximum_records=100,
        )

        self.assertEqual(budget.record_count, 2)
        self.assertEqual(budget.nominal_span_s, 1.0)

    def test_exact_record_limit_is_accepted(self):
        budget = calculate_budget(
            99999,
            1,
            maximum_records=100000,
        )

        self.assertEqual(budget.record_count, 100000)

    def test_above_record_limit_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "exceed"):
            calculate_budget(
                100000,
                1,
                maximum_records=100000,
            )

    def test_existing_software_limit_can_be_checked(self):
        with self.assertRaises(ValueError):
            calculate_budget(
                14400,
                0.2,
                maximum_records=2500,
            )

    def test_slower_sampling_can_fit_same_duration(self):
        budget = calculate_budget(
            14400,
            10,
            maximum_records=2500,
        )

        self.assertEqual(budget.record_count, 1441)

    def test_invalid_numeric_inputs_are_rejected(self):
        for name in ("maximum_duration_s", "interval_s"):
            for value in (
                True, None, 0, -1, "1",
                float("nan"), float("inf"),
            ):
                with self.subTest(name=name, value=value):
                    arguments = {
                        "maximum_duration_s": 10,
                        "interval_s": 1,
                        "maximum_records": 100,
                    }
                    arguments[name] = value

                    with self.assertRaises(ValueError):
                        calculate_budget(**arguments)

    def test_invalid_record_limits_are_rejected(self):
        for value in (True, None, 0, 1, -1, 100.0):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    calculate_budget(
                        10, 1, maximum_records=value
                    )

    def test_ratio_overflow_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate_budget(
                1e308,
                1e-308,
                maximum_records=100000,
            )


class AcquisitionPlanTests(unittest.TestCase):
    def setUp(self):
        fixture = plan_tests.PreconditioningPlanTests(
            methodName="test_two_cycles_have_eight_ordered_phases"
        )
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.document = fixture.document

    def test_only_active_phases_receive_budgets(self):
        plan = build_acquisition_plan(
            self.document,
            maximum_records=100000,
        )

        for phase in plan["phases"]:
            with self.subTest(index=phase["phase_index"]):
                if phase["type"] == "rest":
                    self.assertNotIn(
                        "acquisition_budget", phase
                    )
                else:
                    self.assertEqual(
                        phase["acquisition_budget"]["record_count"],
                        72001,
                    )

    def test_input_is_unchanged(self):
        before = deepcopy(self.document)

        build_acquisition_plan(
            self.document,
            maximum_records=100000,
        )

        self.assertEqual(self.document, before)

    def test_any_oversized_phase_rejects_plan(self):
        self.document["discharge"]["maximum_duration_s"] = 30000

        with self.assertRaises(ValueError):
            build_acquisition_plan(
                self.document,
                maximum_records=100000,
            )


if __name__ == "__main__":
    unittest.main()