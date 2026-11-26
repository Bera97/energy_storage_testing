"""Leakage-current planning: schedule, budget, validation and read-only preview."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
import yaml
import smu
import test_charge_current_sweep
from experiments.config import load_config, validate_config
from experiments.leakage_planning import build_leakage_plan, sample_times
profile = test_charge_current_sweep.profile
SCHEDULE = [
    {"until_s": 900, "interval_s": 0.25},
    {"until_s": 57600, "interval_s": 1},
    {"until_s": 86400, "interval_s": 2},
    {"until_s": 172800, "interval_s": 5},
    {"until_s": 259200, "interval_s": 15},
]
EARLY_STOP = {
    "enabled": True, "minimum_duration_s": 86400, "window_s": 7200,
    "relative_change": 0.01, "check_interval_s": 1800, "consecutive_checks": 3,
}
@pytest.fixture(autouse=True)
def forbid_connections(monkeypatch):
    import experiments.run_live as live
    import experiments.dual_live as dual
    def forbidden(*args, **kwargs):
        pytest.fail("Leakage planning reached hardware or execution.")
    monkeypatch.setattr(live, "_new_smu", forbidden)
    monkeypatch.setattr(live, "execute_live", forbidden)
    monkeypatch.setattr(dual, "execute", forbidden)
def leakage_profile(base):
    doc = deepcopy(base)
    doc.pop("sweep", None)
    doc["experiment"]["type"] = "leakage_current_test"
    doc["experiment"]["cycles"] = 1
    doc["leakage"] = {"maximum_duration_s": 259200, "schedule": deepcopy(SCHEDULE)}
    return doc
def save(document, path):
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return str(path)
def test_default_schedule_meets_requested_sample_count(profile):
    plan = build_leakage_plan(leakage_profile(profile))
    schedule = plan["schedule"]
    assert [s["sample_count"] for s in schedule["segments"]] == [3600, 56700, 14400, 17280, 5760]
    assert schedule["total_sample_count"] == 97741
    assert 97000 <= schedule["total_sample_count"] <= 99999
    assert schedule["unused_records"] == 100000 - 97741
    assert plan["report_times_s"] == [3600.0, 36000.0, 86400.0, 172800.0, 259200.0]
    assert plan["early_stop"] == {"enabled": False}
    assert plan["execution_available"] and not plan["will_connect"]
    assert json.loads(json.dumps(plan, allow_nan=False)) == plan
def test_sample_times_are_deterministic_and_monotonic(profile):
    segments = build_leakage_plan(leakage_profile(profile))["schedule"]["segments"]
    times = list(sample_times(segments))
    assert len(times) == 97741
    assert times[0] == 0.0 and times[1] == 0.25
    assert times[3600] == pytest.approx(900.0) and times[3601] == pytest.approx(901.0)
    assert times[-1] == pytest.approx(259200.0)
    assert all(b > a for a, b in zip(times, times[1:]))
def test_record_budget_is_enforced(profile):
    doc = leakage_profile(profile)
    doc["leakage"]["schedule"] = [{"until_s": 259200, "interval_s": 1}]
    with pytest.raises(ValueError, match="limit"):
        validate_config(doc)
    with pytest.raises(ValueError, match="limit"):
        build_leakage_plan(leakage_profile(profile), maximum_records=50000)
def test_duration_is_capped_at_72_hours(profile):
    doc = leakage_profile(profile)
    doc["leakage"]["maximum_duration_s"] = 259201
    doc["leakage"]["schedule"][-1]["until_s"] = 259201
    with pytest.raises(ValueError, match="72 h"):
        validate_config(doc)
@pytest.mark.parametrize("schedule", [
    None, [], "x",
    [{"until_s": 100, "interval_s": 1}, {"until_s": 100, "interval_s": 1}],
    [{"until_s": 100, "interval_s": 1}],
    [{"until_s": 259200, "interval_s": 0.01}],
    [{"until_s": 259200, "interval_s": 15, "extra": 1}],
    [{"until_s": 259200, "interval_s": 0}],
    [{"until_s": 259200, "interval_s": True}],
])
def test_invalid_schedule_rejected(profile, schedule):
    doc = leakage_profile(profile)
    doc["leakage"]["schedule"] = schedule
    with pytest.raises(ValueError):
        validate_config(doc)
def test_cycles_must_be_one(profile):
    doc = leakage_profile(profile)
    doc["experiment"]["cycles"] = 2
    with pytest.raises(ValueError, match="cycles"):
        validate_config(doc)
def test_early_stop_options(profile):
    doc = leakage_profile(profile)
    doc["leakage"]["early_stop"] = deepcopy(EARLY_STOP)
    assert build_leakage_plan(doc)["early_stop"]["enabled"] is True
    for change in ({"window_s": 50000}, {"relative_change": 1.5},
                   {"consecutive_checks": 0}, {"minimum_duration_s": 259200}):
        bad = deepcopy(doc)
        bad["leakage"]["early_stop"].update(change)
        with pytest.raises(ValueError):
            validate_config(bad)
    missing = deepcopy(doc)
    del missing["leakage"]["early_stop"]["window_s"]
    with pytest.raises(ValueError, match="requires"):
        validate_config(missing)
def test_public_preview_is_read_only(profile, tmp_path, capsys):
    path = save(leakage_profile(profile), tmp_path / "leak.yaml")
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert smu.main(["--config", path]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["experiment_type"] == "leakage_current_test"
    assert result["schedule"]["total_sample_count"] == 97741
    assert result["execution_available"] and not result["will_connect"]
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
def test_dual_leakage_is_rejected(profile, tmp_path, capsys):
    first = leakage_profile(profile)
    second = deepcopy(first)
    second["instrument"]["channel"] = 2
    second["dut"]["specimen_id"] = "different_specimen"
    args = ["--config", save(first, tmp_path / "a.yaml"), "--config", save(second, tmp_path / "b.yaml"), "--preview"]
    assert smu.main(args) == 1
    assert "exactly one" in capsys.readouterr().err.lower()
def test_example_profile_matches_requested_schedule():
    root = Path(__file__).resolve().parents[1]
    doc = load_config(root / "configs/leakage_current_trial_7_8_ch1.yaml")["config"]
    plan = build_leakage_plan(doc)
    assert plan["schedule"]["total_sample_count"] == 97741
    assert plan["maximum_duration_s"] == 259200
    assert plan["early_stop"]["enabled"] is False
