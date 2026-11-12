"""Offline tests for the retained dual-batch validation parser."""
import json
import pytest
import experiments.dual_live as dual
@pytest.mark.parametrize("valid, expected_status", [(True, 0), (False, 1)])
def test_validation_reports_result_without_execution(
    monkeypatch, capsys, tmp_path, valid, expected_status
):
    calls = []
    result = {"valid": valid, "errors": [] if valid else ["synthetic invalid evidence"]}
    def check(directory):
        calls.append(directory)
        return result
    def forbidden(*args, **kwargs):
        pytest.fail("Validation attempted execution or instrument construction.")
    monkeypatch.setattr(dual, "check_batch", check)
    monkeypatch.setattr(dual, "execute", forbidden)
    monkeypatch.setattr(dual, "RealBackend", forbidden)
    monkeypatch.setattr(dual, "DualInstrumentSession", forbidden)
    directory = str(tmp_path / "recorded_batch")
    assert dual.main(["dual-check", directory]) == expected_status
    assert calls == [directory]
    assert json.loads(capsys.readouterr().out) == result
@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["dual-check"],
        ["dual-run", "--config", "unused.yaml"],
        ["--config", "unused.yaml", "--execute"],
        ["dual-check", "unused", "--execute"],
    ],
)
def test_invalid_or_retired_commands_do_not_validate_or_execute(
    monkeypatch, arguments
):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid arguments reached a backend operation.")
    monkeypatch.setattr(dual, "check_batch", forbidden)
    monkeypatch.setattr(dual, "execute", forbidden)
    with pytest.raises(SystemExit) as caught:
        dual.main(arguments)
    assert caught.value.code == 2
