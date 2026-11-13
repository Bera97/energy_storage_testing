"""Offline tests for charging-current sweep configuration and preview."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
import yaml
import smu
from experiments.config import load_config, validate_config
from experiments.sweeps import build_charge_current_sweep_plan
@pytest.fixture
def profile(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    document = yaml.safe_load(
        (repository / "configs/preconditioning.parallel.lic.test_run.yaml")
        .read_text(encoding="utf-8")
    )
    document.pop("execution", None)
    document["experiment"]["type"] = "charge_current_sweep"
    document["experiment"]["name"] = "test_charge_sweep"
    document["output"]["root_directory"] = str(tmp_path / "never_create_runs")
    document["output"]["analysis_directory"] = str(tmp_path / "never_create_analysis")
    document["sweep"] = {
        "charge_current_a": [0.1, 0.2],
        "repeats": 2,
        "initial_discharge": True,
    }
    return document
def save(profile, tmp_path):
    path = tmp_path / "sweep.yaml"
    path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    return path
def test_expansion_preserves_inputs_and_unswept_settings(profile):
    original = deepcopy(profile)
    plan = build_charge_current_sweep_plan(profile)
    assert profile == original
    assert plan["condition_count"] == 4
    assert [item["charge_current_a"] for item in plan["conditions"]] == [
        0.1, 0.1, 0.2, 0.2
    ]
    assert [item["repeat_index"] for item in plan["conditions"]] == [1, 2, 1, 2]
    assert [item["condition_id"] for item in plan["conditions"]] == [
        "condition_0001_repeat_0001",
        "condition_0001_repeat_0002",
        "condition_0002_repeat_0001",
        "condition_0002_repeat_0002",
    ]
    assert plan["condition_phase_count"] == (
        4 * profile["experiment"]["cycles"] * 4
    )
    assert plan["total_phase_count"] == 2 + plan["condition_phase_count"]
    assert json.loads(json.dumps(plan, allow_nan=False)) == plan
    for item in plan["conditions"]:
        config = item["config"]
        assert config["experiment"]["type"] == "preconditioning"
        assert config["experiment"]["cycles"] == profile["experiment"]["cycles"]
        assert "sweep" not in config
        for field in ("dut", "instrument", "acquisition", "discharge", "rest", "output"):
            assert config[field] == profile[field]
        assert config["charge"]["termination"] == profile["charge"]["termination"]
        currents = item["preview"]["acquisition_plan"]["resolved_currents"]
        assert currents["charge_current_a"] == item["charge_current_a"]
        assert currents["charge_termination_current_a"] == pytest.approx(
            item["charge_current_a"]
            * profile["charge"]["termination"]["current_factor"]
        )
    plan["conditions"][0]["config"]["rest"]["after_charge_s"] = 999
    assert plan["conditions"][1]["config"]["rest"] == profile["rest"]
    assert profile == original
@pytest.mark.parametrize("values", [
    [], None, "0.1", [True], [0], [-0.1],
    [float("nan")], [float("inf")], ["0.1"],
    [0.1, 0.1], [0.1, 0.5],
])
def test_invalid_currents_are_rejected(profile, values):
    profile["sweep"]["charge_current_a"] = values
    with pytest.raises(ValueError):
        build_charge_current_sweep_plan(profile)
@pytest.mark.parametrize("repeats", [True, 0, -1, 1.5, "2", 1001])
def test_invalid_repeats_are_rejected(profile, repeats):
    profile["sweep"]["repeats"] = repeats
    with pytest.raises(ValueError):
        validate_config(profile)
@pytest.mark.parametrize("section", ["sweep", "experiment"])
def test_unknown_fields_are_rejected(profile, section):
    profile[section]["unexpected"] = 1
    with pytest.raises(ValueError, match="unknown fields"):
        validate_config(profile)
def test_invalid_later_condition_rejects_the_whole_plan(profile):
    profile["sweep"]["charge_current_a"] = [0.1, 0.5]
    with pytest.raises(ValueError, match="condition 2"):
        build_charge_current_sweep_plan(profile)
def test_acquisition_budget_is_validated(profile):
    profile["acquisition"]["interval_s"] = 0.001
    with pytest.raises(ValueError, match="exceed"):
        build_charge_current_sweep_plan(profile)
def test_explicit_lower_ceiling_is_not_increased(profile):
    profile["execution"] = {
        "expected_serial": "MY61391173",
        "maximum_voltage_v": 3.81,
        "maximum_current_a": 0.15,
    }
    with pytest.raises(ValueError, match="below"):
        build_charge_current_sweep_plan(profile)
def test_missing_monitoring_is_rejected_by_full_planning(profile):
    del profile["acquisition"]["maximum_sample_gap_s"]
    with pytest.raises(ValueError, match="monitoring"):
        build_charge_current_sweep_plan(profile)
def test_duplicate_yaml_keys_are_rejected(profile, tmp_path):
    path = save(profile, tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8") + "\nsweep: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Duplicate"):
        load_config(path)
@pytest.mark.parametrize("flags", [[], ["--preview"]])
def test_public_preview_does_not_execute_or_write(
    profile, tmp_path, monkeypatch, capsys, flags
):
    import experiments.run_live as single
    import experiments.dual_live as dual
    path = save(profile, tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    def forbidden(*args, **kwargs):
        pytest.fail("Sweep preview reached hardware or execution.")
    monkeypatch.setattr(single, "_new_smu", forbidden)
    monkeypatch.setattr(single, "execute_live", forbidden)
    monkeypatch.setattr(dual, "execute", forbidden)
    assert smu.main(["--config", str(path), *flags]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["condition_count"] == 4
    assert plan["execution_enabled"] is False
    assert plan["will_connect"] is False
    assert plan["will_enable_output"] is False
    after = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before
    assert not Path(profile["output"]["root_directory"]).exists()
    assert not Path(profile["output"]["analysis_directory"]).exists()
@pytest.mark.parametrize("flags", [
    ["--execute"],
    ["--execute", "--analyze"],
    ["--analyze", "existing_run"],
])
def test_unimplemented_execution_and_analysis_are_blocked(
    profile, tmp_path, monkeypatch, capsys, flags
):
    import experiments.run_live as single
    path = save(profile, tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("Preview-only sweep attempted execution or analysis.")
    monkeypatch.setattr(single, "_new_smu", forbidden)
    monkeypatch.setattr(single, "run_configured", forbidden)
    monkeypatch.setattr(smu, "_unified_analyze", forbidden)
    assert smu.main(["--config", str(path), *flags]) == 1
    assert "preview-only" in capsys.readouterr().err
def test_multiple_profiles_are_rejected(profile, tmp_path, capsys):
    path = save(profile, tmp_path)
    assert smu.main([
        "--config", str(path), "--config", str(path), "--preview",
    ]) == 1
    assert "exactly one profile" in capsys.readouterr().err
def test_normal_preconditioning_still_validates(profile):
    del profile["sweep"]
    profile["experiment"]["type"] = "preconditioning"
    assert validate_config(profile)["config"] == profile
def test_normal_preconditioning_does_not_accept_sweep_fields(profile):
    profile["experiment"]["type"] = "preconditioning"
    with pytest.raises(ValueError, match="unknown fields"):
        validate_config(profile)
def test_initial_discharge_uses_existing_profile_settings(profile):
    original = deepcopy(profile)
    plan = build_charge_current_sweep_plan(profile)
    initial = plan["initial_discharge"]
    assert initial["stage_id"] == "initial_discharge"
    assert initial["state"] == "planned"
    assert initial["phase_count"] == 2
    assert [phase["type"] for phase in initial["phases"]] == [
        "discharge_cc", "rest",
    ]
    assert initial["current_a"] == pytest.approx(0.4)
    assert initial["cutoff_voltage_v"] == profile["discharge"]["cutoff_voltage_v"]
    assert initial["maximum_duration_s"] == profile["discharge"]["maximum_duration_s"]
    assert initial["rest_after_s"] == profile["rest"]["after_discharge_s"]
    assert initial["phases"][1]["duration_s"] == initial["rest_after_s"]
    assert initial["phases"][1]["requires_verified_output_off"] is True
    assert initial["phases"][0]["acquisition_budget"]["record_count"] > 1
    assert profile == original
@pytest.mark.parametrize("value", [False, None, 0, 1, "true"])
def test_initial_discharge_requires_explicit_boolean_true(profile, value):
    profile["sweep"]["initial_discharge"] = value
    with pytest.raises(ValueError, match="initial_discharge"):
        validate_config(profile)
def test_missing_initial_discharge_policy_is_rejected(profile):
    del profile["sweep"]["initial_discharge"]
    with pytest.raises(ValueError, match="initial_discharge"):
        validate_config(profile)
def test_initial_discharge_occurs_once_not_once_per_repeat(profile):
    profile["sweep"]["repeats"] = 3
    profile["experiment"]["cycles"] = 2
    plan = build_charge_current_sweep_plan(profile)
    assert plan["condition_count"] == 6
    assert plan["condition_phase_count"] == 48
    assert plan["initial_discharge"]["phase_count"] == 2
    assert plan["total_phase_count"] == 50
