"""Outputs-OFF configuration of voltage-source CC-CV charging.

No output-ON or acquisition-initiate command is sent.

This helper verifies configuration consistency, not the complete
instrument operating envelope, physical wiring, DUT condition,
protection configuration, or electrical isolation.

A successful return does not authorize cell execution.

Caller must retain an outer shutdown boundary around configuration
and all subsequent operations. Configuration is not transactional:
a failure can leave partially changed settings.
"""

import math

from experiments.config import (
    require_charge_monitoring_settings,
    validate_config,
)


class ChargeConfigurationError(RuntimeError):
    """Instrument readback does not match required charge settings."""


def _finite_readback(value, name):
    if isinstance(value, bool) or not isinstance(
        value, (int, float)
    ):
        raise ChargeConfigurationError(
            f"{name} readback must be a finite number."
        )

    try:
        result = float(value)
    except (ValueError, OverflowError) as error:
        raise ChargeConfigurationError(
            f"{name} readback must be a finite number."
        ) from error

    if not math.isfinite(result):
        raise ChargeConfigurationError(
            f"{name} readback must be a finite number."
        )

    return result


def configure_charge_source(smu, document):
    """Configure and verify one voltage-source charge channel.

    Configuration validation occurs before any instrument access.

    Returns:
        JSON-compatible configuration evidence. This is not an
        execution authorization or a complete hardware preflight.
    """
    validated = validate_config(document)
    monitoring = require_charge_monitoring_settings(document)

    config = validated["config"]
    currents = validated["resolved_currents"]

    number = config["instrument"]["channel"]
    selected = smu.chan1 if number == 1 else smu.chan2

    target = float(config["charge"]["target_voltage_v"])
    charge_current = currents["charge_current_a"]

    ratings = config["dut"]["ratings"]
    rated_voltage = float(ratings["maximum_voltage_v"])
    rated_charge_current = float(
        ratings["maximum_continuous_charge_current_a"]
    )
    four_wire = config["instrument"]["four_wire"]

    def checked(operation, *arguments):
        result = operation(*arguments)
        smu.check_errors()
        return result

    def require_outputs_off():
        for channel in (smu.chan1, smu.chan2):
            state = checked(channel.get_output_state)

            if state is not False:
                raise ChargeConfigurationError(
                    f"Channel {channel.channel} must explicitly "
                    "report output OFF."
                )

    def require_text(command, expected):
        response = checked(smu._query, command)

        if response.strip().upper() != expected:
            raise ChargeConfigurationError(
                f"{command}: expected {expected!r}, "
                f"received {response!r}."
            )

    smu.check_errors()
    require_outputs_off()

    checked(smu.abort)

    for channel in (smu.chan1, smu.chan2):
        checked(channel.set_auto_output_on, False)

    checked(selected.set_output_off_mode, "HIZ")
    checked(selected.configure_fixed_dc, "VOLT")
    checked(selected.set_voltage, target)
    checked(selected.set_current_limit, charge_current)
    checked(selected.set_four_wire, four_wire)

    source_mode = checked(selected.get_source_mode)

    if source_mode != "VOLT":
        raise ChargeConfigurationError(
            "Selected channel did not report VOLT source mode."
        )

    require_text(f":SOUR{number}:FUNC:SHAP?", "DC")
    require_text(f":SOUR{number}:VOLT:MODE?", "FIX")

    actual_voltage = _finite_readback(
        checked(selected.get_voltage_setpoint),
        "voltage setpoint",
    )
    actual_current = _finite_readback(
        checked(selected.get_current_limit),
        "current compliance",
    )

    if not math.isclose(
        actual_voltage,
        target,
        rel_tol=1e-9,
        abs_tol=1e-12,
    ):
        raise ChargeConfigurationError(
            "Voltage setpoint readback differs from the target."
        )

    if not math.isclose(
        actual_current,
        charge_current,
        rel_tol=1e-9,
        abs_tol=1e-15,
    ):
        raise ChargeConfigurationError(
            "Current compliance readback differs from "
            "the configured charging current."
        )

    if not 0 < actual_voltage <= rated_voltage:
        raise ChargeConfigurationError(
            "Voltage setpoint readback exceeds the supplied "
            "DUT voltage rating or is not positive."
        )

    if not 0 < actual_current <= rated_charge_current:
        raise ChargeConfigurationError(
            "Current compliance readback exceeds the supplied "
            "DUT charge-current rating or is not positive."
        )

    actual_four_wire = checked(selected.get_four_wire)

    if actual_four_wire is not four_wire:
        raise ChargeConfigurationError(
            "Sense-wiring configuration readback differs."
        )

    off_mode = checked(selected.get_output_off_mode)

    if off_mode != "HIZ":
        raise ChargeConfigurationError(
            "Selected output-off mode did not read back as HIZ."
        )

    for channel in (smu.chan1, smu.chan2):
        automatic = checked(channel.get_auto_output_on)

        if automatic is not False:
            raise ChargeConfigurationError(
                f"Channel {channel.channel}: automatic "
                "output-on did not read back as OFF."
            )

    require_outputs_off()

    return {
        "channel": number,
        "source_function": source_mode,
        "source_shape": "DC",
        "source_operation": "FIX",
        "voltage_setpoint_v": actual_voltage,
        "current_compliance_a": actual_current,
        "four_wire": actual_four_wire,
        "selected_output_off_mode": off_mode,
        "both_outputs_reported_off": True,
        "both_auto_output_on_reported_off": True,
        "charge_monitoring": monitoring,
        "operating_envelope_verified": False,
        "hardware_execution_authorized": False,
    }