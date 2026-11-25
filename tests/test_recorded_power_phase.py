"""Recorded driver-contract phase integration and adversarial offline replay tests."""
import csv
import hashlib
import io
import json
import pytest
from test_constant_power_controller import limits
from test_power_adapter import VirtualClock
from test_power_smu_backend import make
from experiments.recorded_power_phase import run_recorded_power_phase
from experiments.constant_power import PowerControlError
from recording.power_phase import PowerPhaseRecording, marker_text
from analysis.power_phase import validate_power_phase


def context():
    backend, smu, spot, enables = make()
    clock = VirtualClock()
    smu.resource_address = "FAKE::INSTR"
    original_call = smu.call
    def timed_call(name):
        clock.time += 0.0005
        return original_call(name)
    smu.call = timed_call
    original_read = spot.read
    voltages = [3.8, 3.6, 3.4, 2.2]
    count = 0
    def read():
        nonlocal count
        result = original_read()
        result.voltage_v = voltages[min(count, len(voltages)-1)]
        count += 1
        return result
    spot.read = read
    return backend, smu, spot, clock, enables


def execute(tmp_path, *, ctx=None, **changes):
    ctx = context() if ctx is None else ctx
    backend, smu, spot, clock, enables = ctx
    arguments = dict(backend=backend, clock=clock.now, sleep=clock.sleep, channel=1,
                     maximum_probe_current_a=0.001, specimen_id="BENCH_SPECIMEN",
                     execute_requested=True, configuration_snapshot={"test_fixture": True})
    arguments.update(changes)
    result = run_recorded_power_phase(limits(), tmp_path / "phase", **arguments)
    return result, ctx


def manifest(tmp_path):
    return json.loads((tmp_path / "phase/power_phase.json").read_text())


