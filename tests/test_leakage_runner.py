"""Leakage runner: schedule logging, retries, limits, early stop and shutdown."""
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import pytest
import test_charge_current_sweep
from acquisition.spot_acquisition import MeasurementStatusError, SMUReading
from experiments.config import validate_config
from experiments.leakage_planning import build_leakage_plan, sample_times
from experiments.leakage_runner import (
    CSV_NAME, METADATA_NAME, LeakageRunError, evaluate_stabilization,
    planned_samples, run_leakage_test,
)
from test_leakage_planning import leakage_profile
profile = test_charge_current_sweep.profile
SMALL = {
    "maximum_duration_s": 10,
    "final_window_s": 5,
    "schedule": [{"until_s": 2, "interval_s": 0.5}, {"until_s": 10, "interval_s": 2}],
}
OFFSETS = [0.0, 0.5, 1.0, 1.5, 2.0, 4.0, 6.0, 8.0, 10.0]
def small_doc(base, **extra):
    doc = leakage_profile(base)
    doc["leakage"] = {**json.loads(json.dumps(SMALL)), **extra}
    return doc
def early_doc(base):
    doc = leakage_profile(base)
    doc["leakage"] = {
        "maximum_duration_s": 400, "final_window_s": 20,
        "schedule": [{"until_s": 400, "interval_s": 1}],
        "early_stop": {"enabled": True, "minimum_duration_s": 100, "window_s": 20,
                       "relative_change": 0.01, "check_interval_s": 10,
                       "consecutive_checks": 3},
    }
    return doc
def reading(voltage, current, status=0, channel=1):
    return SMUReading(channel, voltage, current, 123.0, status)
class FakeTime:
    def __init__(self):
        self.now = 1000.0
    def clock(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds
class FakeChannel:
    def __init__(self, smu):
        self.smu = smu
    def get_output_state(self):
        return self.smu.output_on
class FakeSMU:
    def __init__(self, fail_shutdown):
        self.output_on = False
        self.fail_shutdown = fail_shutdown
        self.events = []
        self.chan1 = FakeChannel(self)
        self.chan2 = FakeChannel(self)
    def shutdown(self):
        self.events.append("shutdown")
        if self.fail_shutdown:
            raise RuntimeError("shutdown boom")
        self.output_on = False
class FakeSpot:
    def __init__(self, rig):
        self.rig = rig
        self.nplc = None
    def configure(self, nplc):
        self.nplc = nplc
    def read(self):
        self.rig.timing.now += self.rig.read_cost
        self.rig.reads += 1
        return self.rig.behavior(self.rig)
class Rig:
    def __init__(self, document, behavior=None, read_cost=0.05, fail_shutdown=False):
        plan = build_leakage_plan(document)
        ratings = document["dut"]["ratings"]
        self.document = document
        self.hold = plan["hold_voltage_v"]
        self.compliance = plan["compliance_current_a"]
        self.vmin = ratings["minimum_voltage_v"]
        self.vmax = ratings["maximum_voltage_v"]
        self.timing = FakeTime()
        self.smu = FakeSMU(fail_shutdown)
        self.events = self.smu.events
        self.behavior = behavior or (lambda rig: rig.nominal())
        self.read_cost = read_cost
        self.reads = 0
        self.start = None
        self.spot = None
    def nominal(self):
        return reading(self.hold, self.compliance / 1000)
    def elapsed(self):
        return self.timing.now - self.start
    def configure_source(self, smu, base):
        self.events.append("configure")
        return {"voltage_setpoint_v": self.hold}
    def spot_factory(self, smu, channel):
        self.events.append("spot")
        self.spot = FakeSpot(self)
        return self.spot
    def enable_output(self, smu, channel, *, max_voltage_v, max_current_a):
        self.events.append(("enable", channel, max_voltage_v, max_current_a))
        smu.output_on = True
        self.start = self.timing.now
    def run(self, run_directory):
        return run_leakage_test(
            self.document, smu=self.smu, maximum_voltage_v=self.vmax,
            maximum_current_a=self.compliance, configure_source=self.configure_source,
            spot_factory=self.spot_factory, enable_output=self.enable_output,
            run_directory=run_directory, clock=self.timing.clock, sleep=self.timing.sleep,
            utc_now=lambda: datetime(2026, 11, 26, 20, 0, 0, tzinfo=timezone.utc),
        )
def rows_of(run):
    with (run / CSV_NAME).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))
def metadata_of(run):
    return json.loads((run / METADATA_NAME).read_text(encoding="utf-8"))
