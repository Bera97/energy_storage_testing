"""Regression tests for signed NR1 Boolean responses."""

from unittest.mock import MagicMock

import pytest

from drivers.scpi import parse_scpi_bool
from drivers.spot_acquisition import SpotAcquisition


@pytest.mark.parametrize(
    "response, expected",
    [
        ("0", False),
        ("+0", False),
        ("-0", False),
        ("1", True),
        ("+1", True),
        (" +1\n", True),
        (" +0\r\n", False),
    ],
)
def test_nr1_boolean_response(response, expected):
    assert parse_scpi_bool(response, "TEST?") is expected


@pytest.mark.parametrize(
    "response",
    ["", "2", "-1", "1.0", "1e0", "ON", "OFF", "nan", "1,0"],
)
def test_invalid_boolean_response(response):
    with pytest.raises(RuntimeError, match="Invalid Boolean"):
        parse_scpi_bool(response, "TEST?")


def make_acquisition(voltage_state, current_state):
    smu = MagicMock()
    smu.chan1.get_output_state.return_value = True
    smu.chan1.get_auto_output_on.return_value = False

    responses = {
        ":FORM:DATA?": "ASC",
        ":FORM:ELEM:SENS?": "VOLT,CURR,TIME,STAT",
        ':SENS1:FUNC:STAT? "VOLT"': voltage_state,
        ':SENS1:FUNC:STAT? "CURR"': current_state,
        ":MEAS? (@1)": "1,0.0001443,0.25,0",
    }

    smu._query.side_effect = responses.__getitem__

    return smu, SpotAcquisition(smu, 1)


def test_signed_enabled_states_allow_acquisition():
    smu, acquisition = make_acquisition("+1", "+1")

    reading = acquisition.read()

    assert reading.voltage_v == 1.0
    assert reading.current_a == pytest.approx(0.0001443)
    smu._query.assert_any_call(":MEAS? (@1)")
    smu._write.assert_not_called()


@pytest.mark.parametrize(
    "voltage_state, current_state",
    [("+0", "+1"), ("+1", "+0")],
)
def test_signed_disabled_state_still_blocks_acquisition(
    voltage_state, current_state
):
    smu, acquisition = make_acquisition(
        voltage_state, current_state
    )

    with pytest.raises(RuntimeError, match="not enabled"):
        acquisition.read()

    commands = [
        item.args[0]
        for item in smu._query.call_args_list
    ]

    assert ":MEAS? (@1)" not in commands