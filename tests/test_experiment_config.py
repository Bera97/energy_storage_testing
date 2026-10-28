"""Offline tests for YAML experiment configuration."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

import yaml

from experiments.config import load_config, validate_config


class ExperimentConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "experiment.yaml"

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
                "nominal_capacitance_f": None,
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
                    "hold_time_s": 10.0,
                },
                "maximum_duration_s": 14400.0,
                "maximum_cv_duration_s": 3600.0,
            },
            "rest": {
                "after_charge_s": 900.0,
                "after_discharge_s": 900.0,
            },
            "discharge": {
                "mode": "constant_current",
                "current": {
                    "basis": "rated_max_discharge_fraction",
                    "factor": 0.5,
                },
                "cutoff_voltage_v": 2.7,
                "maximum_duration_s": 14400.0,
            },
            "output": {"root_directory": "runs"},
        }

    def save(self):
        self.path.write_text(
            yaml.safe_dump(self.document),
            encoding="utf-8",
        )

    def test_valid_yaml_resolves_currents(self):
        self.save()

        result = load_config(self.path)

        self.assertEqual(result["config"], self.document)
        self.assertEqual(
            result["resolved_currents"],
            {
                "charge_current_a": 1.0,
                "discharge_current_a": 2.0,
                "charge_termination_current_a": 0.1,
            },
        )

    def test_validation_does_not_modify_input(self):
        before = deepcopy(self.document)

        result = validate_config(self.document)
        result["config"]["experiment"]["name"] = "changed"

        self.assertEqual(self.document, before)

    def test_loading_does_not_create_or_modify_files(self):
        self.save()
        before = {
            path.name: path.read_bytes()
            for path in self.path.parent.iterdir()
        }

        load_config(self.path)

        after = {
            path.name: path.read_bytes()
            for path in self.path.parent.iterdir()
        }
        self.assertEqual(before, after)

    def test_duplicate_yaml_key_is_rejected(self):
        self.path.write_text(
            "schema_version: 1\nschema_version: 1\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            ValueError, "Duplicate configuration key"
        ):
            load_config(self.path)

    def test_unknown_nested_key_is_rejected(self):
        self.document["charge"]["target_volatge_v"] = 4.1

        with self.assertRaisesRegex(ValueError, "unknown fields"):
            validate_config(self.document)

    def test_missing_field_is_rejected(self):
        del self.document["charge"]["termination"]

        with self.assertRaisesRegex(ValueError, "missing fields"):
            validate_config(self.document)

    def test_required_null_setting_is_rejected(self):
        self.document["charge"]["target_voltage_v"] = None

        with self.assertRaises(ValueError):
            validate_config(self.document)

    def test_cycles_must_be_positive_integer(self):
        for value in (True, 0, -1, 2.5, "2"):
            with self.subTest(value=value):
                self.document["experiment"]["cycles"] = value

                with self.assertRaises(ValueError):
                    validate_config(self.document)

    def test_unknown_experiment_is_rejected(self):
        self.document["experiment"]["type"] = "unknown"

        with self.assertRaises(ValueError):
            validate_config(self.document)

    def test_voltage_relationships_are_checked(self):
        cases = (
            (2.4, 4.1),
            (2.7, 4.3),
            (4.1, 4.1),
            (4.2, 4.1),
        )

        for cutoff, target in cases:
            with self.subTest(cutoff=cutoff, target=target):
                self.document["discharge"]["cutoff_voltage_v"] = cutoff
                self.document["charge"]["target_voltage_v"] = target

                with self.assertRaisesRegex(ValueError, "Voltages"):
                    validate_config(self.document)

    def test_invalid_timeout_relationship_is_rejected(self):
        self.document["charge"]["maximum_cv_duration_s"] = 20000

        with self.assertRaises(ValueError):
            validate_config(self.document)

    def test_hold_time_must_fit_cv_duration(self):
        self.document["charge"]["termination"]["hold_time_s"] = 3600

        with self.assertRaises(ValueError):
            validate_config(self.document)

    def test_invalid_numeric_settings_are_rejected(self):
        paths = (
            ("instrument", "nplc"),
            ("acquisition", "interval_s"),
            ("rest", "after_charge_s"),
            ("rest", "after_discharge_s"),
            ("charge", "maximum_duration_s"),
            ("discharge", "maximum_duration_s"),
        )

        for section, field in paths:
            for value in (True, 0, -1, float("nan"), float("inf")):
                with self.subTest(
                    section=section, field=field, value=value
                ):
                    document = deepcopy(self.document)
                    document[section][field] = value

                    with self.assertRaises(ValueError):
                        validate_config(document)

    def test_instrument_types_are_checked(self):
        for field, value in (
            ("channel", True),
            ("channel", 3),
            ("four_wire", "false"),
        ):
            with self.subTest(field=field, value=value):
                document = deepcopy(self.document)
                document["instrument"][field] = value

                with self.assertRaises(ValueError):
                    validate_config(document)

    def test_edlc_and_lic_current_policies(self):
        for category, factor, expected in (
            ("edlc", 0.10, 0.2),
            ("lic", 0.50, 1.0),
        ):
            with self.subTest(category=category):
                document = deepcopy(self.document)
                document["dut"]["category"] = category
                document["dut"]["nominal_capacity_ah"] = None
                document["dut"]["nominal_capacitance_f"] = 100
                document["charge"]["current"] = {
                    "basis": "rated_max_charge_fraction",
                    "factor": factor,
                }

                result = validate_config(document)

                self.assertAlmostEqual(
                    result["resolved_currents"]["charge_current_a"],
                    expected,
                )

    def test_unsafe_yaml_tag_is_rejected(self):
        self.path.write_text(
            "!!python/object/apply:builtins.str [123]",
            encoding="utf-8",
        )

        with self.assertRaises(ValueError):
            load_config(self.path)

    def test_missing_file_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Cannot load"):
            load_config(self.path)

    def test_empty_yaml_is_rejected(self):
        self.path.write_text("", encoding="utf-8")

        with self.assertRaises(ValueError):
            load_config(self.path)

    def test_boolean_schema_version_is_rejected(self):
        self.document["schema_version"] = True

        with self.assertRaises(ValueError):
            validate_config(self.document)


if __name__ == "__main__":
    unittest.main()