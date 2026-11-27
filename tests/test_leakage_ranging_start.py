"""Range handling at the start and end of a step-down hold."""
from datetime import datetime, timezone
import pytest

import test_charge_current_sweep
from acquisition.spot_acquisition import MeasurementStatusError
from experiments import leakage_execution
from experiments.leakage_ranging import RangingError, enable_auto_range
from experiments.leakage_runner import LeakageRunError, run_leakage_test
from experiments.settings import settings_for
from test_leakage_planning import leakage_profile
from test_leakage_ranging import (
    FakeScpi, Recorder, decaying, make_rig, metadata_of, ranging_doc, rows_of,
)
from test_leakage_runner import Rig, reading, small_doc

profile = test_charge_current_sweep.profile
OVERFLOW = "Invalid current field: '+9.900000E+37'."


class Restore:
    def __init__(self, error=None):
        self.calls = []
        self.outputs_on = []
        self.error = error

    def __call__(self, smu, channel):
        self.calls.append(channel)
        self.outputs_on.append(smu.output_on)
        if self.error is not None:
            raise self.error


def run_with(rig, run, set_range, restore):
    return run_leakage_test(
        rig.document, smu=rig.smu, maximum_voltage_v=rig.vmax,
        maximum_current_a=rig.compliance, configure_source=rig.configure_source,
        spot_factory=rig.spot_factory, enable_output=rig.enable_output,
        set_range=set_range, restore_auto_range=restore, run_directory=run,
        clock=rig.timing.clock, sleep=rig.timing.sleep,
        utc_now=lambda: datetime(2026, 11, 27, 9, 0, 0, tzinfo=timezone.utc),
    )


def always_overflow(rig):
    raise RuntimeError(OVERFLOW)


@pytest.mark.parametrize("kind", ["invalid_field", "status_flag"])
def test_overflow_on_the_first_reading_takes_manual_control_at_the_compliance_range(profile, tmp_path, kind):
    recorder = Recorder()

    def behavior(rig, holder):
        if recorder.range is None:
            if kind == "invalid_field":
                raise RuntimeError(OVERFLOW)
            raise MeasurementStatusError(reading(rig.hold, 0.0, 1 << 13))
        return decaying(rig)

    rig, holder = make_rig(profile, behavior)
    run = tmp_path / "run"
    result = run_with(rig, run, recorder, None)
    events = result["range_control"]["events"]
    assert recorder.calls[0] == 1.0 and events[0]["action"] == "start_overflow"
    assert {event["action"] for event in events[1:]} == {"step_down"}
    assert result["state"] == "completed" and result["read_retry_count"] == 0
    rows = rows_of(run)
    assert len(rows) == 401 and float(rows[0]["range_a"]) == 1.0
    assert all(row["read_failures"] == "0" for row in rows)


def test_auto_mode_still_stops_after_repeated_overflow_at_the_first_sample(profile, tmp_path):
    recorder = Recorder()
    rig = Rig(small_doc(profile), always_overflow)
    run = tmp_path / "run"
    with pytest.raises(LeakageRunError, match="3 consecutive"):
        run_with(rig, run, recorder, Restore())
    assert recorder.calls == [] and metadata_of(run)["state"] == "failed"
    assert rig.events[-1] == "shutdown"


def test_auto_ranging_is_restored_after_the_outputs_are_off(profile, tmp_path):
    recorder, restore = Recorder(), Restore()
    rig, holder = make_rig(profile)
    run = tmp_path / "run"
    result = run_with(rig, run, recorder, restore)
    assert restore.calls == [1] and restore.outputs_on == [False]
    assert result["range_control"]["auto_range_restored"] is True
    assert metadata_of(run)["range_control"]["auto_range_restored"] is True


def test_auto_ranging_is_restored_after_a_failed_run(profile, tmp_path):
    recorder, restore = Recorder(), Restore()

    def behavior(rig, holder):
        if recorder.range == holder["expected"][1]:
            raise MeasurementStatusError(reading(rig.hold, 0.0, 1 << 4))
        return decaying(rig)

    rig, holder = make_rig(profile, behavior)
    run = tmp_path / "run"
    with pytest.raises(MeasurementStatusError):
        run_with(rig, run, recorder, restore)
    assert restore.calls == [1] and restore.outputs_on == [False]
    assert metadata_of(run)["range_control"]["auto_range_restored"] is True


def test_restore_is_not_attempted_when_no_range_was_fixed(profile, tmp_path):
    restore = Restore()
    run_with(Rig(small_doc(profile)), tmp_path / "auto", Recorder(), restore)
    rig, holder = make_rig(profile, lambda r, h: reading(r.hold, r.compliance, status=2))
    run_with(rig, tmp_path / "compliance", Recorder(), restore)
    assert restore.calls == []


def test_restore_failure_is_recorded_but_does_not_fail_the_run(profile, tmp_path):
    restore = Restore(RangingError("no readback"))
    rig, holder = make_rig(profile)
    run = tmp_path / "run"
    result = run_with(rig, run, Recorder(), restore)
    assert result["state"] == "completed"
    control = metadata_of(run)["range_control"]
    assert control["auto_range_restored"] is False
    assert control["restore_error"]["message"] == "no readback"


def test_restore_is_skipped_when_shutdown_cannot_be_verified(profile, tmp_path):
    restore = Restore()
    rig = Rig(ranging_doc(profile), decaying, fail_shutdown=True)
    with pytest.raises(LeakageRunError, match="Shutdown"):
        run_with(rig, tmp_path / "run", Recorder(), restore)
    assert restore.calls == []


def test_enable_auto_range_switches_auto_on_and_verifies_it():
    smu = FakeScpi(auto="1")
    assert enable_auto_range(smu, 2) is None
    assert smu.writes == [":SENS2:CURR:RANG:AUTO ON"]
    assert enable_auto_range(FakeScpi(auto="ON"), 1) is None
    with pytest.raises(RangingError, match="ON"):
        enable_auto_range(FakeScpi(auto="0"), 1)


def test_execution_passes_the_restore_hook(profile, tmp_path, monkeypatch):
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
    assert seen["restore_auto_range"] is enable_auto_range
