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
    assert "Sweep analysis is not implemented" in capsys.readouterr().err
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
@pytest.fixture(autouse=True)
def forbid_real_sweep_instrument(monkeypatch):
    import experiments.run_live as live
    def forbidden(*args, **kwargs):
        pytest.fail("Offline sweep test reached real instrument construction.")
    monkeypatch.setattr(live, "_new_smu", forbidden)
@pytest.fixture
def sweep_execution(profile, monkeypatch):
    import experiments.run_live as live
    import experiments.sweeps as sweeps
    from experiments.preconditioning_runner import _execution_plan
    control = {"calls": [], "fail_at": None, "error": None}
    def fake_execute(configuration, **options):
        root = Path(configuration["output"]["root_directory"])
        overall = json.loads((root / "sweep.json").read_text(encoding="utf-8"))
        assert overall["state"] == "running"
        assert overall["active_stage_id"] == options["experiment_id"]
        assert overall["stages"][len(control["calls"])]["state"] == "running"
        initial = options.get("initial_discharge_only", False)
        control["calls"].append((deepcopy(configuration), deepcopy(options)))
        directory = root / options["experiment_id"]
        directory.mkdir()
        if len(control["calls"]) == control["fail_at"]:
            raise control["error"]
        plan = _execution_plan(
            configuration, initial_discharge_only=initial
        )
        report = {
            "state": "completed",
            "shutdown_verified": True,
            "completed_phase_count": plan["phase_count"],
            "plan": plan,
        }
        (directory / "experiment.json").write_text(
            json.dumps({"state": "completed"}),
            encoding="utf-8",
        )
        return report, directory
    def fake_check(*args):
        return {"valid": True, "state": "completed", "errors": []}
    monkeypatch.setattr(live, "execute_live", fake_execute)
    monkeypatch.setattr(sweeps, "_check_sweep_stage", fake_check)
    return control
def test_sweep_executes_preparation_then_all_conditions(
    profile, sweep_execution
):
    from experiments.sweeps import execute_charge_current_sweep
    original = deepcopy(profile)
    manifest, directory = execute_charge_current_sweep(
        profile, sweep_id="offline_sweep"
    )
    calls = sweep_execution["calls"]
    assert len(calls) == 5
    assert calls[0][1]["initial_discharge_only"] is True
    assert all(
        "initial_discharge_only" not in options
        for _, options in calls[1:]
    )
    assert [configuration["charge"]["current"]["current_a"]
            for configuration, _ in calls[1:]] == [0.1, 0.1, 0.2, 0.2]
    assert manifest["state"] == "completed"
    assert manifest["completed_stage_count"] == 5
    assert all(stage["state"] == "completed" for stage in manifest["stages"])
    assert sum(stage["completed_phase_count"] for stage in manifest["stages"]) == (
        manifest["plan"]["total_phase_count"]
    )
    persisted = json.loads((directory / "sweep.json").read_text(encoding="utf-8"))
    assert persisted == manifest
    assert profile == original
@pytest.mark.parametrize("fail_at", [1, 2, 3, 5])
@pytest.mark.parametrize("interrupted", [False, True])
def test_sweep_stops_and_preserves_failure(
    profile, sweep_execution, fail_at, interrupted
):
    from experiments.sweeps import execute_charge_current_sweep
    error = (
        KeyboardInterrupt("synthetic interruption")
        if interrupted else TimeoutError("synthetic phase timeout")
    )
    sweep_execution["fail_at"] = fail_at
    sweep_execution["error"] = error
    with pytest.raises(type(error)) as caught:
        execute_charge_current_sweep(profile, sweep_id="failed_sweep")
    assert caught.value is error
    assert len(sweep_execution["calls"]) == fail_at
    manifest = json.loads(
        (error.sweep_directory / "sweep.json").read_text(encoding="utf-8")
    )
    assert manifest["state"] == ("interrupted" if interrupted else "raised")
    assert manifest["completed_stage_count"] == fail_at - 1
    assert manifest["stages"][fail_at - 1]["state"] == "raised"
    assert all(
        stage["state"] == "not_started"
        for stage in manifest["stages"][fail_at:]
    )
