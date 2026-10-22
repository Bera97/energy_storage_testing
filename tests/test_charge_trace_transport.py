"""Offline tests for the charge trace transport."""

from dataclasses import dataclass
import unittest

from acquisition.charge_trace_transport import ChargeTraceTransport


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


class FakeSMU:
    def __init__(self):
        self.shutdown_calls = 0

    def shutdown(self):
        self.shutdown_calls += 1


class FakeAcquisition:
    def __init__(self, batches):
        self.channel = 1
        self._count = 10
        self._trace_ready = True
        self._smu = FakeSMU()
        self.readings = ()
        self.commands = []
        self.batches = list(batches)
        self.preflight_calls = 0

    def _preflight(self):
        self.preflight_calls += 1

    def _write(self, command):
        self.commands.append(command)

    def _register(self, command):
        return 0

    def _check_lost_events(self):
        pass

    def _read_new(self, records):
        batch = self.batches.pop(0) if self.batches else []
        records.extend(batch)
        self.readings = tuple(records)
        return batch


class FakeWriter:
    def __init__(self):
        self.closed = False
        self.failed = False
        self.records_flushed = 0
        self.rows = []
        self.error = None

    def write_batch(self, batch):
        if self.error is not None:
            self.failed = True
            raise self.error

        self.rows.extend(batch)
        self.records_flushed += len(batch)
        return len(batch)


class ChargeTraceTransportTests(unittest.TestCase):
    def setUp(self):
        self.phase = {
            "type": "charge_cc_cv",
            "current_magnitude_a": 1.0,
            "termination_current_a": 0.1,
            "termination_hold_time_s": 2.0,
            "termination_requires_cv": True,
            "maximum_duration_s": 100.0,
            "maximum_cv_duration_s": 40.0,
        }
        self.writer = FakeWriter()
        self.clock = FakeClock()

    def run_transport(self, acquisition, **changes):
        transport = ChargeTraceTransport(
            acquisition,
            self.writer,
        )
        arguments = {
            "cv_classifier": lambda reading: True,
            "instrument_timestamp_origin_s": 0.0,
            "phase_elapsed_at_origin_s": 0.0,
            "maximum_sample_gap_s": 1.1,
            "clock": self.clock,
            "sleep": self.clock.sleep,
            **changes,
        }

        report = transport.acquire_charge(
            self.phase,
            **arguments,
        )
        return transport, report

    def successful_batch(self):
        return [
            FakeReading(0),
            FakeReading(1),
            FakeReading(2),
        ]

    def test_success_starts_acquisition_and_shuts_down(self):
        acquisition = FakeAcquisition([
            self.successful_batch(),
            [],
        ])

        _, report = self.run_transport(acquisition)

        self.assertEqual(report["state"], "returned")
        self.assertEqual(report["retained_count"], 3)
        self.assertEqual(report["records_flushed"], 3)
        self.assertEqual(acquisition._smu.shutdown_calls, 1)
        self.assertEqual(
            acquisition.commands,
            [
                ":INIT:ACQ (@1)",
                ":TRAC1:FEED:CONT NEV",
            ],
        )
        self.assertFalse(acquisition._trace_ready)

    def test_post_shutdown_records_are_saved_once(self):
        trailing = FakeReading(3)
        acquisition = FakeAcquisition([
            self.successful_batch(),
            [trailing],
        ])

        _, report = self.run_transport(acquisition)

        self.assertEqual(report["triggering_record_index"], 2)
        self.assertEqual(report["retained_count"], 4)
        self.assertEqual(report["records_flushed"], 4)
        self.assertEqual(report["trailing_record_count"], 1)
        self.assertEqual(
            self.writer.rows,
            self.successful_batch() + [trailing],
        )

    def test_trailing_fault_is_preserved_and_reported(self):
        trailing = FakeReading(3, has_fault=True)
        acquisition = FakeAcquisition([
            self.successful_batch(),
            [trailing],
        ])

        _, report = self.run_transport(acquisition)

        self.assertEqual(report["fault_record_indices"], [3])
        self.assertTrue(self.writer.rows[-1].has_fault)

    def test_csv_failure_preserves_original_error_and_records(self):
        original = OSError("simulated disk failure")
        self.writer.error = original
        acquisition = FakeAcquisition([
            self.successful_batch(),
            [],
        ])

        with self.assertRaises(OSError) as caught:
            self.run_transport(acquisition)

        self.assertIs(caught.exception, original)
        self.assertEqual(len(original.partial_readings), 3)
        self.assertGreaterEqual(
            acquisition._smu.shutdown_calls, 1
        )
        self.assertEqual(
            original.charge_loop_report["state"], "raised"
        )
        self.assertFalse(acquisition._trace_ready)

    def test_closed_writer_is_rejected_before_start(self):
        self.writer.closed = True
        acquisition = FakeAcquisition([])

        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.run_transport(acquisition)

        self.assertEqual(acquisition.commands, [])

    def test_nonfresh_writer_is_rejected(self):
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
            transport.acquire_charge(
                self.phase,
                cv_classifier=lambda reading: True,
                instrument_timestamp_origin_s=0,
                phase_elapsed_at_origin_s=0,
                maximum_sample_gap_s=1.1,
            )

    def test_unconfigured_acquisition_is_rejected(self):
        acquisition = FakeAcquisition([])
        acquisition._trace_ready = False

        with self.assertRaisesRegex(RuntimeError, "Configure"):
            self.run_transport(acquisition)

        self.assertEqual(acquisition.commands, [])


if __name__ == "__main__":
    unittest.main()