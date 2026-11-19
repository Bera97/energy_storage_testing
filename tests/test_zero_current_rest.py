"""Offline zero-current-rest policy, recording, and integrated runner tests."""
from copy import deepcopy
import json
from types import SimpleNamespace
import pytest
import experiments.zero_current_rest as zr
from experiments.config import load_config, validate_config
from experiments.sweeps import build_charge_current_sweep_plan, execute_charge_current_sweep
from experiments.preconditioning import build_plan
from analysis.export_experiment import validate_sweep, export_sweep
from analysis.dual_experiment import check_batch, export_dual_sweep
from experiments.dual_preconditioning import build_dual_plan
from experiments.dual_live import execute
import test_cv_hold_sweep as sweep_tests

single_rig = sweep_tests.single_rig
documents = sweep_tests.documents
rig = sweep_tests.rig
pair = sweep_tests.pair


class Clock:
    def __init__(self):
        self.now = 0.0
    def __call__(self):
        return self.now
    def sleep(self, duration):
        self.now += duration


class Channel:
    def __init__(self):
        self.on = False
        self.auto = False
        self.mode = "CURR"
        self.current = 0.0
        self.limit = 3.81
        self.wire = True
        self.off = "HIZ"
        self.nplc = 1.0
    def get_output_state(self):
        return self.on
    def set_auto_output_on(self, value):
        self.auto = value
    def get_auto_output_on(self):
        return self.auto
    def set_output_off_mode(self, value):
        self.off = value
    def get_output_off_mode(self):
        return self.off
    def configure_fixed_dc(self, value):
        self.mode = value
    def get_source_mode(self):
        return self.mode
    def set_current(self, value):
        self.current = value
    def get_current_setpoint(self):
        return self.current
    def set_voltage_limit(self, value):
        self.limit = value
    def get_voltage_limit(self):
        return self.limit
    def set_four_wire(self, value):
        self.wire = value
    def get_four_wire(self):
        return self.wire
    def get_nplc(self):
        return self.nplc


class Driver:
    def __init__(self):
        self.chan1, self.chan2 = Channel(), Channel()
        self.commands = []
    def _write(self, command):
        self.commands.append(command)
        if command == ":OUTP1 ON":
            self.chan1.on = True
        elif command == ":OUTP2 ON":
            self.chan2.on = True
    def _query(self, command):
        if "FUNC:SHAP?" in command:
            return "DC"
        if "CURR:MODE?" in command:
            return "FIX"
        raise AssertionError(command)
    def check_errors(self):
        pass
    def stop(self, channel=1):
        (self.chan1 if channel == 1 else self.chan2).on = False
        return True


class Spot:
    current = -1e-10
    voltage = 3.0
    status = 1
    error = None
    def __init__(self, driver, channel):
        self.driver, self.channel = driver, channel
    def configure(self, nplc):
        assert not self.driver.chan1.on and not self.driver.chan2.on
        (self.driver.chan1 if self.channel == 1 else self.driver.chan2).nplc = nplc
    def read(self):
        if self.error is not None:
            raise self.error
        return SimpleNamespace(channel=self.channel, timestamp_s=0.0,
            voltage_v=self.voltage, current_a=self.current, status=self.status)


def configuration():
    return {"instrument": {"channel": 1, "four_wire": True, "nplc": 1.0},
        "dut": {"specimen_id": "OFFLINE", "ratings": {
            "minimum_voltage_v": 2.19, "maximum_voltage_v": 3.81,
            "maximum_continuous_charge_current_a": 0.4,
            "maximum_continuous_discharge_current_a": 0.4}},
        "acquisition": {"interval_s": 0.2, "maximum_sample_gap_s": 0.5},
        "rest": {"mode": "zero_current_logging", "maximum_abs_current_a": 0.001,
                 "after_charge_s": 0.6, "after_discharge_s": 0.6}}


def task(tmp_path, *, shared=False, spot=Spot):
    config = configuration()
    phase = {"phase_index": 1, "cycle": 1, "type": "rest",
             "follows": "charge", "duration_s": 0.6}
    zr.decorate_rest_phases([phase], config)
    driver, clock = Driver(), Clock()
    driver.chan2.on = shared
    current = zr.ZeroCurrentRestTask(driver, config, phase, tmp_path / "rest",
        driver.stop, clock=clock, shared=shared, spot_factory=spot)
    return current, driver, clock, config, phase