def test_recording_validation_failure_prevents_first_charge(
    profile, sweep_execution, monkeypatch
):
    import experiments.sweeps as sweeps
    def reject(*args):
        raise RuntimeError("synthetic recording validation failure")
    monkeypatch.setattr(sweeps, "_check_sweep_stage", reject)
    with pytest.raises(RuntimeError, match="recording validation"):
        sweeps.execute_charge_current_sweep(profile, sweep_id="bad_recording")
    assert len(sweep_execution["calls"]) == 1
def test_existing_sweep_destination_prevents_execution(
    profile, sweep_execution
):
    from experiments.sweeps import execute_charge_current_sweep
    destination = Path(profile["output"]["root_directory"]) / "existing"
    destination.mkdir(parents=True)
    with pytest.raises(FileExistsError):
        execute_charge_current_sweep(profile, sweep_id="existing")
    assert sweep_execution["calls"] == []
@pytest.mark.parametrize("sweep_id", ["../escape", "", "bad/name", "x" * 81])
def test_invalid_sweep_ids_do_not_create_runs(
    profile, sweep_execution, sweep_id
):
    from experiments.sweeps import execute_charge_current_sweep
    with pytest.raises(ValueError, match="sweep_id"):
        execute_charge_current_sweep(profile, sweep_id=sweep_id)
    assert sweep_execution["calls"] == []
    assert not Path(profile["output"]["root_directory"]).exists()
def test_missing_later_serial_prevents_all_execution(
    profile, sweep_execution, monkeypatch
):
    import experiments.sweeps as sweeps
    plan = build_charge_current_sweep_plan(profile)
    plan["conditions"][-1]["execution_settings"]["expected_serial"] = None
    monkeypatch.setattr(
        sweeps, "build_charge_current_sweep_plan", lambda document: deepcopy(plan)
    )
    with pytest.raises(ValueError, match="expected_serial"):
        sweeps.execute_charge_current_sweep(profile, sweep_id="no_serial")
    assert sweep_execution["calls"] == []
    assert not Path(profile["output"]["root_directory"]).exists()
def test_public_execute_dispatches_sweep(
    profile, tmp_path, monkeypatch, capsys
):
    import experiments.sweeps as sweeps
    path = save(profile, tmp_path)
    calls = []
    def fake_run(document, *, sweep_id):
        calls.append((document, sweep_id))
        return 0
    monkeypatch.setattr(smu, "_unified_run_name", lambda document: "cli_sweep")
    monkeypatch.setattr(sweeps, "run_configured_sweep", fake_run)
    assert smu.main(["--config", str(path), "--execute"]) == 0
    assert len(calls) == 1
    assert calls[0][1] == "cli_sweep"
    assert capsys.readouterr().err == ""
@pytest.mark.parametrize("interrupted", [False, True])
def test_sweep_cli_reports_failure_status(
    profile, monkeypatch, capsys, interrupted
):
    import experiments.sweeps as sweeps
    def fail(*args, **kwargs):
        if interrupted:
            raise KeyboardInterrupt("cancel")
        raise OSError("recording failure")
    monkeypatch.setattr(sweeps, "execute_charge_current_sweep", fail)
    assert sweeps.run_configured_sweep(
        profile, sweep_id="cli_failure"
    ) == (130 if interrupted else 1)
    document = json.loads(capsys.readouterr().err)
    assert document["state"] == ("interrupted" if interrupted else "raised")
def test_preparation_validator_requires_explicit_scope(profile, tmp_path):
    from analysis.validate_preconditioning_experiment import validate_experiment
    from experiments.preconditioning_runner import _execution_plan
    from recording.preconditioning_experiment import PreconditioningExperiment
    configuration = build_charge_current_sweep_plan(profile)["conditions"][0]["config"]
    experiment = PreconditioningExperiment(
        tmp_path,
        "initial_only",
        plan=_execution_plan(configuration, initial_discharge_only=True),
        metadata={"configuration": configuration},
    )
    normal = validate_experiment(experiment.directory)
    preparation = validate_experiment(
        experiment.directory, initial_discharge_only=True
    )
    assert normal["valid"] is False
    assert preparation["valid"] is True
    assert preparation["state"] == "running"
