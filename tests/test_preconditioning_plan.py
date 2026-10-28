"""Offline tests for preconditioning planning and the dry-run CLI."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest

import yaml

from experiments.__main__ import main
from experiments.preconditioning import build_plan


class PreconditioningPlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "experiment.yaml"

        self.document = {
            "schema_version": 1,
            "experiment": {
                "type": "preconditioning",
                "name": "synthetic_preconditioning",
                "cycles": 2,
            },
            "dut": {
                "name": "Synthetic cell",
                "specimen_id": "TEST-001",
                "category": "battery",
                "chemistry": "synthetic",
                "manufacturer": "test",
                "model": "test",
                "nominal_capacity_ah": 2.0,
                "ratings": {
                    "minimum_voltage_v": 2.5,
                    "maximum_voltage_v": 4.2,
                    "maximum_continuous_charge_current_a": 2.0,
                    "maximum_continuous_discharge_current_a": 4.0,
                },
            },
            "instrument": {
                "visa_address": "TEST::INSTR",
                "channel": 1,
                "four_wire": False,
                "nplc": 1.0,
            },
            "acquisition": {"interval_s": 0.2},
            "charge": {
                "mode": "cc_cv",
                "current": {
                    "basis": "capacity_c_rate",
                    "factor": 0.5,
                },
                "target_voltage_v": 4.1,
                "termination": {
                    "current_reference": "rated_max_charge_current",
                    "current_factor": 0.05,
                    "hold_time_s": 10,
                },
                "maximum_duration_s": 14400,
                "maximum_cv_duration_s": 3600,
            },
            "rest": {
                "after_charge_s": 900,
                "after_discharge_s": 900,
            },
            "discharge": {
                "mode": "constant_current",
                "current": {
                    "basis": "rated_max_discharge_fraction",
                    "factor": 0.5,
                },
                "cutoff_voltage_v": 2.7,
                "maximum_duration_s": 14400,
            },
            "output": {
                "root_directory": str(
                    self.root / "must-not-be-created"
                ),
            },
        }

    def save(self):
        self.path.write_text(
            yaml.safe_dump(self.document),
            encoding="utf-8",
        )

    def test_two_cycles_have_eight_ordered_phases(self):
        plan = build_plan(self.document)

        self.assertEqual(plan["cycles"], 2)
        self.assertEqual(plan["phase_count"], 8)
        self.assertEqual(
            [phase["type"] for phase in plan["phases"]],
            [
                "charge_cc_cv",
                "rest",
                "discharge_cc",
                "rest",
            ] * 2,
        )
        self.assertEqual(
            [phase["cycle"] for phase in plan["phases"]],
            [1, 1, 1, 1, 2, 2, 2, 2],
        )
        self.assertEqual(
            [phase["phase_index"] for phase in plan["phases"]],
            list(range(8)),
        )

    def test_cycles_means_total_not_additional_repeats(self):
        self.document["experiment"]["cycles"] = 1

        plan = build_plan(self.document)

        self.assertEqual(plan["phase_count"], 4)

    def test_charge_phase_contains_resolved_settings(self):
        phase = build_plan(self.document)["phases"][0]

        self.assertAlmostEqual(phase["current_magnitude_a"], 1)
        self.assertAlmostEqual(phase["target_voltage_v"], 4.1)
        self.assertAlmostEqual(phase["termination_current_a"], 0.1)
        self.assertEqual(phase["termination_hold_time_s"], 10)
        self.assertTrue(phase["termination_requires_cv"])
        self.assertEqual(phase["maximum_duration_s"], 14400)
        self.assertEqual(phase["maximum_cv_duration_s"], 3600)

    def test_discharge_phase_contains_resolved_settings(self):
        phase = build_plan(self.document)["phases"][2]

        self.assertAlmostEqual(phase["current_magnitude_a"], 2)
        self.assertAlmostEqual(phase["cutoff_voltage_v"], 2.7)
        self.assertEqual(phase["maximum_duration_s"], 14400)

    def test_rest_phases_require_verified_output_off(self):
        phases = build_plan(self.document)["phases"]

        for index, follows in ((1, "charge"), (3, "discharge")):
            with self.subTest(index=index):
                self.assertEqual(phases[index]["follows"], follows)
                self.assertEqual(phases[index]["duration_s"], 900)
                self.assertTrue(
                    phases[index]["requires_verified_output_off"]
                )

    def test_input_and_plan_are_independent(self):
        before = deepcopy(self.document)

        plan = build_plan(self.document)
        plan["configuration"]["experiment"]["name"] = "changed"

        self.assertEqual(self.document, before)

    def test_invalid_configuration_is_rejected(self):
        self.document["experiment"]["cycles"] = 0

        with self.assertRaises(ValueError):
            build_plan(self.document)

    def test_plan_is_strict_json_compatible(self):
        plan = build_plan(self.document)

        encoded = json.dumps(plan, allow_nan=False)

        self.assertEqual(json.loads(encoded), plan)
        self.assertFalse(plan["execution_enabled"])

    def test_cli_prints_plan_without_creating_files(self):
        self.save()
        before = {
            path.name: path.read_bytes()
            for path in self.root.iterdir()
        }
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([
                "--config", str(self.path),
                "--dry-run",
            ])

        after = {
            path.name: path.read_bytes()
            for path in self.root.iterdir()
        }
        plan = json.loads(stdout.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(plan["phase_count"], 8)
        self.assertEqual(before, after)

    def test_cli_invalid_configuration_returns_one(self):
        self.document["charge"]["target_voltage_v"] = None
        self.save()
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([
                "--config", str(self.path),
                "--dry-run",
            ])

        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn(
            "Experiment planning failed",
            stderr.getvalue(),
        )

    def test_cli_requires_explicit_dry_run(self):
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                main(["--config", str(self.path)])

        self.assertEqual(caught.exception.code, 2)

    def test_cli_has_no_execute_option(self):
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                main([
                    "--config", str(self.path),
                    "--dry-run",
                    "--execute",
                ])

        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()