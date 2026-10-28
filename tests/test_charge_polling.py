"""Offline tests for charge polling, deadlines, and cleanup."""

from dataclasses import dataclass
import unittest

from experiments.phase_polling import (
    ChargeAcquisitionExhausted,
    ChargeShutdownError,
    TracePoll,
    run_charge_polling,
)
from experiments.charge_trace_monitor import ChargeTraceMonitor


@dataclass(frozen=True)
class FakeReading:
    timestamp_s: float
    current_a: float = 0.05
    has_fault: bool = False


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ChargePollingTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.events = []
        self.saved = []
        self.polls = []

        phase = {
            "type": "charge_cc_cv",
            "current_magnitude_a": 1.0,
            "termination_current_a": 0.1,
            "termination_hold_time_s": 2.0,
            "termination_requires_cv": True,
            "maximum_duration_s": 100.0,
            "maximum_cv_duration_s": 40.0,
        }

        self.monitor = ChargeTraceMonitor(
            phase,
            save_batch=self.saved.append,
            cv_classifier=lambda reading: True,
            instrument_timestamp_origin_s=0.0,
            phase_elapsed_at_origin_s=0.0,
            maximum_sample_gap_s=1.1,
        )

    def start(self):
        self.events.append("start")

    def poll(self):
        self.events.append("poll")

        if self.polls:
            return self.polls.pop(0)

        return TracePoll((), False)

    def shutdown(self):
        self.events.append("shutdown")
        return True

    def finalize(self):
        self.events.append("finalize")

    def run_loop(self, **changes):
        arguments = {
            "monitor": self.monitor,
            "start": self.start,
            "poll": self.poll,
            "shutdown_verified": self.shutdown,
            "finalize": self.finalize,
            "requested_count": 10,
            "timeout_s": 10,
            "poll_interval_s": 1,
            "clock": self.clock,
            "sleep": self.clock.sleep,
            **changes,
        }
        return run_charge_polling(**arguments)

    def successful_batch(self):
        return TracePoll(
            (
                FakeReading(0),
                FakeReading(1),
                FakeReading(2),
            ),
            False,
        )

    def test_success_shuts_down_before_finalizing(self):
        self.polls = [self.successful_batch()]

        report = self.run_loop()

        self.assertEqual(report["state"], "returned")
        self.assertEqual(
            report["stop_reason"], "cv_current_termination"
        )
        self.assertTrue(report["shutdown_verified"])
        self.assertTrue(report["finalized"])
        self.assertEqual(
            self.events,
            ["start", "poll", "shutdown", "finalize"],
        )

    def test_endpoint_at_final_record_is_accepted(self):
        self.polls = [self.successful_batch()]

        report = self.run_loop(requested_count=3)

        self.assertEqual(report["state"], "returned")

    def test_count_exhaustion_without_endpoint_is_failure(self):
        self.polls = [
            TracePoll((FakeReading(0),), False)
        ]

        with self.assertRaises(ChargeAcquisitionExhausted) as caught:
            self.run_loop(requested_count=1)

        report = caught.exception.charge_loop_report
        self.assertEqual(report["state"], "raised")
        self.assertTrue(report["shutdown_verified"])
        self.assertTrue(report["finalized"])

    def test_idle_without_endpoint_is_failure(self):
        self.polls = [TracePoll((), True)]

        with self.assertRaises(ChargeAcquisitionExhausted):
            self.run_loop()

        self.assertEqual(
            self.events[-2:], ["shutdown", "finalize"]
        )

    def test_no_records_still_reaches_host_deadline(self):
        with self.assertRaises(TimeoutError) as caught:
            self.run_loop(timeout_s=3)

        self.assertEqual(self.clock.now, 3)
        self.assertTrue(
            caught.exception.charge_loop_report["shutdown_verified"]
        )

    def test_slow_poll_preserves_batch_then_reports_timeout(self):
        def slow_poll():
            self.clock.now = 11
            return TracePoll((FakeReading(0),), False)

        with self.assertRaises(TimeoutError):
            self.run_loop(poll=slow_poll)

        self.assertEqual(len(self.saved), 1)

    def test_interrupt_preserves_identity_and_attempts_cleanup(self):
        original = KeyboardInterrupt()

        def interrupted_poll():
            raise original

        with self.assertRaises(KeyboardInterrupt) as caught:
            self.run_loop(poll=interrupted_poll)

        self.assertIs(caught.exception, original)
        self.assertEqual(
            self.events[-2:], ["shutdown", "finalize"]
        )

    def test_failed_shutdown_skips_finalization(self):
        self.polls = [self.successful_batch()]

        def failed_shutdown():
            self.events.append("shutdown")
            return False

        with self.assertRaises(ChargeShutdownError) as caught:
            self.run_loop(shutdown_verified=failed_shutdown)

        self.assertNotIn("finalize", self.events)
        self.assertFalse(
            caught.exception.charge_loop_report["shutdown_verified"]
        )

    def test_finalization_failure_is_not_retried(self):
        self.polls = [self.successful_batch()]
        original = OSError("finalization failed")
        attempts = []

        def failed_finalize():
            attempts.append(1)
            raise original

        with self.assertRaises(OSError) as caught:
            self.run_loop(finalize=failed_finalize)

        self.assertIs(caught.exception, original)
        self.assertEqual(len(attempts), 1)
        self.assertTrue(
            original.charge_loop_report["shutdown_verified"]
        )

    def test_cleanup_finalization_failure_preserves_original(self):
        original = TimeoutError("poll failed")

        def failed_poll():
            raise original

        def failed_finalize():
            raise OSError("disk failed")

        with self.assertRaises(TimeoutError) as caught:
            self.run_loop(
                poll=failed_poll,
                finalize=failed_finalize,
            )

        self.assertIs(caught.exception, original)
        self.assertEqual(
            original.charge_loop_report["finalization_error"]["type"],
            "OSError",
        )

    def test_overcount_is_rejected_after_batch_saving(self):
        self.polls = [self.successful_batch()]

        with self.assertRaisesRegex(
            RuntimeError, "exceed"
        ):
            self.run_loop(requested_count=2)

        self.assertEqual(len(self.saved[0]), 3)


if __name__ == "__main__":
    unittest.main()