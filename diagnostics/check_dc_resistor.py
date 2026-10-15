"""Low-power B2902B functional check using a 6.93 kOhm resistor.

Channel 1 only.
External multimeter must be connected across the resistor in DC volts.
No battery, capacitor, or other circuit may be connected.

Changes instrument configuration and does not restore prior settings.
"""

import logging
import statistics
import time

from drivers.dc_output import enable_dc_output
from drivers.keysight_b2902b import KeysightB2902B
from drivers.smu_ranges import SMURanges


VISA_ADDRESS = "USB0::0x2A8D::0x9201::MY61391173::0::INSTR"

RESISTANCE_OHM = 6930.0
VOLTAGE_SOURCE_V = 1.0
CURRENT_SOURCE_A = 100e-6

SAMPLE_COUNT = 5
SAMPLE_INTERVAL_S = 0.2


def checked(smu, operation, *arguments):
    result = operation(*arguments)
    smu.check_errors()
    return result


def configure_channel(smu, source_function):
    """Configure with both outputs off and trigger activity aborted."""
    smu.shutdown()

    channel = smu.chan1
    ranges = SMURanges(smu, channel=1)

    checked(smu, channel.set_auto_output_on, False)
    checked(smu, channel.set_output_off_mode, "HIZ")

    channel.configure_fixed_dc(source_function)
    channel.set_four_wire(False)
    channel.configure_spot_measurement(nplc=1.0)

    # Keep continuous output during the spot measurements.
    # This setting is independent of automatic output-on.
    checked(smu, smu._write, ":OUTP1:OFF:AUTO OFF")

    if source_function == "VOLT":
        checked(smu, channel.set_voltage, 0.0)
        ranges.set_source_range("VOLT", 2.0)
        checked(smu, channel.set_current_limit, 0.001)

        ranges.set_measurement_range("VOLT", 2.0)
        ranges.set_measurement_autorange("CURR", True)

        checked(smu, channel.set_voltage, VOLTAGE_SOURCE_V)

    else:
        checked(smu, channel.set_current, 0.0)
        ranges.set_source_range("CURR", 0.001)
        checked(smu, channel.set_voltage_limit, 1.0)

        ranges.set_measurement_range("VOLT", 2.0)
        ranges.set_measurement_range("CURR", 0.001)

        checked(smu, channel.set_current, CURRENT_SOURCE_A)

    print(
        f"  Source function: {checked(smu, channel.get_source_mode)}",
        flush=True,
    )
    print(
        f"  NPLC: {checked(smu, channel.get_nplc):.6g}",
        flush=True,
    )
    print(
        f"  Four-wire sensing: "
        f"{checked(smu, channel.get_four_wire)}",
        flush=True,
    )

    return channel


def collect_readings(channel):
    voltage_readings = []
    current_readings = []

    time.sleep(0.5)

    for index in range(SAMPLE_COUNT):
        voltage = channel.measure_voltage()
        current = channel.measure_current()

        voltage_readings.append(voltage)
        current_readings.append(current)

        print(
            f"  Reading {index + 1}: "
            f"V={voltage:.8g} V, "
            f"I={current * 1e6:.8g} uA",
            flush=True,
        )

        time.sleep(SAMPLE_INTERVAL_S)

    return (
        statistics.mean(voltage_readings),
        statistics.mean(current_readings),
    )


def show_comparison(voltage, current, expected_voltage, expected_current):
    print(f"\n  Mean SMU voltage: {voltage:.8g} V", flush=True)
    print(
        f"  Mean SMU current: {current * 1e6:.8g} uA",
        flush=True,
    )
    print(
        f"  Expected voltage: {expected_voltage:.8g} V",
        flush=True,
    )
    print(
        f"  Expected current: {expected_current * 1e6:.8g} uA",
        flush=True,
    )

    if current != 0:
        print(
            f"  Apparent V/I resistance: "
            f"{voltage / current:.8g} ohm",
            flush=True,
        )

    # Broad functional screen, not an accuracy specification.
    voltage_ok = abs(voltage - expected_voltage) <= (
        0.05 * abs(expected_voltage)
    )
    current_ok = abs(current - expected_current) <= (
        0.05 * abs(expected_current)
    )

    if not voltage_ok or not current_ok:
        raise RuntimeError(
            "Readings differ from the resistor prediction by more "
            "than the diagnostic's 5% functional-check threshold. "
            "Stop and inspect the output before proceeding."
        )


def run_phase(smu, source_function):
    print(
        f"\n=== {source_function} source phase ===",
        flush=True,
    )

    channel = configure_channel(smu, source_function)

    input(
        "\nConfirm resistor and DMM wiring, then press Enter "
        "to enable channel 1. Ctrl+C cancels: "
    )

    enable_dc_output(
        smu,
        channel=1,
        max_voltage_v=1.2,
        max_current_a=0.001,
    )

    print("\nChannel 1 enabled.", flush=True)

    voltage, current = collect_readings(channel)

    if source_function == "VOLT":
        expected_voltage = VOLTAGE_SOURCE_V
        expected_current = VOLTAGE_SOURCE_V / RESISTANCE_OHM
    else:
        expected_current = CURRENT_SOURCE_A
        expected_voltage = CURRENT_SOURCE_A * RESISTANCE_OHM

    show_comparison(
        voltage,
        current,
        expected_voltage,
        expected_current,
    )

    input(
        "\nRecord the external DMM voltage while output is ON. "
        "Press Enter to shut down: "
    )

    smu.shutdown()
    print("Shutdown from active output completed.", flush=True)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    print(
        "Required: channel 1 connected only to the 6.93 kOhm resistor; "
        "DMM across it in DC volts. Channel 2 disconnected.",
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
                "Existing errors were recorded. "
                "Stopped before changing settings."
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
                state = checked(smu, channel.get_output_state)
                print(
                    f"Final channel {channel.channel} output: "
                    f"{'ON' if state else 'OFF'}",
                    flush=True,
                )

            print(
                "\nDC resistor functional check passed.",
                flush=True,
            )


if __name__ == "__main__":
    main()