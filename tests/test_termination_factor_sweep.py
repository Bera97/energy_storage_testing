"""Offline termination-factor sweep planning, recording, validation and export."""
from copy import deepcopy
import csv
import json
from pathlib import Path
import pytest
import yaml
import smu
import experiments.run_live as single
import experiments.dual_live as dual
import experiments.dual_instrument_io as instrument_io
from test_cv_hold_sweep import pair, rig, documents, single_rig
from experiments.config import validate_config, load_config
from experiments.sweeps import build_charge_current_sweep_plan, execute_charge_current_sweep
from experiments.dual_preconditioning import build_dual_plan
from analysis.export_experiment import validate_sweep, export_sweep, _sweep_group_rows
from analysis.dual_experiment import check_batch, export_dual_sweep

KIND = "termination_current_factor_sweep"


def factor_document(document, factors=(0.1, 0.5), hold=1.0):
    result = deepcopy(document)
    result["experiment"]["type"] = KIND
    result["charge"]["termination"] = {
        "mode": "after_current_threshold",
        "current_reference": "configured_charge_current",
        "current_factor": list(factors),
        "hold_time_s": hold,
    }
    return result


def test_expansion_preserves_fixed_settings_and_input(single_rig):
    document, driver = single_rig
    document = factor_document(document, (0.5, 0.1, 0.25))
    document["sweep"]["repeats"] = 2
    before = deepcopy(document)
    plan = build_charge_current_sweep_plan(document)
    assert plan["parameter"] == "charge.termination.current_factor"
    assert plan["unit"] == "1"
    assert plan["ordering"] == "initial_discharge_then_supplied_factor_order_then_repeat"
    assert plan["condition_count"] == 6
    assert [c["termination_current_factor"] for c in plan["conditions"]] == [0.5, 0.5, 0.1, 0.1, 0.25, 0.25]
    for c in plan["conditions"]:
        scalar = deepcopy(before)
        scalar.pop("sweep")
        scalar["experiment"]["type"] = "preconditioning"
        scalar["charge"]["termination"]["current_factor"] = c["termination_current_factor"]
        assert c["config"] == scalar
        assert c["cv_hold_time_s"] == 1.0
        assert c["termination_current_a"] == pytest.approx(c["charge_current_a"] * c["termination_current_factor"])
    assert document == before
    assert driver.is_connected is False


@pytest.mark.parametrize("values", [None, [], 0.1, [0], [-1], [True], ["0.1"], [float("nan")], [float("inf")], [1], [1.1], [0.1, 0.1], [0.1, 1]])
def test_invalid_factors_reject_before_execution(single_rig, values):
    document, driver = single_rig
    document = factor_document(document)
    document["charge"]["termination"]["current_factor"] = values
    with pytest.raises(ValueError):
        build_charge_current_sweep_plan(document)
    assert driver.is_connected is False
    assert not Path(document["output"]["root_directory"]).exists()


@pytest.mark.parametrize("repeat", [0, -1, True, 1.5, 501])
def test_invalid_or_oversized_repeats(single_rig, repeat):
    document, _ = single_rig
    document = factor_document(document)
    document["sweep"]["repeats"] = repeat
    with pytest.raises(ValueError):
        validate_config(document)


@pytest.mark.parametrize("change", ["mode", "reference", "duration_list", "current_list", "second_axis", "initial_discharge", "legacy_hold"])
def test_unsupported_combinations(single_rig, change):
    document, driver = single_rig
    document = factor_document(document)
    termination = document["charge"]["termination"]
    if change == "mode":
        termination["mode"] = "from_cv_entry"
    elif change == "reference":
        termination["current_reference"] = "rated_max_charge_current"
    elif change == "duration_list":
        termination["hold_time_s"] = [1, 2]
    elif change == "current_list":
        document["charge"]["current"] = {"current_a": [0.1, 0.2]}
    elif change == "second_axis":
        document["sweep"]["charge_current_a"] = [0.1, 0.2]
    elif change == "initial_discharge":
        document["sweep"]["initial_discharge"] = False
    else:
        document["charge"]["cv_hold"] = {"mode": "from_cv_entry", "hold_time_s": 1}
    with pytest.raises(ValueError):
        validate_config(document)
    assert driver.is_connected is False


def test_single_factor_recording_validation_and_exports(single_rig, tmp_path):
    document, driver = single_rig
    document = factor_document(document)
    document["sweep"]["repeats"] = 2
    manifest, directory = execute_charge_current_sweep(document, sweep_id="single_duration")
    assert manifest["state"] == "completed"
    assert manifest["completed_stage_count"] == 5
    assert "termination_current_factor" not in manifest["stages"][0]
    checked = validate_sweep(directory)
    assert checked["valid"], checked["errors"]
    destination = tmp_path / "factor_export"
    report = export_sweep(directory, destination)
    assert [r["termination_current_factor"] for r in report["cycles"]] == [0.1, 0.1, 0.5, 0.5]
    assert {r["termination_current_factor"] for r in report["termination_factor_summary"]} == {0.1, 0.5}
    assert all(r["observation_count"] == 2 for r in report["termination_factor_summary"])
    assert (destination / "ANALYSIS_COMPLETE").is_file()
    for filename in ("summary.csv", "cycle_comparison.csv", "termination_factor_summary.csv"):
        with (destination / filename).open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert rows
        assert "termination_current_factor" in rows[0]
        assert "termination_current_a" in rows[0]
    for row in report["cycles"]:
        assert row["termination_current_a"] == pytest.approx(row["charge_current_a"] * row["termination_current_factor"])
    assert driver.is_connected is False


