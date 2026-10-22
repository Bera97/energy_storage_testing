"""Offline tests for explicit trace timestamp configuration."""

import unittest

from drivers.trace_timebase import configure_trace_timebase


class FakeChannel:
    def __init__(self, number, output_on=False):
        self.channel = number
        self.output_on = output_on

    def get_output_state(self):
        return self.output_on


class FakeSMU:
    def __init__(self):
        self.chan1 = FakeChannel(1)
        self.chan2 = FakeChannel(2)
        self.commands = []
        self.queries = []
        self.error_checks = 0
        self.responses = {
            ":SYST:TIME:TIM:COUN:RES:AUTO?": "1",
            ":TRAC1:TST:FORM?": "ABS",
            ":TRAC2:TST:FORM?": "ABS",
        }
        self.error_on_write = None

    def check_errors(self):
        self.error_checks += 1

    def abort(self):
        self.commands.append("ABORT")

    def _write(self, command):
        self.commands.append(command)

        if self.error_on_write is not None:
            raise self.error_on_write

    def _query(self, command):
        self.queries.append(command)
        return self.responses[command]


class TraceTimebaseTests(unittest.TestCase):
    def test_configures_and_verifies_channel_one(self):
        smu = FakeSMU()

        result = configure_trace_timebase(smu, 1)

        self.assertEqual(
            smu.commands,
            [
                "ABORT",
                ":SYST:TIME:TIM:COUN:RES:AUTO ON",
                ":TRAC1:TST:FORM ABS",
            ],
        )
        self.assertEqual(
            smu.queries,
            [
                ":SYST:TIME:TIM:COUN:RES:AUTO?",
                ":TRAC1:TST:FORM?",
            ],
        )
        self.assertTrue(result["timer_auto_reset"])
        self.assertTrue(result["host_phase_deadline_required"])
        self.assertFalse(result["output_enable_time_established"])

    def test_channel_two_uses_its_trace_commands(self):
        smu = FakeSMU()

        result = configure_trace_timebase(smu, 2)

        self.assertEqual(result["channel"], 2)
        self.assertIn(":TRAC2:TST:FORM ABS", smu.commands)

    def test_either_output_on_prevents_configuration(self):
        for selected in ("chan1", "chan2"):
            with self.subTest(selected=selected):
                smu = FakeSMU()
                getattr(smu, selected).output_on = True

                with self.assertRaisesRegex(
                    RuntimeError, "Both outputs"
                ):
                    configure_trace_timebase(smu, 1)

                self.assertEqual(smu.commands, [])
                self.assertEqual(smu.queries, [])

    def test_invalid_channel_prevents_instrument_access(self):
        for channel in (True, 0, 3, 1.0, "1"):
            with self.subTest(channel=channel):
                smu = FakeSMU()

                with self.assertRaises(ValueError):
                    configure_trace_timebase(smu, channel)

                self.assertEqual(smu.error_checks, 0)
                self.assertEqual(smu.commands, [])

    def test_disabled_timer_reset_is_rejected(self):
        smu = FakeSMU()
        smu.responses[
            ":SYST:TIME:TIM:COUN:RES:AUTO?"
        ] = "0"

        with self.assertRaisesRegex(
            RuntimeError, "automatic reset"
        ):
            configure_trace_timebase(smu, 1)

        self.assertNotIn(":TRAC1:TST:FORM ABS", smu.commands)

    def test_wrong_trace_format_is_rejected(self):
        smu = FakeSMU()
        smu.responses[":TRAC1:TST:FORM?"] = "DELT"

        with self.assertRaisesRegex(
            RuntimeError, "timestamp format"
        ):
            configure_trace_timebase(smu, 1)

    def test_write_failure_preserves_original_exception(self):
        smu = FakeSMU()
        original = OSError("simulated communication failure")
        smu.error_on_write = original

        with self.assertRaises(OSError) as caught:
            configure_trace_timebase(smu, 1)

        self.assertIs(caught.exception, original)
        self.assertEqual(
            smu.commands,
            [
                "ABORT",
                ":SYST:TIME:TIM:COUN:RES:AUTO ON",
            ],
        )

    def test_no_output_on_or_init_command_is_sent(self):
        smu = FakeSMU()

        configure_trace_timebase(smu, 1)

        for command in smu.commands:
            self.assertFalse(command.startswith(":OUTP"))
            self.assertFalse(command.startswith(":INIT"))


if __name__ == "__main__":
    unittest.main()