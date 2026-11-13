"""Whole-experiment single-channel analysis export.

Importing this module does not connect to hardware.
"""

import csv
from importlib import import_module
import json
from pathlib import Path
import sys


METRICS = (
    "sample_count",
    "recorded_span_s",
    "signed_charge_mah",
    "signed_energy_wh",
    "positive_energy_wh",
    "negative_energy_wh",
    "signed_charge_c",
    "signed_energy_j",
    "positive_energy_j",
    "negative_energy_j",
)

CSV_FIELDS = (
    "phase_index",
    "cycle",
    "type",
    "source_directory",
    *METRICS,
    "warning_count",
)


def _write_json(path, document):
    text = json.dumps(
        document,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    )
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text + "\n")


def export_experiment(
    directory,
    output_directory,
    *,
    phase_root=None,
    initial_discharge_only=False,
):
    """Export a completed experiment. Inputs must remain unchanged.

    Existing destinations are refused. Failure can leave partial output;
    only ANALYSIS_COMPLETE establishes successful export finalization.
    """
    validator = import_module(
        "analysis.validate_preconditioning_experiment"
    )
    exporter = import_module(
        "analysis.export_recorded_analysis"
    )

    source = Path(directory).resolve(strict=True)
    destination = Path(output_directory).resolve()
    manifest_path = source / "experiment.json"

    scope_options = (
        {"initial_discharge_only": True}
        if initial_discharge_only else {}
    )
    if type(initial_discharge_only) is not bool:
        raise ValueError("initial_discharge_only must be a boolean.")
    document = validator._read_json(manifest_path)
    validation = validator.validate_experiment(
        source,
        phase_root=phase_root,
        **scope_options,
    )

    if validation["valid"] is not True:
        raise ValueError(
            "Experiment validation failed: "
            + "; ".join(validation["errors"])
        )

    if validation["state"] != "completed":
        raise ValueError(
            "Whole-experiment export requires a completed experiment."
        )

    if document != validator._read_json(manifest_path):
        raise ValueError(
            "Manifest changed during validation; "
            "stop concurrent writes."
        )

    if destination.exists():
        raise FileExistsError(
            f"Output directory already exists: {destination}"
        )

    if not destination.parent.is_dir():
        raise ValueError(
            "Output parent must already exist."
        )

    allowed_root = Path(
        source.parent if phase_root is None else phase_root
    ).resolve(strict=True)

    references = []
    seen = set()

    for evidence_index, phase in enumerate(document["plan"]["phases"]):
        if phase["type"] not in (
            "charge_cc_cv",
            "discharge_cc",
        ):
            continue

        entry = document["backend_evidence"][evidence_index]

        if any(
            entry[key] != phase[key]
            for key in ("phase_index", "cycle", "type")
        ):
            raise ValueError(
                "Phase evidence identity differs from the plan."
            )

        reference = Path(entry["run_directory"])

        if not reference.is_absolute():
            raise ValueError(
                "Phase references must be absolute."
            )

        reference = reference.resolve(strict=True)

        if (
            not reference.is_dir()
            or not reference.is_relative_to(allowed_root)
        ):
            raise ValueError(
                "Phase reference is not a directory "
                "within the allowed root."
            )

        if reference in seen:
            raise ValueError(
                "Duplicate phase recording reference."
            )

        seen.add(reference)
        references.append((phase, reference))

    if (
        not references
        or len(references)
        != validation["phase_recordings_checked"]
    ):
        raise ValueError(
            "Active-phase count differs from validated recordings."
        )

    protected_directories = (
        source,
        *(reference for _, reference in references),
    )

    for protected in protected_directories:
        if (
            destination == protected
            or destination.is_relative_to(protected)
        ):
            raise ValueError(
                "Output must be outside the experiment "
                "and all phase recordings."
            )

    destination.mkdir()
    rows = []

    for phase, reference in references:
        name = (
            f"phase_{phase['phase_index']:04d}_"
            f"{phase['type']}"
        )

        result = exporter.export_analysis(
            reference,
            destination / name,
        )

        if (
            result["acquisition_outcome"]["state"]
            != "returned"
        ):
            raise ValueError(
                "A phase no longer has a returned "
                "acquisition outcome."
            )

        metrics = result["calculation"]["summary"]
        warnings = list(result["warnings"])

        rows.append({
            "phase_index": phase["phase_index"],
            "cycle": phase["cycle"],
            "type": phase["type"],
            "source_directory": str(reference),
            "analysis_directory": name,
            **{
                key: metrics[key]
                for key in METRICS
            },
            "warning_count": len(warnings),
            "warnings": warnings,
        })

    if document != validator._read_json(manifest_path):
        raise ValueError(
            "Manifest changed during export; "
            "output is incomplete."
        )

    final_validation = validator.validate_experiment(
        source,
        phase_root=phase_root,
        **scope_options,
    )

    if (
        final_validation["valid"] is not True
        or final_validation["state"] != "completed"
    ):
        raise ValueError(
            "Experiment no longer validates; "
            "output is incomplete."
        )

    report = {
        "schema_version": 1,
        "analysis_state": "completed",
        "experiment_state": "completed",
        "source_directory": str(source),
        "validation": final_validation,
        "warnings": list(final_validation["warnings"]),
        "sign_policy": (
            "Signed quantities retain measurement signs; "
            "directional energies are nonnegative magnitudes."
        ),
        "phases": rows,
    }

    _write_json(
        destination / "summary.json",
        report,
    )

    with (destination / "summary.csv").open(
        "x",
        encoding="utf-8",
        newline="",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=CSV_FIELDS,
        )
        writer.writeheader()
        writer.writerows(
            {
                key: row[key]
                for key in CSV_FIELDS
            }
            for row in rows
        )

    with (destination / "ANALYSIS_COMPLETE").open(
        "x",
        encoding="utf-8",
    ) as handle:
        handle.write("completed\n")

    return report
