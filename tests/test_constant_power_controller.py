"""Pure control tests; no driver, VISA, filesystem or sleep dependency."""
from dataclasses import replace
import json
import pytest
from experiments.constant_power import (
    PowerLimits, PowerSample, ConstantPowerController, PowerControlError,
    PowerFeedbackError, PowerTimeoutError, PowerTrackingError,
)


def limits(**changes):
    values = dict(power_w=0.6, cutoff_voltage_v=2.2, minimum_voltage_v=2.19,
                  maximum_voltage_v=3.81, maximum_current_a=0.4,
                  maximum_duration_s=10, update_interval_s=0.2,
                  maximum_feedback_age_s=0.5, relative_tolerance=0.05,
                  absolute_tolerance_w=0.005, startup_grace_s=1,
                  maximum_tracking_error_s=1)
    values.update(changes)
    return PowerLimits(**values)


def start(**changes):
    control = ConstantPowerController(limits(**changes))
    decision = control.start(now_s=0, sample=PowerSample(0, 3.0, 0), outputs_verified_off=True)
    control.acknowledge_setpoint(now_s=0, readback_current_a=decision.current_setpoint_a)
    return control, decision


def feed(control, time, voltage=3.0, current=None, **flags):
    current = -control.limits.power_w / voltage if current is None else current
    decision = control.update(now_s=time, sample=PowerSample(time, voltage, current, **flags))
    if decision.action == "set_current":
        control.acknowledge_setpoint(now_s=time, readback_current_a=decision.current_setpoint_a)
    return decision


def test_negative_current_tracks_power_as_voltage_falls():
    control, decision = start()
    assert decision.current_setpoint_a == pytest.approx(-0.2)
    decision = feed(control, 0.25, 2.5, -0.2)
    assert decision.current_setpoint_a == pytest.approx(-0.24)
    assert decision.measured_power_w == pytest.approx(0.5)
    assert json.loads(json.dumps(decision.to_dict(), allow_nan=False)) == decision.to_dict()


def test_update_cadence_holds_between_setpoint_updates():
    control, _ = start()
    assert feed(control, 0.1).action == "hold"
    assert feed(control, 0.25).action == "set_current"


def test_cutoff_is_successful_stop_with_no_further_setpoint():
    control, _ = start()
    decision = feed(control, 0.25, 2.2, -0.2)
    assert decision.action == "stop" and decision.stop_reason == "voltage_min"
    assert decision.current_setpoint_a is None
    assert control.state == "completed"
    with pytest.raises(PowerControlError):
        control.update(now_s=0.3, sample=PowerSample(0.3, 2.2, -0.2))
    assert control.state == "completed"


@pytest.mark.parametrize("value", [None, True, 0, -1, "0.6", float("nan"), float("inf"), 10**1000])
def test_invalid_power_rejected(value):
    with pytest.raises(ValueError):
        limits(power_w=value)


@pytest.mark.parametrize("changes", [
    {"power_w": 0.9}, {"cutoff_voltage_v": 0}, {"maximum_current_a": 0},
    {"maximum_voltage_v": 2.2}, {"minimum_voltage_v": 2.3},
    {"relative_tolerance": 1}, {"update_interval_s": 0.5},
    {"maximum_duration_s": 2}, {"absolute_tolerance_w": -1},
])
def test_invalid_limits_rejected(changes):
    with pytest.raises(ValueError):
        limits(**changes)


@pytest.mark.parametrize("flag", [False, None, 1, "true"])
def test_start_requires_explicit_outputs_off_evidence(flag):
    control = ConstantPowerController(limits())
    with pytest.raises(PowerControlError):
        control.start(now_s=0, sample=PowerSample(0, 3, 0), outputs_verified_off=flag)
    assert control.state == "failed"


def test_start_already_at_cutoff_does_not_request_output():
    control = ConstantPowerController(limits())
    decision = control.start(now_s=0, sample=PowerSample(0, 2.2, 0), outputs_verified_off=True)
    assert decision.action == "stop" and control.state == "completed"


