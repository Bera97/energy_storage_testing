"""Dual-channel profile discharge through the shared coordinator, replay and export (fake wire)."""
from copy import deepcopy
from pathlib import Path
from unittest import mock
import pytest
import smu
from analysis.export_shared_power import export_shared_power_analysis, validate_shared_power_analysis
from analysis.shared_power_replay import replay_shared_power_acquisition
from experiments.dual_instrument_io import DualInstrumentSession
from experiments.dual_preconditioning import build_dual_plan
from experiments.power_planning import build_dual_constant_power_sweep_plan
from experiments.power_shared_coordinator import run_shared_power_sweep
from experiments.power_shared_transport import BorrowedPowerChannel, SharedPowerSession
from test_charge_current_sweep import profile
from test_constant_power_planning import power_profile
from test_power_shared_coordinator import ScriptWire, journal
from test_power_shared_phases import TraceWire
CONFIGS = Path(__file__).resolve().parents[1] / "configs"
STEPS = ((1, 0.1), (1, 0.0), (1, 0.05))
def to_profile(document, steps, kind="current"):
    result = deepcopy(document)
    result["experiment"]["type"] = "profile_discharge_test"
    del result["sweep"]
    control = result.pop("power_control")
    result["profile"] = {"kind": kind, "steps": [{"duration_s": d, "value": v} for d, v in steps]}
    result["profile_control"] = {**control, "zero_current_tolerance_a": 0.002}
    return result
class ProfileWire(ScriptWire):
    """ScriptWire whose borrowed-channel measurements follow the commanded current."""
    def __init__(self, resource, documents, samples_before_cutoff):
        super().__init__(resource, documents)
        self.samples_before_cutoff = samples_before_cutoff
    def _query(self, command):
        if command.startswith(":MEAS? "):
            c = int(command.split("(@")[1][0])
            lease = None if self.owner is None else self.owner._channels.get(c)
            if isinstance(lease, BorrowedPowerChannel):
                self.cp_samples[c] += 1
                self.measure_voltage[c] = (3.0 if self.cp_samples[c] < self.samples_before_cutoff
                                           else lease.limits.cutoff_voltage_v)
                self.measure_current[c] = float(self.responses[f":SOUR{c}:CURR?"])
            else:
                self.measure_voltage[c] = 3.0
            return TraceWire._query(self, command)
        return super()._query(command)
@pytest.fixture
def rig(profile, tmp_path):
    def build(*, steps=STEPS, other_steps=None, samples_before_cutoff=21):
        first = power_profile(profile)
        first["execution"] = {"expected_serial": "TESTSERIAL", "maximum_voltage_v": 3.81,
                              "maximum_current_a": 0.4}
        first["charge"].update(maximum_duration_s=10, maximum_cv_duration_s=5)
        first["charge"]["termination"]["hold_time_s"] = 0.6
        first["discharge"]["maximum_duration_s"] = 10
        first["rest"].update(after_charge_s=0.4, after_discharge_s=0.4)
        first = to_profile(first, steps)
        second = deepcopy(first)
        second["instrument"]["channel"] = 2
        second["dut"]["specimen_id"] = "cell_two"
        if other_steps is not None:
            second["profile"]["steps"] = [{"duration_s": d, "value": v} for d, v in other_steps]
        docs = [first, second]
        plan = build_dual_constant_power_sweep_plan(docs)
        wire = ProfileWire(plan["resource"], docs, samples_before_cutoff)
        session = DualInstrumentSession(
            build_dual_plan([p["initial_preparation"]["configuration"] for p in plan["channels"]]),
            "TESTSERIAL", smu_factory=lambda *args, **kwargs: wire)
        session.connect()
        owner = SharedPowerSession(docs, session=session, clock=wire.clock.now, output_authorized=True)
        wire.owner = owner
        def run():
            return run_shared_power_sweep(owner, tmp_path / "acquisition", sleep=wire.clock.sleep,
                                          maximum_probe_current_a={1: 0.001, 2: 0.001},
                                          execute_requested=True)
        return docs, wire, owner, run, tmp_path / "acquisition"
    return build
def test_dual_profile_run_applies_every_step_including_zero_current(rig):
    _, wire, owner, run, root = rig()
    report = run()
    assert report["state"] == "acquisition_completed_unreplayed"
    assert [c["completed_stage_count"] for c in report["channels"]] == [6, 6]
    rows = journal(root)
    for channel in (1, 2):
        requested = [r["data"]["current_a"] for r in rows
                     if r["kind"] == "current_requested" and r["channel"] == channel]
        acknowledged = [r["data"]["readback_current_a"] for r in rows
                        if r["kind"] == "current_acknowledged" and r["channel"] == channel]
        assert requested[:4] == [-0.1, 0.0, -0.05, -0.1]
        assert acknowledged == requested
        decisions = [r["data"]["decision"] for r in rows
                     if r["kind"] == "power_decision" and r["channel"] == channel]
        assert {d["profile_step"] for d in decisions} == {0, 1, 2}
    assert report["owner_cleanup"]["outputs_reported_off"] and wire.close_count == 1 and owner.closed
def test_dual_profile_acquisition_replays_and_exports(rig, tmp_path):
    _, _, _, run, root = rig()
    run()
    replay = replay_shared_power_acquisition(root, collect_samples=True)
    assert replay["valid"] is True and replay["phase_count"] == 12
    profile_phases = [p for p in replay["phases"] if p["request"]["phase"]["type"] == "discharge_profile"]
    assert len(profile_phases) == 2 and all(len(p["points"]) >= 15 for p in profile_phases)
    result = export_shared_power_analysis(root, tmp_path / "analysis")
    assert result["state"] == "completed"
    assert validate_shared_power_analysis(tmp_path / "analysis")["valid"] is True
def test_dual_planner_requires_the_same_profile(rig):
    with pytest.raises(ValueError, match="same profile"):
        rig(other_steps=((2, 0.1), (1, 0.0)))
def test_dual_planner_rejects_mixing_cp_and_profile_documents(profile):
    cp = power_profile(profile)
    other = to_profile(power_profile(profile), STEPS)
    other["instrument"]["channel"] = 2
    other["dut"]["specimen_id"] = "other"
    with pytest.raises(ValueError, match="experiment type"):
        build_dual_constant_power_sweep_plan([cp, other])
def test_cli_dual_profile_execution_dispatches_to_the_dual_runner():
    args = ["--config", str(CONFIGS / "profile_current_trial_7_8_ch1.yaml"),
            "--config", str(CONFIGS / "profile_current_trial_5_6_ch2.yaml"), "--execute"]
    with mock.patch("experiments.power_dual_execution.run_configured_dual_power_sweep",
                    return_value=0) as launcher:
        assert smu.main(args) == 0
    assert launcher.call_count == 1
