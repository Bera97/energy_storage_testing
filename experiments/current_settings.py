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

    Battery charge basis:
        capacity_c_rate

    EDLC and LIC charge basis:
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

    if category == "battery":
        if charge_basis != "capacity_c_rate":
            raise ValueError(
                "Battery charging requires "
                "charge_basis=capacity_c_rate."
            )

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
                "EDLC and LIC charging require "
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