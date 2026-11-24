"""Virtual-clock/paired-reading simulations. Never construct a real instrument."""
import csv
from dataclasses import asdict
import json
import pytest
from test_constant_power_controller import limits
from experiments.constant_power import (
    ConstantPowerController, PowerSample, PowerControlError,
    PowerFeedbackError, PowerTimeoutError, PowerTrackingError,
)
from experiments.power_adapter import run_simulated_power_phase, PowerFrame, PowerAdapterError
from recording.power_simulation import PowerSimulationRecording, MARKER_TEXT


class VirtualClock:
    def __init__(self):
        self.time = 0.0
        self.sleeps = []

    def now(self):
        return self.time

    def sleep(self, delay):
        assert delay > 0
        self.sleeps.append(delay)
        self.time += delay


class FakeBackend:
    simulation_only = True

    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.current = 0.0
        self.enabled = False
        self.closed = False
        self.voltages = [3.8, 3.6, 3.4, 2.2]
        self.read_count = 0
        self.setup_changes = {}
        self.failures = {}
        self.delays = {}
        self.readback_override = None
        self.measured_current = None
        self.frame_channel = 1
        self.status = 1
        self.final_states = (False, False)
        self.raw_timestamps = None

    def _call(self, name):
        self.calls.append(name)
        self.clock.time += self.delays.get(name, 0.002)
        if name in self.failures:
            raise self.failures[name]

    def prepare(self, policy):
        self._call("prepare")
        self.current = 0
        result = dict(channel=1, selected_output_off=True, current_source=True,
                      auto_output_on=False, current_setpoint_a=0,
                      voltage_compliance_v=policy.maximum_voltage_v)
        result.update(self.setup_changes)
        return result

    def set_output(self, enabled):
        self._call("set_output")
        assert enabled is True and self.current == 0
        self.enabled = True

    def get_output_state(self):
        self._call("get_output_state")
        return self.enabled

    def read_fresh(self):
        self._call("read_fresh")
        assert self.enabled
        index = min(self.read_count, len(self.voltages) - 1)
        current = self.current if self.measured_current is None else self.measured_current
        timestamp = self.read_count if self.raw_timestamps is None else self.raw_timestamps[index]
        self.read_count += 1
        return PowerFrame(self.frame_channel, self.voltages[index], current, timestamp, self.status)

    def set_current(self, value):
        self._call("set_current")
        assert self.enabled and value < 0
        self.current = value

    def get_current_setpoint(self):
        self._call("get_current_setpoint")
        return self.current if self.readback_override is None else self.readback_override

    def shutdown_all(self):
        self._call("shutdown_all")
        self.enabled = False

    def get_output_states(self):
        self._call("get_output_states")
        return self.final_states

    def close_session(self):
        self._call("close_session")
        self.closed = True


def run(tmp_path, backend, **changes):
    return run_simulated_power_phase(
        limits(**changes), tmp_path / "phase", backend=backend,
        clock=backend.clock.now, sleep=backend.clock.sleep,
        channel=1, maximum_probe_current_a=0.001,
    )


def evidence(tmp_path):
    directory = tmp_path / "phase"
    metadata = json.loads((directory / "power_simulation.json").read_text())
    events = [json.loads(line) for line in (directory / "power_events.jsonl").read_text().splitlines()]
    with (directory / "power_samples.csv").open(newline="") as handle:
        samples = list(csv.DictReader(handle))
    return metadata, events, samples


def assert_cleanup(backend):
    assert backend.calls[-3:] == ["shutdown_all", "get_output_states", "close_session"]


def test_success_records_probe_decisions_commands_readbacks_and_shutdown(tmp_path):
    clock = VirtualClock()
    backend = FakeBackend(clock)
    result = run(tmp_path, backend)
    assert result["state"] == "completed" and result["stop_reason"] == "voltage_min"
    assert_cleanup(backend)
    assert backend.closed and not backend.enabled
    metadata, events, samples = evidence(tmp_path)
    assert metadata["simulation_only"] is True and metadata["hardware_execution_authorized"] is False
    assert metadata["outcome"]["outputs_reported_off"] is True
    assert metadata["sample_count_flushed"] == 4
    assert [s["stage"] for s in samples] == ["zero_current_probe"] + ["power_discharge"] * 3
    assert float(samples[0]["current_a"]) == 0
    assert all(float(s["current_a"]) < 0 for s in samples[1:])
    names = [event["event"] for event in events]
    assert names.index("probe_output_on_verified") < names.index("current_command_requested")
    assert names.count("current_setpoint_acknowledged") == 3
    assert [e["event_index"] for e in events] == list(range(len(events)))
    assert (tmp_path / "phase/SIMULATION_COMPLETE").read_text() == MARKER_TEXT
    assert not (tmp_path / "phase/COMPLETE").exists()
    assert not (tmp_path / "phase/ANALYSIS_COMPLETE").exists()
    assert clock.sleeps


