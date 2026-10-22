"""Read-only validation of a preconditioning experiment manifest.

No hardware communication, file modification, or experiment execution.

Referenced phase directories must:
- Be absolute paths.
- Resolve within the allowed phase root.
- Not duplicate another active-phase reference.

By default, the allowed root is the experiment directory's parent.

JSON input rejects duplicate keys and non-finite numeric constants.

Validation checks persisted evidence and consistency. It does not
independently verify physical shutdown, measurement accuracy,
or file authenticity.
"""

import json
from pathlib import Path

from analysis.preconditioning_report_checks import (
    check_runner_report,
)
from analysis.validate_recorded_run import validate_run
from experiments.phase_outcome import validate_transport_outcome


def _reject_constant(value):
    raise ValueError(
        f"Non-finite JSON constant is not permitted: {value}"
    )


def _unique_object(pairs):
    result = {}

    for key, value in pairs:
        if key in result:
            raise ValueError(
                f"Duplicate JSON object key: {key}"
            )

        result[key] = value

    return result


def _read_json(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(
            handle,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )


def _identity_matches(entry, planned):
    return (
        isinstance(entry, dict)
        and type(entry.get("phase_index")) is int
        and entry["phase_index"] == planned["phase_index"]
        and type(entry.get("cycle")) is int
        and entry["cycle"] == planned["cycle"]
        and entry.get("type") == planned["type"]
    )


def _error_object(value):
    return (
        isinstance(value, dict)
        and isinstance(value.get("type"), str)
        and bool(value["type"])
        and isinstance(value.get("message"), str)
    )


def validate_experiment(directory, *, phase_root=None):
    """Return a JSON-compatible validation report.

    Valid raised/running evidence is not a completed experiment.
    Inspect both valid and state.

    Files are read sequentially, not as a filesystem snapshot.
    Do not modify recordings concurrently with validation.
    """
    directory = Path(directory)

    result = {
        "valid": False,
        "state": None,
        "phase_recordings_checked": 0,
        "errors": [],
        "warnings": [],
    }
    errors = result["errors"]
    warnings = result["warnings"]

    try:
        document = _read_json(
            directory / "experiment.json"
        )
    except (
        OSError,
        ValueError,
        UnicodeError,
        RecursionError,
    ) as error:
        errors.append(
            "Cannot read experiment manifest: "
            f"{type(error).__name__}: {error}"
        )
        return result

    if not isinstance(document, dict):
        errors.append("Manifest must be a JSON object.")
        return result

    if (
        type(document.get("experiment_manifest_schema_version"))
        is not int
        or document["experiment_manifest_schema_version"] != 1
    ):
        errors.append(
            "Unsupported experiment manifest schema."
        )

    state = document.get("state")
    result["state"] = state

    if state not in ("running", "completed", "raised"):
        errors.append("Unknown experiment manifest state.")

    plan = document.get("plan")
    phases = (
        plan.get("phases")
        if isinstance(plan, dict)
        else None
    )

    if not isinstance(phases, list) or not phases:
        errors.append(
            "Manifest plan must contain phases."
        )
        return result

    expected_types = (
        "charge_cc_cv",
        "rest",
        "discharge_cc",
        "rest",
    )

    plan_errors = []

    if len(phases) % 4 != 0:
        plan_errors.append(
            "Plan does not contain complete four-phase cycles."
        )

    for index, phase in enumerate(phases):
        if not isinstance(phase, dict):
            plan_errors.append(
                f"Plan phase {index} is not an object."
            )
            continue

        if (
            type(phase.get("phase_index")) is not int
            or phase["phase_index"] != index
            or type(phase.get("cycle")) is not int
            or phase["cycle"] != index // 4 + 1
            or phase.get("type") != expected_types[index % 4]
        ):
            plan_errors.append(
                f"Plan phase {index} has inconsistent order or identity."
            )

    if plan_errors:
        errors.extend(plan_errors)
        return result

    returns = document.get("backend_returns")

    if not isinstance(returns, list):
        errors.append(
            "backend_returns must be a list."
        )
        returns = []

    if len(returns) > len(phases):
        errors.append(
            "Backend returns exceed planned phases."
        )

    operations = {
        "charge_cc_cv": "charge",
        "discharge_cc": "discharge",
        "rest": "rest",
    }

    for index, entry in enumerate(returns[:len(phases)]):
        planned = phases[index]

        if (
            not _identity_matches(entry, planned)
            or entry.get("operation") != operations[planned["type"]]
            or entry.get("state") != "backend_returned"
        ):
            errors.append(
                f"Backend return {index} differs from the plan."
            )

    report = document.get("experiment_report")

    if report is not None:
        if not isinstance(report, dict):
            errors.append(
                "experiment_report must be an object or null."
            )
        else:
            errors.extend(
                check_runner_report(plan, report)
            )

    for field in (
        "experiment_report_error",
        "backend_evidence_error",
    ):
        if document.get(field) is not None:
            errors.append(
                f"Manifest reports lost or invalid evidence: {field}."
            )

    if state == "completed":
        if not isinstance(report, dict):
            errors.append(
                "Completed manifest requires a runner report."
            )
        elif report.get("state") != "completed":
            errors.append(
                "Completed manifest requires a completed runner report."
            )

        if len(returns) != len(phases):
            errors.append(
                "Completed manifest has incomplete backend returns."
            )

        if document.get("error") is not None:
            errors.append(
                "Completed manifest contains an experiment error."
            )

    elif state == "raised":
        if not _error_object(document.get("error")):
            errors.append(
                "Raised manifest requires a structured error."
            )

        if (
            isinstance(report, dict)
            and report.get("state") == "completed"
        ):
            warnings.append(
                "Runner completed, but experiment finalization raised."
            )

        if report is None:
            warnings.append(
                "Raised manifest has no persisted runner report."
            )

    elif state == "running":
        warnings.append(
            "Experiment has no successfully persisted final outcome."
        )

    evidence = document.get("backend_evidence")

    if (
        evidence is None
        and state == "running"
        and not returns
    ):
        evidence = []

    if not isinstance(evidence, list):
        errors.append(
            "backend_evidence must be a list."
        )
        evidence = []

    if state == "completed" and len(evidence) != len(phases):
        errors.append(
            "Completed manifest has incomplete backend evidence."
        )

    if len(evidence) > len(phases):
        errors.append(
            "Backend evidence exceeds planned phases."
        )

    if len(evidence) < len(returns):
        errors.append(
            "Backend evidence is missing for recorded backend returns."
        )

    try:
        allowed_root = Path(
            phase_root
            if phase_root is not None
            else directory.parent
        ).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        errors.append(
            f"Cannot resolve allowed phase root: {error}"
        )
        return result

    if not allowed_root.is_dir():
        errors.append(
            "Allowed phase root must be a directory."
        )
        return result

    seen_directories = set()

    for index, entry in enumerate(evidence[:len(phases)]):
        planned = phases[index]

        if not _identity_matches(entry, planned):
            errors.append(
                f"Backend evidence {index} differs from the plan."
            )
            continue

        if planned["type"] == "rest":
            rest_report = entry.get("rest_report")

            if (
                entry.get("state") != "rest_completed"
                or entry.get("stop_reason") != "rest_elapsed"
                or not isinstance(rest_report, dict)
                or rest_report.get("state") != "returned"
                or rest_report.get("shutdown_verified") is not True
                or rest_report.get(
                    "both_outputs_reported_off"
                ) is not True
                or rest_report.get("requested_duration_s")
                != planned.get("duration_s")
            ):
                errors.append(
                    f"Rest evidence {index} is inconsistent."
                )

            continue

        expected_reason = (
            "cv_current_termination"
            if planned["type"] == "charge_cc_cv"
            else "voltage_min"
        )

        if (
            entry.get("state") != "recording_finalized"
            or entry.get("stop_reason") != expected_reason
        ):
            errors.append(
                f"Active-phase evidence {index} is inconsistent."
            )

        reference = entry.get("run_directory")

        if not isinstance(reference, str) or not reference:
            errors.append(
                f"Phase {index} has no recording directory."
            )
            continue

        path = Path(reference)

        if not path.is_absolute():
            errors.append(
                f"Phase {index} recording reference must be absolute."
            )
            continue

        try:
            path = path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            errors.append(
                f"Cannot resolve phase {index} recording: {error}"
            )
            continue

        if not path.is_relative_to(allowed_root):
            errors.append(
                f"Phase {index} recording is outside the allowed phase root."
            )
            continue

        if path in seen_directories:
            errors.append(
                f"Phase {index} duplicates another recording reference."
            )
            continue

        seen_directories.add(path)

        try:
            # Parse strictly before handing the recording to the
            # existing CSV/metadata validator.
            phase_document = _read_json(
                path / "metadata.json"
            )
            validation = validate_run(path)

        except (
            OSError,
            ValueError,
            TypeError,
            UnicodeError,
            RecursionError,
        ) as error:
            errors.append(
                f"Cannot validate phase {index}: "
                f"{type(error).__name__}: {error}"
            )
            continue

        result["phase_recordings_checked"] += 1

        if not validation["valid"]:
            errors.extend(
                f"Phase {index}: {message}"
                for message in validation["errors"]
            )

        warnings.extend(
            f"Phase {index}: {message}"
            for message in validation.get("warnings", [])
        )

        if validation.get("state") != "returned":
            errors.append(
                f"Phase {index} reference is not a returned recording."
            )

        if not isinstance(phase_document, dict):
            errors.append(
                f"Phase {index} metadata is not an object."
            )
            continue

        stored_phase = phase_document.get(
            "experiment_phase"
        )
        transport_report = phase_document.get(
            "phase_transport_report"
        )
        outcome = phase_document.get("outcome")

        if not isinstance(stored_phase, dict):
            errors.append(
                f"Phase {index} has no stored experiment phase."
            )
            continue

        for key, value in planned.items():
            if (
                key not in stored_phase
                or stored_phase[key] != value
            ):
                errors.append(
                    f"Phase {index} stored field differs from plan: {key}."
                )

        try:
            validate_transport_outcome(
                stored_phase,
                transport_report,
            )
        except (RuntimeError, ValueError, TypeError) as error:
            errors.append(
                f"Phase {index} transport evidence: {error}"
            )

        if not isinstance(outcome, dict):
            errors.append(
                f"Phase {index} has no recording outcome."
            )
            continue

        if (
            outcome.get("state") != "returned"
            or outcome.get("stop_reason") != expected_reason
            or outcome.get("shutdown_verified") is not True
            or outcome.get("fault_record_indices") != []
        ):
            errors.append(
                f"Phase {index} recording outcome is inconsistent."
            )

        if isinstance(transport_report, dict):
            rows = validation.get("rows_read")

            if (
                transport_report.get("records_flushed") != rows
                or outcome.get("records_flushed") != rows
                or outcome.get("retained_count") != rows
                or transport_report.get("requested_count")
                != outcome.get("requested_count")
                or transport_report.get("triggering_record_index")
                != outcome.get("triggering_record_index")
            ):
                errors.append(
                    f"Phase {index} recording and transport counts "
                    "or trigger evidence differ."
                )

    result["valid"] = not errors
    return result