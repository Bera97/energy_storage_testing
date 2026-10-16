"""Resistor-only hardware check of the monitored acquisition API."""

import logging

from diagnostics.check_dc_resistor import (
    VISA_ADDRESS,
    configure_channel,
)
from drivers.dc_output import enable_dc_output
from drivers.keysight_b2902b import KeysightB2902B
from drivers.monitored_acquisition import (
    MonitoredAcquisition,
    StopLimits,
)


def run_phase(smu, *, early_stop):
    count = 100 if early_stop else 20
    label = "early termination" if early_stop else "completion"

    print(f"\n=== Monitored {label} ===", flush=True)
    configure_channel(smu, "VOLT")

    acquisition = MonitoredAcquisition(smu, channel=1)
    acquisition.configure(count=count, interval_s=0.2, nplc=1.0)

    input(
        "\nConfirm resistor/DMM wiring; no external supply or cell. "
        "Press Enter to enable channel 1, Ctrl+C to cancel: "
    )

    try:
        enable_dc_output(
            smu,
            channel=1,
            max_voltage_v=1.2,
            max_current_a=0.001,
        )

        limits = (
            StopLimits(max_voltage_v=0.9)
            if early_stop
            else StopLimits(
                min_voltage_v=0.95,
                max_voltage_v=1.05,
                min_current_a=135e-6,
                max_current_a=155e-6,
            )
        )

        result = acquisition.acquire(limits)

        print(f"Stop reason: {result.stop_reason}", flush=True)
        print(
            f"Retained records: {len(result.readings)}/{count}",
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

        if early_stop:
            if result.stop_reason != "voltage_max":
                raise RuntimeError("Expected voltage_max termination.")

            if not 1 <= len(result.readings) < count:
                raise RuntimeError("Early termination was not established.")

            print(
                f"Triggering record: {result.triggering_reading}",
                flush=True,
            )

        elif (
            result.stop_reason != "completed"
            or len(result.readings) != count
        ):
            raise RuntimeError("Normal completion failed.")

        if result.fault_readings:
            raise RuntimeError("Unexpected measurement fault records.")

        print("Phase passed; shutdown verified.", flush=True)

    except BaseException as original_error:
        try:
            smu.shutdown()
        except Exception as cleanup_error:
            original_error.add_note(
                "Diagnostic shutdown could not be verified: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
        raise


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    print(
        "Channel 1: 6.93 kOhm resistor, DMM across it in DC volts.\n"
        "Channel 2 disconnected. FNIRSI disconnected. No cell/capacitor.",
        flush=True,
    )
    input("Confirm both outputs OFF. Enter to connect, Ctrl+C cancels: ")

    with KeysightB2902B(VISA_ADDRESS) as smu:
        print(f"\nInstrument: {smu.identity}", flush=True)

        errors = smu.read_errors()
        if errors:
            raise RuntimeError(
                f"Existing errors; stopped before configuration: {errors}"
            )

        run_phase(smu, early_stop=False)
        run_phase(smu, early_stop=True)

        print("\nMonitored resistor diagnostic passed.", flush=True)


if __name__ == "__main__":
    main()