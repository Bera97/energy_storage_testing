"""Offline tests for discharge trace transport."""

from dataclasses import dataclass
import unittest

from test_charge_trace_transport import (
    FakeAcquisition,
    FakeClock,
    FakeWriter,
)

from acquisition.discharge_trace_transport import (
    DischargeTraceTransport,
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


class DischargeTraceTransportTests(unittest.TestCase):
    def setUp(self):
        self.phase = {
            "type": "discharge_cc",
            "current_magnitude_a": 1.0,
            "cutoff_voltage_v": 2.7,
            "maximum_duration_s": 100.0,
        }
        self.writer = FakeWriter()
        self.clock = FakeClock()

    def run_transport(self, acquisition, **changes):
        transport = DischargeTraceTransport(
            acquisition,
            self.writer,
        )
        arguments = {
            "instrument_timestamp_origin_s": 0.0,
            "phase_elapsed_at_origin_s": 0.0,
            "maximum_sample_gap_s": 1.1,
            "maximum_voltage_v": 4.2,
            "clock": self.clock,
            "sleep": self.clock.sleep,
            **changes,
        }

        report = transport.acquire_discharge(
            self.phase,
            **arguments,
        )
        return transport, report

    def successful_batch(self):
        return [
            FakeReading(0, 3.8),
            FakeReading(1, 2.7),
        ]

    def test_success_shuts_down_and_saves_records(self):
        acquisition = FakeAcquisition([
            self.successful_batch(),
            [],
        ])

        _, report = self.run_transport(acquisition)

        self.assertEqual(report["state"], "returned")
        self.assertEqual(report["stop_reason"], "voltage_min")
        self.assertEqual(report["records_flushed"], 2)
        self.assertEqual(acquisition._smu.shutdown_calls, 1)
        self.assertFalse(acquisition._trace_ready)
        self.assertEqual(
            acquisition.commands,
            [
                ":INIT:ACQ (@1)",
                ":TRAC1:FEED:CONT NEV",
            ],
        )

    def test_trailing_records_are_saved_once(self):
        trailing = FakeReading(2, 2.6)
        acquisition = FakeAcquisition([
            self.successful_batch(),
            [trailing],
        ])

        _, report = self.run_transport(acquisition)

        self.assertEqual(report["triggering_record_index"], 1)
        self.assertEqual(report["trailing_record_count"], 1)
        self.assertEqual(report["records_flushed"], 3)
        self.assertEqual(
            self.writer.rows,
            self.successful_batch() + [trailing],
        )

    def test_trailing_fault_remains_visible(self):
        acquisition = FakeAcquisition([
            self.successful_batch(),
            [FakeReading(2, 2.6, status=9)],
        ])

        _, report = self.run_transport(acquisition)

        self.assertEqual(report["fault_record_indices"], [2])

    def test_csv_failure_preserves_original_error(self):
        original = OSError("simulated disk failure")
        self.writer.error = original
        acquisition = FakeAcquisition([
            self.successful_batch(),
            [],
        ])

        with self.assertRaises(OSError) as caught:
            self.run_transport(acquisition)

        self.assertIs(caught.exception, original)
        self.assertEqual(len(original.partial_readings), 2)
        self.assertTrue(
            original.discharge_loop_report[
                "shutdown_verified"
            ]
        )

    def test_nonfresh_writer_is_rejected_before_start(self):
        self.writer.records_flushed = 1
        acquisition = FakeAcquisition([])

        with self.assertRaisesRegex(RuntimeError, "fresh"):
            self.run_transport(acquisition)

        self.assertEqual(acquisition.commands, [])

    def test_transport_cannot_be_reused(self):
        acquisition = FakeAcquisition([
            self.successful_batch(),
            [],
        ])
        transport, _ = self.run_transport(acquisition)

        with self.assertRaisesRegex(RuntimeError, "fresh transport"):
            transport.acquire_discharge(
                self.phase,
                instrument_timestamp_origin_s=0,
                phase_elapsed_at_origin_s=0,
                maximum_sample_gap_s=1.1,
                maximum_voltage_v=4.2,
            )


if __name__ == "__main__":
    unittest.main()