"""Read-only offline analysis of validated single-channel CP sweep recordings.

CC uses within-trace instrument timestamps through the triggering record. CP
and measured rest use host read-bracket midpoints (estimates, not instrument time).
Never join phases, probe/loaded samples, or unmeasured gaps. No public dispatch.
"""
import csv
import hashlib
import io
from pathlib import Path, PurePosixPath
import statistics
from analysis.energy_charge import calculate_energy_charge
from analysis.power_sweep import validate_power_sweep
from experiments.power_sweep_contract import digest, read_json, require, snapshot

ANALYSIS_VERSION = 1
TOTALS = ("signed_charge_mah", "positive_charge_mah", "negative_charge_mah",
          "signed_energy_wh", "positive_energy_wh", "negative_energy_wh")
SAMPLE_FIELDS = ("record_index", "time_s", "raw_instrument_timestamp_s", "host_read_started_s",
                 "host_read_finished_s", "voltage_v", "current_a", "status", "elapsed_time_s", "dt_s",
                 "signed_power_w", "cumulative_signed_charge_mah", "cumulative_signed_energy_wh",
                 "cumulative_positive_energy_wh", "cumulative_negative_energy_wh")


def _bound_path(root, relative):
    path = PurePosixPath(relative)
    require(not path.is_absolute() and bool(path.parts) and all(p not in (".", "..") for p in path.parts),
            "Unsafe source reference.")
    current = root
    for part in path.parts:
        current = current / part
        require(not current.is_symlink(), "Source symlinks are not permitted.")
    require(current.is_file(), "Missing source file: " + relative)
    return current


def _verify_inventory(root, inventory):
    for name, expected in inventory.items():
        path = _bound_path(root, name)
        require(path.stat().st_size == expected["size_bytes"] and digest(path) == expected["sha256"],
                "Source changed: " + name)


def _preflight(directory):
    original = Path(directory)
    require(not original.is_symlink(), "Source root must not be a symlink.")
    root = original.resolve(strict=True)
    path = _bound_path(root, "power_sweep.json")
    before = path.read_bytes()
    checked = validate_power_sweep(root)
    require(checked["valid"] is True, "Sweep validation failed: " + "; ".join(checked["errors"]))
    require(path.read_bytes() == before, "Sweep changed during preflight.")
    document = read_json(path)
    inventory = {}
    def add(name):
        file = _bound_path(root, name)
        inventory[name] = {"sha256": digest(file), "size_bytes": file.stat().st_size}
    add("power_sweep.json")
    add("POWER_SWEEP_COMPLETE")
    stages = []
    for request, reference in zip(document["requests"], document["stages"]):
        prefix = request["stage_id"]
        add(prefix + "/stage.json")
        require(inventory[prefix + "/stage.json"]["sha256"] == reference["stage_sha256"], "Stage changed after validation.")
        stage = read_json(root / prefix / "stage.json")
        for relative, descriptor in stage["files"].items():
            inventory[prefix + "/" + relative] = snapshot(descriptor)
        stages.append(stage)
    require(inventory["power_sweep.json"]["sha256"] == hashlib.sha256(before).hexdigest(), "Sweep changed after preflight validation.")
    _verify_inventory(root, inventory)
    return root, document, stages, inventory


def _csv_rows(root, relative, inventory):
    path = _bound_path(root, relative)
    raw = path.read_bytes()
    require(len(raw) == inventory[relative]["size_bytes"] and hashlib.sha256(raw).hexdigest() == inventory[relative]["sha256"],
            "Source changed before calculation: " + relative)
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""), strict=True)
    rows = list(reader)
    require(all(None not in row and all(v is not None for v in row.values()) for row in rows), "Malformed sample CSV.")
    return rows


