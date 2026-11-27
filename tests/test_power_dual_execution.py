"""Dual-channel constant-power launcher and public dispatch; fake wire only, no VISA."""
from copy import deepcopy
import json
from pathlib import Path
import re
import pytest
import yaml
import smu
import test_charge_current_sweep
from analysis import export_shared_power
from experiments import power_dual_execution
from experiments.power_planning import build_dual_constant_power_sweep_plan
from experiments.power_shared_transport import SharedPowerSession
from test_constant_power_planning import power_profile
from test_power_shared_transport import Wire
profile = test_charge_current_sweep.profile
NAME = "20261127_220000_dual_cp"


@pytest.fixture(autouse=True)
def probe_ceiling(monkeypatch):
    monkeypatch.setattr(power_dual_execution, "probe_current_ceiling", lambda document: 0.001)
@pytest.fixture
def setup(profile, tmp_path):
    first = power_profile(profile)
    first["execution"] = {"expected_serial": "TESTSERIAL", "maximum_voltage_v": 3.81,
                          "maximum_current_a": 0.4}
    second = deepcopy(first)
    second["instrument"]["channel"] = 2
    second["instrument"]["nplc"] = 2
    second["dut"]["specimen_id"] = "second_separate_cell"
    documents = [first, second]
    for document in documents:
        document["output"].update(root_directory=str(tmp_path / "runs"),
                                  analysis_directory=str(tmp_path / "out"), file_name="dual_cp")
    plan = build_dual_constant_power_sweep_plan(documents)
    return documents, Wire(plan["resource"])
def launch(documents, driver, name=NAME):
    return power_dual_execution.run_configured_dual_power_sweep(
        documents, name, clock=driver.clock.now, sleep=driver.clock.sleep,
        smu_factory=lambda *args, **kwargs: driver)
def patch_engine(monkeypatch, behavior):
    seen = {}
    def stub(owner, directory, **kwargs):
        seen.update(owner=owner, directory=directory, kwargs=kwargs)
        return behavior(owner)
    monkeypatch.setattr(power_dual_execution, "run_shared_power_sweep", stub)
    return seen
def finished(owner):
    with owner:
        pass
    return {"state": "acquisition_completed_unreplayed", "completion_qualified": False,
            "owner_cleanup": owner.cleanup_report,
            "channels": [{"channel": c, "specimen_id": owner._profiles[c]["specimen_id"],
                          "state": "acquisition_completed_unreplayed", "completed_stage_count": 18}
                         for c in (1, 2)]}
def error_report(capsys):
    err = capsys.readouterr().err
    return json.loads(err[err.index("{"):])
def test_success_builds_the_shared_owner_and_prints_a_summary(setup, tmp_path, monkeypatch, capsys):
    documents, driver = setup
    seen = patch_engine(monkeypatch, finished)
    assert launch(documents, driver) == 0
    owner, kwargs = seen["owner"], seen["kwargs"]
    assert isinstance(owner, SharedPowerSession) and owner.output_authorized is True
    assert seen["directory"] == (tmp_path / "runs").absolute() / NAME
    assert kwargs["maximum_probe_current_a"] == {1: 0.001, 2: 0.001}
    assert kwargs["execute_requested"] is True
    assert kwargs["maximum_scheduler_steps"] == power_dual_execution.MAXIMUM_SCHEDULER_STEPS
    assert callable(kwargs["sleep"])
    assert driver.connect_count == 1 and driver.shutdown_count == 1 and driver.close_count == 1
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == "acquisition_completed_unreplayed" and out["physical_qualification"] is False
    assert [item["channel"] for item in out["channels"]] == [1, 2]
    assert out["outputs_reported_off"] is True and out["completion_qualified"] is False
def test_a_rejection_before_the_owner_claims_still_shuts_down_and_closes(setup, tmp_path, monkeypatch, capsys):
    documents, driver = setup
    def rejected(owner):
        raise ValueError("rejected before entry")
    patch_engine(monkeypatch, rejected)
    assert launch(documents, driver) == 1
    assert driver.connect_count == 1 and driver.shutdown_count == 1 and driver.close_count == 1
    report = error_report(capsys)
    assert report["error_type"] == "ValueError"
    assert report["directory"] == str((tmp_path / "runs").absolute() / NAME)
def test_a_failure_after_the_owner_claims_is_cleaned_up_once(setup, monkeypatch, capsys):
    documents, driver = setup
    def failing(owner):
        with owner:
            raise OSError("boom")
    patch_engine(monkeypatch, failing)
    assert launch(documents, driver) == 1
    assert driver.shutdown_count == 1 and driver.close_count == 1
    report = error_report(capsys)
    assert report["error_type"] == "OSError" and report["outputs_reported_off"] is True
