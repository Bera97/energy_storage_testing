"""Offline tests of independent progression and stop-both failures."""
from copy import deepcopy
import pytest
import yaml
import smu
import test_dual_preconditioning as planning_tests
from experiments.dual_preconditioning import build_dual_plan
from experiments.dual_coordinator import (
    SimulatedInstrument,
    VirtualClock,
    run_simulation,
)
pair = planning_tests.pair
@pytest.fixture
def plan(pair):
    for document in pair:
        document["experiment"]["cycles"] = 1
        document["acquisition"]["interval_s"] = 0.1
        document["acquisition"]["maximum_sample_gap_s"] = 0.15
        document["charge"]["termination"]["hold_time_s"] = 0.2
        document["charge"]["maximum_duration_s"] = 3.0
        document["charge"]["maximum_cv_duration_s"] = 2.0
        document["discharge"]["maximum_duration_s"] = 3.0
        document["rest"]["after_charge_s"] = 0.2
        document["rest"]["after_discharge_s"] = 0.2
    return build_dual_plan(pair)
def rig():
    clock = VirtualClock()
    backend = SimulatedInstrument(clock, phase_duration_s=0.1)
    return clock, backend
def execute(plan, clock, backend):
    return run_simulation(
        plan,
        backend,
        clock=clock,
        sleep=clock.sleep,
    )
def test_both_channels_complete_and_input_unchanged(plan):
    before = deepcopy(plan)
    clock, backend = rig()
    report = execute(plan, clock, backend)
    assert report["state"] == "completed"
    assert report["simulation_only"] is True
    assert report["hardware_execution_enabled"] is False
    assert report["final_stop_verified"] is True
    assert report["close_succeeded"] is True
    assert plan == before
    for channel in report["channels"]:
        assert channel["state"] == "completed"
        assert len(channel["outcomes"]) == 4
        assert [item["stop_reason"] for item in channel["outcomes"]] == [
            "cv_current_termination", "rest_elapsed",
            "voltage_min", "rest_elapsed",
        ]
    actions = [item["action"] for item in backend.events]
    assert actions.count("connect") == 1
    assert actions.count("configure_shared") == 1
    assert actions.count("shutdown_all") == 2
    assert actions[-1] == "close"
    assert backend.active == {}
    assert backend.connected is False
def test_normal_channel_stop_leaves_other_channel_active(plan):
    clock, backend = rig()
    execute(plan, clock, backend)
    assert any(
        event["action"] == "stop_channel"
        and event["channel"] == 1
        and 2 in event["other_active_channels"]
        for event in backend.events
    )
def test_rest_durations_are_respected(plan):
    clock, backend = rig()
    report = execute(plan, clock, backend)
    for channel in report["channels"]:
        for outcome in channel["outcomes"]:
            if outcome["type"] == "rest":
                assert (
                    outcome["finished_s"] - outcome["started_s"]
                    >= 0.2 - 1e-12
                )
@pytest.mark.parametrize("channel", [1, 2])
def test_phase_fault_stops_both_and_closes(plan, channel):
    clock, backend = rig()
    backend.fail_channel = channel
    with pytest.raises(RuntimeError, match="Synthetic") as caught:
        execute(plan, clock, backend)
    report = caught.value.dual_run_report
    assert report["state"] == "raised"
    assert report["final_stop_verified"] is True
    assert report["close_succeeded"] is True
    assert backend.active == {}
    assert backend.connected is False
def test_timeout_is_not_reported_as_completion(plan):
    clock, backend = rig()
    backend.phase_duration_s = 10.0
    with pytest.raises(TimeoutError) as caught:
        execute(plan, clock, backend)
    report = caught.value.dual_run_report
    assert report["state"] == "raised"
    assert report["final_stop_verified"] is True
    assert not any(item["outcomes"] for item in report["channels"])
    assert backend.active == {}
def test_late_poll_result_is_rejected(plan, monkeypatch):
    clock, backend = rig()
    original = backend.poll_phase
    def slow_poll(channel):
        clock.now += 10.0
        return original(channel)
    monkeypatch.setattr(backend, "poll_phase", slow_poll)
    with pytest.raises(TimeoutError, match="during polling"):
        execute(plan, clock, backend)
    assert backend.finalized == []
    assert backend.active == {}
