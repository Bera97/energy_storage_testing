"""Recorded CP phase simulation with injected backend, clock and sleep.

No driver/VISA import or concrete hardware transport. Public SMU execution stays
blocked. An explicit simulation-only backend is required before files or I/O.
This backend flag is a caller contract, NOT a security boundary or sandbox.
"""
from dataclasses import dataclass
import math
from pathlib import Path
from experiments.constant_power import (
    ConstantPowerController, PowerLimits, PowerSample, PowerControlError, _number,
)
from recording.power_simulation import PowerSimulationRecording


class PowerAdapterError(PowerControlError):
    pass


@dataclass(frozen=True)
class PowerFrame:
    channel: int
    voltage_v: float
    current_a: float
    raw_instrument_timestamp_s: float
    status: int

    def __post_init__(self):
        if type(self.channel) is not int or self.channel not in (1, 2):
            raise ValueError("Invalid frame channel.")
        if type(self.status) is not int or not 0 <= self.status <= 0xFFFFFFFF:
            raise ValueError("Invalid frame status.")
        for key in ("voltage_v", "current_a", "raw_instrument_timestamp_s"):
            value = _number(getattr(self, key), key, signed=key != "raw_instrument_timestamp_s", zero=True)
            if abs(value) >= 9e37:
                raise ValueError("Invalid/sentinel measurement value.")
            object.__setattr__(self, key, value)

    def power_sample(self, acquired_at_s):
        return PowerSample(
            acquired_at_s, self.voltage_v, self.current_a,
            current_source=bool(self.status & 1),
            in_compliance=bool(self.status & 0b110),
            has_fault=bool(self.status & ((1 << 3) | (1 << 4) | (1 << 5) | (1 << 13))),
        )


class _HostClock:
    def __init__(self, clock):
        self.clock = clock
        self.last = None

    def now(self):
        value = _number(self.clock(), "host-monotonic clock", zero=True)
        if self.last is not None and value < self.last:
            raise PowerAdapterError("Host-monotonic clock moved backwards.")
        self.last = value
        return value


def _validate_setup(setup, limits, channel):
    if not isinstance(setup, dict):
        raise PowerAdapterError("Probe setup must return structured readback evidence.")
    if (type(setup.get("channel")) is not int or setup["channel"] != channel or
            setup.get("selected_output_off") is not True or
            setup.get("current_source") is not True or
            setup.get("auto_output_on") is not False):
        raise PowerAdapterError("Probe setup did not verify output OFF, CURR and auto-output OFF.")
    level = _number(setup.get("current_setpoint_a"), "probe setpoint", signed=True)
    compliance = _number(setup.get("voltage_compliance_v"), "probe voltage compliance")
    if level != 0 or not math.isclose(compliance, limits.maximum_voltage_v, rel_tol=1e-9, abs_tol=1e-12):
        raise PowerAdapterError("Probe setpoint or voltage compliance readback mismatch.")


def _wait(controller, host, sleep, due_s):
    quantum = min(controller.limits.update_interval_s, controller.limits.maximum_feedback_age_s) / 4
    while True:
        now = host.now()
        controller.watchdog(now_s=now)
        if now >= due_s:
            return
        wake = min(due_s, now + quantum, controller.started_s + controller.limits.maximum_duration_s)
        if controller.bad_tracking_since_s is not None:
            wake = min(wake, controller.bad_tracking_since_s + controller.limits.maximum_tracking_error_s)
        sleep(wake - now)
        if host.now() <= now:
            raise PowerAdapterError("Sleep/clock did not advance; refusing a busy loop.")


