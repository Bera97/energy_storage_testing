"""Offline tests for source-function and compliance readback."""

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
@pytest.mark.parametrize("mode", ["VOLT", "CURR"])
def test_source_mode_readback(
    connected_smu, channel_number, mode
):
    smu, instrument = connected_smu
    instrument.query.return_value = mode + "\n"

    channel = getattr(smu, f"chan{channel_number}")

    assert channel.get_source_mode() == mode
    instrument.query.assert_called_once_with(
        f":SOUR{channel_number}:FUNC:MODE?"
    )
    instrument.write.assert_not_called()


@pytest.mark.parametrize(
    "response", ["", "UNKNOWN", "VOLT,CURR", "1"]
)
def test_invalid_source_mode_is_rejected(
    connected_smu, response
):
    smu, instrument = connected_smu
    instrument.query.return_value = response

    with pytest.raises(RuntimeError, match="Unexpected response"):
        smu.chan1.get_source_mode()


@pytest.mark.parametrize("channel_number", [1, 2])
@pytest.mark.parametrize(
    "method_name, quantity, response, expected",
    [
        ("get_current_limit", "CURR", "+1.000000E-02\n", 0.01),
        ("get_voltage_limit", "VOLT", "+4.000000E+00\n", 4.0),
        ("get_current_limit", "CURR", "+0.00000000E+000", 0.0),
        ("get_voltage_limit", "VOLT", "+0.00000000E+000", 0.0),
    ],
)
def test_compliance_readback(
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
        f":SENS{channel_number}:{quantity}:PROT?"
    )
    instrument.write.assert_not_called()


@pytest.mark.parametrize(
    "method_name", ["get_current_limit", "get_voltage_limit"]
)
@pytest.mark.parametrize(
    "response", ["", "invalid", "nan", "inf", "-inf", "-1", "1,2"]
)
def test_invalid_compliance_readback_is_rejected(
    connected_smu, method_name, response
):
    smu, instrument = connected_smu
    instrument.query.return_value = response

    with pytest.raises(RuntimeError, match="Invalid"):
        getattr(smu.chan1, method_name)()


@pytest.mark.parametrize(
    "method_name", ["set_current_limit", "set_voltage_limit"]
)
def test_zero_compliance_setter_remains_rejected(
    connected_smu, method_name
):
    smu, instrument = connected_smu

    with pytest.raises(ValueError, match="greater than zero"):
        getattr(smu.chan1, method_name)(0.0)

    instrument.write.assert_not_called()


@pytest.mark.parametrize(
    "method_name",
    ["get_source_mode", "get_current_limit", "get_voltage_limit"],
)
def test_disconnected_readback_is_rejected(method_name):
    smu = KeysightB2902B("USB0::TEST::INSTR")

    with pytest.raises(RuntimeError, match="not connected"):
        getattr(smu.chan1, method_name)()


def test_readback_communication_failure_is_propagated(
    connected_smu,
):
    smu, instrument = connected_smu
    instrument.query.side_effect = RuntimeError(
        "Simulated communication failure"
    )

    with pytest.raises(
        RuntimeError, match="Simulated communication failure"
    ):
        smu.chan1.get_current_limit()