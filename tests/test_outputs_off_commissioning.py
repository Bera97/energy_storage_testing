"""Offline tests for outputs-OFF commissioning.

No instrument connection or real output enabling.
"""

from contextlib import redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

from commissioning.outputs_off import (
    commission_outputs_off,
    main,
)


class FakeChannel:
    def __init__(self, mode):
        self.mode = mode
        self.output = False
        self.automatic = False

    def get_output_state(self):
        return self.output

    def get_auto_output_on(self):
        return self.automatic

    def get_output_off_mode(self):
        return "HIZ"

    def get_source_mode(self):
        return self.mode

    def get_voltage_setpoint(self):
        return 1.0

    def get_current_limit(self):
        return 0.01

    def get_current_setpoint(self):
        return -0.01

    def get_voltage_limit(self):
        return 2.0


class FakeSMU:
    def __init__(self):
        self.identity = "Keysight Technologies,B2902B,FAKE,FAKE"
        self.chan1 = FakeChannel("VOLT")
        self.chan2 = FakeChannel("CURR")
        self.shutdown_calls = 0
        self.connect_calls = 0
        self.close_calls = 0
        self.query_error = None
        self.shutdown_errors = {}

    def connect(self):
        self.connect_calls += 1
        return self

    def close(self):
        self.close_calls += 1

    def shutdown(self):
        self.shutdown_calls += 1

        error = self.shutdown_errors.get(
            self.shutdown_calls
        )
        if error is not None:
            raise error

    def check_errors(self):
        if self.query_error is not None:
            raise self.query_error


class OutputsOffCommissioningTests(unittest.TestCase):
    def setUp(self):
        self.smu = FakeSMU()

    def test_success_checks_both_channels_and_shuts_down_twice(self):
        report = commission_outputs_off(self.smu)

        self.assertEqual(
            report["state"],
            "outputs_off_verified",
        )
        self.assertEqual(len(report["channels"]), 2)
        self.assertTrue(
            report["initial_shutdown_verified"]
        )
        self.assertTrue(
            report["final_shutdown_verified"]
        )
        self.assertEqual(self.smu.shutdown_calls, 2)
        self.assertFalse(
            report["output_enable_attempted"]
        )
        self.assertFalse(
            report["acquisition_initiation_attempted"]
        )

    def test_current_source_readback_has_correct_units(self):
        report = commission_outputs_off(self.smu)
        channel = report["channels"][1]

        self.assertEqual(channel["source_mode"], "CURR")
        self.assertEqual(channel["source_setpoint"], -0.01)
        self.assertEqual(channel["source_setpoint_unit"], "A")
        self.assertEqual(channel["active_compliance_unit"], "V")

    def test_reported_output_on_is_rejected(self):
        self.smu.chan1.output = True

        with self.assertRaises(RuntimeError) as caught:
            commission_outputs_off(self.smu)

        self.assertTrue(
            caught.exception.shutdown_report["verified"]
        )
        self.assertEqual(self.smu.shutdown_calls, 2)

    def test_query_error_is_preserved(self):
        original = OSError("synthetic read failure")
        self.smu.query_error = original

        with self.assertRaises(OSError) as caught:
            commission_outputs_off(self.smu)

        self.assertIs(caught.exception, original)
        self.assertTrue(
            original.shutdown_report["verified"]
        )

    def test_cleanup_failure_does_not_replace_read_error(self):
        original = OSError("primary read failure")
        self.smu.query_error = original
        self.smu.shutdown_errors[2] = RuntimeError(
            "cleanup shutdown failed"
        )

        with self.assertRaises(OSError) as caught:
            commission_outputs_off(self.smu)

        self.assertIs(caught.exception, original)
        self.assertFalse(
            original.shutdown_report["verified"]
        )

    def test_final_shutdown_failure_prevents_success(self):
        original = OSError("final shutdown failed")
        self.smu.shutdown_errors[2] = original

        with self.assertRaises(OSError) as caught:
            commission_outputs_off(self.smu)

        self.assertIs(caught.exception, original)
        self.assertEqual(self.smu.shutdown_calls, 3)
        self.assertEqual(
            original.commissioning_report["state"],
            "raised",
        )

    def test_default_cli_does_not_construct_instrument(self):
        output = io.StringIO()

        with patch(
            "commissioning.outputs_off._new_smu"
        ) as factory:
            with redirect_stdout(output):
                code = main([
                    "--resource",
                    "FAKE_RESOURCE",
                ])

        self.assertEqual(code, 0)
        factory.assert_not_called()
        self.assertEqual(
            json.loads(output.getvalue())["state"],
            "dry_run",
        )

    def test_explicit_connect_uses_fake_and_closes_session(self):
        output = io.StringIO()

        with patch(
            "commissioning.outputs_off._new_smu",
            return_value=self.smu,
        ):
            with redirect_stdout(output):
                code = main([
                    "--resource",
                    "FAKE_RESOURCE",
                    "--connect",
                ])

        self.assertEqual(code, 0)
        self.assertEqual(self.smu.connect_calls, 1)
        self.assertEqual(self.smu.close_calls, 1)
        self.assertEqual(
            json.loads(output.getvalue())["state"],
            "outputs_off_verified",
        )

    def test_cli_failure_still_closes_session(self):
        self.smu.query_error = OSError(
            "synthetic CLI read failure"
        )
        output = io.StringIO()

        with patch(
            "commissioning.outputs_off._new_smu",
            return_value=self.smu,
        ):
            with redirect_stdout(output):
                code = main([
                    "--resource",
                    "FAKE_RESOURCE",
                    "--connect",
                ])

        self.assertEqual(code, 1)
        self.assertEqual(self.smu.close_calls, 1)
        self.assertEqual(
            json.loads(output.getvalue())["state"],
            "raised",
        )


if __name__ == "__main__":
    unittest.main()