def run_simulated_power_phase(limits, directory, *, backend, clock, sleep,
                              channel, maximum_probe_current_a, maximum_samples=100000):
    """Own one SIMULATED phase/backend through shutdown-all and session close.

    backend.prepare(limits) configures zero current with selected output OFF and
    returns setup readbacks. set_output(True) explicitly activates the probe.
    read_fresh() returns one fresh paired PowerFrame acquired DURING that call,
    never queued/cached data or separately queried V and I. set_current() and
    get_current_setpoint() must check errors. get_output_states() returns both
    channel output readbacks after shutdown_all(). close_session() is last.

    Host read brackets are conservative timing bounds, not proof of instrument
    freshness. Raw instrument timestamps are retained without continuity claims.
    No retry/resume; no real hardware authorization; no ordinary COMPLETE marker.
    Blocking backend/filesystem calls can defeat software timing limits.
    """
    if not isinstance(limits, PowerLimits):
        raise ValueError("Validated PowerLimits required.")
    if type(channel) is not int or channel not in (1, 2):
        raise ValueError("channel must be 1 or 2.")
    if type(maximum_samples) is not int or maximum_samples < 1:
        raise ValueError("maximum_samples must be a positive integer.")
    probe_ceiling = _number(maximum_probe_current_a, "maximum_probe_current_a")
    if probe_ceiling > limits.maximum_current_a:
        raise ValueError("Probe ceiling exceeds the configured current ceiling.")
    methods = (
        "prepare", "set_output", "get_output_state", "read_fresh", "set_current",
        "get_current_setpoint", "shutdown_all", "get_output_states", "close_session",
    )
    if getattr(backend, "simulation_only", None) is not True:
        raise ValueError("This increment accepts only an explicitly simulation-only backend.")
    if not all(callable(getattr(backend, name, None)) for name in methods):
        raise ValueError("Incomplete simulation backend contract.")
    if not callable(clock) or not callable(sleep):
        raise ValueError("Explicit clock and sleep callables required.")
    directory = Path(directory)
    if directory.exists():
        raise FileExistsError("Refusing to overwrite existing simulation evidence.")
    host = _HostClock(clock)
    controller = ConstantPowerController(limits)
    recorder = None
    failure = None
    outcome = {
        "state": "running", "stop_reason": None,
        "shutdown_commands_returned": False, "outputs_reported_off": None,
        "session_close_returned": False, "cleanup_errors": [],
    }
    try:
        recorder = PowerSimulationRecording(directory, metadata={
            "limits": limits.to_dict(), "channel": channel,
            "maximum_probe_current_a": probe_ceiling, "maximum_samples": maximum_samples,
            "timestamp_basis": "host_read_brackets; raw instrument timestamps retained unmapped",
            "freshness_basis": "backend fresh paired-read contract; not independently verified",
        })
        from experiments.power_phase_engine import drive_power_phase
        outcome.update(drive_power_phase(
            limits, backend=backend, controller=controller, host=host, recorder=recorder,
            channel=channel, probe_ceiling=probe_ceiling, maximum_samples=maximum_samples, sleep=sleep,
        ))
    except BaseException as error:
        failure = error
    finally:
        def cleanup_error(error):
            nonlocal failure
            text = f"{type(error).__name__}: {error}"
            outcome["cleanup_errors"].append(text)
            if failure is None:
                failure = error
            else:
                failure.add_note("Power phase cleanup: " + text)
        try:
            backend.shutdown_all()
            outcome["shutdown_commands_returned"] = True
        except BaseException as error:
            cleanup_error(error)
        try:
            states = backend.get_output_states()
            if not isinstance(states, (list, tuple)) or len(states) != 2 or any(type(v) is not bool for v in states):
                raise PowerAdapterError("Malformed post-shutdown output readbacks.")
            outcome["output_states"] = list(states)
            outcome["outputs_reported_off"] = all(value is False for value in states)
            if outcome["outputs_reported_off"] is not True:
                raise PowerAdapterError("Shutdown did not report both outputs OFF.")
        except BaseException as error:
            cleanup_error(error)
        try:
            backend.close_session()
            outcome["session_close_returned"] = True
        except BaseException as error:
            cleanup_error(error)
    outcome["controller_state"] = controller.state
    outcome["phase_started_s"] = controller.started_s
    if recorder is not None:
        if failure is None:
            try:
                recorder.event("cleanup_outcome", **outcome)
                recorder.complete(outcome)
            except BaseException as error:
                failure = error
        if failure is not None:
            try:
                recorder.fail(failure, outcome)
            except BaseException as secondary:
                failure.add_note(f"Failure evidence export: {type(secondary).__name__}: {secondary}")
    if failure is not None:
        try:
            failure.power_phase_outcome = {**outcome, "state": "raised"}
        except BaseException as secondary:
            failure.add_note(f"Could not attach structured phase outcome: {secondary}")
        raise failure
    return {**outcome, "state": "completed", "directory": str(directory)}
