"""Offline tests for prepared-phase recording coordination."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_experiment_phase_run import ExperimentPhaseRunTests

from recording.experiment_phase_run import (
    acquire_prepared_phase_and_finalize,
)


class FakeTransport:
    def __init__(self, acquisition, writer, readings, report):
        self.acquisition = acquisition
        self.writer = writer
        self.readings = readings
        self.report = report
        self.error = None
        self.calls = []

    def _acquire(self, phase, **arguments):
        self.calls.append((phase["type"], arguments))
        self.writer.write_batch(self.readings)
        self.acquisition.readings = self.readings

        if self.error is not None:
            raise self.error

        return self.report

    def acquire_charge(self, phase, **arguments):
        return self._acquire(phase, **arguments)

    def acquire_discharge(self, phase, **arguments):
        return self._acquire(phase, **arguments)


class PhaseRecordedAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ExperimentPhaseRunTests(
            methodName=(
                "test_success_creates_qualified_result_and_complete"
            )
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

        self.run = self.fixture.run
        self.acquisition = SimpleNamespace(readings=())

        self.transport = FakeTransport(
            self.acquisition,
            self.run.writer,
            self.fixture.readings,
            self.fixture.report,
        )

        self.prepared = SimpleNamespace(
            phase=self.fixture.phase,
            acquisition=self.acquisition,
            transport=self.transport,
            cv_classifier=object(),
        )

    def acquire(self):
        return acquire_prepared_phase_and_finalize(
            self.prepared,
            self.run,
            instrument_timestamp_origin_s=0.0,
            phase_elapsed_at_origin_s=0.0,
            maximum_sample_gap_s=1.1,
            maximum_voltage_v=4.2,
        )

    def metadata(self):
        return json.loads(
            (self.run.directory / "metadata.json").read_text(
                encoding="utf-8"
            )
        )

    def test_success_returns_qualified_phase_result(self):
        result, directory = self.acquire()

        self.assertEqual(
            result.stop_reason,
            "cv_current_termination",
        )
        self.assertEqual(directory, self.run.directory)
        self.assertTrue((directory / "COMPLETE").is_file())
        self.assertEqual(len(self.transport.calls), 1)

    def test_discharge_dispatch(self):
        self.fixture.phase["type"] = "discharge_cc"
        self.fixture.report.update({
            "phase_type": "discharge",
            "expected_stop_reason": "voltage_min",
            "stop_reason": "voltage_min",
        })

        result, _ = self.acquire()

        self.assertEqual(result.stop_reason, "voltage_min")
        self.assertEqual(
            self.transport.calls[0][0],
            "discharge_cc",
        )

    def test_wrong_writer_is_rejected_before_acquisition(self):
        self.transport.writer = object()

        with self.assertRaisesRegex(ValueError, "exactly"):
            self.acquire()

        self.assertEqual(self.transport.calls, [])
        self.assertTrue(self.run.writer.closed)
        self.assertEqual(
            self.metadata()["outcome"]["state"],
            "raised",
        )

    def test_acquisition_error_is_preserved(self):
        original = TimeoutError("synthetic timeout")
        original.charge_loop_report = {
            "state": "raised",
            "shutdown_verified": True,
            "cleanup_error": None,
        }
        self.transport.error = original

        with self.assertRaises(TimeoutError) as caught:
            self.acquire()

        self.assertIs(caught.exception, original)
        self.assertEqual(
            original.partial_readings,
            self.fixture.readings,
        )
        self.assertEqual(
            original.run_directory,
            self.run.directory,
        )

        outcome = self.metadata()["outcome"]
        self.assertTrue(outcome["shutdown_verified"])
        self.assertEqual(
            outcome["shutdown_evidence"],
            "smu_shutdown_returned",
        )
        self.assertIn(
            "charge_loop_report",
            outcome["phase_reports"],
        )
        self.assertFalse(
            (self.run.directory / "COMPLETE").exists()
        )

    def test_failed_shutdown_evidence_is_preserved(self):
        original = RuntimeError("acquisition failed")
        original.discharge_loop_report = {
            "state": "raised",
            "shutdown_verified": False,
            "cleanup_error": {
                "type": "OSError",
                "message": "shutdown communication failed",
            },
        }
        self.transport.error = original

        with self.assertRaises(RuntimeError):
            self.acquire()

        outcome = self.metadata()["outcome"]
        self.assertIs(outcome["shutdown_verified"], False)
        self.assertEqual(
            outcome["shutdown_evidence"],
            "smu_shutdown_raised",
        )

    def test_absent_shutdown_evidence_remains_unknown(self):
        self.transport.error = RuntimeError("no report")

        with self.assertRaises(RuntimeError):
            self.acquire()

        self.assertIsNone(
            self.metadata()["outcome"]["shutdown_verified"]
        )

    def test_conflicting_shutdown_evidence_remains_unknown(self):
        original = RuntimeError("conflicting reports")
        original.charge_loop_report = {
            "shutdown_verified": True,
            "cleanup_error": None,
        }
        original.discharge_loop_report = {
            "shutdown_verified": False,
            "cleanup_error": {
                "type": "OSError",
                "message": "shutdown failed",
            },
        }
        self.transport.error = original

        with self.assertRaises(RuntimeError):
            self.acquire()

        self.assertIsNone(
            self.metadata()["outcome"]["shutdown_verified"]
        )

    def test_existing_shutdown_report_is_not_overwritten(self):
        original = RuntimeError("existing malformed report")
        original.shutdown_report = {"unexpected": True}
        original.charge_loop_report = {
            "shutdown_verified": True,
            "cleanup_error": None,
        }
        self.transport.error = original

        with self.assertRaises(RuntimeError):
            self.acquire()

        self.assertEqual(
            original.shutdown_report,
            {"unexpected": True},
        )
        outcome = self.metadata()["outcome"]
        self.assertIsNone(outcome["shutdown_verified"])
        self.assertIsNotNone(
            outcome["shutdown_report_error"]
        )

    def test_returned_report_is_preserved_if_qualification_fails(self):
        self.fixture.report["stop_reason"] = "completed"

        with self.assertRaises(RuntimeError) as caught:
            self.acquire()

        self.assertEqual(
            caught.exception.phase_transport_report,
            self.fixture.report,
        )
        outcome = self.metadata()["outcome"]
        self.assertEqual(outcome["state"], "raised")
        self.assertIn(
            "phase_transport_report",
            outcome["phase_reports"],
        )
        self.assertTrue(outcome["shutdown_verified"])
        self.assertFalse(
            (self.run.directory / "COMPLETE").exists()
        )

    def test_failure_export_error_does_not_replace_original(self):
        original = TimeoutError("primary acquisition error")
        self.transport.error = original

        with patch.object(
            self.run,
            "fail",
            side_effect=OSError("metadata unavailable"),
        ):
            with self.assertRaises(TimeoutError) as caught:
                self.acquire()

        self.assertIs(caught.exception, original)
        self.assertTrue(self.run.writer.closed)
        self.assertTrue(
            any(
                "metadata unavailable" in note
                for note in original.__notes__
            )
        )

    def test_malformed_phase_report_is_preserved_as_report_error(self):
        original = RuntimeError("malformed report")
        original.charge_loop_report = {
            "shutdown_verified": True,
            "cleanup_error": None,
            "invalid_number": float("nan"),
        }
        self.transport.error = original

        with self.assertRaises(RuntimeError):
            self.acquire()

        outcome = self.metadata()["outcome"]
        self.assertIn(
            "charge_loop_report",
            outcome["phase_report_errors"],
        )
        self.assertIsNone(outcome["shutdown_verified"])


if __name__ == "__main__":
    unittest.main()