"""Concrete transport calls through an injected fake driver; no VISA/network."""
from types import SimpleNamespace
import pytest
from test_constant_power_controller import limits
from experiments.power_smu_backend import B2902BPowerBackend, PowerTransportError


class FakeChannel:
    def __init__(self, smu, number):
        self.smu, self.channel = smu, number
        self.on, self.auto, self.mode, self.off_mode = False, False, "CURR", "HIZ"
        self.current, self.voltage, self.four_wire, self.nplc = 0.0, 3.81, True, 1.0
        self.bad_readbacks = {}

    def get(self, name):
        self.smu.call(f"ch{self.channel}.get_{name}")
        return self.bad_readbacks.get(name, getattr(self, name))

    def set(self, name, value):
        self.smu.call(f"ch{self.channel}.set_{name}")
        setattr(self, name, value)

    def get_output_state(self): return self.get("on")
    def get_auto_output_on(self): return self.get("auto")
    def get_source_mode(self): return self.get("mode")
    def get_output_off_mode(self): return self.get("off_mode")
    def get_current_setpoint(self): return self.get("current")
    def get_voltage_limit(self): return self.get("voltage")
    def get_four_wire(self): return self.get("four_wire")
    def get_nplc(self): return self.get("nplc")
    def set_auto_output_on(self, value): self.set("auto", value)
    def set_output_off_mode(self, value): self.set("off_mode", value)
    def configure_fixed_dc(self, value): self.set("mode", value)
    def set_current(self, value): self.set("current", value)
    def set_voltage_limit(self, value): self.set("voltage", value)
    def set_four_wire(self, value): self.set("four_wire", value)


class FakeSMU:
    def __init__(self):
        self.is_connected = True
        self.identity = "Keysight Technologies,B2902B,TESTSERIAL,1.0"
        self.calls, self.failures = [], {}
        self.chan1, self.chan2 = FakeChannel(self, 1), FakeChannel(self, 2)
        self.responses = {}

    def call(self, name):
        self.calls.append(name)
        if name in self.failures:
            raise self.failures[name]

    def check_errors(self): self.call("check_errors")
    def abort(self): self.call("abort")
    def _query(self, command):
        self.call(command)
        return self.responses.get(command, "DC" if command.endswith("SHAP?") else "FIX")
    def shutdown(self):
        self.call("shutdown")
        self.chan1.on = self.chan2.on = False
    def close(self):
        self.call("close")
        self.is_connected = False


class FakeStatusError(Exception):
    def __init__(self, reading):
        self.reading = reading


class FakeSpot:
    def __init__(self, smu, channel):
        self.smu, self.channel = smu, channel
        self.error = None
        self.status = 1
        self.timestamp = 4.0
        self.read_channel = channel
        self.current = None

    def configure(self, nplc):
        self.smu.call("spot.configure")
        (self.smu.chan1 if self.channel == 1 else self.smu.chan2).nplc = nplc

    def read(self):
        self.smu.call("spot.read")
        selected = self.smu.chan1 if self.channel == 1 else self.smu.chan2
        reading = SimpleNamespace(channel=self.read_channel, voltage_v=3.0,
                                  current_a=selected.current if self.current is None else self.current,
                                  timestamp_s=self.timestamp, status=self.status)
        if self.error == "status":
            raise FakeStatusError(reading)
        if self.error is not None:
            raise self.error
        return reading


def make(*, channel=1, authorized=True, **overrides):
    smu = FakeSMU()
    spot = FakeSpot(smu, channel)
    enabled = []
    def enable(driver, number, **ceilings):
        smu.call("enable_dc_output")
        assert driver is smu and number == channel
        assert ceilings == {"max_voltage_v": 3.81, "max_current_a": 0.4}
        enabled.append(number)
        (smu.chan1 if number == 1 else smu.chan2).on = True
    settings = dict(channel=channel, expected_serial="TESTSERIAL", four_wire=True,
                    nplc=1, maximum_voltage_v=3.81, maximum_current_a=0.4,
                    output_authorized=authorized, spot_factory=lambda driver, number: spot,
                    enable_output=enable, status_error_type=FakeStatusError)
    settings.update(overrides)
    return B2902BPowerBackend(smu, **settings), smu, spot, enabled


