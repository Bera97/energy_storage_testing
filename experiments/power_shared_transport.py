"""INTERNAL PHYSICAL-CAPABLE borrowed CP channels over one already-connected session.

No factory/connect/CLI/coordinator/recording API. Explicit authorized context owns
all cleanup; endpoints never globally abort, reformat, reset time, shut down or
close. Do NOT use this on a DUT as a qualification shortcut. No hardware timing,
source/sink envelope, accuracy, electrical isolation or native dual replay is qualified.
"""
from functools import wraps
import json
import math
from threading import Lock, get_ident
from acquisition.spot_acquisition import SpotAcquisition, MeasurementStatusError
from drivers.scpi import parse_scpi_bool
from experiments.constant_power import PowerLimits, _number
from experiments.dual_instrument_io import DualInstrumentSession
from experiments.dual_preconditioning import build_dual_plan
from experiments.power_adapter import PowerFrame
from experiments.power_planning import build_dual_constant_power_sweep_plan

_CLAIM_LOCK = Lock()
_MARKER = "_shared_power_transport_owner"
CLOCK_AUTO = ":SYST:TIME:TIM:COUN:RES:AUTO"


class SharedPowerTransportError(RuntimeError):
    pass


def _require(condition, message):
    if not condition:
        raise SharedPowerTransportError(message)


def _copy(value):
    return json.loads(json.dumps(value, allow_nan=False))


def _same(a, b, message):
    _require(json.dumps(a, sort_keys=True, allow_nan=False) == json.dumps(b, sort_keys=True, allow_nan=False), message)


