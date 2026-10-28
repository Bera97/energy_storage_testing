"""Shared scheduling core and simulation entry point. Real backend: dual_live."""
import argparse
from copy import deepcopy
import json
import math
import sys
from experiments.config import load_config
from experiments.dual_preconditioning import build_dual_plan
def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite and positive.")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive.")
    return value
class VirtualClock:
    def __init__(self):
        self.now = 0.0
    def __call__(self):
        return self.now
    def sleep(self, duration):
        self.now += duration
class SimulatedInstrument:
    """Fake endpoints exercise orchestration, not electrochemistry."""
    simulation_only = True
    def __init__(self, clock, *, phase_duration_s=1.0, fail_channel=None):
        self.clock = clock
        self.phase_duration_s = _positive(
            phase_duration_s, "phase_duration_s"
        )
        if fail_channel not in (None, 1, 2):
            raise ValueError("fail_channel must be 1, 2, or None.")
        self.fail_channel = fail_channel
        self.active = {}
        self.events = []
        self.finalized = []
        self.connected = False
    def connect(self, resource):
        self.connected = True
        self.events.append({
            "action": "connect",
            "resource": resource,
        })
    def configure_shared(self, plan):
        if self.active:
            raise RuntimeError(
                "Shared configuration attempted with active channels."
            )
        self.events.append({"action": "configure_shared"})
    def start_phase(self, channel, phase):
        if not self.connected or channel in self.active:
            raise RuntimeError("Invalid simulated start.")
        self.active[channel] = (self.clock(), deepcopy(phase))
        self.events.append({
            "action": "start",
            "channel": channel,
        })
    def poll_phase(self, channel):
        if channel == self.fail_channel:
            raise RuntimeError(f"Synthetic channel {channel} fault")
        started, phase = self.active[channel]
        duration = self.phase_duration_s * (
            1.0 if channel == 1 else 1.5
        )
        if self.clock() - started < duration:
            return None
        return {
            "state": "completed",
            "stop_reason": (
                "cv_current_termination"
                if phase["type"] == "charge_cc_cv"
                else "voltage_min"
            ),
            "evidence_kind": "simulated",
        }
    def stop_channel(self, channel):
        self.active.pop(channel, None)
        self.events.append({
            "action": "stop_channel",
            "channel": channel,
            "other_active_channels": sorted(self.active),
        })
        return True
    def finalize_phase(self, channel, phase, outcome):
        self.finalized.append({
            "channel": channel,
            "phase": deepcopy(phase),
            "outcome": deepcopy(outcome),
        })
    def shutdown_all(self):
        self.active.clear()
        self.events.append({"action": "shutdown_all"})
        return True
    def close(self):
        self.connected = False
        self.events.append({"action": "close"})
