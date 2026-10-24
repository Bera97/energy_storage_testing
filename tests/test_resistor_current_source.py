"""Offline tests for positive current-source commissioning."""

from contextlib import redirect_stdout
import io
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from acquisition.monitored_acquisition import MonitoredResult
from acquisition.spot_acquisition import SMUReading
from commissioning.resistor_current_source import (
    _prepare_current_source,
    _qualify_result,
    main,
    run_current_source_test,
)


def make_result(*, status=1, voltage=0.693, current=0.0001):
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


class ResistorCurrentSourceTests(unittest.TestCase):
    def test_expected_current_source_result_qualifies(self):
        _qualify_result(make_result())

    def test_voltage_source_status_is_rejected(self):
        with self.assertRaises(RuntimeError):
            _qualify_result(make_result(status=0))

    def test_compliance_is_rejected(self):
        with self.assertRaises(RuntimeError):
            _qualify_result(make_result(status=1 | 2))

    def test_measurement_fault_is_rejected(self):
        with self.assertRaises(RuntimeError):
            _qualify_result(make_result(status=1 | 8))

    def test_wrong_current_direction_is_rejected(self):
        with self.assertRaises(RuntimeError):
            _qualify_result(make_result(current=-0.0001))

    def test_preparation_selects_current_mode_and_voltage_compliance(self):
        smu = Mock()
        smu.chan1.get_output_state.return_value = False
        smu.chan2.get_output_state.return_value = False

        channel = smu.chan1
        channel.get_source_mode.return_value = "CURR"
        channel.get_current_setpoint.return_value = 0.0001
        channel.get_voltage_limit.return_value = 1.0
        channel.get_four_wire.return_value = False
        channel.get_output_off_mode.return_value = "HIZ"
        channel.get_auto_output_on.return_value = False
        channel.get_nplc.return_value = 1.0

        acquisition = Mock()

        with patch(
            "commissioning.resistor_current_source.SMURanges"
        ) as ranges_class, patch(
            "commissioning.resistor_current_source."
            "configure_trace_timebase",
            return_value={"offline_fake": True},
        ):
            ranges = ranges_class.return_value
            ranges.get_source_autorange.return_value = True
            ranges.get_measurement_autorange.return_value = True

            evidence = _prepare_current_source(
                smu,
                acquisition,
            )

        channel.configure_fixed_dc.assert_called_once_with(
            "CURR"
        )
        channel.set_current.assert_called_once_with(0.0001)
        channel.set_voltage_limit.assert_called_once_with(1.0)
        channel.set_voltage.assert_not_called()
        channel.set_current_limit.assert_not_called()
        self.assertEqual(evidence["source_mode"], "CURR")

    def test_dry_run_does_not_construct_instrument(self):
        output = io.StringIO()

        with patch(
            "commissioning.resistor_current_source._new_smu"
        ) as factory:
            with redirect_stdout(output):
                code = main([
                    "--resource", "FAKE_RESOURCE",
                ])

        self.assertEqual(code, 0)
        factory.assert_not_called()
        self.assertEqual(
            json.loads(output.getvalue())["state"],
            "dry_run",
        )

    def test_fake_run_finalizes(self):
        smu = FakeSMU()

        with tempfile.TemporaryDirectory() as temporary:
            with patch(
                "commissioning.resistor_current_source."
                "DeadlineMonitoredAcquisition",
                FakeAcquisition,
            ), patch(
                "commissioning.resistor_current_source."
                "_prepare_current_source",
                return_value={"offline_fake": True},
            ), patch(
                "commissioning.resistor_current_source."
                "enable_dc_output"
            ):
                result, directory = run_current_source_test(
                    smu,
                    temporary,
                    "offline_current_source",
                )

            self.assertTrue(
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
                "commissioning.resistor_current_source."
                "DeadlineMonitoredAcquisition",
                FakeAcquisition,
            ), patch(
                "commissioning.resistor_current_source."
                "_prepare_current_source",
                return_value={"offline_fake": True},
            ), patch(
                "commissioning.resistor_current_source."
                "enable_dc_output",
                side_effect=original,
            ):
                with self.assertRaises(OSError) as caught:
                    run_current_source_test(
                        smu,
                        temporary,
                        "failed_current_source",
                    )

        self.assertIs(caught.exception, original)
        self.assertTrue(
            original.shutdown_report["verified"]
        )


if __name__ == "__main__":
    unittest.main()