def test_planned_samples_match_the_planning_schedule(profile):
    segments = build_leakage_plan(leakage_profile(profile))["schedule"]["segments"]
    assert [offset for offset, _ in planned_samples(segments)] == list(sample_times(segments))
def test_full_run_logs_schedule_and_shuts_down(profile, tmp_path):
    rig = Rig(small_doc(profile))
    run = tmp_path / "run"
    result = rig.run(run)
    rows = rows_of(run)
    assert [float(row["scheduled_s"]) for row in rows] == OFFSETS
    assert result["state"] == "completed"
    assert result["recorded_sample_count"] == result["planned_sample_count"] == 9
    assert all(row["read_failures"] == "0" for row in rows)
    stored = metadata_of(run)
    assert stored["state"] == "completed" and stored["shutdown_verified"] is True
    assert stored["csv_sha256"] == hashlib.sha256((run / CSV_NAME).read_bytes()).hexdigest()
    assert stored["hardware_qualified"] is False
    assert rig.events == ["configure", "spot",
                          ("enable", 1, rig.vmax, rig.compliance), "shutdown"]
    assert rig.spot.nplc == rig.document["instrument"]["nplc"]
    assert not rig.smu.output_on
def test_metadata_reports_running_during_the_hold(profile, tmp_path):
    run = tmp_path / "run"
    seen = []
    def behavior(rig):
        if not seen:
            seen.append(metadata_of(run)["state"])
        return rig.nominal()
    Rig(small_doc(profile), behavior).run(run)
    assert seen == ["running"]
def test_existing_directory_is_refused_before_any_instrument_access(profile, tmp_path):
    rig = Rig(small_doc(profile))
    run = tmp_path / "run"
    run.mkdir()
    with pytest.raises(FileExistsError):
        rig.run(run)
    assert rig.events == []
def test_two_consecutive_read_failures_are_retried(profile, tmp_path):
    def behavior(rig):
        if rig.reads in (3, 4):
            raise OSError("usb glitch")
        return rig.nominal()
    run = tmp_path / "run"
    result = Rig(small_doc(profile), behavior).run(run)
    rows = rows_of(run)
    assert result["state"] == "completed" and len(rows) == 9
    assert rows[2]["read_failures"] == "2"
    assert result["read_retry_count"] == 2
    assert len(result["read_retry_events"]) == 2
def test_third_consecutive_failure_stops_and_keeps_partial_data(profile, tmp_path):
    def behavior(rig):
        if rig.reads in (3, 4, 5):
            raise OSError("usb glitch")
        return rig.nominal()
    rig = Rig(small_doc(profile), behavior)
    run = tmp_path / "run"
    with pytest.raises(LeakageRunError, match="consecutive"):
        rig.run(run)
    stored = metadata_of(run)
    assert stored["state"] == "failed" and stored["recorded_sample_count"] == 2
    assert len(rows_of(run)) == 2
    assert rig.events[-1] == "shutdown" and not rig.smu.output_on
    assert stored["error"]["type"] == "LeakageRunError"
def test_zero_tolerance_stops_on_first_failure(profile, tmp_path):
    def behavior(rig):
        if rig.reads == 3:
            raise OSError("usb glitch")
        return rig.nominal()
    doc = small_doc(profile, maximum_consecutive_read_failures=0)
    rig = Rig(doc, behavior)
    with pytest.raises(LeakageRunError):
        rig.run(tmp_path / "run")
    assert rig.reads == 3
def test_fault_status_is_not_retried(profile, tmp_path):
    def behavior(rig):
        if rig.reads == 3:
            raise MeasurementStatusError(reading(rig.hold, 0.0, 1 << 4))
        return rig.nominal()
    rig = Rig(small_doc(profile), behavior)
    run = tmp_path / "run"
    with pytest.raises(MeasurementStatusError):
        rig.run(run)
    assert rig.reads == 3
    assert metadata_of(run)["state"] == "failed" and not rig.smu.output_on
@pytest.mark.parametrize("case", ["above_hold", "below_minimum", "overcurrent", "wrong_channel"])
def test_unsafe_readings_stop_the_run(profile, tmp_path, case):
    def behavior(rig):
        if rig.reads < 3:
            return rig.nominal()
        voltage, current, channel = rig.hold, rig.compliance / 1000, 1
        if case == "above_hold":
            voltage = rig.hold + 0.5
        elif case == "below_minimum":
            voltage = rig.vmin - 0.1
        elif case == "overcurrent":
            current = rig.compliance * 2
        else:
            channel = 2
        return reading(voltage, current, channel=channel)
    rig = Rig(small_doc(profile), behavior)
    run = tmp_path / "run"
    with pytest.raises(LeakageRunError):
        rig.run(run)
    assert metadata_of(run)["state"] == "failed"
    assert len(rows_of(run)) == 2 and rig.events[-1] == "shutdown"
