"""Offline native-recorder integration; no real driver or connection library."""
from copy import deepcopy
import json
import pytest
import test_recorded_phase_executor as legacy
from test_recorded_power_phase import WireSMU
from experiments.acquisition_budget import build_acquisition_plan
from experiments.power_planning import validate_constant_power_sweep
from experiments.power_sweep_backend import RecordedPowerSweepBackend
from experiments.recorded_power_sweep import run_recorded_power_sweep
from experiments.power_sweep_contract import PowerSweepError, completion_text, digest, write_json, tree_files
from analysis.power_sweep import validate_power_sweep


class Clock(legacy.FakeClock):
    @property
    def time(self):
        return self.now

    @time.setter
    def time(self, value):
        self.now = value


@pytest.fixture
def context(tmp_path):
    fixture = legacy.RecordedPhaseExecutorTests(methodName="test_charge_returns_finalized_recording")
    fixture.setUp()
    fixture.clock = Clock()
    document = deepcopy(fixture.document)
    document["experiment"]["type"] = "constant_power_discharge_sweep"
    document["execution"] = {"expected_serial": "TESTSERIAL", "maximum_voltage_v": 4.2, "maximum_current_a": 4.0}
    document["sweep"] = {"power_w": [0.8, 0.4], "repeats": 2, "initial_discharge": True}
    document["power_control"] = {"maximum_current_a": 0.4, "update_interval_s": 1.0,
        "maximum_feedback_age_s": 2.0, "relative_tolerance": 0.05, "absolute_tolerance_w": 0.005,
        "startup_grace_s": 2.0, "maximum_tracking_error_s": 2.0}
    sessions = []
    def open_session(request, config):
        phase = request["phase"]
        if phase["type"] == "discharge_cp" or phase.get("rest_mode") == "zero_current_logging":
            smu = WireSMU(fixture.clock, config["instrument"]["channel"])
            original_query, original_write = smu._query, smu._write
            def query(command):
                value = original_query(command)
                if command.startswith(":MEAS?"):
                    parts = value.split(",")
                    parts[0] = str(3.2 if phase["type"] == "rest" else [3.8, 3.6, 3.4, 2.7][min(smu.read_count-1, 3)])
                    value = ",".join(parts)
                return value
            def write(command):
                if " " not in command:
                    smu.clock.time += 0.00001
                    smu.commands.append(command)
                    return None
                return original_write(command)
            smu._query, smu._write = query, write
        else:
            smu = legacy.FakeSMU(fixture.events)
            smu.is_connected = True
            smu.identity = "Keysight Technologies,B2902B,TESTSERIAL,1.0"
            def close():
                smu.is_connected = False
                fixture.events.append("close")
            smu.close = close
            fixture.smu = smu
        smu.resource_address = config["instrument"]["visa_address"]
        sessions.append((request, smu))
        return smu
    def make(**changes):
        fixture.document = validate_constant_power_sweep(document)["preparation_config"]
        fixture.acquisition_plan = build_acquisition_plan(fixture.document, maximum_records=100000)
        arguments = dict(open_session=open_session, maximum_probe_current_a=0.001,
                         clock=fixture.clock, sleep=fixture.clock.sleep, output_authorized=True,
                         rest_poll_interval_s=1000.0)
        arguments.update(changes)
        return RecordedPowerSweepBackend(document, **arguments)
    def execute(backend=None, **changes):
        arguments = dict(backend=make() if backend is None else backend, execute_requested=True)
        arguments.update(changes)
        return run_recorded_power_sweep(document, tmp_path / "sweep", **arguments)
    yield document, make, execute, sessions, fixture
    fixture.doCleanups()


def manifest(tmp_path):
    return json.loads((tmp_path / "sweep/power_sweep.json").read_text())


def rebind(root, stage_id=None):
    doc = json.loads((root / "power_sweep.json").read_text())
    if stage_id is not None:
        path = root / stage_id / "stage.json"
        stage = json.loads(path.read_text())
        stage["files"] = tree_files(path.parent)
        write_json(path, stage)
        next(r for r in doc["stages"] if r["stage_id"] == stage_id)["stage_sha256"] = digest(path)
    write_json(root / "power_sweep.json", doc)
    (root / "POWER_SWEEP_COMPLETE").write_text(completion_text(root / "power_sweep.json"), newline="\n")