@pytest.mark.parametrize("sample", [
    PowerSample(0, 3.0, -0.2), PowerSample(0.3, 3.0, -0.2),
    PowerSample(0.1, 3.82, -0.2), PowerSample(0.1, 2.18, -0.2),
    PowerSample(0.1, 3, -0.41), PowerSample(0.1, 3, 0.01),
    PowerSample(0.1, 3, -0.2, current_source=False),
    PowerSample(0.1, 3, -0.2, in_compliance=True),
    PowerSample(0.1, 3, -0.2, has_fault=True),
    PowerSample(0.1, 3, -0.2, has_fault=1),
    PowerSample(0.1, float("nan"), -0.2),
])
def test_invalid_feedback_is_terminal(sample):
    control, _ = start()
    with pytest.raises((PowerControlError, ValueError)):
        control.update(now_s=0.2, sample=sample)
    assert control.state == "failed"


def test_no_feedback_watchdog_and_phase_duration_deadline():
    control, _ = start()
    with pytest.raises(PowerTimeoutError):
        control.watchdog(now_s=0.501)
    control, _ = start(maximum_duration_s=2.1)
    for time in (0.4, 0.8, 1.2, 1.6, 2.0):
        feed(control, time)
    with pytest.raises(PowerTimeoutError):
        control.watchdog(now_s=2.1)


def test_pending_current_requires_acknowledgement():
    control = ConstantPowerController(limits())
    control.start(now_s=0, sample=PowerSample(0, 3, 0), outputs_verified_off=True)
    with pytest.raises(PowerControlError):
        control.update(now_s=0.1, sample=PowerSample(0.1, 3, -0.2))


@pytest.mark.parametrize("current", [0.2, -0.3, 0, float("nan")])
def test_wrong_setpoint_readback_fails(current):
    control = ConstantPowerController(limits())
    control.start(now_s=0, sample=PowerSample(0, 3, 0), outputs_verified_off=True)
    with pytest.raises((PowerFeedbackError, ValueError)):
        control.acknowledge_setpoint(now_s=0.1, readback_current_a=current)
    assert control.state == "failed"


def test_feedback_must_not_predate_acknowledgement():
    control = ConstantPowerController(limits())
    first = control.start(now_s=0, sample=PowerSample(0, 3, 0), outputs_verified_off=True)
    control.acknowledge_setpoint(now_s=0.15, readback_current_a=first.current_setpoint_a)
    with pytest.raises(PowerFeedbackError, match="predates"):
        control.update(now_s=0.2, sample=PowerSample(0.1, 3, -0.2))


def test_bad_tracking_is_bounded_and_is_not_silently_accepted():
    control, _ = start(startup_grace_s=0)
    for time in (0.25, 0.5, 0.75, 1.0):
        feed(control, time, current=-0.05)
    with pytest.raises(PowerTrackingError):
        feed(control, 1.25, current=-0.05)
    assert control.state == "failed"


def test_tracking_recovery_resets_error_window():
    control, _ = start(startup_grace_s=0)
    feed(control, 0.25, current=-0.05)
    feed(control, 0.5)
    feed(control, 0.75, current=-0.05)
    feed(control, 1.0, current=-0.05)
    feed(control, 1.25)
    assert control.bad_tracking_since_s is None


def test_backward_host_clock_is_terminal():
    control, _ = start()
    feed(control, 0.25)
    with pytest.raises(PowerFeedbackError, match="backwards"):
        control.watchdog(now_s=0.2)


def test_fault_takes_priority_over_cutoff():
    control, _ = start()
    with pytest.raises(PowerFeedbackError, match="fault"):
        feed(control, 0.25, 2.2, -0.2, has_fault=True)


def test_limits_are_immutable_and_serializable():
    original = limits()
    changed = replace(original, power_w=0.4)
    assert original.power_w == 0.6 and changed.power_w == 0.4
    assert json.loads(json.dumps(original.to_dict(), allow_nan=False)) == original.to_dict()


def test_late_tracking_recovery_does_not_erase_expired_deadline():
    control, _ = start(startup_grace_s=0)
    for time in (0.25, 0.5, 0.75, 1.0):
        feed(control, time, current=-0.05)
    with pytest.raises(PowerTrackingError):
        feed(control, 1.25)
    assert control.state == "failed"


def test_overflow_in_terminal_sample_is_failure_not_completion():
    control = ConstantPowerController(limits(
        power_w=1, minimum_voltage_v=0, cutoff_voltage_v=1.6e308,
        maximum_voltage_v=1.7e308, maximum_current_a=2))
    with pytest.raises(ValueError):
        control.start(now_s=0, sample=PowerSample(0, 1.6e308, 2), outputs_verified_off=True)
    assert control.state == "failed"
