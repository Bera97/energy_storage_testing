"""Hardware-free dual scheduling with actual CP decisions and synthetic CC/rest."""
from copy import deepcopy
from dataclasses import replace
import json
import pytest
from test_charge_current_sweep import profile
from test_constant_power_planning import power_profile
from experiments.constant_power import PowerFeedbackError, PowerTimeoutError
from experiments.power_dual_scheduler import run_dual_power_simulation
from experiments.power_dual_simulation import SimulatedDualPowerInstrument
from experiments.power_sweep_contract import PowerSweepError, build_sequence


@pytest.fixture
def documents(profile):
    first = power_profile(profile)
    first["experiment"]["cycles"] = 1
    first["sweep"].update(power_w=[0.4, 0.8], repeats=2)
    first["rest"].update(after_charge_s=0.1, after_discharge_s=0.1)
    second = deepcopy(first)
    second["instrument"]["channel"] = 2
    second["dut"]["specimen_id"] = "independent_second_specimen"
    second["sweep"].update(power_w=[0.4], repeats=1)
    return [first, second]


def run(documents, backend=None, **changes):
    backend = SimulatedDualPowerInstrument() if backend is None else backend
    arguments = dict(backend=backend, maximum_probe_current_a={1:0.001, 2:0.001})
    arguments.update(changes)
    return run_dual_power_simulation(documents, **arguments), backend


def started(report, channel, stage_id):
    return next(e["time_s"] for e in report["events"] if e["action"] == "stage_started" and e["channel"] == channel and e["stage_id"] == stage_id)


def test_complete_unequal_queues_match_canonical_order_and_keep_specimens_separate(documents):
    original = deepcopy(documents)
    report, backend = run(documents)
    assert report["state"] == "completed" and report["simulation_only"] is True
    assert report["hardware_execution_available"] is False and report["physical_qualification"] is False
    for document, channel in zip(documents, report["channels"]):
        requests = build_sequence(document)[1]
        assert [s["request"] for s in channel["stages"]] == requests
        assert channel["completed_stage_count"] == len(requests)
        assert channel["specimen_id"] == document["dut"]["specimen_id"]
        assert channel["configuration"] == document
        assert channel["interrupted_stage"] is None and channel["state"] == "completed"
    assert [c["completed_stage_count"] for c in report["channels"]] == [18, 6]
    assert backend.closed and not backend.connected and not any(backend.output_states.values())
    assert documents == original and json.loads(json.dumps(report, allow_nan=False)) == report


def test_initial_discharge_rest_barrier_is_once_per_channel(documents):
    report, _ = run(documents)
    barrier = report["initial_preparation_barrier_released_s"]
    assert barrier is not None
    for channel in report["channels"]:
        assert channel["stages"][1]["finished_s"] <= barrier
        assert channel["stages"][2]["started_s"] >= barrier
        assert sum(s["request"]["scope"] == "initial_preparation" for s in channel["stages"]) == 2
    assert len([e for e in report["events"] if e["action"] == "initial_preparation_barrier_released"]) == 1


def test_faster_channel_advances_while_peer_cp_is_still_active(documents):
    backend = SimulatedDualPowerInstrument(cutoff_after_samples={1:2, 2:10})
    report, _ = run(documents, backend)
    first, second = report["channels"]
    assert first["stages"][6]["started_s"] < second["stages"][4]["finished_s"]
    assert any(e["peer_output_on"] for e in report["events"] if e["action"] == "stop_channel" and e["channel"] == 1)
    assert started(report, 1, "stage_00002") <= started(report, 2, "stage_00002")


def test_completed_channel_does_not_stop_or_close_peer_session(documents):
    report, _ = run(documents)
    events = report["events"]
    assert sum(e["action"] == "open_simulation" for e in events) == 1
    assert sum(e["action"] == "configure_shared" for e in events) == 1
    assert sum(e["action"] == "close_simulation" for e in events) == 1
    assert sum(e["action"] == "shutdown_all" for e in events) == 2
    assert report["channels"][1]["stages"][-1]["finished_s"] < report["channels"][0]["stages"][-1]["finished_s"]
    assert report["final_stop_verified"] and report["final_output_states"] == [False,False]


