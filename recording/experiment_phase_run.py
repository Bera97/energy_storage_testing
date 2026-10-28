"""Recording lifecycle for one experiment charge/discharge phase.

No instrument communication or shutdown operations.

Create before enabling output.
Pass writer to the phase transport.

finish_phase():
- Checks transport evidence against retained records.
- Closes CSV.
- Writes phase metadata.
- Creates COMPLETE.
- Returns a qualified PhaseResult and directory.

Inherited fail(error):
- Closes CSV and records exception metadata.
- Does not create COMPLETE.
- Uses the existing structured shutdown-report contract.

A COMPLETE marker means the recording was finalized. It is not
proof of measurement accuracy or electrical safety.
"""

from copy import deepcopy
from dataclasses import asdict
import json

from acquisition.spot_acquisition import SMUReading
from experiments.phase_outcome import (
    PhaseOutcomeError,
    RecordingFinalization,
    qualify_phase_outcome,
    validate_transport_outcome,
)
from recording.streaming_run import (
    StreamingRun,
    utc_now,
)


class ExperimentPhaseRun(StreamingRun):
    """An incremental recording with experiment-phase finalization."""

    def __init__(self, base_directory, run_id, *, metadata):
        super().__init__(
            base_directory,
            run_id,
            metadata=metadata,
        )
        self.finalization_evidence = None

    def finish_phase(self, phase, report, readings):
        """Return (PhaseResult, finalized_directory).

        The transport must already have completed verified shutdown
        and final trace retrieval.

        A failure is not automatically converted to failure metadata:
        the recording owner must call fail(error), preserving the
        original exception and partial readings.
        """
        self._require_unfinalized()

        # Snapshot inputs before beginning file finalization.
        phase_snapshot = deepcopy(phase)
        report_snapshot = json.loads(
            json.dumps(
                report,
                ensure_ascii=False,
                allow_nan=False,
            )
        )
        records = tuple(readings)

        validate_transport_outcome(
            phase_snapshot,
            report_snapshot,
        )

        if any(
            not isinstance(reading, SMUReading)
            for reading in records
        ):
            raise TypeError(
                "Every retained record must be an SMUReading."
            )

        if len(records) != report_snapshot["retained_count"]:
            raise PhaseOutcomeError(
                "Actual retained records differ from "
                "the transport report."
            )

        actual_faults = [
            index
            for index, reading in enumerate(records)
            if reading.has_fault
        ]

        if actual_faults != report_snapshot["fault_record_indices"]:
            raise PhaseOutcomeError(
                "Actual fault records differ from "
                "the transport report."
            )

        if actual_faults:
            raise PhaseOutcomeError(
                "Actual retained records include faults."
            )

        if self.writer.failed:
            raise PhaseOutcomeError(
                "Cannot finalize after a CSV writer failure."
            )

        if self.writer.records_flushed != len(records):
            raise PhaseOutcomeError(
                "CSV flushed count differs from retained records."
            )

        expected_channel = self._document["metadata"].get(
            "channel"
        )

        if (
            type(expected_channel) is not int
            or expected_channel not in (1, 2)
        ):
            raise PhaseOutcomeError(
                "Phase recording metadata must specify channel 1 or 2."
            )

        if any(
            reading.channel != expected_channel
            for reading in records
        ):
            raise PhaseOutcomeError(
                "Retained record channel differs from metadata."
            )

        trigger_index = report_snapshot[
            "triggering_record_index"
        ]
        trigger = records[trigger_index]

        # Close must succeed before finalized metadata or COMPLETE.
        self.close()

        if not self.writer.closed or self.writer.failed:
            raise PhaseOutcomeError(
                "CSV writer did not close successfully."
            )

        document = {
            **self._document,
            "finalized_at_utc": utc_now(),
            "record_count": len(records),
            "experiment_phase": phase_snapshot,
            "phase_transport_report": report_snapshot,
            "outcome": {
                "state": "returned",
                "stop_reason": report_snapshot["stop_reason"],
                "requested_count": report_snapshot[
                    "requested_count"
                ],
                "retained_count": len(records),
                "records_flushed": self.writer.records_flushed,
                "triggering_record_index": trigger_index,
                "triggering_reading": asdict(trigger),
                "shutdown_verified": True,
                "shutdown_evidence": (
                    "Phase transport returned after "
                    "verified shutdown and final trace retrieval"
                ),
                "fault_record_indices": actual_faults,
            },
        }

        self._replace_metadata(document)

        with (self.directory / "COMPLETE").open(
            "x",
            encoding="utf-8",
        ) as handle:
            handle.write("Experiment phase files finalized.\n")

        self.finalized = True
        self.finalization_evidence = RecordingFinalization(
            writer_closed=self.writer.closed,
            writer_failed=self.writer.failed,
            close_error=None,
            metadata_finalized=True,
        )

        result = qualify_phase_outcome(
            phase_snapshot,
            report_snapshot,
            recording=self.finalization_evidence,
        )

        return result, self.directory

    def fail(self, error):
        """Close CSV and preserve structured phase failure evidence.

        Never creates COMPLETE.
        Does not communicate with the instrument.
        """
        from recording.streaming_run import _shutdown_outcome

        self._require_unfinalized()

        if not isinstance(error, BaseException):
            raise TypeError("error must be an exception.")

        report_names = (
            "phase_transport_report",
            "charge_loop_report",
            "discharge_loop_report",
            "charge_preparation_report",
            "discharge_preparation_report",
            "rest_report",
            "activation_report",
            "executor_previous_shutdown_report",
        )

        reports = {}
        report_errors = {}

        for name in report_names:
            if not hasattr(error, name):
                continue

            try:
                snapshot = json.loads(
                    json.dumps(
                        getattr(error, name),
                        ensure_ascii=False,
                        allow_nan=False,
                    )
                )

                if not isinstance(snapshot, dict):
                    raise TypeError(
                        "Phase report must be a dictionary."
                    )

            except (
                TypeError,
                ValueError,
                OverflowError,
                RecursionError,
            ) as snapshot_error:
                report_errors[name] = (
                    f"{type(snapshot_error).__name__}: "
                    f"{snapshot_error}"
                )
            else:
                reports[name] = snapshot

        # Translate only explicit, consistent shutdown evidence.
        # Preserve any existing shutdown_report, even if malformed.
        if not hasattr(error, "shutdown_report"):
            candidates = []
            incomplete = bool(report_errors)

            for report in reports.values():
                if (
                    "shutdown_verified" not in report
                    or "cleanup_error" not in report
                ):
                    incomplete = True
                    continue

                verified = report["shutdown_verified"]
                cleanup = report["cleanup_error"]

                if verified is True and cleanup is None:
                    candidates.append({
                        "attempted": True,
                        "verified": True,
                        "evidence": "smu_shutdown_returned",
                        "error_type": None,
                        "error_message": None,
                    })

                elif (
                    verified is False
                    and isinstance(cleanup, dict)
                    and isinstance(cleanup.get("type"), str)
                    and bool(cleanup["type"])
                    and isinstance(cleanup.get("message"), str)
                ):
                    candidates.append({
                        "attempted": True,
                        "verified": False,
                        "evidence": "smu_shutdown_raised",
                        "error_type": cleanup["type"],
                        "error_message": cleanup["message"],
                    })

                else:
                    incomplete = True

            if (
                not incomplete
                and candidates
                and all(
                    candidate == candidates[0]
                    for candidate in candidates
                )
            ):
                error.shutdown_report = candidates[0]

        shutdown_outcome = _shutdown_outcome(error)
        close_error_text = None

        try:
            self.close()
        except BaseException as close_error:
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
                    error, "partial_readings"
                ),
                "retained_in_memory_count": len(partial),
                "records_flushed": self.writer.records_flushed,
                "csv_writer_failed": self.writer.failed,
                "csv_close_error": close_error_text,
                "phase_reports": reports,
                "phase_report_errors": report_errors,
                **shutdown_outcome,
            },
        }

        self._replace_metadata(document)
        self.finalized = True
        return self.directory

    def result_for_phase(self, phase):
        """Return a qualified result for this exact planned phase.

        Checks the recording owner's current state and stored phase
        evidence. Does not reopen or independently audit CSV contents.
        """
        if not isinstance(phase, dict):
            raise TypeError("phase must be a dictionary.")

        if not self.finalized:
            raise PhaseOutcomeError(
                "Phase recording has not been finalized."
            )

        if self.finalization_evidence is None:
            raise PhaseOutcomeError(
                "Recording was not finalized as a successful phase."
            )

        if not (self.directory / "COMPLETE").is_file():
            raise PhaseOutcomeError(
                "Successful phase recording has no COMPLETE marker."
            )

        if not self.writer.closed or self.writer.failed:
            raise PhaseOutcomeError(
                "Phase CSV writer is not successfully closed."
            )

        stored_phase = self._document.get("experiment_phase")
        report = self._document.get("phase_transport_report")

        if not isinstance(stored_phase, dict):
            raise PhaseOutcomeError(
                "Stored experiment phase is missing."
            )

        # Preparation may add acquisition budgets or monitoring
        # settings, but every planned field must still match.
        for key, value in phase.items():
            if key not in stored_phase or stored_phase[key] != value:
                raise PhaseOutcomeError(
                    "Recording does not match the requested "
                    f"phase field: {key}."
                )

        if not isinstance(report, dict):
            raise PhaseOutcomeError(
                "Stored transport report is missing."
            )

        if (
            self.writer.records_flushed
            != report.get("records_flushed")
        ):
            raise PhaseOutcomeError(
                "Current writer count differs from stored evidence."
            )

        return qualify_phase_outcome(
            stored_phase,
            report,
            recording=self.finalization_evidence,
        )


