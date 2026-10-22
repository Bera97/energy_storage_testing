"""Offline tests for outputs-OFF rest."""

import unittest

from experiments.rest_outputs_off import rest_outputs_off


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, duration):
        self.sleeps.append(duration)
        self.now += duration


class FakeChannel:
    def __init__(self):
        self.states = [False]
        self.calls = 0

    def get_output_state(self):
        self.calls += 1

        if len(self.states) > 1:
            return self.states.pop(0)

        return self.states[0]


class FakeSMU:
    def __init__(self):
        self.chan1 = FakeChannel()
        self.chan2 = FakeChannel()
        self.shutdown_calls = 0
        self.shutdown_errors = {}
        self.error = None

    def shutdown(self):
        self.shutdown_calls += 1
        error = self.shutdown_errors.get(self.shutdown_calls)

        if error is not None:
            raise error

    def check_errors(self):
        if self.error is not None:
            raise self.error


class RestOutputsOffTests(unittest.TestCase):
    def setUp(self):
        self.smu = FakeSMU()
        self.clock = FakeClock()

    def rest(self, duration=0.25, **changes):
        return rest_outputs_off(
            self.smu,
            duration,
            **{
                "poll_interval_s": 0.1,
                "clock": self.clock,
                "sleep": self.clock.sleep,
                **changes,
            },
        )

    def test_success_checks_outputs_and_shuts_down_twice(self):
        report = self.rest()

        self.assertEqual(report["state"], "returned")
        self.assertAlmostEqual(report["elapsed_s"], 0.25)
        self.assertTrue(
            report["initial_shutdown_verified"]
        )
        self.assertTrue(report["shutdown_verified"])
        self.assertTrue(
            report["both_outputs_reported_off"]
        )
        self.assertEqual(self.smu.shutdown_calls, 2)
        self.assertEqual(report["output_check_count"], 4)
        self.assertEqual(self.smu.chan1.calls, 4)
        self.assertEqual(self.smu.chan2.calls, 4)

    def test_last_sleep_is_limited_to_remaining_duration(self):
        self.rest()

        self.assertEqual(len(self.clock.sleeps), 3)
        self.assertAlmostEqual(self.clock.sleeps[0], 0.1)
        self.assertAlmostEqual(self.clock.sleeps[1], 0.1)
        self.assertAlmostEqual(self.clock.sleeps[2], 0.05)

    def test_zero_duration_still_verifies_and_cleans_up(self):
        report = self.rest(0)

        self.assertEqual(report["elapsed_s"], 0)
        self.assertEqual(report["output_check_count"], 1)
        self.assertEqual(self.smu.shutdown_calls, 2)
        self.assertEqual(self.clock.sleeps, [])

    def test_output_on_during_rest_is_rejected(self):
        self.smu.chan2.states = [False, True]

        with self.assertRaisesRegex(RuntimeError, "OFF") as caught:
            self.rest()

        self.assertEqual(self.smu.shutdown_calls, 2)
        self.assertEqual(
            caught.exception.rest_report["state"],
            "raised",
        )
        self.assertTrue(
            caught.exception.shutdown_report["verified"]
        )

    def test_unknown_output_state_is_rejected(self):
        self.smu.chan1.states = [None]

        with self.assertRaises(RuntimeError):
            self.rest()

        self.assertEqual(self.smu.shutdown_calls, 2)

    def test_integer_zero_is_not_accepted_as_explicit_false(self):
        self.smu.chan1.states = [0]

        with self.assertRaises(RuntimeError):
            self.rest()

    def test_instrument_error_is_preserved(self):
        original = OSError("instrument error query failed")
        self.smu.error = original

        with self.assertRaises(OSError) as caught:
            self.rest()

        self.assertIs(caught.exception, original)
        self.assertTrue(
            original.shutdown_report["verified"]
        )

    def test_initial_shutdown_failure_still_attempts_cleanup(self):
        original = OSError("initial shutdown failed")
        self.smu.shutdown_errors[1] = original

        with self.assertRaises(OSError) as caught:
            self.rest()

        self.assertIs(caught.exception, original)
        self.assertEqual(self.smu.shutdown_calls, 2)
        self.assertFalse(
            original.rest_report["initial_shutdown_verified"]
        )
        self.assertTrue(
            original.rest_report["shutdown_verified"]
        )

    def test_cleanup_failure_does_not_replace_original(self):
        original = KeyboardInterrupt("synthetic interruption")
        cleanup = OSError("cleanup shutdown failed")

        def interrupted_sleep(duration):
            raise original

        self.smu.shutdown_errors[2] = cleanup

        with self.assertRaises(KeyboardInterrupt) as caught:
            self.rest(sleep=interrupted_sleep)

        self.assertIs(caught.exception, original)
        self.assertFalse(
            original.shutdown_report["verified"]
        )
        self.assertEqual(
            original.rest_report["cleanup_error"]["type"],
            "OSError",
        )
        self.assertTrue(
            any(
                "cleanup shutdown failed" in note
                for note in original.__notes__
            )
        )

    def test_final_shutdown_failure_prevents_success(self):
        original = OSError("final shutdown failed")
        self.smu.shutdown_errors[2] = original

        with self.assertRaises(OSError) as caught:
            self.rest()

        self.assertIs(caught.exception, original)
        self.assertEqual(
            original.rest_report["state"],
            "raised",
        )
        self.assertFalse(
            original.shutdown_report["verified"]
        )

    def test_backwards_clock_is_rejected(self):
        values = iter([10.0, 9.0])

        with self.assertRaisesRegex(RuntimeError, "backwards"):
            self.rest(clock=lambda: next(values))

        self.assertEqual(self.smu.shutdown_calls, 2)

    def test_invalid_duration_prevents_instrument_access(self):
        for value in (
            -1,
            True,
            None,
            "1",
            float("nan"),
            float("inf"),
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.rest(value)

        self.assertEqual(self.smu.shutdown_calls, 0)

    def test_invalid_poll_interval_prevents_instrument_access(self):
        for value in (0, -1, True, float("nan")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.rest(poll_interval_s=value)

        self.assertEqual(self.smu.shutdown_calls, 0)


if __name__ == "__main__":
    unittest.main()