def test_cp_commands_and_acknowledgements_are_owned_by_the_correct_power_condition(documents):
    report, _ = run(documents)
    for channel in report["channels"]:
        for stage in channel["stages"]:
            if stage["request"]["phase"]["type"] != "discharge_cp":
                continue
            assert stage["stop_reason"] == "voltage_min"
            assert stage["samples"][0]["current_a"] == 0
            assert stage["decisions"][0]["current_setpoint_a"] == pytest.approx(-stage["request"]["power_w"]/stage["samples"][0]["voltage_v"])
            for ack in stage["acknowledgements"]:
                assert ack["requested_current_a"] < 0 and ack["requested_current_a"] == ack["readback_current_a"]
                assert ack["acknowledged_s"] >= ack["requested_s"]


def test_virtual_latency_is_inside_control_and_feedback_time(documents):
    report, _ = run(documents, SimulatedDualPowerInstrument(call_latency_s=0.001))
    assert report["state"] == "completed"
    stage = report["channels"][0]["stages"][4]
    assert stage["decisions"][0]["host_time_s"] > stage["samples"][0]["acquired_at_s"]


@pytest.mark.parametrize("options", [{"poll_interval_s":0}, {"poll_interval_s":True}, {"maximum_scheduler_steps":True},
    {"maximum_probe_current_a":{1:0.001}}, {"maximum_probe_current_a":{1:1.0,2:0.001}}])
def test_invalid_preflight_does_not_claim_or_touch_backend(documents, options):
    backend = SimulatedDualPowerInstrument()
    with pytest.raises((ValueError, PowerSweepError)):
        run(documents, backend, **options)
    assert not backend.claimed and backend.events == []


def test_invalid_later_condition_in_peer_rejected_before_virtual_open(documents):
    documents[1]["sweep"]["power_w"].append(100)
    backend = SimulatedDualPowerInstrument()
    with pytest.raises(ValueError):
        run(documents, backend)
    assert backend.events == [] and not backend.claimed


def test_duck_typed_or_real_transport_is_rejected(documents):
    from test_power_smu_backend import make
    backend, smu, _, _ = make()
    backend.simulation_only = True
    with pytest.raises(PowerSweepError, match="SimulatedDualPowerInstrument"):
        run(documents, backend)
    assert smu.calls == []


def test_single_use_backend_cannot_resume_or_reconnect(documents):
    report, backend = run(documents)
    before = deepcopy(backend.events)
    with pytest.raises(PowerSweepError, match="fresh"):
        run(documents, backend)
    assert backend.events == before and report["state"] == "completed"


def test_measured_and_off_rest_are_channel_local_synthetic_endpoints(documents):
    documents[1]["rest"].update(mode="zero_current_logging", maximum_abs_current_a=0.001)
    report, _ = run(documents)
    for channel in report["channels"]:
        for stage in channel["stages"]:
            if stage["request"]["phase"]["type"] == "rest":
                assert stage["stop_reason"] == "rest_elapsed"
                assert stage["samples"] == []
    starts = [e for e in report["events"] if e["action"] == "stage_started" and e["type"] == "rest"]
    assert all(e["output_on"] is (e["channel"] == 2) for e in starts)


def test_probe_cutoff_does_not_require_a_power_command(documents):
    report, _ = run(documents, SimulatedDualPowerInstrument(cutoff_after_samples={1:1,2:1}))
    for channel in report["channels"]:
        for stage in channel["stages"]:
            if stage["request"]["phase"]["type"] == "discharge_cp":
                assert len(stage["samples"]) == 1 and stage["acknowledgements"] == []
                assert stage["decisions"][0]["action"] == "stop"


