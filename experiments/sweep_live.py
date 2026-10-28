"""Supervised single-channel sweeps and read-only evidence checking."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import sys
from uuid import uuid4
from analysis.validate_preconditioning_experiment import (
    _read_json, validate_experiment,
)
from experiments.config import _mapping
from experiments.execution_settings import (
    new_id, validate_execution, require_ceilings,
)
from experiments.preconditioning import build_plan
from experiments.rate_sweep import build_sweep, load_sweep
from experiments.run_live import execute_live, execution_preview
def require(condition, message):
    if not condition:
        raise ValueError(message)
def save(root, document):
    text = json.dumps(document, indent=2, allow_nan=False) + "\n"
    temporary = root / (uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(text)
        temporary.replace(root / "sweep.json")
    finally:
        temporary.unlink(missing_ok=True)
def jobs_for(plan, preparation):
    require(
        isinstance(plan, dict) and bool(plan.get("points")),
        "Missing sweep points.",
    )
    require(
        plan.get("preparation") == preparation,
        "Preparation differs from the configuration snapshot.",
    )
    profile = plan["points"][0]["acquisition_plan"]["configuration"]
    rebuilt = build_sweep(plan["settings"], profile)
    require(
        all(plan.get(key) == value for key, value in rebuilt.items()),
        "Sweep plan changed or does not validate.",
    )
    _mapping(
        preparation, "preparation",
        ("charge_current", "discharge_fraction"),
    )
    settings = deepcopy(plan["settings"])
    settings.update(
        kind="discharging",
        values=[preparation["discharge_fraction"]],
        repetitions=1,
        reference={"charge_current": preparation["charge_current"]},
    )
    first = build_sweep(settings, profile)["points"][0]
    jobs = [{
        "job_id": "preparation",
        "role": "preparation",
        "point_index": None,
        "value": None,
        "basis": None,
        "repetitions": 1,
        "configuration": first["acquisition_plan"]["configuration"],
    }]
    for point in plan["points"]:
        jobs.append({
            "job_id": f"point_{point['point_index'] + 1:04d}",
            "role": "measurement",
            "point_index": point["point_index"],
            "value": point["value"],
            "basis": point["basis"],
            "repetitions": point["repetitions"],
            "configuration": point["acquisition_plan"]["configuration"],
        })
    for job in jobs:
        config = job["configuration"]
        require(
            "execution" in config,
            "Set execution settings in the specimen profile.",
        )
        settings = validate_execution(config["execution"])
        preview = execution_preview(config)
        require_ceilings(settings, preview)
        specimen = config["dut"]["specimen_id"]
        require(
            not specimen.startswith("REPLACE_")
            and not specimen.endswith("_CH2_REVIEW"),
            "Replace placeholder specimen IDs.",
        )
        require(
            config["discharge"]["cutoff_voltage_v"] > 0,
            "Live sweeps require a positive discharge cutoff.",
        )
    return deepcopy(jobs)
def context_for(document, job):
    return {
        "sweep_id": document["sweep_id"],
        "kind": document["plan"]["kind"],
        **{
            key: job[key]
            for key in (
                "job_id", "role", "point_index",
                "value", "basis", "repetitions",
            )
        },
    }
def child_config(job, root):
    config = deepcopy(job["configuration"])
    config["output"]["root_directory"] = str(root)
    return config
def check_child(root, doc, spec, job):
    require(isinstance(job, dict), "Job must be an object.")
    require(
        all(job.get(key) == spec[key] for key in spec),
        "Job differs from the planned sequence.",
    )
    require(
        job["state"] == "completed" and job.get("error") is None,
        "A job did not complete.",
    )
    child = (root / spec["job_id"]).resolve(strict=True)
    require(
        child.is_relative_to(root) and child != root,
        "Child path escaped sweep directory.",
    )
    require(
        Path(job["directory"]).is_absolute()
        and Path(job["directory"]).resolve() == child,
        "Child reference mismatch.",
    )
    config = child_config(spec, root)
    evidence = _read_json(child / "experiment.json")
    require(
        evidence["state"] == "completed",
        "Child experiment is not completed.",
    )
    require(
        evidence["plan"] == build_plan(config),
        "Child phase plan differs from sweep point.",
    )
    metadata = evidence["metadata"]
    require(
        metadata["configuration"] == config,
        "Child configuration differs from sweep point.",
    )
    require(
        metadata["sweep_context"] == context_for(doc, spec),
        "Child sweep labels differ.",
    )
    serial = config["execution"]["expected_serial"].strip()
    require(
        metadata["expected_instrument_serial"] == serial,
        "Child expected serial mismatch.",
    )
    session = evidence["session_outcome"]
    require(
        all(
            session.get(key) is True
            for key in (
                "serial_verified",
                "shutdown_attempted",
                "shutdown_verified",
                "close_attempted",
                "close_succeeded",
            )
        ),
        "Child session cleanup or identity evidence is incomplete.",
    )
    identity = [
        part.strip() for part in session["identity"].split(",")
    ]
    require(
        len(identity) == 4
        and identity[1].upper() == "B2902B"
        and identity[2] == serial,
        "Child instrument identity mismatch.",
    )
    require(
        session.get("shutdown_error") is None
        and session.get("close_error") is None,
        "Child reports cleanup errors.",
    )
    validation = validate_experiment(child, phase_root=child)
    require(
        validation["valid"] is True
        and validation["state"] == "completed",
        "Invalid child recordings: " + str(validation["errors"]),
    )
    require(
        validation["phase_recordings_checked"]
        == 2 * spec["repetitions"],
        "Incomplete child active recordings.",
    )
    for phase, entry in zip(
        evidence["plan"]["phases"],
        evidence["backend_evidence"],
    ):
        if phase["type"] == "rest":
            continue
        reference = Path(entry["run_directory"]).resolve(strict=True)
        require(
            reference.is_relative_to(child),
            "Phase escaped its child experiment.",
        )
        phase_metadata = _read_json(reference / "metadata.json")
        require(
            phase_metadata["metadata"]["channel"]
            == config["instrument"]["channel"],
            "Recorded channel differs from profile.",
        )
    return validation
def check_sweep(directory, *, require_marker=True):
    result = {
        "valid": False,
        "state": None,
        "jobs_checked": 0,
        "phase_recordings_checked": 0,
        "errors": [],
        "warnings": [],
    }
    try:
        root = Path(directory).resolve(strict=True)
        doc = _read_json(root / "sweep.json")
        require(isinstance(doc, dict), "Manifest must be an object.")
        result["state"] = doc["state"]
        require(
            type(doc["sweep_schema_version"]) is int
            and doc["sweep_schema_version"] == 1,
            "Unsupported sweep manifest.",
        )
        require(
            doc["execution_mode"] == "supervised_single_channel",
            "Unexpected execution mode.",
        )
        require(
            doc["preparation_excluded_from_measurement_results"] is True,
            "Preparation must remain separate from measurement results.",
        )
        require(
            doc["state"] == "completed" and doc.get("error") is None,
            "Sweep is not completed.",
        )
        require(
            doc["sweep_id"] == root.name,
            "Sweep directory identity mismatch.",
        )
        if require_marker:
            require(
                (root / "SWEEP_COMPLETE").is_file(),
                "Missing SWEEP_COMPLETE.",
            )
        expected = jobs_for(doc["plan"], doc["preparation"])
        require(
            len(doc["jobs"]) == len(expected),
            "Wrong number of jobs.",
        )
        for spec, job in zip(expected, doc["jobs"]):
            validation = check_child(root, doc, spec, job)
            result["jobs_checked"] += 1
            result["phase_recordings_checked"] += (
                validation["phase_recordings_checked"]
            )
            result["warnings"].extend(
                f"{spec['job_id']}: {text}"
                for text in validation["warnings"]
            )
        result["valid"] = True
    except Exception as error:
        result["errors"].append(
            f"{type(error).__name__}: {error}"
        )
    return result
def execute_sweep(plan, preparation, sweep_id, *, execute=False):
    require(
        execute is True,
        "Explicit execute authorization is required.",
    )
    require(
        isinstance(sweep_id, str)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", sweep_id) is not None,
        "Invalid sweep ID.",
    )
    specs = jobs_for(plan, preparation)
    base = Path(
        specs[0]["configuration"]["output"]["root_directory"]
    ).resolve()
    root = base / sweep_id
    document = {
        "sweep_schema_version": 1,
        "sweep_id": sweep_id,
        "state": "running",
        "execution_mode": "supervised_single_channel",
        "plan": deepcopy(plan),
        "preparation": deepcopy(preparation),
        "preparation_excluded_from_measurement_results": True,
        "session_policy": (
            "one session per job; shutdown and close between jobs"
        ),
        "jobs": [
            {
                **spec,
                "state": "planned",
                "directory": str(root / spec["job_id"]),
                "error": None,
            }
            for spec in specs
        ],
        "error": None,
    }
    json.dumps(document, allow_nan=False)
    root.mkdir(parents=True, exist_ok=False)
    marker_created = False
    active = None
    try:
        save(root, document)
        for spec, job in zip(specs, document["jobs"]):
            active = job
            job["state"] = "running"
            save(root, document)
            config = child_config(spec, root)
            settings = validate_execution(config["execution"])
            report, directory = execute_live(
                config,
                experiment_id=spec["job_id"],
                expected_serial=settings["expected_serial"],
                maximum_voltage_v=settings["maximum_voltage_v"],
                maximum_current_a=settings["maximum_current_a"],
                context=context_for(document, spec),
            )
            require(
                Path(directory).resolve() == root / spec["job_id"],
                "Runner returned an unexpected directory.",
            )
            require(
                report["state"] == "completed"
                and report["shutdown_verified"] is True,
                "Child did not establish completion.",
            )
            job["state"] = "completed"
            check_child(root, document, spec, job)
            save(root, document)
            print(
                f"{spec['job_id']}: completed and session returned",
                file=sys.stderr,
                flush=True,
            )
            active = None
        document["state"] = "completed"
        save(root, document)
        validation = check_sweep(root, require_marker=False)
        require(
            validation["valid"],
            "; ".join(validation["errors"]),
        )
        with (root / "SWEEP_COMPLETE").open(
            "x", encoding="utf-8"
        ) as handle:
            marker_created = True
            handle.write(
                "Completed supervised single-channel sweep.\n"
            )
        return root
    except BaseException as error:
        if marker_created:
            try:
                (root / "SWEEP_COMPLETE").unlink(missing_ok=True)
            except OSError as marker_error:
                error.add_note(
                    "Could not remove completion marker: "
                    + str(marker_error)
                )
        document["state"] = "raised"
        document["error"] = {
            "type": type(error).__name__,
            "message": str(error),
        }
        if active is not None:
            active.update(
                state="raised",
                error=document["error"],
            )
        error.sweep_directory = str(root)
        try:
            save(root, document)
        except BaseException as export_error:
            error.add_note(
                f"Sweep failure export failed: {export_error}"
            )
        raise
def main(argv=None):
    arguments = list(
        sys.argv[1:] if argv is None else argv
    )
    if arguments and arguments[0] == "sweep-check":
        parser = argparse.ArgumentParser(
            prog="python -m smu sweep-check"
        )
        parser.add_argument("directory")
        args = parser.parse_args(arguments[1:])
        result = check_sweep(args.directory)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0 if result["valid"] else 1
    parser = argparse.ArgumentParser(
        prog="python -m smu sweep-run"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--sweep-id")
    args = parser.parse_args(
        arguments[1:]
        if arguments and arguments[0] == "sweep-run"
        else arguments
    )
    try:
        plan = load_sweep(args.config)
        preparation = plan.get("preparation")
        jobs = jobs_for(plan, preparation)
        if not args.execute:
            print(json.dumps({
                "state": "preview",
                "will_connect": False,
                "will_enable_output": False,
                "measurement_cycles": plan["total_cycles"],
                "preparation_cycles": 1,
                "job_count": len(jobs),
                "plan": plan,
                "jobs": jobs,
            }, indent=2, allow_nan=False))
            return 0
        directory = execute_sweep(
            plan,
            preparation,
            args.sweep_id or new_id("sweep"),
            execute=True,
        )
        print(json.dumps({
            "state": "completed",
            "sweep_directory": str(directory),
        }, indent=2))
        return 0
    except BaseException as error:
        if isinstance(error, SystemExit):
            raise
        print(json.dumps({
            "state": "raised",
            "error_type": type(error).__name__,
            "error_message": str(error),
            "error_notes": list(
                getattr(error, "__notes__", ())
            ),
            "sweep_directory": getattr(
                error, "sweep_directory", None
            ),
        }, indent=2), file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1
