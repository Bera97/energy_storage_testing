"""Read-only recorded CP sweep validation; no connection, outputs or energy export.

Hashes detect inconsistency, not authorship. Re-reading is not a transactional
snapshot. Keep recordings unchanged while running or validating a sweep.
"""
import csv
from dataclasses import asdict
from pathlib import Path
from acquisition.spot_acquisition import parse_reading
from analysis.power_phase import validate_power_phase
from analysis.validate_recorded_run import validate_run
from experiments import zero_current_rest as zrest
from experiments.config import require_charge_monitoring_settings
from experiments.charge_cv_classifier import VoltageSourceCVClassifier
from experiments.charge_trace_monitor import ChargeTraceMonitor
from experiments.discharge_trace_monitor import DischargeTraceMonitor
from experiments.charge_controller import charge_stop_reason
from experiments.constant_power import _number
from experiments.phase_outcome import validate_transport_outcome
from experiments.power_sweep_contract import (
    MAXIMUM_RECORDS, base_phase, build_sequence, completion_text, digest, read_json, require, same, tree_files,
)


def _cleanup(value):
    require(isinstance(value, dict), "Missing cleanup evidence.")
    for key in ("shutdown_commands_returned", "outputs_reported_off", "session_close_returned"):
        require(value.get(key) is True, f"Cleanup not successful: {key}")
    same(value.get("output_states"), [False, False], "Both output states must be false.")
    same(value.get("errors"), [], "Cleanup errors disallow completion.")