def test_fault_in_one_cp_stops_both_and_never_completes_top_level(documents):
    class Fault(SimulatedDualPowerInstrument):
        def read_power(self, channel):
            frame = super().read_power(channel)
            if channel == 2:
                frame["sample"] = replace(frame["sample"], has_fault=True)
            return frame
    backend = Fault()
    with pytest.raises(PowerFeedbackError) as caught:
        run(documents, backend)
    report = caught.value.dual_power_simulation_report
    assert report["state"] == "raised" and report["final_stop_verified"]
    assert backend.closed and not any(backend.output_states.values())
    assert report["channels"][1]["completed_stage_count"] == 4


def test_slow_peer_callback_is_detected_before_any_later_setpoint(documents):
    class Slow(SimulatedDualPowerInstrument):
        def read_power(self, channel):
            frame = super().read_power(channel)
            if channel == 2:
                self.clock.sleep(0.6)
                self._event("injected_delay", channel)
            return frame
    backend = Slow()
    with pytest.raises(PowerTimeoutError):
        run(documents, backend)
    index = next(i for i,e in enumerate(backend.events) if e["action"] == "injected_delay")
    assert not any(e["action"] in ("set_current", "activate_stage", "prepare_stage") for e in backend.events[index+1:])
    assert backend.closed and not any(backend.output_states.values())


@pytest.mark.parametrize("operation", ["prepare_stage", "read_power", "poll_stage", "stop_channel", "finalize_stage"])
def test_channel_operation_that_changes_peer_output_is_rejected(documents, operation):
    class BadScope(SimulatedDualPowerInstrument):
        pass
    original = getattr(BadScope, operation)
    def wrong(self, channel, *args):
        result = original(self, channel, *args)
        if self.output_states[3-channel]:
            self.output_states[3-channel] = False
        return result
    setattr(BadScope, operation, wrong)
    backend = BadScope()
    with pytest.raises(PowerSweepError, match="peer output"):
        run(documents, backend)
    assert backend.closed and not any(backend.output_states.values())


def test_cross_channel_cp_sample_is_rejected(documents):
    class Crossed(SimulatedDualPowerInstrument):
        def read_power(self, channel):
            frame = super().read_power(channel);frame["channel"] = 3-channel
            return frame
    backend = Crossed()
    with pytest.raises(PowerSweepError, match="cross-channel"):
        run(documents, backend)
    assert backend.closed


def test_cross_channel_generic_endpoint_is_rejected(documents):
    class Crossed(SimulatedDualPowerInstrument):
        def poll_stage(self, channel):
            result = super().poll_stage(channel)
            if result is not None:
                result["channel"] = 3-channel
            return result
    with pytest.raises(PowerSweepError, match="cross-channel"):
        run(documents, Crossed())



@pytest.mark.parametrize("kind", ["resource", "specimen", "channel", "zero_rest"])
def test_incompatible_profiles_do_not_touch_backend(documents, kind):
    if kind == "resource":
        documents[1]["instrument"]["visa_address"] = "OTHER::INSTR"
    elif kind == "specimen":
        documents[1]["dut"]["specimen_id"] = documents[0]["dut"]["specimen_id"]
    elif kind == "channel":
        documents[1]["instrument"]["channel"] = 1
    else:
        documents[1]["rest"]["after_charge_s"] = 0
    backend = SimulatedDualPowerInstrument()
    with pytest.raises(ValueError):
        run(documents, backend)
    assert not backend.claimed and backend.events == []


@pytest.mark.parametrize("fault", ["polarity", "compliance", "source_mode", "stale", "voltage"])
def test_invalid_cp_feedback_fails_both(documents, fault):
    class Invalid(SimulatedDualPowerInstrument):
        def read_power(self, channel):
            frame = super().read_power(channel)
            changes = {"polarity":{"current_a":0.1}, "compliance":{"in_compliance":True},
                       "source_mode":{"current_source":False}, "stale":{"acquired_at_s":frame["sample"].acquired_at_s-0.6},
                       "voltage":{"voltage_v":100.0}}
            frame["sample"] = replace(frame["sample"], **changes[fault])
            return frame
    backend = Invalid()
    with pytest.raises(PowerFeedbackError):
        run(documents, backend)
    assert backend.closed and backend.output_states == {1:False,2:False}


