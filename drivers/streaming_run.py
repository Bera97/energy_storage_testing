"""File lifecycle for one incrementally recorded acquisition.

No instrument communication or shutdown commands.

Create before enabling output.
Pass run.writer to StreamingMonitoredAcquisition.

finish(result):
    Close CSV, save returned-result metadata, create COMPLETE.

fail(error):
    Attempt CSV close and save exception metadata.
    Include structured acquisition-cleanup shutdown evidence.
    Never create COMPLETE.

close():
    Close CSV only; does not claim successful finalization.

A run directory without COMPLETE must not be treated as a
successfully finalized export.
"""

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from uuid import uuid4

from drivers.incremental_csv import IncrementalCSVWriter
from drivers.monitored_acquisition import MonitoredResult


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _shutdown_outcome(error):
    """Snapshot and validate shutdown evidence attached to an error.

    Never infer successful shutdown from exception text or notes.

    An absent or malformed report leaves verification unknown.
    Report validation problems do not prevent failure export.
    """
    outcome = {
        "shutdown_verified": None,
        "shutdown_evidence": "unknown",
        "shutdown_report": None,
        "shutdown_report_error": None,
    }

    report = getattr(error, "shutdown_report", None)

    if report is None:
        return outcome

    if not isinstance(report, dict):
        outcome["shutdown_report_error"] = (
            "shutdown_report must be a dictionary."
        )
        return outcome

    # Snapshot the complete report and check JSON compatibility.
    try:
        snapshot = json.loads(
            json.dumps(
                report,
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        outcome["shutdown_report_error"] = (
            "shutdown_report is not JSON-compatible."
        )
        return outcome

    outcome["shutdown_report"] = snapshot

    required = {
        "attempted",
        "verified",
        "evidence",
        "error_type",
        "error_message",
    }

    valid = required.issubset(snapshot)

    if valid:
        verified = snapshot["verified"]
        evidence = snapshot["evidence"]

        valid = (
            snapshot["attempted"] is True
            and (
                verified is None
                or type(verified) is bool
            )
        )

        if valid and verified is True:
            valid = (
                evidence == "smu_shutdown_returned"
                and snapshot["error_type"] is None
                and snapshot["error_message"] is None
            )

        elif valid and verified is False:
            valid = (
                evidence == "smu_shutdown_raised"
                and isinstance(snapshot["error_type"], str)
                and bool(snapshot["error_type"])
                and isinstance(snapshot["error_message"], str)
            )

        elif valid:
            valid = (
                evidence == "shutdown_attempt_started"
                and snapshot["error_type"] is None
                and snapshot["error_message"] is None
            )

    if not valid:
        outcome["shutdown_report_error"] = (
            "shutdown_report does not match the expected "
            "shutdown-reporting contract."
        )
        return outcome

    outcome["shutdown_verified"] = snapshot["verified"]
    outcome["shutdown_evidence"] = snapshot["evidence"]
    return outcome


class StreamingRun:
    def __init__(self, base_directory, run_id, *, metadata):
        if (
            not isinstance(run_id, str)
            or re.fullmatch(
                r"[A-Za-z0-9_-]{1,80}",
                run_id,
            ) is None
        ):
            raise ValueError(
                "run_id must contain 1-80 ASCII letters, digits, "
                "underscores, or hyphens."
            )

        if not isinstance(metadata, dict):
            raise TypeError("metadata must be a dictionary.")

        # Validate and snapshot metadata before creating files.
        metadata_snapshot = json.loads(
            json.dumps(
                metadata,
                ensure_ascii=False,
                allow_nan=False,
            )
        )

        self.directory = Path(base_directory) / run_id
        self.writer = None
        self.finalized = False

        self._document = {
            "schema_version": 1,
            "run_id": run_id,
            "created_at_utc": utc_now(),
            "timestamp_basis": "raw_instrument_time",
            "export_mode": "incremental_csv",
            "metadata": metadata_snapshot,
            "outcome": {
                "state": "unfinalized",
                "shutdown_verified": None,
            },
        }

        self.directory.mkdir(
            parents=True,
            exist_ok=False,
        )

        initial_text = self._encode(self._document)

        with (self.directory / "metadata.json").open(
            "x",
            encoding="utf-8",
        ) as handle:
            handle.write(initial_text)

        self.writer = IncrementalCSVWriter(
            self.directory / "measurements.csv"
        )

    @staticmethod
    def _encode(document):
        return json.dumps(
            document,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n"

    def _replace_metadata(self, document):
        text = self._encode(document)

        temporary = self.directory / (
            f"metadata_{uuid4().hex}.tmp"
        )

        with temporary.open(
            "x",
            encoding="utf-8",
        ) as handle:
            handle.write(text)

        temporary.replace(
            self.directory / "metadata.json"
        )
        self._document = document

    def _require_unfinalized(self):
        if self.finalized:
            raise RuntimeError(
                "Run has already been finalized."
            )

    def close(self):
        if self.writer is not None:
            self.writer.close()

    def finish(self, result):
        """Finalize an actual returned monitored-acquisition result.

        Assumes result came from the acquisition's normal return path.
        Does not independently verify the instrument's state.
        """
        self._require_unfinalized()

        if not isinstance(result, MonitoredResult):
            raise TypeError(
                "result must be a MonitoredResult."
            )

        if self.writer.failed:
            raise RuntimeError(
                "Cannot finalize successfully after a CSV failure."
            )

        if self.writer.records_flushed != len(result.readings):
            raise RuntimeError(
                "CSV flushed count differs from result record count."
            )

        trigger = result.triggering_reading
        trigger_index = None

        if trigger is not None:
            trigger_index = next(
                (
                    index
                    for index, reading in enumerate(result.readings)
                    if reading == trigger
                ),
                None,
            )

            if trigger_index is None:
                raise ValueError(
                    "Triggering record is absent "
                    "from result.readings."
                )

        # Close must succeed before claiming finalized export.
        self.close()

        document = {
            **self._document,
            "finalized_at_utc": utc_now(),
            "record_count": len(result.readings),
            "outcome": {
                "state": "returned",
                "stop_reason": result.stop_reason,
                "requested_count": result.requested_count,
                "retained_count": len(result.readings),
                "records_flushed": self.writer.records_flushed,
                "triggering_record_index": trigger_index,
                "triggering_reading": (
                    asdict(trigger)
                    if trigger is not None
                    else None
                ),
                "decision_elapsed_s": result.decision_elapsed_s,
                "shutdown_elapsed_s": result.shutdown_elapsed_s,
                "shutdown_verified": True,
                "shutdown_evidence": (
                    "MonitoredAcquisition returned after "
                    "shutdown completed"
                ),
                "fault_record_indices": [
                    index
                    for index, reading in enumerate(result.readings)
                    if reading.has_fault
                ],
            },
        }

        self._replace_metadata(document)

        with (self.directory / "COMPLETE").open(
            "x",
            encoding="utf-8",
        ) as handle:
            handle.write("Run files finalized.\n")

        self.finalized = True
        return self.directory

    def fail(self, error):
        """Record a failure without creating COMPLETE.

        Caller must re-raise the original acquisition error.
        This method can itself raise if metadata cannot be saved.

        Shutdown evidence is copied from error.shutdown_report.
        No instrument communication is performed here.

        records_flushed is not necessarily the CSV row count:
        a failed batch may already have written some or all rows.
        """
        self._require_unfinalized()

        if not isinstance(error, BaseException):
            raise TypeError("error must be an exception.")

        shutdown_outcome = _shutdown_outcome(error)
        close_error_text = None

        try:
            self.close()
        except Exception as close_error:
            close_error_text = (
                f"{type(close_error).__name__}: {close_error}"
            )

        partial = tuple(
            getattr(error, "partial_readings", ())
        )

        document = {
            **self._document,
            "failure_recorded_at_utc": utc_now(),
            "outcome": {
                "state": "raised",
                "stop_reason": "exception",
                "error_type": type(error).__name__,
                "error_message": str(error),
                "error_notes": list(
                    getattr(error, "__notes__", ())
                ),
                "partial_readings_attribute_present": hasattr(
                    error,
                    "partial_readings",
                ),
                "retained_in_memory_count": len(partial),
                "records_flushed": self.writer.records_flushed,
                "csv_writer_failed": self.writer.failed,
                "csv_close_error": close_error_text,
                **shutdown_outcome,
            },
        }

        self._replace_metadata(document)
        self.finalized = True
        return self.directory