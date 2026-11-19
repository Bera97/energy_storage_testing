"""Timed-CV policy and offline concrete single/dual integration tests."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
from experiments.charge_controller import (
    ChargeController,
    ChargeCVHoldError,
    ChargeFaultError,
    validate_charge_policy_evidence,
)
import test_dual_live as dual_tests
pair = dual_tests.pair
rig = dual_tests.rig
def phase(mode="from_cv_entry", duration=3.0):
    return {
        "type": "charge_cc_cv",
        "current_magnitude_a": 1.0,
        "termination_current_a": 0.1,
        "termination_hold_time_s": 10.0,
        "termination_requires_cv": True,
        "maximum_duration_s": 100.0,
        "maximum_cv_duration_s": 40.0,
        "cv_hold": {"mode": mode, "duration_s": duration},
    }
def observe(controller, elapsed, current=0.5, cv=True, **extra):
    return controller.update(
        elapsed_s=elapsed, current_a=current, cv_active=cv, **extra
    )
def test_total_cv_hold_does_not_wait_for_current_threshold():
    planned = phase()
    original = deepcopy(planned)
    controller = ChargeController(planned)
    assert observe(controller, 0, 0.01, False).stop_reason is None
    assert observe(controller, 1, 0.9).stop_reason is None
    assert observe(controller, 3, 0.8).stop_reason is None
    assert observe(controller, 4, 0.7).stop_reason == "cv_hold_elapsed"
    evidence = controller.cv_hold_evidence
    assert evidence["timer_started_elapsed_s"] == 1
    assert evidence["first_below_threshold_elapsed_s"] is None
    validate_charge_policy_evidence(planned, evidence)
    assert planned == original
def test_additional_hold_starts_at_first_strict_crossing_and_latches():
    planned = phase("after_current_threshold")
    controller = ChargeController(planned)
    observe(controller, 0, 0.01, False)
    observe(controller, 1, 0.2)
    observe(controller, 2, 0.1)
    assert controller.cv_hold_evidence["timer_started_elapsed_s"] is None
    assert observe(controller, 3, 0.099).stop_reason is None
    assert controller.cv_hold_evidence["timer_started_elapsed_s"] == 3
    assert observe(controller, 4, 0.9).stop_reason is None
    assert observe(controller, 6, 0.9).stop_reason == "cv_hold_elapsed"
    validate_charge_policy_evidence(planned, controller.cv_hold_evidence)
@pytest.mark.parametrize("mode", ["from_cv_entry", "after_current_threshold"])
def test_loss_of_cv_after_timer_start_fails(mode):
    controller = ChargeController(phase(mode))
    observe(controller, 0, 0.05)
    with pytest.raises(ChargeCVHoldError):
        observe(controller, 1, 0.05, False)
    assert controller.state == "failed"
@pytest.mark.parametrize("mode", ["from_cv_entry", "after_current_threshold"])
def test_fault_takes_priority_over_hold_completion(mode):
    controller = ChargeController(phase(mode))
    observe(controller, 0, 0.05)
    with pytest.raises(ChargeFaultError):
        observe(controller, 3, 0.05, has_fault=True)
def test_threshold_never_reached_ends_in_timeout_not_success():
    controller = ChargeController(phase("after_current_threshold"))
    observe(controller, 0, 0.5)
    with pytest.raises(TimeoutError):
        observe(controller, 40, 0.5)
@pytest.mark.parametrize("mode", ["from_cv_entry", "after_current_threshold"])
def test_total_timeout_takes_priority_at_same_timestamp(mode):
    controller = ChargeController(phase(mode))
    observe(controller, 0, 1.0, False)
    observe(controller, 97, 0.05)
    with pytest.raises(TimeoutError):
        observe(controller, 100, 0.05)
@pytest.mark.parametrize(
    "policy",
    [
        None, {}, {"mode": "bad", "duration_s": 3},
        {"mode": "from_cv_entry", "duration_s": 0},
        {"mode": "from_cv_entry", "duration_s": True},
        {"mode": "from_cv_entry", "duration_s": float("nan")},
        {"mode": "from_cv_entry", "duration_s": 40},
        {"mode": "from_cv_entry", "duration_s": 3, "unexpected": 1},
    ],
)
def test_invalid_cv_policy_rejected(policy):
    planned = phase()
    planned["cv_hold"] = policy
    with pytest.raises(ValueError):
        ChargeController(planned)
def test_inconsistent_completion_evidence_rejected():
    planned = phase()
    controller = ChargeController(planned)
    observe(controller, 0, 0.5)
    observe(controller, 3, 0.5)
    evidence = controller.cv_hold_evidence
    evidence["timed_hold_elapsed_s"] = 0
    with pytest.raises(ValueError):
        validate_charge_policy_evidence(planned, evidence)
@pytest.mark.parametrize("mode", ["from_cv_entry", "after_current_threshold"])
def test_concrete_single_recording_and_validation(mode, tmp_path):
    import test_recorded_phase_executor as executor_tests
    from experiments.acquisition_budget import build_acquisition_plan
    from experiments.recorded_phase_executor import RecordedPhaseExecutor
    from experiments.recorded_preconditioning_backend import RecordedPreconditioningBackend
    from recording.preconditioning_experiment import run_recorded_preconditioning
    from analysis.validate_preconditioning_experiment import validate_experiment
    from analysis.export_experiment import export_experiment
    fixture = executor_tests.RecordedPhaseExecutorTests(
        methodName="test_charge_returns_finalized_recording"
    )
    fixture.setUp()
    try:
        fixture.document["charge"]["termination"]["current_reference"] = "configured_charge_current"
        fixture.document["charge"]["cv_hold"] = {"mode": mode, "duration_s": 2.0}
        fixture.acquisition_plan = build_acquisition_plan(fixture.document, maximum_records=100000)
        base = tmp_path / "recordings"
        executor = RecordedPhaseExecutor(
            fixture.smu,
            fixture.document,
            base / "single_hold" / "phases",
            "cv_test",
            maximum_voltage_v=4.2,
            maximum_current_a=4.0,
            clock=fixture.clock,
            sleep=fixture.clock.sleep,
        )
        backend = RecordedPreconditioningBackend(
            fixture.smu,
            execute_active_phase=executor,
            rest_poll_interval_s=1000.0,
            clock=fixture.clock,
            sleep=fixture.clock.sleep,
        )
        report, directory = run_recorded_preconditioning(
            fixture.document, backend, base, "single_hold"
        )
        assert report["state"] == "completed"
        validation = validate_experiment(directory, phase_root=directory)
        assert validation["valid"], validation["errors"]
        for entry in backend.phase_outcomes:
            if entry["type"] != "charge_cc_cv":
                continue
            metadata = json.loads(
                (Path(entry["run_directory"]) / "metadata.json").read_text(encoding="utf-8")
            )
            transport = metadata["phase_transport_report"]
            assert transport["stop_reason"] == "cv_hold_elapsed"
            assert transport["charge_policy_evidence"]["mode"] == mode
        result = export_experiment(
            directory, tmp_path / "single_analysis", phase_root=directory
        )
        assert result["analysis_state"] == "completed"
    finally:
        fixture.doCleanups()
@pytest.mark.parametrize(
    "modes",
    [
        ("from_cv_entry", "after_current_threshold"),
        ("after_current_threshold", "from_cv_entry"),
    ],
)
def test_concrete_dual_policies_record_validate_and_analyze(rig, tmp_path, modes):
    from experiments.dual_preconditioning import build_dual_plan
    from experiments.dual_live import execute
    from analysis.dual_experiment import check_batch, analyze_batch
    old_plan, driver, options = rig
    documents = [
        deepcopy(preview["acquisition_plan"]["configuration"])
        for preview in old_plan["channels"]
    ]
    for document, mode in zip(documents, modes):
        document["charge"]["termination"]["current_reference"] = "configured_charge_current"
        document["charge"]["termination"]["current_factor"] = 0.5
        document["charge"]["cv_hold"] = {"mode": mode, "duration_s": 0.4}
    directory = tmp_path / "dual_hold"
    report = execute(build_dual_plan(documents), directory, "TESTSERIAL", **options)
    assert report["state"] == "completed"
    assert driver.closed is True
    assert driver.chan1.on is False and driver.chan2.on is False
    for channel in report["channels"]:
        charge = next(
            outcome for outcome in channel["outcomes"]
            if outcome["type"] == "charge_cc_cv"
        )
        assert charge["stop_reason"] == "cv_hold_elapsed"
        metadata = json.loads(
            (Path(charge["run_directory"]) / "metadata.json").read_text(encoding="utf-8")
        )
        evidence = metadata["phase_transport_report"]["charge_policy_evidence"]
        assert evidence["mode"] == modes[channel["channel"] - 1]
        assert evidence["timed_hold_elapsed_s"] >= 0.4
    checked = check_batch(directory)
    assert checked["valid"], checked["errors"]
    result = analyze_batch(directory, tmp_path / "dual_analysis")
    assert result["analysis_state"] == "completed"
@pytest.mark.parametrize(
    "mode", ["current_threshold", "from_cv_entry", "after_current_threshold"]
)
def test_unified_termination_modes_plan_from_one_location(rig, mode):
    from experiments.config import validate_config
    from experiments.preconditioning import build_plan
    old_plan, _, _ = rig
    document = deepcopy(old_plan["channels"][0]["acquisition_plan"]["configuration"])
    document["charge"].pop("cv_hold", None)
    document["charge"]["termination"] = {
        "mode": mode,
        "current_reference": "configured_charge_current",
        "current_factor": 0.5,
        "hold_time_s": 0.5,
    }
    validated = validate_config(document)["config"]
    assert validated == document
    planned = build_plan(document)["phases"][0]
    if mode == "current_threshold":
        assert "cv_hold" not in planned
        assert planned["termination_hold_time_s"] == 0.5
    else:
        assert planned["cv_hold"] == {"mode": mode, "duration_s": 0.5}
def test_unified_and_separate_cv_settings_cannot_be_combined(rig):
    from experiments.config import validate_config
    old_plan, _, _ = rig
    document = deepcopy(old_plan["channels"][0]["acquisition_plan"]["configuration"])
    document["charge"]["termination"]["mode"] = "from_cv_entry"
    document["charge"]["cv_hold"] = {
        "mode": "from_cv_entry", "duration_s": 0.5
    }
    with pytest.raises(ValueError, match="one place"):
        validate_config(document)
def test_all_tracked_experiment_yaml_profiles_use_unified_termination():
    import subprocess
    import yaml
    from experiments.config import _UniqueSafeLoader
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["git", "ls-files", "--", "configs"],
        cwd=root, capture_output=True, text=True, check=True,
    )
    checked = []
    for name in result.stdout.splitlines():
        if Path(name).suffix.lower() not in (".yaml", ".yml"):
            continue
        document = yaml.load(
            (root / name).read_text(encoding="utf-8"),
            Loader=_UniqueSafeLoader,
        )
        if not (
            isinstance(document, dict)
            and isinstance(document.get("charge"), dict)
            and isinstance(document["charge"].get("termination"), dict)
            and isinstance(document.get("experiment"), dict)
        ):
            continue
        charge = document["charge"]
        assert "cv_hold" not in charge, name
        assert set(charge["termination"]) == {
            "mode", "current_reference", "current_factor", "hold_time_s"
        }, name
        assert charge["termination"]["mode"] in (
            "current_threshold", "from_cv_entry", "after_current_threshold"
        ), name
        checked.append(name)
    assert checked
@pytest.mark.parametrize("mode", ["from_cv_entry", "after_current_threshold"])
def test_canonical_and_legacy_timed_policies_resolve_equivalently(rig, mode):
    from experiments.charge_controller import resolve_charge_cv_hold
    old_plan, _, _ = rig
    old_charge = deepcopy(
        old_plan["channels"][0]["acquisition_plan"]["configuration"]["charge"]
    )
    old_charge["cv_hold"] = {"mode": mode, "duration_s": 0.5}
    old_charge["termination"].pop("mode", None)
    new_charge = deepcopy(old_charge)
    del new_charge["cv_hold"]
    new_charge["termination"]["mode"] = mode
    new_charge["termination"]["hold_time_s"] = 0.5
    assert resolve_charge_cv_hold(old_charge) == resolve_charge_cv_hold(new_charge)
