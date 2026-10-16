"""Supervised delayed lower-voltage crossing with a resistor.

DPS150 OUT+ -> 6.93 kOhm -> CH1 FORCE HI
DPS150 OUT- -------------> CH1 FORCE LO

DMM across SMU terminals in DC volts.
Channel 2 disconnected. No cell/capacitor.
DPS150 is manually controlled.
"""

import logging
import math
import time

from diagnostics.check_dc_resistor import (
    VISA_ADDRESS,
    checked,
    configure_channel,
)
from diagnostics.check_live_buffer_resistor import (
    configure_trace,
    query_integer,
    write_checked,
)
from diagnostics.check_monitored_sink_resistor import (
    retrieve_retaining_faults,
)
from drivers.buffered_acquisition import BufferedAcquisition
from drivers.dc_output import enable_dc_output
from drivers.keysight_b2902b import KeysightB2902B


COUNT = 150
INTERVAL_S = 0.2
CUTOFF_V = 1.10
MIN_INITIAL_RECORDS = 10


def check_reading(reading):
    if (
        reading.has_fault
        or reading.in_compliance
        or not reading.current_source
        or not 0.75 <= reading.voltage_v <= 1.50
        or not -105e-6 <= reading.current_a <= -95e-6
        or reading.power_w >= 0
    ):
        raise RuntimeError(
            f"Unexpected operating point: {reading!r}"
        )


def run_check(smu):
    readings = []
    initial_count = 0
    adjustment_announced = False
    previous = None
    before_crossing = None
    triggering_reading = None

    try:
        channel = configure_channel(smu, "CURR")
        checked(smu, channel.set_voltage_limit, 2.0)
        checked(smu, channel.set_current, -100e-6)

        acquisition = BufferedAcquisition(smu, channel=1)
        acquisition.configure(
            count=COUNT,
            interval_s=INTERVAL_S,
            nplc=1.0,
        )
        configure_trace(smu, COUNT)

        current = checked(smu, channel.get_current_setpoint)
        limit = checked(smu, channel.get_voltage_limit)

        if not math.isclose(
            current, -100e-6, rel_tol=1e-6, abs_tol=1e-12
        ):
            raise RuntimeError(
                f"Current-setpoint mismatch: {current}"
            )

        if not math.isclose(
            limit, 2.0, rel_tol=1e-6, abs_tol=1e-9
        ):
            raise RuntimeError(
                f"Voltage-compliance mismatch: {limit}"
            )

        input(
            "\nEnable DPS150 at 2.00 V / 10 mA.\n"
            "Confirm DMM approximately +2 V with SMU output OFF.\n"
            "Do not adjust voltage until instructed.\n"
            "If incorrect, turn DPS150 OFF and Ctrl+C.\n"
            "Otherwise press Enter to begin sinking: "
        )

        enable_dc_output(
            smu,
            channel=1,
            max_voltage_v=2.1,
            max_current_a=0.001,
        )

        started = time.monotonic()
        deadline = started + 35.0
        write_checked(smu, ":INIT:ACQ (@1)")

        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "No valid delayed crossing within the time budget."
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
                smu, readings, COUNT
            )

            for reading in new_readings:
                check_reading(reading)

                if not adjustment_announced:
                    if not 1.20 <= reading.voltage_v <= 1.45:
                        raise RuntimeError(
                            "Initial voltage was not in the expected "
                            "above-cutoff band. Do not adjust early."
                        )

                    initial_count += 1

                elif reading.voltage_v <= CUTOFF_V:
                    if (
                        previous is None
                        or previous.voltage_v <= CUTOFF_V
                    ):
                        raise RuntimeError(
                            "No preceding above-cutoff record."
                        )

                    before_crossing = previous
                    triggering_reading = reading
                    break

                previous = reading

            if triggering_reading is not None:
                break

            if (
                not adjustment_announced
                and initial_count >= MIN_INITIAL_RECORDS
            ):
                adjustment_announced = True
                print(
                    "\nNOW REDUCE DPS150 from 2.00 V to 1.60 V.\n"
                    "Leave its output ON and current limit unchanged.\n"
                    "Do not change wiring. Acquisition continues.",
                    flush=True,
                )

            if idle:
                raise RuntimeError(
                    "Acquisition completed without a valid crossing. "
                    "Turn DPS150 OFF; do not extend the run manually."
                )

            time.sleep(0.05)

        decision_elapsed = time.monotonic() - started

        smu.shutdown()
        shutdown_elapsed = time.monotonic() - started

        print(
            "\nSMU shutdown verified. "
            "TURN DPS150 OUTPUT OFF NOW.",
            flush=True,
        )
        input("Confirm DPS150 output is OFF, then press Enter: ")

        write_checked(smu, ":TRAC1:FEED:CONT NEV")
        retrieve_retaining_faults(smu, readings, COUNT)

        for reading in readings:
            check_reading(reading)

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

        if len(readings) >= COUNT:
            raise RuntimeError(
                "Crossing detected, but acquisition did not stop early."
            )

        print("\nStop reason: voltage_min", flush=True)
        print(
            f"Initial above-cutoff records before adjustment prompt: "
            f"{initial_count}",
            flush=True,
        )
        print(
            f"Retained records: {len(readings)}/{COUNT}",
            flush=True,
        )
        print(
            f"Last above-cutoff record: {before_crossing!r}",
            flush=True,
        )
        print(
            f"First at/below-cutoff record: {triggering_reading!r}",
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
        print(
            "Delayed sink cutoff diagnostic passed.",
            flush=True,
        )

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
        "Existing series-resistor sink wiring only.\n"
        "DMM in DC volts. Channel 2 disconnected.\n"
        "No cell/capacitor. DPS150 not connected to PC.\n"
        "DPS150 initially 2.00 V / 10 mA, output OFF.\n"
        "When instructed, reduce output setting to 1.60 V.",
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