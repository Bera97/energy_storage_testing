"""Bounded channel-local trace acquisition. Never enables an output.
Caller owns source preparation, phase/CV deadlines, recording finalization,
and global fault cleanup. consume_batch normally is a fresh monitor's ingest
method, which saves the complete batch before evaluating it.
After stopping, save_trailing_batch saves final records without re-entering
a monitor that has already accepted its endpoint.
A bounded batch is not a hard wall-clock bound on a VISA operation.
"""
from dataclasses import dataclass
import math
from acquisition.buffered_acquisition import (
    BufferedAcquisition,
    BufferedStatusError,
    parse_buffer,
)
from drivers.scpi import parse_scpi_bool
CLOCK_AUTO = ":SYST:TIME:TIM:COUN:RES:AUTO"
def configure_shared_trace_clock(session):
    """Disable global timer auto-reset with both outputs OFF. No reset."""
    session._require_connected()
    for channel in (1, 2):
        state = session._channel(channel).get_output_state()
        session.smu.check_errors()
        if state is not False:
            raise RuntimeError(
                "Both outputs must be OFF before changing the shared clock."
            )
    session.smu._write(CLOCK_AUTO + " OFF")
    session.smu.check_errors()
    response = session.smu._query(CLOCK_AUTO + "?")
    session.smu.check_errors()
    if parse_scpi_bool(response, CLOCK_AUTO + "?"):
        raise RuntimeError("Timer auto-reset did not report OFF.")
    return {
        "timer_auto_reset": False,
        "global_timer_reset_issued": False,
    }
@dataclass(frozen=True)
class ChannelBatch:
    readings: tuple
    idle: bool
    pending_records: int
    decision: object
