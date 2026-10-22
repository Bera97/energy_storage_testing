"""Offline integration of runner, backend, executor, and recording.

Real:
- Preconditioning sequence orchestration.
- Recorded backend.
- Concrete recorded phase executor.
- Trace transports and endpoint controllers.
- Temporary CSV/JSON recording.
- Recorded-run validation.

Fake:
- Instrument operations.
- Source preparation.
- Output activation.
- Acquisition data.
- Host clock and sleeping.

No instrument connection or real output enabling.
"""

import json
import unittest
from unittest.mock import patch

import test_recorded_phase_executor as executor_tests

from analysis.validate_recorded_run import validate_run
from experiments.preconditioning_runner import (
    run_preconditioning,
)
from experiments.recorded_preconditioning_backend import (
    RecordedPreconditioningBackend,
)
from recording.experiment_phase_run import ExperimentPhaseRun


class RecordedPreconditioningIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = executor_tests.RecordedPhaseExecutorTests(
            methodName="test_charge_returns_finalized_recording"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

        self.executed_indices = []
        self.failure_phase_index = None
        self.phase_error = None
        self.fail_cleanup_after_phase_error = False
        self.cleanup_failure_active = False

        self.normal_shutdown = self.fixture.smu.shutdown

        shutdown_patcher = patch.object(
            self.fixture.smu,
            "shutdown",
            side_effect=self.shutdown,
        )
        shutdown_patcher.start()
        self.addCleanup(shutdown_patcher.stop)

        self.backend = RecordedPreconditioningBackend(
            self.fixture.smu,
            execute_active_phase=self.execute,
            rest_poll_interval_s=1000.0,
            clock=self.fixture.clock,
            sleep=self.fixture.clock.sleep,
        )

    def shutdown(self):
        if self.cleanup_failure_active:
            raise OSError(
                "synthetic integration cleanup failure"
            )

        return self.normal_shutdown()

    def execute(self, phase):
        index = phase["phase_index"]
        self.executed_indices.append(index)

        if index == self.failure_phase_index:
            self.fixture.activation_error = self.phase_error

            if self.fail_cleanup_after_phase_error:
                self.cleanup_failure_active = True
        else:
            self.fixture.activation_error = None

        return self.fixture.executor(phase)

    def run_sequence(self):
        return run_preconditioning(
            self.fixture.document,
            self.backend,
        )

    def directory(self, phase_index):
        return (
            self.fixture.recording_fixture.root
            / f"executor_test_phase_{phase_index}"
        )

    def metadata(self, phase_index):
        return json.loads(
            (
                self.directory(phase_index)
                / "metadata.json"
            ).read_text(encoding="utf-8")
        )

    def test_two_cycles_complete_through_concrete_executor(self):
        report = self.run_sequence()

        self.assertEqual(report["state"], "completed")
        self.assertEqual(
            report["completed_phase_count"],
            8,
        )
        self.assertEqual(
            self.executed_indices,
            [0, 2, 4, 6],
        )
        self.assertEqual(
            [
                entry["type"]
                for entry in self.backend.phase_outcomes
            ],
            [
                "charge_cc_cv",
                "rest",
                "discharge_cc",
                "rest",
            ] * 2,
        )

        self.assertTrue(report["shutdown_verified"])
        self.assertFalse(self.fixture.smu.chan1.on)
        self.assertFalse(self.fixture.smu.chan2.on)

        for index in (0, 2, 4, 6):
            with self.subTest(phase_index=index):
                self.assertTrue(
                    (
                        self.directory(index) / "COMPLETE"
                    ).is_file()
                )

    def test_all_active_recordings_pass_existing_validator(self):
        self.run_sequence()

        for index in (0, 2, 4, 6):
            with self.subTest(phase_index=index):
                validation = validate_run(
                    self.directory(index)
                )

                self.assertTrue(
                    validation["valid"],
                    validation["errors"],
                )
                self.assertEqual(
                    validation["state"],
                    "returned",
                )
                self.assertGreater(
                    validation["rows_read"],
                    0,
                )

    def test_second_cycle_failure_stops_later_phases(self):
        original = OSError(
            "synthetic second-cycle activation failure"
        )
        self.failure_phase_index = 4
        self.phase_error = original

        with self.assertRaises(OSError) as caught:
            self.run_sequence()

        self.assertIs(caught.exception, original)
        self.assertEqual(
            self.executed_indices,
            [0, 2, 4],
        )

        report = original.experiment_report
        self.assertEqual(report["state"], "raised")
        self.assertEqual(
            report["completed_phase_count"],
            4,
        )
        self.assertEqual(
            report["active_phase_index"],
            4,
        )
        self.assertTrue(report["shutdown_verified"])

        self.assertFalse(
            (self.directory(4) / "COMPLETE").exists()
        )
        self.assertFalse(self.directory(6).exists())

        failure = self.metadata(4)["outcome"]
        self.assertEqual(failure["state"], "raised")
        self.assertTrue(failure["shutdown_verified"])

    def test_later_failure_preserves_earlier_successful_recordings(self):
        self.failure_phase_index = 4
        self.phase_error = TimeoutError(
            "synthetic later phase failure"
        )

        with self.assertRaises(TimeoutError):
            self.run_sequence()

        for index in (0, 2):
            with self.subTest(phase_index=index):
                directory = self.directory(index)

                self.assertTrue(
                    (directory / "COMPLETE").is_file()
                )

                validation = validate_run(directory)
                self.assertTrue(
                    validation["valid"],
                    validation["errors"],
                )
                self.assertEqual(
                    validation["state"],
                    "returned",
                )

        self.assertEqual(
            self.metadata(4)["outcome"]["state"],
            "raised",
        )

    def test_rest_interrupt_prevents_discharge(self):
        original = KeyboardInterrupt(
            "synthetic rest interruption"
        )

        with patch(
            (
                "experiments.recorded_preconditioning_backend."
                "rest_outputs_off"
            ),
            side_effect=original,
        ):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.run_sequence()

        self.assertIs(caught.exception, original)
        self.assertEqual(self.executed_indices, [0])
        self.assertEqual(
            original.experiment_report[
                "completed_phase_count"
            ],
            1,
        )
        self.assertEqual(
            original.experiment_report[
                "active_phase_index"
            ],
            1,
        )
        self.assertTrue(
            original.experiment_report[
                "shutdown_verified"
            ]
        )
        self.assertTrue(
            (self.directory(0) / "COMPLETE").is_file()
        )
        self.assertFalse(self.directory(2).exists())

    def test_cleanup_failure_does_not_replace_phase_error(self):
        original = TimeoutError(
            "synthetic primary phase failure"
        )
        self.failure_phase_index = 4
        self.phase_error = original
        self.fail_cleanup_after_phase_error = True

        with self.assertRaises(TimeoutError) as caught:
            self.run_sequence()

        self.assertIs(caught.exception, original)
        self.assertEqual(
            self.executed_indices,
            [0, 2, 4],
        )

        experiment = original.experiment_report
        self.assertFalse(
            experiment["shutdown_verified"]
        )
        self.assertEqual(
            experiment["cleanup_error"]["type"],
            "OSError",
        )

        failure = self.metadata(4)["outcome"]
        self.assertFalse(
            failure["shutdown_verified"]
        )
        self.assertEqual(
            failure["shutdown_evidence"],
            "smu_shutdown_raised",
        )
        self.assertFalse(
            (self.directory(4) / "COMPLETE").exists()
        )

    def test_failure_export_error_does_not_replace_phase_error(self):
        original = OSError(
            "synthetic activation failure"
        )
        self.failure_phase_index = 0
        self.phase_error = original

        with patch.object(
            ExperimentPhaseRun,
            "fail",
            side_effect=OSError(
                "synthetic failure metadata unavailable"
            ),
        ):
            with self.assertRaises(OSError) as caught:
                self.run_sequence()

        self.assertIs(caught.exception, original)
        self.assertEqual(self.executed_indices, [0])
        self.assertTrue(self.fixture.last_writer.closed)
        self.assertFalse(
            (self.directory(0) / "COMPLETE").exists()
        )
        self.assertTrue(
            any(
                "failure metadata unavailable" in note
                for note in original.__notes__
            )
        )
        self.assertTrue(
            original.experiment_report[
                "shutdown_verified"
            ]
        )

    def test_completed_experiment_and_backend_reports_are_json_compatible(
        self,
    ):
        report = self.run_sequence()

        for document in (
            report,
            self.backend.phase_outcomes,
        ):
            with self.subTest(
                document_type=type(document).__name__
            ):
                self.assertEqual(
                    json.loads(
                        json.dumps(
                            document,
                            allow_nan=False,
                        )
                    ),
                    document,
                )


if __name__ == "__main__":
    unittest.main()