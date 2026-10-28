"""Offline phase-adapter integration tests using a fake instrument."""
from copy import deepcopy
import json
import re
import pytest
from analysis.validate_recorded_run import validate_run
from experiments.dual_coordinator import VirtualClock
from experiments.dual_instrument_io import DualInstrumentSession
from experiments.dual_phase import DualPhase
from experiments.dual_preconditioning import build_dual_plan
import test_dual_trace as trace_tests
pair = trace_tests.pair
class SourceChannel(trace_tests.io_tests.FakeChannel):
    def __init__(self, driver, number):
        super().__init__()
        self.driver = driver
        self.number = number
        self.on = False
        self.auto = False
        self.mode = "VOLT"
        self.off_mode = "HIZ"
        self.four_wire = False
        self.voltage = 0.0
        self.current = 0.0
        self.current_limit = 1.0
        self.voltage_limit = 4.2
        self.bad_current_readback = False
    def set_output_off_mode(self, value):
        self.off_mode = value
        self.driver._write(f":OUTP{self.number}:OFF:MODE {value}")
    def configure_fixed_dc(self, mode):
        self.mode = mode
        self.driver._write(f":SOUR{self.number}:FUNC:SHAP DC")
        self.driver._write(f":SOUR{self.number}:{mode}:MODE FIX")
        self.driver._write(f":SOUR{self.number}:FUNC:MODE {mode}")
    def set_voltage(self, value):
        self.voltage = value
        self.driver._write(f":SOUR{self.number}:VOLT {value}")
    def set_current(self, value):
        self.current = value
        self.driver._write(f":SOUR{self.number}:CURR {value}")
    def set_current_limit(self, value):
        self.current_limit = value
        self.driver._write(f":SENS{self.number}:CURR:PROT {value}")
    def set_voltage_limit(self, value):
        self.voltage_limit = value
        self.driver._write(f":SENS{self.number}:VOLT:PROT {value}")
    def set_four_wire(self, value):
        self.four_wire = value
        self.driver._write(
            f":SENS{self.number}:REM {'ON' if value else 'OFF'}"
        )
    def get_source_mode(self):
        return self.mode
    def get_output_off_mode(self):
        return self.off_mode
    def get_four_wire(self):
        return self.four_wire
    def get_voltage_setpoint(self):
        return self.voltage
    def get_current_setpoint(self):
        return self.current
    def get_current_limit(self):
        if self.bad_current_readback:
            return self.current_limit * 2
        return self.current_limit
    def get_voltage_limit(self):
        return self.voltage_limit
class SourceSMU(trace_tests.TraceSMU):
    def __init__(self):
        super().__init__()
        self.chan1 = SourceChannel(self, 1)
        self.chan2 = SourceChannel(self, 2)
        self.phases = {}
    def _write(self, command):
        match = re.fullmatch(r":OUTP([12]) ON", command)
        if match:
            channel = int(match[1])
            phase = self.phases[channel]
            assert phase.run is not None
            assert phase.run.writer.path.is_file()
            selected = self.chan1 if channel == 1 else self.chan2
            selected.on = True
        super()._write(command)
    def _query(self, command):
        if re.fullmatch(r":SOUR[12]:FUNC:SHAP\?", command):
            return "DC"
        if re.fullmatch(r":SOUR[12]:(VOLT|CURR):MODE\?", command):
            return "FIX"
        return super()._query(command)
@pytest.fixture
def setup(pair, tmp_path):
    documents = deepcopy(pair)
    for document in documents:
        document["experiment"]["cycles"] = 1
        document["charge"]["maximum_duration_s"] = 10
        document["charge"]["maximum_cv_duration_s"] = 5
        document["charge"]["termination"]["hold_time_s"] = 0.5
        document["discharge"]["maximum_duration_s"] = 10
    driver = SourceSMU()
    session = DualInstrumentSession(
        build_dual_plan(documents),
        "TESTSERIAL",
        smu_factory=lambda *args, **kwargs: driver,
    )
    session.connect()
    clock = VirtualClock()
    def make(channel, kind, *, authorized=True):
        preview = session.plan["channels"][channel - 1]
        phase = next(
            item for item in preview["acquisition_plan"]["phases"]
            if item["type"] == kind
        )
        adapter = DualPhase(
            session,
            channel,
            phase,
            tmp_path,
            f"channel_{channel}_{kind}",
            clock=clock,
            allow_output=authorized,
        )
        driver.phases[channel] = adapter
        return adapter
    try:
        yield make, session, driver, clock
    finally:
        for phase in driver.phases.values():
            if phase.run is not None:
                phase.run.close()
        session.close()
