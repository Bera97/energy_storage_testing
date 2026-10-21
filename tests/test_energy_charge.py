"""Offline tests for signed charge and energy calculations."""

import json
import unittest

from analysis.energy_charge import calculate_energy_charge


class EnergyChargeTests(unittest.TestCase):
    def test_constant_voltage_and_current(self):
        result = calculate_energy_charge(
            [0, 1, 2],
            [2, 2, 2],
            [0.5, 0.5, 0.5],
        )
        summary = result["summary"]

        self.assertEqual(summary["sample_count"], 3)
        self.assertEqual(summary["interval_count"], 2)
        self.assertAlmostEqual(summary["recorded_span_s"], 2)
        self.assertAlmostEqual(summary["signed_charge_c"], 1)
        self.assertAlmostEqual(
            summary["signed_charge_mah"], 1 / 3.6
        )
        self.assertAlmostEqual(summary["signed_energy_j"], 2)
        self.assertAlmostEqual(
            summary["signed_energy_wh"], 2 / 3600
        )
        self.assertAlmostEqual(
            summary["time_weighted_mean_voltage_v"], 2
        )
        self.assertAlmostEqual(
            summary["time_weighted_mean_signed_power_w"], 1
        )

    def test_irregular_intervals_and_changing_values(self):
        result = calculate_energy_charge(
            [10, 11, 14],
            [2, 4, 6],
            [1, 2, 1],
        )
        summary = result["summary"]
        samples = result["samples"]

        self.assertEqual(
            [row["elapsed_time_s"] for row in samples],
            [0, 1, 4],
        )
        self.assertEqual(
            [row["dt_s"] for row in samples],
            [None, 1, 3],
        )
        self.assertEqual(
            [row["signed_power_w"] for row in samples],
            [2, 8, 6],
        )
        self.assertEqual(
            [
                row["cumulative_signed_charge_c"]
                for row in samples
            ],
            [0, 1.5, 6],
        )
        self.assertEqual(
            [
                row["cumulative_signed_energy_j"]
                for row in samples
            ],
            [0, 5, 26],
        )
        self.assertAlmostEqual(
            summary["time_weighted_mean_voltage_v"], 4.5
        )
        self.assertAlmostEqual(
            summary["time_weighted_mean_signed_power_w"], 6.5
        )
        self.assertEqual(
            summary["minimum_sample_interval_s"], 1
        )
        self.assertEqual(
            summary["maximum_sample_interval_s"], 3
        )
        self.assertEqual(summary["minimum_voltage_v"], 2)
        self.assertEqual(summary["maximum_voltage_v"], 6)
        self.assertEqual(summary["minimum_current_a"], 1)
        self.assertEqual(summary["maximum_current_a"], 2)

    def test_negative_current_preserves_sign(self):
        result = calculate_energy_charge(
            [0, 2],
            [3, 3],
            [-0.5, -0.5],
        )
        summary = result["summary"]

        self.assertAlmostEqual(summary["signed_charge_c"], -1)
        self.assertAlmostEqual(summary["signed_energy_j"], -3)
        self.assertAlmostEqual(
            summary["time_weighted_mean_signed_power_w"],
            -1.5,
        )

    def test_sign_changes_preserve_cumulative_history(self):
        result = calculate_energy_charge(
            [0, 1, 2],
            [2, 2, 2],
            [-1, 0, 1],
        )

        self.assertEqual(
            [
                row["cumulative_signed_charge_c"]
                for row in result["samples"]
            ],
            [0, -0.5, 0],
        )
        self.assertEqual(
            [
                row["cumulative_signed_energy_j"]
                for row in result["samples"]
            ],
            [0, -1, 0],
        )

    def test_single_sample_has_no_integration_coverage(self):
        result = calculate_energy_charge(
            [42],
            [1.3],
            [-0.0001],
        )
        summary = result["summary"]

        self.assertEqual(summary["recorded_span_s"], 0)
        self.assertEqual(summary["interval_count"], 0)
        self.assertEqual(summary["signed_charge_c"], 0)
        self.assertEqual(summary["signed_energy_j"], 0)
        self.assertIsNone(
            summary["time_weighted_mean_voltage_v"]
        )
        self.assertIsNone(
            summary["time_weighted_mean_signed_power_w"]
        )
        self.assertIsNone(result["samples"][0]["dt_s"])
        self.assertTrue(result["warnings"])

    def test_empty_input_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate_energy_charge([], [], [])

    def test_mismatched_lengths_are_rejected(self):
        with self.assertRaises(ValueError):
            calculate_energy_charge(
                [0, 1], [2], [1, 1]
            )

    def test_nonincreasing_timestamps_are_rejected(self):
        for timestamps in ([0, 0], [1, 0]):
            with self.subTest(timestamps=timestamps):
                with self.assertRaises(ValueError):
                    calculate_energy_charge(
                        timestamps, [2, 2], [1, 1]
                    )

    def test_nonfinite_inputs_are_rejected(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            for column in range(3):
                inputs = [
                    [0, 1],
                    [2, 2],
                    [1, 1],
                ]
                inputs[column][1] = value

                with self.subTest(value=value, column=column):
                    with self.assertRaises(ValueError):
                        calculate_energy_charge(*inputs)

    def test_calculation_overflow_is_rejected(self):
        cases = [
            (
                [-1e308, 1e308],
                [1, 1],
                [1, 1],
            ),
            (
                [0, 1],
                [1e308, 1e308],
                [1e308, 1e308],
            ),
            (
                [0, 1e308],
                [1, 1],
                [10, 10],
            ),
        ]

        for inputs in cases:
            with self.subTest(inputs=inputs):
                with self.assertRaises(ValueError):
                    calculate_energy_charge(*inputs)

    def test_inputs_are_not_modified(self):
        timestamps = [10, 11, 14]
        voltages = [2, 4, 6]
        currents = [1, 2, 1]
        before = (
            timestamps.copy(),
            voltages.copy(),
            currents.copy(),
        )

        calculate_energy_charge(
            timestamps, voltages, currents
        )

        self.assertEqual(
            (timestamps, voltages, currents), before
        )

    def test_result_is_strict_json_compatible(self):
        for inputs in (
            ([0, 1], [2, 2], [-1, -1]),
            ([42], [2], [-1]),
        ):
            with self.subTest(inputs=inputs):
                result = calculate_energy_charge(*inputs)
                encoded = json.dumps(result, allow_nan=False)
                self.assertIsInstance(json.loads(encoded), dict)


if __name__ == "__main__":
    unittest.main()