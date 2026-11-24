"""Exclusive, fail-closed simulation recording lifecycle; no hardware imports."""
import json
import pytest
from recording.power_simulation import PowerSimulationRecording, MARKER_TEXT


def success():
    return dict(stop_reason="voltage_min", controller_state="completed",
                shutdown_commands_returned=True, outputs_reported_off=True,
                session_close_returned=True)


def test_finalization_is_single_use_and_is_not_hardware_evidence(tmp_path):
    record = PowerSimulationRecording(tmp_path / "phase", metadata={"case": "test"})
    record.event("test_event", value=1)
    record.complete(success())
    initial = (record.directory / "power_simulation.json").read_bytes()
    assert (record.directory / "SIMULATION_COMPLETE").read_text() == MARKER_TEXT
    with pytest.raises(RuntimeError):
        record.complete(success())
    with pytest.raises(RuntimeError):
        record.fail(ValueError("late failure"), {})
    assert (record.directory / "power_simulation.json").read_bytes() == initial
    assert not (record.directory / "COMPLETE").exists()


@pytest.mark.parametrize("field,value", [
    ("stop_reason", "timeout"), ("controller_state", "active"),
    ("shutdown_commands_returned", False), ("outputs_reported_off", None),
    ("session_close_returned", False),
])
def test_missing_cleanup_or_wrong_endpoint_cannot_complete(tmp_path, field, value):
    record = PowerSimulationRecording(tmp_path / "phase", metadata={})
    outcome = success()
    outcome[field] = value
    with pytest.raises(ValueError):
        record.complete(outcome)
    record.fail(ValueError("incomplete"), outcome)
    assert not (record.directory / "SIMULATION_COMPLETE").exists()
    assert json.loads((record.directory / "power_simulation.json").read_text())["outcome"]["state"] == "raised"


def test_failed_serialization_poisoning_prevents_retry_and_success(tmp_path):
    record = PowerSimulationRecording(tmp_path / "phase", metadata={})
    with pytest.raises(ValueError):
        record.event("invalid", value=float("nan"))
    assert record.failed
    with pytest.raises(RuntimeError):
        record.event("do_not_retry")
    with pytest.raises(RuntimeError):
        record.complete(success())
    record.fail(ValueError("original"), {})
    metadata = json.loads((record.directory / "power_simulation.json").read_text())
    assert metadata["writer_failed"] is True and metadata["event_count_flushed"] == 0


def test_invalid_initial_metadata_does_not_create_directory(tmp_path):
    directory = tmp_path / "phase"
    with pytest.raises(ValueError):
        PowerSimulationRecording(directory, metadata={"bad": float("nan")})
    assert not directory.exists()


def test_existing_destination_is_never_overwritten(tmp_path):
    directory = tmp_path / "phase"
    directory.mkdir()
    (directory / "keep.txt").write_text("preserve")
    with pytest.raises(FileExistsError):
        PowerSimulationRecording(directory, metadata={})
    assert (directory / "keep.txt").read_text() == "preserve"


def test_close_attempts_both_streams_if_one_close_raises(tmp_path):
    record = PowerSimulationRecording(tmp_path / "phase", metadata={})
    original_samples = record._samples
    events = record._events
    class FailClose:
        def close(self):
            original_samples.close()
            raise OSError("CSV close failure")
    record._samples = FailClose()
    with pytest.raises(OSError, match="CSV close failure"):
        record.complete(success())
    assert events.closed and record.failed
    record.fail(ValueError("failed close"), {})
    metadata = json.loads((record.directory / "power_simulation.json").read_text())
    assert metadata["close_errors"] and metadata["outcome"]["state"] == "raised"
    assert not (record.directory / "SIMULATION_COMPLETE").exists()


def test_existing_marker_is_not_deleted_on_exclusive_create_failure(tmp_path):
    record = PowerSimulationRecording(tmp_path / "phase", metadata={})
    marker = record.directory / "SIMULATION_COMPLETE"
    marker.write_text("preexisting; not ours")
    with pytest.raises(FileExistsError):
        record.complete(success())
    record.fail(ValueError("unexpected marker"), {})
    assert marker.read_text() == "preexisting; not ours"
    assert json.loads((record.directory / "power_simulation.json").read_text())["outcome"]["state"] == "raised"