def test_raw_instrument_time_resets_are_preserved_not_used_as_host_time(tmp_path):
    backend = FakeBackend(VirtualClock())
    backend.raw_timestamps = [12, 0, 5, 1]
    run(tmp_path, backend)
    _, _, samples = evidence(tmp_path)
    assert [float(s["raw_instrument_timestamp_s"]) for s in samples] == [12, 0, 5, 1]
    starts = [float(s["host_read_started_s"]) for s in samples]
    assert starts == sorted(set(starts))


@pytest.mark.parametrize("field,value", [
    ("selected_output_off", False), ("auto_output_on", True),
    ("current_source", False), ("current_setpoint_a", -0.1),
    ("current_setpoint_a", False), ("voltage_compliance_v", 4.0),
    ("channel", 2), ("channel", True),
])
def test_bad_probe_setup_never_enables_output(tmp_path, field, value):
    backend = FakeBackend(VirtualClock())
    backend.setup_changes[field] = value
    with pytest.raises((PowerControlError, ValueError)):
        run(tmp_path, backend)
    assert "set_output" not in backend.calls
    assert_cleanup(backend)
    metadata, _, _ = evidence(tmp_path)
    assert metadata["outcome"]["state"] == "raised"
    assert not (tmp_path / "phase/SIMULATION_COMPLETE").exists()


@pytest.mark.parametrize("operation", ["prepare", "set_output", "get_output_state", "read_fresh", "set_current", "get_current_setpoint"])
def test_backend_faults_always_attempt_shutdown_readback_and_close(tmp_path, operation):
    backend = FakeBackend(VirtualClock())
    original = OSError("injected " + operation)
    backend.failures[operation] = original
    with pytest.raises(OSError) as caught:
        run(tmp_path, backend)
    assert caught.value is original
    assert_cleanup(backend)
    assert backend.closed
    metadata, _, _ = evidence(tmp_path)
    assert metadata["outcome"]["state"] == "raised"
    assert not (tmp_path / "phase/SIMULATION_COMPLETE").exists()


def test_keyboard_interrupt_preserves_primary_and_cleans_up(tmp_path):
    backend = FakeBackend(VirtualClock())
    original = KeyboardInterrupt("injected")
    backend.failures["read_fresh"] = original
    with pytest.raises(KeyboardInterrupt) as caught:
        run(tmp_path, backend)
    assert caught.value is original
    assert_cleanup(backend)
    assert evidence(tmp_path)[0]["outcome"]["error_type"] == "KeyboardInterrupt"


@pytest.mark.parametrize("operation", ["shutdown_all", "get_output_states", "close_session"])
def test_cleanup_failure_prevents_completion(tmp_path, operation):
    backend = FakeBackend(VirtualClock())
    backend.failures[operation] = OSError("cleanup " + operation)
    with pytest.raises(OSError):
        run(tmp_path, backend)
    assert_cleanup(backend)
    assert evidence(tmp_path)[0]["outcome"]["state"] == "raised"
    assert not (tmp_path / "phase/SIMULATION_COMPLETE").exists()


def test_cleanup_failure_does_not_replace_primary_error(tmp_path):
    backend = FakeBackend(VirtualClock())
    original = ValueError("primary measurement error")
    backend.failures.update(read_fresh=original, shutdown_all=OSError("shutdown"), close_session=OSError("close"))
    with pytest.raises(ValueError) as caught:
        run(tmp_path, backend)
    assert caught.value is original
    assert len(original.power_phase_outcome["cleanup_errors"]) == 2
    assert_cleanup(backend)


@pytest.mark.parametrize("states", [(True, False), (False, True), (0, 0), (False,), None])
def test_output_off_must_be_explicitly_reported_for_both_channels(tmp_path, states):
    backend = FakeBackend(VirtualClock())
    backend.final_states = states
    with pytest.raises(PowerAdapterError):
        run(tmp_path, backend)
    assert_cleanup(backend)
    assert not (tmp_path / "phase/SIMULATION_COMPLETE").exists()


