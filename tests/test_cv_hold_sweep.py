"""Offline single/dual CV-duration sweep integration."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
import yaml
import smu
import experiments.run_live as single
import experiments.dual_live as dual
import experiments.dual_instrument_io as instrument_io
import test_dual_live as dual_tests
import test_recorded_phase_executor as executor_tests
from experiments.config import validate_config
from experiments.sweeps import (
    build_charge_current_sweep_plan,
    execute_charge_current_sweep,
)
from experiments.dual_preconditioning import build_dual_plan
from analysis.export_experiment import validate_sweep, export_sweep
from analysis.dual_experiment import check_batch, export_dual_sweep
pair = dual_tests.pair
rig = dual_tests.rig
@pytest.fixture
def documents(rig, tmp_path):
    base, driver, options = rig
    result = [
        deepcopy(preview["acquisition_plan"]["configuration"])
        for preview in base["channels"]
    ]
    for index, document in enumerate(result):
        document["experiment"]["type"] = "cv_hold_sweep"
        document["charge"]["termination"] = {
            "mode": "from_cv_entry" if index == 0 else "after_current_threshold",
            "current_reference": "configured_charge_current",
            "current_factor": 0.5,
            "hold_time_s": [0.4, 0.8] if index == 0 else [0.6],
        }
        document["sweep"] = {
            "repeats": 2 if index == 0 else 1,
            "initial_discharge": True,
        }
        document["output"] = {
            "root_directory": str(tmp_path / "runs"),
            "analysis_directory": str(tmp_path / "analysis"),
            "file_name": "cv_hold_sweep",
        }
        ratings = document["dut"]["ratings"]
        document["execution"] = {
            "expected_serial": "TESTSERIAL",
            "maximum_voltage_v": ratings["maximum_voltage_v"],
            "maximum_current_a": max(
                ratings["maximum_continuous_charge_current_a"],
                ratings["maximum_continuous_discharge_current_a"],
            ),
        }
    return result, driver, options
def test_duration_expansion_changes_only_selected_hold(documents):
    profiles, driver, _ = documents
    before = deepcopy(profiles[0])
    plan = build_charge_current_sweep_plan(profiles[0])
    assert plan["experiment_type"] == "cv_hold_sweep"
    assert plan["parameter"] == "charge.termination.hold_time_s"
    assert plan["unit"] == "s"
    assert [entry["cv_hold_time_s"] for entry in plan["conditions"]] == [
        0.4, 0.4, 0.8, 0.8
    ]
    for entry in plan["conditions"]:
        assert entry["config"]["charge"]["current"] == before["charge"]["current"]
        assert entry["config"]["charge"]["termination"]["mode"] == "from_cv_entry"
        assert entry["config"]["charge"]["termination"]["hold_time_s"] == entry["cv_hold_time_s"]
    assert profiles[0] == before
    assert driver.is_connected is False
@pytest.mark.parametrize(
    "values",
    [[], None, 1, [0], [-1], [True], [float("nan")], [0.4, 0.4], [0.4, 3]],
)
def test_invalid_duration_lists_reject_before_execution(documents, values):
    profiles, driver, _ = documents
    profiles[0]["charge"]["termination"]["hold_time_s"] = values
    with pytest.raises(ValueError):
        build_charge_current_sweep_plan(profiles[0])
    assert driver.is_connected is False
def test_duration_sweep_requires_timed_policy_and_one_duration_location(documents):
    profiles, _, _ = documents
    document = deepcopy(profiles[0])
    document["charge"]["termination"]["mode"] = "current_threshold"
    with pytest.raises(ValueError, match="requires"):
        validate_config(document)
    document = deepcopy(profiles[0])
    document["sweep"]["hold_time_s"] = [0.4, 0.8]
    with pytest.raises(ValueError, match="unknown fields"):
        validate_config(document)
def test_dual_duration_sweep_records_compares_and_keeps_channels_separate(
    documents, tmp_path
):
    profiles, driver, options = documents
    plan = build_dual_plan(profiles)
    assert plan["experiment_type"] == "dual_cv_hold_sweep"
    assert plan["total_phase_count"] == 24
    directory = tmp_path / "dual_duration"
    report = dual.execute(plan, directory, "TESTSERIAL", **options)
    assert report["state"] == "completed"
    assert driver.closed is True
    assert driver.chan1.on is False and driver.chan2.on is False
    checked = check_batch(directory)
    assert checked["valid"], checked["errors"]
    destination = tmp_path / "dual_export"
    result = export_dual_sweep(directory, destination)
    assert len(result["cycles"]) == 5
    assert {row["cv_hold_time_s"] for row in result["cycles"] if row["channel"] == 1} == {0.4, 0.8}
    assert {row["cv_hold_time_s"] for row in result["cycles"] if row["channel"] == 2} == {0.6}
    assert (destination / "hold_time_summary.csv").is_file()
    assert (destination / "ANALYSIS_COMPLETE").is_file()
    for row in result["hold_time_summary"]:
        assert row["observation_count"] == (2 if row["channel"] == 1 else 1)
def test_dual_duration_preview_does_not_construct_hardware(
    documents, tmp_path, monkeypatch, capsys
):
    profiles, _, _ = documents
    def forbidden(*args, **kwargs):
        pytest.fail("Preview reached instrument construction.")
    monkeypatch.setattr(instrument_io, "_new_smu", forbidden)
    arguments = ["--preview"]
    for index, profile in enumerate(profiles):
        path = tmp_path / f"duration_{index}.yaml"
        path.write_text(yaml.safe_dump(profile), encoding="utf-8")
        arguments.extend(["--config", str(path)])
    assert smu.main(arguments) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["experiment_type"] == "dual_cv_hold_sweep"
    assert plan["will_connect"] is False
@pytest.fixture
def single_rig(tmp_path, monkeypatch):
    from experiments.acquisition_budget import build_acquisition_plan
    case = executor_tests.RecordedPhaseExecutorTests(
        methodName="test_charge_returns_finalized_recording"
    )
    case.setUp()
    try:
        document = deepcopy(case.document)
        document["experiment"]["type"] = "cv_hold_sweep"
        document["experiment"]["cycles"] = 1
        document["charge"]["termination"] = {
            "mode": "from_cv_entry",
            "current_reference": "configured_charge_current",
            "current_factor": 0.1,
            "hold_time_s": [1.0, 2.0],
        }
        document["charge"]["maximum_duration_s"] = 10
        document["charge"]["maximum_cv_duration_s"] = 5
        document["discharge"]["maximum_duration_s"] = 10
        document["rest"] = {"after_charge_s": 0.05, "after_discharge_s": 0.05}
        document["sweep"] = {"repeats": 1, "initial_discharge": True}
        document["output"] = {
            "root_directory": str(tmp_path / "single_runs"),
            "analysis_directory": str(tmp_path / "single_analysis"),
            "file_name": "cv_hold_sweep",
        }
        document["execution"] = {
            "expected_serial": "TESTSERIAL",
            "maximum_voltage_v": 4.2,
            "maximum_current_a": 4.0,
        }
        driver = case.smu
        driver.identity = "Keysight Technologies,B2902B,TESTSERIAL,TESTFW"
        driver.is_connected = False
        root = Path(document["output"]["root_directory"]) / "single_duration"
        def connect():
            progress = json.loads((root / "sweep.json").read_text(encoding="utf-8"))
            assert (root / progress["active_stage_id"] / "experiment.json").is_file()
            driver.is_connected = True
            return driver
        def close():
            driver.is_connected = False
        driver.connect = connect
        driver.close = close
        executor_class = single.RecordedPhaseExecutor
        backend_class = single.RecordedPreconditioningBackend
        def executor(smu, configuration, *args, **options):
            case.document = deepcopy(configuration)
            case.acquisition_plan = build_acquisition_plan(
                configuration, maximum_records=100000
            )
            return executor_class(
                smu, configuration, *args,
                clock=case.clock, sleep=case.clock.sleep, **options,
            )
        def backend(smu, **options):
            return backend_class(
                smu, clock=case.clock, sleep=case.clock.sleep, **options
            )
        monkeypatch.setattr(single, "_new_smu", lambda *args: driver)
        monkeypatch.setattr(single, "RecordedPhaseExecutor", executor)
        monkeypatch.setattr(single, "RecordedPreconditioningBackend", backend)
        yield document, driver
    finally:
        case.doCleanups()
@pytest.mark.parametrize("mode", ["from_cv_entry", "after_current_threshold"])
def test_single_duration_sweep_full_recording_and_export(
    single_rig, tmp_path, mode
):
    document, driver = single_rig
    document["charge"]["termination"]["mode"] = mode
    manifest, directory = execute_charge_current_sweep(
        document, sweep_id="single_duration"
    )
    assert manifest["state"] == "completed"
    assert manifest["completed_stage_count"] == 3
    checked = validate_sweep(directory)
    assert checked["valid"], checked["errors"]
    destination = tmp_path / "single_export"
    report = export_sweep(directory, destination)
    assert [row["cv_hold_time_s"] for row in report["cycles"]] == [1.0, 2.0]
    assert {row["cv_hold_time_s"] for row in report["hold_time_summary"]} == {1.0, 2.0}
    assert all(row["observation_count"] == 1 for row in report["hold_time_summary"])
    assert (destination / "hold_time_summary.csv").is_file()
    assert driver.is_connected is False
def test_repository_duration_examples_have_one_fixed_current_selection(monkeypatch):
    from experiments.config import load_config
    def forbidden(*args, **kwargs):
        pytest.fail("Example validation attempted instrument construction.")
    monkeypatch.setattr(single, "_new_smu", forbidden)
    monkeypatch.setattr(instrument_io, "_new_smu", forbidden)
    root = Path(__file__).resolve().parents[1]
    documents = []
    for channel in (1, 2):
        path = root / f"configs/cv_hold_sweep.ch{channel}.example.yaml"
        document = load_config(path)["config"]
        active = [
            key for key, value in document["charge"]["current"].items()
            if value is not None
        ]
        assert active == ["current_a"]
        fixed_current = document["charge"]["current"]["current_a"]
        plan = build_charge_current_sweep_plan(document)
        assert all(
            entry["charge_current_a"] == fixed_current
            for entry in plan["conditions"]
        )
        assert [
            entry["cv_hold_time_s"] for entry in plan["conditions"]
        ] == document["charge"]["termination"]["hold_time_s"]
        documents.append(document)
    plan = build_dual_plan(documents)
    assert plan["experiment_type"] == "dual_cv_hold_sweep"
    assert plan["will_connect"] is False
