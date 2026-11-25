"""Fake-wire native CC/CV/rest integration; no instrument or public execution."""
from copy import deepcopy
import re
from concurrent.futures import ThreadPoolExecutor
import pytest
from experiments.constant_power import PowerLimits
from experiments.dual_instrument_io import DualInstrumentSession
from experiments.dual_preconditioning import build_dual_plan
from experiments.power_planning import build_dual_constant_power_sweep_plan
from experiments.power_shared_transport import SharedPowerSession
from experiments.power_shared_phases import MARKER, validate_borrowed_native_stage
from experiments.power_sweep_contract import build_sequence, digest, read_json, write_json
from test_charge_current_sweep import profile
from test_constant_power_planning import power_profile
from test_power_shared_transport import Wire


class TraceWire(Wire):
    def __init__(self, resource):
        super().__init__(resource)
        self.samples = {1:[], 2:[]}
        self.trace_responses = {":STAT:QUES:COND?":"0", ":STAT:OPER:COND?":"0"}
        for c in (1,2):
            self.trace_responses.update({f":TRIG{c}:ACQ:TIM? MIN":"0.00002", f":TRIG{c}:ACQ:TIM? MAX":"100000",
                                        f":SENS{c}:VOLT:APER?":"0.02"})
    def _write(self, command):
        clear = re.fullmatch(r":TRAC([12]):CLE",command)
        if clear:
            self._touch(command);self.samples[int(clear[1])] = [];return
        if command.startswith(":INIT:ACQ"):
            self._touch(command);return
        super()._write(command)
    def _query(self, command):
        if command in self.trace_responses:
            self._touch(command);return self.trace_responses[command]
        count = re.fullmatch(r":TRAC([12]):POIN:ACT\?",command)
        data = re.fullmatch(r":TRAC([12]):DATA\? (\d+),(\d+)",command)
        if count:
            self._touch(command);return str(len(self.samples[int(count[1])]))
        if data:
            self._touch(command);c,start,size=map(int,data.groups())
            return ",".join(str(v) for row in self.samples[c][start:start+size] for v in row)
        return super()._query(command)


@pytest.fixture
def rig(profile,tmp_path):
    def build(*,measured=False):
        first=power_profile(profile)
        first["execution"]={"expected_serial":"TESTSERIAL","maximum_voltage_v":3.81,"maximum_current_a":0.4}
        first["charge"].update(maximum_duration_s=10,maximum_cv_duration_s=5)
        first["charge"]["termination"]["hold_time_s"]=0.6
        first["discharge"]["maximum_duration_s"]=10
        first["rest"].update(after_charge_s=0.4,after_discharge_s=0.4)
        if measured:
            first["rest"].update(mode="zero_current_logging", maximum_abs_current_a=0.001)
        second=deepcopy(first);second["instrument"]["channel"]=2;second["dut"]["specimen_id"]="cell_two"
        docs=[first,second];plan=build_dual_constant_power_sweep_plan(docs)
        driver=TraceWire(plan["resource"])
        session=DualInstrumentSession(build_dual_plan([p["initial_preparation"]["configuration"] for p in plan["channels"]]),
            "TESTSERIAL",smu_factory=lambda *args,**kwargs:driver)
        session.connect()
        owner=SharedPowerSession(docs,session=session,clock=driver.clock.now,output_authorized=True)
        requests={c:build_sequence(plan["channels"][c-1]["configuration"])[1] for c in (1,2)}
        def request(c,kind):return next(r for r in requests[c] if r["phase"]["type"]==kind)
        def native(c=1,kind="discharge_cc",name="native"):
            return owner.borrow_native(c,request(c,kind),tmp_path/name)
        def cp(c=2):
            endpoint=owner.borrow(c,PowerLimits(**plan["channels"][c-1]["conditions"][0]["controller_limits"]),maximum_probe_current_a=0.001)
            endpoint.prepare();endpoint.set_output(True);return endpoint
        return docs,driver,owner,native,cp,request
    return build


def complete(task,driver):
    phase=task.phase;c=task.channel
    if phase["type"]=="charge_cc_cv":
        target=phase["target_voltage_v"];current=phase["termination_current_a"]*0.5
        driver.samples[c]=[(target,current,100+i*0.2,0) for i in range(5)]
        driver.clock.sleep(1.0)
    else:
        driver.samples[c]=[(3.0,-phase["current_magnitude_a"],100,1),
                           (phase["cutoff_voltage_v"],-phase["current_magnitude_a"],100.2,1)]
        driver.clock.sleep(0.25)
    assert task.poll() is None
    return task.poll()


