"""Validate the shared MonitoredAcquisition interface while sinking.

DPS150 OUT+ -> 6.93 kOhm resistor -> CH1 FORCE HI
DPS150 OUT- --------------------> CH1 FORCE LO

DMM across SMU terminals in DC volts.
Channel 2 disconnected. No cell/capacitor.
DPS150 output is manually controlled.
"""

import logging
import math
import statistics

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


def run_phase(smu, *, early_stop):
    count = 100 if early_stop else 20
    expected_reason = "voltage_min" if early_stop else "completed"

    print(
        "\n=== Shared monitored interface: "
        f"{'early termination' if early_stop else 'completion'} ===",
        flush=True,
    )

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
            interval_s=0.2,
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
            min_voltage_v=1.4 if early_stop else 1.15,
            max_voltage_v=1.45,
            min_current_a=-105e-6,
            max_current_a=-95e-6,
            stop_on_compliance=True,
        )

        print(
            f"\nCurrent setpoint: {current * 1e6:.6g} uA\n"
            f"Voltage compliance: {limit:.6g} V\n"
            f"Requested records: {count}\n"
            "SMU outputs are still OFF.",
            flush=True,
        )

        input(
            "\nEnable DPS150 at 2.00 V / 10 mA.\n"
            "Confirm DMM approximately +2 V with SMU OFF.\n"
            "Leave DPS150 at 2.00 V throughout this phase.\n"
            "If incorrect, turn DPS150 OFF and Ctrl+C.\n"
            "Otherwise press Enter to enable sinking: "
        )

        enable_dc_output(
            smu,
            channel=1,
            max_voltage_v=2.1,
            max_current_a=0.001,
        )

        # Shared interface owns acquisition and verified shutdown.
        result = acquisition.acquire(
            limits,
            timeout_s=count * 0.2 + 5.0,
            poll_interval_s=0.05,
        )

        print(
            "\nSMU shutdown verified. "
            "TURN DPS150 OUTPUT OFF NOW.",
            flush=True,
        )
        input("Confirm DPS150 output is OFF, then press Enter: ")

        # Report the result before checking expected test outcomes.
        print(f"\nStop reason: {result.stop_reason}", flush=True)
        print(
            f"Retained records: "
            f"{len(result.readings)}/{result.requested_count}",
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

        if result.triggering_reading is not None:
            print(
                f"Triggering record: {result.triggering_reading!r}",
                flush=True,
            )

        if result.stop_reason != expected_reason:
            raise RuntimeError(
                f"Expected {expected_reason!r}, "
                f"received {result.stop_reason!r}."
            )

        if early_stop:
            if not 1 <= len(result.readings) < count:
                raise RuntimeError("Acquisition did not stop early.")
            if result.triggering_reading is None:
                raise RuntimeError("Triggering record was not retained.")
        elif len(result.readings) != count:
            raise RuntimeError("Normal completion has wrong record count.")

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

        intervals = [
            later.timestamp_s - earlier.timestamp_s
            for earlier, later in zip(
                result.readings, result.readings[1:]
            )
        ]
        if any(abs(interval - 0.2) > 0.03 for interval in intervals):
            raise RuntimeError(
                "Timestamp spacing failed the broad timing check."
            )

        print(
            f"Mean voltage: "
            f"{statistics.mean(r.voltage_v for r in result.readings):.9g} V",
            flush=True,
        )
        print(
            f"Mean current: "
            f"{statistics.mean(r.current_a for r in result.readings) * 1e6:.9g} uA",
            flush=True,
        )
        print(
            f"Mean signed power: "
            f"{statistics.mean(r.power_w for r in result.readings) * 1e6:.9g} uW",
            flush=True,
        )
        print("Phase passed; SMU shutdown verified.", flush=True)

    except BaseException as original_error:
        print(
            "\nSTOP: TURN DPS150 OUTPUT OFF NOW.",
            flush=True,
        )

        # Also covers errors before acquire() takes ownership.
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

            errors = smu.read_errors()
            if errors:
                raise RuntimeError(
                    f"Existing errors: {errors!r}. "
                    "Stopped before configuration."
                )

            run_phase(smu, early_stop=False)
            run_phase(smu, early_stop=True)

            print(
                "\nShared-interface monitored sink diagnostic passed.",
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