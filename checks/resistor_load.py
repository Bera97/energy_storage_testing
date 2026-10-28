"""Low-energy resistor-load commissioning for channel 1.

OUTPUT-CAPABLE only with --execute and --resistor-only.

Fixed commissioning configuration:
- Channel 1, local/2-wire sensing.
- 1 V voltage source.
- 250 uA current compliance.
- HIZ output-off mode.
- 100 readings at 0.1 s intervals, NPLC 1.
- 15 s host deadline starting before output enabling.

Not for cells, batteries, external supplies, or discharge tests.

The finite result is retained in memory and written after shutdown.
On failure, retained partial readings are saved when possible.
Failed CSV writes are never retried.

Host deadlines cannot interrupt blocked VISA operations.
"""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from statistics import fmean
import time

from acquisition.monitored_acquisition import (
    MonitoredAcquisition,
    StopLimits,
)
from checks.outputs_off import _new_smu
from drivers.dc_output import enable_dc_output
from drivers.shutdown_reporting import attempt_shutdown_with_report
from drivers.smu_ranges import SMURanges
from drivers.trace_timebase import configure_trace_timebase
from recording.streaming_run import StreamingRun


SETTINGS = {
    "channel": 1,
    "voltage_v": 1.0,
    "current_compliance_a": 0.00025,
    "record_count": 100,
    "interval_s": 0.1,
    "nplc": 1.0,
    "host_timeout_s": 15.0,
    "maximum_sample_gap_s": 0.25,
    "minimum_voltage_v": 0.9,
    "maximum_voltage_v": 1.1,
    "minimum_current_a": 0.0001,
    "maximum_current_a": 0.0002,
    "nominal_resistance_ohm": 6930.0,
    "resistor_rating_w": 0.25,
}


class DeadlineMonitoredAcquisition(MonitoredAcquisition):
    """Check the outer host deadline immediately before INIT."""

    def __init__(self, smu, channel):
        super().__init__(smu, channel)
        self.host_deadline_s = None

    def _write(self, command):
        if command.startswith(":INIT:ACQ"):
            if self.host_deadline_s is None:
                raise RuntimeError(
                    "Host deadline must be set before initiation."
                )

            if time.monotonic() >= self.host_deadline_s:
                raise TimeoutError(
                    "Resistor-test deadline reached before INIT."
                )

        return super()._write(command)


def _prepare(smu, acquisition):
    channel = smu.chan1
    ranges = SMURanges(smu, 1)

    def checked(operation, *arguments):
        value = operation(*arguments)
        smu.check_errors()
        return value

    for item in (smu.chan1, smu.chan2):
        if checked(item.get_output_state) is not False:
            raise RuntimeError(
                "Both outputs must explicitly report OFF."
            )

    checked(channel.set_output_off_mode, "HIZ")
    checked(channel.set_auto_output_on, False)
    checked(channel.configure_fixed_dc, "VOLT")
    checked(channel.set_four_wire, False)

    ranges.set_source_autorange("VOLT", True)
    ranges.set_measurement_autorange("VOLT", True)
    ranges.set_measurement_autorange("CURR", True)

    checked(
        channel.set_current_limit,
        SETTINGS["current_compliance_a"],
    )
    checked(channel.set_voltage, SETTINGS["voltage_v"])

    acquisition.configure(
        count=SETTINGS["record_count"],
        interval_s=SETTINGS["interval_s"],
        nplc=SETTINGS["nplc"],
    )

    timebase = configure_trace_timebase(smu, 1)

    evidence = {
        "source_mode": checked(channel.get_source_mode),
        "voltage_setpoint_v": checked(
            channel.get_voltage_setpoint
        ),
        "current_compliance_a": checked(
            channel.get_current_limit
        ),
        "four_wire": checked(channel.get_four_wire),
        "output_off_mode": checked(
            channel.get_output_off_mode
        ),
        "automatic_output_on": checked(
            channel.get_auto_output_on
        ),
        "source_voltage_autorange": (
            ranges.get_source_autorange("VOLT")
        ),
        "measurement_voltage_autorange": (
            ranges.get_measurement_autorange("VOLT")
        ),
        "measurement_current_autorange": (
            ranges.get_measurement_autorange("CURR")
        ),
        "nplc": checked(channel.get_nplc),
        "timebase": timebase,
    }

    if (
        evidence["source_mode"] != "VOLT"
        or evidence["four_wire"] is not False
        or evidence["output_off_mode"] != "HIZ"
        or evidence["automatic_output_on"] is not False
        or evidence["source_voltage_autorange"] is not True
        or evidence["measurement_voltage_autorange"] is not True
        or evidence["measurement_current_autorange"] is not True
    ):
        raise RuntimeError(
            "Resistor-test configuration readback mismatch."
        )

    for key, expected in (
        ("voltage_setpoint_v", SETTINGS["voltage_v"]),
        (
            "current_compliance_a",
            SETTINGS["current_compliance_a"],
        ),
        ("nplc", SETTINGS["nplc"]),
    ):
        if not math.isclose(
            evidence[key],
            expected,
            rel_tol=1e-6,
            abs_tol=1e-12,
        ):
            raise RuntimeError(
                f"Configuration readback mismatch: {key}."
            )

    for item in (smu.chan1, smu.chan2):
        if checked(item.get_output_state) is not False:
            raise RuntimeError(
                "Output changed state during preparation."
            )

    return evidence


