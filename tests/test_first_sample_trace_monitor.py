"""Offline tests for first-sample-relative trace timing."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from experiments.charge_trace_monitor import ChargeTraceMonitor
from experiments.first_sample_trace_monitor import (
    FirstSampleTraceMonitor,
)


class RecordingController:
    def __init__(self, phase):
        self.elapsed_values = []

    def update(
        self,
        *,
        elapsed_s,
        current_a,
        cv_active,
        has_fault,
    ):
        self.elapsed_values.append(elapsed_s)
        return SimpleNamespace(stop_reason=None)


class FirstSampleTraceMonitorTests(unittest.TestCase):
    def setUp(self):
        self.saved_batches = []
        self.constructed_monitors = []

        patcher = patch(
            "experiments.charge_trace_monitor.ChargeController",
            RecordingController,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        def factory(origin):
            monitor = ChargeTraceMonitor(
                {"type": "charge_cc_cv"},
                save_batch=self.saved_batches.append,
                cv_classifier=lambda reading: False,
                instrument_timestamp_origin_s=origin,
                phase_elapsed_at_origin_s=0.0,
                maximum_sample_gap_s=2.0,
            )
            self.constructed_monitors.append(monitor)
            return monitor

        self.monitor = FirstSampleTraceMonitor(factory)

    @staticmethod
    def reading(timestamp):
        return SimpleNamespace(
            timestamp_s=timestamp,
            current_a=1.0,
            has_fault=False,
        )

    def active_controller(self):
        return self.constructed_monitors[-1].controller

    def test_first_sample_defines_zero_elapsed(self):
        self.monitor.ingest([
            self.reading(100.0),
            self.reading(101.0),
        ])

        self.assertEqual(
            self.active_controller().elapsed_values,
            [0.0, 1.0],
        )
        self.assertEqual(
            self.monitor.instrument_timestamp_origin_s,
            100.0,
        )

    def test_origin_is_not_reset_for_later_batches(self):
        self.monitor.ingest([self.reading(100.0)])
        self.monitor.ingest([self.reading(101.5)])

        self.assertEqual(
            self.active_controller().elapsed_values,
            [0.0, 1.5],
        )
        self.assertEqual(
            self.monitor.instrument_timestamp_origin_s,
            100.0,
        )

    def test_empty_batches_do_not_establish_origin(self):
        decision = self.monitor.ingest([])

        self.assertEqual(decision.records_saved, 0)
        self.assertIsNone(
            self.monitor.instrument_timestamp_origin_s
        )
        self.assertEqual(len(self.constructed_monitors), 1)

    def test_first_batch_is_saved_once(self):
        first = self.reading(100.0)
        second = self.reading(101.0)

        self.monitor.ingest([first, second])

        self.assertEqual(
            self.saved_batches,
            [(first, second)],
        )
        self.assertEqual(self.monitor.records_saved, 2)
        self.assertEqual(self.monitor.records_evaluated, 2)
        self.assertEqual(len(self.constructed_monitors), 2)

    def test_provisional_monitor_never_evaluates_valid_records(self):
        self.monitor.ingest([self.reading(100.0)])

        provisional = self.constructed_monitors[0]
        self.assertEqual(provisional.records_saved, 0)
        self.assertEqual(
            provisional.controller.elapsed_values,
            [],
        )

    def test_invalid_first_timestamp_is_preserved_and_rejected(self):
        reading = self.reading(float("nan"))

        with self.assertRaises(ValueError):
            self.monitor.ingest([reading])

        self.assertEqual(self.saved_batches, [(reading,)])
        self.assertEqual(self.monitor.state, "failed")
        self.assertEqual(self.monitor.records_saved, 1)

    def test_timestamp_gap_checks_still_apply(self):
        self.monitor.ingest([self.reading(100.0)])

        with self.assertRaises(RuntimeError):
            # MeasurementGapError inherits RuntimeError.
            self.monitor.ingest([self.reading(103.0)])

        self.assertEqual(self.monitor.state, "failed")
        self.assertEqual(self.monitor.records_saved, 2)

    def test_failed_adapter_cannot_be_reused(self):
        with self.assertRaises(ValueError):
            self.monitor.ingest([
                self.reading(float("nan"))
            ])

        with self.assertRaisesRegex(
            RuntimeError, "terminal"
        ):
            self.monitor.ingest([self.reading(100.0)])

    def test_evidence_does_not_claim_output_enable_time(self):
        self.monitor.ingest([self.reading(100.0)])
        evidence = self.monitor.timing_evidence

        self.assertEqual(
            evidence["measurement_time_basis"],
            "first_recorded_sample",
        )
        self.assertEqual(
            evidence["instrument_timestamp_origin_s"],
            100.0,
        )
        self.assertEqual(
            evidence["measurement_elapsed_at_origin_s"],
            0.0,
        )
        self.assertFalse(
            evidence["output_enable_time_established"]
        )
        self.assertTrue(
            evidence["host_phase_deadline_required"]
        )

    def test_invalid_factory_is_rejected(self):
        with self.assertRaises(ValueError):
            FirstSampleTraceMonitor(None)


if __name__ == "__main__":
    unittest.main()