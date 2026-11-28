"""Profile-discharge planning only. No execution, connection or recording API.
The experiment is the standard initial CC discharge, then experiment.cycles repetitions of
charge, rest, profile discharge (repeated until cutoff), rest. Validation and phase building
reuse the constant-power planner through a private stand-in document, so ratings, ceilings and
charge/CV-hold policy are validated by the existing code.
"""
from copy import deepcopy
import json
from experiments.profile_control import KINDS, ProfileLimits, profile_cycle_s
KIND = "profile_discharge_test"
CONTROL_FIELDS = (
    "maximum_current_a", "update_interval_s", "maximum_feedback_age_s",
    "relative_tolerance", "absolute_tolerance_w", "startup_grace_s",
    "maximum_tracking_error_s",
)
PROFILE_CONTROL_FIELDS = CONTROL_FIELDS + ("zero_current_tolerance_a",)
STAND_IN_POWER_W = 1e-6
def _sections(document):
    from experiments.config import _mapping, _number
    _mapping(document, "config", required=(
        "schema_version", "experiment", "dut", "instrument", "acquisition", "charge", "rest",
        "discharge", "output", "profile", "profile_control",
    ), optional=("execution",))
    experiment = _mapping(document["experiment"], "experiment", required=("type", "name", "cycles"))
    if experiment["type"] != KIND:
        raise ValueError("Expected experiment.type=profile_discharge_test.")
    profile = _mapping(document["profile"], "profile", required=("kind", "steps"))
    if profile["kind"] not in KINDS:
        raise ValueError("profile.kind must be current or power.")
    steps = profile["steps"]
    if not isinstance(steps, list) or not steps:
        raise ValueError("profile.steps must be a nonempty list.")
    parsed = []
    for index, step in enumerate(steps):
        step = _mapping(step, f"profile.steps[{index}]", required=("duration_s", "value"))
        duration = step["duration_s"]
        if type(duration) is not int or duration < 1:
            raise ValueError(f"profile.steps[{index}].duration_s must be whole seconds >= 1.")
        parsed.append((duration, _number(step["value"], f"profile.steps[{index}].value", allow_zero=True)))
    if not any(value > 0 for _, value in parsed):
        raise ValueError("The profile must contain at least one discharging step.")
    control = _mapping(document["profile_control"], "profile_control", required=PROFILE_CONTROL_FIELDS)
    control = {
        key: _number(control[key], f"profile_control.{key}",
                     allow_zero=key in ("absolute_tolerance_w", "startup_grace_s"))
        for key in PROFILE_CONTROL_FIELDS
    }
    return profile["kind"], tuple(parsed), control
def _stand_in(document, control):
    stand_in = deepcopy(document)
    stand_in["experiment"]["type"] = "constant_power_discharge_sweep"
    del stand_in["profile"]
    del stand_in["profile_control"]
    stand_in["sweep"] = {"power_w": [STAND_IN_POWER_W], "repeats": 1, "initial_discharge": True}
    stand_in["power_control"] = {key: control[key] for key in CONTROL_FIELDS}
    return stand_in
def _limits(base, kind, steps, control):
    values = dict(base)
    peak = max(value for _, value in steps)
    values["power_w"] = peak if kind == "power" else peak * values["maximum_voltage_v"]
    return ProfileLimits(**values, profile_kind=kind, profile_steps=steps,
                         zero_current_tolerance_a=control["zero_current_tolerance_a"])
def validate_profile_discharge_test(document):
    from experiments.power_planning import validate_constant_power_sweep
    kind, steps, control = _sections(document)
    validated = validate_constant_power_sweep(_stand_in(document, control))
    limits = _limits(validated["power_limits"][0], kind, steps, control)
    return {
        "config": deepcopy(document),
        "preparation_config": validated["preparation_config"],
        "profile_limits": limits.to_dict(),
    }
def build_profile_plan(document, *, maximum_records=100000):
    from experiments.power_planning import build_constant_power_sweep_plan
    kind, steps, control = _sections(document)
    plan = build_constant_power_sweep_plan(_stand_in(document, control), maximum_records=maximum_records)
    limits = _limits(plan["conditions"][0]["controller_limits"], kind, steps, control)
    limit_document = limits.to_dict()
    profile = {
        "kind": kind, "steps": [[duration, value] for duration, value in steps],
        "cycle_s": profile_cycle_s(steps), "repeats_until": "discharge_cutoff_voltage",
    }
    peak = max(value for _, value in steps)
    worst_case_current = peak if kind == "current" else peak / limits.cutoff_voltage_v
    for condition in plan["conditions"]:
        condition["power_w"] = None
        condition["profile"] = deepcopy(profile)
        condition["controller_limits"] = deepcopy(limit_document)
        condition["worst_case_requested_current_a"] = worst_case_current
        for phase in condition["phases"]:
            if phase["type"] != "discharge_cp":
                continue
            phase["type"] = "discharge_profile"
            phase.pop("power_w")
            phase.pop("current_convention")
            phase["controller_limits"] = deepcopy(limit_document)
            phase["profile"] = deepcopy(profile)
            phase["control_method"] = "host_feedback_proposal_not_hardware_qualified"
    plan.update(
        experiment_type=KIND, parameter="profile", unit="A" if kind == "current" else "W",
        configuration=deepcopy(document), profile_preview_schema_version=1,
        ordering="initial_cc_discharge_then_cycles_of_charge_rest_profile_discharge_rest",
        limitations=[
            "Planning/preview only; profile execution and analysis are not implemented yet.",
            "The profile repeats until the discharge cutoff voltage or discharge.maximum_duration_s.",
            "Steps are whole seconds and are evaluated once per update interval (at most 1 s).",
            "Host setpoint/acquisition integration, physical operating envelope and timing are unqualified.",
            "No output command, connection, recording directory, retry or resume is performed.",
        ],
    )
    json.dumps(plan, allow_nan=False)
    return plan
