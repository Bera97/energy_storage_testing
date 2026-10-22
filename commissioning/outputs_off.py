"""Explicit outputs-OFF commissioning.

Default:
    Dry run. No instrument construction or connection.

With --connect:
    Connect, attempt shutdown, read configuration, attempt final
    shutdown, and close the session.

Never enables output or initiates acquisition.

This is not electrically non-invasive: shutdown changes instrument
state. Use on an isolated commissioning setup, not an active experiment.

Reports describe driver calls and instrument readbacks. They do not
establish physical isolation or suitability for a particular DUT.
"""

import argparse
from datetime import datetime, timezone
import json

from drivers.shutdown_reporting import attempt_shutdown_with_report


def _error_document(error):
    return {
        "type": type(error).__name__,
        "message": str(error),
    }


def _cleanup_preserving_original(smu, original_error):
    try:
        attempt_shutdown_with_report(smu, original_error)
    except BaseException as cleanup_error:
        original_error.add_note(
            "Commissioning cleanup was interrupted: "
            f"{type(cleanup_error).__name__}: {cleanup_error}"
        )


def commission_outputs_off(smu):
    """Inspect an already-connected instrument with outputs OFF.

    Does not connect or close the session.

    On runtime failure:
    - Attempt shutdown.
    - Preserve the original exception.
    - Attach commissioning_report and shutdown_report.
    """
    report = {
        "commissioning_schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "state": "running",
        "identity": None,
        "initial_shutdown_verified": None,
        "final_shutdown_verified": None,
        "channels": [],
        "output_enable_attempted": False,
        "acquisition_initiation_attempted": False,
        "error": None,
    }

    def checked(operation):
        value = operation()
        smu.check_errors()
        return value

    try:
        smu.shutdown()
        report["initial_shutdown_verified"] = True
        report["identity"] = smu.identity

        for number, channel in (
            (1, smu.chan1),
            (2, smu.chan2),
        ):
            output = checked(channel.get_output_state)
            automatic = checked(channel.get_auto_output_on)

            if output is not False:
                raise RuntimeError(
                    f"Channel {number} did not explicitly report OFF."
                )

            if automatic is not False:
                raise RuntimeError(
                    f"Channel {number} automatic output-on "
                    "did not explicitly report OFF."
                )

            mode = checked(channel.get_source_mode)

            if mode == "VOLT":
                setpoint = checked(channel.get_voltage_setpoint)
                compliance = checked(channel.get_current_limit)
                setpoint_unit = "V"
                compliance_unit = "A"

            elif mode == "CURR":
                setpoint = checked(channel.get_current_setpoint)
                compliance = checked(channel.get_voltage_limit)
                setpoint_unit = "A"
                compliance_unit = "V"

            else:
                raise RuntimeError(
                    f"Channel {number} has an unsupported "
                    f"source mode: {mode!r}."
                )

            report["channels"].append({
                "channel": number,
                "output_reported_on": output,
                "automatic_output_on": automatic,
                "output_off_mode": checked(
                    channel.get_output_off_mode
                ),
                "source_mode": mode,
                "source_setpoint": setpoint,
                "source_setpoint_unit": setpoint_unit,
                "active_compliance": compliance,
                "active_compliance_unit": compliance_unit,
            })

        # Reject non-JSON or non-finite readbacks before success.
        json.dumps(report, allow_nan=False)

        smu.shutdown()
        report["final_shutdown_verified"] = True
        report["state"] = "outputs_off_verified"

        return report

    except BaseException as original_error:
        report["state"] = "raised"
        report["error"] = _error_document(original_error)

        if report["initial_shutdown_verified"] is None:
            report["initial_shutdown_verified"] = False

        _cleanup_preserving_original(smu, original_error)

        report["cleanup_shutdown_report"] = getattr(
            original_error,
            "shutdown_report",
            None,
        )
        report["error_notes"] = list(
            getattr(original_error, "__notes__", ())
        )

        original_error.commissioning_report = report
        raise


def _positive_integer(value):
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "Value must be a positive integer."
        ) from error

    if number <= 0:
        raise argparse.ArgumentTypeError(
            "Value must be a positive integer."
        )

    return number


def _new_smu(resource, timeout_ms, visa_library):
    # Deliberately deferred until explicit --connect.
    from drivers.keysight_b2902b import KeysightB2902B

    return KeysightB2902B(
        resource,
        timeout_ms=timeout_ms,
        visa_library=visa_library,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Outputs-OFF commissioning. Defaults to dry run. "
            "Never enables output or initiates acquisition."
        ),
        epilog=(
            "--connect changes instrument state by attempting "
            "shutdown. Do not use on an active experiment."
        ),
    )
    parser.add_argument(
        "--resource",
        required=True,
        help="Explicit VISA resource address.",
    )
    parser.add_argument(
        "--connect",
        action="store_true",
        help="Actually connect and perform outputs-OFF checks.",
    )
    parser.add_argument(
        "--timeout-ms",
        type=_positive_integer,
        default=5000,
        help="VISA operation timeout in milliseconds.",
    )
    parser.add_argument(
        "--visa-library",
        default=None,
        help="Optional PyVISA backend/library selection.",
    )

    arguments = parser.parse_args(argv)

    if not arguments.resource.strip():
        parser.error("--resource must not be empty.")

    if not arguments.connect:
        print(json.dumps({
            "state": "dry_run",
            "resource": arguments.resource,
            "will_connect": False,
            "will_enable_output": False,
            "will_initiate_acquisition": False,
            "explicit_connect_required": True,
        }, indent=2))
        return 0

    smu = None
    exit_code = 0

    try:
        smu = _new_smu(
            arguments.resource,
            arguments.timeout_ms,
            arguments.visa_library,
        )
        smu.connect()
        report = commission_outputs_off(smu)

    except BaseException as original_error:
        exit_code = (
            130 if isinstance(original_error, KeyboardInterrupt)
            else 1
        )

        report = getattr(
            original_error,
            "commissioning_report",
            {
                "state": "raised",
                "error": _error_document(original_error),
                "shutdown_verification": "not_established",
            },
        )

    finally:
        if smu is not None:
            try:
                smu.close()
            except BaseException as close_error:
                exit_code = exit_code or 1
                report = {
                    **report,
                    "state": "raised",
                    "session_close_error": _error_document(
                        close_error
                    ),
                }

    # A malformed readback must not make failure reporting itself
    # emit invalid JSON.
    try:
        text = json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError):
        exit_code = exit_code or 1
        text = json.dumps({
            "state": "raised",
            "error": {
                "type": "ReportSerializationError",
                "message": (
                    "Commissioning report contains invalid "
                    "JSON values."
                ),
            },
        }, indent=2)

    print(text)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())