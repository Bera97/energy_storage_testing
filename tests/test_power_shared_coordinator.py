"""Local-only scripted-wire tests of the physical-capable coordinator; no DUT."""
from copy import deepcopy
import json
import re
from pathlib import Path
import pytest
from experiments.constant_power import PowerLimits
from experiments.dual_instrument_io import DualInstrumentSession
from experiments.dual_preconditioning import build_dual_plan
from experiments.power_planning import build_dual_constant_power_sweep_plan
from experiments.power_shared_transport import SharedPowerSession, BorrowedPowerChannel
from experiments.power_shared_coordinator import MANIFEST, run_shared_power_sweep
from experiments.power_sweep_contract import read_json
from test_charge_current_sweep import profile
from test_constant_power_planning import power_profile
from test_power_shared_phases import TraceWire


class ScriptWire(TraceWire):
    def __init__(self,resource,documents):
        super().__init__(resource)
        self.documents = {d["instrument"]["channel"]:d for d in documents}
        self.owner = None
        self.acquisitions = {}
        self.cp_samples = {1:0,2:0}
        self.hook = None
    def _touch(self,command):
        super()._touch(command)
        if self.hook is not None:self.hook(self,command)
    def _refresh(self,c):
        acquisition = self.acquisitions.get(c)
        if acquisition is None or not acquisition["active"]:return
        interval = float(self.responses[f":TRIG{c}:ACQ:TIM?"])
        count = min(int(self.responses[f":TRIG{c}:ACQ:COUN?"]),int((self.clock.time-acquisition["started"])/interval))
        document = self.documents[c]
        mode = self.responses[f":SOUR{c}:FUNC:MODE?"]
        while len(self.samples[c])<count:
            i = len(self.samples[c])
            if mode=="VOLT":
                voltage = float(self.responses[f":SOUR{c}:VOLT?"])
                current = 0.005
                status = 0
            else:
                voltage = 3.0 if i==0 else document["discharge"]["cutoff_voltage_v"]
                current = float(self.responses[f":SOUR{c}:CURR?"])
                status = 1
            self.samples[c].append((voltage,current,100+acquisition["started"]+(i+1)*interval,status))
    def _write(self,command):
        abort = re.fullmatch(r":ABOR:(?:ALL|ACQ) \(@([12])\)",command)
        if abort:
            c = int(abort[1]);self._refresh(c)
            if c in self.acquisitions:self.acquisitions[c]["active"] = False
        super()._write(command)
        start = re.fullmatch(r":INIT:ACQ \(@([12])\)",command)
        if start:self.acquisitions[int(start[1])] = {"started":self.clock.time,"active":True}
        on = re.fullmatch(r":OUTP([12]) ON",command)
        if on:self.cp_samples[int(on[1])] = 0
    def _query(self,command):
        trace = re.match(r":TRAC([12]):(?:POIN:ACT|DATA)\?",command)
        if trace:self._refresh(int(trace[1]))
        if command.startswith(":MEAS? "):
            c = int(command.split("(@")[1][0]);lease = None if self.owner is None else self.owner._channels.get(c)
            if isinstance(lease,BorrowedPowerChannel):
                self.cp_samples[c] += 1
                self.measure_voltage[c] = 3.0 if self.cp_samples[c]<3 else lease.limits.cutoff_voltage_v
            else:
                self.measure_voltage[c] = 3.0
        return super()._query(command)


@pytest.fixture
def setup(profile,tmp_path):
    def build(*,measured=False,asymmetric=True,authorized=True,clock=None,sleep=None,skew=False):
        first = power_profile(profile)
        first["execution"] = {"expected_serial":"TESTSERIAL","maximum_voltage_v":3.81,"maximum_current_a":0.4}
        first["sweep"].update(power_w=[0.4],repeats=1)
        first["charge"].update(maximum_duration_s=10,maximum_cv_duration_s=5)
        first["charge"]["termination"]["hold_time_s"] = 0.6
        first["discharge"]["maximum_duration_s"] = 10
        first["rest"].update(after_charge_s=0.4,after_discharge_s=0.4)
        if measured:first["rest"].update(mode="zero_current_logging",maximum_abs_current_a=0.001)
        second = deepcopy(first)
        second["instrument"]["channel"] = 2;second["dut"]["specimen_id"] = "cell_two"
        if asymmetric:second["sweep"]["power_w"] = [0.4,0.8]
        if skew:second["charge"]["termination"]["hold_time_s"] = 1.3
        docs = [first,second]
        plan = build_dual_constant_power_sweep_plan(docs)
        wire = ScriptWire(plan["resource"],docs)
        session = DualInstrumentSession(build_dual_plan([p["initial_preparation"]["configuration"] for p in plan["channels"]]),
            "TESTSERIAL",smu_factory=lambda *args,**kwargs:wire)
        session.connect()
        owner = SharedPowerSession(docs,session=session,clock=wire.clock.now if clock is None else clock,output_authorized=authorized)
        wire.owner = owner
        args = dict(sleep=wire.clock.sleep if sleep is None else sleep,maximum_probe_current_a={1:0.001,2:0.001},execute_requested=True)
        def run(name="acquisition",**changes):
            return run_shared_power_sweep(owner,tmp_path/name,**{**args,**changes})
        return docs,wire,owner,run,tmp_path/"acquisition"
    return build


