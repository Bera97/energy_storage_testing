"""Supervised, approximately three-minute streaming sink check.

DPS150 OUT+ -> 6.93 kOhm resistor -> CH1 FORCE HI
DPS150 OUT- --------------------> CH1 FORCE LO

DMM across SMU terminals in DC volts.
Channel 2 disconnected. No cell/capacitor.
DPS150: 2.00 V / 10 mA, initially OFF.

One finite acquisition:
    900 records
    0.2-second instrument interval
    179.8-second first-to-last nominal sample span

No buffer rearming. No external-supply automation.
CSV is written and flushed after each retrieved nonempty batch.
"""

import csv
from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import statistics
from uuid import uuid4

from checks.check_dc_resistor import (
    VISA_ADDRESS,
    checked,
    configure_channel,
)
from drivers.dc_output import enable_dc_output
from drivers.keysight_b2902b import KeysightB2902B
from acquisition.monitored_acquisition import StopLimits
from acquisition.streaming_acquisition import (
    StreamingMonitoredAcquisition,
)
from recording.recorded_acquisition import (
    acquire_streaming_and_finalize,
)
from recording.streaming_run import StreamingRun


COUNT = 900
INTERVAL_S = 0.2
TIMEOUT_S = 195.0
TIMING_TOLERANCE_S = 0.03
BASE_DIRECTORY = Path("runs")


def verify_export(run, result, run_id):
    """Check finalized files against the returned acquisition result."""
    if not run.finalized:
        raise RuntimeError("Run was not finalized.")

    if not run.writer.closed:
        raise RuntimeError("CSV writer is still open.")

    if run.writer.failed:
        raise RuntimeError("CSV writer reports a failure.")

    if not (run.directory / "COMPLETE").is_file():
        raise RuntimeError("COMPLETE marker is missing.")

    document = json.loads(
        (run.directory / "metadata.json").read_text(
            encoding="utf-8"
        )
    )

    with (run.directory / "measurements.csv").open(
        encoding="utf-8",
        newline="",
    ) as handle:
        rows = list(csv.DictReader(handle))

    retained = len(result.readings)

    if (
        len(rows) != retained
        or document["record_count"] != retained
        or run.writer.records_flushed != retained
    ):
        raise RuntimeError(
            "CSV, metadata, retained, and flushed counts differ."
        )

    if (
        document["run_id"] != run_id
        or document["export_mode"] != "incremental_csv"
        or document["timestamp_basis"] != "raw_instrument_time"
    ):
        raise RuntimeError("Unexpected saved run identification/format.")

    metadata = document["metadata"]

    if (
        metadata["requested_count"] != COUNT
        or metadata["interval_s"] != INTERVAL_S
        or metadata["stop_limits"]["min_voltage_v"] != 1.15
        or metadata["stop_limits"]["max_voltage_v"] != 1.45
    ):
        raise RuntimeError("Saved acquisition configuration is incorrect.")

    outcome = document["outcome"]

    if (
        outcome["state"] != "returned"
        or outcome["stop_reason"] != result.stop_reason
        or outcome["shutdown_verified"] is not True
        or outcome["requested_count"] != COUNT
        or outcome["retained_count"] != retained
        or outcome["records_flushed"] != retained
        or outcome["triggering_record_index"] is not None
        or outcome["triggering_reading"] is not None
        or outcome["fault_record_indices"] != []
    ):
        raise RuntimeError("Unexpected saved outcome metadata.")

    for index, (row, reading) in enumerate(
        zip(rows, result.readings)
    ):
        if int(row["record_index"]) != index:
            raise RuntimeError(
                f"CSV record index mismatch at row {index}."
            )

        expected_numeric = {
            "instrument_timestamp_s": reading.timestamp_s,
            "voltage_v": reading.voltage_v,
            "current_a": reading.current_a,
            "signed_power_w": reading.power_w,
        }

        for field, expected_value in expected_numeric.items():
            if float(row[field]) != expected_value:
                raise RuntimeError(
                    f"CSV mismatch at record {index}: {field}."
                )

        if (
            int(row["channel"]) != reading.channel
            or int(row["status"]) != reading.status
            or row["in_compliance"] != str(reading.in_compliance)
            or row["has_fault"] != str(reading.has_fault)
        ):
            raise RuntimeError(
                f"CSV channel/status mismatch at record {index}."
            )


