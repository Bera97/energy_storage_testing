"""Offline tests for immediate source-level settings."""

from unittest.mock import MagicMock

import pytest

from drivers.keysight_b2902b import KeysightB2902B


@pytest.fixture
def connected_smu(monkeypatch):
    instrument = MagicMock()
    instrument.query.return_value = (
        "Keysight Technologies,B2902B,"
        "TEST_SERIAL,TEST_FIRMWARE"
    )

    resource_manager = MagicMock()
    resource_manager.open_resource.return_value = instrument

    monkeypatch.setattr(
        "drivers.keysight_b2902b.pyvisa.ResourceManager",
        MagicMock(return_value=resource_manager),
    )

    with KeysightB2902B("USB0::TEST::INSTR") as smu:
        instrument.query.reset_mock()
        yield smu, instrument


@pytest.mark.parametrize("channel_number", [1, 2])
@pytest.mark.parametrize(
    "method_name, quantity",
    [
        ("set_voltage", "VOLT"),
        ("set_current", "CURR"),
    ],
)
@pytest.mark.parametrize(
    "value, formatted",
    [
        (0.0, "0"),
        (0.01, "0.01"),
        (-0.01, "-0.01"),
    ],
)
def test_source_level_command(
    connected_smu,
    channel_number,
    method_name,
    quantity,
    value,
    formatted,
):
    smu, instrument = connected_smu
    channel = getattr(smu, f"chan{channel_number}")

    getattr(channel, method_name)(value)

    instrument.write.assert_called_once_with(
        f":SOUR{channel_number}:{quantity} {formatted}"
    )
    instrument.query.assert_not_called()


@pytest.mark.parametrize(
    "method_name",
    ["set_voltage", "set_current"],
)
@pytest.mark.parametrize(
    "value",
    [True, None, "1", float("nan"), float("inf"), float("-inf")],
)
def test_invalid_source_level_is_rejected(
    connected_smu, method_name, value
):
    smu, instrument = connected_smu

    with pytest.raises(ValueError):
        getattr(smu.chan1, method_name)(value)

    instrument.write.assert_not_called()


@pytest.mark.parametrize("channel_number", [1, 2])
@pytest.mark.parametrize(
    "method_name, quantity",
    [
        ("get_voltage_setpoint", "VOLT"),
        ("get_current_setpoint", "CURR"),
    ],
)
@pytest.mark.parametrize(
    "response, expected",
    [
        ("+0.00000000E+000\n", 0.0),
        ("+1.00000000E-002\n", 0.01),
        ("-1.00000000E-002\n", -0.01),
    ],
)
def test_source_level_readback(
    connected_smu,
    channel_number,
    method_name,
    quantity,
    response,
    expected,
):
    smu, instrument = connected_smu
    instrument.query.return_value = response
    channel = getattr(smu, f"chan{channel_number}")

    assert getattr(channel, method_name)() == pytest.approx(expected)

    instrument.query.assert_called_once_with(
        f":SOUR{channel_number}:{quantity}?"
    )
    instrument.write.assert_not_called()


@pytest.mark.parametrize(
    "method_name",
    ["get_voltage_setpoint", "get_current_setpoint"],
)
@pytest.mark.parametrize(
    "response",
    ["", "invalid", "nan", "inf", "-inf", "1,2"],
)
def test_invalid_source_readback_is_rejected(
    connected_smu, method_name, response
):
    smu, instrument = connected_smu
    instrument.query.return_value = response

    with pytest.raises(RuntimeError, match="Invalid"):
        getattr(smu.chan1, method_name)()


@pytest.mark.parametrize(
    "method_name, arguments",
    [
        ("set_voltage", (0.1,)),
        ("set_current", (0.001,)),
        ("get_voltage_setpoint", ()),
        ("get_current_setpoint", ()),
    ],
)
def test_disconnected_source_method_is_rejected(
    method_name, arguments
):
    smu = KeysightB2902B("USB0::TEST::INSTR")

    with pytest.raises(RuntimeError, match="not connected"):
        getattr(smu.chan1, method_name)(*arguments)