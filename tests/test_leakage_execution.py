"""Leakage execution wiring: ownership, identity, directory policy, exit codes, CLI."""
import json
from pathlib import Path
import re
import types
import pytest

import smu
import test_charge_current_sweep
from analysis import leakage_analysis
from experiments import leakage_execution
from experiments.config import load_config
from experiments.leakage_planning import build_leakage_plan
from experiments.leakage_runner import LeakageRunError
from experiments.settings import settings_for
from test_leakage_planning import leakage_profile, save

profile = test_charge_current_sweep.profile
NAME = "20261126_200000_leak"


class FakeSession:
    def __init__(self, serial):
        self.identity = f"Keysight,B2902B,{serial},FW1"
        self.is_connected = True
        self.closed = False
        self.shutdowns = 0

    def shutdown(self):
        self.shutdowns += 1

    def close(self):
        self.closed = True


@pytest.fixture
def wiring(monkeypatch):
    state = types.SimpleNamespace(session=None, opened=0, runs=[], raises=None, serial=None)

    def open_session(request, document, *, timeout_ms, visa_library):
        state.opened += 1
        state.session = FakeSession(state.serial or settings_for(document)["expected_serial"])
        return state.session

    def runner(document, **kwargs):
        state.runs.append(kwargs)
        if state.raises is not None:
            raise state.raises
        return {"state": "completed", "recorded_sample_count": 5, "planned_sample_count": 5,
                "elapsed_s": 1.0, "read_retry_count": 0, "shutdown_verified": True,
                "early_stop": {"triggered": False}}

    monkeypatch.setattr(leakage_execution, "open_owned_session", open_session)
    monkeypatch.setattr(leakage_execution, "run_leakage_test", runner)
    return state


def prepared(base, tmp_path):
    doc = leakage_profile(base)
    doc["output"]["root_directory"] = str(tmp_path / "runs")
    return doc


def test_success_wires_real_helpers_and_prints_summary(profile, tmp_path, wiring, capsys):
    from acquisition.spot_acquisition import SpotAcquisition
    from drivers.dc_output import enable_dc_output
    from experiments.charge_setup import configure_charge_source
    doc = prepared(profile, tmp_path)
    assert leakage_execution.run_configured_leakage(doc, NAME) == 0
    kwargs = wiring.runs[0]
    settings = settings_for(doc)
    assert kwargs["configure_source"] is configure_charge_source
    assert kwargs["spot_factory"] is SpotAcquisition
    assert kwargs["enable_output"] is enable_dc_output
    assert kwargs["maximum_voltage_v"] == settings["maximum_voltage_v"]
    assert kwargs["maximum_current_a"] == settings["maximum_current_a"]
    assert kwargs["run_directory"] == (tmp_path / "runs").absolute() / NAME
    assert kwargs["smu"] is wiring.session and wiring.session.closed
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == "completed" and out["hardware_qualified"] is False
    assert out["directory"] == str(kwargs["run_directory"])


def test_identity_mismatch_stops_before_the_runner(profile, tmp_path, wiring, capsys):
    wiring.serial = "OTHERSERIAL"
    assert leakage_execution.run_configured_leakage(prepared(profile, tmp_path), NAME) == 1
    assert wiring.runs == [] and wiring.session.closed and wiring.session.shutdowns == 1
    assert "identity" in capsys.readouterr().err


def test_existing_destination_is_refused_before_connecting(profile, tmp_path, wiring):
    (tmp_path / "runs" / NAME).mkdir(parents=True)
    assert leakage_execution.run_configured_leakage(prepared(profile, tmp_path), NAME) == 1
    assert wiring.opened == 0


@pytest.mark.parametrize("name", ["../x", "a b", "", "x" * 81])
def test_bad_run_names_are_refused(profile, tmp_path, wiring, name):
    assert leakage_execution.run_configured_leakage(prepared(profile, tmp_path), name) == 1
    assert wiring.opened == 0


def test_missing_serial_is_refused_before_connecting(profile, tmp_path, wiring, capsys):
    doc = prepared(profile, tmp_path)
    doc.pop("execution", None)
    doc["instrument"]["visa_address"] = "TCPIP0::192.168.0.2::inst0::INSTR"
    assert leakage_execution.run_configured_leakage(doc, NAME) == 1
    assert wiring.opened == 0 and "expected_serial" in capsys.readouterr().err


