"""Experimental constant-power planning only. No execution or recording API."""
from copy import deepcopy
import json
from pathlib import Path
from experiments.constant_power import PowerLimits

KIND = "constant_power_discharge_sweep"
CONTROL_FIELDS = (
    "maximum_current_a", "update_interval_s", "maximum_feedback_age_s",
    "relative_tolerance", "absolute_tolerance_w", "startup_grace_s",
    "maximum_tracking_error_s",
)


def _base(document):
    from experiments.config import _mapping, _number, validate_config
    _mapping(document, "config", required=(
        "schema_version", "experiment", "dut", "instrument", "acquisition",
        "charge", "rest", "discharge", "output", "sweep", "power_control",
    ), optional=("execution",))
    experiment = _mapping(document["experiment"], "experiment", required=("type", "name", "cycles"))
    if experiment["type"] != KIND:
        raise ValueError("Expected experiment.type=constant_power_discharge_sweep.")
    sweep = _mapping(document["sweep"], "sweep", required=("power_w", "repeats", "initial_discharge"))
    if sweep["initial_discharge"] is not True:
        raise ValueError("sweep.initial_discharge must be true.")
    repeats = sweep["repeats"]
    values = sweep["power_w"]
    if type(repeats) is not int or repeats < 1:
        raise ValueError("sweep.repeats must be a positive integer.")
    if not isinstance(values, list) or not values or len(values) * repeats > 1000:
        raise ValueError("A nonempty power list and at most 1000 condition/repeat entries are required.")
    values = [_number(value, f"sweep.power_w[{index}]") for index, value in enumerate(values)]
    if len(set(values)) != len(values):
        raise ValueError("Duplicate powers; use sweep.repeats instead.")
    control = _mapping(document["power_control"], "power_control", required=CONTROL_FIELDS)
    control = {
        key: _number(control[key], f"power_control.{key}",
                     allow_zero=key in ("absolute_tolerance_w", "startup_grace_s"))
        for key in CONTROL_FIELDS
    }
    base = deepcopy(document)
    del base["sweep"]
    del base["power_control"]
    base["experiment"]["type"] = "preconditioning"
    validated = validate_config(base)
    ratings = base["dut"]["ratings"]
    ceiling = _number(control["maximum_current_a"], "power_control.maximum_current_a")
    if ceiling > ratings["maximum_continuous_discharge_current_a"]:
        raise ValueError("Power-control current ceiling exceeds the supplied discharge rating.")
    if control["update_interval_s"] < base["acquisition"]["interval_s"]:
        raise ValueError("Power updates cannot be planned faster than acquisition.interval_s.")
    gap = base["acquisition"].get("maximum_sample_gap_s")
    if gap is not None and control["maximum_feedback_age_s"] < gap:
        raise ValueError("Feedback age limit must cover the configured maximum sample gap.")
    limits = []
    for index, power in enumerate(values, 1):
        try:
            limits.append(PowerLimits(
                power_w=power,
                cutoff_voltage_v=base["discharge"]["cutoff_voltage_v"],
                minimum_voltage_v=ratings["minimum_voltage_v"],
                maximum_voltage_v=ratings["maximum_voltage_v"],
                maximum_duration_s=base["discharge"]["maximum_duration_s"],
                **control,
            ))
        except (ValueError, TypeError) as error:
            raise ValueError(f"Power condition {index} ({power:g} W): {error}") from error
    return validated["config"], limits


def validate_constant_power_sweep(document):
    base, limits = _base(document)
    return {
        "config": deepcopy(document),
        "preparation_config": base,
        "power_limits": [entry.to_dict() for entry in limits],
    }


