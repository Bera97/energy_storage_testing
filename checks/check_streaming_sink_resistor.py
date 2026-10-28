"""Streaming early-termination check while sinking current.

DPS150 OUT+ -> 6.93 kOhm resistor -> CH1 FORCE HI
DPS150 OUT- --------------------> CH1 FORCE LO

DMM across SMU terminals in DC volts.
Channel 2 disconnected. No cell/capacitor.
DPS150: 2.00 V / 10 mA, initially OFF.

Requests 100 instrument-timed records but deliberately stops when
measured voltage is <= 1.4 V.

Creates recording files before enabling SMU output.
Writes and flushes each retrieved batch.
External supply output is manually controlled.
"""

import csv
from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
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


COUNT = 100
INTERVAL_S = 0.2
CUTOFF_V = 1.4
BASE_DIRECTORY = Path("runs")


def verify_run(run, result, run_id):
    """Compare the incremental CSV and metadata with the result."""
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
            "CSV, metadata, flushed, and retained counts differ."
        )

    if (
        document["run_id"] != run_id
        or document["export_mode"] != "incremental_csv"
        or document["timestamp_basis"] != "raw_instrument_time"
    ):
        raise RuntimeError("Unexpected run identification or format.")

    outcome = document["outcome"]

    if (
        outcome["state"] != "returned"
        or outcome["stop_reason"] != result.stop_reason
        or outcome["shutdown_verified"] is not True
        or outcome["requested_count"] != COUNT
        or outcome["retained_count"] != retained
        or outcome["records_flushed"] != retained
        or outcome["fault_record_indices"] != []
    ):
        raise RuntimeError("Unexpected final outcome metadata.")

    if document["metadata"]["stop_limits"]["min_voltage_v"] != CUTOFF_V:
        raise RuntimeError("Saved voltage cutoff is incorrect.")

    for index, (row, reading) in enumerate(
        zip(rows, result.readings)
    ):
        if int(row["record_index"]) != index:
            raise RuntimeError("CSV record index mismatch.")

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

    trigger = result.triggering_reading
    saved_index = outcome["triggering_record_index"]

    if trigger is None:
        raise RuntimeError("Result is missing its triggering record.")

    if type(saved_index) is not int:
        raise RuntimeError("Saved trigger index is not an integer.")

    if not 0 <= saved_index < retained:
        raise RuntimeError("Saved trigger index is out of bounds.")

    if result.readings[saved_index] != trigger:
        raise RuntimeError("Saved trigger index points to the wrong record.")

    if outcome["triggering_reading"] != asdict(trigger):
        raise RuntimeError("Saved triggering measurement is incorrect.")

    trigger_row = rows[saved_index]

    if (
        float(trigger_row["instrument_timestamp_s"]) != trigger.timestamp_s
        or float(trigger_row["voltage_v"]) != trigger.voltage_v
        or float(trigger_row["current_a"]) != trigger.current_a
        or int(trigger_row["status"]) != trigger.status
    ):
        raise RuntimeError("Triggering CSV row differs from the result.")

    return saved_index


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
            min_voltage_v=CUTOFF_V,
            max_voltage_v=1.45,
            min_current_a=-105e-6,
            max_current_a=-95e-6,
            stop_on_compliance=True,
        )

        run_id = (
            datetime.now(timezone.utc).strftime(
                "streaming_sink_cutoff_%Y%m%dT%H%M%S_%fZ"
            )
            + "_"
            + uuid4().hex[:8]
        )

        metadata = {
            "diagnostic": "check_streaming_sink_resistor",
            "phase": "immediate_voltage_min",
            "expected_stop_reason": "voltage_min",
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
            f"Deliberate lower-voltage cutoff: {CUTOFF_V} V\n"
            "SMU outputs are still OFF.",
            flush=True,
        )

        input(
            "\nEnable DPS150 at 2.00 V / 10 mA.\n"
            "Confirm DMM approximately +2 V with SMU output OFF.\n"
            "Leave DPS150 at 2.00 V throughout this test.\n"
            "Do not reduce its voltage.\n"
            "If incorrect, turn DPS150 OFF and Ctrl+C.\n"
            "Otherwise press Enter to enable sinking: "
        )

        enable_dc_output(
            smu,
            channel=1,
            max_voltage_v=2.1,
            max_current_a=0.001,
        )

        result, directory = acquire_streaming_and_finalize(
            acquisition,
            run,
            limits=limits,
            timeout_s=COUNT * INTERVAL_S + 5.0,
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
            f"Triggering record: {result.triggering_reading!r}",
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

        if result.stop_reason != "voltage_min":
            raise RuntimeError(
                f"Expected voltage_min; received {result.stop_reason!r}."
            )

        if not 1 <= len(result.readings) < COUNT:
            raise RuntimeError("Acquisition did not terminate early.")

        if result.requested_count != COUNT:
            raise RuntimeError("Incorrect requested count in result.")

        for reading in result.readings:
            if (
                reading.has_fault
                or reading.in_compliance
                or not reading.current_source
                or not 1.15 <= reading.voltage_v <= 1.45
                or not -105e-6 <= reading.current_a <= -95e-6
                or reading.power_w >= 0
            ):
                raise RuntimeError(
                    f"Unexpected retained reading: {reading!r}"
                )

        trigger = result.triggering_reading

        if trigger is None or trigger.voltage_v > CUTOFF_V:
            raise RuntimeError(
                "Triggering measurement does not satisfy voltage_min."
            )

        intervals = [
            later.timestamp_s - earlier.timestamp_s
            for earlier, later in zip(
                result.readings, result.readings[1:]
            )
        ]

        if any(
            abs(interval - INTERVAL_S) > 0.03
            for interval in intervals
        ):
            raise RuntimeError("Unexpected timestamp spacing.")

        saved_index = verify_run(run, result, run_id)

        if saved_index != 0:
            raise RuntimeError(
                "Expected the initial operating point "
                "to trigger at CSV record index 0."
            )

        print(
            f"Verified triggering CSV index: {saved_index}\n"
            "Incremental CSV, final metadata, and trigger mapping "
            "read back correctly.\n"
            "Streaming early-termination diagnostic passed.",
            flush=True,
        )

    except BaseException as original_error:
        print(
            "\nSTOP: TURN DPS150 OUTPUT OFF NOW.",
            flush=True,
        )

        # Instrument cleanup comes before further file operations.
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
        "Existing series-resistor sink wiring only.\n"
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