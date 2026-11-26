"""Offline CP acknowledgement precision regressions from the failed bench log."""
import math
import pytest
from experiments.constant_power import (
    ConstantPowerController, PowerLimits, PowerSample,
    PowerFeedbackError, PowerTimeoutError,
)

PAIRS = [
    (-0.10583130489998943, -0.105831305),
    (-0.10624028897358602, -0.106240289),
    (-0.10629957878791906, -0.106299579),
]

def controller(requested=-0.10629957878791906):
    limits = PowerLimits(power_w=0.4, cutoff_voltage_v=2.2,
        minimum_voltage_v=2.19, maximum_voltage_v=3.81,
        maximum_current_a=0.4, maximum_duration_s=10,
        update_interval_s=0.2, maximum_feedback_age_s=0.5,
        relative_tolerance=0.05, absolute_tolerance_w=0.005,
        startup_grace_s=1, maximum_tracking_error_s=1)
    control = ConstantPowerController(limits)
    decision = control.start(now_s=0,
        sample=PowerSample(0, -limits.power_w/requested, 0),
        outputs_verified_off=True)
    assert math.isclose(decision.current_setpoint_a, requested, rel_tol=1e-14)
    return control

@pytest.mark.parametrize("requested,readback", PAIRS)
def test_recorded_rounding_pairs_are_acknowledged(requested, readback):
    control = controller(requested)
    control.acknowledge_setpoint(now_s=0.1, readback_current_a=readback)
    assert control.state == "active"
    assert control.pending_current_a is None
    assert control.last_ack_s == 0.1


def test_original_comparison_reproduces_recorded_failure():
    requested, readback = PAIRS[-1]
    assert not math.isclose(readback, requested, rel_tol=1e-9, abs_tol=1e-12)

@pytest.mark.parametrize("factor,accepted", [(0.5, True), (2, False)])
def test_acknowledgement_has_a_narrow_relative_bound(factor, accepted):
    control = controller()
    requested = control.pending_current_a
    readback = requested + factor * abs(requested) * 1e-8
    if accepted:
        control.acknowledge_setpoint(now_s=0.1, readback_current_a=readback)
        assert control.state == "active"
    else:
        with pytest.raises(PowerFeedbackError):
            control.acknowledge_setpoint(now_s=0.1, readback_current_a=readback)
        assert control.state == "failed"

@pytest.mark.parametrize("readback", [0, 0.1063, -0.400000001, -0.106,
    -0.105, -0.11, -0.1062995])
def test_wrong_polarity_ceiling_and_material_mismatches_still_fail(readback):
    control = controller()
    with pytest.raises(PowerFeedbackError):
        control.acknowledge_setpoint(now_s=0.1, readback_current_a=readback)
    assert control.state == "failed"

@pytest.mark.parametrize("readback", [float("nan"), float("inf"), None, True])
def test_invalid_readbacks_still_fail(readback):
    control = controller()
    with pytest.raises(ValueError):
        control.acknowledge_setpoint(now_s=0.1, readback_current_a=readback)
    assert control.state == "failed"


def test_rounding_acceptance_does_not_bypass_stale_feedback_deadline():
    control = controller()
    with pytest.raises(PowerTimeoutError):
        control.acknowledge_setpoint(now_s=0.501, readback_current_a=PAIRS[-1][1])
    assert control.state == "failed"


def test_rounding_acceptance_preserves_feedback_provenance_check():
    control = controller()
    control.acknowledge_setpoint(now_s=0.1, readback_current_a=PAIRS[-1][1])
    with pytest.raises(PowerFeedbackError, match="predates"):
        control.update(now_s=0.2, sample=PowerSample(0.05, 3.76, PAIRS[-1][1]))
    assert control.state == "failed"
