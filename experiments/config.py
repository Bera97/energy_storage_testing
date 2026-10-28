"""Hardware-free loading and validation of experiment configuration."""

from copy import deepcopy
from dataclasses import asdict
import math
from pathlib import Path

import yaml

from experiments.execution_settings import validate_execution

from experiments.current_settings import (
    parse_charge_current,
    resolve_currents,
)


class _UniqueSafeLoader(yaml.SafeLoader):
    """Safe YAML loader with duplicate-key rejection."""


def _unique_mapping(loader, node):
    result = {}

    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)

        if not isinstance(key, str):
            raise yaml.constructor.ConstructorError(
                None,
                None,
                "Configuration keys must be strings.",
                key_node.start_mark,
            )

        if key in result:
            raise yaml.constructor.ConstructorError(
                None,
                None,
                f"Duplicate configuration key: {key}",
                key_node.start_mark,
            )

        result[key] = loader.construct_object(
            value_node, deep=True
        )

    return result


_UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _unique_mapping,
)


def _mapping(value, name, required, optional=()):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping.")

    required = set(required)
    allowed = required | set(optional)
    keys = set(value)

    if not all(isinstance(key, str) for key in keys):
        raise ValueError(f"{name} keys must be strings.")

    missing = required - keys
    unknown = keys - allowed

    if missing:
        raise ValueError(
            f"{name} missing fields: {', '.join(sorted(missing))}."
        )

    if unknown:
        raise ValueError(
            f"{name} unknown fields: {', '.join(sorted(unknown))}."
        )

    return value


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string.")
    return value


def _number(value, name, allow_zero=False):
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

    if result < 0 or (result == 0 and not allow_zero):
        requirement = "nonnegative" if allow_zero else "positive"
        raise ValueError(f"{name} must be {requirement}.")

    return result


