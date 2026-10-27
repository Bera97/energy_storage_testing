"""Concrete recorded charge/discharge phase execution.

OUTPUT-CAPABLE when called with real instrument dependencies.

Not connected to the CLI by this module.

Recording is created before preparation and output activation.
The host phase deadline starts before output activation.

Measurement time is explicitly first-recorded-sample-relative.
It is not a reconstruction of the physical output-enable instant.

The caller retains the outer experiment shutdown boundary.
Only one controller/thread may use the instrument.
"""

from copy import deepcopy
from dataclasses import asdict
import math
import re
import time

from acquisition.buffered_acquisition import BufferedAcquisition
from acquisition.charge_trace_transport import ChargeTraceTransport
from acquisition.discharge_trace_transport import (
    DischargeTraceTransport,
)
from drivers.shutdown_reporting import attempt_shutdown_with_report
from experiments.acquisition_budget import build_acquisition_plan
from experiments.charge_trace_monitor import ChargeTraceMonitor
from experiments.config import require_charge_monitoring_settings
from experiments.discharge_trace_monitor import DischargeTraceMonitor
from experiments.phase_activation import activate_phase_output
from experiments.preconditioning import build_plan
from experiments.prepare_charge import prepare_charge
from experiments.prepare_discharge import prepare_discharge
from recording.experiment_phase_run import ExperimentPhaseRun
from recording.experiment_phase_run import (
    _record_failure_preserving_original,
)


def _finite_number(value, name, *, positive=False):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
    ):
        raise ValueError(f"{name} must be a finite number.")

    try:
        result = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError(
            f"{name} must be a finite number."
        ) from error

    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number.")

    if positive and result <= 0:
        raise ValueError(f"{name} must be positive.")

    return result