def test_mismatched_setpoint_readback_is_not_acknowledged(documents):
    class WrongCurrent(SimulatedDualPowerInstrument):
        def read_current(self, channel):
            return super().read_current(channel)*0.5
    backend = WrongCurrent()
    with pytest.raises(PowerFeedbackError, match="readback mismatch") as caught:
        run(documents, backend)
    report = caught.value.dual_power_simulation_report
    assert any(c["interrupted_stage"] and not c["interrupted_stage"]["acknowledgements"] for c in report["channels"])
    assert backend.closed


def test_keyboard_interrupt_preserved_through_all_independent_cleanup_attempts(documents):
    original = KeyboardInterrupt("primary virtual acquisition interrupt")
    calls = []
    class BrokenCleanup(SimulatedDualPowerInstrument):
        broken = False
        def read_power(self, channel):
            self.broken = True
            raise original
        def shutdown_all(self):
            if self.broken:
                calls.append("shutdown_all")
                raise OSError("secondary global shutdown")
            return super().shutdown_all()
        def stop_channel(self, channel):
            if self.broken:
                calls.append(f"stop_{channel}")
                if channel == 1:
                    raise OSError("secondary selected stop")
            return super().stop_channel(channel)
        def output_state(self, channel):
            if self.broken:
                calls.append(f"read_{channel}")
            return super().output_state(channel)
        def close_simulation(self):
            calls.append("close")
            return super().close_simulation()
    backend = BrokenCleanup()
    with pytest.raises(KeyboardInterrupt) as caught:
        run(documents, backend)
    assert caught.value is original
    assert calls == ["shutdown_all", "stop_1", "read_1", "stop_2", "read_2", "close"]
    report = original.dual_power_simulation_report
    assert report["state"] == "raised" and report["final_stop_verified"] is False
    assert report["session_close_returned"] is True and len(report["cleanup_errors"]) >= 2
    assert report["final_output_states"] == [True,False]
    assert len(original.__notes__) >= 2


def test_final_cleanup_failure_prevents_success_even_after_all_stages(documents):
    class BadClose(SimulatedDualPowerInstrument):
        def close_simulation(self):
            raise OSError("virtual close failed")
    with pytest.raises(OSError, match="close failed") as caught:
        run(documents, BadClose())
    report = caught.value.dual_power_simulation_report
    assert report["state"] == "raised" and report["session_close_returned"] is False
    assert report["final_stop_verified"] is True
    assert all(c["state"] == "completed" for c in report["channels"])


def test_frozen_virtual_sleep_is_terminal_and_cleans_up(documents):
    backend = SimulatedDualPowerInstrument()
    backend.clock.sleep = lambda duration: None
    with pytest.raises(PowerSweepError, match="did not advance"):
        run(documents, backend)
    assert backend.closed and backend.output_states == {1:False,2:False}


def test_backwards_clock_still_allows_cleanup(documents):
    class Backwards(SimulatedDualPowerInstrument):
        def read_power(self, channel):
            frame = super().read_power(channel)
            self.clock.time -= 0.01
            return frame
    backend = Backwards()
    with pytest.raises(PowerSweepError, match="backwards"):
        run(documents, backend)
    assert backend.closed and backend.output_states == {1:False,2:False}


def test_step_budget_is_terminal_and_does_not_allow_resume(documents):
    backend = SimulatedDualPowerInstrument()
    with pytest.raises(RuntimeError, match="step budget"):
        run(documents, backend, maximum_scheduler_steps=1)
    assert backend.closed and backend.claimed
    with pytest.raises(PowerSweepError, match="fresh"):
        run(documents, backend)