def validate_measurements(result):
    """Apply broad functional checks to the completed run."""
    if result.stop_reason != "completed":
        raise RuntimeError(
            f"Expected completed; received {result.stop_reason!r}."
        )

    if (
        result.requested_count != COUNT
        or len(result.readings) != COUNT
    ):
        raise RuntimeError("Unexpected acquisition record count.")

    if result.triggering_reading is not None:
        raise RuntimeError("Unexpected triggering record.")

    for index, reading in enumerate(result.readings):
        if (
            reading.channel != 1
            or reading.has_fault
            or reading.in_compliance
            or not reading.current_source
            or not 1.15 <= reading.voltage_v <= 1.45
            or not -105e-6 <= reading.current_a <= -95e-6
            or reading.power_w >= 0
        ):
            raise RuntimeError(
                f"Unexpected record {index}: {reading!r}"
            )

    intervals = [
        later.timestamp_s - earlier.timestamp_s
        for earlier, later in zip(
            result.readings, result.readings[1:]
        )
    ]

    if any(
        abs(interval - INTERVAL_S) > TIMING_TOLERANCE_S
        for interval in intervals
    ):
        raise RuntimeError(
            "Timestamp spacing failed the broad timing check."
        )

    actual_span = (
        result.readings[-1].timestamp_s
        - result.readings[0].timestamp_s
    )
    expected_span = (COUNT - 1) * INTERVAL_S

    if abs(actual_span - expected_span) > TIMING_TOLERANCE_S:
        raise RuntimeError(
            f"Unexpected timestamp span: {actual_span} s; "
            f"expected approximately {expected_span} s."
        )

    return intervals, actual_span