def test_real_live_preparation_scope_closes_without_charging():
    import test_live_experiment_entry as live_tests
    from experiments.run_live import execute_live
    fixture = live_tests.LiveExperimentEntryTests(
        methodName="test_success_reserves_before_connect_and_closes_before_finish"
    )
    fixture.setUp()
    try:
        report, directory = execute_live(
            fixture.fixture.document,
            experiment_id="offline_live_test",
            expected_serial="TESTSERIAL",
            maximum_voltage_v=4.2,
            maximum_current_a=2.0,
            initial_discharge_only=True,
        )
        assert report["completed_phase_count"] == 2
        assert [phase["type"] for phase in report["plan"]["phases"]] == [
            "discharge_cc", "rest"
        ]
        assert [
            operation for operation, _ in fixture.backend.calls
            if operation != "shutdown"
        ] == ["discharge", "rest"]
        manifest = json.loads(
            (directory / "experiment.json").read_text(encoding="utf-8")
        )
        assert manifest["state"] == "completed"
        assert manifest["session_outcome"]["close_succeeded"] is True
        assert fixture.events == ["connect", "session_shutdown", "close"]
    finally:
        fixture.doCleanups()
@pytest.mark.parametrize("failure", ["discharge", "rest", "shutdown"])
def test_real_live_preparation_failures_close_the_session(failure):
    import test_live_experiment_entry as live_tests
    from experiments.run_live import execute_live
    fixture = live_tests.LiveExperimentEntryTests(
        methodName="test_success_reserves_before_connect_and_closes_before_finish"
    )
    fixture.setUp()
    try:
        original = TimeoutError(f"synthetic {failure} failure")
        if failure == "discharge":
            fixture.backend.phase_error = original
        elif failure == "rest":
            fixture.backend.rest_error = original
        else:
            fixture.smu.shutdown_error = original
        with pytest.raises(TimeoutError) as caught:
            execute_live(
                fixture.fixture.document,
                experiment_id="offline_live_test",
                expected_serial="TESTSERIAL",
                maximum_voltage_v=4.2,
                maximum_current_a=2.0,
                initial_discharge_only=True,
            )
        assert caught.value is original
        assert fixture.events[-1] == "close"
        assert not any(
            operation == "charge" for operation, _ in fixture.backend.calls
        )
        assert fixture.manifest()["state"] == "raised"
    finally:
        fixture.doCleanups()
@pytest.mark.parametrize(
    "field",
    [
        "serial_verified", "shutdown_attempted", "shutdown_verified",
        "close_attempted", "close_succeeded",
    ],
)
def test_stage_gate_rejects_unverified_session_field(
    profile, tmp_path, monkeypatch, field
):
    import analysis.validate_preconditioning_experiment as validation
    from experiments.preconditioning_runner import _execution_plan
    from experiments.settings import settings_for
    from experiments.sweeps import _check_sweep_stage
    configuration = build_charge_current_sweep_plan(profile)["conditions"][0]["config"]
    serial = settings_for(configuration)["expected_serial"]
    report = {"state": "completed"}
    session = {
        "identity": f"Keysight Technologies,B2902B,{serial},TESTFW",
        "serial_verified": True,
        "shutdown_attempted": True,
        "shutdown_verified": True,
        "close_attempted": True,
        "close_succeeded": True,
        "shutdown_error": None,
        "close_error": None,
    }
    session[field] = False
    manifest = {
        "plan": _execution_plan(configuration, initial_discharge_only=True),
        "metadata": {
            "configuration": configuration,
            "expected_instrument_serial": serial,
        },
        "experiment_report": report,
        "session_outcome": session,
    }
    directory = tmp_path / "stage_gate"
    directory.mkdir()
    (directory / "experiment.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    monkeypatch.setattr(
        validation, "validate_experiment",
        lambda *args, **kwargs: {
            "valid": True, "state": "completed", "errors": []
        },
    )
    with pytest.raises(RuntimeError, match=field):
        _check_sweep_stage(directory, configuration, True, report)