class RecordedPhaseExecutor:
    """Callable returning one successfully finalized ExperimentPhaseRun.

    maximum_voltage_v and maximum_current_a are explicit caller
    ceilings for the existing DC output-enable preflight.

    They do not independently validate the complete operating
    envelope and are not continuously enforced hardware limits.
    """

    def __init__(
        self,
        smu,
        document,
        base_directory,
        run_prefix,
        *,
        maximum_voltage_v,
        maximum_current_a,
        poll_interval_s=0.05,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        if (
            not isinstance(run_prefix, str)
            or re.fullmatch(
                r"[A-Za-z0-9_-]{1,40}",
                run_prefix,
            ) is None
        ):
            raise ValueError(
                "run_prefix must contain 1-40 ASCII letters, "
                "digits, underscores, or hyphens."
            )

        if not callable(clock) or not callable(sleep):
            raise ValueError(
                "clock and sleep must be callable."
            )

        self.maximum_voltage_v = _finite_number(
            maximum_voltage_v,
            "maximum_voltage_v",
            positive=True,
        )
        self.maximum_current_a = _finite_number(
            maximum_current_a,
            "maximum_current_a",
            positive=True,
        )
        self.poll_interval_s = _finite_number(
            poll_interval_s,
            "poll_interval_s",
            positive=True,
        )

        document_snapshot = deepcopy(document)

        acquisition_plan = build_acquisition_plan(
            document_snapshot,
            maximum_records=BufferedAcquisition.MAX_RECORDS,
        )

        self.plan = build_plan(document_snapshot)
        self.configuration = deepcopy(
            acquisition_plan["configuration"]
        )
        self.monitoring = deepcopy(
            require_charge_monitoring_settings(
                self.configuration
            )
        )

        self.smu = smu
        self.document = document_snapshot
        self.base_directory = base_directory
        self.run_prefix = run_prefix
        self.clock = clock
        self.sleep = sleep

    def _planned_phase(self, phase):
        if not isinstance(phase, dict):
            raise ValueError("phase must be a dictionary.")

        if phase.get("type") not in (
            "charge_cc_cv",
            "discharge_cc",
        ):
            raise ValueError(
                "Executor accepts charge or discharge phases only."
            )

        index = phase.get("phase_index")

        if type(index) is not int or index < 0:
            raise ValueError(
                "phase_index must be a nonnegative integer."
            )

        expected = next(
            (
                item
                for item in self.plan["phases"]
                if item["phase_index"] == index
            ),
            None,
        )

        if expected is None or phase != expected:
            raise ValueError(
                "Requested phase differs from the configured plan."
            )

        return deepcopy(expected)

    def _validate_prepared(self, prepared, planned, writer):
        if not isinstance(prepared.phase, dict):
            raise ValueError(
                "Prepared phase must be a dictionary."
            )

        for key, value in planned.items():
            if (
                key not in prepared.phase
                or prepared.phase[key] != value
            ):
                raise ValueError(
                    "Prepared phase differs from the plan: "
                    f"{key}."
                )

        if (
            prepared.evidence.get("state") != "prepared"
            or prepared.evidence.get(
                "both_outputs_reported_off"
            ) is not True
        ):
            raise ValueError(
                "Preparation did not verify both outputs OFF."
            )

        if (
            prepared.transport.writer is not writer
            or prepared.transport.acquisition
            is not prepared.acquisition
        ):
            raise ValueError(
                "Prepared acquisition/transport/writer ownership "
                "does not match."
            )

        if (
            writer.closed
            or writer.failed
            or writer.records_flushed != 0
        ):
            raise ValueError(
                "Prepared recording writer must be fresh and usable."
            )

        if (
            prepared.acquisition._count is None
            or not prepared.acquisition._trace_ready
        ):
            raise ValueError(
                "Prepared acquisition is not configured."
            )

        # Validate monitor configuration before output activation.
        # Construction performs no saving or hardware operations.
        if planned["type"] == "charge_cc_cv":
            if not isinstance(
                prepared.transport,
                ChargeTraceTransport,
            ):
                raise TypeError(
                    "Expected ChargeTraceTransport."
                )

            ChargeTraceMonitor(
                prepared.phase,
                save_batch=lambda batch: None,
                cv_classifier=prepared.cv_classifier,
                instrument_timestamp_origin_s=0.0,
                phase_elapsed_at_origin_s=0.0,
                maximum_sample_gap_s=self.monitoring[
                    "maximum_sample_gap_s"
                ],
            )

        else:
            if not isinstance(
                prepared.transport,
                DischargeTraceTransport,
            ):
                raise TypeError(
                    "Expected DischargeTraceTransport."
                )

            DischargeTraceMonitor(
                prepared.phase,
                save_batch=lambda batch: None,
                instrument_timestamp_origin_s=0.0,
                phase_elapsed_at_origin_s=0.0,
                maximum_sample_gap_s=self.monitoring[
                    "maximum_sample_gap_s"
                ],
                maximum_voltage_v=self.monitoring[
                    "maximum_voltage_v"
                ],
            )

    def __call__(self, phase):
        planned = self._planned_phase(phase)
        channel = self.configuration["instrument"]["channel"]

        run = None
        prepared = None
        activation = None
        report = None

        try:
            run = ExperimentPhaseRun(
                self.base_directory,
                (
                    f"{self.run_prefix}"
                    f"_phase_{planned['phase_index']}"
                ),
                metadata={
                    "channel": channel,
                    "configuration": self.configuration,
                    "planned_phase": planned,
                    "execution_policy": {
                        "measurement_time_basis": (
                            "first_recorded_sample"
                        ),
                        "host_phase_start": (
                            "before_output_activation"
                        ),
                        "maximum_voltage_v": (
                            self.maximum_voltage_v
                        ),
                        "maximum_current_a": (
                            self.maximum_current_a
                        ),
                    },
                },
            )

            if planned["type"] == "charge_cc_cv":
                prepared = prepare_charge(
                    self.smu,
                    self.document,
                    run.writer,
                    cycle=planned["cycle"],
                )
            else:
                prepared = prepare_discharge(
                    self.smu,
                    self.document,
                    run.writer,
                    cycle=planned["cycle"],
                )

            self._validate_prepared(
                prepared,
                planned,
                run.writer,
            )

            phase_started = _finite_number(
                self.clock(),
                "host phase start",
            )

            activation = activate_phase_output(
                self.smu,
                channel,
                maximum_voltage_v=self.maximum_voltage_v,
                maximum_current_a=self.maximum_current_a,
                clock=self.clock,
            )

            now = _finite_number(
                self.clock(),
                "host clock after activation",
            )

            if not (
                activation.channel == channel
                and phase_started
                <= activation.host_before_enable_s
                <= activation.host_after_verification_s
                <= now
            ):
                raise RuntimeError(
                    "Activation timing evidence is inconsistent."
                )

            elapsed = now - phase_started

            if (
                not math.isfinite(elapsed)
                or elapsed >= planned["maximum_duration_s"]
            ):
                raise TimeoutError(
                    "Phase host deadline reached during activation."
                )

            arguments = {
                "instrument_timestamp_origin_s": None,
                "phase_elapsed_at_origin_s": None,
                "maximum_sample_gap_s": self.monitoring[
                    "maximum_sample_gap_s"
                ],
                "poll_interval_s": self.poll_interval_s,
                "phase_started_s": phase_started,
                "clock": self.clock,
                "sleep": self.sleep,
                "measurement_time_basis": (
                    "first_recorded_sample"
                ),
            }

            if planned["type"] == "charge_cc_cv":
                report = prepared.transport.acquire_charge(
                    prepared.phase,
                    cv_classifier=prepared.cv_classifier,
                    **arguments,
                )
            else:
                report = prepared.transport.acquire_discharge(
                    prepared.phase,
                    maximum_voltage_v=self.monitoring[
                        "maximum_voltage_v"
                    ],
                    **arguments,
                )

            # Do not begin success-file finalization until this
            # executor's additional shutdown attempt returns.
            self.smu.shutdown()

            report["preparation_evidence"] = deepcopy(
                prepared.evidence
            )
            report["activation_evidence"] = {
                **asdict(activation),
                "measurement_time_basis": (
                    "first_recorded_sample"
                ),
                "output_enable_time_established": False,
            }

            run.finish_phase(
                prepared.phase,
                report,
                prepared.acquisition.readings,
            )

            return run

        except BaseException as original_error:
            if report is not None:
                original_error.phase_transport_report = report

            if prepared is not None:
                if not hasattr(original_error, "partial_readings"):
                    original_error.partial_readings = tuple(
                        prepared.acquisition.readings
                    )

                preparation_attribute = (
                    "charge_preparation_report"
                    if planned["type"] == "charge_cc_cv"
                    else "discharge_preparation_report"
                )

                if not hasattr(
                    original_error,
                    preparation_attribute,
                ):
                    setattr(
                        original_error,
                        preparation_attribute,
                        deepcopy(prepared.evidence),
                    )

            if (
                activation is not None
                and not hasattr(original_error, "activation_report")
            ):
                original_error.activation_report = {
                    "state": "activated",
                    **asdict(activation),
                    "instrument_timestamp_mapping_established": False,
                }

            # Preserve earlier shutdown evidence in memory.
            if hasattr(original_error, "shutdown_report"):
                original_error.executor_previous_shutdown_report = (
                    deepcopy(original_error.shutdown_report)
                )

            # This canonical report describes the executor's latest
            # cleanup attempt. Cleanup precedes failure recording.
            try:
                attempt_shutdown_with_report(
                    self.smu,
                    original_error,
                )
            except BaseException as cleanup_error:
                original_error.add_note(
                    "Executor cleanup was interrupted: "
                    f"{type(cleanup_error).__name__}: "
                    f"{cleanup_error}"
                )

            if run is not None and not run.finalized:
                _record_failure_preserving_original(
                    run,
                    original_error,
                )

            raise