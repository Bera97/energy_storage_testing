"""Offline tests for paired records and status handling."""

from unittest.mock import MagicMock, call

import pytest

from drivers.keysight_b2902b import KeysightB2902B
from drivers.spot_acquisition import (
    MeasurementStatusError,
    SpotAcquisition,
    parse_reading,
)


@pytest.fixture
def rig(monkeypatch):
    instrument = MagicMock()

    responses = {
        "*IDN?": "Keysight Technologies,B2902B,TEST,TEST",
        ":SYST:ERR?": '+0,"No error"',
        ":FORM:DATA?": "ASC",
        ":FORM:ELEM:SENS?": "VOLT,CURR,TIME,STAT",
    }

    for number in (1, 2):
        responses.update({
            f":OUTP{number}?": "0",
            f":OUTP{number}:ON:AUTO?": "0",
            f":SENS{number}:VOLT:NPLC? MIN": "0.001",
            f":SENS{number}:VOLT:NPLC? MAX": "100",
            f':SENS{number}:FUNC:STAT? "VOLT"': "1",
            f':SENS{number}:FUNC:STAT? "CURR"': "1",
            f":MEAS? (@{number})": "1,-0.001,0.25,0",
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


def test_parse_record():
    reading = parse_reading("1,-0.001,0.25,+0.0E+00\n", 1)

    assert reading.voltage_v == 1
    assert reading.current_a == -0.001
    assert reading.timestamp_s == 0.25
    assert reading.status == 0
    assert reading.power_w == pytest.approx(-0.001)


@pytest.mark.parametrize(
    "status, attribute",
    [
        (1, "current_source"),
        (2, "in_compliance"),
        (4, "in_compliance"),
        (6, "in_compliance"),
        (8, "over_voltage"),
        (16, "over_current"),
        (32, "over_temperature"),
        (8192, "range_overflow"),
    ],
)
def test_status_flags(status, attribute):
    reading = parse_reading(f"1,0.001,0,{status}", 1)

    assert getattr(reading, attribute) is True


@pytest.mark.parametrize(
    "response",
    [
        "",
        "1,2,3",
        "1,2,3,0,4",
        "nan,0.001,0,0",
        "1,inf,0,0",
        "9.91e37,0.001,0,0",
        "1,0.001,-1,0",
        "1,0.001,0,-1",
        "1,0.001,0,0.5",
        "1,0.001,0,nan",
        "1,0.001,0,4294967296",
    ],
)
def test_invalid_record_rejected(response):
    with pytest.raises(RuntimeError):
        parse_reading(response, 1)


def test_configure_record_format(rig):
    smu, instrument, _ = rig

    SpotAcquisition(smu, 1).configure()

    instrument.write.assert_any_call(
        ":FORM:ELEM:SENS VOLT,CURR,TIME,STAT"
    )


def test_configuration_refuses_other_channel_on(rig):
    smu, instrument, responses = rig
    responses[":OUTP2?"] = "1"

    with pytest.raises(RuntimeError, match="Both outputs"):
        SpotAcquisition(smu, 1).configure()

    instrument.write.assert_not_called()


@pytest.mark.parametrize("number", [1, 2])
def test_read_uses_one_measurement_query(rig, number):
    smu, instrument, responses = rig
    responses[f":OUTP{number}?"] = "1"

    reading = SpotAcquisition(smu, number).read()

    assert reading.channel == number

    measurements = [
        item for item in instrument.query.call_args_list
        if item.args[0].startswith(":MEAS")
    ]
    assert measurements == [call(f":MEAS? (@{number})")]
    instrument.write.assert_not_called()


def test_read_refuses_output_off(rig):
    smu, instrument, _ = rig

    with pytest.raises(RuntimeError, match="already be ON"):
        SpotAcquisition(smu, 1).read()

    assert call(":MEAS? (@1)") not in instrument.query.call_args_list


@pytest.mark.parametrize(
    "command, response",
    [
        (":OUTP1:ON:AUTO?", "1"),
        (":FORM:DATA?", "REAL,64"),
        (":FORM:ELEM:SENS?", "VOLT,CURR"),
        (':SENS1:FUNC:STAT? "VOLT"', "0"),
        (':SENS1:FUNC:STAT? "CURR"', "0"),
    ],
)
def test_read_rejects_wrong_configuration(rig, command, response):
    smu, instrument, responses = rig
    responses[":OUTP1?"] = "1"
    responses[command] = response

    with pytest.raises(RuntimeError):
        SpotAcquisition(smu, 1).read()

    assert call(":MEAS? (@1)") not in instrument.query.call_args_list


@pytest.mark.parametrize("status", [8, 16, 32, 8192])
def test_fault_preserves_record_on_exception(rig, status):
    smu, _, responses = rig
    responses[":OUTP1?"] = "1"
    responses[":MEAS? (@1)"] = f"1,0.001,0.25,{status}"

    with pytest.raises(MeasurementStatusError) as caught:
        SpotAcquisition(smu, 1).read()

    assert caught.value.reading.status == status
    assert caught.value.reading.timestamp_s == 0.25


@pytest.mark.parametrize("status", [2, 4, 6])
def test_compliance_is_returned_not_discarded(rig, status):
    smu, _, responses = rig
    responses[":OUTP1?"] = "1"
    responses[":MEAS? (@1)"] = f"1,0.001,0.25,{status}"

    reading = SpotAcquisition(smu, 1).read()

    assert reading.in_compliance is True
    assert reading.has_fault is False


def test_unknown_status_bits_are_retained():
    reading = parse_reading("1,0.001,0,128", 1)

    assert reading.status == 128
    assert reading.has_fault is False


def test_disconnected_read_is_rejected():
    smu = KeysightB2902B("USB0::TEST::INSTR")

    with pytest.raises(RuntimeError, match="not connected"):
        SpotAcquisition(smu, 1).read()