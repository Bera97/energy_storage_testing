"""Read-only analysis of incremental streaming run directories.

No instrument-driver imports or hardware communication.
Does not create, modify, or delete files.

Only analyze recordings that are no longer being written.

An accepted recording is not necessarily a successfully completed
acquisition. The validation report and original acquisition outcome
are preserved separately from the calculated quantities.
"""

import csv
import json
from pathlib import Path

from analysis.energy_charge import calculate_energy_charge
from analysis.validate_recorded_run import validate_run


def analyze_run(directory):
    """Validate a recorded run and return its offline analysis.

    Supports metadata.json with export_mode == "incremental_csv".

    Returns:
        A JSON-compatible dictionary containing the source metadata,
        validation report, acquisition outcome, and calculations.

    Raises:
        ValueError: Validation fails, the recording has no samples,
            inputs cannot be read, or calculations fail.

    This function does not lock source files. The recording must
    remain unchanged throughout validation and analysis.
    """
    directory = Path(directory)
    validation = validate_run(directory)

    if not validation["valid"]:
        details = "; ".join(validation["errors"])
        raise ValueError(
            f"Recorded-run validation failed: {details}"
        )

    if validation["rows_read"] == 0:
        raise ValueError(
            "The recording contains no samples to analyze."
        )

    timestamps = []
    voltages = []
    currents = []

    try:
        document = json.loads(
            (directory / "metadata.json").read_text(
                encoding="utf-8"
            )
        )

        with (directory / "measurements.csv").open(
            encoding="utf-8",
            newline="",
        ) as handle:
            reader = csv.DictReader(handle, strict=True)

            for row in reader:
                timestamps.append(
                    float(row["instrument_timestamp_s"])
                )
                voltages.append(float(row["voltage_v"]))
                currents.append(float(row["current_a"]))

    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
        csv.Error,
    ) as error:
        raise ValueError(
            f"Cannot read validated recording: {error}"
        ) from error

    if len(timestamps) != validation["rows_read"]:
        raise ValueError(
            "CSV row count changed after validation. "
            "Analyze only recordings that are no longer being written."
        )

    calculation = calculate_energy_charge(
        timestamps,
        voltages,
        currents,
    )

    warnings = list(validation["warnings"])
    warnings.extend(calculation["warnings"])

    return {
        "analysis_schema_version": 1,
        "source_directory": str(directory),
        "source_format": "incremental_csv",
        "validation": validation,
        "source_metadata": document,
        "acquisition_outcome": document["outcome"],
        "warnings": warnings,
        "calculation": calculation,
    }