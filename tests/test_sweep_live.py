"""Offline sweep lifecycle tests with explicit fake child execution."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
import yaml
import smu
from experiments.preconditioning import build_plan
from experiments.rate_sweep import build_sweep
import experiments.sweep_live as live
from test_rate_sweep import PROFILE, SETTINGS
@pytest.fixture
def rig(tmp_path, monkeypatch):
    profile = deepcopy(PROFILE)
    profile["execution"] = {
        "expected_serial": "TEST",
        "maximum_voltage_v": 5.55,
        "maximum_current_a": 0.1,
    }
    profile["output"]["root_directory"] = str(tmp_path)
    settings = {
        **deepcopy(SETTINGS),
        "values": [0.2, 0.5],
        "repetitions": 1,
    }
    plan = build_sweep(settings, profile)
    preparation = {
        "charge_current": {"c_rate": 2},
        "discharge_fraction": 0.5,
    }
    plan["preparation"] = preparation
    calls = []
    def runner(config, **options):
        root = Path(config["output"]["root_directory"])
        assert (root / "sweep.json").is_file()
        directory = root / options["experiment_id"]
        directory.mkdir()
        calls.append(options["experiment_id"])
        child_plan = build_plan(config)
        evidence = []
        for phase in child_plan["phases"]:
            if phase["type"] == "rest":
                evidence.append({})
                continue
            recording = directory / f"phase_{phase['phase_index']}"
            recording.mkdir()
            (recording / "metadata.json").write_text(
                json.dumps({
                    "metadata": {
                        "channel": config["instrument"]["channel"],
                    },
                }),
                encoding="utf-8",
            )
            evidence.append({
                "run_directory": str(recording),
            })
        document = {
            "state": "completed",
            "plan": child_plan,
            "metadata": {
                "configuration": config,
                "sweep_context": options["context"],
                "expected_instrument_serial": "TEST",
            },
            "session_outcome": {
                "serial_verified": True,
                "shutdown_attempted": True,
                "shutdown_verified": True,
                "close_attempted": True,
                "close_succeeded": True,
                "identity": "Keysight,B2902B,TEST,FW",
                "shutdown_error": None,
                "close_error": None,
            },
            "backend_evidence": evidence,
        }
        (directory / "experiment.json").write_text(
            json.dumps(document), encoding="utf-8",
        )
        return {
            "state": "completed",
            "shutdown_verified": True,
        }, directory
    def validator(directory, *, phase_root):
        assert directory == phase_root
        doc = json.loads(
            (directory / "experiment.json").read_text(
                encoding="utf-8",
            )
        )
        return {
            "valid": True,
            "state": "completed",
            "phase_recordings_checked": 2 * doc["plan"]["cycles"],
            "errors": [],
            "warnings": [],
        }
    monkeypatch.setattr(live, "execute_live", runner)
    monkeypatch.setattr(live, "validate_experiment", validator)
    return plan, preparation, calls, runner
def test_complete_sweep_and_checker(rig):
    plan, preparation, calls, _ = rig
    before = deepcopy(plan)
    root = live.execute_sweep(
        plan, preparation, "test", execute=True,
    )
    assert plan == before
    assert calls == ["preparation", "point_0001", "point_0002"]
    result = live.check_sweep(root)
    assert result["valid"], result["errors"]
    assert result["jobs_checked"] == 3
    assert result["phase_recordings_checked"] == 6
    assert (root / "SWEEP_COMPLETE").is_file()
    (root / "SWEEP_COMPLETE").unlink()
    assert live.check_sweep(root)["valid"] is False
def test_no_authorization_no_files(rig, tmp_path):
    plan, preparation, calls, _ = rig
    with pytest.raises(ValueError, match="authorization"):
        live.execute_sweep(plan, preparation, "unauthorized")
    assert calls == []
    assert not (tmp_path / "unauthorized").exists()
def test_existing_destination_not_overwritten(rig, tmp_path):
    plan, preparation, calls, _ = rig
    root = tmp_path / "existing"
    root.mkdir()
    sentinel = root / "keep"
    sentinel.write_text("keep")
    with pytest.raises(FileExistsError):
        live.execute_sweep(
            plan, preparation, "existing", execute=True,
        )
    assert calls == []
    assert sentinel.read_text() == "keep"
@pytest.mark.parametrize("exception", [OSError, KeyboardInterrupt])
def test_failure_or_interrupt_stops_sequence(
    rig, tmp_path, monkeypatch, exception
):
    plan, preparation, calls, original = rig
    def fail(config, **options):
        if options["experiment_id"] == "point_0001":
            raise exception("synthetic failure")
        return original(config, **options)
    monkeypatch.setattr(live, "execute_live", fail)
    with pytest.raises(exception, match="synthetic failure"):
        live.execute_sweep(
            plan, preparation, "failed", execute=True,
        )
    root = tmp_path / "failed"
    doc = json.loads((root / "sweep.json").read_text())
    assert calls == ["preparation"]
    assert doc["state"] == "raised"
    assert [
        job["state"] for job in doc["jobs"]
    ] == ["completed", "raised", "planned"]
    assert not (root / "SWEEP_COMPLETE").exists()
def test_invalid_child_prevents_next_job(rig, tmp_path, monkeypatch):
    plan, preparation, calls, _ = rig
    monkeypatch.setattr(
        live, "validate_experiment",
        lambda *a, **k: {
            "valid": False,
            "state": "completed",
            "errors": ["bad CSV"],
            "warnings": [],
        },
    )
    with pytest.raises(ValueError, match="bad CSV"):
        live.execute_sweep(
            plan, preparation, "badchild", execute=True,
        )
    assert calls == ["preparation"]
    assert not (
        tmp_path / "badchild" / "SWEEP_COMPLETE"
    ).exists()
def test_all_points_preflight_before_first_job(rig, tmp_path):
    plan, preparation, calls, _ = rig
    plan["points"][1]["acquisition_plan"]["configuration"][
        "execution"
    ]["maximum_current_a"] = 0.0001
    with pytest.raises(ValueError):
        live.execute_sweep(
            plan, preparation, "badplan", execute=True,
        )
    assert calls == []
    assert not (tmp_path / "badplan").exists()
def test_tampered_context_rejected(rig):
    plan, preparation, _, _ = rig
    root = live.execute_sweep(
        plan, preparation, "tampered", execute=True,
    )
    path = root / "point_0001" / "experiment.json"
    document = json.loads(path.read_text())
    document["metadata"]["sweep_context"]["value"] = 999
    path.write_text(json.dumps(document))
    assert live.check_sweep(root)["valid"] is False
def test_cli_preview_and_missing_preparation(
    rig, tmp_path, monkeypatch, capsys
):
    plan, preparation, calls, _ = rig
    profile = plan["points"][0]["acquisition_plan"]["configuration"]
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(yaml.safe_dump(profile))
    path = tmp_path / "sweep.yaml"
    document = {
        "profile": "profile.yaml",
        "sweep": plan["settings"],
        "preparation": preparation,
    }
    path.write_text(yaml.safe_dump(document))
    monkeypatch.setattr(
        live, "execute_sweep",
        lambda *a, **k: pytest.fail("Preview executed hardware"),
    )
    assert smu.main([
        "sweep-run", "--config", str(path),
    ]) == 0
    assert json.loads(
        capsys.readouterr().out
    )["will_connect"] is False
    assert calls == []
    del document["preparation"]
    path.write_text(yaml.safe_dump(document))
    assert smu.main([
        "sweep-run", "--config", str(path),
    ]) == 1
def test_malformed_manifest_returns_invalid(tmp_path):
    (tmp_path / "sweep.json").write_text("[]")
    assert live.check_sweep(tmp_path)["valid"] is False
def test_failed_cleanup_stops_before_next_job(
    rig, tmp_path, monkeypatch
):
    plan, preparation, calls, original = rig
    def bad_cleanup(config, **options):
        report, directory = original(config, **options)
        path = directory / "experiment.json"
        doc = json.loads(path.read_text())
        doc["session_outcome"]["shutdown_verified"] = False
        path.write_text(json.dumps(doc))
        return report, directory
    monkeypatch.setattr(live, "execute_live", bad_cleanup)
    with pytest.raises(ValueError, match="cleanup"):
        live.execute_sweep(
            plan, preparation, "cleanup_failed", execute=True,
        )
    assert calls == ["preparation"]
    assert not (
        tmp_path / "cleanup_failed" / "SWEEP_COMPLETE"
    ).exists()
