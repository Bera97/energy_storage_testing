"""Step-down-only current ranging: controller, range setter, planning, runner and analysis."""
import csv
from datetime import datetime, timezone
import json
import math
import pytest

import test_charge_current_sweep
from acquisition.spot_acquisition import MeasurementStatusError
from analysis.leakage_analysis import analyze_leakage_run
from experiments import leakage_execution
from experiments.config import validate_config
from experiments.leakage_planning import build_leakage_plan
from experiments.leakage_ranging import (
    DEFAULTS, RANGE_LADDER, RangingError, StepDownRanger, apply_current_range,
    validate_range_control,
)
from experiments.leakage_runner import CSV_NAME, METADATA_NAME, run_leakage_test
from experiments.settings import settings_for
from test_leakage_planning import leakage_profile
from test_leakage_runner import Rig, reading, small_doc

profile = test_charge_current_sweep.profile


def ranging_doc(base, **control):
    doc = leakage_profile(base)
    doc["leakage"] = {
        "maximum_duration_s": 400, "final_window_s": 20,
        "schedule": [{"until_s": 400, "interval_s": 1}],
        "range_control": {"mode": "step_down", "minimum_range_a": 1e-4,
                          "confirm_samples": 5, "headroom": 0.5, "confirm_window_s": 0,
                          **control},
    }
    return doc


class Recorder:
    def __init__(self):
        self.calls = []
        self.outputs_on = []
        self.range = None

    def __call__(self, smu, channel, range_a):
        self.calls.append(range_a)
        self.outputs_on.append(smu.output_on)
        self.range = range_a
        return range_a


def run_ranging(rig, run, set_range):
    return run_leakage_test(
        rig.document, smu=rig.smu, maximum_voltage_v=rig.vmax,
        maximum_current_a=rig.compliance, configure_source=rig.configure_source,
        spot_factory=rig.spot_factory, enable_output=rig.enable_output,
        set_range=set_range, run_directory=run, clock=rig.timing.clock,
        sleep=rig.timing.sleep,
        utc_now=lambda: datetime(2026, 11, 27, 9, 0, 0, tzinfo=timezone.utc),
    )


def decaying(rig):
    if rig.reads <= 3:
        return reading(rig.hold, rig.compliance, status=2)
    return reading(rig.hold, 0.75 * rig.compliance * math.exp(-rig.elapsed() / 20))


def expected_sequence(rig, minimum=1e-4):
    ladder = [rung for rung in RANGE_LADDER if rung >= minimum * (1 - 1e-9)]
    first_current = 0.75 * rig.compliance * math.exp(-3.05 / 20)
    takeover = next(rung for rung in ladder if rung >= first_current / 0.8)
    return [takeover] + [rung for rung in reversed(ladder) if rung < takeover]


def make_rig(base, behavior=None, **control):
    holder = {}

    def respond(rig):
        return behavior(rig, holder) if behavior else decaying(rig)

    rig = Rig(ranging_doc(base, **control), respond)
    holder["expected"] = expected_sequence(rig)
    return rig, holder


def rows_of(run):
    with (run / CSV_NAME).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def metadata_of(run):
    return json.loads((run / METADATA_NAME).read_text(encoding="utf-8"))


def test_range_is_fixed_before_the_output_is_enabled_then_steps_down_one_rung_at_a_time(profile, tmp_path):
    recorder = Recorder()
    rig, holder = make_rig(profile)
    run = tmp_path / "run"
    result = run_ranging(rig, run, recorder)
    expected = holder["expected"]
    assert recorder.calls == expected and len(expected) >= 2
    assert recorder.outputs_on[0] is False and all(recorder.outputs_on[1:])
    rows = rows_of(run)
    assert float(rows[0]["range_a"]) == expected[0]
    assert float(rows[-1]["range_a"]) == expected[-1]
    events = result["range_control"]["events"]
    assert [event["action"] for event in events] == ["start"] + ["step_down"] * (len(expected) - 1)
    assert events[0]["sample_index"] is None
    assert result["range_control"]["current_range_a"] == expected[-1]
    assert result["state"] == "completed" and len(rows) == 401