@pytest.mark.parametrize("channel",[1,2])
@pytest.mark.parametrize("kind",["charge_cc_cv","discharge_cc"])
def test_actual_native_active_phase_completes_without_changing_cp_peer(rig,channel,kind):
    docs,wire,owner,native,cp,_=rig()
    with owner:
        peer=cp(3-channel);peer.set_current(-0.1)
        before=owner._fingerprint(3-channel);mark=len(wire.commands)
        phase=native(channel,kind);phase.start()
        assert peer.read_fresh().channel==3-channel
        outcome=complete(phase,wire)
        assert outcome["state"]=="completed" and phase.state=="completed"
        assert owner._fingerprint(3-channel)==before
        assert wire.shutdown_count==0 and wire.close_count==0
        validation=validate_borrowed_native_stage(phase.directory,docs[channel-1],phase.request)
        assert validation["evidence"]["kind"]=="incremental_csv"
        writes=[c for c in wire.commands[mark:] if "?" not in c and c!="CHECK_ERRORS"]
        assert not any(c.startswith((":FORM:",":SYST:")) or "(@1,2)" in c for c in writes)
        assert not any(c.startswith((f":OUTP{3-channel}",f":TRAC{3-channel}",f":TRIG{3-channel}",f":ARM{3-channel}")) for c in writes)
    assert wire.shutdown_count==1 and wire.close_count==1 and wire.connect_count==1


def test_outputs_off_rest_is_channel_local_and_cannot_complete_early(rig):
    docs,wire,owner,native,cp,_=rig()
    with owner:
        peer=cp();rest=native(kind="rest");rest.start()
        assert rest.poll() is None and peer.get_output_state() is True
        wire.clock.sleep(0.2);assert rest.poll() is None
        wire.clock.sleep(0.21);assert rest.poll()["stop_reason"]=="rest_elapsed"
        assert peer.get_output_state() is True
        validate_borrowed_native_stage(rest.directory,docs[0],rest.request)


def test_zero_current_rest_uses_actual_recorder_and_preserves_peer(rig):
    docs,wire,owner,native,cp,_=rig(measured=True)
    with owner:
        peer=cp();rest=native(kind="rest");rest.start()
        assert rest.poll() is None
        wire.clock.sleep(0.21);assert rest.poll() is None
        wire.clock.sleep(0.21);assert rest.poll()["state"]=="completed"
        doc=validate_borrowed_native_stage(rest.directory,docs[0],rest.request)
        assert doc["evidence"]["kind"]=="measured_rest" and peer.get_output_state() is True
        assert (rest.directory/"rest"/"measurements.csv").is_file()


def test_active_lease_cannot_overlap_cp_on_same_channel(rig,tmp_path):
    _,wire,owner,native,_,_=rig()
    with pytest.raises(RuntimeError,match="already"):
        with owner:
            phase=native();phase.start()
            limits=PowerLimits(**owner._profiles[1]["conditions"][0]["controller_limits"])
            owner.borrow(1,limits,maximum_probe_current_a=0.001)
    assert wire.close_count==1 and phase.state=="aborted"
    assert read_json(phase.directory/"stage.json")["state"]=="raised"


def test_completed_native_lease_releases_channel_for_next_native_or_cp(rig,tmp_path):
    _,wire,owner,native,_,_=rig()
    with owner:
        first=native();first.start();complete(first,wire)
        second=native(kind="rest",name="second");second.start()
        wire.clock.sleep(0.41);second.poll()
        limits=PowerLimits(**owner._profiles[1]["conditions"][0]["controller_limits"])
        power=owner.borrow(1,limits,maximum_probe_current_a=0.001);power.prepare();power.set_output(True)
    assert wire.close_count==1


def test_native_fault_stops_both_before_failure_recording(rig,monkeypatch):
    _,wire,owner,native,cp,_=rig()
    original=OSError("native trace query")
    with pytest.raises(OSError) as caught:
        with owner:
            cp();phase=native();phase.start()
            source=phase.task.abort_after_stop
            def observed(*args):
                assert wire.close_count==1 and wire.shutdown_count==1
                return source(*args)
            monkeypatch.setattr(phase.task,"abort_after_stop",observed)
            wire.failures[":STAT:QUES:COND?"]=original
            phase.poll()
    assert caught.value is original and phase.task.run.writer.closed
    assert read_json(phase.directory/"stage.json")["state"]=="raised"
    assert not (phase.directory/MARKER).exists() and wire.close_count==1