def _points(stage, rows, root, inventory):
    request, phase, evidence = stage["request"], stage["request"]["phase"], stage["evidence"]
    excluded_probe, excluded_tail = 0, 0
    if evidence["kind"] == "incremental_csv":
        relative = request["stage_id"] + "/" + evidence["directory"] + "/metadata.json"
        # Metadata is bound by the preflight inventory and checked again at exit.
        meta = read_json(_bound_path(root, relative))
        last = meta["outcome"]["triggering_record_index"]
        excluded_tail = len(rows) - last - 1
        rows = rows[:last + 1]
        basis = "within_trace_instrument_timestamp"
    elif evidence["kind"] == "power":
        excluded_probe = sum(row["stage"] == "zero_current_probe" for row in rows)
        rows = [row for row in rows if row["stage"] == "power_discharge"]
        basis = "host_read_bracket_midpoint_estimate"
    else:
        basis = "relative_host_read_bracket_midpoint_estimate"
    points = []
    for index, row in enumerate(rows):
        if phase["type"] in ("charge_cc_cv", "discharge_cc"):
            before = after = None
            raw = float(row["instrument_timestamp_s"])
            time = raw
            record = int(row["record_index"])
        else:
            before = float(row["host_read_started_s"] if phase["type"] == "discharge_cp" else row["host_before_s"])
            after = float(row["host_read_finished_s"] if phase["type"] == "discharge_cp" else row["host_after_s"])
            raw = float(row["raw_instrument_timestamp_s"] if phase["type"] == "discharge_cp" else row["instrument_timestamp_s"])
            time = before + (after - before) / 2
            record = int(row["record_index"]) if phase["type"] == "discharge_cp" else index
        points.append({"record_index": record, "time_s": time, "raw_instrument_timestamp_s": raw,
                       "host_read_started_s": before, "host_read_finished_s": after,
                       "voltage_v": float(row["voltage_v"]), "current_a": float(row["current_a"]), "status": int(row["status"])})
    return points, basis, excluded_probe, excluded_tail


def _integrate(points):
    if not points:
        return None
    result = calculate_energy_charge([p["time_s"] for p in points], [p["voltage_v"] for p in points], [p["current_a"] for p in points])
    # Do not expose legacy 'instrument timestamp' labels for host-derived time.
    for name in ("first_instrument_timestamp_s", "last_instrument_timestamp_s"):
        result["summary"].pop(name)
    return result


def _phase(root, stage, inventory):
    request, evidence = stage["request"], stage["evidence"]
    phase = request["phase"]
    if evidence["kind"] == "outputs_off_rest":
        points, basis, probes, tail = [], "unmeasured_outputs_off_rest", 0, 0
    else:
        name = "power_samples.csv" if evidence["kind"] == "power" else "measurements.csv"
        relative = request["stage_id"] + "/" + evidence["directory"] + "/" + name
        points, basis, probes, tail = _points(stage, _csv_rows(root, relative, inventory), root, inventory)
    calculation = _integrate(points)
    has_interval = len(points) >= 2
    warnings = []
    if not has_interval:
        warnings.append("No observed integration interval; integrated totals are null, not measured zero.")
    if phase["type"] == "discharge_cp":
        warnings.append("Probe and command-to-first-loaded-sample coverage excluded; host midpoint time is an estimate.")
    if phase["type"] == "rest":
        warnings.append("Rest data, when measured, are SMU-terminal quantities, not internal self-discharge.")
    summary = {key: request[key] for key in ("stage_id", "scope", "condition_id", "condition_index", "repeat_index", "power_w")}
    summary.update(phase_index=phase["phase_index"], cycle=phase["cycle"], type=phase["type"],
                   follows=phase.get("follows"), time_basis=basis, sample_count=len(points),
                   interval_count=max(0, len(points)-1), has_integration_interval=has_interval,
                   excluded_probe_count=probes, excluded_trailing_cc_count=tail,
                   first_sample_time_s=points[0]["time_s"] if points else None,
                   last_sample_time_s=points[-1]["time_s"] if points else None,
                   recorded_span_s=calculation["summary"]["recorded_span_s"] if calculation else None,
                   maximum_read_bracket_s=max((p["host_read_finished_s"]-p["host_read_started_s"] for p in points
                                               if p["host_read_started_s"] is not None), default=None), warnings=warnings)
    for key in TOTALS:
        summary[key] = calculation["summary"][key] if has_interval else None
    summary["time_weighted_mean_signed_power_w"] = calculation["summary"]["time_weighted_mean_signed_power_w"] if has_interval else None
    summary["mean_discharge_power_error_w"] = (-summary["time_weighted_mean_signed_power_w"] - request["power_w"]
                                              if phase["type"] == "discharge_cp" and has_interval else None)
    samples = []
    if calculation:
        for point, derived in zip(points, calculation["samples"]):
            samples.append({**point, **{k: derived[k] for k in SAMPLE_FIELDS if k in derived}})
    return summary, samples


