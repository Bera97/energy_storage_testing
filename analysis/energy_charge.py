"""Offline charge and energy calculations.

No instrument-driver imports, hardware communication, or file operations.

Uses actual timestamp differences and trapezoidal integration.
Power is calculated as voltage * current at each sample.

Signed quantities retain the signs of the supplied measurements.

Positive and negative quantities are nonnegative magnitudes:
    signed charge = positive charge - negative charge
    signed energy = positive energy - negative energy

Direction-separated integration assumes linear interpolation of:
    current, for charge
    sampled power, for energy

Intervals with a sign change are split at the interpolated zero crossing.

No device-specific charging/discharging interpretation is applied.
Integration covers only the first-to-last recorded timestamp.
No extrapolation, gap exclusion, or measurement filtering.
"""

import math


ANALYSIS_VERSION = 2


def _finite_values(values, name):
    try:
        result = [float(value) for value in values]
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(
            f"{name} must contain finite numeric values."
        ) from error

    if not all(math.isfinite(value) for value in result):
        raise ValueError(
            f"{name} must contain finite numeric values."
        )

    return result


def _finite_result(value):
    if not math.isfinite(value):
        raise ValueError(
            "Calculation produced a non-finite result."
        )
    return value


def _integrate(values, intervals):
    cumulative = [0.0]

    for index in range(1, len(values)):
        mean = (
            0.5 * values[index - 1]
            + 0.5 * values[index]
        )
        increment = _finite_result(
            mean * intervals[index]
        )
        cumulative.append(
            _finite_result(cumulative[-1] + increment)
        )

    return cumulative


def _directional_interval(first, second, duration):
    """Return positive and negative area magnitudes for one interval."""
    if first >= 0 and second >= 0:
        positive = _finite_result(
            (0.5 * first + 0.5 * second) * duration
        )
        return positive, 0.0

    if first <= 0 and second <= 0:
        negative = _finite_result(
            (0.5 * abs(first) + 0.5 * abs(second))
            * duration
        )
        return 0.0, negative

    first_magnitude = abs(first)
    second_magnitude = abs(second)

    scale = max(first_magnitude, second_magnitude)
    scaled_first = first_magnitude / scale
    scaled_second = second_magnitude / scale
    scaled_sum = scaled_first + scaled_second

    first_fraction = scaled_first / scaled_sum
    second_fraction = scaled_second / scaled_sum

    first_area = _finite_result(
        (0.5 * first_magnitude)
        * (duration * first_fraction)
    )
    second_area = _finite_result(
        (0.5 * second_magnitude)
        * (duration * second_fraction)
    )

    if first > 0:
        return first_area, second_area

    return second_area, first_area


def _integrate_directions(values, intervals):
    positive = [0.0]
    negative = [0.0]

    for index in range(1, len(values)):
        positive_increment, negative_increment = (
            _directional_interval(
                values[index - 1],
                values[index],
                intervals[index],
            )
        )

        positive.append(
            _finite_result(
                positive[-1] + positive_increment
            )
        )
        negative.append(
            _finite_result(
                negative[-1] + negative_increment
            )
        )

    return positive, negative