@pytest.mark.parametrize("channel", [1, 2])
def test_preparation_and_explicit_zero_probe_use_existing_driver_contract(channel):
    backend, smu, spot, enabled = make(channel=channel)
    assert smu.calls == [] and backend.simulation_only is False
    result = backend.prepare(limits())
    assert result["channel"] == channel and result["current_setpoint_a"] == 0
    assert result["selected_output_off"] is True and result["current_source"] is True
    assert not smu.chan1.on and not smu.chan2.on and enabled == []
    backend.set_output(True)
    assert enabled == [channel] and backend.get_output_state() is True
    backend.set_current(-0.2)
    assert backend.get_current_setpoint() == -0.2
    frame = backend.read_fresh()
    assert frame.channel == channel and frame.current_a == -0.2
    assert frame.raw_instrument_timestamp_s == 4.0
    backend.shutdown_all()
    assert backend.get_output_states() == (False, False)
    backend.close_session()
    assert not smu.is_connected
    assert smu.calls.index("shutdown") < smu.calls.index("close")


def test_default_output_authorization_is_off():
    backend = B2902BPowerBackend(object(), channel=1, expected_serial="TESTSERIAL",
                                four_wire=True, nplc=1, maximum_voltage_v=3.81, maximum_current_a=0.4)
    assert backend.output_authorized is False and backend.simulation_only is False


def test_unauthorized_activation_is_rejected_before_enable_helper():
    backend, _, _, enabled = make(authorized=False)
    backend.prepare(limits())
    with pytest.raises(PowerTransportError, match="authorization"):
        backend.set_output(True)
    assert enabled == [] and backend.failed


@pytest.mark.parametrize("identity", [None, "bad", "X,B2901B,TESTSERIAL,1", "X,B2902B,WRONG,1", "X,B2902B,TESTSERIAL,"])
def test_wrong_identity_never_reaches_configuration(identity):
    backend, smu, _, _ = make()
    smu.identity = identity
    with pytest.raises(PowerTransportError):
        backend.prepare(limits())
    assert "abort" not in smu.calls
    assert not any(".set_" in name for name in smu.calls)


@pytest.mark.parametrize("channel", [1, 2])
def test_existing_output_on_is_not_silently_disabled_to_bypass_preflight(channel):
    backend, smu, _, _ = make()
    (smu.chan1 if channel == 1 else smu.chan2).on = True
    with pytest.raises(PowerTransportError, match="Both outputs"):
        backend.prepare(limits())
    assert "abort" not in smu.calls


@pytest.mark.parametrize("field,value", [
    ("expected_serial", "REPLACE_SERIAL"), ("expected_serial", None),
    ("four_wire", 1), ("nplc", True), ("maximum_voltage_v", 0),
    ("maximum_current_a", float("nan")), ("output_authorized", 1),
])
def test_constructor_rejects_invalid_supplied_settings(field, value):
    with pytest.raises(ValueError):
        make(**{field: value})


def test_phase_limits_checked_before_any_instrument_call():
    backend, smu, _, _ = make(maximum_current_a=0.3)
    with pytest.raises(ValueError, match="ceilings"):
        backend.prepare(limits())
    assert smu.calls == []


@pytest.mark.parametrize("field,value", [
    ("mode", "VOLT"), ("current", -0.1), ("current", True),
    ("voltage", 4), ("four_wire", False), ("nplc", 2),
    ("off_mode", "ZERO"), ("auto", True), ("on", 0),
])
def test_preparation_readback_mismatch_is_terminal(field, value):
    backend, smu, _, enabled = make()
    smu.chan1.bad_readbacks[field] = value
    with pytest.raises((ValueError, PowerTransportError)):
        backend.prepare(limits())
    assert enabled == [] and backend.failed


@pytest.mark.parametrize("value", [0, 0.1, -0.41, True, float("inf")])
def test_invalid_power_command_never_reaches_current_setter(value):
    backend, smu, _, _ = make()
    backend.prepare(limits())
    backend.set_output(True)
    before = smu.calls.count("ch1.set_current")
    with pytest.raises(ValueError):
        backend.set_current(value)
    assert smu.calls.count("ch1.set_current") == before


def test_nonzero_or_wrong_mode_after_preparation_prevents_probe_activation():
    backend, smu, _, enabled = make()
    backend.prepare(limits())
    smu.chan1.current = -0.2
    with pytest.raises(PowerTransportError, match="zero"):
        backend.set_output(True)
    assert enabled == []