@pytest.mark.parametrize("kind", ["invalid_field", "status_flag"])
def test_overflow_steps_up_once_then_locks_out_step_downs(profile, tmp_path, kind):
    recorder, state = Recorder(), {"spiked": False}

    def behavior(rig, holder):
        if recorder.range == holder["expected"][1] and not state["spiked"]:
            state["spiked"] = True
            if kind == "invalid_field":
                raise RuntimeError("Invalid current field: '9.91E+37'.")
            raise MeasurementStatusError(reading(rig.hold, 0.0, 1 << 13))
        return decaying(rig)

    rig, holder = make_rig(profile, behavior)
    run = tmp_path / "run"
    result = run_ranging(rig, run, recorder)
    expected = holder["expected"]
    assert recorder.calls == [expected[0], expected[1], expected[0]]
    actions = [event["action"] for event in result["range_control"]["events"]]
    assert actions == ["start", "step_down", "step_up"]
    assert result["range_control"]["increase_count"] == 1
    assert result["read_retry_count"] == 0 and len(rows_of(run)) == 401
    assert all(row["read_failures"] == "0" for row in rows_of(run))


def test_overflow_at_the_highest_range_stops_and_shuts_down(profile, tmp_path):
    recorder = Recorder()

    def behavior(rig, holder):
        if recorder.range == holder["expected"][0]:
            raise RuntimeError("Invalid current field: 'x'.")
        return decaying(rig)

    rig, holder = make_rig(profile, behavior)
    if holder["expected"][0] != RANGE_LADDER[-1]:
        pytest.skip("fixture compliance does not take over at the highest range")
    with pytest.raises(RangingError, match="highest"):
        run_ranging(rig, tmp_path / "run", recorder)
    assert metadata_of(tmp_path / "run")["state"] == "failed" and rig.events[-1] == "shutdown"


def test_other_fault_bits_are_never_treated_as_overflow(profile, tmp_path):
    recorder = Recorder()

    def behavior(rig, holder):
        if recorder.range == holder["expected"][1]:
            raise MeasurementStatusError(reading(rig.hold, 0.0, (1 << 13) | (1 << 4)))
        return decaying(rig)

    rig, holder = make_rig(profile, behavior)
    with pytest.raises(MeasurementStatusError):
        run_ranging(rig, tmp_path / "run", recorder)
    assert recorder.calls == holder["expected"][:2] and rig.events[-1] == "shutdown"


def test_too_many_range_increases_stop_the_run(profile, tmp_path):
    recorder = Recorder()

    def behavior(rig, holder):
        if recorder.range == holder["expected"][1]:
            raise RuntimeError("Invalid current field: 'x'.")
        return decaying(rig)

    rig, holder = make_rig(profile, behavior, maximum_range_increases=0)
    with pytest.raises(RangingError, match="range increases"):
        run_ranging(rig, tmp_path / "run", recorder)


def test_range_setter_failure_stops_and_shuts_down(profile, tmp_path):
    def failing(smu, channel, range_a):
        raise RangingError("boom")

    rig, holder = make_rig(profile)
    with pytest.raises(RangingError, match="boom"):
        run_ranging(rig, tmp_path / "run", failing)
    assert rig.events[-1] == "shutdown" and not rig.smu.output_on
    assert not any(isinstance(event, tuple) for event in rig.events)


def test_auto_mode_never_changes_the_range(profile, tmp_path):
    recorder = Recorder()
    rig = Rig(small_doc(profile))
    run = tmp_path / "run"
    result = run_ranging(rig, run, recorder)
    assert recorder.calls == [] and result["range_control"]["mode"] == "auto"
    assert {row["range_a"] for row in rows_of(run)} == {""}


def test_missing_range_setter_is_refused_before_anything_happens(profile, tmp_path):
    rig, holder = make_rig(profile)
    with pytest.raises(ValueError, match="set_range"):
        run_ranging(rig, tmp_path / "run", None)
    assert rig.events == [] and not (tmp_path / "run").exists()


def test_range_stays_at_the_start_range_while_the_source_stays_in_compliance(profile, tmp_path):
    recorder = Recorder()
    rig, holder = make_rig(profile, lambda r, h: reading(r.hold, r.compliance, status=2))
    result = run_ranging(rig, tmp_path / "run", recorder)
    assert recorder.calls == holder["expected"][:1] and result["state"] == "completed"


def controller(**control):
    return StepDownRanger(validate_range_control({"mode": "step_down", "confirm_samples": 3,
                                                  "headroom": 0.5, "confirm_window_s": 0, **control}))


