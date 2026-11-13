"""Hardware-free CC-CV charge-termination decisions.

No driver imports, hardware communication, timing sleeps, or file I/O.

Inputs:
- elapsed_s: monotonic elapsed time since charge-phase start.
- current_a: current expressed positive into the DUT.
- cv_active: explicit CV indication supplied by the backend.
- has_fault: documented measurement-fault indication.

The backend must establish valid CV detection and enforce voltage,
current, polarity, sampling-gap, and instrument operating limits.

A hold is established from consecutive qualifying observations and
their elapsed times. This does not prove continuous behavior between
measurements.

The backend/runner remains responsible for shutdown on every exit.
"""

from dataclasses import dataclass
import math


class ChargeFaultError(RuntimeError):
    """A charging measurement reported a fault."""


class ChargePolarityError(RuntimeError):
    """Current has the opposite sign to the controller convention."""


def _number(value, name, *, allow_zero=False):
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

    if result < 0 or (result == 0 and not allow_zero):
        requirement = "nonnegative" if allow_zero else "positive"
        raise ValueError(f"{name} must be {requirement}.")

    return result



def validate_cv_hold(value, maximum_cv_duration_s):
    """Validate an explicit timed-CV policy without I/O."""
    if not isinstance(value, dict) or set(value) != {"mode", "duration_s"}:
        raise ValueError("cv_hold must contain exactly mode and duration_s.")
    if value["mode"] not in ("from_cv_entry", "after_current_threshold"):
        raise ValueError(
            "cv_hold.mode must be from_cv_entry or after_current_threshold."
        )
    duration = _number(value["duration_s"], "cv_hold.duration_s")
    maximum = _number(maximum_cv_duration_s, "maximum_cv_duration_s")
    if duration >= maximum:
        raise ValueError("CV hold duration must be shorter than the CV timeout.")
    return {"mode": value["mode"], "duration_s": duration}

def resolve_charge_cv_hold(charge):
    """Resolve canonical termination or a legacy configuration without I/O.
    Canonical YAML uses termination.mode and termination.hold_time_s.
    Legacy charge.cv_hold is accepted only when termination.mode is absent.
    Internal phase dictionaries still carry the derived cv_hold policy.
    """
    termination = charge["termination"]
    if "mode" not in termination:
        if "cv_hold" in charge:
            return validate_cv_hold(
                charge["cv_hold"], charge["maximum_cv_duration_s"]
            )
        return None
    if "cv_hold" in charge:
        raise ValueError(
            "Configure charge termination in one place: remove charge.cv_hold "
            "when charge.termination.mode is present."
        )
    mode = termination["mode"]
    if mode not in (
        "current_threshold", "from_cv_entry", "after_current_threshold"
    ):
        raise ValueError(
            "charge.termination.mode must be current_threshold, "
            "from_cv_entry, or after_current_threshold."
        )
    if mode == "current_threshold":
        return None
    return validate_cv_hold(
        {"mode": mode, "duration_s": termination["hold_time_s"]},
        charge["maximum_cv_duration_s"],
    )

def charge_stop_reason(phase):
    return (
        "cv_hold_elapsed"
        if "cv_hold" in phase else "cv_current_termination"
    )
def charge_policy_evidence(monitor):
    """Unwrap measurement adapters without hardware or file operations."""
    current = monitor
    for _ in range(6):
        controller = getattr(current, "controller", None)
        if controller is not None:
            return getattr(controller, "cv_hold_evidence", None)
        following = getattr(current, "measurement_monitor", None)
        if following is None:
            following = getattr(current, "policy_monitor", None)
        if following is None or following is current:
            return None
        current = following
    raise RuntimeError("Charge monitor adapter chain is unexpectedly deep.")
