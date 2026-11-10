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
import sys

from analysis.validate_preconditioning_experiment import _read_json
from experiments.dual_live import check_batch
from smu import CSV_FIELDS, METRICS


DUAL_CSV_FIELDS = ("channel", *CSV_FIELDS)


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


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m smu analyze-dual",
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

    try:
        report = analyze_batch(
            arguments.directory,
            arguments.output_directory,
        )
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RuntimeError,
        csv.Error,
    ) as error:
        print(
            f"Analysis failed: {error}",
            file=sys.stderr,
        )
        print(
            "Any destination left behind is partial "
            "unless ANALYSIS_COMPLETE exists.",
            file=sys.stderr,
        )
        return 1

    print(
        "Analysis: "
        f"{Path(arguments.output_directory).resolve()}"
    )
    print(
        f"{'CH':>2} "
        f"{'Phase':>5} "
        f"{'Type':<14} "
        f"{'Samples':>8} "
        f"{'Span(s)':>10} "
        f"{'Q(mAh)':>12} "
        f"{'E(Wh)':>12}"
    )

    for row in report["phases"]:
        print(
            f"{row['channel']:>2} "
            f"{row['phase_index']:>5} "
            f"{row['type']:<14} "
            f"{row['sample_count']:>8} "
            f"{row['recorded_span_s']:>10.3f} "
            f"{row['signed_charge_mah']:>12.6g} "
            f"{row['signed_energy_wh']:>12.6g}"
        )

        for warning in row["warnings"]:
            print(
                f"WARNING CH{row['channel']} "
                f"phase {row['phase_index']}: "
                f"{warning}",
                file=sys.stderr,
            )

    for warning in report["warnings"]:
        print(
            f"WARNING: {warning}",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
