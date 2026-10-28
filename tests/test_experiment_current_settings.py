"""Offline tests for experiment current resolution."""

from dataclasses import FrozenInstanceError
import unittest

from experiments.current_settings import resolve_currents


class ExperimentCurrentSettingsTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "category": "battery",
            "nominal_capacity_ah": 2.0,
            "maximum_charge_current_a": 2.0,
            "maximum_discharge_current_a": 4.0,
            "charge_basis": "capacity_c_rate",
            "charge_factor": 0.5,
            "discharge_factor": 0.5,
            "termination_reference": "rated_max_charge_current",
            "termination_factor": 0.05,
        }

    def resolve(self, **changes):
        settings = {**self.settings, **changes}
        return resolve_currents(**settings)

    def test_battery_uses_capacity_c_rate(self):
        result = self.resolve()

        self.assertAlmostEqual(result.charge_current_a, 1.0)
        self.assertAlmostEqual(result.discharge_current_a, 2.0)
        self.assertAlmostEqual(
            result.charge_termination_current_a, 0.1
        )

    def test_edlc_uses_ten_percent_of_rated_charge_current(self):
        result = self.resolve(
            category="edlc",
            nominal_capacity_ah=None,
            charge_basis="rated_max_charge_fraction",
            charge_factor=0.10,
        )

        self.assertAlmostEqual(result.charge_current_a, 0.2)

    def test_lic_uses_half_rated_charge_current(self):
        result = self.resolve(
            category="lic",
            nominal_capacity_ah=None,
            charge_basis="rated_max_charge_fraction",
            charge_factor=0.50,
        )

        self.assertAlmostEqual(result.charge_current_a, 1.0)

    def test_termination_can_reference_configured_current(self):
        result = self.resolve(
            termination_reference="configured_charge_current",
        )

        self.assertAlmostEqual(
            result.charge_termination_current_a, 0.05
        )

    def test_charge_above_rating_is_rejected(self):
        with self.assertRaisesRegex(
            ValueError, "exceeds"
        ):
            self.resolve(maximum_charge_current_a=0.8)

    def test_termination_must_be_below_charge_current(self):
        for factor in (0.5, 0.75):
            with self.subTest(factor=factor):
                with self.assertRaisesRegex(
                    ValueError, "strictly below"
                ):
                    self.resolve(termination_factor=factor)

    def test_battery_requires_capacity(self):
        with self.assertRaisesRegex(
            ValueError, "nominal_capacity_ah"
        ):
            self.resolve(nominal_capacity_ah=None)


    def test_all_categories_support_c_rate_and_fraction(self):
        for category in ("battery", "edlc", "lic"):
            for basis in (
                "capacity_c_rate",
                "rated_max_charge_fraction",
            ):
                with self.subTest(category=category, basis=basis):
                    result = self.resolve(
                        category=category,
                        charge_basis=basis,
                    )
                    self.assertAlmostEqual(
                        result.charge_current_a, 1.0
                    )
        with self.assertRaises(ValueError):
            self.resolve(charge_basis="unknown")

    def test_unknown_category_is_rejected(self):
        with self.assertRaises(ValueError):
            self.resolve(category="unknown")

    def test_unknown_termination_reference_is_rejected(self):
        with self.assertRaises(ValueError):
            self.resolve(termination_reference="max_current")

    def test_invalid_positive_numbers_are_rejected(self):
        fields = (
            "nominal_capacity_ah",
            "maximum_charge_current_a",
            "maximum_discharge_current_a",
            "charge_factor",
            "discharge_factor",
            "termination_factor",
        )
        values = (
            None,
            True,
            False,
            0,
            -1,
            "0.5",
            float("nan"),
            float("inf"),
            -float("inf"),
        )

        for field in fields:
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        self.resolve(**{field: value})

    def test_fractions_above_one_are_rejected(self):
        for field in ("discharge_factor", "termination_factor"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    self.resolve(**{field: 1.1})

        with self.assertRaises(ValueError):
            self.resolve(
                category="edlc",
                charge_basis="rated_max_charge_fraction",
                charge_factor=1.1,
            )

    def test_c_rate_above_one_is_not_itself_a_fraction_error(self):
        result = self.resolve(
            charge_factor=1.5,
            maximum_charge_current_a=4.0,
        )

        self.assertAlmostEqual(result.charge_current_a, 3.0)

    def test_overflow_is_rejected(self):
        with self.assertRaises(ValueError):
            self.resolve(
                nominal_capacity_ah=1e308,
                charge_factor=10.0,
                maximum_charge_current_a=1e308,
            )

    def test_underflow_to_zero_is_rejected(self):
        with self.assertRaises(ValueError):
            self.resolve(
                nominal_capacity_ah=1e-300,
                charge_factor=1e-300,
            )

    def test_result_is_immutable(self):
        result = self.resolve()

        with self.assertRaises(FrozenInstanceError):
            result.charge_current_a = 99.0


if __name__ == "__main__":
    unittest.main()