def test_keyboard_interrupt_returns_130_and_closes_the_session(setup, monkeypatch):
    documents, driver = setup
    def interrupted(owner):
        raise KeyboardInterrupt
    patch_engine(monkeypatch, interrupted)
    assert launch(documents, driver) == 130
    assert driver.shutdown_count == 1 and driver.close_count == 1
def test_identity_mismatch_never_reaches_the_engine(setup, monkeypatch, capsys):
    documents, driver = setup
    driver.identity = "Keysight,B2902B,OTHER,FW"
    seen = patch_engine(monkeypatch, lambda owner: {})
    assert launch(documents, driver) == 1
    assert "owner" not in seen
    assert driver.shutdown_count == 1 and driver.close_count == 1
    assert "does not match" in error_report(capsys)["error_message"]
def test_bad_name_and_existing_destination_are_refused_before_connecting(setup, tmp_path, monkeypatch):
    documents, driver = setup
    seen = patch_engine(monkeypatch, lambda owner: {})
    assert launch(documents, driver, name="a b") == 1
    (tmp_path / "runs" / NAME).mkdir(parents=True)
    assert launch(documents, driver) == 1
    assert driver.connect_count == 0 and driver.commands == [] and "owner" not in seen
def cli(documents, tmp_path):
    args = []
    for index, document in enumerate(documents):
        path = tmp_path / f"cp_{index}.yaml"
        path.write_text(yaml.safe_dump(document))
        args += ["--config", str(path)]
    return args
def test_preview_reports_dual_execution_available_without_connecting(setup, tmp_path, capsys):
    documents, _ = setup
    assert smu.main(cli(documents, tmp_path) + ["--preview"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["execution_available"] is True and out["will_connect"] is False
    assert out["will_enable_output"] is False
    assert [item["channel"] for item in out["channels"]] == [1, 2]
def test_execute_dispatches_both_profiles_with_a_timestamped_name(setup, tmp_path, monkeypatch):
    documents, _ = setup
    seen = []
    monkeypatch.setattr(power_dual_execution, "run_configured_dual_power_sweep",
                        lambda docs, run_name: seen.append((docs, run_name)) or 0)
    assert smu.main(cli(documents, tmp_path) + ["--execute"]) == 0
    assert re.fullmatch(r"\d{8}_\d{6}_dual_cp", seen[0][1])
    assert [doc["instrument"]["channel"] for doc in seen[0][0]] == [1, 2]
def test_execute_then_analyze_replays_the_recording(setup, tmp_path, monkeypatch):
    documents, _ = setup
    calls = []
    def launcher(docs, run_name):
        (tmp_path / "runs" / run_name).mkdir(parents=True)
        calls.append(run_name)
        return 0
    def export(source, destination, *, expected_documents=None):
        calls.append((Path(source), Path(destination),
                      [doc["instrument"]["channel"] for doc in expected_documents]))
        return {"state": "completed"}
    monkeypatch.setattr(power_dual_execution, "run_configured_dual_power_sweep", launcher)
    monkeypatch.setattr(export_shared_power, "export_shared_power_analysis", export)
    assert smu.main(cli(documents, tmp_path) + ["--execute", "--analyze"]) == 0
    run_name = calls[0]
    assert calls[1] == (tmp_path / "runs" / run_name, tmp_path / "out" / run_name, [1, 2])
def test_a_failed_run_skips_the_replay(setup, tmp_path, monkeypatch, capsys):
    documents, _ = setup
    monkeypatch.setattr(power_dual_execution, "run_configured_dual_power_sweep",
                        lambda docs, run_name: 1)
    monkeypatch.setattr(export_shared_power, "export_shared_power_analysis",
                        lambda *args, **kwargs: pytest.fail("replay must be skipped"))
    assert smu.main(cli(documents, tmp_path) + ["--execute", "--analyze"]) == 1
    assert "analysis skipped" in capsys.readouterr().err
def test_analyze_needs_an_analysis_directory_in_every_profile(setup, tmp_path, monkeypatch, capsys):
    documents, _ = setup
    del documents[1]["output"]["analysis_directory"]
    monkeypatch.setattr(power_dual_execution, "run_configured_dual_power_sweep",
                        lambda docs, run_name: pytest.fail("must not run"))
    assert smu.main(cli(documents, tmp_path) + ["--execute", "--analyze"]) == 1
    assert "analysis_directory" in capsys.readouterr().err
