"""Offline tests for output-state queries and output disable."""

from unittest.mock import MagicMock, call

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
    "response, expected",
    [("0\n", False), ("1\n", True)],
)
def test_output_state(
    connected_smu, channel_number, response, expected
):
    smu, instrument = connected_smu
    instrument.query.return_value = response

    channel = getattr(smu, f"chan{channel_number}")

    assert channel.get_output_state() is expected
    instrument.query.assert_called_once_with(
        f":OUTP{channel_number}?"
    )
    instrument.write.assert_not_called()


@pytest.mark.parametrize(
    "response", ["", "2", "-1", "ON", "OFF", "0,1", "nan"]
)
def test_invalid_output_state_is_rejected(
    connected_smu, response
):
    smu, instrument = connected_smu
    instrument.query.return_value = response

    with pytest.raises(RuntimeError, match="Unexpected response"):
        smu.chan1.get_output_state()


@pytest.mark.parametrize("channel_number", [1, 2])
def test_disable_output_command(connected_smu, channel_number):
    smu, instrument = connected_smu
    channel = getattr(smu, f"chan{channel_number}")

    channel.disable_output()

    instrument.write.assert_called_once_with(
        f":OUTP{channel_number} OFF"
    )
    instrument.query.assert_not_called()


@pytest.mark.parametrize("channel_number", [1, 2])
@pytest.mark.parametrize("mode", ["NORM", "HIZ", "ZERO"])
def test_output_off_mode(connected_smu, channel_number, mode):
    smu, instrument = connected_smu
    instrument.query.return_value = mode + "\n"

    channel = getattr(smu, f"chan{channel_number}")

    assert channel.get_output_off_mode() == mode
    instrument.query.assert_called_once_with(
        f":OUTP{channel_number}:OFF:MODE?"
    )
    instrument.write.assert_not_called()


@pytest.mark.parametrize("response", ["", "UNKNOWN", "1"])
def test_invalid_off_mode_is_rejected(connected_smu, response):
    smu, instrument = connected_smu
    instrument.query.return_value = response

    with pytest.raises(RuntimeError, match="Unexpected response"):
        smu.chan1.get_output_off_mode()


@pytest.mark.parametrize(
    "method_name",
    [
        "get_output_state",
        "disable_output",
        "get_output_off_mode",
    ],
)
def test_disconnected_output_method_is_rejected(method_name):
    smu = KeysightB2902B("USB0::TEST::INSTR")

    with pytest.raises(RuntimeError, match="not connected"):
        getattr(smu.chan1, method_name)()


def test_disable_both_channel_commands(connected_smu):
    smu, instrument = connected_smu

    smu.chan1.disable_output()
    smu.chan2.disable_output()

    assert instrument.write.call_args_list == [
        call(":OUTP1 OFF"),
        call(":OUTP2 OFF"),
    ]


def test_disable_communication_failure_is_propagated(
    connected_smu,
):
    smu, instrument = connected_smu
    instrument.write.side_effect = RuntimeError(
        "Simulated communication failure"
    )

    with pytest.raises(
        RuntimeError, match="Simulated communication failure"
    ):
        smu.chan1.disable_output()