def test_zero_source_recording_and_validation(tmp_path):
    current, driver, clock, config, phase = task(tmp_path)
    report = current.run_to_completion(sleep=clock.sleep)
    path, rows = zr.validate_record(report, phase, config, tmp_path)
    assert report["elapsed_s"] >= 0.6
    assert len(rows) >= 2 and rows[0]["current_a"] < 0
    assert (path / "COMPLETE").is_file()
    assert driver.chan1.current == 0.0 and not driver.chan1.on
    assert not driver.chan2.on


def test_channel_local_rest_does_not_stop_peer_or_reformat(tmp_path):
    current, driver, clock, _, _ = task(tmp_path, shared=True)
    current.run_to_completion(sleep=clock.sleep)
    assert driver.chan2.on is True
    assert not any(command.startswith(":FORM") for command in driver.commands)
    assert not any(command.startswith(":ABOR:ALL (@2)") for command in driver.commands)


@pytest.mark.parametrize("attribute,value", [
    ("current", 0.01), ("voltage", 4.0), ("voltage", 1.0),
    ("status", 0), ("status", 3), ("status", 1 | (1 << 3)),
    ("current", float("nan")),
])
def test_bad_readings_stop_and_preserve_failure(tmp_path, attribute, value):
    fake = type("BadSpot", (Spot,), {attribute: value})
    current, driver, clock, _, _ = task(tmp_path, spot=fake)
    with pytest.raises((ValueError, RuntimeError)):
        current.run_to_completion(sleep=clock.sleep)
    assert not driver.chan1.on
    assert not (current.directory / "COMPLETE").exists()
    assert json.loads((current.directory / "rest.json").read_text())["state"] == "raised"


def test_wrong_zero_readback_never_enables_output(tmp_path):
    current, driver, _, _, _ = task(tmp_path)
    driver.chan1.get_current_setpoint = lambda: 0.001
    with pytest.raises(RuntimeError, match="exactly zero"):
        current.start()
    assert ":OUTP1 ON" not in driver.commands


def test_gap_and_keyboard_interrupt_are_not_success(tmp_path):
    current, driver, clock, _, _ = task(tmp_path)
    current.start()
    current.poll()
    clock.now = 0.55
    with pytest.raises(TimeoutError):
        current.poll()
    assert not driver.chan1.on
    other = tmp_path / "interrupt"
    class Interrupted(Spot):
        def read(self):
            raise KeyboardInterrupt()
    current, driver, clock, _, _ = task(other, spot=Interrupted)
    with pytest.raises(KeyboardInterrupt):
        current.run_to_completion(sleep=clock.sleep)
    assert not driver.chan1.on
    assert not (current.directory / "COMPLETE").exists()


