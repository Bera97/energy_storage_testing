"""INTERNAL PHYSICAL-CAPABLE cooperative dual CP/native acquisition coordinator.

Explicit fresh authorized owner, already-connected session, clock and sleep only.
No factory, CLI, hardware qualification, retry/resume or qualified completion marker.
Acquisition diagnostics/journal are provisional: whole-sweep replay is NOT provided.
Callbacks are not preemptible. Native output/clock/format ownership remains shared.
"""
from dataclasses import asdict
import json
from pathlib import Path
from experiments.constant_power import _number
from experiments.power_adapter import PowerFrame
from experiments.power_planning import build_dual_constant_power_sweep_plan
from experiments.power_shared_transport import SharedPowerSession
from experiments.power_sweep_contract import (
    CONTROLLED_PHASES, MAXIMUM_RECORDS, build_sequence, control_section, phase_controller, phase_limits,
    require, same, snapshot, tree_files, write_json,
)

MANIFEST = "dual_power_acquisition.json"


def run_shared_power_sweep(owner, directory, *, sleep, maximum_probe_current_a,
                           execute_requested=False, poll_interval_s=0.05, maximum_scheduler_steps=1000000):
    """Acquire canonical native phases and controlled CP with one outer owner.

    Rejection before owner entry leaves the connected session caller-owned.
    A returned acquisition_completed_unreplayed report is NOT replay-qualified
    experimental success, scientific validation, or permission for public DUT use.
    """
    require(isinstance(owner, SharedPowerSession), "Explicit SharedPowerSession required.")
    owner._thread()
    require(execute_requested is True and owner.output_authorized is True, "Explicit execution/output authorization required.")
    require(all(getattr(owner,k) is False for k in ("claimed","ready","closed","failed")) and owner._channels == {}, "Fresh unclaimed owner required.")
    require(callable(sleep), "Explicit sleep callable required.")
    interval = _number(poll_interval_s,"poll interval")
    require(type(maximum_scheduler_steps) is int and maximum_scheduler_steps > 0, "Positive integer scheduler budget required.")
    require(isinstance(maximum_probe_current_a,dict) and set(maximum_probe_current_a)=={1,2}
            and all(type(c) is int for c in maximum_probe_current_a), "Probe ceilings for integer channels 1 and 2 required.")
    plan = build_dual_constant_power_sweep_plan([owner._profiles[c]["configuration"] for c in (1,2)])
    same(owner.plan,plan,"Owner plan changed before coordination.")
    same(owner.session.plan,owner._base_plan,"Base session plan changed before coordination.")
    probes = {c:_number(maximum_probe_current_a[c],"probe ceiling") for c in (1,2)}
    states = {}
    for preview in plan["channels"]:
        c = preview["channel"]
        require(probes[c] <= control_section(preview["configuration"])["maximum_current_a"], "Probe exceeds phase current ceiling.")
        child,requests = build_sequence(preview["configuration"])
        same(child,preview,"Canonical child plan differs.")
        states[c] = dict(channel=c,specimen_id=preview["specimen_id"],requests=requests,position=0,
                         lease=None,controller=None,record=None,hard_deadline=None,due=0.0,stages=[])
    root = Path(directory).absolute()
    require(".." not in root.parts and not any(p.is_symlink() for p in (root,*root.parents)) and not root.exists(), "Fresh nonsymlink output directory required.")
    require(root.parent.is_dir(), "Create the parent output directory before execution.")
    owner._time()
    report = {"shared_power_acquisition_schema_version":1,"state":"preparing","completion_qualified":False,
              "whole_sweep_replay_available":False,"physical_qualification":False,"joint_timing_qualified":False,
              "diagnostic_only":True,"session_policy":"one_already_connected_dual_session_one_owner",
              "timing_policy":"host_monotonic_callback_guards; raw instrument time diagnostic only",
              "plan":snapshot(plan),"probe_ceilings_a":probes,"poll_interval_s":interval,
              "maximum_scheduler_steps":maximum_scheduler_steps,"maximum_records_per_cp_phase":MAXIMUM_RECORDS,
              "initial_preparation_barrier_released_s":None,"channels":[],"owner_cleanup":None,
              "journal_rows":0,"journal_closed":False,"error":None,"files":{},
              "limitations":["Native acquisition plus CP control, not whole-sweep replay-qualified completion.",
                  "No public dispatch or hardware qualification; injected clocks/drivers are trusted dependencies.",
                  "Cooperative callbacks are not preemptible and do not establish shutdown latency."]}
    stream = primary = None
    root_created = False
    last_served = 2
    sequence = 0

    def now():
        return owner._time()

    def guard():
        try:
            owner._usable()
            same(owner.session.plan,owner._base_plan,"Base session plan changed during coordination.")
            for state in states.values():
                lease = state["lease"]
                if state["record"] is not None and state["record"]["request"]["phase"]["type"] not in CONTROLLED_PHASES and lease is not None and lease.state == "active":
                    lease.check_deadline()
            for state in states.values():
                controller = state["controller"]
                if controller is not None and controller.state != "completed":
                    controller.watchdog(now_s=now())
            time = now()
            for state in states.values():
                if state["record"] is not None and state["hard_deadline"] is not None and time >= state["hard_deadline"]:
                    raise TimeoutError(f"Channel {state['channel']} stage deadline reached.")
            return time
        except BaseException as error:
            owner._fail(error)
            raise

    def call(operation,*args,**kwargs):
        guard()
        result = operation(*args,**kwargs)
        guard()
        return result

    def control(operation,**kwargs):
        guard()
        stamp = now()  # Evaluate AFTER watchdogs; never pass a pre-guard stale time.
        result = operation(now_s=stamp,**kwargs)
        guard()
        return result,stamp

    def emit(kind,state=None,**data):
        nonlocal sequence
        row = {"sequence":sequence,"kind":kind,"channel":None if state is None else state["channel"],
               "stage_id":None if state is None or state["record"] is None else state["record"]["request"]["stage_id"],"data":data}
        text = json.dumps(row,allow_nan=False,separators=(",",":"))+"\n"
        require(stream.write(text)==len(text),"Short acquisition journal write.")
        stream.flush()
        sequence += 1
        return row

    def release_barrier():
        if report["initial_preparation_barrier_released_s"] is None and all(s["position"]>=2 for s in states.values()):
            time = now()
            call(emit,"initial_preparation_barrier_released",host_s=time)
            report["initial_preparation_barrier_released_s"] = time

    def start(state):
        c = state["channel"]
        request = state["requests"][state["position"]]
        phase = request["phase"]
        started = now()
        state["record"] = {"request":snapshot(request),"state":"running","started_s":started,"finished_s":None,
                           "stop_reason":None,"sample_count":0,"last_feedback":None,"native_outcome":None,
                           "journal_first_sequence":sequence,"journal_last_sequence":None,"stop_verified":False}
        state["hard_deadline"] = None if phase["type"]=="rest" else _number(started+phase["maximum_duration_s"],"stage deadline")
        call(emit,"stage_start",state,host_s=started,request=snapshot(request))
        if phase["type"] not in CONTROLLED_PHASES:
            state["lease"] = call(owner.borrow_native,c,request,root/f"channel_{c}"/request["stage_id"])
            call(state["lease"].start)
        else:
            limits = phase_limits(phase)
            state["lease"] = call(owner.borrow,c,limits,maximum_probe_current_a=probes[c])
            setup = call(state["lease"].prepare)
            controller = phase_controller(limits)
            controller.arm_zero_current_probe(now_s=now(),outputs_verified_off=setup["selected_output_off"],
                current_source_verified=setup["current_source"],zero_current_setpoint_a=setup["current_setpoint_a"],maximum_probe_current_a=probes[c])
            state["controller"] = controller
            call(emit,"probe_armed",state,host_s=controller.started_s,setup=setup)
            call(state["lease"].set_output,True)
            on = call(state["lease"].get_output_state)
            _,enabled_s = control(controller.confirm_zero_current_probe_enabled,output_reported_on=on)
            call(emit,"probe_enabled",state,host_s=enabled_s,output_reported_on=on)
        state["due"] = now()

    def finish(state,reason,native=None):
        record = state["record"]
        if native is None:
            stop = call(state["lease"].stop)
            require(stop["channel"] == state["channel"] and stop["output_reported_off"] is True
                    and stop["automatic_output_on_reported_off"] is True,"CP selected stop did not verify OFF.")
        else:
            require(native["state"]=="completed" and type(native["channel"]) is int and native["channel"]==state["channel"]
                    and native["stage_id"]==record["request"]["stage_id"] and native["specimen_id"]==state["specimen_id"]
                    and state["lease"].state=="completed" and state["lease"].stopped is True,"Native stage outcome identity differs.")
            record["native_outcome"] = snapshot(native)
        require(state["lease"].stopped is True and state["channel"] not in owner._channels,"Completed stage still owns a channel lease.")
        call(emit,"stage_acquisition_finished",state,host_s=now(),stop_reason=reason)
        record.update(state="acquisition_completed_unreplayed",finished_s=now(),stop_reason=reason,stop_verified=True,journal_last_sequence=sequence-1)
        state["stages"].append(record)
        state.update(record=None,lease=None,controller=None,hard_deadline=None,position=state["position"]+1,due=now())
        release_barrier()

    def feedback(state):
        frame = None
        before = now()
        try:
            guard()
            frame = state["lease"].read_fresh()
            require(isinstance(frame,PowerFrame) and frame.channel==state["channel"],"Invalid/cross-channel paired CP frame.")
            after = now()
            item = {"frame":asdict(frame),"read_started_s":before,"read_finished_s":after,
                    "instrument_read_bracket_s":state["lease"]._fault_bracket}
            state["record"]["last_feedback"] = snapshot(item)
            guard()
        except BaseException as error:
            owner._fail(error)  # Stop before potentially slow failure persistence.
            retained = frame if frame is not None else getattr(error,"power_frame",None)
            if isinstance(retained,PowerFrame):
                item = {"frame":asdict(retained),"read_started_s":before,"read_finished_s":None,
                        "instrument_read_bracket_s":getattr(error,"power_read_bracket_s",None)}
                state["record"]["last_feedback"] = snapshot(item)
                try:emit("power_feedback_fault",state,**item)
                except BaseException as secondary:error.add_note(f"Fault feedback journal: {type(secondary).__name__}: {secondary}")
            raise
        # Raw paired evidence is preserved before the next watchdog check.
        emit("power_feedback",state,**item)
        guard()
        state["record"]["sample_count"] += 1
        return frame.power_sample(before)

    def advance(state):
        controller = state["controller"]
        if controller is None:
            outcome = call(state["lease"].poll)
            if outcome is not None:
                finish(state,outcome["stop_reason"],outcome)
            else:
                state["due"] = now()+interval
            return
        require(state["record"]["sample_count"] < MAXIMUM_RECORDS,"CP record budget exhausted.")
        sample = feedback(state)
        method = controller.start_from_zero_current_probe if controller.state=="probing" else controller.update
        decision,decided_s = control(method,sample=sample)
        call(emit,"power_decision",state,host_s=decided_s,decision=decision.to_dict())
        if decision.action=="stop":
            finish(state,decision.stop_reason)
            return
        if decision.action=="set_current":
            requested = now()
            call(emit,"current_requested",state,host_s=requested,current_a=decision.current_setpoint_a)
            call(state["lease"].set_current,decision.current_setpoint_a)
            value = call(state["lease"].get_current_setpoint)
            _,acknowledged = control(controller.acknowledge_setpoint,readback_current_a=value)
            call(emit,"current_acknowledged",state,requested_s=requested,acknowledged_s=acknowledged,
                 requested_current_a=decision.current_setpoint_a,readback_current_a=value)
        state["due"] = max(sample.acquired_at_s+controller.limits.update_interval_s,now())

    def deadline(state):
        if state["record"] is None:
            return None
        controller = state["controller"]
        if controller is None:
            return state["lease"].next_deadline_s if state["lease"] is not None else state["hard_deadline"]
        reference = controller.started_s if controller.last_sample_s is None else controller.last_sample_s
        bounds = [state["hard_deadline"],reference+controller.limits.maximum_feedback_age_s]
        if controller.bad_tracking_since_s is not None:
            bounds.append(controller.bad_tracking_since_s+controller.limits.maximum_tracking_error_s)
        return min(bounds)

    try:
        root.mkdir(exist_ok=False)
        root_created = True
        write_json(root/MANIFEST,report,create=True)
        stream = (root/"events.jsonl").open("x",encoding="utf-8",newline="\n")
        with owner:
            for c in (1,2):call((root/f"channel_{c}").mkdir,exist_ok=False)
            call(emit,"owner_ready",host_s=now(),evidence=owner.startup_evidence)
            for _ in range(maximum_scheduler_steps):
                time = guard()
                release_barrier()
                if all(s["position"]==len(s["requests"]) for s in states.values()):break
                ready,wakes = [],[]
                for c,state in states.items():
                    if state["position"]==len(state["requests"]):continue
                    if state["position"]==2 and state["record"] is None and report["initial_preparation_barrier_released_s"] is None:continue
                    bound = deadline(state)
                    due = state["due"] if bound is None else min(state["due"],bound)
                    wakes.append(due)
                    if due<=time:
                        ready.append((c==last_served,bound if bound is not None else due,c))
                if ready:
                    c = min(ready)[-1]
                    state,last_served = states[c],c
                    start(state) if state["record"] is None else advance(state)
                    if all(s["position"]==len(s["requests"]) for s in states.values()):break
                else:
                    require(wakes and min(wakes)>time,"Coordinator cannot make forward progress.")
                    call(sleep,min(wakes)-time)
                    require(now()>time,"Sleep did not advance the host clock.")
            else:
                raise RuntimeError("Shared coordinator scheduler budget exhausted.")
    except BaseException as error:
        primary = error
        if owner.claimed and not owner.closed:owner._fail(error)
    finally:
        if stream is not None:
            try:stream.close();report["journal_closed"] = True
            except BaseException as error:
                if primary is None:primary = error
                else:primary.add_note(f"Acquisition journal close: {type(error).__name__}: {error}")
        report["owner_cleanup"] = snapshot(owner.cleanup_report)
        report["journal_rows"] = sequence
        for state in states.values():
            done = state["position"]==len(state["requests"])
            if state["record"] is not None:
                state["record"]["state"] = "aborted"
                state["record"]["controller_state"] = None if state["controller"] is None else state["controller"].state
            report["channels"].append({"channel":state["channel"],"specimen_id":state["specimen_id"],
                "state":"acquisition_completed_unreplayed" if done else "aborted","completed_stage_count":state["position"],
                "stages":state["stages"],"interrupted_stage":state["record"],
                "next_stage_id":None if done else state["requests"][state["position"]]["stage_id"]})
        report["state"] = "acquisition_completed_unreplayed" if primary is None else "raised"
        if primary is not None:report["error"] = {"type":type(primary).__name__,"message":str(primary),"notes":list(getattr(primary,"__notes__",()))}
        if root_created and root.is_dir():
            try:
                report["files"] = {k:v for k,v in tree_files(root).items() if k!=MANIFEST}
                write_json(root/MANIFEST,report)
            except BaseException as error:
                if primary is None:
                    primary = error;report["state"] = "raised";report["error"] = {"type":type(error).__name__,"message":str(error)}
                else:primary.add_note(f"Acquisition diagnostics: {type(error).__name__}: {error}")
    if primary is not None:
        primary.shared_power_acquisition_report = snapshot(report)
        raise primary
    return snapshot(report)
