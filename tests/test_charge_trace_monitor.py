"""Offline tests for charge trace-batch monitoring."""

from dataclasses import dataclass
import unittest

from experiments.charge_controller import ChargeFaultError
from experiments.charge_trace_monitor import (
    ChargeTraceMonitor,
    MeasurementGapError,
)


@dataclass(frozen=True)
class FakeReading:
    timestamp_s: float
    current_a: float
    cv_active: bool
    has_fault: bool = False


class ChargeTraceMonitorTests(unittest.TestCase):
    def setUp(self):
        self.phase = {
            "type": "charge_cc_cv",
            "current_magnitude_a": 1.0,
            "termination_current_a": 0.1,
            "termination_hold_time_s": 2.0,
            "termination_requires_cv": True,
            "maximum_duration_s": 100.0,
            "maximum_cv_duration_s": 40.0,
        }
        self.saved_batches = []

    def make_monitor(self, **changes):
        arguments = {
            "save_batch": self.saved_batches.append,
            "cv_classifier": lambda reading: reading.cv_active,
            "instrument_timestamp_origin_s": 100.0,
            "phase_elapsed_at_origin_s": 0.0,
            "maximum_sample_gap_s": 1.1,
            **changes,
        }

        return ChargeTraceMonitor(
            self.phase,
            **arguments,
        )

    def test_hold_spans_multiple_batches(self):
        monitor = self.make_monitor()

        first = monitor.ingest([
            FakeReading(100, 1.0, False),
            FakeReading(101, 0.09, True),
        ])
        last = monitor.ingest([
            FakeReading(102, 0.09, True),
            FakeReading(103, 0.09, True),
        ])

        self.assertIsNone(first.stop_reason)
        self.assertEqual(
            last.stop_reason, "cv_current_termination"
        )
        self.assertEqual(last.triggering_record_index, 3)
        self.assertEqual(last.records_saved, 4)
        self.assertEqual(last.records_evaluated, 4)

    def test_batch_is_saved_before_classification(self):
        events = []

        def save(batch):
            events.append("save")
            self.saved_batches.append(batch)

        def classify(reading):
            events.append("classify")
            return reading.cv_active

        monitor = self.make_monitor(
            save_batch=save,
            cv_classifier=classify,
        )

        monitor.ingest([FakeReading(100, 1.0, False)])

        self.assertEqual(events, ["save", "classify"])

    def test_records_after_trigger_are_also_saved(self):
        monitor = self.make_monitor()
        batch = [
            FakeReading(100, 0.09, True),
            FakeReading(101, 0.09, True),
            FakeReading(102, 0.09, True),
            FakeReading(103, 0.09, True),
        ]

        result = monitor.ingest(batch)

        self.assertEqual(result.triggering_record_index, 2)
        self.assertEqual(result.records_saved, 4)
        self.assertEqual(result.records_evaluated, 3)
        self.assertEqual(
            self.saved_batches, [tuple(batch)]
        )

    def test_gap_across_batches_is_rejected_after_saving(self):
        monitor = self.make_monitor()
        monitor.ingest([FakeReading(100, 0.09, True)])
        second = [FakeReading(103, 0.09, True)]

        with self.assertRaises(MeasurementGapError):
            monitor.ingest(second)

        self.assertEqual(
            self.saved_batches[-1], tuple(second)
        )
        self.assertEqual(monitor.records_saved, 2)
        self.assertEqual(monitor.state, "failed")

    def test_repeated_timestamp_is_rejected(self):
        monitor = self.make_monitor()

        with self.assertRaises(ValueError):
            monitor.ingest([
                FakeReading(100, 1.0, False),
                FakeReading(100, 1.0, False),
            ])

        self.assertEqual(monitor.records_saved, 2)

    def test_saving_failure_prevents_evaluation_and_retry(self):
        original = OSError("simulated disk error")

        def fail(batch):
            raise original

        monitor = self.make_monitor(save_batch=fail)

        with self.assertRaises(OSError) as caught:
            monitor.ingest([FakeReading(100, 1.0, False)])

        self.assertIs(caught.exception, original)
        self.assertEqual(monitor.records_saved, 0)
        self.assertEqual(monitor.records_evaluated, 0)
        self.assertEqual(monitor.state, "failed")

        with self.assertRaises(RuntimeError):
            monitor.ingest([FakeReading(100, 1.0, False)])

    def test_fault_record_is_saved_before_error(self):
        monitor = self.make_monitor()
        batch = [
            FakeReading(100, 1.0, False, has_fault=True)
        ]

        with self.assertRaises(ChargeFaultError):
            monitor.ingest(batch)

        self.assertEqual(
            self.saved_batches, [tuple(batch)]
        )
        self.assertEqual(monitor.state, "failed")

    def test_explicit_time_mapping_reaches_controller(self):
        monitor = self.make_monitor(
            phase_elapsed_at_origin_s=5.0,
        )

        monitor.ingest([FakeReading(100, 0.5, True)])

        decision = monitor.controller.update(
            elapsed_s=6.0,
            current_a=0.5,
            cv_active=True,
        )
        self.assertEqual(decision.first_cv_elapsed_s, 5.0)

    def test_invalid_cv_classification_is_rejected(self):
        monitor = self.make_monitor(
            cv_classifier=lambda reading: "CV"
        )

        with self.assertRaises(ValueError):
            monitor.ingest([FakeReading(100, 1.0, False)])

        self.assertEqual(monitor.records_saved, 1)
        self.assertEqual(monitor.state, "failed")

    def test_completed_monitor_cannot_be_reused(self):
        monitor = self.make_monitor()
        monitor.ingest([
            FakeReading(100, 0.09, True),
            FakeReading(101, 0.09, True),
            FakeReading(102, 0.09, True),
        ])

        with self.assertRaises(RuntimeError):
            monitor.ingest([FakeReading(103, 0.09, True)])

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
            monitor.ingest([FakeReading(99, 1.0, False)])

        self.assertEqual(monitor.records_saved, 1)


if __name__ == "__main__":
    unittest.main()