def test_cp_fault_also_finalizes_native_peer_without_extra_driver_io(rig):
    _,wire,owner,native,cp,_=rig()
    with pytest.raises(RuntimeError):
        with owner:
            power=cp();phase=native();phase.start()
            wire.status[2]=9
            power.read_fresh()
    assert phase.state=="aborted" and phase.task.run.writer.closed
    assert wire.commands[-1]=="CLOSE" and wire.close_count==1


def test_body_interrupt_is_preserved_and_both_native_recorders_finalize(rig):
    _,wire,owner,native,_,_=rig()
    original=KeyboardInterrupt("operator")
    with pytest.raises(KeyboardInterrupt) as caught:
        with owner:
            first=native(1,name="one");second=native(2,name="two")
            first.start();second.start();raise original
    assert caught.value is original and wire.close_count==1
    for phase in (first,second):
        assert phase.task.run.writer.closed
        assert read_json(phase.directory/"stage.json")["state"]=="raised"


def test_normal_owner_exit_cannot_silently_abandon_active_native_phase(rig):
    _,wire,owner,native,_,_=rig()
    with pytest.raises(RuntimeError,match="before native stage completed"):
        with owner:
            phase=native();phase.start()
    assert phase.state=="aborted" and wire.close_count==1


def test_caught_native_fault_is_reraised_at_owner_exit(rig):
    _,wire,owner,native,_,_=rig()
    original=OSError("native read")
    with pytest.raises(OSError) as caught:
        with owner:
            phase=native();phase.start();wire.failures[":STAT:QUES:COND?"]=original
            try:phase.poll()
            except OSError:pass
    assert caught.value is original and wire.close_count==1


def test_invalid_request_does_not_prepare_native_output(rig,tmp_path):
    _,wire,owner,_,_,request=rig()
    with pytest.raises(RuntimeError,match="canonical"):
        with owner:
            wrong=deepcopy(request(1,"discharge_cc"));wrong["phase"]["current_magnitude_a"]*=2
            mark=len(wire.commands)
            owner.borrow_native(1,wrong,tmp_path/"bad")
    assert ":OUTP1 ON" not in wire.commands[mark:] and not (tmp_path/"bad").exists()


def test_cp_request_is_not_accepted_as_native_phase(rig,tmp_path):
    _,wire,owner,_,_,request=rig()
    with pytest.raises(RuntimeError,match="CP phases"):
        with owner:owner.borrow_native(1,request(1,"discharge_cp"),tmp_path/"bad")
    assert wire.close_count==1


def test_native_callback_wrong_thread_is_rejected_without_io(rig):
    _,wire,owner,native,_,_=rig()
    with owner:
        phase=native();before=list(wire.commands)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(RuntimeError,match="controlling thread"):pool.submit(phase.start).result()
        assert wire.commands==before and phase.state=="new"
        phase.start();complete(phase,wire)


@pytest.mark.parametrize("change",["csv","marker","request","channel","timing","file_list"])
def test_completed_native_stage_rejects_corruption(rig,change):
    docs,wire,owner,native,_,_=rig()
    with owner:
        phase=native();phase.start();complete(phase,wire)
    if change=="csv":
        with (phase.directory/"phase"/"measurements.csv").open("a") as stream:stream.write("bad\n")
    elif change=="marker":(phase.directory/MARKER).write_text("wrong\n")
    else:
        doc=read_json(phase.directory/"stage.json")
        if change=="request":doc["request"]["stage_id"]="other"
        elif change=="channel":doc["channel"]=2
        elif change=="timing":doc["finished_s"]=-1
        elif change=="file_list":doc["files"]={}
        write_json(phase.directory/"stage.json",doc)
        (phase.directory/MARKER).write_text("SHARED_NATIVE_STAGE sha256="+digest(phase.directory/"stage.json")+"\n")
    with pytest.raises((RuntimeError,ValueError)):
        validate_borrowed_native_stage(phase.directory,docs[0],phase.request)


def test_measured_rest_fault_closes_stream_after_owner_stop(rig):
    _,wire,owner,native,cp,_=rig(measured=True)
    with pytest.raises(RuntimeError):
        with owner:
            cp();rest=native(kind="rest");rest.start();wire.measure_current[1]=0.1;rest.poll()
    assert rest.task.stream.closed and rest.state=="aborted" and wire.close_count==1
    assert wire.commands[-1]=="CLOSE"


