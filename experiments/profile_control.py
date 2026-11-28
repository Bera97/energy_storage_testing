"""Pure profile-discharge decisions: a stepped current or power profile repeated until cutoff.
No instrument or filesystem I/O. The controller proposes current setpoints (zero allowed) and
never applies them; the caller records evidence, verifies readbacks and cleans up on every exit.
It mirrors ConstantPowerController's interface so the shared engine and the offline replay can
drive either. Profile time starts at the first decision after the zero-current probe and steps
are whole seconds, evaluated once per update interval (at most 1 s).
"""
from dataclasses import asdict, dataclass
import math
from experiments.constant_power import (
    ConstantPowerController, PowerControlError, PowerDecision, PowerFeedbackError, PowerLimits,
    PowerSample, PowerTrackingError, _number, _terminal_on_error,
)
KINDS = ("current", "power")
MAXIMUM_STEPS = 100000
PARENT_FIELDS = tuple(PowerLimits.__dataclass_fields__)
@dataclass(frozen=True)
class ProfileLimits(PowerLimits):
    """PowerLimits plus the profile. power_w is a reference value (peak power), not a target."""
    profile_kind: str
    profile_steps: tuple
    zero_current_tolerance_a: float
    def __post_init__(self):
        for key in PARENT_FIELDS:
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
        if self.update_interval_s > 1.0:
            raise ValueError("update_interval_s must not exceed the 1 s profile step resolution.")
        if self.profile_kind not in KINDS:
            raise ValueError("profile_kind must be current or power.")
        tolerance = _number(self.zero_current_tolerance_a, "zero_current_tolerance_a")
        if tolerance > self.maximum_current_a:
            raise ValueError("zero_current_tolerance_a must not exceed the current ceiling.")
        object.__setattr__(self, "zero_current_tolerance_a", tolerance)
        steps = self.profile_steps
        if not isinstance(steps, (list, tuple)) or not 1 <= len(steps) <= MAXIMUM_STEPS:
            raise ValueError(f"profile_steps needs 1 to {MAXIMUM_STEPS} steps.")
        cleaned = []
        for index, step in enumerate(steps):
            if not isinstance(step, (list, tuple)) or len(step) != 2:
                raise ValueError(f"profile step {index} must be (duration_s, value).")
            duration, value = step
            if type(duration) is not int or duration < 1:
                raise ValueError(f"profile step {index} duration must be whole seconds >= 1.")
            value = _number(value, f"profile step {index} value", zero=True)
            if self.profile_kind == "current":
                if value > self.maximum_current_a:
                    raise ValueError(f"profile step {index} exceeds the current ceiling.")
            elif value / self.cutoff_voltage_v > self.maximum_current_a:
                raise ValueError(f"profile step {index} exceeds the current ceiling at cutoff.")
            cleaned.append((duration, value))
        if not any(value > 0 for _, value in cleaned):
            raise ValueError("The profile must contain at least one discharging step.")
        object.__setattr__(self, "profile_steps", tuple(cleaned))
    def to_dict(self):
        document = asdict(self)
        document["profile_steps"] = [[duration, value] for duration, value in self.profile_steps]
        return document
def profile_cycle_s(steps):
    return sum(duration for duration, _ in steps)
def profile_value_at(steps, elapsed_s):
    """Return (step_index, value) at elapsed_s; the profile repeats until the phase stops."""
    position = elapsed_s % profile_cycle_s(steps)
    end = 0
    for index, (duration, value) in enumerate(steps):
        end += duration
        if position < end:
            return index, value
    return len(steps) - 1, steps[-1][1]
@dataclass(frozen=True)
class ProfileDecision(PowerDecision):
    profile_step: int
    profile_elapsed_s: float
    requested_value: float
