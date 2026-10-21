"""Read-only validation of incremental streaming run directories.

No instrument-driver imports or hardware communication.

Supported:
    metadata.json with export_mode == "incremental_csv"
    measurements.csv
    COMPLETE marker, when successfully finalized

An accepted failed acquisition is not a successful experiment.
Validation checks file consistency, not measurement accuracy,
electrical safety, or scientific completeness.

CLI:
    python analysis/validate_recorded_run.py RUN_DIRECTORY

Exit codes:
    0: no validation errors; warnings may exist
    1: validation errors found
"""

import argparse
import csv
import json
import math
from pathlib import Path


FIELDS = (
    "record_index",
    "channel",
    "instrument_timestamp_s",
    "voltage_v",
    "current_a",
    "signed_power_w",
    "status",
    "in_compliance",
    "has_fault",
)


def _count(value):
    return type(value) is int and value >= 0


def validate_run(directory):
    """Return a JSON-compatible report without modifying the run."""
    directory = Path(directory)

    report = {
        "directory": str(directory),
        "format": "incremental_csv",
        "state": None,
        "rows_read": 0,
        "errors": [],
        "warnings": [],
        "valid": False,
    }

    errors = report["errors"]
    warnings = report["warnings"]

    def finish():
        report["valid"] = not errors
        return report

    def reject_constant(value):
        raise ValueError(f"Non-finite JSON constant: {value}")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        document = json.loads(
            (directory / "metadata.json").read_text(
                encoding="utf-8"
            ),
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except (OSError, UnicodeError, ValueError) as error:
        errors.append(f"Cannot read metadata: {error}")
        return finish()

    if not isinstance(document, dict):
        errors.append("Metadata must be a JSON object.")
        return finish()

    if document.get("schema_version") != 1:
        errors.append("Unsupported metadata schema_version.")

    if document.get("export_mode") != "incremental_csv":
        errors.append(
            "This validator supports incremental_csv runs only."
        )
        return finish()

    outcome = document.get("outcome")
    metadata = document.get("metadata")

    if not isinstance(outcome, dict):
        errors.append("Metadata outcome must be an object.")
        return finish()

    if not isinstance(metadata, dict):
        errors.append("Metadata metadata field must be an object.")
        return finish()

    state = outcome.get("state")
    report["state"] = state

    if state not in ("returned", "raised", "unfinalized"):
        errors.append("Unknown or missing outcome state.")

    marker = directory / "COMPLETE"

    if state == "returned":
        if not marker.is_file():
            errors.append("Returned run is missing COMPLETE.")

        if outcome.get("shutdown_verified") is not True:
            errors.append(
                "Returned run must record verified shutdown."
            )

        if not isinstance(outcome.get("stop_reason"), str):
            errors.append("Returned run lacks a stop reason.")

    elif state in ("raised", "unfinalized"):
        if marker.exists():
            errors.append(
                "Failed/unfinalized run must not have COMPLETE."
            )

        warnings.append(
            "This is not a successfully finalized acquisition."
        )

        if state == "raised":
            if outcome.get("stop_reason") != "exception":
                errors.append(
                    "Raised run must have stop_reason=exception."
                )

            if not isinstance(outcome.get("error_type"), str):
                errors.append("Raised run lacks error_type.")

    verified = outcome.get("shutdown_verified")

    if verified is not None and type(verified) is not bool:
        errors.append("shutdown_verified must be bool or null.")

    if verified is None:
        warnings.append("Shutdown verification is unknown.")
    elif verified is False:
        warnings.append("Shutdown verification was unsuccessful.")

    shutdown = outcome.get("shutdown_report")

    if shutdown is not None:
        if not isinstance(shutdown, dict):
            errors.append("shutdown_report must be an object.")
        elif outcome.get("shutdown_report_error") is not None:
            if verified is not None:
                errors.append(
                    "Rejected shutdown report must leave "
                    "verification unknown."
                )
            warnings.append(
                "Exporter recorded an unusable shutdown report."
            )
        else:
            required = {
                "attempted",
                "verified",
                "evidence",
                "error_type",
                "error_message",
            }

            valid_shutdown = required.issubset(shutdown)

            if valid_shutdown:
                value = shutdown["verified"]

                valid_shutdown = (
                    shutdown["attempted"] is True
                    and (
                        value is None
                        or type(value) is bool
                    )
                )

                if valid_shutdown and value is True:
                    valid_shutdown = (
                        shutdown["evidence"]
                        == "smu_shutdown_returned"
                        and shutdown["error_type"] is None
                        and shutdown["error_message"] is None
                    )
                elif valid_shutdown and value is False:
                    valid_shutdown = (
                        shutdown["evidence"] == "smu_shutdown_raised"
                        and isinstance(shutdown["error_type"], str)
                        and bool(shutdown["error_type"])
                        and isinstance(
                            shutdown["error_message"], str
                        )
                    )
                elif valid_shutdown:
                    valid_shutdown = (
                        shutdown["evidence"]
                        == "shutdown_attempt_started"
                        and shutdown["error_type"] is None
                        and shutdown["error_message"] is None
                    )

            if not valid_shutdown:
                errors.append("Malformed shutdown report.")
            elif (
                shutdown["verified"] is not verified
                or shutdown["evidence"]
                != outcome.get("shutdown_evidence")
            ):
                errors.append(
                    "Shutdown report contradicts its summary."
                )

    elif outcome.get("shutdown_report_error") is not None:
        if verified is not None:
            errors.append(
                "Unusable shutdown report cannot establish "
                "verified shutdown."
            )
        warnings.append(
            "Exporter could not preserve a usable shutdown report."
        )

    expected_channel = metadata.get("channel")

    if expected_channel is not None:
        if (
            type(expected_channel) is not int
            or expected_channel not in (1, 2)
        ):
            errors.append("Metadata channel must be 1 or 2.")

    previous_time = None
    observed_channel = None
    fault_count = 0
    compliance_count = 0
    first_time = None
    last_time = None

    try:
        with (directory / "measurements.csv").open(
            encoding="utf-8",
            newline="",
        ) as handle:
            reader = csv.DictReader(handle, strict=True)

            if tuple(reader.fieldnames or ()) != FIELDS:
                errors.append(
                    "CSV header does not match the supported schema."
                )
                return finish()

            for index, row in enumerate(reader):
                report["rows_read"] += 1
                label = f"CSV record {index}"

                try:
                    if None in row or any(
                        value is None for value in row.values()
                    ):
                        raise ValueError("incorrect field count")

                    record_index = int(row["record_index"])
                    channel = int(row["channel"])
                    status = int(row["status"])

                    if record_index != index:
                        raise ValueError(
                            "record_index is not consecutive from zero"
                        )

                    if channel not in (1, 2):
                        raise ValueError("channel must be 1 or 2")

                    if status < 0:
                        raise ValueError("status must be nonnegative")

                    values = {
                        name: float(row[name])
                        for name in (
                            "instrument_timestamp_s",
                            "voltage_v",
                            "current_a",
                            "signed_power_w",
                        )
                    }

                    if not all(
                        math.isfinite(value)
                        for value in values.values()
                    ):
                        raise ValueError("non-finite measurement")

                    for name in ("in_compliance", "has_fault"):
                        if row[name] not in ("True", "False"):
                            raise ValueError(
                                f"{name} must be True or False"
                            )

                except (ValueError, TypeError) as error:
                    errors.append(f"{label}: {error}")
                    continue

                if observed_channel is None:
                    observed_channel = channel
                elif channel != observed_channel:
                    errors.append(
                        f"{label}: mixed channels are unsupported."
                    )

                if (
                    expected_channel is not None
                    and channel != expected_channel
                ):
                    errors.append(
                        f"{label}: channel disagrees with metadata."
                    )

                timestamp = values["instrument_timestamp_s"]

                if (
                    previous_time is not None
                    and timestamp <= previous_time
                ):
                    errors.append(
                        f"{label}: timestamp did not increase."
                    )

                previous_time = timestamp

                if first_time is None:
                    first_time = timestamp
                last_time = timestamp

                calculated_power = (
                    values["voltage_v"] * values["current_a"]
                )

                if not math.isclose(
                    values["signed_power_w"],
                    calculated_power,
                    rel_tol=1e-9,
                    abs_tol=1e-15,
                ):
                    errors.append(
                        f"{label}: signed_power_w differs from V*I."
                    )

                fault_count += row["has_fault"] == "True"
                compliance_count += row["in_compliance"] == "True"

    except (OSError, UnicodeError, csv.Error) as error:
        errors.append(f"Cannot read CSV: {error}")
        return finish()

    rows = report["rows_read"]

    report["recorded_span_s"] = (
        last_time - first_time
        if first_time is not None
        else None
    )
    report["fault_flagged_rows"] = fault_count
    report["compliance_flagged_rows"] = compliance_count

    if rows == 0:
        warnings.append("CSV contains no measurement records.")

    if fault_count:
        warnings.append(f"{fault_count} rows carry a fault flag.")

    if compliance_count:
        warnings.append(
            f"{compliance_count} rows carry a compliance flag."
        )

    if state == "returned":
        for location, name in (
            (document, "record_count"),
            (outcome, "retained_count"),
            (outcome, "records_flushed"),
        ):
            value = location.get(name)
            if not _count(value) or value != rows:
                errors.append(f"{name} must match CSV row count.")

        requested = outcome.get("requested_count")

        if not _count(requested) or requested == 0:
            errors.append("Invalid requested_count.")
        elif rows > requested:
            errors.append("CSV exceeds requested_count.")
        elif (
            outcome.get("stop_reason") == "completed"
            and rows != requested
        ):
            errors.append(
                "Completed acquisition does not match requested_count."
            )

        trigger_index = outcome.get("triggering_record_index")
        trigger = outcome.get("triggering_reading")

        if trigger is None:
            if trigger_index is not None:
                errors.append(
                    "Trigger index exists without a triggering reading."
                )
        elif (
            not isinstance(trigger, dict)
            or not _count(trigger_index)
            or trigger_index >= rows
        ):
            errors.append("Invalid triggering record reference.")

    elif state == "raised":
        flushed = outcome.get("records_flushed")
        retained = outcome.get("retained_in_memory_count")
        writer_failed = outcome.get("csv_writer_failed")
        close_error = outcome.get("csv_close_error")

        if not _count(flushed):
            errors.append("Invalid records_flushed.")
        if not _count(retained):
            errors.append("Invalid retained_in_memory_count.")
        if type(writer_failed) is not bool:
            errors.append("csv_writer_failed must be bool.")
        if close_error is not None and not isinstance(
            close_error, str
        ):
            errors.append("csv_close_error must be string or null.")

        if _count(flushed) and _count(retained):
            if flushed > retained:
                errors.append(
                    "Flushed count exceeds retained count."
                )

            if writer_failed is True or close_error is not None:
                warnings.append(
                    "CSV I/O failed: physical rows may exceed "
                    "the successfully flushed count."
                )
                if rows < flushed or rows > retained:
                    errors.append(
                        "CSV row count is outside flushed/retained bounds."
                    )
            elif writer_failed is False:
                if rows != flushed:
                    errors.append(
                        "CSV row count differs from flushed count."
                    )
                if retained != flushed:
                    warnings.append(
                        "Some retained readings were not exported."
                    )

    return finish()


def main():
    parser = argparse.ArgumentParser(
        description="Read-only validation of one streaming run."
    )
    parser.add_argument(
        "directory",
        help="Run directory containing metadata.json and measurements.csv",
    )
    arguments = parser.parse_args()

    report = validate_run(arguments.directory)
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())