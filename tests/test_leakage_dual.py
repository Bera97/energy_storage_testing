"""Leakage hold on both channels: runner, output enable, execution wiring and command line."""
from copy import deepcopy
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import types
import pytest
import smu
import test_charge_current_sweep
from acquisition.spot_acquisition import SpotAcquisition
from analysis import leakage_analysis
from experiments import leakage_dual_execution
from experiments.charge_setup import configure_charge_source
from experiments.leakage_dual_runner import (
    BATCH_NAME, MARKER_NAME, enable_dual_dc_output, run_dual_leakage_test,
    validate_dual_documents,
)
from experiments.leakage_planning import build_leakage_plan
from experiments.leakage_ranging import StepDownRanger, apply_current_range, enable_auto_range
from experiments.leakage_runner import CSV_NAME, METADATA_NAME, LeakageRunError
from experiments.settings import settings_for
from test_leakage_planning import save
from test_leakage_runner import FakeTime, reading, small_doc
profile = test_charge_current_sweep.profile
OFFSETS = [0.0, 0.5, 1.0, 1.5, 2.0, 4.0, 6.0, 8.0, 10.0]
NAME = "20261127_200000_dual_leak"
EARLY_STOP = {"enabled": True, "minimum_duration_s": 6, "window_s": 2, "relative_change": 0.01,
              "check_interval_s": 1, "consecutive_checks": 3}
def pair(base, **extra):
    first = small_doc(base, **extra)
    second = deepcopy(first)
    second["instrument"]["channel"] = 2
    second["dut"]["specimen_id"] = "second_specimen"
    return [first, second]
class FakeChannel:
    def __init__(self, smu, number):
        self.smu, self.channel = smu, number
    def get_output_state(self):
        return self.smu.on[self.channel]
class FakeSMU:
    def __init__(self, fail_shutdown=False):
        self.on = {1: False, 2: False}
        self.fail_shutdown = fail_shutdown
        self.events = []
        self.chan1, self.chan2 = FakeChannel(self, 1), FakeChannel(self, 2)
    def shutdown(self):
        self.events.append("shutdown")
        if self.fail_shutdown:
            raise RuntimeError("shutdown boom")
        self.on = {1: False, 2: False}
class FakeSpot:
    def __init__(self, rig, channel):
        self.rig, self.channel = rig, channel
    def configure(self, nplc):
        self.nplc = nplc
    def read(self):
        self.rig.timing.now += self.rig.read_cost
        self.rig.reads[self.channel] += 1
        self.rig.read_log.append(self.channel)
        return self.rig.behavior(self.rig, self.channel)
class DualRig:
    def __init__(self, documents, behavior=None, read_cost=0.05, fail_shutdown=False):
        plan = build_leakage_plan(documents[0])
        self.documents = documents
        self.hold = plan["hold_voltage_v"]
        self.compliance = plan["compliance_current_a"]
        self.vmax = documents[0]["dut"]["ratings"]["maximum_voltage_v"]
        self.timing = FakeTime()
        self.smu = FakeSMU(fail_shutdown)
        self.events = self.smu.events
        self.behavior = behavior or (lambda rig, channel: rig.nominal(channel))
        self.read_cost = read_cost
        self.reads = {1: 0, 2: 0}
        self.read_log = []
        self.calls = []
        self.restores = []
    def nominal(self, channel):
        return reading(self.hold, self.compliance / 1000, channel=channel)
    def configure_source(self, smu, base):
        self.events.append(("configure", base["instrument"]["channel"]))
        return {"voltage_setpoint_v": self.hold}
    def spot_factory(self, smu, channel):
        self.events.append(("spot", channel))
        return FakeSpot(self, channel)
    def enable_output(self, smu, channel, *, max_voltage_v, max_current_a):
        self.events.append(("enable", channel))
        smu.on[channel] = True
    def set_range(self, smu, channel, range_a):
        self.calls.append((channel, range_a, dict(smu.on)))
        return range_a
    def restore(self, smu, channel):
        self.restores.append((channel, dict(smu.on)))
    def run(self, run_directory, **extra):
        return run_dual_leakage_test(
            self.documents, smu=self.smu, maximum_voltage_v=self.vmax,
            maximum_current_a=self.compliance, configure_source=self.configure_source,
            spot_factory=self.spot_factory, enable_output=self.enable_output,
            run_directory=run_directory, clock=self.timing.clock, sleep=self.timing.sleep,
            utc_now=lambda: datetime(2026, 11, 27, 20, 0, 0, tzinfo=timezone.utc), **extra)