def run_check(smu):
    run = None

    try:
        channel = configure_channel(smu, "CURR")
        checked(smu, channel.set_voltage_limit, 2.0)
        checked(smu, channel.set_current, -100e-6)

        current = checked(smu, channel.get_current_setpoint)
        voltage_limit = checked(smu, channel.get_voltage_limit)

        if not math.isclose(
            current, -100e-6, rel_tol=1e-6, abs_tol=1e-12
        ):
            raise RuntimeError("Current-setpoint readback mismatch.")

        if not math.isclose(
            voltage_limit, 2.0, rel_tol=1e-6, abs_tol=1e-9
        ):
            raise RuntimeError("Voltage-compliance readback mismatch.")

        limits = StopLimits(
            min_voltage_v=1.15,
            max_voltage_v=1.45,
            min_current_a=-105e-6,
            max_current_a=-95e-6,
            stop_on_compliance=True,
        )

        run_id = (
            datetime.now(timezone.utc).strftime(
                "streaming_sink_duration_%Y%m%dT%H%M%S_%fZ"
            )
            + "_"
            + uuid4().hex[:8]
        )

        metadata = {
            "diagnostic": "check_streaming_sink_duration",
            "expected_stop_reason": "completed",
            "instrument_identity": smu.identity,
            "visa_address": VISA_ADDRESS,
            "channel": 1,
            "source_function": checked(
                smu, channel.get_source_mode
            ),
            "current_setpoint_a": current,
            "voltage_compliance_v": voltage_limit,
            "nplc": checked(smu, channel.get_nplc),
            "four_wire": checked(smu, channel.get_four_wire),
            "requested_count": COUNT,
            "interval_s": INTERVAL_S,
            "nominal_first_to_last_span_s": (
                (COUNT - 1) * INTERVAL_S
            ),
            "acquisition_timeout_s": TIMEOUT_S,
            "stop_limits": asdict(limits),
            "nominal_series_resistance_ohm": 6930.0,
            "external_supply": {
                "model": "FNIRSI DPS150",
                "nominal_voltage_v": 2.0,
                "current_limit_a": 0.010,
                "control": "manual",
            },
            "dut": "series resistor and external supply; no storage cell",
            "flush_policy": "after header and each retrieved nonempty batch",
            "supervised": True,
            "buffer_rearming": False,
        }

        run = StreamingRun(
            BASE_DIRECTORY,
            run_id,
            metadata=metadata,
        )

        acquisition = StreamingMonitoredAcquisition(
            smu,
            channel=1,
            writer=run.writer,
        )
        acquisition.configure(
            count=COUNT,
            interval_s=INTERVAL_S,
            nplc=1.0,
        )

        if acquisition.writer is not run.writer:
            raise RuntimeError("Acquisition has the wrong CSV writer.")

        if (
            run.writer.closed
            or run.writer.failed
            or run.writer.records_flushed != 0
        ):
            raise RuntimeError("CSV writer is not fresh and usable.")

        print(
            f"\nRun directory created: {run.directory.resolve()}\n"
            "Initial metadata saved; CSV header written and flushed.\n"
            f"Requested records: {COUNT}\n"
            f"Instrument interval: {INTERVAL_S} s\n"
            "Approximate acquisition duration: 3 minutes.\n"
            "SMU outputs are still OFF.",
            flush=True,
        )

        input(
            "\nEnable DPS150 at 2.00 V / 10 mA.\n"
            "Confirm DMM approximately +2 V with SMU output OFF.\n"
            "Leave DPS150 at 2.00 V throughout the run.\n"
            "Remain at the setup; no progress messages during acquisition.\n"
            "If incorrect, turn DPS150 OFF and Ctrl+C.\n"
            "Otherwise press Enter to enable sinking: "
        )

        enable_dc_output(
            smu,
            channel=1,
            max_voltage_v=2.1,
            max_current_a=0.001,
        )

        print(
            "\nACQUISITION STARTING: approximately 3 minutes.\n"
            "Do not adjust the supply or wiring.\n"
            "To cancel: Ctrl+C and turn DPS150 output OFF.",
            flush=True,
        )

        result, directory = acquire_streaming_and_finalize(
            acquisition,
            run,
            limits=limits,
            timeout_s=TIMEOUT_S,
            poll_interval_s=0.05,
        )

        print(
            "\nSMU shutdown verified; streaming files finalized.\n"
            "TURN DPS150 OUTPUT OFF NOW.",
            flush=True,
        )
        input("Confirm DPS150 output is OFF, then press Enter: ")

        print(f"\nStop reason: {result.stop_reason}", flush=True)
        print(
            f"Retained records: {len(result.readings)}/{COUNT}",
            flush=True,
        )
        print(
            f"Records written and flushed: "
            f"{run.writer.records_flushed}",
            flush=True,
        )
        print(
            f"PC time to decision: "
            f"{result.decision_elapsed_s:.6f} s",
            flush=True,
        )
        print(
            f"PC time through verified shutdown: "
            f"{result.shutdown_elapsed_s:.6f} s",
            flush=True,
        )
        print(
            f"Run directory: {directory.resolve()}",
            flush=True,
        )

        intervals, actual_span = validate_measurements(result)
        verify_export(run, result, run_id)

        voltages = [r.voltage_v for r in result.readings]
        currents_ua = [
            r.current_a * 1e6 for r in result.readings
        ]

        print(
            f"Instrument first-to-last span: {actual_span:.9g} s",
            flush=True,
        )
        print(
            "Interval min/mean/max: "
            f"{min(intervals):.9g} / "
            f"{statistics.mean(intervals):.9g} / "
            f"{max(intervals):.9g} s",
            flush=True,
        )
        print(
            "Voltage min/mean/max: "
            f"{min(voltages):.9g} / "
            f"{statistics.mean(voltages):.9g} / "
            f"{max(voltages):.9g} V",
            flush=True,
        )
        print(
            "Current min/mean/max: "
            f"{min(currents_ua):.9g} / "
            f"{statistics.mean(currents_ua):.9g} / "
            f"{max(currents_ua):.9g} uA",
            flush=True,
        )
        print(
            f"Observed statuses: "
            f"{sorted({r.status for r in result.readings})}",
            flush=True,
        )
        print(
            "All CSV values and final metadata read back correctly.\n"
            "Streaming sink duration diagnostic passed.",
            flush=True,
        )

    except BaseException as original_error:
        print(
            "\nSTOP: TURN DPS150 OUTPUT OFF NOW.",
            flush=True,
        )

        # Instrument cleanup before additional file operations.
        try:
            smu.shutdown()
        except Exception as cleanup_error:
            original_error.add_note(
                "Additional SMU shutdown attempt failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )

        if run is not None:
            print(
                f"Run files: {run.directory.resolve()}",
                flush=True,
            )

            if not run.finalized:
                try:
                    run.fail(original_error)
                except Exception as metadata_error:
                    original_error.add_note(
                        "Additional failure-metadata save failed: "
                        f"{type(metadata_error).__name__}: {metadata_error}"
                    )

            try:
                run.close()
            except Exception as close_error:
                original_error.add_note(
                    "Additional CSV close failed: "
                    f"{type(close_error).__name__}: {close_error}"
                )

        raise


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    print(
        "Supervised approximately three-minute sink test.\n"
        "Existing series-resistor wiring only.\n"
        "DPS150 OUT+ -> 6.93 kOhm -> CH1 FORCE HI\n"
        "DPS150 OUT- -> CH1 FORCE LO\n"
        "DMM across SMU terminals in DC volts.\n"
        "Channel 2 disconnected. No cell/capacitor.\n"
        "DPS150 not connected to PC.\n"
        "DPS150: 2.00 V / 10 mA, output OFF.",
        flush=True,
    )

    try:
        input(
            "\nConfirm DPS150 and both SMU outputs are OFF. "
            "Enter to connect, Ctrl+C cancels: "
        )

        with KeysightB2902B(VISA_ADDRESS) as smu:
            print(f"\nInstrument: {smu.identity}", flush=True)

            errors = smu.read_errors()

            if errors:
                raise RuntimeError(
                    f"Existing errors: {errors!r}. "
                    "Stopped before configuration."
                )

            run_check(smu)

    finally:
        print(
            "\nLeave DPS150 output OFF. "
            "The script cannot control it.",
            flush=True,
        )


if __name__ == "__main__":
    main()