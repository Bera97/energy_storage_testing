"""Internal recorded driver-contract phase; no CLI dispatch or connection factory."""
import json
from pathlib import Path
from experiments.constant_power import ConstantPowerController, PowerLimits, _number
from experiments.power_adapter import _HostClock, PowerAdapterError
from experiments.power_smu_backend import B2902BPowerBackend
from recording.power_phase import PowerPhaseRecording


def run_recorded_power_phase(limits, directory, *, backend, clock, sleep,
                              channel, maximum_probe_current_a, maximum_samples=100000,
                              specimen_id, execute_requested=False, configuration_snapshot=None):
    """Internal driver-contract phase API; public CLI execution remains disabled.

    Requires an already-connected, fresh B2902BPowerBackend and explicit software
    execution/output authorization. After successful preflight this function owns
    shutdown-all, readbacks and session close, including recorder creation failure.
    Rejected inputs/existing destinations leave backend ownership with the caller.
    Injected fake drivers exercise this path in tests; flags are not physical proof.
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
    if execute_requested is not True:
        raise ValueError("Explicit execute_requested=True is required for this internal API.")
    if not isinstance(backend, B2902BPowerBackend) or backend.simulation_only is not False:
        raise ValueError("A concrete non-simulation B2902BPowerBackend is required.")
    if backend.output_authorized is not True or backend.channel != channel:
        raise ValueError("Backend output authorization/channel mismatch.")
    if any((backend.prepared, backend.enabled, backend.failed, backend.stopped, backend.closed)):
        raise ValueError("A fresh, exclusively owned backend is required.")
    if limits.maximum_voltage_v > backend.maximum_voltage_v or limits.maximum_current_a > backend.maximum_current_a:
        raise ValueError("Phase limits exceed backend execution ceilings.")
    if not isinstance(specimen_id, str) or not specimen_id.strip() or specimen_id.strip().upper().startswith("REPLACE"):
        raise ValueError("A concrete specimen_id is required.")
    resource = getattr(backend.smu, "resource_address", None)
    if not isinstance(resource, str) or not resource.strip() or "REPLACE" in resource.upper():
        raise ValueError("A concrete backend resource address is required.")
    if configuration_snapshot is not None and not isinstance(configuration_snapshot, dict):
        raise ValueError("configuration_snapshot must be a mapping or None.")
    snapshot = json.loads(json.dumps(configuration_snapshot, allow_nan=False))
    if not all(callable(getattr(backend, name, None)) for name in methods):
        raise ValueError("Incomplete power backend contract.")
    if not callable(clock) or not callable(sleep):
        raise ValueError("Explicit clock and sleep callables required.")
    directory = Path(directory)
    if directory.exists():
        raise FileExistsError("Refusing to overwrite existing power-phase evidence.")
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
        recorder = PowerPhaseRecording(directory, metadata={
            "limits": limits.to_dict(), "channel": channel,
            "specimen_id": specimen_id.strip(), "configuration_snapshot": snapshot,
            "instrument": {
                "resource_address": resource, "expected_serial": backend.expected_serial,
                "four_wire": backend.four_wire, "nplc": backend.nplc,
                "maximum_voltage_v": backend.maximum_voltage_v,
                "maximum_current_a": backend.maximum_current_a,
                "driver_type": type(backend.smu).__module__ + "." + type(backend.smu).__qualname__,
            },
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
            outcome["cleanup_started_s"] = host.now()
        except BaseException as error:
            cleanup_error(error)
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
        try:
            outcome["cleanup_finished_s"] = host.now()
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
