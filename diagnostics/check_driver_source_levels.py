"""Validate source settings with outputs OFF and no circuit connected.

Never sends output ON, INIT, READ, or MEAS commands.
Changes source function, compliance, and immediate source settings.
Does not restore the previous instrument configuration.
"""

import logging
import math

from drivers.keysight_b2902b import KeysightB2902B


logger = logging.getLogger(__name__)

VISA_ADDRESS = "USB0::0x2A8D::0x9201::MY61391173::0::INSTR"


def checked(smu, operation, *arguments):
    """Perform one operation, then check the instrument error queue."""
    result = operation(*arguments)
    smu.check_errors()
    return result


def require_output_off(smu, channel):
    state = checked(smu, channel.get_output_state)

    if state:
        raise RuntimeError(
            f"Channel {channel.channel} reports output ON. "
            "Stopping this output-off diagnostic."
        )


def verify_value(label, actual, expected, absolute_tolerance):
    print(
        f"  {label}: requested={expected:.12g}, "
        f"readback={actual:.12g}",
        flush=True,
    )

    if not math.isclose(
        actual,
        expected,
        rel_tol=1e-6,
        abs_tol=absolute_tolerance,
    ):
        raise RuntimeError(
            f"{label} mismatch: expected {expected!r}, "
            f"received {actual!r}."
        )


def verify_mode(smu, channel, expected):
    actual = checked(smu, channel.get_source_mode)

    if actual != expected:
        raise RuntimeError(
            f"Channel {channel.channel}: expected source mode "
            f"{expected}, received {actual}."
        )


def check_channel(smu, channel):
    print(f"\n--- Channel {channel.channel} ---", flush=True)
    require_output_off(smu, channel)

    print("Voltage-source configuration:", flush=True)

    checked(smu, channel.set_mode_voltage_source)
    verify_mode(smu, channel, "VOLT")

    checked(smu, channel.set_current_limit, 0.001)
    actual_limit = checked(smu, channel.get_current_limit)

    verify_value(
        "Current compliance [A]",
        actual_limit,
        0.001,
        absolute_tolerance=1e-8,
    )

    for voltage in (0.0, 0.1, -0.1):
        require_output_off(smu, channel)
        checked(smu, channel.set_voltage, voltage)
        actual = checked(smu, channel.get_voltage_setpoint)

        verify_value(
            "Voltage setpoint [V]",
            actual,
            voltage,
            absolute_tolerance=1e-6,
        )

        require_output_off(smu, channel)

    checked(smu, channel.set_voltage, 0.0)

    print("Current-source configuration:", flush=True)

    checked(smu, channel.set_mode_current_source)
    verify_mode(smu, channel, "CURR")

    checked(smu, channel.set_voltage_limit, 0.5)
    actual_limit = checked(smu, channel.get_voltage_limit)

    verify_value(
        "Voltage compliance [V]",
        actual_limit,
        0.5,
        absolute_tolerance=1e-6,
    )

    for current in (0.0, 0.00001, -0.00001):
        require_output_off(smu, channel)
        checked(smu, channel.set_current, current)
        actual = checked(smu, channel.get_current_setpoint)

        verify_value(
            "Current setpoint [A]",
            actual,
            current,
            absolute_tolerance=1e-10,
        )

        require_output_off(smu, channel)

    checked(smu, channel.set_current, 0.0)
    print("  Channel source-setting checks passed.", flush=True)


def cleanup_outputs(smu):
    """Attempt cleanup for both channels independently."""
    failures = []

    for channel in (smu.chan1, smu.chan2):
        try:
            channel.disable_output()
            smu.check_errors()

            state = channel.get_output_state()
            smu.check_errors()

            if state:
                raise RuntimeError("Output still reports ON.")

            print(
                f"Cleanup: channel {channel.channel} reports OFF.",
                flush=True,
            )

        except Exception as error:
            failures.append((channel.channel, str(error)))
            logger.exception(
                "Cleanup failed for channel %s.",
                channel.channel,
            )

    return failures


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    with KeysightB2902B(VISA_ADDRESS) as smu:
        print(f"Instrument: {smu.identity}", flush=True)

        existing_errors = smu.read_errors()

        if existing_errors:
            print("Existing instrument errors:", flush=True)

            for code, message in existing_errors:
                print(f"  {code}: {message}", flush=True)

            print(
                "Stopped before changing settings.",
                flush=True,
            )
            return

        # Refuse to start configuration if either output is already on.
        for channel in (smu.chan1, smu.chan2):
            require_output_off(smu, channel)

        print(
            "\nBoth outputs report OFF. Starting configuration checks.",
            flush=True,
        )

        try:
            for channel in (smu.chan1, smu.chan2):
                check_channel(smu, channel)
        finally:
            print("\nAttempting output-off cleanup...", flush=True)
            cleanup_failures = cleanup_outputs(smu)

        if cleanup_failures:
            raise RuntimeError(
                f"Output cleanup could not be verified: "
                f"{cleanup_failures!r}"
            )

        print(
            "\nSource-setting diagnostic passed.",
            flush=True,
        )


if __name__ == "__main__":
    main()