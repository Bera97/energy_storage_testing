"""Offline tests for DC configuration and scalar measurements."""

from unittest.mock import MagicMock, call

import pytest

from drivers.keysight_b2902b import KeysightB2902B


@pytest.fixture
def rig(monkeypatch):
    instrument = MagicMock()

    responses = {
        "*IDN?": "Keysight Technologies,B2902B,TEST,TEST",
        ":SYST:ERR?": '+0,"No error"',
        ":FORM:DATA?": "ASC",
    }

    for channel in (1, 2):
        responses.update({
            f":OUTP{channel}?": "0",
            f":OUTP{channel}:ON:AUTO?": "0",
            f":SENS{channel}:VOLT:NPLC? MIN": "0.001",
            f":SENS{channel}:VOLT:NPLC? MAX": "100",
            f":SENS{channel}:VOLT:NPLC?": "1",
            f":SENS{channel}:REM?": "0",
            f":MEAS:VOLT? (@{channel})": "0.1",
            f":MEAS:CURR? (@{channel})": "-0.0001",
        })

    instrument.query.side_effect = responses.__getitem__

    manager = MagicMock()
    manager.open_resource.return_value = instrument

    monkeypatch.setattr(
        "drivers.keysight_b2902b.pyvisa.ResourceManager",
        MagicMock(return_value=manager),
    )

    with KeysightB2902B("USB0::TEST::INSTR") as smu:
        instrument.query.reset_mock()
        yield smu, instrument, responses


@pytest.mark.parametrize("number", [1, 2])
@pytest.mark.parametrize("function", ["VOLT", "CURR"])
def test_fixed_dc_commands(rig, number, function):
    smu, instrument, _ = rig
    channel = getattr(smu, f"chan{number}")

    channel.configure_fixed_dc(function)

    assert instrument.write.call_args_list == [
        call(f":SOUR{number}:FUNC:SHAP DC"),
        call(f":SOUR{number}:{function}:MODE FIX"),
        call(f":SOUR{number}:FUNC:MODE {function}"),
    ]


def test_dc_configuration_rejects_output_on(rig):
    smu, instrument, responses = rig
    responses[":OUTP1?"] = "1"

    with pytest.raises(RuntimeError, match="must be OFF"):
        smu.chan1.configure_fixed_dc("VOLT")

    instrument.write.assert_not_called()


@pytest.mark.parametrize("function", ["", "INVALID", None, True])
def test_invalid_source_function(rig, function):
    smu, instrument, _ = rig

    with pytest.raises(ValueError):
        smu.chan1.configure_fixed_dc(function)

    instrument.write.assert_not_called()


@pytest.mark.parametrize("enabled, state", [(True, "ON"), (False, "OFF")])
def test_four_wire_setting(rig, enabled, state):
    smu, instrument, _ = rig

    smu.chan1.set_four_wire(enabled)

    instrument.write.assert_called_once_with(
        f":SENS1:REM {state}"
    )


def test_nplc_setting(rig):
    smu, instrument, _ = rig

    smu.chan1.set_nplc(1)

    assert instrument.write.call_args_list == [
        call(":SENS1:VOLT:NPLC:AUTO OFF"),
        call(":SENS1:VOLT:NPLC 1"),
    ]


@pytest.mark.parametrize("value", [0, -1, 0.0001, 101, True, "1"])
def test_invalid_nplc_rejected(rig, value):
    smu, instrument, _ = rig

    with pytest.raises(ValueError):
        smu.chan1.set_nplc(value)

    instrument.write.assert_not_called()


def test_spot_configuration(rig):
    smu, instrument, _ = rig

    smu.chan1.configure_spot_measurement(1)

    assert instrument.write.call_args_list == [
        call(":SENS1:VOLT:NPLC:AUTO OFF"),
        call(":SENS1:VOLT:NPLC 1"),
        call(':SENS1:FUNC:OFF "RES"'),
        call(':SENS1:FUNC:ON "VOLT","CURR"'),
        call(":FORM:DATA ASC"),
    ]


@pytest.mark.parametrize("number", [1, 2])
@pytest.mark.parametrize(
    "method, quantity, expected",
    [
        ("measure_voltage", "VOLT", 0.1),
        ("measure_current", "CURR", -0.0001),
    ],
)
def test_measurement(rig, number, method, quantity, expected):
    smu, instrument, responses = rig
    responses[f":OUTP{number}?"] = "1"

    channel = getattr(smu, f"chan{number}")

    assert getattr(channel, method)() == pytest.approx(expected)

    instrument.query.assert_any_call(
        f":MEAS:{quantity}? (@{number})"
    )
    instrument.write.assert_not_called()


def test_measurement_refuses_output_off(rig):
    smu, instrument, _ = rig

    with pytest.raises(RuntimeError, match="enable output explicitly"):
        smu.chan1.measure_voltage()

    assert call(":MEAS:VOLT? (@1)") not in (
        instrument.query.call_args_list
    )


def test_measurement_refuses_auto_on(rig):
    smu, instrument, responses = rig
    responses[":OUTP1?"] = "1"
    responses[":OUTP1:ON:AUTO?"] = "1"

    with pytest.raises(RuntimeError, match="Automatic output-on"):
        smu.chan1.measure_voltage()

    assert call(":MEAS:VOLT? (@1)") not in (
        instrument.query.call_args_list
    )


def test_measurement_refuses_binary_format(rig):
    smu, instrument, responses = rig
    responses[":OUTP1?"] = "1"
    responses[":FORM:DATA?"] = "REAL,64"

    with pytest.raises(RuntimeError, match="ASCII"):
        smu.chan1.measure_voltage()

    assert call(":MEAS:VOLT? (@1)") not in (
        instrument.query.call_args_list
    )


@pytest.mark.parametrize(
    "response",
    ["", "invalid", "nan", "inf", "9.91e37", "9.9e37", "-9.9e37", "1,2"],
)
def test_invalid_measurement_rejected(rig, response):
    smu, _, responses = rig
    responses[":OUTP1?"] = "1"
    responses[":MEAS:VOLT? (@1)"] = response

    with pytest.raises(RuntimeError, match="Invalid"):
        smu.chan1.measure_voltage()


def test_zero_measurement_is_valid(rig):
    smu, _, responses = rig
    responses[":OUTP1?"] = "1"
    responses[":MEAS:VOLT? (@1)"] = "0"

    assert smu.chan1.measure_voltage() == 0.0


def test_measurement_instrument_error_is_not_ignored(rig):
    smu, instrument, responses = rig
    responses[":OUTP1?"] = "1"

    def query(command):
        if command == ":MEAS:VOLT? (@1)":
            responses[":SYST:ERR?"] = '-230,"Data corrupt or stale"'
        return responses[command]

    instrument.query.side_effect = query

    # Bound the error-drain test to avoid returning an endless error.
    error_responses = iter([
        '+0,"No error"',
        '+0,"No error"',
        '+0,"No error"',
        '+0,"No error"',
        '-230,"Data corrupt or stale"',
        '+0,"No error"',
    ])

    def query_with_error_queue(command):
        if command == ":SYST:ERR?":
            return next(error_responses)
        return responses[command]

    instrument.query.side_effect = query_with_error_queue

    with pytest.raises(RuntimeError, match="Data corrupt or stale"):
        smu.chan1.measure_voltage()