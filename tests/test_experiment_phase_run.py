"""Offline tests for experiment-phase recording finalization."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acquisition.spot_acquisition import SMUReading
from analysis.validate_recorded_run import validate_run
from experiments.phase_outcome import PhaseOutcomeError
from recording.experiment_phase_run import ExperimentPhaseRun


class ExperimentPhaseRunTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

        self.phase = {
            "phase_index": 0,
            "cycle": 1,
            "type": "charge_cc_cv",
            "acquisition_budget": {
                "record_count": 100,
            },
        }

        self.report = {
            "state": "returned",
            "phase_type": "charge",
            "expected_stop_reason": "cv_current_termination",
            "stop_reason": "cv_current_termination",
            "requested_count": 100,
            "retained_count": 3,
            "records_flushed": 3,
            "records_saved_before_finalization": 2,
            "triggering_record_index": 1,
            "trailing_record_count": 1,
            "fault_record_indices": [],
            "shutdown_verified": True,
            "finalization_attempted": True,
            "finalized": True,
            "error": None,
            "cleanup_error": None,
            "finalization_error": None,
        }

        self.readings = tuple(
            SMUReading(
                channel=1,
                voltage_v=4.1,
                current_a=0.05,
                timestamp_s=float(index),
                status=0,
            )
            for index in range(3)
        )

        self.run = ExperimentPhaseRun(
            self.root,
            "synthetic_charge",
            metadata={
                "channel": 1,
                "experiment": "synthetic",
            },
        )
        self.addCleanup(self.run.close)

    def write_records(self):
        self.run.writer.write_batch(self.readings)

    def finish(self):
        return self.run.finish_phase(
            self.phase,
            self.report,
            self.readings,
        )

    def test_success_creates_qualified_result_and_complete(self):
        self.write_records()

        result, directory = self.finish()

        self.assertEqual(
            result.stop_reason, "cv_current_termination"
        )
        self.assertEqual(directory, self.run.directory)
        self.assertTrue((directory / "COMPLETE").is_file())
        self.assertTrue(self.run.writer.closed)
        self.assertTrue(self.run.finalized)
        self.assertTrue(
            self.run.finalization_evidence.metadata_finalized
        )

    def test_export_is_accepted_by_existing_validator(self):
        self.write_records()
        self.finish()

        validation = validate_run(self.run.directory)

        self.assertTrue(
            validation["valid"], validation["errors"]
        )
        self.assertEqual(validation["rows_read"], 3)
        self.assertEqual(validation["warnings"], [])

    def test_metadata_preserves_phase_and_transport_report(self):
        self.write_records()
        self.finish()

        document = json.loads(
            (self.run.directory / "metadata.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(
            document["experiment_phase"], self.phase
        )
        self.assertEqual(
            document["phase_transport_report"], self.report
        )
        self.assertEqual(
            document["outcome"]["triggering_record_index"], 1
        )
        self.assertNotIn(
            "decision_elapsed_s", document["outcome"]
        )

    def test_discharge_phase_can_be_finalized(self):
        self.phase["type"] = "discharge_cc"
        self.report.update({
            "phase_type": "discharge",
            "expected_stop_reason": "voltage_min",
            "stop_reason": "voltage_min",
        })
        self.readings = tuple(
            SMUReading(
                channel=1,
                voltage_v=3.0 - 0.2 * index,
                current_a=-1.0,
                timestamp_s=float(index),
                status=1,
            )
            for index in range(3)
        )
        self.write_records()

        result, _ = self.finish()

        self.assertEqual(result.stop_reason, "voltage_min")

    def test_fault_hidden_by_report_is_rejected(self):
        self.readings = (
            *self.readings[:2],
            SMUReading(
                channel=1,
                voltage_v=4.1,
                current_a=0.05,
                timestamp_s=2.0,
                status=1 << 3,
            ),
        )
        self.write_records()

        with self.assertRaises(PhaseOutcomeError):
            self.finish()

        self.assertFalse(
            (self.run.directory / "COMPLETE").exists()
        )

    def test_actual_record_count_must_match(self):
        self.write_records()

        with self.assertRaises(PhaseOutcomeError):
            self.run.finish_phase(
                self.phase,
                self.report,
                self.readings[:2],
            )

    def test_missing_csv_rows_prevent_success(self):
        with self.assertRaises(PhaseOutcomeError):
            self.finish()

        self.assertFalse(
            (self.run.directory / "COMPLETE").exists()
        )

    def test_metadata_failure_creates_no_complete(self):
        self.write_records()
        original = OSError("simulated metadata failure")

        with patch.object(
            self.run,
            "_replace_metadata",
            side_effect=original,
        ):
            with self.assertRaises(OSError) as caught:
                self.finish()

        self.assertIs(caught.exception, original)
        self.assertTrue(self.run.writer.closed)
        self.assertFalse(self.run.finalized)
        self.assertFalse(
            (self.run.directory / "COMPLETE").exists()
        )

    def test_inherited_failure_recording_creates_no_complete(self):
        self.write_records()
        original = TimeoutError("synthetic timeout")
        original.partial_readings = self.readings

        self.run.fail(original)

        self.assertTrue(self.run.writer.closed)
        self.assertFalse(
            (self.run.directory / "COMPLETE").exists()
        )

        validation = validate_run(self.run.directory)
        self.assertTrue(
            validation["valid"], validation["errors"]
        )
        self.assertEqual(validation["state"], "raised")

    def test_finalized_recording_cannot_be_reused(self):
        self.write_records()
        self.finish()

        with self.assertRaises(RuntimeError):
            self.finish()

    def test_phase_and_report_are_not_modified(self):
        self.write_records()
        phase_before = deepcopy(self.phase)
        report_before = deepcopy(self.report)

        self.finish()

        self.assertEqual(self.phase, phase_before)
        self.assertEqual(self.report, report_before)


if __name__ == "__main__":
    unittest.main()