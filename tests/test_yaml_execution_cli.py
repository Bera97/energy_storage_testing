"""Offline regression tests for YAML-first execution."""
from copy import deepcopy
import json
import re

import pytest
import yaml
from experiments.config import validate_config
from experiments.settings import new_id, require_ceilings, settings_for, validate_execution
import experiments.dual_live as dual
import experiments.run_live as single
import test_dual_phase as phase_tests

pair = phase_tests.pair


def execution_for(document):
    ratings = document["dut"]["ratings"]
    return {
        "expected_serial": "TESTSERIAL",
        "maximum_voltage_v": ratings["maximum_voltage_v"],
        "maximum_current_a": max(
            ratings["maximum_continuous_charge_current_a"],
            ratings["maximum_continuous_discharge_current_a"],
        ),
    }


def save_profiles(pair, tmp_path):
    documents = deepcopy(pair)
    paths = []
    for index, document in enumerate(documents):
        document["execution"] = execution_for(document)
        path = tmp_path / f"profile_{index}.yaml"
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
        paths.append(path)
    return documents, paths


def test_config_accepts_execution_and_preserves_snapshot(pair):
    doc = deepcopy(pair[0])
    doc["execution"] = execution_for(doc)
    original = deepcopy(doc)
    assert validate_config(doc)["config"]["execution"] == doc["execution"]
    assert doc == original


@pytest.mark.parametrize("bad", [True, 0, -1, float("nan"), float("inf"), "0.1"])
def test_execution_rejects_invalid_ceilings(bad):
    section = dict(expected_serial="TEST", maximum_voltage_v=6.0, maximum_current_a=bad)
    with pytest.raises(ValueError):
        validate_execution(section)


def test_execution_rejects_unknown_fields():
    with pytest.raises(ValueError):
        validate_execution(dict(expected_serial="TEST", maximum_voltage_v=6, maximum_current_a=1, execute=True))


def test_execution_rejects_null_section(pair):
    doc = deepcopy(pair[0])
    doc["execution"] = None
    with pytest.raises(ValueError):
        validate_config(doc)


def test_execution_rejects_missing_serial():
    with pytest.raises(ValueError):
        validate_execution(dict(maximum_voltage_v=6, maximum_current_a=1))


def test_cli_cannot_override_yaml():
    config = {"execution": dict(expected_serial="TEST", maximum_voltage_v=6, maximum_current_a=1)}
    with pytest.raises(ValueError, match="conflicts"):
        settings_for(config, expected_serial="OTHER")


def test_generated_ids_are_valid_and_unique():
    values = {new_id("dual") for _ in range(100)}
    assert len(values) == 100
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value) for value in values)


def test_single_operation_resolves_yaml_settings(
    pair, tmp_path, monkeypatch, capsys
):
    documents, _ = save_profiles(pair, tmp_path)
    document = documents[0]
    calls = []
    def fake_execute(config, **kwargs):
        calls.append((config, kwargs))
        return (
            {
                "completed_phase_count": 4,
                "shutdown_verified": True,
            },
            tmp_path / "synthetic_result",
        )
    monkeypatch.setattr(single, "execute_live", fake_execute)
    assert single.run_configured(document, execute=True) == 0
    assert len(calls) == 1
    supplied_document, options = calls[0]
    assert supplied_document == document
    assert options["expected_serial"] == document["execution"]["expected_serial"]
    assert options["maximum_voltage_v"] == document["execution"]["maximum_voltage_v"]
    assert options["maximum_current_a"] == document["execution"]["maximum_current_a"]
    assert options["confirm_specimen"] is None
    assert options["experiment_id"].startswith("experiment_")
    assert json.loads(capsys.readouterr().out)["state"] == "completed"


def test_ceiling_check_rejects_insufficient_limit():
    with pytest.raises(ValueError, match="below"):
        require_ceilings(dict(maximum_voltage_v=4, maximum_current_a=1), dict(required_preflight_ceiling_v=5, required_preflight_ceiling_a=0.1))


