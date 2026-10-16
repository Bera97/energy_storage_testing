"""Instrument-timed sinking and lower-voltage termination check.

DPS150 OUT+ -> 6.93 kOhm resistor -> CH1 FORCE HI
DPS150 OUT- --------------------> CH1 FORCE LO

DMM across CH1 terminals in DC volts.
Channel 2 disconnected. No cell/capacitor.
DPS150 output must be controlled manually.
"""

import logging
import math
import statistics
import time

from diagnostics.check_dc_resistor import (
    VISA_ADDRESS,
    checked,
    configure_channel,
)
from diagnostics.check_live_buffer_resistor import (
    configure_trace,
    query_integer,
    retrieve_new_records,
    write_checked,
)
from drivers.buffered_acquisition import (
    BufferedAcquisition,
    BufferedStatusError,
)
from drivers.dc_output import enable_dc_output
from drivers.keysight_b2902b import KeysightB2902B


INTERVAL_S = 0.2
POLL_S = 0.05
CURRENT_A = -100e-6
VOLTAGE_LIMIT_V = 2.0
TEST_VOLTAGE_MIN_V = 1.4


def retrieve_retaining_faults(smu, readings, count):
    try:
        return retrieve_new_records(smu, readings, count)
    except BufferedStatusError as error:
        # parse_buffer() retains the fault-containing batch on
        # this exception, but the retrieval helper has not appended it.
        readings.extend(error.readings)
        raise


def check_operating_point(reading):
    if (
        reading.has_fault
        or reading.in_compliance
        or not reading.current_source
        or not 1.15 <= reading.voltage_v <= 1.45
        or not -105e-6 <= reading.current_a <= -95e-6
        or reading.power_w >= 0
    ):
        raise RuntimeError(
            f"Unexpected sink operating point: {reading!r}; "
            f"power={reading.power_w} W."
        )