def test_native_deadline_fault_is_global_but_completion_is_channel_local(rig):
    _,wire,owner,native,cp,_=rig()
    with pytest.raises(TimeoutError):
        with owner:
            cp();phase=native();phase.start()
            wire.clock.sleep(11);phase.check_deadline()
    assert phase.state=="aborted" and wire.close_count==1



def test_native_fault_status_record_is_retained(rig):
    _,wire,owner,native,_,_=rig()
    with pytest.raises((RuntimeError,ValueError)) as caught:
        with owner:
            phase=native();phase.start()
            wire.samples[1]=[(3.0,-phase.phase["current_magnitude_a"],100.0,9)]
            phase.poll()
    assert caught.value.partial_readings[0].status==9
    assert phase.task.run.writer.closed and wire.close_count==1
    assert not (phase.directory/MARKER).exists()


def test_native_failure_metadata_error_is_secondary_and_reported(rig,monkeypatch):
    _,wire,owner,native,_,_=rig()
    original=OSError("original acquisition")
    with pytest.raises(OSError) as caught:
        with owner:
            phase=native();phase.start()
            def broken(*args):raise OSError("secondary metadata")
            monkeypatch.setattr(phase.task.run,"fail",broken)
            wire.failures[":STAT:QUES:COND?"]=original
            phase.poll()
    assert caught.value is original and phase.task.run.writer.closed
    assert any(row["operation"]=="finalize_native_channel_1" for row in original.shared_power_cleanup_report["errors"])
    assert wire.close_count==1


def test_marker_write_failure_cannot_release_phase_as_success(rig,monkeypatch):
    from pathlib import Path
    _,wire,owner,native,_,_=rig()
    original=OSError("marker write")
    source=Path.open
    def opening(path,*args,**kwargs):
        if path.name==MARKER and args and args[0]=="x":raise original
        return source(path,*args,**kwargs)
    with pytest.raises(OSError) as caught:
        with owner:
            phase=native();phase.start();monkeypatch.setattr(Path,"open",opening)
            complete(phase,wire)
    assert caught.value is original and phase.state=="aborted"
    assert read_json(phase.directory/"stage.json")["state"]=="raised" and not (phase.directory/MARKER).exists()
    assert wire.close_count==1


def test_backwards_host_clock_fails_both_and_retains_native_record(rig):
    _,wire,owner,native,cp,_=rig()
    with pytest.raises(RuntimeError,match="backwards"):
        with owner:
            cp();phase=native();phase.start()
            wire.clock.sleep(1);owner._time();wire.clock.time-=0.5
            phase.poll()
    assert phase.state=="aborted" and wire.close_count==1


def test_completed_outer_stage_binds_native_outcome_identity(rig):
    docs,wire,owner,native,_,_=rig()
    with owner:
        phase=native();phase.start();complete(phase,wire)
    doc=read_json(phase.directory/"stage.json");doc["evidence"]["outcome"]["phase_index"]+=1
    write_json(phase.directory/"stage.json",doc)
    (phase.directory/MARKER).write_text("SHARED_NATIVE_STAGE sha256="+digest(phase.directory/"stage.json")+"\n")
    with pytest.raises(RuntimeError,match="outcome identity"):
        validate_borrowed_native_stage(phase.directory,docs[0],phase.request)


def test_outputs_off_rest_unexpected_on_is_global_failure(rig):
    _,wire,owner,native,cp,_=rig()
    with pytest.raises(RuntimeError,match="Outputs-off rest"):
        with owner:
            cp();phase=native(kind="rest");phase.start()
            wire.responses[":OUTP1?"]="1";phase.poll()
    assert wire.close_count==1 and phase.state=="aborted"



def test_native_borrow_does_no_driver_io_and_unused_lease_creates_no_files(rig):
    _,wire,owner,native,cp,_=rig()
    with owner:
        cp();before=list(wire.commands)
        phase=native()
        assert wire.commands==before and not phase.directory.exists()
    assert not phase.directory.exists() and wire.connect_count==1


def test_changed_session_plan_is_rejected_before_native_output_activation(rig):
    _,wire,owner,native,cp,_=rig()
    with pytest.raises(RuntimeError,match="session plan changed"):
        with owner:
            cp();phase=native();owner.session.plan["resource"]="DIFFERENT::INSTR"
            mark=len(wire.commands);phase.start()
    assert ":OUTP1 ON" not in wire.commands[mark:] and wire.close_count==1
