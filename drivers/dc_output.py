"""Explicit DC output enable with configuration preflight checks."""

from typing import TYPE_CHECKING

from drivers.smu_channel import SMUChannel

if TYPE_CHECKING:
    from drivers.keysight_b2902b import KeysightB2902B


def enable_dc_output(
    smu: "KeysightB2902B",
    channel: int,
    *,
    max_voltage_v: float,
    max_current_a: float,
) -> None:
    """Enable one channel after DC preflight checks.

    Initial policy:
        Both channels must initially report OFF.
        Selected channel must use DC/FIX operation.
        Automatic output-on must be OFF.
        Output-off mode must be HIZ.
        Setpoint and active compliance must fit caller ceilings.

    Aborts trigger activity on the selected channel before reading
    its source configuration.

    Does not configure source levels, ranges, or compliance.
    Does not validate the complete instrument operating envelope.
    Does not continuously enforce the supplied ceilings.

    Caller must arrange shutdown in a finally block.

    If output enabling is attempted and then fails, output OFF is
    attempted before propagating the original exception.

    SCPI output command reference: section 4-80.
    """
    if type(channel) is not int or channel not in (1, 2):
        raise ValueError("channel must be integer 1 or 2.")

    voltage_ceiling = SMUChannel._positive_number(
        max_voltage_v, "max_voltage_v"
    )
    current_ceiling = SMUChannel._positive_number(
        max_current_a, "max_current_a"
    )

    selected = smu.chan1 if channel == 1 else smu.chan2

    def checked(operation, *arguments):
        result = operation(*arguments)
        smu.check_errors()
        return result

    def require_text(command, expected):
        response = checked(smu._query, command)

        if response != expected:
            raise RuntimeError(
                f"{command}: expected {expected!r}, "
                f"received {response!r}."
            )

    smu.check_errors()

    for item in (smu.chan1, smu.chan2):
        if checked(item.get_output_state):
            raise RuntimeError(
                f"Channel {item.channel} already reports output ON. "
                "Both channels must be OFF before this operation."
            )

    checked(smu._write, f":ABOR:ALL (@{channel})")

    if checked(selected.get_auto_output_on):
        raise RuntimeError("Automatic output-on must be OFF.")

    if checked(selected.get_output_off_mode) != "HIZ":
        raise RuntimeError("Output-off mode must be HIZ.")

    require_text(f":SOUR{channel}:FUNC:SHAP?", "DC")

    source_function = checked(selected.get_source_mode)
    require_text(
        f":SOUR{channel}:{source_function}:MODE?",
        "FIX",
    )

    if source_function == "VOLT":
        voltage = abs(checked(selected.get_voltage_setpoint))
        current = checked(selected.get_current_limit)

        if current <= 0:
            raise RuntimeError(
                "Current compliance must be greater than zero."
            )

    else:
        current = abs(checked(selected.get_current_setpoint))
        voltage = checked(selected.get_voltage_limit)

        if voltage <= 0:
            raise RuntimeError(
                "Voltage compliance must be greater than zero."
            )

    if voltage > voltage_ceiling:
        raise RuntimeError(
            f"Voltage setting/compliance {voltage:.12g} V exceeds "
            f"the caller ceiling {voltage_ceiling:.12g} V."
        )

    if current > current_ceiling:
        raise RuntimeError(
            f"Current setting/compliance {current:.12g} A exceeds "
            f"the caller ceiling {current_ceiling:.12g} A."
        )

    # No output-on command has been sent before this point.
    try:
        checked(smu._write, f":OUTP{channel} ON")

        if not checked(selected.get_output_state):
            raise RuntimeError(
                f"Channel {channel} did not report output ON."
            )

    except BaseException as original_error:
        try:
            selected.disable_output()
            smu.check_errors()

            if checked(selected.get_output_state):
                raise RuntimeError(
                    "Output still reports ON after rollback."
                )

        except Exception as cleanup_error:
            original_error.add_note(
                "Output-off rollback could not be verified: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )

        raise