def _qualify_result(result):
    records = result.readings

    if (
        result.stop_reason != "completed"
        or result.requested_count != SETTINGS["record_count"]
        or len(records) != SETTINGS["record_count"]
    ):
        raise RuntimeError(
            "Resistor test did not complete its finite acquisition; "
            f"stop reason: {result.stop_reason!r}."
        )

    for index, reading in enumerate(records):
        if (
            reading.channel != 1
            or reading.has_fault
            or reading.in_compliance
            or reading.status & 1
            or not (
                SETTINGS["minimum_voltage_v"]
                < reading.voltage_v
                < SETTINGS["maximum_voltage_v"]
            )
            or not (
                SETTINGS["minimum_current_a"]
                < reading.current_a
                < SETTINGS["maximum_current_a"]
            )
        ):
            raise RuntimeError(
                f"Record {index} failed resistor-test checks."
            )

    for earlier, later in zip(records, records[1:]):
        gap = later.timestamp_s - earlier.timestamp_s

        if not 0 < gap <= SETTINGS["maximum_sample_gap_s"]:
            raise RuntimeError(
                f"Unexpected resistor-test sample gap: {gap!r}."
            )


def _cleanup_preserving_original(smu, error):
    previous = getattr(error, "shutdown_report", None)

    try:
        attempt_shutdown_with_report(smu, error)
    except BaseException as cleanup_error:
        error.add_note(
            "Resistor-test cleanup was interrupted: "
            f"{type(cleanup_error).__name__}: {cleanup_error}"
        )

    return previous