def test_takeover_picks_the_smallest_rung_with_headroom():
    assert controller().takeover(0.395) == 1.0
    assert controller().takeover(-0.05) == 0.1
    assert controller().takeover(5e-9) == 1e-7


def test_step_down_needs_a_full_confirmation_window_without_peaks():
    ranger = controller()
    ranger.takeover(0.395)
    assert [ranger.observe(0.04, t) for t in range(2)] == [None, None]
    assert ranger.observe(0.2, 2) is None
    assert [ranger.observe(0.04, t) for t in (3, 4)] == [None, None]
    assert ranger.observe(0.04, 5) == 0.1


def test_step_down_never_goes_up_and_can_skip_to_the_lowest_fitting_rung():
    ranger = controller()
    ranger.takeover(0.395)
    assert [ranger.observe(0.9, t) for t in range(5)] == [None] * 5 and ranger.range_a == 1.0
    assert [ranger.observe(4e-4, t) for t in range(3)] == [None, None, 0.001]


def test_step_down_respects_the_minimum_range():
    ranger = controller(minimum_range_a=1e-4)
    ranger.takeover(0.395)
    results = [ranger.observe(1e-9, t) for t in range(3)]
    assert results[-1] == 1e-4
    assert [ranger.observe(1e-9, t) for t in range(10, 20)] == [None] * 10


def test_overflow_steps_up_one_rung_and_locks_out_step_downs():
    ranger = controller(overflow_lockout_s=100)
    ranger.takeover(0.005)
    assert ranger.range_a == 0.01 and ranger.overflow(10) == 0.1
    assert [ranger.observe(1e-6, 20 + t) for t in range(3)] == [None] * 3
    assert ranger.observe(1e-6, 200) == 1e-5


def test_overflow_limits():
    ranger = controller()
    ranger.takeover(0.9)
    with pytest.raises(RangingError, match="highest"):
        ranger.overflow(0)
    ranger = controller(maximum_range_increases=1)
    ranger.takeover(0.005)
    ranger.overflow(0)
    with pytest.raises(RangingError, match="range increases"):
        ranger.overflow(1)


class FakeScpi:
    def __init__(self, auto="0", readback="1E-3"):
        self.writes, self.auto, self.readback = [], auto, readback

    def _write(self, command):
        self.writes.append(command)

    def _query(self, command):
        return self.auto if command.endswith("AUTO?") else self.readback

    def check_errors(self):
        pass


def test_apply_current_range_sets_a_fixed_range_and_verifies_it():
    smu = FakeScpi()
    assert apply_current_range(smu, 1, 1e-3) == 1e-3
    assert smu.writes == [":SENS1:CURR:RANG:AUTO OFF", ":SENS1:CURR:RANG 0.0005"]
    assert apply_current_range(FakeScpi(readback="5E-4"), 2, 1e-3) == 5e-4


@pytest.mark.parametrize("kwargs", [{"auto": "1"}, {"readback": "1E-2"}, {"readback": "abc"}])
def test_apply_current_range_rejects_bad_readbacks(kwargs):
    with pytest.raises(RangingError):
        apply_current_range(FakeScpi(**kwargs), 1, 1e-3)


def test_range_control_defaults_to_automatic_ranging(profile):
    assert build_leakage_plan(small_doc(profile))["range_control"] == DEFAULTS


def test_step_down_profile_is_accepted(profile):
    control = build_leakage_plan(ranging_doc(profile))["range_control"]
    assert control["mode"] == "step_down" and control["minimum_range_a"] == 1e-4


@pytest.mark.parametrize("control", [
    {"mode": "fast"}, {"minimum_range_a": 2e-7}, {"headroom": 0}, {"headroom": 0.95},
    {"headroom": True}, {"confirm_samples": 0}, {"confirm_samples": True},
    {"confirm_samples": 1.5}, {"overflow_lockout_s": -1}, {"maximum_range_increases": -1},
    {"maximum_range_increases": True}, {"unknown": 1},
    {"confirm_window_s": -1}, {"confirm_window_s": 3601}, {"confirm_window_s": True},
])
def test_invalid_range_control_is_rejected(profile, control):
    with pytest.raises(ValueError):
        validate_config(ranging_doc(profile, **control))