def journal(root):
    return [json.loads(line) for line in (root/"events.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("measured",[False,True])
def test_complete_native_shaped_dual_acquisition_is_not_replay_qualified(setup,measured):
    _,wire,owner,run,root = setup(measured=measured)
    report = run()
    assert report["state"]=="acquisition_completed_unreplayed" and report["completion_qualified"] is False
    assert report["whole_sweep_replay_available"] is False and report["physical_qualification"] is False
    assert [c["completed_stage_count"] for c in report["channels"]]==[6,10]
    assert report["owner_cleanup"]["outputs_reported_off"] and report["journal_closed"]
    assert wire.connect_count==1 and wire.shutdown_count==1 and wire.close_count==1 and owner.closed
    assert not (root/"COMPLETE").exists() and not (root/"POWER_SWEEP_COMPLETE").exists()
    assert read_json(root/MANIFEST)==report
    rows = journal(root)
    assert [r["sequence"] for r in rows]==list(range(len(rows)))
    assert report["journal_rows"]==len(rows)
    assert any(r["kind"]=="power_feedback" for r in rows)
    assert any(r["kind"]=="current_acknowledged" for r in rows)
    assert all(s["stop_verified"] for c in report["channels"] for s in c["stages"])


def test_initial_preparation_barrier_is_enforced_before_either_charge(setup):
    _,_,_,run,root = setup()
    run();rows = journal(root)
    barrier = next(r["sequence"] for r in rows if r["kind"]=="initial_preparation_barrier_released")
    for c in (1,2):
        for stage in ("stage_00000","stage_00001"):
            assert next(r["sequence"] for r in rows if r["kind"]=="stage_acquisition_finished" and r["channel"]==c and r["stage_id"]==stage)<barrier
    assert all(r["sequence"]>barrier for r in rows if r["kind"]=="stage_start" and r["data"]["request"]["scope"]=="condition")


def test_channels_progress_independently_after_barrier_and_finish_separately(setup):
    _,_,_,run,root = setup()
    run();rows = journal(root)
    one_done = max(r["sequence"] for r in rows if r["channel"]==1)
    assert any(r["channel"]==2 and r["sequence"]>one_done for r in rows)
    starts = [r for r in rows if r["kind"]=="stage_start"]
    assert [r["stage_id"] for r in starts if r["channel"]==1]==[f"stage_{i:05d}" for i in range(6)]
    assert [r["stage_id"] for r in starts if r["channel"]==2]==[f"stage_{i:05d}" for i in range(10)]


@pytest.mark.parametrize("change",["authorization","execute","probes","budget","interval"])
def test_preflight_rejection_leaves_session_unclaimed_and_creates_no_files(setup,change):
    _,wire,owner,run,root = setup(authorized=change!="authorization")
    args = {}
    if change=="execute":args["execute_requested"] = False
    elif change=="probes":args["maximum_probe_current_a"] = {1:0.001}
    elif change=="budget":args["maximum_scheduler_steps"] = 0
    elif change=="interval":args["poll_interval_s"] = True
    before = list(wire.commands)
    with pytest.raises((RuntimeError,ValueError)):run(**args)
    assert wire.commands==before and wire.close_count==0 and not owner.claimed and not root.exists()


def test_scheduler_budget_fault_stops_both_and_preserves_partial_diagnostics(setup):
    _,wire,owner,run,root = setup()
    with pytest.raises(RuntimeError,match="scheduler budget") as caught:run(maximum_scheduler_steps=3)
    report = caught.value.shared_power_acquisition_report
    assert report["state"]=="raised" and report["owner_cleanup"]["outputs_reported_off"]
    assert wire.close_count==1 and owner.closed and read_json(root/MANIFEST)["state"]=="raised"
    assert all(c["state"]=="aborted" for c in report["channels"])


def test_nonadvancing_sleep_is_terminal(setup):
    _,wire,_,run,_ = setup(sleep=lambda duration:None)
    with pytest.raises(RuntimeError,match="Sleep did not advance"):run()
    assert wire.close_count==1


def test_feedback_fault_retains_paired_frame_and_closes_native_peer(setup):
    _,wire,_,run,root = setup()
    def fault(driver,command):
        if command.startswith(":MEAS?"):
            c = int(command.split("(@")[1][0])
            if isinstance(driver.owner._channels.get(c),BorrowedPowerChannel):driver.status[c] = 9
    wire.hook = fault
    with pytest.raises(RuntimeError) as caught:run()
    report = caught.value.shared_power_acquisition_report
    assert wire.close_count==1 and report["state"]=="raised"
    rows = [r for r in journal(root) if r["kind"]=="power_feedback_fault"]
    assert rows and rows[0]["data"]["frame"]["status"]==9
    assert any(c["interrupted_stage"] is not None for c in report["channels"])



def test_slow_native_peer_callback_trips_cp_watchdog_on_return(setup):
    _,wire,_,run,root = setup(skew=True)
    injected = False
    def stall(driver,command):
        nonlocal injected
        cp = driver.owner._channels.get(1)
        native = driver.owner._channels.get(2)
        if not injected and command.startswith(":TRAC2:") and isinstance(cp,BorrowedPowerChannel) and cp.power_command_applied and native is not None:
            injected = True
            driver.clock.sleep(driver.owner._profiles[1]["configuration"]["power_control"]["maximum_feedback_age_s"]+0.1)
    wire.hook = stall
    with pytest.raises(RuntimeError,match="stale") as caught:run()
    assert injected and wire.close_count==1 and caught.value.shared_power_acquisition_report["state"]=="raised"
    assert read_json(root/MANIFEST)["completion_qualified"] is False


def test_slow_journal_flush_is_guarded_and_raw_feedback_survives(setup,monkeypatch):
    _,wire,_,run,root = setup()
    original = Path.open
    delayed = False
    class SlowJournal:
        def __init__(self,stream):self.stream=stream;self.last=None
        def write(self,text):self.last=json.loads(text);return self.stream.write(text)
        def flush(self):
            nonlocal delayed
            self.stream.flush()
            if not delayed and self.last is not None and self.last["kind"]=="power_feedback":
                delayed=True;wire.clock.sleep(wire.owner._profiles[1]["configuration"]["power_control"]["maximum_feedback_age_s"]+0.1)
        def close(self):return self.stream.close()
    def opening(path,*args,**kwargs):
        result=original(path,*args,**kwargs)
        return SlowJournal(result) if path.name=="events.jsonl" and args and args[0]=="x" else result
    monkeypatch.setattr(Path,"open",opening)
    with pytest.raises(RuntimeError,match="stale"):run()
    assert delayed and wire.close_count==1
    assert any(row["kind"]=="power_feedback" for row in journal(root))


def test_increasing_clock_on_every_call_does_not_create_stale_controller_arguments(setup):
    _,wire,owner,run,_=setup(asymmetric=False)
    def ticking():
        wire.clock.time+=0.000001
        return wire.clock.time
    owner.clock=ticking
    assert run()["state"]=="acquisition_completed_unreplayed"


def test_original_interrupt_survives_secondary_shutdown_failure(setup):
    _,wire,_,run,root=setup()
    original=KeyboardInterrupt("operator")
    fired=False
    def interrupt(driver,command):
        nonlocal fired
        if not fired and command.startswith(":MEAS?"):
            fired=True;driver.failures["GLOBAL_SHUTDOWN"]=OSError("secondary shutdown");raise original
    wire.hook=interrupt
    with pytest.raises(KeyboardInterrupt) as caught:run()
    assert caught.value is original and wire.close_count==1
    assert original.shared_power_acquisition_report["owner_cleanup"]["errors"]
    assert read_json(root/MANIFEST)["state"]=="raised"


def test_partial_journal_write_failure_is_not_experimental_success(setup,monkeypatch):
    _,wire,_,run,root=setup()
    original=Path.open
    class BrokenJournal:
        def __init__(self,stream):self.stream=stream;self.writes=0
        def write(self,text):
            self.writes+=1
            if self.writes>=5:raise OSError("journal write failed")
            return self.stream.write(text)
        def flush(self):return self.stream.flush()
        def close(self):return self.stream.close()
    def opening(path,*args,**kwargs):
        result=original(path,*args,**kwargs)
        return BrokenJournal(result) if path.name=="events.jsonl" and args and args[0]=="x" else result
    monkeypatch.setattr(Path,"open",opening)
    with pytest.raises(OSError,match="journal write failed") as caught:run()
    assert wire.close_count==1 and caught.value.shared_power_acquisition_report["state"]=="raised"
    assert not (root/"COMPLETE").exists()


def test_journal_close_failure_is_propagated_after_one_owner_cleanup(setup,monkeypatch):
    _,wire,_,run,_=setup(asymmetric=False)
    original=Path.open
    class CloseError:
        def __init__(self,stream):self.stream=stream
        def write(self,text):return self.stream.write(text)
        def flush(self):return self.stream.flush()
        def close(self):self.stream.close();raise OSError("journal close failed")
    def opening(path,*args,**kwargs):
        result=original(path,*args,**kwargs)
        return CloseError(result) if path.name=="events.jsonl" and args and args[0]=="x" else result
    monkeypatch.setattr(Path,"open",opening)
    with pytest.raises(OSError,match="journal close failed") as caught:run()
    assert wire.close_count==1 and caught.value.shared_power_acquisition_report["journal_closed"] is False


def test_output_on_at_entry_causes_cleanup_not_continued_preparation(setup):
    _,wire,_,run,_=setup();wire.responses[":OUTP1?"]="1"
    with pytest.raises(RuntimeError,match="Both outputs"):run()
    assert ":SYST:GRO:DEF (@1),(@2)" not in wire.commands and wire.close_count==1


def test_completed_owner_cannot_be_reused(setup):
    _,wire,_,run,root=setup(asymmetric=False)
    run();before=list(wire.commands)
    with pytest.raises(RuntimeError,match="Fresh unclaimed"):run(name="second")
    assert wire.commands==before and not (root.parent/"second").exists()


def test_changed_plan_rejected_without_io_or_directory_creation(setup):
    _,wire,owner,run,root=setup();owner.plan["resource"]="DIFFERENT::INSTR"
    before=list(wire.commands)
    with pytest.raises(RuntimeError,match="Owner plan changed"):run()
    assert wire.commands==before and not owner.claimed and not root.exists()


def test_current_acknowledgement_mismatch_is_terminal(setup):
    _,wire,_,run,_=setup()
    injected=False
    def mismatch(driver,command):
        nonlocal injected
        if command.startswith(":SOUR1:CURR?"):
            lease=driver.owner._channels.get(1)
            if not injected and isinstance(lease,BorrowedPowerChannel) and lease.power_command_applied:
                injected=True;driver.responses[":SOUR1:CURR?"]="-0.1"
    wire.hook=mismatch
    with pytest.raises(RuntimeError,match="readback mismatch"):run()
    assert injected and wire.close_count==1


def test_no_implicit_connection_factory_is_used(setup,monkeypatch):
    import experiments.dual_instrument_io as io
    _,wire,_,run,_=setup(asymmetric=False)
    def forbidden(*args,**kwargs):pytest.fail("Unexpected connection factory")
    monkeypatch.setattr(io,"_new_smu",forbidden)
    run();assert wire.connect_count==1



def test_primary_error_retains_known_on_cleanup_state(setup):
    _,wire,_,run,_=setup()
    original=OSError("primary measurement")
    fired=False
    def fault(driver,command):
        nonlocal fired
        if not fired and command.startswith(":MEAS?"):
            fired=True
            driver.failures["GLOBAL_SHUTDOWN"]=OSError("shutdown failed")
            driver.failures[":OUTP1 OFF"]=OSError("output OFF failed")
            raise original
    wire.hook=fault
    with pytest.raises(OSError) as caught:run()
    assert caught.value is original and wire.close_count==1
    cleanup=original.shared_power_acquisition_report["owner_cleanup"]
    assert cleanup["channels"][0]["output_state"] is True and cleanup["outputs_reported_off"] is False


def test_nonfinite_clock_still_allows_independent_cleanup(setup):
    _,wire,_,run,_=setup()
    fired=False
    def fault(driver,command):
        nonlocal fired
        if not fired and command.startswith(":TRAC1:DATA?"):
            fired=True;driver.clock.time=float("nan")
    wire.hook=fault
    with pytest.raises(ValueError) as caught:run()
    assert fired and wire.close_count==1
    assert caught.value.shared_power_acquisition_report["state"]=="raised"


def test_clock_moving_backwards_is_terminal(setup):
    _,wire,_,run,_=setup()
    fired=False
    def backwards(driver,command):
        nonlocal fired
        if not fired and command.startswith(":TRAC1:DATA?"):
            fired=True;driver.clock.time-=0.1
    wire.hook=backwards
    with pytest.raises(RuntimeError,match="backwards"):run()
    assert fired and wire.close_count==1


def test_failed_current_write_remains_primary_and_has_no_ack(setup):
    _,wire,_,run,root=setup()
    original=OSError("current write failed")
    fired=False
    def fault(driver,command):
        nonlocal fired
        lease=driver.owner._channels.get(1)
        if not fired and command.startswith(":SOUR1:CURR -") and isinstance(lease,BorrowedPowerChannel):
            fired=True;raise original
    wire.hook=fault
    with pytest.raises(OSError) as caught:run()
    assert caught.value is original and wire.close_count==1
    rows=journal(root)
    assert any(r["kind"]=="current_requested" and r["channel"]==1 for r in rows)
    assert not any(r["kind"]=="current_acknowledged" and r["channel"]==1 for r in rows)


def test_failed_final_manifest_export_does_not_claim_completed_run(setup,monkeypatch):
    import experiments.power_shared_coordinator as module
    _,wire,_,run,root=setup(asymmetric=False)
    original=module.write_json
    def write(path,document,**kwargs):
        if Path(path).name==MANIFEST and not kwargs.get("create",False):raise OSError("final manifest failed")
        return original(path,document,**kwargs)
    monkeypatch.setattr(module,"write_json",write)
    with pytest.raises(OSError,match="final manifest failed") as caught:run()
    assert caught.value.shared_power_acquisition_report["state"]=="raised" and wire.close_count==1
    assert read_json(root/MANIFEST)["state"]!="acquisition_completed_unreplayed"


def test_existing_output_directory_is_not_reused_or_overwritten(setup):
    _,wire,owner,run,root=setup()
    root.mkdir();(root/"keep.txt").write_text("existing evidence")
    before=list(wire.commands)
    with pytest.raises(RuntimeError,match="Fresh nonsymlink"):run()
    assert wire.commands==before and not owner.claimed and (root/"keep.txt").read_text()=="existing evidence"


def test_public_cli_still_refuses_physical_cp_dispatch(setup,tmp_path,capsys):
    import smu,yaml
    docs,wire,_,_,_=setup()
    paths=[]
    for c,doc in enumerate(docs):
        path=tmp_path/f"channel{c}.yaml";path.write_text(yaml.safe_dump(doc));paths.extend(["--config",str(path)])
    before=list(wire.commands)
    from unittest import mock
    with mock.patch("experiments.power_dual_execution.run_configured_dual_power_sweep",
                    return_value=0) as launcher:
        assert smu.main([*paths,"--execute"]) == 0
    assert launcher.call_count == 1
    assert wire.commands==before


def test_wrong_owner_protocol_cannot_be_used_as_physical_adapter(tmp_path):
    with pytest.raises(RuntimeError,match="SharedPowerSession"):
        run_shared_power_sweep(object(),tmp_path/"never",sleep=lambda duration:None,
                              maximum_probe_current_a={1:0.001,2:0.001},execute_requested=True)
    assert not (tmp_path/"never").exists()



def test_successful_skewed_native_and_cp_controllers_really_overlap(setup):
    _,wire,_,run,_=setup(skew=True)
    overlap=False
    def observe(driver,command):
        nonlocal overlap
        one=driver.owner._channels.get(1);two=driver.owner._channels.get(2)
        if isinstance(one,BorrowedPowerChannel) and one.power_command_applied and two is not None and not isinstance(two,BorrowedPowerChannel):
            if command.startswith(":TRAC2:"):overlap=True
    wire.hook=observe
    report=run()
    assert overlap and report["state"]=="acquisition_completed_unreplayed"
    assert wire.shutdown_count==1 and wire.close_count==1


def test_directory_creation_race_does_not_overwrite_foreign_manifest(setup,monkeypatch):
    _,wire,owner,run,root=setup()
    original=Path.mkdir
    def racing(path,*args,**kwargs):
        if path==root:
            original(path,*args,**kwargs)
            (root/MANIFEST).write_text('{"state":"foreign"}')
            raise FileExistsError("another run won directory creation")
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,"mkdir",racing)
    before=list(wire.commands)
    with pytest.raises(FileExistsError):run()
    assert read_json(root/MANIFEST)=={"state":"foreign"}
    assert wire.commands==before and not owner.claimed
