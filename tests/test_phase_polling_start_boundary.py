"""Deadline regression tests for the shared phase polling loop."""

from types import SimpleNamespace
import unittest

from experiments.phase_polling import (
    TracePoll,
    run_phase_polling,
)


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class EndpointMonitor:
    """Minimal monitor returning a charge endpoint on a nonempty batch."""

    def __init__(self):
        self.state = "running"
        self.records_saved = 0
        self.saved = []

    def ingest(self, readings):
        records = tuple(readings)
        self.saved.extend(records)
        self.records_saved += len(records)

        if not records:
            return SimpleNamespace(
                stop_reason=None,
                triggering_record_index=None,
            )

        self.state = "completed"

        return SimpleNamespace(
            stop_reason="cv_current_termination",
            triggering_record_index=self.records_saved - 1,
        )


class PhasePollingStartBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.monitor = EndpointMonitor()
        self.calls = []

    def start(self):
        self.calls.append("start")

    def poll(self):
        self.calls.append("poll")
        return TracePoll(
            readings=("synthetic endpoint record",),
            idle=False,
        )

    def shutdown(self):
        self.calls.append("shutdown")
        return True

    def finalize(self):
        self.calls.append("finalize")

    def run_loop(self, **changes):
        return run_phase_polling(
            **{
                "phase_type": "charge",
                "expected_stop_reason": (
                    "cv_current_termination"
                ),
                "monitor": self.monitor,
                "start": self.start,
                "poll": self.poll,
                "shutdown_verified": self.shutdown,
                "finalize": self.finalize,
                "requested_count": 10,
                "timeout_s": 5.0,
                "poll_interval_s": 0.1,
                "phase_started_s": 0.0,
                "clock": self.clock,
                "sleep": self.clock.sleep,
                **changes,
            }
        )

    def test_expired_phase_does_not_start_acquisition(self):
        self.clock.now = 5.0

        with self.assertRaises(TimeoutError) as caught:
            self.run_loop()

        self.assertEqual(
            self.calls,
            ["shutdown", "finalize"],
        )
        report = caught.exception.phase_loop_report
        self.assertEqual(report["state"], "raised")
        self.assertTrue(report["shutdown_verified"])
        self.assertTrue(report["finalized"])
        self.assertIsNone(report["stop_reason"])

    def test_startup_consuming_deadline_prevents_polling(self):
        def slow_start():
            self.calls.append("start")
            self.clock.now = 5.0

        with self.assertRaises(TimeoutError):
            self.run_loop(start=slow_start)

        self.assertEqual(
            self.calls,
            ["start", "shutdown", "finalize"],
        )
        self.assertEqual(self.monitor.records_saved, 0)

    def test_time_before_polling_entry_counts_toward_deadline(self):
        self.clock.now = 4.8

        def slow_start():
            self.calls.append("start")
            self.clock.now += 0.3

        with self.assertRaises(TimeoutError) as caught:
            self.run_loop(start=slow_start)

        self.assertNotIn("poll", self.calls)
        report = caught.exception.phase_loop_report
        self.assertEqual(
            report["phase_started_monotonic_s"],
            0.0,
        )
        self.assertEqual(
            report["host_deadline_origin"],
            "caller_supplied_phase_start",
        )

    def test_late_endpoint_batch_is_saved_but_not_accepted(self):
        def late_poll():
            self.calls.append("poll")
            self.clock.now = 5.0
            return TracePoll(
                readings=("late endpoint record",),
                idle=False,
            )

        with self.assertRaises(TimeoutError) as caught:
            self.run_loop(poll=late_poll)

        self.assertEqual(
            self.monitor.saved,
            ["late endpoint record"],
        )

        report = caught.exception.phase_loop_report
        self.assertEqual(
            report["records_saved_before_finalization"],
            1,
        )
        self.assertIsNone(report["stop_reason"])
        self.assertTrue(report["shutdown_verified"])
        self.assertTrue(report["finalized"])

    def test_endpoint_before_deadline_returns_success(self):
        self.clock.now = 4.0

        report = self.run_loop()

        self.assertEqual(report["state"], "returned")
        self.assertEqual(
            report["stop_reason"],
            "cv_current_termination",
        )
        self.assertEqual(
            self.calls,
            ["start", "poll", "shutdown", "finalize"],
        )

    def test_start_error_is_preserved_after_cleanup(self):
        original = OSError("synthetic initiation failure")

        def failed_start():
            self.calls.append("start")
            raise original

        with self.assertRaises(OSError) as caught:
            self.run_loop(start=failed_start)

        self.assertIs(caught.exception, original)
        self.assertEqual(
            self.calls,
            ["start", "shutdown", "finalize"],
        )
        self.assertTrue(
            original.phase_loop_report["shutdown_verified"]
        )

    def test_cleanup_failure_does_not_replace_start_error(self):
        original = OSError("synthetic initiation failure")

        def failed_start():
            self.calls.append("start")
            raise original

        def failed_shutdown():
            self.calls.append("shutdown")
            raise RuntimeError("synthetic shutdown failure")

        with self.assertRaises(OSError) as caught:
            self.run_loop(
                start=failed_start,
                shutdown_verified=failed_shutdown,
            )

        self.assertIs(caught.exception, original)
        self.assertNotIn("finalize", self.calls)
        self.assertFalse(
            original.phase_loop_report["shutdown_verified"]
        )


if __name__ == "__main__":
    unittest.main()