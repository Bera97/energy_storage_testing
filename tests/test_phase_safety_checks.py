"""Offline tests for final phase deadline and history checks."""

from types import SimpleNamespace
import unittest

import test_recorded_phase_executor as executor_tests

from experiments.phase_polling import (
    TracePoll,
    run_phase_polling,
)
from experiments.phase_safety_monitor import PhaseSafetyMonitor


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class WaitingChargeMonitor:
    def __init__(self):
        self.state = "running"
        self.records_saved = 0
        self.records_evaluated = 0
        self.controller = SimpleNamespace(
            first_cv_elapsed_s=None,
            maximum_cv_duration_s=5.0,
        )

    def ingest(self, readings):
        records = tuple(readings)
        self.records_saved += len(records)
        self.records_evaluated += len(records)

        if records and self.controller.first_cv_elapsed_s is None:
            self.controller.first_cv_elapsed_s = 0.0

        return SimpleNamespace(
            stop_reason=None,
            triggering_record_index=None,
        )


class PhaseSafetyChecksTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.base = WaitingChargeMonitor()

        self.monitor = PhaseSafetyMonitor(
            self.base,
            phase_started_s=0.0,
            maximum_duration_s=100.0,
            clock=self.clock,
            measurement_time_basis="first_recorded_sample",
            instrument_timestamp_origin_s=None,
            phase_elapsed_at_origin_s=None,
        )

    def test_expired_preflight_prevents_initiation(self):
        commands = []

        def start():
            self.clock.now = 100.0
            self.monitor.before_initiation()
            commands.append("INIT")

        with self.assertRaises(TimeoutError):
            start()

        self.assertEqual(commands, [])

    def test_cv_deadline_is_absent_before_observed_cv(self):
        self.monitor.before_initiation()

        self.assertIsNone(
            self.monitor.host_cv_deadline_s
        )

    def test_cv_deadline_uses_pre_initiation_reference(self):
        self.clock.now = 2.0
        self.monitor.before_initiation()

        self.base.controller.first_cv_elapsed_s = 10.0

        self.assertEqual(
            self.monitor.host_cv_deadline_s,
            17.0,
        )

    def test_cv_timeout_expires_without_more_measurements(self):
        calls = []
        first_poll = True

        def start():
            self.monitor.before_initiation()

        def poll():
            nonlocal first_poll

            if first_poll:
                first_poll = False
                return TracePoll(
                    readings=("first CV observation",),
                    idle=False,
                )

            return TracePoll(readings=(), idle=False)

        def shutdown():
            calls.append("shutdown")
            return True

        def finalize():
            calls.append("finalize")

        with self.assertRaisesRegex(
            TimeoutError,
            "CV deadline",
        ) as caught:
            run_phase_polling(
                phase_type="charge",
                expected_stop_reason="cv_current_termination",
                monitor=self.monitor,
                start=start,
                poll=poll,
                shutdown_verified=shutdown,
                finalize=finalize,
                requested_count=100,
                timeout_s=100.0,
                poll_interval_s=1.0,
                phase_started_s=0.0,
                clock=self.clock,
                sleep=self.clock.sleep,
            )

        self.assertEqual(self.clock.now, 5.0)
        self.assertEqual(self.base.records_saved, 1)
        self.assertEqual(calls, ["shutdown", "finalize"])
        self.assertTrue(
            caught.exception.phase_loop_report[
                "shutdown_verified"
            ]
        )

    def test_explicit_mapping_uses_phase_start_reference(self):
        monitor = PhaseSafetyMonitor(
            self.base,
            phase_started_s=10.0,
            maximum_duration_s=100.0,
            clock=self.clock,
            measurement_time_basis="explicit_mapping",
            instrument_timestamp_origin_s=0.0,
            phase_elapsed_at_origin_s=0.0,
        )

        self.base.controller.first_cv_elapsed_s = 20.0

        self.assertEqual(
            monitor.host_cv_deadline_s,
            35.0,
        )


class ShutdownHistoryPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = executor_tests.RecordedPhaseExecutorTests(
            methodName="test_activation_error_is_preserved"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_previous_shutdown_report_is_saved_in_failure_metadata(self):
        original = OSError("synthetic activation failure")
        previous = {
            "attempted": True,
            "verified": False,
            "evidence": "smu_shutdown_raised",
            "error_type": "OSError",
            "error_message": "earlier shutdown failed",
        }

        original.shutdown_report = previous
        self.fixture.activation_error = original
        phase = self.fixture.phase("charge_cc_cv")

        with self.assertRaises(OSError) as caught:
            self.fixture.executor(phase)

        self.assertIs(caught.exception, original)

        _, document = self.fixture.failure_metadata(phase)
        outcome = document["outcome"]

        self.assertEqual(
            outcome["phase_reports"][
                "executor_previous_shutdown_report"
            ],
            previous,
        )
        self.assertTrue(outcome["shutdown_verified"])


if __name__ == "__main__":
    unittest.main()