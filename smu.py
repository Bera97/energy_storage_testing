"""Single public CLI. Importing this module does not connect to hardware."""

import argparse
import csv
from importlib import import_module
import json
from pathlib import Path
import sys


ROUTES = {
    "plan": "experiments.run_live",
    "dual-plan": "experiments.dual_preconditioning",
    "run": "experiments.run_live",
    "check": "analysis.check_preconditioning_experiment",
    "analyze-phase": "analysis.export_recorded_analysis",
}

METRICS = (
    "sample_count",
    "recorded_span_s",
    "signed_charge_mah",
    "signed_energy_wh",
    "positive_energy_wh",
    "negative_energy_wh",
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

    document = validator._read_json(manifest_path)
    validation = validator.validate_experiment(
        source,
        phase_root=phase_root,
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

    for phase in document["plan"]["phases"]:
        if phase["type"] not in (
            "charge_cc_cv",
            "discharge_cc",
        ):
            continue

        entry = document["backend_evidence"][
            phase["phase_index"]
        ]

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


def _analyze_main(argv):
    parser = argparse.ArgumentParser(
        prog="python -m smu analyze",
        description=(
            "Analyze all active phases of a completed, "
            "unchanged experiment."
        ),
    )

    parser.add_argument(
        "directory",
        help="Directory containing experiment.json",
    )
    parser.add_argument(
        "output_directory",
        help="New destination; parent must exist",
    )
    parser.add_argument(
        "--phase-root",
        default=None,
    )

    arguments = parser.parse_args(argv)

    try:
        report = export_experiment(
            arguments.directory,
            arguments.output_directory,
            phase_root=arguments.phase_root,
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
        f"{'Phase':>5} "
        f"{'Type':<14} "
        f"{'Samples':>8} "
        f"{'Span(s)':>10} "
        f"{'Q(mAh)':>12} "
        f"{'E(Wh)':>12}"
    )

    for row in report["phases"]:
        print(
            f"{row['phase_index']:>5} "
            f"{row['type']:<14} "
            f"{row['sample_count']:>8} "
            f"{row['recorded_span_s']:>10.3f} "
            f"{row['signed_charge_mah']:>12.6g} "
            f"{row['signed_energy_wh']:>12.6g}"
        )

        for warning in row["warnings"]:
            print(
                f"WARNING phase {row['phase_index']}: "
                f"{warning}",
                file=sys.stderr,
            )

    for warning in report["warnings"]:
        print(
            f"WARNING: {warning}",
            file=sys.stderr,
        )

    return 0


def main(argv=None):
    arguments = list(
        sys.argv[1:] if argv is None else argv
    )

    parser = argparse.ArgumentParser(
        prog="python -m smu",
        description=(
            "Plan, run, validate, and analyze "
            "energy-storage experiments."
        ),
        epilog=(
            "Use COMMAND --help for options. "
            "run still requires --execute "
            "for hardware execution."
        ),
    )

    parser.add_argument(
        "command",
        choices=(*ROUTES, "analyze"),
    )

    if not arguments:
        parser.print_help(file=sys.stderr)
        return 2

    if arguments[0] in ("-h", "--help"):
        parser.print_help()
        return 0

    command = parser.parse_args(
        arguments[:1]
    ).command
    forwarded = arguments[1:]

    if command == "analyze":
        return _analyze_main(forwarded)

    if command == "plan":
        forwarded = [
            "--dry-run",
            *forwarded,
        ]

    return import_module(
        ROUTES[command]
    ).main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())