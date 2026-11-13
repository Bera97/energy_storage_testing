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
@pytest.fixture
def completed_recorded_sweep(tmp_path, monkeypatch):
    """Real live runner/recorder/transports; fake instrument and virtual clock."""
    import test_recorded_phase_executor as executor_tests
    import experiments.run_live as live
    from experiments.acquisition_budget import build_acquisition_plan
    from experiments.sweeps import execute_charge_current_sweep
    fixture = executor_tests.RecordedPhaseExecutorTests(
        methodName="test_charge_returns_finalized_recording"
    )
    fixture.setUp()
    try:
        document = deepcopy(fixture.document)
        document["experiment"]["type"] = "charge_current_sweep"
        document["experiment"]["cycles"] = 1
        document["charge"]["termination"]["current_reference"] = "configured_charge_current"
        document["charge"]["termination"]["hold_time_s"] = 2.0
        document["charge"]["maximum_duration_s"] = 10
        document["charge"]["maximum_cv_duration_s"] = 5
        document["discharge"]["maximum_duration_s"] = 10
        document["rest"]["after_charge_s"] = 0.1
        document["rest"]["after_discharge_s"] = 0.1
        document["output"] = {
            "root_directory": str(tmp_path / "recordings"),
            "file_name": "offline_sweep",
            "analysis_directory": str(tmp_path / "analysis"),
        }
        document["execution"] = {
            "expected_serial": "TESTSERIAL",
            "maximum_voltage_v": 4.2,
            "maximum_current_a": 4.0,
        }
        document["sweep"] = {
            "charge_current_a": [0.1, 0.2],
            "repeats": 1,
            "initial_discharge": True,
        }
        root = Path(document["output"]["root_directory"]) / "recorded_sweep"
        driver = fixture.smu
        driver.identity = "Keysight Technologies,B2902B,TESTSERIAL,TESTFW"
        driver.is_connected = False
        connections = []
        def connect():
            overall = json.loads((root / "sweep.json").read_text(encoding="utf-8"))
            stage_id = overall["active_stage_id"]
            assert (root / stage_id / "experiment.json").is_file()
            connections.append(stage_id)
            driver.is_connected = True
            return driver
        def close():
            driver.is_connected = False
        driver.connect = connect
        driver.close = close
        executor_class = live.RecordedPhaseExecutor
        backend_class = live.RecordedPreconditioningBackend
        def executor(smu, configuration, *args, **options):
            fixture.document = deepcopy(configuration)
            fixture.acquisition_plan = build_acquisition_plan(
                configuration, maximum_records=100000
            )
            return executor_class(
                smu, configuration, *args,
                clock=fixture.clock,
                sleep=fixture.clock.sleep,
                **options,
            )
        def backend(smu, **options):
            return backend_class(
                smu, clock=fixture.clock, sleep=fixture.clock.sleep, **options
            )
        monkeypatch.setattr(live, "_new_smu", lambda *args: driver)
        monkeypatch.setattr(live, "RecordedPhaseExecutor", executor)
        monkeypatch.setattr(live, "RecordedPreconditioningBackend", backend)
        manifest, directory = execute_charge_current_sweep(
            document, sweep_id="recorded_sweep"
        )
        assert manifest["state"] == "completed"
        assert len(connections) == 3
        assert driver.is_connected is False
        yield document, directory, manifest
    finally:
        fixture.doCleanups()
def test_full_recorded_sweep_validates(completed_recorded_sweep):
    from analysis.export_experiment import validate_sweep
    _, directory, _ = completed_recorded_sweep
    validation = validate_sweep(directory)
    assert validation["valid"], validation["errors"]
    assert validation["stages_checked"] == 3
    assert validation["phase_recordings_checked"] == 5
