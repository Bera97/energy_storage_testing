"""Dormant single-phase B2902B transport over an ALREADY CONNECTED driver.

No connection factory, CLI dispatch, hardware-run recorder or VISA import.
prepare() changes configuration. set_output(True) can activate a connected DUT
only with explicit output_authorized=True. Neither is a qualification procedure.
The caller must own an outer shutdown-all/close boundary on EVERY exit.
"""
from functools import wraps
import math
from experiments.constant_power import PowerLimits, _number


class PowerTransportError(RuntimeError):
    pass


def _terminal(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except BaseException:
            self.failed = True
            raise
    return wrapped


class B2902BPowerBackend:
    """One owned, initially outputs-OFF session; NOT a shared dual backend.

    Real transport is intentionally rejected by run_simulated_power_phase().
    Cleanup methods stay callable after failures. close_session() never implies
    output shutdown, and its return is not proof of underlying VISA closure.
    Factory injections are trusted unit-test dependencies, not a safety boundary.
    """
    simulation_only = False

    def __init__(self, smu, *, channel, expected_serial, four_wire, nplc,
                 maximum_voltage_v, maximum_current_a, output_authorized=False,
                 spot_factory=None, enable_output=None, status_error_type=None):
        if type(channel) is not int or channel not in (1, 2):
            raise ValueError("channel must be 1 or 2.")
        if (not isinstance(expected_serial, str) or not expected_serial.strip()
                or expected_serial.strip().upper().startswith("REPLACE")):
            raise ValueError("A concrete expected instrument serial is required.")
        if type(four_wire) is not bool or type(output_authorized) is not bool:
            raise ValueError("Wiring/output authorization flags must be actual booleans.")
        for name, operation in (("spot_factory", spot_factory), ("enable_output", enable_output)):
            if operation is not None and not callable(operation):
                raise ValueError(f"{name} must be callable.")
        if status_error_type is not None and (
            spot_factory is None or not isinstance(status_error_type, type)
            or not issubclass(status_error_type, Exception)
        ):
            raise ValueError("A test status exception requires an injected spot factory.")
        self.smu = smu
        self.channel = channel
        self.expected_serial = expected_serial.strip()
        self.four_wire = four_wire
        self.nplc = _number(nplc, "nplc")
        self.maximum_voltage_v = _number(maximum_voltage_v, "maximum_voltage_v")
        self.maximum_current_a = _number(maximum_current_a, "maximum_current_a")
        self.output_authorized = output_authorized
        self._spot_factory = spot_factory
        self._enable_output = enable_output
        self._status_error_type = status_error_type or ()
        self._spot = None
        self.limits = None
        self.zero_ok = False
        self.polarity_tolerance_a = 0.0
        self.prepared = False
        self.enabled = False
        self.power_command_applied = False
        self.failed = False
        self.stopped = False
        self.closed = False

    @property
    def selected(self):
        return self.smu.chan1 if self.channel == 1 else self.smu.chan2

    def _usable(self, *, prepared=False, active=False):
        if self.failed or self.stopped or self.closed:
            raise PowerTransportError("Transport is terminal; no automatic retry/resume.")
        if prepared and not self.prepared:
            raise PowerTransportError("Transport has not completed preparation.")
        if active and not self.enabled:
            raise PowerTransportError("The explicit zero-current probe is not active.")

    def _checked(self, operation, *args):
        result = operation(*args)
        self.smu.check_errors()
        return result

    def _both_off(self):
        for item in (self.smu.chan1, self.smu.chan2):
            if self._checked(item.get_output_state) is not False:
                raise PowerTransportError("Both outputs must explicitly report OFF.")

    @staticmethod
    def _same(actual, expected, name):
        actual = _number(actual, name, signed=True)
        if not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-12):
            raise PowerTransportError(f"{name} readback mismatch.")
        return actual

    def _active_readbacks(self):
        if (self._checked(self.selected.get_output_state) is not True
                or self._checked(self.selected.get_auto_output_on) is not False
                or self._checked(self.selected.get_source_mode) != "CURR"):
            raise PowerTransportError("Active output/source/auto-output readback mismatch.")
        self._same(self._checked(self.selected.get_voltage_limit),
                   self.limits.maximum_voltage_v, "voltage compliance")

    @_terminal
    def prepare(self, limits):
        self._usable()
        if self.prepared:
            raise PowerTransportError("Preparation is single-use.")
        if not isinstance(limits, PowerLimits):
            raise ValueError("Validated PowerLimits required.")
        if limits.maximum_voltage_v > self.maximum_voltage_v or limits.maximum_current_a > self.maximum_current_a:
            raise ValueError("Phase limits exceed supplied execution ceilings.")
        if self.smu.is_connected is not True:
            raise PowerTransportError("An already-connected, exclusively owned driver is required.")
        identity = self.smu.identity
        if not isinstance(identity, str):
            raise PowerTransportError("Instrument identity must be text.")
        parts = [part.strip() for part in identity.split(",")]
        if (len(parts) != 4 or not all(parts) or parts[1].upper() != "B2902B"
                or parts[2] != self.expected_serial):
            raise PowerTransportError("Instrument model/serial identity mismatch.")
        self.smu.check_errors()
        self._both_off()
        self._checked(self.smu.abort)
        for item in (self.smu.chan1, self.smu.chan2):
            self._checked(item.set_auto_output_on, False)
        self._checked(self.selected.set_output_off_mode, "HIZ")
        self._checked(self.selected.configure_fixed_dc, "CURR")
        self._checked(self.selected.set_current, 0.0)
        self._checked(self.selected.set_voltage_limit, limits.maximum_voltage_v)
        self._checked(self.selected.set_four_wire, self.four_wire)
        if self._spot_factory is None:
            from acquisition.spot_acquisition import SpotAcquisition, MeasurementStatusError
            self._spot = SpotAcquisition(self.smu, self.channel)
            self._status_error_type = MeasurementStatusError
        else:
            self._spot = self._spot_factory(self.smu, self.channel)
        self._checked(self._spot.configure, self.nplc)
        for command, expected in (
            (f":SOUR{self.channel}:FUNC:SHAP?", "DC"),
            (f":SOUR{self.channel}:CURR:MODE?", "FIX"),
        ):
            if self._checked(self.smu._query, command) != expected:
                raise PowerTransportError("DC/FIX source readback mismatch.")
        mode = self._checked(self.selected.get_source_mode)
        level = self._checked(self.selected.get_current_setpoint)
        compliance = self._checked(self.selected.get_voltage_limit)
        if mode != "CURR" or _number(level, "zero-current readback", signed=True) != 0:
            raise PowerTransportError("Probe must use verified CURR and exactly zero setpoint.")
        self._same(compliance, limits.maximum_voltage_v, "voltage compliance")
        self._same(self._checked(self.selected.get_nplc), self.nplc, "NPLC")
        if (self._checked(self.selected.get_four_wire) is not self.four_wire
                or self._checked(self.selected.get_output_off_mode) != "HIZ"):
            raise PowerTransportError("Wiring/output-off readback mismatch.")
        for item in (self.smu.chan1, self.smu.chan2):
            if self._checked(item.get_auto_output_on) is not False:
                raise PowerTransportError("Automatic output-on did not report OFF.")
        self._both_off()
        self.limits = limits
        self.zero_ok = hasattr(limits, "profile_kind")
        self.polarity_tolerance_a = limits.zero_current_tolerance_a if self.zero_ok else 0.0
        self.prepared = True
        return dict(channel=self.channel, selected_output_off=True, current_source=True,
                    auto_output_on=False, current_setpoint_a=float(level),
                    voltage_compliance_v=float(compliance), identity=identity,
                    four_wire=self.four_wire, nplc=self.nplc)

    @_terminal
    def set_output(self, enabled):
        self._usable(prepared=True)
        if enabled is not True or self.enabled:
            raise PowerTransportError("Only one explicit zero-current probe activation is supported.")
        if self.output_authorized is not True:
            raise PowerTransportError("Output activation is disabled without explicit internal authorization.")
        self._both_off()
        if self._checked(self.selected.get_source_mode) != "CURR":
            raise PowerTransportError("Probe source mode changed after preparation.")
        level = _number(self._checked(self.selected.get_current_setpoint), "probe setpoint", signed=True)
        if level != 0:
            raise PowerTransportError("Probe activation requires an exactly zero current setpoint.")
        self._same(self._checked(self.selected.get_voltage_limit), self.limits.maximum_voltage_v, "voltage compliance")
        enable = self._enable_output
        if enable is None:
            from drivers.dc_output import enable_dc_output
            enable = enable_dc_output
        enable(self.smu, self.channel, max_voltage_v=self.maximum_voltage_v,
               max_current_a=self.maximum_current_a)
        self.smu.check_errors()
        if self._checked(self.selected.get_output_state) is not True:
            raise PowerTransportError("Probe activation did not report output ON.")
        self.enabled = True

    @_terminal
    def get_output_state(self):
        self._usable(prepared=True)
        result = self._checked(self.selected.get_output_state)
        if type(result) is not bool:
            raise PowerTransportError("Output readback must be an actual boolean.")
        return result

    @_terminal
    def read_fresh(self):
        self._usable(prepared=True, active=True)
        self._active_readbacks()
        try:
            reading = self._spot.read()
        except Exception as error:
            if not isinstance(error, self._status_error_type):
                raise
            reading = error.reading
        from experiments.power_adapter import PowerFrame
        frame = PowerFrame(reading.channel, reading.voltage_v, reading.current_a,
                           reading.timestamp_s, reading.status)
        sample = frame.power_sample(0.0)
        if (frame.channel != self.channel or sample.has_fault or sample.in_compliance
                or not sample.current_source
                or not self.limits.minimum_voltage_v <= frame.voltage_v <= self.limits.maximum_voltage_v
                or abs(frame.current_a) > self.limits.maximum_current_a
                or (self.power_command_applied and frame.current_a > self.polarity_tolerance_a)):
            self.failed = True
        return frame

    @_terminal
    def set_current(self, current_a):
        self._usable(prepared=True, active=True)
        value = _number(current_a, "power current command", signed=True)
        if self.zero_ok and value == 0:
            self._active_readbacks()
            self._checked(self.selected.set_current, value)
            self.power_command_applied = True
            return
        if not -self.limits.maximum_current_a <= value < 0:
            raise ValueError("Power commands must be negative and within the phase current ceiling.")
        self._active_readbacks()
        self._checked(self.selected.set_current, value)
        self.power_command_applied = True

    @_terminal
    def get_current_setpoint(self):
        self._usable(prepared=True, active=True)
        self._active_readbacks()
        value = _number(self._checked(self.selected.get_current_setpoint), "current readback", signed=True)
        if self.zero_ok and value == 0:
            return value
        if not -self.limits.maximum_current_a <= value < 0:
            raise PowerTransportError("Power setpoint readback exceeds the permitted negative-current range.")
        return value

    def shutdown_all(self):
        try:
            self.smu.shutdown()
        finally:
            self.stopped = True
            self.enabled = False

    def get_output_states(self):
        states = []
        first = None
        for item in (self.smu.chan1, self.smu.chan2):
            try:
                state = self._checked(item.get_output_state)
                if type(state) is not bool:
                    raise PowerTransportError("Output readbacks must be actual booleans.")
                states.append(state)
            except BaseException as error:
                states.append(None)
                if first is None:
                    first = error
                else:
                    first.add_note(f"Other output readback: {type(error).__name__}: {error}")
        if first is not None:
            raise first
        return tuple(states)

    def close_session(self):
        if self.closed:
            return
        self.closed = True
        self.smu.close()
