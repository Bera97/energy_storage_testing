"""Offline coverage for bounded channel-local trace acquisition."""
import re
import pytest
from acquisition.dual_trace import (
    CLOCK_AUTO,
    ChannelTraceAcquisition,
    configure_shared_trace_clock,
)
from experiments.dual_instrument_io import DualInstrumentSession
from experiments.dual_preconditioning import build_dual_plan
from experiments.discharge_trace_monitor import DischargeTraceMonitor
from recording.incremental_csv import IncrementalCSVWriter
import test_dual_instrument_io as io_tests
pair = io_tests.pair
class TraceSMU(io_tests.FakeSMU):
    def __init__(self):
        super().__init__()
        self.samples = {1: [], 2: []}
        self.counts = {1: 10, 2: 10}
        self.intervals = {1: 0.2, 2: 0.2}
        self.feeds = {1: "NEXT", 2: "NEXT"}
        self.queries = []
        self.auto_reset = "0"
        self.questionable = 0
        self.operating = (1 << 4) | (1 << 10)
    def _write(self, command):
        super()._write(command)
        match = re.fullmatch(r":TRIG([12]):ACQ:COUN (\d+)", command)
        if match:
            self.counts[int(match[1])] = int(match[2])
        match = re.fullmatch(r":TRIG([12]):ACQ:TIM (.+)", command)
        if match:
            self.intervals[int(match[1])] = float(match[2])
        match = re.fullmatch(r":TRAC([12]):FEED:CONT (\w+)", command)
        if match:
            self.feeds[int(match[1])] = match[2]
        match = re.fullmatch(r":TRAC([12]):CLE", command)
        if match:
            self.samples[int(match[1])] = []
        if command == CLOCK_AUTO + " OFF":
            self.auto_reset = "0"
    def _query(self, command):
        self.queries.append(command)
        fixed = {
            ":FORM:DATA?": "ASC",
            ":FORM:ELEM:SENS?": "VOLT,CURR,TIME,STAT",
            CLOCK_AUTO + "?": self.auto_reset,
            ":STAT:QUES:COND?": str(self.questionable),
            ":STAT:OPER:COND?": str(self.operating),
        }
        if command in fixed:
            return fixed[command]
        if ":FUNC:STAT?" in command:
            return "1"
        if command.endswith("? MIN"):
            return "0.00002"
        if command.endswith("? MAX"):
            return "100000"
        if command.endswith(":VOLT:APER?"):
            return "0.02"
        match = re.fullmatch(r":TRIG([12]):ACQ:TIM\?", command)
        if match:
            return str(self.intervals[int(match[1])])
        match = re.fullmatch(r":TRIG([12]):ACQ:COUN\?", command)
        if match:
            return str(self.counts[int(match[1])])
        match = re.fullmatch(r":TRAC([12]):POIN:ACT\?", command)
        if match:
            return str(len(self.samples[int(match[1])]))
        match = re.fullmatch(r":TRAC([12]):DATA\? (\d+),(\d+)", command)
        if match:
            channel, start, count = map(int, match.groups())
            records = self.samples[channel][start:start + count]
            return ",".join(str(value) for row in records for value in row)
        match = re.fullmatch(r":TRAC([12]):FEED:CONT\?", command)
        if match:
            return self.feeds[int(match[1])]
        if command.endswith(":FEED?"):
            return "SENS"
        if command.endswith(":TST:FORM?"):
            return "ABS"
        raise AssertionError(f"Unexpected query: {command}")
@pytest.fixture
def rig(pair):
    driver = TraceSMU()
    session = DualInstrumentSession(
        build_dual_plan(pair),
        "TESTSERIAL",
        smu_factory=lambda *args, **kwargs: driver,
    )
    session.connect()
    try:
        yield session, driver
    finally:
        session.close()
def make_reader(rig, *, channel=1, consume=None, trailing=None, chunk=2):
    session, driver = rig
    for number in (1, 2):
        selected = driver.chan1 if number == 1 else driver.chan2
        selected.on = number != channel
        selected.auto = False
    saved = []
    def save_tail(batch):
        saved.extend(batch)
        return len(batch)
    reader = ChannelTraceAcquisition(
        session,
        channel,
        saved.extend if consume is None else consume,
        save_trailing_batch=save_tail if trailing is None else trailing,
        max_batch_records=chunk,
    )
    reader.configure(10, 0.2)
    return reader, driver, saved
@pytest.mark.parametrize("channel", [1, 2])
def test_configuration_only_writes_selected_channel(rig, channel):
    _, driver, _ = make_reader(rig, channel=channel)
    peer = 2 if channel == 1 else 1
    for command in driver.commands:
        assert not command.startswith((
            ":FORM", ":SYST",
            f":OUTP{peer}", f":TRAC{peer}",
            f":TRIG{peer}", f":ARM{peer}",
        ))
    selected = driver.chan2 if peer == 2 else driver.chan1
    assert selected.on is True
    assert driver.shutdown_calls == 0
    assert f":ABOR:ACQ (@{channel})" in driver.commands
