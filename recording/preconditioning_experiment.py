"""Persist the overall preconditioning experiment lifecycle.

No output enabling or instrument-driver imports.

The supplied backend may be output-capable. Calling
run_recorded_preconditioning() executes that backend.

The manifest is created before any backend execution.

Individual phase CSV recordings remain owned by the active-phase
executor. This module records experiment progress and final outcomes.

experiment.json state:
- running: no successful experiment finalization has been recorded.
- completed: runner completed, including its shutdown checks.
- raised: execution or progress recording raised an exception.

Atomic metadata replacement does not guarantee power-loss durability.
Single controller/thread only.
"""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from uuid import uuid4

from experiments.preconditioning import build_plan
from experiments.preconditioning_runner import run_preconditioning


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _snapshot(value):
    return json.loads(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
        )
    )


def _failure_snapshot(value):
    try:
        return _snapshot(value), None
    except (
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
    ) as error:
        return None, f"{type(error).__name__}: {error}"


class PreconditioningExperiment:
    """File lifecycle for one overall experiment manifest."""

    def __init__(
        self,
        base_directory,
        experiment_id,
        *,
        plan,
        metadata=None,
    ):
        if (
            not isinstance(experiment_id, str)
            or re.fullmatch(
                r"[A-Za-z0-9_-]{1,80}",
                experiment_id,
            ) is None
        ):
            raise ValueError(
                "experiment_id must contain 1-80 ASCII letters, "
                "digits, underscores, or hyphens."
            )

        if metadata is None:
            metadata = {}

        if not isinstance(metadata, dict):
            raise TypeError("metadata must be a dictionary.")

        plan_snapshot = _snapshot(plan)
        metadata_snapshot = _snapshot(metadata)

        self.directory = Path(base_directory) / experiment_id
        self.finalized = False

        self._document = {
            "experiment_manifest_schema_version": 1,
            "experiment_id": experiment_id,
            "created_at_utc": _utc_now(),
            "state": "running",
            "metadata": metadata_snapshot,
            "plan": plan_snapshot,
            "backend_returns": [],
            "backend_evidence": None,
            "experiment_report": None,
            "error": None,
        }

        initial_text = self._encode(self._document)

        self.directory.mkdir(
            parents=True,
            exist_ok=False,
        )

        with (self.directory / "experiment.json").open(
            "x",
            encoding="utf-8",
        ) as handle:
            handle.write(initial_text)

    @staticmethod
    def _encode(document):
        return json.dumps(
            document,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n"

    def _replace(self, document):
        text = self._encode(document)
        temporary = self.directory / (
            f"experiment_{uuid4().hex}.tmp"
        )

        try:
            with temporary.open(
                "x",
                encoding="utf-8",
            ) as handle:
                handle.write(text)

            temporary.replace(
                self.directory / "experiment.json"
            )

        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # An orphan temporary file must not replace the
                # primary metadata-write exception.
                pass

        self._document = document

    def _require_unfinalized(self):
        if self.finalized:
            raise RuntimeError(
                "Experiment manifest is already finalized."
            )

    def record_backend_return(
        self,
        operation,
        phase,
        backend_evidence,
    ):
        """Record backend return, not runner-confirmed completion."""
        self._require_unfinalized()

        if operation not in ("charge", "discharge", "rest"):
            raise ValueError("Unknown backend operation.")

        phase_snapshot = _snapshot(phase)

        entry = {
            "recorded_at_utc": _utc_now(),
            "operation": operation,
            "phase_index": phase_snapshot["phase_index"],
            "cycle": phase_snapshot["cycle"],
            "type": phase_snapshot["type"],
            "state": "backend_returned",
        }

        document = {
            **self._document,
            "updated_at_utc": _utc_now(),
            "backend_returns": [
                *self._document["backend_returns"],
                entry,
            ],
            "backend_evidence": _snapshot(backend_evidence),
        }

        self._replace(document)

    def finish(self, report, backend_evidence):
        """Finalize only an overall completed runner report."""
        self._require_unfinalized()
        report_snapshot = _snapshot(report)

        phase_count = len(self._document["plan"]["phases"])

        if (
            not isinstance(report_snapshot, dict)
            or report_snapshot.get("state") != "completed"
            or report_snapshot.get("shutdown_verified") is not True
            or type(
                report_snapshot.get("completed_phase_count")
            ) is not int
            or report_snapshot["completed_phase_count"] != phase_count
            or report_snapshot.get("plan") != self._document["plan"]
        ):
            raise ValueError(
                "Runner report does not establish experiment completion."
            )

        phases = report_snapshot.get("phases")

        if (
            not isinstance(phases, list)
            or len(phases) != phase_count
        ):
            raise ValueError(
                "Completed runner report has inconsistent phases."
            )

        for planned, outcome in zip(
            self._document["plan"]["phases"],
            phases,
        ):
            if (
                not isinstance(outcome, dict)
                or outcome.get("phase_index") != planned["phase_index"]
                or outcome.get("cycle") != planned["cycle"]
                or outcome.get("type") != planned["type"]
                or outcome.get("state") != "completed"
                or outcome.get("shutdown_verified") is not True
            ):
                raise ValueError(
                    "Runner phase outcome does not establish completion."
                )

        document = {
            **self._document,
            "state": "completed",
            "finalized_at_utc": _utc_now(),
            "backend_evidence": _snapshot(backend_evidence),
            "experiment_report": report_snapshot,
            "error": None,
        }

        self._replace(document)
        self.finalized = True
        return self.directory

    def fail(self, error, backend_evidence):
        """Record a failure without replacing the original exception."""
        self._require_unfinalized()

        if not isinstance(error, BaseException):
            raise TypeError("error must be an exception.")

        report, report_error = _failure_snapshot(
            getattr(error, "experiment_report", None)
        )
        evidence, evidence_error = _failure_snapshot(
            backend_evidence
        )

        document = {
            **self._document,
            "state": "raised",
            "failure_recorded_at_utc": _utc_now(),
            "backend_evidence": evidence,
            "backend_evidence_error": evidence_error,
            "experiment_report": report,
            "experiment_report_error": report_error,
            "error": {
                "type": type(error).__name__,
                "message": str(error),
                "notes": list(
                    getattr(error, "__notes__", ())
                ),
            },
        }

        self._replace(document)
        self.finalized = True
        return self.directory


class _ProgressBackend:
    """Delegate phase execution and record successful backend returns.

    Shutdown is delegated without manifest I/O so an export failure
    cannot itself masquerade as a failed shutdown operation.
    """

    def __init__(self, backend, experiment):
        self.backend = backend
        self.experiment = experiment

    def shutdown_verified(self):
        return self.backend.shutdown_verified()

    def _call(self, operation, phase):
        planned = deepcopy(phase)

        result = getattr(self.backend, operation)(
            deepcopy(planned)
        )

        self.experiment.record_backend_return(
            operation,
            planned,
            getattr(self.backend, "phase_outcomes", []),
        )

        return result

    def charge(self, phase):
        return self._call("charge", phase)

    def discharge(self, phase):
        return self._call("discharge", phase)

    def rest(self, phase):
        return self._call("rest", phase)


def run_recorded_preconditioning(
    document,
    backend,
    base_directory,
    experiment_id,
    *,
    metadata=None,
):
    """Return (runner_report, experiment_directory).

    Executes the supplied backend.

    Invalid configuration and manifest-creation failures occur
    before backend execution.

    Execution/progress errors pass through the runner's cleanup
    boundary before failure-manifest recording is attempted.

    A manifest-finalization error after runner completion receives
    experiment_report containing that completed runner report.
    """
    document_snapshot = deepcopy(document)
    plan = build_plan(document_snapshot)

    experiment = PreconditioningExperiment(
        base_directory,
        experiment_id,
        plan=plan,
        metadata=metadata,
    )

    progress_backend = _ProgressBackend(
        backend,
        experiment,
    )
    report = None

    try:
        report = run_preconditioning(
            document_snapshot,
            progress_backend,
        )

        directory = experiment.finish(
            report,
            getattr(backend, "phase_outcomes", []),
        )

        return report, directory

    except BaseException as original_error:
        original_error.experiment_directory = experiment.directory

        if (
            report is not None
            and not hasattr(original_error, "experiment_report")
        ):
            original_error.experiment_report = deepcopy(report)
            original_error.add_note(
                "The runner completed, but experiment manifest "
                "finalization failed."
            )

        try:
            experiment.fail(
                original_error,
                getattr(backend, "phase_outcomes", []),
            )

        except BaseException as export_error:
            original_error.add_note(
                "Experiment failure manifest could not be finalized: "
                f"{type(export_error).__name__}: {export_error}"
            )

        raise