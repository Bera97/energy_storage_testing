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
