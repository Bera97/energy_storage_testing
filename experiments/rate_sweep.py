"""Offline rate-sweep planning. No hardware execution is exposed."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
import yaml
from experiments.config import (
    load_config, _UniqueSafeLoader, _mapping, _number,
)
from experiments.acquisition_budget import build_acquisition_plan
def build_sweep(settings, profile):
    _mapping(settings, "sweep", (
        "schema_version", "kind", "values", "repetitions", "reference",
        "rest_after_charge_s", "rest_after_discharge_s",
        "charge_maximum_duration_s", "discharge_maximum_duration_s",
    ))
    if (
        type(settings["schema_version"]) is not int
        or settings["schema_version"] != 1
    ):
        raise ValueError("Unsupported sweep schema_version.")
    kind = settings["kind"]
    if kind not in ("charging", "discharging"):
        raise ValueError("kind must be charging or discharging.")
    values = settings["values"]
    if not isinstance(values, list) or not 1 <= len(values) <= 100:
        raise ValueError("values must contain 1 to 100 ordered test points.")
    values = [_number(value, "sweep value") for value in values]
    if len(set(values)) != len(values):
        raise ValueError("Duplicate sweep values are not allowed.")
    if kind == "discharging" and any(value > 1 for value in values):
        raise ValueError("Discharge fractions must not exceed 1.")
    repetitions = settings["repetitions"]
    if type(repetitions) is not int or not 1 <= repetitions <= 100:
        raise ValueError("repetitions must be between 1 and 100.")
    for key in (
        "rest_after_charge_s", "rest_after_discharge_s",
        "charge_maximum_duration_s", "discharge_maximum_duration_s",
    ):
        _number(settings[key], key)
    reference_key = (
        "discharge_fraction" if kind == "charging" else "charge_current"
    )
    _mapping(settings["reference"], "reference", (reference_key,))
    if kind == "charging":
        fraction = _number(
            settings["reference"][reference_key], reference_key
        )
        if fraction > 1:
            raise ValueError("Reference discharge fraction must not exceed 1.")
    elif not isinstance(settings["reference"][reference_key], dict):
        raise ValueError(
            "reference.charge_current must be a current-selection mapping."
        )
    points = []
    warnings = []
    for index, value in enumerate(values):
        config = deepcopy(profile)
        config["experiment"]["cycles"] = repetitions
        config["rest"] = {
            "after_charge_s": settings["rest_after_charge_s"],
            "after_discharge_s": settings["rest_after_discharge_s"],
        }
        config["charge"]["maximum_duration_s"] = (
            settings["charge_maximum_duration_s"]
        )
        config["discharge"]["maximum_duration_s"] = (
            settings["discharge_maximum_duration_s"]
        )
        if kind == "charging":
            config["charge"]["current"] = {"c_rate": value}
            fraction = settings["reference"]["discharge_fraction"]
        else:
            config["charge"]["current"] = deepcopy(
                settings["reference"]["charge_current"]
            )
            fraction = value
        config["discharge"]["current"] = {
            "basis": "rated_max_discharge_fraction",
            "factor": fraction,
        }
        try:
            plan = build_acquisition_plan(
                config, maximum_records=100000
            )
            active = [
                phase for phase in plan["phases"]
                if phase["type"] != "rest"
            ]
            largest_current = max(
                phase["current_magnitude_a"] for phase in active
            )
            if largest_current > 3.0:
                raise ValueError(
                    "Requested current exceeds the conservative "
                    "3 A DC screening limit."
                )
            execution = config.get("execution")
            if execution is not None and (
                largest_current > execution["maximum_current_a"]
                or config["dut"]["ratings"]["maximum_voltage_v"]
                > execution["maximum_voltage_v"]
            ):
                raise ValueError(
                    "Requested point exceeds YAML execution ceilings."
                )
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError(
                f"Point {index + 1}, value {value}: {error}"
            ) from error
        charge, discharge = active[:2]
        capacity = config["dut"].get("nominal_capacity_ah")
        if capacity is not None:
            nominal_seconds = (
                float(capacity) / charge["current_magnitude_a"] * 3600
            )
            if nominal_seconds >= settings["charge_maximum_duration_s"]:
                warnings.append(
                    f"Point {index + 1}: charge timeout is no longer "
                    "than nominal Q/I time; CV and starting-state effects "
                    "are not included in that estimate."
                )
        points.append({
            "point_index": index,
            "value": value,
            "basis": (
                "capacity_c_rate"
                if kind == "charging"
                else "rated_max_discharge_fraction"
            ),
            "repetitions": repetitions,
            "charge_current_a": charge["current_magnitude_a"],
            "discharge_current_a": discharge["current_magnitude_a"],
            "termination_current_a": plan["resolved_currents"][
                "charge_termination_current_a"
            ],
            "largest_phase_record_count": max(
                phase["acquisition_budget"]["record_count"]
                for phase in active
            ),
            "acquisition_plan": plan,
        })
    return {
        "sweep_plan_schema_version": 1,
        "state": "planned",
        "kind": kind,
        "will_connect": False,
        "will_enable_output": False,
        "hardware_execution_available": True,
        "instrument_operating_envelope_verified": False,
        "settings": deepcopy(settings),
        "points": points,
        "total_cycles": len(points) * repetitions,
        "total_phases": len(points) * repetitions * 4,
        "warnings": [
            "Planning snapshot only; use sweep-run for explicit execution.",
            "Existing finite per-phase buffer limits remain; "
            "there is no buffer rollover.",
            "Current screening does not qualify the full instrument "
            "source/sink or dual-channel envelope.",
            "Termination settings are inherited from the profile; "
            "configured-current references vary with charge rate.",
            "The first-cycle starting state is not established "
            "by this plan.",
        ] + warnings,
    }
def load_sweep(path):
    path = Path(path).resolve(strict=True)
    try:
        document = yaml.load(
            path.read_text(encoding="utf-8"),
            Loader=_UniqueSafeLoader,
        )
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ValueError(
            f"Cannot read sweep configuration: {error}"
        ) from error
    _mapping(
        document, "document", ("profile", "sweep"),
        optional=("preparation",),
    )
    reference = document["profile"]
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError("profile must be a nonempty path.")
    profile_path = Path(reference)
    if not profile_path.is_absolute():
        profile_path = path.parent / profile_path
    profile = load_config(profile_path)["config"]
    result = build_sweep(document["sweep"], profile)
    result["profile_path"] = str(profile_path.resolve())
    result["preparation"] = deepcopy(document.get("preparation"))
    return result
def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Preview charging/discharging rate sweeps; "
            "no hardware execution."
        )
    )
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--json", action="store_true",
        help="Print the complete plan and configuration snapshots",
    )
    args = parser.parse_args(argv)
    try:
        plan = load_sweep(args.config)
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"Sweep planning failed: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(plan, indent=2, allow_nan=False))
    else:
        print(
            f"{plan['kind']} sweep: {plan['total_cycles']} cycles, "
            f"{plan['total_phases']} phases"
        )
        print(
            "PREVIEW ONLY: no connection or output."
        )
        print(
            "Point Value       Charge(mA) Discharge(mA) "
            "Term(mA)   Max records"
        )
        for point in plan["points"]:
            print(
                f"{point['point_index'] + 1:5} "
                f"{point['value']:<11g} "
                f"{point['charge_current_a'] * 1000:10.6g} "
                f"{point['discharge_current_a'] * 1000:13.6g} "
                f"{point['termination_current_a'] * 1000:10.6g} "
                f"{point['largest_phase_record_count']:11}"
            )
        for warning in plan["warnings"]:
            print("WARNING:", warning)
    return 0
