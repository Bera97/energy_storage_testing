"""Orchestrate streaming acquisition and run-file finalization.

No configuration or output-enable commands.

Caller must:
- Create StreamingRun before enabling output.
- Construct StreamingMonitoredAcquisition with run.writer.
- Configure acquisition while outputs are OFF.
- Arrange cleanup for failures before calling this function.

The acquisition owns instrument shutdown.
StreamingRun owns CSV closure and metadata finalization.
"""

def _record_failure_preserving_original(run, original_error):
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

    Preconditions:
      acquisition is configured and uses exactly run.writer.
      run has not already been finalized.

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