def build_constant_power_sweep_plan(document, *, maximum_records=100000):
    if isinstance(document, dict) and isinstance(document.get("experiment"), dict) and (
            document["experiment"].get("type") == "profile_discharge_test"):
        from experiments.profile_planning import build_profile_plan
        return build_profile_plan(document, maximum_records=maximum_records)
    from experiments.run_live import execution_preview
    from experiments.settings import settings_for, require_ceilings
    base, limits = _base(document)
    preview = execution_preview(base, maximum_records=maximum_records)
    settings = settings_for(base)
    require_ceilings(settings, preview)
    required_current = max(preview["required_preflight_ceiling_a"], limits[0].maximum_current_a)
    required = {
        "required_preflight_ceiling_a": required_current,
        "required_preflight_ceiling_v": preview["required_preflight_ceiling_v"],
    }
    require_ceilings(settings, required)
    conditions = []
    for index, policy in enumerate(limits, 1):
        for repeat in range(1, document["sweep"]["repeats"] + 1):
            phases = deepcopy(preview["acquisition_plan"]["phases"])
            for phase in phases:
                if phase["type"] != "discharge_cc":
                    continue
                phase["type"] = "discharge_cp"
                phase.pop("current_magnitude_a")
                phase.update(
                    power_w=policy.power_w,
                    controller_limits=policy.to_dict(),
                    current_convention="I_command = -P_requested / V_fresh; P_measured = -V * I",
                    control_method="host_feedback_proposal_not_hardware_qualified",
                )
            conditions.append({
                "condition_id": f"condition_{index:04d}_repeat_{repeat:04d}",
                "condition_index": index,
                "repeat_index": repeat,
                "power_w": policy.power_w,
                "charge_current_a": preview["acquisition_plan"]["resolved_currents"]["charge_current_a"],
                "worst_case_requested_current_a": policy.power_w / policy.cutoff_voltage_v,
                "controller_limits": policy.to_dict(),
                "phase_count": len(phases),
                "phases": phases,
            })
    plan = {
        "constant_power_preview_schema_version": 1,
        "experiment_type": KIND,
        "experiment_name": document["experiment"]["name"],
        "state": "preview",
        "execution_enabled": False,
        "execution_available": False,
        "will_connect": False,
        "will_enable_output": False,
        "configuration": deepcopy(document),
        "parameter": "sweep.power_w",
        "unit": "W",
        "channel": preview["channel"],
        "specimen_id": preview["specimen_id"],
        "resource": preview["resource"],
        "ordering": "initial_cc_discharge_then_supplied_power_order_then_repeat",
        "initial_preparation": {
            "configuration": deepcopy(base),
            "current_a": preview["acquisition_plan"]["resolved_currents"]["discharge_current_a"],
            "phase_count": 2,
            "phases": deepcopy(preview["acquisition_plan"]["phases"][2:4]),
        },
        "execution_settings": settings,
        **required,
        "condition_count": len(conditions),
        "total_phase_count": 2 + sum(entry["phase_count"] for entry in conditions),
        "conditions": conditions,
        "limitations": [
            "Experimental planning/controller increment only; execution and analysis are disabled.",
            "Host setpoint/acquisition integration, physical operating envelope and timing are unqualified.",
            "The current ceiling must allow P/cutoff; this does not guarantee transient tracking.",
            "No output command, connection, recording directory, retry or resume is performed.",
            "The initial preparation remains constant-current and is independent of swept powers.",
            "The future adapter must record feedback/commands/readbacks and shut down on every exit.",
        ],
    }
    json.dumps(plan, allow_nan=False)
    return plan


def build_dual_constant_power_sweep_plan(documents, *, maximum_records=100000):
    from experiments.dual_preconditioning import build_dual_plan
    if not isinstance(documents, (list, tuple)) or len(documents) != 2:
        raise ValueError("Exactly two constant-power profiles are required.")
    plans = [build_constant_power_sweep_plan(doc, maximum_records=maximum_records) for doc in documents]
    kinds = {plan["experiment_type"] for plan in plans}
    if len(kinds) != 1:
        raise ValueError("Both channels must use the same experiment type; do not mix constant-power and profile profiles.")
    if kinds == {"profile_discharge_test"} and documents[0]["profile"] != documents[1]["profile"]:
        raise ValueError("Both channels must use the same profile (the profile sections differ).")
    base_plan = build_dual_plan([
        plan["initial_preparation"]["configuration"] for plan in plans
    ], maximum_records=maximum_records)
    for field in ("root_directory", "analysis_directory"):
        roots = [doc["output"].get(field) for doc in documents]
        if (roots[0] is None) != (roots[1] is None) or (
            roots[0] is not None and Path(roots[0]).resolve() != Path(roots[1]).resolve()
        ):
            raise ValueError(f"Both power profiles must use matching output.{field}.")
    if plans[0]["execution_settings"]["expected_serial"] != plans[1]["execution_settings"]["expected_serial"]:
        raise ValueError("Both power profiles must use the same expected serial.")
    return {
        "constant_power_dual_preview_schema_version": 1,
        "experiment_type": "dual_constant_power_discharge_sweep",
        "state": "preview", "execution_enabled": False, "execution_available": False,
        "will_connect": False, "will_enable_output": False,
        "resource": base_plan["resource"],
        "initial_preparation_barrier_planned": True,
        "joint_timing_qualified": False,
        "total_phase_count": sum(plan["total_phase_count"] for plan in plans),
        "channels": sorted(plans, key=lambda plan: plan["channel"]),
        "limitations": ["Planning only: no shared-session power execution backend is connected."],
    }
