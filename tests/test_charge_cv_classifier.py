"""Offline tests for voltage-source CV qualification."""

from dataclasses import dataclass
import unittest

from experiments.charge_controller import (
    ChargeFaultError,
    ChargePolarityError,
)
from experiments.charge_cv_classifier import (
    ChargeSourceModeError,
    ChargeVoltageError,
    VoltageSourceCVClassifier,
)
from experiments.charge_trace_monitor import ChargeTraceMonitor


@dataclass(frozen=True)
class FakeReading:
    timestamp_s: float = 0.0
    voltage_v: float = 4.0
    current_a: float = 0.05
    status: int = 0
    has_fault: bool = False


class ChargeCVClassifierTests(unittest.TestCase):
    def setUp(self):
        self.classifier = VoltageSourceCVClassifier(
            target_voltage_v=4.0,
            voltage_tolerance_v=0.02,
            maximum_voltage_v=4.1,
        )

    def test_near_target_without_compliance_qualifies(self):
        self.assertTrue(self.classifier(FakeReading()))

    def test_voltage_source_selection_alone_is_insufficient(self):
        self.assertFalse(
            self.classifier(
                FakeReading(voltage_v=3.5, status=0)
            )
        )

    def test_each_compliance_encoding_prevents_cv(self):
        for status in (2, 4, 6):
            with self.subTest(status=status):
                self.assertFalse(
                    self.classifier(FakeReading(status=status))
                )

    def test_current_source_selection_is_rejected(self):
        with self.assertRaises(ChargeSourceModeError):
            self.classifier(FakeReading(status=1))

    def test_fault_is_rejected(self):
        with self.assertRaises(ChargeFaultError):
            self.classifier(FakeReading(has_fault=True))

    def test_negative_current_is_rejected(self):
        with self.assertRaises(ChargePolarityError):
            self.classifier(FakeReading(current_a=-0.01))

    def test_negative_voltage_is_rejected(self):
        with self.assertRaises(ChargeVoltageError):
            self.classifier(FakeReading(voltage_v=-0.01))

    def test_voltage_above_ceiling_is_rejected(self):
        with self.assertRaises(ChargeVoltageError):
            self.classifier(FakeReading(voltage_v=4.11))

    def test_voltage_above_cv_band_but_below_ceiling_is_not_cv(self):
        self.assertFalse(
            self.classifier(FakeReading(voltage_v=4.05))
        )

    def test_unrelated_status_bits_are_not_used_as_cv_flags(self):
        self.assertTrue(
            self.classifier(FakeReading(status=1 << 10))
        )
        self.assertFalse(
            self.classifier(
                FakeReading(
                    voltage_v=3.5,
                    status=1 << 10,
                )
            )
        )

    def test_nonfinite_measurements_are_rejected(self):
        for field in ("voltage_v", "current_a"):
            for value in (float("nan"), float("inf")):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        self.classifier(
                            FakeReading(**{field: value})
                        )

    def test_invalid_status_is_rejected(self):
        for status in (True, -1, 1.0, 0x100000000):
            with self.subTest(status=status):
                with self.assertRaises(ValueError):
                    self.classifier(FakeReading(status=status))

    def test_invalid_settings_are_rejected(self):
        cases = (
            {"target_voltage_v": 0},
            {"voltage_tolerance_v": 0},
            {"voltage_tolerance_v": 4.0},
            {"maximum_voltage_v": 3.9},
            {"target_voltage_v": True},
            {"maximum_voltage_v": float("inf")},
        )

        for changes in cases:
            with self.subTest(changes=changes):
                settings = {
                    "target_voltage_v": 4.0,
                    "voltage_tolerance_v": 0.02,
                    "maximum_voltage_v": 4.1,
                    **changes,
                }

                with self.assertRaises(ValueError):
                    VoltageSourceCVClassifier(**settings)

    def test_classifier_integrates_with_trace_monitor(self):
        phase = {
            "type": "charge_cc_cv",
            "current_magnitude_a": 1.0,
            "termination_current_a": 0.1,
            "termination_hold_time_s": 2.0,
            "termination_requires_cv": True,
            "maximum_duration_s": 100.0,
            "maximum_cv_duration_s": 40.0,
        }
        saved = []

        monitor = ChargeTraceMonitor(
            phase,
            save_batch=saved.append,
            cv_classifier=self.classifier,
            instrument_timestamp_origin_s=0.0,
            phase_elapsed_at_origin_s=0.0,
            maximum_sample_gap_s=1.1,
        )

        result = monitor.ingest([
            # Expected current limiting: not CV.
            FakeReading(
                timestamp_s=0,
                voltage_v=3.5,
                current_a=1.0,
                status=2,
            ),
            # Qualified CV observations below termination current.
            FakeReading(timestamp_s=1),
            FakeReading(timestamp_s=2),
            FakeReading(timestamp_s=3),
        ])

        self.assertEqual(
            result.stop_reason, "cv_current_termination"
        )
        self.assertEqual(result.triggering_record_index, 3)
        self.assertEqual(result.records_saved, 4)
        self.assertEqual(len(saved[0]), 4)

    def test_compliance_resets_termination_hold(self):
        phase = {
            "type": "charge_cc_cv",
            "current_magnitude_a": 1.0,
            "termination_current_a": 0.1,
            "termination_hold_time_s": 2.0,
            "termination_requires_cv": True,
            "maximum_duration_s": 100.0,
            "maximum_cv_duration_s": 40.0,
        }

        monitor = ChargeTraceMonitor(
            phase,
            save_batch=lambda batch: None,
            cv_classifier=self.classifier,
            instrument_timestamp_origin_s=0.0,
            phase_elapsed_at_origin_s=0.0,
            maximum_sample_gap_s=1.1,
        )

        result = monitor.ingest([
            FakeReading(timestamp_s=0),
            FakeReading(timestamp_s=1),
            FakeReading(timestamp_s=2, status=2),
            FakeReading(timestamp_s=3),
            FakeReading(timestamp_s=4),
        ])

        self.assertIsNone(result.stop_reason)

        final = monitor.ingest([
            FakeReading(timestamp_s=5),
        ])

        self.assertEqual(
            final.stop_reason, "cv_current_termination"
        )
        self.assertEqual(final.triggering_record_index, 5)


if __name__ == "__main__":
    unittest.main()