def _run_backend(plan, backend, *, clock, sleep, poll_interval_s=0.05, simulation_only=True):
    """Internal shared scheduling core; public entries retain their own execution authorization."""

    def deadline_for(state):
        callback = getattr(backend, 'phase_deadline', None)
        return state['deadline_s'] if callback is None else callback(state['channel'], state['deadline_s'])
    interval = _positive(poll_interval_s, 'poll_interval_s')
    if not callable(clock) or not callable(sleep):
        raise ValueError('clock and sleep must be callable.')
    previews = plan['channels']
    documents = [item['acquisition_plan']['configuration'] for item in previews]
    maximum = previews[0]['acquisition_plan']['acquisition_planning_limit']
    verified = build_dual_plan(documents, maximum_records=maximum)
    if plan != verified:
        raise ValueError('Plan differs from a freshly validated dual plan.')
    states = {item['channel']: {'channel': item['channel'], 'specimen_id': item['specimen_id'], 'state': 'pending', 'next_index': 0, 'phases': deepcopy(item['acquisition_plan']['phases']), 'outcomes': [], 'current_phase': None} for item in verified['channels']}
    report = {'schema_version': 1, 'simulation_only': simulation_only, 'hardware_execution_enabled': not simulation_only, 'evidence_kind': 'simulated' if simulation_only else 'instrument_readback_and_recording', 'state': 'running', 'startup_stop_verified': None, 'final_stop_verified': None, 'close_succeeded': None, 'error': None, 'cleanup_errors': [], 'channels': []}
    primary = None
    last_time = None

    def now():
        nonlocal last_time
        value = clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError('Clock must return a finite number.')
        value = float(value)
        if not math.isfinite(value) or (last_time is not None and value < last_time):
            raise RuntimeError('Invalid or backwards clock.')
        last_time = value
        return value

    def require_stop(channel):
        if backend.stop_channel(channel) is not True:
            raise RuntimeError(f'Channel {channel} stop was not verified.')

    def finish(state, timestamp, result):
        channel = state['channel']
        phase = state['current_phase']
        require_stop(channel)
        outcome = {**result, 'phase_index': phase['phase_index'], 'cycle': phase['cycle'], 'type': phase['type'], 'started_s': state['started_s'], 'finished_s': timestamp, 'stop_verified': True, 'evidence_kind': 'simulated' if simulation_only else 'instrument_readback_and_recording'}
        backend.finalize_phase(channel, phase, outcome)
        state['outcomes'].append(outcome)
        state['current_phase'] = None
        state['next_index'] += 1
        state['state'] = 'completed' if state['next_index'] == len(state['phases']) else 'pending'
    try:
        backend.connect(verified['resource'])
        if backend.shutdown_all() is not True:
            raise RuntimeError('Initial stop-both was not verified.')
        report['startup_stop_verified'] = True
        backend.configure_shared(deepcopy(verified))
        while any((state['state'] != 'completed' for state in states.values())):
            for channel in sorted(states):
                state = states[channel]
                if state['state'] == 'completed':
                    continue
                timestamp = now()
                if state['state'] == 'pending':
                    phase = deepcopy(state['phases'][state['next_index']])
                    state['current_phase'] = phase
                    state['started_s'] = timestamp
                    if phase['type'] == 'rest':
                        require_stop(channel)
                        state['deadline_s'] = timestamp + phase['duration_s']
                        state['state'] = 'resting'
                    else:
                        state['deadline_s'] = timestamp + phase['maximum_duration_s']
                        state['state'] = 'active'
                        backend.start_phase(channel, deepcopy(phase))
                    timestamp = now()
                if state['state'] == 'resting':
                    if timestamp >= deadline_for(state):
                        finish(state, timestamp, {'state': 'completed', 'stop_reason': 'rest_elapsed'})
                    continue
                if timestamp >= deadline_for(state):
                    raise TimeoutError(f'Channel {channel} phase deadline reached.')
                result = backend.poll_phase(channel)
                timestamp = now()
                if timestamp >= deadline_for(state):
                    raise TimeoutError(f'Channel {channel} phase deadline reached during polling.')
                if result is None:
                    continue
                phase = state['current_phase']
                reason = 'cv_current_termination' if phase['type'] == 'charge_cc_cv' else 'voltage_min'
                if not isinstance(result, dict) or result.get('state') != 'completed' or result.get('stop_reason') != reason:
                    raise RuntimeError(f'Channel {channel} returned an invalid endpoint.')
                finish(state, timestamp, result)
            if any((state['state'] != 'completed' for state in states.values())):
                timestamp = now()
                remaining = [deadline_for(state) - timestamp for state in states.values() if state['state'] in ('active', 'resting')]
                duration = min([interval] + remaining)
                sleep(max(0.0, duration))
    except BaseException as error:
        primary = error
    finally:
        try:
            if backend.shutdown_all() is not True:
                raise RuntimeError('Final stop-both was not verified.')
            report['final_stop_verified'] = True
        except BaseException as error:
            report['final_stop_verified'] = False
            report['cleanup_errors'].append({'type': type(error).__name__, 'message': str(error)})
            if primary is None:
                primary = error
            else:
                primary.add_note(f'Final stop-both failed: {error}')
        if callable(getattr(backend, 'abort_unfinished', None)):
            try:
                backend.abort_unfinished(primary, report['final_stop_verified'])
            except BaseException as error:
                report['cleanup_errors'].append({'type': type(error).__name__, 'message': str(error)})
                if primary is None:
                    primary = error
                else:
                    primary.add_note(f'Interrupted-phase export failed: {error}')
        try:
            backend.close()
            report['close_succeeded'] = True
        except BaseException as error:
            report['close_succeeded'] = False
            report['cleanup_errors'].append({'type': type(error).__name__, 'message': str(error)})
            if primary is None:
                primary = error
            else:
                primary.add_note(f'Session close failed: {error}')
    for state in states.values():
        if state['state'] != 'completed':
            state['state'] = 'aborted'
        report['channels'].append({'channel': state['channel'], 'specimen_id': state['specimen_id'], 'state': state['state'], 'outcomes': deepcopy(state['outcomes']), 'interrupted_phase': deepcopy(state['current_phase'])})
    report['events'] = deepcopy(backend.events)
    if primary is not None:
        report['state'] = 'raised'
        report['error'] = {'type': type(primary).__name__, 'message': str(primary)}
        primary.dual_run_report = report
        raise primary
    report['state'] = 'completed'
    return report


def run_simulation(
    plan, backend, *, clock, sleep, poll_interval_s=0.05
):
    if not isinstance(backend, SimulatedInstrument):
        raise TypeError(
            "Only SimulatedInstrument is supported; "
            "no live backend exists."
        )
    return _run_backend(
        plan, backend,
        clock=clock, sleep=sleep,
        poll_interval_s=poll_interval_s,
        simulation_only=True,
    )

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m smu dual-simulate",
        description=(
            "SIMULATION ONLY: "
            "no SMU connection or physical outputs."
        ),
    )
    parser.add_argument(
        "--config",
        action="append",
        required=True,
    )
    parser.add_argument(
        "--phase-duration-s",
        type=float,
        default=1.0,
    )
    parser.add_argument(
        "--fail-channel",
        type=int,
        choices=(1, 2),
    )
    arguments = parser.parse_args(argv)
    if len(arguments.config) != 2:
        parser.error("Supply --config exactly twice.")
    try:
        plan = build_dual_plan([
            load_config(path)["config"]
            for path in arguments.config
        ])
        clock = VirtualClock()
        backend = SimulatedInstrument(
            clock,
            phase_duration_s=arguments.phase_duration_s,
            fail_channel=arguments.fail_channel,
        )
        report = run_simulation(
            plan,
            backend,
            clock=clock,
            sleep=clock.sleep,
        )
    except BaseException as error:
        if isinstance(error, SystemExit):
            raise
        report = getattr(error, "dual_run_report", {
            "state": "raised",
            "simulation_only": True,
            "hardware_execution_enabled": False,
            "error": {
                "type": type(error).__name__,
                "message": str(error),
            },
        })
        print(
            json.dumps(report, indent=2, allow_nan=False),
            file=sys.stderr,
        )
        return (
            130 if isinstance(error, KeyboardInterrupt)
            else 1
        )
    print(json.dumps(
        report,
        indent=2,
        allow_nan=False,
    ))
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
