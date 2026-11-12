"""Offline API tests for experiment acquisition planning."""
from copy import deepcopy
import json
import unittest
import test_preconditioning_plan as plan_tests
from experiments.acquisition_budget import build_acquisition_plan
from experiments.config import load_config
class ExperimentBudgetPlanTests(unittest.TestCase):
    def setUp(self):
        fixture = plan_tests.PreconditioningPlanTests(
            methodName="test_two_cycles_have_eight_ordered_phases"
        )
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.save()
        self.path = fixture.path
        self.root = fixture.root
    def plan(self, maximum_records=100000):
        document = load_config(self.path)["config"]
        return build_acquisition_plan(
            document,
            maximum_records=maximum_records,
        )
    def test_explicit_limit_is_included_in_plan(self):
        plan = self.plan()
        self.assertEqual(plan["acquisition_planning_limit"], 100000)
        active = [
            phase for phase in plan["phases"]
            if phase["type"] != "rest"
        ]
        self.assertEqual(len(active), 4)
        for phase in active:
            budget = phase["acquisition_budget"]
            self.assertEqual(budget["record_count"], 72001)
            self.assertEqual(budget["maximum_records"], 100000)
            self.assertGreaterEqual(
                budget["nominal_span_s"],
                phase["maximum_duration_s"],
            )
    def test_stricter_limit_can_reject_configuration(self):
        with self.assertRaisesRegex(ValueError, "exceed"):
            self.plan(maximum_records=2500)
    def test_invalid_api_limits_are_rejected(self):
        for value in (1, 0, -1, True, 2.5, "100000", None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError, "maximum_records"
                ):
                    self.plan(maximum_records=value)
    def test_api_does_not_inherit_retired_cli_upper_bound(self):
        plan = self.plan(maximum_records=100001)
        self.assertEqual(plan["acquisition_planning_limit"], 100001)
        for phase in plan["phases"]:
            if phase["type"] != "rest":
                self.assertEqual(
                    phase["acquisition_budget"]["maximum_records"],
                    100001,
                )
    def test_planning_does_not_create_or_modify_files(self):
        before = {
            path.name: path.read_bytes()
            for path in self.root.iterdir()
        }
        self.plan()
        after = {
            path.name: path.read_bytes()
            for path in self.root.iterdir()
        }
        self.assertEqual(before, after)
    def test_input_is_unchanged_and_plan_is_strict_json(self):
        document = load_config(self.path)["config"]
        before = deepcopy(document)
        plan = build_acquisition_plan(
            document,
            maximum_records=100000,
        )
        self.assertEqual(document, before)
        self.assertEqual(
            json.loads(json.dumps(plan, allow_nan=False)),
            plan,
        )
if __name__ == "__main__":
    unittest.main()
