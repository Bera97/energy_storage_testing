"""Post-run export adapters for monitored acquisition.

No instrument communication and no automatic acquisition.

A normal MonitoredAcquisition return establishes that its shutdown
completed. An exception alone does not establish shutdown success.

Export failures propagate. Callers must preserve an original
acquisition exception if exporting that exception also fails.
"""

from dataclasses import asdict

from acquisition.monitored_acquisition import MonitoredResult
from recording.run_data import save_run


def save_monitored_result(
    base_directory,
    run_id,
    result,
    *,
    metadata,
):
    """Export a returned result after acquisition/shutdown."""
    if not isinstance(result, MonitoredResult):
        raise TypeError("result must be a MonitoredResult.")

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
                "Triggering record is absent from result.readings."
            )

    outcome = {
        "acquisition_exit": "returned",
        "stop_reason": result.stop_reason,
        "requested_count": result.requested_count,
        "retained_count": len(result.readings),
        "triggering_record_index": trigger_index,
        "triggering_reading": (
            asdict(trigger) if trigger is not None else None
        ),
        "decision_elapsed_s": result.decision_elapsed_s,
        "shutdown_elapsed_s": result.shutdown_elapsed_s,
        "shutdown_verified": True,
        "shutdown_evidence": (
            "MonitoredAcquisition returned after shutdown completed"
        ),
        "fault_record_indices": [
            index
            for index, reading in enumerate(result.readings)
            if reading.has_fault
        ],
    }

    return save_run(
        base_directory,
        run_id,
        result.readings,
        metadata=metadata,
        outcome=outcome,
    )


def save_monitored_failure(
    base_directory,
    run_id,
    error,
    *,
    metadata,
    shutdown_verified=None,
):
    """Export an acquisition exception and its available partial data.

    shutdown_verified:
      None  -> unknown; default, never inferred from exception text
      True  -> caller independently verified shutdown
      False -> caller could not verify shutdown

    This function saves the exception; it does not re-raise it.
    The caller remains responsible for propagating the original error.
    """
    if not isinstance(error, BaseException):
        raise TypeError("error must be an exception.")

    if shutdown_verified is not None and type(shutdown_verified) is not bool:
        raise TypeError("shutdown_verified must be bool or None.")

    records = tuple(getattr(error, "partial_readings", ()))

    outcome = {
        "acquisition_exit": "raised",
        "stop_reason": "exception",
        "error_type": type(error).__name__,
        "error_message": str(error),
        "error_notes": list(getattr(error, "__notes__", ())),
        "partial_readings_attribute_present": hasattr(
            error, "partial_readings"
        ),
        "retained_count": len(records),
        "shutdown_verified": shutdown_verified,
        "shutdown_evidence": (
            "unknown"
            if shutdown_verified is None
            else "caller_supplied"
        ),
    }

    return save_run(
        base_directory,
        run_id,
        records,
        metadata=metadata,
        outcome=outcome,
    )