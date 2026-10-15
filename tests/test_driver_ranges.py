"""Offline tests for source and measurement range control."""

from unittest.mock import MagicMock

import pytest

from drivers.keysight_b2902b import KeysightB2902B
from drivers.smu_ranges import SMURanges


@pytest.fixture
def rig(monkeypatch):
    instrument = MagicMock()

    responses = {
        "*IDN?": "Keysight Technologies,B2902B,TEST,TEST",
        ":SYST:ERR?": '+0,"No error"',
        ":OUTP1?": "0",
        ":OUTP2?": "0",
    }

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
@pytest.mark.parametrize(
    "kind, subsystem",
    [("source", "SOUR"), ("measurement", "SENS")],
)
@pytest.mark.parametrize("quantity", ["VOLT", "CURR"])
def test_manual_range_command(
    rig, number, kind, subsystem, quantity
):
    smu, instrument, _ = rig
    ranges = SMURanges(smu, number)

    getattr(ranges, f"set_{kind}_range")(quantity, 0.1)

    instrument.write.assert_called_once_with(
        f":{subsystem}{number}:{quantity}:RANG 0.1"
    )


@pytest.mark.parametrize(
    "kind, subsystem",
    [("source", "SOUR"), ("measurement", "SENS")],
)
@pytest.mark.parametrize("enabled, state", [(True, "ON"), (False, "OFF")])
def test_autorange_command(rig, kind, subsystem, enabled, state):
    smu, instrument, _ = rig
    ranges = SMURanges(smu, 2)

    getattr(ranges, f"set_{kind}_autorange")("CURR", enabled)

    instrument.write.assert_called_once_with(
        f":{subsystem}2:CURR:RANG:AUTO {state}"
    )


@pytest.mark.parametrize(
    "kind, subsystem",
    [("source", "SOUR"), ("measurement", "SENS")],
)
def test_range_readback(rig, kind, subsystem):
    smu, instrument, responses = rig
    command = f":{subsystem}1:VOLT:RANG?"
    responses[command] = "2"

    ranges = SMURanges(smu, 1)

    assert getattr(ranges, f"get_{kind}_range")("VOLT") == 2.0
    instrument.query.assert_any_call(command)
    instrument.write.assert_not_called()


@pytest.mark.parametrize(
    "kind, subsystem",
    [("source", "SOUR"), ("measurement", "SENS")],
)
@pytest.mark.parametrize("response, expected", [("0", False), ("1", True)])
def test_autorange_readback(rig, kind, subsystem, response, expected):
    smu, _, responses = rig
    responses[f":{subsystem}1:CURR:RANG:AUTO?"] = response

    ranges = SMURanges(smu, 1)

    assert getattr(ranges, f"get_{kind}_autorange")("CURR") is expected


@pytest.mark.parametrize(
    "value",
    [0, -1, True, None, "2", float("nan"), float("inf")],
)
def test_invalid_range_rejected(rig, value):
    smu, instrument, _ = rig
    ranges = SMURanges(smu, 1)

    with pytest.raises(ValueError):
        ranges.set_source_range("VOLT", value)

    instrument.write.assert_not_called()


@pytest.mark.parametrize("quantity", ["", "RES", "INVALID", None])
def test_invalid_quantity_rejected(rig, quantity):
    smu, instrument, _ = rig

    with pytest.raises(ValueError):
        SMURanges(smu, 1).set_measurement_range(quantity, 2)

    instrument.write.assert_not_called()


@pytest.mark.parametrize(
    "method, arguments",
    [
        ("set_source_range", ("VOLT", 2)),
        ("set_measurement_range", ("VOLT", 2)),
        ("set_source_autorange", ("CURR", True)),
        ("set_measurement_autorange", ("CURR", True)),
    ],
)
def test_range_change_refuses_output_on(rig, method, arguments):
    smu, instrument, responses = rig
    responses[":OUTP1?"] = "1"

    with pytest.raises(RuntimeError, match="must be OFF"):
        getattr(SMURanges(smu, 1), method)(*arguments)

    instrument.write.assert_not_called()


@pytest.mark.parametrize("enabled", [0, 1, "ON", None])
def test_invalid_autorange_argument_rejected(rig, enabled):
    smu, instrument, _ = rig

    with pytest.raises(ValueError):
        SMURanges(smu, 1).set_source_autorange("CURR", enabled)

    instrument.write.assert_not_called()


@pytest.mark.parametrize("response", ["0", "-1", "nan", "inf", "9.91e37", "bad"])
def test_invalid_range_response_rejected(rig, response):
    smu, _, responses = rig
    responses[":SOUR1:VOLT:RANG?"] = response

    with pytest.raises(RuntimeError, match="Invalid range"):
        SMURanges(smu, 1).get_source_range("VOLT")


@pytest.mark.parametrize("response", ["", "2", "ON"])
def test_invalid_autorange_response_rejected(rig, response):
    smu, _, responses = rig
    responses[":SENS1:CURR:RANG:AUTO?"] = response

    with pytest.raises(RuntimeError, match="Invalid autorange"):
        SMURanges(smu, 1).get_measurement_autorange("CURR")


@pytest.mark.parametrize("number", [0, 3, True, "1"])
def test_invalid_channel_rejected(number):
    smu = KeysightB2902B("USB0::TEST::INSTR")

    with pytest.raises(ValueError):
        SMURanges(smu, number)


def test_disconnected_range_change_rejected():
    smu = KeysightB2902B("USB0::TEST::INSTR")

    with pytest.raises(RuntimeError, match="not connected"):
        SMURanges(smu, 1).set_source_range("VOLT", 2)


def test_instrument_range_conflict_is_propagated(rig):
    smu, instrument, responses = rig
    queue = []

    def write(command):
        queue.extend([
            '-221,"Settings conflict"',
            '+0,"No error"',
        ])

    def query(command):
        if command == ":SYST:ERR?" and queue:
            return queue.pop(0)
        return responses[command]

    instrument.write.side_effect = write
    instrument.query.side_effect = query

    with pytest.raises(RuntimeError, match="Settings conflict"):
        SMURanges(smu, 1).set_source_range("VOLT", 0.2)