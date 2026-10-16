"""Hardware check of finite timer acquisition using a resistor.

Channel 1: 6.93 kOhm resistor, DMM across it in DC-voltage mode.
Channel 2: disconnected.
No battery or capacitor connected.

Changes instrument configuration and does not restore prior settings.
"""

import logging
import math
import statistics
import time

from diagnostics.check_dc_resistor import (
    configure_channel,
    RESISTANCE_OHM,
    VISA_ADDRESS,
)
from drivers.buffered_acquisition import BufferedAcquisition
from drivers.dc_output import enable_dc_output
from drivers.keysight_b2902b import KeysightB2902B


RECORD_COUNT = 20
INTERVAL_S = 0.2

# Broad functional timing screen, not an instrument specification.
TIMING_TOLERANCE_S = 0.03


def verify_records(readings, source_function):
    if len(readings) != RECORD_COUNT:
        raise RuntimeError(
            f"Expected {RECORD_COUNT} records, received {len(readings)}."
        )

    if source_function == "VOLT":
        expected_voltage = 1.0
        expected_current = expected_voltage / RESISTANCE_OHM
    else:
        expected_current = 100e-6
        expected_voltage = expected_current * RESISTANCE_OHM

    for index, reading in enumerate(readings):
        if reading.current_source != (source_function == "CURR"):
            raise RuntimeError(
                f"Record {index}: source-mode status does not match "
                f"{source_function}."
            )

        if reading.in_compliance:
            raise RuntimeError(
                f"Record {index}: unexpected compliance condition."
            )

        if not math.isclose(
            reading.voltage_v,
            expected_voltage,
            rel_tol=0.05,
            abs_tol=1e-6,
        ):
            raise RuntimeError(
                f"Record {index}: unexpected voltage "
                f"{reading.voltage_v} V."
            )

        if not math.isclose(
            reading.current_a,
            expected_current,
            rel_tol=0.05,
            abs_tol=1e-9,
        ):
            raise RuntimeError(
                f"Record {index}: unexpected current "
                f"{reading.current_a} A."
            )

    intervals = [
        later.timestamp_s - earlier.timestamp_s
        for earlier, later in zip(readings, readings[1:])
    ]

    actual_span = (
        readings[-1].timestamp_s - readings[0].timestamp_s
    )
    expected_span = (RECORD_COUNT - 1) * INTERVAL_S

    print("\nSummary:", flush=True)
    print(f"  Returned records: {len(readings)}", flush=True)
    print(
        f"  Mean voltage: "
        f"{statistics.mean(item.voltage_v for item in readings):.9g} V",
        flush=True,
    )
    print(
        f"  Mean current: "
        f"{statistics.mean(item.current_a for item in readings) * 1e6:.9g} uA",
        flush=True,
    )
    print(
        f"  Observed statuses: "
        f"{sorted({item.status for item in readings})}",
        flush=True,
    )
    print(f"  Requested interval: {INTERVAL_S:.9g} s", flush=True)
    print(
        f"  Returned interval min/mean/max: "
        f"{min(intervals):.9g} / "
        f"{statistics.mean(intervals):.9g} / "
        f"{max(intervals):.9g} s",
        flush=True,
    )
    print(
        f"  First-to-last span: {actual_span:.9g} s "
        f"(nominal {expected_span:.9g} s)",
        flush=True,
    )
    print(
        f"  Individual timestamp differences: {intervals}",
        flush=True,
    )

    if any(
        abs(interval - INTERVAL_S) > TIMING_TOLERANCE_S
        for interval in intervals
    ):
        raise RuntimeError(
            "Returned timestamp spacing failed the diagnostic's "
            "broad functional timing check."
        )

    if abs(actual_span - expected_span) > TIMING_TOLERANCE_S:
        raise RuntimeError(
            "First-to-last timestamp span failed the diagnostic's "
            "broad functional timing check."
        )


def run_phase(smu, source_function):
    print(
        f"\n=== Buffered acquisition: {source_function} source ===",
        flush=True,
    )

    configure_channel(smu, source_function)

    acquisition = BufferedAcquisition(smu, channel=1)
    acquisition.configure(
        count=RECORD_COUNT,
        interval_s=INTERVAL_S,
        nplc=1.0,
    )

    input(
        "\nConfirm resistor/DMM wiring. Press Enter to enable "
        "channel 1, or Ctrl+C to cancel: "
    )

    enable_dc_output(
        smu,
        channel=1,
        max_voltage_v=1.2,
        max_current_a=0.001,
    )

    print(
        f"\nCollecting {RECORD_COUNT} instrument-timed records...",
        flush=True,
    )

    start = time.perf_counter()
    readings = acquisition.acquire(timeout_s=10.0)
    pc_elapsed = time.perf_counter() - start

    # Turn output off as soon as the finite acquisition is retrieved.
    smu.shutdown()
    print("Acquisition retrieved; shutdown completed.", flush=True)

    print(
        "\n #   Instrument time[s]   Voltage[V]   "
        "Current[uA]   Status   Compliance",
        flush=True,
    )

    for index, reading in enumerate(readings, start=1):
        print(
            f"{index:2d}   "
            f"{reading.timestamp_s:18.10g}   "
            f"{reading.voltage_v:10.7g}   "
            f"{reading.current_a * 1e6:11.7g}   "
            f"{reading.status:6d}   "
            f"{reading.in_compliance}",
            flush=True,
        )

    print(
        f"\nPC acquisition-call elapsed time: {pc_elapsed:.6f} s",
        flush=True,
    )

    verify_records(readings, source_function)
    print("Buffered phase functional checks passed.", flush=True)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    print(
        "Required: 6.93 kOhm resistor on channel 1; "
        "DMM across it in DC volts; channel 2 disconnected.",
        flush=True,
    )

    input(
        "Confirm both outputs are OFF and no cell is connected. "
        "Press Enter to connect, or Ctrl+C to cancel: "
    )

    with KeysightB2902B(VISA_ADDRESS) as smu:
        print(f"\nInstrument: {smu.identity}", flush=True)

        existing_errors = smu.read_errors()

        if existing_errors:
            for code, message in existing_errors:
                print(
                    f"Existing error: {code}: {message}",
                    flush=True,
                )

            raise RuntimeError(
                "Stopped before configuration due to existing errors."
            )

        try:
            run_phase(smu, "VOLT")
            run_phase(smu, "CURR")

        except BaseException as original_error:
            try:
                smu.shutdown()
            except Exception as cleanup_error:
                original_error.add_note(
                    "Final shutdown could not be verified: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            raise

        else:
            smu.shutdown()

            for channel in (smu.chan1, smu.chan2):
                state = channel.get_output_state()
                smu.check_errors()

                if state:
                    raise RuntimeError(
                        f"Channel {channel.channel} still reports ON."
                    )

            print(
                "\nBuffered resistor diagnostic passed. "
                "Both outputs report OFF.",
                flush=True,
            )


if __name__ == "__main__":
    main()