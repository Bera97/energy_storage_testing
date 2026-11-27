"""Host-controlled, step-down-only current measurement ranging for the leakage hold.

With automatic ranging the SMU switches current ranges repeatedly as the current decays,
and each switch shifts the delivered voltage by about 0.1 mV. On a large capacitive DUT
that moves the current by orders of magnitude. This controller keeps the range fixed and
steps DOWN only after the current has stayed well inside the lower range. It steps UP
only after a measured overflow, then refuses to step down again for a lockout period.
"""
from collections import deque
import math

RANGE_LADDER = (1e-8, 1e-7, 1e-6, 1e-5, 1e-4, 1e-3, 1e-2, 1e-1, 1.0)
MODES = ("auto", "step_down")
DEFAULTS = {
    "mode": "auto", "minimum_range_a": 1e-7, "headroom": 0.2, "confirm_samples": 20,
    "confirm_window_s": 60.0, "overflow_lockout_s": 3600.0, "maximum_range_increases": 10,
}
MAXIMUM_CONFIRM_WINDOW_S = 3600.0
TAKEOVER_HEADROOM = 0.8
READBACK_RATIO = (0.9, 2.5)


class RangingError(RuntimeError):
    """Range control failed or the current left the highest allowed range."""


def validate_range_control(value):
    """Return a complete range_control mapping; reject unknown or invalid fields."""
    from experiments.config import _mapping, _number
    if value is None:
        return dict(DEFAULTS)
    given = _mapping(value, "leakage.range_control", required=(), optional=tuple(DEFAULTS))
    mode = given.get("mode", DEFAULTS["mode"])
    if mode not in MODES:
        raise ValueError("leakage.range_control.mode must be auto or step_down.")
    minimum = _number(given.get("minimum_range_a", DEFAULTS["minimum_range_a"]),
                      "leakage.range_control.minimum_range_a")
    if not any(math.isclose(minimum, rung, rel_tol=1e-9) for rung in RANGE_LADDER):
        raise ValueError("leakage.range_control.minimum_range_a must be one of: "
                         + ", ".join(f"{rung:g}" for rung in RANGE_LADDER) + ".")
    headroom = _number(given.get("headroom", DEFAULTS["headroom"]), "leakage.range_control.headroom")
    if headroom > 0.9:
        raise ValueError("leakage.range_control.headroom must be at most 0.9.")
    confirm = given.get("confirm_samples", DEFAULTS["confirm_samples"])
    if type(confirm) is not int or not 1 <= confirm <= 100000:
        raise ValueError("leakage.range_control.confirm_samples must be an integer from 1 to 100000.")
    window = _number(given.get("confirm_window_s", DEFAULTS["confirm_window_s"]),
                     "leakage.range_control.confirm_window_s", allow_zero=True)
    if window > MAXIMUM_CONFIRM_WINDOW_S:
        raise ValueError("leakage.range_control.confirm_window_s must be at most 3600.")
    lockout = _number(given.get("overflow_lockout_s", DEFAULTS["overflow_lockout_s"]),
                      "leakage.range_control.overflow_lockout_s", allow_zero=True)
    increases = given.get("maximum_range_increases", DEFAULTS["maximum_range_increases"])
    if type(increases) is not int or not 0 <= increases <= 1000:
        raise ValueError("leakage.range_control.maximum_range_increases must be an integer from 0 to 1000.")
    return {"mode": mode, "minimum_range_a": minimum, "headroom": headroom,
            "confirm_samples": confirm, "confirm_window_s": window,
            "overflow_lockout_s": lockout,
            "maximum_range_increases": increases}


class StepDownRanger:
    """Track one manual current range; never steps up except via overflow()."""

    def __init__(self, control):
        floor = control["minimum_range_a"] * (1 - 1e-9)
        self.control = control
        self.ladder = tuple(rung for rung in RANGE_LADDER if rung >= floor)
        self.range_a = None
        self.recent = deque()  # (offset_s, absolute current)
        self.floor_a = None
        self.floor_until_s = -math.inf
        self.increases = 0

    def takeover(self, current_a):
        """Choose the first manual range from a settled (non-compliance) reading."""
        need = abs(current_a) / TAKEOVER_HEADROOM
        self.range_a = next((rung for rung in self.ladder if rung >= need), self.ladder[-1])
        self.recent.clear()
        return self.range_a

    def observe(self, current_a, offset_s):
        """Record a good reading; return a lower range to step down to, or None."""
        self.recent.append((offset_s, abs(current_a)))
        window = self.control.get("confirm_window_s", 0.0)
        keep = self.control["confirm_samples"]
        while len(self.recent) > keep and (window <= 0 or self.recent[1][0] <= offset_s - window):
            self.recent.popleft()
        if len(self.recent) < keep:
            return None
        if window > 0 and offset_s - self.recent[0][0] < window * (1 - 1e-9):
            return None
        peak = max(value for _, value in self.recent)
        locked = offset_s < self.floor_until_s
        for rung in self.ladder:
            if rung >= self.range_a * (1 - 1e-9):
                break
            if locked and rung < self.floor_a * (1 - 1e-9):
                continue
            if peak <= self.control["headroom"] * rung:
                self.range_a = rung
                self.recent.clear()
                return rung
        return None

    def overflow(self, offset_s):
        """Step up one rung after a measured overflow; raise if impossible or too frequent."""
        higher = [rung for rung in self.ladder if rung > self.range_a * (1 + 1e-9)]
        if not higher:
            raise RangingError("Measurement overflow at the highest allowed current range.")
        self.increases += 1
        if self.increases > self.control["maximum_range_increases"]:
            raise RangingError(
                f"More than {self.control['maximum_range_increases']} range increases; stopping.")
        self.range_a = higher[0]
        self.floor_a = higher[0]
        self.floor_until_s = offset_s + self.control["overflow_lockout_s"]
        self.recent.clear()
        return self.range_a


def apply_current_range(smu, channel, range_a):
    """Fix the current measurement range and verify the readback; return the readback."""
    value = 0.5 * range_a
    smu._write(f":SENS{channel}:CURR:RANG:AUTO OFF")
    smu.check_errors()
    smu._write(f":SENS{channel}:CURR:RANG {value:.12g}")
    smu.check_errors()
    auto = str(smu._query(f":SENS{channel}:CURR:RANG:AUTO?")).strip().upper()
    smu.check_errors()
    response = smu._query(f":SENS{channel}:CURR:RANG?")
    smu.check_errors()
    if auto not in ("0", "OFF"):
        raise RangingError("Automatic current ranging did not read back OFF.")
    try:
        readback = float(response)
    except ValueError as error:
        raise RangingError(f"Invalid current range readback: {response!r}.") from error
    ratio = readback / value
    if not READBACK_RATIO[0] <= ratio <= READBACK_RATIO[1]:
        raise RangingError(
            f"Current range readback {readback:g} A does not match the requested {range_a:g} A range.")
    return readback


def enable_auto_range(smu, channel):
    """Switch current measurement ranging back to AUTO and verify the readback."""
    smu._write(f":SENS{channel}:CURR:RANG:AUTO ON")
    smu.check_errors()
    auto = str(smu._query(f":SENS{channel}:CURR:RANG:AUTO?")).strip().upper()
    smu.check_errors()
    if auto not in ("1", "ON"):
        raise RangingError("Automatic current ranging did not read back ON.")
