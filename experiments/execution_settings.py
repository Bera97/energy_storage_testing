"""YAML execution settings; no hardware imports or interactive prompts."""
from datetime import datetime, timezone
import math
from uuid import uuid4

FIELDS = ("expected_serial", "maximum_voltage_v", "maximum_current_a")


def validate_execution(value):
    if not isinstance(value, dict) or set(value) != set(FIELDS):
        raise ValueError("execution must contain exactly: " + ", ".join(FIELDS))
    serial = value["expected_serial"]
    if not isinstance(serial, str) or not serial.strip():
        raise ValueError("execution.expected_serial must be a nonempty string.")
    if serial.strip().startswith("REPLACE_") or "," in serial:
        raise ValueError("Replace execution.expected_serial with the actual serial.")
    result = {"expected_serial": serial.strip()}
    for key in FIELDS[1:]:
        number = value[key]
        if type(number) not in (int, float):
            raise ValueError(f"execution.{key} must be finite and positive.")
        try:
            number = float(number)
        except (OverflowError, ValueError) as error:
            raise ValueError(f"execution.{key} must be finite and positive.") from error
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"execution.{key} must be finite and positive.")
        result[key] = number
    return result


def settings_for(config, **legacy):
    stored = config.get("execution")
    if stored is None:
        return {key: legacy.get(key) for key in FIELDS}
    settings = validate_execution(stored)
    for key, value in legacy.items():
        if value is not None and value != settings[key]:
            raise ValueError(f"CLI {key} conflicts with YAML execution.{key}.")
    return settings


def require_ceilings(settings, preview):
    for key, required in (
        ("maximum_voltage_v", "required_preflight_ceiling_v"),
        ("maximum_current_a", "required_preflight_ceiling_a"),
    ):
        value = settings[key]
        if value is not None and value < preview[required]:
            raise ValueError(f"execution.{key} is below the configured requirement.")


def new_id(prefix):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    return f"{prefix}_{stamp}_{uuid4().hex[:8]}"


def resolve_single(config, arguments):
    settings = settings_for(
        config,
        expected_serial=arguments.expected_serial,
        maximum_voltage_v=arguments.max_voltage_v,
        maximum_current_a=arguments.max_current_a,
    )
    arguments.expected_serial = settings["expected_serial"]
    arguments.max_voltage_v = settings["maximum_voltage_v"]
    arguments.max_current_a = settings["maximum_current_a"]
    if arguments.execute and arguments.experiment_id is None:
        arguments.experiment_id = new_id("experiment")
    return settings
