"""Offline tests for direction-separated charge and energy."""

import json
import unittest

from analysis.energy_charge import calculate_energy_charge
from analysis.export_recorded_analysis import ANALYSIS_FIELDS


class EnergyChargeDirectionTests(unittest.TestCase):
    def assert_directional_identity(self, result):
        summary = result["summary"]

        for quantity, unit in (
            ("charge", "c"),
            ("charge", "mah"),
            ("energy", "j"),
            ("energy", "wh"),
        ):
            signed = summary[f"signed_{quantity}_{unit}"]
            positive = summary[f"positive_{quantity}_{unit}"]
            negative = summary[f"negative_{quantity}_{unit}"]

            self.assertGreaterEqual(positive, 0)
            self.assertGreaterEqual(negative, 0)
            self.assertAlmostEqual(
                signed,
                positive - negative,
            )

            previous_positive = 0.0
            previous_negative = 0.0

            for row in result["samples"]:
                prefix = "cumulative"
                signed = row[
                    f"{prefix}_signed_{quantity}_{unit}"
                ]
                positive = row[
                    f"{prefix}_positive_{quantity}_{unit}"
                ]
                negative = row[
                    f"{prefix}_negative_{quantity}_{unit}"
                ]

                self.assertGreaterEqual(
                    positive, previous_positive
                )
                self.assertGreaterEqual(
                    negative, previous_negative
                )
                self.assertAlmostEqual(
                    signed,
                    positive - negative,
                )

                previous_positive = positive
                previous_negative = negative

    def test_positive_only(self):
        result = calculate_energy_charge(
            [0, 2],
            [3, 3],
            [0.5, 0.5],
        )
        summary = result["summary"]

        self.assertAlmostEqual(
            summary["positive_charge_c"], 1
        )
        self.assertEqual(summary["negative_charge_c"], 0)
        self.assertAlmostEqual(
            summary["positive_energy_j"], 3
        )
        self.assertEqual(summary["negative_energy_j"], 0)
        self.assertAlmostEqual(
            summary["positive_charge_mah"], 1 / 3.6
        )
        self.assertAlmostEqual(
            summary["positive_energy_wh"], 3 / 3600
        )
        self.assert_directional_identity(result)

    def test_negative_only(self):
        result = calculate_energy_charge(
            [0, 2],
            [3, 3],
            [-0.5, -0.5],
        )
        summary = result["summary"]

        self.assertEqual(summary["positive_charge_c"], 0)
        self.assertAlmostEqual(
            summary["negative_charge_c"], 1
        )
        self.assertEqual(summary["positive_energy_j"], 0)
        self.assertAlmostEqual(
            summary["negative_energy_j"], 3
        )
        self.assertAlmostEqual(
            summary["negative_charge_mah"], 1 / 3.6
        )
        self.assertAlmostEqual(
            summary["negative_energy_wh"], 3 / 3600
        )
        self.assert_directional_identity(result)

    def test_symmetric_crossing_does_not_hide_exchange(self):
        result = calculate_energy_charge(
            [0, 2],
            [2, 2],
            [-1, 1],
        )
        summary = result["summary"]

        self.assertAlmostEqual(summary["signed_charge_c"], 0)
        self.assertAlmostEqual(summary["signed_energy_j"], 0)
        self.assertAlmostEqual(
            summary["positive_charge_c"], 0.5
        )
        self.assertAlmostEqual(
            summary["negative_charge_c"], 0.5
        )
        self.assertAlmostEqual(
            summary["positive_energy_j"], 1
        )
        self.assertAlmostEqual(
            summary["negative_energy_j"], 1
        )
        self.assert_directional_identity(result)

    def test_asymmetric_crossing(self):
        result = calculate_energy_charge(
            [0, 4],
            [2, 2],
            [-1, 3],
        )
        summary = result["summary"]

        self.assertAlmostEqual(
            summary["positive_charge_c"], 4.5
        )
        self.assertAlmostEqual(
            summary["negative_charge_c"], 0.5
        )
        self.assertAlmostEqual(summary["signed_charge_c"], 4)
        self.assertAlmostEqual(
            summary["positive_energy_j"], 9
        )
        self.assertAlmostEqual(
            summary["negative_energy_j"], 1
        )
        self.assert_directional_identity(result)

    def test_reverse_asymmetric_crossing(self):
        result = calculate_energy_charge(
            [0, 4],
            [2, 2],
            [3, -1],
        )
        summary = result["summary"]

        self.assertAlmostEqual(
            summary["positive_charge_c"], 4.5
        )
        self.assertAlmostEqual(
            summary["negative_charge_c"], 0.5
        )
        self.assert_directional_identity(result)

    def test_zero_endpoint(self):
        result = calculate_energy_charge(
            [0, 1, 3],
            [2, 2, 2],
            [-2, 0, 2],
        )
        summary = result["summary"]

        self.assertAlmostEqual(
            summary["negative_charge_c"], 1
        )
        self.assertAlmostEqual(
            summary["positive_charge_c"], 2
        )
        self.assertAlmostEqual(
            summary["negative_energy_j"], 2
        )
        self.assertAlmostEqual(
            summary["positive_energy_j"], 4
        )
        self.assert_directional_identity(result)

    def test_energy_direction_uses_power_not_current(self):
        result = calculate_energy_charge(
            [0, 2],
            [-2, 2],
            [1, 1],
        )
        summary = result["summary"]

        self.assertAlmostEqual(
            summary["positive_charge_c"], 2
        )
        self.assertEqual(summary["negative_charge_c"], 0)
        self.assertAlmostEqual(
            summary["positive_energy_j"], 1
        )
        self.assertAlmostEqual(
            summary["negative_energy_j"], 1
        )
        self.assertAlmostEqual(summary["signed_energy_j"], 0)
        self.assert_directional_identity(result)

    def test_irregular_intervals_and_multiple_crossings(self):
        result = calculate_energy_charge(
            [10, 11, 14, 16],
            [2, 2, 2, 2],
            [-1, 1, -1, 1],
        )
        summary = result["summary"]

        self.assertAlmostEqual(
            summary["positive_charge_c"], 1.5
        )
        self.assertAlmostEqual(
            summary["negative_charge_c"], 1.5
        )
        self.assertAlmostEqual(
            summary["positive_energy_j"], 3
        )
        self.assertAlmostEqual(
            summary["negative_energy_j"], 3
        )
        self.assert_directional_identity(result)

    def test_all_zero_values(self):
        result = calculate_energy_charge(
            [0, 1, 2],
            [0, 0, 0],
            [0, 0, 0],
        )
        summary = result["summary"]

        for name in (
            "positive_charge_c",
            "negative_charge_c",
            "positive_energy_j",
            "negative_energy_j",
        ):
            self.assertEqual(summary[name], 0)

        self.assert_directional_identity(result)

    def test_single_sample_has_zero_directional_totals(self):
        result = calculate_energy_charge(
            [42],
            [2],
            [-1],
        )
        summary = result["summary"]

        for name in (
            "positive_charge_c",
            "negative_charge_c",
            "positive_energy_j",
            "negative_energy_j",
        ):
            self.assertEqual(summary[name], 0)

        self.assertTrue(result["warnings"])
        self.assert_directional_identity(result)

    def test_large_crossing_avoids_endpoint_sum_overflow(self):
        result = calculate_energy_charge(
            [0, 1],
            [1, 1],
            [-1e308, 1e308],
        )
        summary = result["summary"]

        self.assertAlmostEqual(
            summary["positive_charge_c"] / 1e308,
            0.25,
        )
        self.assertAlmostEqual(
            summary["negative_charge_c"] / 1e308,
            0.25,
        )
        self.assertEqual(summary["signed_charge_c"], 0)

        json.dumps(result, allow_nan=False)

    def test_directional_overflow_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate_energy_charge(
                [0, 10],
                [1, 1],
                [-1e308, 1e308],
            )

    def test_export_fields_match_calculation_samples(self):
        result = calculate_energy_charge(
            [0, 1],
            [2, 2],
            [-1, 1],
        )

        for row in result["samples"]:
            self.assertEqual(
                set(row),
                set(ANALYSIS_FIELDS),
            )

    def test_version_and_provenance(self):
        result = calculate_energy_charge(
            [0, 1],
            [2, 2],
            [-1, 1],
        )

        self.assertEqual(result["analysis_version"], 2)
        self.assertIn(
            "zero crossings",
            result["directional_integration_method"],
        )
        self.assertIn(
            "nonnegative",
            result["sign_convention"],
        )
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()