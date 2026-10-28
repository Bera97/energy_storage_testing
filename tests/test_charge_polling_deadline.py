"""Offline tests for externally established charge-phase deadlines."""

from types import SimpleNamespace
import unittest

from experiments.phase_polling import (
    TracePoll,
    run_charge_polling,
)


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeMonitor:
    def __init__(self):
        self.state = "running"
        self.records_saved = 0

    def ingest(self, readings):
        self.records_saved += len(readings)
        return SimpleNamespace(
            stop_reason=None,
            triggering_record_index=None,
        )


class ChargePollingDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(105.0)
        self.monitor = FakeMonitor()
        self.events = []

    def start(self):
        self.events.append("start")

    def poll(self):
        self.events.append("poll")
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
            "requested_count": 100,
            "timeout_s": 10.0,
            "poll_interval_s": 1.0,
            "phase_started_s": 100.0,
            "clock": self.clock,
            "sleep": self.clock.sleep,
            **changes,
        }
        return run_charge_polling(**arguments)

    def test_preexisting_phase_time_counts_toward_timeout(self):
        with self.assertRaises(TimeoutError) as caught:
            self.run_loop()

        # Five seconds had already elapsed at function entry.
        self.assertEqual(self.clock.now, 110.0)
        report = caught.exception.charge_loop_report
        self.assertEqual(
            report["phase_started_monotonic_s"], 100.0
        )
        self.assertEqual(
            report["host_deadline_origin"],
            "caller_supplied_phase_start",
        )
        self.assertEqual(
            self.events[-2:], ["shutdown", "finalize"]
        )

    def test_expired_phase_never_starts_acquisition(self):
        self.clock.now = 110.0

        with self.assertRaises(TimeoutError):
            self.run_loop()

        self.assertEqual(
            self.events, ["shutdown", "finalize"]
        )

    def test_time_spent_starting_acquisition_counts(self):
        def slow_start():
            self.events.append("start")
            self.clock.now = 111.0

        with self.assertRaises(TimeoutError):
            self.run_loop(start=slow_start)

        self.assertNotIn("poll", self.events)
        self.assertEqual(
            self.events,
            ["start", "shutdown", "finalize"],
        )

    def test_omitted_origin_preserves_entry_time_behavior(self):
        with self.assertRaises(TimeoutError) as caught:
            self.run_loop(phase_started_s=None)

        self.assertEqual(self.clock.now, 115.0)
        self.assertEqual(
            caught.exception.charge_loop_report[
                "host_deadline_origin"
            ],
            "polling_function_entry",
        )

    def test_future_origin_is_rejected_before_execution(self):
        with self.assertRaisesRegex(ValueError, "future"):
            self.run_loop(phase_started_s=106.0)

        self.assertEqual(self.events, [])

    def test_invalid_origin_is_rejected(self):
        for value in (True, float("nan"), float("inf"), "100"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.run_loop(phase_started_s=value)

        self.assertEqual(self.events, [])

    def test_backwards_clock_is_failure_with_cleanup(self):
        def backwards_poll():
            self.clock.now = 104.0
            return TracePoll((), False)

        with self.assertRaisesRegex(
            RuntimeError, "backwards"
        ):
            self.run_loop(poll=backwards_poll)

        self.assertEqual(
            self.events[-2:], ["shutdown", "finalize"]
        )


if __name__ == "__main__":
    unittest.main()