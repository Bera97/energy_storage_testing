"""Offline tests for channel mapping and command generation."""

from unittest.mock import MagicMock, call

import pytest

from drivers.keysight_b2902b import KeysightB2902B
from drivers.smu_channel import SMUChannel


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
        yield smu, instrument


def test_channel_numbers():
    smu = KeysightB2902B("USB0::TEST::INSTR")

    assert smu.chan1.channel == 1
    assert smu.chan2.channel == 2
    assert smu.chan1 is not smu.chan2


@pytest.mark.parametrize(
    "channel",
    [0, 3, -1, True, 1.0, "1"],
)
def test_invalid_channel_is_rejected(channel):
    smu = KeysightB2902B("USB0::TEST::INSTR")

    with pytest.raises(ValueError):
        SMUChannel(smu, channel)


@pytest.mark.parametrize("channel_number", [1, 2])
def test_source_mode_commands(
    connected_smu,
    channel_number,
):
    smu, instrument = connected_smu
    channel = getattr(smu, f"chan{channel_number}")

    channel.set_mode_voltage_source()
    channel.set_mode_current_source()

    assert instrument.write.call_args_list == [
        call(f":SOUR{channel_number}:FUNC:MODE VOLT"),
        call(f":SOUR{channel_number}:FUNC:MODE CURR"),
    ]


@pytest.mark.parametrize("channel_number", [1, 2])
def test_compliance_commands(
    connected_smu,
    channel_number,
):
    smu, instrument = connected_smu
    channel = getattr(smu, f"chan{channel_number}")

    channel.set_current_limit(0.01)
    channel.set_voltage_limit(4.0)

    assert instrument.write.call_args_list == [
        call(f":SENS{channel_number}:CURR:PROT 0.01"),
        call(f":SENS{channel_number}:VOLT:PROT 4"),
    ]


@pytest.mark.parametrize(
    "value",
    [
        0,
        -1,
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
        "0.01",
        None,
    ],
)
@pytest.mark.parametrize(
    "method_name",
    ["set_current_limit", "set_voltage_limit"],
)
def test_invalid_compliance_is_rejected(
    connected_smu,
    value,
    method_name,
):
    smu, instrument = connected_smu

    with pytest.raises(ValueError):
        getattr(smu.chan1, method_name)(value)

    instrument.write.assert_not_called()


@pytest.mark.parametrize(
    "method_name, arguments",
    [
        ("set_mode_voltage_source", ()),
        ("set_mode_current_source", ()),
        ("set_current_limit", (0.01,)),
        ("set_voltage_limit", (4.0,)),
    ],
)
def test_disconnected_channel_command_is_rejected(
    method_name,
    arguments,
):
    smu = KeysightB2902B("USB0::TEST::INSTR")

    with pytest.raises(RuntimeError, match="not connected"):
        getattr(smu.chan1, method_name)(*arguments)


def test_channel_command_after_close_is_rejected(
    connected_smu,
):
    smu, instrument = connected_smu
    smu.close()

    with pytest.raises(RuntimeError, match="not connected"):
        smu.chan2.set_current_limit(0.01)

    instrument.write.assert_not_called()


def test_configuration_does_not_enable_output(
    connected_smu,
):
    smu, instrument = connected_smu

    smu.chan1.set_mode_voltage_source()
    smu.chan1.set_current_limit(0.01)

    smu.chan2.set_mode_current_source()
    smu.chan2.set_voltage_limit(4.0)

    assert instrument.write.call_args_list == [
        call(":SOUR1:FUNC:MODE VOLT"),
        call(":SENS1:CURR:PROT 0.01"),
        call(":SOUR2:FUNC:MODE CURR"),
        call(":SENS2:VOLT:PROT 4"),
    ]