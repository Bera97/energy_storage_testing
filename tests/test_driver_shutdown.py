"""Offline tests for trigger abort and explicit shutdown."""

from unittest.mock import MagicMock, call

import pytest

from drivers.keysight_b2902b import (
    KeysightB2902B,
    ShutdownError,
)


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

        responses = {
            ":SYST:ERR?": '+0,"No error"',
            ":OUTP1?": "0",
            ":OUTP2?": "0",
            ":OUTP1:ON:AUTO?": "0",
            ":OUTP2:ON:AUTO?": "0",
        }

        instrument.query.side_effect = responses.__getitem__
        yield smu, instrument


def test_abort_command(connected_smu):
    smu, instrument = connected_smu

    smu.abort()

    instrument.write.assert_called_once_with(
        ":ABOR:ALL (@1,2)"
    )
    instrument.query.assert_not_called()


def test_shutdown_commands_and_connection_preserved(connected_smu):
    smu, instrument = connected_smu

    smu.shutdown()

    assert instrument.write.call_args_list == [
        call(":ABOR:ALL (@1,2)"),
        call(":OUTP1:ON:AUTO OFF"),
        call(":OUTP1 OFF"),
        call(":OUTP2:ON:AUTO OFF"),
        call(":OUTP2 OFF"),
    ]

    assert smu.is_connected is True
    instrument.close.assert_not_called()

    instrument.query.assert_any_call(":OUTP1?")
    instrument.query.assert_any_call(":OUTP2?")
    instrument.query.assert_any_call(":OUTP1:ON:AUTO?")
    instrument.query.assert_any_call(":OUTP2:ON:AUTO?")


@pytest.mark.parametrize(
    "failed_command",
    [
        ":ABOR:ALL (@1,2)",
        ":OUTP1:ON:AUTO OFF",
        ":OUTP1 OFF",
    ],
)
def test_shutdown_continues_after_command_failure(
    connected_smu, failed_command
):
    smu, instrument = connected_smu

    def write(command):
        if command == failed_command:
            raise RuntimeError("Simulated write failure")

    instrument.write.side_effect = write

    with pytest.raises(ShutdownError) as caught:
        smu.shutdown()

    instrument.write.assert_any_call(":OUTP2:ON:AUTO OFF")
    instrument.write.assert_any_call(":OUTP2 OFF")
    instrument.query.assert_any_call(":OUTP2?")

    assert any(
        "Simulated write failure" in str(error)
        for _, error in caught.value.failures
    )


@pytest.mark.parametrize(
    "failed_query",
    [
        ":OUTP1?",
        ":OUTP1:ON:AUTO?",
        ":SYST:ERR?",
    ],
)
def test_shutdown_continues_after_query_failure(
    connected_smu, failed_query
):
    smu, instrument = connected_smu

    def query(command):
        if command == failed_query:
            raise RuntimeError("Simulated query failure")
        if command == ":SYST:ERR?":
            return '+0,"No error"'
        return "0"

    instrument.query.side_effect = query

    with pytest.raises(ShutdownError):
        smu.shutdown()

    instrument.write.assert_any_call(":OUTP2 OFF")
    instrument.query.assert_any_call(":OUTP2?")
    instrument.query.assert_any_call(":OUTP2:ON:AUTO?")


@pytest.mark.parametrize(
    "on_query",
    [":OUTP1?", ":OUTP2?", ":OUTP1:ON:AUTO?", ":OUTP2:ON:AUTO?"],
)
def test_shutdown_rejects_on_readback(connected_smu, on_query):
    smu, instrument = connected_smu

    def query(command):
        if command == ":SYST:ERR?":
            return '+0,"No error"'
        return "1" if command == on_query else "0"

    instrument.query.side_effect = query

    with pytest.raises(ShutdownError):
        smu.shutdown()


def test_shutdown_reports_instrument_errors(connected_smu):
    smu, instrument = connected_smu
    error_responses = iter([
        '-113,"Undefined header"',
        '+0,"No error"',
        '+0,"No error"',
    ])

    def query(command):
        if command == ":SYST:ERR?":
            return next(error_responses)
        return "0"

    instrument.query.side_effect = query

    with pytest.raises(ShutdownError, match="Undefined header"):
        smu.shutdown()

    instrument.query.assert_any_call(":OUTP2?")


@pytest.mark.parametrize("method_name", ["abort", "shutdown"])
def test_disconnected_operation_is_rejected(method_name):
    smu = KeysightB2902B("USB0::TEST::INSTR")

    with pytest.raises(RuntimeError, match="not connected"):
        getattr(smu, method_name)()