def test_mutated_csv_and_missing_marker_are_rejected(tmp_path):
    current, _, clock, config, phase = task(tmp_path)
    report = current.run_to_completion(sleep=clock.sleep)
    csv_path = current.directory / "measurements.csv"
    csv_path.write_text(csv_path.read_text().replace("3.0", "3.1"), encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        zr.validate_record(report, phase, config, tmp_path)
    (current.directory / "COMPLETE").unlink()
    with pytest.raises(ValueError, match="COMPLETE"):
        zr.validate_record(report, phase, config, tmp_path)


@pytest.mark.parametrize("mode,guard", [("bad", 0.001), ("zero_current_logging", None),
    ("zero_current_logging", True), ("zero_current_logging", 0.4), ("outputs_off", 0.001)])
def test_invalid_modes_and_guards(mode, guard):
    config = configuration()
    config["rest"].update(mode=mode, maximum_abs_current_a=guard)
    with pytest.raises(ValueError):
        zr.validate_rest_settings(config)


def test_single_sweep_with_actual_recorded_rests(single_rig, tmp_path, monkeypatch):
    document, driver = single_rig
    document["rest"].update(mode="zero_current_logging", maximum_abs_current_a=0.001)
    facade = Driver()
    driver.chan1, driver.chan2 = facade.chan1, facade.chan2
    driver._write, driver._query = facade._write, facade._query
    monkeypatch.setattr("acquisition.spot_acquisition.SpotAcquisition", Spot)
    manifest, directory = execute_charge_current_sweep(document, sweep_id="single_duration")
    assert manifest["state"] == "completed"
    checked = validate_sweep(directory)
    assert checked["valid"], checked["errors"]
    destination = tmp_path / "analysis"
    report = export_sweep(directory, destination)
    assert len(report["rest_phases"]) == 5
    assert len(report["cycles"]) == 2
    assert (destination / "rest_summary.csv").is_file()
    assert (destination / "ANALYSIS_COMPLETE").is_file()


def test_dual_sweep_with_actual_recorded_rests(documents, tmp_path, monkeypatch):
    profiles, driver, options = documents
    for document in profiles:
        document["rest"].update(mode="zero_current_logging", maximum_abs_current_a=0.001)
    monkeypatch.setattr("acquisition.spot_acquisition.SpotAcquisition", Spot)
    directory = tmp_path / "dual_measured"
    result = execute(build_dual_plan(profiles), directory, "TESTSERIAL", **options)
    assert result["state"] == "completed"
    checked = check_batch(directory)
    assert checked["valid"], checked["errors"]
    destination = tmp_path / "dual_analysis"
    report = export_dual_sweep(directory, destination)
    assert len(report["rest_phases"]) == 12
    assert len(report["cycles"]) == 5
    assert not driver.chan1.on and not driver.chan2.on


def test_defaults_and_preview_never_construct_instruments(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline planning tried instrument construction.")
    monkeypatch.setattr("experiments.run_live._new_smu", forbidden)
    monkeypatch.setattr("experiments.dual_instrument_io._new_smu", forbidden)
    doc = load_config("configs/cv_hold.parallel.lic.test_run.yaml")["config"]
    before = deepcopy(doc)
    ordinary = build_charge_current_sweep_plan(doc)
    assert "rest_mode" not in ordinary["initial_discharge"]["phases"][1]
    doc["rest"].update(mode="zero_current_logging", maximum_abs_current_a=0.001)
    plan = build_charge_current_sweep_plan(doc)
    assert plan["total_phase_count"] == ordinary["total_phase_count"] == 6
    assert zr.is_measured(plan["initial_discharge"]["phases"][1])
    explicit = deepcopy(before)
    explicit["rest"]["mode"] = "outputs_off"
    validated = validate_config(explicit)
    assert validated["config"] == explicit
    base = deepcopy(plan["conditions"][0]["config"])
    assert sum(zr.is_measured(p) for p in build_plan(base)["phases"]) == 2


def test_recording_failure_stops_before_success_marker(tmp_path):
    current, driver, _, _, _ = task(tmp_path)
    current.start()
    def fail(row):
        raise OSError("synthetic CSV failure")
    current.writer = SimpleNamespace(writerow=fail)
    with pytest.raises(OSError, match="CSV failure"):
        current.poll()
    assert not driver.chan1.on and current.stream.closed
    assert not (current.directory / "COMPLETE").exists()


def test_shutdown_failure_is_not_success(tmp_path):
    current, driver, clock, _, _ = task(tmp_path)
    def stop_failure():
        driver.chan1.on = False
        raise OSError("synthetic shutdown failure")
    current.stop = stop_failure
    with pytest.raises(OSError, match="shutdown failure"):
        current.run_to_completion(sleep=clock.sleep)
    assert current.stream.closed
    assert not (current.directory / "COMPLETE").exists()


def test_slow_query_preserves_sample_but_fails_deadline(tmp_path):
    current, driver, clock, _, _ = task(tmp_path)
    class Slow(Spot):
        def read(self):
            clock.sleep(2)
            return super().read()
    current.spot_factory = Slow
    with pytest.raises(TimeoutError):
        current.run_to_completion(sleep=clock.sleep)
    assert len(current.rows) == 1
    assert not driver.chan1.on
    assert not (current.directory / "COMPLETE").exists()

def test_nonzero_clock_rest_endpoint_never_requests_zero_sleep(tmp_path):
    config = configuration()
    config["rest"]["after_charge_s"] = 0.05
    config["rest"]["after_discharge_s"] = 0.05
    phase = {
        "phase_index": 1, "cycle": 1, "type": "rest",
        "follows": "charge", "duration_s": 0.05,
    }
    zr.decorate_rest_phases([phase], config)
    driver, clock = Driver(), Clock()
    clock.now = 0.2
    current = zr.ZeroCurrentRestTask(
        driver, config, phase, tmp_path / "boundary_rest",
        driver.stop, clock=clock, spot_factory=Spot,
    )
    sleep_calls = []
    def positive_sleep(duration):
        assert duration > 0, "Rest loop requested zero-delay sleep."
        sleep_calls.append(duration)
        assert len(sleep_calls) <= 20, "Rest loop did not terminate."
        clock.sleep(duration)
    report = current.run_to_completion(
        sleep=positive_sleep, poll_interval_s=0.05
    )
    _, rows = zr.validate_record(report, phase, config, tmp_path)
    assert rows[-1]["host_after_s"] >= phase["duration_s"]
    assert not driver.chan1.on
