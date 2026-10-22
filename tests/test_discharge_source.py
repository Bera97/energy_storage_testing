"""Offline tests for outputs-OFF discharge configuration."""

import json
import unittest

import test_preconditioning_plan as plan_tests
from test_charge_source import (
    FakeChannel as ChargeFakeChannel,
    FakeSMU as ChargeFakeSMU,
)

from experiments.discharge_source import (
    DischargeConfigurationError,
    configure_discharge_source,
)


class FakeChannel(ChargeFakeChannel):
    def __init__(self, smu, channel):
        super().__init__(smu, channel)
        self.current = 0.0
        self.voltage_limit = 0.0

    def set_current(self, value):
        self.event("set_current", value)
        self.current = value

    def get_current_setpoint(self):
        return self.readback(
            "get_current_setpoint",
            self.current,
        )

    def set_voltage_limit(self, value):
        self.event("set_voltage_limit", value)
        self.voltage_limit = value

    def get_voltage_limit(self):
        return self.readback(
            "get_voltage_limit",
            self.voltage_limit,
        )


class FakeSMU(ChargeFakeSMU):
    def __init__(self):
        super().__init__()
        self.chan1 = FakeChannel(self, 1)
        self.chan2 = FakeChannel(self, 2)
        self.responses.update({
            ":SOUR1:CURR:MODE?": "FIX",
            ":SOUR2:CURR:MODE?": "FIX",
        })


class DischargeSourceTests(unittest.TestCase):
    def setUp(self):
        fixture = plan_tests.PreconditioningPlanTests(
            methodName="test_two_cycles_have_eight_ordered_phases"
        )
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.document = fixture.document
        self.smu = FakeSMU()

    def test_configures_negative_current_and_upper_voltage_limit(self):
        result = configure_discharge_source(
            self.smu,
            self.document,
        )

        self.assertEqual(result["source_function"], "CURR")
        self.assertAlmostEqual(
            result["current_setpoint_a"], -2.0
        )
        self.assertAlmostEqual(
            result["voltage_compliance_v"], 4.2
        )
        self.assertAlmostEqual(
            result["software_discharge_cutoff_v"], 2.7
        )
        self.assertTrue(result["both_outputs_reported_off"])
        self.assertFalse(result["operating_envelope_verified"])
        self.assertFalse(result["hardware_execution_authorized"])
        self.assertFalse(self.smu.chan1.output_on)
        self.assertFalse(self.smu.chan2.output_on)

    def test_compliance_is_not_set_to_discharge_cutoff(self):
        configure_discharge_source(
            self.smu,
            self.document,
        )

        self.assertEqual(self.smu.chan1.voltage_limit, 4.2)
        self.assertNotEqual(
            self.smu.chan1.voltage_limit,
            self.document["discharge"]["cutoff_voltage_v"],
        )

    def test_channel_two_is_supported(self):
        self.document["instrument"]["channel"] = 2

        result = configure_discharge_source(
            self.smu,
            self.document,
        )

        self.assertEqual(result["channel"], 2)
        self.assertAlmostEqual(self.smu.chan2.current, -2.0)
        self.assertEqual(self.smu.chan1.current, 0.0)

    def test_invalid_config_prevents_instrument_access(self):
        self.document["discharge"]["cutoff_voltage_v"] = 5.0

        with self.assertRaises(ValueError):
            configure_discharge_source(
                self.smu,
                self.document,
            )

        self.assertEqual(self.smu.events, [])

    def test_either_output_on_prevents_configuration(self):
        for selected in ("chan1", "chan2"):
            with self.subTest(selected=selected):
                smu = FakeSMU()
                getattr(smu, selected).output_on = True

                with self.assertRaises(
                    DischargeConfigurationError
                ):
                    configure_discharge_source(
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

    def test_current_readback_mismatch_is_rejected(self):
        self.smu.chan1.readback_overrides[
            "get_current_setpoint"
        ] = -1.0

        with self.assertRaisesRegex(
            DischargeConfigurationError,
            "Current setpoint",
        ):
            configure_discharge_source(
                self.smu,
                self.document,
            )

    def test_positive_current_readback_is_rejected(self):
        self.smu.chan1.readback_overrides[
            "get_current_setpoint"
        ] = 2.0

        with self.assertRaises(
            DischargeConfigurationError
        ):
            configure_discharge_source(
                self.smu,
                self.document,
            )

    def test_voltage_limit_mismatch_is_rejected(self):
        self.smu.chan1.readback_overrides[
            "get_voltage_limit"
        ] = 3.0

        with self.assertRaisesRegex(
            DischargeConfigurationError,
            "Voltage compliance",
        ):
            configure_discharge_source(
                self.smu,
                self.document,
            )

    def test_nonfinite_readbacks_are_rejected(self):
        for name in (
            "get_current_setpoint",
            "get_voltage_limit",
        ):
            with self.subTest(name=name):
                smu = FakeSMU()
                smu.chan1.readback_overrides[name] = float("nan")

                with self.assertRaises(
                    DischargeConfigurationError
                ):
                    configure_discharge_source(
                        smu,
                        self.document,
                    )

    def test_wrong_source_mode_is_rejected(self):
        self.smu.chan1.readback_overrides[
            "get_source_mode"
        ] = "VOLT"

        with self.assertRaises(
            DischargeConfigurationError
        ):
            configure_discharge_source(
                self.smu,
                self.document,
            )

    def test_wrong_shape_or_operation_is_rejected(self):
        for command, response in (
            (":SOUR1:FUNC:SHAP?", "PULS"),
            (":SOUR1:CURR:MODE?", "SWE"),
        ):
            with self.subTest(command=command):
                smu = FakeSMU()
                smu.responses[command] = response

                with self.assertRaises(
                    DischargeConfigurationError
                ):
                    configure_discharge_source(
                        smu,
                        self.document,
                    )

    def test_wrong_sense_or_off_mode_is_rejected(self):
        for name, value in (
            ("get_four_wire", True),
            ("get_output_off_mode", "ZERO"),
        ):
            with self.subTest(name=name):
                smu = FakeSMU()
                smu.chan1.readback_overrides[name] = value

                with self.assertRaises(
                    DischargeConfigurationError
                ):
                    configure_discharge_source(
                        smu,
                        self.document,
                    )

    def test_automatic_output_on_is_rejected(self):
        self.smu.chan2.readback_overrides[
            "get_auto_output_on"
        ] = True

        with self.assertRaises(
            DischargeConfigurationError
        ):
            configure_discharge_source(
                self.smu,
                self.document,
            )

    def test_instrument_error_preserves_identity(self):
        original = RuntimeError("simulated instrument error")
        self.smu.error = original

        with self.assertRaises(RuntimeError) as caught:
            configure_discharge_source(
                self.smu,
                self.document,
            )

        self.assertIs(caught.exception, original)

    def test_result_is_strict_json_compatible(self):
        result = configure_discharge_source(
            self.smu,
            self.document,
        )

        self.assertEqual(
            json.loads(json.dumps(result, allow_nan=False)),
            result,
        )


if __name__ == "__main__":
    unittest.main()