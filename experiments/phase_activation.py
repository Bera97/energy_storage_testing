"""Explicit phase output activation with host-time evidence.

OUTPUT-CAPABLE: calling this function may energize one SMU channel.

Not connected to the CLI or preconditioning backend.

Caller must create recording and prepare the source/acquisition
before calling this function, and retain an outer shutdown boundary.

Host timestamps bracket the enable helper call and verification.
They do not establish an exact physical output-enable instant or
an instrument-to-host timestamp mapping.
"""

from dataclasses import dataclass
import math
import time

from drivers.dc_output import enable_dc_output
from drivers.shutdown_reporting import attempt_shutdown_with_report


def _finite_number(value, name, *, positive=False):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
    ):
        raise ValueError(f"{name} must be a finite number.")

    try:
        result = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError(
            f"{name} must be a finite number."
        ) from error

    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number.")

    if positive and result <= 0:
        raise ValueError(f"{name} must be positive.")

    return result


@dataclass(frozen=True)
class PhaseActivation:
    """Host-time evidence; no instrument timestamp origin."""

    channel: int
    host_before_enable_s: float
    host_after_verification_s: float

    @property
    def enable_call_and_verification_elapsed_s(self):
        return (
            self.host_after_verification_s
            - self.host_before_enable_s
        )


def activate_phase_output(
    smu,
    channel,
    *,
    maximum_voltage_v,
    maximum_current_a,
    clock=time.monotonic,
):
    """Enable one prepared channel and return host-time evidence.

    On success, output remains enabled for the caller's acquisition.

    On runtime failure, attempt shutdown and re-raise the original
    exception with activation_report and shutdown_report attached.

    Argument-validation failures occur before instrument access.
    """
    if type(channel) is not int or channel not in (1, 2):
        raise ValueError("channel must be integer 1 or 2.")

    voltage_ceiling = _finite_number(
        maximum_voltage_v,
        "maximum_voltage_v",
        positive=True,
    )
    current_ceiling = _finite_number(
        maximum_current_a,
        "maximum_current_a",
        positive=True,
    )

    if not callable(clock):
        raise ValueError("clock must be callable.")

    report = {
        "state": "starting",
        "channel": channel,
        "maximum_voltage_v": voltage_ceiling,
        "maximum_current_a": current_ceiling,
        "host_before_enable_s": None,
        "host_after_verification_s": None,
        "selected_output_reported_on": None,
        "other_output_reported_off": None,
        "instrument_timestamp_mapping_established": False,
        "shutdown_verified": None,
        "error": None,
        "cleanup_error": None,
    }

    try:
        for item in (smu.chan1, smu.chan2):
            state = item.get_output_state()
            smu.check_errors()

            if state is not False:
                raise RuntimeError(
                    "Both outputs must explicitly report OFF "
                    "before phase activation."
                )

        before = _finite_number(
            clock(),
            "host clock before enable",
        )
        report["host_before_enable_s"] = before

        enable_dc_output(
            smu,
            channel,
            max_voltage_v=voltage_ceiling,
            max_current_a=current_ceiling,
        )

        selected = smu.chan1 if channel == 1 else smu.chan2
        other = smu.chan2 if channel == 1 else smu.chan1

        selected_state = selected.get_output_state()
        smu.check_errors()
        report["selected_output_reported_on"] = (
            selected_state is True
        )

        if selected_state is not True:
            raise RuntimeError(
                "Selected output did not explicitly report ON."
            )

        other_state = other.get_output_state()
        smu.check_errors()
        report["other_output_reported_off"] = (
            other_state is False
        )

        if other_state is not False:
            raise RuntimeError(
                "Unselected output did not explicitly report OFF."
            )

        after = _finite_number(
            clock(),
            "host clock after verification",
        )
        report["host_after_verification_s"] = after

        if after < before:
            raise RuntimeError(
                "Host clock moved backwards during activation."
            )

        return PhaseActivation(
            channel=channel,
            host_before_enable_s=before,
            host_after_verification_s=after,
        )

    except BaseException as original_error:
        report["state"] = "raised"
        report["error"] = {
            "type": type(original_error).__name__,
            "message": str(original_error),
        }

        try:
            shutdown = attempt_shutdown_with_report(
                smu,
                original_error,
            )

        except BaseException as cleanup_error:
            # The shutdown helper attached its in-progress report
            # before attempting shutdown. Keep verification unknown.
            report["cleanup_error"] = {
                "type": type(cleanup_error).__name__,
                "message": str(cleanup_error),
            }
            original_error.add_note(
                "Activation cleanup was interrupted: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )

        else:
            report["shutdown_verified"] = shutdown["verified"]

            if shutdown["verified"] is False:
                report["cleanup_error"] = {
                    "type": shutdown["error_type"],
                    "message": shutdown["error_message"],
                }

        original_error.activation_report = report
        raise