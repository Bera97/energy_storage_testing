"""Offline full dual-run tests with fake instrument dependencies."""
from copy import deepcopy
import json
import pytest
import yaml
import smu
from experiments.dual_coordinator import VirtualClock
from experiments.dual_instrument_io import DualInstrumentSession
from experiments.dual_live import check_batch, execute
from experiments.dual_phase import DualPhase
from experiments.dual_preconditioning import build_dual_plan
import experiments.dual_instrument_io as io
import test_dual_phase as phase_tests
import test_dual_instrument_io as io_tests
pair = phase_tests.pair
@pytest.fixture
def rig(pair, monkeypatch):
    documents = deepcopy(pair)
    for document in documents:
        document["experiment"]["cycles"] = 1
        document["charge"]["maximum_duration_s"] = 5
        document["charge"]["maximum_cv_duration_s"] = 3
        document["charge"]["termination"]["hold_time_s"] = 0.5
        document["discharge"]["maximum_duration_s"] = 5
        document["rest"]["after_charge_s"] = 0.05
        document["rest"]["after_discharge_s"] = 0.05
    plan = build_dual_plan(documents)
    driver = phase_tests.SourceSMU()
    driver.operating = 0
    clock = VirtualClock()
    monkeypatch.setattr(
        io, "SpotAcquisition", io_tests.FakeSpot
    )
    def session_factory(plan, serial):
        return DualInstrumentSession(
            plan,
            serial,
            smu_factory=lambda *args, **kwargs: driver,
        )
    def phase_factory(session, channel, phase, directory, run_id, **kwargs):
        task = DualPhase(
            session, channel, phase, directory, run_id, **kwargs
        )
        driver.phases[channel] = task
        return task
    def sleep(duration):
        clock.sleep(duration)
        for channel, task in driver.phases.items():
            selected = (
                driver.chan1 if channel == 1 else driver.chan2
            )
            if task.state != "active" or not selected.on:
                continue
            next_sample = getattr(
                task, "_test_next_sample", task.started + 0.2
            )
            while next_sample <= clock.now + 1e-10:
                elapsed = next_sample - task.started
                if task.phase["type"] == "charge_cc_cv":
                    voltage = task.phase["target_voltage_v"]
                    current = 0.05
                    status = 0
                else:
                    voltage = max(2.6, 3.8 - 2 * elapsed)
                    current = -task.phase["current_magnitude_a"]
                    status = 1
                driver.samples[channel].append(
                    (
                        voltage, current,
                        1000 + next_sample, status,
                    )
                )
                next_sample += 0.2
            task._test_next_sample = next_sample
    options = dict(
        clock=clock,
        sleep=sleep,
        session_factory=session_factory,
        phase_factory=phase_factory,
    )
    return plan, driver, options
def test_full_dual_run_and_verification(rig, tmp_path):
    plan, driver, options = rig
    directory = tmp_path / "batch"
    report = execute(
        plan, directory, "TESTSERIAL", **options
    )
    assert report["state"] == "completed"
    assert report["simulation_only"] is False
    assert report["final_stop_verified"] is True
    assert report["session_close_returned"] is True
    assert driver.closed is True
    assert driver.chan1.on is False
    assert driver.chan2.on is False
    validation = check_batch(directory)
    assert validation["valid"], validation["errors"]
    assert validation["phase_recordings_checked"] == 4
    assert (directory / "DUAL_COMPLETE").is_file()
def test_bus_fault_stops_both_and_closes_all_recordings(
    rig, tmp_path, monkeypatch
):
    plan, driver, options = rig
    original = driver._query
    def fail(command):
        if command.startswith(":TRAC2:DATA?"):
            raise OSError("synthetic dual bus fault")
        return original(command)
    monkeypatch.setattr(driver, "_query", fail)
    directory = tmp_path / "failed"
    with pytest.raises(OSError, match="bus fault"):
        execute(plan, directory, "TESTSERIAL", **options)
    assert driver.closed is True
    assert driver.chan1.on is False
    assert driver.chan2.on is False
    assert not (directory / "DUAL_COMPLETE").exists()
    assert all(
        task.run.writer.closed
        for task in driver.phases.values()
        if task.run is not None
    )
    document = json.loads(
        (directory / "dual_experiment.json").read_text(
            encoding="utf-8"
        )
    )
    assert document["state"] == "raised"
    assert check_batch(directory)["valid"] is False
def test_existing_batch_is_not_overwritten(rig, tmp_path):
    plan, driver, options = rig
    directory = tmp_path / "existing"
    directory.mkdir()
    sentinel = directory / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        execute(plan, directory, "TESTSERIAL", **options)
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert driver.is_connected is False
def test_execution_no_longer_requires_qualification_flag(rig, tmp_path):
    plan, driver, options = rig
    directory = tmp_path / "without_qualification_flag"
    report = execute(plan, directory, "TESTSERIAL", **options)
    assert report["state"] == "completed"
    assert driver.closed is True
    assert check_batch(directory)["valid"] is True
def test_public_preview_does_not_construct_instrument(
    pair, tmp_path, monkeypatch, capsys
):
    def forbidden(*args, **kwargs):
        pytest.fail("Preview attempted instrument construction.")
    monkeypatch.setattr(io, "_new_smu", forbidden)
    arguments = ["dual-run"]
    for index, document in enumerate(pair):
        path = tmp_path / f"channel_{index}.yaml"
        path.write_text(
            yaml.safe_dump(document), encoding="utf-8"
        )
        arguments += ["--config", str(path)]
    assert smu.main(arguments) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["will_connect"] is False
def test_missing_completion_marker_is_rejected(rig, tmp_path):
    plan, _, options = rig
    directory = tmp_path / "batch"
    execute(plan, directory, "TESTSERIAL", **options)
    (directory / "DUAL_COMPLETE").unlink()
    assert check_batch(directory)["valid"] is False

def test_grouped_startup_is_released_before_peer_configuration(
    rig, tmp_path, monkeypatch
):
    plan, driver, options = rig
    driver.grouped = True
    original = driver._write
    def grouped_hardware(command):
        if (
            driver.grouped
            and driver.chan1.on
            and command.startswith((
                ":SOUR2:", ":ARM2:", ":TRIG2:", ":TRAC2:"
            ))
        ):
            raise RuntimeError(
                "Settings conflict: other triggers in group are busy"
            )
        return original(command)
    monkeypatch.setattr(driver, "_write", grouped_hardware)
    directory = tmp_path / "grouped_startup"
    report = execute(
        plan, directory, "TESTSERIAL", **options
    )
    assert report["state"] == "completed"
    assert driver.grouped is False
    assert driver.commands.index(":SYST:GRO:DEF (@1),(@2)") < (
        driver.commands.index(":OUTP1 ON")
    )
    assert check_batch(directory)["valid"] is True
