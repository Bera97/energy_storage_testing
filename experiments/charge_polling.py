"""Charge-specific interface to shared phase polling."""

import time

from experiments.phase_polling import (
    AcquisitionExhausted,
    TracePoll,
    TraceShutdownError,
    run_phase_polling,
)


ChargeAcquisitionExhausted = AcquisitionExhausted
ChargeShutdownError = TraceShutdownError


def run_charge_polling(
    *,
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
    """Run charge polling and retain the charge report attribute."""
    try:
        return run_phase_polling(
            phase_type="charge",
            expected_stop_reason="cv_current_termination",
            monitor=monitor,
            start=start,
            poll=poll,
            shutdown_verified=shutdown_verified,
            finalize=finalize,
            requested_count=requested_count,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            phase_started_s=phase_started_s,
            clock=clock,
            sleep=sleep,
        )
    except BaseException as original_error:
        report = getattr(
            original_error,
            "phase_loop_report",
            None,
        )

        if isinstance(report, dict):
            original_error.charge_loop_report = report

        raise