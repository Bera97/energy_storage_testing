"""Pure constant-power discharge decisions. No instrument or filesystem I/O.

This controller proposes negative current setpoints; it never applies them.
A caller must record evidence, verify setpoint readbacks, enforce call deadlines,
and attempt output-OFF cleanup on EVERY exit, including exceptions.
"""
from dataclasses import dataclass, asdict
from functools import wraps
import math


class PowerControlError(RuntimeError):
    """An unsuccessful power-control phase requiring external cleanup."""


class PowerTimeoutError(PowerControlError):
    pass


class PowerFeedbackError(PowerControlError):
    pass


class PowerTrackingError(PowerControlError):
    pass


def _number(value, name, *, zero=False, signed=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite numeric data.")
    try:
        value = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be finite numeric data.") from error
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite numeric data.")
    if not signed and (value < 0 or (value == 0 and not zero)):
        raise ValueError(f"{name} must be {'nonnegative' if zero else 'positive'}.")
    return value


@dataclass(frozen=True)
class PowerLimits:
    power_w: float
    cutoff_voltage_v: float
    minimum_voltage_v: float
    maximum_voltage_v: float
    maximum_current_a: float
    maximum_duration_s: float
    update_interval_s: float
    maximum_feedback_age_s: float
    relative_tolerance: float
    absolute_tolerance_w: float
    startup_grace_s: float
    maximum_tracking_error_s: float

    def __post_init__(self):
        for key in self.__dataclass_fields__:
            value = _number(getattr(self, key), key, zero=key in (
                "minimum_voltage_v", "absolute_tolerance_w", "startup_grace_s"))
            object.__setattr__(self, key, value)
        if not self.minimum_voltage_v <= self.cutoff_voltage_v < self.maximum_voltage_v:
            raise ValueError("Require minimum voltage <= positive cutoff < maximum voltage.")
        if self.relative_tolerance >= 1:
            raise ValueError("relative_tolerance must be below 1.")
        if self.maximum_feedback_age_s <= self.update_interval_s:
            raise ValueError("Feedback age limit must exceed the update interval.")
        if self.startup_grace_s + self.maximum_tracking_error_s >= self.maximum_duration_s:
            raise ValueError("Tracking grace/error windows must fit within phase duration.")
        if self.power_w / self.cutoff_voltage_v > self.maximum_current_a:
            raise ValueError("Requested power exceeds current capability at cutoff.")

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class PowerSample:
    acquired_at_s: float
    voltage_v: float
    current_a: float
    current_source: bool = True
    in_compliance: bool = False
    has_fault: bool = False


@dataclass(frozen=True)
class PowerDecision:
    action: str
    current_setpoint_a: float | None
    stop_reason: str | None
    requested_power_w: float
    measured_power_w: float
    power_error_w: float
    host_time_s: float
    sample_time_s: float

    def to_dict(self):
        return asdict(self)


def _terminal_on_error(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except BaseException:
            if self.state not in ("completed", "failed"):
                self.state = "failed"
            raise
    return wrapped


class ConstantPowerController:
    """Fresh host-timed feedback, explicit setpoint acknowledgement, no I/O.

    Times share one host-monotonic clock. acquired_at_s is a conservative
    acquisition start time, not arrival time or an unmapped instrument timestamp.
    Feedback after a command must have been acquired AFTER its acknowledgement.
    start() is retained for pure tests with a supplied outputs-off sample.
    Recorded spot acquisition instead uses the explicit zero-current probe API.
    """
    def __init__(self, limits):
        if not isinstance(limits, PowerLimits):
            raise ValueError("A validated PowerLimits instance is required.")
        self.limits = limits
        self.state = "new"
        self.started_s = None
        self.last_host_s = None
        self.last_sample_s = None
        self.last_ack_s = None
        self.last_request_s = None
        self.pending_current_a = None
        self.bad_tracking_since_s = None
        self.probe_enabled_s = None
        self.maximum_probe_current_a = None

    def _now(self, value):
        now = _number(value, "host time", zero=True)
        if self.last_host_s is not None and now < self.last_host_s:
            raise PowerFeedbackError("Host clock moved backwards.")
        self.last_host_s = now
        return now

    def _deadline(self, now):
        if now - self.started_s >= self.limits.maximum_duration_s:
            raise PowerTimeoutError("Constant-power phase duration exceeded.")
        reference = self.started_s if self.last_sample_s is None else self.last_sample_s
        if now - reference > self.limits.maximum_feedback_age_s:
            raise PowerTimeoutError("Constant-power feedback is stale.")
        if (self.bad_tracking_since_s is not None and
                now - self.bad_tracking_since_s >= self.limits.maximum_tracking_error_s):
            raise PowerTrackingError("Power-tracking deadline reached without recovery evidence.")

    def _sample(self, sample, now, *, starting=False):
        if not isinstance(sample, PowerSample):
            raise ValueError("Expected a PowerSample.")
        stamp = _number(sample.acquired_at_s, "sample time", zero=True)
        voltage = _number(sample.voltage_v, "sample voltage", signed=True)
        current = _number(sample.current_a, "sample current", signed=True)
        if any(type(getattr(sample, key)) is not bool for key in (
            "current_source", "in_compliance", "has_fault")):
            raise ValueError("Sample state flags must be actual booleans.")
        if sample.has_fault:
            raise PowerFeedbackError("Instrument reported a measurement fault.")
        if stamp > now or now - stamp > self.limits.maximum_feedback_age_s:
            raise PowerFeedbackError("Future-dated or stale feedback sample.")
        if self.last_sample_s is not None:
            if stamp <= self.last_sample_s:
                raise PowerFeedbackError("Feedback sample times must increase strictly.")
            if stamp - self.last_sample_s > self.limits.maximum_feedback_age_s:
                raise PowerFeedbackError("Feedback sample gap exceeded.")
        if self.last_ack_s is not None and stamp < self.last_ack_s:
            raise PowerFeedbackError("Feedback predates the acknowledged current setpoint.")
        if not self.limits.minimum_voltage_v <= voltage <= self.limits.maximum_voltage_v:
            raise PowerFeedbackError("Voltage is outside supplied device limits.")
        if abs(current) > self.limits.maximum_current_a:
            raise PowerFeedbackError("Measured current exceeds the configured ceiling.")
        if sample.in_compliance:
            raise PowerFeedbackError("Discharge compliance prevents valid power control.")
        if not starting and (not sample.current_source or current > 0):
            raise PowerFeedbackError("Wrong discharge source mode or current polarity.")
        self.last_sample_s = stamp
        return PowerSample(stamp, voltage, current, sample.current_source,
                           sample.in_compliance, sample.has_fault)

    def _decision(self, action, sample, now, current=None, reason=None):
        power = _number(-sample.voltage_v * sample.current_a,
                        "measured power", signed=True)
        error = _number(power - self.limits.power_w, "power error", signed=True)
        return PowerDecision(action, current, reason, self.limits.power_w,
                             power, error, now, sample.acquired_at_s)

    def _decide(self, sample, now, *, starting=False):
        if sample.voltage_v <= self.limits.cutoff_voltage_v:
            decision = self._decision("stop", sample, now, reason="voltage_min")
            self.state = "completed"
            return decision
        desired = -self.limits.power_w / sample.voltage_v
        if not math.isfinite(desired) or abs(desired) > self.limits.maximum_current_a:
            raise PowerControlError("Requested power cannot be maintained within the current ceiling.")
        measured = _number(-sample.voltage_v * sample.current_a, "measured power", signed=True)
        tolerance = max(self.limits.absolute_tolerance_w,
                        self.limits.relative_tolerance * self.limits.power_w)
        if not starting and now - self.started_s >= self.limits.startup_grace_s:
            if abs(measured - self.limits.power_w) > tolerance:
                if self.bad_tracking_since_s is None:
                    self.bad_tracking_since_s = now
                if now - self.bad_tracking_since_s >= self.limits.maximum_tracking_error_s:
                    raise PowerTrackingError("Power tracking stayed outside tolerance.")
            else:
                self.bad_tracking_since_s = None
        if self.last_request_s is None or now - self.last_request_s >= self.limits.update_interval_s:
            self.pending_current_a = desired
            self.last_request_s = now
            self.state = "awaiting_setpoint"
            return self._decision("set_current", sample, now, current=desired)
        return self._decision("hold", sample, now)

    @_terminal_on_error
    def start(self, *, now_s, sample, outputs_verified_off):
        if self.state != "new":
            raise PowerControlError("Use a fresh controller for each phase.")
        if outputs_verified_off is not True:
            raise PowerControlError("The future adapter must verify outputs OFF before startup.")
        now = self._now(now_s)
        self.started_s = now
        sample = self._sample(sample, now, starting=True)
        return self._decide(sample, now, starting=True)

    @_terminal_on_error
    def acknowledge_setpoint(self, *, now_s, readback_current_a):
        if self.state != "awaiting_setpoint":
            raise PowerControlError("No current command is awaiting acknowledgement.")
        now = self._now(now_s)
        self._deadline(now)
        current = _number(readback_current_a, "setpoint readback", signed=True)
        if (current >= 0 or abs(current) > self.limits.maximum_current_a
                or not math.isclose(current, self.pending_current_a, rel_tol=1e-8, abs_tol=1e-12)):
            raise PowerFeedbackError("Current setpoint readback mismatch.")
        self.last_ack_s = now
        self.pending_current_a = None
        self.state = "active"

    @_terminal_on_error
    def update(self, *, now_s, sample):
        if self.state != "active":
            raise PowerControlError("Power feedback requires an acknowledged current command.")
        now = self._now(now_s)
        self._deadline(now)
        sample = self._sample(sample, now)
        return self._decide(sample, now)

    @_terminal_on_error
    def watchdog(self, *, now_s):
        if self.state not in ("active", "awaiting_setpoint", "probe_armed", "probing"):
            raise PowerControlError("No active power-control phase.")
        now = self._now(now_s)
        self._deadline(now)

    @_terminal_on_error
    def arm_zero_current_probe(self, *, now_s, outputs_verified_off,
                               current_source_verified, zero_current_setpoint_a,
                               maximum_probe_current_a):
        """Start the phase deadline before explicit zero-current probe activation.

        These arguments are readback evidence supplied by the adapter, not
        independent electrical verification. No output command is sent here.
        """
        if self.state != "new":
            raise PowerControlError("Use a fresh controller for each phase.")
        if outputs_verified_off is not True or current_source_verified is not True:
            raise PowerControlError("Probe setup must report output OFF and current-source mode.")
        level = _number(zero_current_setpoint_a, "probe setpoint", signed=True)
        ceiling = _number(maximum_probe_current_a, "maximum_probe_current_a")
        if level != 0 or ceiling > self.limits.maximum_current_a:
            raise PowerControlError("Probe requires a verified zero setpoint and a valid current ceiling.")
        self.started_s = self._now(now_s)
        self.maximum_probe_current_a = ceiling
        self.state = "probe_armed"

    @_terminal_on_error
    def confirm_zero_current_probe_enabled(self, *, now_s, output_reported_on):
        if self.state != "probe_armed" or output_reported_on is not True:
            raise PowerControlError("Explicit probe activation must be verified before sampling.")
        now = self._now(now_s)
        self._deadline(now)
        self.probe_enabled_s = now
        self.state = "probing"

    @_terminal_on_error
    def start_from_zero_current_probe(self, *, now_s, sample):
        if self.state != "probing":
            raise PowerControlError("An explicitly enabled zero-current probe is required.")
        now = self._now(now_s)
        self._deadline(now)
        sample = self._sample(sample, now, starting=True)
        if sample.acquired_at_s < self.probe_enabled_s:
            raise PowerFeedbackError("Probe sample predates verified probe activation.")
        if not sample.current_source or abs(sample.current_a) > self.maximum_probe_current_a:
            raise PowerFeedbackError("Probe source mode or measured near-zero current is invalid.")
        return self._decide(sample, now, starting=True)
