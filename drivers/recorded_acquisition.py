"""Run monitored acquisition, then export its result or failure.

Post-run export only, not streaming logging.

Caller configures acquisition and explicitly enables output.
The acquisition implementation owns shutdown.

No automatic retry, resume, or output enabling.
"""

from drivers.monitored_run_data import (
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
      Propagate the export error with acquisition_result attached,
      so the completed in-memory result remains accessible.

    This function does not reserve the destination before acquisition.
    Use a fresh run_id. Destination preflight will be added before
    hardware integration.
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