"""Single public CLI. Importing this module does not connect to hardware."""

import argparse
import csv
from datetime import datetime
from importlib import import_module
import json
from pathlib import Path
import sys


ROUTES = {
    "plan": "experiments.run_live",
    "dual-run": "experiments.dual_live",
    "dual-check": "experiments.dual_live",
    "dual-probe": "experiments.dual_instrument_io",
    "dual-plan": "experiments.dual_preconditioning",
    "dual-simulate": "experiments.dual_coordinator",
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


def _unified_config_paths(documents, paths):
    """Return validated (document, path) pairs for one or two profiles."""

    if not 1 <= len(documents) <= 2:
        raise SystemExit(
            "Pass --config once for one channel or twice for "
            "both channels of one instrument."
        )

    return list(zip(documents, paths))


def _unified_run_name(document):
    """Build the timestamped run-folder name from output.file_name."""

    name = document["output"].get("file_name")

    if name is None:
        raise ValueError(
            "Set output.file_name in the YAML profile; the run "
            "folder becomes <timestamp>_<file_name> inside "
            "output.root_directory."
        )

    if name.startswith("REPLACE_"):
        raise ValueError(
            "Replace output.file_name with a real name for your run."
        )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    return f"{stamp}_{name}"


def _unified_analyze(document, run_name):
    """Analyze one recorded run; folder names come from YAML only."""

    output = document["output"]

    if "analysis_directory" not in output:
        raise ValueError(
            "Set output.analysis_directory in the YAML profile "
            "to use --analyze."
        )

    root = Path(output["root_directory"])
    source = root / run_name

    if not source.is_dir():
        raise ValueError(
            f"No recorded run named {run_name!r} inside "
            f"{root.resolve()}."
        )

    destination = Path(output["analysis_directory"]) / run_name
    destination.parent.mkdir(parents=True, exist_ok=True)

    if (source / "dual_experiment.json").is_file():
        return import_module(
            "experiments.dual_analysis"
        ).main([str(source), str(destination)])

    if (source / "experiment.json").is_file():
        return _analyze_main([str(source), str(destination)])

    raise ValueError(
        f"{source.resolve()} contains no experiment manifest; "
        "pass the folder name of a recorded single run or dual "
        "batch inside output.root_directory."
    )


def _unified_main(arguments):
    parser = argparse.ArgumentParser(
        prog="python -m smu",
        description=(
            "One-command experiment interface: preview, execute, "
            "and analyze from one YAML profile. One --config runs "
            "one channel; two --config arguments run both channels "
            "of one instrument. Without mode flags, preview only."
        ),
        epilog=(
            "--execute is the explicit instruction to run hardware. "
            "--execute --analyze analyzes the run when it is over."
        ),
    )

    parser.add_argument(
        "--config",
        action="append",
        required=True,
        help="YAML profile; pass twice for dual channel",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Validate and print the plan; no hardware (default)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run the configured experiment on hardware",
    )
    parser.add_argument(
        "--analyze",
        nargs="?",
        const="",
        default=None,
        metavar="RUN_NAME",
        help=(
            "Analyze a recorded run. With --execute, analyze the "
            "run just recorded; without --execute, RUN_NAME is the "
            "folder inside output.root_directory"
        ),
    )

    parsed = parser.parse_args(arguments)

    if parsed.preview and parsed.execute:
        parser.error("--preview and --execute are exclusive.")
    if parsed.preview and parsed.analyze is not None:
        parser.error("--preview cannot be combined with --analyze.")
    if parsed.execute and parsed.analyze not in (None, ""):
        parser.error(
            "--execute --analyze does not take a run name; the "
            "run just recorded is analyzed."
        )
    if parsed.analyze is not None and parsed.analyze == "" and not parsed.execute:
        parser.error(
            "--analyze without --execute requires the run folder "
            "name inside output.root_directory."
        )

    config_loader = import_module("experiments.config")
    documents = []
    paths = []

    for path in parsed.config:
        documents.append(config_loader.load_config(path)["config"])
        paths.append(path)

    try:
        _unified_config_paths(documents, paths)
        run_name = None

        if parsed.execute:
            run_name = _unified_run_name(documents[0])
            forwarded = ["--execute", "--experiment-id", run_name]

            if len(documents) == 2:
                forwarded = [
                    "dual-run",
                    "--config", paths[0],
                    "--config", paths[1],
                    "--execute",
                    "--batch-id", run_name,
                ]

                status = import_module(
                    "experiments.dual_live"
                ).main(forwarded)
            else:
                forwarded = [
                    "--config", paths[0],
                    "--execute",
                    "--experiment-id", run_name,
                ]

                status = import_module(
                    "experiments.run_live"
                ).main(forwarded)

            if status != 0:
                if parsed.analyze is not None:
                    print(
                        "Run did not complete; analysis skipped.",
                        file=sys.stderr,
                    )

                return status

            if parsed.analyze is None:
                return 0
        elif parsed.analyze is not None:
            run_name = parsed.analyze
        else:
            parsed.preview = True

        if parsed.preview:
            if len(documents) == 2:
                return import_module(
                    "experiments.dual_preconditioning"
                ).main([
                    "--config", paths[0],
                    "--config", paths[1],
                ])

            return import_module(
                "experiments.run_live"
            ).main(["--config", paths[0], "--dry-run"])

        return _unified_analyze(documents[0], run_name)
    except ValueError as error:
        print(f"Command failed: {error}", file=sys.stderr)
        return 1


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
            "Without a command word, the flag interface applies: "
            "--config PROFILE [--preview | --execute [--analyze] | "
            "--analyze RUN_NAME]. Legacy subcommands remain "
            "available; run still requires --execute for hardware "
            "execution."
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

    if not arguments[0].startswith("-"):
        command = parser.parse_args(
            arguments[:1]
        ).command
        forwarded = arguments[1:]

        if command in ("dual-run", "dual-check"):
            return import_module("experiments.dual_live").main([command, *forwarded])

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

    return _unified_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())