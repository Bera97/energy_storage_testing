"""Internal PHYSICAL-CAPABLE session-per-stage CP sweep backend.

No session defaults, CLI dispatch, retry/resume or shared/dual session support.
The injected factory owns failed connection attempts until it returns a driver.
Fake factory injections are trusted tests, not a physical-proof/security boundary.
"""
from pathlib import Path
from experiments.config import require_charge_monitoring_settings
from experiments.constant_power import _number
from experiments.power_adapter import _HostClock
from experiments.power_smu_backend import B2902BPowerBackend
from experiments.recorded_power_phase import run_recorded_power_phase
from experiments.recorded_phase_executor import RecordedPhaseExecutor
from experiments.rest_outputs_off import rest_outputs_off
from experiments import zero_current_rest as zrest
from experiments.power_sweep_contract import (
    CONTROLLED_PHASES, MAXIMUM_RECORDS, base_phase, build_sequence, control_section, phase_limits, require, same,
    snapshot, tree_files, write_json,
)


class RecordedPowerSweepBackend:
    def __init__(self, document, *, open_session, maximum_probe_current_a,
                 clock, sleep, output_authorized=False, poll_interval_s=0.05,
                 rest_poll_interval_s=0.1, active_executor_factory=RecordedPhaseExecutor,
                 power_backend_factory=B2902BPowerBackend):
        self.document = snapshot(document)
        self.plan, self.requests = build_sequence(self.document)
        require(type(output_authorized) is bool, "Output authorization must be boolean.")
        require(all(callable(fn) for fn in (open_session, clock, sleep, active_executor_factory,
                                           power_backend_factory)), "Explicit callable dependencies required.")
        self.probe_ceiling = _number(maximum_probe_current_a, "probe ceiling")
        require(self.probe_ceiling <= control_section(document)["maximum_current_a"], "Probe exceeds power-control ceiling.")
        self.poll_interval_s = _number(poll_interval_s, "poll interval")
        self.rest_poll_interval_s = _number(rest_poll_interval_s, "rest poll interval")
        self.settings = snapshot(self.plan["execution_settings"])
        serial = self.settings["expected_serial"]
        require(isinstance(serial, str) and bool(serial.strip()) and not serial.upper().startswith("REPLACE"),
                "A concrete expected serial is required.")
        self.configuration = snapshot(self.plan["initial_preparation"]["configuration"])
        require_charge_monitoring_settings(self.configuration)
        require(isinstance(self.plan["resource"], str) and bool(self.plan["resource"].strip())
                and "REPLACE" not in self.plan["resource"].upper(), "Concrete instrument resource required.")
        require(isinstance(self.plan["specimen_id"], str) and bool(self.plan["specimen_id"].strip())
                and not self.plan["specimen_id"].upper().startswith("REPLACE"), "Concrete specimen identity required.")
        self.policy = {"maximum_probe_current_a": self.probe_ceiling, "maximum_records": MAXIMUM_RECORDS,
                       "session_policy": "new_owned_session_per_stage"}
        self.open_session, self.sleep = open_session, sleep
        self.clock = _HostClock(clock).now
        self.output_authorized = output_authorized
        self.active_executor_factory, self.power_backend_factory = active_executor_factory, power_backend_factory
        self.claimed, self.failed, self.in_stage, self.position = False, False, False, 0

    def _cleanup(self, smu):
        result = {"shutdown_commands_returned": False, "output_states": [None, None],
                  "outputs_reported_off": None, "session_close_returned": False, "errors": []}
        failures = []
        def record(error):
            failures.append(error)
            result["errors"].append(f"{type(error).__name__}: {error}")
        if smu is None:
            return result, failures
        try:
            smu.shutdown()
            result["shutdown_commands_returned"] = True
        except BaseException as error:
            record(error)
        for index, name in enumerate(("chan1", "chan2")):
            try:
                state = getattr(smu, name).get_output_state()
                smu.check_errors()
                require(type(state) is bool, "Malformed cleanup output state.")
                result["output_states"][index] = state
                require(state is False, "Cleanup did not report both outputs OFF.")
            except BaseException as error:
                record(error)
        states = result["output_states"]
        result["outputs_reported_off"] = None if any(v is None for v in states) else all(v is False for v in states)
        try:
            smu.close()
            result["session_close_returned"] = True
        except BaseException as error:
            record(error)
        return result, failures

    def execute_stage(self, request, directory):
        require(self.claimed and self.output_authorized is True and not self.in_stage and not self.failed,
                "Stage backend is not authorized/available.")
        require(self.position < len(self.requests), "No stage remains.")
        same(request, self.requests[self.position], "Stage request differs from canonical order.")
        directory = Path(directory)
        directory.mkdir(exist_ok=False)
        document = {"power_sweep_stage_schema_version": 1, "state": "running",
                    "request": snapshot(request), "configuration": self.configuration,
                    "sweep_configuration": self.document, "execution_policy": self.policy,
                    "execution_settings": self.settings, "instrument_identity": None,
                    "driver_type": None, "resource_address": None, "evidence": None,
                    "started_s": None, "finished_s": None, "cleanup": None, "error": None, "files": {}}
        write_json(directory / "stage.json", document, create=True)
        self.in_stage = True
        smu, failure, delegated = None, None, None
        try:
            document["started_s"] = self.clock()
            smu = self.open_session(snapshot(request), snapshot(self.document))
            require(smu is not None and smu.is_connected is True, "Factory must return an already-connected owned driver.")
            document["instrument_identity"] = smu.identity
            document["driver_type"] = type(smu).__module__ + "." + type(smu).__qualname__
            document["resource_address"] = smu.resource_address
            parts = smu.identity.split(",")
            require(len(parts) == 4 and all(p.strip() for p in parts) and parts[1].strip().upper() == "B2902B"
                    and parts[2].strip() == self.settings["expected_serial"], "Instrument identity mismatch.")
            require(smu.resource_address == self.plan["resource"], "Instrument resource differs from configuration.")
            smu.check_errors()
            for selected in (smu.chan1, smu.chan2):
                require(selected.get_output_state() is False, "New stage requires both outputs OFF.")
                smu.check_errors()
            phase = request["phase"]
            if phase["type"] in CONTROLLED_PHASES:
                instrument = self.configuration["instrument"]
                transport = self.power_backend_factory(smu, channel=instrument["channel"], four_wire=instrument["four_wire"],
                    nplc=instrument["nplc"], output_authorized=True, **self.settings)
                try:
                    outcome = run_recorded_power_phase(phase_limits(phase), directory / "power",
                        backend=transport, clock=self.clock, sleep=self.sleep, channel=instrument["channel"],
                        maximum_probe_current_a=self.probe_ceiling, maximum_samples=MAXIMUM_RECORDS,
                        specimen_id=self.plan["specimen_id"], configuration_snapshot=self.document, execute_requested=True)
                except BaseException as error:
                    delegated = getattr(error, "power_phase_outcome", None)
                    if not isinstance(delegated, dict):
                        delegated = None
                    raise
                else:
                    delegated = outcome
                document["evidence"] = {"kind": "power", "directory": "power"}
            else:
                planned = base_phase(self.configuration, request)
                if phase["type"] == "rest" and not zrest.is_measured(phase):
                    report = rest_outputs_off(smu, phase["duration_s"], clock=self.clock, sleep=self.sleep,
                                              poll_interval_s=self.rest_poll_interval_s)
                    document["evidence"] = {"kind": "outputs_off_rest", "report": report}
                else:
                    executor = self.active_executor_factory(smu, snapshot(self.configuration), directory, "phase",
                        maximum_voltage_v=self.settings["maximum_voltage_v"], maximum_current_a=self.settings["maximum_current_a"],
                        clock=self.clock, sleep=self.sleep, poll_interval_s=self.poll_interval_s)
                    if phase["type"] == "rest":
                        report = executor.record_rest(planned)
                        document["evidence"] = {"kind": "measured_rest", "report": report,
                                                "directory": f"rest_phase_{phase['phase_index']:04d}"}
                    else:
                        run = executor(planned)
                        expected = f"phase_phase_{phase['phase_index']}"
                        same(str(run.directory.resolve()), str((directory / expected).resolve()), "Unexpected active recording directory.")
                        document["evidence"] = {"kind": "incremental_csv", "directory": expected}
        except BaseException as error:
            failure = error
        finally:
            if delegated is None:
                cleanup, errors = self._cleanup(smu)
            else:
                cleanup = {key: delegated.get(key) for key in (
                    "shutdown_commands_returned", "output_states", "outputs_reported_off", "session_close_returned")}
                cleanup["errors"] = list(delegated.get("cleanup_errors", ()))
                errors = []
            document["cleanup"] = cleanup
            for error in errors:
                if failure is None:
                    failure = error
                else:
                    failure.add_note(f"Sweep stage cleanup: {type(error).__name__}: {error}")
            try:
                document["finished_s"] = self.clock()
            except BaseException as error:
                if failure is None:
                    failure = error
                else:
                    failure.add_note(f"Sweep stage clock: {error}")
            self.in_stage = False
        if failure is None:
            try:
                document["state"] = "completed"
                document["files"] = tree_files(directory)
                write_json(directory / "stage.json", document)
            except BaseException as error:
                failure = error
        if failure is not None:
            self.failed = True
            document["state"] = "raised"
            document["error"] = {"type": type(failure).__name__, "message": str(failure),
                                 "notes": list(getattr(failure, "__notes__", ()))}
            try:
                write_json(directory / "stage.json", document)
            except BaseException as secondary:
                failure.add_note(f"Sweep stage evidence export: {type(secondary).__name__}: {secondary}")
            raise failure
        self.position += 1