def rewrite(tmp_path, *, events=None, rows=None, metadata=None):
    root = tmp_path / "phase"
    if events is not None:
        (root / "power_events.jsonl").write_text("".join(json.dumps(e, allow_nan=False)+"\n" for e in events))
    if rows is not None:
        from recording.power_phase import FIELDS
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        (root / "power_samples.csv").write_text(buffer.getvalue())
    document = manifest(tmp_path) if metadata is None else metadata
    for name in ("power_events.jsonl", "power_samples.csv"):
        raw = (root / name).read_bytes()
        document["files"][name] = {"sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}
    text = json.dumps(document, indent=2, allow_nan=False)+"\n"
    (root / "power_phase.json").write_text(text, newline="\n")
    (root / "POWER_PHASE_COMPLETE").write_text(marker_text(text.encode()), newline="\n")


def data(tmp_path):
    root = tmp_path / "phase"
    events = [json.loads(line) for line in (root / "power_events.jsonl").read_text().splitlines()]
    with (root / "power_samples.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    return events, rows


def test_success_is_replayed_from_persisted_phase_evidence(tmp_path):
    result, ctx = execute(tmp_path)
    checked = validate_power_phase(tmp_path / "phase")
    assert checked["valid"], checked["errors"]
    assert checked["record_count"] == 4 and checked["physical_qualification"] is False
    assert result["state"] == "completed"
    assert not ctx[1].is_connected and not ctx[1].chan1.on and not ctx[1].chan2.on
    assert "test_power_smu_backend.FakeSMU" in manifest(tmp_path)["metadata"]["instrument"]["driver_type"]
    assert not (tmp_path / "phase/COMPLETE").exists()
    assert not (tmp_path / "phase/SIMULATION_COMPLETE").exists()


def test_validator_does_not_modify_any_evidence(tmp_path):
    execute(tmp_path)
    before = {p.name:p.read_bytes() for p in (tmp_path / "phase").iterdir()}
    assert validate_power_phase(tmp_path / "phase")["valid"]
    assert before == {p.name:p.read_bytes() for p in (tmp_path / "phase").iterdir()}


@pytest.mark.parametrize("arguments", [
    {"execute_requested": False}, {"execute_requested": 1},
    {"specimen_id": "REPLACE_ME"}, {"channel": 2}, {"maximum_samples": True},
])
def test_preflight_rejection_leaves_backend_ownership_with_caller(tmp_path, arguments):
    ctx = context()
    with pytest.raises(ValueError):
        execute(tmp_path, ctx=ctx, **arguments)
    assert ctx[1].calls == [] and not (tmp_path / "phase").exists()
    assert ctx[1].is_connected


def test_existing_destination_not_overwritten(tmp_path):
    (tmp_path / "phase").mkdir()
    ctx = context()
    with pytest.raises(FileExistsError):
        execute(tmp_path, ctx=ctx)
    assert ctx[1].calls == []


@pytest.mark.parametrize("operation", ["abort", "spot.configure", "enable_dc_output", "spot.read", "ch1.set_current", "shutdown", "close"])
def test_driver_failures_never_create_a_completed_phase(tmp_path, operation):
    ctx = context()
    ctx[1].failures[operation] = OSError("injected "+operation)
    with pytest.raises(OSError):
        execute(tmp_path, ctx=ctx)
    assert "shutdown" in ctx[1].calls and "close" in ctx[1].calls
    assert not (tmp_path / "phase/POWER_PHASE_COMPLETE").exists()
    assert manifest(tmp_path)["outcome"]["state"] == "raised"
    assert not validate_power_phase(tmp_path / "phase")["valid"]


def test_valid_fault_record_is_saved_before_controller_failure(tmp_path):
    ctx = context()
    ctx[2].status, ctx[2].error = 9, "status"
    with pytest.raises(PowerControlError):
        execute(tmp_path, ctx=ctx)
    _, rows = data(tmp_path)
    assert rows[0]["status"] == "9"
    assert "close" in ctx[1].calls


def test_log_failure_cannot_prevent_shutdown_or_replace_primary(tmp_path, monkeypatch):
    original = PowerPhaseRecording.event
    def failing(self, event, **details):
        if event == "current_command_requested":
            raise OSError("event journal failed")
        return original(self, event, **details)
    monkeypatch.setattr(PowerPhaseRecording, "event", failing)
    ctx = context()
    ctx[1].failures["shutdown"] = RuntimeError("secondary shutdown")
    with pytest.raises(OSError, match="event journal failed") as caught:
        execute(tmp_path, ctx=ctx)
    assert "close" in ctx[1].calls
    assert any("secondary shutdown" in note for note in caught.value.__notes__)
    assert manifest(tmp_path)["outcome"]["state"] == "raised"


@pytest.mark.parametrize("name", ["power_phase.json", "power_events.jsonl", "power_samples.csv", "POWER_PHASE_COMPLETE"])
def test_missing_evidence_is_invalid(tmp_path, name):
    execute(tmp_path)
    (tmp_path / "phase" / name).unlink()
    assert not validate_power_phase(tmp_path / "phase")["valid"]


@pytest.mark.parametrize("name", ["power_phase.json", "power_events.jsonl", "power_samples.csv", "POWER_PHASE_COMPLETE"])
def test_unbound_file_change_is_invalid(tmp_path, name):
    execute(tmp_path)
    with (tmp_path / "phase" / name).open("ab") as stream:
        stream.write(b"\n")
    assert not validate_power_phase(tmp_path / "phase")["valid"]


@pytest.mark.parametrize("field,value", [
    ("current_setpoint_a", -0.39), ("requested_power_w", 0.5),
    ("sample_time_s", 0.0), ("record_index", 100), ("action", "hold"),
])
def test_replay_rejects_changed_decisions_even_with_recomputed_hashes(tmp_path, field, value):
    execute(tmp_path)
    events, _ = data(tmp_path)
    next(e for e in events if e["event"] == "control_decision")[field] = value
    rewrite(tmp_path, events=events)
    assert not validate_power_phase(tmp_path / "phase")["valid"]


@pytest.mark.parametrize("field,value", [
    ("voltage_v", "2.0"), ("current_a", "0.2"), ("status", "9"),
    ("host_read_finished_s", "0"), ("channel", "2"),
    ("measured_discharge_power_w", "0.9"), ("stage", "power_discharge"),
])
def test_replay_rejects_changed_samples_even_with_recomputed_hashes(tmp_path, field, value):
    execute(tmp_path)
    _, rows = data(tmp_path)
    rows[0][field] = value
    rewrite(tmp_path, rows=rows)
    assert not validate_power_phase(tmp_path / "phase")["valid"]


@pytest.mark.parametrize("event_name", ["current_command_requested", "current_command_returned", "current_readback", "current_setpoint_acknowledged", "cleanup_outcome"])
def test_missing_command_or_cleanup_event_fails_replay_even_with_fresh_counts_and_hashes(tmp_path, event_name):
    execute(tmp_path)
    events, _ = data(tmp_path)
    events.remove(next(e for e in events if e["event"] == event_name))
    for index,event in enumerate(events):
        event["event_index"] = index
    document = manifest(tmp_path)
    document["event_count_flushed"] = len(events)
    rewrite(tmp_path, events=events, metadata=document)
    assert not validate_power_phase(tmp_path / "phase")["valid"]


def test_simulation_and_ordinary_markers_are_rejected(tmp_path):
    execute(tmp_path)
    (tmp_path / "phase/SIMULATION_COMPLETE").write_text("wrong evidence kind")
    assert not validate_power_phase(tmp_path / "phase")["valid"]


def test_duplicate_json_keys_and_nonfinite_values_rejected(tmp_path):
    execute(tmp_path)
    path = tmp_path / "phase/power_phase.json"
    text = path.read_text().replace('"simulation_only": false', '"simulation_only": true, "simulation_only": false')
    path.write_text(text, newline="\n")
    (tmp_path / "phase/POWER_PHASE_COMPLETE").write_text(marker_text(text.encode()), newline="\n")
    assert not validate_power_phase(tmp_path / "phase")["valid"]


def test_symlink_evidence_rejected(tmp_path):
    execute(tmp_path)
    path = tmp_path / "phase/power_samples.csv"
    outside = tmp_path / "copied.csv"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation is unavailable.")
    assert not validate_power_phase(tmp_path / "phase")["valid"]


@pytest.mark.parametrize("field,value", [("sample_count_flushed", True), ("event_count_flushed", True), ("simulation_only", True)])
def test_rebound_manifest_type_or_origin_tampering_is_invalid(tmp_path, field, value):
    execute(tmp_path)
    document = manifest(tmp_path)
    document[field] = value
    rewrite(tmp_path, metadata=document)
    assert not validate_power_phase(tmp_path / "phase")["valid"]


def test_early_acknowledgement_is_rejected_even_with_rebound_files(tmp_path):
    execute(tmp_path)
    events, _ = data(tmp_path)
    ack = next(e for e in events if e["event"] == "current_setpoint_acknowledged")
    ack["controller_acknowledged_at_s"] = 0.0
    rewrite(tmp_path, events=events)
    assert not validate_power_phase(tmp_path / "phase")["valid"]


def test_nonfinite_event_json_is_rejected(tmp_path):
    import re
    execute(tmp_path)
    path = tmp_path / "phase/power_events.jsonl"
    text = re.sub(r'"host_time_s": [0-9.eE+-]+', '"host_time_s": NaN', path.read_text(), count=1)
    path.write_text(text, newline="\n")
    rewrite(tmp_path)
    assert not validate_power_phase(tmp_path / "phase")["valid"]


def test_completed_export_validation_failure_removes_marker_and_records_failure(tmp_path, monkeypatch):
    import analysis.power_phase as validation
    monkeypatch.setattr(validation, "validate_power_phase", lambda directory: {"valid": False, "errors": ["injected"]})
    with pytest.raises(ValueError, match="Persisted power-phase validation failed"):
        execute(tmp_path)
    assert not (tmp_path / "phase/POWER_PHASE_COMPLETE").exists()
    assert manifest(tmp_path)["outcome"]["state"] == "raised"


class WireSMU:
    """Stateful SCPI wire fake using actual SMUChannel/SpotAcquisition/DC helpers."""
    def __init__(self, clock, channel):
        from drivers.smu_channel import SMUChannel
        self.clock, self.channel = clock, channel
        self.resource_address = "FAKE::WIRE::INSTR"
        self.identity = "Keysight Technologies,B2902B,TESTSERIAL,1.0"
        self.is_connected = True
        self.chan1, self.chan2 = SMUChannel(self, 1), SMUChannel(self, 2)
        self.responses = {":FORM:DATA?": "ASC", ":FORM:ELEM:SENS?": "VOLT,CURR,TIME,STAT"}
        self.commands, self.read_count, self.status = [], 0, 1
        for i in (1, 2):
            self.responses.update({
                f":OUTP{i}?": "0", f":OUTP{i}:ON:AUTO?": "0", f":OUTP{i}:OFF:MODE?": "HIZ",
                f":SOUR{i}:FUNC:MODE?": "CURR", f":SOUR{i}:FUNC:SHAP?": "DC", f":SOUR{i}:CURR:MODE?": "FIX",
                f":SOUR{i}:CURR?": "0", f":SENS{i}:VOLT:PROT?": "3.81", f":SENS{i}:REM?": "1",
                f":SENS{i}:VOLT:NPLC?": "1", f":SENS{i}:VOLT:NPLC? MIN": "0.001", f":SENS{i}:VOLT:NPLC? MAX": "100",
                f':SENS{i}:FUNC:STAT? "VOLT"': "1", f':SENS{i}:FUNC:STAT? "CURR"': "1",
            })

    def _query(self, command):
        self.clock.time += 0.00001
        self.commands.append(command)
        if command.startswith(":MEAS? "):
            assert command == f":MEAS? (@{self.channel})"
            voltage = [3.8, 3.6, 3.4, 2.2][min(self.read_count, 3)]
            self.read_count += 1
            return f"{voltage},{self.responses[f':SOUR{self.channel}:CURR?']},0,{self.status}"
        return self.responses[command]

    def _write(self, command):
        self.clock.time += 0.00001
        self.commands.append(command)
        base, value = command.split(" ", 1)
        if value in ("ON", "OFF"):
            value = "1" if value == "ON" else "0"
        self.responses[base + "?"] = value

    def check_errors(self):
        self.clock.time += 0.00001

    def abort(self):
        self._write(":ABOR:ALL (@1,2)")

    def shutdown(self):
        self.abort()
        for selected in (self.chan1, self.chan2):
            selected.set_auto_output_on(False)
            selected.disable_output()
        assert self.chan1.get_output_state() is False
        assert self.chan2.get_output_state() is False

    def close(self):
        self.is_connected = False


@pytest.mark.parametrize("channel", [1, 2])
def test_actual_paired_acquisition_and_dc_preflight_helpers_with_scpi_wire_fake(tmp_path, channel):
    from experiments.power_smu_backend import B2902BPowerBackend
    clock = VirtualClock()
    smu = WireSMU(clock, channel)
    backend = B2902BPowerBackend(smu, channel=channel, expected_serial="TESTSERIAL", four_wire=True,
                                nplc=1, maximum_voltage_v=3.81, maximum_current_a=0.4,
                                output_authorized=True)
    result = run_recorded_power_phase(limits(), tmp_path / "phase", backend=backend,
                                     clock=clock.now, sleep=clock.sleep, channel=channel,
                                     maximum_probe_current_a=0.001, specimen_id="WIRE_SPECIMEN",
                                     execute_requested=True)
    assert result["state"] == "completed"
    checked = validate_power_phase(tmp_path / "phase")
    assert checked["valid"], checked["errors"]
    assert checked["channel"] == channel
    assert [c for c in smu.commands if c.startswith(":MEAS")] == [f":MEAS? (@{channel})"] * 4
    assert f":OUTP{channel} ON" in smu.commands
    assert smu.responses[":OUTP1?"] == smu.responses[":OUTP2?"] == "0"
    assert not smu.is_connected


def test_actual_measurement_status_exception_is_persisted_before_failure(tmp_path):
    from experiments.power_smu_backend import B2902BPowerBackend
    clock = VirtualClock()
    smu = WireSMU(clock, 1)
    smu.status = 9
    backend = B2902BPowerBackend(smu, channel=1, expected_serial="TESTSERIAL", four_wire=True,
                                nplc=1, maximum_voltage_v=3.81, maximum_current_a=0.4, output_authorized=True)
    with pytest.raises(PowerControlError):
        run_recorded_power_phase(limits(), tmp_path / "phase", backend=backend, clock=clock.now,
                                 sleep=clock.sleep, channel=1, maximum_probe_current_a=0.001,
                                 specimen_id="WIRE_SPECIMEN", execute_requested=True)
    assert data(tmp_path)[1][0]["status"] == "9"
    assert not smu.is_connected



def test_recorder_creation_failure_still_closes_owned_transport(tmp_path, monkeypatch):
    import experiments.recorded_power_phase as runner
    ctx = context()
    def fail(*args, **kwargs):
        raise OSError("recording allocation failed")
    monkeypatch.setattr(runner, "PowerPhaseRecording", fail)
    with pytest.raises(OSError, match="recording allocation"):
        execute(tmp_path, ctx=ctx)
    assert "shutdown" in ctx[1].calls and "close" in ctx[1].calls


def test_disabled_backend_output_authorization_rejected_before_ownership(tmp_path):
    ctx = context()
    ctx[0].output_authorized = False
    with pytest.raises(ValueError, match="authorization"):
        execute(tmp_path, ctx=ctx)
    assert ctx[1].calls == [] and ctx[1].is_connected


def test_csv_close_failure_cannot_finalize_a_recorded_phase(tmp_path, monkeypatch):
    original = PowerPhaseRecording.close
    def failed_close(self):
        original(self)
        raise OSError("injected CSV close")
    monkeypatch.setattr(PowerPhaseRecording, "close", failed_close)
    ctx = context()
    with pytest.raises(OSError, match="CSV close"):
        execute(tmp_path, ctx=ctx)
    assert not ctx[1].is_connected
    assert manifest(tmp_path)["outcome"]["state"] == "raised"
    assert not (tmp_path / "phase/POWER_PHASE_COMPLETE").exists()


def test_keyboard_interrupt_preserved_after_independent_cleanup(tmp_path):
    ctx = context()
    original = KeyboardInterrupt("injected acquisition interrupt")
    ctx[1].failures["spot.read"] = original
    with pytest.raises(KeyboardInterrupt) as caught:
        execute(tmp_path, ctx=ctx)
    assert caught.value is original
    assert "shutdown" in ctx[1].calls and "close" in ctx[1].calls
    assert manifest(tmp_path)["outcome"]["error_type"] == "KeyboardInterrupt"


def test_shutdown_returning_without_outputs_off_raises_adapter_error_and_closes(tmp_path):
    from experiments.power_adapter import PowerAdapterError
    ctx = context()
    def no_off_shutdown():
        ctx[1].call("shutdown")
    ctx[1].shutdown = no_off_shutdown
    with pytest.raises(PowerAdapterError, match="both outputs OFF"):
        execute(tmp_path, ctx=ctx)
    assert "close" in ctx[1].calls
    assert not ctx[1].is_connected
    outcome = manifest(tmp_path)["outcome"]
    assert outcome["error_type"] == "PowerAdapterError"
    assert outcome["outputs_reported_off"] is False
    assert not (tmp_path / "phase/POWER_PHASE_COMPLETE").exists()


def test_malformed_cleanup_readbacks_raise_adapter_error_and_close(tmp_path):
    from experiments.power_adapter import PowerAdapterError
    ctx = context()
    ctx[0].get_output_states = lambda: (False,)
    with pytest.raises(PowerAdapterError, match="Malformed post-shutdown"):
        execute(tmp_path, ctx=ctx)
    assert "close" in ctx[1].calls
    assert not ctx[1].is_connected
    assert manifest(tmp_path)["outcome"]["error_type"] == "PowerAdapterError"
    assert not (tmp_path / "phase/POWER_PHASE_COMPLETE").exists()