def test_channel_specific_cp_cadences_and_reversed_profile_order(documents):
    documents[1]["power_control"].update(update_interval_s=0.4, maximum_feedback_age_s=0.9)
    report, _ = run(list(reversed(documents)))
    assert [c["channel"] for c in report["channels"]] == [1,2]
    for channel in report["channels"]:
        interval = channel["configuration"]["power_control"]["update_interval_s"]
        for stage in channel["stages"]:
            stamps = [s["acquired_at_s"] for s in stage["samples"]]
            assert all(b-a >= interval-1e-12 for a,b in zip(stamps, stamps[1:]))


def test_simulation_does_not_create_files_or_reach_hardware_factories(documents, tmp_path, monkeypatch):
    import experiments.run_live as single
    import experiments.dual_instrument_io as shared
    def forbidden(*args, **kwargs):
        pytest.fail("Virtual scheduler reached an instrument factory")
    monkeypatch.setattr(single, "_new_smu", forbidden)
    monkeypatch.setattr(shared, "_new_smu", forbidden)
    run(documents)
    assert list(tmp_path.iterdir()) == []


def test_public_dual_cp_execution_stays_disabled(documents, tmp_path, capsys):
    import smu
    import yaml
    paths = []
    for i, document in enumerate(documents):
        path = tmp_path / f"power_{i}.yaml"
        path.write_text(yaml.safe_dump(document));paths.append(str(path))
    assert smu.main(["--config",paths[0],"--config",paths[1],"--execute"]) == 1
    assert "planning only" in capsys.readouterr().err.lower()
    assert not (tmp_path / "never_create_runs").exists()



def test_persistent_bad_tracking_uses_actual_controller_watchdog(documents):
    from experiments.constant_power import PowerTrackingError
    for doc in documents:
        doc["power_control"].update(startup_grace_s=0.0, maximum_tracking_error_s=0.3)
    class Drift(SimulatedDualPowerInstrument):
        def read_power(self, channel):
            frame = super().read_power(channel)
            frame["sample"] = replace(frame["sample"], current_a=0.0)
            return frame
    backend = Drift(cutoff_after_samples={1:20,2:20})
    with pytest.raises(PowerTrackingError):
        run(documents, backend)
    assert backend.closed and backend.output_states == {1:False,2:False}


def test_synthetic_charge_deadline_is_enforced(documents):
    for doc in documents:
        doc["charge"].update(maximum_duration_s=1.0, maximum_cv_duration_s=0.9)
        doc["charge"]["termination"]["hold_time_s"] = 0.6
    backend = SimulatedDualPowerInstrument(active_duration_s={1:2,2:2})
    with pytest.raises(TimeoutError, match="stage deadline"):
        run(documents, backend)
    assert backend.closed and backend.output_states == {1:False,2:False}


def test_failed_finalization_blocks_next_stage_and_barrier(documents):
    class NoReceipt(SimulatedDualPowerInstrument):
        def finalize_stage(self, channel, stage):
            super().finalize_stage(channel, stage)
            return False
    backend = NoReceipt()
    with pytest.raises(PowerSweepError, match="finalization failed") as caught:
        run(documents, backend)
    report = caught.value.dual_power_simulation_report
    assert report["initial_preparation_barrier_released_s"] is None
    assert not any(e["action"] == "stage_started" and e["stage_id"] == "stage_00001" for e in backend.events)
    assert backend.closed


def test_backend_must_be_run_on_its_owning_thread(documents):
    from concurrent.futures import ThreadPoolExecutor
    backend = SimulatedDualPowerInstrument()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run, documents, backend)
        with pytest.raises(PowerSweepError, match="one controlling thread"):
            future.result()
    assert backend.events == [] and not backend.claimed



def test_returning_from_close_without_closing_model_cannot_complete(documents):
    class NoClose(SimulatedDualPowerInstrument):
        def close_simulation(self):
            return None
    with pytest.raises(PowerSweepError, match="model closed") as caught:
        run(documents, NoClose())
    report = caught.value.dual_power_simulation_report
    assert report["state"] == "raised" and report["session_close_returned"] is False
