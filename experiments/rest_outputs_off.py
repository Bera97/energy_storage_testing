"""Host-timed rest with both SMU outputs OFF.

No output enabling and no measurement acquisition.

Uses the existing SMU contract:
- shutdown() returns only after its shutdown verification succeeds.
- chan1/chan2.get_output_state() report output state.
- check_errors() raises on instrument errors.

Host timing and periodic output-state checks do not independently
prove electrical isolation or continuous physical output state.
"""

import math
import time


def _finite_number(value, name, *, positive=False):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
    ):
        raise ValueError(f"{name} must be a finite number.")

    value = float(value)

    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number.")

    if positive:
        if value <= 0:
            raise ValueError(f"{name} must be positive.")
    elif value < 0:
        raise ValueError(f"{name} must be nonnegative.")

    return value


def _error_document(error):
    return {
        "type": type(error).__name__,
        "message": str(error),
    }


def rest_outputs_off(
    smu,
    duration_s,
    *,
    poll_interval_s=0.1,
    clock=time.monotonic,
    sleep=time.sleep,
):
    """Return a rest report after final shutdown succeeds.

    Invalid arguments are rejected before instrument access.

    Runtime exceptions retain their identity and receive:
    - rest_report
    - shutdown_report

    The caller must retain an outer experiment shutdown boundary.
    """
    duration_s = _finite_number(
        duration_s,
        "duration_s",
    )
    poll_interval_s = _finite_number(
        poll_interval_s,
        "poll_interval_s",
        positive=True,
    )

    if not callable(clock) or not callable(sleep):
        raise ValueError("clock and sleep must be callable.")

    report = {
        "phase_type": "rest",
        "state": "running",
        "requested_duration_s": duration_s,
        "elapsed_s": None,
        "output_check_count": 0,
        "both_outputs_reported_off": False,
        "initial_shutdown_verified": None,
        "shutdown_verified": None,
        "error": None,
        "cleanup_error": None,
    }

    previous_time = None

    def read_clock():
        nonlocal previous_time

        value = _finite_number(clock(), "clock value")

        if previous_time is not None and value < previous_time:
            raise RuntimeError(
                "Rest clock moved backwards."
            )

        previous_time = value
        return value

    def require_outputs_off():
        for channel in (smu.chan1, smu.chan2):
            state = channel.get_output_state()
            smu.check_errors()

            if state is not False:
                report["both_outputs_reported_off"] = False
                raise RuntimeError(
                    "Both outputs must explicitly report OFF "
                    "throughout rest."
                )

        report["output_check_count"] += 1
        report["both_outputs_reported_off"] = True

    def final_shutdown():
        shutdown_report = {
            "attempted": True,
            "verified": None,
            "evidence": "shutdown_attempt_started",
            "error_type": None,
            "error_message": None,
        }

        try:
            smu.shutdown()
        except BaseException as cleanup_error:
            report["shutdown_verified"] = False
            report["cleanup_error"] = _error_document(
                cleanup_error
            )
            shutdown_report.update({
                "verified": False,
                "evidence": "smu_shutdown_raised",
                "error_type": type(cleanup_error).__name__,
                "error_message": str(cleanup_error),
            })
            return shutdown_report, cleanup_error

        report["shutdown_verified"] = True
        shutdown_report.update({
            "verified": True,
            "evidence": "smu_shutdown_returned",
        })
        return shutdown_report, None

    try:
        try:
            smu.shutdown()
        except BaseException:
            report["initial_shutdown_verified"] = False
            raise

        report["initial_shutdown_verified"] = True
        require_outputs_off()

        started = read_clock()
        deadline = started + duration_s

        if not math.isfinite(deadline):
            raise ValueError("Rest deadline is not finite.")

        while True:
            now = read_clock()
            report["elapsed_s"] = now - started

            if now >= deadline:
                break

            sleep(min(poll_interval_s, deadline - now))
            require_outputs_off()

    except BaseException as original_error:
        report["state"] = "raised"
        report["error"] = _error_document(original_error)

        shutdown_report, cleanup_error = final_shutdown()

        original_error.rest_report = report
        original_error.shutdown_report = shutdown_report

        if cleanup_error is not None:
            original_error.add_note(
                "Rest cleanup shutdown also failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )

        raise

    shutdown_report, cleanup_error = final_shutdown()

    if cleanup_error is not None:
        report["state"] = "raised"
        report["error"] = _error_document(cleanup_error)
        cleanup_error.rest_report = report
        cleanup_error.shutdown_report = shutdown_report
        raise cleanup_error

    report["state"] = "returned"
    return report