def _record_failure_preserving_original(run, original_error):
    """Attempt failure export without replacing the primary error."""
    try:
        directory = run.fail(original_error)

    except BaseException as export_error:
        original_error.add_note(
            "Experiment phase failure metadata could not "
            "be finalized: "
            f"{type(export_error).__name__}: {export_error}"
        )

        try:
            run.close()
        except BaseException as close_error:
            original_error.add_note(
                "Additional experiment CSV close also failed: "
                f"{type(close_error).__name__}: {close_error}"
            )

    else:
        original_error.run_directory = directory


def acquire_prepared_phase_and_finalize(
    prepared,
    run,
    *,
    instrument_timestamp_origin_s,
    phase_elapsed_at_origin_s,
    maximum_sample_gap_s,
    maximum_voltage_v,
    poll_interval_s=0.05,
    phase_started_s=None,
    clock=None,
    sleep=None,
    measurement_time_basis="explicit_mapping",
):
    """Return (PhaseResult, finalized_directory).

    No output enabling is performed.

    Setup, acquisition, qualification, and recording errors are
    re-raised after attempting failure recording.

    A wrong run type or an already-finalized run is rejected without
    attempting to modify that recording.

    First-sample mode requires both explicit timestamp origins to
    be None and a finite caller-supplied host phase start.
    """
    if not isinstance(run, ExperimentPhaseRun):
        raise TypeError(
            "run must be an ExperimentPhaseRun."
        )

    if run.finalized:
        raise RuntimeError(
            "Run has already been finalized."
        )

    report = None

    try:
        phase = prepared.phase
        acquisition = prepared.acquisition
        transport = prepared.transport

        if not isinstance(phase, dict):
            raise TypeError(
                "Prepared phase must be a dictionary."
            )

        if transport.writer is not run.writer:
            raise ValueError(
                "Prepared transport must use exactly run.writer."
            )

        if transport.acquisition is not acquisition:
            raise ValueError(
                "Prepared transport and acquisition do not match."
            )

        if run.writer.closed or run.writer.failed:
            raise ValueError(
                "Recording writer must be open and usable."
            )

        if run.writer.records_flushed != 0:
            raise ValueError(
                "Recording writer must be fresh."
            )

        arguments = {
            "instrument_timestamp_origin_s": (
                instrument_timestamp_origin_s
            ),
            "phase_elapsed_at_origin_s": (
                phase_elapsed_at_origin_s
            ),
            "maximum_sample_gap_s": maximum_sample_gap_s,
            "poll_interval_s": poll_interval_s,
            "phase_started_s": phase_started_s,
        }

        if clock is not None:
            arguments["clock"] = clock

        if sleep is not None:
            arguments["sleep"] = sleep

        if measurement_time_basis != "explicit_mapping":
            arguments["measurement_time_basis"] = (
                measurement_time_basis
            )

        phase_type = phase.get("type")

        if phase_type == "charge_cc_cv":
            arguments["cv_classifier"] = prepared.cv_classifier
            acquire = transport.acquire_charge

        elif phase_type == "discharge_cc":
            arguments["maximum_voltage_v"] = maximum_voltage_v
            acquire = transport.acquire_discharge

        else:
            raise ValueError(
                "Expected a prepared charge or discharge phase."
            )

        report = acquire(
            phase,
            **arguments,
        )

        return run.finish_phase(
            phase,
            report,
            acquisition.readings,
        )

    except BaseException as original_error:
        if report is not None:
            original_error.phase_transport_report = report

        acquisition = getattr(prepared, "acquisition", None)

        if not hasattr(original_error, "partial_readings"):
            original_error.partial_readings = tuple(
                getattr(acquisition, "readings", ())
            )

        _record_failure_preserving_original(
            run,
            original_error,
        )
        raise
