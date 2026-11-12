"""Modern CLI operations must bypass execution backend parsers."""
import pytest
import experiments.run_live as single
import experiments.dual_live as dual
import experiments.dual_preconditioning as planner
import test_unified_cli as unified
@pytest.fixture(autouse=True)
def forbid_backend_parser(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Modern CLI invoked the single-channel backend parser.")
    monkeypatch.setattr(single, "main", forbidden)
    monkeypatch.setattr(dual, "main", forbidden)
    monkeypatch.setattr(planner, "main", forbidden)
def test_preview_dispatches_directly(tmp_path, capsys):
    unified.test_preview_single_channel(tmp_path, capsys)
def test_execute_dispatches_directly(tmp_path, monkeypatch, capsys):
    unified.test_execute_uses_timestamped_run_folder(
        tmp_path, monkeypatch, capsys
    )
def test_dual_execute_dispatches_directly(tmp_path, monkeypatch, capsys):
    unified.test_dual_execute_uses_timestamped_batch_folder(
        tmp_path, monkeypatch, capsys
    )
def test_failed_dual_execution_skips_analysis(tmp_path, monkeypatch, capsys):
    paths = unified.save_pair(unified.make_pair(tmp_path), tmp_path)
    def fail_execution(*args, **kwargs):
        raise RuntimeError("synthetic dual execution failure")
    monkeypatch.setattr(dual, "execute", fail_execution)
    status = unified.smu.main([
        "--config", paths[0],
        "--config", paths[1],
        "--execute",
        "--analyze",
    ])
    assert status == 1
    captured = capsys.readouterr()
    assert "synthetic dual execution failure" in captured.err
    assert "analysis skipped" in captured.err
def test_dual_preview_dispatches_directly(tmp_path, capsys):
    unified.test_preview_dual_channel(tmp_path, capsys)
def test_dual_analysis_import_does_not_load_live_backend():
    from pathlib import Path
    import subprocess
    import sys
    code = (
        "import sys; "
        "import analysis.dual_experiment; "
        "assert 'experiments.dual_live' not in sys.modules; "
        "assert 'drivers.keysight_b2902b' not in sys.modules; "
        "assert 'pyvisa' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
def _public_profiles(tmp_path, channel_count):
    from copy import deepcopy
    from pathlib import Path
    import yaml
    repository = Path(__file__).resolve().parents[1]
    documents = []
    for channel in range(1, channel_count + 1):
        source = (
            repository / "configs"
            / f"preconditioning.channel{channel}.yaml"
        )
        document = deepcopy(
            yaml.safe_load(source.read_text(encoding="utf-8"))
        )
        document["output"]["root_directory"] = str(tmp_path / "runs")
        document["output"]["analysis_directory"] = str(tmp_path / "analysis")
        document["output"]["file_name"] = f"preflight_ch{channel}"
        preview = single.execution_preview(document)
        document["execution"] = {
            "expected_serial": "TESTSERIAL",
            "maximum_voltage_v": preview["required_preflight_ceiling_v"],
            "maximum_current_a": preview["required_preflight_ceiling_a"],
        }
        documents.append(document)
    return documents
def _write_public_profiles(tmp_path, documents):
    import yaml
    arguments = []
    for index, document in enumerate(documents, 1):
        path = tmp_path / f"public_profile_{index}.yaml"
        path.write_text(
            yaml.safe_dump(document),
            encoding="utf-8",
        )
        arguments.extend(["--config", str(path)])
    return arguments
def _public_status(arguments):
    try:
        return unified.smu.main(arguments)
    except SystemExit as error:
        return error.code
def _block_public_execution(monkeypatch):
    import experiments.dual_instrument_io as instrument_io
    entered = []
    def forbidden(*args, **kwargs):
        entered.append((args, kwargs))
        raise AssertionError("Execution or instrument construction was reached.")
    monkeypatch.setattr(single, "execute_live", forbidden)
    monkeypatch.setattr(dual, "execute", forbidden)
    monkeypatch.setattr(single, "_new_smu", forbidden)
    monkeypatch.setattr(instrument_io, "_new_smu", forbidden)
    return entered
def test_public_dual_serial_disagreement_prevents_execution(
    tmp_path, monkeypatch, capsys
):
    documents = _public_profiles(tmp_path, 2)
    documents[1]["execution"]["expected_serial"] = "OTHER_SERIAL"
    arguments = _write_public_profiles(tmp_path, documents)
    entered = _block_public_execution(monkeypatch)
    assert _public_status([*arguments, "--execute"]) == 1
    assert entered == []
    assert "same expected instrument serial" in capsys.readouterr().err
@pytest.mark.parametrize("channel_count", [1, 2])
@pytest.mark.parametrize(
    "setting",
    ["maximum_voltage_v", "maximum_current_a"],
)
def test_public_insufficient_ceiling_prevents_execution(
    tmp_path, monkeypatch, capsys, channel_count, setting
):
    documents = _public_profiles(tmp_path, channel_count)
    documents[0]["execution"][setting] /= 2
    arguments = _write_public_profiles(tmp_path, documents)
    entered = _block_public_execution(monkeypatch)
    assert _public_status([*arguments, "--execute"]) == 1
    assert entered == []
    error = capsys.readouterr().err
    assert f"execution.{setting}" in error
    assert "below the configured requirement" in error
@pytest.mark.parametrize("channel_count", [1, 2])
def test_public_missing_execution_settings_prevents_execution(
    tmp_path, monkeypatch, capsys, channel_count
):
    documents = _public_profiles(tmp_path, channel_count)
    for document in documents:
        del document["execution"]
    arguments = _write_public_profiles(tmp_path, documents)
    entered = _block_public_execution(monkeypatch)
    assert _public_status([*arguments, "--execute"]) in (1, 2)
    assert entered == []
    assert "serial" in capsys.readouterr().err.lower()
@pytest.mark.parametrize("channel_count", [1, 2])
def test_public_execution_does_not_prompt(
    tmp_path, monkeypatch, capsys, channel_count
):
    documents = _public_profiles(tmp_path, channel_count)
    arguments = _write_public_profiles(tmp_path, documents)
    executions = []
    prompts = []
    def forbidden_prompt(*args, **kwargs):
        prompts.append((args, kwargs))
        raise AssertionError("Unexpected interactive confirmation.")
    def fake_single(document, **options):
        executions.append(("single", document, options))
        return (
            {
                "completed_phase_count": 4,
                "shutdown_verified": True,
            },
            tmp_path / "synthetic_result",
        )
    def fake_dual(plan, directory, serial):
        executions.append(("dual", plan, serial))
        return {
            "state": "completed",
            "final_stop_verified": True,
            "session_close_returned": True,
        }
    def forbidden_instrument(*args, **kwargs):
        raise AssertionError("Instrument construction must not occur.")
    import experiments.dual_instrument_io as instrument_io
    monkeypatch.setattr("builtins.input", forbidden_prompt)
    monkeypatch.setattr(single, "_new_smu", forbidden_instrument)
    monkeypatch.setattr(instrument_io, "_new_smu", forbidden_instrument)
    monkeypatch.setattr(single, "execute_live", fake_single)
    monkeypatch.setattr(dual, "execute", fake_dual)
    assert _public_status([*arguments, "--execute"]) == 0
    assert prompts == []
    assert len(executions) == 1
    assert executions[0][0] == ("single" if channel_count == 1 else "dual")
    if channel_count == 1:
        assert executions[0][2]["expected_serial"] == "TESTSERIAL"
        assert executions[0][2]["confirm_specimen"] is None
    else:
        assert executions[0][2] == "TESTSERIAL"
    assert '"state": "completed"' in capsys.readouterr().out
