"""Offline tests for explicit charge-monitoring configuration."""

from copy import deepcopy
import unittest

import test_preconditioning_plan as plan_tests

from experiments.config import (
    require_charge_monitoring_settings,
    validate_config,
)
from experiments.preconditioning import build_plan


class ExperimentMonitoringConfigTests(unittest.TestCase):
    def setUp(self):
        fixture = plan_tests.PreconditioningPlanTests(
            methodName="test_two_cycles_have_eight_ordered_phases"
        )
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.document = fixture.document

    def add_monitoring_settings(self):
        self.document["charge"][
            "cv_voltage_tolerance_v"
        ] = 0.02
        self.document["acquisition"][
            "maximum_sample_gap_s"
        ] = 0.25

    def test_old_config_remains_valid_for_planning(self):
        result = validate_config(self.document)

        self.assertIsNone(result["charge_monitoring"])
        self.assertEqual(
            set(result["missing_charge_monitoring_fields"]),
            {
                "charge.cv_voltage_tolerance_v",
                "acquisition.maximum_sample_gap_s",
            },
        )

    def test_missing_settings_block_monitoring_requirement(self):
        with self.assertRaisesRegex(
            ValueError, "Explicit charge-monitoring"
        ):
            require_charge_monitoring_settings(
                self.document
            )

    def test_explicit_settings_are_resolved(self):
        self.add_monitoring_settings()

        result = require_charge_monitoring_settings(
            self.document
        )

        self.assertEqual(
            result,
            {
                "target_voltage_v": 4.1,
                "voltage_tolerance_v": 0.02,
                "maximum_voltage_v": 4.2,
                "maximum_sample_gap_s": 0.25,
            },
        )

    def test_gap_below_nominal_interval_is_rejected(self):
        self.add_monitoring_settings()
        self.document["acquisition"][
            "maximum_sample_gap_s"
        ] = 0.1

        with self.assertRaisesRegex(
            ValueError, "at least"
        ):
            validate_config(self.document)

    def test_gap_equal_to_hold_is_rejected(self):
        self.add_monitoring_settings()
        self.document["acquisition"][
            "maximum_sample_gap_s"
        ] = 10

        with self.assertRaisesRegex(
            ValueError, "below charge.termination"
        ):
            validate_config(self.document)

    def test_tolerance_must_be_below_target(self):
        self.add_monitoring_settings()
        self.document["charge"][
            "cv_voltage_tolerance_v"
        ] = 4.1

        with self.assertRaises(ValueError):
            validate_config(self.document)

    def test_invalid_explicit_monitoring_values_are_rejected(self):
        paths = (
            ("charge", "cv_voltage_tolerance_v"),
            ("acquisition", "maximum_sample_gap_s"),
        )

        self.add_monitoring_settings()

        for section, field in paths:
            for value in (
                None,
                True,
                0,
                -1,
                "0.25",
                float("nan"),
                float("inf"),
            ):
                with self.subTest(
                    section=section,
                    field=field,
                    value=value,
                ):
                    document = deepcopy(self.document)
                    document[section][field] = value

                    with self.assertRaises(ValueError):
                        validate_config(document)

    def test_partial_settings_remain_explicitly_incomplete(self):
        self.document["charge"][
            "cv_voltage_tolerance_v"
        ] = 0.02

        result = validate_config(self.document)

        self.assertIsNone(result["charge_monitoring"])
        self.assertEqual(
            result["missing_charge_monitoring_fields"],
            ["acquisition.maximum_sample_gap_s"],
        )

        with self.assertRaises(ValueError):
            require_charge_monitoring_settings(
                self.document
            )

    def test_plan_includes_settings_for_each_charge_phase(self):
        self.add_monitoring_settings()

        plan = build_plan(self.document)
        charge_phases = [
            phase
            for phase in plan["phases"]
            if phase["type"] == "charge_cc_cv"
        ]

        self.assertEqual(len(charge_phases), 2)

        for phase in charge_phases:
            self.assertEqual(
                phase["monitoring"],
                plan["charge_monitoring"],
            )

        self.assertEqual(
            plan["missing_charge_monitoring_fields"],
            [],
        )
        self.assertFalse(plan["execution_enabled"])

    def test_phase_monitoring_dictionaries_are_independent(self):
        self.add_monitoring_settings()

        plan = build_plan(self.document)
        first = plan["phases"][0]["monitoring"]
        second = plan["phases"][4]["monitoring"]

        first["voltage_tolerance_v"] = 999

        self.assertEqual(
            second["voltage_tolerance_v"], 0.02
        )
        self.assertEqual(
            plan["charge_monitoring"]["voltage_tolerance_v"],
            0.02,
        )
        self.assertEqual(
            self.document["charge"][
                "cv_voltage_tolerance_v"
            ],
            0.02,
        )

    def test_input_document_is_not_modified(self):
        self.add_monitoring_settings()
        before = deepcopy(self.document)

        require_charge_monitoring_settings(
            self.document
        )
        build_plan(self.document)

        self.assertEqual(self.document, before)


if __name__ == "__main__":
    unittest.main()