def rows_of(run, channel):
    with (run / f"channel_{channel}" / CSV_NAME).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))
def metadata_of(run, channel):
    return json.loads((run / f"channel_{channel}" / METADATA_NAME).read_text(encoding="utf-8"))
def batch_of(run):
    return json.loads((run / BATCH_NAME).read_text(encoding="utf-8"))
def test_both_channels_are_logged_on_one_schedule_and_shut_down_together(profile, tmp_path):
    rig = DualRig(pair(profile))
    run = tmp_path / "run"
    result = rig.run(run)
    assert result["state"] == "completed" and result["shutdown_verified"] is True
    for channel in (1, 2):
        rows = rows_of(run, channel)
        assert [float(row["scheduled_s"]) for row in rows] == OFFSETS
        meta = metadata_of(run, channel)
        assert meta["state"] == "completed" and meta["channel"] == channel
        assert meta["shutdown_verified"] is True and meta["hardware_qualified"] is False
        digest = hashlib.sha256((run / f"channel_{channel}" / CSV_NAME).read_bytes()).hexdigest()
        assert meta["csv_sha256"] == digest
    assert rig.events == [("configure", 1), ("configure", 2), ("spot", 1), ("spot", 2),
                          ("enable", 1), ("enable", 2), "shutdown"]
    assert rig.read_log == [1, 2] * 9
    assert rig.smu.on == {1: False, 2: False}
    assert (run / MARKER_NAME).exists() and batch_of(run)["state"] == "completed"
@pytest.mark.parametrize("faulty, rows_one, rows_two", [(1, 2, 2), (2, 3, 2)])
def test_a_fault_on_either_channel_stops_both(profile, tmp_path, faulty, rows_one, rows_two):
    def behavior(rig, channel):
        if channel == faulty and rig.reads[channel] == 3:
            return reading(rig.hold + 0.5, rig.compliance / 1000, channel=channel)
        return rig.nominal(channel)
    rig = DualRig(pair(profile), behavior)
    run = tmp_path / "run"
    with pytest.raises(LeakageRunError):
        rig.run(run)
    assert len(rows_of(run, 1)) == rows_one and len(rows_of(run, 2)) == rows_two
    for channel in (1, 2):
        meta = metadata_of(run, channel)
        assert meta["state"] == "failed" and meta["shutdown_verified"] is True
    assert metadata_of(run, faulty)["error"]["type"] == "LeakageRunError"
    assert metadata_of(run, 3 - faulty)["error"]["type"] == "PeerChannelStopped"
    batch = batch_of(run)
    assert batch["state"] == "failed" and batch["culprit_channel"] == faulty
    assert rig.events[-1] == "shutdown" and rig.smu.on == {1: False, 2: False}
    assert not (run / MARKER_NAME).exists()
def test_read_failures_are_retried_per_channel(profile, tmp_path):
    def behavior(rig, channel):
        if channel == 2 and rig.reads[2] in (3, 4):
            raise OSError("usb glitch")
        return rig.nominal(channel)
    rig = DualRig(pair(profile), behavior)
    run = tmp_path / "run"
    result = rig.run(run)
    assert result["state"] == "completed"
    assert rows_of(run, 2)[2]["read_failures"] == "2"
    assert metadata_of(run, 2)["read_retry_count"] == 2
    assert metadata_of(run, 1)["read_retry_count"] == 0
