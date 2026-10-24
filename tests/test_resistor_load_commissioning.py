"""Offline tests for resistor-load commissioning."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acquisition.monitored_acquisition import MonitoredResult
from acquisition.spot_acquisition import SMUReading
from commissioning.resistor_load import (
    DeadlineMonitoredAcquisition,
    SETTINGS,
    _qualify_result,
    main,
    run_resistor_test,
)


def nominal_result():
    records = tuple(
        SMUReading(
            channel=1,
            voltage_v=1.0,
            current_a=1.0 / 6930.0,
            timestamp_s=index * 0.1,
            status=0,
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
    def __init__(self, smu, channel):
        self.smu = smu
        self.channel = channel
        self.host_deadline_s = None
        self.readings = ()

    def acquire(self, limits, *, timeout_s, poll_interval_s):
        result = nominal_result()
        self.readings = result.readings
        self.smu.shutdown()
        return result


class ResistorLoadCommissioningTests(unittest.TestCase):
    def test_nominal_result_qualifies(self):
        _qualify_result(nominal_result())

    def test_wrong_stop_reason_is_rejected(self):
        nominal = nominal_result()

        result = MonitoredResult(
            readings=nominal.readings,
            stop_reason="compliance",
            triggering_reading=nominal.readings[0],
            decision_elapsed_s=0.1,
            shutdown_elapsed_s=0.2,
            requested_count=100,
        )

        with self.assertRaises(RuntimeError):
            _qualify_result(result)

    def test_out_of_window_measurement_is_rejected(self):
        nominal = nominal_result()
        records = list(nominal.readings)
        records[50] = SMUReading(
            channel=1,
            voltage_v=1.0,
            current_a=0.00024,
            timestamp_s=5.0,
            status=0,
        )

        result = MonitoredResult(
            readings=tuple(records),
            stop_reason="completed",
            triggering_reading=None,
            decision_elapsed_s=10.0,
            shutdown_elapsed_s=10.1,
            requested_count=100,
        )

        with self.assertRaisesRegex(RuntimeError, "Record 50"):
            _qualify_result(result)

    def test_expired_deadline_prevents_init_write(self):
        acquisition = DeadlineMonitoredAcquisition(
            object(), 1
        )
        acquisition.host_deadline_s = 1.0

        with patch(
            "commissioning.resistor_load.time.monotonic",
            return_value=2.0,
        ):
            with self.assertRaises(TimeoutError):
                acquisition._write(":INIT:ACQ (@1)")

    def test_dry_run_does_not_construct_instrument(self):
        output = io.StringIO()

        with patch(
            "commissioning.resistor_load._new_smu"
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

    def test_execute_requires_resistor_confirmation(self):
        with redirect_stdout(io.StringIO()):
            with patch("sys.stderr", new=io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    main([
                        "--resource", "FAKE_RESOURCE",
                        "--execute",
                    ])

        self.assertEqual(caught.exception.code, 2)

    def test_fake_run_finalizes_recording(self):
        smu = FakeSMU()

        with tempfile.TemporaryDirectory() as temporary:
            with patch(
                "commissioning.resistor_load."
                "DeadlineMonitoredAcquisition",
                FakeAcquisition,
            ), patch(
                "commissioning.resistor_load._prepare",
                return_value={"offline_fake": True},
            ), patch(
                "commissioning.resistor_load.enable_dc_output"
            ) as enable:
                result, directory = run_resistor_test(
                    smu,
                    temporary,
                    "offline_resistor",
                )

            self.assertEqual(len(result.readings), 100)
            self.assertTrue(
                (directory / "COMPLETE").is_file()
            )
            self.assertTrue(
                (directory / "measurements.csv").is_file()
            )
            enable.assert_called_once()
            self.assertGreaterEqual(smu.shutdown_calls, 3)

    def test_activation_error_is_preserved_and_recorded(self):
        smu = FakeSMU()
        original = OSError("synthetic activation failure")

        with tempfile.TemporaryDirectory() as temporary:
            with patch(
                "commissioning.resistor_load."
                "DeadlineMonitoredAcquisition",
                FakeAcquisition,
            ), patch(
                "commissioning.resistor_load._prepare",
                return_value={"offline_fake": True},
            ), patch(
                "commissioning.resistor_load.enable_dc_output",
                side_effect=original,
            ):
                with self.assertRaises(OSError) as caught:
                    run_resistor_test(
                        smu,
                        temporary,
                        "failed_resistor",
                    )

            self.assertIs(caught.exception, original)

            directory = Path(temporary) / "failed_resistor"
            document = json.loads(
                (directory / "metadata.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(
                document["outcome"]["state"],
                "raised",
            )
            self.assertTrue(
                document["outcome"]["shutdown_verified"]
            )
            self.assertFalse(
                (directory / "COMPLETE").exists()
            )


if __name__ == "__main__":
    unittest.main()