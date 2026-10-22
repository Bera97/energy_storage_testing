"""Structural checks for persisted preconditioning runner reports.

No file operations, hardware communication, or experiment execution.
"""


def _error_object(value):
    return (
        isinstance(value, dict)
        and isinstance(value.get("type"), str)
        and bool(value["type"])
        and isinstance(value.get("message"), str)
    )


def check_runner_report(plan, report):
    """Return consistency errors for a persisted runner report.

    A completed runner report may legitimately appear inside a
    raised experiment manifest when later manifest export failed.
    This function checks the runner report independently.
    """
    errors = []

    if not isinstance(plan, dict):
        return ["Runner validation requires a plan object."]

    planned_phases = plan.get("phases")

    if not isinstance(planned_phases, list):
        return ["Runner validation requires planned phases."]

    if not isinstance(report, dict):
        return ["Runner report must be an object."]

    if (
        type(report.get("experiment_report_schema_version"))
        is not int
        or report["experiment_report_schema_version"] != 1
    ):
        errors.append("Unsupported runner report schema.")

    if report.get("plan") != plan:
        errors.append("Runner report plan differs from manifest plan.")

    state = report.get("state")

    if state not in ("completed", "raised"):
        errors.append("Unknown persisted runner report state.")

    outcomes = report.get("phases")

    if not isinstance(outcomes, list):
        errors.append("Runner phase outcomes must be a list.")
        return errors

    if len(outcomes) > len(planned_phases):
        errors.append("Runner outcomes exceed planned phases.")

    expected_reasons = {
        "charge_cc_cv": "cv_current_termination",
        "discharge_cc": "voltage_min",
        "rest": "rest_elapsed",
    }

    completed_entries = 0

    for index, outcome in enumerate(
        outcomes[:len(planned_phases)]
    ):
        planned = planned_phases[index]

        if not isinstance(planned, dict):
            errors.append(
                f"Planned phase {index} is not an object."
            )
            continue

        if not isinstance(outcome, dict):
            errors.append(
                f"Runner phase {index} is not an object."
            )
            continue

        if (
            type(outcome.get("phase_index")) is not int
            or outcome["phase_index"] != planned.get("phase_index")
            or type(outcome.get("cycle")) is not int
            or outcome["cycle"] != planned.get("cycle")
            or outcome.get("type") != planned.get("type")
        ):
            errors.append(
                f"Runner phase {index} identity differs from plan."
            )

        expected_reason = expected_reasons.get(
            planned.get("type")
        )
        phase_state = outcome.get("state")

        if phase_state == "completed":
            completed_entries += 1

            if outcome.get("stop_reason") != expected_reason:
                errors.append(
                    f"Runner phase {index} has the wrong stop reason."
                )

            if outcome.get("shutdown_verified") is not True:
                errors.append(
                    f"Runner phase {index} lacks verified shutdown."
                )

        elif phase_state == "raised":
            if state != "raised" or index != len(outcomes) - 1:
                errors.append(
                    "Only the final phase of a raised runner "
                    "report may have state raised."
                )

            if outcome.get("stop_reason") not in (
                None,
                expected_reason,
            ):
                errors.append(
                    f"Failed runner phase {index} has an "
                    "inconsistent stop reason."
                )

        else:
            errors.append(
                f"Runner phase {index} has an invalid final state."
            )

    count = report.get("completed_phase_count")

    if (
        type(count) is not int
        or count < 0
        or count != completed_entries
    ):
        errors.append(
            "Runner completed-phase count differs from its outcomes."
        )

    if type(report.get("shutdown_verified")) is not bool:
        errors.append(
            "Final runner shutdown verification must be boolean."
        )

    if state == "completed":
        if (
            len(outcomes) != len(planned_phases)
            or completed_entries != len(planned_phases)
            or report.get("active_phase_index") is not None
            or report.get("shutdown_verified") is not True
            or report.get("error") is not None
            or report.get("cleanup_error") is not None
        ):
            errors.append(
                "Completed runner report has inconsistent final evidence."
            )

    elif state == "raised":
        if not _error_object(report.get("error")):
            errors.append(
                "Raised runner report requires a structured error."
            )

        if outcomes:
            last = outcomes[-1]

            if (
                not isinstance(last, dict)
                or last.get("state") != "raised"
                or type(report.get("active_phase_index")) is not int
                or report["active_phase_index"]
                != last.get("phase_index")
            ):
                errors.append(
                    "Raised runner report has an inconsistent active phase."
                )
        elif (
            report.get("active_phase_index") is not None
            or count != 0
        ):
            errors.append(
                "Runner failure before phase entry has "
                "inconsistent phase bookkeeping."
            )

        if report.get("shutdown_verified") is True:
            if report.get("cleanup_error") is not None:
                errors.append(
                    "Verified runner cleanup contains a cleanup error."
                )

        elif report.get("shutdown_verified") is False:
            if not _error_object(report.get("cleanup_error")):
                errors.append(
                    "Failed runner cleanup requires a structured error."
                )

    return errors