def run_resistor_test(smu, base_directory, run_id):
    """Return (MonitoredResult, recording_directory).

    Requires an already-connected instrument.
    Does not close the session.

    Recording is reserved before output enabling.
    """
    run = None
    acquisition = None
    result = None

    try:
        run = StreamingRun(
            base_directory,
            run_id,
            metadata={
                "channel": 1,
                "identity": smu.identity,
                "commissioning_test": "resistor_load",
                "settings": SETTINGS,
                "acceptance_policy": (
                    "broad_functional_checks_not_calibration"
                ),
            },
        )

        smu.shutdown()

        acquisition = DeadlineMonitoredAcquisition(smu, 1)
        preparation = _prepare(smu, acquisition)

        # Persist readbacks while outputs are still OFF.
        run._replace_metadata({
            **run._document,
            "metadata": {
                **run._document["metadata"],
                "preparation_readback": preparation,
            },
        })

        started = time.monotonic()
        acquisition.host_deadline_s = (
            started + SETTINGS["host_timeout_s"]
        )

        enable_dc_output(
            smu,
            1,
            max_voltage_v=SETTINGS["voltage_v"],
            max_current_a=SETTINGS["current_compliance_a"],
        )

        remaining = (
            acquisition.host_deadline_s - time.monotonic()
        )

        if remaining <= 0:
            raise TimeoutError(
                "Resistor-test deadline reached during activation."
            )

        limits = StopLimits(
            min_voltage_v=SETTINGS["minimum_voltage_v"],
            max_voltage_v=SETTINGS["maximum_voltage_v"],
            min_current_a=SETTINGS["minimum_current_a"],
            max_current_a=SETTINGS["maximum_current_a"],
            stop_on_compliance=True,
        )

        result = acquisition.acquire(
            limits,
            timeout_s=remaining,
            poll_interval_s=0.05,
        )

        # Additional executor-level shutdown before file export.
        smu.shutdown()

        run.writer.write_batch(result.readings)

        if time.monotonic() >= acquisition.host_deadline_s:
            raise TimeoutError(
                "Resistor-test host deadline was exceeded."
            )

        _qualify_result(result)
        directory = run.finish(result)

        return result, directory

    except BaseException as original_error:
        if not hasattr(original_error, "partial_readings"):
            original_error.partial_readings = tuple(
                result.readings
                if result is not None
                else getattr(acquisition, "readings", ())
            )

        previous_shutdown = _cleanup_preserving_original(
            smu,
            original_error,
        )

        if run is not None and not run.finalized:
            if previous_shutdown is not None:
                run._document["metadata"][
                    "previous_shutdown_report"
                ] = previous_shutdown

            # Preserve retained data after cleanup. Never retry
            # a writer that may have partly written a failed batch.
            if (
                not run.writer.closed
                and not run.writer.failed
                and run.writer.records_flushed == 0
            ):
                try:
                    run.writer.write_batch(
                        original_error.partial_readings
                    )
                except BaseException as write_error:
                    original_error.add_note(
                        "Partial resistor data could not be saved: "
                        f"{type(write_error).__name__}: {write_error}"
                    )

            try:
                original_error.run_directory = run.fail(
                    original_error
                )
            except BaseException as export_error:
                original_error.add_note(
                    "Resistor failure export also failed: "
                    f"{type(export_error).__name__}: {export_error}"
                )
                try:
                    run.close()
                except BaseException as close_error:
                    original_error.add_note(
                        "Resistor CSV close also failed: "
                        f"{type(close_error).__name__}: {close_error}"
                    )

        raise


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Channel-1 resistor commissioning: 1 V, 250 uA "
            "compliance, 100 samples. Defaults to dry run."
        )
    )
    parser.add_argument("--resource", required=True)
    parser.add_argument(
        "--base-directory",
        default="commissioning_runs",
    )
    parser.add_argument(
        "--run-id",
        default=None,
    )
    parser.add_argument(
        "--execute",
        action="store_true",
    )
    parser.add_argument(
        "--resistor-only",
        action="store_true",
        help=(
            "Confirm that only the resistor and voltage-mode "
            "multimeter are connected; no cell or external supply."
        ),
    )
    parser.add_argument(
        "--visa-library",
        default=None,
    )

    arguments = parser.parse_args(argv)

    if not arguments.resource.strip():
        parser.error("--resource must not be empty.")

    if arguments.execute and not arguments.resistor_only:
        parser.error(
            "--execute requires --resistor-only."
        )

    run_id = arguments.run_id or datetime.now(
        timezone.utc
    ).strftime("resistor_%Y%m%dT%H%M%S%fZ")

    if not arguments.execute:
        print(json.dumps({
            "state": "dry_run",
            "resource": arguments.resource,
            "run_id": run_id,
            "base_directory": arguments.base_directory,
            "will_connect": False,
            "will_enable_output": False,
            "settings": SETTINGS,
        }, indent=2))
        return 0

    smu = None
    primary_error = None
    result = None
    directory = None

    try:
        smu = _new_smu(
            arguments.resource,
            5000,
            arguments.visa_library,
        )
        smu.connect()

        result, directory = run_resistor_test(
            smu,
            arguments.base_directory,
            run_id,
        )

    except BaseException as error:
        primary_error = error

    finally:
        if smu is not None:
            if smu.is_connected:
                try:
                    smu.shutdown()
                except BaseException as cleanup_error:
                    if primary_error is None:
                        primary_error = cleanup_error
                    else:
                        primary_error.add_note(
                            "Final session shutdown also failed: "
                            f"{type(cleanup_error).__name__}: "
                            f"{cleanup_error}"
                        )

            try:
                smu.close()
            except BaseException as close_error:
                if primary_error is None:
                    primary_error = close_error
                else:
                    primary_error.add_note(
                        "VISA close also failed: "
                        f"{type(close_error).__name__}: {close_error}"
                    )

    if primary_error is not None:
        print(json.dumps({
            "state": "raised",
            "error_type": type(primary_error).__name__,
            "error_message": str(primary_error),
            "error_notes": list(
                getattr(primary_error, "__notes__", ())
            ),
            "run_directory": str(
                getattr(
                    primary_error,
                    "run_directory",
                    directory,
                )
            ),
        }, indent=2))
        return (
            130
            if isinstance(primary_error, KeyboardInterrupt)
            else 1
        )

    mean_voltage = fmean(
        reading.voltage_v for reading in result.readings
    )
    mean_current = fmean(
        reading.current_a for reading in result.readings
    )

    print(json.dumps({
        "state": "resistor_test_completed",
        "run_directory": str(Path(directory).resolve()),
        "record_count": len(result.readings),
        "mean_voltage_v": mean_voltage,
        "mean_current_a": mean_current,
        "resistance_from_mean_vi_ohm": (
            mean_voltage / mean_current
        ),
        "shutdown_completed": True,
        "independent_multimeter_check_required": True,
    }, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())