def test_keyboard_interrupt_still_stops_both(plan, monkeypatch):
    clock, backend = rig()
    def interrupt(channel):
        raise KeyboardInterrupt("synthetic interruption")
    monkeypatch.setattr(backend, "poll_phase", interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        execute(plan, clock, backend)
    assert caught.value.dual_run_report["final_stop_verified"] is True
    assert backend.active == {}
    assert backend.connected is False
def test_unverified_channel_stop_aborts_run(plan, monkeypatch):
    clock, backend = rig()
    monkeypatch.setattr(backend, "stop_channel", lambda channel: False)
    with pytest.raises(RuntimeError, match="stop was not verified"):
        execute(plan, clock, backend)
    assert backend.active == {}
    assert backend.connected is False
def test_wrong_endpoint_is_rejected(plan, monkeypatch):
    clock, backend = rig()
    monkeypatch.setattr(
        backend, "poll_phase",
        lambda channel: {"state": "completed", "stop_reason": "wrong"},
    )
    with pytest.raises(RuntimeError, match="invalid endpoint"):
        execute(plan, clock, backend)
    assert backend.finalized == []
    assert backend.active == {}
def test_finalization_failure_stops_both(plan, monkeypatch):
    clock, backend = rig()
    def fail(*args):
        raise OSError("synthetic finalization failure")
    monkeypatch.setattr(backend, "finalize_phase", fail)
    with pytest.raises(OSError, match="finalization") as caught:
        execute(plan, clock, backend)
    assert caught.value.dual_run_report["state"] == "raised"
    assert backend.active == {}
    assert backend.connected is False
def test_cleanup_failure_does_not_replace_primary(plan, monkeypatch):
    clock, backend = rig()
    backend.fail_channel = 1
    original = backend.shutdown_all
    calls = 0
    def shutdown():
        nonlocal calls
        calls += 1
        result = original()
        if calls == 2:
            raise OSError("synthetic cleanup failure")
        return result
    monkeypatch.setattr(backend, "shutdown_all", shutdown)
    with pytest.raises(RuntimeError, match="Synthetic") as caught:
        execute(plan, clock, backend)
    report = caught.value.dual_run_report
    assert report["final_stop_verified"] is False
    assert report["cleanup_errors"][0]["type"] == "OSError"
    assert report["close_succeeded"] is True
def test_close_failure_prevents_overall_success(plan, monkeypatch):
    clock, backend = rig()
    def fail_close():
        raise OSError("synthetic close failure")
    monkeypatch.setattr(backend, "close", fail_close)
    with pytest.raises(OSError, match="close failure") as caught:
        execute(plan, clock, backend)
    report = caught.value.dual_run_report
    assert report["state"] == "raised"
    assert report["close_succeeded"] is False
def test_changed_plan_is_rejected_before_connect(plan):
    clock, backend = rig()
    plan["channels"][0]["acquisition_plan"]["phases"][0][
        "maximum_duration_s"
    ] = 99.0
    with pytest.raises(ValueError, match="freshly validated"):
        execute(plan, clock, backend)
    assert backend.events == []
def test_non_simulator_backend_is_rejected(plan):
    clock = VirtualClock()
    with pytest.raises(TypeError, match="no live backend"):
        execute(plan, clock, object())
def test_public_cli_success(pair, tmp_path, capsys):
    arguments = ["dual-simulate", "--phase-duration-s", "0.1"]
    for index, document in enumerate(pair):
        path = tmp_path / f"profile_{index}.yaml"
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
        arguments += ["--config", str(path)]
    assert smu.main(arguments) == 0
    assert '"simulation_only": true' in capsys.readouterr().out
def test_public_cli_fault_returns_failure(pair, tmp_path, capsys):
    arguments = ["dual-simulate", "--fail-channel", "2"]
    for index, document in enumerate(pair):
        path = tmp_path / f"profile_{index}.yaml"
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
        arguments += ["--config", str(path)]
    assert smu.main(arguments) == 1
    assert '"state": "raised"' in capsys.readouterr().err