class ProfileController(ConstantPowerController):
    """Fresh host-timed feedback, explicit setpoint acknowledgement, zero-current steps allowed."""
    def __init__(self, limits):
        if not isinstance(limits, ProfileLimits):
            raise ValueError("A validated ProfileLimits instance is required.")
        super().__init__(limits)
        self.profile_started_s = None
        self.requested_value = None
        self.pending_target = None
        self.target_in_force = None
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
            raise PowerFeedbackError("Discharge compliance prevents valid profile control.")
        if not starting and (not sample.current_source
                             or current > self.limits.zero_current_tolerance_a):
            raise PowerFeedbackError("Wrong discharge source mode or current polarity.")
        self.last_sample_s = stamp
        return PowerSample(stamp, voltage, current, sample.current_source,
                           sample.in_compliance, sample.has_fault)
    def _profile_decision(self, action, sample, now, step, elapsed, value, *, current=None, reason=None):
        requested = value if self.limits.profile_kind == "power" else value * sample.voltage_v
        measured = _number(-sample.voltage_v * sample.current_a, "measured power", signed=True)
        error = _number(measured - requested, "power error", signed=True)
        return ProfileDecision(action, current, reason, requested, measured, error, now,
                               sample.acquired_at_s, step, elapsed, value)
    def _off_target(self, sample):
        target, limits = self.target_in_force, self.limits
        if limits.profile_kind == "power":
            measured = -sample.voltage_v * sample.current_a
            tolerance = max(limits.absolute_tolerance_w, limits.relative_tolerance * target)
        else:
            measured = -sample.current_a
            tolerance = max(limits.zero_current_tolerance_a, limits.relative_tolerance * target)
        return abs(measured - target) > tolerance
    def _decide(self, sample, now, *, starting=False):
        limits = self.limits
        if self.profile_started_s is None:
            self.profile_started_s = now
        elapsed = now - self.profile_started_s
        step, value = profile_value_at(limits.profile_steps, elapsed)
        if sample.voltage_v <= limits.cutoff_voltage_v:
            self.state = "completed"
            return self._profile_decision("stop", sample, now, step, elapsed, value,
                                          reason="voltage_min")
        if limits.profile_kind == "power":
            command = 0.0 if value == 0 else -value / sample.voltage_v
        else:
            command = 0.0 - value
        if not math.isfinite(command) or abs(command) > limits.maximum_current_a:
            raise PowerControlError("Requested profile step cannot be maintained within the current ceiling.")
        if (not starting and self.target_in_force is not None
                and now - self.started_s >= limits.startup_grace_s):
            if self._off_target(sample):
                if self.bad_tracking_since_s is None:
                    self.bad_tracking_since_s = now
                if now - self.bad_tracking_since_s >= limits.maximum_tracking_error_s:
                    raise PowerTrackingError("Profile tracking stayed outside tolerance.")
            else:
                self.bad_tracking_since_s = None
        changed = self.requested_value is None or value != self.requested_value
        due = self.last_request_s is None or now - self.last_request_s >= limits.update_interval_s
        if changed or (limits.profile_kind == "power" and value > 0 and due):
            self.pending_current_a = command
            self.pending_target = value
            self.requested_value = value
            self.last_request_s = now
            self.state = "awaiting_setpoint"
            return self._profile_decision("set_current", sample, now, step, elapsed, value,
                                          current=command)
        return self._profile_decision("hold", sample, now, step, elapsed, value)
    @_terminal_on_error
    def acknowledge_setpoint(self, *, now_s, readback_current_a):
        if self.state != "awaiting_setpoint":
            raise PowerControlError("No current command is awaiting acknowledgement.")
        now = self._now(now_s)
        self._deadline(now)
        current = _number(readback_current_a, "setpoint readback", signed=True)
        if (current > 0 or abs(current) > self.limits.maximum_current_a
                or not math.isclose(current, self.pending_current_a, rel_tol=1e-8, abs_tol=1e-12)):
            raise PowerFeedbackError("Current setpoint readback mismatch.")
        self.last_ack_s = now
        self.pending_current_a = None
        self.target_in_force = self.pending_target
        self.pending_target = None
        self.state = "active"
