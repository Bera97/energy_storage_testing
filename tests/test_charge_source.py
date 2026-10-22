"""Offline tests for outputs-OFF charging source configuration."""

import json
import unittest

import test_preconditioning_plan as plan_tests

from experiments.charge_source import (
    ChargeConfigurationError,
    configure_charge_source,
)


class FakeChannel:
    def __init__(self, smu, channel):
        self.smu = smu
        self.channel = channel
        self.output_on = False
        self.automatic = False
        self.off_mode = "NORM"
        self.source_mode = "CURR"
        self.voltage = 0.0
        self.current_limit = 0.0
        self.four_wire = False
        self.readback_overrides = {}

    def event(self, name, *arguments):
        self.smu.events.append(
            (self.channel, name, *arguments)
        )

    def readback(self, name, default):
        self.event(name)
        return self.readback_overrides.get(name, default)

    def get_output_state(self):
        return self.readback(
            "get_output_state", self.output_on
        )

    def set_auto_output_on(self, enabled):
        self.event("set_auto_output_on", enabled)
        self.automatic = enabled

    def get_auto_output_on(self):
        return self.readback(
            "get_auto_output_on", self.automatic
        )

    def set_output_off_mode(self, mode):
        self.event("set_output_off_mode", mode)
        self.off_mode = mode

    def get_output_off_mode(self):
        return self.readback(
            "get_output_off_mode", self.off_mode
        )

    def configure_fixed_dc(self, mode):
        self.event("configure_fixed_dc", mode)
        self.source_mode = mode

    def get_source_mode(self):
        return self.readback(
            "get_source_mode", self.source_mode
        )

    def set_voltage(self, value):
        self.event("set_voltage", value)
        self.voltage = value

    def get_voltage_setpoint(self):
        return self.readback(
            "get_voltage_setpoint", self.voltage
        )

    def set_current_limit(self, value):
        self.event("set_current_limit", value)
        self.current_limit = value

    def get_current_limit(self):
        return self.readback(
            "get_current_limit", self.current_limit
        )

    def set_four_wire(self, enabled):
        self.event("set_four_wire", enabled)
        self.four_wire = enabled

    def get_four_wire(self):
        return self.readback(
            "get_four_wire", self.four_wire
        )


class FakeSMU:
    def __init__(self):
        self.events = []
        self.chan1 = FakeChannel(self, 1)
        self.chan2 = FakeChannel(self, 2)
        self.responses = {
            ":SOUR1:FUNC:SHAP?": "DC",
            ":SOUR1:VOLT:MODE?": "FIX",
            ":SOUR2:FUNC:SHAP?": "DC",
            ":SOUR2:VOLT:MODE?": "FIX",
        }
        self.error = None

    def check_errors(self):
        self.events.append(("check_errors",))

        if self.error is not None:
            raise self.error

    def abort(self):
        self.events.append(("abort",))

    def _query(self, command):
        self.events.append(("query", command))
        return self.responses[command]


