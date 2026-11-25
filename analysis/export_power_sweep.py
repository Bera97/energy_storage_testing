"""Exclusive derived-only CP analysis exports; no hardware or public dispatch.

Raw recordings must be retained separately. Export validation is portable and
checks internal consistency, not source authorship or physical qualification.
"""
import csv
import hashlib
import io
import math
import re
from acquisition.spot_acquisition import parse_reading
from pathlib import Path
from analysis.analyze_power_sweep import (
    ANALYSIS_VERSION, SAMPLE_FIELDS, TOTALS, _analyze, _conditions, _cycles, _integrate,
    _preflight, _verify_inventory,
)
from experiments.power_sweep_contract import build_sequence, digest, read_json, require, same, write_json

PHASE_FIELDS = ("stage_id", "scope", "condition_id", "condition_index", "repeat_index", "power_w",
                "phase_index", "cycle", "type", "follows", "time_basis", "sample_count", "interval_count",
                "has_integration_interval", "excluded_probe_count", "excluded_trailing_cc_count",
                "first_sample_time_s", "last_sample_time_s", "recorded_span_s", "maximum_read_bracket_s",
                *TOTALS, "time_weighted_mean_signed_power_w", "mean_discharge_power_error_w")
CYCLE_FIELDS = ("condition_index", "repeat_index", "power_w", "condition_id", "cycle", "charge_stage_id",
                "discharge_stage_id", "charge_recorded_span_s", "discharge_recorded_span_s", "charge_energy_wh",
                "discharge_energy_wh", "charge_mah", "discharge_mah", "observed_discharge_to_charge_energy_ratio",
                "observed_discharge_to_charge_coulomb_ratio")
CONDITION_FIELDS = ("condition_index", "power_w", "cycle_count", "repeat_count", "observed_discharge_cycle_count",
                    "discharge_energy_wh_mean", "discharge_energy_wh_sample_std", "observed_energy_ratio_count", "observed_energy_ratio_mean")


def _table(rows, fields):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _write_text(path, text):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        require(stream.write(text) == len(text), "Short derived file write.")
        stream.flush()


