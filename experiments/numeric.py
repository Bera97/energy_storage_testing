"""Hardware-free numeric checks used by experiment components.
Keep separate error policies where existing callers distinguish
invalid/non-finite input from an out-of-range finite value.
"""
import math
def finite_number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number.")
    try:
        result = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be a finite number.") from error
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number.")
    if positive and result <= 0:
        raise ValueError(f"{name} must be positive.")
    return result
def positive_number(value, name):
    result = finite_number(value, name)
    if result <= 0:
        raise ValueError(f"{name} must be positive.")
    return result
def positive_setting_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite and positive.")
    try:
        number = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be finite and positive.") from error
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be finite and positive.")
    return number
