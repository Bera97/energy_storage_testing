"""Hardware validation of paired voltage/current spot records.

Uses the previously tested resistor configuration.
Channel 1 only; channel 2 disconnected.
No battery or capacitor connected.

Instrument timestamps are inspected, not assumed continuous.
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
from drivers.dc_output import enable_dc_output
from drivers.keysight_b2902b import KeysightB2902B
from acquisition.spot_acquisition import SpotAcquisition


SAMPLE_COUNT = 10
PAUSE_BETWEEN_READS_S = 0.2


def check_prediction(reading, expected_voltage, expected_current):
    """Broad functional screen, not an accuracy specification."""
    if not math.isclose(
        reading.voltage_v,
        expected_voltage,
        rel_tol=0.05,
        abs_tol=1e-6,
    ):
        raise RuntimeError(
            f"Unexpected voltage: {reading.voltage_v} V; "
            f"expected approximately {expected_voltage} V."
        )

    if not math.isclose(
        reading.current_a,
        expected_current,
        rel_tol=0.05,
        abs_tol=1e-9,
    ):
        raise RuntimeError(
            f"Unexpected current: {reading.current_a} A; "
            f"expected approximately {expected_current} A."
        )

    if reading.in_compliance:
        raise RuntimeError(
            "Unexpected compliance condition during resistor test. "
            f"Reading: {reading!r}"
        )


def summarize(readings):
    voltages = [item.voltage_v for item in readings]
    currents = [item.current_a for item in readings]
    timestamps = [item.timestamp_s for item in readings]

    print("\nSummary:", flush=True)
    print(
        f"  Mean voltage: {statistics.mean(voltages):.9g} V",
        flush=True,
    )
    print(
        f"  Mean current: {statistics.mean(currents) * 1e6:.9g} uA",
        flush=True,
    )
    print(
        f"  Mean signed power: "
        f"{statistics.mean(item.power_w for item in readings) * 1e6:.9g} uW",
        flush=True,
    )
    print(
        f"  Observed status values: "
        f"{sorted({item.status for item in readings})}",
        flush=True,
    )
    print(
        f"  Raw instrument timestamps: {timestamps}",
        flush=True,
    )

    differences = [
        later - earlier
        for earlier, later in zip(timestamps, timestamps[1:])
    ]

    print(
        f"  Instrument timestamp differences: {differences}",
        flush=True,
    )

    if all(value > 0 for value in differences):
        print(
            "  Timestamps increased across this test's spot queries.",
            flush=True,
        )
    else:
        print(
            "  Timestamps did not increase strictly across spot queries.",
            flush=True,
        )

    print(
        "  No energy integration or sampling-rate claim is made.",
        flush=True,
    )


def run_phase(smu, source_function):
    print(
        f"\n=== Paired acquisition: {source_function} source ===",
        flush=True,
    )

    configure_channel(smu, source_function)

    acquisition = SpotAcquisition(smu, channel=1)
    acquisition.configure(nplc=1.0)

    if source_function == "VOLT":
        expected_voltage = 1.0
        expected_current = expected_voltage / RESISTANCE_OHM
    else:
        expected_current = 100e-6
        expected_voltage = expected_current * RESISTANCE_OHM

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

    time.sleep(0.5)
    start = time.perf_counter()
    readings = []

    print(
        "\n #   PC elapsed[s]   SMU timestamp[s]   "
        "Voltage[V]   Current[uA]   Status   Compliance",
        flush=True,
    )

    for index in range(SAMPLE_COUNT):
        reading = acquisition.read()
        pc_elapsed = time.perf_counter() - start
        readings.append(reading)

        print(
            f"{index + 1:2d}   "
            f"{pc_elapsed:12.6f}   "
            f"{reading.timestamp_s:16.9g}   "
            f"{reading.voltage_v:10.7g}   "
            f"{reading.current_a * 1e6:11.7g}   "
            f"{reading.status:6d}   "
            f"{reading.in_compliance}",
            flush=True,
        )

        if reading.current_source != (source_function == "CURR"):
            raise RuntimeError(
                "Measurement status source-function bit does not "
                f"match the configured source function: {reading!r}"
            )

        check_prediction(
            reading,
            expected_voltage,
            expected_current,
        )

        time.sleep(PAUSE_BETWEEN_READS_S)

    summarize(readings)

    input(
        "\nRecord the external DMM voltage. "
        "Press Enter to shut down this phase: "
    )

    smu.shutdown()
    print("Phase shutdown completed.", flush=True)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    print(
        "Required: 6.93 kOhm resistor on channel 1, "
        "DMM across resistor in DC volts, channel 2 disconnected.",
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
                print(f"Existing error: {code}: {message}", flush=True)

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
                "\nPaired spot hardware check passed. "
                "Both outputs report OFF.",
                flush=True,
            )


if __name__ == "__main__":
    main()