@pytest.mark.parametrize("status,error", [(9, None), (8193, "status"), (3, None), (0, None)])
def test_valid_fault_records_are_returned_for_recording_and_transport_becomes_terminal(status, error):
    backend, _, spot, _ = make()
    backend.prepare(limits())
    backend.set_output(True)
    spot.status, spot.error = status, error
    frame = backend.read_fresh()
    assert frame.status == status and backend.failed
    with pytest.raises(PowerTransportError):
        backend.set_current(-0.2)
    backend.shutdown_all()
    backend.close_session()


def test_communication_exception_is_not_misrepresented_as_a_status_record():
    backend, _, spot, _ = make()
    backend.prepare(limits())
    backend.set_output(True)
    original = OSError("read failed")
    spot.error = original
    with pytest.raises(OSError) as caught:
        backend.read_fresh()
    assert caught.value is original


def test_both_cleanup_readbacks_attempted_even_if_first_fails():
    backend, smu, _, _ = make()
    smu.failures["ch1.get_on"] = OSError("first readback")
    with pytest.raises(OSError):
        backend.get_output_states()
    assert "ch2.get_on" in smu.calls
    backend.close_session()
    assert "close" in smu.calls


def test_shutdown_failure_does_not_prevent_independent_close():
    backend, smu, _, _ = make()
    smu.failures["shutdown"] = OSError("shutdown")
    with pytest.raises(OSError):
        backend.shutdown_all()
    assert backend.stopped
    backend.close_session()
    assert "close" in smu.calls


def test_close_is_idempotent_and_does_not_claim_to_disable_outputs():
    backend, smu, _, _ = make()
    backend.close_session()
    backend.close_session()
    assert smu.calls == ["close"]


def test_reprepare_and_reenable_are_rejected():
    backend, _, _, _ = make()
    backend.prepare(limits())
    with pytest.raises(PowerTransportError, match="single-use"):
        backend.prepare(limits())
    backend, _, _, _ = make()
    backend.prepare(limits())
    backend.set_output(True)
    with pytest.raises(PowerTransportError):
        backend.set_output(True)


@pytest.mark.parametrize("response", ["LIST", "PULS"])
def test_non_dc_fixed_source_shape_is_rejected(response):
    backend, smu, _, enabled = make()
    smu.responses[":SOUR1:FUNC:SHAP?"] = response
    with pytest.raises(PowerTransportError, match="DC/FIX"):
        backend.prepare(limits())
    assert enabled == []


def test_disconnected_session_is_not_implicitly_opened():
    backend, smu, _, _ = make()
    smu.is_connected = False
    with pytest.raises(PowerTransportError, match="already-connected"):
        backend.prepare(limits())
    assert smu.calls == []


def test_positive_current_after_power_command_is_retained_then_terminal():
    backend, _, spot, _ = make()
    backend.prepare(limits())
    backend.set_output(True)
    backend.set_current(-0.2)
    spot.current = 0.001
    frame = backend.read_fresh()
    assert frame.current_a == 0.001 and backend.failed


def test_default_lazy_bindings_resolve_existing_helper_names(monkeypatch):
    import sys
    from types import ModuleType
    backend, smu, spot, _ = make()
    backend._spot_factory = None
    backend._enable_output = None
    acquisition_module = ModuleType("acquisition.spot_acquisition")
    acquisition_module.SpotAcquisition = lambda driver, channel: spot
    acquisition_module.MeasurementStatusError = FakeStatusError
    output_module = ModuleType("drivers.dc_output")
    activations = []
    def enable(driver, channel, **ceilings):
        activations.append((channel, ceilings))
        driver.chan1.on = True
    output_module.enable_dc_output = enable
    for parent_name in ("acquisition", "drivers"):
        if parent_name not in sys.modules:
            parent = ModuleType(parent_name)
            parent.__path__ = []
            monkeypatch.setitem(sys.modules, parent_name, parent)
    monkeypatch.setitem(sys.modules, "acquisition.spot_acquisition", acquisition_module)
    monkeypatch.setitem(sys.modules, "drivers.dc_output", output_module)
    backend.prepare(limits())
    backend.set_output(True)
    spot.error, spot.status = "status", 9
    assert backend.read_fresh().status == 9
    assert activations == [(1, {"max_voltage_v": 3.81, "max_current_a": 0.4})]
