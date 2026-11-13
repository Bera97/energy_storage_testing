"""Offline regression tests for defaults read from existing YAML."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
import smu
from experiments.settings import settings_for
def profile():
    return {
        "instrument": {
            "visa_address":
                "USB0::0x2A8D::0x9201::MY61391173::0::INSTR",
        },
        "dut": {"ratings": {
            "maximum_voltage_v": 3.81,
            "maximum_continuous_charge_current_a": 0.4,
            "maximum_continuous_discharge_current_a": 0.2,
        }},
    }
def test_defaults_use_existing_yaml_and_do_not_modify_it():
    document = profile()
    before = deepcopy(document)
    assert settings_for(document) == {
        "expected_serial": "MY61391173",
        "maximum_voltage_v": 3.81,
        "maximum_current_a": 0.4,
    }
    assert document == before
@pytest.mark.parametrize("resource", [
    "USB::0x2A8D::0x9201::MY61391173::INSTR",
    "USB0::0x2A8D::0x9201::MY61391173::0::INSTR",
    "USB0::0x2A8D::0x9201::MY61391173",
])
def test_supported_usb_resource_forms(resource):
    document = profile()
    document["instrument"]["visa_address"] = resource
    assert settings_for(document)["expected_serial"] == "MY61391173"
@pytest.mark.parametrize("resource", [
    "TCPIP0::192.0.2.1::inst0::INSTR",
    "TEST::INSTR",
    "USB0::0x2A8D::0x9201::*::0::INSTR",
    "USB0::0x2A8D::0x9201::REPLACE_SERIAL::0::INSTR",
])
def test_ambiguous_resource_does_not_invent_serial(resource):
    document = profile()
    document["instrument"]["visa_address"] = resource
    assert settings_for(document)["expected_serial"] is None

@pytest.mark.parametrize("serial", ["serial-123", "TEST_SERIAL.2"])
def test_serial_with_lowercase_s_is_preserved(serial):
    document = profile()
    document["instrument"]["visa_address"] = (
        f"USB0::0x2A8D::0x9201::{serial}::0::INSTR"
    )
    assert settings_for(document)["expected_serial"] == serial
@pytest.mark.parametrize("serial", ["BAD SERIAL", "BAD\tSERIAL", "BAD\\SERIAL"])
def test_serial_with_whitespace_or_backslash_is_rejected(serial):
    document = profile()
    document["instrument"]["visa_address"] = (
        f"USB0::0x2A8D::0x9201::{serial}::0::INSTR"
    )
    assert settings_for(document)["expected_serial"] is None

def test_explicit_limits_are_preserved():
    document = profile()
    document["execution"] = {
        "expected_serial": "EXPLICIT",
        "maximum_voltage_v": 3.5,
        "maximum_current_a": 0.1,
    }
    assert settings_for(document) == document["execution"]
def test_larger_discharge_rating_is_included():
    document = profile()
    document["dut"]["ratings"][
        "maximum_continuous_discharge_current_a"
    ] = 0.8
    assert settings_for(document)["maximum_current_a"] == 0.8
def test_original_public_command_uses_profile_without_execution_section(
    tmp_path, monkeypatch, capsys
):
    import yaml
    import experiments.run_live as single
    repository = Path(__file__).resolve().parents[1]
    source = repository / "configs/preconditioning.parallel.lic.test_run.yaml"
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    document.pop("execution", None)
    document["output"]["root_directory"] = str(tmp_path / "runs")
    document["output"]["analysis_directory"] = str(tmp_path / "analysis")
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    executions = []
    analyses = []
    def fake_execute(config, **options):
        executions.append(options)
        return {
            "completed_phase_count": 4,
            "shutdown_verified": True,
        }, tmp_path / "synthetic_result"
    def fake_analyze(config, run_name):
        analyses.append(run_name)
        return 0
    def forbidden(*args, **kwargs):
        pytest.fail("Test attempted hardware access or an interactive prompt.")
    monkeypatch.setattr(single, "execute_live", fake_execute)
    monkeypatch.setattr(single, "_new_smu", forbidden)
    monkeypatch.setattr(smu, "_unified_analyze", fake_analyze)
    monkeypatch.setattr("builtins.input", forbidden)
    assert smu.main([
        "--config", str(path), "--execute", "--analyze",
    ]) == 0
    assert len(executions) == 1
    assert executions[0]["expected_serial"] == "MY61391173"
    assert executions[0]["maximum_voltage_v"] == 3.81
    assert executions[0]["maximum_current_a"] == 0.4
    assert analyses == [executions[0]["experiment_id"]]
    assert json.loads(capsys.readouterr().out)["state"] == "completed"