@pytest.mark.parametrize("status", [0, 3, 5, 9, 17, 33, 8193])
def test_invalid_mode_compliance_or_fault_is_preserved_before_failure(tmp_path, status):
    backend = FakeBackend(VirtualClock())
    backend.status = status
    with pytest.raises(PowerControlError):
        run(tmp_path, backend)
    metadata, _, samples = evidence(tmp_path)
    assert int(samples[0]["status"]) == status
    assert metadata["sample_count_flushed"] == 1
    assert_cleanup(backend)


def test_wrong_readback_is_failure_not_an_acknowledged_command(tmp_path):
    backend = FakeBackend(VirtualClock())
    backend.readback_override = -0.3
    with pytest.raises(PowerFeedbackError, match="mismatch"):
        run(tmp_path, backend)
    _, events, _ = evidence(tmp_path)
    assert "current_readback" in [e["event"] for e in events]
    assert "current_setpoint_acknowledged" not in [e["event"] for e in events]
    assert_cleanup(backend)


def test_wrong_channel_sample_is_retained_before_failure(tmp_path):
    backend = FakeBackend(VirtualClock())
    backend.frame_channel = 2
    with pytest.raises(PowerAdapterError, match="channel"):
        run(tmp_path, backend)
    assert evidence(tmp_path)[2][0]["channel"] == "2"
    assert_cleanup(backend)


def test_delayed_feedback_is_recorded_then_rejected(tmp_path):
    backend = FakeBackend(VirtualClock())
    backend.delays["read_fresh"] = 0.6
    with pytest.raises(PowerTimeoutError):
        run(tmp_path, backend)
    assert len(evidence(tmp_path)[2]) == 1
    assert_cleanup(backend)


def test_overcurrent_probe_is_rejected_without_power_command(tmp_path):
    backend = FakeBackend(VirtualClock())
    backend.measured_current = 0.002
    with pytest.raises(PowerFeedbackError, match="Probe"):
        run(tmp_path, backend)
    assert "set_current" not in backend.calls
    assert_cleanup(backend)


def test_power_tracking_and_phase_duration_are_failure_endpoints(tmp_path):
    backend = FakeBackend(VirtualClock())
    backend.voltages = [3.0]
    backend.measured_current = 0.0
    with pytest.raises(PowerTrackingError):
        run(tmp_path, backend, startup_grace_s=0)
    assert_cleanup(backend)
    backend = FakeBackend(VirtualClock())
    backend.voltages = [3.0]
    with pytest.raises(PowerTimeoutError, match="duration"):
        run_simulated_power_phase(limits(maximum_duration_s=2.1), tmp_path / "timeout",
                                 backend=backend, clock=backend.clock.now, sleep=backend.clock.sleep,
                                 channel=1, maximum_probe_current_a=0.001)
    assert_cleanup(backend)


def test_budget_exhaustion_is_not_success(tmp_path):
    backend = FakeBackend(VirtualClock())
    with pytest.raises(PowerAdapterError, match="budget"):
        run_simulated_power_phase(limits(), tmp_path / "phase", backend=backend,
                                 clock=backend.clock.now, sleep=backend.clock.sleep,
                                 channel=1, maximum_probe_current_a=0.001, maximum_samples=1)
    assert_cleanup(backend)
    assert not (tmp_path / "phase/SIMULATION_COMPLETE").exists()


def test_write_failure_does_not_prevent_instrument_cleanup(tmp_path, monkeypatch):
    backend = FakeBackend(VirtualClock())
    original = PowerSimulationRecording.event
    def fail_command_log(self, event, **details):
        if event == "current_command_requested":
            raise OSError("journal failure before power command")
        return original(self, event, **details)
    monkeypatch.setattr(PowerSimulationRecording, "event", fail_command_log)
    with pytest.raises(OSError, match="journal failure"):
        run(tmp_path, backend)
    assert "set_current" not in backend.calls
    assert_cleanup(backend)
    assert evidence(tmp_path)[0]["outcome"]["state"] == "raised"