def test_poll_reads_bounded_chunks_without_duplicates(rig):
    reader, driver, saved = make_reader(rig)
    driver.chan1.on = True
    reader.start()
    driver.samples[1] = [
        (1, 0.001, index * 0.2, 0)
        for index in range(5)
    ]
    results = [reader.poll() for _ in range(3)]
    assert [len(item.readings) for item in results] == [2, 2, 1]
    assert [item.pending_records for item in results] == [3, 1, 0]
    assert [item.idle for item in results] == [False, False, True]
    assert len(saved) == 5
    assert driver.chan2.on is True
    data_queries = [
        query for query in driver.queries if query.startswith(":TRAC1:DATA? ")
    ]
    assert data_queries == [
        ":TRAC1:DATA? 0,2",
        ":TRAC1:DATA? 2,2",
        ":TRAC1:DATA? 4,1",
    ]
def test_start_never_enables_output(rig):
    reader, driver, _ = make_reader(rig)
    with pytest.raises(RuntimeError, match="output state"):
        reader.start()
    assert driver.chan1.on is False
    assert not any(":INIT:" in cmd for cmd in driver.commands)
def test_auto_reset_must_remain_disabled(rig):
    reader, driver, _ = make_reader(rig)
    driver.chan1.on = True
    driver.auto_reset = "1"
    with pytest.raises(RuntimeError, match="auto-reset OFF"):
        reader.start()
    assert not any(":INIT:" in cmd for cmd in driver.commands)
def test_selected_lost_event_is_fatal(rig):
    reader, driver, _ = make_reader(rig)
    driver.chan1.on = True
    reader.start()
    driver.questionable = 1 << 12
    with pytest.raises(RuntimeError, match="lost acquisition"):
        reader.poll()
    assert reader.state == "failed"
def test_peer_lost_event_is_not_mislabelled_as_selected_event(rig):
    reader, driver, _ = make_reader(rig)
    driver.chan1.on = True
    reader.start()
    driver.questionable = 1 << 14
    reader.poll()
    assert reader.state == "active"
def test_count_decrease_is_fatal(rig):
    reader, driver, _ = make_reader(rig)
    driver.chan1.on = True
    reader.start()
    driver.samples[1] = [(1, 0.001, 0, 0)]
    reader.poll()
    driver.samples[1] = []
    with pytest.raises(RuntimeError, match="count decreased"):
        reader.poll()
def test_consumer_failure_preserves_readings_and_cannot_retry(rig):
    def fail(batch):
        raise OSError("synthetic CSV failure")
    reader, driver, _ = make_reader(rig, consume=fail)
    driver.chan1.on = True
    reader.start()
    driver.samples[1] = [(1, 0.001, 0, 0)]
    with pytest.raises(OSError) as caught:
        reader.poll()
    assert len(caught.value.partial_readings) == 1
    assert reader.state == "failed"
    with pytest.raises(RuntimeError):
        reader.poll()
def test_stop_and_drain_do_not_reenter_endpoint_monitor(rig):
    active = []
    trailing = []
    def save_tail(batch):
        trailing.extend(batch)
        return len(batch)
    reader, driver, _ = make_reader(
        rig, consume=active.extend, trailing=save_tail
    )
    driver.chan1.on = True
    reader.start()
    driver.samples[1] = [(1, 0.001, 0, 0)]
    reader.poll()
    reader.stop()
    driver.samples[1].append((1, 0.001, 0.2, 0))
    reader.poll()
    assert len(active) == 1
    assert len(trailing) == 1
    assert reader.state == "drained"
    assert driver.chan1.on is False
    assert driver.chan2.on is True
def test_real_discharge_monitor_saves_csv_and_returns_endpoint(
    rig, tmp_path
):
    writer = IncrementalCSVWriter(tmp_path / "measurements.csv")
    phase = {
        "type": "discharge_cc",
        "current_magnitude_a": 0.01,
        "cutoff_voltage_v": 0.5,
        "maximum_duration_s": 10,
    }
    monitor = DischargeTraceMonitor(
        phase,
        save_batch=writer.write_batch,
        instrument_timestamp_origin_s=0.0,
        phase_elapsed_at_origin_s=0.0,
        maximum_sample_gap_s=0.5,
        maximum_voltage_v=4.2,
    )
    try:
        reader, driver, _ = make_reader(
            rig,
            consume=monitor.ingest,
            trailing=writer.write_batch,
        )
        driver.chan1.on = True
        reader.start()
        driver.samples[1] = [
            (1.0, -0.01, 0.0, 1),
            (0.4, -0.01, 0.2, 1),
        ]
        result = reader.poll()
        assert result.decision.stop_reason == "voltage_min"
        assert writer.records_flushed == 2
        assert monitor.state == "completed"
        reader.stop()
        reader.poll()
        assert reader.state == "drained"
        assert driver.chan2.on is True
    finally:
        writer.close()
def test_shared_clock_cannot_be_changed_with_active_output(rig):
    session, driver = rig
    with pytest.raises(RuntimeError, match="Both outputs"):
        configure_shared_trace_clock(session)
    assert driver.commands == []
def test_shared_clock_is_disabled_without_resetting_counter(rig):
    session, driver = rig
    session.shutdown_all()
    driver.auto_reset = "1"
    evidence = configure_shared_trace_clock(session)
    assert evidence["timer_auto_reset"] is False
    assert evidence["global_timer_reset_issued"] is False
    assert driver.commands == [CLOCK_AUTO + " OFF"]
