"""Offline integration tests for acquisition shutdown reports.

Exercises the real acquire() exception handler.
No VISA connection or hardware required.
"""

import unittest

from drivers.monitored_acquisition import MonitoredAcquisition
from drivers.spot_acquisition import SMUReading


class FakeSMU:
    def __init__(self, shutdown_error=None):
        self.shutdown_calls = 0
        self.shutdown_error = shutdown_error

    def shutdown(self):
        self.shutdown_calls += 1

        if self.shutdown_error is not None:
            raise self.shutdown_error


class InterruptingAcquisition(MonitoredAcquisition):
    """Retain one batch, then raise the supplied original exception."""

    def __init__(self, smu, original_error):
        super().__init__(smu, channel=1)
        self._count = 100
        self._interval_s = 0.2
        self._trace_ready = True
        self.original_error = original_error
        self.delivered = False

        self.record = SMUReading(
            channel=1,
            voltage_v=1.303,
            current_a=-100e-6,
            timestamp_s=0.0,
            status=3201,
        )

    def _preflight(self):
        if not self._trace_ready:
            raise RuntimeError("Not configured.")

    def _write(self, command):
        pass

    def _register(self, command):
        return 0

    def _check_lost_events(self):
        pass

    def _read_new(self, records):
        if self.delivered:
            raise self.original_error

        self.delivered = True
        records.append(self.record)
        self.readings = tuple(records)
        return [self.record]


class MonitoredShutdownReportingTests(unittest.TestCase):
    def test_cancellation_records_successful_shutdown(self):
        original = KeyboardInterrupt("operator cancellation")
        smu = FakeSMU()
        acquisition = InterruptingAcquisition(smu, original)

        with self.assertRaises(KeyboardInterrupt) as caught:
            acquisition.acquire(
                timeout_s=1.0,
                poll_interval_s=0.001,
            )

        self.assertIs(caught.exception, original)
        self.assertEqual(smu.shutdown_calls, 1)
        self.assertEqual(
            original.partial_readings,
            (acquisition.record,),
        )
        self.assertIs(
            original.shutdown_report["verified"],
            True,
        )
        self.assertEqual(
            original.shutdown_report["evidence"],
            "smu_shutdown_returned",
        )
        self.assertFalse(acquisition._trace_ready)

    def test_failed_shutdown_does_not_mask_cancellation(self):
        original = KeyboardInterrupt("operator cancellation")
        smu = FakeSMU(
            RuntimeError("simulated shutdown failure")
        )
        acquisition = InterruptingAcquisition(smu, original)

        with self.assertRaises(KeyboardInterrupt) as caught:
            acquisition.acquire(
                timeout_s=1.0,
                poll_interval_s=0.001,
            )

        self.assertIs(caught.exception, original)
        self.assertEqual(smu.shutdown_calls, 1)
        self.assertIs(
            original.shutdown_report["verified"],
            False,
        )
        self.assertEqual(
            original.shutdown_report["error_type"],
            "RuntimeError",
        )
        self.assertEqual(
            original.partial_readings,
            (acquisition.record,),
        )
        self.assertTrue(
            any(
                "Shutdown could not be verified" in note
                for note in original.__notes__
            )
        )

    def test_non_cancellation_error_gets_the_same_reporting(self):
        original = OSError("simulated retrieval failure")
        smu = FakeSMU()
        acquisition = InterruptingAcquisition(smu, original)

        with self.assertRaises(OSError) as caught:
            acquisition.acquire(
                timeout_s=1.0,
                poll_interval_s=0.001,
            )

        self.assertIs(caught.exception, original)
        self.assertEqual(smu.shutdown_calls, 1)
        self.assertIs(
            original.shutdown_report["verified"],
            True,
        )
        self.assertEqual(
            acquisition.readings,
            (acquisition.record,),
        )


if __name__ == "__main__":
    unittest.main()