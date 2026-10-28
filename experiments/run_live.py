"""Supervised YAML-configured preconditioning; preview unless --execute.
Legacy execution arguments remain compatible; no confirmation prompt.
No automatic retry, resume, or external-supply control.
"""

import argparse
import json
import math
from pathlib import Path
import re
import sys

from experiments.execution_settings import resolve_single, require_ceilings

from experiments.acquisition_budget import build_acquisition_plan
from experiments.config import (
    load_config,
    require_charge_monitoring_settings,
)
from experiments.preconditioning import build_plan
from experiments.preconditioning_runner import run_preconditioning
from experiments.recorded_phase_executor import RecordedPhaseExecutor
from experiments.recorded_preconditioning_backend import (
    RecordedPreconditioningBackend,
)
from recording.preconditioning_experiment import (
    PreconditioningExperiment,
    _ProgressBackend,
)


def _positive_number(value, name):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
    ):
        raise ValueError(f"{name} must be finite and positive.")

    try:
        number = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError(
            f"{name} must be finite and positive."
        ) from error

    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be finite and positive.")

    return number


def _error_document(error):
    return {
        "type": type(error).__name__,
        "message": str(error),
    }


def _new_smu(resource, timeout_ms, visa_library):
    # Import and construction are deferred until live execution.
    from drivers.keysight_b2902b import KeysightB2902B

    return KeysightB2902B(
        resource,
        timeout_ms=timeout_ms,
        visa_library=visa_library,
    )


def execution_preview(document, *, maximum_records=100000):
    """Validate live-required settings without hardware or file writes."""
    if (
        type(maximum_records) is not int
        or not 2 <= maximum_records <= 100000
    ):
        raise ValueError(
            "--maximum-records must be between 2 and 100000."
        )

    acquisition_plan = build_acquisition_plan(
        document,
        maximum_records=maximum_records,
    )
    config = acquisition_plan["configuration"]

    require_charge_monitoring_settings(config)

    active = [
        phase
        for phase in acquisition_plan["phases"]
        if phase["type"] in ("charge_cc_cv", "discharge_cc")
    ]

    required_current = max(
        phase["current_magnitude_a"]
        for phase in active
    )

    # The existing discharge preparation sets voltage compliance
    # to the supplied DUT maximum voltage.
    required_voltage = float(
        config["dut"]["ratings"]["maximum_voltage_v"]
    )

    return {
        "state": "preview",
        "will_connect": False,
        "will_enable_output": False,
        "specimen_id": config["dut"]["specimen_id"],
        "resource": config["instrument"]["visa_address"],
        "channel": config["instrument"]["channel"],
        "required_preflight_ceiling_v": required_voltage,
        "required_preflight_ceiling_a": required_current,
        "acquisition_plan": acquisition_plan,
    }


def _fail_manifest(experiment, error, backend, report):
    error.experiment_directory = experiment.directory

    if (
        report is not None
        and not hasattr(error, "experiment_report")
    ):
        error.experiment_report = report

    try:
        experiment.fail(
            error,
            getattr(backend, "phase_outcomes", []),
        )
    except BaseException as export_error:
        error.add_note(
            "Live experiment failure manifest could not be saved: "
            f"{type(export_error).__name__}: {export_error}"
        )


