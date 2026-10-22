"""Offline tests for discharge trace-batch monitoring."""

from dataclasses import dataclass
import unittest

from experiments.discharge_controller import (
    DischargeComplianceError,
    DischargeFaultError,
    DischargeVoltageError,
)
from experiments.discharge_trace_monitor import (
    DischargeMeasurementGapError,
    DischargeSourceModeError,
    DischargeTraceMonitor,
)


@dataclass(frozen=True)
class FakeReading:
    timestamp_s: float
    voltage_v: float
    current_a: float = -1.0
    status: int = 1

    @property
    def in_compliance(self):
        return bool(self.status & 0b110)

    @property
    def has_fault(self):
        return bool(self.status & (1 << 3))


class DischargeTraceMonitorTests(unittest.TestCase):
    def setUp(self):
        self.phase = {
            "type": "discharge_cc",
            "current_magnitude_a": 1.0,
            "cutoff_voltage_v": 2.7,
            "maximum_duration_s": 100.0,
        }
        self.saved_batches = []

    def make_monitor(self, **changes):
        arguments = {
            "save_batch": self.saved_batches.append,
            "instrument_timestamp_origin_s": 100.0,
            "phase_elapsed_at_origin_s": 0.0,
            "maximum_sample_gap_s": 1.1,
            "maximum_voltage_v": 4.2,
            **changes,
        }

        return DischargeTraceMonitor(
            self.phase,
            **arguments,
        )

    def test_cutoff_across_batches(self):
        monitor = self.make_monitor()

        first = monitor.ingest([
            FakeReading(100, 3.8),
            FakeReading(101, 3.5),
        ])
        last = monitor.ingest([
            FakeReading(102, 3.0),
            FakeReading(103, 2.7),
        ])

        self.assertIsNone(first.stop_reason)
        self.assertEqual(last.stop_reason, "voltage_min")
        self.assertEqual(last.triggering_record_index, 3)
        self.assertEqual(last.records_saved, 4)
        self.assertEqual(last.records_evaluated, 4)

    def test_entire_batch_including_post_cutoff_record_is_saved(self):
        monitor = self.make_monitor()
        batch = [
            FakeReading(100, 3.8),
            FakeReading(101, 2.7),
            FakeReading(102, 2.6),
        ]

        result = monitor.ingest(batch)

        self.assertEqual(result.triggering_record_index, 1)
        self.assertEqual(result.records_saved, 3)
        self.assertEqual(result.records_evaluated, 2)
        self.assertEqual(
            self.saved_batches,
            [tuple(batch)],
        )

    def test_gap_across_batches_is_rejected_after_saving(self):
        monitor = self.make_monitor()
        monitor.ingest([FakeReading(100, 3.8)])
        second = [FakeReading(103, 3.5)]

        with self.assertRaises(DischargeMeasurementGapError):
            monitor.ingest(second)

        self.assertEqual(
            self.saved_batches[-1],
            tuple(second),
        )
        self.assertEqual(monitor.state, "failed")

    def test_repeated_timestamp_is_rejected(self):
        monitor = self.make_monitor()

        with self.assertRaises(ValueError):
            monitor.ingest([
                FakeReading(100, 3.8),
                FakeReading(100, 3.5),
            ])

        self.assertEqual(monitor.records_saved, 2)

    def test_saving_failure_prevents_evaluation_and_retry(self):
        original = OSError("simulated disk failure")

        def failed_save(batch):
            raise original

        monitor = self.make_monitor(
            save_batch=failed_save,
        )

        with self.assertRaises(OSError) as caught:
            monitor.ingest([FakeReading(100, 3.8)])

        self.assertIs(caught.exception, original)
        self.assertEqual(monitor.records_evaluated, 0)
        self.assertEqual(monitor.state, "failed")

        with self.assertRaises(RuntimeError):
            monitor.ingest([FakeReading(100, 3.8)])

    def test_fault_at_cutoff_is_not_success(self):
        monitor = self.make_monitor()
        batch = [
            FakeReading(
                100,
                2.7,
                status=1 | (1 << 3),
            )
        ]

        with self.assertRaises(DischargeFaultError):
            monitor.ingest(batch)

        self.assertEqual(
            self.saved_batches,
            [tuple(batch)],
        )

    def test_compliance_at_cutoff_is_not_success(self):
        monitor = self.make_monitor()

        with self.assertRaises(DischargeComplianceError):
            monitor.ingest([
                FakeReading(100, 2.7, status=3)
            ])

    def test_wrong_source_selection_is_rejected(self):
        monitor = self.make_monitor()

        with self.assertRaises(DischargeSourceModeError):
            monitor.ingest([
                FakeReading(100, 3.8, status=0)
            ])

    def test_voltage_above_ceiling_is_rejected(self):
        monitor = self.make_monitor()

        with self.assertRaises(DischargeVoltageError):
            monitor.ingest([
                FakeReading(100, 4.3)
            ])

    def test_explicit_phase_time_mapping_is_used(self):
        monitor = self.make_monitor(
            phase_elapsed_at_origin_s=5.0,
        )

        monitor.ingest([FakeReading(100, 3.8)])

        with self.assertRaises(ValueError):
            monitor.controller.update(
                elapsed_s=5.0,
                voltage_v=3.7,
                current_a=-1.0,
            )

    def test_completed_monitor_cannot_be_reused(self):
        monitor = self.make_monitor()
        monitor.ingest([FakeReading(100, 2.7)])

        with self.assertRaises(RuntimeError):
            monitor.ingest([FakeReading(101, 2.6)])

    def test_empty_batch_is_a_no_op(self):
        monitor = self.make_monitor()

        result = monitor.ingest([])

        self.assertEqual(result.records_saved, 0)
        self.assertEqual(result.records_evaluated, 0)
        self.assertIsNone(result.stop_reason)
        self.assertEqual(self.saved_batches, [])

    def test_record_before_time_origin_is_rejected(self):
        monitor = self.make_monitor()

        with self.assertRaises(ValueError):
            monitor.ingest([FakeReading(99, 3.8)])

        self.assertEqual(monitor.records_saved, 1)

    def test_invalid_configuration_is_rejected(self):
        for changes in (
            {"maximum_sample_gap_s": 0},
            {"maximum_voltage_v": 2.7},
            {"phase_elapsed_at_origin_s": -1},
            {"instrument_timestamp_origin_s": float("nan")},
            {"save_batch": None},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.make_monitor(**changes)


if __name__ == "__main__":
    unittest.main()