def analyze_and_print(
    exporter,
    directory,
    output_directory,
    *,
    dual=False,
    **export_options,
):
    """Run an offline exporter and present its summary."""
    try:
        report = exporter(
            directory,
            output_directory,
            **export_options,
        )
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RuntimeError,
        csv.Error,
    ) as error:
        print(f"Analysis failed: {error}", file=sys.stderr)
        print(
            "Any destination left behind is partial "
            "unless ANALYSIS_COMPLETE exists.",
            file=sys.stderr,
        )
        return 1
    print(f"Analysis: {Path(output_directory).resolve()}")
    header = (
        f"{'Phase':>5} "
        f"{'Type':<14} "
        f"{'Samples':>8} "
        f"{'Span(s)':>10} "
        f"{'Q(mAh)':>12} "
        f"{'E(Wh)':>12} "
        f"{'Q(C)':>12} "
        f"{'E(J)':>12}"
    )
    print((f"{'CH':>2} " if dual else "") + header)
    for row in report["phases"]:
        values = (
            f"{row['phase_index']:>5} "
            f"{row['type']:<14} "
            f"{row['sample_count']:>8} "
            f"{row['recorded_span_s']:>10.3f} "
            f"{row['signed_charge_mah']:>12.6g} "
            f"{row['signed_energy_wh']:>12.6g} "
            f"{row.get('signed_charge_c', row['signed_charge_mah'] * 3.6):>12.6g} "
            f"{row.get('signed_energy_j', row['signed_energy_wh'] * 3600.0):>12.6g}"
        )
        channel_prefix = f"{row['channel']:>2} " if dual else ""
        print(channel_prefix + values)
        warning_prefix = (
            f"WARNING CH{row['channel']} phase {row['phase_index']}: "
            if dual
            else f"WARNING phase {row['phase_index']}: "
        )
        for warning in row["warnings"]:
            print(warning_prefix + str(warning), file=sys.stderr)
    for warning in report["warnings"]:
        print(f"WARNING: {warning}", file=sys.stderr)
    return 0
def _require_sweep(condition, message):
    if not condition:
        raise ValueError(message)
