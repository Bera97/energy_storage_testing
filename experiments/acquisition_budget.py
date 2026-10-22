"""Hardware-free sizing of finite experiment acquisitions.

No instrument access or file operations.

The record limit is an explicit planning input. Planning against
a larger limit does not change or validate driver capabilities.

Nominal span = (record_count - 1) * interval_s.

The backend must separately enforce elapsed-time deadlines and
treat count exhaustion before a valid endpoint as unsuccessful.
"""

from dataclasses import asdict, dataclass
import math

from experiments.preconditioning import build_plan


@dataclass(frozen=True)
class AcquisitionBudget:
    maximum_duration_s: float
    interval_s: float
    record_count: int
    nominal_span_s: float
    maximum_records: int


def _positive_number(value, name):
    if isinstance(value, bool) or not isinstance(
        value, (int, float)
    ):
        raise ValueError(f"{name} must be finite and positive.")

    try:
        number = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError(
            f"{name} must be finite and positive."
        ) from error

    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be finite and positive.")

    return number


def calculate_budget(
    maximum_duration_s,
    interval_s,
    *,
    maximum_records,
):
    """Return a finite acquisition budget or reject an oversized phase."""
    duration = _positive_number(
        maximum_duration_s,
        "maximum_duration_s",
    )
    interval = _positive_number(interval_s, "interval_s")

    if (
        type(maximum_records) is not int
        or maximum_records < 2
    ):
        raise ValueError(
            "maximum_records must be an integer of at least 2."
        )

    intervals_needed = duration / interval

    if not math.isfinite(intervals_needed):
        raise ValueError("Requested acquisition is too large.")

    count = max(2, math.ceil(intervals_needed) + 1)

    if count > maximum_records:
        raise ValueError(
            f"Phase requires {count} records, exceeding "
            f"the planning limit of {maximum_records}. "
            "Increase interval_s or revise the phase duration."
        )

    span = (count - 1) * interval

    # Conservatively cover a floating-point rounding shortfall.
    if span < duration:
        count += 1

        if count > maximum_records:
            raise ValueError(
                "Covering the phase duration would exceed "
                "the record limit."
            )

        span = (count - 1) * interval

    if not math.isfinite(span):
        raise ValueError("Nominal acquisition span is not finite.")

    return AcquisitionBudget(
        maximum_duration_s=duration,
        interval_s=interval,
        record_count=count,
        nominal_span_s=span,
        maximum_records=maximum_records,
    )


def build_acquisition_plan(document, *, maximum_records):
    """Add acquisition budgets to active phases of a validated plan.

    Rest phases do not receive a measurement acquisition budget.
    The caller's document is not modified.
    """
    plan = build_plan(document)
    interval = plan["configuration"]["acquisition"]["interval_s"]

    for phase in plan["phases"]:
        if phase["type"] == "rest":
            continue

        budget = calculate_budget(
            phase["maximum_duration_s"],
            interval,
            maximum_records=maximum_records,
        )
        phase["acquisition_budget"] = asdict(budget)

    plan["acquisition_planning_limit"] = maximum_records
    return plan