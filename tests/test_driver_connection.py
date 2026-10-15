"""Connection-layer tests using mocked VISA objects.

These tests do not communicate with real hardware.
"""

from unittest.mock import MagicMock

import pytest

from drivers.keysight_b2902b import KeysightB2902B


VISA_ADDRESS = "USB0::TEST::INSTR"
IDENTITY = "Keysight Technologies,B2902B,TEST_SERIAL,TEST_FIRMWARE"


@pytest.fixture
def mock_visa(monkeypatch):
    """Replace the driver's VISA resource manager with mock objects."""
    instrument = MagicMock()
    instrument.query.return_value = IDENTITY

    resource_manager = MagicMock()
    resource_manager.open_resource.return_value = instrument

    resource_manager_factory = MagicMock(
        return_value=resource_manager
    )

    monkeypatch.setattr(
        "drivers.keysight_b2902b.pyvisa.ResourceManager",
        resource_manager_factory,
    )

    return resource_manager_factory, resource_manager, instrument


def test_successful_connection_and_cleanup(mock_visa):
    factory, resource_manager, instrument = mock_visa

    smu = KeysightB2902B(VISA_ADDRESS, timeout_ms=5000)

    assert smu.is_connected is False

    with smu as connected_smu:
        assert connected_smu is smu
        assert smu.is_connected is True
        assert smu.identity == IDENTITY

        assert instrument.timeout == 5000
        assert instrument.write_termination == "\n"
        assert instrument.read_termination == "\n"

        factory.assert_called_once_with()
        resource_manager.open_resource.assert_called_once_with(
            VISA_ADDRESS
        )
        instrument.query.assert_called_once_with("*IDN?")

        # Connecting must not send configuration/output commands.
        instrument.write.assert_not_called()

    assert smu.is_connected is False
    instrument.close.assert_called_once_with()
    resource_manager.close.assert_called_once_with()

    with pytest.raises(RuntimeError, match="not connected"):
        _ = smu.identity


def test_wrong_model_is_rejected_and_cleaned_up(mock_visa):
    _, resource_manager, instrument = mock_visa

    instrument.query.return_value = (
        "Keysight Technologies,B2912B,TEST_SERIAL,TEST_FIRMWARE"
    )

    smu = KeysightB2902B(VISA_ADDRESS)

    with pytest.raises(RuntimeError, match="Expected B2902B"):
        smu.connect()

    assert smu.is_connected is False
    instrument.close.assert_called_once_with()
    resource_manager.close.assert_called_once_with()
    instrument.write.assert_not_called()


def test_query_failure_is_propagated_and_cleaned_up(mock_visa):
    _, resource_manager, instrument = mock_visa

    instrument.query.side_effect = RuntimeError(
        "Simulated communication failure"
    )

    smu = KeysightB2902B(VISA_ADDRESS)

    with pytest.raises(
        RuntimeError,
        match="Simulated communication failure",
    ):
        smu.connect()

    assert smu.is_connected is False
    instrument.close.assert_called_once_with()
    resource_manager.close.assert_called_once_with()


def test_repeated_connect_and_close(mock_visa):
    factory, resource_manager, instrument = mock_visa

    smu = KeysightB2902B(VISA_ADDRESS)

    smu.connect()
    smu.connect()

    factory.assert_called_once_with()
    resource_manager.open_resource.assert_called_once_with(
        VISA_ADDRESS
    )
    instrument.query.assert_called_once_with("*IDN?")

    smu.close()
    smu.close()

    assert smu.is_connected is False
    instrument.close.assert_called_once_with()
    resource_manager.close.assert_called_once_with()