def execute_live(
    document,
    *,
    experiment_id,
    confirm_specimen=None,
    expected_serial,
    maximum_voltage_v,
    maximum_current_a,
    maximum_records=100000,
    timeout_ms=5000,
    visa_library=None,
    context=None,
):
    """Return (runner_report, experiment_directory).

    Reserves the manifest before instrument construction/connection.
    Final shutdown and session close precede experiment finalization.

    This function executes the configured charge/rest/discharge/rest
    sequence. It is not a commissioning diagnostic.
    """
    preview = execution_preview(
        document,
        maximum_records=maximum_records,
    )
    config = preview["acquisition_plan"]["configuration"]

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

    if confirm_specimen is not None and confirm_specimen != config["dut"]["specimen_id"]:
        raise ValueError(
            "--confirm-specimen must exactly match "
            "dut.specimen_id."
        )

    if (
        not isinstance(expected_serial, str)
        or not expected_serial.strip()
    ):
        raise ValueError(
            "--expected-serial must be a nonempty string."
        )

    voltage_ceiling = _positive_number(
        maximum_voltage_v,
        "--max-voltage-v",
    )
    current_ceiling = _positive_number(
        maximum_current_a,
        "--max-current-a",
    )

    if voltage_ceiling < preview["required_preflight_ceiling_v"]:
        raise ValueError(
            "--max-voltage-v is below the voltage compliance "
            "required by the configured discharge preparation."
        )

    if current_ceiling < preview["required_preflight_ceiling_a"]:
        raise ValueError(
            "--max-current-a is below a configured phase current."
        )

    if type(timeout_ms) is not int or timeout_ms <= 0:
        raise ValueError("--timeout-ms must be a positive integer.")

    plan = build_plan(config)

    experiment = PreconditioningExperiment(
        config["output"]["root_directory"],
        experiment_id,
        plan=plan,
        metadata={
            "execution_mode": "live",
            "configuration": config,
            "expected_instrument_serial": expected_serial,
            "specimen_id": config["dut"]["specimen_id"],
            "specimen_confirmation_required": False,
            "operator_preflight_ceilings": {
                "maximum_voltage_v": voltage_ceiling,
                "maximum_current_a": current_ceiling,
            },
            "acquisition_planning_limit": maximum_records,
            "external_supply_control": "none",
            "sweep_context": context,
        },
    )

    smu = None
    backend = None
    report = None
    primary_error = None

    session = {
        "identity": None,
        "serial_verified": False,
        "shutdown_attempted": False,
        "shutdown_verified": None,
        "shutdown_error": None,
        "close_attempted": False,
        "close_succeeded": None,
        "close_error": None,
    }

    try:
        smu = _new_smu(
            config["instrument"]["visa_address"],
            timeout_ms,
            visa_library,
        )
        smu.connect()

        identity = smu.identity
        session["identity"] = identity

        fields = [
            field.strip()
            for field in identity.split(",")
        ]

        if (
            len(fields) != 4
            or fields[1].upper() != "B2902B"
            or fields[2] != expected_serial
        ):
            raise RuntimeError(
                "Connected instrument identity does not match "
                "the required B2902B serial number."
            )

        session["serial_verified"] = True

        executor = RecordedPhaseExecutor(
            smu,
            config,
            experiment.directory / "phases",
            "run",
            maximum_voltage_v=voltage_ceiling,
            maximum_current_a=current_ceiling,
        )

        backend = RecordedPreconditioningBackend(
            smu,
            execute_active_phase=executor,
        )

        progress_backend = _ProgressBackend(
            backend,
            experiment,
        )

        report = run_preconditioning(
            config,
            progress_backend,
        )

    except BaseException as error:
        primary_error = error

    finally:
        if smu is not None:
            if smu.is_connected:
                session["shutdown_attempted"] = True

                try:
                    smu.shutdown()
                except BaseException as shutdown_error:
                    session["shutdown_verified"] = False
                    session["shutdown_error"] = _error_document(
                        shutdown_error
                    )

                    if primary_error is None:
                        primary_error = shutdown_error
                    else:
                        primary_error.add_note(
                            "Final live-session shutdown also failed: "
                            f"{type(shutdown_error).__name__}: "
                            f"{shutdown_error}"
                        )
                else:
                    session["shutdown_verified"] = True

            session["close_attempted"] = True

            try:
                smu.close()
            except BaseException as close_error:
                session["close_succeeded"] = False
                session["close_error"] = _error_document(
                    close_error
                )

                if primary_error is None:
                    primary_error = close_error
                else:
                    primary_error.add_note(
                        "Live-session close also failed: "
                        f"{type(close_error).__name__}: {close_error}"
                    )
            else:
                session["close_succeeded"] = True

    # Persist session evidence only after the cleanup attempts.
    try:
        experiment._replace({
            **experiment._document,
            "session_outcome": session,
        })
    except BaseException as evidence_error:
        if primary_error is None:
            primary_error = evidence_error
        else:
            primary_error.add_note(
                "Session evidence could not be saved: "
                f"{type(evidence_error).__name__}: {evidence_error}"
            )

    if primary_error is not None:
        _fail_manifest(
            experiment,
            primary_error,
            backend,
            report,
        )
        raise primary_error.with_traceback(
            primary_error.__traceback__
        )

    try:
        directory = experiment.finish(
            report,
            backend.phase_outcomes,
        )
    except BaseException as finalization_error:
        finalization_error.add_note(
            "The runner and session cleanup completed, but "
            "experiment finalization failed."
        )
        _fail_manifest(
            experiment,
            finalization_error,
            backend,
            report,
        )
        raise

    return report, directory


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Preview or explicitly execute a recorded "
            "preconditioning experiment."
        ),
        epilog=(
            "Supplied DUT ratings and operator ceilings are not "
            "independent verification of the operating envelope, "
            "wiring, datasheet limits, or physical protection."
        ),
    )
    parser.add_argument("--config", required=True)

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")

    parser.add_argument("--experiment-id")
    parser.add_argument("--confirm-specimen", help=argparse.SUPPRESS)
    parser.add_argument("--expected-serial")
    parser.add_argument("--max-voltage-v", type=float)
    parser.add_argument("--max-current-a", type=float)
    parser.add_argument(
        "--maximum-records",
        type=int,
        default=100000,
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=5000,
    )
    parser.add_argument("--visa-library", default=None)

    arguments = parser.parse_args(argv)


    try:
        validated = load_config(arguments.config)
        document = validated["config"]
        settings = resolve_single(document, arguments)
        if arguments.execute:
            required = {
                "--experiment-id": arguments.experiment_id,
                "--expected-serial": arguments.expected_serial,
                "--max-voltage-v": arguments.max_voltage_v,
                "--max-current-a": arguments.max_current_a,
            }

            missing = [
                name
                for name, value in required.items()
                if value is None
            ]

            if missing:
                parser.error(
                    "--execute requires " + ", ".join(missing)
                )

        require_ceilings(settings, execution_preview(
            document, maximum_records=arguments.maximum_records
        ))

        if not arguments.execute:
            preview = execution_preview(
                document,
                maximum_records=arguments.maximum_records,
            )
            print(json.dumps(
                preview,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            ))
            return 0

        report, directory = execute_live(
            document,
            experiment_id=arguments.experiment_id,
            confirm_specimen=arguments.confirm_specimen,
            expected_serial=arguments.expected_serial,
            maximum_voltage_v=arguments.max_voltage_v,
            maximum_current_a=arguments.max_current_a,
            maximum_records=arguments.maximum_records,
            timeout_ms=arguments.timeout_ms,
            visa_library=arguments.visa_library,
        )

        print(json.dumps({
            "state": "completed",
            "experiment_directory": str(
                Path(directory).resolve()
            ),
            "completed_phase_count": (
                report["completed_phase_count"]
            ),
            "shutdown_verified": report["shutdown_verified"],
            "session_shutdown_and_close_completed": True,
        }, indent=2))

        return 0

    except BaseException as error:
        if isinstance(error, SystemExit):
            raise

        directory = getattr(
            error,
            "experiment_directory",
            None,
        )
        report = getattr(error, "experiment_report", None)

        print(json.dumps({
            "state": "raised",
            "error_type": type(error).__name__,
            "error_message": str(error),
            "error_notes": list(
                getattr(error, "__notes__", ())
            ),
            "experiment_directory": (
                str(Path(directory).resolve())
                if directory is not None
                else None
            ),
            "runner_state": (
                report.get("state")
                if isinstance(report, dict)
                else None
            ),
        }, indent=2), file=sys.stderr)

        return (
            130
            if isinstance(error, KeyboardInterrupt)
            else 1
        )


if __name__ == "__main__":
    raise SystemExit(main())