def run_phase(smu, *, early_stop):
    count = 100 if early_stop else 20
    label = (
        "Monitored lower-voltage termination"
        if early_stop
        else "Monitored sink completion"
    )

    readings = []
    triggering_reading = None
    reason = None

    print(f"\n=== {label} ===", flush=True)

    try:
        input(
            "\nConfirm DPS150 output is OFF before configuration. "
            "Press Enter, or Ctrl+C to cancel: "
        )

        channel = configure_channel(smu, "CURR")
        checked(smu, channel.set_voltage_limit, VOLTAGE_LIMIT_V)
        checked(smu, channel.set_current, CURRENT_A)

        acquisition = BufferedAcquisition(smu, channel=1)
        acquisition.configure(
            count=count,
            interval_s=INTERVAL_S,
            nplc=1.0,
        )
        configure_trace(smu, count)

        current = checked(smu, channel.get_current_setpoint)
        limit = checked(smu, channel.get_voltage_limit)

        if not math.isclose(
            current, CURRENT_A, rel_tol=1e-6, abs_tol=1e-12
        ):
            raise RuntimeError(
                f"Current-setpoint mismatch: {current}"
            )

        if not math.isclose(
            limit, VOLTAGE_LIMIT_V, rel_tol=1e-6, abs_tol=1e-9
        ):
            raise RuntimeError(
                f"Voltage-compliance mismatch: {limit}"
            )

        print(
            f"\nCurrent setpoint: {current * 1e6:.6g} uA\n"
            f"Voltage compliance: {limit:.6g} V\n"
            f"Requested records: {count}\n"
            f"Instrument interval: {INTERVAL_S} s\n"
            "SMU outputs are still OFF.",
            flush=True,
        )

        input(
            "\nEnable DPS150 at 2.00 V / 10 mA.\n"
            "Confirm DMM shows approximately +2 V with SMU OFF.\n"
            "If incorrect, turn DPS150 OFF and Ctrl+C.\n"
            "Otherwise press Enter to enable sinking: "
        )

        enable_dc_output(
            smu,
            channel=1,
            max_voltage_v=2.1,
            max_current_a=0.001,
        )

        started = time.monotonic()
        deadline = started + count * INTERVAL_S + 5.0
        write_checked(smu, ":INIT:ACQ (@1)")

        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "Monitored sink acquisition timed out."
                )

            operation = query_integer(smu, ":STAT:OPER:COND?")
            idle = bool(operation & (1 << 4))

            questionable = query_integer(
                smu, ":STAT:QUES:COND?"
            )
            if questionable & (1 << 12):
                raise RuntimeError(
                    "Lost acquisition event reported."
                )

            new_readings = retrieve_retaining_faults(
                smu, readings, count
            )

            # Retain the entire retrieved batch, then evaluate it.
            # Fault/unexpected-operation checks take precedence.
            for reading in new_readings:
                check_operating_point(reading)

            if early_stop:
                for reading in new_readings:
                    if reading.voltage_v <= TEST_VOLTAGE_MIN_V:
                        triggering_reading = reading
                        reason = "voltage_min"
                        break

            if reason is not None:
                break

            if idle:
                reason = "completed"
                break

            time.sleep(POLL_S)

        decision_elapsed = time.monotonic() - started

        # Stop output before final retrieval or console reporting.
        smu.shutdown()
        shutdown_elapsed = time.monotonic() - started

        print(
            "\nSMU shutdown verified. "
            "TURN DPS150 OUTPUT OFF NOW.",
            flush=True,
        )
        input("Confirm DPS150 output is OFF, then press Enter: ")

        write_checked(smu, ":TRAC1:FEED:CONT NEV")
        retrieve_retaining_faults(smu, readings, count)

        for reading in readings:
            check_operating_point(reading)

        if early_stop:
            if reason != "voltage_min":
                raise RuntimeError(
                    "Lower-voltage termination did not occur."
                )
            if not 1 <= len(readings) < count:
                raise RuntimeError(
                    "Acquisition did not terminate early."
                )
        else:
            if reason != "completed" or len(readings) != count:
                raise RuntimeError(
                    f"Incomplete acquisition: {len(readings)}/{count}"
                )

        intervals = [
            later.timestamp_s - earlier.timestamp_s
            for earlier, later in zip(readings, readings[1:])
        ]

        if any(
            abs(interval - INTERVAL_S) > 0.03
            for interval in intervals
        ):
            raise RuntimeError(
                "Timestamp spacing failed the broad timing check."
            )

        print(f"\nStop reason: {reason}", flush=True)
        print(
            f"Retained records: {len(readings)}/{count}",
            flush=True,
        )
        print(
            f"Mean voltage: "
            f"{statistics.mean(r.voltage_v for r in readings):.9g} V",
            flush=True,
        )
        print(
            f"Mean current: "
            f"{statistics.mean(r.current_a for r in readings) * 1e6:.9g} uA",
            flush=True,
        )
        print(
            f"Mean signed power: "
            f"{statistics.mean(r.power_w for r in readings) * 1e6:.9g} uW",
            flush=True,
        )
        print(
            f"PC time to decision: {decision_elapsed:.6f} s",
            flush=True,
        )
        print(
            f"PC time through verified shutdown: "
            f"{shutdown_elapsed:.6f} s",
            flush=True,
        )
        print(
            f"Instrument timestamps: "
            f"{[r.timestamp_s for r in readings]}",
            flush=True,
        )

        if triggering_reading is not None:
            print(
                f"Triggering record: {triggering_reading!r}",
                flush=True,
            )

        print("Phase passed; SMU shutdown verified.", flush=True)

    except BaseException as original_error:
        original_error.partial_readings = tuple(readings)

        print(
            "\nSTOP: TURN DPS150 OUTPUT OFF NOW.",
            flush=True,
        )

        try:
            smu.shutdown()
        except Exception as cleanup_error:
            original_error.add_note(
                "SMU shutdown could not be verified: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )

        raise


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    print(
        "DPS150 OUT+ -> 6.93 kOhm -> CH1 FORCE HI\n"
        "DPS150 OUT- -> CH1 FORCE LO\n"
        "DMM across SMU terminals in DC volts.\n"
        "Channel 2 disconnected. No cell/capacitor.\n"
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

            existing_errors = smu.read_errors()
            if existing_errors:
                raise RuntimeError(
                    f"Existing errors: {existing_errors!r}. "
                    "Stopped before configuration."
                )

            run_phase(smu, early_stop=False)
            run_phase(smu, early_stop=True)

            print(
                "\nMonitored sink resistor diagnostic passed.",
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