def test_keyboard_interrupt_returns_130_and_closes_the_session(profile, tmp_path, wiring):
    wiring.raises = KeyboardInterrupt()
    assert leakage_execution.run_configured_leakage(prepared(profile, tmp_path), NAME) == 130
    assert wiring.session.closed


def test_run_error_returns_1_reports_directory_and_closes(profile, tmp_path, wiring, capsys):
    wiring.raises = LeakageRunError("boom")
    assert leakage_execution.run_configured_leakage(prepared(profile, tmp_path), NAME) == 1
    err = capsys.readouterr().err
    report = json.loads(err[err.index("{"):])
    assert report["error_type"] == "LeakageRunError" and report["error_message"] == "boom"
    assert report["directory"] == str((tmp_path / "runs").absolute() / NAME)
    assert wiring.session.closed


def cli_profile(base, tmp_path):
    doc = leakage_profile(base)
    doc["output"].update(root_directory=str(tmp_path / "runs"),
                         analysis_directory=str(tmp_path / "out"), file_name="leak_test")
    return doc, save(doc, tmp_path / "leak.yaml")


def test_cli_execute_dispatches_with_a_timestamped_run_name(profile, tmp_path, monkeypatch):
    doc, path = cli_profile(profile, tmp_path)
    seen = []
    monkeypatch.setattr(leakage_execution, "run_configured_leakage",
                        lambda document, run_name: seen.append(run_name) or 0)
    assert smu.main(["--config", path, "--execute"]) == 0
    assert re.fullmatch(r"\d{8}_\d{6}_leak_test", seen[0])


def test_cli_execute_then_analyze_uses_the_recorded_run(profile, tmp_path, monkeypatch):
    doc, path = cli_profile(profile, tmp_path)
    calls = []

    def runner(document, run_name):
        (tmp_path / "runs" / run_name).mkdir(parents=True)
        calls.append(run_name)
        return 0

    def analyze(source, destination, *, expected_document=None):
        calls.append((Path(source), Path(destination)))
        return {"valid": True}

    monkeypatch.setattr(leakage_execution, "run_configured_leakage", runner)
    monkeypatch.setattr(leakage_analysis, "analyze_leakage_run", analyze)
    assert smu.main(["--config", path, "--execute", "--analyze"]) == 0
    run_name = calls[0]
    assert calls[1] == (tmp_path / "runs" / run_name, tmp_path / "out" / run_name)


def test_cli_failed_run_skips_analysis(profile, tmp_path, monkeypatch, capsys):
    doc, path = cli_profile(profile, tmp_path)
    monkeypatch.setattr(leakage_execution, "run_configured_leakage", lambda document, run_name: 1)
    monkeypatch.setattr(leakage_analysis, "analyze_leakage_run",
                        lambda *a, **k: pytest.fail("analysis must be skipped"))
    assert smu.main(["--config", path, "--execute", "--analyze"]) == 1
    assert "analysis skipped" in capsys.readouterr().err


def test_cli_analyze_only_and_invalid_result(profile, tmp_path, monkeypatch):
    doc, path = cli_profile(profile, tmp_path)
    (tmp_path / "runs" / "run1").mkdir(parents=True)
    results = iter([{"valid": True}, {"valid": False}])
    monkeypatch.setattr(leakage_analysis, "analyze_leakage_run",
                        lambda source, destination, *, expected_document=None: next(results))
    assert smu.main(["--config", path, "--analyze", "run1"]) == 0
    assert smu.main(["--config", path, "--analyze", "run1"]) == 1


def test_cli_analyze_errors(profile, tmp_path, capsys):
    doc, path = cli_profile(profile, tmp_path)
    assert smu.main(["--config", path, "--analyze", "missing_run"]) == 1
    assert "No recorded leakage run" in capsys.readouterr().err
    del doc["output"]["analysis_directory"]
    assert smu.main(["--config", save(doc, tmp_path / "other.yaml"), "--analyze", "run1"]) == 1
    assert "analysis_directory" in capsys.readouterr().err


def test_bench_check_profile_is_valid_and_short():
    root = Path(__file__).resolve().parents[1]
    doc = load_config(root / "configs/leakage_current_bench_check_ch1.yaml")["config"]
    plan = build_leakage_plan(doc)
    assert plan["schedule"]["total_sample_count"] == 1201
    assert plan["maximum_duration_s"] == 600 and plan["early_stop"]["enabled"] is False
