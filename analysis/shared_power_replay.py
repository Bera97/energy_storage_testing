"""Offline replay of shared native/CP recorded control evidence. Never runs hardware.

Valid means recorded evidence is internally consistent and accepted by replayed
controllers/monitors, NOT authentic instrument provenance, physical qualification,
unobserved watchdog execution, electrical isolation or scientific completeness.
Keep sources unchanged; repeated inventory checks are not an atomic snapshot.
"""
import argparse
import csv
from dataclasses import asdict
import json
from pathlib import Path
from acquisition.spot_acquisition import parse_reading
from analysis.validate_recorded_run import validate_run
from experiments import zero_current_rest as zr
from experiments.charge_controller import charge_stop_reason
from experiments.charge_cv_classifier import VoltageSourceCVClassifier
from experiments.charge_trace_monitor import ChargeTraceMonitor
from experiments.discharge_trace_monitor import DischargeTraceMonitor
from experiments.config import require_charge_monitoring_settings
from experiments.constant_power import PowerSample, _number
from experiments.phase_outcome import validate_transport_outcome
from experiments.power_planning import build_dual_constant_power_sweep_plan
from experiments.power_sweep_contract import (
    CONTROLLED_PHASES, MAXIMUM_RECORDS, build_sequence, control_section, digest, phase_controller, phase_limits,
    read_json, require, same, snapshot, tree_files,
)

MANIFEST = "dual_power_acquisition.json"
MAX_ANALYSIS_POINTS = 1000000


def _time(value):
    return _number(value,"recorded host time",zero=True)


def _root(directory):
    root=Path(directory).absolute()
    require(".." not in root.parts and not any(p.is_symlink() for p in (root,*root.parents)) and root.is_dir(),"Unsafe/missing source directory.")
    return root


def inventory(root):
    result={}
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(),"Source symlinks are forbidden.")
        if path.is_file() and path!=root/MANIFEST:
            result[path.relative_to(root).as_posix()]={"sha256":digest(path),"size_bytes":path.stat().st_size}
    return result


def _json_line(text):
    def pairs(items):
        result={}
        for key,value in items:
            require(key not in result,"Duplicate journal key.");result[key]=value
        return result
    def invalid(value):raise ValueError("Nonfinite journal constant: "+value)
    return json.loads(text,object_pairs_hook=pairs,parse_constant=invalid)