def test_single_cli_uses_yaml_without_prompt(pair, tmp_path, monkeypatch, capsys):
    docs, paths = save_profiles(pair, tmp_path)
    calls = []
    def fake(document, **kwargs):
        calls.append((document, kwargs))
        return dict(completed_phase_count=4, shutdown_verified=True), tmp_path / "result"
    monkeypatch.setattr(single, "execute_live", fake)
    monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("Unexpected confirmation prompt"))
    assert single.main(["--config", str(paths[0]), "--execute"]) == 0
    assert len(calls) == 1
    assert calls[0][1]["confirm_specimen"] is None
    assert calls[0][1]["expected_serial"] == "TESTSERIAL"
    assert calls[0][1]["maximum_voltage_v"] == docs[0]["execution"]["maximum_voltage_v"]
    assert json.loads(capsys.readouterr().out)["state"] == "completed"


def test_dual_cli_uses_yaml_without_qualification_or_prompt(pair, tmp_path, monkeypatch, capsys):
    _, paths = save_profiles(pair, tmp_path)
    calls = []
    def fake(plan, directory, serial):
        calls.append((plan, directory, serial))
        return dict(state="completed", final_stop_verified=True, session_close_returned=True)
    monkeypatch.setattr(dual, "execute", fake)
    monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("Unexpected confirmation prompt"))
    args = ["dual-run", "--config", str(paths[0]), "--config", str(paths[1])]
    assert dual.main(args + ["--execute"]) == 0
    assert len(calls) == 1
    assert calls[0][2] == "TESTSERIAL"
    assert calls[0][1].name.startswith("dual_")
    assert json.loads(capsys.readouterr().out)["state"] == "completed"


@pytest.mark.parametrize("command", ["run", "dual-run"])
def test_default_remains_preview(pair, tmp_path, monkeypatch, capsys, command):
    _, paths = save_profiles(pair, tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("Preview invoked execution")
    monkeypatch.setattr(single, "execute_live", forbidden)
    monkeypatch.setattr(dual, "execute", forbidden)
    args = [command, "--config", str(paths[0])]
    if command == "dual-run":
        args += ["--config", str(paths[1])]
    status = (
        single.main(args[1:]) if command == "run"
        else dual.main(args)
    )
    assert status == 0
    assert json.loads(capsys.readouterr().out)["will_connect"] is False


def test_dual_serial_disagreement_prevents_execution(pair, tmp_path, monkeypatch):
    docs, paths = save_profiles(pair, tmp_path)
    docs[1]["execution"]["expected_serial"] = "OTHER"
    paths[1].write_text(yaml.safe_dump(docs[1]), encoding="utf-8")
    monkeypatch.setattr(dual, "execute", lambda *args: pytest.fail("Should not execute"))
    assert dual.main(["dual-run", "--config", str(paths[0]), "--config", str(paths[1]), "--execute"]) == 1


def test_removed_qualification_option_is_rejected():
    with pytest.raises(SystemExit) as caught:
        dual.main(["dual-run", "--config", "unused1", "--config", "unused2", "--qualification-run"])
    assert caught.value.code == 2


def test_single_yaml_path_preserves_real_entry_cleanup(monkeypatch):
    import test_live_experiment_entry as live_tests
    case = live_tests.LiveExperimentEntryTests(
        methodName="test_success_reserves_before_connect_and_closes_before_finish"
    )
    try:
        case.setUp()
        case.fixture.document["execution"] = {
            "expected_serial": "TESTSERIAL",
            "maximum_voltage_v": 4.2,
            "maximum_current_a": 2.0,
        }
        case.fixture.save()
        monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("Unexpected prompt"))
        assert single.main([
            "--config", str(case.fixture.path), "--execute",
            "--experiment-id", "offline_live_test",
        ]) == 0
        assert case.manifest()["state"] == "completed"
        assert case.events == ["connect", "session_shutdown", "close"]
        case.executor_factory.assert_called_once()
    finally:
        case.doCleanups()
