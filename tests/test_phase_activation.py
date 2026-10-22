"""Offline tests for the phase activation boundary."""

import unittest
from unittest.mock import patch

from test_rest_outputs_off import FakeSMU

from experiments.phase_activation import activate_phase_output


class PhaseActivationTests(unittest.TestCase):
    def setUp(self):
        self.smu = FakeSMU()
        self.times = iter([10.0, 10.25])

        patcher = patch(
            "experiments.phase_activation.enable_dc_output",
            side_effect=self.fake_enable,
        )
        self.enable = patcher.start()
        self.addCleanup(patcher.stop)

    def fake_enable(self, smu, channel, **arguments):
        selected = smu.chan1 if channel == 1 else smu.chan2
        selected.states = [True]

    def activate(self, channel=1, **changes):
        return activate_phase_output(
            self.smu,
            channel,
            **{
                "maximum_voltage_v": 4.2,
                "maximum_current_a": 2.0,
                "clock": lambda: next(self.times),
                **changes,
            },
        )

    def test_success_records_host_bracket(self):
        evidence = self.activate()

        self.assertEqual(evidence.channel, 1)
        self.assertEqual(evidence.host_before_enable_s, 10.0)
        self.assertEqual(
            evidence.host_after_verification_s, 10.25
        )
        self.assertEqual(
            evidence.enable_call_and_verification_elapsed_s,
            0.25,
        )
        self.assertEqual(self.smu.shutdown_calls, 0)

        self.enable.assert_called_once_with(
            self.smu,
            1,
            max_voltage_v=4.2,
            max_current_a=2.0,
        )

    def test_channel_two_can_be_selected(self):
        evidence = self.activate(channel=2)

        self.assertEqual(evidence.channel, 2)
        self.assertIs(
            self.smu.chan1.get_output_state(), False
        )
        self.assertIs(
            self.smu.chan2.get_output_state(), True
        )

    def test_initial_output_on_prevents_enable(self):
        self.smu.chan1.states = [True]

        with self.assertRaises(RuntimeError):
            self.activate()

        self.enable.assert_not_called()
        self.assertEqual(self.smu.shutdown_calls, 1)

    def test_enable_failure_is_preserved(self):
        original = OSError("synthetic enable failure")
        self.enable.side_effect = original

        with self.assertRaises(OSError) as caught:
            self.activate()

        self.assertIs(caught.exception, original)
        self.assertTrue(
            original.shutdown_report["verified"]
        )
        self.assertEqual(
            original.activation_report["state"],
            "raised",
        )

    def test_unselected_output_on_triggers_shutdown(self):
        def enable_both(smu, channel, **arguments):
            smu.chan1.states = [True]
            smu.chan2.states = [True]

        self.enable.side_effect = enable_both

        with self.assertRaisesRegex(
            RuntimeError, "Unselected"
        ):
            self.activate()

        self.assertEqual(self.smu.shutdown_calls, 1)

    def test_backwards_clock_triggers_shutdown(self):
        self.times = iter([10.0, 9.0])

        with self.assertRaisesRegex(
            RuntimeError, "backwards"
        ):
            self.activate()

        self.assertEqual(self.smu.shutdown_calls, 1)

    def test_shutdown_failure_does_not_replace_enable_error(self):
        original = OSError("enable failed")
        self.enable.side_effect = original
        self.smu.shutdown_errors[1] = RuntimeError(
            "shutdown failed"
        )

        with self.assertRaises(OSError) as caught:
            self.activate()

        self.assertIs(caught.exception, original)
        self.assertFalse(
            original.shutdown_report["verified"]
        )
        self.assertEqual(
            original.activation_report["cleanup_error"]["type"],
            "RuntimeError",
        )

    def test_second_interrupt_does_not_replace_enable_error(self):
        original = OSError("enable failed")
        self.enable.side_effect = original
        self.smu.shutdown_errors[1] = KeyboardInterrupt(
            "cleanup interrupted"
        )

        with self.assertRaises(OSError) as caught:
            self.activate()

        self.assertIs(caught.exception, original)
        self.assertIsNone(
            original.shutdown_report["verified"]
        )
        self.assertTrue(
            any(
                "cleanup was interrupted" in note
                for note in original.__notes__
            )
        )

    def test_invalid_arguments_do_not_touch_instrument(self):
        for changes in (
            {"maximum_voltage_v": 0},
            {"maximum_current_a": -1},
            {"maximum_current_a": True},
            {"maximum_voltage_v": float("nan")},
            {"clock": None},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.activate(**changes)

        self.enable.assert_not_called()
        self.assertEqual(self.smu.shutdown_calls, 0)
        self.assertEqual(self.smu.chan1.calls, 0)
        self.assertEqual(self.smu.chan2.calls, 0)


if __name__ == "__main__":
    unittest.main()