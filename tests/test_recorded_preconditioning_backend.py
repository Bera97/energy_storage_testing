"""Offline integration tests for the recorded preconditioning backend."""

from copy import deepcopy
import json
import unittest

from test_experiment_phase_run import ExperimentPhaseRunTests
from test_rest_outputs_off import FakeClock, FakeSMU
import test_preconditioning_plan as plan_tests

from experiments.phase_outcome import PhaseOutcomeError
from experiments.preconditioning_runner import (
    run_preconditioning,
)
from experiments.recorded_preconditioning_backend import (
    RecordedPreconditioningBackend,
)
from recording.experiment_phase_run import ExperimentPhaseRun


class RecordedPreconditioningBackendTests(unittest.TestCase):
    def setUp(self):
        plan_fixture = plan_tests.PreconditioningPlanTests(
            methodName="test_two_cycles_have_eight_ordered_phases"
        )
        plan_fixture.setUp()
        self.addCleanup(plan_fixture.doCleanups)
        self.document = plan_fixture.document

        self.recording_fixture = ExperimentPhaseRunTests(
            methodName=(
                "test_success_creates_qualified_result_and_complete"
            )
        )
        self.recording_fixture.setUp()
        self.addCleanup(self.recording_fixture.doCleanups)

        self.smu = FakeSMU()
        self.clock = FakeClock()
        self.executed = []
        self.runs = []
        self.executor_error = None
        self.return_unfinalized = False
        self.mutate_recorded_phase = False

        self.backend = RecordedPreconditioningBackend(
            self.smu,
            execute_active_phase=self.execute,
            rest_poll_interval_s=1000.0,
            clock=self.clock,
            sleep=self.clock.sleep,
        )

    def execute(self, phase):
        self.executed.append(deepcopy(phase))

        if self.executor_error is not None:
            raise self.executor_error

        run = ExperimentPhaseRun(
            self.recording_fixture.root,
            f"phase_{phase['phase_index']}",
            metadata={"channel": 1},
        )
        self.addCleanup(run.close)
        self.runs.append(run)

        if self.return_unfinalized:
            return run

        prepared_phase = deepcopy(phase)
        prepared_phase["acquisition_budget"] = {
            "record_count": 100,
        }

        if self.mutate_recorded_phase:
            prepared_phase["cycle"] = 999

        report = deepcopy(self.recording_fixture.report)

        if phase["type"] == "discharge_cc":
            report.update({
                "phase_type": "discharge",
                "expected_stop_reason": "voltage_min",
                "stop_reason": "voltage_min",
            })

        readings = self.recording_fixture.readings
        run.writer.write_batch(readings)
        run.finish_phase(
            prepared_phase,
            report,
            readings,
        )
        return run

    def test_two_cycles_complete_with_recordings_and_rest(self):
        report = run_preconditioning(
            self.document,
            self.backend,
        )

        self.assertEqual(report["state"], "completed")
        self.assertEqual(report["completed_phase_count"], 8)
        self.assertEqual(len(self.runs), 4)
        self.assertEqual(len(self.backend.phase_outcomes), 8)

        self.assertEqual(
            [phase["type"] for phase in self.executed],
            ["charge_cc_cv", "discharge_cc"] * 2,
        )

        self.assertTrue(
            all(
                run.writer.closed
                and (run.directory / "COMPLETE").is_file()
                for run in self.runs
            )
        )

    def test_backend_evidence_is_json_compatible(self):
        run_preconditioning(self.document, self.backend)

        evidence = self.backend.phase_outcomes
        self.assertEqual(
            json.loads(json.dumps(evidence, allow_nan=False)),
            evidence,
        )

    def test_unfinalized_recording_stops_sequence(self):
        self.return_unfinalized = True

        with self.assertRaises(PhaseOutcomeError) as caught:
            run_preconditioning(
                self.document,
                self.backend,
            )

        self.assertEqual(len(self.executed), 1)
        self.assertEqual(
            caught.exception.experiment_report[
                "completed_phase_count"
            ],
            0,
        )
        self.assertEqual(self.backend.phase_outcomes, [])

    def test_recording_for_wrong_phase_stops_sequence(self):
        self.mutate_recorded_phase = True

        with self.assertRaisesRegex(
            PhaseOutcomeError,
            "cycle",
        ):
            run_preconditioning(
                self.document,
                self.backend,
            )

        self.assertEqual(len(self.executed), 1)
        self.assertEqual(self.backend.phase_outcomes, [])

    def test_executor_error_is_not_replaced(self):
        original = TimeoutError("synthetic active timeout")
        self.executor_error = original

        with self.assertRaises(TimeoutError) as caught:
            run_preconditioning(
                self.document,
                self.backend,
            )

        self.assertIs(caught.exception, original)
        self.assertTrue(
            original.experiment_report["shutdown_verified"]
        )
        self.assertEqual(len(self.executed), 1)

    def test_output_on_prevents_executor_call(self):
        self.smu.chan1.states = [True]

        with self.assertRaises(RuntimeError):
            self.backend.charge({
                "phase_index": 0,
                "cycle": 1,
                "type": "charge_cc_cv",
            })

        self.assertEqual(self.executed, [])

    def test_wrong_executor_return_type_is_rejected(self):
        self.backend.execute_active_phase = (
            lambda phase: object()
        )

        with self.assertRaisesRegex(
            TypeError,
            "ExperimentPhaseRun",
        ):
            run_preconditioning(
                self.document,
                self.backend,
            )

    def test_rest_does_not_call_active_executor(self):
        self.backend.rest({
            "phase_index": 1,
            "cycle": 1,
            "type": "rest",
            "duration_s": 0.25,
        })

        self.assertEqual(self.executed, [])
        self.assertAlmostEqual(self.clock.now, 0.25)
        self.assertEqual(
            self.backend.phase_outcomes[0]["stop_reason"],
            "rest_elapsed",
        )

    def test_wrong_phase_method_is_rejected(self):
        with self.assertRaises(ValueError):
            self.backend.discharge({
                "phase_index": 0,
                "cycle": 1,
                "type": "charge_cc_cv",
            })

        self.assertEqual(self.executed, [])
        self.assertEqual(self.smu.shutdown_calls, 0)

    def test_missing_executor_is_rejected(self):
        with self.assertRaises(ValueError):
            RecordedPreconditioningBackend(
                self.smu,
                execute_active_phase=None,
            )


if __name__ == "__main__":
    unittest.main()