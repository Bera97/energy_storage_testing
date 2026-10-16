"""Recorded completion and early-termination checks while sinking.

DPS150 OUT+ -> 6.93 kOhm resistor -> CH1 FORCE HI
DPS150 OUT- --------------------> CH1 FORCE LO

DMM across SMU terminals in DC volts.
Channel 2 disconnected. No cell/capacitor.
DPS150: 2.00 V, 10 mA limit, initially OFF.

Uses post-run export, not incremental/streaming logging.
External supply output must be controlled manually.
"""

import csv
from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
from uuid import uuid4

from diagnostics.check_dc_resistor import (
    VISA_ADDRESS,
    checked,
    configure_channel,
)
from drivers.dc_output import enable_dc_output
from drivers.keysight_b2902b import KeysightB2902B
from drivers.monitored_acquisition import (
    MonitoredAcquisition,
    StopLimits,
)
from drivers.recorded_acquisition import acquire_and_save
from drivers.run_preflight import prepare_run_destination


INTERVAL_S = 0.2
BASE_DIRECTORY = Path("runs")


def verify_export(directory, result, *, run_id, requested_count):
    """Reopen the files and compare their contents with the result."""
    if not (directory / "COMPLETE").is_file():
        raise RuntimeError("Export completion marker is missing.")

    document = json.loads(
        (directory / "metadata.json").read_text(encoding="utf-8")
    )

    with (directory / "measurements.csv").open(
        encoding="utf-8",
        newline="",
    ) as handle:
        rows = list(csv.DictReader(handle))

    if document["run_id"] != run_id:
        raise RuntimeError("Saved run ID is incorrect.")

    if len(rows) != len(result.readings):
        raise RuntimeError("CSV record count differs from result.")

    if document["record_count"] != len(result.readings):
        raise RuntimeError("Metadata record count differs from result.")

    if document["timestamp_basis"] != "raw_instrument_time":
        raise RuntimeError("Unexpected saved timestamp basis.")

    outcome = document["outcome"]

    if outcome["acquisition_exit"] != "returned":
        raise RuntimeError("Unexpected saved acquisition exit.")

    if outcome["stop_reason"] != result.stop_reason:
        raise RuntimeError("Saved stop reason differs from result.")

    if outcome["shutdown_verified"] is not True:
        raise RuntimeError("Saved shutdown verification is not True.")

    if outcome["requested_count"] != requested_count:
        raise RuntimeError("Saved requested count is incorrect.")

    if outcome["retained_count"] != len(result.readings):
        raise RuntimeError("Saved retained count is incorrect.")

    expected_fault_indices = [
        index
        for index, reading in enumerate(result.readings)
        if reading.has_fault
    ]

    if outcome["fault_record_indices"] != expected_fault_indices:
        raise RuntimeError("Saved fault indices differ from result.")

    for index, (row, reading) in enumerate(
        zip(rows, result.readings)
    ):
        if int(row["record_index"]) != index:
            raise RuntimeError("CSV record index mismatch.")

        expected_numeric_fields = {
            "instrument_timestamp_s": reading.timestamp_s,
            "voltage_v": reading.voltage_v,
            "current_a": reading.current_a,
            "signed_power_w": reading.power_w,
        }

        for field, expected_value in expected_numeric_fields.items():
            if float(row[field]) != expected_value:
                raise RuntimeError(
                    f"CSV mismatch at record {index}, field {field}."
                )

        if (
            int(row["channel"]) != reading.channel
            or int(row["status"]) != reading.status
            or row["in_compliance"] != str(reading.in_compliance)
            or row["has_fault"] != str(reading.has_fault)
        ):
            raise RuntimeError(
                f"CSV status/channel mismatch at record {index}."
            )

    trigger = result.triggering_reading
    saved_trigger = outcome["triggering_reading"]
    saved_index = outcome["triggering_record_index"]

    if trigger is None:
        if saved_trigger is not None or saved_index is not None:
            raise RuntimeError(
                "Export contains a trigger absent from the result."
            )

    else:
        if type(saved_index) is not int:
            raise RuntimeError("Saved triggering index is not an integer.")

        if not 0 <= saved_index < len(rows):
            raise RuntimeError("Saved triggering index is out of bounds.")

        if result.readings[saved_index] != trigger:
            raise RuntimeError(
                "Saved triggering index points to the wrong record."
            )

        if saved_trigger != asdict(trigger):
            raise RuntimeError(
                "Saved triggering measurement differs from result."
            )

        trigger_row = rows[saved_index]

        expected_trigger_fields = {
            "instrument_timestamp_s": trigger.timestamp_s,
            "voltage_v": trigger.voltage_v,
            "current_a": trigger.current_a,
            "signed_power_w": trigger.power_w,
        }

        for field, expected_value in expected_trigger_fields.items():
            if float(trigger_row[field]) != expected_value:
                raise RuntimeError(
                    f"Triggering CSV row mismatch: {field}."
                )

        if (
            int(trigger_row["channel"]) != trigger.channel
            or int(trigger_row["status"]) != trigger.status
        ):
            raise RuntimeError("Triggering CSV row status/channel mismatch.")

    return saved_index


def validate_operating_records(readings):
    """Broad resistor/supply functional checks, not accuracy limits."""
    for reading in readings:
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

    intervals = [
        later.timestamp_s - earlier.timestamp_s
        for earlier, later in zip(readings, readings[1:])
    ]

    if any(
        abs(interval - INTERVAL_S) > 0.03
        for interval in intervals
    ):
        raise RuntimeError("Unexpected instrument timestamp spacing.")


