"""Offline dual-sweep tests using the concrete dual hardware adapter.
Instrument operations and sleeping are fake. No real output is enabled.
"""
from copy import deepcopy
import json
from pathlib import Path
import pytest
import yaml
import smu
import experiments.dual_live as live
import experiments.dual_instrument_io as io
import test_dual_live as dual_tests
from analysis.dual_experiment import check_batch, export_dual_sweep
from experiments.dual_preconditioning import build_dual_plan
from experiments.sweeps import build_dual_charge_current_sweep_plan
pair = dual_tests.pair
rig = dual_tests.rig
@pytest.fixture
def sweep_rig(rig, tmp_path):
    base, driver, options = rig
    documents = [
        deepcopy(preview["acquisition_plan"]["configuration"])
        for preview in base["channels"]
    ]
    for document in documents:
        document["experiment"]["type"] = "charge_current_sweep"
        document["charge"]["termination"]["current_reference"] = "configured_charge_current"
        document["charge"]["termination"]["current_factor"] = 0.5
        document["output"] = {
            "root_directory": str(tmp_path / "runs"),
            "analysis_directory": str(tmp_path / "analysis"),
            "file_name": "dual_sweep",
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
        document["sweep"] = {
            "charge_current_a": [0.2, 0.4],
            "repeats": 2,
            "initial_discharge": True,
        }
    documents[1]["sweep"]["charge_current_a"] = [0.3]
    documents[1]["sweep"]["repeats"] = 1
    return documents, driver, options
def test_dual_sweep_plan_is_independent_and_hardware_free(sweep_rig):
    documents, driver, _ = sweep_rig
    original = deepcopy(documents)
    plan = build_dual_plan(documents)
    assert plan == build_dual_charge_current_sweep_plan(documents)
    assert plan["experiment_type"] == "dual_charge_current_sweep"
    assert plan["will_connect"] is False
    assert plan["will_enable_output"] is False
    assert [
        preview["sweep_plan"]["condition_count"]
        for preview in plan["channels"]
    ] == [4, 1]
    assert [
        preview["acquisition_plan"]["phase_count"]
        for preview in plan["channels"]
    ] == [18, 6]
    assert plan["total_phase_count"] == 24
    for preview in plan["channels"]:
        phases = preview["acquisition_plan"]["phases"]
        assert [phase["phase_index"] for phase in phases] == list(range(len(phases)))
        assert [phase["type"] for phase in phases[:2]] == ["discharge_cc", "rest"]
    assert documents == original
    assert driver.is_connected is False
    assert not Path(documents[0]["output"]["root_directory"]).exists()
@pytest.mark.parametrize("problem", ["channel", "specimen", "resource", "root", "serial", "ceiling"])
def test_invalid_dual_sweep_rejects_before_connection(sweep_rig, problem):
    documents, driver, _ = sweep_rig
    if problem == "channel":
        documents[1]["instrument"]["channel"] = 1
    elif problem == "specimen":
        documents[1]["dut"]["specimen_id"] = documents[0]["dut"]["specimen_id"]
    elif problem == "resource":
        documents[1]["instrument"]["visa_address"] = "OTHER::INSTR"
    elif problem == "root":
        documents[1]["output"]["root_directory"] += "_different"
    elif problem == "serial":
        documents[1]["execution"]["expected_serial"] = "WRONG"
    else:
        documents[1]["execution"]["maximum_current_a"] = 0.001
    with pytest.raises(ValueError):
        build_dual_plan(documents)
    assert driver.is_connected is False
def test_concrete_dual_sweep_records_and_analyzes_both_channels(sweep_rig, tmp_path):
    documents, driver, options = sweep_rig
    plan = build_dual_plan(documents)
    directory = tmp_path / "batch"
    report = live.execute(plan, directory, "TESTSERIAL", **options)
    assert report["state"] == "completed"
    assert report["session_close_returned"] is True
    assert driver.closed is True
    assert driver.chan1.on is False
    assert driver.chan2.on is False
    validation = check_batch(directory)
    assert validation["valid"], validation["errors"]
    assert validation["phase_recordings_checked"] == 12
    initial_finishes = [
        channel["outcomes"][1]["finished_s"]
        for channel in report["channels"]
    ]
    first_charge_starts = [
        channel["outcomes"][2]["started_s"]
        for channel in report["channels"]
    ]
    assert min(first_charge_starts) >= max(initial_finishes)
    destination = tmp_path / "export"
    result = export_dual_sweep(directory, destination)
    assert len(result["phases"]) == 12
    assert len(result["cycles"]) == 5
    assert [row["channel"] for row in result["cycles"]].count(1) == 4
    assert [row["channel"] for row in result["cycles"]].count(2) == 1
    assert (destination / "ANALYSIS_COMPLETE").is_file()
    for row in result["phases"]:
        assert (destination / row["analysis_directory"] / "summary.json").is_file()
        metadata = json.loads(
            (Path(row["source_directory"]) / "metadata.json").read_text(encoding="utf-8")
        )
        if row["type"] == "charge_cc_cv":
            assert metadata["metadata"]["configuration"]["charge"]["current"][
                "current_a"
            ] == row["charge_current_a"]
    assert all(
        row["condition_id"] != "initial_discharge" for row in result["cycles"]
    )
    assert {row["channel"] for row in result["current_summary"]} == {1, 2}
def test_dual_sweep_bus_fault_stops_both(sweep_rig, tmp_path, monkeypatch):
    documents, driver, options = sweep_rig
    original = driver._query
    def fail(command):
        if command.startswith(":TRAC2:DATA?"):
            raise OSError("synthetic sweep bus fault")
        return original(command)
    monkeypatch.setattr(driver, "_query", fail)
    directory = tmp_path / "failed"
    with pytest.raises(OSError, match="sweep bus fault"):
        live.execute(build_dual_plan(documents), directory, "TESTSERIAL", **options)
    assert driver.closed is True
    assert driver.chan1.on is False
    assert driver.chan2.on is False
    assert not (directory / "DUAL_COMPLETE").exists()
    assert check_batch(directory)["valid"] is False
def test_dual_sweep_cli_preview_never_constructs_hardware(
    sweep_rig, tmp_path, monkeypatch, capsys
):
    documents, _, _ = sweep_rig
    def forbidden(*args, **kwargs):
        pytest.fail("Preview attempted instrument construction.")
    monkeypatch.setattr(io, "_new_smu", forbidden)
    arguments = ["--preview"]
    for channel, document in enumerate(documents, 1):
        path = tmp_path / f"sweep_{channel}.yaml"
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
        arguments.extend(["--config", str(path)])
    assert smu.main(arguments) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["experiment_type"] == "dual_charge_current_sweep"
    assert preview["will_connect"] is False
def test_dual_sweep_cli_execute_and_analyze(
    sweep_rig, tmp_path, monkeypatch, capsys
):
    documents, _, options = sweep_rig
    original_execute = live.execute
    def execute(plan, directory, serial):
        return original_execute(plan, directory, serial, **options)
    monkeypatch.setattr(live, "execute", execute)
    monkeypatch.setattr(smu, "_unified_run_name", lambda document: "dual_sweep_test")
    arguments = ["--execute", "--analyze"]
    for channel, document in enumerate(documents, 1):
        path = tmp_path / f"live_sweep_{channel}.yaml"
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
        arguments.extend(["--config", str(path)])
    assert smu.main(arguments) == 0
    captured = capsys.readouterr()
    assert "CH1" in captured.out
    assert "CH2" in captured.out
    assert "Q(mAh)" in captured.out
    assert "E_mag(J)" in captured.out
    destination = Path(documents[0]["output"]["analysis_directory"]) / "dual_sweep_test"
    assert (destination / "ANALYSIS_COMPLETE").is_file()
