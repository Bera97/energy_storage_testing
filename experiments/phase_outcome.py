"""Qualify acquisition and recording outcomes before advancing phases.

No hardware communication or file operations.

These are consistency checks on supplied evidence, not proof of
electrical safety, measurement accuracy, or scientific completeness.
"""

from dataclasses import dataclass

from experiments.preconditioning_runner import PhaseResult


class PhaseOutcomeError(RuntimeError):
    """Acquisition or recording evidence does not qualify as success."""


@dataclass(frozen=True)
class RecordingFinalization:
    """Explicit evidence supplied by the recording owner."""

    writer_closed: bool
    writer_failed: bool
    close_error: str | None
    metadata_finalized: bool


def _count(value):
    return type(value) is int and value >= 0


def validate_transport_outcome(phase, report):
    """Validate transport evidence without claiming recording success.

    Returns None on acceptance. Does not modify inputs.
    """
    if not isinstance(phase, dict):
        raise PhaseOutcomeError("phase must be a dictionary.")

    if not isinstance(report, dict):
        raise PhaseOutcomeError("report must be a dictionary.")

    expected = {
        "charge_cc_cv": (
            "charge",
            "cv_current_termination",
        ),
        "discharge_cc": (
            "discharge",
            "voltage_min",
        ),
    }

    phase_type = phase.get("type")

    if (
        not isinstance(phase_type, str)
        or phase_type not in expected
    ):
        raise PhaseOutcomeError(
            "Expected an active charge or discharge phase."
        )

    expected_type, expected_reason = expected[phase_type]

    if report.get("state") != "returned":
        raise PhaseOutcomeError(
            "Transport did not return a successful acquisition."
        )

    if report.get("phase_type") != expected_type:
        raise PhaseOutcomeError(
            "Transport phase type differs from the planned phase."
        )

    if (
        report.get("expected_stop_reason") != expected_reason
        or report.get("stop_reason") != expected_reason
    ):
        raise PhaseOutcomeError(
            "Transport did not reach the required phase endpoint."
        )

    if report.get("shutdown_verified") is not True:
        raise PhaseOutcomeError(
            "Transport shutdown was not verified."
        )

    if (
        report.get("finalization_attempted") is not True
        or report.get("finalized") is not True
    ):
        raise PhaseOutcomeError(
            "Trace finalization did not complete."
        )

    for field in (
        "error",
        "cleanup_error",
        "finalization_error",
    ):
        if field not in report or report[field] is not None:
            raise PhaseOutcomeError(
                "Transport contains missing or unsuccessful "
                f"{field} evidence."
            )

    requested = report.get("requested_count")
    retained = report.get("retained_count")
    flushed = report.get("records_flushed")
    before_finalization = report.get(
        "records_saved_before_finalization"
    )

    for name, value in (
        ("requested_count", requested),
        ("retained_count", retained),
        ("records_flushed", flushed),
        (
            "records_saved_before_finalization",
            before_finalization,
        ),
    ):
        if not _count(value):
            raise PhaseOutcomeError(
                f"{name} must be a nonnegative integer."
            )

    if not (
        0 < before_finalization <= retained <= requested
        and flushed == retained
    ):
        raise PhaseOutcomeError(
            "Requested, retained, and flushed counts "
            "do not qualify as a complete phase recording."
        )

    trigger = report.get("triggering_record_index")

    if not _count(trigger) or trigger >= before_finalization:
        raise PhaseOutcomeError(
            "Triggering record index is outside the "
            "records evaluated before finalization."
        )

    trailing = report.get("trailing_record_count")

    if (
        not _count(trailing)
        or trailing != retained - before_finalization
    ):
        raise PhaseOutcomeError(
            "Trailing-record count is missing or inconsistent."
        )

    fault_indices = report.get("fault_record_indices")

    if not isinstance(fault_indices, (list, tuple)):
        raise PhaseOutcomeError(
            "Explicit fault-record evidence is required."
        )

    if fault_indices:
        raise PhaseOutcomeError(
            "Retained records include measurement faults."
        )

    if "acquisition_budget" in phase:
        budget = phase["acquisition_budget"]

        if (
            not isinstance(budget, dict)
            or not _count(budget.get("record_count"))
            or budget["record_count"] != requested
        ):
            raise PhaseOutcomeError(
                "Transport count differs from the planned "
                "acquisition budget."
            )


def qualify_phase_outcome(
    phase,
    report,
    *,
    recording,
):
    """Return PhaseResult after transport and recording qualify."""
    validate_transport_outcome(phase, report)

    if not isinstance(recording, RecordingFinalization):
        raise PhaseOutcomeError(
            "RecordingFinalization evidence is required."
        )

    if recording.writer_closed is not True:
        raise PhaseOutcomeError(
            "Measurement writer closure is not verified."
        )

    if recording.writer_failed is not False:
        raise PhaseOutcomeError(
            "Measurement writer failed or its state is unknown."
        )

    if recording.close_error is not None:
        raise PhaseOutcomeError(
            "Measurement writer reported a close error."
        )

    if recording.metadata_finalized is not True:
        raise PhaseOutcomeError(
            "Phase metadata finalization is not verified."
        )

    return PhaseResult(
        stop_reason=report["stop_reason"]
    )