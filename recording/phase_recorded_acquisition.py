"""Coordinate a prepared experiment phase and its recording.

No source configuration or output enabling.

Caller responsibilities:
- Create the recording before any output-enabling attempt.
- Prepare the phase while both outputs are OFF.
- Select an explicit measurement-time policy.
- Establish the host phase start before output enabling.
- Retain an outer shutdown boundary.

The transport owns acquisition-time shutdown.
The recording owns CSV closure and metadata finalization.
"""

from recording.experiment_phase_run import ExperimentPhaseRun


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