def test_step_down_rejects_compliance_above_one_amp(profile):
    doc = ranging_doc(profile)
    doc["dut"]["ratings"]["maximum_continuous_charge_current_a"] = 3.0
    doc["charge"]["current"] = {"current_a": 1.5, "current_fraction": None, "c_rate": None}
    with pytest.raises(ValueError, match="up to 1 A"):
        validate_config(doc)


def test_execution_passes_the_range_setter(profile, tmp_path, monkeypatch):
    seen = {}

    class Session:
        def __init__(self, serial):
            self.identity = f"Keysight,B2902B,{serial},FW1"
            self.is_connected = True

        def close(self):
            pass

        def shutdown(self):
            pass

    def runner(document, **kwargs):
        seen.update(kwargs)
        return {"state": "completed", "recorded_sample_count": 1, "planned_sample_count": 1,
                "elapsed_s": 1.0, "read_retry_count": 0, "shutdown_verified": True,
                "early_stop": {"triggered": False}}

    doc = leakage_profile(profile)
    doc["output"]["root_directory"] = str(tmp_path / "runs")
    monkeypatch.setattr(leakage_execution, "open_owned_session",
                        lambda request, document, *, timeout_ms, visa_library:
                        Session(settings_for(document)["expected_serial"]))
    monkeypatch.setattr(leakage_execution, "run_leakage_test", runner)
    assert leakage_execution.run_configured_leakage(doc, "20261127_090000_leak") == 0
    assert seen["set_range"] is apply_current_range


def test_analysis_counts_range_changes_and_reports_final_window_voltage(profile, tmp_path):
    recorder = Recorder()
    rig, holder = make_rig(profile)
    run = tmp_path / "run"
    run_ranging(rig, run, recorder)
    result = analyze_leakage_run(run, tmp_path / "out")
    changes = len(holder["expected"]) - 1
    assert result["valid"] and result["range_changes"] == {"total": changes, "in_final_window": 0}
    assert result["range_change_samples_excluded"] == changes
    assert result["warnings"] == []
    window = result["final_window"]
    assert window["mean_voltage_v"] == pytest.approx(rig.hold)
    assert window["minimum_voltage_v"] <= window["maximum_voltage_v"]


def test_analysis_flags_automatic_ranging_and_accepts_legacy_csv(profile, tmp_path):
    rig = Rig(small_doc(profile, report_times_s=[2, 6, 10]))
    run = tmp_path / "run"
    rig.run(run)
    with (run / CSV_NAME).open(newline="", encoding="utf-8") as handle:
        rows = [row[:-2] for row in csv.reader(handle)]
    with (run / CSV_NAME).open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(rows)
    meta = metadata_of(run)
    meta["csv_sha256"] = None
    (run / METADATA_NAME).write_text(json.dumps(meta), encoding="utf-8")
    result = analyze_leakage_run(run, tmp_path / "out")
    assert result["valid"] and result["range_changes"] == {"total": 0, "in_final_window": 0}
    assert any("Automatic current ranging" in warning for warning in result["warnings"])


def test_default_range_control_waits_one_minute_with_wide_headroom():
    assert DEFAULTS["confirm_window_s"] == 60.0 and DEFAULTS["headroom"] == 0.2
def test_step_down_waits_for_the_whole_time_window():
    ranger = controller(confirm_window_s=60)
    ranger.takeover(0.395)
    assert [ranger.observe(1e-3, t) for t in (0, 15, 30, 45)] == [None] * 4
    assert ranger.observe(1e-3, 60) == 0.01
def test_a_peak_inside_the_time_window_blocks_the_step_down_until_it_leaves():
    ranger = controller(confirm_window_s=60)
    ranger.takeover(0.395)
    results = {t: ranger.observe(0.3 if t == 10 else 1e-3, t) for t in range(0, 90, 10)}
    assert all(results[t] is None for t in range(0, 80, 10))
    assert results[80] == 0.01
def test_a_wandering_current_never_steps_below_the_range_it_can_overflow():
    ranger = StepDownRanger(validate_range_control({"mode": "step_down"}))
    ranger.takeover(0.395)
    steps = [ranger.observe(1e-3 * abs(math.sin(0.9 * k)), 0.25 * k) for k in range(3600)]
    assert [step for step in steps if step] == [0.01]
    quiet = [ranger.observe(1e-5, 0.25 * k) for k in range(3600, 4400)]
    assert ranger.range_a == 1e-4 and all(step < 0.01 for step in quiet if step)
