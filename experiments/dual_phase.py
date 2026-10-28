"""Channel-local recorded phase adapter. No public live execution command.
Output capability requires explicit allow_output=True. Source settings are
checked against the validated profile, not an independently qualified envelope.
Any runtime phase fault attempts global stop before recording the failure.
The owning coordinator must also abort/finalize the peer and close the session.
"""
from copy import deepcopy
import math
import time
from acquisition.dual_trace import ChannelTraceAcquisition
from experiments.charge_cv_classifier import VoltageSourceCVClassifier
from experiments.charge_trace_monitor import ChargeTraceMonitor
from experiments.discharge_trace_monitor import DischargeTraceMonitor
from experiments.first_sample_trace_monitor import create_trace_monitor
from experiments.phase_safety_monitor import PhaseSafetyMonitor
from recording.experiment_phase_run import (
    ExperimentPhaseRun,
    _record_failure_preserving_original,
)
class DualPhase:
    def __init__(
        self,
        session,
        channel,
        phase,
        directory,
        run_id,
        *,
        clock=time.monotonic,
        allow_output=False,
        drain_timeout_s=10.0,
    ):
        if type(channel) is not int or channel not in (1, 2):
            raise ValueError("channel must be integer 1 or 2.")
        if type(allow_output) is not bool or not callable(clock):
            raise ValueError(
                "Explicit boolean authorization and a clock are required."
            )
        if (
            isinstance(drain_timeout_s, bool)
            or not isinstance(drain_timeout_s, (int, float))
            or not math.isfinite(drain_timeout_s)
            or drain_timeout_s <= 0
        ):
            raise ValueError("drain_timeout_s must be finite and positive.")
        preview = next(
            item for item in session.plan["channels"]
            if item["channel"] == channel
        )
        planned = next(
            (
                item
                for item in preview["acquisition_plan"]["phases"]
                if item == phase
            ),
            None,
        )
        if (
            planned is None
            or planned["type"] not in ("charge_cc_cv", "discharge_cc")
        ):
            raise ValueError(
                "Phase must exactly match an active phase "
                "in the validated plan."
            )
        self.session = session
        self.channel = channel
        self.phase = deepcopy(planned)
        self.config = deepcopy(
            preview["acquisition_plan"]["configuration"]
        )
        self.directory = directory
        self.run_id = run_id
        self.clock = clock
        self.allow_output = allow_output
        self.drain_timeout_s = float(drain_timeout_s)
        self.state = "new"
        self.run = None
        self.reader = None
        self.monitor = None
        self.started = None
        self.last_clock = None
        self.saved = 0
        self.decision = None
        self.outcome = None
    def _now(self):
        value = self.clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise RuntimeError("Host clock must be finite.")
        value = float(value)
        if self.last_clock is not None and value < self.last_clock:
            raise RuntimeError("Host clock moved backwards.")
        self.last_clock = value
        return value
    def _checked(self, operation, *args):
        self.session._require_connected()
        result = operation(*args)
        self.session.smu.check_errors()
        return result
    def _same(self, actual, expected, label):
        if (
            isinstance(actual, bool)
            or not isinstance(actual, (int, float))
            or not math.isfinite(actual)
            or not math.isclose(
                actual, expected, rel_tol=1e-9, abs_tol=1e-15
            )
        ):
            raise RuntimeError(f"{label} readback mismatch.")
    def _require_off(self):
        selected = self.session._channel(self.channel)
        if self._checked(selected.get_output_state) is not False:
            raise RuntimeError("Selected channel must report OFF.")
        return selected
    def _verify_source(self):
        selected = self._require_off()
        charge = self.phase["type"] == "charge_cc_cv"
        mode = "VOLT" if charge else "CURR"
        if self._checked(selected.get_source_mode) != mode:
            raise RuntimeError("Source mode readback mismatch.")
        for command, expected in (
            (f":SOUR{self.channel}:FUNC:SHAP?", "DC"),
            (f":SOUR{self.channel}:{mode}:MODE?", "FIX"),
        ):
            response = self._checked(self.session.smu._query, command)
            if response.strip().upper() != expected:
                raise RuntimeError(
                    f"Source configuration mismatch: {command}"
                )
        if self._checked(selected.get_auto_output_on) is not False:
            raise RuntimeError("Automatic output-on must report OFF.")
        if self._checked(selected.get_output_off_mode) != "HIZ":
            raise RuntimeError("Output-off mode must report HIZ.")
        if (
            self._checked(selected.get_four_wire)
            is not self.config["instrument"]["four_wire"]
        ):
            raise RuntimeError("Sense configuration readback mismatch.")
        self._same(
            self._checked(selected.get_nplc),
            self.config["instrument"]["nplc"],
            "NPLC",
        )
        if charge:
            self._same(
                self._checked(selected.get_voltage_setpoint),
                self.phase["target_voltage_v"],
                "Voltage",
            )
            self._same(
                self._checked(selected.get_current_limit),
                self.phase["current_magnitude_a"],
                "Current compliance",
            )
        else:
            self._same(
                self._checked(selected.get_current_setpoint),
                -self.phase["current_magnitude_a"],
                "Discharge current",
            )
            self._same(
                self._checked(selected.get_voltage_limit),
                self.config["dut"]["ratings"]["maximum_voltage_v"],
                "Voltage compliance",
            )
    def _configure_source(self):
        selected = self._require_off()
        self._checked(
            self.session.smu._write,
            f":ABOR:ALL (@{self.channel})",
        )
        self._checked(selected.set_auto_output_on, False)
        self._checked(selected.set_output_off_mode, "HIZ")
        charge = self.phase["type"] == "charge_cc_cv"
        self._checked(
            selected.configure_fixed_dc,
            "VOLT" if charge else "CURR",
        )
        if charge:
            self._checked(
                selected.set_voltage,
                self.phase["target_voltage_v"],
            )
            self._checked(
                selected.set_current_limit,
                self.phase["current_magnitude_a"],
            )
        else:
            self._checked(
                selected.set_current,
                -self.phase["current_magnitude_a"],
            )
            self._checked(
                selected.set_voltage_limit,
                self.config["dut"]["ratings"]["maximum_voltage_v"],
            )
        self._checked(
            selected.set_four_wire,
            self.config["instrument"]["four_wire"],
        )
        self._verify_source()
    def _save(self, batch):
        writer = self.run.writer
        if (
            writer.closed
            or writer.failed
            or writer.records_flushed != self.saved
        ):
            raise RuntimeError(
                "Recording writer is closed, failed, or inconsistent."
            )
        written = writer.write_batch(batch)
        if written != len(batch):
            raise RuntimeError("Recording batch count mismatch.")
        self.saved += written
        if writer.records_flushed != self.saved:
            raise RuntimeError("Flushed record count mismatch.")
        return written
    def _deadline(self):
        now = self._now()
        if now - self.started >= self.phase["maximum_duration_s"]:
            raise TimeoutError("Channel phase host deadline reached.")
        cv_deadline = self.monitor.host_cv_deadline_s
        if cv_deadline is not None and now >= cv_deadline:
            raise TimeoutError("Channel CV host deadline reached.")
    def start(self):
        if self.state != "new":
            raise RuntimeError("Use a fresh phase adapter.")
        if self.allow_output is not True:
            raise RuntimeError(
                "Output capability is not authorized for this adapter."
            )
        self.state = "preparing"
        try:
            self.run = ExperimentPhaseRun(
                self.directory,
                self.run_id,
                metadata={
                    "channel": self.channel,
                    "configuration": self.config,
                    "planned_phase": self.phase,
                    "adapter": "dual_phase",
                    "operating_envelope_verified": False,
                },
            )
            self._configure_source()
            self.started = self._now()
            monitoring = self.config["acquisition"]
            def factory(origin, elapsed):
                common = dict(
                    save_batch=self._save,
                    instrument_timestamp_origin_s=origin,
                    phase_elapsed_at_origin_s=elapsed,
                    maximum_sample_gap_s=monitoring[
                        "maximum_sample_gap_s"
                    ],
                )
                if self.phase["type"] == "charge_cc_cv":
                    classifier = VoltageSourceCVClassifier(
                        target_voltage_v=self.phase["target_voltage_v"],
                        voltage_tolerance_v=self.config["charge"][
                            "cv_voltage_tolerance_v"
                        ],
                        maximum_voltage_v=self.config["dut"][
                            "ratings"
                        ]["maximum_voltage_v"],
                    )
                    return ChargeTraceMonitor(
                        self.phase,
                        cv_classifier=classifier,
                        **common,
                    )
                return DischargeTraceMonitor(
                    self.phase,
                    maximum_voltage_v=self.config["dut"][
                        "ratings"
                    ]["maximum_voltage_v"],
                    **common,
                )
            measurement = create_trace_monitor(
                factory,
                measurement_time_basis="first_recorded_sample",
                instrument_timestamp_origin_s=None,
                phase_elapsed_at_origin_s=None,
                phase_started_s=self.started,
            )
            self.monitor = PhaseSafetyMonitor(
                measurement,
                phase_started_s=self.started,
                maximum_duration_s=self.phase["maximum_duration_s"],
                clock=self.clock,
                measurement_time_basis="first_recorded_sample",
                instrument_timestamp_origin_s=None,
                phase_elapsed_at_origin_s=None,
            )
            self.reader = ChannelTraceAcquisition(
                self.session,
                self.channel,
                self.monitor.ingest,
                save_trailing_batch=self._save,
            )
            budget = self.phase["acquisition_budget"]
            self.reader.configure(
                budget["record_count"],
                budget["interval_s"],
            )
            self._deadline()
            self._verify_source()
            self._checked(
                self.session.smu._write,
                f":OUTP{self.channel} ON",
            )
            if (
                self._checked(
                    self.session._channel(
                        self.channel
                    ).get_output_state
                )
                is not True
            ):
                raise RuntimeError(
                    "Selected channel did not report ON."
                )
            self._deadline()
            self.monitor.before_initiation()
            self.reader.start()
            self._deadline()
            self.state = "active"
        except BaseException as error:
            self._fail(error)
            raise
    def poll(self):
        if self.state == "completed":
            return deepcopy(self.outcome)
        if self.state not in ("active", "draining"):
            raise RuntimeError("Phase is not active or draining.")
        try:
            if self.state == "active":
                self._deadline()
                batch = self.reader.poll()
                self._deadline()
                decision = batch.decision
                if (
                    decision is not None
                    and decision.stop_reason is not None
                ):
                    expected = (
                        "cv_current_termination"
                        if self.phase["type"] == "charge_cc_cv"
                        else "voltage_min"
                    )
                    if decision.stop_reason != expected:
                        raise RuntimeError("Unexpected phase endpoint.")
                    self.decision = decision
                    self.before_finalization = self.saved
                    self.drain_deadline = (
                        self._now() + self.drain_timeout_s
                    )
                    self.reader.stop()
                    self.state = "draining"
                    return None
                if (
                    batch.idle
                    or len(self.reader.readings) == self.reader.count
                ):
                    raise RuntimeError(
                        "Finite acquisition ended without "
                        "the required endpoint."
                    )
                return None
            if self._now() >= self.drain_deadline:
                raise TimeoutError(
                    "Channel final trace drain timed out."
                )
            self.reader.poll()
            if self._now() >= self.drain_deadline:
                raise TimeoutError(
                    "Channel final trace drain timed out."
                )
            if self.reader.state != "drained":
                return None
            kind = (
                "charge"
                if self.phase["type"] == "charge_cc_cv"
                else "discharge"
            )
            records = tuple(self.reader.readings)
            report = {
                "state": "returned",
                "phase_type": kind,
                "expected_stop_reason": self.decision.stop_reason,
                "stop_reason": self.decision.stop_reason,
                "shutdown_verified": True,
                "shutdown_scope": "selected_channel",
                "finalization_attempted": True,
                "finalized": True,
                "error": None,
                "cleanup_error": None,
                "finalization_error": None,
                "requested_count": self.reader.count,
                "retained_count": len(records),
                "records_flushed": self.run.writer.records_flushed,
                "records_saved_before_finalization": (
                    self.before_finalization
                ),
                "triggering_record_index": (
                    self.decision.triggering_record_index
                ),
                "trailing_record_count": (
                    len(records) - self.before_finalization
                ),
                "fault_record_indices": [
                    index
                    for index, record in enumerate(records)
                    if record.has_fault
                ],
                "measurement_timing": self.monitor.timing_evidence,
            }
            result, directory = self.run.finish_phase(
                self.phase,
                report,
                records,
            )
            self.outcome = {
                "state": "completed",
                "stop_reason": result.stop_reason,
                "run_directory": str(directory.resolve()),
                "evidence_kind": "instrument_readback_and_recording",
                "phase_index": self.phase["phase_index"],
                "cycle": self.phase["cycle"],
                "type": self.phase["type"],
            }
            self.state = "completed"
            return deepcopy(self.outcome)
        except BaseException as error:
            self._fail(error)
            raise
    def _fail(self, error):
        self.state = "failed"
        shutdown = {
            "attempted": True,
            "verified": None,
            "evidence": "shutdown_attempt_started",
            "error_type": None,
            "error_message": None,
        }
        try:
            if self.session.shutdown_all() is not True:
                raise RuntimeError(
                    "Global shutdown was not verified."
                )
        except BaseException as cleanup:
            shutdown.update(
                verified=False,
                evidence="smu_shutdown_raised",
                error_type=type(cleanup).__name__,
                error_message=str(cleanup),
            )
            error.add_note(
                f"Dual-phase global cleanup failed: {cleanup}"
            )
        else:
            shutdown.update(
                verified=True,
                evidence="smu_shutdown_returned",
            )
        self.abort_after_stop(error, shutdown)
    def abort_after_stop(self, error, shutdown_report):
        """Record interruption after the caller's global stop attempt."""
        if self.state == "completed":
            return
        error.partial_readings = (
            tuple(self.reader.readings)
            if self.reader is not None
            else ()
        )
        error.shutdown_report = deepcopy(shutdown_report)
        error.phase_transport_report = {
            "state": "raised",
            "channel": self.channel,
            "phase_index": self.phase["phase_index"],
            "retained_count": len(error.partial_readings),
            "raw_trace_response": getattr(
                error, "raw_trace_response", None
            ),
            "error": {
                "type": type(error).__name__,
                "message": str(error),
            },
        }
        if self.run is not None and not self.run.finalized:
            _record_failure_preserving_original(self.run, error)
        self.state = "aborted"
