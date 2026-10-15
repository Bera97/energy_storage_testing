"""Offline tests for explicit DC output enabling."""

from unittest.mock import MagicMock, call

import pytest

from drivers.dc_output import enable_dc_output
from drivers.keysight_b2902b import KeysightB2902B


@pytest.fixture
def rig(monkeypatch):
    instrument = MagicMock()

    responses = {
        "*IDN?": "Keysight Technologies,B2902B,TEST,TEST",
        ":SYST:ERR?": '+0,"No error"',
        ":OUTP1?": "0",
        ":OUTP2?": "0",
    }

    for number in (1, 2):
        responses.update({
            f":OUTP{number}:ON:AUTO?": "0",
            f":OUTP{number}:OFF:MODE?": "HIZ",
            f":SOUR{number}:FUNC:SHAP?": "DC",
            f":SOUR{number}:FUNC:MODE?": "VOLT",
            f":SOUR{number}:VOLT:MODE?": "FIX",
            f":SOUR{number}:CURR:MODE?": "FIX",
            f":SOUR{number}:VOLT?": "0.1",
            f":SOUR{number}:CURR?": "0.0001",
            f":SENS{number}:CURR:PROT?": "0.001",
            f":SENS{number}:VOLT:PROT?": "0.5",
        })

    def write(command):
        for number in (1, 2):
            if command == f":OUTP{number} ON":
                responses[f":OUTP{number}?"] = "1"
            elif command == f":OUTP{number} OFF":
                responses[f":OUTP{number}?"] = "0"

    instrument.write.side_effect = write
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
def test_enable_dc_output(rig, number, function):
    smu, instrument, responses = rig
    responses[f":SOUR{number}:FUNC:MODE?"] = function

    enable_dc_output(
        smu,
        number,
        max_voltage_v=1,
        max_current_a=0.002,
    )

    assert instrument.write.call_args_list == [
        call(f":ABOR:ALL (@{number})"),
        call(f":OUTP{number} ON"),
    ]
    assert responses[f":OUTP{number}?"] == "1"


@pytest.mark.parametrize(
    "command, response",
    [
        (":OUTP1?", "1"),
        (":OUTP2?", "1"),
        (":OUTP1:ON:AUTO?", "1"),
        (":OUTP1:OFF:MODE?", "ZERO"),
        (":SOUR1:FUNC:SHAP?", "PULS"),
        (":SOUR1:VOLT:MODE?", "LIST"),
        (":SOUR1:VOLT?", "2"),
        (":SOUR1:VOLT?", "-2"),
        (":SENS1:CURR:PROT?", "0.1"),
        (":SENS1:CURR:PROT?", "0"),
    ],
)
def test_preflight_failure_never_sends_output_on(
    rig, command, response
):
    smu, instrument, responses = rig
    responses[command] = response

    with pytest.raises(RuntimeError):
        enable_dc_output(
            smu,
            1,
            max_voltage_v=1,
            max_current_a=0.002,
        )

    assert call(":OUTP1 ON") not in (
        instrument.write.call_args_list
    )


@pytest.mark.parametrize(
    "command, response",
    [
        (":SOUR1:CURR?", "0.1"),
        (":SENS1:VOLT:PROT?", "2"),
        (":SENS1:VOLT:PROT?", "0"),
    ],
)
def test_current_source_ceiling_checks(rig, command, response):
    smu, instrument, responses = rig
    responses[":SOUR1:FUNC:MODE?"] = "CURR"
    responses[command] = response

    with pytest.raises(RuntimeError):
        enable_dc_output(
            smu,
            1,
            max_voltage_v=1,
            max_current_a=0.002,
        )

    assert call(":OUTP1 ON") not in (
        instrument.write.call_args_list
    )


@pytest.mark.parametrize("number", [0, 3, True, "1"])
def test_invalid_channel(rig, number):
    smu, instrument, _ = rig

    with pytest.raises(ValueError):
        enable_dc_output(
            smu,
            number,
            max_voltage_v=1,
            max_current_a=0.002,
        )

    instrument.write.assert_not_called()


@pytest.mark.parametrize(
    "voltage, current",
    [
        (0, 0.001),
        (1, 0),
        (float("inf"), 0.001),
        (1, float("nan")),
        (True, 0.001),
    ],
)
def test_invalid_ceilings(rig, voltage, current):
    smu, instrument, _ = rig

    with pytest.raises(ValueError):
        enable_dc_output(
            smu,
            1,
            max_voltage_v=voltage,
            max_current_a=current,
        )

    instrument.write.assert_not_called()


def test_failed_enable_readback_attempts_output_off(rig):
    smu, instrument, _ = rig

    # Simulate ON being ignored; state remains OFF.
    instrument.write.side_effect = None

    with pytest.raises(RuntimeError, match="did not report"):
        enable_dc_output(
            smu,
            1,
            max_voltage_v=1,
            max_current_a=0.002,
        )

    instrument.write.assert_any_call(":OUTP1 ON")
    instrument.write.assert_any_call(":OUTP1 OFF")


def test_failed_enable_write_attempts_output_off(rig):
    smu, instrument, _ = rig

    def write(command):
        if command == ":OUTP1 ON":
            raise RuntimeError("Simulated enable failure")

    instrument.write.side_effect = write

    with pytest.raises(RuntimeError, match="Simulated enable failure"):
        enable_dc_output(
            smu,
            1,
            max_voltage_v=1,
            max_current_a=0.002,
        )

    instrument.write.assert_any_call(":OUTP1 OFF")


def test_disconnected_enable_is_rejected():
    smu = KeysightB2902B("USB0::TEST::INSTR")

    with pytest.raises(RuntimeError, match="not connected"):
        enable_dc_output(
            smu,
            1,
            max_voltage_v=1,
            max_current_a=0.002,
        )