"""Profile discharge planning/preview: no execution, no hardware."""
import json
from pathlib import Path
import pytest
import yaml
import smu
from experiments.config import load_config, validate_config
from experiments.power_planning import (
    build_constant_power_sweep_plan, build_dual_constant_power_sweep_plan,
)
from experiments.profile_control import ProfileLimits
CONFIGS = Path(__file__).resolve().parents[1] / "configs"
CURRENT_CH1 = CONFIGS / "profile_current_trial_7_8_ch1.yaml"
CURRENT_CH2 = CONFIGS / "profile_current_trial_5_6_ch2.yaml"
POWER_CH1 = CONFIGS / "profile_power_trial_7_8_ch1.yaml"
CP_CH1 = CONFIGS / "constant_power_trial_7_8_ch1.yaml"
def document(path):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
@pytest.mark.parametrize("path", [CURRENT_CH1, CURRENT_CH2, POWER_CH1])
def test_example_profiles_load(path):
    loaded = load_config(path)
    assert loaded["config"]["experiment"]["type"] == "profile_discharge_test"
    assert ProfileLimits(**loaded["profile_limits"]).profile_kind in ("current", "power")
def test_plan_has_one_condition_and_profile_discharge_phases():
    plan = build_constant_power_sweep_plan(document(CURRENT_CH1))
    assert plan["experiment_type"] == "profile_discharge_test" and plan["condition_count"] == 1
    condition = plan["conditions"][0]
    assert [p["type"] for p in condition["phases"]] == ["charge_cc_cv", "rest", "discharge_profile", "rest"]
    assert [p["type"] for p in plan["initial_preparation"]["phases"]] == ["discharge_cc", "rest"]
    assert plan["total_phase_count"] == 6 and condition["power_w"] is None
    assert "power_w" not in condition["phases"][2]
    assert condition["phases"][0]["cv_hold"]["mode"] == "after_current_threshold"
    json.dumps(plan, allow_nan=False)
def test_cycles_multiply_the_profile_phases():
    config = document(CURRENT_CH1)
    config["experiment"]["cycles"] = 2
    plan = build_constant_power_sweep_plan(config)
    types = [p["type"] for p in plan["conditions"][0]["phases"]]
    assert types.count("discharge_profile") == 2 and plan["total_phase_count"] == 10
def test_phase_limits_round_trip_and_describe_the_profile():
    plan = build_constant_power_sweep_plan(document(CURRENT_CH1))
    phase = plan["conditions"][0]["phases"][2]
    limits = ProfileLimits(**phase["controller_limits"])
    assert limits.profile_steps == ((20, 0.3), (10, 0.0), (20, 0.15))
    assert phase["profile"]["cycle_s"] == 50 and phase["profile"]["kind"] == "current"
def test_power_profile_reports_worst_case_current_at_cutoff():
    plan = build_constant_power_sweep_plan(document(POWER_CH1))
    assert plan["unit"] == "W"
    assert plan["conditions"][0]["worst_case_requested_current_a"] == pytest.approx(1.0 / 3.4)
def test_dual_preview_accepts_two_channel_profiles():
    plan = build_dual_constant_power_sweep_plan([document(CURRENT_CH1), document(CURRENT_CH2)])
    assert [c["channel"] for c in plan["channels"]] == [1, 2] and plan["total_phase_count"] == 12
BAD = [
    lambda d: d["profile"].update(kind="voltage"),
    lambda d: d["profile"].update(steps=[]),
    lambda d: d["profile"]["steps"][0].update(duration_s=0),
    lambda d: d["profile"]["steps"][0].update(duration_s=1.5),
    lambda d: d["profile"]["steps"][0].update(value=-0.1),
    lambda d: d["profile"]["steps"][0].update(value=0.5),
    lambda d: d["profile"].update(steps=[{"duration_s": 5, "value": 0.0}]),
    lambda d: d["profile"].update(extra=1),
    lambda d: d["profile_control"].pop("zero_current_tolerance_a"),
    lambda d: d.pop("profile_control"),
    lambda d: d.update(sweep={"power_w": [1.0], "repeats": 1, "initial_discharge": True}),
    lambda d: d["profile_control"].update(update_interval_s=2.0),
]
@pytest.mark.parametrize("change", BAD)
def test_invalid_profiles_are_rejected(change):
    broken = document(CURRENT_CH1)
    change(broken)
    with pytest.raises(ValueError):
        validate_config(broken)
    with pytest.raises(ValueError):
        build_constant_power_sweep_plan(broken)
def test_cli_preview_marks_profile_execution_unavailable(capsys):
    assert smu.main(["--config", str(CURRENT_CH1)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["execution_available"] is False and plan["offline_analysis_available"] is False
    assert plan["experiment_type"] == "profile_discharge_test"
def test_cli_dual_preview_marks_every_channel_available(capsys):
    assert smu.main(["--config", str(CURRENT_CH1), "--config", str(CURRENT_CH2)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert [c["execution_available"] for c in plan["channels"]] == [True, True]
@pytest.mark.parametrize("extra", [["--execute"], ["--analyze", "some_run"]])
def test_cli_refuses_profile_execution_and_analysis(extra, capsys):
    assert smu.main(["--config", str(CURRENT_CH1), *extra]) == 1
    assert "not implemented" in capsys.readouterr().err
def test_cli_refuses_mixing_profile_and_cp_profiles(capsys):
    assert smu.main(["--config", str(CURRENT_CH1), "--config", str(CP_CH1)]) == 1
    assert "mix" in capsys.readouterr().err
