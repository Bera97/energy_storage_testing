"""Offline whole-batch analysis for dual-channel runs.

No instrument imports or hardware communication. Reads the dual
batch manifest, validates the evidence, exports per-phase analysis
for every completed active phase, and writes a combined summary
with an ANALYSIS_COMPLETE marker.

Existing destinations are refused; a destination without
ANALYSIS_COMPLETE is partial and must not be trusted.
"""

import argparse
import csv
from importlib import import_module
import json
from pathlib import Path

from analysis.validate_preconditioning_experiment import _read_json
import math
from analysis.validate_recorded_run import validate_run
from experiments.phase_outcome import validate_transport_outcome
from analysis.export_experiment import CSV_FIELDS, METRICS
from analysis.export_experiment import analyze_and_print


DUAL_CSV_FIELDS = ("channel", *CSV_FIELDS)


def _require_batch(condition, message):
    if not condition:
        raise ValueError(message)


def check_batch(directory, *, require_marker=True):
    from experiments.dual_preconditioning import build_dual_plan

    root = Path(directory).resolve()
    result = {
        "valid": False,
        "phase_recordings_checked": 0,
        "errors": [],
        "warnings": [],
    }
    try:
        doc = _read_json(root / "dual_experiment.json")
        _require_batch(
            doc["dual_manifest_schema_version"] == 1,
            "Unsupported dual manifest.",
        )
        _require_batch(
            doc["state"] == "completed",
            "Dual experiment is not completed.",
        )
        if require_marker:
            _require_batch(
                (root / "DUAL_COMPLETE").is_file(),
                "Missing DUAL_COMPLETE.",
            )
        plan = doc["plan"]
        rebuilt = build_dual_plan(
            [
                preview["acquisition_plan"]["configuration"]
                for preview in plan["channels"]
            ],
            maximum_records=plan["channels"][0][
                "acquisition_plan"
            ]["acquisition_planning_limit"],
        )
        _require_batch(plan == rebuilt, "Stored plan does not validate.")
        report = doc["report"]
        _require_batch(
            report["state"] == "completed"
            and report["simulation_only"] is False
            and report["hardware_execution_enabled"] is True
            and report.get("error") is None,
            "Report does not establish real-backend completion.",
        )
        _require_batch(
            report["startup_stop_verified"] is True
            and report["final_stop_verified"] is True,
            "Global shutdown evidence is incomplete.",
        )
        _require_batch(
            report["session_close_returned"] is True
            and not report["cleanup_errors"],
            "Cleanup did not complete.",
        )
        identity = doc["identity"].split(",")
        _require_batch(
            len(identity) == 4
            and identity[1].strip().upper() == "B2902B"
            and identity[2].strip() == doc["expected_serial"],
            "Instrument identity mismatch.",
        )
        _require_batch(
            len(report["channels"]) == 2,
            "Expected two channel reports.",
        )
        seen = set()
        for preview, channel in zip(
            plan["channels"], report["channels"]
        ):
            _require_batch(
                channel["channel"] == preview["channel"]
                and channel["specimen_id"] == preview["specimen_id"]
                and channel["state"] == "completed",
                "Channel report mismatch.",
            )
            phases = preview["acquisition_plan"]["phases"]
            _require_batch(
                len(phases) == len(channel["outcomes"]),
                "Incomplete phase outcomes.",
            )
            previous = None
            for phase, outcome in zip(
                phases, channel["outcomes"]
            ):
                _require_batch(
                    all(
                        outcome[key] == phase[key]
                        for key in ("phase_index", "cycle", "type")
                    ),
                    "Phase identity mismatch.",
                )
                _require_batch(
                    outcome["state"] == "completed"
                    and outcome["stop_verified"] is True,
                    "Phase stop not verified.",
                )
                start = outcome["started_s"]
                finish = outcome["finished_s"]
                _require_batch(
                    all(
                        type(value) in (int, float)
                        and math.isfinite(value)
                        for value in (start, finish)
                    )
                    and finish >= start
                    and (previous is None or start >= previous),
                    "Invalid phase timing.",
                )
                previous = finish
                if phase["type"] == "rest":
                    _require_batch(
                        outcome["stop_reason"] == "rest_elapsed"
                        and finish - start + 1e-9
                        >= phase["duration_s"],
                        "Invalid rest evidence.",
                    )
                    continue
                reference = Path(outcome["run_directory"])
                _require_batch(
                    reference.is_absolute(),
                    "Recording reference must be absolute.",
                )
                reference = reference.resolve(strict=True)
                _require_batch(
                    reference.is_relative_to(root)
                    and reference != root
                    and reference not in seen,
                    "Invalid or duplicate recording path.",
                )
                seen.add(reference)
                validation = validate_run(reference)
                _require_batch(
                    validation["valid"]
                    and validation["state"] == "returned",
                    "Invalid phase recording: "
                    + str(validation["errors"]),
                )
                _require_batch(
                    validation.get("fault_flagged_rows", 0) == 0,
                    "Recording contains measurement faults.",
                )
                metadata = _read_json(
                    reference / "metadata.json"
                )
                _require_batch(
                    metadata["metadata"]["channel"]
                    == channel["channel"]
                    and metadata["experiment_phase"] == phase,
                    "Recorded phase differs from plan.",
                )
                transport = metadata["phase_transport_report"]
                validate_transport_outcome(phase, transport)
                _require_batch(
                    transport["retained_count"]
                    == validation["rows_read"]
                    and outcome["stop_reason"]
                    == transport["stop_reason"],
                    "Recording count or endpoint mismatch.",
                )
                result["phase_recordings_checked"] += 1
                result["warnings"].extend(
                    f"CH{channel['channel']} "
                    f"phase {phase['phase_index']}: {warning}"
                    for warning in validation["warnings"]
                )
        result["valid"] = True
    except (
        OSError, ValueError, TypeError, KeyError, RuntimeError
    ) as error:
        result["errors"].append(
            f"{type(error).__name__}: {error}"
        )
    return result


