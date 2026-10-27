"""Offline tests for intentional resistor current compliance."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acquisition.monitored_acquisition import MonitoredResult
from acquisition.spot_acquisition import SMUReading
from checks.resistor_compliance import (
    _qualify_result,
    main,
    run_compliance_test,
)


def make_result(*, status=2, voltage=0.693, current=0.0001):
    records = tuple(
        SMUReading(
            channel=1,
            voltage_v=voltage,
            current_a=current,
            timestamp_s=index * 0.1,
            status=status,
        )
        for index in range(100)
    )

    return MonitoredResult(
        readings=records,
        stop_reason="completed",
        triggering_reading=None,
        decision_elapsed_s=10.0,
        shutdown_elapsed_s=10.1,
        requested_count=100,
    )


class FakeSMU:
    identity = "Keysight Technologies,B2902B,FAKE,FAKE"

    def __init__(self):
        self.shutdown_calls = 0

    def shutdown(self):
        self.shutdown_calls += 1


class FakeAcquisition:
    last_limits = None

    def __init__(self, smu, channel):
        self.smu = smu
        self.readings = ()
        self.host_deadline_s = None

    def acquire(self, limits, *, timeout_s, poll_interval_s):
        type(self).last_limits = limits
        result = make_result()
        self.readings = result.readings
        self.smu.shutdown()
        return result


class ResistorComplianceTests(unittest.TestCase):
    def test_expected_compliance_result_qualifies(self):
        result = make_result()

        self.assertTrue(
            result.readings[0].in_compliance
        )
        _qualify_result(result)

    def test_absent_compliance_indication_is_rejected(self):
        with self.assertRaisesRegex(
            RuntimeError,
            "No retained record reported compliance",
        ):
            _qualify_result(make_result(status=0))

    def test_normal_one_volt_result_is_rejected(self):
        with self.assertRaises(RuntimeError):
            _qualify_result(
                make_result(
                    status=0,
                    voltage=1.0,
                    current=1.0 / 6930.0,
                )
            )

    def test_measurement_fault_is_rejected(self):
        with self.assertRaises(RuntimeError):
            _qualify_result(make_result(status=2 | 8))

    def test_default_cli_does_not_construct_instrument(self):
        output = io.StringIO()

        with patch(
            "checks.resistor_compliance._new_smu"
        ) as factory:
            with redirect_stdout(output):
                code = main([
                    "--resource", "FAKE_RESOURCE",
                ])

        self.assertEqual(code, 0)
        factory.assert_not_called()
        document = json.loads(output.getvalue())
        self.assertEqual(document["state"], "dry_run")
        self.assertTrue(document["compliance_expected"])
        self.assertEqual(
            document["settings"]["current_compliance_a"],
            0.0001,
        )

    def test_fake_run_allows_compliance_and_finalizes(self):
        smu = FakeSMU()

        with tempfile.TemporaryDirectory() as temporary:
            with patch(
                "checks.resistor_compliance."
                "DeadlineMonitoredAcquisition",
                FakeAcquisition,
            ), patch(
                "checks.resistor_compliance."
                "_prepare_compliance",
                return_value={"offline_fake": True},
            ), patch(
                "checks.resistor_compliance.enable_dc_output"
            ):
                result, directory = run_compliance_test(
                    smu,
                    temporary,
                    "offline_compliance",
                )

            self.assertFalse(
                FakeAcquisition.last_limits.stop_on_compliance
            )
            self.assertEqual(len(result.readings), 100)
            self.assertTrue(
                (directory / "COMPLETE").is_file()
            )

    def test_activation_error_is_preserved(self):
        smu = FakeSMU()
        original = OSError("synthetic activation failure")

        with tempfile.TemporaryDirectory() as temporary:
            with patch(
                "checks.resistor_compliance."
                "DeadlineMonitoredAcquisition",
                FakeAcquisition,
            ), patch(
                "checks.resistor_compliance."
                "_prepare_compliance",
                return_value={"offline_fake": True},
            ), patch(
                "checks.resistor_compliance.enable_dc_output",
                side_effect=original,
            ):
                with self.assertRaises(OSError) as caught:
                    run_compliance_test(
                        smu,
                        temporary,
                        "failed_compliance",
                    )

            self.assertIs(caught.exception, original)
            self.assertTrue(
                original.shutdown_report["verified"]
            )

            directory = Path(temporary) / "failed_compliance"
            self.assertFalse(
                (directory / "COMPLETE").exists()
            )


if __name__ == "__main__":
    unittest.main()