def validate_sweep(directory):
    """Read-only validation of completed sweeps; no filesystem snapshot."""
    from copy import deepcopy
    from analysis.validate_preconditioning_experiment import _read_json
    from experiments.preconditioning_runner import _execution_plan
    from experiments.sweeps import (
        _check_sweep_stage, build_charge_current_sweep_plan,
    )
    result = {
        "valid": False,
        "state": None,
        "stages_checked": 0,
        "phase_recordings_checked": 0,
        "errors": [],
        "warnings": [],
    }
    try:
        root = Path(directory).resolve(strict=True)
        _require_sweep(root.is_dir(), "Sweep source must be a directory.")
        document = _read_json(root / "sweep.json")
        _require_sweep(isinstance(document, dict), "Sweep manifest must be an object.")
        result["state"] = document.get("state")
        _require_sweep(
            type(document.get("sweep_manifest_schema_version")) is int
            and document["sweep_manifest_schema_version"] == 1,
            "Unsupported sweep manifest schema.",
        )
        _require_sweep(
            document.get("state") == "completed",
            "Sweep is not completed.",
        )
        _require_sweep(
            document.get("sweep_id") == root.name,
            "Sweep ID differs from its directory name.",
        )
        _require_sweep(
            document.get("active_stage_id") is None
            and document.get("error") is None
            and document.get("session_policy")
            == "one_verified_closed_session_per_stage",
            "Sweep has inconsistent completion/session evidence.",
        )
        rebuilt = build_charge_current_sweep_plan(document["configuration"])
        stored_plan = document["plan"]
        _require_sweep(isinstance(stored_plan, dict), "Stored plan must be an object.")
        # Descriptive limitation text is not an execution parameter.
        _require_sweep(
            {key: value for key, value in stored_plan.items() if key != "limitations"}
            == {key: value for key, value in rebuilt.items() if key != "limitations"},
            "Stored sweep plan differs from the configured plan.",
        )
        specifications = [
            ("initial_discharge", rebuilt["conditions"][0], True),
            *[
                (entry["condition_id"], entry, False)
                for entry in rebuilt["conditions"]
            ],
        ]
        stages = document["stages"]
        _require_sweep(
            isinstance(stages, list) and len(stages) == len(specifications),
            "Sweep has missing or extra stages.",
        )
        _require_sweep(
            type(document.get("completed_stage_count")) is int
            and document["completed_stage_count"] == len(stages),
            "Sweep completed-stage count is inconsistent.",
        )
        phase_count = 0
        stage_documents = []
        for stage, (stage_id, condition, initial_only) in zip(stages, specifications):
            _require_sweep(isinstance(stage, dict), "Stage must be an object.")
            configuration = deepcopy(condition["config"])
            configuration["output"]["root_directory"] = str(root)
            expected_identity = {
                "stage_id": stage_id,
                "kind": "initial_discharge" if initial_only else "condition",
                "condition_index": None if initial_only else condition["condition_index"],
                "repeat_index": None if initial_only else condition["repeat_index"],
                "charge_current_a": None if initial_only else condition["charge_current_a"],
            }
            _require_sweep(
                all(stage.get(key) == value for key, value in expected_identity.items())
                and stage.get("initial_discharge_only") is initial_only,
                f"Stage order or identity differs: {stage_id}.",
            )
            _require_sweep(
                stage.get("state") == "completed"
                and stage.get("error") is None
                and stage.get("configuration") == configuration
                and stage.get("execution_settings") == condition["execution_settings"],
                f"Stage configuration/completion differs: {stage_id}.",
            )
            reference = Path(stage["experiment_directory"])
            _require_sweep(
                reference.is_absolute()
                and reference.resolve(strict=True) == root / stage_id,
                f"Stage recording path differs: {stage_id}.",
            )
            persisted_validation = stage.get("validation")
            _require_sweep(
                isinstance(persisted_validation, dict)
                and persisted_validation.get("valid") is True
                and persisted_validation.get("state") == "completed"
                and persisted_validation.get("errors") == [],
                f"Stage lacks successful persisted validation: {stage_id}.",
            )
            stage_document = _read_json(reference / "experiment.json")
            _require_sweep(
                isinstance(stage_document, dict)
                and all(
                    isinstance(stage_document.get(field), dict)
                    for field in (
                        "metadata", "session_outcome", "experiment_report"
                    )
                ),
                f"Stage manifest has malformed evidence objects: {stage_id}.",
            )
            checked = _check_sweep_stage(
                reference,
                configuration,
                initial_only,
                stage_document["experiment_report"],
            )
            expected_count = _execution_plan(
                configuration, initial_discharge_only=initial_only
            )["phase_count"]
            _require_sweep(
                type(stage.get("completed_phase_count")) is int
                and stage["completed_phase_count"] == expected_count,
                f"Stage completed-phase count differs: {stage_id}.",
            )
            phase_count += expected_count
            result["stages_checked"] += 1
            result["phase_recordings_checked"] += checked["phase_recordings_checked"]
            result["warnings"].extend(
                f"{stage_id}: {warning}" for warning in checked.get("warnings", [])
            )
            stage_documents.append((reference, stage_document))
        _require_sweep(
            phase_count == rebuilt["total_phase_count"],
            "Sweep completed-phase total differs from its plan.",
        )
        _require_sweep(
            document == _read_json(root / "sweep.json")
            and all(
                stored == _read_json(reference / "experiment.json")
                for reference, stored in stage_documents
            ),
            "A manifest changed during validation; stop concurrent writes.",
        )
        result["valid"] = True
    except (
        OSError, ValueError, TypeError, KeyError, RuntimeError,
        OverflowError, RecursionError,
    ) as error:
        result["errors"].append(f"{type(error).__name__}: {error}")
    return result
