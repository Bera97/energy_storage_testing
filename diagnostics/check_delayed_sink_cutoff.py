"""Delayed sink cutoff using the shared MonitoredAcquisition interface.

DPS150 OUT+ -> 6.93 kOhm -> CH1 FORCE HI
DPS150 OUT- -------------> CH1 FORCE LO

DMM across SMU terminals in DC volts.
Channel 2 disconnected. No cell/capacitor.
DPS150 output is manually controlled.
"""

import logging
import math

from diagnostics.check_dc_resistor import (
    VISA_ADDRESS,
    checked,
    configure_channel,
)
from drivers.dc_output import enable_dc_output
from drivers.keysight_b2902b import KeysightB2902B
from acquisition.monitored_acquisition import (
    MonitoredAcquisition,
    StopLimits,
)


COUNT = 150
INTERVAL_S = 0.2
CUTOFF_V = 1.10
MIN_INITIAL_RECORDS = 10


def validate_result(result):
    if result.stop_reason != "voltage_min":
        raise RuntimeError(
            f"Expected voltage_min; received {result.stop_reason!r}."
        )

    trigger = result.triggering_reading
    if trigger is None:
        raise RuntimeError("Triggering record is missing.")

    records = result.readings

    if not MIN_INITIAL_RECORDS < len(records) < COUNT:
        raise RuntimeError(
            f"Unexpected retained count: {len(records)}/{COUNT}."
        )

    trigger_index = next(
        (
            index
            for index, reading in enumerate(records)
            if reading == trigger
        ),
        None,
    )

    if trigger_index is None:
        raise RuntimeError(
            "Triggering record is not in the retained records."
        )

    if trigger_index < MIN_INITIAL_RECORDS:
        raise RuntimeError(
            "Cutoff occurred too early to validate a delayed crossing."
        )

    for reading in records:
        if (
            reading.has_fault
            or reading.in_compliance
            or not reading.current_source
            or not 0.75 <= reading.voltage_v <= 1.50
            or not -105e-6 <= reading.current_a <= -95e-6
            or reading.power_w >= 0
        ):
            raise RuntimeError(
                f"Unexpected retained reading: {reading!r}"
            )

    if any(
        not 1.20 <= reading.voltage_v <= 1.45
        for reading in records[:MIN_INITIAL_RECORDS]
    ):
        raise RuntimeError(
            "Initial records were not in the expected starting band."
        )

    if any(
        reading.voltage_v <= CUTOFF_V
        for reading in records[:trigger_index]
    ):
        raise RuntimeError(
            "An earlier retained record already met the cutoff."
        )

    previous = records[trigger_index - 1]

    if not previous.voltage_v > CUTOFF_V >= trigger.voltage_v:
        raise RuntimeError(
            "Adjacent records do not bracket the voltage cutoff."
        )

    intervals = [
        later.timestamp_s - earlier.timestamp_s
        for earlier, later in zip(records, records[1:])
    ]

    if any(
        abs(interval - INTERVAL_S) > 0.03
        for interval in intervals
    ):
        raise RuntimeError(
            "Timestamp spacing failed the broad timing check."
        )

    return trigger_index, previous


def run_check(smu):
    try:
        channel = configure_channel(smu, "CURR")
        checked(smu, channel.set_voltage_limit, 2.0)
        checked(smu, channel.set_current, -100e-6)

        acquisition = MonitoredAcquisition(smu, channel=1)
        acquisition.configure(
            count=COUNT,
            interval_s=INTERVAL_S,
            nplc=1.0,
        )

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

        limits = StopLimits(
            min_voltage_v=CUTOFF_V,
            max_voltage_v=1.50,
            min_current_a=-105e-6,
            max_current_a=-95e-6,
            stop_on_compliance=True,
        )

        input(
            "\nEnable DPS150 at 2.00 V / 10 mA.\n"
            "Confirm DMM approximately +2 V with SMU output OFF.\n"
            "After ACQUISITION STARTING, wait at least 5 seconds,\n"
            "then reduce DPS150 to 1.60 V while leaving output ON.\n"
            "Do not increase above 2.00 V or change wiring.\n"
            "If incorrect, turn DPS150 OFF and Ctrl+C.\n"
            "Otherwise press Enter to begin: "
        )

        enable_dc_output(
            smu,
            channel=1,
            max_voltage_v=2.1,
            max_current_a=0.001,
        )

        print(
            "\nACQUISITION STARTING.\n"
            "Wait at least 5 seconds, then reduce DPS150 to 1.60 V.\n"
            "No further adjustment prompt will appear.",
            flush=True,
        )

        result = acquisition.acquire(
            limits,
            timeout_s=35.0,
            poll_interval_s=0.05,
        )

        print(
            "\nSMU shutdown verified. "
            "TURN DPS150 OUTPUT OFF NOW.",
            flush=True,
        )
        input("Confirm DPS150 output is OFF, then press Enter: ")

        print(f"\nStop reason: {result.stop_reason}", flush=True)
        print(
            f"Retained records: "
            f"{len(result.readings)}/{result.requested_count}",
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
            f"Instrument timestamps: "
            f"{[r.timestamp_s for r in result.readings]}",
            flush=True,
        )

        trigger_index, previous = validate_result(result)

        print(
            f"Above-cutoff records before trigger: {trigger_index}",
            flush=True,
        )
        print(
            f"Last above-cutoff record: {previous!r}",
            flush=True,
        )
        print(
            "Shared-interface delayed sink cutoff diagnostic passed.",
            flush=True,
        )

    except BaseException as original_error:
        print(
            "\nSTOP: TURN DPS150 OUTPUT OFF NOW.",
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
        "DMM in DC volts. Channel 2 disconnected.\n"
        "No cell/capacitor. DPS150 not connected to PC.\n"
        "DPS150 initially 2.00 V / 10 mA, output OFF.",
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