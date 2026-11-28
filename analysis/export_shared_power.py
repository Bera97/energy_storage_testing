"""Exclusive offline exports of replay-valid shared acquisitions. No hardware.

Derived intervals only; no phase joining, gap extrapolation or probe integration.
Portable export checks recalculate derived data, not source authenticity.
"""
import argparse
import csv
import io
import json
from pathlib import Path
import statistics
from acquisition.spot_acquisition import parse_reading
from analysis.energy_charge import calculate_energy_charge
from analysis.shared_power_replay import replay_shared_power_acquisition, inventory
from experiments.constant_power import _number
from experiments.power_planning import build_dual_constant_power_sweep_plan
from experiments.power_sweep_contract import digest, read_json, require, same, snapshot, write_json

MARKER="SHARED_POWER_ANALYSIS_COMPLETE"
TOTALS=("signed_charge_mah","positive_charge_mah","negative_charge_mah","signed_energy_wh","positive_energy_wh","negative_energy_wh")
POINTS=("time_s","voltage_v","current_a","status","raw_instrument_timestamp_s","host_read_started_s","host_read_finished_s")


def _table(rows,fields):
    output=io.StringIO(newline="");writer=csv.DictWriter(output,fieldnames=fields,lineterminator="\n")
    writer.writeheader();writer.writerows(rows);return output.getvalue()


def _write(path,text):
    with path.open("x",encoding="utf-8",newline="\n") as stream:
        require(stream.write(text)==len(text),"Short derived export write.");stream.flush()


def _phase(entry):
    request=entry["request"];points=entry["points"]
    result={k:entry[k] for k in ("channel","specimen_id","time_basis","excluded_probe_count","excluded_trailing_count")}
    result.update({k:request[k] for k in ("stage_id","scope","condition_id","condition_index","repeat_index","power_w")})
    result.update({k:request["phase"][k] for k in ("type","phase_index","cycle")})
    result.update(sample_count=len(points),interval_count=max(0,len(points)-1),has_integration_interval=len(points)>=2,
                  recorded_span_s=None,mean_signed_power_w=None,mean_discharge_power_error_w=None)
    result.update({k:None for k in TOTALS})
    if len(points)>=2:
        calculated=calculate_energy_charge([p["time_s"] for p in points],[p["voltage_v"] for p in points],[p["current_a"] for p in points])["summary"]
        result.update({k:calculated[k] for k in TOTALS});result["recorded_span_s"]=calculated["recorded_span_s"]
        result["mean_signed_power_w"]=calculated["time_weighted_mean_signed_power_w"]
        if result["type"]=="discharge_cp":result["mean_discharge_power_error_w"]=-result["mean_signed_power_w"]-request["power_w"]
    return result


def _groups(phases):
    grouped={}
    for p in phases:
        if p["scope"]=="condition":grouped.setdefault((p["channel"],p["condition_id"],p["cycle"]),[]).append(p)
    cycles=[]
    for (channel,condition,cycle),rows in sorted(grouped.items()):
        charge=next(p for p in rows if p["type"]=="charge_cc_cv");discharge=next(p for p in rows if p["type"] in ("discharge_cp","discharge_profile"))
        ratio=lambda a,b:a/b if a is not None and b is not None and b>0 else None
        cycles.append({"channel":channel,"specimen_id":discharge["specimen_id"],"condition_id":condition,"cycle":cycle,
            "condition_index":discharge["condition_index"],"repeat_index":discharge["repeat_index"],"power_w":discharge["power_w"],
            "charge_energy_wh":charge["positive_energy_wh"],"discharge_energy_wh":discharge["negative_energy_wh"],
            "observed_energy_ratio":ratio(discharge["negative_energy_wh"],charge["positive_energy_wh"]),
            "observed_coulomb_ratio":ratio(discharge["negative_charge_mah"],charge["positive_charge_mah"])})
    groups={}
    for row in cycles:groups.setdefault((row["channel"],row["condition_index"],row["power_w"]),[]).append(row)
    conditions=[]
    for (channel,index,power),rows in sorted(groups.items()):
        values=[r["discharge_energy_wh"] for r in rows if r["discharge_energy_wh"] is not None]
        conditions.append({"channel":channel,"specimen_id":rows[0]["specimen_id"],"condition_index":index,"power_w":power,
            "cycle_count":len(rows),"repeat_count":len({r["repeat_index"] for r in rows}),"observed_count":len(values),
            "discharge_energy_wh_mean":statistics.mean(values) if values else None,
            "discharge_energy_wh_sample_std":statistics.stdev(values) if len(values)>1 else None})
    return cycles,conditions


def _files(root):
    result={}
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(),"Analysis symlinks are forbidden.")
        if path.is_file() and path not in (root/"analysis.json",root/MARKER):
            result[path.relative_to(root).as_posix()]={"sha256":digest(path),"size_bytes":path.stat().st_size}
    return result