def analyze_batch(directory, output_directory):
    """Validate a dual batch and export per-phase analysis.

    Returns the combined summary document. Inputs must remain
    unchanged during the export.
    """

    source = Path(directory).resolve(strict=True)
    destination = Path(output_directory).resolve()

    if destination == source or source in destination.parents:
        raise ValueError(
            "Output must be outside the dual batch directory."
        )

    if destination.exists():
        raise FileExistsError(
            f"Output directory already exists: {destination}"
        )

    validation = check_batch(source)

    if validation["valid"] is not True:
        raise ValueError(
            "Dual batch validation failed: "
            + "; ".join(validation["errors"])
        )

    document = _read_json(source / "dual_experiment.json")
    if document["plan"].get("experiment_type") == "dual_charge_current_sweep":
        return export_dual_sweep(source, destination)
    destination.mkdir(parents=True)

    exporter = import_module(
        "analysis.export_recorded_analysis"
    )

    rows = []

    for entry in document["progress"]:
        if entry.get("type") not in (
            "charge_cc_cv",
            "discharge_cc",
        ):
            continue

        if entry.get("state") != "completed":
            continue

        reference = entry.get("run_directory")

        if not reference:
            continue

        name = (
            f"channel_{entry['channel']}"
            f"_phase_{entry['phase_index']:04d}"
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

        rows.append({
            "channel": entry["channel"],
            "phase_index": entry["phase_index"],
            "cycle": entry["cycle"],
            "type": entry["type"],
            "source_directory": str(reference),
            "analysis_directory": name,
            **{
                key: metrics[key]
                for key in METRICS
            },
            "warning_count": len(result["warnings"]),
            "warnings": list(result["warnings"]),
        })

    if not rows:
        raise ValueError(
            "The batch contains no completed active-phase "
            "recordings to analyze."
        )

    final_validation = check_batch(source)

    if final_validation["valid"] is not True:
        raise ValueError(
            "Batch no longer validates; output is incomplete."
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

    with (destination / "summary.json").open(
        "x",
        encoding="utf-8",
    ) as handle:
        handle.write(
            json.dumps(
                report,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        )

    with (destination / "summary.csv").open(
        "x",
        encoding="utf-8",
        newline="",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=DUAL_CSV_FIELDS,
        )
        writer.writeheader()
        writer.writerows(
            {
                key: row[key]
                for key in DUAL_CSV_FIELDS
            }
            for row in rows
        )

    with (destination / "ANALYSIS_COMPLETE").open(
        "x",
        encoding="utf-8",
    ) as handle:
        handle.write("completed\n")

    return report


def analyze_run(directory, output_directory):
    """Export and print dual-run analysis without parsing arguments."""
    return analyze_and_print(
        analyze_batch,
        directory,
        output_directory,
        dual=True,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="dual-analysis backend",
        description=(
            "Analyze all completed active phases of a finished, "
            "unchanged dual batch."
        ),
    )

    parser.add_argument(
        "directory",
        help="Directory containing dual_experiment.json",
    )
    parser.add_argument(
        "output_directory",
        help="New destination outside the batch",
    )

    arguments = parser.parse_args(argv)

    return analyze_run(
        arguments.directory,
        arguments.output_directory,
    )


if __name__ == "__main__":
    raise SystemExit(main())
def export_dual_sweep(directory, output_directory):
    """Validate and export both sweep channels without pooling specimens."""
    from analysis.export_experiment import (
        SWEEP_CYCLE_FIELDS,
        _sweep_cycle_rows,
        _sweep_group_rows,
        _write_json,
        _write_sweep_csv,
    )
    source = Path(directory).resolve(strict=True)
    destination = Path(output_directory).resolve()
    _require_batch(
        destination != source
        and not destination.is_relative_to(source)
        and not source.is_relative_to(destination),
        "Analysis must be outside the source tree and not its ancestor.",
    )
    if destination.exists():
        raise FileExistsError(f"Output directory already exists: {destination}")
    document = _read_json(source / "dual_experiment.json")
    _require_batch(
        document["plan"].get("experiment_type") == "dual_charge_current_sweep",
        "Expected a dual charging-current sweep.",
    )
    validation = check_batch(source)
    _require_batch(
        validation["valid"],
        "Dual sweep validation failed: " + "; ".join(validation["errors"]),
    )
    _require_batch(
        document == _read_json(source / "dual_experiment.json"),
        "Manifest changed during validation.",
    )
    destination.mkdir(parents=True)
    exporter = import_module("analysis.export_recorded_analysis")
    phases = []
    cycles = []
    groups = []
    for preview, channel_report in zip(
        document["plan"]["channels"], document["report"]["channels"]
    ):
        channel = preview["channel"]
        specimen = preview["specimen_id"]
        condition_rows = {}
        conditions = {
            entry["condition_id"]: entry
            for entry in preview["sweep_plan"]["conditions"]
        }
        for phase, outcome in zip(
            preview["acquisition_plan"]["phases"],
            channel_report["outcomes"],
        ):
            if phase["type"] == "rest":
                continue
            name = f"channel_{channel}_phase_{phase['phase_index']:04d}"
            result = exporter.export_analysis(
                outcome["run_directory"], destination / name
            )
            _require_batch(
                result["acquisition_outcome"]["state"] == "returned",
                "Phase no longer has a returned recording.",
            )
            metrics = result["calculation"]["summary"]
            row = {
                "channel": channel,
                "specimen_id": specimen,
                "stage_id": phase["sweep_stage_id"],
                "condition_index": phase["sweep_condition_index"],
                "repeat_index": phase["sweep_repeat_index"],
                "charge_current_a": phase["sweep_charge_current_a"],
                "phase_index": phase["phase_index"],
                "cycle": phase["cycle"],
                "type": phase["type"],
                "source_directory": outcome["run_directory"],
                "analysis_directory": name,
                **{key: metrics[key] for key in METRICS},
                "warning_count": len(result["warnings"]),
                "warnings": list(result["warnings"]),
            }
            phases.append(row)
            if phase["sweep_stage_id"] != "initial_discharge":
                condition_rows.setdefault(
                    phase["sweep_stage_id"], []
                ).append(row)
        channel_cycles = []
        for stage_id, rows in condition_rows.items():
            condition = conditions[stage_id]
            stage = {**condition, "stage_id": stage_id}
            channel_cycles.extend({
                "channel": channel,
                "specimen_id": specimen,
                **row,
            } for row in _sweep_cycle_rows(stage, rows))
        cycles.extend(channel_cycles)
        groups.extend({
            "channel": channel,
            "specimen_id": specimen,
            **row,
        } for row in _sweep_group_rows(channel_cycles))
    _write_sweep_csv(
        destination / "summary.csv",
        (
            "channel", "specimen_id", "stage_id", "condition_index",
            "repeat_index", "charge_current_a", "analysis_directory", *CSV_FIELDS,
        ),
        phases,
    )
    _write_sweep_csv(
        destination / "cycle_comparison.csv",
        ("channel", "specimen_id", *SWEEP_CYCLE_FIELDS),
        cycles,
    )
    _write_sweep_csv(
        destination / "current_summary.csv",
        (
            "channel", "specimen_id", "charge_current_a", "metric", "unit",
            "condition_count", "cycle_count", "observation_count", "mean",
            "sample_stddev", "minimum", "maximum",
        ),
        groups,
    )
    final_validation = check_batch(source)
    _require_batch(
        final_validation["valid"]
        and document == _read_json(source / "dual_experiment.json"),
        "Dual sweep changed or no longer validates; analysis is incomplete.",
    )
    report = {
        "dual_sweep_analysis_schema_version": 1,
        "analysis_state": "completed",
        "source_directory": str(source),
        "validation": final_validation,
        "warnings": [
            *final_validation["warnings"],
            "Initial preparation is excluded from condition comparisons.",
            "Recorded spans are sample-to-sample, not output-enable durations.",
            "Signed charge retains measurement signs; directional energy "
            "magnitudes are not independently established device efficiency.",
            "Statistics are descriptive and kept separate by channel/specimen.",
            "Inputs must remain unchanged; validation is not a filesystem snapshot.",
        ],
        "phases": phases,
        "cycles": cycles,
        "current_summary": groups,
    }
    _write_json(destination / "summary.json", report)
    with (destination / "ANALYSIS_COMPLETE").open("x", encoding="utf-8") as handle:
        handle.write("completed\n")
    return report
def analyze_dual_sweep(directory, output_directory):
    """Offline dual sweep export with per-channel multi-unit presentation."""
    import sys
    from analysis.export_experiment import _print_sweep_summary
    try:
        report = export_dual_sweep(directory, output_directory)
    except (
        OSError, ValueError, TypeError, KeyError, RuntimeError,
        OverflowError, csv.Error,
    ) as error:
        print(f"Dual sweep analysis failed: {error}", file=sys.stderr)
        print(
            "Any destination is partial unless its ANALYSIS_COMPLETE exists.",
            file=sys.stderr,
        )
        return 1
    for channel in sorted({row["channel"] for row in report["cycles"]}):
        rows = [row for row in report["cycles"] if row["channel"] == channel]
        print(f"\nCH{channel} ? specimen {rows[0]['specimen_id']}")
        _print_sweep_summary(
            {"cycles": rows, "warnings": []}, output_directory
        )
    for warning in report["warnings"]:
        print(f"WARNING: {warning}", file=sys.stderr)
    return 0
