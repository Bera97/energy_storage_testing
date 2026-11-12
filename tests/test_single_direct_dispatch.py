"""Modern single-channel operations must bypass the backend parser."""
import pytest
import experiments.run_live as single
import test_unified_cli as unified
@pytest.fixture(autouse=True)
def forbid_backend_parser(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Modern CLI invoked the single-channel backend parser.")
    monkeypatch.setattr(single, "main", forbidden)
def test_preview_dispatches_directly(tmp_path, capsys):
    unified.test_preview_single_channel(tmp_path, capsys)
def test_execute_dispatches_directly(tmp_path, monkeypatch, capsys):
    unified.test_execute_uses_timestamped_run_folder(
        tmp_path, monkeypatch, capsys
    )
