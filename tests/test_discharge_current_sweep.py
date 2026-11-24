"""Offline CC discharge sweep regressions; no real instrument construction."""
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
import experiments.zero_current_rest as zrest
from test_cv_hold_sweep import pair, rig, documents, single_rig
from test_charge_current_sweep import profile, sweep_execution
from experiments.config import validate_config, load_config
from experiments.sweeps import build_charge_current_sweep_plan, execute_charge_current_sweep
from experiments.dual_preconditioning import build_dual_plan
from analysis.export_experiment import validate_sweep, export_sweep
from analysis.dual_experiment import check_batch, export_dual_sweep


@pytest.fixture(autouse=True)
def no_real_instrument(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline discharge-sweep test reached real instrument construction")
    monkeypatch.setattr(single, "_new_smu", forbidden)
    monkeypatch.setattr(instrument_io, "_new_smu", forbidden)


def discharge_document(document, fractions=(0.25, 0.5)):
    result = deepcopy(document)
    result["experiment"]["type"] = "discharge_current_sweep"
    termination = result["charge"]["termination"]
    for key in ("current_factor", "hold_time_s"):
        if isinstance(termination[key], list):
            termination[key] = termination[key][0]
    maximum = result["dut"]["ratings"]["maximum_continuous_discharge_current_a"]
    result["discharge"]["current"] = {
        "basis": "rated_max_discharge_fraction", "factor": 0.75
    }
    result["sweep"] = {
        "discharge_current_a": [maximum * value for value in fractions],
        "repeats": 1, "initial_discharge": True,
    }
    return result


def test_expansion_changes_only_discharge_and_keeps_preparation_fixed(profile):
    document = discharge_document(profile, (0.5, 0.25, 0.75))
    document["sweep"]["repeats"] = 2
    original = deepcopy(document)
    plan = build_charge_current_sweep_plan(document)
    maximum = document["dut"]["ratings"]["maximum_continuous_discharge_current_a"]
    expected = [maximum * f for f in (0.5, 0.5, 0.25, 0.25, 0.75, 0.75)]
    assert plan["parameter"] == "sweep.discharge_current_a"
    assert plan["unit"] == "A"
    assert plan["condition_count"] == 6
    assert [c["discharge_current_a"] for c in plan["conditions"]] == pytest.approx(expected)
    assert plan["initial_discharge"]["current_a"] == pytest.approx(maximum * 0.75)
    assert plan["initial_preparation"]["config"]["discharge"] == document["discharge"]
    for entry, factor in zip(plan["conditions"], (0.5, 0.5, 0.25, 0.25, 0.75, 0.75)):
        config = entry["config"]
        for field in ("charge", "dut", "instrument", "acquisition", "rest", "output"):
            assert config[field] == document[field]
        assert config["discharge"]["current"]["factor"] == pytest.approx(factor)
        assert config["discharge"]["cutoff_voltage_v"] == document["discharge"]["cutoff_voltage_v"]
        assert config["discharge"]["maximum_duration_s"] == document["discharge"]["maximum_duration_s"]
        assert entry["preview"]["acquisition_plan"]["resolved_currents"]["discharge_current_a"] == entry["discharge_current_a"]
        assert config["experiment"]["type"] == "preconditioning"
    assert document == original
    assert json.loads(json.dumps(plan, allow_nan=False)) == plan


@pytest.mark.parametrize("values", [None, [], "0.1", [0], [-0.1], [True], ["0.1"], [float("nan")], [float("inf")], [0.1, 0.1]])
def test_invalid_discharge_values_reject_before_any_execution(profile, sweep_execution, values):
    document = discharge_document(profile)
    document["sweep"]["discharge_current_a"] = values
    with pytest.raises(ValueError):
        execute_charge_current_sweep(document, sweep_id="invalid_discharge")
    assert sweep_execution["calls"] == []
    assert not Path(document["output"]["root_directory"]).exists()


def test_later_overrated_current_rejects_the_whole_sweep(profile, sweep_execution):
    document = discharge_document(profile, (0.25, 1.01))
    with pytest.raises(ValueError, match="condition 2"):
        execute_charge_current_sweep(document, sweep_id="overrated")
    assert sweep_execution["calls"] == []
    assert not Path(document["output"]["root_directory"]).exists()


@pytest.mark.parametrize("value", [0, -1, True, 1.5, 1001])
def test_invalid_repeats(profile, value):
    document = discharge_document(profile)
    document["sweep"]["repeats"] = value
    with pytest.raises(ValueError):
        validate_config(document)


@pytest.mark.parametrize("axis", ["charge_current_a", "hold_time_s", "unexpected"])
def test_multiple_axes_and_unknown_fields_rejected(profile, axis):
    document = discharge_document(profile)
    document["sweep"][axis] = [0.1, 0.2]
    with pytest.raises(ValueError, match="unknown fields"):
        validate_config(document)


def test_initial_preparation_ceiling_is_checked_even_when_conditions_fit(profile):
    document = discharge_document(profile, (0.1, 0.2))
    document["charge"]["current"] = {"current_a": 0.1}
    document["charge"]["termination"]["current_reference"] = "configured_charge_current"
    maximum = document["dut"]["ratings"]["maximum_continuous_discharge_current_a"]
    document["execution"] = {
        "expected_serial": "TESTSERIAL", "maximum_voltage_v": 3.81,
        "maximum_current_a": maximum * 0.5,
    }
    with pytest.raises(ValueError, match="below"):
        build_charge_current_sweep_plan(document)


@pytest.mark.parametrize("change", ["initial", "hold_list", "factor_list", "charge_list", "mode"])
def test_unsupported_combinations(profile, change):
    document = discharge_document(profile)
    if change == "initial":
        document["sweep"]["initial_discharge"] = False
    elif change == "hold_list":
        document["charge"]["termination"]["hold_time_s"] = [1, 2]
    elif change == "factor_list":
        document["charge"]["termination"]["current_factor"] = [0.1, 0.5]
    elif change == "charge_list":
        document["charge"]["current"] = {"current_a": [0.1, 0.2]}
    else:
        document["discharge"]["mode"] = "constant_power"
    with pytest.raises(ValueError):
        build_charge_current_sweep_plan(document)


def test_preparation_and_condition_order_use_distinct_discharge_currents(profile, sweep_execution):
    document = discharge_document(profile)
    document["sweep"]["repeats"] = 2
    manifest, directory = execute_charge_current_sweep(document, sweep_id="discharge_order")
    calls = sweep_execution["calls"]
    assert len(calls) == 5
    assert calls[0][0]["discharge"]["current"]["factor"] == 0.75
    assert calls[0][1]["initial_discharge_only"] is True
    assert [c["discharge"]["current"]["factor"] for c, _ in calls[1:]] == pytest.approx([0.25, 0.25, 0.5, 0.5])
    assert all(c["charge"] == document["charge"] for c, _ in calls)
    assert manifest["state"] == "completed"
    assert "discharge_current_a" not in manifest["stages"][0]
    assert all("discharge_current_a" in stage for stage in manifest["stages"][1:])


@pytest.mark.parametrize("fail_at", [1, 2, 3])
@pytest.mark.parametrize("interrupt", [False, True])
def test_failure_stops_later_discharge_conditions(profile, sweep_execution, fail_at, interrupt):
    document = discharge_document(profile)
    error = KeyboardInterrupt("synthetic stop") if interrupt else TimeoutError("synthetic timeout")
    sweep_execution.update(fail_at=fail_at, error=error)
    with pytest.raises(type(error)) as caught:
        execute_charge_current_sweep(document, sweep_id="stopped_discharge")
    assert caught.value is error
    assert len(sweep_execution["calls"]) == fail_at
    manifest = json.loads((error.sweep_directory / "sweep.json").read_text(encoding="utf-8"))
    assert manifest["completed_stage_count"] == fail_at - 1
    assert all(s["state"] == "not_started" for s in manifest["stages"][fail_at:])


def test_existing_run_is_never_overwritten(profile, sweep_execution):
    document = discharge_document(profile)
    destination = Path(document["output"]["root_directory"]) / "existing"
    destination.mkdir(parents=True)
    with pytest.raises(FileExistsError):
        execute_charge_current_sweep(document, sweep_id="existing")
    assert sweep_execution["calls"] == []


def test_single_discharge_sweep_full_recording_validation_and_export(single_rig, tmp_path):
    document, driver = single_rig
    document = discharge_document(document)
    document["sweep"]["repeats"] = 2
    before = deepcopy(document)
    manifest, directory = execute_charge_current_sweep(document, sweep_id="single_duration")
    assert manifest["completed_stage_count"] == 5
    checked = validate_sweep(directory)
    assert checked["valid"], checked["errors"]
    snapshot = {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    destination = tmp_path / "discharge_export"
    report = export_sweep(directory, destination)
    assert len(report["cycles"]) == 4
    expected = [value for value in document["sweep"]["discharge_current_a"] for _ in range(2)]
    assert [r["discharge_current_a"] for r in report["cycles"]] == pytest.approx(expected)
    assert len({r["discharge_current_a"] for r in report["discharge_current_summary"]}) == 2
    assert all(r["observation_count"] == 2 for r in report["discharge_current_summary"])
    for filename in ("summary.csv", "cycle_comparison.csv", "discharge_current_summary.csv"):
        with (destination / filename).open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert rows and "discharge_current_a" in rows[0]
    assert (destination / "ANALYSIS_COMPLETE").is_file()
    assert snapshot == {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    assert document == before
    assert driver.is_connected is False


@pytest.mark.parametrize("mutation", ["label", "initial_config", "initial_plan"])
def test_recorded_sweep_tampering_rejected(single_rig, mutation):
    document, _ = single_rig
    document = discharge_document(document)
    manifest, directory = execute_charge_current_sweep(document, sweep_id="single_duration")
    if mutation == "label":
        manifest["stages"][1]["discharge_current_a"] += 0.01
    elif mutation == "initial_config":
        manifest["stages"][0]["configuration"]["discharge"]["current"]["factor"] = 0.5
    else:
        manifest["plan"]["initial_preparation"]["config"]["discharge"]["current"]["factor"] = 0.5
    (directory / "sweep.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert validate_sweep(directory)["valid"] is False


def make_dual(documents):
    profiles, driver, options = documents
    profiles = [discharge_document(profiles[0]), discharge_document(profiles[1], (0.5,))]
    profiles[0]["sweep"]["repeats"] = 2
    return profiles, driver, options


def test_dual_preparation_and_measured_rest_config_routing(documents):
    profiles, _, _ = make_dual(documents)
    for profile in profiles:
        maximum = profile["dut"]["ratings"]["maximum_continuous_discharge_current_a"]
        profile["rest"].update(mode="zero_current_logging", maximum_abs_current_a=maximum * 0.001)
    plan = build_dual_plan(profiles)
    assert plan["experiment_type"] == "dual_discharge_current_sweep"
    assert plan["initial_preparation_barrier"] is True
    for preview in plan["channels"]:
        preparation = preview["sweep_plan"]["initial_preparation"]
        assert preview["required_preflight_ceiling_a"] >= preparation["preview"]["required_preflight_ceiling_a"]
        for phase in preview["acquisition_plan"]["phases"]:
            if phase["sweep_stage_id"] == "initial_discharge":
                assert zrest.config_for_phase(plan, preview["channel"], phase) == preparation["config"]
                if phase["type"] == "discharge_cc":
                    assert phase["current_magnitude_a"] == preview["sweep_plan"]["initial_discharge"]["current_a"]
            else:
                config = zrest.config_for_phase(plan, preview["channel"], phase)
                if phase["type"] == "discharge_cc":
                    resolved = validate_config(config)["resolved_currents"]["discharge_current_a"]
                    assert phase["current_magnitude_a"] == resolved
                    assert phase["sweep_discharge_current_a"] == resolved


def test_dual_discharge_sweep_recording_and_factor_separation(documents, tmp_path):
    profiles, driver, options = make_dual(documents)
    plan = build_dual_plan(profiles)
    directory = tmp_path / "dual_discharge"
    report = dual.execute(plan, directory, "TESTSERIAL", **options)
    assert report["state"] == "completed"
    assert driver.closed and not driver.chan1.on and not driver.chan2.on
    checked = check_batch(directory)
    assert checked["valid"], checked["errors"]
    destination = tmp_path / "dual_analysis"
    result = export_dual_sweep(directory, destination)
    assert len(result["cycles"]) == 5
    for channel, profile in enumerate(profiles, 1):
        actual = {r["discharge_current_a"] for r in result["cycles"] if r["channel"] == channel}
        assert sorted(actual) == pytest.approx(sorted(profile["sweep"]["discharge_current_a"]))
    assert all(r["observation_count"] == (2 if r["channel"] == 1 else 1) for r in result["discharge_current_summary"])
    assert (destination / "discharge_current_summary.csv").is_file()
    assert (destination / "ANALYSIS_COMPLETE").is_file()


def test_dual_bus_failure_stops_both_channels(documents, tmp_path, monkeypatch):
    profiles, driver, options = make_dual(documents)
    original = driver._query
    def fail(command):
        if command.startswith(":TRAC2:DATA?"):
            raise OSError("synthetic discharge-sweep bus fault")
        return original(command)
    monkeypatch.setattr(driver, "_query", fail)
    directory = tmp_path / "failed_dual"
    with pytest.raises(OSError, match="bus fault"):
        dual.execute(build_dual_plan(profiles), directory, "TESTSERIAL", **options)
    assert driver.closed and not driver.chan1.on and not driver.chan2.on
    assert not (directory / "DUAL_COMPLETE").exists()


def test_single_and_dual_cli_preview_is_read_only(documents, tmp_path, capsys):
    profiles, _, _ = make_dual(documents)
    paths = []
    for channel, profile in enumerate(profiles, 1):
        path = tmp_path / f"discharge_{channel}.yaml"
        path.write_text(yaml.safe_dump(profile), encoding="utf-8")
        paths.append(str(path))
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert smu.main(["--config", paths[0], "--preview"]) == 0
    single_plan = json.loads(capsys.readouterr().out)
    assert single_plan["experiment_type"] == "discharge_current_sweep"
    assert not single_plan["will_connect"] and not single_plan["will_enable_output"]
    assert smu.main(["--config", paths[0], "--config", paths[1], "--preview"]) == 0
    dual_plan = json.loads(capsys.readouterr().out)
    assert dual_plan["experiment_type"] == "dual_discharge_current_sweep"
    assert not dual_plan["will_connect"]
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_cli_execution_routes_to_existing_sweep_runner(profile, tmp_path, monkeypatch):
    import experiments.sweeps as sweeps
    document = discharge_document(profile)
    path = tmp_path / "discharge.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    calls = []
    monkeypatch.setattr(sweeps, "run_configured_sweep", lambda document, **kwargs: calls.append(document) or 0)
    assert smu.main(["--config", str(path), "--execute"]) == 0
    assert len(calls) == 1 and calls[0]["experiment"]["type"] == "discharge_current_sweep"


def test_repository_examples_follow_declared_lists_and_preserve_preparation():
    root = Path(__file__).resolve().parents[1]
    profiles = []
    for channel in (1, 2):
        profile = load_config(root / f"configs/discharge_current_sweep.ch{channel}.example.yaml")["config"]
        plan = build_charge_current_sweep_plan(profile)
        expected = [v for v in profile["sweep"]["discharge_current_a"] for _ in range(profile["sweep"]["repeats"])]
        assert [c["discharge_current_a"] for c in plan["conditions"]] == pytest.approx(expected)
        assert all(c["config"]["charge"] == profile["charge"] for c in plan["conditions"])
        assert plan["initial_preparation"]["config"]["discharge"] == profile["discharge"]
        profiles.append(profile)
    assert build_dual_plan(profiles)["will_connect"] is False