def export_shared_power_analysis(source_directory,output_directory,*,expected_documents=None):
    replay=replay_shared_power_acquisition(source_directory,expected_documents=expected_documents,collect_samples=True)
    source=Path(replay["source_directory"]);destination=Path(output_directory).absolute()
    require(".." not in destination.parts and not any(p.is_symlink() for p in (destination,*destination.parents))
            and not destination.exists(),"Fresh nonsymlink analysis destination required.")
    require(destination!=source and source not in destination.parents and destination not in source.parents,"Analysis must not overlap source.")
    phases=[_phase(entry) for entry in replay["phases"]];cycles,conditions=_groups(phases)
    header={"shared_power_analysis_schema_version":1,"state":"running","physical_qualification":False,
            "recorded_control_replay_valid":True,"source_directory":str(source),"source_manifest_sha256":replay["source_manifest_sha256"],
            "source_files":replay["source_files"],"configurations":replay["configurations"],"phases":phases,"cycles":cycles,"conditions":conditions,
            "integration_policy":"trapezoidal sampled I and V*I; per-phase observed intervals only; probes and trailing CC samples excluded",
            "ratio_policy":"ratios of observed coverage; not full-cycle thermodynamic efficiency; never clamped",
            "rest_policy":"SMU-terminal quantities, not internal self-discharge; unmeasured rests have null totals",
            "warnings":replay["limitations"]+["Host midpoint times are estimates. No interpolation across phases or unobserved coverage.",
                "Fewer than two samples gives null integrated totals, not measured zero."],"files":{},"error":None}
    header=snapshot(header)  # Reject nonfinite results before any output creation.
    destination.parent.mkdir(parents=True,exist_ok=True);destination.mkdir(exist_ok=False);marker_created=False
    try:
        write_json(destination/"analysis.json",header,create=True);(destination/"phase_samples").mkdir()
        for entry in replay["phases"]:
            name=f"channel_{entry['channel']}_{entry['request']['stage_id']}.csv";_write(destination/"phase_samples"/name,_table(entry["points"],POINTS))
        for name,rows in (("phase_summary.csv",phases),("cycle_comparison.csv",cycles),("condition_summary.csv",conditions)):
            _write(destination/name,_table(rows,list(rows[0]) if rows else []))
        final=replay_shared_power_acquisition(source,expected_documents=expected_documents)
        same(final["source_manifest_sha256"],replay["source_manifest_sha256"],"Source manifest changed during export.")
        same(final["source_files"],replay["source_files"],"Source changed during export.")
        header.update(state="completed",files=_files(destination));write_json(destination/"analysis.json",header)
        with (destination/MARKER).open("x",encoding="utf-8",newline="\n") as stream:
            marker_created=True;text="SHARED_POWER_ANALYSIS sha256="+digest(destination/"analysis.json")+"\n"
            require(stream.write(text)==len(text),"Short analysis marker write.")
        checked=validate_shared_power_analysis(destination);require(checked["valid"],"Derived export validation failed: "+str(checked["errors"]))
        same(inventory(source),replay["source_files"],"Source changed at export exit.")
        require(digest(source/"dual_power_acquisition.json")==replay["source_manifest_sha256"],"Source manifest changed at export exit.")
        return {"state":"completed","directory":str(destination),"phase_count":len(phases),"physical_qualification":False}
    except BaseException as error:
        if marker_created:
            try:(destination/MARKER).unlink(missing_ok=True)
            except BaseException as secondary:error.add_note(f"Analysis marker cleanup: {secondary}")
        header.update(state="raised",error={"type":type(error).__name__,"message":str(error)})
        try:write_json(destination/"analysis.json",header)
        except BaseException as secondary:error.add_note(f"Analysis failure metadata: {secondary}")
        raise