def _endpoint_call(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        self.owner._thread()  # A rejected foreign thread must not perform cleanup I/O.
        self._fault_frame = self._fault_bracket = None
        try:
            self.owner._usable()
            _require(self.owner._channels.get(self.channel) is self and not self.stopped, "Borrowed channel lease is terminal/stale.")
            return self.owner._scoped(self.channel, lambda: method(self, *args, **kwargs))
        except BaseException as error:
            if self._fault_frame is not None:
                error.power_frame = self._fault_frame
                error.power_read_bracket_s = self._fault_bracket
            self.owner._fail(error)
            raise
    return wrapped


class SharedPowerSession:
    """One-use controlling-thread owner. Enter transfers shutdown/close responsibility.

    Constructor/pre-enter validation does no driver I/O. Entry performs physical
    configuration and may fail/clean up. Authorization defaults OFF. Subclasses,
    injected drivers, clocks and direct session access are trusted, not a sandbox.
    Do not use the session elsewhere while this owner is active.
    """
    simulation_only = False

    def __init__(self, documents, *, session, clock, output_authorized=False):
        _require(type(output_authorized) is bool and callable(clock), "Boolean authorization and explicit clock required.")
        _require(isinstance(session, DualInstrumentSession), "An already-connected DualInstrumentSession is required.")
        self.thread = get_ident()
        _require(session.owner == self.thread and session.connected, "Session must already be connected on this controlling thread.")
        plan = build_dual_constant_power_sweep_plan(_copy(documents))
        expected_base = build_dual_plan([p["initial_preparation"]["configuration"] for p in plan["channels"]])
        _same(session.plan, expected_base, "Session plan differs from canonical CP preparation profiles.")
        serial = plan["channels"][0]["execution_settings"]["expected_serial"]
        _require(isinstance(serial, str) and bool(serial.strip()) and not serial.upper().startswith("REPLACE"), "Concrete expected serial required.")
        _require(session.expected_serial == serial, "Session serial policy differs.")
        for preview in plan["channels"]:
            specimen = preview["specimen_id"]
            _require(isinstance(specimen, str) and bool(specimen.strip()) and not specimen.upper().startswith("REPLACE"),
                     "Concrete specimen identities required.")
        smu = session.smu
        parts = smu.identity.split(",") if isinstance(smu.identity, str) else []
        _require(len(parts) == 4 and all(p.strip() for p in parts) and parts[1].strip().upper() == "B2902B"
                 and parts[2].strip() == serial and session.identity == smu.identity, "Cached model/serial identity differs.")
        _require(isinstance(plan["resource"], str) and "REPLACE" not in plan["resource"].upper()
                 and getattr(smu, "resource_address", None) == plan["resource"], "Concrete matching resource required.")
        _require(getattr(smu, _MARKER, None) is None, "Driver already claimed by a shared CP owner.")
        self.session, self._smu, self.clock = session, smu, clock
        self.plan, self._base_plan = _copy(plan), _copy(expected_base)
        self._serial, self._identity, self._resource = serial, smu.identity, plan["resource"]
        self.output_authorized = output_authorized
        self.claimed = self.ready = self.closed = self.failed = False
        self._last_time = self._failure = None
        self._channels, self._spots = {}, {}
        self._profiles = {p["channel"]: _copy(p) for p in plan["channels"]}
        self.startup_evidence = None
        self.cleanup_report = None

    def _thread(self):
        _require(get_ident() == self.thread, "Shared CP session requires one controlling thread.")

    def _time(self):
        value = _number(self.clock(), "host clock", zero=True)
        _require(self._last_time is None or value >= self._last_time, "Host clock moved backwards.")
        self._last_time = value
        return value

    def _checked(self, operation, *args):
        result = operation(*args)
        self._smu.check_errors()
        return result

    def _selected(self, channel):
        _require(type(channel) is int and channel in (1, 2), "Integer channel 1 or 2 required.")
        return self._smu.chan1 if channel == 1 else self._smu.chan2

    def _usable(self):
        self._thread()
        _require(self.claimed and self.ready and not self.closed and not self.failed, "Shared CP owner is not available.")
        _require(self.output_authorized is True, "Shared CP output authorization is disabled.")
        _require(self.session.owner == self.thread and self.session.smu is self._smu and self._smu.is_connected is True
                 and getattr(self._smu, _MARKER, None) is self, "Shared CP session ownership changed.")
        _require(self._smu.identity == self._identity and self.session.identity == self._identity
                 and self.session.expected_serial == self._serial and self._smu.resource_address == self._resource,
                 "Shared CP identity/resource changed.")

    def _shared_readbacks(self):
        query = lambda command: self._checked(self._smu._query, command)
        _require("".join(query(":SYST:GRO:DEF?").split()) in ("(@1),(@2)", "(@2),(@1)"), "Independent trigger groups were lost.")
        _require(query(":FORM:DATA?") == "ASC", "Shared data format changed.")
        _require(tuple(p.strip().upper() for p in query(":FORM:ELEM:SENS?").split(",")) == SpotAcquisition.ELEMENTS,
                 "Shared result elements changed.")
        _require(parse_scpi_bool(query(CLOCK_AUTO+"?"), CLOCK_AUTO+"?") is False, "Shared timer auto-reset changed.")

    def _fingerprint(self, channel):
        selected = self._selected(channel)
        state = self._checked(selected.get_output_state)
        automatic = self._checked(selected.get_auto_output_on)
        wire = self._checked(selected.get_four_wire)
        _require(type(state) is bool and type(automatic) is bool and type(wire) is bool, "Malformed peer boolean readback.")
        _require(automatic is False, "Peer automatic output-on must remain OFF.")
        mode = self._checked(selected.get_source_mode)
        _require(mode in ("CURR", "VOLT"), "Unsupported peer source mode.")
        level = self._checked(selected.get_current_setpoint if mode == "CURR" else selected.get_voltage_setpoint)
        compliance = self._checked(selected.get_voltage_limit if mode == "CURR" else selected.get_current_limit)
        level = _number(level, "peer level", signed=True, zero=True)
        compliance = _number(compliance, "peer compliance")
        shape = self._checked(self._smu._query, f":SOUR{channel}:FUNC:SHAP?")
        fixed = self._checked(self._smu._query, f":SOUR{channel}:{mode}:MODE?")
        off = self._checked(selected.get_output_off_mode)
        nplc = _number(self._checked(selected.get_nplc), "peer NPLC")
        if state:
            limits = self._profiles[channel]["execution_settings"]
            _require(shape == "DC" and fixed == "FIX" and off == "HIZ", "Active peer is not DC/FIX/HIZ.")
            voltage, current = (compliance, abs(level)) if mode == "CURR" else (abs(level), compliance)
            _require(voltage <= limits["maximum_voltage_v"] and current <= limits["maximum_current_a"], "Peer exceeds declared execution ceilings.")
        return dict(output_on=state, automatic=automatic, four_wire=wire, mode=mode, level=level,
                    compliance=compliance, shape=shape, fixed=fixed, off_mode=off, nplc=nplc)

    def _scoped(self, channel, operation):
        self._usable()
        self._shared_readbacks()
        before = self._fingerprint(3-channel)
        result = operation()
        self._shared_readbacks()
        _same(self._fingerprint(3-channel), before, "Channel-local operation changed peer configuration/output.")
        return result

    def _stop_selected(self, channel):
        selected = self._selected(channel)
        failures = []
        for operation, args in ((self._smu._write, (f":ABOR:ALL (@{channel})",)),
                (selected.set_auto_output_on, (False,)), (selected.disable_output, ()), (self._smu.check_errors, ())):
            try:
                operation(*args)
            except BaseException as error:
                failures.append(error)
        for operation in (selected.get_output_state, selected.get_auto_output_on):
            try:
                _require(self._checked(operation) is False, "Selected output/automatic output-on did not report OFF.")
            except BaseException as error:
                failures.append(error)
        if failures:
            first = failures[0]
            for error in failures[1:]:
                first.add_note(f"Additional selected stop failure: {type(error).__name__}: {error}")
            raise first
        return {"channel": channel, "output_reported_off": True, "automatic_output_on_reported_off": True}

    def __enter__(self):
        self._thread()
        _require(self.output_authorized is True and not self.claimed and not self.closed, "Fresh explicit shared CP authorization required.")
        _require(self.session.smu is self._smu and self.session.owner == self.thread and self.session.connected, "Session changed before ownership transfer.")
        _same(self.session.plan, self._base_plan, "Session plan changed before ownership transfer.")
        _require(self.session.expected_serial == self._serial and self.session.identity == self._identity
                 and self._smu.identity == self._identity and self._smu.resource_address == self._resource,
                 "Identity/resource changed before ownership transfer.")
        self._time()  # Reject an invalid initial clock before ownership/I/O.
        with _CLAIM_LOCK:
            _require(getattr(self._smu, _MARKER, None) is None, "Driver already claimed by a shared CP owner.")
            setattr(self._smu, _MARKER, self)
            self.claimed = True
        try:
            for channel in (1, 2):
                _require(self._checked(self._selected(channel).get_output_state) is False, "Both outputs must be OFF at shared CP entry.")
            for channel in (1, 2):
                self._stop_selected(channel)
                self._checked(self._selected(channel).set_output_off_mode, "HIZ")
            self.session.configure_shared()  # Existing OFF-only grouping/format/NPLC/clock setup.
            self._shared_readbacks()
            for channel in (1, 2):
                selected = self._selected(channel)
                _require(self._checked(selected.get_output_state) is False and self._checked(selected.get_auto_output_on) is False
                         and self._checked(selected.get_output_off_mode) == "HIZ", "Shared setup did not leave both channels OFF/auto-OFF/HIZ.")
            self._spots = {c: SpotAcquisition(self._smu, c) for c in (1, 2)}
            self.startup_evidence = {"identity": self._identity, "resource_address": self._resource,
                "trigger_groups": _copy(self.session.trigger_group_evidence), "trace_clock": _copy(self.session.trace_clock_evidence),
                "outputs_reported_off": True, "automatic_outputs_reported_off": True, "physical_qualification": False}
            self.ready = True
            return self
        except BaseException as error:
            self._fail(error)
            raise

    def borrow(self, channel, limits, *, maximum_probe_current_a):
        self._thread()
        try:
            self._usable()
            self._selected(channel)
            _require(channel not in self._channels, "Channel already has a borrowed CP lease.")
            _require(isinstance(limits, PowerLimits), "Validated PowerLimits required.")
            allowed = [p["controller_limits"] for p in self._profiles[channel]["conditions"]]
            _require(limits.to_dict() in allowed, "CP limits are not a canonical condition for this channel.")
            ceilings = self._profiles[channel]["execution_settings"]
            _require(limits.maximum_voltage_v <= ceilings["maximum_voltage_v"] and limits.maximum_current_a <= ceilings["maximum_current_a"],
                     "Borrowed phase exceeds execution ceilings.")
            probe = _number(maximum_probe_current_a, "probe ceiling")
            _require(probe <= limits.maximum_current_a, "Probe exceeds phase current ceiling.")
            endpoint = BorrowedPowerChannel(self, channel, limits, probe)
            self._channels[channel] = endpoint
            return endpoint
        except BaseException as error:
            self._fail(error)
            raise

    def borrow_native(self, channel, request, directory):
        """Borrow one canonical recorded CC/CV or rest phase; never a CP phase."""
        self._thread()
        try:
            self._usable()
            self._selected(channel)
            _require(channel not in self._channels, "Channel already has a borrowed lease.")
            from experiments.power_shared_phases import BorrowedNativePhase
            endpoint = BorrowedNativePhase(self, channel, request, directory)
            self._channels[channel] = endpoint
            return endpoint
        except BaseException as error:
            self._fail(error)
            raise

    def _cleanup(self, primary=None):
        if self.closed:
            return self._failure
        self.closed, self.ready = True, False
        report = {"shutdown_returned": False, "channels": [], "session_close_returned": False, "errors": []}
        first = primary
        def attempt(label, operation, *args):
            nonlocal first
            try:
                return True, operation(*args)
            except BaseException as error:
                report["errors"].append({"operation": label, "type": type(error).__name__, "message": str(error)})
                if first is None:
                    first = error
                elif error is not first:
                    first.add_note(f"Shared CP cleanup {label}: {type(error).__name__}: {error}")
                return False, None
        report["shutdown_returned"], _ = attempt("shutdown_all", self._smu.shutdown)
        for channel in (1, 2):
            stopped, _ = attempt(f"stop_channel_{channel}", self._stop_selected, channel)
            evidence = {"channel": channel, "stop_returned": stopped}
            for key, method in (("output_state", self._selected(channel).get_output_state),
                                ("automatic_output_on", self._selected(channel).get_auto_output_on)):
                success, value = attempt(f"channel_{channel}_{key}", self._checked, method)
                evidence[key] = value if type(value) is bool else None
                if success and value is not False:
                    def invalid():
                        raise SharedPowerTransportError(f"Channel {channel} {key} was not OFF: {value!r}")
                    attempt(f"channel_{channel}_{key}_verification", invalid)
            report["channels"].append(evidence)
        report["session_close_returned"], _ = attempt("close_session", self._smu.close)
        report["outputs_reported_off"] = all(c["output_state"] is False for c in report["channels"])
        # File-only native finalizers run AFTER stop-both and the close attempt.
        # They must not re-enter normal transport or issue any driver command.
        for channel, endpoint in tuple(self._channels.items()):
            finalize = getattr(endpoint, "_after_owner_stop", None)
            if finalize is not None:
                attempt(f"finalize_native_channel_{channel}", finalize, first, _copy(report))
        self.cleanup_report, self._failure = _copy(report), first
        self.failed = first is not None
        for endpoint in self._channels.values():
            endpoint.stopped = True
        if first is not None:
            first.shared_power_cleanup_report = _copy(report)
        return first

    def _fail(self, error):
        if not self.claimed:
            return  # Unclaimed/pre-enter rejection must leave caller ownership intact.
        self.failed = True
        if self.closed:
            if self._failure is None:
                self._failure = error
            if self.cleanup_report is not None:
                error.shared_power_cleanup_report = _copy(self.cleanup_report)
            return
        self._cleanup(error)

    def close(self):
        self._thread()
        _require(self.claimed, "Unclaimed session remains the caller's responsibility.")
        failure = self._cleanup()
        if failure is not None:
            raise failure
        return _copy(self.cleanup_report)

    def __exit__(self, kind, error, traceback):
        self._thread()
        failure = self._cleanup(error)
        if error is None and failure is not None:
            raise failure
        return False


class BorrowedPowerChannel:
    """Obtain ONLY via SharedPowerSession.borrow(). No session close/global stop API."""
    simulation_only = False

    def __init__(self, owner, channel, limits, probe):
        self.owner, self.channel, self.limits, self.probe_ceiling = owner, channel, limits, probe
        self.zero_ok = hasattr(limits, "profile_kind")
        self.polarity_tolerance_a = limits.zero_current_tolerance_a if self.zero_ok else 0.0
        self.prepared = self.enabled = self.stopped = self.power_command_applied = False
        self._fault_frame = self._fault_bracket = None

    @property
    def selected(self):
        return self.owner._selected(self.channel)

    def _command_in_range(self, value):
        return -self.limits.maximum_current_a <= value < 0 or (self.zero_ok and value == 0)

    def _configuration(self, active):
        checked = self.owner._checked
        _require(checked(self.selected.get_output_state) is active and checked(self.selected.get_auto_output_on) is False,
                 "Selected output/automatic output-on changed.")
        _require(checked(self.selected.get_source_mode) == "CURR" and checked(self.selected.get_output_off_mode) == "HIZ",
                 "Selected source/off mode changed.")
        for command, expected in ((f":SOUR{self.channel}:FUNC:SHAP?", "DC"), (f":SOUR{self.channel}:CURR:MODE?", "FIX")):
            _require(checked(self.owner._smu._query, command) == expected, "Selected DC/FIX mode changed.")
        config = self.owner._profiles[self.channel]["configuration"]["instrument"]
        for actual, expected, name in ((checked(self.selected.get_voltage_limit), self.limits.maximum_voltage_v, "voltage compliance"),
                                      (checked(self.selected.get_nplc), float(config["nplc"]), "NPLC")):
            _require(math.isclose(_number(actual, name), expected, rel_tol=1e-9, abs_tol=1e-12), "Selected "+name+" differs.")
        _require(checked(self.selected.get_four_wire) is config["four_wire"], "Selected wiring differs.")
        level = _number(checked(self.selected.get_current_setpoint), "selected current", signed=True, zero=True)
        _require(self._command_in_range(level) if self.power_command_applied else level == 0,
                 "Selected current is not the permitted zero-probe/negative-power setpoint.")

    @_endpoint_call
    def prepare(self):
        _require(not self.prepared, "Borrowed CP preparation is single-use.")
        self.owner._stop_selected(self.channel)
        checked = self.owner._checked
        checked(self.selected.set_output_off_mode, "HIZ")
        checked(self.selected.configure_fixed_dc, "CURR")
        checked(self.selected.set_current, 0.0)
        checked(self.selected.set_voltage_limit, self.limits.maximum_voltage_v)
        checked(self.selected.set_four_wire, self.owner._profiles[self.channel]["configuration"]["instrument"]["four_wire"])
        # Spot.configure would rewrite global format and requires BOTH channels OFF.
        self._configuration(False)
        self.prepared = True
        return dict(channel=self.channel, selected_output_off=True, current_source=True,
                    auto_output_on=False, current_setpoint_a=0.0, voltage_compliance_v=self.limits.maximum_voltage_v,
                    identity=self.owner._identity)

    @_endpoint_call
    def set_output(self, enabled):
        _require(self.prepared and not self.enabled and enabled is True, "Explicit one-time probe activation required.")
        self._configuration(False)
        self.owner._checked(self.owner._smu._write, f":ABOR:ALL (@{self.channel})")
        self.owner._checked(self.owner._smu._write, f":OUTP{self.channel} ON")
        self._configuration(True)
        self.enabled = True

    @_endpoint_call
    def read_fresh(self):
        _require(self.prepared and self.enabled, "Borrowed CP probe is not active.")
        self._configuration(True)
        started = self.owner._time()
        try:
            reading = self.owner._spots[self.channel].read()
        except MeasurementStatusError as error:
            reading = error.reading
        frame = PowerFrame(reading.channel, reading.voltage_v, reading.current_a, reading.timestamp_s, reading.status)
        self._fault_frame = frame
        finished = self.owner._time()
        self._fault_bracket = (started, finished)
        sample = frame.power_sample(started)
        _require(frame.channel == self.channel and not sample.has_fault and not sample.in_compliance and sample.current_source
                 and self.limits.minimum_voltage_v <= frame.voltage_v <= self.limits.maximum_voltage_v
                 and abs(frame.current_a) <= (self.limits.maximum_current_a if self.power_command_applied else self.probe_ceiling)
                 and (not self.power_command_applied or frame.current_a <= self.polarity_tolerance_a), "Borrowed CP feedback is invalid; paired frame retained on error.")
        return frame

    @_endpoint_call
    def set_current(self, current_a):
        _require(self.prepared and self.enabled, "Borrowed CP probe is not active.")
        value = _number(current_a, "CP command", signed=True)
        _require(self._command_in_range(value), "CP command must be negative and within phase ceiling.")
        self._configuration(True)
        self.owner._checked(self.selected.set_current, value)
        self.power_command_applied = True

    @_endpoint_call
    def get_current_setpoint(self):
        _require(self.enabled and self.power_command_applied, "No CP command has been applied.")
        self._configuration(True)
        value = _number(self.owner._checked(self.selected.get_current_setpoint), "CP readback", signed=True)
        _require(self._command_in_range(value), "CP readback is outside the permitted negative range.")
        return value

    @_endpoint_call
    def get_output_state(self):
        _require(self.prepared, "Prepare borrowed CP first.")
        result = self.owner._checked(self.selected.get_output_state)
        _require(type(result) is bool, "Malformed selected output state.")
        _require(result is self.enabled, "Unexpected selected output transition.")
        return result

    @_endpoint_call
    def stop(self):
        result = self.owner._stop_selected(self.channel)
        self.enabled, self.stopped = False, True
        del self.owner._channels[self.channel]
        return result
