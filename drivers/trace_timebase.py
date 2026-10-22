"""Explicit trace timestamp configuration with both outputs OFF.

No output enabling or acquisition initiation is performed.

Timer auto-reset is instrument-wide. Do not share the instrument
with another controller or concurrently running acquisition.

ABS trace timestamps do not establish when source output was enabled.
"""

from drivers.scpi import parse_scpi_bool


def configure_trace_timebase(smu, channel):
    """Configure and verify the timer/trace timestamp policy.

    Caller must arrange shutdown if any configuration operation fails.
    A failure may leave partially changed configuration.
    """
    if type(channel) is not int or channel not in (1, 2):
        raise ValueError("channel must be integer 1 or 2.")

    def checked(operation, *arguments):
        result = operation(*arguments)
        smu.check_errors()
        return result

    smu.check_errors()

    for item in (smu.chan1, smu.chan2):
        if checked(item.get_output_state):
            raise RuntimeError(
                "Both outputs must be OFF before configuring "
                "the trace timebase."
            )

    checked(smu.abort)

    reset_command = ":SYST:TIME:TIM:COUN:RES:AUTO"
    checked(smu._write, f"{reset_command} ON")

    response = checked(smu._query, f"{reset_command}?")

    if not parse_scpi_bool(response, f"{reset_command}?"):
        raise RuntimeError(
            "Timer automatic reset did not read back as enabled."
        )

    trace_command = f":TRAC{channel}:TST:FORM"
    checked(smu._write, f"{trace_command} ABS")

    response = checked(smu._query, f"{trace_command}?")

    if response.strip().upper() != "ABS":
        raise RuntimeError(
            "Trace timestamp format did not read back as ABS."
        )

    return {
        "channel": channel,
        "timer_auto_reset": True,
        "timer_reset_event": "initiate",
        "trace_timestamp_format": "ABS",
        "trace_time_basis": (
            "Elapsed relative to the first trace timestamp."
        ),
        "output_enable_time_established": False,
        "host_phase_deadline_required": True,
    }