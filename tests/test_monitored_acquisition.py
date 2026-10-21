"""Offline tests: no VISA connection or hardware required."""

import unittest

from acquisition.monitored_acquisition import (
    MonitoredAcquisition,
    StopLimits,
)
from acquisition.spot_acquisition import SMUReading


def reading(time_s=0.0, voltage=1.0, current=144e-6, status=3200):
    return SMUReading(
        channel=1,
        voltage_v=voltage,
        current_a=current,
        timestamp_s=time_s,
        status=status,
    )


class FakeSMU:
    def __init__(self, shutdown_error=False):
        self.shutdown_calls = 0
        self.shutdown_error = shutdown_error

    def shutdown(self):
        self.shutdown_calls += 1
        if self.shutdown_error:
            raise RuntimeError("simulated shutdown failure")


class FakeAcquisition(MonitoredAcquisition):
    def __init__(self, batches, *, idle=True, shutdown_error=False):
        super().__init__(FakeSMU(shutdown_error), 1)
        self._count = sum(len(batch) for batch in batches)
        self._interval_s = 0.2
        self._trace_ready = True
        self.batches = list(batches)
        self.idle = idle
        self.commands = []

    def _preflight(self):
        if not self._trace_ready:
            raise RuntimeError("Not configured.")

    def _write(self, command):
        self.commands.append(command)

    def _register(self, command):
        return (1 << 4) if self.idle else 0

    def _check_lost_events(self):
        pass

    def _read_new(self, records):
        batch = self.batches.pop(0) if self.batches else []
        records.extend(batch)
        self.readings = tuple(records)
        return batch


class StopLimitsTests(unittest.TestCase):
    def test_no_threshold(self):
        self.assertIsNone(StopLimits().reason_for(reading()))

    def test_inclusive_voltage_limit(self):
        limits = StopLimits(max_voltage_v=1.0)
        self.assertEqual(limits.reason_for(reading()), "voltage_max")

    def test_signed_current_limit(self):
        limits = StopLimits(min_current_a=-100e-6)
        self.assertEqual(
            limits.reason_for(reading(current=-100e-6)),
            "current_min",
        )
        self.assertIsNone(
            limits.reason_for(reading(current=100e-6))
        )

    def test_compliance(self):
        limits = StopLimits()
        self.assertEqual(
            limits.reason_for(reading(status=3200 | 2)),
            "compliance",
        )

    def test_fault_priority(self):
        limits = StopLimits(max_voltage_v=0.9)
        self.assertEqual(
            limits.reason_for(reading(status=3200 | (1 << 3))),
            "measurement_fault",
        )

    def test_invalid_limits(self):
        for kwargs in (
            {"max_voltage_v": float("nan")},
            {"min_current_a": True},
            {"min_voltage_v": 2.0, "max_voltage_v": 1.0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    StopLimits(**kwargs)


class AcquisitionTests(unittest.TestCase):
    def test_completion_and_shutdown(self):
        acquisition = FakeAcquisition([[reading()]])
        result = acquisition.acquire()
        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(len(result.readings), 1)
        self.assertEqual(acquisition._smu.shutdown_calls, 1)

    def test_early_stop_retains_post_decision_records(self):
        first = reading()
        second = reading(time_s=0.2)
        acquisition = FakeAcquisition(
            [[first], [second]],
            idle=False,
        )
        result = acquisition.acquire(
            StopLimits(max_voltage_v=0.9)
        )
        self.assertEqual(result.stop_reason, "voltage_max")
        self.assertEqual(result.triggering_reading, first)
        self.assertEqual(result.readings, (first, second))
        self.assertEqual(acquisition._smu.shutdown_calls, 1)

    def test_fault_record_retained(self):
        fault = reading(status=3200 | (1 << 3))
        acquisition = FakeAcquisition([[fault]], idle=False)
        result = acquisition.acquire()
        self.assertEqual(result.stop_reason, "measurement_fault")
        self.assertEqual(result.fault_readings, (fault,))

    def test_timeout_preserves_partial_records(self):
        acquisition = FakeAcquisition([[reading()]], idle=False)
        with self.assertRaises(TimeoutError) as caught:
            acquisition.acquire(
                timeout_s=0.02,
                poll_interval_s=0.03,
            )
        self.assertEqual(len(caught.exception.partial_readings), 1)
        self.assertEqual(acquisition._smu.shutdown_calls, 1)

    def test_interrupt_preserved_if_shutdown_fails(self):
        acquisition = FakeAcquisition(
            [[reading()]],
            shutdown_error=True,
        )

        def interrupted():
            raise KeyboardInterrupt("simulated interruption")

        acquisition._check_lost_events = interrupted

        with self.assertRaises(KeyboardInterrupt) as caught:
            acquisition.acquire()

        self.assertEqual(caught.exception.partial_readings, ())
        self.assertTrue(
            any(
                "Shutdown could not be verified" in note
                for note in caught.exception.__notes__
            )
        )

    def test_cannot_reuse_without_configuration(self):
        acquisition = FakeAcquisition([[reading()]])
        acquisition.acquire()

        with self.assertRaises(RuntimeError):
            acquisition.acquire()

    def test_real_trace_parser_preserves_fault_batch(self):
        acquisition = MonitoredAcquisition(FakeSMU(), 1)
        acquisition._count = 1
        acquisition._trace_count = lambda: 1
        acquisition._query = lambda command: (
            f"1,0.000144,0,{3200 | (1 << 3)}"
        )

        records = []
        batch = acquisition._read_new(records)

        self.assertEqual(len(records), 1)
        self.assertTrue(batch[0].has_fault)

    def test_real_trace_parser_rejects_cross_batch_time(self):
        acquisition = MonitoredAcquisition(FakeSMU(), 1)
        acquisition._count = 2
        acquisition._trace_count = lambda: 2
        acquisition._query = lambda command: "1,0.000144,0,3200"

        with self.assertRaises(RuntimeError):
            acquisition._read_new([reading(time_s=0.2)])


if __name__ == "__main__":
    unittest.main()