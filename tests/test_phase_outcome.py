"""Offline tests for experiment phase outcome qualification."""

from copy import deepcopy
from dataclasses import replace
import unittest

from experiments.phase_outcome import (
    PhaseOutcomeError,
    RecordingFinalization,
    qualify_phase_outcome,
)


class PhaseOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.phase = {
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
            "retained_count": 5,
            "records_flushed": 5,
            "records_saved_before_finalization": 4,
            "triggering_record_index": 2,
            "trailing_record_count": 1,
            "fault_record_indices": [],
            "shutdown_verified": True,
            "finalization_attempted": True,
            "finalized": True,
            "error": None,
            "cleanup_error": None,
            "finalization_error": None,
        }

        self.recording = RecordingFinalization(
            writer_closed=True,
            writer_failed=False,
            close_error=None,
            metadata_finalized=True,
        )

    def qualify(self, **changes):
        return qualify_phase_outcome(
            changes.get("phase", self.phase),
            changes.get("report", self.report),
            recording=changes.get(
                "recording", self.recording
            ),
        )

    def test_charge_endpoint_qualifies(self):
        result = self.qualify()

        self.assertEqual(
            result.stop_reason,
            "cv_current_termination",
        )

    def test_discharge_endpoint_qualifies(self):
        self.phase["type"] = "discharge_cc"
        self.report.update({
            "phase_type": "discharge",
            "expected_stop_reason": "voltage_min",
            "stop_reason": "voltage_min",
        })

        result = self.qualify()

        self.assertEqual(result.stop_reason, "voltage_min")

    def test_wrong_endpoint_does_not_qualify(self):
        self.report["stop_reason"] = "completed"

        with self.assertRaises(PhaseOutcomeError):
            self.qualify()

    def test_wrong_phase_type_does_not_qualify(self):
        self.report["phase_type"] = "discharge"

        with self.assertRaises(PhaseOutcomeError):
            self.qualify()

    def test_raised_transport_does_not_qualify(self):
        self.report["state"] = "raised"

        with self.assertRaises(PhaseOutcomeError):
            self.qualify()

    def test_trailing_fault_blocks_phase_success(self):
        self.report["fault_record_indices"] = [4]

        with self.assertRaisesRegex(
            PhaseOutcomeError,
            "measurement faults",
        ):
            self.qualify()

    def test_missing_fault_evidence_is_rejected(self):
        del self.report["fault_record_indices"]

        with self.assertRaises(PhaseOutcomeError):
            self.qualify()

    def test_shutdown_and_trace_finalization_must_be_explicit(self):
        for field in (
            "shutdown_verified",
            "finalization_attempted",
            "finalized",
        ):
            for value in (False, None, 1):
                with self.subTest(field=field, value=value):
                    report = deepcopy(self.report)
                    report[field] = value

                    with self.assertRaises(PhaseOutcomeError):
                        self.qualify(report=report)

    def test_transport_errors_block_success(self):
        for field in (
            "error",
            "cleanup_error",
            "finalization_error",
        ):
            with self.subTest(field=field):
                report = deepcopy(self.report)
                report[field] = {
                    "type": "RuntimeError",
                    "message": "synthetic failure",
                }

                with self.assertRaises(PhaseOutcomeError):
                    self.qualify(report=report)

    def test_missing_transport_error_evidence_is_rejected(self):
        report = deepcopy(self.report)
        del report["cleanup_error"]

        with self.assertRaises(PhaseOutcomeError):
            self.qualify(report=report)

    def test_inconsistent_counts_are_rejected(self):
        cases = (
            {"records_flushed": 4},
            {"retained_count": 101},
            {"records_saved_before_finalization": 6},
            {"records_saved_before_finalization": 0},
            {"requested_count": True},
            {"trailing_record_count": 2},
        )

        for changes in cases:
            with self.subTest(changes=changes):
                report = {
                    **self.report,
                    **changes,
                }

                with self.assertRaises(PhaseOutcomeError):
                    self.qualify(report=report)

    def test_trigger_must_reference_prefinalization_records(self):
        for trigger in (None, True, -1, 4, 5):
            with self.subTest(trigger=trigger):
                report = {
                    **self.report,
                    "triggering_record_index": trigger,
                }

                with self.assertRaises(PhaseOutcomeError):
                    self.qualify(report=report)

    def test_acquisition_budget_must_match(self):
        self.phase["acquisition_budget"]["record_count"] = 99

        with self.assertRaises(PhaseOutcomeError):
            self.qualify()

    def test_writer_and_metadata_finalization_are_required(self):
        cases = (
            {"writer_closed": False},
            {"writer_closed": 1},
            {"writer_failed": True},
            {"writer_failed": None},
            {"close_error": "simulated close failure"},
            {"metadata_finalized": False},
            {"metadata_finalized": 1},
        )

        for changes in cases:
            with self.subTest(changes=changes):
                evidence = replace(
                    self.recording,
                    **changes,
                )

                with self.assertRaises(PhaseOutcomeError):
                    self.qualify(recording=evidence)

    def test_missing_recording_evidence_is_rejected(self):
        with self.assertRaises(PhaseOutcomeError):
            self.qualify(recording=None)

    def test_inputs_are_not_modified(self):
        before_phase = deepcopy(self.phase)
        before_report = deepcopy(self.report)

        self.qualify()

        self.assertEqual(self.phase, before_phase)
        self.assertEqual(self.report, before_report)


if __name__ == "__main__":
    unittest.main()