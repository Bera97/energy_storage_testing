"""Finite, instrument-timed acquisition for constant DC output."""

import math
import time
from typing import TYPE_CHECKING

from drivers.scpi import parse_scpi_bool
from drivers.smu_channel import SMUChannel
from acquisition.spot_acquisition import (
    SMUReading,
    SpotAcquisition,
    parse_reading,
)

if TYPE_CHECKING:
    from drivers.keysight_b2902b import KeysightB2902B


class BufferedStatusError(RuntimeError):
    """Buffered records contain documented measurement fault flags."""

    def __init__(self, readings: list[SMUReading]):
        self.readings = tuple(readings)
        self.fault_indices = tuple(
            index
            for index, reading in enumerate(readings)
            if reading.has_fault
        )

        super().__init__(
            "Buffered acquisition contains measurement faults at "
            f"zero-based record indices {self.fault_indices}."
        )


def parse_buffer(
    response: str,
    channel: int,
    expected_count: int,
) -> list[SMUReading]:
    """Parse interleaved VOLT,CURR,TIME,STAT records."""
    if (
        type(expected_count) is not int
        or expected_count <= 0
    ):
        raise ValueError("expected_count must be a positive integer.")

    fields = response.strip().split(",")

    if len(fields) != expected_count * 4:
        raise RuntimeError(
            f"Expected {expected_count * 4} data fields for "
            f"{expected_count} records; received {len(fields)}."
        )

    readings = [
        parse_reading(
            ",".join(fields[index:index + 4]),
            channel,
        )
        for index in range(0, len(fields), 4)
    ]

    if any(
        later.timestamp_s <= earlier.timestamp_s
        for earlier, later in zip(readings, readings[1:])
    ):
        raise RuntimeError(
            "Buffered timestamps are not strictly increasing. "
            "Inspect timing and transmitted timestamp precision."
        )

    if any(reading.has_fault for reading in readings):
        raise BufferedStatusError(readings)

    return readings


