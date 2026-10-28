"""Resolve experiment current magnitudes without hardware access.

These checks establish consistency with user-supplied ratings.
They do not establish whether those ratings are correct or whether
an experiment is electrically safe.
"""

from dataclasses import dataclass
import math


CATEGORIES = ("battery", "edlc", "lic")

TERMINATION_REFERENCES = (
    "rated_max_charge_current",
    "configured_charge_current",
)


@dataclass(frozen=True)
class ResolvedCurrents:
    charge_current_a: float
    discharge_current_a: float
    charge_termination_current_a: float


def _positive_number(value, name):
    if isinstance(value, bool) or not isinstance(
        value, (int, float)
    ):
        raise ValueError(
            f"{name} must be a positive finite number."
        )

    try:
        result = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError(
            f"{name} must be a positive finite number."
        ) from error

    if not math.isfinite(result) or result <= 0:
        raise ValueError(
            f"{name} must be a positive finite number."
        )

    return result


def _fraction(value, name):
    result = _positive_number(value, name)

    if result > 1:
        raise ValueError(f"{name} must not exceed 1.")

    return result


def _positive_product(left, right, name):
    return _positive_number(left * right, name)


def resolve_currents(
    *,
    category,
    nominal_capacity_ah,
    maximum_charge_current_a,
    maximum_discharge_current_a,
    charge_basis,
    charge_factor,
    discharge_factor,
    termination_reference,
    termination_factor,
):
    """Resolve charge, discharge, and termination current magnitudes.

    All supported device categories may select:
        absolute_current
        capacity_c_rate, using explicit nominal_capacity_ah
        rated_max_charge_fraction

    Discharge:
        Fraction of maximum continuous discharge current.

    Charge termination:
        Fraction of either rated maximum charge current or the
        configured charge current.

    Factors are explicit inputs. Protocol defaults belong in the
    configuration layer rather than being hidden here.
    """
    if category not in CATEGORIES:
        raise ValueError(
            f"category must be one of {CATEGORIES}."
        )

    maximum_charge = _positive_number(
        maximum_charge_current_a,
        "maximum_charge_current_a",
    )
    maximum_discharge = _positive_number(
        maximum_discharge_current_a,
        "maximum_discharge_current_a",
    )

    if charge_basis == "absolute_current":
        charge_current = _positive_number(
            charge_factor, "charge_factor"
        )

    elif charge_basis == "capacity_c_rate":

        capacity = _positive_number(
            nominal_capacity_ah,
            "nominal_capacity_ah",
        )
        factor = _positive_number(
            charge_factor,
            "charge_factor",
        )
        charge_current = _positive_product(
            capacity,
            factor,
            "resolved charge current",
        )

    else:
        if charge_basis != "rated_max_charge_fraction":
            raise ValueError(
                "Fraction charging requires "
                "charge_basis=rated_max_charge_fraction."
            )

        factor = _fraction(charge_factor, "charge_factor")
        charge_current = _positive_product(
            maximum_charge,
            factor,
            "resolved charge current",
        )

    if charge_current > maximum_charge:
        raise ValueError(
            "Resolved charge current exceeds "
            "maximum_charge_current_a."
        )

    discharge_fraction = _fraction(
        discharge_factor,
        "discharge_factor",
    )
    discharge_current = _positive_product(
        maximum_discharge,
        discharge_fraction,
        "resolved discharge current",
    )

    if termination_reference not in TERMINATION_REFERENCES:
        raise ValueError(
            "termination_reference must be one of "
            f"{TERMINATION_REFERENCES}."
        )

    termination_fraction = _fraction(
        termination_factor,
        "termination_factor",
    )

    reference_current = (
        maximum_charge
        if termination_reference == "rated_max_charge_current"
        else charge_current
    )

    termination_current = _positive_product(
        reference_current,
        termination_fraction,
        "resolved termination current",
    )

    if termination_current >= charge_current:
        raise ValueError(
            "Charge termination current must be strictly below "
            "the configured charge current."
        )

    return ResolvedCurrents(
        charge_current_a=charge_current,
        discharge_current_a=discharge_current,
        charge_termination_current_a=termination_current,
    )

def parse_charge_current(
    selection,
    *,
    category,
    nominal_capacity_ah,
    maximum_charge_current_a,
):
    """Translate one explicit selection or a legacy basis/factor pair."""
    if not isinstance(selection, dict):
        raise ValueError("charge.current must be a mapping.")
    if not all(isinstance(key, str) for key in selection):
        raise ValueError("charge.current keys must be strings.")
    legacy = {"basis", "factor"}
    if legacy.intersection(selection):
        if set(selection) != legacy:
            raise ValueError(
                "Do not mix legacy basis/factor with current selections."
            )
        return selection["basis"], selection["factor"]
    allowed = {"current_a", "current_fraction", "c_rate"}
    unknown = set(selection) - allowed
    if unknown:
        raise ValueError(
            "charge.current unknown fields: "
            + ", ".join(sorted(unknown))
        )
    chosen = [
        key for key in allowed
        if selection.get(key) is not None
    ]
    if len(chosen) != 1:
        raise ValueError(
            "Set exactly one of current_a, current_fraction, or c_rate."
        )
    method = chosen[0]
    value = selection[method]
    if method == "current_a":
        current = _positive_number(value, "charge.current.current_a")
    elif method == "current_fraction":
        maximum = _positive_number(
            maximum_charge_current_a, "maximum_charge_current_a"
        )
        fraction = _fraction(
            value, "charge.current.current_fraction"
        )
        current = _positive_product(
            maximum, fraction, "resolved charge current"
        )
    else:
        capacity = _positive_number(
            nominal_capacity_ah, "nominal_capacity_ah"
        )
        rate = _positive_number(value, "charge.current.c_rate")
        current = _positive_product(
            capacity, rate, "resolved charge current"
        )
    return "absolute_current", current