def test_an_interrupt_aborts_both_channels(profile, tmp_path):
    def behavior(rig, channel):
        if channel == 2 and rig.reads[2] == 3:
            raise KeyboardInterrupt
        return rig.nominal(channel)
    rig = DualRig(pair(profile), behavior)
    run = tmp_path / "run"
    with pytest.raises(KeyboardInterrupt):
        rig.run(run)
    for channel in (1, 2):
        meta = metadata_of(run, channel)
        assert meta["state"] == "aborted" and meta["shutdown_verified"] is True
    assert batch_of(run)["state"] == "aborted" and rig.smu.on == {1: False, 2: False}
    assert not (run / MARKER_NAME).exists()
def test_a_failed_shutdown_is_recorded_and_not_hidden(profile, tmp_path):
    rig = DualRig(pair(profile), fail_shutdown=True)
    run = tmp_path / "run"
    with pytest.raises(LeakageRunError, match="Shutdown"):
        rig.run(run)
    for channel in (1, 2):
        meta = metadata_of(run, channel)
        assert meta["state"] == "failed" and meta["shutdown_verified"] is False
        assert meta["cleanup_error"]["message"] == "shutdown boom"
    assert not (run / MARKER_NAME).exists()
def test_profiles_must_differ_only_by_channel_and_specimen(profile):
    ordered, validated = validate_dual_documents(pair(profile))
    assert [doc["instrument"]["channel"] for doc in ordered] == [1, 2] and len(validated) == 2
    swapped = pair(profile)[::-1]
    assert [doc["instrument"]["channel"] for doc in validate_dual_documents(swapped)[0]] == [1, 2]
    different = pair(profile)
    different[1]["charge"]["target_voltage_v"] = 3.7
    with pytest.raises(ValueError, match="identical"):
        validate_dual_documents(different)
    same_channel = pair(profile)
    same_channel[1]["instrument"]["channel"] = 1
    with pytest.raises(ValueError, match="one profile for channel 1"):
        validate_dual_documents(same_channel)
    same_specimen = pair(profile)
    same_specimen[1]["dut"]["specimen_id"] = same_specimen[0]["dut"]["specimen_id"]
    with pytest.raises(ValueError, match="specimen"):
        validate_dual_documents(same_specimen)
    placeholder = pair(profile)
    placeholder[1]["dut"]["specimen_id"] = "REPLACE_SPECIMEN_CH2"
    with pytest.raises(ValueError, match="specimen"):
        validate_dual_documents(placeholder)
    early = pair(profile)
    for doc in early:
        doc["leakage"]["early_stop"] = dict(EARLY_STOP)
    with pytest.raises(ValueError, match="Early stop"):
        validate_dual_documents(early)
    with pytest.raises(ValueError, match="exactly two"):
        validate_dual_documents(pair(profile)[:1])
def test_an_invalid_pair_is_refused_before_anything_happens(profile, tmp_path):
    rig = DualRig(pair(profile))
    rig.documents[1]["charge"]["target_voltage_v"] = 3.7
    with pytest.raises(ValueError, match="identical"):
        rig.run(tmp_path / "run")
    assert rig.events == [] and not (tmp_path / "run").exists()
def test_each_channel_gets_its_own_start_range_and_first_step(profile, tmp_path):
    documents = pair(profile, range_control={"mode": "step_down"})
    rig = DualRig(documents)
    run = tmp_path / "run"
    rig.run(run, set_range=rig.set_range, restore_auto_range=rig.restore)
    ranger = StepDownRanger(build_leakage_plan(documents[0])["range_control"])
    start_range = ranger.takeover(rig.compliance)
    first_range = ranger.initial_step(rig.compliance / 1000)
    expected = [(1, start_range), (2, start_range)]
    if first_range is not None:
        expected += [(1, first_range), (2, first_range)]
    assert [(channel, value) for channel, value, _ in rig.calls] == expected
    assert all(on == {1: False, 2: False} for _, _, on in rig.calls[:2])
    assert all(on == {1: True, 2: True} for _, _, on in rig.calls[2:])
    for channel in (1, 2):
        control = metadata_of(run, channel)["range_control"]
        actions = [event["action"] for event in control["events"]]
        assert actions == ["start"] + (["step_down"] if first_range is not None else [])
        assert control["auto_range_restored"] is True
        if first_range is not None:
            assert rows_of(run, channel)[1]["after_range_change"] == "1"
    assert rig.restores == [(1, {1: False, 2: False}), (2, {1: False, 2: False})]
