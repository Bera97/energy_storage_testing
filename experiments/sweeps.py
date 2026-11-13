"""Sweep planning and supervised sequential execution.
Planning is hardware-free and creates no recording directories.
execute_charge_current_sweep is OUTPUT-CAPABLE through execute_live.
No automatic retry, resume, parallel execution, or supply control.
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
        "execution_available": True,
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
            "Preview performs no execution; --execute is required. "
            "Completed sweeps can be exported with --analyze RUN_NAME.",
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
def _sweep_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
def _write_sweep_manifest(directory, document):
    """Atomic replacement; not a power-loss durability guarantee."""
    from uuid import uuid4
    text = json.dumps(document, indent=2, allow_nan=False) + "\n"
    temporary = directory / f"sweep_{uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(text)
        temporary.replace(directory / "sweep.json")
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
def _check_sweep_stage(
    directory, configuration, initial_discharge_only, runner_report
):
    """Require consistent recordings, canonical plan, and closed session."""
    from analysis.validate_preconditioning_experiment import (
        _read_json, validate_experiment,
    )
    from experiments.preconditioning_runner import _execution_plan
    from experiments.settings import settings_for
    validation = validate_experiment(
        directory,
        phase_root=directory,
        initial_discharge_only=initial_discharge_only,
    )
    if (
        validation.get("valid") is not True
        or validation.get("state") != "completed"
    ):
        raise RuntimeError(
            "Stage recordings failed validation: "
            + "; ".join(validation.get("errors", []))
        )
    manifest = _read_json(directory / "experiment.json")
    expected_plan = _execution_plan(
        configuration,
        initial_discharge_only=initial_discharge_only,
    )
    metadata = manifest.get("metadata", {})
    if (
        manifest.get("plan") != expected_plan
        or metadata.get("configuration") != configuration
        or manifest.get("experiment_report") != runner_report
    ):
        raise RuntimeError(
            "Stage manifest, returned report, or configuration differs."
        )
    expected_serial = settings_for(configuration)["expected_serial"]
    session = manifest.get("session_outcome", {})
    for field in (
        "serial_verified", "shutdown_attempted", "shutdown_verified",
        "close_attempted", "close_succeeded",
    ):
        if session.get(field) is not True:
            raise RuntimeError(f"Stage session lacks verified {field}.")
    if (
        session.get("shutdown_error") is not None
        or session.get("close_error") is not None
        or metadata.get("expected_instrument_serial") != expected_serial
    ):
        raise RuntimeError("Stage session contains inconsistent final evidence.")
    identity = session.get("identity")
    fields = (
        [field.strip() for field in identity.split(",")]
        if isinstance(identity, str) else []
    )
    if (
        len(fields) != 4
        or fields[1].upper() != "B2902B"
        or fields[2] != expected_serial
    ):
        raise RuntimeError("Stage session identity does not match the profile.")
    return validation
def execute_charge_current_sweep(document, *, sweep_id):
    """Run initial preparation and conditions, returning (manifest, directory).
    Each stage uses execute_live's existing session and cleanup boundary.
    All conditions are planned before reserving files or connecting.
    Persisted validation and session-close checks gate the next stage.
    Existing sweep destinations are never overwritten or resumed.
    """
    from pathlib import Path
    import re
    from experiments.run_live import execute_live
    plan = build_charge_current_sweep_plan(deepcopy(document))
    if (
        not isinstance(sweep_id, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,80}", sweep_id) is None
    ):
        raise ValueError(
            "sweep_id must contain 1-80 ASCII letters, digits, '_' or '-'."
        )
    for condition in plan["conditions"]:
        serial = condition["execution_settings"].get("expected_serial")
        if not isinstance(serial, str) or not serial.strip():
            raise ValueError(
                f"{condition['condition_id']}: execution.expected_serial "
                "must be provided or derived from the VISA address."
            )
    directory = (
        Path(document["output"]["root_directory"]) / sweep_id
    ).resolve()
    specifications = [
        ("initial_discharge", plan["conditions"][0], True),
        *[
            (entry["condition_id"], entry, False)
            for entry in plan["conditions"]
        ],
    ]
    stages = []
    for stage_id, condition, initial_only in specifications:
        configuration = deepcopy(condition["config"])
        configuration["output"]["root_directory"] = str(directory)
        stages.append({
            "stage_id": stage_id,
            "kind": (
                "initial_discharge" if initial_only else "condition"
            ),
            "state": "pending",
            "initial_discharge_only": initial_only,
            "condition_index": (
                None if initial_only else condition["condition_index"]
            ),
            "repeat_index": (
                None if initial_only else condition["repeat_index"]
            ),
            "charge_current_a": (
                None if initial_only else condition["charge_current_a"]
            ),
            "configuration": configuration,
            "execution_settings": deepcopy(condition["execution_settings"]),
            "experiment_directory": str(directory / stage_id),
            "completed_phase_count": 0,
            "validation": None,
            "error": None,
        })
    manifest = {
        "sweep_manifest_schema_version": 1,
        "sweep_id": sweep_id,
        "created_at_utc": _sweep_now(),
        "state": "running",
        "configuration": deepcopy(document),
        "plan": plan,
        "session_policy": "one_verified_closed_session_per_stage",
        "active_stage_id": None,
        "completed_stage_count": 0,
        "stages": stages,
        "error": None,
    }
    initial_text = json.dumps(manifest, indent=2, allow_nan=False) + "\n"
    directory.mkdir(parents=True, exist_ok=False)
    with (directory / "sweep.json").open("x", encoding="utf-8") as handle:
        handle.write(initial_text)
    active = None
    try:
        for stage in stages:
            active = stage
            stage["state"] = "running"
            stage["started_at_utc"] = _sweep_now()
            manifest["active_stage_id"] = stage["stage_id"]
            _write_sweep_manifest(directory, manifest)
            options = {
                "experiment_id": stage["stage_id"],
                **stage["execution_settings"],
            }
            if stage["initial_discharge_only"]:
                options["initial_discharge_only"] = True
            report, recorded_directory = execute_live(
                deepcopy(stage["configuration"]), **options
            )
            recorded_directory = Path(recorded_directory).resolve()
            if recorded_directory != directory / stage["stage_id"]:
                raise RuntimeError("Executor returned an unexpected stage path.")
            stage["validation"] = _check_sweep_stage(
                recorded_directory,
                stage["configuration"],
                stage["initial_discharge_only"],
                report,
            )
            stage["completed_phase_count"] = report["completed_phase_count"]
            stage["state"] = "completed"
            stage["finished_at_utc"] = _sweep_now()
            manifest["completed_stage_count"] += 1
            manifest["active_stage_id"] = None
            _write_sweep_manifest(directory, manifest)
            active = None
        completed_phases = sum(
            stage["completed_phase_count"] for stage in stages
        )
        if completed_phases != plan["total_phase_count"]:
            raise RuntimeError("Completed sweep phase count differs from its plan.")
        manifest["state"] = "completed"
        manifest["finished_at_utc"] = _sweep_now()
        _write_sweep_manifest(directory, manifest)
        return deepcopy(manifest), directory
    except BaseException as error:
        failure = {
            "type": type(error).__name__,
            "message": str(error),
            "notes": list(getattr(error, "__notes__", ())),
        }
        manifest["state"] = (
            "interrupted" if isinstance(error, KeyboardInterrupt) else "raised"
        )
        manifest["error"] = failure
        manifest["finished_at_utc"] = _sweep_now()
        if active is not None:
            active["state"] = "raised"
            active["error"] = failure
        for stage in stages:
            if stage["state"] == "pending":
                stage["state"] = "not_started"
        error.sweep_directory = directory
        try:
            _write_sweep_manifest(directory, manifest)
        except BaseException as recording_error:
            error.add_note(
                "Sweep failure manifest could not be saved: "
                f"{type(recording_error).__name__}: {recording_error}"
            )
        raise
def run_configured_sweep(document, *, sweep_id):
    """CLI adapter; no command parsing, retry, resume, or automatic analysis."""
    import sys
    try:
        manifest, directory = execute_charge_current_sweep(
            document, sweep_id=sweep_id
        )
        print(json.dumps({
            "state": manifest["state"],
            "sweep_directory": str(directory),
            "condition_count": manifest["plan"]["condition_count"],
            "completed_stage_count": manifest["completed_stage_count"],
            "completed_phase_count": sum(
                stage["completed_phase_count"] for stage in manifest["stages"]
            ),
        }, indent=2, allow_nan=False))
        return 0
    except BaseException as error:
        print(json.dumps({
            "state": (
                "interrupted" if isinstance(error, KeyboardInterrupt)
                else "raised"
            ),
            "error_type": type(error).__name__,
            "error_message": str(error),
            "notes": list(getattr(error, "__notes__", ())),
            "sweep_directory": (
                str(error.sweep_directory)
                if hasattr(error, "sweep_directory") else None
            ),
        }, indent=2, allow_nan=False), file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1