class BufferedAcquisition:
    """Finite timer acquisition using the instrument data buffer.

    configure() requires both outputs OFF.
    acquire() requires the selected output already ON.

    No output-on command is sent by this class.
    Successful acquisition leaves the output unchanged.

    On acquisition failure, acquisition abort is attempted.
    Caller must still arrange smu.shutdown() in a finally block.

    Single controller/thread only. No spot queries, other INIT calls,
    or configuration changes are permitted during acquisition.
    """

    MAX_RECORDS = 2500

    def __init__(self, smu: "KeysightB2902B", channel: int):
        if type(channel) is not int or channel not in (1, 2):
            raise ValueError("channel must be integer 1 or 2.")

        self._smu = smu
        self.channel = channel
        self._count = None
        self._interval_s = None

    @property
    def _selected(self):
        return (
            self._smu.chan1
            if self.channel == 1
            else self._smu.chan2
        )

    def _write(self, command):
        self._smu._write(command)
        self._smu.check_errors()

    def _query(self, command):
        response = self._smu._query(command)
        self._smu.check_errors()
        return response

    def _number(self, command):
        return SMUChannel._parse_number(
            self._query(command), command
        )

    def _register(self, command):
        response = self._query(command)

        try:
            value = int(response)
        except ValueError as error:
            raise RuntimeError(
                f"Invalid register response: {command}: {response!r}"
            ) from error

        if not 0 <= value <= 65535:
            raise RuntimeError(
                f"Invalid register value: {command}: {value}"
            )

        return value

    def _check_lost_events(self):
        value = self._register(":STAT:QUES:COND?")

        # Questionable Condition: acquire-event-lost flags.
        bit = 12 if self.channel == 1 else 14

        if value & (1 << bit):
            raise RuntimeError(
                f"Channel {self.channel}: instrument reports "
                "a lost acquisition arm/trigger event."
            )

    def configure(
        self,
        count: int,
        interval_s: float,
        nplc: float = 1.0,
    ) -> None:
        """Configure finite timer acquisition with outputs OFF.

        Leaves source levels, source ranges, measurement ranges,
        and sense wiring unchanged.

        Interval >= aperture is a necessary check, not a guarantee
        that all ranging/settling overhead fits the timer period.
        """
        if type(count) is not int or not 1 <= count <= self.MAX_RECORDS:
            raise ValueError(
                f"count must be an integer from 1 to {self.MAX_RECORDS}."
            )

        interval = SMUChannel._positive_number(
            interval_s, "interval_s"
        )

        # Invalidate previous configuration if this attempt fails.
        self._count = None
        self._interval_s = None

        for channel in (self._smu.chan1, self._smu.chan2):
            state = channel.get_output_state()
            self._smu.check_errors()

            if state:
                raise RuntimeError(
                    "Both outputs must be OFF before configuration."
                )

        self._write(f":ABOR:ACQ (@{self.channel})")

        SpotAcquisition(self._smu, self.channel).configure(nplc)

        # Register responses are interpreted as decimal integers.
        self._write(":FORM:SREG ASC")

        base = f":TRIG{self.channel}:ACQ:TIM"
        minimum = self._number(f"{base}? MIN")
        maximum = self._number(f"{base}? MAX")

        if minimum <= 0 or maximum < minimum:
            raise RuntimeError("Invalid instrument timer bounds.")

        if not minimum <= interval <= maximum:
            raise ValueError(
                f"interval_s must be between {minimum} and {maximum}."
            )

        aperture = self._number(
            f":SENS{self.channel}:VOLT:APER?"
        )

        if aperture <= 0 or interval < aperture:
            raise ValueError(
                f"interval_s={interval} is shorter than "
                f"the measurement aperture {aperture} s."
            )

        commands = (
            f":OUTP{self.channel}:ON:AUTO OFF",
            f":OUTP{self.channel}:OFF:AUTO OFF",
            f":ARM{self.channel}:ACQ:COUN 1",
            f":ARM{self.channel}:ACQ:DEL 0",
            f":ARM{self.channel}:ACQ:SOUR AINT",
            f":ARM{self.channel}:ACQ:BYP OFF",
            f":TRIG{self.channel}:ACQ:BYP OFF",
            f":TRIG{self.channel}:ACQ:DEL 0",
            f":TRIG{self.channel}:ACQ:SOUR TIM",
            f":TRIG{self.channel}:ACQ:TIM {interval:.12g}",
            f":TRIG{self.channel}:ACQ:COUN {count}",
        )

        for command in commands:
            self._write(command)

        actual_interval = self._number(
            f":TRIG{self.channel}:ACQ:TIM?"
        )
        actual_count = self._number(
            f":TRIG{self.channel}:ACQ:COUN?"
        )

        if not math.isclose(
            actual_interval, interval, rel_tol=1e-6, abs_tol=1e-9
        ):
            raise RuntimeError(
                f"Timer readback mismatch: {actual_interval}."
            )

        if actual_count != count:
            raise RuntimeError(
                f"Acquisition-count readback mismatch: {actual_count}."
            )

        self._count = count
        self._interval_s = actual_interval

    def acquire(
        self,
        timeout_s: float | None = None,
    ) -> list[SMUReading]:
        """Start acquisition, wait for idle, and fetch all records.

        timeout_s bounds completion polling, not a native VISA call
        that fails to return. Individual I/O uses the VISA timeout.

        Does not disable the source output after acquisition.
        """
        if self._count is None:
            raise RuntimeError("Call configure() successfully first.")

        if timeout_s is None:
            timeout_s = self._count * self._interval_s + 5.0

        timeout = SMUChannel._positive_number(
            timeout_s, "timeout_s"
        )

        state = self._selected.get_output_state()
        self._smu.check_errors()
        if not state:
            raise RuntimeError("Selected output must already be ON.")

        automatic = self._selected.get_auto_output_on()
        self._smu.check_errors()
        if automatic:
            raise RuntimeError("Automatic output-on must be OFF.")

        # Confirm the expected result format before starting.
        if self._query(":FORM:DATA?") != "ASC":
            raise RuntimeError("Acquisition requires ASCII format.")

        elements = tuple(
            item.strip().upper()
            for item in self._query(":FORM:ELEM:SENS?").split(",")
        )
        if elements != ("VOLT", "CURR", "TIME", "STAT"):
            raise RuntimeError("Unexpected acquisition result format.")

        for quantity in ("VOLT", "CURR"):
            command = (
                f':SENS{self.channel}:FUNC:STAT? "{quantity}"'
            )
            if not parse_scpi_bool(self._query(command), command):
                raise RuntimeError(
                    f"{quantity} measurement is disabled."
                )

        self._check_lost_events()

        try:
            self._write(f":INIT:ACQ (@{self.channel})")
            deadline = time.monotonic() + timeout
            idle_bit = 4 if self.channel == 1 else 10

            while True:
                state = self._register(":STAT:OPER:COND?")

                if state & (1 << idle_bit):
                    break

                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Instrument acquisition did not complete "
                        "within the requested timeout."
                    )

                time.sleep(0.02)

            self._check_lost_events()

            response = self._query(
                f":FETC:ARR? (@{self.channel})"
            )

            return parse_buffer(
                response, self.channel, self._count
            )

        except BaseException as original_error:
            try:
                self._write(f":ABOR:ACQ (@{self.channel})")
            except Exception as abort_error:
                original_error.add_note(
                    "Acquisition abort failed: "
                    f"{type(abort_error).__name__}: {abort_error}"
                )
            raise