class EnableChannel:
    def __init__(self, smu, number):
        self.smu, self.channel = smu, number
    def get_output_state(self):
        return self.smu.on[self.channel]
    def get_auto_output_on(self):
        return False
    def get_output_off_mode(self):
        return "HIZ"
    def get_source_mode(self):
        return "VOLT"
    def get_voltage_setpoint(self):
        return 3.8
    def get_current_limit(self):
        return 0.4
    def disable_output(self):
        self.smu.on[self.channel] = False
        self.smu.writes.append(f"disable {self.channel}")
class EnableSMU:
    def __init__(self, on1=False, on2=False, drop_peer=False):
        self.on = {1: on1, 2: on2}
        self.writes = []
        self.drop_peer = drop_peer
        self.chan1, self.chan2 = EnableChannel(self, 1), EnableChannel(self, 2)
    def check_errors(self):
        pass
    def _write(self, command):
        self.writes.append(command)
        if command.startswith(":OUTP") and command.endswith(" ON"):
            channel = int(command[5])
            self.on[channel] = True
            if self.drop_peer:
                self.on[3 - channel] = False
    def _query(self, command):
        return "DC" if command.endswith("SHAP?") else "FIX"
def test_the_second_channel_can_be_enabled_while_the_first_is_on():
    smu = EnableSMU(on1=True)
    enable_dual_dc_output(smu, 2, max_voltage_v=3.81, max_current_a=0.4)
    assert smu.on == {1: True, 2: True} and ":OUTP2 ON" in smu.writes
    assert not any("(@1)" in item or item.startswith(":OUTP1") for item in smu.writes)
def test_enabling_a_channel_that_is_already_on_is_refused():
    with pytest.raises(RuntimeError, match="already"):
        enable_dual_dc_output(EnableSMU(on2=True), 2, max_voltage_v=3.81, max_current_a=0.4)
def test_a_peer_that_drops_out_rolls_the_new_channel_back():
    smu = EnableSMU(on1=True, drop_peer=True)
    with pytest.raises(RuntimeError, match="Peer"):
        enable_dual_dc_output(smu, 2, max_voltage_v=3.81, max_current_a=0.4)
    assert "disable 2" in smu.writes and smu.on[2] is False
def test_the_caller_ceilings_are_enforced():
    with pytest.raises(RuntimeError, match="exceeds"):
        enable_dual_dc_output(EnableSMU(), 1, max_voltage_v=3.81, max_current_a=0.3)
    with pytest.raises(RuntimeError, match="exceeds"):
        enable_dual_dc_output(EnableSMU(), 1, max_voltage_v=3.7, max_current_a=0.4)
    with pytest.raises(ValueError):
        enable_dual_dc_output(EnableSMU(), 3, max_voltage_v=3.81, max_current_a=0.4)
def cli_args(documents, tmp_path):
    args = []
    for document in documents:
        path = save(document, tmp_path / f"leak{document['instrument']['channel']}.yaml")
        args += ["--config", path]
    return args
def cli_pair(base, tmp_path):
    documents = pair(base)
    for document in documents:
        document["output"].update(root_directory=str(tmp_path / "runs"),
                                  analysis_directory=str(tmp_path / "out"), file_name="dual_leak")
    return documents
