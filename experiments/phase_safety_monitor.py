"""Deadline adapter for an existing phase trace monitor.

No output enabling.

Adds:
- A total-phase deadline check immediately before initiation.
- A host-side CV deadline after first observed CV.

In first-sample mode, the host instant before acquisition initiation
is a conservative lower bound for the first sample's host time.

This uses host and instrument seconds as the configured timing units.
It is not an independent hardware protection or clock calibration.
"""

import math

from experiments.first_sample_trace_monitor import (
    trace_timing_evidence,
)


def _number(value, name):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
    ):
        raise ValueError(f"{name} must be finite.")

    try:
        result = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be finite.") from error

    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite.")

    return result


class PhaseSafetyMonitor:
    """Delegate measurement decisions and expose host deadline evidence."""

    def __init__(
        self,
        monitor,
        *,
        phase_started_s,
        maximum_duration_s,
        clock,
        measurement_time_basis,
        instrument_timestamp_origin_s,
        phase_elapsed_at_origin_s,
    ):
        if not callable(clock):
            raise ValueError("clock must be callable.")

        self.measurement_monitor = monitor
        self._clock = clock

        self._started = _number(
            clock() if phase_started_s is None else phase_started_s,
            "host phase start",
        )
        self._maximum_duration = _number(
            maximum_duration_s,
            "maximum_duration_s",
        )

        if self._maximum_duration <= 0:
            raise ValueError(
                "maximum_duration_s must be positive."
            )

        self._basis = measurement_time_basis
        self._instrument_origin = instrument_timestamp_origin_s
        self._elapsed_origin = phase_elapsed_at_origin_s
        self._before_initiation = None

    @property
    def state(self):
        return self.measurement_monitor.state

    @property
    def records_saved(self):
        return self.measurement_monitor.records_saved

    @property
    def records_evaluated(self):
        return self.measurement_monitor.records_evaluated

    def ingest(self, readings):
        return self.measurement_monitor.ingest(readings)

    def before_initiation(self):
        """Call after acquisition preflight, before sending INIT."""
        now = _number(self._clock(), "host clock")

        if now < self._started:
            raise RuntimeError(
                "Host clock precedes the phase start."
            )

        elapsed = now - self._started

        if not math.isfinite(elapsed):
            raise RuntimeError(
                "Host phase elapsed time is not finite."
            )

        if elapsed >= self._maximum_duration:
            raise TimeoutError(
                "Phase host deadline reached before "
                "acquisition initiation."
            )

        self._before_initiation = now

    @property
    def host_cv_deadline_s(self):
        """Return a CV deadline after observed CV, otherwise None."""
        monitor = self.measurement_monitor
        policy_monitor = getattr(
            monitor,
            "policy_monitor",
            monitor,
        )
        controller = getattr(
            policy_monitor,
            "controller",
            None,
        )

        first_cv = getattr(
            controller,
            "first_cv_elapsed_s",
            None,
        )

        if first_cv is None:
            return None

        first_cv = _number(
            first_cv,
            "first observed CV elapsed time",
        )
        maximum_cv = _number(
            controller.maximum_cv_duration_s,
            "maximum CV duration",
        )

        if self._basis == "first_recorded_sample":
            if self._before_initiation is None:
                raise RuntimeError(
                    "CV timing has no acquisition-start reference."
                )

            reference = self._before_initiation
        else:
            reference = self._started

        deadline = reference + first_cv + maximum_cv

        if not math.isfinite(deadline):
            raise RuntimeError(
                "Host CV deadline is not finite."
            )

        return deadline

    @property
    def timing_evidence(self):
        evidence = trace_timing_evidence(
            self.measurement_monitor,
            instrument_timestamp_origin_s=self._instrument_origin,
            phase_elapsed_at_origin_s=self._elapsed_origin,
        )

        return {
            **evidence,
            "host_before_initiation_s": self._before_initiation,
            "host_cv_deadline_s": self.host_cv_deadline_s,
            "host_cv_deadline_policy": (
                "pre_initiation_lower_bound_plus_observed_cv_elapsed"
                if self._basis == "first_recorded_sample"
                else "caller_phase_start_plus_observed_cv_elapsed"
            ),
        }