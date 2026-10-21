"""Low-power current-sinking check with an external 2 V supply.

DPS150 OUT+ -> 6.93 kOhm resistor -> CH1 FORCE HI
DPS150 OUT- --------------------> CH1 FORCE LO

DMM across CH1 FORCE HI/LO in DC-voltage mode.
Channel 2 disconnected. No cell/capacitor.
External supply is manually controlled, not controlled by this script.
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
from drivers.dc_output import enable_dc_output
from drivers.keysight_b2902b import KeysightB2902B
from acquisition.spot_acquisition import SpotAcquisition


CURRENT_A = -100e-6
VOLTAGE_COMPLIANCE_V = 2.0
SAMPLE_COUNT = 10


def run_check(smu):
    channel = configure_channel(smu, "CURR")

    # configure_channel() leaves both SMU outputs OFF.
    checked(smu, channel.set_voltage_limit, VOLTAGE_COMPLIANCE_V)
    checked(smu, channel.set_current, CURRENT_A)

    acquisition = SpotAcquisition(smu, channel=1)
    acquisition.configure(nplc=1.0)

    actual_current = checked(smu, channel.get_current_setpoint)
    actual_limit = checked(smu, channel.get_voltage_limit)

    if not math.isclose(
        actual_current, CURRENT_A, rel_tol=1e-6, abs_tol=1e-12
    ):
        raise RuntimeError(
            f"Unexpected current-setpoint readback: {actual_current}"
        )

    if not math.isclose(
        actual_limit,
        VOLTAGE_COMPLIANCE_V,
        rel_tol=1e-6,
        abs_tol=1e-9,
    ):
        raise RuntimeError(
            f"Unexpected voltage-compliance readback: {actual_limit}"
        )

    print(
        f"\nConfigured current: {actual_current * 1e6:.6g} uA\n"
        f"Voltage compliance: {actual_limit:.6g} V\n"
        "SMU output is still OFF.",
        flush=True,
    )

    input(
        "\nNow enable the DPS150 at 2.00 V / 10 mA.\n"
        "With SMU output OFF, confirm the DMM shows approximately "
        "+2 V at the SMU terminals.\n"
        "If polarity/value is wrong, turn DPS150 OFF and Ctrl+C.\n"
        "Otherwise press Enter to enable the SMU at -100 uA: "
    )

    enable_dc_output(
        smu,
        channel=1,
        max_voltage_v=2.1,
        max_current_a=0.001,
    )

    readings = []

    print(
        "\n #   Voltage[V]   Current[uA]   Power[uW]   "
        "Status   Compliance",
        flush=True,
    )

    deadline = time.monotonic() + 10.0

    for index in range(1, SAMPLE_COUNT + 1):
        if time.monotonic() >= deadline:
            raise TimeoutError("Sink diagnostic exceeded its time budget.")

        reading = acquisition.read()
        readings.append(reading)

        # Broad functional checks, not accuracy specifications.
        if (
            not reading.current_source
            or reading.in_compliance
            or not 1.15 <= reading.voltage_v <= 1.45
            or not -105e-6 <= reading.current_a <= -95e-6
            or reading.power_w >= 0
        ):
            raise RuntimeError(
                f"Unexpected sink operating point: {reading!r}; "
                f"power={reading.power_w} W."
            )

        print(
            f"{index:2d}   "
            f"{reading.voltage_v:10.7g}   "
            f"{reading.current_a * 1e6:11.7g}   "
            f"{reading.power_w * 1e6:9.7g}   "
            f"{reading.status:6d}   "
            f"{reading.in_compliance}",
            flush=True,
        )

        if index < SAMPLE_COUNT:
            time.sleep(0.2)

    # End the active sink test before summary/reporting.
    smu.shutdown()

    print(
        "\nSMU shutdown verified. TURN DPS150 OUTPUT OFF NOW.",
        flush=True,
    )
    input("Confirm DPS150 output is OFF, then press Enter: ")

    print(
        "\nSummary:\n"
        f"  Records: {len(readings)}\n"
        f"  Mean voltage: "
        f"{statistics.mean(r.voltage_v for r in readings):.9g} V\n"
        f"  Mean current: "
        f"{statistics.mean(r.current_a for r in readings) * 1e6:.9g} uA\n"
        f"  Mean signed power: "
        f"{statistics.mean(r.power_w for r in readings) * 1e6:.9g} uW\n"
        "Current-sinking functional check passed.",
        flush=True,
    )


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    print(
        "Required: DPS150 OUT+ -> 6.93 kOhm -> CH1 FORCE HI;\n"
        "DPS150 OUT- -> CH1 FORCE LO.\n"
        "DMM across SMU terminals in DC volts.\n"
        "Channel 2 disconnected. No cell/capacitor.\n"
        "DPS150: 2.00 V, 10 mA limit, output OFF.",
        flush=True,
    )

    input(
        "\nConfirm DPS150 and both SMU outputs are OFF. "
        "Enter to connect, Ctrl+C cancels: "
    )

    try:
        with KeysightB2902B(VISA_ADDRESS) as smu:
            print(f"\nInstrument: {smu.identity}", flush=True)

            try:
                existing_errors = smu.read_errors()
                if existing_errors:
                    raise RuntimeError(
                        f"Existing errors: {existing_errors!r}. "
                        "Stopped before configuration."
                    )

                run_check(smu)

            except BaseException as original_error:
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

    finally:
        print(
            "\nLeave DPS150 output OFF. The script cannot control it.",
            flush=True,
        )


if __name__ == "__main__":
    main()