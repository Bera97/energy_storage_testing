"""Hardware-free CV qualification for voltage-source charging.

No driver imports, hardware communication, or file operations.

B2900B measurement status:
- Bit 0: source selection; 0=voltage source, 1=current source.
- Bits 1 and 2: compliance indication.

Voltage-source selection alone is not proof of CV regulation.

This policy qualifies an observation only when:
- No documented measurement fault is reported.
- Voltage-source selection is reported.
- Current has the positive-into-DUT charging sign.
- The configured software voltage ceiling is not exceeded.
- No compliance indication is present.
- Voltage is within the configured target band.

The hardware backend must verify actual source configuration,
readbacks, operating limits, wiring, and protection settings.
"""


import math

from experiments.charge_controller import (
    ChargeFaultError,
    ChargePolarityError,
)


class ChargeSourceModeError(RuntimeError):
    """A record reports current-source rather than voltage-source selection."""


class ChargeVoltageError(RuntimeError):
    """A charging record exceeds the configured software voltage ceiling."""


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


class VoltageSourceCVClassifier:
    """Callable CV-qualification policy for one charge phase.

    Expected record attributes:
        voltage_v
        current_a
        status
        has_fault

    Current must already follow the positive-into-DUT convention.
    """

    def __init__(
        self,
        *,
        target_voltage_v,
        voltage_tolerance_v,
        maximum_voltage_v,
    ):
        target = _finite_number(
            target_voltage_v, "target_voltage_v"
        )
        tolerance = _finite_number(
            voltage_tolerance_v, "voltage_tolerance_v"
        )
        maximum = _finite_number(
            maximum_voltage_v, "maximum_voltage_v"
        )

        if target <= 0:
            raise ValueError("target_voltage_v must be positive.")

        if not 0 < tolerance < target:
            raise ValueError(
                "voltage_tolerance_v must be positive "
                "and below target_voltage_v."
            )

        if maximum < target:
            raise ValueError(
                "maximum_voltage_v must be at least "
                "target_voltage_v."
            )

        self.target_voltage_v = target
        self.voltage_tolerance_v = tolerance
        self.maximum_voltage_v = maximum

    def __call__(self, reading):
        status = reading.status

        if (
            type(status) is not int
            or not 0 <= status <= 0xFFFFFFFF
        ):
            raise ValueError(
                "Reading status must be an unsigned 32-bit integer."
            )

        if type(reading.has_fault) is not bool:
            raise ValueError("Reading has_fault must be boolean.")

        if reading.has_fault:
            raise ChargeFaultError(
                "Charging measurement reported a fault."
            )

        voltage = _finite_number(
            reading.voltage_v, "reading.voltage_v"
        )
        current = _finite_number(
            reading.current_a, "reading.current_a"
        )

        if status & 1:
            raise ChargeSourceModeError(
                "Charging record reports current-source selection; "
                "this policy requires voltage-source selection."
            )

        if current < 0:
            raise ChargePolarityError(
                "Expected charging current positive into the DUT."
            )

        if voltage < 0:
            raise ChargeVoltageError(
                "Charging record has negative terminal voltage."
            )

        if voltage > self.maximum_voltage_v:
            raise ChargeVoltageError(
                "Charging voltage exceeds the configured "
                "software ceiling."
            )

        in_compliance = bool(status & 0b110)

        if in_compliance:
            return False

        return (
            abs(voltage - self.target_voltage_v)
            <= self.voltage_tolerance_v
        )