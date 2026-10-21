"""Export offline recorded-run analysis to a new directory.

No instrument-driver imports or hardware communication.

Usage:
    python -m analysis.export_recorded_analysis RUN_DIRECTORY OUTPUT_DIRECTORY

The output directory must not already exist.
Its parent directory must exist.

Source recording files are never intentionally modified.
Only analyze recordings that are no longer being written.

Exit codes:
    0: analysis files written; warnings may exist
    1: validation, calculation, or export failed

If writing fails after directory creation, partial output may remain.
A partial directory must not be treated as a completed export.
"""

import argparse
import csv
import json
from pathlib import Path
import sys

from analysis.analyze_recorded_run import analyze_run


ANALYSIS_FIELDS = (
    "sample_index",
    "elapsed_time_s",
    "dt_s",
    "signed_power_w",
    "cumulative_signed_charge_c",
    "cumulative_signed_charge_mah",
    "cumulative_signed_energy_j",
    "cumulative_signed_energy_wh",
    "cumulative_positive_charge_c",
    "cumulative_positive_charge_mah",
    "cumulative_negative_charge_c",
    "cumulative_negative_charge_mah",
    "cumulative_positive_energy_j",
    "cumulative_positive_energy_wh",
    "cumulative_negative_energy_j",
    "cumulative_negative_energy_wh",
)


def export_analysis(directory, output_directory):
    """Analyze a recording and write two files to a new directory.

    Returns:
        The summary document written to summary.json.

    Raises:
        ValueError: Analysis fails or the destination is within
            the source recording directory.
        OSError: Destination creation or file writing fails.

    Existing output directories are never reused or overwritten.
    """
    source = Path(directory).resolve()
    destination = Path(output_directory).resolve()

    if destination == source or source in destination.parents:
        raise ValueError(
            "Output directory must be outside the source "
            "recording directory."
        )

    result = analyze_run(source)

    samples = result["calculation"]["samples"]

    calculation_summary = {
        key: value
        for key, value in result["calculation"].items()
        if key != "samples"
    }

    summary = {
        **result,
        "calculation": calculation_summary,
        "export": {
            "schema_version": 2,
            "analysis_csv": "analysis.csv",
            "summary_json": "summary.json",
            "analysis_csv_fields": list(ANALYSIS_FIELDS),
            "sample_index_mapping": (
                "sample_index equals source CSV record_index"
            ),
            "first_dt_policy": (
                "The first dt_s is empty because no preceding "
                "recorded sample exists."
            ),
            "directional_totals_policy": (
                "Positive and negative totals are nonnegative "
                "magnitudes, not device-labelled delivered "
                "or absorbed quantities."
            ),
        },
    }

    summary_text = json.dumps(
        summary,
        indent=2,
        allow_nan=False,
    ) + "\n"

    destination.mkdir()

    with (destination / "analysis.csv").open(
        "x",
        encoding="utf-8",
        newline="",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=ANALYSIS_FIELDS,
        )
        writer.writeheader()
        writer.writerows(samples)

    with (destination / "summary.json").open(
        "x",
        encoding="utf-8",
        newline="\n",
    ) as handle:
        handle.write(summary_text)

    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Export offline charge and energy analysis "
            "to a new directory."
        )
    )
    parser.add_argument(
        "directory",
        help="Source incremental_csv run directory",
    )
    parser.add_argument(
        "output_directory",
        help=(
            "New output directory outside the source run; "
            "parent must already exist"
        ),
    )
    arguments = parser.parse_args(argv)

    try:
        summary = export_analysis(
            arguments.directory,
            arguments.output_directory,
        )
    except (OSError, ValueError, csv.Error) as error:
        print(
            f"Analysis export failed: {error}",
            file=sys.stderr,
        )
        return 1

    print(
        f"Analysis written to: "
        f"{Path(arguments.output_directory).resolve()}"
    )
    print(
        f"Samples: "
        f"{summary['calculation']['summary']['sample_count']}"
    )
    print(
        f"Acquisition state: "
        f"{summary['acquisition_outcome']['state']}"
    )

    for warning in summary["warnings"]:
        print(f"Warning: {warning}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())