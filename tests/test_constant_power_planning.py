"""Power planning, offline analysis refusal and blocked unqualified execution."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
import yaml
import smu
from test_charge_current_sweep import profile
from experiments.config import load_config, validate_config
from experiments.power_planning import (
    build_constant_power_sweep_plan, build_dual_constant_power_sweep_plan,
)


@pytest.fixture(autouse=True)
def forbid_output_and_connections(monkeypatch):
    import experiments.run_live as live
    import experiments.dual_live as dual
    import experiments.dual_instrument_io as io
    import experiments.sweeps as sweeps
    def forbidden(*args, **kwargs):
        pytest.fail("Experimental power planning reached hardware or execution.")
    monkeypatch.setattr(live, "_new_smu", forbidden)
    monkeypatch.setattr(live, "execute_live", forbidden)
    monkeypatch.setattr(dual, "execute", forbidden)
    monkeypatch.setattr(io, "_new_smu", forbidden)
    monkeypatch.setattr(sweeps, "run_configured_sweep", forbidden)


def power_profile(profile):
    result = deepcopy(profile)
    result["experiment"]["type"] = "constant_power_discharge_sweep"
    result["sweep"] = {"power_w": [0.4, 0.8], "repeats": 2, "initial_discharge": True}
    result["power_control"] = {
        "maximum_current_a": 0.4, "update_interval_s": 0.2,
        "maximum_feedback_age_s": 0.5, "relative_tolerance": 0.05,
        "absolute_tolerance_w": 0.005, "startup_grace_s": 1,
        "maximum_tracking_error_s": 1,
    }
    return result


def save(document, path):
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return str(path)


def test_expansion_keeps_charge_and_preparation_and_input_order(profile):
    document = power_profile(profile)
    document["sweep"]["power_w"] = [0.8, 0.4]
    original = deepcopy(document)
    plan = build_constant_power_sweep_plan(document)
    assert [c["power_w"] for c in plan["conditions"]] == [0.8, 0.8, 0.4, 0.4]
    assert plan["initial_preparation"]["current_a"] == pytest.approx(0.4)
    assert plan["total_phase_count"] == 2 + 4 * 4 * document["experiment"]["cycles"]
    assert not plan["execution_available"] and not plan["will_connect"]
    base_charge = plan["initial_preparation"]["configuration"]["charge"]
    assert base_charge == document["charge"]
    for condition in plan["conditions"]:
        types = [p["type"] for p in condition["phases"]]
        assert types == ["charge_cc_cv", "rest", "discharge_cp", "rest"] * document["experiment"]["cycles"]
        for phase in condition["phases"]:
            if phase["type"] == "discharge_cp":
                assert "current_magnitude_a" not in phase
                assert phase["power_w"] == condition["power_w"]
                assert phase["controller_limits"]["maximum_current_a"] == 0.4
    assert document == original
    assert json.loads(json.dumps(plan, allow_nan=False)) == plan


@pytest.mark.parametrize("powers", [[], None, [0], [True], [float("inf")], [float("nan")], [0.4, 0.4], [0.4, 0.9]])
def test_invalid_or_later_impossible_power_rejected(profile, powers):
    document = power_profile(profile)
    document["sweep"]["power_w"] = powers
    with pytest.raises(ValueError):
        build_constant_power_sweep_plan(document)
    assert not Path(document["output"]["root_directory"]).exists()


@pytest.mark.parametrize("field,value", [
    ("maximum_current_a", 0.5), ("maximum_current_a", True),
    ("update_interval_s", 0.1), ("update_interval_s", "0.2"),
    ("maximum_feedback_age_s", 0.25), ("relative_tolerance", 1),
    ("absolute_tolerance_w", -1), ("startup_grace_s", 3600),
])
def test_bad_control_settings_rejected(profile, field, value):
    document = power_profile(profile)
    document["power_control"][field] = value
    with pytest.raises(ValueError):
        validate_config(document)


def test_acquisition_budget_and_explicit_execution_ceiling(profile):
    document = power_profile(profile)
    with pytest.raises(ValueError):
        build_constant_power_sweep_plan(document, maximum_records=10)
    document["execution"] = {
        "expected_serial": "TESTSERIAL", "maximum_voltage_v": 3.81,
        "maximum_current_a": 0.3,
    }
    with pytest.raises(ValueError, match="below"):
        build_constant_power_sweep_plan(document)


@pytest.mark.parametrize("flags", [[], ["--preview"]])
def test_public_preview_is_read_only(profile, tmp_path, capsys, flags):
    document = power_profile(profile)
    path = save(document, tmp_path / "power.yaml")
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert smu.main(["--config", path, *flags]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["experiment_type"] == "constant_power_discharge_sweep"
    assert result["execution_available"] is True and not result["will_connect"]
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("flags", [["--execute"], ["--execute", "--analyze"]])
def test_public_execution_dispatches_to_supervised_runner(profile, tmp_path, monkeypatch, flags):
    import experiments.power_execution as execution
    document = power_profile(profile)
    document["output"]["file_name"] = "cp_unit"
    path = save(document, tmp_path / "power.yaml")
    calls, analyzed = [], []
    def runner(doc, run_name):
        calls.append((doc, run_name))
        return 0
    monkeypatch.setattr(execution, "run_configured_power_sweep", runner)
    monkeypatch.setattr(smu, "_unified_power_analyze", lambda docs, name: analyzed.append(name) or 0)
    assert smu.main(["--config", path, *flags]) == 0
    assert len(calls) == 1
    assert calls[0][0]["experiment"]["type"] == "constant_power_discharge_sweep"
    assert calls[0][1].endswith("_cp_unit")
    assert analyzed == ([calls[0][1]] if "--analyze" in flags else [])
def test_failed_public_execution_skips_analysis(profile, tmp_path, monkeypatch, capsys):
    import experiments.power_execution as execution
    document = power_profile(profile)
    document["output"]["file_name"] = "cp_unit"
    path = save(document, tmp_path / "power.yaml")
    monkeypatch.setattr(execution, "run_configured_power_sweep", lambda doc, name: 1)
    def forbidden(*args, **kwargs):
        pytest.fail("Analysis must be skipped after a failed run.")
    monkeypatch.setattr(smu, "_unified_power_analyze", forbidden)
    assert smu.main(["--config", path, "--execute", "--analyze"]) == 1
    assert "analysis skipped" in capsys.readouterr().err.lower()
def test_execute_analyze_requires_analysis_directory_before_hardware(profile, tmp_path, monkeypatch, capsys):
    import experiments.power_execution as execution
    document = power_profile(profile)
    document["output"]["file_name"] = "cp_unit"
    del document["output"]["analysis_directory"]
    path = save(document, tmp_path / "power.yaml")
    def forbidden(*args, **kwargs):
        pytest.fail("Hardware must not run without an analysis directory.")
    monkeypatch.setattr(execution, "run_configured_power_sweep", forbidden)
    assert smu.main(["--config", path, "--execute", "--analyze"]) == 1
    assert "analysis_directory" in capsys.readouterr().err
def test_dual_preview_and_blocked_dual_execution(profile, tmp_path, capsys):
    first = power_profile(profile)
    second = deepcopy(first)
    second["instrument"]["channel"] = 2
    second["dut"]["specimen_id"] = "different_specimen"
    second["sweep"]["power_w"] = [0.4]
    paths = [save(d, tmp_path / f"power_{i}.yaml") for i, d in enumerate((first, second))]
    args = ["--config", paths[0], "--config", paths[1]]
    assert smu.main([*args, "--preview"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["experiment_type"] == "dual_constant_power_discharge_sweep"
    assert [p["condition_count"] for p in result["channels"]] == [4, 2]
    assert smu.main([*args, "--execute"]) == 1
    assert "planning only" in capsys.readouterr().err.lower()


def test_mixed_experiments_rejected(profile, tmp_path, capsys):
    args = ["--config", save(power_profile(profile), tmp_path / "cp.yaml"),
            "--config", save(profile, tmp_path / "cc.yaml"), "--preview"]
    assert smu.main(args) == 1
    assert "two constant-power" in capsys.readouterr().err.lower()


def test_duplicate_channel_and_unknown_fields_rejected(profile):
    document = power_profile(profile)
    with pytest.raises(ValueError, match="channel"):
        build_dual_constant_power_sweep_plan([document, document])
    document["sweep"]["discharge_current_a"] = [0.1, 0.2]
    with pytest.raises(ValueError, match="unknown fields"):
        validate_config(document)


def test_examples_match_declared_lists():
    root = Path(__file__).resolve().parents[1]
    documents = []
    for channel in (1, 2):
        doc = load_config(root / f"configs/constant_power_sweep.ch{channel}.example.yaml")["config"]
        plan = build_constant_power_sweep_plan(doc)
        expected = [p for p in doc["sweep"]["power_w"] for _ in range(doc["sweep"]["repeats"])]
        assert [c["power_w"] for c in plan["conditions"]] == expected
        assert not plan["execution_available"]
        documents.append(doc)
    assert not build_dual_constant_power_sweep_plan(documents)["execution_available"]


def test_public_analysis_missing_source_is_read_only(profile,tmp_path,capsys):
    document=power_profile(profile);path=save(document,tmp_path/"power.yaml")
    assert smu.main(["--config",path,"--analyze","missing_run"])==1
    assert "No recorded CP run" in capsys.readouterr().err
    assert not Path(document["output"]["root_directory"]).exists()
    assert not Path(document["output"]["analysis_directory"]).exists()