def _stage(directory, request, configuration, sweep_configuration, policy, plan):
    root = Path(directory)
    require(root.is_dir() and not root.is_symlink(), "Missing or symlink stage directory.")
    path = root / "stage.json"
    require(path.is_file() and not path.is_symlink(), "Missing or symlink stage manifest.")
    before = path.read_bytes()
    doc = read_json(path)
    require(type(doc.get("power_sweep_stage_schema_version")) is int and doc["power_sweep_stage_schema_version"] == 1
            and doc.get("state") == "completed", "Stage is not completed schema-1 evidence.")
    for key, expected in (("request", request), ("configuration", configuration),
        ("sweep_configuration", sweep_configuration), ("execution_policy", policy),
        ("execution_settings", plan["execution_settings"])):
        same(doc[key], expected, "Stage metadata differs: " + key)
    settings = plan["execution_settings"]
    parts = doc["instrument_identity"].split(",")
    require(len(parts) == 4 and all(p.strip() for p in parts) and parts[1].strip().upper() == "B2902B"
            and parts[2].strip() == settings["expected_serial"], "Stage identity/serial mismatch.")
    require(doc["resource_address"] == plan["resource"] and isinstance(doc["driver_type"], str)
            and bool(doc["driver_type"].strip()), "Missing resource/driver provenance.")
    start = _number(doc["started_s"], "stage start", zero=True)
    finish = _number(doc["finished_s"], "stage finish", zero=True)
    require(start <= finish and doc.get("error") is None, "Stage clock/error state is invalid.")
    _cleanup(doc["cleanup"])
    files = tree_files(root)
    same(doc["files"], files, "Stage file digests changed.")
    phase, evidence = request["phase"], doc["evidence"]
    require(isinstance(evidence, dict), "Missing stage evidence.")
    if phase["type"] == "discharge_cp":
        same(evidence, {"kind": "power", "directory": "power"}, "Wrong power reference.")
        checked = validate_power_phase(root / "power")
        require(checked["valid"] is True, "Power phase: " + "; ".join(checked["errors"]))
        native = read_json(root / "power/power_phase.json")
        meta = native["metadata"]
        for key, expected in (("limits", phase["controller_limits"]), ("configuration_snapshot", sweep_configuration),
            ("channel", plan["channel"]), ("specimen_id", plan["specimen_id"]),
            ("maximum_probe_current_a", policy["maximum_probe_current_a"]), ("maximum_samples", MAXIMUM_RECORDS)):
            same(meta[key], expected, "Power phase metadata differs: " + key)
        instrument = meta["instrument"]
        for key in ("expected_serial", "maximum_voltage_v", "maximum_current_a"):
            same(instrument[key], settings[key], "Power instrument settings differ.")
        same(instrument["four_wire"], configuration["instrument"]["four_wire"], "Power wiring differs.")
        same(instrument["nplc"], _number(configuration["instrument"]["nplc"], "NPLC"), "Power NPLC differs.")
        for key in ("resource_address", "driver_type"):
            same(instrument[key], doc[key], "Power driver provenance differs.")
        outcome = native["outcome"]
        require(start <= _number(outcome["phase_started_s"], "power start", zero=True)
                <= _number(outcome["cleanup_finished_s"], "power cleanup finish", zero=True) <= finish,
                "Power clock lies outside its stage.")
        expected_files = {"power/" + name for name in ("power_phase.json", "power_events.jsonl", "power_samples.csv", "POWER_PHASE_COMPLETE")}
    elif phase["type"] in ("charge_cc_cv", "discharge_cc"):
        name = f"phase_phase_{phase['phase_index']}"
        same(evidence, {"kind": "incremental_csv", "directory": name}, "Wrong CC reference.")
        checked = validate_run(root / name)
        require(checked["valid"] is True and checked["state"] == "returned", "CC validation: " + "; ".join(checked["errors"]))
        native = read_json(root / name / "metadata.json")
        meta = native["metadata"]
        same(meta["configuration"], configuration, "CC configuration differs.")
        same(meta["channel"], plan["channel"], "CC channel differs.")
        same(meta["planned_phase"], base_phase(configuration, request), "CC planned identity differs.")
        execution = meta["execution_policy"]
        require(execution["measurement_time_basis"] == "first_recorded_sample"
                and execution["host_phase_start"] == "before_output_activation", "CC timing policy differs.")
        for key in ("maximum_voltage_v", "maximum_current_a"):
            same(execution[key], settings[key], "CC execution ceilings differ.")
        stored, report, outcome = native["experiment_phase"], native["phase_transport_report"], native["outcome"]
        for key, value in phase.items():
            same(stored[key], value, "CC stored phase differs: " + key)
        validate_transport_outcome(stored, report)
        reason = charge_stop_reason(phase) if phase["type"] == "charge_cc_cv" else "voltage_min"
        require(outcome["state"] == "returned" and outcome["stop_reason"] == reason
                and outcome["shutdown_verified"] is True and outcome["fault_record_indices"] == [], "CC completion differs.")
        for key in ("records_flushed", "retained_count"):
            same(outcome[key], checked["rows_read"], "CC count differs.")
        same(report["records_flushed"], checked["rows_read"], "CC transport count differs.")
        for key in ("requested_count", "triggering_record_index"):
            same(outcome[key], report[key], "CC outcome/report differ.")
        activation = report["activation_evidence"]
        same(activation["channel"], plan["channel"], "CC activation channel differs.")
        require(start <= _number(activation["host_before_enable_s"], "before enable", zero=True)
                <= _number(activation["host_after_verification_s"], "after enable", zero=True) <= finish,
                "CC activation clock lies outside stage.")
        require(activation["measurement_time_basis"] == "first_recorded_sample"
                and activation["output_enable_time_established"] is False, "CC activation basis differs.")
        with (root / name / "measurements.csv").open(encoding="utf-8", newline="") as stream:
            readings = []
            for row in csv.DictReader(stream):
                require(int(row["channel"]) == plan["channel"], "CC CSV channel differs.")
                reading = parse_reading(",".join(row[k] for k in ("voltage_v", "current_a", "instrument_timestamp_s", "status")), plan["channel"])
                require(row["has_fault"] == str(reading.has_fault) and row["in_compliance"] == str(reading.in_compliance)
                        and not reading.has_fault, "CC CSV status/flags differ or report a fault.")
                readings.append(reading)
        same(asdict(readings[outcome["triggering_record_index"]]), outcome["triggering_reading"], "CC trigger differs from CSV.")
        timing = report["measurement_timing"]
        require(timing["measurement_time_basis"] == "first_recorded_sample"
                and timing["output_enable_time_established"] is False, "CC measurement basis differs.")
        same(timing["instrument_timestamp_origin_s"], readings[0].timestamp_s, "CC timestamp anchor differs.")
        same(timing["measurement_elapsed_at_origin_s"], 0.0, "CC elapsed anchor differs.")
        monitoring = require_charge_monitoring_settings(configuration)
        arguments = dict(save_batch=lambda batch: None, instrument_timestamp_origin_s=readings[0].timestamp_s,
                         phase_elapsed_at_origin_s=0.0, maximum_sample_gap_s=monitoring["maximum_sample_gap_s"])
        if phase["type"] == "charge_cc_cv":
            classifier = VoltageSourceCVClassifier(**{k: monitoring[k] for k in
                ("target_voltage_v", "voltage_tolerance_v", "maximum_voltage_v")})
            monitor = ChargeTraceMonitor(stored, cv_classifier=classifier, **arguments)
        else:
            monitor = DischargeTraceMonitor(stored, maximum_voltage_v=monitoring["maximum_voltage_v"], **arguments)
        decision = monitor.ingest(tuple(readings))
        require(decision.stop_reason == reason and decision.triggering_record_index == outcome["triggering_record_index"],
                "CC controller replay differs from persisted endpoint.")
        expected_files = {name + "/" + item for item in ("metadata.json", "measurements.csv", "COMPLETE")}
    elif phase["type"] == "rest" and zrest.is_measured(phase):
        name = f"rest_phase_{phase['phase_index']:04d}"
        require(evidence.get("kind") == "measured_rest" and evidence.get("directory") == name, "Wrong measured-rest reference.")
        same(evidence["report"]["run_directory"], str((root / name).resolve()), "Measured-rest path differs.")
        zrest.validate_record(evidence["report"], base_phase(configuration, request), configuration, root)
        native = read_json(root / name / "rest.json")
        activation = native["activation"]
        require(start <= _number(activation["host_before_enable_s"], "rest before enable", zero=True)
                <= _number(activation["host_after_readback_s"], "rest after enable", zero=True) <= finish,
                "Measured-rest activation lies outside stage.")
        require(_number(evidence["report"]["elapsed_s"], "measured-rest elapsed", zero=True) <= finish - start + 1e-9,
                "Measured-rest elapsed exceeds stage duration.")
        expected_files = {name + "/" + item for item in ("rest.json", "measurements.csv", "COMPLETE")}
    else:
        require(phase["type"] == "rest" and evidence.get("kind") == "outputs_off_rest", "Unsupported stage evidence.")
        report = evidence["report"]
        require(report["state"] == "returned" and report["phase_type"] == "rest"
                and report["initial_shutdown_verified"] is True and report["shutdown_verified"] is True
                and report["both_outputs_reported_off"] is True and report["error"] is None
                and report["cleanup_error"] is None, "Outputs-OFF rest did not complete.")
        same(report["requested_duration_s"], float(phase["duration_s"]), "Rest duration differs.")
        elapsed = _number(report["elapsed_s"], "rest elapsed", zero=True)
        require(phase["duration_s"] <= elapsed + 1e-9 and elapsed <= finish - start + 1e-9, "Rest elapsed duration is invalid.")
        require(type(report["output_check_count"]) is int and report["output_check_count"] >= 1, "Rest has no output checks.")
        expected_files = set()
    require(set(files) == expected_files, "Unexpected/missing stage files.")
    same(tree_files(root), files, "Stage files changed during validation.")
    require(path.read_bytes() == before, "Stage manifest changed during validation.")
    return {"valid": True, "errors": [], "stage_id": request["stage_id"], "started_s": start, "finished_s": finish, "files": files}


