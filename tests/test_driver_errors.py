"""Offline tests for instrument-error handling."""

from unittest.mock import MagicMock, call

import pytest

from drivers.keysight_b2902b import (
    ErrorQueueLimitError,
    InstrumentError,
    KeysightB2902B,
)


IDENTITY = (
    "Keysight Technologies,B2902B,"
    "TEST_SERIAL,TEST_FIRMWARE"
)


@pytest.fixture
def connected_smu(monkeypatch):
    instrument = MagicMock()
    instrument.query.return_value = IDENTITY

    resource_manager = MagicMock()
    resource_manager.open_resource.return_value = instrument

    monkeypatch.setattr(
        "drivers.keysight_b2902b.pyvisa.ResourceManager",
        MagicMock(return_value=resource_manager),
    )

    with KeysightB2902B("USB0::TEST::INSTR") as smu:
        instrument.query.reset_mock()
        yield smu, instrument


@pytest.mark.parametrize(
    "response, expected",
    [
        ('+0,"No error"\n', (0, "No error")),
        (
            '-113,"Undefined header"',
            (-113, "Undefined header"),
        ),
        (
            '-222,"Data out of range, channel 1"',
            (-222, "Data out of range, channel 1"),
        ),
        (
            '+101,"Wrong password"',
            (101, "Wrong password"),
        ),
    ],
)
def test_read_error_parses_response(
    connected_smu,
    response,
    expected,
):
    smu, instrument = connected_smu
    instrument.query.return_value = response

    assert smu.read_error() == expected

    instrument.query.assert_called_once_with(":SYST:ERR?")
    instrument.write.assert_not_called()


@pytest.mark.parametrize(
    "response",
    [
        "",
        "No error",
        'invalid,"Message"',
        '-113,"Unclosed message',
        '-113,"Message","Extra field"',
        '-113,""',
    ],
)
def test_malformed_error_response_is_rejected(
    connected_smu,
    response,
):
    smu, instrument = connected_smu
    instrument.query.return_value = response

    with pytest.raises(RuntimeError, match="Malformed"):
        smu.read_error()


def test_read_errors_preserves_order(connected_smu):
    smu, instrument = connected_smu

    instrument.query.side_effect = [
        '-113,"Undefined header"',
        '-222,"Data out of range"',
        '+0,"No error"',
    ]

    assert smu.read_errors() == [
        (-113, "Undefined header"),
        (-222, "Data out of range"),
    ]

    assert instrument.query.call_args_list == [
        call(":SYST:ERR?"),
        call(":SYST:ERR?"),
        call(":SYST:ERR?"),
    ]


def test_check_errors_accepts_empty_queue(connected_smu):
    smu, instrument = connected_smu
    instrument.query.return_value = '+0,"No error"'

    assert smu.check_errors() is None


def test_check_errors_raises_with_error_details(connected_smu):
    smu, instrument = connected_smu

    instrument.query.side_effect = [
        '-113,"Undefined header"',
        '+0,"No error"',
    ]

    with pytest.raises(InstrumentError) as caught:
        smu.check_errors()

    assert caught.value.errors == (
        (-113, "Undefined header"),
    )

    assert "Undefined header" in str(caught.value)


def test_queue_query_limit_preserves_consumed_errors(
    connected_smu,
):
    smu, instrument = connected_smu
    instrument.query.return_value = '-113,"Undefined header"'

    with pytest.raises(ErrorQueueLimitError) as caught:
        smu.read_errors(max_queries=2)

    assert instrument.query.call_count == 2
    assert caught.value.errors == (
        (-113, "Undefined header"),
        (-113, "Undefined header"),
    )


@pytest.mark.parametrize(
    "max_queries",
    [0, -1, True, 1.5, "100", None],
)
def test_invalid_query_limit_is_rejected(
    connected_smu,
    max_queries,
):
    smu, instrument = connected_smu

    with pytest.raises(ValueError):
        smu.read_errors(max_queries=max_queries)

    instrument.query.assert_not_called()


def test_disconnected_error_read_is_rejected():
    smu = KeysightB2902B("USB0::TEST::INSTR")

    with pytest.raises(RuntimeError, match="not connected"):
        smu.read_error()


def test_error_query_failure_is_not_swallowed(connected_smu):
    smu, instrument = connected_smu

    instrument.query.side_effect = RuntimeError(
        "Simulated communication failure"
    )

    with pytest.raises(
        RuntimeError,
        match="Simulated communication failure",
    ):
        smu.read_error()