def charge_records():
    return [
        (4.1, 0.05, 100.0 + index * 0.2, 0)
        for index in range(4)
    ]
def test_output_authorization_is_required(setup):
    make, _, driver, _ = setup
    phase = make(1, "charge_cc_cv", authorized=False)
    with pytest.raises(RuntimeError, match="not authorized"):
        phase.start()
    assert phase.run is None
    assert driver.commands == []
def test_two_phases_can_progress_without_stopping_peer(setup):
    make, _, driver, clock = setup
    first = make(1, "charge_cc_cv")
    second = make(2, "discharge_cc")
    first.start()
    second.start()
    assert driver.chan1.on is True
    assert driver.chan2.on is True
    driver.samples[1] = charge_records()
    clock.now = 0.8
    assert first.poll() is None
    assert driver.chan1.on is False
    assert driver.chan2.on is True
    result = first.poll()
    assert result["stop_reason"] == "cv_current_termination"
    assert driver.chan2.on is True
    driver.samples[2] = [
        (3.0, -2.0, 200.0, 1),
        (2.6, -2.0, 200.2, 1),
    ]
    assert second.poll() is None
    result = second.poll()
    assert result["stop_reason"] == "voltage_min"
    for phase in (first, second):
        validation = validate_run(phase.run.directory)
        assert validation["valid"], validation["errors"]
        assert (phase.run.directory / "COMPLETE").is_file()
    assert driver.shutdown_calls == 0
def test_trailing_records_are_saved_after_endpoint(setup):
    make, _, driver, clock = setup
    phase = make(1, "charge_cc_cv")
    phase.start()
    driver.samples[1] = charge_records()
    clock.now = 0.8
    phase.poll()
    driver.samples[1].append((4.1, 0.01, 100.8, 0))
    phase.poll()
    document = json.loads(
        (phase.run.directory / "metadata.json").read_text(
            encoding="utf-8"
        )
    )
    report = document["phase_transport_report"]
    assert report["records_saved_before_finalization"] == 4
    assert report["retained_count"] == 5
    assert report["trailing_record_count"] == 1
    assert report["shutdown_scope"] == "selected_channel"
def test_source_mismatch_prevents_output_and_records_failure(setup):
    make, _, driver, _ = setup
    phase = make(1, "charge_cc_cv")
    driver.chan1.bad_current_readback = True
    driver.chan2.on = True
    with pytest.raises(RuntimeError, match="readback mismatch"):
        phase.start()
    assert ":OUTP1 ON" not in driver.commands
    assert driver.chan1.on is False
    assert driver.chan2.on is False
    assert not (phase.run.directory / "COMPLETE").exists()
    assert phase.run.writer.closed
def test_timeout_attempts_stop_both_before_failure_export(setup):
    make, _, driver, clock = setup
    phase = make(1, "charge_cc_cv")
    phase.start()
    driver.chan2.on = True
    clock.now = 20
    with pytest.raises(TimeoutError):
        phase.poll()
    assert driver.shutdown_calls == 1
    assert driver.chan1.on is False
    assert driver.chan2.on is False
    assert not (phase.run.directory / "COMPLETE").exists()
    validation = validate_run(phase.run.directory)
    assert validation["valid"], validation["errors"]
    assert validation["state"] == "raised"
def test_csv_failure_preserves_original_error(setup, monkeypatch):
    make, _, driver, _ = setup
    phase = make(1, "charge_cc_cv")
    phase.start()
    driver.chan2.on = True
    driver.samples[1] = charge_records()
    original = OSError("synthetic CSV failure")
    def fail(batch):
        raise original
    monkeypatch.setattr(phase.run.writer, "write_batch", fail)
    with pytest.raises(OSError) as caught:
        phase.poll()
    assert caught.value is original
    assert len(original.partial_readings) == 4
    assert driver.chan1.on is False
    assert driver.chan2.on is False
    assert phase.run.writer.closed
    assert not (phase.run.directory / "COMPLETE").exists()
