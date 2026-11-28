"""Internal recorded single-channel CP sweep. No CLI dispatch or hardware defaults.

Each backend call owns/closes one session. No retry/resume or analysis export.
"""
from pathlib import Path
from analysis.power_sweep import validate_power_sweep, validate_power_sweep_stage
from experiments.constant_power import _number
from experiments.power_sweep_backend import RecordedPowerSweepBackend
from experiments.power_sweep_contract import (
    build_sequence, completion_text, control_section, digest, require, same, snapshot, write_json,
)


def run_recorded_power_sweep(document, directory, *, backend, execute_requested=False):
    require(execute_requested is True, "Explicit internal sweep execution authorization required.")
    require(isinstance(backend, RecordedPowerSweepBackend) and backend.output_authorized is True
            and backend.claimed is False and backend.failed is False and backend.in_stage is False
            and type(backend.position) is int and backend.position == 0, "A fresh authorized sweep backend is required.")
    configuration = snapshot(document)
    plan, requests = build_sequence(configuration)
    same(configuration, backend.document, "Backend configuration differs.")
    same(requests, backend.requests, "Backend plan differs.")
    same(plan, backend.plan, "Backend canonical preview differs.")
    same(backend.configuration, plan["initial_preparation"]["configuration"], "Backend preparation configuration differs.")
    same(backend.settings, plan["execution_settings"], "Backend execution settings differ.")
    probe = _number(backend.probe_ceiling, "probe ceiling")
    require(probe <= control_section(configuration)["maximum_current_a"], "Probe exceeds configured ceiling.")
    same(backend.policy, {"maximum_probe_current_a": probe, "maximum_records": 100000,
                         "session_policy": "new_owned_session_per_stage"}, "Backend execution policy differs.")
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=False)
    manifest = {"power_sweep_schema_version": 1, "state": "running", "physical_qualification": False,
                "configuration": configuration, "execution_policy": snapshot(backend.policy), "requests": requests,
                "completed_stage_count": 0, "active_stage_id": None, "stages": [], "error": None}
    write_json(root / "power_sweep.json", manifest, create=True)
    backend.claimed = True
    marker_created = False
    try:
        for request in requests:
            manifest["active_stage_id"] = request["stage_id"]
            write_json(root / "power_sweep.json", manifest)
            stage_root = root / request["stage_id"]
            backend.execute_stage(snapshot(request), stage_root)
            checked = validate_power_sweep_stage(stage_root, request,
                plan["initial_preparation"]["configuration"], configuration, backend.policy, _plan=plan)
            require(checked["valid"] is True, "Stage evidence gate: " + "; ".join(checked["errors"]))
            manifest["stages"].append({"stage_id": request["stage_id"], "stage_sha256": digest(stage_root / "stage.json")})
            manifest["completed_stage_count"] += 1
            manifest["active_stage_id"] = None
            write_json(root / "power_sweep.json", manifest)
        manifest["state"] = "completed"
        write_json(root / "power_sweep.json", manifest)
        text = completion_text(root / "power_sweep.json")
        with (root / "POWER_SWEEP_COMPLETE").open("x", encoding="utf-8", newline="\n") as stream:
            marker_created = True
            require(stream.write(text) == len(text), "Short completion marker write.")
            stream.flush()
        checked = validate_power_sweep(root)
        require(checked["valid"] is True, "Final sweep validation: " + "; ".join(checked["errors"]))
        return {"state": "completed", "directory": str(root), "completed_stage_count": len(requests)}
    except BaseException as error:
        backend.failed = True
        if marker_created:
            try:
                (root / "POWER_SWEEP_COMPLETE").unlink(missing_ok=True)
            except BaseException as secondary:
                error.add_note(f"Sweep marker cleanup: {secondary}")
        manifest["state"] = "raised"
        manifest["error"] = {"type": type(error).__name__, "message": str(error), "notes": list(getattr(error, "__notes__", ()))}
        try:
            write_json(root / "power_sweep.json", manifest)
        except BaseException as secondary:
            error.add_note(f"Sweep failure export: {type(secondary).__name__}: {secondary}")
        try:
            error.power_sweep_report = snapshot(manifest)
            error.power_sweep_directory = str(root)
        except BaseException as secondary:
            error.add_note(f"Sweep failure attachment: {secondary}")
        raise
