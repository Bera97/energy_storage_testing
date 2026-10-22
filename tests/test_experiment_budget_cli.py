"""Offline tests for acquisition budgets in the dry-run CLI."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import unittest

import test_preconditioning_plan as plan_tests

from experiments.__main__ import main


class ExperimentBudgetCLITests(unittest.TestCase):
    def setUp(self):
        fixture = plan_tests.PreconditioningPlanTests(
            methodName="test_two_cycles_have_eight_ordered_phases"
        )
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.save()

        self.path = fixture.path
        self.root = fixture.root

    def invoke(self, *extra):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([
                "--config", str(self.path),
                "--dry-run",
                *extra,
            ])

        return code, stdout.getvalue(), stderr.getvalue()

    def test_default_limit_is_included_in_plan(self):
        code, stdout, stderr = self.invoke()

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")

        plan = json.loads(stdout)
        self.assertEqual(
            plan["acquisition_planning_limit"], 100000
        )

        active = [
            phase
            for phase in plan["phases"]
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
        code, stdout, stderr = self.invoke(
            "--maximum-records", "2500"
        )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("exceed", stderr)

    def test_out_of_range_limits_are_rejected(self):
        for value in ("1", "100001"):
            with self.subTest(value=value):
                code, stdout, stderr = self.invoke(
                    "--maximum-records", value
                )

                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertIn("--maximum-records", stderr)

    def test_dry_run_does_not_create_or_modify_files(self):
        before = {
            path.name: path.read_bytes()
            for path in self.root.iterdir()
        }

        code, _, _ = self.invoke()

        after = {
            path.name: path.read_bytes()
            for path in self.root.iterdir()
        }

        self.assertEqual(code, 0)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()