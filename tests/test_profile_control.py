"""Profile discharge controller: pure decisions, no I/O."""
import json
import pytest
from experiments.constant_power import (
    PowerFeedbackError, PowerLimits, PowerSample, PowerTrackingError,
)
from experiments.profile_control import ProfileController, ProfileLimits, profile_value_at
STEPS = ((2, 0.1), (3, 0.0), (1, 0.2))
def limits(kind="current", steps=STEPS, **changes):
    values = dict(power_w=0.8, cutoff_voltage_v=3.4, minimum_voltage_v=2.19, maximum_voltage_v=3.81,
                  maximum_current_a=0.4, maximum_duration_s=3600.0, update_interval_s=1.0,
                  maximum_feedback_age_s=2.5, relative_tolerance=0.05, absolute_tolerance_w=0.005,
                  startup_grace_s=1.0, maximum_tracking_error_s=2.0, profile_kind=kind,
                  profile_steps=steps, zero_current_tolerance_a=0.002)
    values.update(changes)
    return ProfileLimits(**values)
def arm(controller):
    controller.arm_zero_current_probe(now_s=0.0, outputs_verified_off=True, current_source_verified=True,
                                      zero_current_setpoint_a=0.0, maximum_probe_current_a=0.001)
    controller.confirm_zero_current_probe_enabled(now_s=0.125, output_reported_on=True)
def drive(controller, ticks, voltage=lambda t: 3.7, follow=1.0, noise=0.0):
    arm(controller)
    command, decisions, t = 0.0, [], 0.5
    for _ in range(ticks):
        probing = controller.state == "probing"
        current = command * follow if command else (0.0 if probing else noise)
        sample = PowerSample(t, voltage(t), current)
        method = controller.start_from_zero_current_probe if probing else controller.update
        decision = method(now_s=t + 0.25, sample=sample)
        decisions.append(decision)
        if decision.action == "stop":
            break
        if decision.action == "set_current":
            command = decision.current_setpoint_a
            controller.acknowledge_setpoint(now_s=t + 0.5, readback_current_a=command)
        t += 1.0
    return decisions
def test_current_profile_steps_including_zero_and_repeats():
    decisions = drive(ProfileController(limits()), 9)
    assert [(d.action, d.current_setpoint_a) for d in decisions] == [
        ("set_current", -0.1), ("hold", None), ("set_current", 0.0), ("hold", None), ("hold", None),
        ("set_current", -0.2), ("set_current", -0.1), ("hold", None), ("set_current", 0.0)]
    assert [d.profile_step for d in decisions] == [0, 0, 1, 1, 1, 2, 0, 0, 1]
    assert [d.profile_elapsed_s for d in decisions] == [float(k) for k in range(9)]
    assert decisions[0].requested_value == 0.1
def test_power_profile_commands_power_over_voltage_each_second_and_zero_when_idle():
    decisions = drive(ProfileController(limits("power", ((2, 0.4), (2, 0.0)))), 6)
    expected = -0.4 / 3.7
    assert [d.action for d in decisions] == ["set_current", "set_current", "set_current", "hold",
                                             "set_current", "set_current"]
    assert [d.current_setpoint_a for d in decisions] == [
        pytest.approx(expected), pytest.approx(expected), 0.0, None,
        pytest.approx(expected), pytest.approx(expected)]
def test_voltage_cutoff_stops_in_any_step():
    controller = ProfileController(limits())
    decisions = drive(controller, 10, voltage=lambda t: 3.7 if t < 4 else 3.4)
    assert len(decisions) == 5 and decisions[-1].action == "stop"
    assert decisions[-1].stop_reason == "voltage_min" and controller.state == "completed"
    assert decisions[-1].profile_step == 1
def test_tracking_failure_is_detected_against_the_setpoint_in_force():
    with pytest.raises(PowerTrackingError):
        drive(ProfileController(limits("current", ((10, 0.1),))), 8, follow=0.5)
def test_zero_step_tolerates_noise_but_not_charging_current():
    steps = ((2, 0.1), (3, 0.0))
    assert drive(ProfileController(limits("current", steps)), 7, noise=0.0015)
    with pytest.raises(PowerFeedbackError, match="polarity"):
        drive(ProfileController(limits("current", steps)), 7, noise=0.01)
@pytest.mark.parametrize("changes", [
    dict(profile_kind="voltage"), dict(profile_steps=()), dict(profile_steps=((0, 0.1),)),
    dict(profile_steps=((1.5, 0.1),)), dict(profile_steps=((1, -0.1),)), dict(profile_steps=((1, 0.0),)),
    dict(profile_steps=((1, 0.5),)), dict(update_interval_s=1.5, maximum_feedback_age_s=3.0),
    dict(zero_current_tolerance_a=0.0), dict(profile_steps=((1, True),)), dict(profile_steps=("ab",)),
])
def test_invalid_limits_are_rejected(changes):
    with pytest.raises(ValueError):
        limits(**changes)
def test_power_step_must_fit_the_current_ceiling_at_cutoff():
    with pytest.raises(ValueError, match="ceiling"):
        limits("power", ((1, 2.0),))
def test_limits_round_trip_through_json():
    original = limits()
    stored = json.loads(json.dumps(original.to_dict()))
    assert stored["profile_steps"] == [[2, 0.1], [3, 0.0], [1, 0.2]]
    assert ProfileLimits(**stored) == original
def test_profile_value_at_repeats():
    assert profile_value_at(STEPS, 0.0) == (0, 0.1)
    assert profile_value_at(STEPS, 1.999) == (0, 0.1)
    assert profile_value_at(STEPS, 2.0) == (1, 0.0)
    assert profile_value_at(STEPS, 5.0) == (2, 0.2)
    assert profile_value_at(STEPS, 6.0) == (0, 0.1)
    assert profile_value_at(STEPS, 12.5) == (0, 0.1)
def test_decisions_replay_identically():
    first = [d.to_dict() for d in drive(ProfileController(limits()), 9)]
    second = [d.to_dict() for d in drive(ProfileController(limits()), 9)]
    assert first == second and "profile_step" in first[0]
def test_acknowledgement_must_match_the_command_including_zero():
    controller = ProfileController(limits())
    arm(controller)
    decision = controller.start_from_zero_current_probe(now_s=0.75, sample=PowerSample(0.5, 3.7, 0.0))
    assert decision.action == "set_current"
    with pytest.raises(PowerFeedbackError, match="mismatch"):
        controller.acknowledge_setpoint(now_s=1.0, readback_current_a=0.0)
def test_controller_requires_profile_limits():
    plain = PowerLimits(power_w=0.8, cutoff_voltage_v=3.4, minimum_voltage_v=2.19, maximum_voltage_v=3.81,
                        maximum_current_a=0.4, maximum_duration_s=3600.0, update_interval_s=1.0,
                        maximum_feedback_age_s=2.5, relative_tolerance=0.05, absolute_tolerance_w=0.005,
                        startup_grace_s=1.0, maximum_tracking_error_s=2.0)
    with pytest.raises(ValueError, match="ProfileLimits"):
        ProfileController(plain)
