"""Intentional current-compliance commissioning on channel 1.

OUTPUT-CAPABLE only with --execute and --resistor-only.

For the existing approximately 6.93 kohm resistor only:
- Voltage-source setpoint: 1 V.
- Current compliance: 100 uA.
- Expected terminal voltage: approximately 0.693 V.
- 100 readings at 0.1 s intervals.
- Local/2-wire sensing.
- No cells or external supplies.

Compliance is expected, but measurement faults are not.

The finite result is retained in memory and exported after shutdown.
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
    SETTINGS as NORMAL_SETTINGS,
    DeadlineMonitoredAcquisition,
    _cleanup_preserving_original,
    _prepare as prepare_normal_resistor,
)
from drivers.dc_output import enable_dc_output
from recording.streaming_run import StreamingRun


SETTINGS = {
    **NORMAL_SETTINGS,
    "current_compliance_a": 0.0001,
    "minimum_voltage_v": 0.60,
    "maximum_voltage_v": 0.80,
    "minimum_current_a": 0.00009,
    "maximum_current_a": 0.00011,
}


def _prepare_compliance(smu, acquisition):
    """Configure common settings, then set 100 uA with output OFF."""
    evidence = prepare_normal_resistor(smu, acquisition)

    for channel in (smu.chan1, smu.chan2):
        state = channel.get_output_state()
        smu.check_errors()

        if state is not False:
            raise RuntimeError(
                "Both outputs must remain OFF before "
                "changing compliance."
            )

    smu.chan1.set_current_limit(
        SETTINGS["current_compliance_a"]
    )
    smu.check_errors()

    actual = smu.chan1.get_current_limit()
    smu.check_errors()

    if not math.isclose(
        actual,
        SETTINGS["current_compliance_a"],
        rel_tol=1e-6,
        abs_tol=1e-12,
    ):
        raise RuntimeError(
            "100 uA compliance did not read back correctly."
        )

    for channel in (smu.chan1, smu.chan2):
        state = channel.get_output_state()
        smu.check_errors()

        if state is not False:
            raise RuntimeError(
                "Output changed state during compliance preparation."
            )

    return {
        **evidence,
        "current_compliance_a": actual,
        "test_policy": "intentional_current_compliance",
    }


def _qualify_result(result):
    records = result.readings

    if (
        result.stop_reason != "completed"
        or result.requested_count != SETTINGS["record_count"]
        or len(records) != SETTINGS["record_count"]
    ):
        raise RuntimeError(
            "Compliance test did not complete its finite acquisition; "
            f"stop reason: {result.stop_reason!r}."
        )

    for index, reading in enumerate(records):
        if (
            reading.channel != 1
            or reading.has_fault
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
                f"Record {index} failed compliance-test checks."
            )

    compliance_count = sum(
        reading.in_compliance for reading in records
    )

    if compliance_count == 0:
        raise RuntimeError(
            "No retained record reported compliance. "
            "Inspect the measurement and status evidence."
        )

    for earlier, later in zip(records, records[1:]):
        gap = later.timestamp_s - earlier.timestamp_s

        if not 0 < gap <= SETTINGS["maximum_sample_gap_s"]:
            raise RuntimeError(
                f"Unexpected compliance-test sample gap: {gap!r}."
            )


def run_compliance_test(smu, base_directory, run_id):
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
                "commissioning_test": "resistor_current_compliance",
                "settings": SETTINGS,
                "compliance_expected": True,
                "acceptance_policy": (
                    "broad_functional_checks_not_calibration"
                ),
            },
        )

        smu.shutdown()

        acquisition = DeadlineMonitoredAcquisition(smu, 1)
        preparation = _prepare_compliance(smu, acquisition)

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
                "Compliance-test deadline reached during activation."
            )

        limits = StopLimits(
            min_voltage_v=SETTINGS["minimum_voltage_v"],
            max_voltage_v=SETTINGS["maximum_voltage_v"],
            min_current_a=SETTINGS["minimum_current_a"],
            max_current_a=SETTINGS["maximum_current_a"],
            stop_on_compliance=False,
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
                "Compliance-test host deadline was exceeded."
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
                        "Partial compliance data could not be saved: "
                        f"{type(write_error).__name__}: {write_error}"
                    )

            try:
                original_error.run_directory = run.fail(
                    original_error
                )
            except BaseException as export_error:
                original_error.add_note(
                    "Compliance failure export also failed: "
                    f"{type(export_error).__name__}: {export_error}"
                )

                try:
                    run.close()
                except BaseException as close_error:
                    original_error.add_note(
                        "Compliance CSV close also failed: "
                        f"{type(close_error).__name__}: {close_error}"
                    )

        raise


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Channel-1 intentional current-compliance test: "
            "1 V setpoint, 100 uA compliance, 100 samples. "
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
    ).strftime("compliance_%Y%m%dT%H%M%S%fZ")

    if not arguments.execute:
        print(json.dumps({
            "state": "dry_run",
            "resource": arguments.resource,
            "run_id": run_id,
            "base_directory": arguments.base_directory,
            "will_connect": False,
            "will_enable_output": False,
            "compliance_expected": True,
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

        result, directory = run_compliance_test(
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
    compliance_count = sum(
        reading.in_compliance for reading in result.readings
    )

    print(json.dumps({
        "state": "compliance_test_completed",
        "run_directory": str(Path(directory).resolve()),
        "record_count": len(result.readings),
        "mean_voltage_v": mean_voltage,
        "mean_current_a": mean_current,
        "resistance_from_mean_vi_ohm": (
            mean_voltage / mean_current
        ),
        "compliance_flagged_records": compliance_count,
        "shutdown_completed": True,
        "independent_multimeter_check_required": True,
    }, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())