def test_whole_sweep_order_one_preparation_and_session_cleanup(context, tmp_path):
    document, _, execute, sessions, _ = context
    original = deepcopy(document)
    result = execute()
    checked = validate_power_sweep(tmp_path / "sweep")
    assert checked["valid"], checked["errors"]
    assert result["completed_stage_count"] == 34
    assert [r["phase"]["type"] for r, _ in sessions[:2]] == ["discharge_cc", "rest"]
    assert [r["power_w"] for r, _ in sessions if r["phase"]["type"] == "discharge_cp"] == [0.8]*4+[0.4]*4
    assert sum(r["phase"]["type"] == "discharge_cc" for r, _ in sessions) == 1
    assert all(smu.is_connected is False for _, smu in sessions)
    assert len({id(smu) for _, smu in sessions}) == 34
    assert document == original


def test_read_only_validator(context, tmp_path):
    _, _, execute, _, _ = context
    execute()
    root = tmp_path / "sweep"
    before = {str(p):p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert validate_power_sweep(root)["valid"]
    assert before == {str(p):p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("requested", [False, 1, "yes", None])
def test_no_explicit_authorization_means_no_directory_or_sessions(context, tmp_path, requested):
    _, _, execute, sessions, _ = context
    with pytest.raises(PowerSweepError):
        execute(execute_requested=requested)
    assert not sessions and not (tmp_path / "sweep").exists()


def test_bad_later_power_rejects_before_recording_or_session(context, tmp_path):
    doc, make, _, sessions, _ = context
    doc["sweep"]["power_w"][-1] = 100.0
    with pytest.raises(ValueError):
        make()
    assert not sessions and not (tmp_path / "sweep").exists()


def test_existing_root_never_resumed(context, tmp_path):
    _, _, execute, sessions, _ = context
    (tmp_path / "sweep").mkdir()
    with pytest.raises(FileExistsError):
        execute()
    assert not sessions


def test_session_factory_failure_preserves_original(context, tmp_path):
    _, make, execute, _, _ = context
    original = OSError("failed connection owned by factory")
    def failed(*args):
        raise original
    backend = make(open_session=failed)
    with pytest.raises(OSError) as caught:
        execute(backend)
    assert caught.value is original and backend.failed
    assert manifest(tmp_path)["state"] == "raised" and manifest(tmp_path)["completed_stage_count"] == 0
    assert not (tmp_path / "sweep/POWER_SWEEP_COMPLETE").exists()


def test_missing_cc_evidence_blocks_next_stage(context, tmp_path):
    _, make, execute, sessions, fixture = context
    native_class = fixture.executor.__class__
    def factory(*args, **kwargs):
        native = native_class(*args, **kwargs)
        def call(phase):
            run = native(phase)
            (run.directory / "COMPLETE").unlink()
            return run
        return call
    with pytest.raises(PowerSweepError, match="Stage evidence gate"):
        execute(make(active_executor_factory=factory))
    assert len(sessions) == 1 and sessions[0][1].is_connected is False
    assert manifest(tmp_path)["completed_stage_count"] == 0


def test_close_failure_stops_before_rest(context, tmp_path):
    _, make, execute, sessions, _ = context
    backend = make()
    normal = backend.open_session
    def open_failed(request, config):
        smu = normal(request, config)
        def close():
            raise OSError("injected session close")
        smu.close = close
        return smu
    backend.open_session = open_failed
    with pytest.raises(OSError, match="injected session close"):
        execute(backend)
    assert len(sessions) == 1 and manifest(tmp_path)["state"] == "raised"


@pytest.mark.parametrize("relative", ["power_sweep.json", "POWER_SWEEP_COMPLETE", "stage_00000/stage.json", "stage_00000/phase_phase_2/measurements.csv"])
def test_missing_evidence_invalid(context, tmp_path, relative):
    _, _, execute, _, _ = context
    execute()
    (tmp_path / "sweep" / relative).unlink()
    assert not validate_power_sweep(tmp_path / "sweep")["valid"]


def test_rebound_condition_mismatch_invalid(context, tmp_path):
    _, _, execute, _, _ = context
    execute()
    root = tmp_path / "sweep"
    doc = manifest(tmp_path)
    doc["requests"][4]["power_w"] = 0.1
    write_json(root / "power_sweep.json", doc)
    rebind(root)
    assert not validate_power_sweep(root)["valid"]


def test_rebound_stage_identity_invalid(context, tmp_path):
    _, _, execute, _, _ = context
    execute()
    root = tmp_path / "sweep"
    path = root / "stage_00000/stage.json"
    stage = json.loads(path.read_text())
    stage["request"]["scope"] = "condition"
    write_json(path, stage)
    rebind(root, "stage_00000")
    assert not validate_power_sweep(root)["valid"]


def test_measured_rest_native_helpers_and_validator(context, tmp_path):
    doc, _, execute, sessions, _ = context
    doc["experiment"]["cycles"] = 1
    doc["sweep"]["power_w"], doc["sweep"]["repeats"] = [0.4], 1
    doc["rest"].update(mode="zero_current_logging", maximum_abs_current_a=0.001,
                        after_charge_s=0.6, after_discharge_s=0.6)
    execute()
    checked = validate_power_sweep(tmp_path / "sweep")
    assert checked["valid"], checked["errors"]
    assert checked["stage_count"] == 6 and all(not smu.is_connected for _, smu in sessions)
    rests = [p for p in (tmp_path / "sweep").glob('stage_*/rest_phase_*/measurements.csv')]
    assert len(rests) == 3



def small(context, measured=False):
    doc = context[0]
    doc["experiment"]["cycles"] = 1
    doc["sweep"]["power_w"], doc["sweep"]["repeats"] = [0.4], 1
    doc["rest"].update(after_charge_s=0.6, after_discharge_s=0.6)
    if measured:
        doc["rest"].update(mode="zero_current_logging", maximum_abs_current_a=0.001)


@pytest.mark.parametrize("wrong", ["serial", "resource", "output_on"])
def test_stage_identity_and_outputs_are_checked_before_preparation(context, tmp_path, wrong):
    _, make, execute, sessions, fixture = context
    small(context)
    backend = make()
    normal = backend.open_session
    def opening(request, config):
        smu = normal(request, config)
        if wrong == "serial":
            smu.identity = "Keysight Technologies,B2902B,OTHER,1.0"
        elif wrong == "resource":
            smu.resource_address = "OTHER::INSTR"
        else:
            smu.chan1.on = True
        return smu
    backend.open_session = opening
    with pytest.raises(PowerSweepError):
        execute(backend)
    assert len(sessions) == 1 and sessions[0][1].is_connected is False
    assert "prepare" not in fixture.events


def test_all_cleanup_operations_attempted_without_masking_primary(context, tmp_path):
    _, make, execute, sessions, _ = context
    original = OSError("primary preparation failure")
    calls = []
    def factory(smu, *args, **kwargs):
        def shutdown():
            calls.append("shutdown")
            raise RuntimeError("secondary shutdown")
        def first():
            calls.append("first")
            raise RuntimeError("secondary readback")
        def second():
            calls.append("second")
            return False
        smu.shutdown = shutdown
        smu.chan1.get_output_state, smu.chan2.get_output_state = first, second
        raise original
    with pytest.raises(OSError) as caught:
        execute(make(active_executor_factory=factory))
    assert caught.value is original and calls == ["shutdown", "first", "second"]
    assert sessions[0][1].is_connected is False
    assert len(original.__notes__) >= 2


def test_cp_cleanup_is_owned_once_and_no_queries_follow_close(context, tmp_path):
    _, make, execute, sessions, _ = context
    small(context)
    backend = make()
    normal = backend.open_session
    closed = []
    def opening(request, config):
        smu = normal(request, config)
        if request["phase"]["type"] == "discharge_cp":
            query, close = smu._query, smu.close
            def checked_query(command):
                assert smu.is_connected
                return query(command)
            def checked_close():
                assert smu.is_connected
                closed.append(smu)
                close()
            smu._query, smu.close = checked_query, checked_close
        return smu
    backend.open_session = opening
    execute(backend)
    assert len(closed) == 1 and all(not smu.is_connected for _, smu in sessions)


def test_cp_fault_preserves_sample_closes_and_stops_before_rest(context, tmp_path):
    from experiments.constant_power import PowerControlError
    _, make, execute, sessions, _ = context
    small(context)
    backend = make()
    normal = backend.open_session
    def opening(request, config):
        smu = normal(request, config)
        if request["phase"]["type"] == "discharge_cp":
            smu.status = 9
        return smu
    backend.open_session = opening
    with pytest.raises(PowerControlError):
        execute(backend)
    assert len(sessions) == 5 and all(not smu.is_connected for _, smu in sessions)
    assert manifest(tmp_path)["completed_stage_count"] == 4
    assert (tmp_path / "sweep/stage_00004/power/power_samples.csv").read_text().count("\n") == 2
    assert not (tmp_path / "sweep/POWER_SWEEP_COMPLETE").exists()


def test_cp_preflight_rejection_retains_outer_session_cleanup(context, tmp_path):
    _, make, execute, sessions, _ = context
    small(context)
    with pytest.raises(ValueError, match="B2902BPowerBackend"):
        execute(make(power_backend_factory=lambda *args, **kwargs: object()))
    assert len(sessions) == 5 and all(not smu.is_connected for _, smu in sessions)
    assert manifest(tmp_path)["completed_stage_count"] == 4


def test_keyboard_interrupt_during_measured_rest_is_preserved(context, tmp_path):
    _, make, execute, sessions, _ = context
    small(context, measured=True)
    backend = make()
    normal = backend.open_session
    original = KeyboardInterrupt("injected measured rest interruption")
    def opening(request, config):
        smu = normal(request, config)
        if request["phase"]["type"] == "rest":
            query = smu._query
            def interrupted(command):
                if command.startswith(":MEAS?"):
                    raise original
                return query(command)
            smu._query = interrupted
        return smu
    backend.open_session = opening
    with pytest.raises(KeyboardInterrupt) as caught:
        execute(backend)
    assert caught.value is original
    assert len(sessions) == 2 and all(not smu.is_connected for _, smu in sessions)
    assert manifest(tmp_path)["completed_stage_count"] == 1


def test_stage_header_failure_occurs_before_session_open(context, tmp_path, monkeypatch):
    import experiments.power_sweep_backend as module
    _, _, execute, sessions, _ = context
    def failed(*args, **kwargs):
        raise OSError("injected stage allocation")
    monkeypatch.setattr(module, "write_json", failed)
    with pytest.raises(OSError, match="stage allocation"):
        execute()
    assert not sessions and manifest(tmp_path)["state"] == "raised"


def test_root_header_failure_occurs_before_session_open(context, tmp_path, monkeypatch):
    import experiments.recorded_power_sweep as module
    _, _, execute, sessions, _ = context
    def failed(*args, **kwargs):
        raise OSError("injected root allocation")
    monkeypatch.setattr(module, "write_json", failed)
    with pytest.raises(OSError, match="root allocation"):
        execute()
    assert not sessions


def test_final_validation_failure_removes_marker_and_preserves_partial_manifest(context, tmp_path, monkeypatch):
    import experiments.recorded_power_sweep as module
    _, _, execute, sessions, _ = context
    small(context)
    monkeypatch.setattr(module, "validate_power_sweep", lambda root: {"valid": False, "errors": ["injected"]})
    with pytest.raises(PowerSweepError, match="Final sweep validation"):
        execute()
    assert all(not smu.is_connected for _, smu in sessions)
    assert manifest(tmp_path)["state"] == "raised" and manifest(tmp_path)["completed_stage_count"] == 6
    assert not (tmp_path / "sweep/POWER_SWEEP_COMPLETE").exists()


def test_stage_failure_makes_backend_terminal(context, tmp_path):
    _, make, execute, sessions, _ = context
    def failed(*args):
        raise OSError("failed connection")
    backend = make(open_session=failed)
    with pytest.raises(OSError):
        execute(backend)
    with pytest.raises(PowerSweepError, match="fresh authorized"):
        execute(backend)
    assert not sessions


def test_rebound_cc_endpoint_cannot_bypass_controller_replay(context, tmp_path):
    import csv
    import io
    _, _, execute, _, _ = context
    small(context)
    execute()
    root = tmp_path / "sweep"
    directory = root / "stage_00002/phase_phase_0"
    path = directory / "measurements.csv"
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        fields, rows = reader.fieldnames, list(reader)
    for row in rows:
        row["current_a"] = "1.0"
        row["signed_power_w"] = str(float(row["voltage_v"]))
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    path.write_text(buffer.getvalue(), newline="\n")
    doc = json.loads((directory / "metadata.json").read_text())
    doc["outcome"]["triggering_reading"]["current_a"] = 1.0
    write_json(directory / "metadata.json", doc)
    rebind(root, "stage_00002")
    checked = validate_power_sweep(root)
    assert not checked["valid"] and "replay" in ";".join(checked["errors"])


def test_late_evidence_mutation_is_detected_at_end_of_sweep_validation(context, tmp_path, monkeypatch):
    import analysis.power_sweep as module
    _, _, execute, _, _ = context
    small(context)
    execute()
    normal = module._stage
    def changing(directory, request, *args):
        checked = normal(directory, request, *args)
        if request["stage_id"] == "stage_00000":
            path = directory / "phase_phase_2/measurements.csv"
            path.write_bytes(path.read_bytes() + b"\n")
        return checked
    monkeypatch.setattr(module, "_stage", changing)
    assert not validate_power_sweep(tmp_path / "sweep")["valid"]


def test_false_output_authorization_cannot_open_any_session(context, tmp_path):
    _, make, execute, sessions, _ = context
    with pytest.raises(PowerSweepError):
        execute(make(output_authorized=False))
    assert not sessions and not (tmp_path / "sweep").exists()


def test_missing_charge_monitoring_rejected_before_connection(context, tmp_path):
    doc, make, _, sessions, _ = context
    doc["charge"].pop("cv_voltage_tolerance_v")
    with pytest.raises(ValueError, match="charge-monitoring"):
        make()
    assert not sessions



@pytest.mark.parametrize("field,value", [("completed_stage_count", True), ("state", "running"), ("physical_qualification", True)])
def test_rebound_manifest_cannot_fake_completion(context, tmp_path, field, value):
    _, _, execute, _, _ = context
    small(context)
    execute()
    root = tmp_path / "sweep"
    doc = manifest(tmp_path)
    doc[field] = value
    write_json(root / "power_sweep.json", doc)
    rebind(root)
    assert not validate_power_sweep(root)["valid"]


def test_rebound_measured_rest_external_path_is_rejected_lexically(context, tmp_path):
    _, _, execute, _, _ = context
    small(context, measured=True)
    execute()
    root = tmp_path / "sweep"
    path = root / "stage_00001/stage.json"
    doc = json.loads(path.read_text())
    doc["evidence"]["report"]["run_directory"] = "//DO_NOT_ACCESS/unknown/recording"
    write_json(path, doc)
    rebind(root, "stage_00001")
    checked = validate_power_sweep(root)
    assert not checked["valid"] and "Measured-rest path differs" in ";".join(checked["errors"])


def test_completion_marker_write_failure_is_recorded_without_success(context, tmp_path, monkeypatch):
    from pathlib import Path
    _, _, execute, sessions, _ = context
    small(context)
    normal = Path.open
    def fail_marker(self, *args, **kwargs):
        if self.name == "POWER_SWEEP_COMPLETE" and args and args[0] == "x":
            raise OSError("injected marker write")
        return normal(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", fail_marker)
    with pytest.raises(OSError, match="marker write"):
        execute()
    assert all(not smu.is_connected for _, smu in sessions)
    assert manifest(tmp_path)["state"] == "raised"
    assert not (tmp_path / "sweep/POWER_SWEEP_COMPLETE").exists()


def test_recorded_child_status_flag_tampering_is_rejected(context, tmp_path):
    _, _, execute, _, _ = context
    small(context)
    execute()
    root = tmp_path / "sweep"
    path = root / "stage_00000/phase_phase_2/measurements.csv"
    text = path.read_text()
    lines = text.splitlines()
    import csv
    import io
    reader = csv.DictReader(io.StringIO(text))
    fields, rows = reader.fieldnames, list(reader)
    rows[0]["status"] = "9"
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    assert len(lines) == len(rows)+1
    path.write_text(buffer.getvalue(), newline="\n")
    rebind(root, "stage_00000")
    assert not validate_power_sweep(root)["valid"]



@pytest.mark.parametrize("field", ["configuration", "settings", "policy", "position"])
def test_mutated_backend_is_rejected_before_any_session(context, tmp_path, field):
    _, make, execute, sessions, _ = context
    backend = make()
    if field == "configuration":
        backend.configuration["instrument"]["channel"] = 2
    elif field == "settings":
        backend.settings["maximum_current_a"] = 3.0
    elif field == "policy":
        backend.policy["maximum_probe_current_a"] = 0.2
    else:
        backend.position = 1
    with pytest.raises(PowerSweepError):
        execute(backend)
    assert not sessions and not (tmp_path / "sweep").exists()
