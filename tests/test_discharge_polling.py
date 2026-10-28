"""Offline integration tests for discharge monitoring and polling."""

from dataclasses import dataclass
import unittest

from experiments.phase_polling import (
    DischargeAcquisitionExhausted,
    DischargeShutdownError,
    TracePoll,
    run_discharge_polling,
)
from experiments.discharge_trace_monitor import (
    DischargeTraceMonitor,
)
from experiments.phase_polling import (
    UnexpectedPhaseTermination,
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


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class DischargePollingTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.events = []
        self.saved = []
        self.polls = []

        phase = {
            "type": "discharge_cc",
            "current_magnitude_a": 1.0,
            "cutoff_voltage_v": 2.7,
            "maximum_duration_s": 100.0,
        }

        self.monitor = DischargeTraceMonitor(
            phase,
            save_batch=self.saved.append,
            instrument_timestamp_origin_s=0.0,
            phase_elapsed_at_origin_s=0.0,
            maximum_sample_gap_s=1.1,
            maximum_voltage_v=4.2,
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
            "timeout_s": 10.0,
            "poll_interval_s": 1.0,
            "clock": self.clock,
            "sleep": self.clock.sleep,
            **changes,
        }

        return run_discharge_polling(**arguments)

    def test_cutoff_shuts_down_then_finalizes(self):
        self.polls = [
            TracePoll(
                (
                    FakeReading(0, 3.8),
                    FakeReading(1, 2.7),
                ),
                False,
            )
        ]

        report = self.run_loop()

        self.assertEqual(report["state"], "returned")
        self.assertEqual(report["phase_type"], "discharge")
        self.assertEqual(report["stop_reason"], "voltage_min")
        self.assertEqual(report["triggering_record_index"], 1)
        self.assertEqual(
            self.events,
            ["start", "poll", "shutdown", "finalize"],
        )

    def test_cutoff_at_last_requested_record_is_accepted(self):
        self.polls = [
            TracePoll(
                (
                    FakeReading(0, 3.8),
                    FakeReading(1, 2.7),
                ),
                True,
            )
        ]

        report = self.run_loop(requested_count=2)

        self.assertEqual(report["state"], "returned")

    def test_count_exhaustion_without_cutoff_is_failure(self):
        self.polls = [
            TracePoll((FakeReading(0, 3.8),), False)
        ]

        with self.assertRaises(
            DischargeAcquisitionExhausted
        ) as caught:
            self.run_loop(requested_count=1)

        report = caught.exception.discharge_loop_report
        self.assertEqual(report["state"], "raised")
        self.assertTrue(report["shutdown_verified"])
        self.assertTrue(report["finalized"])

    def test_no_measurements_still_reaches_deadline(self):
        with self.assertRaises(TimeoutError) as caught:
            self.run_loop(timeout_s=3)

        self.assertEqual(self.clock.now, 3)
        self.assertTrue(
            caught.exception.discharge_loop_report[
                "shutdown_verified"
            ]
        )

    def test_external_phase_start_is_respected(self):
        self.clock.now = 5

        with self.assertRaises(TimeoutError):
            self.run_loop(
                phase_started_s=0,
                timeout_s=10,
            )

        self.assertEqual(self.clock.now, 10)

    def test_interrupt_preserves_original_exception(self):
        original = KeyboardInterrupt()

        def interrupted_poll():
            raise original

        with self.assertRaises(KeyboardInterrupt) as caught:
            self.run_loop(poll=interrupted_poll)

        self.assertIs(caught.exception, original)
        self.assertEqual(
            self.events[-2:],
            ["shutdown", "finalize"],
        )

    def test_failed_shutdown_prevents_finalization(self):
        self.polls = [
            TracePoll((FakeReading(0, 2.7),), False)
        ]

        with self.assertRaises(DischargeShutdownError):
            self.run_loop(shutdown_verified=lambda: False)

        self.assertNotIn("finalize", self.events)

    def test_wrong_endpoint_is_rejected(self):
        class WrongMonitor:
            state = "running"
            records_saved = 0

            def ingest(self, readings):
                self.records_saved += len(readings)

                class Decision:
                    stop_reason = "cv_current_termination"
                    triggering_record_index = 0

                return Decision()

        self.polls = [
            TracePoll((FakeReading(0, 3.8),), False)
        ]

        with self.assertRaises(UnexpectedPhaseTermination):
            self.run_loop(monitor=WrongMonitor())

        self.assertEqual(
            self.events[-2:],
            ["shutdown", "finalize"],
        )


if __name__ == "__main__":
    unittest.main()