"""Hardware-free sweep validation, expansion, and planning.
No execution backend or recording-directory creation.
Existing preconditioning profiles remain independently validated.
"""
from copy import deepcopy
import json
from experiments.config import _mapping, _number, validate_config
def validate_charge_current_sweep(document):
    """Expand validated current conditions without modifying the input."""
    _mapping(
        document,
        "config",
        required=(
            "schema_version", "experiment", "dut", "instrument",
            "acquisition", "charge", "rest", "discharge", "output",
            "sweep",
        ),
        optional=("execution",),
    )
    experiment = _mapping(
        document["experiment"],
        "experiment",
        required=("type", "name", "cycles"),
    )
    if experiment["type"] != "charge_current_sweep":
        raise ValueError("Expected experiment.type=charge_current_sweep.")
    sweep = _mapping(
        document["sweep"],
        "sweep",
        required=("charge_current_a", "repeats", "initial_discharge"),
    )
    if sweep["initial_discharge"] is not True:
        raise ValueError("sweep.initial_discharge must be true.")

    values = sweep["charge_current_a"]
    if not isinstance(values, list) or not values:
        raise ValueError("sweep.charge_current_a must be a nonempty list.")
    repeats = sweep["repeats"]
    if type(repeats) is not int or repeats < 1:
        raise ValueError("sweep.repeats must be a positive integer.")
    if len(values) * repeats > 1000:
        raise ValueError(
            "A sweep may contain at most 1000 condition/repeat entries."
        )
    currents = [
        _number(value, f"sweep.charge_current_a[{index}]")
        for index, value in enumerate(values)
    ]
    if len(set(currents)) != len(currents):
        raise ValueError(
            "Duplicate charging-current values; use sweep.repeats instead."
        )
    base = deepcopy(document)
    del base["sweep"]
    base["experiment"]["type"] = "preconditioning"
    conditions = []
    for index, current in enumerate(currents, 1):
        condition = deepcopy(base)
        condition["charge"]["current"] = {"current_a": current}
        try:
            validated = validate_config(condition)
        except ValueError as error:
            raise ValueError(
                f"Sweep condition {index} ({current:g} A): {error}"
            ) from error
        for repeat in range(1, repeats + 1):
            conditions.append({
                "condition_id": f"condition_{index:04d}_repeat_{repeat:04d}",
                "condition_index": index,
                "repeat_index": repeat,
                "charge_current_a": current,
                "config": deepcopy(validated["config"]),
            })
    return {
        "config": deepcopy(document),
        "condition_count": len(conditions),
        "sweep_conditions": conditions,
    }
def build_charge_current_sweep_plan(document):
    """Validate every condition, monitoring setting, ceiling, and budget."""
    from experiments.run_live import execution_preview
    from experiments.settings import require_ceilings, settings_for
    validated = validate_charge_current_sweep(document)
    conditions = []
    for entry in validated["sweep_conditions"]:
        config = entry["config"]
        try:
            preview = execution_preview(config)
            settings = settings_for(config)
            require_ceilings(settings, preview)
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError(
                f"Sweep {entry['condition_id']}: {error}"
            ) from error
        conditions.append({
            **deepcopy(entry),
            "execution_settings": settings,
            "preview": preview,
        })
    initial_config = conditions[0]["config"]
    initial_phases = deepcopy(
        conditions[0]["preview"]["acquisition_plan"]["phases"][2:4]
    )
    initial_discharge = {
        "stage_id": "initial_discharge",
        "state": "planned",
        "phase_count": 2,
        "current_a": initial_phases[0]["current_magnitude_a"],
        "cutoff_voltage_v": initial_phases[0]["cutoff_voltage_v"],
        "maximum_duration_s": initial_phases[0]["maximum_duration_s"],
        "rest_after_s": initial_config["rest"]["after_discharge_s"],
        "phases": initial_phases,
        "phase_identity_policy": (
            "Canonical preconditioning phase identities retained; "
            "this separate stage executes only discharge and following rest."
        ),
    }

    plan = {
        "sweep_plan_schema_version": 1,
        "experiment_type": "charge_current_sweep",
        "experiment_name": document["experiment"]["name"],
        "state": "preview",
        "execution_enabled": False,
        "will_connect": False,
        "will_enable_output": False,
        "parameter": "charge.current.current_a",
        "unit": "A",
        "ordering": "initial_discharge_then_supplied_current_order_then_repeat",
        "repeats_per_current": document["sweep"]["repeats"],
        "condition_count": len(conditions),
        "condition_phase_count": sum(
            item["preview"]["acquisition_plan"]["phase_count"]
            for item in conditions
        ),
        "initial_discharge": initial_discharge,
        "total_phase_count": 2 + sum(
            item["preview"]["acquisition_plan"]["phase_count"]
            for item in conditions
        ),
        "starting_state_policy": (
            "Initial discharge to configured cutoff, followed by configured "
            "post-discharge rest, once before the first condition. "
            "No charging condition may start unless both initial phases succeed."
        ),
        "limitations": [
            "Preview only; sweep execution and combined analysis are not implemented.",
            "Each condition retains the configured preconditioning cycle sequence.",
            "Termination-reference rules remain unchanged; inspect the resolved "
            "termination current for every condition.",
            "Condition IDs are planned namespaces; no recording folders are created.",
            "A valid plan does not verify physical ratings, wiring, or protection.",
        ],
        "conditions": conditions,
    }
    json.dumps(plan, allow_nan=False)
    return plan
