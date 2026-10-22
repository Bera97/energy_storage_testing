"""Hardware-free constant-current discharge decisions.

No driver imports, hardware communication, sleeps, or file I/O.

Current convention:
    Negative current into the DUT means discharge.

The backend must separately enforce instrument/DUT operating limits,
connection checks, sampling-gap policy, and shutdown on every exit.

An observed cutoff is a software endpoint, not proof of a valid
capacity experiment or an electrically isolated DUT.
"""

from dataclasses import dataclass
import math


class DischargeFaultError(RuntimeError):
    """A discharge measurement reported a fault."""


class DischargePolarityError(RuntimeError):
    """Measured current flows in the charging direction."""


class DischargeComplianceError(RuntimeError):
    """Unexpected compliance occurred during constant-current discharge."""


class DischargeVoltageError(RuntimeError):
    """Measured terminal voltage has invalid polarity."""


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


@dataclass(frozen=True)
class DischargeDecision:
    state: str
    stop_reason: str | None
    elapsed_s: float
    voltage_v: float


class DischargeController:
    """Single-use endpoint controller for one discharge phase.

    Consumes the discharge-phase dictionary from build_plan().

    Timeout takes priority over cutoff at the same observation time.
    Fault, polarity, and compliance failures are not relabelled as
    successful voltage-cutoff termination.
    """

    def __init__(self, phase):
        if not isinstance(phase, dict):
            raise ValueError("phase must be a dictionary.")

        if phase.get("type") != "discharge_cc":
            raise ValueError("Expected a discharge_cc phase.")

        self.current_magnitude_a = _positive_number(
            phase.get("current_magnitude_a"),
            "current_magnitude_a",
        )

        self.cutoff_voltage_v = _finite_number(
            phase.get("cutoff_voltage_v"),
            "cutoff_voltage_v",
        )

        if self.cutoff_voltage_v < 0:
            raise ValueError(
                "cutoff_voltage_v must be nonnegative."
            )

        self.maximum_duration_s = _positive_number(
            phase.get("maximum_duration_s"),
            "maximum_duration_s",
        )

        self.state = "waiting"
        self._last_elapsed_s = None

    def update(
        self,
        *,
        elapsed_s,
        voltage_v,
        current_a,
        in_compliance=False,
        has_fault=False,
    ):
        """Consume one measurement and return a DischargeDecision.

        Any failed update leaves the controller terminal.

        This measurement-based timeout is supplemented by the
        future acquisition loop's host-monotonic deadline.
        """
        if self.state in ("completed", "failed"):
            raise RuntimeError(
                "Discharge controller is terminal; "
                "create a fresh one."
            )

        try:
            return self._update(
                elapsed_s=elapsed_s,
                voltage_v=voltage_v,
                current_a=current_a,
                in_compliance=in_compliance,
                has_fault=has_fault,
            )
        except Exception:
            self.state = "failed"
            raise

    def _update(
        self,
        *,
        elapsed_s,
        voltage_v,
        current_a,
        in_compliance,
        has_fault,
    ):
        elapsed = _finite_number(elapsed_s, "elapsed_s")

        if elapsed < 0:
            raise ValueError("elapsed_s must be nonnegative.")

        if type(has_fault) is not bool:
            raise ValueError("has_fault must be boolean.")

        if type(in_compliance) is not bool:
            raise ValueError("in_compliance must be boolean.")

        if (
            self._last_elapsed_s is not None
            and elapsed <= self._last_elapsed_s
        ):
            raise ValueError("elapsed_s must increase strictly.")

        self._last_elapsed_s = elapsed

        if has_fault:
            raise DischargeFaultError(
                "Discharge measurement reported a fault."
            )

        if elapsed >= self.maximum_duration_s:
            raise TimeoutError(
                "Maximum discharge duration reached."
            )

        voltage = _finite_number(voltage_v, "voltage_v")
        current = _finite_number(current_a, "current_a")

        if voltage < 0:
            raise DischargeVoltageError(
                "Discharge measurement has negative terminal voltage."
            )

        if current > 0:
            raise DischargePolarityError(
                "Expected discharge current negative into the DUT."
            )

        if in_compliance:
            raise DischargeComplianceError(
                "Unexpected compliance during constant-current "
                "discharge."
            )

        reason = None

        if voltage <= self.cutoff_voltage_v:
            self.state = "completed"
            reason = "voltage_min"
        else:
            self.state = "discharging"

        return DischargeDecision(
            state=self.state,
            stop_reason=reason,
            elapsed_s=elapsed,
            voltage_v=voltage,
        )