def validate_shared_power_analysis(directory):
    """Portable derived consistency only; does not revalidate original raw sources."""
    try:
        root=Path(directory).absolute();require(not any(p.is_symlink() for p in (root,*root.parents)),"Analysis symlinks are forbidden.")
        before=(root/"analysis.json").read_bytes();doc=read_json(root/"analysis.json")
        require(type(doc["shared_power_analysis_schema_version"]) is int and doc["shared_power_analysis_schema_version"]==1
                and doc["state"]=="completed" and doc["error"] is None and doc["recorded_control_replay_valid"] is True
                and doc["physical_qualification"] is False,"Invalid analysis state.")
        require((root/MARKER).read_text()=="SHARED_POWER_ANALYSIS sha256="+digest(root/"analysis.json")+"\n","Analysis marker differs.")
        files=_files(root);same(files,doc["files"],"Derived file inventory differs.")
        from experiments.power_sweep_contract import build_sequence
        build_dual_constant_power_sweep_plan(doc["configurations"]);requests={}
        for configuration in doc["configurations"]:
            plan,sequence=build_sequence(configuration)
            for request in sequence:requests[(plan["channel"],request["stage_id"])]=(plan,request)
        require(len(doc["phases"])==len(requests),"Analysis phase count differs.")
        expected={"phase_summary.csv","cycle_comparison.csv","condition_summary.csv"};recalculated=[];seen=set()
        for phase in doc["phases"]:
            require(type(phase["channel"]) is int,"Invalid derived channel type.");key=(phase["channel"],phase["stage_id"])
            require(key in requests and key not in seen,"Duplicate/unknown analysis stage.");seen.add(key)
            plan,request=requests[key];same(phase["specimen_id"],plan["specimen_id"],"Analysis specimen differs.")
            name=f"phase_samples/channel_{key[0]}_{key[1]}.csv";expected.add(name)
            with (root/name).open(encoding="utf-8",newline="") as stream:
                reader=csv.DictReader(stream,strict=True);same(reader.fieldnames,list(POINTS),"Derived sample columns differ.");points=[]
                for i,row in enumerate(reader):
                    require(i<100000 and None not in row and all(v is not None for v in row.values()),"Malformed/over-budget derived sample.")
                    points.append({k:(None if row[k]=="" else int(row[k]) if k=="status" else float(row[k])) for k in POINTS})
            kind=request["phase"]["type"]
            basis=("within_trace_instrument_timestamp" if kind in ("charge_cc_cv","discharge_cc") else
                   "host_read_bracket_midpoint_estimate" if kind in ("discharge_cp","discharge_profile") else
                   "relative_host_read_bracket_midpoint_estimate" if request["phase"].get("rest_mode")=="zero_current_logging" else "unmeasured_outputs_off_rest")
            same(phase["time_basis"],basis,"Derived time basis differs.");same(phase["excluded_probe_count"],1 if kind in ("discharge_cp","discharge_profile") else 0,"Derived probe exclusion differs.")
            require(type(phase["excluded_trailing_count"]) is int and phase["excluded_trailing_count"]>=0
                    and (kind in ("charge_cc_cv","discharge_cc") or phase["excluded_trailing_count"]==0),"Derived trailing exclusion differs.")
            require(basis!="unmeasured_outputs_off_rest" or points==[],"Unmeasured rest contains invented samples.")
            for point in points:
                _number(point["time_s"],"derived time",zero=True)
                reading=parse_reading(",".join(str(point[k]) for k in ("voltage_v","current_a","raw_instrument_timestamp_s","status")),key[0])
                require(not reading.has_fault,"Derived sample reports a fault.")
                if kind in ("charge_cc_cv","discharge_cc"):
                    require(point["time_s"]==point["raw_instrument_timestamp_s"] and point["host_read_started_s"] is None
                            and point["host_read_finished_s"] is None,"Derived native timestamp differs.")
                else:
                    a=_number(point["host_read_started_s"],"derived read before",zero=True);b=_number(point["host_read_finished_s"],"derived read after",zero=True)
                    require(a<=b and point["time_s"]==a+(b-a)/2,"Derived midpoint differs.")
                    if kind in ("discharge_cp","discharge_profile"):require(reading.current_source and not reading.in_compliance and reading.current_a<=request["phase"]["controller_limits"].get("zero_current_tolerance_a",0.0),"Derived CP flags/polarity differ.")
            expected_phase=_phase({"request":request,"points":points,**{k:phase[k] for k in
                                   ("channel","specimen_id","time_basis","excluded_probe_count","excluded_trailing_count")}})
            same(expected_phase,phase,"Derived calculations differ.");recalculated.append(expected_phase)
        same(sorted(files),sorted(expected),"Unexpected/missing derived files.")
        cycles,conditions=_groups(recalculated);same(cycles,doc["cycles"],"Derived cycles differ.");same(conditions,doc["conditions"],"Derived conditions differ.")
        for name,rows in (("phase_summary.csv",recalculated),("cycle_comparison.csv",cycles),("condition_summary.csv",conditions)):
            require((root/name).read_text()==_table(rows,list(rows[0]) if rows else []),"Derived summary differs.")
        same(_files(root),files,"Analysis changed during validation.");require((root/"analysis.json").read_bytes()==before,"Analysis metadata changed.")
        return {"valid":True,"errors":[],"phase_count":len(recalculated),"physical_qualification":False,"source_replay_revalidated":False}
    except Exception as error:return {"valid":False,"errors":[f"{type(error).__name__}: {error}"],"physical_qualification":False}


def main(argv=None):
    parser=argparse.ArgumentParser(description="Replay/export shared CP acquisition, or check a derived export. No hardware.")
    parser.add_argument("source");parser.add_argument("destination",nargs="?");parser.add_argument("--check",action="store_true");args=parser.parse_args(argv)
    if args.check:
        if args.destination is not None:parser.error("--check does not take a destination")
        result=validate_shared_power_analysis(args.source);print(json.dumps(result,indent=2));return 0 if result["valid"] else 1
    if args.destination is None:parser.error("an export destination is required")
    try:result=export_shared_power_analysis(args.source,args.destination)
    except (OSError,ValueError,RuntimeError) as error:print(f"Analysis failed: {error}");return 1
    print(json.dumps(result,indent=2));return 0


if __name__=="__main__":raise SystemExit(main())
