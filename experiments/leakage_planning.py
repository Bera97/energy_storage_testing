"""Leakage-current test planning only: validation, sampling schedule and preview.
One channel holds a constant voltage (charge.target_voltage_v) with the charge
current as compliance while the supply current is logged for at most 72 h.
This module has no execution, recording or connection API.
"""
from copy import deepcopy
import json
import math
KIND = "leakage_current_test"
MAXIMUM_DURATION_S = 72 * 3600
MAXIMUM_RECORDS = 100000
MAXIMUM_SEGMENTS = 20
DEFAULT_REPORT_TIMES_S = (3600, 36000, 86400, 172800, 259200)
DEFAULT_FINAL_WINDOW_S = 1800
EARLY_STOP_FIELDS = (
    "minimum_duration_s", "window_s", "relative_change",
    "check_interval_s", "consecutive_checks",
)
def _schedule(value, duration, minimum_interval):
    from experiments.config import _mapping, _number
    if not isinstance(value, list) or not 1 <= len(value) <= MAXIMUM_SEGMENTS:
        raise ValueError(f"leakage.schedule must list 1 to {MAXIMUM_SEGMENTS} segments.")
    segments = []
    start = 0.0
    for index, item in enumerate(value, 1):
        name = f"leakage.schedule[{index}]"
        item = _mapping(item, name, required=("until_s", "interval_s"))
        until = _number(item["until_s"], f"{name}.until_s")
        interval = _number(item["interval_s"], f"{name}.interval_s")
        if until <= start:
            raise ValueError(f"{name}.until_s must exceed the previous segment end.")
        if interval < minimum_interval:
            raise ValueError(
                f"{name}.interval_s must be at least acquisition.interval_s ({minimum_interval:g} s)."
            )
        if interval > until - start:
            raise ValueError(f"{name}.interval_s must not exceed the segment length.")
        segments.append({
            "segment_index": index,
            "start_s": start,
            "until_s": until,
            "interval_s": interval,
            "sample_count": math.floor((until - start) / interval + 1e-9),
        })
        start = until
    if not math.isclose(start, duration, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("The last leakage.schedule until_s must equal leakage.maximum_duration_s.")
    return segments
def sample_times(segments):
    """Yield deterministic schedule offsets in seconds, starting at 0."""
    yield 0.0
    for segment in segments:
        for step in range(1, segment["sample_count"] + 1):
            yield segment["start_s"] + step * segment["interval_s"]
def _report_times(value, duration):
    from experiments.config import _number
    if value is None:
        return [float(t) for t in DEFAULT_REPORT_TIMES_S if t <= duration]
    if not isinstance(value, list) or not 1 <= len(value) <= MAXIMUM_SEGMENTS:
        raise ValueError(f"leakage.report_times_s must list 1 to {MAXIMUM_SEGMENTS} times.")
    times = [_number(item, f"leakage.report_times_s[{i}]") for i, item in enumerate(value, 1)]
    if any(b <= a for a, b in zip(times, times[1:])) or times[-1] > duration:
        raise ValueError("leakage.report_times_s must increase and not exceed leakage.maximum_duration_s.")
    return times
def _early_stop(value, duration):
    from experiments.config import _mapping, _number
    if value is None:
        return {"enabled": False}
    stop = _mapping(value, "leakage.early_stop", required=("enabled",), optional=EARLY_STOP_FIELDS)
    if type(stop["enabled"]) is not bool:
        raise ValueError("leakage.early_stop.enabled must be boolean.")
    if not stop["enabled"]:
        return {"enabled": False}
    missing = [field for field in EARLY_STOP_FIELDS if field not in stop]
    if missing:
        raise ValueError("Enabled leakage.early_stop requires: " + ", ".join(missing) + ".")
    minimum = _number(stop["minimum_duration_s"], "leakage.early_stop.minimum_duration_s")
    window = _number(stop["window_s"], "leakage.early_stop.window_s")
    change = _number(stop["relative_change"], "leakage.early_stop.relative_change")
    interval = _number(stop["check_interval_s"], "leakage.early_stop.check_interval_s")
    checks = stop["consecutive_checks"]
    if type(checks) is not int or checks < 1:
        raise ValueError("leakage.early_stop.consecutive_checks must be a positive integer.")
    if not 0 < change < 1:
        raise ValueError("leakage.early_stop.relative_change must be between 0 and 1.")
    if minimum < 2 * window:
        raise ValueError("leakage.early_stop.minimum_duration_s must be at least twice window_s.")
    if minimum >= duration:
        raise ValueError("leakage.early_stop.minimum_duration_s must be below leakage.maximum_duration_s.")
    return {
        "enabled": True, "minimum_duration_s": minimum, "window_s": window,
        "relative_change": change, "check_interval_s": interval,
        "consecutive_checks": checks,
    }
def _parse(document):
    from experiments.config import _mapping, _number, validate_config
    _mapping(document, "config", required=(
        "schema_version", "experiment", "dut", "instrument", "acquisition",
        "charge", "rest", "discharge", "output", "leakage",
    ), optional=("execution",))
    experiment = _mapping(document["experiment"], "experiment", required=("type", "name", "cycles"))
    if experiment["type"] != KIND:
        raise ValueError("Expected experiment.type=leakage_current_test.")
    if type(experiment["cycles"]) is not int or experiment["cycles"] != 1:
        raise ValueError("experiment.cycles must be 1 for a leakage test.")
    leakage = _mapping(document["leakage"], "leakage",
        required=("maximum_duration_s", "schedule"),
        optional=("early_stop", "report_times_s", "final_window_s"))
    duration = _number(leakage["maximum_duration_s"], "leakage.maximum_duration_s")
    if duration > MAXIMUM_DURATION_S:
        raise ValueError("leakage.maximum_duration_s must not exceed 259200 s (72 h).")
    base = deepcopy(document)
    del base["leakage"]
    base["experiment"]["type"] = "preconditioning"
    validated = validate_config(base)
    config = validated["config"]
    segments = _schedule(leakage["schedule"], duration, float(config["acquisition"]["interval_s"]))
    window = _number(leakage.get("final_window_s", DEFAULT_FINAL_WINDOW_S), "leakage.final_window_s")
    if window > duration:
        raise ValueError("leakage.final_window_s must not exceed leakage.maximum_duration_s.")
    return config, {
        "hold_voltage_v": float(config["charge"]["target_voltage_v"]),
        "compliance_current_a": float(validated["resolved_currents"]["charge_current_a"]),
        "maximum_duration_s": duration,
        "segments": segments,
        "total_sample_count": 1 + sum(item["sample_count"] for item in segments),
        "early_stop": _early_stop(leakage.get("early_stop"), duration),
        "report_times_s": _report_times(leakage.get("report_times_s"), duration),
        "final_window_s": window,
    }
def _require_budget(total, limit):
    if total > limit:
        raise ValueError(f"Schedule needs {total} records; the limit is {limit}.")
def validate_leakage_test(document):
    base, parsed = _parse(document)
    _require_budget(parsed["total_sample_count"], MAXIMUM_RECORDS)
    return {"config": deepcopy(document), "preparation_config": base, "leakage": parsed}
def build_leakage_plan(document, *, maximum_records=MAXIMUM_RECORDS):
    from experiments.settings import settings_for, require_ceilings
    validated = validate_leakage_test(document)
    parsed = validated["leakage"]
    _require_budget(parsed["total_sample_count"], maximum_records)
    base = validated["preparation_config"]
    settings = settings_for(base)
    required = {
        "required_preflight_ceiling_a": parsed["compliance_current_a"],
        "required_preflight_ceiling_v": parsed["hold_voltage_v"],
    }
    require_ceilings(settings, required)
    plan = {
        "leakage_preview_schema_version": 1,
        "experiment_type": KIND,
        "experiment_name": document["experiment"]["name"],
        "state": "preview",
        "execution_enabled": False,
        "execution_available": False,
        "will_connect": False,
        "will_enable_output": False,
        "configuration": deepcopy(document),
        "channel": base["instrument"]["channel"],
        "specimen_id": base["dut"]["specimen_id"],
        "resource": base["instrument"]["visa_address"],
        "method": "constant_voltage_hold_log_supply_current",
        "hold_voltage_v": parsed["hold_voltage_v"],
        "compliance_current_a": parsed["compliance_current_a"],
        "maximum_duration_s": parsed["maximum_duration_s"],
        "maximum_duration_h": parsed["maximum_duration_s"] / 3600,
        "schedule": {
            "segments": parsed["segments"],
            "total_sample_count": parsed["total_sample_count"],
            "record_limit": maximum_records,
            "unused_records": maximum_records - parsed["total_sample_count"],
        },
        "early_stop": parsed["early_stop"],
        "report_times_s": parsed["report_times_s"],
        "final_window_s": parsed["final_window_s"],
        "execution_settings": settings,
        **required,
        "limitations": [
            "Planning only: execution, recording and analysis are not available yet.",
            "Preview does not connect, enable outputs or create directories.",
            "Hold voltage and compliance are operator-supplied; they are not verified safe limits.",
            "Leakage includes the initial charge-up transient; interpret late-time current only.",
        ],
    }
    json.dumps(plan, allow_nan=False)
    return plan