def run_phase(smu, *, early_stop):
    count = 100 if early_stop else 20
    phase = "early_termination" if early_stop else "completion"
    expected_reason = "voltage_min" if early_stop else "completed"

    print(f"\n=== Recorded sink: {phase} ===", flush=True)

    try:
        input(
            "\nConfirm DPS150 output is OFF. "
            "Press Enter to configure, Ctrl+C cancels: "
        )

        channel = configure_channel(smu, "CURR")
        checked(smu, channel.set_voltage_limit, 2.0)
        checked(smu, channel.set_current, -100e-6)

        acquisition = MonitoredAcquisition(smu, channel=1)
        acquisition.configure(
            count=count,
            interval_s=INTERVAL_S,
            nplc=1.0,
        )

        current = checked(smu, channel.get_current_setpoint)
        voltage_limit = checked(smu, channel.get_voltage_limit)

        if not math.isclose(
            current,
            -100e-6,
            rel_tol=1e-6,
            abs_tol=1e-12,
        ):
            raise RuntimeError("Current-setpoint readback mismatch.")

        if not math.isclose(
            voltage_limit,
            2.0,
            rel_tol=1e-6,
            abs_tol=1e-9,
        ):
            raise RuntimeError("Voltage-compliance readback mismatch.")

        limits = StopLimits(
            min_voltage_v=1.4 if early_stop else 1.15,
            max_voltage_v=1.45,
            min_current_a=-105e-6,
            max_current_a=-95e-6,
            stop_on_compliance=True,
        )

        run_id = (
            datetime.now(timezone.utc).strftime(
                f"sink_{phase}_%Y%m%dT%H%M%S_%fZ"
            )
            + "_"
            + uuid4().hex[:8]
        )

        metadata = {
            "diagnostic": "check_recorded_sink_resistor",
            "phase": phase,
            "expected_stop_reason": expected_reason,
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
            "requested_count": count,
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
            "export_mode": "post_run",
            "prepared_at_utc": datetime.now(
                timezone.utc
            ).isoformat(),
        }

        destination = prepare_run_destination(
            BASE_DIRECTORY,
            run_id,
            metadata=metadata,
        )

        print(
            f"\nSave destination checked: {destination.resolve()}\n"
            f"Requested records: {count}\n"
            f"Expected stop reason: {expected_reason}\n"
            "SMU outputs are still OFF.",
            flush=True,
        )

        input(
            "\nEnable DPS150 at 2.00 V / 10 mA.\n"
            "Confirm DMM approximately +2 V with SMU OFF.\n"
            "Leave DPS150 at 2.00 V throughout this phase.\n"
            "Do not reduce its voltage in this diagnostic.\n"
            "If incorrect, turn DPS150 OFF and Ctrl+C.\n"
            "Otherwise press Enter to enable sinking: "
        )

        metadata["enable_requested_at_utc"] = datetime.now(
            timezone.utc
        ).isoformat()

        enable_dc_output(
            smu,
            channel=1,
            max_voltage_v=2.1,
            max_current_a=0.001,
        )

        result, directory = acquire_and_save(
            acquisition,
            BASE_DIRECTORY,
            run_id,
            metadata=metadata,
            limits=limits,
            timeout_s=count * INTERVAL_S + 5.0,
            poll_interval_s=0.05,
        )

        print(
            "\nSMU shutdown verified; export completed.\n"
            "TURN DPS150 OUTPUT OFF NOW.",
            flush=True,
        )
        input("Confirm DPS150 output is OFF, then press Enter: ")

        # Verify the saved evidence before judging the phase outcome.
        saved_trigger_index = verify_export(
            directory,
            result,
            run_id=run_id,
            requested_count=count,
        )

        print(f"\nStop reason: {result.stop_reason}", flush=True)
        print(
            f"Saved records: {len(result.readings)}/{count}",
            flush=True,
        )
        print(
            f"Triggering record index: {saved_trigger_index}",
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

        if result.stop_reason != expected_reason:
            raise RuntimeError(
                f"Expected {expected_reason!r}; "
                f"received {result.stop_reason!r}."
            )

        if result.requested_count != count:
            raise RuntimeError("Unexpected result requested_count.")

        if early_stop:
            if not 1 <= len(result.readings) < count:
                raise RuntimeError("Acquisition did not terminate early.")

            trigger = result.triggering_reading

            if trigger is None:
                raise RuntimeError("Triggering record was not retained.")

            if trigger.voltage_v > limits.min_voltage_v:
                raise RuntimeError(
                    "Triggering record does not satisfy voltage_min."
                )

            if saved_trigger_index != 0:
                raise RuntimeError(
                    "Expected the stable initial operating point "
                    "to trigger at CSV record index 0."
                )

        else:
            if len(result.readings) != count:
                raise RuntimeError("Completion has wrong record count.")

            if result.triggering_reading is not None:
                raise RuntimeError(
                    "Unexpected triggering record on completion."
                )

        validate_operating_records(result.readings)

        print(
            "CSV values, metadata, and triggering-record mapping "
            "verified.\n"
            "Phase passed.",
            flush=True,
        )

    except BaseException as original_error:
        print(
            "\nSTOP: TURN DPS150 OUTPUT OFF NOW.",
            flush=True,
        )

        if hasattr(original_error, "run_directory"):
            print(
                f"Failure data exported to: "
                f"{original_error.run_directory}",
                flush=True,
            )

        try:
            smu.shutdown()
        except Exception as cleanup_error:
            original_error.add_note(
                "Additional shutdown attempt failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
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

            run_phase(smu, early_stop=False)
            run_phase(smu, early_stop=True)

            print(
                "\nRecorded completion and early-termination "
                "diagnostic passed.",
                flush=True,
            )

    finally:
        print(
            "\nLeave DPS150 output OFF. "
            "The script cannot control it.",
            flush=True,
        )


if __name__ == "__main__":
    main()