def test_full_sweep_export_preserves_sources(completed_recorded_sweep, tmp_path):
    import csv
    from analysis.export_experiment import export_sweep
    _, directory, _ = completed_recorded_sweep
    before = {
        path.relative_to(directory): path.read_bytes()
        for path in directory.rglob("*") if path.is_file()
    }
    destination = tmp_path / "combined_analysis"
    report = export_sweep(directory, destination)
    assert report["analysis_state"] == "completed"
    assert len(report["phases"]) == 5
    assert len(report["cycles"]) == 2
    assert all(row["condition_id"] != "initial_discharge" for row in report["cycles"])
    assert (destination / "ANALYSIS_COMPLETE").is_file()
    assert (
        destination / "initial_discharge" / "ANALYSIS_COMPLETE"
    ).is_file()
    for name, count in [
        ("summary.csv", 5), ("cycle_comparison.csv", 2), ("current_summary.csv", 14)
    ]:
        with (destination / name).open(encoding="utf-8", newline="") as handle:
            assert len(list(csv.DictReader(handle))) == count
    assert all(row["sample_stddev"] is None for row in report["current_summary"])
    after = {
        path.relative_to(directory): path.read_bytes()
        for path in directory.rglob("*") if path.is_file()
    }
    assert after == before
@pytest.mark.parametrize(
    "mutation",
    [
        "state", "count", "order", "path", "configuration",
        "stage_state", "phase_count", "session", "missing_recording",
    ],
)
def test_sweep_validation_rejects_inconsistent_evidence(
    completed_recorded_sweep, mutation
):
    from analysis.export_experiment import validate_sweep
    _, directory, manifest = completed_recorded_sweep
    stage = manifest["stages"][1]
    if mutation == "state":
        manifest["state"] = "running"
    elif mutation == "count":
        manifest["completed_stage_count"] -= 1
    elif mutation == "order":
        manifest["stages"][1], manifest["stages"][2] = (
            manifest["stages"][2], manifest["stages"][1]
        )
    elif mutation == "path":
        stage["experiment_directory"] = str(directory.parent)
    elif mutation == "configuration":
        stage["configuration"]["rest"]["after_charge_s"] = 999
    elif mutation == "stage_state":
        stage["state"] = "pending"
    elif mutation == "phase_count":
        stage["completed_phase_count"] = 999
    else:
        stage_directory = Path(stage["experiment_directory"])
        stage_manifest = json.loads(
            (stage_directory / "experiment.json").read_text(encoding="utf-8")
        )
        if mutation == "session":
            stage_manifest["session_outcome"]["close_succeeded"] = False
            (stage_directory / "experiment.json").write_text(
                json.dumps(stage_manifest), encoding="utf-8"
            )
        else:
            evidence = next(
                entry for entry in stage_manifest["backend_evidence"]
                if "run_directory" in entry
            )
            (Path(evidence["run_directory"]) / "metadata.json").unlink()
    (directory / "sweep.json").write_text(json.dumps(manifest), encoding="utf-8")
    validation = validate_sweep(directory)
    assert validation["valid"] is False
    assert validation["errors"]
def test_invalid_sweep_creates_no_analysis(completed_recorded_sweep, tmp_path):
    from analysis.export_experiment import export_sweep
    _, directory, manifest = completed_recorded_sweep
    manifest["state"] = "raised"
    (directory / "sweep.json").write_text(json.dumps(manifest), encoding="utf-8")
    destination = tmp_path / "must_not_exist"
    with pytest.raises(ValueError, match="validation failed"):
        export_sweep(directory, destination)
    assert not destination.exists()
def test_sweep_export_refuses_existing_or_nested_destination(
    completed_recorded_sweep, tmp_path
):
    from analysis.export_experiment import export_sweep
    _, directory, _ = completed_recorded_sweep
    destination = tmp_path / "existing_analysis"
    destination.mkdir()
    with pytest.raises(FileExistsError):
        export_sweep(directory, destination)
    with pytest.raises(ValueError, match="outside"):
        export_sweep(directory, directory / "analysis")
