"""Single public CLI. Importing this module does not connect to hardware."""

import argparse
import json
from datetime import datetime
from importlib import import_module
from pathlib import Path
import sys

from analysis.export_experiment import export_experiment
from analysis.export_experiment import analyze_and_print



def _analyze_single(directory, output_directory, *, phase_root=None):
    """Export and print single-run analysis without parsing arguments."""
    return analyze_and_print(
        export_experiment,
        directory,
        output_directory,
        phase_root=phase_root,
    )


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

    if (
        not isinstance(run_name, str)
        or not run_name
        or run_name in (".", "..")
        or "/" in run_name
        or chr(92) in run_name
    ):
        raise ValueError("RUN_NAME must be a single run-folder name.")
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

    if (source / "sweep.json").is_file():
        return import_module("analysis.export_experiment").analyze_sweep(
            str(source), str(destination)
        )
    if (source / "dual_experiment.json").is_file():
        if document["experiment"]["type"] == "charge_current_sweep":
            return import_module(
                "analysis.dual_experiment"
            ).analyze_dual_sweep(str(source), str(destination))
        return import_module(
            "analysis.dual_experiment"
        ).analyze_run(str(source), str(destination))

    if (source / "experiment.json").is_file():
        return _analyze_single(str(source), str(destination))

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
        if any(
            doc["experiment"]["type"] == "charge_current_sweep"
            for doc in documents
        ):
            if len(documents) == 2:
                if any(
                    doc["experiment"]["type"] != "charge_current_sweep"
                    for doc in documents
                ):
                    raise ValueError(
                        "Use two sweep profiles or two preconditioning profiles."
                    )
                if parsed.analyze is not None and any(
                    "analysis_directory" not in doc["output"]
                    for doc in documents
                ):
                    raise ValueError("Set output.analysis_directory in both profiles.")
                planner = import_module("experiments.sweeps")
                planner.build_dual_charge_current_sweep_plan(documents)
                if parsed.execute:
                    run_name = _unified_run_name(documents[0])
                    status = import_module(
                        "experiments.dual_live"
                    ).run_configured(
                        documents, execute_requested=True, batch_id=run_name
                    )
                    if status != 0:
                        if parsed.analyze is not None:
                            print(
                                "Run did not complete; analysis skipped.",
                                file=sys.stderr,
                            )
                        return status
                    if parsed.analyze is not None:
                        return _unified_analyze(documents[0], run_name)
                    return 0
                if parsed.analyze is not None:
                    return _unified_analyze(documents[0], parsed.analyze)
                print(json.dumps(
                    planner.build_dual_charge_current_sweep_plan(documents),
                    indent=2, allow_nan=False,
                ))
                return 0
            if len(documents) != 1:
                raise ValueError("Supply one or two sweep profiles.")
            document = documents[0]
            if (
                parsed.analyze is not None
                and "analysis_directory" not in document["output"]
            ):
                raise ValueError(
                    "Set output.analysis_directory in the YAML profile "
                    "to use --analyze."
                )
            planner = import_module("experiments.sweeps")
            if parsed.execute:
                run_name = _unified_run_name(document)
                status = planner.run_configured_sweep(
                    document, sweep_id=run_name
                )
                if status != 0:
                    if parsed.analyze is not None:
                        print(
                            "Run did not complete; analysis skipped.",
                            file=sys.stderr,
                        )
                    return status
                if parsed.analyze is not None:
                    return _unified_analyze(document, run_name)
                return 0
            if parsed.analyze is not None:
                return _unified_analyze(document, parsed.analyze)
            plan = planner.build_charge_current_sweep_plan(document)
            print(json.dumps(plan, indent=2, allow_nan=False))
            return 0

        run_name = None

        if parsed.execute:
            run_name = _unified_run_name(documents[0])


            if len(documents) == 2:


                status = import_module("experiments.dual_live").run_configured(
                    documents, execute_requested=True,
                    batch_id=run_name,
                )
            else:


                status = import_module("experiments.run_live").run_configured(
                    documents[0], execute=True,
                    experiment_id=run_name,
                )

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
                return import_module("experiments.dual_preconditioning").preview_configured(documents)

            return import_module("experiments.run_live").run_configured(documents[0])

        return _unified_analyze(documents[0], run_name)
    except ValueError as error:
        print(f"Command failed: {error}", file=sys.stderr)
        return 1


def main(argv=None):
    """Run the public preview/execute/analyze flag interface."""
    arguments = list(
        sys.argv[1:] if argv is None else argv
    )
    if not arguments:
        print(
            "usage: python -m smu --config PROFILE "
            "[--preview | --execute [--analyze] | "
            "--analyze RUN_NAME]",
            file=sys.stderr,
        )
        return 2
    try:
        return _unified_main(arguments)
    except SystemExit as error:
        if error.code == 0:
            return 0
        raise


if __name__ == "__main__":
    raise SystemExit(main())
