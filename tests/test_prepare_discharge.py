"""Offline tests for outputs-OFF discharge preparation."""

import json
import unittest
from unittest.mock import patch

import test_preconditioning_plan as plan_tests
from test_discharge_source import FakeSMU as SourceFakeSMU
from test_prepare_charge import FakeAcquisition, FakeWriter

from experiments.prepare_discharge import prepare_discharge


class FakeSMU(SourceFakeSMU):
    def __init__(self):
        super().__init__()
        self.shutdown_calls = 0
        self.shutdown_error = None

    def shutdown(self):
        self.shutdown_calls += 1

        if self.shutdown_error is not None:
            raise self.shutdown_error

        self.chan1.output_on = False
        self.chan2.output_on = False


class PrepareDischargeTests(unittest.TestCase):
    def setUp(self):
        fixture = plan_tests.PreconditioningPlanTests(
            methodName="test_two_cycles_have_eight_ordered_phases"
        )
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.document = fixture.document

        self.document["charge"][
            "cv_voltage_tolerance_v"
        ] = 0.02
        self.document["acquisition"][
            "maximum_sample_gap_s"
        ] = 0.25

        self.smu = FakeSMU()
        self.writer = FakeWriter()

        patcher = patch(
            "experiments.prepare_discharge.configure_trace_timebase",
            return_value={
                "trace_timestamp_format": "ABS",
                "timer_auto_reset": True,
                "output_enable_time_established": False,
            },
        )
        self.timebase = patcher.start()
        self.addCleanup(patcher.stop)

    def prepare(self, **changes):
        return prepare_discharge(
            self.smu,
            self.document,
            self.writer,
            **{
                "cycle": 1,
                "acquisition_factory": FakeAcquisition,
                **changes,
            },
        )

    def test_assembles_discharge_without_enabling_output(self):
        prepared = self.prepare()

        self.assertEqual(prepared.evidence["state"], "prepared")
        self.assertEqual(prepared.phase["type"], "discharge_cc")
        self.assertEqual(
            prepared.acquisition.configuration["count"],
            72001,
        )
        self.assertAlmostEqual(
            prepared.evidence["source"]["current_setpoint_a"],
            -2.0,
        )
        self.assertEqual(
            prepared.phase["monitoring"],
            {
                "maximum_sample_gap_s": 0.25,
                "maximum_voltage_v": 4.2,
            },
        )
        self.assertFalse(self.smu.chan1.output_on)
        self.assertFalse(self.smu.chan2.output_on)
        self.assertEqual(self.smu.shutdown_calls, 0)
        self.assertFalse(
            prepared.evidence["hardware_execution_authorized"]
        )

    def test_second_cycle_is_selected(self):
        prepared = self.prepare(cycle=2)

        self.assertEqual(prepared.phase["cycle"], 2)
        self.assertEqual(prepared.phase["phase_index"], 6)

    def test_invalid_cycle_prevents_instrument_access(self):
        with self.assertRaises(ValueError):
            self.prepare(cycle=3)

        self.assertEqual(self.smu.events, [])

    def test_missing_monitoring_prevents_instrument_access(self):
        del self.document["acquisition"]["maximum_sample_gap_s"]

        with self.assertRaises(ValueError):
            self.prepare()

        self.assertEqual(self.smu.events, [])

    def test_closed_writer_prevents_instrument_access(self):
        self.writer.closed = True

        with self.assertRaises(ValueError):
            self.prepare()

        self.assertEqual(self.smu.events, [])

    def test_acquisition_failure_attempts_shutdown(self):
        original = RuntimeError("configuration failed")

        class FailedAcquisition(FakeAcquisition):
            def configure(self, **arguments):
                raise original

        with self.assertRaises(RuntimeError) as caught:
            self.prepare(
                acquisition_factory=FailedAcquisition
            )

        self.assertIs(caught.exception, original)
        self.assertEqual(self.smu.shutdown_calls, 1)
        self.assertTrue(
            original.discharge_preparation_report[
                "shutdown_verified"
            ]
        )

    def test_cleanup_failure_preserves_original(self):
        original = RuntimeError("timebase failed")
        self.timebase.side_effect = original
        self.smu.shutdown_error = OSError("shutdown failed")

        with self.assertRaises(RuntimeError) as caught:
            self.prepare()

        self.assertIs(caught.exception, original)
        report = original.discharge_preparation_report
        self.assertFalse(report["shutdown_verified"])
        self.assertEqual(
            report["cleanup_error"]["type"], "OSError"
        )

    def test_evidence_is_json_compatible(self):
        evidence = self.prepare().evidence

        self.assertEqual(
            json.loads(json.dumps(evidence, allow_nan=False)),
            evidence,
        )


if __name__ == "__main__":
    unittest.main()