def test_cli_preview_lists_both_channels(profile, tmp_path, capsys):
    args = cli_args(cli_pair(profile, tmp_path), tmp_path)
    assert smu.main(args + ["--preview"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [item["channel"] for item in out["channels"]] == [1, 2]
    assert out["will_connect"] is False and out["will_enable_output"] is False
def test_cli_rejects_mismatched_profiles(profile, tmp_path, capsys):
    documents = cli_pair(profile, tmp_path)
    documents[1]["charge"]["target_voltage_v"] = 3.7
    assert smu.main(cli_args(documents, tmp_path) + ["--preview"]) == 1
    assert "identical" in capsys.readouterr().err
def test_cli_rejects_mixing_leakage_with_other_experiments(profile, tmp_path, capsys):
    first = pair(profile)[0]
    second = deepcopy(profile)
    second["instrument"]["channel"] = 2
    args = ["--config", save(first, tmp_path / "a.yaml"), "--config", save(second, tmp_path / "b.yaml")]
    assert smu.main(args + ["--preview"]) == 1
    assert "mix" in capsys.readouterr().err.lower()
def test_cli_execute_dispatches_with_a_timestamped_run_name(profile, tmp_path, monkeypatch):
    args = cli_args(cli_pair(profile, tmp_path), tmp_path)
    seen = []
    monkeypatch.setattr(leakage_dual_execution, "run_configured_dual_leakage",
                        lambda documents, run_name: seen.append((documents, run_name)) or 0)
    assert smu.main(args + ["--execute"]) == 0
    assert re.fullmatch(r"\d{8}_\d{6}_dual_leak", seen[0][1])
    assert [doc["instrument"]["channel"] for doc in seen[0][0]] == [1, 2]
def test_cli_execute_then_analyze_analyzes_each_channel_folder(profile, tmp_path, monkeypatch):
    args = cli_args(cli_pair(profile, tmp_path), tmp_path)
    calls = []
    def runner(documents, run_name):
        (tmp_path / "runs" / run_name).mkdir(parents=True)
        calls.append(run_name)
        return 0
    def analyze(source, destination, *, expected_document=None):
        calls.append((Path(source), Path(destination), expected_document["instrument"]["channel"]))
        return {"valid": True}
    monkeypatch.setattr(leakage_dual_execution, "run_configured_dual_leakage", runner)
    monkeypatch.setattr(leakage_analysis, "analyze_leakage_run", analyze)
    assert smu.main(args + ["--execute", "--analyze"]) == 0
    run_name = calls[0]
    assert calls[1:] == [
        (tmp_path / "runs" / run_name / "channel_1", tmp_path / "out" / run_name / "channel_1", 1),
        (tmp_path / "runs" / run_name / "channel_2", tmp_path / "out" / run_name / "channel_2", 2),
    ]
def test_cli_failed_run_skips_analysis(profile, tmp_path, monkeypatch, capsys):
    args = cli_args(cli_pair(profile, tmp_path), tmp_path)
    monkeypatch.setattr(leakage_dual_execution, "run_configured_dual_leakage",
                        lambda documents, run_name: 1)
    monkeypatch.setattr(leakage_analysis, "analyze_leakage_run",
                        lambda *a, **k: pytest.fail("analysis must be skipped"))
    assert smu.main(args + ["--execute", "--analyze"]) == 1
    assert "analysis skipped" in capsys.readouterr().err
def test_cli_analyze_only_reports_validity_and_errors(profile, tmp_path, monkeypatch, capsys):
    documents = cli_pair(profile, tmp_path)
    args = cli_args(documents, tmp_path)
    (tmp_path / "runs" / "run1").mkdir(parents=True)
    results = iter([{"valid": True}, {"valid": True}, {"valid": True}, {"valid": False}])
    monkeypatch.setattr(leakage_analysis, "analyze_leakage_run",
                        lambda source, destination, *, expected_document=None: next(results))
    assert smu.main(args + ["--analyze", "run1"]) == 0
    assert smu.main(args + ["--analyze", "run1"]) == 1
    capsys.readouterr()
    assert smu.main(args + ["--analyze", "missing_run"]) == 1
    assert "No recorded dual leakage run" in capsys.readouterr().err
    for document in documents:
        del document["output"]["analysis_directory"]
    assert smu.main(cli_args(documents, tmp_path) + ["--analyze", "run1"]) == 1
    assert "analysis_directory" in capsys.readouterr().err
class FakeSession:
    def __init__(self, serial):
        self.identity = f"Keysight,B2902B,{serial},FW1"
        self.is_connected = True
        self.closed = False
        self.shutdowns = 0
    def shutdown(self):
        self.shutdowns += 1
    def close(self):
        self.closed = True
@pytest.fixture
def wiring(monkeypatch):
    state = types.SimpleNamespace(session=None, runs=[], serial=None, raises=None)
    def open_session(request, document, *, timeout_ms, visa_library):
        state.session = FakeSession(state.serial or settings_for(document)["expected_serial"])
        return state.session
    def runner(documents, **kwargs):
        state.runs.append((documents, kwargs))
        if state.raises is not None:
            raise state.raises
        return {"state": "completed", "elapsed_s": 1.0, "shutdown_verified": True,
                "channels": [{"channel": 1}, {"channel": 2}]}
    monkeypatch.setattr(leakage_dual_execution, "open_owned_session", open_session)
    monkeypatch.setattr(leakage_dual_execution, "run_dual_leakage_test", runner)
    return state
def prepared(base, tmp_path):
    documents = pair(base)
    for document in documents:
        document["output"]["root_directory"] = str(tmp_path / "runs")
    return documents
def test_success_wires_the_dual_helpers_and_prints_a_summary(profile, tmp_path, wiring, capsys):
    documents = prepared(profile, tmp_path)
    assert leakage_dual_execution.run_configured_dual_leakage(documents, NAME) == 0
    _, kwargs = wiring.runs[0]
    assert kwargs["configure_source"] is configure_charge_source
    assert kwargs["spot_factory"] is SpotAcquisition
    assert kwargs["enable_output"] is enable_dual_dc_output
    assert kwargs["set_range"] is apply_current_range
    assert kwargs["restore_auto_range"] is enable_auto_range
    assert kwargs["run_directory"] == (tmp_path / "runs").absolute() / NAME
    assert wiring.session.closed
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == "completed" and out["hardware_qualified"] is False
def test_identity_mismatch_stops_before_the_runner(profile, tmp_path, wiring, capsys):
    wiring.serial = "OTHERSERIAL"
    assert leakage_dual_execution.run_configured_dual_leakage(prepared(profile, tmp_path), NAME) == 1
    assert wiring.runs == [] and wiring.session.closed and wiring.session.shutdowns == 1
    assert "identity" in capsys.readouterr().err
def test_existing_destination_and_bad_names_are_refused_before_connecting(profile, tmp_path, wiring):
    (tmp_path / "runs" / NAME).mkdir(parents=True)
    assert leakage_dual_execution.run_configured_dual_leakage(prepared(profile, tmp_path), NAME) == 1
    assert leakage_dual_execution.run_configured_dual_leakage(prepared(profile, tmp_path), "a b") == 1
    assert wiring.session is None
def test_mismatched_profiles_are_refused_before_connecting(profile, tmp_path, wiring, capsys):
    documents = prepared(profile, tmp_path)
    documents[1]["charge"]["target_voltage_v"] = 3.7
    assert leakage_dual_execution.run_configured_dual_leakage(documents, NAME) == 1
    assert wiring.session is None and "identical" in capsys.readouterr().err
def test_run_error_returns_1_reports_the_directory_and_closes(profile, tmp_path, wiring, capsys):
    wiring.raises = LeakageRunError("boom")
    assert leakage_dual_execution.run_configured_dual_leakage(prepared(profile, tmp_path), NAME) == 1
    err = capsys.readouterr().err
    report = json.loads(err[err.index("{"):])
    assert report["error_type"] == "LeakageRunError" and report["error_message"] == "boom"
    assert report["directory"] == str((tmp_path / "runs").absolute() / NAME)
    assert wiring.session.closed
def test_keyboard_interrupt_returns_130_and_closes_the_session(profile, tmp_path, wiring):
    wiring.raises = KeyboardInterrupt()
    assert leakage_dual_execution.run_configured_dual_leakage(prepared(profile, tmp_path), NAME) == 130
    assert wiring.session.closed
