"""Shared hardware-independent polling for one active phase.

No instrument-driver imports.

The caller supplies:
- A fresh measurement monitor.
- Acquisition start and polling callbacks.
- Verified-shutdown and recording-finalization callbacks.
- A host-monotonic phase start and timeout.

For real execution, establish phase_started_s before attempting
output enabling, using the same clock supplied here.

A deadline cannot interrupt a blocked callback or VISA operation.
The caller must retain an outer shutdown boundary.
"""

from dataclasses import dataclass
import math
import time


class AcquisitionExhausted(RuntimeError):
    """Finite acquisition ended without the required phase endpoint."""


class TraceShutdownError(RuntimeError):
    """Reported output-off state could not be verified."""


class UnexpectedPhaseTermination(RuntimeError):
    """A monitor returned a stop reason inappropriate for the phase."""


@dataclass(frozen=True)
class TracePoll:
    readings: tuple
    idle: bool


def _finite_number(value, name):
    if isinstance(value, bool) or not isinstance(
        value, (int, float)
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

    return result


def _positive_number(value, name):
    result = _finite_number(value, name)

    if result <= 0:
        raise ValueError(f"{name} must be positive.")

    return result


def run_phase_polling(
    *,
    phase_type,
    expected_stop_reason,
    monitor,
    start,
    poll,
    shutdown_verified,
    finalize,
    requested_count,
    timeout_s,
    poll_interval_s=0.05,
    phase_started_s=None,
    clock=time.monotonic,
    sleep=time.sleep,
):
    """Poll a phase, shut down, then finalize retained measurements.

    Execution exceptions receive phase_loop_report.
    Argument-validation failures occur before execution handling.
    """
    supported = {
        "charge": "cv_current_termination",
        "discharge": "voltage_min",
    }

    if not isinstance(phase_type, str) or phase_type not in supported:
        raise ValueError("phase_type must be charge or discharge.")

    if expected_stop_reason != supported[phase_type]:
        raise ValueError(
            "expected_stop_reason does not match phase_type."
        )

    timeout = _positive_number(timeout_s, "timeout_s")
    interval = _positive_number(
        poll_interval_s, "poll_interval_s"
    )

    if type(requested_count) is not int or requested_count < 1:
        raise ValueError("requested_count must be a positive integer.")

    for name, callback in (
        ("start", start),
        ("poll", poll),
        ("shutdown_verified", shutdown_verified),
        ("finalize", finalize),
        ("clock", clock),
        ("sleep", sleep),
    ):
        if not callable(callback):
            raise ValueError(f"{name} must be callable.")

    if monitor.state != "running" or monitor.records_saved != 0:
        raise ValueError("A fresh running trace monitor is required.")

    entered = _finite_number(clock(), "clock reading")

    if phase_started_s is None:
        started = entered
        origin_policy = "polling_function_entry"
    else:
        started = _finite_number(
            phase_started_s, "phase_started_s"
        )
        origin_policy = "caller_supplied_phase_start"

        if started > entered:
            raise ValueError(
                "phase_started_s must not be in the future."
            )

    last_clock = entered

    report = {
        "state": "running",
        "phase_type": phase_type,
        "expected_stop_reason": expected_stop_reason,
        "requested_count": requested_count,
        "phase_started_monotonic_s": started,
        "host_deadline_origin": origin_policy,
        "stop_reason": None,
        "triggering_record_index": None,
        "records_saved_before_finalization": 0,
        "shutdown_verified": None,
        "finalized": False,
        "finalization_attempted": False,
        "error": None,
        "cleanup_error": None,
        "finalization_error": None,
    }

    def require_shutdown():
        if shutdown_verified() is not True:
            raise TraceShutdownError(
                "Output OFF was not explicitly verified."
            )

    def finish_recording():
        report["finalization_attempted"] = True

        try:
            finalize()
        except BaseException as error:
            report["finalization_error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
            raise

        report["finalized"] = True

    def check_deadline():
        nonlocal last_clock

        now = _finite_number(clock(), "clock reading")

        if now < last_clock:
            raise RuntimeError(
                "Host-monotonic clock moved backwards."
            )

        last_clock = now
        elapsed = now - started

        if not math.isfinite(elapsed):
            raise RuntimeError(
                "Host-monotonic elapsed time is not finite."
            )

        if elapsed >= timeout:
            raise TimeoutError(
                f"{phase_type.capitalize()} acquisition "
                "host deadline reached."
            )

        return timeout - elapsed

    try:
        check_deadline()
        start()
        check_deadline()

        while True:
            check_deadline()
            batch = poll()

            if not isinstance(batch, TracePoll):
                raise TypeError("poll() must return TracePoll.")

            if type(batch.idle) is not bool:
                raise ValueError("TracePoll.idle must be boolean.")

            # Preserve retrieved records before checking whether
            # their retrieval exceeded the host deadline.
            decision = monitor.ingest(batch.readings)
            report["records_saved_before_finalization"] = (
                monitor.records_saved
            )

            check_deadline()

            if monitor.records_saved > requested_count:
                raise RuntimeError(
                    "Retrieved records exceed requested_count."
                )

            if decision.stop_reason is not None:
                if decision.stop_reason != expected_stop_reason:
                    raise UnexpectedPhaseTermination(
                        f"{phase_type} monitor returned "
                        f"{decision.stop_reason!r}; expected "
                        f"{expected_stop_reason!r}."
                    )

                report["stop_reason"] = decision.stop_reason
                report["triggering_record_index"] = (
                    decision.triggering_record_index
                )
                break

            if batch.idle or monitor.records_saved == requested_count:
                raise AcquisitionExhausted(
                    "Finite acquisition ended without "
                    f"{expected_stop_reason}."
                )

            remaining = check_deadline()
            sleep(min(interval, remaining))

        require_shutdown()
        report["shutdown_verified"] = True

        finish_recording()
        report["state"] = "returned"
        return report

    except BaseException as original_error:
        report["state"] = "raised"
        report["error"] = {
            "type": type(original_error).__name__,
            "message": str(original_error),
        }
        report["records_saved_before_finalization"] = (
            monitor.records_saved
        )

        if report["shutdown_verified"] is not True:
            try:
                require_shutdown()
            except BaseException as cleanup_error:
                report["shutdown_verified"] = False
                report["cleanup_error"] = {
                    "type": type(cleanup_error).__name__,
                    "message": str(cleanup_error),
                }
                original_error.add_note(
                    f"{phase_type.capitalize()} cleanup could "
                    "not verify output OFF: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            else:
                report["shutdown_verified"] = True

        if (
            report["shutdown_verified"] is True
            and not report["finalization_attempted"]
        ):
            try:
                finish_recording()
            except BaseException as finalization_error:
                original_error.add_note(
                    f"{phase_type.capitalize()} recording "
                    "finalization failed: "
                    f"{type(finalization_error).__name__}: "
                    f"{finalization_error}"
                )

        original_error.phase_loop_report = dict(report)
        raise