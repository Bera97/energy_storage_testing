"""SIMULATION-ONLY independent dual CP scheduling; no connection/file/CLI API.

Actual CP decision controllers, synthetic CC/rest endpoints, one virtual session.
This is not a physical shared-session adapter or a native recorded dual sweep.
Cooperative checks detect overrun AFTER a callback returns; they cannot preempt it.
"""
from dataclasses import asdict
from threading import get_ident
from experiments.constant_power import ConstantPowerController, PowerLimits, PowerSample, _number
from experiments.power_dual_simulation import SimulatedDualPowerInstrument, DualPowerVirtualClock, synthetic_endpoint
from experiments.power_planning import build_dual_constant_power_sweep_plan
from experiments.power_sweep_contract import MAXIMUM_RECORDS, build_sequence, require, same, snapshot


def run_dual_power_simulation(documents, *, backend, maximum_probe_current_a,
                              poll_interval_s=0.05, maximum_scheduler_steps=1000000):
    """One-use virtual owner, initial two-stage barrier, independent queues, fail both.

    No arbitrary backend protocol, physical-mode flag, output authorization, factory,
    native completion marker or recording directory is accepted. Injected subclasses
    are trusted tests, NOT a sandbox. Preflight rejection leaves the backend unused.
    """
    require(isinstance(backend, SimulatedDualPowerInstrument) and backend.simulation_only is True,
            "Only an explicit SimulatedDualPowerInstrument is accepted.")
    require(backend.owner == get_ident(), "The virtual session must have one controlling thread.")
    require(isinstance(backend.clock, DualPowerVirtualClock), "An explicit virtual clock is required.")
    require(all(getattr(backend, k) is False for k in ("claimed", "connected", "closed", "configured"))
            and backend.active == {} and backend.events == []
            and set(backend.output_states) == {1, 2} and all(v is False for v in backend.output_states.values()),
            "A fresh, idle virtual backend is required.")
    interval = _number(poll_interval_s, "poll interval")
    require(type(maximum_scheduler_steps) is int and maximum_scheduler_steps >= 1, "Positive integer scheduling budget required.")
    require(isinstance(maximum_probe_current_a, dict) and set(maximum_probe_current_a) == {1, 2}
            and all(type(k) is int for k in maximum_probe_current_a), "Explicit probe ceilings for integer channels 1 and 2 required.")
    documents = snapshot(documents)
    plan = build_dual_constant_power_sweep_plan(documents, maximum_records=MAXIMUM_RECORDS)
    states = {}
    probes = {}
    for preview in plan["channels"]:
        channel = preview["channel"]
        checked, requests = build_sequence(preview["configuration"])
        same(checked, preview, "Dual child differs from canonical single-channel plan.")
        probes[channel] = _number(maximum_probe_current_a[channel], "probe ceiling")
        require(probes[channel] <= preview["configuration"]["power_control"]["maximum_current_a"], "Probe exceeds channel current ceiling.")
        states[channel] = {"channel": channel, "specimen_id": preview["specimen_id"], "configuration": preview["configuration"],
                           "requests": requests, "position": 0, "record": None, "controller": None,
                           "hard_deadline_s": None, "due_s": 0.0, "stages": []}
    _number(backend.clock(), "initial virtual time", zero=True)
    report = {"dual_power_simulation_schema_version": 1, "simulation_only": True,
              "hardware_execution_available": False, "physical_qualification": False,
              "joint_timing_qualified": False, "state": "running", "channels": [],
              "initial_preparation_barrier_released_s": None, "startup_stop_verified": False,
              "final_stop_verified": False, "final_output_states": [None, None], "session_close_returned": False,
              "cleanup_errors": [], "error": None,
              "limitations": ["SIMULATED session and samples; not a native recorded power sweep.",
                  "Real CP decision controllers; CC/rest completion is synthetic, without native monitor/recording replay.",
                  "Callbacks are cooperative, not preemptible; virtual timing does not qualify shared hardware latency.",
                  "No physical backend, hardware connection, files, native completion markers or public dispatch."]}
    primary, last_time, last_served = None, None, 2
    backend.claimed = True

    def now():
        nonlocal last_time
        time = _number(backend.clock(), "virtual time", zero=True)
        require(last_time is None or time >= last_time, "Virtual clock moved backwards.")
        last_time = time
        return time

    def guard():
        time = now()
        require(backend.simulation_only is True, "Simulation-only identity changed during scheduling.")
        for state in states.values():
            if state["record"] is None:
                continue
            deadline = state["hard_deadline_s"]
            if deadline is not None and time >= deadline:
                raise TimeoutError(f"Channel {state['channel']} stage deadline reached.")
            controller = state["controller"]
            if controller is not None and controller.state != "completed":
                controller.watchdog(now_s=time)
        return time

    def call(operation, *arguments):
        guard()
        result = operation(*arguments)
        guard()
        return result

    def channel_call(operation, channel, *arguments):
        peer = 3-channel
        before = call(backend.output_state, peer)
        require(type(before) is bool, "Malformed peer synthetic output state.")
        result = call(operation, channel, *arguments)
        require(call(backend.output_state, peer) is before, "Channel-local operation changed the peer output.")
        return result

    def release_barrier():
        if report["initial_preparation_barrier_released_s"] is None and all(s["position"] >= 2 for s in states.values()):
            report["initial_preparation_barrier_released_s"] = now()
            backend._event("initial_preparation_barrier_released")

    def start(state):
        channel = state["channel"]
        request = state["requests"][state["position"]]
        phase = request["phase"]
        time = now()
        state["record"] = {"request": snapshot(request), "state": "running", "started_s": time, "finished_s": None,
                           "stop_reason": None, "stop_verified": False, "samples": [], "decisions": [], "acknowledgements": []}
        state["hard_deadline_s"] = None if phase["type"] == "rest" else _number(time+phase["maximum_duration_s"], "stage deadline")
        setup = channel_call(backend.prepare_stage, channel, snapshot(request))
        require(isinstance(setup, dict) and type(setup.get("channel")) is int and setup["channel"] == channel and setup.get("stage_id") == request["stage_id"]
                and setup.get("simulation_only") is True and setup.get("selected_output_off") is True,
                "Synthetic preparation identity/output state differs.")
        if phase["type"] == "discharge_cp":
            controller = ConstantPowerController(PowerLimits(**phase["controller_limits"]))
            state["controller"] = controller
            controller.arm_zero_current_probe(now_s=now(), outputs_verified_off=setup["selected_output_off"],
                current_source_verified=setup.get("current_source"), zero_current_setpoint_a=setup.get("current_setpoint_a"),
                maximum_probe_current_a=probes[channel])
        on = channel_call(backend.activate_stage, channel)
        expected_on = phase["type"] != "rest" or phase.get("rest_mode") == "zero_current_logging"
        require(type(on) is bool and on is expected_on, "Synthetic activation readback differs.")
        if state["controller"] is not None:
            state["controller"].confirm_zero_current_probe_enabled(now_s=now(), output_reported_on=on)
        state["rest_started_s"] = now() if phase["type"] == "rest" else None
        state["due_s"] = _number(now()+phase["duration_s"], "rest completion time", zero=True) if phase["type"] == "rest" else now()

    def finish(state, reason):
        channel, record = state["channel"], state["record"]
        require(channel_call(backend.stop_channel, channel) is True, "Selected synthetic channel stop failed.")
        require(call(backend.output_state, channel) is False, "Selected synthetic channel did not report OFF.")
        record.update(stop_reason=reason, stop_verified=True)
        require(channel_call(backend.finalize_stage, channel, snapshot(record)) is True, "Synthetic stage finalization failed.")
        record.update(state="completed", finished_s=now())
        state["stages"].append(record)
        state.update(record=None, controller=None, hard_deadline_s=None, position=state["position"]+1, due_s=now())
        release_barrier()

    def advance(state):
        channel, record = state["channel"], state["record"]
        phase = record["request"]["phase"]
        controller = state["controller"]
        if controller is None:
            result = channel_call(backend.poll_stage, channel)
            if result is None:
                state["due_s"] = _number(now()+interval, "next synthetic poll")
                return
            require(isinstance(result, dict) and type(result.get("channel")) is int and result["channel"] == channel and result.get("stage_id") == record["request"]["stage_id"]
                    and result.get("simulation_only") is True and result.get("state") == "completed"
                    and result.get("stop_reason") == synthetic_endpoint(phase), "Invalid or cross-channel synthetic endpoint.")
            if phase["type"] == "rest":
                require(now()-state["rest_started_s"] >= phase["duration_s"], "Synthetic rest completed early.")
            finish(state, result["stop_reason"])
            return
        require(len(record["samples"]) < MAXIMUM_RECORDS, "Synthetic CP sample budget exhausted.")
        frame = channel_call(backend.read_power, channel)
        require(isinstance(frame, dict) and type(frame.get("channel")) is int and frame["channel"] == channel
                and frame.get("stage_id") == record["request"]["stage_id"] and frame.get("simulation_only") is True,
                "Invalid or cross-channel synthetic CP sample.")
        sample = frame.get("sample")
        require(isinstance(sample, PowerSample), "Synthetic CP must return a PowerSample.")
        record["samples"].append(snapshot(asdict(sample)))
        decision = (controller.start_from_zero_current_probe(now_s=now(), sample=sample) if controller.state == "probing"
                    else controller.update(now_s=now(), sample=sample))
        record["decisions"].append(decision.to_dict())
        if decision.action == "stop":
            finish(state, decision.stop_reason)
            return
        if decision.action == "set_current":
            requested = now()
            channel_call(backend.set_current, channel, decision.current_setpoint_a)
            readback = channel_call(backend.read_current, channel)
            acknowledged = now()
            controller.acknowledge_setpoint(now_s=acknowledged, readback_current_a=readback)
            record["acknowledgements"].append({"requested_s": requested, "acknowledged_s": acknowledged,
                "requested_current_a": decision.current_setpoint_a, "readback_current_a": readback})
        state["due_s"] = _number(max(sample.acquired_at_s+controller.limits.update_interval_s, now()), "next CP sample time")

    def deadline(state):
        controller = state["controller"]
        if controller is None:
            return state["hard_deadline_s"]
        reference = controller.started_s if controller.last_sample_s is None else controller.last_sample_s
        candidates = [state["hard_deadline_s"], reference+controller.limits.maximum_feedback_age_s]
        if controller.bad_tracking_since_s is not None:
            candidates.append(controller.bad_tracking_since_s+controller.limits.maximum_tracking_error_s)
        return min(candidates)

    try:
        call(backend.open_simulation, snapshot(plan))
        require(call(backend.shutdown_all) is True, "Initial synthetic stop-both failed.")
        for channel in (1, 2):
            require(call(backend.output_state, channel) is False, "Initial synthetic output not OFF.")
        report["startup_stop_verified"] = True
        call(backend.configure_shared)
        for _ in range(maximum_scheduler_steps):
            time = guard()
            release_barrier()
            if all(s["position"] == len(s["requests"]) for s in states.values()):
                break
            ready, wakes = [], []
            for channel, state in states.items():
                if state["position"] == len(state["requests"]):
                    continue
                if state["record"] is None and state["position"] == 2 and report["initial_preparation_barrier_released_s"] is None:
                    continue
                due = state["due_s"]
                bound = deadline(state) if state["record"] is not None else None
                wakes.append(due)
                if bound is not None:
                    wakes.append(bound)
                if due <= time:
                    priority = 0 if state["controller"] is not None else 1 if state["record"] is not None else 2
                    ready.append((priority, bound if priority == 0 else due, channel == last_served, channel))
            if ready:
                channel = min(ready)[-1]
                state = states[channel]
                last_served = channel
                start(state) if state["record"] is None else advance(state)
                if all(s["position"] == len(s["requests"]) for s in states.values()):
                    break
                continue
            require(bool(wakes), "No runnable virtual stage remains.")
            duration = min(wakes)-time
            require(duration > 0, "Virtual scheduler cannot make progress.")
            backend.clock.sleep(duration)
            require(now() > time, "Virtual sleep did not advance the clock.")
        else:
            raise RuntimeError("Virtual scheduler step budget exhausted.")
    except BaseException as error:
        primary = error
    finally:
        def cleanup(label, operation, *arguments):
            nonlocal primary
            result = None
            try:
                result = operation(*arguments)
                if label == "close_simulation":
                    require(backend.closed is True and backend.connected is False, "Virtual close did not leave the model closed.")
                else:
                    require(result is (False if label.startswith("output_state") else True), "Synthetic cleanup readback/acknowledgement failed.")
                return True, result
            except BaseException as error:
                report["cleanup_errors"].append({"operation": label, "type": type(error).__name__, "message": str(error)})
                if primary is None:
                    primary = error
                else:
                    primary.add_note(f"Dual simulation cleanup {label}: {type(error).__name__}: {error}")
                return False, result if type(result) is bool else None
        success, _ = cleanup("shutdown_all", backend.shutdown_all)
        stops = []
        for channel in (1, 2):
            stopped, _ = cleanup(f"stop_channel_{channel}", backend.stop_channel, channel)
            read, value = cleanup(f"output_state_{channel}", backend.output_state, channel)
            report["final_output_states"][channel-1] = value
            stops.append(stopped and read)
        report["final_stop_verified"] = success and all(stops)
        report["session_close_returned"], _ = cleanup("close_simulation", backend.close_simulation)
    for state in states.values():
        done = state["position"] == len(state["requests"])
        report["channels"].append({"channel": state["channel"], "specimen_id": state["specimen_id"],
            "configuration": state["configuration"], "state": "completed" if done else "aborted",
            "completed_stage_count": state["position"], "stages": state["stages"], "interrupted_stage": state["record"],
            "next_stage_id": None if done else state["requests"][state["position"]]["stage_id"]})
    report["events"] = snapshot(backend.events)
    report["state"] = "raised" if primary is not None else "completed"
    if primary is not None:
        report["error"] = {"type": type(primary).__name__, "message": str(primary)}
        try:
            primary.dual_power_simulation_report = snapshot(report)
        except BaseException as secondary:
            primary.add_note(f"Dual simulation report attachment: {type(secondary).__name__}: {secondary}")
        raise primary
    return snapshot(report)
