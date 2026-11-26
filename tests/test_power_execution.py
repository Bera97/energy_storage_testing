"""Offline tests for public constant-power execution. Never connect a real SMU."""
import json
from pathlib import Path
import pytest
import yaml
import experiments.power_execution as execution
from experiments.config import load_config
import test_charge_current_sweep
from test_constant_power_planning import power_profile

profile = test_charge_current_sweep.profile
@pytest.fixture
def document(profile, tmp_path):
    doc = power_profile(profile)
    doc["rest"]["mode"] = "zero_current_logging"
    doc["rest"]["maximum_abs_current_a"] = 0.001
    doc["output"]["root_directory"] = str(tmp_path / "runs")
    path = tmp_path / "cp.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return load_config(path)["config"]
@pytest.fixture(autouse=True)
def block_connection(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline test attempted hardware connection")
    monkeypatch.setattr(execution, "open_owned_session", forbidden)
def test_probe_ceiling_comes_from_rest_limit(document):
    assert execution.probe_current_ceiling(document) == 0.001
@pytest.mark.parametrize("value", [None, 0, -0.001, True, float("inf"), "0.001"])
def test_bad_probe_ceiling_rejected(document, value):
    document["rest"]["maximum_abs_current_a"] = value
    with pytest.raises(ValueError, match="rest.maximum_abs_current_a"):
        execution.probe_current_ceiling(document)
def test_default_clock_is_high_resolution():
    import time
    assert execution.run_configured_power_sweep.__kwdefaults__["clock"] is time.perf_counter
def test_execution_uses_probe_ceiling_and_reports_unqualified(document, monkeypatch, capsys):
    import experiments.recorded_power_sweep as module
    seen = []
    def run(doc, destination, *, backend, execute_requested):
        assert execute_requested is True
        assert backend.output_authorized is True
        assert backend.probe_ceiling == 0.001
        seen.append(destination)
        return {"state": "completed", "directory": str(destination)}
    monkeypatch.setattr(module, "run_recorded_power_sweep", run)
    assert execution.run_configured_power_sweep(document, "20260101_000000_cp") == 0
    assert len(seen) == 1
    assert json.loads(capsys.readouterr().out)["physical_qualification"] is False
@pytest.mark.parametrize("name", ["", "../x", "a b", "x" * 81, None])
def test_invalid_run_name_never_connects(document, name):
    assert execution.run_configured_power_sweep(document, name) == 1
def test_existing_destination_is_refused(document):
    (Path(document["output"]["root_directory"]) / "dup").mkdir(parents=True)
    assert execution.run_configured_power_sweep(document, "dup") == 1
def test_runner_failure_returns_error_and_reports(document, monkeypatch, capsys):
    import experiments.recorded_power_sweep as module
    def run(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(module, "run_recorded_power_sweep", run)
    assert execution.run_configured_power_sweep(document, "failing_run") == 1
    error = json.loads(capsys.readouterr().err)
    assert error["state"] == "raised" and error["error_message"] == "boom"
