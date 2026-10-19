"""Structured evidence for shutdown attempted during error handling.

Calls the existing SMU shutdown procedure.
Does not enable outputs, close the VISA session, or control an
external supply.

The original exception is retained and is not raised by this helper.
The caller must re-raise it after cleanup.

Reports describe an individual shutdown attempt, not electrical
isolation or external-supply state.
"""


def attempt_shutdown_with_report(smu, original_error):
    """Attempt shutdown and attach its outcome to original_error.

    Returns a JSON-compatible report dictionary.

    Ordinary shutdown exceptions are recorded instead of propagated,
    preserving the original acquisition exception.

    A second KeyboardInterrupt/SystemExit during shutdown is not
    swallowed. The attached report then remains verified=None.
    """
    if not isinstance(original_error, BaseException):
        raise TypeError("original_error must be an exception.")

    report = {
        "attempted": True,
        "verified": None,
        "evidence": "shutdown_attempt_started",
        "error_type": None,
        "error_message": None,
    }

    # Attach before invoking shutdown so an interrupted cleanup
    # does not leave a misleading successful report.
    original_error.shutdown_report = report

    try:
        smu.shutdown()

    except Exception as shutdown_error:
        report.update({
            "verified": False,
            "evidence": "smu_shutdown_raised",
            "error_type": type(shutdown_error).__name__,
            "error_message": str(shutdown_error),
        })

        original_error.add_note(
            "Shutdown could not be verified: "
            f"{type(shutdown_error).__name__}: {shutdown_error}"
        )

    else:
        report.update({
            "verified": True,
            "evidence": "smu_shutdown_returned",
        })

    return dict(report)