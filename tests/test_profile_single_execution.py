"""Single-channel profile discharge through the recorded sweep backend, replay and export (wire fake)."""
from copy import deepcopy
import json
from pathlib import Path
from unittest import mock
import pytest
import smu
import test_recorded_phase_executor as legacy
from test_recorded_power_phase import WireSMU
from test_recorded_power_sweep import Clock
from analysis.analyze_power_sweep import analyze_power_sweep
from analysis.export_power_sweep import export_power_sweep_analysis, validate_power_sweep_analysis
from analysis.power_sweep import validate_power_sweep
from experiments.acquisition_budget import build_acquisition_plan
from experiments.power_sweep_backend import RecordedPowerSweepBackend
from experiments.profile_planning import validate_profile_discharge_test
from experiments.recorded_power_sweep import run_recorded_power_sweep
CONFIGS = Path(__file__).resolve().parents[1] / "configs"
STEPS = ((2, 0.1), (2, 0.0), (2, 0.05))
READS_BEFORE_CUTOFF = 10
class ProfileWireSMU(WireSMU):
    """WireSMU whose terminal voltage stays high for N reads, then falls below any cutoff."""
    def __init__(self, clock, channel, reads_before_cutoff):
        super().__init__(clock, channel)
        self.reads_before_cutoff = reads_before_cutoff
    def _query(self, command):
        if command.startswith(":MEAS? "):
            parts = super()._query(command).split(",")
            parts[0] = str(3.8 if self.read_count <= self.reads_before_cutoff else 2.7)
            return ",".join(parts)
        return super()._query(command)
@pytest.fixture
def context(tmp_path):
    fixture = legacy.RecordedPhaseExecutorTests(methodName="test_charge_returns_finalized_recording")
    fixture.setUp()
    fixture.clock = Clock()
    document = deepcopy(fixture.document)
    document["experiment"]["type"] = "profile_discharge_test"
    document["experiment"]["cycles"] = 1
    document["execution"] = {"expected_serial": "TESTSERIAL", "maximum_voltage_v": 4.2, "maximum_current_a": 4.0}
    document["discharge"]["maximum_duration_s"] = 60
    document["rest"].update(after_charge_s=0.6, after_discharge_s=0.6)
    document["profile"] = {"kind": "current", "steps": [{"duration_s": d, "value": v} for d, v in STEPS]}
    document["profile_control"] = {
        "maximum_current_a": 0.4, "update_interval_s": 1.0, "maximum_feedback_age_s": 2.0,
        "relative_tolerance": 0.05, "absolute_tolerance_w": 0.005, "startup_grace_s": 2.0,
        "maximum_tracking_error_s": 2.0, "zero_current_tolerance_a": 0.002}
    sessions = []
    def open_session(request, config):
        if request["phase"]["type"] == "discharge_profile":
            smu_ = ProfileWireSMU(fixture.clock, config["instrument"]["channel"], READS_BEFORE_CUTOFF)
            original_write = smu_._write
            def write(command):
                if " " not in command:
                    smu_.clock.time += 0.00001
                    smu_.commands.append(command)
                    return None
                return original_write(command)
            smu_._write = write
        else:
            smu_ = legacy.FakeSMU(fixture.events)
            smu_.is_connected = True
            smu_.identity = "Keysight Technologies,B2902B,TESTSERIAL,1.0"
            def close():
                smu_.is_connected = False
                fixture.events.append("close")
            smu_.close = close
            fixture.smu = smu_
        smu_.resource_address = config["instrument"]["visa_address"]
        sessions.append((request, smu_))
        return smu_
    def make(**changes):
        fixture.document = validate_profile_discharge_test(document)["preparation_config"]
        fixture.acquisition_plan = build_acquisition_plan(fixture.document, maximum_records=100000)
        arguments = dict(open_session=open_session, maximum_probe_current_a=0.001, clock=fixture.clock,
                         sleep=fixture.clock.sleep, output_authorized=True, rest_poll_interval_s=1000.0)
        arguments.update(changes)
        return RecordedPowerSweepBackend(document, **arguments)
    def execute():
        return run_recorded_power_sweep(document, tmp_path / "sweep", backend=make(), execute_requested=True)
    yield document, make, execute, sessions
    fixture.doCleanups()
def events(tmp_path):
    path = tmp_path / "sweep/stage_00004/power/power_events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]
def test_single_profile_run_applies_steps_replays_analyzes_and_exports(context, tmp_path):
    _, _, execute, sessions = context
    result = execute()
    assert result["completed_stage_count"] == 6
    checked = validate_power_sweep(tmp_path / "sweep")
    assert checked["valid"], checked["errors"]
    rows = events(tmp_path)
    requested = [e["current_setpoint_a"] for e in rows if e["event"] == "current_command_requested"]
    acknowledged = [e["current_setpoint_a"] for e in rows if e["event"] == "current_setpoint_acknowledged"]
    assert requested[:3] == [-0.1, 0.0, -0.05] and acknowledged == requested
    assert {e["profile_step"] for e in rows if e["event"] == "control_decision"} == {0, 1, 2}
    assert all(not smu_.is_connected for _, smu_ in sessions)
    report = analyze_power_sweep(tmp_path / "sweep")
    phase = next(p for p in report["phases"] if p["type"] == "discharge_profile")
    assert phase["sample_count"] >= 8 and phase["mean_discharge_power_error_w"] is None
    exported = export_power_sweep_analysis(tmp_path / "sweep", tmp_path / "analysis")
    assert exported["state"] == "completed"
    assert validate_power_sweep_analysis(tmp_path / "analysis")["valid"] is True
def test_cli_single_profile_execution_dispatches_to_the_single_runner():
    args = ["--config", str(CONFIGS / "profile_current_trial_7_8_ch1.yaml"), "--execute"]
    with mock.patch("experiments.power_execution.run_configured_power_sweep", return_value=0) as launcher:
        assert smu.main(args) == 0
    assert launcher.call_count == 1
