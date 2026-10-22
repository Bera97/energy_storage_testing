"""Offline tests for concrete recorded phase execution.

No instrument connection or real output enabling.
"""

from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_experiment_phase_run as recording_tests
import test_preconditioning_plan as plan_tests
from test_charge_trace_transport import FakeAcquisition

from acquisition.buffered_acquisition import BufferedAcquisition
from acquisition.charge_trace_transport import ChargeTraceTransport
from acquisition.discharge_trace_transport import (
    DischargeTraceTransport,
)
from acquisition.spot_acquisition import SMUReading
from experiments.acquisition_budget import build_acquisition_plan
from experiments.phase_activation import PhaseActivation
from experiments.recorded_phase_executor import RecordedPhaseExecutor


class FakeChannel:
    def __init__(self):
        self.on = False

    def get_output_state(self):
        return self.on


class FakeSMU:
    def __init__(self, events):
        self.events = events
        self.chan1 = FakeChannel()
        self.chan2 = FakeChannel()
        self.shutdown_calls = 0

    def check_errors(self):
        return None

    def shutdown(self):
        self.shutdown_calls += 1
        self.events.append("shutdown")
        self.chan1.on = False
        self.chan2.on = False


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class RecordedPhaseExecutorTests(unittest.TestCase):
    def setUp(self):
        plan_fixture = plan_tests.PreconditioningPlanTests(
            methodName="test_two_cycles_have_eight_ordered_phases"
        )
        plan_fixture.setUp()
        self.addCleanup(plan_fixture.doCleanups)
        self.document = deepcopy(plan_fixture.document)

        self.document["charge"]["cv_voltage_tolerance_v"] = 0.02
        self.document["acquisition"]["maximum_sample_gap_s"] = 1.1

        self.recording_fixture = (
            recording_tests.ExperimentPhaseRunTests(
                methodName=(
                    "test_success_creates_qualified_result_and_complete"
                )
            )
        )
        self.recording_fixture.setUp()
        self.addCleanup(self.recording_fixture.doCleanups)

        self.events = []
        self.smu = FakeSMU(self.events)
        self.clock = FakeClock()
        self.preparation_error = None
        self.activation_error = None
        self.activation_delay_s = 0.0
        self.last_writer = None

        self.acquisition_plan = build_acquisition_plan(
            self.document,
            maximum_records=BufferedAcquisition.MAX_RECORDS,
        )

        for name in ("prepare_charge", "prepare_discharge"):
            patcher = patch(
                f"experiments.recorded_phase_executor.{name}",
                side_effect=(
                    self.fake_prepare_charge
                    if name == "prepare_charge"
                    else self.fake_prepare_discharge
                ),
            )
            patcher.start()
            self.addCleanup(patcher.stop)

        activation_patcher = patch(
            "experiments.recorded_phase_executor.activate_phase_output",
            side_effect=self.fake_activate,
        )
        self.activation_mock = activation_patcher.start()
        self.addCleanup(activation_patcher.stop)

        self.executor = RecordedPhaseExecutor(
            self.smu,
            self.document,
            self.recording_fixture.root,
            "executor_test",
            maximum_voltage_v=4.2,
            maximum_current_a=10.0,
            clock=self.clock,
            sleep=self.clock.sleep,
        )

    def phase(self, phase_type):
        return deepcopy(
            next(
                phase
                for phase in self.executor.plan["phases"]
                if phase["type"] == phase_type
            )
        )

    def prepared_phase(self, cycle, phase_type):
        return deepcopy(
            next(
                phase
                for phase in self.acquisition_plan["phases"]
                if phase["cycle"] == cycle
                and phase["type"] == phase_type
            )
        )

    def prepare_common(self, writer, cycle, phase_type):
        self.events.append("prepare")
        self.last_writer = writer

        self.assertFalse(writer.closed)
        self.assertEqual(writer.records_flushed, 0)
        self.assertTrue(writer.path.is_file())

        if self.preparation_error is not None:
            raise self.preparation_error

        phase = self.prepared_phase(cycle, phase_type)
        channel = self.document["instrument"]["channel"]

        if phase_type == "charge_cc_cv":
            hold = phase["termination_hold_time_s"]
            interval = 1.0
            count = int(hold / interval) + 2

            records = [
                SMUReading(
                    channel=channel,
                    voltage_v=phase["target_voltage_v"],
                    current_a=(
                        phase["termination_current_a"] / 2
                    ),
                    timestamp_s=100.0 + index * interval,
                    status=0,
                )
                for index in range(count)
            ]
        else:
            cutoff = phase["cutoff_voltage_v"]
            records = [
                SMUReading(
                    channel=channel,
                    voltage_v=cutoff + 0.5,
                    current_a=-phase["current_magnitude_a"],
                    timestamp_s=100.0,
                    status=1,
                ),
                SMUReading(
                    channel=channel,
                    voltage_v=cutoff,
                    current_a=-phase["current_magnitude_a"],
                    timestamp_s=101.0,
                    status=1,
                ),
            ]

        acquisition = FakeAcquisition([records, []])
        acquisition._count = phase[
            "acquisition_budget"
        ]["record_count"]
        acquisition.channel = channel
        acquisition._smu = self.smu

        if phase_type == "charge_cc_cv":
            transport = ChargeTraceTransport(
                acquisition,
                writer,
            )
        else:
            transport = DischargeTraceTransport(
                acquisition,
                writer,
            )

        return SimpleNamespace(
            phase=phase,
            evidence={
                "state": "prepared",
                "both_outputs_reported_off": True,
                "shutdown_verified": None,
                "cleanup_error": None,
            },
            acquisition=acquisition,
            transport=transport,
            cv_classifier=lambda reading: True,
        )

    def fake_prepare_charge(
        self,
        smu,
        document,
        writer,
        *,
        cycle,
    ):
        return self.prepare_common(
            writer,
            cycle,
            "charge_cc_cv",
        )

    def fake_prepare_discharge(
        self,
        smu,
        document,
        writer,
        *,
        cycle,
    ):
        return self.prepare_common(
            writer,
            cycle,
            "discharge_cc",
        )

    def fake_activate(
        self,
        smu,
        channel,
        *,
        maximum_voltage_v,
        maximum_current_a,
        clock,
    ):
        self.events.append("activate")
        self.assertIsNotNone(self.last_writer)
        self.assertFalse(self.last_writer.closed)

        if self.activation_error is not None:
            raise self.activation_error

        before = clock()
        self.clock.now += self.activation_delay_s

        selected = smu.chan1 if channel == 1 else smu.chan2
        selected.on = True

        return PhaseActivation(
            channel=channel,
            host_before_enable_s=before,
            host_after_verification_s=clock(),
        )

    def failure_metadata(self, phase):
        directory = (
            self.recording_fixture.root
            / f"executor_test_phase_{phase['phase_index']}"
        )

        document = json.loads(
            (directory / "metadata.json").read_text(
                encoding="utf-8"
            )
        )

        return directory, document

    def test_charge_returns_finalized_recording(self):
        phase = self.phase("charge_cc_cv")

        run = self.executor(phase)

        self.assertTrue(run.finalized)
        self.assertTrue(run.writer.closed)
        self.assertEqual(
            run.result_for_phase(phase).stop_reason,
            "cv_current_termination",
        )
        self.assertEqual(
            self.events[:2],
            ["prepare", "activate"],
        )
        self.assertFalse(self.smu.chan1.on)
        self.assertFalse(self.smu.chan2.on)

    def test_discharge_returns_finalized_recording(self):
        phase = self.phase("discharge_cc")

        run = self.executor(phase)

        self.assertEqual(
            run.result_for_phase(phase).stop_reason,
            "voltage_min",
        )
        self.assertTrue(
            (run.directory / "COMPLETE").is_file()
        )

    def test_preparation_error_prevents_activation(self):
        phase = self.phase("charge_cc_cv")
        original = RuntimeError(
            "synthetic preparation failure"
        )
        self.preparation_error = original

        with self.assertRaises(RuntimeError) as caught:
            self.executor(phase)

        self.assertIs(caught.exception, original)
        self.activation_mock.assert_not_called()
        self.assertTrue(self.last_writer.closed)

        directory, document = self.failure_metadata(phase)

        self.assertEqual(
            document["outcome"]["state"],
            "raised",
        )
        self.assertTrue(
            document["outcome"]["shutdown_verified"]
        )
        self.assertFalse(
            (directory / "COMPLETE").exists()
        )

    def test_activation_error_is_preserved(self):
        phase = self.phase("charge_cc_cv")
        original = OSError(
            "synthetic activation failure"
        )
        self.activation_error = original

        with self.assertRaises(OSError) as caught:
            self.executor(phase)

        self.assertIs(caught.exception, original)
        self.assertTrue(
            original.shutdown_report["verified"]
        )
        self.assertTrue(self.last_writer.closed)

    def test_activation_consuming_deadline_prevents_success(self):
        phase = self.phase("charge_cc_cv")
        self.activation_delay_s = phase["maximum_duration_s"]

        with self.assertRaises(TimeoutError):
            self.executor(phase)

        directory, document = self.failure_metadata(phase)

        self.assertEqual(
            document["outcome"]["state"],
            "raised",
        )
        self.assertEqual(
            document["outcome"]["records_flushed"],
            0,
        )
        self.assertFalse(
            (directory / "COMPLETE").exists()
        )
        self.assertFalse(self.smu.chan1.on)
        self.assertFalse(self.smu.chan2.on)

    def test_wrong_planned_phase_is_rejected_before_preparation(self):
        phase = self.phase("charge_cc_cv")
        phase["current_magnitude_a"] *= 2

        with self.assertRaises(ValueError):
            self.executor(phase)

        self.assertEqual(self.events, [])
        self.activation_mock.assert_not_called()

    def test_existing_run_directory_prevents_activation(self):
        phase = self.phase("discharge_cc")
        self.executor(phase)
        self.activation_mock.reset_mock()

        with self.assertRaises(FileExistsError):
            self.executor(phase)

        self.activation_mock.assert_not_called()

    def test_success_metadata_preserves_execution_evidence(self):
        phase = self.phase("discharge_cc")

        run = self.executor(phase)

        document = json.loads(
            (run.directory / "metadata.json").read_text(
                encoding="utf-8"
            )
        )
        report = document["phase_transport_report"]

        self.assertEqual(
            report["measurement_timing"][
                "measurement_time_basis"
            ],
            "first_recorded_sample",
        )
        self.assertIn(
            "preparation_evidence",
            report,
        )
        self.assertIn(
            "activation_evidence",
            report,
        )
        self.assertFalse(
            report["activation_evidence"][
                "output_enable_time_established"
            ]
        )


if __name__ == "__main__":
    unittest.main()