def _ratio(numerator, denominator):
    return numerator / denominator if numerator is not None and denominator is not None and denominator > 0 else None


def _cycles(phases):
    groups = {}
    for phase in phases:
        if phase["scope"] != "condition":
            continue
        groups.setdefault((phase["condition_id"], phase["cycle"]), []).append(phase)
    rows = []
    for (condition, cycle), group in groups.items():
        charge = next(p for p in group if p["type"] == "charge_cc_cv")
        discharge = next(p for p in group if p["type"] == "discharge_cp")
        row = {k: discharge[k] for k in ("condition_index", "repeat_index", "power_w")}
        row.update(condition_id=condition, cycle=cycle, charge_stage_id=charge["stage_id"], discharge_stage_id=discharge["stage_id"],
            charge_recorded_span_s=charge["recorded_span_s"], discharge_recorded_span_s=discharge["recorded_span_s"],
            charge_energy_wh=charge["positive_energy_wh"], discharge_energy_wh=discharge["negative_energy_wh"],
            charge_mah=charge["positive_charge_mah"], discharge_mah=discharge["negative_charge_mah"],
            observed_discharge_to_charge_energy_ratio=_ratio(discharge["negative_energy_wh"], charge["positive_energy_wh"]),
            observed_discharge_to_charge_coulomb_ratio=_ratio(discharge["negative_charge_mah"], charge["positive_charge_mah"]))
        rows.append(row)
    return rows


def _conditions(cycles):
    groups = {}
    for row in cycles:
        groups.setdefault((row["condition_index"], row["power_w"]), []).append(row)
    result = []
    for (index, power), rows in groups.items():
        energies = [r["discharge_energy_wh"] for r in rows if r["discharge_energy_wh"] is not None]
        ratios = [r["observed_discharge_to_charge_energy_ratio"] for r in rows if r["observed_discharge_to_charge_energy_ratio"] is not None]
        result.append({"condition_index": index, "power_w": power, "cycle_count": len(rows),
            "repeat_count": len({r["repeat_index"] for r in rows}), "observed_discharge_cycle_count": len(energies),
            "discharge_energy_wh_mean": statistics.mean(energies) if energies else None,
            "discharge_energy_wh_sample_std": statistics.stdev(energies) if len(energies)>1 else None,
            "observed_energy_ratio_count": len(ratios), "observed_energy_ratio_mean": statistics.mean(ratios) if ratios else None})
    return result


def _analyze(preflight, sample_callback=None):
    root, document, stages, inventory = preflight
    phases = []
    for stage in stages:
        summary, samples = _phase(root, stage, inventory)
        phases.append(summary)
        if sample_callback is not None:
            sample_callback(summary["stage_id"], samples)
        del samples
    cycles = _cycles(phases)
    _verify_inventory(root, inventory)
    checked = validate_power_sweep(root)
    require(checked["valid"] is True, "Source failed final revalidation: " + "; ".join(checked["errors"]))
    _verify_inventory(root, inventory)
    result = {"power_analysis_schema_version": ANALYSIS_VERSION, "physical_qualification": False,
        "source_directory": str(root), "configuration": document["configuration"], "source_files": inventory,
        "integration_method": "trapezoidal_sample_current_and_sample_V_times_I",
        "coverage_policy": "per-phase first-to-last retained analysis sample only; no joins or extrapolation",
        "ratio_policy": "ratios of observed phase coverage, not full-cycle thermodynamic efficiencies; not clamped",
        "condition_statistics_policy": "arithmetic cycle means; sample standard deviation requires at least two observed cycles",
        "warnings": ["CC trace and CP host-midpoint time bases differ; no cross-session clock mapping is assumed.",
            "Initial CC preparation, probes, inter-stage gaps and unmeasured OFF rests are excluded from cycle ratios.",
            "Ratios do not establish complete round-trip efficiency; retain original source files with this derived export."],
        "phases": phases, "cycles": cycles, "conditions": _conditions(cycles)}
    # Reject overflow/nonfinite derived statistics before returning or exporting.
    return snapshot(result)


def analyze_power_sweep(directory):
    """Read-only; validates a complete sweep before and after calculation."""
    return _analyze(_preflight(directory))