def _file_map(root):
    result = {}
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), "Symlink in analysis export.")
        if path.is_file() and path not in (root / "analysis.json", root / "POWER_ANALYSIS_COMPLETE"):
            raw = path.read_bytes()
            result[path.relative_to(root).as_posix()] = {"sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}
    return result


def _marker(path):
    return "POWER_ANALYSIS_COMPLETE sha256=" + digest(path) + "\n"


def export_power_sweep_analysis(source_directory, output_directory):
    """Validate first; exclusive output; errors retain partial output without a valid marker."""
    source, document, stages, inventory = _preflight(source_directory)
    original = Path(output_directory)
    if original.exists() or original.is_symlink():
        raise FileExistsError("Analysis destination already exists.")
    destination = original.resolve()
    require(destination != source and not destination.is_relative_to(source) and not source.is_relative_to(destination),
            "Analysis destination must be outside, and not enclose, the source.")
    require(destination.parent.is_dir(), "Analysis parent directory must already exist.")
    destination.mkdir(exist_ok=False)
    header = {"power_analysis_export_schema_version": 1, "state": "running", "report": None, "files": {}, "error": None,
              "source_snapshot": {"directory": str(source), "configuration": document["configuration"], "files": inventory}}
    marker_created = False
    try:
        write_json(destination / "analysis.json", header, create=True)
        (destination / "phase_samples").mkdir()
        def save(stage_id, samples):
            _write_text(destination / "phase_samples" / (stage_id + ".csv"), _table(samples, SAMPLE_FIELDS))
        report = _analyze((source, document, stages, inventory), sample_callback=save)
        for name, rows, fields in (("phase_summary.csv", report["phases"], PHASE_FIELDS),
            ("cycle_comparison.csv", report["cycles"], CYCLE_FIELDS), ("condition_summary.csv", report["conditions"], CONDITION_FIELDS)):
            _write_text(destination / name, _table(rows, fields))
        _verify_inventory(source, inventory)
        header.update(state="completed", report=report, files=_file_map(destination))
        write_json(destination / "analysis.json", header)
        with (destination / "POWER_ANALYSIS_COMPLETE").open("x", encoding="utf-8", newline="\n") as stream:
            marker_created = True
            text = _marker(destination / "analysis.json")
            require(stream.write(text) == len(text), "Short analysis marker write.")
            stream.flush()
        checked = validate_power_sweep_analysis(destination)
        require(checked["valid"] is True, "Derived export validation: " + "; ".join(checked["errors"]))
        _verify_inventory(source, inventory)
        return {"state": "completed", "directory": str(destination), "phase_count": len(report["phases"])}
    except BaseException as error:
        if marker_created:
            try:
                (destination / "POWER_ANALYSIS_COMPLETE").unlink(missing_ok=True)
            except BaseException as secondary:
                error.add_note(f"Analysis marker cleanup: {secondary}")
        header.update(state="raised", error={"type": type(error).__name__, "message": str(error), "notes": list(getattr(error, "__notes__", ()))})
        try:
            write_json(destination / "analysis.json", header)
        except BaseException as secondary:
            error.add_note(f"Analysis failure export: {type(secondary).__name__}: {secondary}")
        raise


def _validate(directory):
    root = Path(directory)
    require(root.is_dir() and not root.is_symlink(), "Expected nonsymlink analysis directory.")
    for name in ("analysis.json", "POWER_ANALYSIS_COMPLETE"):
        require((root / name).is_file() and not (root / name).is_symlink(), "Missing/symlink analysis completion.")
    before, marker = (root / "analysis.json").read_bytes(), (root / "POWER_ANALYSIS_COMPLETE").read_bytes()
    require(marker.decode("utf-8") == _marker(root / "analysis.json"), "Analysis completion digest mismatch.")
    header = read_json(root / "analysis.json")
    require(type(header["power_analysis_export_schema_version"]) is int and header["power_analysis_export_schema_version"] == 1
            and header["state"] == "completed" and header["error"] is None, "Analysis is not completed schema-1 evidence.")
    report = header["report"]
    require(type(report["power_analysis_schema_version"]) is int and report["power_analysis_schema_version"] == ANALYSIS_VERSION
            and report["physical_qualification"] is False, "Wrong analysis report version/qualification.")
    same(header["source_snapshot"], {"directory": report["source_directory"], "configuration": report["configuration"],
         "files": report["source_files"]}, "Analysis source snapshot differs from report.")
    _, requests = build_sequence(report["configuration"])
    source_expected = {"power_sweep.json", "POWER_SWEEP_COMPLETE"}
    for request in requests:
        prefix, phase = request["stage_id"], request["phase"]
        source_expected.add(prefix + "/stage.json")
        if phase["type"] == "discharge_cp":
            source_expected.update(prefix + "/power/" + n for n in
                ("power_phase.json", "power_events.jsonl", "power_samples.csv", "POWER_PHASE_COMPLETE"))
        elif phase["type"] in ("charge_cc_cv", "discharge_cc"):
            source_expected.update(prefix + f"/phase_phase_{phase['phase_index']}/" + n for n in
                ("metadata.json", "measurements.csv", "COMPLETE"))
        elif phase.get("rest_mode") == "zero_current_logging":
            source_expected.update(prefix + f"/rest_phase_{phase['phase_index']:04d}/" + n for n in
                ("rest.json", "measurements.csv", "COMPLETE"))
    require(set(report["source_files"]) == source_expected, "Source provenance inventory differs from canonical recording layout.")
    require(isinstance(report["source_directory"], str) and bool(report["source_directory"]), "Missing source provenance location.")
    for item in report["source_files"].values():
        require(set(item) == {"sha256", "size_bytes"} and type(item["size_bytes"]) is int and item["size_bytes"] >= 0
                and isinstance(item["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is not None,
                "Invalid source provenance descriptor.")
    require(report["integration_method"] == "trapezoidal_sample_current_and_sample_V_times_I"
            and report["coverage_policy"] == "per-phase first-to-last retained analysis sample only; no joins or extrapolation"
            and report["ratio_policy"] == "ratios of observed phase coverage, not full-cycle thermodynamic efficiencies; not clamped"
            and report["condition_statistics_policy"] == "arithmetic cycle means; sample standard deviation requires at least two observed cycles",
            "Unknown analysis policy.")
    phases = report["phases"]
    require(len(phases) == len(requests), "Analysis phase count differs from canonical plan.")
    files = _file_map(root)
    same(header["files"], files, "Derived file digests differ.")
    expected = {"phase_summary.csv", "cycle_comparison.csv", "condition_summary.csv"}
    for phase, request in zip(phases, requests):
        for key in ("stage_id", "scope", "condition_id", "condition_index", "repeat_index", "power_w"):
            same(phase[key], request[key], "Analysis phase identity differs.")
        for key in ("type", "phase_index", "cycle"):
            same(phase[key], request["phase"][key], "Analysis phase descriptor differs.")
        same(phase["follows"], request["phase"].get("follows"), "Rest role differs.")
        same(phase["excluded_probe_count"], 1 if phase["type"] == "discharge_cp" else 0, "Probe exclusion count differs.")
        require(type(phase["excluded_trailing_cc_count"]) is int and phase["excluded_trailing_cc_count"] >= 0,
                "Invalid CC trailing-record exclusion count.")
        require(phase["type"] in ("charge_cc_cv", "discharge_cc") or phase["excluded_trailing_cc_count"] == 0,
                "Unexpected trailing-record exclusion.")
        relative = "phase_samples/" + request["stage_id"] + ".csv"
        expected.add(relative)
        raw = (root / relative).read_bytes()
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""), strict=True)
        require(reader.fieldnames == list(SAMPLE_FIELDS), "Unexpected derived sample fields.")
        rows = list(reader)
        require(type(phase["sample_count"]) is int and phase["sample_count"] == len(rows), "Derived sample count mismatch.")
        points = []
        is_cc = phase["type"] in ("charge_cc_cv", "discharge_cc")
        basis = ("within_trace_instrument_timestamp" if is_cc else "host_read_bracket_midpoint_estimate"
                 if phase["type"] == "discharge_cp" else "relative_host_read_bracket_midpoint_estimate"
                 if request["phase"].get("rest_mode") == "zero_current_logging" else "unmeasured_outputs_off_rest")
        same(phase["time_basis"], basis, "Derived time basis differs.")
        for index, row in enumerate(rows):
            require(set(row) == set(SAMPLE_FIELDS) and all(value is not None for value in row.values()), "Malformed derived sample row.")
            point = {k: float(row[k]) for k in ("time_s", "raw_instrument_timestamp_s", "voltage_v", "current_a")}
            require(all(math.isfinite(v) for v in point.values()), "Nonfinite derived sample.")
            point.update(record_index=int(row["record_index"]), status=int(row["status"]))
            require(point["record_index"] == index + (1 if phase["type"] == "discharge_cp" else 0), "Derived record index mismatch.")
            for key in ("host_read_started_s", "host_read_finished_s"):
                point[key] = None if row[key] == "" else float(row[key])
            if is_cc:
                require(point["host_read_started_s"] is None and point["host_read_finished_s"] is None
                        and point["time_s"] == point["raw_instrument_timestamp_s"], "CC derived timestamp differs.")
            else:
                a, b = point["host_read_started_s"], point["host_read_finished_s"]
                require(a is not None and b is not None and math.isfinite(a) and math.isfinite(b) and 0 <= a <= b
                        and point["time_s"] == a + (b-a)/2, "Host midpoint differs from bracket.")
            reading = parse_reading(",".join(str(point[k]) for k in
                ("voltage_v", "current_a", "raw_instrument_timestamp_s", "status")), report["configuration"]["instrument"]["channel"])
            require(not reading.has_fault, "Derived sample reports a fault.")
            if phase["type"] == "discharge_cp":
                require(reading.current_source and not reading.in_compliance and reading.current_a <= 0,
                        "Derived CP source/polarity/compliance differs.")
            points.append(point)
        require(basis != "unmeasured_outputs_off_rest" or not points, "Unmeasured OFF rest cannot contain samples.")
        same(phase["first_sample_time_s"], points[0]["time_s"] if points else None, "First sample time differs.")
        same(phase["last_sample_time_s"], points[-1]["time_s"] if points else None, "Last sample time differs.")
        same(phase["maximum_read_bracket_s"], max((p["host_read_finished_s"]-p["host_read_started_s"] for p in points
             if p["host_read_started_s"] is not None), default=None), "Read bracket summary differs.")
        calculated = _integrate(points)
        has_interval = len(points) >= 2
        same(phase["has_integration_interval"], has_interval, "Integration interval flag differs.")
        same(phase["interval_count"], max(0, len(points)-1), "Integration interval count differs.")
        same(phase["recorded_span_s"], calculated["summary"]["recorded_span_s"] if calculated else None, "Recorded span differs.")
        for key in TOTALS:
            same(phase[key], calculated["summary"][key] if has_interval else None, "Derived phase total differs: " + key)
        mean = calculated["summary"]["time_weighted_mean_signed_power_w"] if has_interval else None
        same(phase["time_weighted_mean_signed_power_w"], mean, "Derived mean power differs.")
        error = -mean-phase["power_w"] if phase["type"] == "discharge_cp" and has_interval else None
        same(phase["mean_discharge_power_error_w"], error, "Derived tracking error differs.")
        derived = [{**p, **{k: d[k] for k in SAMPLE_FIELDS if k in d}} for p, d in zip(points, calculated["samples"])] if calculated else []
        require(raw.decode("utf-8") == _table(derived, SAMPLE_FIELDS), "Derived cumulative sample calculations differ.")
    require(set(files) == expected, "Unexpected/missing derived files.")
    same(report["cycles"], _cycles(phases), "Cycle comparison differs from phase results.")
    same(report["conditions"], _conditions(report["cycles"]), "Condition statistics differ.")
    for name, rows, fields in (("phase_summary.csv", phases, PHASE_FIELDS), ("cycle_comparison.csv", report["cycles"], CYCLE_FIELDS),
                              ("condition_summary.csv", report["conditions"], CONDITION_FIELDS)):
        require((root / name).read_text(encoding="utf-8") == _table(rows, fields), "Summary CSV differs from report: " + name)
    require((root / "analysis.json").read_bytes() == before and (root / "POWER_ANALYSIS_COMPLETE").read_bytes() == marker,
            "Analysis metadata changed during validation.")
    same(_file_map(root), files, "Analysis files changed during validation.")
    return {"valid": True, "errors": [], "phase_count": len(phases), "physical_qualification": False}


def validate_power_sweep_analysis(directory):
    """Read-only integrity/recalculation check, independent of original source location."""
    try:
        return _validate(directory)
    except Exception as error:
        return {"valid": False, "errors": [f"{type(error).__name__}: {error}"], "physical_qualification": False}