def validate_config(document):
    """Return validated configuration and resolved settings.

    Does not modify the supplied document.

    Missing charge-monitoring fields are permitted for offline
    planning, but are reported explicitly. A future hardware backend
    must call require_charge_monitoring_settings().

    Validation establishes consistency with supplied ratings, not
    datasheet correctness, electrical safety, or instrument capability.
    """
    config = deepcopy(document)

    _mapping(
        config,
        "config",
        required=(
            "schema_version",
            "experiment",
            "dut",
            "instrument",
            "acquisition",
            "charge",
            "rest",
            "discharge",
            "output",
        ),
        optional=("execution",),
    )

    if "execution" in config:
        validate_execution(config["execution"])

    if (
        type(config["schema_version"]) is not int
        or config["schema_version"] != 1
    ):
        raise ValueError("Unsupported schema_version.")

    experiment = _mapping(
        config["experiment"],
        "experiment",
        required=("type", "name", "cycles"),
    )

    if experiment["type"] != "preconditioning":
        raise ValueError(
            "Only experiment.type=preconditioning is supported."
        )

    _text(experiment["name"], "experiment.name")

    if (
        type(experiment["cycles"]) is not int
        or experiment["cycles"] < 1
    ):
        raise ValueError(
            "experiment.cycles must be a positive integer."
        )

    dut = _mapping(
        config["dut"],
        "dut",
        required=(
            "name",
            "specimen_id",
            "category",
            "chemistry",
            "manufacturer",
            "model",
            "ratings",
        ),
        optional=(
            "nominal_capacity_ah",
            "nominal_capacitance_f",
        ),
    )

    for field in (
        "name",
        "specimen_id",
        "chemistry",
        "manufacturer",
        "model",
    ):
        _text(dut[field], f"dut.{field}")

    if dut["category"] not in ("battery", "edlc", "lic"):
        raise ValueError(
            "dut.category must be battery, edlc, or lic."
        )

    for field in (
        "nominal_capacity_ah",
        "nominal_capacitance_f",
    ):
        if dut.get(field) is not None:
            _number(dut[field], f"dut.{field}")

    ratings = _mapping(
        dut["ratings"],
        "dut.ratings",
        required=(
            "minimum_voltage_v",
            "maximum_voltage_v",
            "maximum_continuous_charge_current_a",
            "maximum_continuous_discharge_current_a",
        ),
    )

    minimum_voltage = _number(
        ratings["minimum_voltage_v"],
        "dut.ratings.minimum_voltage_v",
        allow_zero=True,
    )
    maximum_voltage = _number(
        ratings["maximum_voltage_v"],
        "dut.ratings.maximum_voltage_v",
    )

    if minimum_voltage >= maximum_voltage:
        raise ValueError(
            "DUT minimum voltage must be below maximum voltage."
        )

    instrument = _mapping(
        config["instrument"],
        "instrument",
        required=(
            "visa_address",
            "channel",
            "four_wire",
            "nplc",
        ),
    )

    _text(
        instrument["visa_address"],
        "instrument.visa_address",
    )

    if (
        type(instrument["channel"]) is not int
        or instrument["channel"] not in (1, 2)
    ):
        raise ValueError(
            "instrument.channel must be 1 or 2."
        )

    if type(instrument["four_wire"]) is not bool:
        raise ValueError(
            "instrument.four_wire must be boolean."
        )

    _number(instrument["nplc"], "instrument.nplc")

    acquisition = _mapping(
        config["acquisition"],
        "acquisition",
        required=("interval_s",),
        optional=("maximum_sample_gap_s",),
    )

    interval = _number(
        acquisition["interval_s"],
        "acquisition.interval_s",
    )

    maximum_gap = None

    if "maximum_sample_gap_s" in acquisition:
        maximum_gap = _number(
            acquisition["maximum_sample_gap_s"],
            "acquisition.maximum_sample_gap_s",
        )

        if maximum_gap < interval:
            raise ValueError(
                "acquisition.maximum_sample_gap_s must be "
                "at least acquisition.interval_s."
            )

    charge = _mapping(
        config["charge"],
        "charge",
        required=(
            "mode",
            "current",
            "target_voltage_v",
            "termination",
            "maximum_duration_s",
            "maximum_cv_duration_s",
        ),
        optional=("cv_voltage_tolerance_v",),
    )

    if charge["mode"] != "cc_cv":
        raise ValueError("charge.mode must be cc_cv.")

    charge_basis, charge_factor = parse_charge_current(
        charge["current"],
        category=dut["category"],
        nominal_capacity_ah=dut.get("nominal_capacity_ah"),
        maximum_charge_current_a=ratings[
            "maximum_continuous_charge_current_a"
        ],
    )

    target_voltage = _number(
        charge["target_voltage_v"],
        "charge.target_voltage_v",
    )

    voltage_tolerance = None

    if "cv_voltage_tolerance_v" in charge:
        voltage_tolerance = _number(
            charge["cv_voltage_tolerance_v"],
            "charge.cv_voltage_tolerance_v",
        )

        if voltage_tolerance >= target_voltage:
            raise ValueError(
                "charge.cv_voltage_tolerance_v must be "
                "below charge.target_voltage_v."
            )

    termination = _mapping(
        charge["termination"],
        "charge.termination",
        required=(
            "current_reference",
            "current_factor",
            "hold_time_s",
        ),
    )

    hold_time = _number(
        termination["hold_time_s"],
        "charge.termination.hold_time_s",
    )
    charge_timeout = _number(
        charge["maximum_duration_s"],
        "charge.maximum_duration_s",
    )
    cv_timeout = _number(
        charge["maximum_cv_duration_s"],
        "charge.maximum_cv_duration_s",
    )

    if cv_timeout > charge_timeout:
        raise ValueError(
            "Maximum CV duration must not exceed "
            "maximum total charge duration."
        )

    if hold_time >= cv_timeout:
        raise ValueError(
            "Termination hold time must be below "
            "maximum CV duration."
        )

    if maximum_gap is not None and maximum_gap >= hold_time:
        raise ValueError(
            "acquisition.maximum_sample_gap_s must be "
            "below charge.termination.hold_time_s."
        )

    rest = _mapping(
        config["rest"],
        "rest",
        required=("after_charge_s", "after_discharge_s"),
    )

    for field in (
        "after_charge_s",
        "after_discharge_s",
    ):
        _number(rest[field], f"rest.{field}")

    discharge = _mapping(
        config["discharge"],
        "discharge",
        required=(
            "mode",
            "current",
            "cutoff_voltage_v",
            "maximum_duration_s",
        ),
    )

    if discharge["mode"] != "constant_current":
        raise ValueError(
            "discharge.mode must be constant_current."
        )

    discharge_current = _mapping(
        discharge["current"],
        "discharge.current",
        required=("basis", "factor"),
    )

    if (
        discharge_current["basis"]
        != "rated_max_discharge_fraction"
    ):
        raise ValueError(
            "discharge.current.basis must be "
            "rated_max_discharge_fraction."
        )

    cutoff_voltage = _number(
        discharge["cutoff_voltage_v"],
        "discharge.cutoff_voltage_v",
        allow_zero=True,
    )

    _number(
        discharge["maximum_duration_s"],
        "discharge.maximum_duration_s",
    )

    if not (
        minimum_voltage
        <= cutoff_voltage
        < target_voltage
        <= maximum_voltage
    ):
        raise ValueError(
            "Voltages must satisfy: DUT minimum <= discharge "
            "cutoff < charge target <= DUT maximum."
        )

    output = _mapping(
        config["output"],
        "output",
        required=("root_directory",),
    )
    _text(output["root_directory"], "output.root_directory")

    currents = resolve_currents(
        category=dut["category"],
        nominal_capacity_ah=dut.get("nominal_capacity_ah"),
        maximum_charge_current_a=ratings[
            "maximum_continuous_charge_current_a"
        ],
        maximum_discharge_current_a=ratings[
            "maximum_continuous_discharge_current_a"
        ],
        charge_basis=charge_basis,
        charge_factor=charge_factor,
        discharge_factor=discharge_current["factor"],
        termination_reference=termination[
            "current_reference"
        ],
        termination_factor=termination["current_factor"],
    )

    missing_monitoring_fields = []

    if voltage_tolerance is None:
        missing_monitoring_fields.append(
            "charge.cv_voltage_tolerance_v"
        )

    if maximum_gap is None:
        missing_monitoring_fields.append(
            "acquisition.maximum_sample_gap_s"
        )

    monitoring = None

    if not missing_monitoring_fields:
        monitoring = {
            "target_voltage_v": target_voltage,
            "voltage_tolerance_v": voltage_tolerance,
            "maximum_voltage_v": maximum_voltage,
            "maximum_sample_gap_s": maximum_gap,
        }

    return {
        "config": config,
        "resolved_currents": asdict(currents),
        "charge_monitoring": monitoring,
        "missing_charge_monitoring_fields": (
            missing_monitoring_fields
        ),
    }


def require_charge_monitoring_settings(document):
    """Return explicit monitoring settings or reject missing fields.

    This checks monitoring configuration only. A successful return
    is not authorization or proof of readiness for cell execution.
    """
    validated = validate_config(document)
    missing = validated["missing_charge_monitoring_fields"]

    if missing:
        raise ValueError(
            "Explicit charge-monitoring settings required: "
            + ", ".join(missing)
        )

    return validated["charge_monitoring"]


def load_config(path):
    """Load and validate one YAML file without hardware access."""
    path = Path(path)

    try:
        text = path.read_text(encoding="utf-8")
        document = yaml.load(
            text,
            Loader=_UniqueSafeLoader,
        )
    except (
        OSError,
        UnicodeError,
        yaml.YAMLError,
    ) as error:
        raise ValueError(
            f"Cannot load experiment configuration: {error}"
        ) from error

    return validate_config(document)