def validate_charge_policy_evidence(phase, evidence):
    """Check persisted timed-hold consistency, not physical accuracy."""
    if "cv_hold" not in phase:
        return
    policy = validate_cv_hold(
        phase["cv_hold"], phase["maximum_cv_duration_s"]
    )
    if not isinstance(evidence, dict):
        raise ValueError("Timed CV completion requires charge_policy_evidence.")
    if (
        evidence.get("mode") != policy["mode"]
        or evidence.get("duration_s") != policy["duration_s"]
        or evidence.get("state") != "completed"
        or evidence.get("threshold_comparison") != "strictly_below"
        or evidence.get("cv_loss_policy") != "fail_after_timer_start"
    ):
        raise ValueError("Timed CV policy evidence differs from the phase.")
    start = _number(
        evidence.get("timer_started_elapsed_s"), "timer start", allow_zero=True
    )
    last = _number(
        evidence.get("last_elapsed_s"), "last elapsed", allow_zero=True
    )
    held = _number(
        evidence.get("timed_hold_elapsed_s"), "timed hold", allow_zero=True
    )
    first_cv = _number(
        evidence.get("first_cv_elapsed_s"), "first CV", allow_zero=True
    )
    if (
        last < start
        or start < first_cv
        or held < policy["duration_s"]
        or not math.isclose(last - start, held, rel_tol=1e-9, abs_tol=1e-9)
        or last >= phase["maximum_duration_s"]
        or last - first_cv >= phase["maximum_cv_duration_s"]
    ):
        raise ValueError("Timed CV elapsed/deadline evidence is inconsistent.")
    if policy["mode"] == "from_cv_entry":
        if start != first_cv:
            raise ValueError("Total CV timer must start at first confirmed CV.")
    else:
        crossing = _number(
            evidence.get("first_below_threshold_elapsed_s"),
            "first below-threshold CV sample",
            allow_zero=True,
        )
        if start != crossing:
            raise ValueError("Additional CV timer did not start at first crossing.")
    threshold = _number(evidence.get("threshold_current_a"), "threshold current")
    if threshold != phase["termination_current_a"]:
        raise ValueError("Recorded threshold differs from the phase.")
class ChargeCVHoldError(RuntimeError):
    """Confirmed CV was lost after a timed holding period started."""

@dataclass(frozen=True)
class ChargeDecision:
    state: str
    stop_reason: str | None
    elapsed_s: float
    first_cv_elapsed_s: float | None
    qualifying_hold_s: float


