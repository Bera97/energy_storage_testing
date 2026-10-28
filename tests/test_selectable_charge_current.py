"""Offline coverage for selectable charging current."""
from copy import deepcopy
import pytest
from experiments.config import load_config, validate_config
import test_experiment_config as config_tests
@pytest.fixture
def case():
    fixture = config_tests.ExperimentConfigTests()
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.doCleanups()
def resolve(case, selection, category="battery"):
    document = deepcopy(case.document)
    document["dut"]["category"] = category
    document["charge"]["current"] = selection
    return validate_config(document)
@pytest.mark.parametrize("category", ["battery", "edlc", "lic"])
@pytest.mark.parametrize("selection", [
    {
        "current_a": 0.5,
        "current_fraction": None,
        "c_rate": None,
    },
    {"current_fraction": 0.25},
])
def test_absolute_and_fraction_choices(case, category, selection):
    result = resolve(case, selection, category)
    assert result["resolved_currents"]["charge_current_a"] == pytest.approx(0.5)
    assert result["resolved_currents"]["discharge_current_a"] == pytest.approx(2.0)
    assert result["resolved_currents"]["charge_termination_current_a"] == pytest.approx(0.1)
    assert result["config"]["charge"]["current"] == selection
def test_battery_c_rate(case):
    result = resolve(case, {"c_rate": 0.25})
    assert result["resolved_currents"]["charge_current_a"] == pytest.approx(0.5)
@pytest.mark.parametrize("category", ["edlc", "lic"])

def test_c_rate_supported_for_capacitors(case, category):
    result = resolve(case, {"c_rate": 0.25}, category)
    assert result["resolved_currents"]["charge_current_a"] == pytest.approx(0.5)
@pytest.mark.parametrize("selection", [
    {},
    {"current_a": None, "current_fraction": None, "c_rate": None},
    {"current_a": 0.5, "current_fraction": 0.25},
    {"current_a": 0.5, "c_rate": 0.25},
    {"current_fraction": 0.25, "c_rate": 0.25},
    {"current_A": 0.5},
    {"unexpected": 0.5},
    {"basis": "capacity_c_rate", "factor": 0.25, "current_a": 0.5},
    {"basis": "capacity_c_rate"},
    [],
])
def test_invalid_selections_rejected(case, selection):
    with pytest.raises(ValueError):
        resolve(case, selection)
@pytest.mark.parametrize("field", [
    "current_a", "current_fraction", "c_rate",
])
@pytest.mark.parametrize("value", [
    True, False, 0, -1, "0.5",
    float("nan"), float("inf"), -float("inf"),
])
def test_invalid_selection_values(case, field, value):
    with pytest.raises(ValueError):
        resolve(case, {field: value})
@pytest.mark.parametrize("selection", [
    {"current_a": 2.1},
    {"current_fraction": 1.1},
    {"c_rate": 2.0},
])
def test_rating_cannot_be_exceeded(case, selection):
    with pytest.raises(ValueError):
        resolve(case, selection)
def test_c_rate_requires_capacity(case):
    case.document["dut"]["nominal_capacity_ah"] = None
    with pytest.raises(ValueError, match="nominal_capacity_ah"):
        resolve(case, {"c_rate": 0.25})
def test_termination_still_must_be_below_charge_current(case):
    with pytest.raises(ValueError, match="strictly below"):
        resolve(case, {"current_a": 0.05})
def test_yaml_loading_and_input_preservation(case):
    selection = {
        "current_a": 0.5,
        "current_fraction": None,
        "c_rate": None,
    }
    case.document["charge"]["current"] = selection
    before = deepcopy(case.document)
    case.save()
    result = load_config(case.path)
    assert result["resolved_currents"]["charge_current_a"] == pytest.approx(0.5)
    assert result["config"] == before
    assert case.document == before
def test_configured_current_termination_reference(case):
    case.document["charge"]["termination"]["current_reference"] = (
        "configured_charge_current"
    )
    result = resolve(case, {"current_a": 0.5})
    assert result["resolved_currents"]["charge_termination_current_a"] == pytest.approx(0.025)
