"""Coordinate monitored acquisition with recorded-data export.

Two recording modes:
- acquire_and_save(): post-run export of in-memory records.
- acquire_streaming_and_finalize(): finalize an incremental recording.

No configuration, output enabling, automatic retry, or resume.

The acquisition implementation owns instrument shutdown.

Callers remain responsible for setup and cleanup around failures
that occur before acquisition begins.
"""

from recording.monitored_run_data import (
    save_monitored_failure,
    save_monitored_result,
)


def acquire_and_save(
    acquisition,
    base_directory,
    run_id,
    *,
    metadata,
    limits=None,
    timeout_s=None,
    poll_interval_s=0.05,
):
    """Return (MonitoredResult, export_directory).

    Acquisition failure:
        Attempt failure export, then re-raise the original exception.

    Export failure after a returned acquisition:
        Propagate the export error with acquisition_result attached.

    This function does not reserve or preflight the destination.
    Call prepare_run_destination() separately before hardware
    operation where appropriate. Export still enforces exclusive
    creation of its run directory.
    """
    try:
        result = acquisition.acquire(
            limits,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
        )

    except BaseException as original_error:
        try:
            directory = save_monitored_failure(
                base_directory,
                run_id,
                original_error,
                metadata=metadata,
                shutdown_verified=None,
            )

        except Exception as export_error:
            original_error.add_note(
                "Failure-data export also failed: "
                f"{type(export_error).__name__}: {export_error}"
            )

        else:
            original_error.run_directory = directory

        raise

    try:
        directory = save_monitored_result(
            base_directory,
            run_id,
            result,
            metadata=metadata,
        )

    except Exception as export_error:
        export_error.acquisition_result = result
        export_error.add_note(
            "Acquisition returned after verified SMU shutdown, "
            "but post-run export failed. The in-memory result is "
            "attached as acquisition_result."
        )
        raise

    return result, directory


def _record_failure_preserving_original(run, original_error):
    """Attempt streaming failure export without replacing the error."""
    try:
        directory = run.fail(original_error)

    except Exception as metadata_error:
        original_error.add_note(
            "Streaming failure metadata could not be finalized: "
            f"{type(metadata_error).__name__}: {metadata_error}"
        )

        # fail() may itself have failed before completing closure.
        try:
            run.close()
        except Exception as close_error:
            original_error.add_note(
                "Additional streaming CSV close also failed: "
                f"{type(close_error).__name__}: {close_error}"
            )

    else:
        original_error.run_directory = directory


def acquire_streaming_and_finalize(
    acquisition,
    run,
    *,
    limits=None,
    timeout_s=None,
    poll_interval_s=0.05,
):
    """Return (MonitoredResult, finalized_run_directory).

    Caller must:
    - Create StreamingRun before enabling output.
    - Construct StreamingMonitoredAcquisition with run.writer.
    - Configure acquisition while outputs are OFF.
    - Arrange cleanup for failures before calling this function.

    Preconditions:
        acquisition is configured and uses exactly run.writer.
        run has not already been finalized.

    Acquisition owns shutdown. StreamingRun owns CSV closure
    and metadata finalization.

    Acquisition exceptions are re-raised unchanged after attempting
    failure recording.

    Finalization errors have acquisition_result and partial_readings
    attached. At that point acquisition already returned after its
    shutdown procedure.
    """
    try:
        result = acquisition.acquire(
            limits,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
        )

    except BaseException as original_error:
        _record_failure_preserving_original(run, original_error)
        raise

    try:
        directory = run.finish(result)

    except BaseException as finalization_error:
        finalization_error.acquisition_result = result
        finalization_error.partial_readings = result.readings
        finalization_error.add_note(
            "Acquisition returned after verified SMU shutdown, "
            "but streaming-run finalization failed."
        )

        _record_failure_preserving_original(
            run,
            finalization_error,
        )
        raise

    return result, directory