def test_existing_destination_and_non_simulated_backends_are_rejected_before_io(tmp_path):
    backend = FakeBackend(VirtualClock())
    (tmp_path / "phase").mkdir()
    with pytest.raises(FileExistsError):
        run(tmp_path, backend)
    assert backend.calls == []
    backend.simulation_only = False
    with pytest.raises(ValueError, match="simulation-only"):
        run_simulated_power_phase(limits(), tmp_path / "other", backend=backend,
                                 clock=backend.clock.now, sleep=backend.clock.sleep,
                                 channel=1, maximum_probe_current_a=0.001)
    assert backend.calls == [] and not (tmp_path / "other").exists()


def test_probe_can_stop_at_cutoff_without_a_power_command(tmp_path):
    backend = FakeBackend(VirtualClock())
    backend.voltages = [2.2]
    result = run(tmp_path, backend)
    assert result["state"] == "completed"
    assert "set_current" not in backend.calls
    assert_cleanup(backend)


def test_probe_requires_real_boolean_activation_and_has_initial_watchdog():
    control = ConstantPowerController(limits())
    control.arm_zero_current_probe(now_s=0, outputs_verified_off=True, current_source_verified=True,
                                  zero_current_setpoint_a=0, maximum_probe_current_a=0.001)
    with pytest.raises(PowerTimeoutError):
        control.watchdog(now_s=0.501)
    control = ConstantPowerController(limits())
    control.arm_zero_current_probe(now_s=0, outputs_verified_off=True, current_source_verified=True,
                                  zero_current_setpoint_a=0, maximum_probe_current_a=0.001)
    with pytest.raises(PowerControlError):
        control.confirm_zero_current_probe_enabled(now_s=0.1, output_reported_on=1)


def test_probe_sample_cannot_predate_probe_activation():
    control = ConstantPowerController(limits())
    control.arm_zero_current_probe(now_s=0, outputs_verified_off=True, current_source_verified=True,
                                  zero_current_setpoint_a=0, maximum_probe_current_a=0.001)
    control.confirm_zero_current_probe_enabled(now_s=0.2, output_reported_on=True)
    with pytest.raises(PowerFeedbackError, match="predates"):
        control.start_from_zero_current_probe(now_s=0.3, sample=PowerSample(0.1, 3.8, 0))


def test_frame_matches_existing_status_bit_meanings():
    frame = PowerFrame(1, 3.0, -0.2, 0, 1 | (1 << 13))
    sample = frame.power_sample(10)
    assert sample.current_source and sample.has_fault and not sample.in_compliance
    assert asdict(frame)["raw_instrument_timestamp_s"] == 0

@pytest.mark.parametrize("changes", [
    {"channel": True}, {"channel": 3}, {"status": True}, {"status": -1},
    {"status": 2**32}, {"voltage_v": float("nan")},
    {"current_a": float("inf")}, {"raw_instrument_timestamp_s": -1},
    {"raw_instrument_timestamp_s": True}, {"voltage_v": 9e37},
])
def test_malformed_frame_rejected(changes):
    arguments = dict(channel=1, voltage_v=3.0, current_a=-0.2,
                     raw_instrument_timestamp_s=0, status=1)
    arguments.update(changes)
    with pytest.raises(ValueError):
        PowerFrame(**arguments)


def test_recording_creation_failure_still_cleans_owned_backend(tmp_path, monkeypatch):
    import experiments.power_adapter as adapter
    def fail_creation(*args, **kwargs):
        raise OSError("recording creation")
    monkeypatch.setattr(adapter, "PowerSimulationRecording", fail_creation)
    backend = FakeBackend(VirtualClock())
    with pytest.raises(OSError, match="recording creation"):
        run(tmp_path, backend)
    assert backend.calls == ["shutdown_all", "get_output_states", "close_session"]


def test_sleep_failure_and_nonadvancing_clock_are_terminal(tmp_path):
    backend = FakeBackend(VirtualClock())
    with pytest.raises(PowerAdapterError, match="did not advance"):
        run_simulated_power_phase(limits(), tmp_path / "phase", backend=backend,
                                 clock=backend.clock.now, sleep=lambda delay: None,
                                 channel=1, maximum_probe_current_a=0.001)
    assert_cleanup(backend)


def test_slow_probe_activation_fails_before_another_backend_query(tmp_path):
    backend = FakeBackend(VirtualClock())
    backend.delays["set_output"] = 0.6
    with pytest.raises(PowerTimeoutError):
        run(tmp_path, backend)
    assert "get_output_state" not in backend.calls
    assert "set_current" not in backend.calls
    assert_cleanup(backend)
