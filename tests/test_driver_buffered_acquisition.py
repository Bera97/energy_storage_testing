"""Offline tests for finite timer acquisition."""

from unittest.mock import MagicMock, call

import pytest

from drivers.buffered_acquisition import (
    BufferedAcquisition,
    BufferedStatusError,
    parse_buffer,
)


def make_rig():
    smu = MagicMock()
    smu.chan1.channel = 1
    smu.chan2.channel = 2
    smu.chan1.get_output_state.return_value = False
    smu.chan2.get_output_state.return_value = False
    smu.chan1.get_auto_output_on.return_value = False

    responses = {
        ":FORM:DATA?": "ASC",
        ":FORM:ELEM:SENS?": "VOLT,CURR,TIME,STAT",
        ":TRIG1:ACQ:TIM? MIN": "0.00002",
        ":TRIG1:ACQ:TIM? MAX": "100000",
        ":SENS1:VOLT:APER?": "0.02",
        ":TRIG1:ACQ:TIM?": "0.2",
        ":TRIG1:ACQ:COUN?": "3",
        ':SENS1:FUNC:STAT? "VOLT"': "+1",
        ':SENS1:FUNC:STAT? "CURR"': "+1",
        ":STAT:QUES:COND?": "0",
        ":STAT:OPER:COND?": "16",
        ":FETC:ARR? (@1)": (
            "1,0.001,0,0,"
            "1,0.001,0.2,0,"
            "1,0.001,0.4,0"
        ),
    }

    smu._query.side_effect = responses.__getitem__
    return smu, responses


def configured_rig():
    smu, responses = make_rig()
    acquisition = BufferedAcquisition(smu, 1)
    acquisition.configure(3, 0.2)
    smu.reset_mock()
    smu.chan1.get_output_state.return_value = True
    smu.chan1.get_auto_output_on.return_value = False
    return smu, responses, acquisition


def test_parse_buffer():
    records = parse_buffer(
        "1,0.001,0,0,1,0.001,0.2,0", 1, 2
    )
    assert len(records) == 2
    assert records[1].timestamp_s == 0.2


def test_wrong_record_count_rejected():
    with pytest.raises(RuntimeError, match="data fields"):
        parse_buffer("1,0.001,0,0", 1, 2)


@pytest.mark.parametrize("second_time", ["0", "-1"])
def test_non_increasing_timestamps_rejected(second_time):
    with pytest.raises(RuntimeError):
        parse_buffer(
            f"1,0.001,0,0,1,0.001,{second_time},0", 1, 2
        )


def test_fault_exception_retains_entire_buffer():
    with pytest.raises(BufferedStatusError) as caught:
        parse_buffer(
            "1,0.001,0,0,1,0.001,0.2,8192", 1, 2
        )

    assert len(caught.value.readings) == 2
    assert caught.value.fault_indices == (1,)


def test_configure_timer_commands():
    smu, _ = make_rig()
    BufferedAcquisition(smu, 1).configure(3, 0.2)

    smu._write.assert_any_call(":TRIG1:ACQ:SOUR TIM")
    smu._write.assert_any_call(":TRIG1:ACQ:TIM 0.2")
    smu._write.assert_any_call(":TRIG1:ACQ:COUN 3")
    smu._write.assert_any_call(":OUTP1:ON:AUTO OFF")


@pytest.mark.parametrize("count", [0, -1, True, 2501])
def test_invalid_count_rejected(count):
    smu, _ = make_rig()
    with pytest.raises(ValueError):
        BufferedAcquisition(smu, 1).configure(count, 0.2)


def test_interval_shorter_than_aperture_rejected():
    smu, _ = make_rig()
    with pytest.raises(ValueError, match="aperture"):
        BufferedAcquisition(smu, 1).configure(3, 0.001)


def test_configure_refuses_output_on():
    smu, _ = make_rig()
    smu.chan2.get_output_state.return_value = True

    with pytest.raises(RuntimeError, match="Both outputs"):
        BufferedAcquisition(smu, 1).configure(3, 0.2)


def test_acquire_starts_once_and_fetches_array():
    smu, _, acquisition = configured_rig()

    records = acquisition.acquire()

    assert len(records) == 3
    smu._write.assert_called_once_with(":INIT:ACQ (@1)")
    smu._query.assert_any_call(":FETC:ARR? (@1)")

    assert all(
        not item.args[0].startswith(":MEAS")
        for item in smu._query.call_args_list
    )


def test_acquire_requires_configuration():
    smu, _ = make_rig()
    with pytest.raises(RuntimeError, match="configure"):
        BufferedAcquisition(smu, 1).acquire()


def test_acquire_refuses_output_off():
    smu, _, acquisition = configured_rig()
    smu.chan1.get_output_state.return_value = False

    with pytest.raises(RuntimeError, match="already be ON"):
        acquisition.acquire()

    smu._write.assert_not_called()


def test_lost_acquisition_event_rejected():
    smu, responses, acquisition = configured_rig()
    responses[":STAT:QUES:COND?"] = str(1 << 12)

    with pytest.raises(RuntimeError, match="lost acquisition"):
        acquisition.acquire()

    smu._write.assert_not_called()


def test_bad_buffer_attempts_abort():
    smu, responses, acquisition = configured_rig()
    responses[":FETC:ARR? (@1)"] = "invalid"

    with pytest.raises(RuntimeError):
        acquisition.acquire()

    smu._write.assert_any_call(":ABOR:ACQ (@1)")


def test_completion_timeout_attempts_abort(monkeypatch):
    smu, responses, acquisition = configured_rig()
    responses[":STAT:OPER:COND?"] = "0"

    clock = iter([0.0, 2.0])
    monkeypatch.setattr(
        "drivers.buffered_acquisition.time.monotonic",
        lambda: next(clock),
    )

    with pytest.raises(TimeoutError):
        acquisition.acquire(timeout_s=1.0)

    smu._write.assert_any_call(":ABOR:ACQ (@1)")