def test_sampling_overrun_stops_the_run(profile, tmp_path):
    def behavior(rig):
        if rig.reads == 3:
            rig.timing.now += 5.0
        return rig.nominal()
    rig = Rig(small_doc(profile), behavior)
    with pytest.raises(LeakageRunError, match="overrun"):
        rig.run(tmp_path / "run")
    assert rig.events[-1] == "shutdown"
def test_keyboard_interrupt_is_finalized_as_aborted(profile, tmp_path):
    def behavior(rig):
        if rig.reads == 3:
            raise KeyboardInterrupt
        return rig.nominal()
    rig = Rig(small_doc(profile), behavior)
    run = tmp_path / "run"
    with pytest.raises(KeyboardInterrupt):
        rig.run(run)
    stored = metadata_of(run)
    assert stored["state"] == "aborted" and stored["shutdown_verified"] is True
    assert len(rows_of(run)) == 2 and not rig.smu.output_on
def test_shutdown_failure_is_recorded_and_not_hidden(profile, tmp_path):
    rig = Rig(small_doc(profile), fail_shutdown=True)
    run = tmp_path / "run"
    with pytest.raises(LeakageRunError, match="Shutdown"):
        rig.run(run)
    stored = metadata_of(run)
    assert stored["state"] == "failed" and stored["shutdown_verified"] is False
    assert stored["cleanup_error"]["message"] == "shutdown boom"
def test_original_error_survives_a_failed_shutdown(profile, tmp_path):
    def behavior(rig):
        if rig.reads >= 3:
            raise OSError("usb glitch")
        return rig.nominal()
    rig = Rig(small_doc(profile), behavior, fail_shutdown=True)
    with pytest.raises(LeakageRunError, match="consecutive") as caught:
        rig.run(tmp_path / "run")
    assert any("shutdown boom" in note for note in caught.value.__notes__)
def test_early_stop_triggers_after_consecutive_stable_checks(profile, tmp_path):
    run = tmp_path / "run"
    result = Rig(early_doc(profile)).run(run)
    assert result["state"] == "stopped_early"
    assert result["recorded_sample_count"] == 121
    stop = metadata_of(run)["early_stop"]
    assert stop["triggered"] is True and stop["stop_offset_s"] == 120.0
    assert len(stop["checks"]) == 3 and all(check["stable"] for check in stop["checks"])
def test_early_stop_does_not_trigger_while_current_still_decays(profile, tmp_path):
    def behavior(rig):
        return reading(rig.hold, 1e-3 * math.exp(-rig.elapsed() / 50))
    result = Rig(early_doc(profile), behavior).run(tmp_path / "run")
    assert result["state"] == "completed" and result["recorded_sample_count"] == 401
    assert result["early_stop"]["triggered"] is False and result["early_stop"]["checks"]
def test_evaluate_stabilization_edges():
    times = [float(t) for t in range(0, 101)]
    assert evaluate_stabilization(times, [2.0] * 101, 100.0, 20.0)[2] == 0.0
    assert evaluate_stabilization(times, [2.0] * 101, 30.0, 20.0) is None
    assert evaluate_stabilization(times, [0.0] * 101, 100.0, 20.0) is None
    ramp = evaluate_stabilization(times, [float(t + 1) for t in times], 100.0, 20.0)
    assert ramp[2] > 0.1
@pytest.mark.parametrize("value", [-1, 11, True, "2", 1.5, None])
def test_read_failure_limit_is_validated(profile, value):
    doc = small_doc(profile, maximum_consecutive_read_failures=value)
    with pytest.raises(ValueError):
        validate_config(doc)
def test_read_failure_limit_defaults_to_two(profile):
    assert build_leakage_plan(small_doc(profile))["maximum_consecutive_read_failures"] == 2
def test_execution_ceilings_must_cover_the_hold(profile, tmp_path):
    rig = Rig(small_doc(profile))
    with pytest.raises(ValueError, match="ceilings"):
        run_leakage_test(
            rig.document, smu=rig.smu, maximum_voltage_v=rig.hold - 0.1,
            maximum_current_a=rig.compliance, configure_source=rig.configure_source,
            spot_factory=rig.spot_factory, enable_output=rig.enable_output,
            run_directory=tmp_path / "run", clock=rig.timing.clock, sleep=rig.timing.sleep,
        )
    assert rig.events == [] and not (tmp_path / "run").exists()