class ChargeController:
    """Single-use controller for one charge phase.

    This controller consumes the charge-phase dictionary produced
    by build_plan(). It does not modify that dictionary.

    CV duration is measured from first observed CV entry, including
    any subsequent excursions out of CV.

    Timeout takes priority over termination at the same timestamp.
    """

    def __init__(self, phase):
        if not isinstance(phase, dict):
            raise ValueError("phase must be a dictionary.")

        if phase.get("type") != "charge_cc_cv":
            raise ValueError("Expected a charge_cc_cv phase.")

        if phase.get("termination_requires_cv") is not True:
            raise ValueError("Charge termination must require CV.")

        self.charge_current_a = _number(
            phase.get("current_magnitude_a"),
            "current_magnitude_a",
        )
        self.termination_current_a = _number(
            phase.get("termination_current_a"),
            "termination_current_a",
        )
        self.hold_time_s = _number(
            phase.get("termination_hold_time_s"),
            "termination_hold_time_s",
        )
        self.maximum_duration_s = _number(
            phase.get("maximum_duration_s"),
            "maximum_duration_s",
        )
        self.maximum_cv_duration_s = _number(
            phase.get("maximum_cv_duration_s"),
            "maximum_cv_duration_s",
        )

        if self.termination_current_a >= self.charge_current_a:
            raise ValueError(
                "Termination current must be below charge current."
            )

        if self.maximum_cv_duration_s > self.maximum_duration_s:
            raise ValueError(
                "CV timeout must not exceed total charge timeout."
            )

        self.cv_hold_policy = (
            validate_cv_hold(phase["cv_hold"], self.maximum_cv_duration_s)
            if "cv_hold" in phase else None
        )
        if self.cv_hold_policy is None and self.hold_time_s >= self.maximum_cv_duration_s:
            raise ValueError(
                "Termination hold must be shorter than CV timeout."
            )

        self.state = "waiting"
        self._last_elapsed_s = None
        self._first_cv_elapsed_s = None
        self._hold_started_s = None
        self._timed_hold_started_s = None
        self._timed_hold_elapsed_s = 0.0
        self._first_below_threshold_elapsed_s = None

    @property
    def first_cv_elapsed_s(self):
        """First observed CV time in the controller clock."""
        return self._first_cv_elapsed_s


    @property
    def cv_hold_evidence(self):
        if self.cv_hold_policy is None:
            return None
        return {
            **self.cv_hold_policy,
            "state": self.state,
            "first_cv_elapsed_s": self._first_cv_elapsed_s,
            "first_below_threshold_elapsed_s": self._first_below_threshold_elapsed_s,
            "timer_started_elapsed_s": self._timed_hold_started_s,
            "timed_hold_elapsed_s": self._timed_hold_elapsed_s,
            "last_elapsed_s": self._last_elapsed_s,
            "threshold_current_a": self.termination_current_a,
            "threshold_comparison": "strictly_below",
            "cv_loss_policy": "fail_after_timer_start",
        }
    def update(
        self,
        *,
        elapsed_s,
        current_a,
        cv_active,
        has_fault=False,
    ):
        """Consume one observation and return a ChargeDecision.

        Any failed update leaves the controller terminal.
        A completed controller cannot be reused.

        Raises:
            TimeoutError: Total-charge or CV timeout reached.
            ChargeFaultError: Measurement fault reported.
            ChargePolarityError: Negative charging current supplied.
            ValueError: Invalid observation.
            RuntimeError: Controller already terminal.
        """
        if self.state in ("completed", "failed"):
            raise RuntimeError(
                "Charge controller is terminal; create a fresh one."
            )

        try:
            return self._update(
                elapsed_s=elapsed_s,
                current_a=current_a,
                cv_active=cv_active,
                has_fault=has_fault,
            )
        except Exception:
            self.state = "failed"
            raise

    def _update(
        self,
        *,
        elapsed_s,
        current_a,
        cv_active,
        has_fault,
    ):
        elapsed = _number(
            elapsed_s, "elapsed_s", allow_zero=True
        )

        if type(cv_active) is not bool:
            raise ValueError("cv_active must be boolean.")

        if type(has_fault) is not bool:
            raise ValueError("has_fault must be boolean.")

        if (
            self._last_elapsed_s is not None
            and elapsed <= self._last_elapsed_s
        ):
            raise ValueError("elapsed_s must increase strictly.")

        self._last_elapsed_s = elapsed

        if has_fault:
            raise ChargeFaultError(
                "Charging measurement reported a fault."
            )

        if elapsed >= self.maximum_duration_s:
            raise TimeoutError("Maximum charge duration reached.")

        if cv_active and self._first_cv_elapsed_s is None:
            self._first_cv_elapsed_s = elapsed

        if (
            self._first_cv_elapsed_s is not None
            and elapsed - self._first_cv_elapsed_s
            >= self.maximum_cv_duration_s
        ):
            raise TimeoutError("Maximum CV duration reached.")

        if (
            isinstance(current_a, (int, float))
            and not isinstance(current_a, bool)
            and current_a < 0
        ):
            raise ChargePolarityError(
                "Expected charging current positive into the DUT."
            )

        current = _number(
            current_a, "current_a", allow_zero=True
        )


        if (
            cv_active
            and current < self.termination_current_a
            and self._first_below_threshold_elapsed_s is None
        ):
            self._first_below_threshold_elapsed_s = elapsed
        if self.cv_hold_policy is not None:
            mode = self.cv_hold_policy["mode"]
            if self._timed_hold_started_s is None:
                if mode == "from_cv_entry" and cv_active:
                    self._timed_hold_started_s = elapsed
                elif (
                    mode == "after_current_threshold"
                    and cv_active
                    and current < self.termination_current_a
                ):
                    self._timed_hold_started_s = elapsed
            reason = None
            if self._timed_hold_started_s is not None:
                if not cv_active:
                    raise ChargeCVHoldError(
                        "Confirmed CV was lost after the timed hold started."
                    )
                self._timed_hold_elapsed_s = elapsed - self._timed_hold_started_s
                self.state = "timed_cv_hold"
                if self._timed_hold_elapsed_s >= self.cv_hold_policy["duration_s"]:
                    self.state = "completed"
                    reason = "cv_hold_elapsed"
            else:
                self.state = "cv" if cv_active else "cc"
            return ChargeDecision(
                state=self.state,
                stop_reason=reason,
                elapsed_s=elapsed,
                first_cv_elapsed_s=self._first_cv_elapsed_s,
                qualifying_hold_s=0.0,
            )
        qualifying = (
            cv_active
            and current <= self.termination_current_a
        )

        if qualifying:
            if self._hold_started_s is None:
                self._hold_started_s = elapsed

            hold = elapsed - self._hold_started_s
            self.state = "cv_hold"
        else:
            self._hold_started_s = None
            hold = 0.0
            self.state = "cv" if cv_active else "cc"

        reason = None

        if qualifying and hold >= self.hold_time_s:
            self.state = "completed"
            reason = "cv_current_termination"

        return ChargeDecision(
            state=self.state,
            stop_reason=reason,
            elapsed_s=elapsed,
            first_cv_elapsed_s=self._first_cv_elapsed_s,
            qualifying_hold_s=hold,
        )