SWEEP_CYCLE_FIELDS = (
    "condition_id", "condition_index", "repeat_index", "charge_current_a", "cycle",
    "charge_recorded_span_s", "discharge_recorded_span_s",
    "charge_signed_charge_mah", "discharge_signed_charge_mah",
    "charge_positive_energy_wh", "discharge_negative_energy_wh",
    "recorded_energy_ratio", "warning_count",
)
SWEEP_GROUP_METRICS = {
    "charge_recorded_span_s": "s",
    "discharge_recorded_span_s": "s",
    "charge_signed_charge_mah": "mAh",
    "discharge_signed_charge_mah": "mAh",
    "charge_positive_energy_wh": "Wh",
    "discharge_negative_energy_wh": "Wh",
    "recorded_energy_ratio": "1",
}
def _sweep_cycle_rows(stage, phase_rows):
    """Pair charge/discharge only within the same condition and cycle."""
    import math
    by_cycle = {}
    for row in phase_rows:
        cycle = by_cycle.setdefault(row["cycle"], {})
        _require_sweep(
            row["type"] not in cycle, "Duplicate active phase within a cycle."
        )
        cycle[row["type"]] = row
    rows = []
    for cycle_number, phases in by_cycle.items():
        _require_sweep(
            set(phases) == {"charge_cc_cv", "discharge_cc"},
            "A condition cycle lacks its charge/discharge pair.",
        )
        charge = phases["charge_cc_cv"]
        discharge = phases["discharge_cc"]
        denominator = charge["positive_energy_wh"]
        ratio = (
            discharge["negative_energy_wh"] / denominator
            if denominator > 0 else None
        )
        _require_sweep(
            ratio is None or math.isfinite(ratio),
            "Recorded energy ratio is not finite.",
        )
        rows.append({
            "condition_id": stage["stage_id"],
            "condition_index": stage["condition_index"],
            "repeat_index": stage["repeat_index"],
            "charge_current_a": stage["charge_current_a"],
            "cycle": cycle_number,
            "charge_recorded_span_s": charge["recorded_span_s"],
            "discharge_recorded_span_s": discharge["recorded_span_s"],
            "charge_signed_charge_mah": charge["signed_charge_mah"],
            "discharge_signed_charge_mah": discharge["signed_charge_mah"],
            "charge_positive_energy_wh": denominator,
            "discharge_negative_energy_wh": discharge["negative_energy_wh"],
            "recorded_energy_ratio": ratio,
            "warning_count": charge["warning_count"] + discharge["warning_count"],
        })
    return rows
def _sweep_group_rows(cycles):
    import statistics
    groups = {}
    for row in cycles:
        groups.setdefault(row["charge_current_a"], []).append(row)
    rows = []
    for current, observations in groups.items():
        for metric, unit in SWEEP_GROUP_METRICS.items():
            values = [
                row[metric] for row in observations if row[metric] is not None
            ]
            rows.append({
                "charge_current_a": current,
                "metric": metric,
                "unit": unit,
                "condition_count": len({
                    row["condition_id"] for row in observations
                }),
                "cycle_count": len(observations),
                "observation_count": len(values),
                "mean": statistics.fmean(values) if values else None,
                "sample_stddev": statistics.stdev(values) if len(values) > 1 else None,
                "minimum": min(values) if values else None,
                "maximum": max(values) if values else None,
            })
    return rows
