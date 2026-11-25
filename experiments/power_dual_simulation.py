"""Explicit virtual instrument for dual CP scheduling tests, NEVER a real backend.

CC/rest endpoints are synthetic; no native monitor/recorder is exercised here.
Subclass/method injections are trusted tests, not a hardware security boundary.
"""
from threading import get_ident
from experiments.constant_power import PowerSample, _number
from experiments.charge_controller import charge_stop_reason
from experiments.power_sweep_contract import require, snapshot


class DualPowerVirtualClock:
    def __init__(self):
        self.time = 0.0

    def __call__(self):
        return self.time

    def sleep(self, duration):
        self.time += _number(duration, "virtual sleep", zero=True)


def synthetic_endpoint(phase):
    if phase["type"] == "rest":
        return "rest_elapsed"
    return charge_stop_reason(phase) if phase["type"] == "charge_cc_cv" else "voltage_min"


class SimulatedDualPowerInstrument:
    """One virtual session; local stage operations never stop the peer."""
    simulation_only = True

    def __init__(self, *, call_latency_s=0.0, active_duration_s=None, cutoff_after_samples=None):
        self.owner = get_ident()
        self.clock = DualPowerVirtualClock()
        self.call_latency_s = _number(call_latency_s, "call latency", zero=True)
        self.active_duration_s = {1: 0.15, 2: 0.3} if active_duration_s is None else dict(active_duration_s)
        self.cutoff_after_samples = {1: 4, 2: 6} if cutoff_after_samples is None else dict(cutoff_after_samples)
        require(set(self.active_duration_s) == {1, 2} and set(self.cutoff_after_samples) == {1, 2}
                and all(type(k) is int for k in self.active_duration_s) and all(type(k) is int for k in self.cutoff_after_samples),
                "Both integer virtual channels required.")
        for channel in (1, 2):
            self.active_duration_s[channel] = _number(self.active_duration_s[channel], "synthetic active duration")
            count = self.cutoff_after_samples[channel]
            require(type(count) is int and count >= 1, "Synthetic cutoff counts must be positive integers.")
        self.claimed = self.connected = self.closed = self.configured = False
        self.active = {}
        self.output_states = {1: False, 2: False}
        self.events = []

    def _event(self, action, channel=None, **details):
        # Cleanup must remain usable even if a test deliberately breaks the clock.
        try:
            time = _number(self.clock(), "virtual event time", zero=True)
        except (ValueError, TypeError, OverflowError):
            time = None
        self.events.append({"action": action, "channel": channel, "time_s": time, **snapshot(details)})

    def _work(self, action, channel):
        require(self.connected and self.configured and not self.closed, "Virtual session is not available.")
        self.clock.sleep(self.call_latency_s)
        self._event(action, channel)

    def open_simulation(self, plan):
        require(not self.connected and not self.closed, "Virtual session is single-use.")
        self.connected = True
        self._event("open_simulation", resource_label=plan["resource"])

    def configure_shared(self):
        require(self.connected and not self.configured and not any(self.output_states.values()), "Virtual shared setup requires both OFF.")
        self.configured = True
        self._event("configure_shared")

    def prepare_stage(self, channel, request):
        self._work("prepare_stage", channel)
        require(channel not in self.active and self.output_states[channel] is False, "Selected virtual channel must be idle and OFF.")
        self.active[channel] = {"request": snapshot(request), "setpoint_a": 0.0, "read_count": 0, "started_s": None}
        return {"channel": channel, "stage_id": request["stage_id"], "selected_output_off": True,
                "current_source": True, "current_setpoint_a": 0.0, "simulation_only": True}

    def activate_stage(self, channel):
        self._work("activate_stage", channel)
        item = self.active[channel]
        phase = item["request"]["phase"]
        on = phase["type"] != "rest" or phase.get("rest_mode") == "zero_current_logging"
        self.output_states[channel] = on
        item["started_s"] = self.clock()
        self._event("stage_started", channel, stage_id=item["request"]["stage_id"], type=phase["type"], output_on=on)
        return on

    def read_power(self, channel):
        started = self.clock()
        self._work("read_power", channel)
        item = self.active[channel]
        phase = item["request"]["phase"]
        require(phase["type"] == "discharge_cp" and self.output_states[channel] is True, "Virtual CP stage is not active.")
        limits = phase["controller_limits"]
        item["read_count"] += 1
        voltage = (limits["cutoff_voltage_v"] if item["read_count"] >= self.cutoff_after_samples[channel] else
                   limits["cutoff_voltage_v"] + 0.5*(limits["maximum_voltage_v"]-limits["cutoff_voltage_v"]))
        return {"channel": channel, "stage_id": item["request"]["stage_id"], "simulation_only": True,
                "sample": PowerSample(started, voltage, item["setpoint_a"])}

    def set_current(self, channel, value):
        self._work("set_current", channel)
        self.active[channel]["setpoint_a"] = _number(value, "synthetic current", signed=True)

    def read_current(self, channel):
        self._work("read_current", channel)
        return self.active[channel]["setpoint_a"]

    def poll_stage(self, channel):
        self._work("poll_stage", channel)
        item = self.active[channel]
        request = item["request"]
        phase = request["phase"]
        require(phase["type"] != "discharge_cp", "CP must use the decision controller, not synthetic endpoints.")
        duration = phase["duration_s"] if phase["type"] == "rest" else self.active_duration_s[channel]
        if self.clock() - item["started_s"] < duration:
            return None
        return {"channel": channel, "stage_id": request["stage_id"], "state": "completed",
                "stop_reason": synthetic_endpoint(phase), "simulation_only": True}

    def stop_channel(self, channel):
        self.output_states[channel] = False
        self.active.pop(channel, None)
        self._event("stop_channel", channel, peer_output_on=self.output_states[3-channel])
        return True

    def output_state(self, channel):
        self._event("output_state", channel)
        return self.output_states[channel]

    def finalize_stage(self, channel, stage):
        self._work("finalize_stage", channel)
        require(self.output_states[channel] is False, "Virtual completion requires selected output OFF.")
        self._event("stage_finalized", channel, stage_id=stage["request"]["stage_id"])
        return True

    def shutdown_all(self):
        self.output_states = {1: False, 2: False}
        self.active.clear()
        self._event("shutdown_all")
        return True

    def close_simulation(self):
        self.closed, self.connected = True, False
        self._event("close_simulation")