class ChargeSourceTests(unittest.TestCase):
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

    def test_configures_and_verifies_charge_source(self):
        result = configure_charge_source(
            self.smu,
            self.document,
        )

        self.assertEqual(result["channel"], 1)
        self.assertEqual(result["source_function"], "VOLT")
        self.assertAlmostEqual(
            result["voltage_setpoint_v"], 4.1
        )
        self.assertAlmostEqual(
            result["current_compliance_a"], 1.0
        )
        self.assertTrue(result["both_outputs_reported_off"])
        self.assertFalse(result["operating_envelope_verified"])
        self.assertFalse(result["hardware_execution_authorized"])

        self.assertFalse(self.smu.chan1.output_on)
        self.assertFalse(self.smu.chan2.output_on)

    def test_channel_two_is_supported(self):
        self.document["instrument"]["channel"] = 2

        result = configure_charge_source(
            self.smu,
            self.document,
        )

        self.assertEqual(result["channel"], 2)
        self.assertEqual(self.smu.chan2.source_mode, "VOLT")
        self.assertEqual(self.smu.chan1.source_mode, "CURR")

    def test_invalid_config_prevents_instrument_access(self):
        self.document["charge"]["target_voltage_v"] = None

        with self.assertRaises(ValueError):
            configure_charge_source(
                self.smu,
                self.document,
            )

        self.assertEqual(self.smu.events, [])

    def test_missing_monitoring_prevents_instrument_access(self):
        del self.document["charge"]["cv_voltage_tolerance_v"]

        with self.assertRaises(ValueError):
            configure_charge_source(
                self.smu,
                self.document,
            )

        self.assertEqual(self.smu.events, [])

    def test_either_output_on_prevents_configuration(self):
        for selected in ("chan1", "chan2"):
            with self.subTest(selected=selected):
                smu = FakeSMU()
                getattr(smu, selected).output_on = True

                with self.assertRaises(ChargeConfigurationError):
                    configure_charge_source(
                        smu,
                        self.document,
                    )

                self.assertNotIn(("abort",), smu.events)
                self.assertFalse(
                    any(
                        len(event) > 1
                        and str(event[1]).startswith("set_")
                        for event in smu.events
                    )
                )

    def test_voltage_readback_mismatch_is_rejected(self):
        self.smu.chan1.readback_overrides[
            "get_voltage_setpoint"
        ] = 4.0

        with self.assertRaisesRegex(
            ChargeConfigurationError, "Voltage setpoint"
        ):
            configure_charge_source(
                self.smu,
                self.document,
            )

    def test_current_readback_mismatch_is_rejected(self):
        self.smu.chan1.readback_overrides[
            "get_current_limit"
        ] = 0.5

        with self.assertRaisesRegex(
            ChargeConfigurationError, "Current compliance"
        ):
            configure_charge_source(
                self.smu,
                self.document,
            )

    def test_nonfinite_readbacks_are_rejected(self):
        for name in (
            "get_voltage_setpoint",
            "get_current_limit",
        ):
            with self.subTest(name=name):
                smu = FakeSMU()
                smu.chan1.readback_overrides[name] = float("nan")

                with self.assertRaises(ChargeConfigurationError):
                    configure_charge_source(
                        smu,
                        self.document,
                    )

    def test_wrong_source_mode_is_rejected(self):
        self.smu.chan1.readback_overrides[
            "get_source_mode"
        ] = "CURR"

        with self.assertRaises(ChargeConfigurationError):
            configure_charge_source(
                self.smu,
                self.document,
            )

    def test_wrong_source_shape_is_rejected(self):
        self.smu.responses[":SOUR1:FUNC:SHAP?"] = "PULS"

        with self.assertRaises(ChargeConfigurationError):
            configure_charge_source(
                self.smu,
                self.document,
            )

    def test_wrong_fixed_operation_is_rejected(self):
        self.smu.responses[":SOUR1:VOLT:MODE?"] = "SWE"

        with self.assertRaises(ChargeConfigurationError):
            configure_charge_source(
                self.smu,
                self.document,
            )

    def test_sense_readback_mismatch_is_rejected(self):
        self.smu.chan1.readback_overrides[
            "get_four_wire"
        ] = True

        with self.assertRaisesRegex(
            ChargeConfigurationError, "Sense"
        ):
            configure_charge_source(
                self.smu,
                self.document,
            )

    def test_wrong_off_mode_is_rejected(self):
        self.smu.chan1.readback_overrides[
            "get_output_off_mode"
        ] = "ZERO"

        with self.assertRaises(ChargeConfigurationError):
            configure_charge_source(
                self.smu,
                self.document,
            )

    def test_auto_output_on_readback_is_rejected(self):
        self.smu.chan2.readback_overrides[
            "get_auto_output_on"
        ] = True

        with self.assertRaises(ChargeConfigurationError):
            configure_charge_source(
                self.smu,
                self.document,
            )

    def test_instrument_error_preserves_identity(self):
        original = RuntimeError("simulated instrument error")
        self.smu.error = original

        with self.assertRaises(RuntimeError) as caught:
            configure_charge_source(
                self.smu,
                self.document,
            )

        self.assertIs(caught.exception, original)

    def test_result_is_strict_json_compatible(self):
        result = configure_charge_source(
            self.smu,
            self.document,
        )

        self.assertEqual(
            json.loads(json.dumps(result, allow_nan=False)),
            result,
        )


if __name__ == "__main__":
    unittest.main()