def _native(root,request,preview,record,identity):
    """Validate bound native stage, replay CC endpoint; measured rest rechecks rows."""
    phase=request["phase"];directory=root/f"channel_{preview['channel']}"/request["stage_id"]
    document=read_json(directory/"stage.json")
    require(type(document.get("shared_native_stage_schema_version")) is int and document["shared_native_stage_schema_version"]==1
            and document["state"]=="completed" and document["error"] is None and document["physical_qualification"] is False,"Invalid native stage state.")
    for key,value in (("configuration",preview["configuration"]),("request",request),("channel",preview["channel"]),
                      ("specimen_id",preview["specimen_id"]),("identity",identity),("resource_address",preview["resource"])):
        same(document[key],value,"Native stage identity differs: "+key)
    same({k:v for k,v in tree_files(directory).items() if k!="NATIVE_STAGE_COMPLETE"},document["files"],"Native stage file inventory differs.")
    require((directory/"NATIVE_STAGE_COMPLETE").read_text()=="SHARED_NATIVE_STAGE sha256="+digest(directory/"stage.json")+"\n","Native stage marker differs.")
    start,finish=_time(document["started_s"]),_time(document["finished_s"])
    require(record["started_s"]<=start<=finish<=record["finished_s"],"Native stage lies outside coordinator bounds.")
    outcome=document["evidence"]["outcome"]
    expected={"state":"completed","channel":preview["channel"],"specimen_id":preview["specimen_id"],
              "stage_id":request["stage_id"],"stop_reason":outcome["stop_reason"],"directory":str(directory),"evidence_kind":document["evidence"]["kind"]}
    same(record["native_outcome"],expected,"Native outer outcome differs.")
    require(outcome["state"]=="completed","Native outcome did not complete.")
    config=preview["initial_preparation"]["configuration"];points=[];probe=tail=0
    if phase["type"] in ("charge_cc_cv","discharge_cc"):
        require(document["evidence"]["kind"]=="incremental_csv","Wrong active evidence kind.")
        path=directory/"phase";same(outcome["run_directory"],str(path),"Native recording path differs.")
        check=validate_run(path)
        require(check["valid"] and check["state"]=="returned","Native CSV validation failed.")
        metadata=read_json(path/"metadata.json")
        same(metadata["metadata"]["configuration"],config,"Native configuration differs.")
        same(metadata["metadata"]["channel"],preview["channel"],"Native channel differs.")
        same(metadata["experiment_phase"],phase,"Native phase differs.")
        transport=metadata["phase_transport_report"];validate_transport_outcome(phase,transport)
        for key in ("phase_index","cycle","type"):
            same(outcome[key],phase[key],"Native outcome identity differs.")
        native_result=metadata["outcome"]
        require(native_result["state"]=="returned" and native_result["shutdown_verified"] is True
                and native_result["fault_record_indices"]==[],"Native recording outcome failed.")
        same(native_result["stop_reason"],outcome["stop_reason"],"Native recording stop reason differs.")
        for key in ("records_flushed","retained_count","requested_count","triggering_record_index"):
            same(native_result[key],transport[key],"Native recording/transport counts differ: "+key)
        require(transport["shutdown_scope"]=="selected_channel","Native stop scope differs.")
        readings=[]
        with (path/"measurements.csv").open(newline="",encoding="utf-8") as stream:
            for i,row in enumerate(csv.DictReader(stream,strict=True)):
                require(i<MAXIMUM_RECORDS and None not in row and all(v is not None for v in row.values())
                        and int(row["record_index"])==i and int(row["channel"])==preview["channel"],"Malformed/cross-channel native row.")
                reading=parse_reading(",".join(row[k] for k in ("voltage_v","current_a","instrument_timestamp_s","status")),preview["channel"])
                require(not reading.has_fault and row["has_fault"]==str(reading.has_fault)
                        and row["in_compliance"]==str(reading.in_compliance),"Native fault/status inconsistency.")
                readings.append(reading)
        require(0<len(readings)==check["rows_read"]==transport["retained_count"],"Native record count differs.")
        monitoring=require_charge_monitoring_settings(config);timing=transport["measurement_timing"]
        require(timing["measurement_time_basis"]=="first_recorded_sample" and timing["output_enable_time_established"] is False,"Native timing policy differs.")
        same(timing["instrument_timestamp_origin_s"],readings[0].timestamp_s,"Native timestamp origin differs.")
        same(timing["measurement_elapsed_at_origin_s"],0.0,"Native elapsed origin differs.")
        args=dict(save_batch=lambda batch:None,instrument_timestamp_origin_s=readings[0].timestamp_s,
                  phase_elapsed_at_origin_s=0.0,maximum_sample_gap_s=monitoring["maximum_sample_gap_s"])
        if phase["type"]=="charge_cc_cv":
            classifier=VoltageSourceCVClassifier(**{k:monitoring[k] for k in ("target_voltage_v","voltage_tolerance_v","maximum_voltage_v")})
            monitor=ChargeTraceMonitor(phase,cv_classifier=classifier,**args)
        else:monitor=DischargeTraceMonitor(phase,maximum_voltage_v=monitoring["maximum_voltage_v"],**args)
        decision=monitor.ingest(tuple(readings))
        reason=charge_stop_reason(phase) if phase["type"]=="charge_cc_cv" else "voltage_min"
        require(decision.stop_reason==reason==outcome["stop_reason"]==record["stop_reason"]
                and decision.triggering_record_index==transport["triggering_record_index"],"Native controller endpoint replay differs.")
        last=decision.triggering_record_index
        same(metadata["outcome"]["triggering_reading"],asdict(readings[last]),"Native triggering reading differs.")
        tail=len(readings)-last-1
        points=[dict(time_s=r.timestamp_s,voltage_v=r.voltage_v,current_a=r.current_a,status=r.status,
                     raw_instrument_timestamp_s=r.timestamp_s,host_read_started_s=None,host_read_finished_s=None) for r in readings[:last+1]]
        basis="within_trace_instrument_timestamp"
    else:
        require(outcome["stop_reason"]==record["stop_reason"]=="rest_elapsed","Wrong rest endpoint.")
        if zr.is_measured(phase):
            require(document["evidence"]["kind"]=="measured_rest","Wrong measured-rest evidence.")
            report=outcome["rest_report"];same(report["run_directory"],str(directory/"rest"),"Measured-rest path differs.")
            require(report["shutdown_scope"]=="selected_channel","Measured-rest stop scope differs.")
            _,rows=zr.validate_record(report,phase,config,directory)
            for row in rows:
                a,b=row["host_before_s"],row["host_after_s"]
                points.append(dict(time_s=a+(b-a)/2,voltage_v=row["voltage_v"],current_a=row["current_a"],status=row["status"],
                                   raw_instrument_timestamp_s=row["instrument_timestamp_s"],host_read_started_s=a,host_read_finished_s=b))
            basis="relative_host_read_bracket_midpoint_estimate"
        else:
            require(document["evidence"]["kind"]=="outputs_off_rest","Wrong outputs-off evidence.")
            rest_start=_time(document["rest_started_s"]);previous=rest_start
            require(start<=rest_start<=finish and len(document["off_readbacks"])>=2,"Missing outputs-off start/checks.")
            for row in document["off_readbacks"]:
                stamp=_time(row["host_s"])
                require(previous<=stamp<=finish and row["output_state"] is False and row["automatic_output_on"] is False,"Invalid outputs-off check.")
                previous=stamp
            require(previous-rest_start>=phase["duration_s"],"Outputs-off rest completed early.")
            basis="unmeasured_outputs_off_rest"
    return points,basis,probe,tail