def test_failed_subexport_leaves_no_top_completion(
    completed_recorded_sweep, tmp_path, monkeypatch
):
    import analysis.export_experiment as analysis
    _, directory, _ = completed_recorded_sweep
    def fail(*args, **kwargs):
        raise OSError("synthetic export failure")
    monkeypatch.setattr(analysis, "export_experiment", fail)
    destination = tmp_path / "partial_analysis"
    with pytest.raises(OSError, match="synthetic"):
        analysis.export_sweep(directory, destination)
    assert not (destination / "ANALYSIS_COMPLETE").exists()
def test_group_statistics_keep_observation_counts_and_sample_stddev():
    from analysis.export_experiment import _sweep_group_rows, SWEEP_GROUP_METRICS
    rows = []
    for cycle, value in enumerate([1.0, 3.0], 1):
        rows.append({
            "condition_id": "same_condition",
            "charge_current_a": 0.1,
            **{metric: value for metric in SWEEP_GROUP_METRICS},
            "cycle": cycle,
        })
    grouped = _sweep_group_rows(rows)
    assert all(row["condition_count"] == 1 for row in grouped)
    assert all(row["observation_count"] == 2 for row in grouped)
    assert all(row["mean"] == 2.0 for row in grouped)
    assert all(row["sample_stddev"] == pytest.approx(2 ** 0.5) for row in grouped)
def test_public_sweep_analysis_is_offline(
    completed_recorded_sweep, tmp_path, monkeypatch, capsys
):
    import experiments.run_live as live
    document, directory, _ = completed_recorded_sweep
    path = save(document, tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("Offline analysis attempted instrument construction.")
    monkeypatch.setattr(live, "_new_smu", forbidden)
    assert smu.main([
        "--config", str(path), "--analyze", directory.name,
    ]) == 0
    assert "Sweep analysis:" in capsys.readouterr().out
    destination = Path(document["output"]["analysis_directory"]) / directory.name
    assert (destination / "ANALYSIS_COMPLETE").is_file()
    assert smu.main([
        "--config", str(path), "--analyze", directory.name,
    ]) == 1
def test_public_execute_then_analyze_dispatches_in_order(
    profile, tmp_path, monkeypatch
):
    import experiments.sweeps as sweeps
    events = []
    path = save(profile, tmp_path)
    monkeypatch.setattr(smu, "_unified_run_name", lambda document: "combined_run")
    def execute(document, *, sweep_id):
        events.append(("execute", sweep_id))
        return 0
    def analyze(document, run_name):
        events.append(("analyze", run_name))
        return 0
    monkeypatch.setattr(sweeps, "run_configured_sweep", execute)
    monkeypatch.setattr(smu, "_unified_analyze", analyze)
    assert smu.main(["--config", str(path), "--execute", "--analyze"]) == 0
    assert events == [("execute", "combined_run"), ("analyze", "combined_run")]
def test_failed_execution_skips_sweep_analysis(
    profile, tmp_path, monkeypatch, capsys
):
    import experiments.sweeps as sweeps
    path = save(profile, tmp_path)
    monkeypatch.setattr(smu, "_unified_run_name", lambda document: "failed_run")
    monkeypatch.setattr(sweeps, "run_configured_sweep", lambda *args, **kwargs: 1)
    def forbidden(*args):
        pytest.fail("Failed sweep reached analysis.")
    monkeypatch.setattr(smu, "_unified_analyze", forbidden)
    assert smu.main(["--config", str(path), "--execute", "--analyze"]) == 1
    assert "analysis skipped" in capsys.readouterr().err
def test_combined_mode_requires_analysis_directory_before_execution(
    profile, tmp_path, monkeypatch, capsys
):
    import experiments.sweeps as sweeps
    del profile["output"]["analysis_directory"]
    path = save(profile, tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("Missing analysis settings were detected after execution.")
    monkeypatch.setattr(sweeps, "run_configured_sweep", forbidden)
    assert smu.main(["--config", str(path), "--execute", "--analyze"]) == 1
    assert "analysis_directory" in capsys.readouterr().err
