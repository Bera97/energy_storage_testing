"""Positive current-source commissioning on channel 1.

OUTPUT-CAPABLE only with --execute and --resistor-only.

For the existing approximately 6.93 kohm resistor only:
- Current-source setpoint: +100 uA.
- Voltage compliance: 1 V.
- Expected terminal voltage: approximately 0.693 V.
- 100 readings at 0.1 s intervals.
- Local/2-wire sensing.
- Compliance is NOT expected.
- No cells or external supplies.

The finite result is exported after shutdown.
Host deadlines cannot interrupt blocked VISA operations.
"""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from statistics import fmean
import time

from acquisition.monitored_acquisition import StopLimits
from commissioning.outputs_off import _new_smu
from commissioning.resistor_load import (
    DeadlineMonitoredAcquisition,
    _cleanup_preserving_original,
)
from drivers.dc_output import enable_dc_output
from drivers.smu_ranges import SMURanges
from drivers.trace_timebase import configure_trace_timebase
from recording.streaming_run import StreamingRun


SETTINGS = {
    "channel": 1,
    "current_a": 0.0001,
    "voltage_compliance_v": 1.0,
    "record_count": 100,
    "interval_s": 0.1,
    "nplc": 1.0,
    "host_timeout_s": 15.0,
    "maximum_sample_gap_s": 0.25,
    "minimum_voltage_v": 0.60,
    "maximum_voltage_v": 0.80,
    "minimum_current_a": 0.00009,
    "maximum_current_a": 0.00011,
    "nominal_resistance_ohm": 6930.0,
    "resistor_rating_w": 0.25,
}


def _prepare_current_source(smu, acquisition):
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
    checked(channel.configure_fixed_dc, "CURR")
    checked(channel.set_four_wire, False)

    ranges.set_source_autorange("CURR", True)
    ranges.set_measurement_autorange("VOLT", True)
    ranges.set_measurement_autorange("CURR", True)

    checked(
        channel.set_voltage_limit,
        SETTINGS["voltage_compliance_v"],
    )
    checked(channel.set_current, SETTINGS["current_a"])

    acquisition.configure(
        count=SETTINGS["record_count"],
        interval_s=SETTINGS["interval_s"],
        nplc=SETTINGS["nplc"],
    )

    timebase = configure_trace_timebase(smu, 1)

    evidence = {
        "source_mode": checked(channel.get_source_mode),
        "current_setpoint_a": checked(
            channel.get_current_setpoint
        ),
        "voltage_compliance_v": checked(
            channel.get_voltage_limit
        ),
        "four_wire": checked(channel.get_four_wire),
        "output_off_mode": checked(
            channel.get_output_off_mode
        ),
        "automatic_output_on": checked(
            channel.get_auto_output_on
        ),
        "source_current_autorange": (
            ranges.get_source_autorange("CURR")
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
        evidence["source_mode"] != "CURR"
        or evidence["four_wire"] is not False
        or evidence["output_off_mode"] != "HIZ"
        or evidence["automatic_output_on"] is not False
        or evidence["source_current_autorange"] is not True
        or evidence["measurement_voltage_autorange"] is not True
        or evidence["measurement_current_autorange"] is not True
    ):
        raise RuntimeError(
            "Current-source configuration readback mismatch."
        )

    for key, expected in (
        ("current_setpoint_a", SETTINGS["current_a"]),
        (
            "voltage_compliance_v",
            SETTINGS["voltage_compliance_v"],
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
            "Current-source test did not complete its "
            "finite acquisition; "
            f"stop reason: {result.stop_reason!r}."
        )

    for index, reading in enumerate(records):
        if (
            reading.channel != 1
            or reading.has_fault
            or reading.in_compliance
            or not reading.status & 1
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
                f"Record {index} failed current-source checks."
            )

    for earlier, later in zip(records, records[1:]):
        gap = later.timestamp_s - earlier.timestamp_s

        if not 0 < gap <= SETTINGS["maximum_sample_gap_s"]:
            raise RuntimeError(
                f"Unexpected current-source sample gap: {gap!r}."
            )


def run_current_source_test(smu, base_directory, run_id):
    """Return (MonitoredResult, recording_directory).

    Requires an already-connected instrument.
    Does not close the session.
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
                "commissioning_test": (
                    "resistor_positive_current_source"
                ),
                "settings": SETTINGS,
                "compliance_expected": False,
                "acceptance_policy": (
                    "broad_functional_checks_not_calibration"
                ),
            },
        )

        smu.shutdown()

        acquisition = DeadlineMonitoredAcquisition(smu, 1)
        preparation = _prepare_current_source(
            smu, acquisition
        )

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
            max_voltage_v=SETTINGS["voltage_compliance_v"],
            max_current_a=SETTINGS["current_a"],
        )

        remaining = (
            acquisition.host_deadline_s - time.monotonic()
        )

        if remaining <= 0:
            raise TimeoutError(
                "Current-source deadline reached during activation."
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

        smu.shutdown()
        run.writer.write_batch(result.readings)

        if time.monotonic() >= acquisition.host_deadline_s:
            raise TimeoutError(
                "Current-source host deadline was exceeded."
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
                        "Partial current-source data could not "
                        "be saved: "
                        f"{type(write_error).__name__}: {write_error}"
                    )

            try:
                original_error.run_directory = run.fail(
                    original_error
                )
            except BaseException as export_error:
                original_error.add_note(
                    "Current-source failure export also failed: "
                    f"{type(export_error).__name__}: {export_error}"
                )

                try:
                    run.close()
                except BaseException as close_error:
                    original_error.add_note(
                        "Current-source CSV close also failed: "
                        f"{type(close_error).__name__}: {close_error}"
                    )

        raise


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Channel-1 positive current-source test: "
            "+100 uA, 1 V voltage compliance, 100 samples. "
            "Defaults to dry run."
        )
    )
    parser.add_argument("--resource", required=True)
    parser.add_argument(
        "--base-directory",
        default="commissioning_runs",
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--resistor-only",
        action="store_true",
        help=(
            "Confirm that only the resistor and voltage-mode "
            "multimeter are connected; no cell or external supply."
        ),
    )
    parser.add_argument("--visa-library", default=None)

    arguments = parser.parse_args(argv)

    if not arguments.resource.strip():
        parser.error("--resource must not be empty.")

    if arguments.execute and not arguments.resistor_only:
        parser.error("--execute requires --resistor-only.")

    run_id = arguments.run_id or datetime.now(
        timezone.utc
    ).strftime("current_source_%Y%m%dT%H%M%S%fZ")

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

        result, directory = run_current_source_test(
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
        recorded_directory = getattr(
            primary_error,
            "run_directory",
            directory,
        )

        print(json.dumps({
            "state": "raised",
            "error_type": type(primary_error).__name__,
            "error_message": str(primary_error),
            "error_notes": list(
                getattr(primary_error, "__notes__", ())
            ),
            "run_directory": (
                str(recorded_directory)
                if recorded_directory is not None
                else None
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
        "state": "current_source_test_completed",
        "run_directory": str(Path(directory).resolve()),
        "record_count": len(result.readings),
        "mean_voltage_v": mean_voltage,
        "mean_current_a": mean_current,
        "resistance_from_mean_vi_ohm": (
            mean_voltage / mean_current
        ),
        "current_source_flagged_records": sum(
            bool(reading.status & 1)
            for reading in result.readings
        ),
        "compliance_flagged_records": sum(
            reading.in_compliance
            for reading in result.readings
        ),
        "shutdown_completed": True,
        "independent_multimeter_check_required": True,
    }, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())