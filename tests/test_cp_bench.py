"""Offline wrapper tests. Never connect a real SMU."""
import json
import yaml
import pytest
import cp_bench
from test_charge_current_sweep import profile
from test_constant_power_planning import power_profile

@pytest.fixture
def config(profile, tmp_path):
    doc = power_profile(profile)
    path = tmp_path / "bench.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return doc, path

@pytest.fixture(autouse=True)
def block_connection(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline wrapper test attempted hardware connection")
    monkeypatch.setattr(cp_bench, "open_owned_session", forbidden)


def test_preview_never_connects(config, capsys):
    _, path = config
    assert cp_bench.main(["--config", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["will_connect"] is False
    assert result["physical_qualification"] is False

@pytest.mark.parametrize("missing", ["specimen", "probe", "run"])
def test_missing_authorization_never_connects(config, missing):
    doc, path = config
    args = ["--config", str(path), "--execute"]
    if missing != "specimen":
        args += ["--confirm-specimen", doc["dut"]["specimen_id"]]
    if missing != "probe":
        args += ["--maximum-probe-current-a", "0.001"]
    if missing != "run":
        args += ["--run-name", "offline"]
    assert cp_bench.main(args) == 1


def test_execution_dispatch_is_explicit_and_offline(config, monkeypatch, capsys):
    import experiments.recorded_power_sweep as module
    doc, path = config
    seen = []
    def run(document, destination, *, backend, execute_requested):
        assert backend.output_authorized is True
        assert execute_requested is True
        assert backend.probe_ceiling == 0.001
        seen.append(destination)
        return {"state": "completed", "directory": str(destination)}
    monkeypatch.setattr(module, "run_recorded_power_sweep", run)
    assert cp_bench.main(["--config", str(path), "--execute",
        "--confirm-specimen", doc["dut"]["specimen_id"],
        "--maximum-probe-current-a", "0.001", "--run-name", "offline"]) == 0
    assert len(seen) == 1
    assert json.loads(capsys.readouterr().out)["physical_qualification"] is False


def test_failed_connection_attempt_closes_driver(monkeypatch):
    import experiments.run_live as live
    calls = []
    primary = OSError("connect failed")
    class Driver:
        is_connected = True
        def connect(self): raise primary
        def shutdown(self): calls.append("shutdown")
        def close(self): calls.append("close")
    monkeypatch.setattr(live, "_new_smu", lambda *args: Driver())
    original = open("cp_bench.py", encoding="utf-8").read()
    namespace = {"__name__": "wrapper_under_test"}
    exec(compile(original, "cp_bench.py", "exec"), namespace)
    with pytest.raises(OSError) as caught:
        namespace["open_owned_session"]({}, {"instrument": {"visa_address": "FAKE"}},
            timeout_ms=500, visa_library=None)
    assert caught.value is primary
    assert calls == ["shutdown", "close"]