def _write_sweep_csv(path, fields, rows):
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
def export_sweep(directory, output_directory):
    """Export only a validated completed sweep; refuse existing destinations."""
    from analysis.validate_preconditioning_experiment import _read_json
    source = Path(directory).resolve(strict=True)
    destination = Path(output_directory).resolve()
    _require_sweep(
        destination != source
        and not destination.is_relative_to(source)
        and not source.is_relative_to(destination),
        "Sweep output must be outside the source tree and not its ancestor.",
    )
    if destination.exists():
        raise FileExistsError(f"Output directory already exists: {destination}")
    _require_sweep(destination.parent.is_dir(), "Output parent must already exist.")
    document = _read_json(source / "sweep.json")
    validation = validate_sweep(source)
    _require_sweep(
        validation["valid"], "Sweep validation failed: " + "; ".join(validation["errors"])
    )
    _require_sweep(
        document == _read_json(source / "sweep.json"),
        "Sweep manifest changed during validation.",
    )
    snapshots = {
        stage["stage_id"]: _read_json(
            source / stage["stage_id"] / "experiment.json"
        )
        for stage in document["stages"]
    }
    destination.mkdir()
    phase_rows = []
    cycle_rows = []
    for stage in document["stages"]:
        stage_source = source / stage["stage_id"]
        options = (
            {"initial_discharge_only": True}
            if stage["initial_discharge_only"] else {}
        )
        report = export_experiment(
            stage_source,
            destination / stage["stage_id"],
            phase_root=stage_source,
            **options,
        )
        phase_rows.extend({
            "stage_id": stage["stage_id"],
            "stage_kind": stage["kind"],
            "condition_index": stage["condition_index"],
            "repeat_index": stage["repeat_index"],
            "charge_current_a": stage["charge_current_a"],
            **row,
            "analysis_directory": (
                Path(stage["stage_id"]) / row["analysis_directory"]
            ).as_posix(),
        } for row in report["phases"])
        if stage["kind"] == "condition":
            cycle_rows.extend(_sweep_cycle_rows(stage, report["phases"]))
    grouped = _sweep_group_rows(cycle_rows)
    _write_sweep_csv(
        destination / "summary.csv",
        (
            "stage_id", "stage_kind", "condition_index", "repeat_index",
            "charge_current_a", "analysis_directory", *CSV_FIELDS,
        ),
        phase_rows,
    )
    _write_sweep_csv(
        destination / "cycle_comparison.csv", SWEEP_CYCLE_FIELDS, cycle_rows
    )
    _write_sweep_csv(
        destination / "current_summary.csv",
        (
            "charge_current_a", "metric", "unit", "condition_count", "cycle_count",
            "observation_count", "mean", "sample_stddev", "minimum", "maximum",
        ),
        grouped,
    )
    final_validation = validate_sweep(source)
    _require_sweep(
        final_validation["valid"],
        "Sweep no longer validates: " + "; ".join(final_validation["errors"]),
    )
    _require_sweep(
        document == _read_json(source / "sweep.json")
        and all(
            snapshot == _read_json(source / stage_id / "experiment.json")
            for stage_id, snapshot in snapshots.items()
        ),
        "A manifest changed during export; output is incomplete.",
    )
    report = {
        "sweep_analysis_schema_version": 1,
        "analysis_state": "completed",
        "source_directory": str(source),
        "validation": final_validation,
        "warnings": [
            *final_validation["warnings"],
            "Initial preparation is excluded from condition/cycle comparisons.",
            "Recorded spans are sample-to-sample spans, not output-enable durations.",
            "Energy ratio is discharge negative-energy magnitude divided by "
            "charge positive-energy magnitude over the recorded samples; "
            "it is not an independently established device efficiency.",
            "Grouped statistics are descriptive. Cycles within a condition "
            "are not asserted to be independent replicates.",
            "Inputs must remain unchanged; validation is not a filesystem snapshot.",
        ],
        "phases": phase_rows,
        "cycles": cycle_rows,
        "current_summary": grouped,
    }
    _write_json(destination / "summary.json", report)
    with (destination / "ANALYSIS_COMPLETE").open("x", encoding="utf-8") as handle:
        handle.write("completed\n")
    return report
def analyze_sweep(directory, output_directory):
    """Offline export and CLI presentation of a completed sweep."""
    try:
        report = export_sweep(directory, output_directory)
    except (
        OSError, ValueError, TypeError, KeyError, RuntimeError,
        OverflowError, csv.Error,
    ) as error:
        print(f"Sweep analysis failed: {error}", file=sys.stderr)
        print(
            "Any destination left behind is partial unless its top-level "
            "ANALYSIS_COMPLETE exists.",
            file=sys.stderr,
        )
        return 1
    print(f"Sweep analysis: {Path(output_directory).resolve()}")
    print(
        f"{'Condition':<29} {'Cycle':>5} {'I(A)':>8} "
        f"{'Charge span(s)':>15} {'Charge E+(Wh)':>15} {'Discharge E-(Wh)':>18}"
    )
    for row in report["cycles"]:
        print(
            f"{row['condition_id']:<29} {row['cycle']:>5} "
            f"{row['charge_current_a']:>8.5g} "
            f"{row['charge_recorded_span_s']:>15.6g} "
            f"{row['charge_positive_energy_wh']:>15.6g} "
            f"{row['discharge_negative_energy_wh']:>18.6g}"
        )
    for warning in report["warnings"]:
        print(f"WARNING: {warning}", file=sys.stderr)
    return 0