def validate_power_sweep_stage(directory, request, configuration, sweep_configuration, policy, *, _plan=None):
    """Internal gate; _plan is an already-built canonical plan, not external evidence."""
    try:
        plan = build_sequence(sweep_configuration)[0] if _plan is None else _plan
        return _stage(directory, request, configuration, sweep_configuration, policy, plan)
    except Exception as error:
        return {"valid": False, "errors": [f"{type(error).__name__}: {error}"]}


def _sweep(directory):
    root = Path(directory)
    require(root.is_dir() and not root.is_symlink(), "Expected nonsymlink sweep root.")
    for name in ("power_sweep.json", "POWER_SWEEP_COMPLETE"):
        require((root / name).is_file() and not (root / name).is_symlink(), "Missing/symlink sweep evidence.")
    before, marker = (root / "power_sweep.json").read_bytes(), (root / "POWER_SWEEP_COMPLETE").read_bytes()
    require(marker.decode("utf-8") == completion_text(root / "power_sweep.json"), "Sweep manifest digest mismatch.")
    doc = read_json(root / "power_sweep.json")
    require(type(doc["power_sweep_schema_version"]) is int and doc["power_sweep_schema_version"] == 1
            and doc["state"] == "completed" and doc["physical_qualification"] is False and doc["error"] is None,
            "Sweep is not completed schema-1 evidence.")
    for name in ("COMPLETE", "SIMULATION_COMPLETE", "DUAL_COMPLETE", "ANALYSIS_COMPLETE"):
        require(not (root / name).exists() and not (root / name).is_symlink(), "Mixed completion schemas.")
    plan, requests = build_sequence(doc["configuration"])
    same(doc["requests"], requests, "Stored sequence differs from canonical configuration.")
    policy = doc["execution_policy"]
    same(sorted(policy), ["maximum_probe_current_a", "maximum_records", "session_policy"], "Unexpected policy fields.")
    require(_number(policy["maximum_probe_current_a"], "probe ceiling") <= doc["configuration"]["power_control"]["maximum_current_a"], "Invalid probe ceiling.")
    same(policy["maximum_records"], MAXIMUM_RECORDS, "Unsupported record budget.")
    require(policy["session_policy"] == "new_owned_session_per_stage", "Unsupported session policy.")
    references = doc["stages"]
    require(type(doc["completed_stage_count"]) is int and doc["completed_stage_count"] == len(requests)
            and len(references) == len(requests) and doc["active_stage_id"] is None, "Incomplete count/state.")
    previous, saved = None, []
    for reference, request in zip(references, requests):
        require(set(reference) == {"stage_id", "stage_sha256"} and reference["stage_id"] == request["stage_id"], "Stage reference/order mismatch.")
        path = root / request["stage_id"]
        require(path.is_dir() and not path.is_symlink() and (path / "stage.json").is_file()
                and not (path / "stage.json").is_symlink(), "Missing/symlink stage reference.")
        original = (path / "stage.json").read_bytes()
        require(reference["stage_sha256"] == digest(path / "stage.json"), "Stage manifest digest mismatch.")
        checked = _stage(path, request, plan["initial_preparation"]["configuration"], doc["configuration"], policy, plan)
        require(previous is None or checked["started_s"] >= previous, "Stage clock moved backwards.")
        previous = checked["finished_s"]
        saved.append((path / "stage.json", original, checked["files"]))
    require((root / "power_sweep.json").read_bytes() == before and (root / "POWER_SWEEP_COMPLETE").read_bytes() == marker,
            "Sweep changed during validation.")
    for path, raw, files in saved:
        require(not path.is_symlink() and path.read_bytes() == raw, "Stage manifest changed during sweep validation.")
        same(tree_files(path.parent), files, "Stage files changed during sweep validation.")
    return {"valid": True, "errors": [], "state": "completed", "stage_count": len(requests),
            "condition_count": plan["condition_count"], "physical_qualification": False}


def validate_power_sweep(directory):
    """Only successfully completed sweeps are valid; partial evidence stays invalid."""
    try:
        return _sweep(directory)
    except Exception as error:
        return {"valid": False, "errors": [f"{type(error).__name__}: {error}"], "state": None, "physical_qualification": False}