def calculate_energy_charge(
    timestamps_s,
    voltage_v,
    current_a,
):
    """Return JSON-compatible cumulative quantities and summary.

    Inputs must have equal, nonzero lengths and finite numeric values.
    Timestamps must increase strictly.

    The first sample has dt_s=None. Cumulative quantities start at zero.

    One sample is accepted, but provides no integration interval:
    recorded_span_s is zero and time-weighted means are None.

    Positive and negative totals are nonnegative magnitudes, not
    device-labelled delivered/absorbed quantities.

    Raises:
        ValueError: Invalid inputs or non-finite calculation results.
    """
    timestamps = _finite_values(
        timestamps_s, "timestamps_s"
    )
    voltages = _finite_values(voltage_v, "voltage_v")
    currents = _finite_values(current_a, "current_a")

    count = len(timestamps)

    if count == 0:
        raise ValueError("At least one sample is required.")

    if len(voltages) != count or len(currents) != count:
        raise ValueError(
            "Timestamp, voltage, and current lengths must match."
        )

    intervals = [None]

    for index in range(1, count):
        interval = _finite_result(
            timestamps[index] - timestamps[index - 1]
        )

        if interval <= 0:
            raise ValueError(
                "Timestamps must increase strictly."
            )

        intervals.append(interval)

    elapsed = [
        _finite_result(timestamp - timestamps[0])
        for timestamp in timestamps
    ]
    power = [
        _finite_result(voltage * current)
        for voltage, current in zip(voltages, currents)
    ]

    charge_c = _integrate(currents, intervals)
    energy_j = _integrate(power, intervals)
    voltage_integral = _integrate(voltages, intervals)

    positive_charge_c, negative_charge_c = (
        _integrate_directions(currents, intervals)
    )
    positive_energy_j, negative_energy_j = (
        _integrate_directions(power, intervals)
    )

    duration = elapsed[-1]

    samples = []

    for index in range(count):
        samples.append({
            "sample_index": index,
            "elapsed_time_s": elapsed[index],
            "dt_s": intervals[index],
            "signed_power_w": power[index],
            "cumulative_signed_charge_c": charge_c[index],
            "cumulative_signed_charge_mah": (
                charge_c[index] / 3.6
            ),
            "cumulative_signed_energy_j": energy_j[index],
            "cumulative_signed_energy_wh": (
                energy_j[index] / 3600.0
            ),
            "cumulative_positive_charge_c": (
                positive_charge_c[index]
            ),
            "cumulative_positive_charge_mah": (
                positive_charge_c[index] / 3.6
            ),
            "cumulative_negative_charge_c": (
                negative_charge_c[index]
            ),
            "cumulative_negative_charge_mah": (
                negative_charge_c[index] / 3.6
            ),
            "cumulative_positive_energy_j": (
                positive_energy_j[index]
            ),
            "cumulative_positive_energy_wh": (
                positive_energy_j[index] / 3600.0
            ),
            "cumulative_negative_energy_j": (
                negative_energy_j[index]
            ),
            "cumulative_negative_energy_wh": (
                negative_energy_j[index] / 3600.0
            ),
        })

    summary = {
        "sample_count": count,
        "interval_count": count - 1,
        "first_instrument_timestamp_s": timestamps[0],
        "last_instrument_timestamp_s": timestamps[-1],
        "recorded_span_s": duration,
        "minimum_sample_interval_s": (
            min(intervals[1:]) if count > 1 else None
        ),
        "maximum_sample_interval_s": (
            max(intervals[1:]) if count > 1 else None
        ),
        "minimum_voltage_v": min(voltages),
        "maximum_voltage_v": max(voltages),
        "minimum_current_a": min(currents),
        "maximum_current_a": max(currents),
        "signed_charge_c": charge_c[-1],
        "signed_charge_mah": charge_c[-1] / 3.6,
        "signed_energy_j": energy_j[-1],
        "signed_energy_wh": energy_j[-1] / 3600.0,
        "positive_charge_c": positive_charge_c[-1],
        "positive_charge_mah": positive_charge_c[-1] / 3.6,
        "negative_charge_c": negative_charge_c[-1],
        "negative_charge_mah": negative_charge_c[-1] / 3.6,
        "positive_energy_j": positive_energy_j[-1],
        "positive_energy_wh": positive_energy_j[-1] / 3600.0,
        "negative_energy_j": negative_energy_j[-1],
        "negative_energy_wh": negative_energy_j[-1] / 3600.0,
        "time_weighted_mean_voltage_v": (
            _finite_result(voltage_integral[-1] / duration)
            if duration > 0
            else None
        ),
        "time_weighted_mean_signed_power_w": (
            _finite_result(energy_j[-1] / duration)
            if duration > 0
            else None
        ),
    }

    warnings = []

    if count == 1:
        warnings.append(
            "One sample provides no integration interval; "
            "zero totals do not establish zero energy exchange."
        )

    return {
        "analysis_version": ANALYSIS_VERSION,
        "integration_method": "trapezoidal",
        "power_method": "sample_voltage_times_sample_current",
        "directional_integration_method": (
            "Piecewise-linear current and sampled power; "
            "sign-changing intervals split at zero crossings."
        ),
        "sign_convention": (
            "Input current sign retained; signed power = V*I. "
            "Positive and negative totals are nonnegative "
            "magnitudes. Signed = positive - negative, "
            "subject to floating-point rounding. "
            "No device charging/discharging interpretation."
        ),
        "coverage_policy": (
            "First-to-last recorded timestamp only; "
            "all adjacent intervals included without extrapolation."
        ),
        "excluded_intervals": [],
        "warnings": warnings,
        "samples": samples,
        "summary": summary,
    }