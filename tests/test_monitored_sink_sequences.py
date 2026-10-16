"""Offline regression tests for monitored current-sinking sequences.

No VISA connection or hardware required.
"""

import unittest

from drivers.monitored_acquisition import (
    MonitoredAcquisition,
    StopLimits,
)
from drivers.spot_acquisition import SMUReading


def sink_reading(timestamp, voltage):
    return SMUReading(
        channel=1,
        voltage_v=voltage,
        current_a=-100e-6,
        timestamp_s=timestamp,
        status=3201,
    )


class FakeSMU:
    def __init__(self):
        self.shutdown_calls = 0

    def shutdown(self):
        self.shutdown_calls += 1


class ScriptedAcquisition(MonitoredAcquisition):
    """Exercise the real acquire() loop using scripted batches."""

    def __init__(self, batches, requested_count=150):
        super().__init__(FakeSMU(), channel=1)
        self._count = requested_count
        self._interval_s = 0.2
        self._trace_ready = True
        self.batches = list(batches)
        self.commands = []

    def _preflight(self):
        if not self._trace_ready:
            raise RuntimeError("Not configured.")

    def _write(self, command):
        self.commands.append(command)

    def _register(self, command):
        # Acquisition remains active until software termination.
        return 0

    def _check_lost_events(self):
        pass

    def _read_new(self, records):
        batch = self.batches.pop(0) if self.batches else []
        records.extend(batch)
        self.readings = tuple(records)
        return batch


class MonitoredSinkSequenceTests(unittest.TestCase):
    def test_delayed_lower_voltage_crossing(self):
        first = sink_reading(0.0, 1.303)
        second = sink_reading(0.2, 1.20)
        above = sink_reading(0.4, 1.102443)
        trigger = sink_reading(0.6, 1.002429)

        acquisition = ScriptedAcquisition(
            [[first], [second], [above], [trigger]]
        )

        result = acquisition.acquire(
            StopLimits(min_voltage_v=1.10),
            timeout_s=1.0,
            poll_interval_s=0.001,
        )

        self.assertEqual(result.stop_reason, "voltage_min")
        self.assertEqual(result.triggering_reading, trigger)
        self.assertEqual(
            result.readings,
            (first, second, above, trigger),
        )
        self.assertEqual(result.requested_count, 150)
        self.assertLess(len(result.readings), result.requested_count)
        self.assertEqual(acquisition._smu.shutdown_calls, 1)
        self.assertFalse(acquisition._trace_ready)

    def test_inclusive_cutoff_in_middle_of_batch(self):
        above = sink_reading(0.0, 1.30)
        trigger = sink_reading(0.2, 1.10)
        later = sink_reading(0.4, 1.00)

        acquisition = ScriptedAcquisition(
            [[above, trigger, later]]
        )

        result = acquisition.acquire(
            StopLimits(min_voltage_v=1.10),
            timeout_s=1.0,
            poll_interval_s=0.001,
        )

        self.assertEqual(result.stop_reason, "voltage_min")
        self.assertEqual(result.triggering_reading, trigger)
        self.assertEqual(
            result.readings,
            (above, trigger, later),
        )
        self.assertEqual(acquisition._smu.shutdown_calls, 1)

    def test_records_arriving_before_shutdown_are_retained(self):
        above = sink_reading(0.0, 1.30)
        trigger = sink_reading(0.2, 1.05)
        final = sink_reading(0.4, 1.00)

        acquisition = ScriptedAcquisition(
            [[above], [trigger], [final]]
        )

        result = acquisition.acquire(
            StopLimits(min_voltage_v=1.10),
            timeout_s=1.0,
            poll_interval_s=0.001,
        )

        self.assertEqual(result.stop_reason, "voltage_min")
        self.assertEqual(result.triggering_reading, trigger)
        self.assertEqual(
            result.readings,
            (above, trigger, final),
        )
        self.assertEqual(
            acquisition.readings,
            result.readings,
        )
        self.assertEqual(acquisition._smu.shutdown_calls, 1)
        self.assertIn(
            ":TRAC1:FEED:CONT NEV",
            acquisition.commands,
        )


if __name__ == "__main__":
    unittest.main()