def replay_shared_power_acquisition(directory, *, expected_documents=None, collect_samples=False):
    """Raise on inconsistency. Never modifies recordings or enables hardware."""
    root=_root(directory);manifest=root/MANIFEST;before=manifest.read_bytes();doc=read_json(manifest)
    require(type(doc.get("shared_power_acquisition_schema_version")) is int and doc["shared_power_acquisition_schema_version"]==1
            and doc["state"]=="acquisition_completed_unreplayed" and doc["error"] is None,"Incomplete/failed acquisition.")
    for name in ("completion_qualified","whole_sweep_replay_available","physical_qualification","joint_timing_qualified"):
        require(doc[name] is False,"Mixed/unsupported acquisition qualification.")
    require(doc["diagnostic_only"] is True and doc["journal_closed"] is True,"Unclosed/non-diagnostic acquisition.")
    same(doc["session_policy"],"one_already_connected_dual_session_one_owner","Session policy differs.")
    same(doc["timing_policy"],"host_monotonic_callback_guards; raw instrument time diagnostic only","Timing policy differs.")
    require(type(doc["maximum_scheduler_steps"]) is int and 0<doc["maximum_scheduler_steps"]<=10000000,"Unsupported scheduler budget.")
    same(doc["maximum_records_per_cp_phase"],MAXIMUM_RECORDS,"Unsupported CP record budget.");_number(doc["poll_interval_s"],"poll interval")
    plan=build_dual_constant_power_sweep_plan([p["configuration"] for p in doc["plan"]["channels"]])
    same(doc["plan"],plan,"Stored plan differs from canonical profiles.")
    if expected_documents is not None:same(build_dual_constant_power_sweep_plan(expected_documents),plan,"CLI profiles differ from recorded profiles.")
    files=inventory(root);same(doc["files"],files,"Source file inventory differs.")
    cleanup=doc["owner_cleanup"]
    require(cleanup["shutdown_returned"] is True and cleanup["outputs_reported_off"] is True
            and cleanup["session_close_returned"] is True and cleanup["errors"]==[],"Unsuccessful owner cleanup.")
    same(cleanup["channels"],[{"channel":c,"stop_returned":True,"output_state":False,"automatic_output_on":False} for c in (1,2)],"Cleanup channel evidence differs.")
    states={};expected_files={"events.jsonl"};phases=[];saved_points=0
    require(len(doc["channels"])==2 and set(doc["probe_ceilings_a"])=={"1","2"},"Channel/probe identities differ.")
    for preview,channel in zip(plan["channels"],doc["channels"]):
        c=preview["channel"];_,requests=build_sequence(preview["configuration"])
        require(type(channel["channel"]) is int and channel["channel"]==c and channel["specimen_id"]==preview["specimen_id"]
                and channel["state"]=="acquisition_completed_unreplayed" and channel["interrupted_stage"] is None
                and channel["next_stage_id"] is None and type(channel["completed_stage_count"]) is int
                and channel["completed_stage_count"]==len(requests)==len(channel["stages"]),"Incomplete/cross-channel progress.")
        probe=_number(doc["probe_ceilings_a"][str(c)],"probe ceiling")
        require(probe<=control_section(preview["configuration"])["maximum_current_a"],"Probe ceiling differs.")
        states[c]=dict(preview=preview,progress=channel,requests=requests,position=0,active=None,probe=probe,previous_finish=None)
        for request in requests:
            if request["phase"]["type"] in CONTROLLED_PHASES:continue
            prefix=f"channel_{c}/{request['stage_id']}/";expected_files.update(prefix+n for n in ("stage.json","NATIVE_STAGE_COMPLETE"))
            child="rest" if request["phase"]["type"]=="rest" else "phase"
            if child=="phase" or zr.is_measured(request["phase"]):
                expected_files.update(prefix+child+"/"+n for n in (("metadata.json","measurements.csv","COMPLETE") if child=="phase" else ("rest.json","measurements.csv","COMPLETE")))
    same(sorted(files),sorted(expected_files),"Unexpected/missing recording files.")
    identity=None;barrier=None;previous_time=None;rows=0

    def at(stamp):
        nonlocal previous_time
        stamp=_time(stamp);require(previous_time is None or stamp>=previous_time,"Journal host clock moved backwards.");previous_time=stamp
        for state in states.values():
            active=state["active"]
            if active is None:continue
            phase=active["request"]["phase"]
            if phase["type"]!="rest":require(stamp<active["record"]["started_s"]+phase["maximum_duration_s"],"Recorded stage host deadline exceeded.")
            controller=active["controller"]
            if controller is not None and controller.state!="completed":controller.watchdog(now_s=stamp)
        return stamp

    with (root/"events.jsonl").open(encoding="utf-8",newline="") as stream:
        for index,line in enumerate(stream):
            require(index<10*doc["maximum_scheduler_steps"]+100 and len(line)<=4000000 and line.endswith("\n"),"Journal budget/truncation violation.")
            row=_json_line(line);same(sorted(row),["channel","data","kind","sequence","stage_id"],"Unknown journal fields.")
            require(type(row["sequence"]) is int and row["sequence"]==index,"Journal sequence gap/reorder.")
            kind,data,c=row["kind"],row["data"],row["channel"];rows=index+1
            if kind=="owner_ready":
                require(index==0 and c is None and row["stage_id"] is None and identity is None,"Unexpected owner-ready event.")
                evidence=data["evidence"];identity=evidence["identity"];parts=identity.split(",")
                require(len(parts)==4 and all(p.strip() for p in parts) and parts[1].strip().upper()=="B2902B"
                        and parts[2].strip()==plan["channels"][0]["execution_settings"]["expected_serial"],"Owner identity differs.")
                require(evidence["resource_address"]==plan["resource"] and evidence["outputs_reported_off"] is True
                        and evidence["automatic_outputs_reported_off"] is True and evidence["physical_qualification"] is False
                        and evidence["trigger_groups"]["independent"] is True,"Incomplete startup evidence.")
                require("".join(evidence["trigger_groups"]["after"].split()) in ("(@1),(@2)","(@2),(@1)"),"Trigger groups differ.")
                same(evidence["trace_clock"],{"timer_auto_reset":False,"global_timer_reset_issued":False},"Shared clock setup differs.")
                at(data["host_s"]);continue
            require(identity is not None,"Journal lacks startup.")
            if kind=="initial_preparation_barrier_released":
                require(c is None and row["stage_id"] is None and barrier is None
                        and all(s["position"]==2 and s["active"] is None for s in states.values()),"Early/duplicate preparation barrier.")
                barrier=at(data["host_s"]);same(barrier,doc["initial_preparation_barrier_released_s"],"Barrier time differs.");continue
            require(type(c) is int and c in states,"Invalid journal channel.")
            state=states[c];position=state["position"];require(position<len(state["requests"]),"Events follow final channel completion.")
            request=state["requests"][position];phase=request["phase"];require(row["stage_id"]==request["stage_id"],"Wrong/cross-channel stage order.")
            if kind=="stage_start":
                require(state["active"] is None and (position<2 or barrier is not None),"Overlapping stage or unreleased barrier.")
                same(data["request"],request,"Stage request differs.");time=at(data["host_s"]);record=state["progress"]["stages"][position]
                same(record["request"],request,"Stored stage request differs.");same(record["started_s"],time,"Stage start differs.")
                require(record["state"]=="acquisition_completed_unreplayed" and record["stop_verified"] is True
                        and type(record["journal_first_sequence"]) is int and record["journal_first_sequence"]==index
                        and _time(record["finished_s"])>=time and (state["previous_finish"] is None or time>=state["previous_finish"]),"Stage progress/time differs.")
                state["active"]=dict(request=request,record=record,controller=None,step="arm",sample=None,count=0,points=[],decision=None,last_feedback=None);continue
            active=state["active"];require(active is not None,"Event outside stage.");controller=active["controller"]
            if kind=="stage_acquisition_finished":
                time=at(data["host_s"]);record=active["record"]
                require(time<=record["finished_s"] and type(record["journal_last_sequence"]) is int and record["journal_last_sequence"]==index,"Stage finish differs.")
                same(data["stop_reason"],record["stop_reason"],"Stop reason differs.")
                if phase["type"] in CONTROLLED_PHASES:
                    require(active["step"]=="finished" and controller.state=="completed" and data["stop_reason"]=="voltage_min"
                            and record["native_outcome"] is None,"CP phase was not completed by its controller.")
                    same(record["sample_count"],active["count"],"CP sample count differs.");same(record["last_feedback"],active["last_feedback"],"Last paired feedback differs.")
                    points,basis,probe,tail=active["points"],"host_read_bracket_midpoint_estimate",1,0
                else:
                    same(record["sample_count"],0,"Native phase contains CP count.");require(record["last_feedback"] is None,"Native phase contains CP feedback.")
                    points,basis,probe,tail=_native(root,request,state["preview"],record,identity)
                saved_points+=len(points);require(not collect_samples or saved_points<=MAX_ANALYSIS_POINTS,"Offline analysis point budget exceeded.")
                phases.append({"channel":c,"specimen_id":state["preview"]["specimen_id"],"request":snapshot(request),"time_basis":basis,
                               "excluded_probe_count":probe,"excluded_trailing_count":tail,"points":points if collect_samples else []})
                state["previous_finish"]=record["finished_s"];at(record["finished_s"]);state["position"]+=1;state["active"]=None;continue
            require(phase["type"] in CONTROLLED_PHASES,"CP event inside native phase.")
            if kind=="probe_armed":
                require(active["step"]=="arm" and controller is None,"Duplicate/out-of-order probe arm.")
                time=at(data["host_s"]);setup=data["setup"];same(setup["channel"],c,"Probe channel differs.");same(setup["identity"],identity,"Probe instrument differs.")
                same(setup["voltage_compliance_v"],phase["controller_limits"]["maximum_voltage_v"],"Probe compliance differs.")
                require(setup["auto_output_on"] is False,"Probe automatic output-on enabled.")
                controller=phase_controller(phase_limits(phase))
                controller.arm_zero_current_probe(now_s=time,outputs_verified_off=setup["selected_output_off"],current_source_verified=setup["current_source"],
                                                 zero_current_setpoint_a=setup["current_setpoint_a"],maximum_probe_current_a=state["probe"])
                active.update(controller=controller,step="enable")
            elif kind=="probe_enabled":
                require(active["step"]=="enable","Unexpected probe activation.");time=at(data["host_s"])
                controller.confirm_zero_current_probe_enabled(now_s=time,output_reported_on=data["output_reported_on"]);active["step"]="feedback"
            elif kind=="power_feedback":
                require(active["step"]=="feedback" and active["count"]<MAXIMUM_RECORDS,"Unexpected/over-budget feedback.")
                a,b=at(data["read_started_s"]),_time(data["read_finished_s"]);require(a<=b,"Reversed read bracket.");bracket=data["instrument_read_bracket_s"]
                require(isinstance(bracket,list) and len(bracket)==2 and a<=_time(bracket[0])<=_time(bracket[1])<=b,"Invalid instrument read bracket.")
                frame=data["frame"];same(sorted(frame),["channel","current_a","raw_instrument_timestamp_s","status","voltage_v"],"Unexpected frame fields.")
                require(type(frame["channel"]) is int and frame["channel"]==c and type(frame["status"]) is int,"Cross-channel/invalid frame.")
                reading=parse_reading(",".join(str(frame[k]) for k in ("voltage_v","current_a","raw_instrument_timestamp_s","status")),c)
                sample=PowerSample(a,reading.voltage_v,reading.current_a,current_source=reading.current_source,in_compliance=reading.in_compliance,has_fault=reading.has_fault);at(b)
                if active["count"]>0 and collect_samples:
                    active["points"].append(dict(time_s=a+(b-a)/2,voltage_v=reading.voltage_v,current_a=reading.current_a,status=reading.status,
                        raw_instrument_timestamp_s=reading.timestamp_s,host_read_started_s=a,host_read_finished_s=b))
                active.update(sample=sample,count=active["count"]+1,step="decision",last_feedback=snapshot(data))
            elif kind=="power_decision":
                require(active["step"]=="decision","Decision lacks fresh paired feedback.");time=at(data["host_s"])
                method=controller.start_from_zero_current_probe if controller.state=="probing" else controller.update
                decision=method(now_s=time,sample=active["sample"]);same(decision.to_dict(),data["decision"],"CP controller decision replay differs.")
                active["decision"]=decision;active["step"]="request" if decision.action=="set_current" else "finished" if decision.action=="stop" else "feedback"
            elif kind=="current_requested":
                require(active["step"]=="request","Unexpected current request.");at(data["host_s"])
                same(data["current_a"],active["decision"].current_setpoint_a,"Current command differs.");active.update(step="ack",requested_s=data["host_s"])
            elif kind=="current_acknowledged":
                require(active["step"]=="ack","Unexpected/missing current request.");same(data["requested_s"],active["requested_s"],"Current request time differs.")
                same(data["requested_current_a"],active["decision"].current_setpoint_a,"Acknowledged request differs.")
                time=at(data["acknowledged_s"]);controller.acknowledge_setpoint(now_s=time,readback_current_a=data["readback_current_a"]);active["step"]="feedback"
            else:raise ValueError("Unknown/fault journal event in a completed acquisition: "+str(kind))
    require(type(doc["journal_rows"]) is int and rows==doc["journal_rows"] and barrier is not None
            and all(s["position"]==len(s["requests"]) and s["active"] is None for s in states.values()),"Truncated/incomplete journal.")
    same(inventory(root),files,"Source changed during replay.");require(manifest.read_bytes()==before,"Manifest changed during replay.")
    return {"valid":True,"errors":[],"state":"recorded_control_replay_valid","physical_qualification":False,
            "source_directory":str(root),"source_manifest_sha256":digest(manifest),"source_files":files,
            "configurations":[p["configuration"] for p in plan["channels"]],"phase_count":len(phases),"journal_rows":rows,"phases":phases,
            "limitations":["Replay checks recorded control points, not authenticity, unrecorded callbacks, hardware timing or electrical qualification.",
                           "Native recordings contain absolute references and must remain at their recorded location."]}


def validate_shared_power_acquisition(directory, *, expected_documents=None):
    try:return replay_shared_power_acquisition(directory,expected_documents=expected_documents)
    except Exception as error:return {"valid":False,"errors":[f"{type(error).__name__}: {error}"],"state":None,"physical_qualification":False}


def main(argv=None):
    parser=argparse.ArgumentParser(description="Offline replay of shared native/CP acquisition; no hardware.")
    parser.add_argument("directory");args=parser.parse_args(argv);result=validate_shared_power_acquisition(args.directory)
    print(json.dumps(result,indent=2,allow_nan=False));return 0 if result["valid"] else 1


if __name__=="__main__":raise SystemExit(main())
