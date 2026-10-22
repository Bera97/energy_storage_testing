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

        if self.hold_time_s >= self.maximum_cv_duration_s:
            raise ValueError(
                "Termination hold must be shorter than CV timeout."
            )

        self.state = "waiting"
        self._last_elapsed_s = None
        self._first_cv_elapsed_s = None
        self._hold_started_s = None

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