@pytest.mark.parametrize("field", ["termination_current_factor", "termination_current_a"])
def test_manifest_factor_tampering_is_rejected(single_rig, field):
    document, _ = single_rig
    document = factor_document(document)
    manifest, directory = execute_charge_current_sweep(document, sweep_id="single_duration")
    manifest["stages"][1][field] += 0.01
    (directory / "sweep.json").write_text(json.dumps(manifest), encoding="utf-8")
    checked = validate_sweep(directory)
    assert not checked["valid"]
    assert any("identity" in error for error in checked["errors"])


def test_dual_factor_recording_and_export(documents, tmp_path):
    profiles, driver, options = documents
    profiles = [factor_document(profiles[0], (0.1, 0.5), 0.4), factor_document(profiles[1], (0.25,), 0.6)]
    plan = build_dual_plan(profiles)
    assert plan["experiment_type"] == "dual_termination_current_factor_sweep"
    assert plan["initial_preparation_barrier"] is True
    directory = tmp_path / "dual_factor"
    result = dual.execute(plan, directory, "TESTSERIAL", **options)
    assert result["state"] == "completed"
    assert driver.closed is True
    assert driver.chan1.on is False and driver.chan2.on is False
    checked = check_batch(directory)
    assert checked["valid"], checked["errors"]
    destination = tmp_path / "dual_export"
    report = export_dual_sweep(directory, destination)
    assert len(report["cycles"]) == 5
    for channel, factors in ((1, {0.1, 0.5}), (2, {0.25})):
        assert {row["termination_current_factor"] for row in report["cycles"] if row["channel"] == channel} == factors
    assert all(row["observation_count"] == (2 if row["channel"] == 1 else 1) for row in report["termination_factor_summary"])
    assert (destination / "termination_factor_summary.csv").is_file()
    assert (destination / "ANALYSIS_COMPLETE").is_file()


def test_cli_single_and_dual_preview_never_construct_hardware(documents, tmp_path, monkeypatch, capsys):
    profiles, _, _ = documents
    profiles = [factor_document(profiles[0], (0.1, 0.5), 0.4), factor_document(profiles[1], (0.25,), 0.6)]
    def forbidden(*args, **kwargs):
        pytest.fail("Offline preview attempted instrument construction")
    monkeypatch.setattr(single, "_new_smu", forbidden)
    monkeypatch.setattr(instrument_io, "_new_smu", forbidden)
    paths = []
    for i, profile in enumerate(profiles):
        path = tmp_path / f"factor_{i}.yaml"
        path.write_text(yaml.safe_dump(profile), encoding="utf-8")
        paths.append(str(path))
    assert smu.main(["--config", paths[0], "--preview"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["experiment_type"] == KIND
    assert plan["will_connect"] is False
    assert smu.main(["--config", paths[0], "--config", paths[1], "--preview"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["experiment_type"] == "dual_" + KIND
    assert plan["will_connect"] is False


def test_cli_execution_dispatch_stays_in_existing_runner(single_rig, tmp_path, monkeypatch):
    import experiments.sweeps as sweeps
    document, _ = single_rig
    document = factor_document(document)
    path = tmp_path / "factor.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    called = []
    def runner(profile, *, sweep_id):
        called.append(profile)
        return 0
    def forbidden(*args, **kwargs):
        pytest.fail("CLI dispatch test attempted instrument construction")
    monkeypatch.setattr(sweeps, "run_configured_sweep", runner)
    monkeypatch.setattr(single, "_new_smu", forbidden)
    assert smu.main(["--config", str(path), "--execute"]) == 0
    assert len(called) == 1 and called[0]["experiment"]["type"] == KIND


def test_cli_offline_analysis_dispatch(single_rig, tmp_path, monkeypatch):
    import analysis.export_experiment as exporter
    document, _ = single_rig
    document = factor_document(document)
    source = Path(document["output"]["root_directory"]) / "offline_factor"
    source.mkdir(parents=True)
    (source / "sweep.json").write_text("{}", encoding="utf-8")
    path = tmp_path / "factor.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    calls = []
    monkeypatch.setattr(exporter, "analyze_sweep", lambda *args: calls.append(args) or 0)
    assert smu.main(["--config", str(path), "--analyze", "offline_factor"]) == 0
    assert len(calls) == 1 and calls[0][0] == str(source)


def test_repository_factor_examples_preview_only(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Example preview attempted instrument construction")
    monkeypatch.setattr(single, "_new_smu", forbidden)
    monkeypatch.setattr(instrument_io, "_new_smu", forbidden)
    root = Path(__file__).resolve().parents[1]
    profiles = []
    for channel in (1, 2):
        document = load_config(root / f"configs/termination_factor_sweep.ch{channel}.example.yaml")["config"]
        plan = build_charge_current_sweep_plan(document)
        assert [
            c["termination_current_factor"] for c in plan["conditions"]
        ] == [
            factor
            for factor in document["charge"]["termination"]["current_factor"]
            for _ in range(document["sweep"]["repeats"])
        ]
        assert {
            c["cv_hold_time_s"] for c in plan["conditions"]
        } == {document["charge"]["termination"]["hold_time_s"]}
        profiles.append(document)
    assert build_dual_plan(profiles)["will_connect"] is False
