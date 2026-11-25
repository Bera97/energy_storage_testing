"""Fake-wire end-to-end shared acquisition, replay, analysis and public CLI tests."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
import yaml
import smu
from analysis.shared_power_replay import replay_shared_power_acquisition, validate_shared_power_acquisition, inventory, MANIFEST
from analysis.export_shared_power import export_shared_power_analysis, validate_shared_power_analysis, MARKER
from experiments.power_sweep_contract import read_json, write_json, digest, tree_files
from test_power_shared_coordinator import setup
from test_charge_current_sweep import profile
from test_constant_power_planning import power_profile


@pytest.mark.parametrize("measured",[False,True])
@pytest.mark.parametrize("asymmetric",[False,True])
@pytest.mark.parametrize("skew",[False,True])
def test_committed_coordinator_replay_and_export(setup,measured,asymmetric,skew):
    docs,wire,owner,run,root=setup(measured=measured,asymmetric=asymmetric,skew=skew)
    acquired=run();before={p:p.read_bytes() for p in root.rglob("*") if p.is_file()}
    checked=replay_shared_power_acquisition(root,expected_documents=docs,collect_samples=True)
    assert checked["valid"] and checked["phase_count"]==sum(c["completed_stage_count"] for c in acquired["channels"])
    result=export_shared_power_analysis(root,root.parent/"derived",expected_documents=docs)
    assert result["state"]=="completed" and validate_shared_power_analysis(root.parent/"derived")["valid"]
    assert wire.connect_count==wire.shutdown_count==wire.close_count==1 and owner.closed
    assert read_json(root/MANIFEST)["completion_qualified"] is False
    report=read_json(root.parent/"derived"/"analysis.json")
    assert report["recorded_control_replay_valid"] is True and report["physical_qualification"] is False
    assert all(p["excluded_probe_count"]==1 for p in report["phases"] if p["type"]=="discharge_cp")
    assert {p:p.read_bytes() for p in root.rglob("*") if p.is_file()}==before


def resign(root,rows=None):
    if rows is not None:(root/"events.jsonl").write_text("".join(json.dumps(row)+"\n" for row in rows))
    header=read_json(root/MANIFEST);header["files"]=inventory(root)
    if rows is not None:header["journal_rows"]=len(rows)
    write_json(root/MANIFEST,header)


@pytest.mark.parametrize("mutation",["decision","ack","channel","stage","sequence","barrier","probe","time","status","frame_channel","drop_ack","truncated","unknown","bracket"])
def test_replay_rejects_rehashed_journal_corruption(setup,mutation):
    _,_,_,run,root=setup(asymmetric=False);run();rows=[json.loads(line) for line in (root/"events.jsonl").read_text().splitlines()]
    if mutation=="decision":next(r for r in rows if r["kind"]=="power_decision")["data"]["decision"]["action"]="stop"
    elif mutation=="ack":next(r for r in rows if r["kind"]=="current_acknowledged")["data"]["readback_current_a"]=-0.01
    elif mutation=="channel":next(r for r in rows if r["kind"]=="power_feedback")["channel"]=2
    elif mutation=="stage":next(r for r in rows if r["kind"]=="power_feedback")["stage_id"]="wrong"
    elif mutation=="sequence":rows[2]["sequence"]+=1
    elif mutation=="barrier":rows.remove(next(r for r in rows if r["kind"]=="initial_preparation_barrier_released"))
    elif mutation=="probe":next(r for r in rows if r["kind"]=="probe_armed")["data"]["setup"]["current_setpoint_a"]=0.1
    elif mutation=="time":next(r for r in rows if r["kind"]=="power_feedback")["data"]["read_finished_s"]=-1
    elif mutation=="status":next(r for r in rows if r["kind"]=="power_feedback")["data"]["frame"]["status"]=9
    elif mutation=="frame_channel":next(r for r in rows if r["kind"]=="power_feedback")["data"]["frame"]["channel"]=2
    elif mutation=="drop_ack":rows.remove(next(r for r in rows if r["kind"]=="current_acknowledged"))
    elif mutation=="truncated":rows=rows[:-1]
    elif mutation=="unknown":rows[-1]["kind"]="invented"
    elif mutation=="bracket":next(r for r in rows if r["kind"]=="power_feedback")["data"]["instrument_read_bracket_s"][0]=-1
    resign(root,rows);assert not validate_shared_power_acquisition(root)["valid"]


@pytest.mark.parametrize("mutation",["state","qualification","closed","cleanup","count","extra","missing","native_endpoint","native_channel","native_outcome","inner_outcome","native_timing"])
def test_replay_rejects_header_native_and_inventory_corruption(setup,mutation):
    _,_,_,run,root=setup(asymmetric=False);run();doc=read_json(root/MANIFEST)
    if mutation=="state":doc["state"]="raised"
    elif mutation=="qualification":doc["completion_qualified"]=True
    elif mutation=="closed":doc["journal_closed"]=False
    elif mutation=="cleanup":doc["owner_cleanup"]["channels"][0]["output_state"]=None
    elif mutation=="count":doc["channels"][0]["completed_stage_count"]-=1
    elif mutation=="extra":(root/"stage.json").write_text("unexpected");doc["files"]=inventory(root)
    elif mutation=="missing":(root/"channel_1"/"stage_00000"/"phase"/"COMPLETE").unlink();doc["files"]=inventory(root)
    else:
        stage=root/"channel_1"/"stage_00000"
        if mutation in ("native_channel","native_outcome"):
            native=read_json(stage/"stage.json")
            if mutation=="native_channel":native["channel"]=2
            else:native["evidence"]["outcome"]["phase_index"]+=1
        else:
            path=stage/"phase"/"metadata.json";data=read_json(path)
            if mutation=="inner_outcome":data["outcome"]["stop_reason"]="invented"
            elif mutation=="native_timing":data["phase_transport_report"]["measurement_timing"]["measurement_time_basis"]="unknown"
            else:data["phase_transport_report"]["stop_reason"]="invented"
            write_json(path,data)
            native=read_json(stage/"stage.json");native["files"]={k:v for k,v in tree_files(stage).items() if k!="NATIVE_STAGE_COMPLETE"}
        write_json(stage/"stage.json",native);(stage/"NATIVE_STAGE_COMPLETE").write_text("SHARED_NATIVE_STAGE sha256="+digest(stage/"stage.json")+"\n")
        doc["files"]=inventory(root)
    write_json(root/MANIFEST,doc);assert not validate_shared_power_acquisition(root)["valid"]


def test_profile_binding_rejects_different_power(setup):
    docs,_,_,run,root=setup(asymmetric=False);run();docs[0]["sweep"]["power_w"]=[0.8]
    assert not validate_shared_power_acquisition(root,expected_documents=docs)["valid"]


def test_duplicate_keys_and_truncated_lines_are_rejected(setup):
    _,_,_,run,root=setup(asymmetric=False);run();path=root/"events.jsonl"
    text=path.read_text();path.write_text(text.replace('"sequence":0','"sequence":0,"sequence":0',1));resign(root)
    assert not validate_shared_power_acquisition(root)["valid"]
    path.write_text(text.rstrip());resign(root);assert not validate_shared_power_acquisition(root)["valid"]


def test_invalid_source_creates_no_export(setup):
    _,_,_,run,root=setup(asymmetric=False);run();doc=read_json(root/MANIFEST);doc["state"]="raised";write_json(root/MANIFEST,doc)
    with pytest.raises(RuntimeError):export_shared_power_analysis(root,root.parent/"never")
    assert not (root.parent/"never").exists()


def test_export_refuses_overlap_existing_destination_and_detects_tampering(setup):
    _,_,_,run,root=setup(asymmetric=False);run();destination=root.parent/"derived"
    with pytest.raises(RuntimeError):export_shared_power_analysis(root,root/"inside")
    export_shared_power_analysis(root,destination)
    with pytest.raises(RuntimeError):export_shared_power_analysis(root,destination)
    with (destination/"phase_summary.csv").open("a") as stream:stream.write("bad\n")
    assert not validate_shared_power_analysis(destination)["valid"]


def test_export_failure_removes_completion_marker(setup,monkeypatch):
    import analysis.export_shared_power as module
    _,_,_,run,root=setup(asymmetric=False);run();original=module._write
    def broken(path,text):
        if path.name=="phase_summary.csv":raise OSError("derived write failed")
        return original(path,text)
    monkeypatch.setattr(module,"_write",broken)
    with pytest.raises(OSError,match="derived write failed"):export_shared_power_analysis(root,root.parent/"partial")
    assert not (root.parent/"partial"/MARKER).exists()
    assert read_json(root.parent/"partial"/"analysis.json")["state"]=="raised"


def test_derived_export_is_portable_without_raw_source(setup):
    _,_,_,run,root=setup(asymmetric=False);run();output=root.parent/"derived";export_shared_power_analysis(root,output)
    root.rename(root.with_name("moved"));checked=validate_shared_power_analysis(output)
    assert checked["valid"] and checked["source_replay_revalidated"] is False


@pytest.mark.parametrize("reverse",[False,True])
def test_public_dual_analysis_cli_dispatch_is_offline(setup,monkeypatch,capsys,reverse):
    docs,wire,_,run,root=setup(asymmetric=False)
    configured=Path(docs[0]["output"]["root_directory"]);configured.mkdir(parents=True)
    # Keep the acquisition at its recorded absolute path by creating it there initially.
    from experiments.power_shared_coordinator import run_shared_power_sweep
    owner=wire.owner;root=configured/"recorded"
    run_shared_power_sweep(owner,root,sleep=wire.clock.sleep,maximum_probe_current_a={1:0.001,2:0.001},execute_requested=True)
    before=list(wire.commands)
    import experiments.dual_instrument_io as io
    import experiments.run_live as live
    def forbidden(*args,**kwargs):pytest.fail("Offline analysis attempted a connection")
    monkeypatch.setattr(io,"_new_smu",forbidden);monkeypatch.setattr(live,"_new_smu",forbidden)
    paths=[]
    for i,document in enumerate(reversed(docs) if reverse else docs):
        path=root.parent/f"profile{i}.yaml";path.write_text(yaml.safe_dump(document));paths.extend(["--config",str(path)])
    assert smu.main([*paths,"--analyze",root.name])==0
    result=json.loads(capsys.readouterr().out);assert result["state"]=="completed"
    assert validate_shared_power_analysis(result["directory"])["valid"] and wire.commands==before


def test_single_analysis_routes_existing_validated_export(profile,tmp_path,monkeypatch,capsys):
    document=power_profile(profile);source=Path(document["output"]["root_directory"])/"single";source.mkdir(parents=True)
    write_json(source/"power_sweep.json",{"configuration":document},create=True)
    original=smu.import_module;calls=[]
    def imported(name):
        if name=="analysis.power_sweep":return SimpleNamespace(validate_power_sweep=lambda p:{"valid":True,"errors":[]})
        if name=="analysis.export_power_sweep":return SimpleNamespace(export_power_sweep_analysis=lambda a,b:calls.append((a,b)) or {"state":"completed"})
        return original(name)
    monkeypatch.setattr(smu,"import_module",imported);path=tmp_path/"single.yaml";path.write_text(yaml.safe_dump(document))
    assert smu.main(["--config",str(path),"--analyze","single"])==0 and calls
    assert json.loads(capsys.readouterr().out)["state"]=="completed"


def test_standalone_offline_cli_exit_codes(setup,capsys):
    from analysis.shared_power_replay import main as replay_main
    from analysis.export_shared_power import main as export_main
    _,_,_,run,root=setup(asymmetric=False);run()
    assert replay_main([str(root)])==0;capsys.readouterr()
    assert export_main([str(root),str(root.parent/"derived")])==0;capsys.readouterr()
    assert export_main([str(root.parent/"derived"),"--check"])==0;capsys.readouterr()
    assert replay_main([str(root.parent/"missing")])==1



def test_resigned_derived_arithmetic_is_recalculated(setup):
    _,_,_,run,root=setup(asymmetric=False);run();output=root.parent/"derived";export_shared_power_analysis(root,output)
    path=output/"analysis.json";doc=read_json(path)
    next(p for p in doc["phases"] if p["type"]=="discharge_cp")["signed_energy_wh"]+=1
    write_json(path,doc);(output/MARKER).write_text("SHARED_POWER_ANALYSIS sha256="+digest(path)+"\n")
    assert not validate_shared_power_analysis(output)["valid"]


def test_stale_feedback_is_rejected_after_resigning(setup):
    _,_,_,run,root=setup(asymmetric=False);run();rows=[json.loads(line) for line in (root/"events.jsonl").read_text().splitlines()]
    data=next(r for r in rows if r["kind"]=="power_feedback")["data"]
    data["read_finished_s"]=data["read_started_s"]+1.0;data["instrument_read_bracket_s"][1]=data["read_finished_s"]
    resign(root,rows);assert not validate_shared_power_acquisition(root)["valid"]
