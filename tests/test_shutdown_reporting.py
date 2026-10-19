"""Offline tests for structured shutdown evidence.

No VISA connection or hardware required.
"""

import json
import unittest

from drivers.shutdown_reporting import (
    attempt_shutdown_with_report,
)


class FakeSMU:
    def __init__(self, shutdown_error=None):
        self.shutdown_calls = 0
        self.shutdown_error = shutdown_error

    def shutdown(self):
        self.shutdown_calls += 1

        if self.shutdown_error is not None:
            raise self.shutdown_error


class ShutdownReportingTests(unittest.TestCase):
    def test_success_attaches_verified_report(self):
        smu = FakeSMU()
        original = TimeoutError("original timeout")

        report = attempt_shutdown_with_report(smu, original)

        self.assertEqual(smu.shutdown_calls, 1)
        self.assertTrue(report["attempted"])
        self.assertIs(report["verified"], True)
        self.assertEqual(
            report["evidence"],
            "smu_shutdown_returned",
        )
        self.assertIsNone(report["error_type"])
        self.assertIsNone(report["error_message"])
        self.assertEqual(original.shutdown_report, report)

        # The report can be included directly in JSON metadata.
        restored = json.loads(json.dumps(report))
        self.assertEqual(restored, report)

    def test_failure_is_recorded_without_replacing_original(self):
        shutdown_error = RuntimeError(
            "simulated output-state query failure"
        )
        smu = FakeSMU(shutdown_error)
        original = TimeoutError("original acquisition timeout")

        report = attempt_shutdown_with_report(smu, original)

        self.assertEqual(smu.shutdown_calls, 1)
        self.assertIs(report["verified"], False)
        self.assertEqual(
            report["evidence"],
            "smu_shutdown_raised",
        )
        self.assertEqual(report["error_type"], "RuntimeError")
        self.assertEqual(
            report["error_message"],
            "simulated output-state query failure",
        )

        self.assertEqual(
            str(original),
            "original acquisition timeout",
        )
        self.assertTrue(
            any(
                "Shutdown could not be verified" in note
                for note in original.__notes__
            )
        )

    def test_original_keyboard_interrupt_is_preserved(self):
        smu = FakeSMU()
        original = KeyboardInterrupt("operator cancellation")

        def cancellation_cleanup():
            try:
                raise original
            except BaseException as error:
                attempt_shutdown_with_report(smu, error)
                raise

        with self.assertRaises(KeyboardInterrupt) as caught:
            cancellation_cleanup()

        self.assertIs(caught.exception, original)
        self.assertIs(
            caught.exception.shutdown_report["verified"],
            True,
        )
        self.assertEqual(smu.shutdown_calls, 1)

    def test_existing_notes_and_partial_records_are_preserved(self):
        smu = FakeSMU(
            OSError("simulated shutdown communication failure")
        )
        original = RuntimeError("original acquisition failure")
        original.partial_readings = ("example retained record",)
        original.add_note("existing diagnostic note")

        attempt_shutdown_with_report(smu, original)

        self.assertEqual(
            original.partial_readings,
            ("example retained record",),
        )
        self.assertEqual(
            original.__notes__[0],
            "existing diagnostic note",
        )
        self.assertEqual(len(original.__notes__), 2)

    def test_returned_report_does_not_mutate_attached_report(self):
        smu = FakeSMU()
        original = RuntimeError("original error")

        report = attempt_shutdown_with_report(smu, original)
        report["verified"] = False

        self.assertIs(
            original.shutdown_report["verified"],
            True,
        )

    def test_second_interrupt_leaves_verification_unknown(self):
        second_interrupt = KeyboardInterrupt(
            "interrupted during shutdown"
        )
        smu = FakeSMU(second_interrupt)
        original = TimeoutError("original acquisition timeout")

        with self.assertRaises(KeyboardInterrupt) as caught:
            attempt_shutdown_with_report(smu, original)

        self.assertIs(caught.exception, second_interrupt)
        self.assertEqual(smu.shutdown_calls, 1)
        self.assertTrue(original.shutdown_report["attempted"])
        self.assertIsNone(
            original.shutdown_report["verified"]
        )
        self.assertEqual(
            original.shutdown_report["evidence"],
            "shutdown_attempt_started",
        )

    def test_invalid_original_error_is_rejected_without_shutdown(self):
        smu = FakeSMU()

        with self.assertRaises(TypeError):
            attempt_shutdown_with_report(smu, "not an exception")

        self.assertEqual(smu.shutdown_calls, 0)


if __name__ == "__main__":
    unittest.main()