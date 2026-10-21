"""Structured voltage/current spot acquisition for the B2902B."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math
from typing import TYPE_CHECKING

from drivers.scpi import parse_scpi_bool

if TYPE_CHECKING:
    from drivers.keysight_b2902b import KeysightB2902B


@dataclass(frozen=True)
class SMUReading:
    """One instrument measurement record.

    timestamp_s is raw instrument time, not PC time.
    Continuity across independent spot queries is not assumed.
    """

    channel: int
    voltage_v: float
    current_a: float
    timestamp_s: float
    status: int

    @property
    def power_w(self) -> float:
        return self.voltage_v * self.current_a

    @property
    def current_source(self) -> bool:
        return bool(self.status & 1)

    @property
    def in_compliance(self) -> bool:
        return bool(self.status & 0b110)

    @property
    def over_voltage(self) -> bool:
        return bool(self.status & (1 << 3))

    @property
    def over_current(self) -> bool:
        return bool(self.status & (1 << 4))

    @property
    def over_temperature(self) -> bool:
        return bool(self.status & (1 << 5))

    @property
    def range_overflow(self) -> bool:
        return bool(self.status & (1 << 13))

    @property
    def has_fault(self) -> bool:
        return any((
            self.over_voltage,
            self.over_current,
            self.over_temperature,
            self.range_overflow,
        ))


class MeasurementStatusError(RuntimeError):
    """A measurement record contains a documented fault flag."""

    def __init__(self, reading: SMUReading):
        self.reading = reading

        flags = []
        if reading.over_voltage:
            flags.append("overvoltage")
        if reading.over_current:
            flags.append("overcurrent")
        if reading.over_temperature:
            flags.append("overtemperature")
        if reading.range_overflow:
            flags.append("measurement-range overflow")

        super().__init__(
            f"Channel {reading.channel}: {', '.join(flags)}; "
            f"status={reading.status}."
        )


def parse_reading(response: str, channel: int) -> SMUReading:
    """Parse VOLT,CURR,TIME,STAT ASCII data.

    Status flags are retained. Unknown bits are not interpreted.
    """
    if type(channel) is not int or channel not in (1, 2):
        raise ValueError("channel must be integer 1 or 2.")

    fields = [field.strip() for field in response.strip().split(",")]

    if len(fields) != 4:
        raise RuntimeError(
            f"Expected VOLT,CURR,TIME,STAT; received {response!r}."
        )

    values = []

    for label, field in zip(
        ("voltage", "current", "timestamp"), fields[:3]
    ):
        try:
            value = float(field)
        except ValueError as error:
            raise RuntimeError(
                f"Invalid {label} field: {field!r}."
            ) from error

        if not math.isfinite(value) or abs(value) >= 9e37:
            raise RuntimeError(
                f"Invalid {label} field: {field!r}."
            )

        values.append(value)

    if values[2] < 0:
        raise RuntimeError("Instrument timestamp must be non-negative.")

    try:
        numeric_status = Decimal(fields[3])

        if (
            not numeric_status.is_finite()
            or numeric_status < 0
            or numeric_status > 0xFFFFFFFF
            or numeric_status != numeric_status.to_integral_value()
        ):
            raise ValueError("Expected a non-negative integer status.")

        status = int(numeric_status)

    except (InvalidOperation, ValueError, OverflowError) as error:
        raise RuntimeError(
            f"Invalid status field: {fields[3]!r}."
        ) from error

    return SMUReading(
        channel=channel,
        voltage_v=values[0],
        current_a=values[1],
        timestamp_s=values[2],
        status=status,
    )


class SpotAcquisition:
    """Acquire structured records using an existing SMU connection.

    Uses instrument-wide ASCII/data-element settings.
    Do not share the instrument with another configuration-changing
    thread, script, or controller.

    read() requires output already ON and automatic output-on OFF.
    It never sends output ON.

    Caller remains responsible for shutdown on exceptions.
    """

    ELEMENTS = ("VOLT", "CURR", "TIME", "STAT")

    def __init__(self, smu: "KeysightB2902B", channel: int):
        if type(channel) is not int or channel not in (1, 2):
            raise ValueError("channel must be integer 1 or 2.")

        self._smu = smu
        self.channel = channel

    @property
    def _selected(self):
        return (
            self._smu.chan1
            if self.channel == 1
            else self._smu.chan2
        )

    def _checked_query(self, command):
        response = self._smu._query(command)
        self._smu.check_errors()
        return response

    def configure(self, nplc: float = 1.0) -> None:
        """Configure paired spot records while both outputs are OFF.

        Leaves source settings, ranges, and sense wiring unchanged.
        """
        for channel in (self._smu.chan1, self._smu.chan2):
            state = channel.get_output_state()
            self._smu.check_errors()

            if state:
                raise RuntimeError(
                    "Both outputs must be OFF before changing "
                    "instrument-wide result formatting."
                )

        self._selected.configure_spot_measurement(nplc)

        self._smu._write(
            ":FORM:ELEM:SENS VOLT,CURR,TIME,STAT"
        )
        self._smu.check_errors()
        self._verify_format()

    def _verify_format(self):
        if self._checked_query(":FORM:DATA?") != "ASC":
            raise RuntimeError("Paired spot acquisition requires ASCII.")

        response = self._checked_query(":FORM:ELEM:SENS?")
        elements = tuple(
            field.strip().upper()
            for field in response.split(",")
        )

        if elements != self.ELEMENTS:
            raise RuntimeError(
                "Expected result elements VOLT,CURR,TIME,STAT; "
                f"received {response!r}."
            )

    def read(self) -> SMUReading:
        """Acquire one record and raise on documented fault flags.

        Compliance alone is returned as a flag, not raised.
        Does not automatically shut down outputs.
        """
        self._smu.check_errors()

        state = self._selected.get_output_state()
        self._smu.check_errors()

        if not state:
            raise RuntimeError(
                "Output must already be ON for paired spot acquisition."
            )

        automatic = self._selected.get_auto_output_on()
        self._smu.check_errors()

        if automatic:
            raise RuntimeError("Automatic output-on must be OFF.")

        self._verify_format()

        for quantity in ("VOLT", "CURR"):
            command = (
                f':SENS{self.channel}:FUNC:STAT? "{quantity}"'
            )
            response = self._checked_query(command)
            enabled = parse_scpi_bool(response, command)

            if not enabled:
                raise RuntimeError(
                    f"{quantity} measurement function is not enabled "
                    f"(raw response: {response!r})."
                )

        response = self._checked_query(
            f":MEAS? (@{self.channel})"
        )
        reading = parse_reading(response, self.channel)

        if reading.has_fault:
            raise MeasurementStatusError(reading)

        return reading