class ChannelTraceAcquisition:
    """One phase, one channel, one bounded batch per poll. No auto-retry."""
    def __init__(
        self,
        session,
        channel,
        consume_batch,
        *,
        save_trailing_batch,
        max_batch_records=256,
    ):
        if type(channel) is not int or channel not in (1, 2):
            raise ValueError("channel must be integer 1 or 2.")
        if not callable(consume_batch):
            raise ValueError("consume_batch must be callable.")
        if not callable(save_trailing_batch):
            raise ValueError("save_trailing_batch must be callable.")
        if (
            type(max_batch_records) is not int
            or not 1 <= max_batch_records <= 1000
        ):
            raise ValueError("max_batch_records must be between 1 and 1000.")
        self.session = session
        self.channel = channel
        self.consume_batch = consume_batch
        self.save_trailing_batch = save_trailing_batch
        self.max_batch_records = max_batch_records
        self.state = "new"
        self.count = None
        self.readings = []

    def _query(self, command):
        self.session._require_connected()
        try:
            response = self.session.smu._query(command)
            self.session.smu.check_errors()
            return response.strip()
        except Exception as error:
            error.add_note(
                f"Dual trace CH{self.channel} query: {command}"
            )
            raise

    def _write(self, command):
        self.session._require_connected()
        try:
            self.session.smu._write(command)
            self.session.smu.check_errors()
        except Exception as error:
            error.add_note(
                f"Dual trace CH{self.channel} write: {command}"
            )
            raise
    def _number(self, command):
        value = float(self._query(command))
        if not math.isfinite(value) or abs(value) >= 9e37:
            raise RuntimeError(f"Invalid numeric readback: {command}")
        return value
    def _integer(self, command, maximum):
        try:
            value = int(self._query(command))
        except ValueError as error:
            raise RuntimeError(
                f"Invalid integer readback: {command}"
            ) from error
        if not 0 <= value <= maximum:
            raise RuntimeError(
                f"Integer readback outside bounds: {command}"
            )
        return value
    def _require_output(self, enabled):
        selected = self.session._channel(self.channel)
        state = selected.get_output_state()
        self.session.smu.check_errors()
        automatic = selected.get_auto_output_on()
        self.session.smu.check_errors()
        if state is not enabled or automatic is not False:
            raise RuntimeError(
                "Selected output state or automatic output-on is invalid."
            )
    def _shared_readback(self):
        if self._query(":FORM:DATA?") != "ASC":
            raise RuntimeError("ASCII data format is required.")
        elements = tuple(
            item.strip().upper()
            for item in self._query(":FORM:ELEM:SENS?").split(",")
        )
        if elements != ("VOLT", "CURR", "TIME", "STAT"):
            raise RuntimeError("Unexpected measurement elements.")
        if parse_scpi_bool(
            self._query(CLOCK_AUTO + "?"),
            CLOCK_AUTO + "?",
        ):
            raise RuntimeError(
                "Dual acquisition requires timer auto-reset OFF."
            )
        for quantity in ("VOLT", "CURR"):
            command = f':SENS{self.channel}:FUNC:STAT? "{quantity}"'
            if not parse_scpi_bool(self._query(command), command):
                raise RuntimeError(f"{quantity} sensing is disabled.")
    def _lost_events(self):
        value = self._integer(":STAT:QUES:COND?", 65535)
        bit = 12 if self.channel == 1 else 14
        if value & (1 << bit):
            raise RuntimeError(
                f"Channel {self.channel}: lost acquisition event."
            )
    def configure(self, count, interval_s):
        if self.state != "new":
            raise RuntimeError("Use a fresh acquisition for each phase.")
        if (
            type(count) is not int
            or not 1 <= count <= BufferedAcquisition.MAX_RECORDS
        ):
            raise ValueError("Invalid record count.")
        if (
            isinstance(interval_s, bool)
            or not isinstance(interval_s, (int, float))
            or not math.isfinite(interval_s)
            or interval_s <= 0
        ):
            raise ValueError("interval_s must be finite and positive.")
        self.state = "failed"
        self._require_output(False)
        self._shared_readback()
        c = self.channel
        base = f":TRIG{c}:ACQ:TIM"
        minimum = self._number(base + "? MIN")
        maximum = self._number(base + "? MAX")
        aperture = self._number(f":SENS{c}:VOLT:APER?")
        if minimum <= 0 or maximum < minimum or aperture <= 0:
            raise RuntimeError("Invalid timer bounds or aperture.")
        if (
            not minimum <= interval_s <= maximum
            or interval_s < aperture
        ):
            raise ValueError(
                "Interval is outside timer bounds or shorter than aperture."
            )
        commands = [
            f":ABOR:ACQ (@{c})",
            f":OUTP{c}:ON:AUTO OFF",
            f":OUTP{c}:OFF:AUTO OFF",
            f":ARM{c}:ACQ:COUN 1",
            f":ARM{c}:ACQ:DEL 0",
            f":ARM{c}:ACQ:SOUR AINT",
            f":ARM{c}:ACQ:BYP OFF",
            f":TRIG{c}:ACQ:BYP OFF",
            f":TRIG{c}:ACQ:DEL 0",
            f":TRIG{c}:ACQ:SOUR TIM",
            f"{base} {interval_s:.12g}",
            f":TRIG{c}:ACQ:COUN {count}",
            f":TRAC{c}:FEED:CONT NEV",
            f":TRAC{c}:CLE",
            f":TRAC{c}:POIN {count}",
            f":TRAC{c}:FEED SENS",
            f":TRAC{c}:TST:FORM ABS",
            f":TRAC{c}:FEED:CONT NEXT",
        ]
        for command in commands:
            self._write(command)
        actual_interval = self._number(base + "?")
        if not math.isclose(
            actual_interval, interval_s, rel_tol=1e-6, abs_tol=1e-9
        ):
            raise RuntimeError("Timer interval readback mismatch.")
        if self._number(f":TRIG{c}:ACQ:COUN?") != count:
            raise RuntimeError("Acquisition count readback mismatch.")
        if self._integer(f":TRAC{c}:POIN:ACT?", count) != 0:
            raise RuntimeError("Trace buffer was not empty.")
        self.count = count
        self.state = "configured"
    def start(self):
        if self.state != "configured":
            raise RuntimeError("Configure successfully before starting.")
        self.state = "failed"
        self._require_output(True)
        self._shared_readback()
        c = self.channel
        for command, expected in (
            (f":TRAC{c}:FEED?", "SENS"),
            (f":TRAC{c}:FEED:CONT?", "NEXT"),
            (f":TRAC{c}:TST:FORM?", "ABS"),
        ):
            if self._query(command).upper() != expected:
                raise RuntimeError(
                    f"Unexpected trace setting: {command}"
                )
        if self._integer(f":TRAC{c}:POIN:ACT?", self.count) != 0:
            raise RuntimeError("Trace contains pre-existing records.")
        self._lost_events()
        self._write(f":INIT:ACQ (@{c})")
        self.state = "active"
    def poll(self):
        if self.state not in ("active", "stopping"):
            raise RuntimeError("Acquisition is not active or draining.")
        response = None
        try:
            self._require_output(self.state == "active")
            self._shared_readback()
            self._lost_events()
            status = self._integer(":STAT:OPER:COND?", 65535)
            available = self._integer(
                f":TRAC{self.channel}:POIN:ACT?",
                self.count,
            )
            if available < len(self.readings):
                raise RuntimeError("Trace count decreased.")
            size = min(
                available - len(self.readings),
                self.max_batch_records,
            )
            batch = ()
            decision = None
            if size:
                response = self._query(
                    f":TRAC{self.channel}:DATA? "
                    f"{len(self.readings)},{size}"
                )
                try:
                    batch = tuple(
                        parse_buffer(response, self.channel, size)
                    )
                except BufferedStatusError as error:
                    batch = tuple(error.readings)
                if (
                    self.readings
                    and batch[0].timestamp_s
                    <= self.readings[-1].timestamp_s
                ):
                    raise RuntimeError(
                        "Timestamps did not increase across batches."
                    )
                self.readings.extend(batch)
                if self.state == "active":
                    decision = self.consume_batch(batch)
                else:
                    written = self.save_trailing_batch(batch)
                    if type(written) is not int or written != len(batch):
                        raise RuntimeError(
                            "Trailing batch save count mismatch."
                        )
            pending = available - len(self.readings)
            bit = 4 if self.channel == 1 else 10
            idle = bool(status & (1 << bit)) and pending == 0
            if self.state == "stopping" and pending == 0:
                self.state = "drained"
            return ChannelBatch(batch, idle, pending, decision)
        except BaseException as error:
            self.state = "failed"
            error.partial_readings = tuple(self.readings)
            if response is not None:
                error.raw_trace_response = response
            raise
    def stop(self):
        if self.count is None:
            raise RuntimeError("No configured acquisition to stop.")
        self.state = "failed"
        self.session.stop_channel(self.channel)
        self._write(f":TRAC{self.channel}:FEED:CONT NEV")
        self.state = "stopping"
