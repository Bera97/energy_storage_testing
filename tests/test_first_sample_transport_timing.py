"""Offline integration tests for first-sample-relative transports."""

from types import SimpleNamespace
import unittest

from test_charge_trace_transport import (
    FakeAcquisition,
    FakeWriter,
)

from acquisition.charge_trace_transport import ChargeTraceTransport
from acquisition.discharge_trace_transport import (
    DischargeTraceTransport,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


def reading(timestamp, voltage, current, status):
    return SimpleNamespace(
        timestamp_s=timestamp,
        voltage_v=voltage,
        current_a=current,
        status=status,
        in_compliance=False,
        has_fault=False,
    )


class FirstSampleTransportTimingTests(unittest.TestCase):
    def setUp(self):
        self.writer = FakeWriter()
        self.clock = FakeClock()

        self.charge_phase = {
            "type": "charge_cc_cv",
            "termination_requires_cv": True,
            "current_magnitude_a": 1.0,
            "termination_current_a": 0.1,
            "termination_hold_time_s": 2.0,
            "maximum_duration_s": 10.0,
            "maximum_cv_duration_s": 5.0,
        }

        self.discharge_phase = {
            "type": "discharge_cc",
            "current_magnitude_a": 1.0,
            "cutoff_voltage_v": 2.7,
            "maximum_duration_s": 10.0,
        }

    def common_arguments(self):
        return {
            "instrument_timestamp_origin_s": None,
            "phase_elapsed_at_origin_s": None,
            "maximum_sample_gap_s": 1.1,
            "phase_started_s": 0.0,
            "clock": self.clock,
            "sleep": self.clock.sleep,
            "measurement_time_basis": "first_recorded_sample",
        }

    def test_charge_hold_uses_relative_measurement_times(self):
        records = [
            reading(100.0, 4.1, 0.05, 0),
            reading(101.0, 4.1, 0.05, 0),
            reading(102.0, 4.1, 0.05, 0),
        ]
        acquisition = FakeAcquisition([records, []])
        transport = ChargeTraceTransport(
            acquisition,
            self.writer,
        )

        report = transport.acquire_charge(
            self.charge_phase,
            cv_classifier=lambda item: True,
            **self.common_arguments(),
        )

        self.assertEqual(
            report["stop_reason"],
            "cv_current_termination",
        )
        self.assertEqual(
            report["triggering_record_index"],
            2,
        )
        self.assertEqual(report["records_flushed"], 3)
        self.assertEqual(
            report["measurement_timing"][
                "instrument_timestamp_origin_s"
            ],
            100.0,
        )
        self.assertFalse(
            report["measurement_timing"][
                "output_enable_time_established"
            ]
        )

    def test_discharge_cutoff_uses_relative_measurement_times(self):
        records = [
            reading(100.0, 3.8, -1.0, 1),
            reading(101.0, 2.7, -1.0, 1),
        ]
        acquisition = FakeAcquisition([records, []])
        transport = DischargeTraceTransport(
            acquisition,
            self.writer,
        )

        report = transport.acquire_discharge(
            self.discharge_phase,
            maximum_voltage_v=4.2,
            **self.common_arguments(),
        )

        self.assertEqual(
            report["stop_reason"],
            "voltage_min",
        )
        self.assertEqual(report["records_flushed"], 2)
        self.assertEqual(
            report["measurement_timing"][
                "measurement_time_basis"
            ],
            "first_recorded_sample",
        )

    def test_first_sample_mode_requires_host_phase_start(self):
        acquisition = FakeAcquisition([])
        transport = ChargeTraceTransport(
            acquisition,
            self.writer,
        )
        arguments = self.common_arguments()
        arguments["phase_started_s"] = None

        with self.assertRaisesRegex(
            ValueError,
            "host phase start",
        ):
            transport.acquire_charge(
                self.charge_phase,
                cv_classifier=lambda item: True,
                **arguments,
            )

        self.assertEqual(acquisition.commands, [])

    def test_first_sample_mode_rejects_explicit_origins(self):
        acquisition = FakeAcquisition([])
        transport = DischargeTraceTransport(
            acquisition,
            self.writer,
        )
        arguments = self.common_arguments()
        arguments["instrument_timestamp_origin_s"] = 0.0

        with self.assertRaisesRegex(
            ValueError,
            "origins",
        ):
            transport.acquire_discharge(
                self.discharge_phase,
                maximum_voltage_v=4.2,
                **arguments,
            )

        self.assertEqual(acquisition.commands, [])

    def test_expired_host_deadline_prevents_initiation(self):
        acquisition = FakeAcquisition([[]])
        transport = ChargeTraceTransport(
            acquisition,
            self.writer,
        )
        self.clock.now = 10.0

        with self.assertRaises(TimeoutError) as caught:
            transport.acquire_charge(
                self.charge_phase,
                cv_classifier=lambda item: True,
                **self.common_arguments(),
            )

        self.assertNotIn(
            ":INIT:ACQ (@1)",
            acquisition.commands,
        )
        report = caught.exception.charge_loop_report
        self.assertTrue(report["shutdown_verified"])
        self.assertIsNone(
            report["measurement_timing"][
                "instrument_timestamp_origin_s"
            ]
        )

    def test_unknown_policy_is_rejected_before_initiation(self):
        acquisition = FakeAcquisition([])
        transport = DischargeTraceTransport(
            acquisition,
            self.writer,
        )
        arguments = self.common_arguments()
        arguments["measurement_time_basis"] = "unknown"

        with self.assertRaises(ValueError):
            transport.acquire_discharge(
                self.discharge_phase,
                maximum_voltage_v=4.2,
                **arguments,
            )

        self.assertEqual(acquisition.commands, [])


if __name__ == "__main__":
    unittest.main()