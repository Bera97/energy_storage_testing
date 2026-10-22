"""Hardware-independent preconditioning sequence orchestration.

No driver imports, hardware communication, or file operations.

The backend is responsible for:
- Safe configuration and hardware capability checks.
- Recording measurements and partial outcomes.
- CC-CV regulation and charge-termination hold logic.
- Enforcing charge, CV, and discharge timeouts.
- Implementing rest timing without enabling output.
- Attempting shutdown and verifying reported output-off state.

This module coordinates phase order. It is not itself a hardware
monitor, timer, measurement recorder, or independent protection.
"""

from copy import deepcopy
from dataclasses import dataclass
from typing import Protocol

from experiments.preconditioning import build_plan


@dataclass(frozen=True)
class PhaseResult:
    """Minimal successful-return information from an active phase."""

    stop_reason: str


class PreconditioningBackend(Protocol):
    """Contract for a future hardware adapter or an offline fake."""

    def shutdown_verified(self) -> bool:
        """Attempt shutdown; return exactly True only after verification."""
        ...

    def charge(self, phase: dict) -> PhaseResult:
        """Run charge; enforce its limits and record its outcome.

        Successful termination reason: cv_current_termination.
        Raise on timeout, faults, cancellation, or recording failure.
        """
        ...

    def discharge(self, phase: dict) -> PhaseResult:
        """Run discharge; enforce its limits and record its outcome.

        Successful termination reason: voltage_min.
        Raise on timeout, faults, cancellation, or recording failure.
        """
        ...

    def rest(self, phase: dict) -> None:
        """Wait for duration_s without enabling output.

        Raise if rest cannot be completed.
        """
        ...


class PhaseTerminationError(RuntimeError):
    """An active phase returned without its required termination."""


class OutputOffVerificationError(RuntimeError):
    """The backend did not explicitly verify reported output OFF."""


def _require_output_off(backend):
    if backend.shutdown_verified() is not True:
        raise OutputOffVerificationError(
            "Backend did not verify output OFF."
        )


def _run_phase(backend, phase):
    phase_type = phase["type"]

    if phase_type == "rest":
        backend.rest(deepcopy(phase))
        return "rest_elapsed"

    if phase_type == "charge_cc_cv":
        result = backend.charge(deepcopy(phase))
        expected = "cv_current_termination"
    elif phase_type == "discharge_cc":
        result = backend.discharge(deepcopy(phase))
        expected = "voltage_min"
    else:
        raise ValueError(f"Unknown phase type: {phase_type}")

    if not isinstance(result, PhaseResult):
        raise TypeError(
            "Active phase backend must return PhaseResult."
        )

    if result.stop_reason != expected:
        raise PhaseTerminationError(
            f"{phase_type} returned stop_reason="
            f"{result.stop_reason!r}; expected {expected!r}."
        )

    return result.stop_reason


def run_preconditioning(document, backend: PreconditioningBackend):
    """Run a validated plan using the supplied backend.

    No real hardware backend is supplied by this module.

    Returns:
        A JSON-compatible experiment report after every phase and
        its following output-off verification completes.

    Raises:
        The original phase/shutdown exception. After execution has
        entered the backend, exceptions receive experiment_report.

    Invalid configuration is rejected before any backend call.
    """
    plan = build_plan(document)

    report = {
        "experiment_report_schema_version": 1,
        "state": "running",
        "plan": plan,
        "completed_phase_count": 0,
        "active_phase_index": None,
        "phases": [],
        "shutdown_verified": None,
        "error": None,
        "cleanup_error": None,
    }

    entry = None

    try:
        _require_output_off(backend)
        report["shutdown_verified"] = True

        for phase in plan["phases"]:
            report["active_phase_index"] = phase["phase_index"]

            entry = {
                "phase_index": phase["phase_index"],
                "cycle": phase["cycle"],
                "type": phase["type"],
                "state": "running",
                "stop_reason": None,
                "shutdown_verified": None,
            }
            report["phases"].append(entry)

            # Active phases may energize output. A rest backend is
            # required not to energize it. Do not assume either state.
            report["shutdown_verified"] = None

            entry["stop_reason"] = _run_phase(backend, phase)

            _require_output_off(backend)

            entry["shutdown_verified"] = True
            entry["state"] = "completed"
            report["shutdown_verified"] = True
            report["completed_phase_count"] += 1

        report["active_phase_index"] = None
        report["state"] = "completed"
        return report

    except BaseException as original_error:
        report["state"] = "raised"
        report["error"] = {
            "type": type(original_error).__name__,
            "message": str(original_error),
        }

        if entry is not None:
            entry["state"] = "raised"

        # Do not let a second cleanup failure replace the original
        # exception, including an original KeyboardInterrupt.
        try:
            _require_output_off(backend)
        except BaseException as cleanup_error:
            report["shutdown_verified"] = False
            report["cleanup_error"] = {
                "type": type(cleanup_error).__name__,
                "message": str(cleanup_error),
            }
            original_error.add_note(
                "Experiment cleanup could not verify output OFF: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
        else:
            report["shutdown_verified"] = True

        original_error.experiment_report = deepcopy(report)
        raise