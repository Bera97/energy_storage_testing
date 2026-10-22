"""Offline tests for assembled outputs-OFF charge preparation."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import test_preconditioning_plan as plan_tests
from test_charge_source import FakeSMU as SourceFakeSMU

from experiments.prepare_charge import prepare_charge


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


class FakeWriter:
    def __init__(self):
        self.closed = False
        self.failed = False
        self.records_flushed = 0


class FakeAcquisition:
    def __init__(self, smu, channel):
        self._smu = smu
        self.channel = channel
        self._count = None
        self._interval_s = None
        self._trace_ready = False
        self.readings = ()
        self.configuration = None

    def configure(self, *, count, interval_s, nplc):
        self.configuration = {
            "count": count,
            "interval_s": interval_s,
            "nplc": nplc,
        }
        self._count = count
        self._interval_s = interval_s
        self._trace_ready = True


class PrepareChargeTests(unittest.TestCase):
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
            "experiments.prepare_charge.configure_trace_timebase",
            return_value={
                "trace_timestamp_format": "ABS",
                "timer_auto_reset": True,
                "output_enable_time_established": False,
            },
        )
        self.timebase = patcher.start()
        self.addCleanup(patcher.stop)

    def prepare(self, **changes):
        arguments = {
            "cycle": 1,
            "acquisition_factory": FakeAcquisition,
            **changes,
        }
        return prepare_charge(
            self.smu,
            self.document,
            self.writer,
            **arguments,
        )

    def test_assembles_charge_without_enabling_output(self):
        prepared = self.prepare()

        self.assertEqual(
            prepared.evidence["state"], "prepared"
        )
        self.assertEqual(
            prepared.acquisition.configuration,
            {
                "count": 72001,
                "interval_s": 0.2,
                "nplc": 1.0,
            },
        )
        self.assertIs(
            prepared.transport.acquisition,
            prepared.acquisition,
        )
        self.assertIs(
            prepared.transport.writer,
            self.writer,
        )
        self.assertFalse(self.smu.chan1.output_on)
        self.assertFalse(self.smu.chan2.output_on)
        self.assertFalse(
            prepared.evidence["hardware_execution_authorized"]
        )
        self.assertEqual(self.smu.shutdown_calls, 0)

    def test_second_cycle_selects_its_charge_phase(self):
        prepared = self.prepare(cycle=2)

        self.assertEqual(prepared.phase["cycle"], 2)
        self.assertEqual(prepared.phase["phase_index"], 4)

    def test_invalid_cycle_never_accesses_instrument(self):
        for cycle in (True, 0, 3):
            with self.subTest(cycle=cycle):
                with self.assertRaises(ValueError):
                    self.prepare(cycle=cycle)

        self.assertEqual(self.smu.events, [])
        self.timebase.assert_not_called()

    def test_oversized_plan_never_accesses_instrument(self):
        self.document["discharge"][
            "maximum_duration_s"
        ] = 30000

        with self.assertRaises(ValueError):
            self.prepare()

        self.assertEqual(self.smu.events, [])

    def test_missing_monitoring_never_accesses_instrument(self):
        del self.document["charge"]["cv_voltage_tolerance_v"]

        with self.assertRaises(ValueError):
            self.prepare()

        self.assertEqual(self.smu.events, [])

    def test_closed_writer_never_accesses_instrument(self):
        self.writer.closed = True

        with self.assertRaises(ValueError):
            self.prepare()

        self.assertEqual(self.smu.events, [])

    def test_acquisition_failure_attempts_shutdown(self):
        original = RuntimeError("acquisition configuration failed")

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
            original.charge_preparation_report[
                "shutdown_verified"
            ]
        )
        self.timebase.assert_not_called()

    def test_timebase_failure_attempts_shutdown(self):
        original = RuntimeError("timebase readback failed")
        self.timebase.side_effect = original

        with self.assertRaises(RuntimeError) as caught:
            self.prepare()

        self.assertIs(caught.exception, original)
        self.assertEqual(self.smu.shutdown_calls, 1)

    def test_cleanup_failure_preserves_original_error(self):
        original = RuntimeError("timebase failed")
        self.timebase.side_effect = original
        self.smu.shutdown_error = OSError("shutdown failed")

        with self.assertRaises(RuntimeError) as caught:
            self.prepare()

        self.assertIs(caught.exception, original)
        report = original.charge_preparation_report
        self.assertFalse(report["shutdown_verified"])
        self.assertEqual(
            report["cleanup_error"]["type"], "OSError"
        )
        self.assertTrue(original.__notes__)

    def test_output_on_after_configuration_is_rejected(self):
        def changed_timebase(smu, channel):
            smu.chan1.output_on = True
            return {"trace_timestamp_format": "ABS"}

        self.timebase.side_effect = changed_timebase

        with self.assertRaisesRegex(
            RuntimeError, "still report OFF"
        ):
            self.prepare()

        self.assertEqual(self.smu.shutdown_calls, 1)

    def test_input_configuration_is_unchanged(self):
        before = deepcopy(self.document)

        prepared = self.prepare()
        prepared.phase["monitoring"][
            "voltage_tolerance_v"
        ] = 999

        self.assertEqual(self.document, before)

    def test_preparation_evidence_is_json_compatible(self):
        prepared = self.prepare()
        evidence = prepared.evidence

        self.assertEqual(
            json.loads(json.dumps(evidence, allow_nan=False)),
            evidence,
        )


if __name__ == "__main__":
    unittest.main()