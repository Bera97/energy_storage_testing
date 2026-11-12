"""Regression checks for shared numeric validation semantics."""
import pytest
from experiments.numeric import (
    finite_number,
    positive_number,
    positive_setting_number,
)
@pytest.mark.parametrize(
    "value",
    [True, False, None, "1", float("nan"), float("inf"), -float("inf"), 10**1000],
)
@pytest.mark.parametrize(
    "validator,message",
    [
        (finite_number, "value must be a finite number."),
        (positive_number, "value must be a finite number."),
        (positive_setting_number, "value must be finite and positive."),
    ],
)
def test_invalid_values_preserve_error_policy(value, validator, message):
    with pytest.raises(ValueError) as caught:
        validator(value, "value")
    assert str(caught.value) == message
@pytest.mark.parametrize("value", [0, -1, -0.5])
def test_finite_values_need_not_be_positive(value):
    assert finite_number(value, "value") == float(value)
@pytest.mark.parametrize("value", [0, -1, -0.5])
def test_positive_policies_preserve_range_messages(value):
    for validator in (
        lambda item, name: finite_number(item, name, positive=True),
        positive_number,
    ):
        with pytest.raises(ValueError) as caught:
            validator(value, "value")
        assert str(caught.value) == "value must be positive."
    with pytest.raises(ValueError) as caught:
        positive_setting_number(value, "value")
    assert str(caught.value) == "value must be finite and positive."
@pytest.mark.parametrize("value", [1, 0.5, 10])
def test_positive_inputs_return_floats(value):
    for validator in (finite_number, positive_number, positive_setting_number):
        result = validator(value, "value")
        assert result == float(value)
        assert type(result) is float
