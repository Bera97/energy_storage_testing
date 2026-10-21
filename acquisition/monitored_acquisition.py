"""Finite live-trace acquisition with software termination checks.

Single controller/thread only.

configure() requires both outputs OFF.
acquire() requires the selected output already ON.

acquire() attempts shutdown of BOTH channels on every exit path.
It never enables output.

Software limits are not independent hardware protection.
Polling timeout does not interrupt a blocked VISA operation.
"""

from dataclasses import dataclass
import math
import time

from acquisition.buffered_acquisition import (
    BufferedAcquisition,
    BufferedStatusError,
    parse_buffer,
)
from drivers.shutdown_reporting import (
    attempt_shutdown_with_report,
)
from acquisition.spot_acquisition import SMUReading


@dataclass(frozen=True)
class StopLimits:
    """Inclusive signed limits: stop at <= minimum or >= maximum."""

    min_voltage_v: float | None = None
    max_voltage_v: float | None = None
    min_current_a: float | None = None
    max_current_a: float | None = None
    stop_on_compliance: bool = True

    def __post_init__(self):
        for name in (
            "min_voltage_v",
            "max_voltage_v",
            "min_current_a",
            "max_current_a",
        ):
            value = getattr(self, name)

            if value is not None:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                ):
                    raise ValueError(
                        f"{name} must be finite or None."
                    )

        if type(self.stop_on_compliance) is not bool:
            raise ValueError("stop_on_compliance must be bool.")

        for lower, upper in (
            (self.min_voltage_v, self.max_voltage_v),
            (self.min_current_a, self.max_current_a),
        ):
            if lower is not None and upper is not None:
                if lower >= upper:
                    raise ValueError(
                        "Each minimum must be less than its maximum."
                    )

    def reason_for(self, reading):
        # Priority applies within each measurement record.
        if reading.has_fault:
            return "measurement_fault"

        if self.stop_on_compliance and reading.in_compliance:
            return "compliance"

        checks = (
            (
                self.min_voltage_v,
                reading.voltage_v,
                "voltage_min",
                True,
            ),
            (
                self.max_voltage_v,
                reading.voltage_v,
                "voltage_max",
                False,
            ),
            (
                self.min_current_a,
                reading.current_a,
                "current_min",
                True,
            ),
            (
                self.max_current_a,
                reading.current_a,
                "current_max",
                False,
            ),
        )

        for limit, value, reason, lower in checks:
            if limit is not None:
                if (lower and value <= limit) or (
                    not lower and value >= limit
                ):
                    return reason

        return None


@dataclass(frozen=True)
class MonitoredResult:
    readings: tuple[SMUReading, ...]
    stop_reason: str
    triggering_reading: SMUReading | None
    decision_elapsed_s: float
    shutdown_elapsed_s: float
    requested_count: int

    @property
    def fault_readings(self):
        return tuple(
            reading
            for reading in self.readings
            if reading.has_fault
        )


class MonitoredAcquisition(BufferedAcquisition):
    """Live trace acquisition with explicit shutdown.

    Retained data:
    - Success: result.readings
    - Exception: exception.partial_readings
    - Either case: acquisition.readings

    Acquisition exceptions also receive exception.shutdown_report,
    describing the exception handler's shutdown attempt.

    A successful return means shutdown() completed without raising.
    This is reported instrument state, not an isolation guarantee.

    Do not mix other acquisition/configuration operations into a run.
    """

    def __init__(self, smu, channel):
        super().__init__(smu, channel)
        self._trace_ready = False
        self.readings = ()

    def configure(self, count, interval_s, nplc=1.0):
        self._trace_ready = False
        super().configure(count, interval_s, nplc)

        prefix = f":TRAC{self.channel}"

        for command in (
            f"{prefix}:FEED:CONT NEV",
            f"{prefix}:CLE",
            f"{prefix}:POIN {count}",
            f"{prefix}:FEED SENS",
            f"{prefix}:TST:FORM ABS",
            f"{prefix}:FEED:CONT NEXT",
        ):
            self._write(command)

        if self._trace_count() != 0:
            raise RuntimeError("Trace buffer was not empty.")

        self._trace_ready = True

    def _trace_count(self):
        command = f":TRAC{self.channel}:POIN:ACT?"
        response = self._query(command)

        try:
            count = int(response)
        except ValueError as error:
            raise RuntimeError(
                f"Invalid trace count: {response!r}"
            ) from error

        if count < 0:
            raise RuntimeError("Negative trace count.")

        return count

    def _read_new(self, records):
        available = self._trace_count()

        if not len(records) <= available <= self._count:
            raise RuntimeError(
                f"Unexpected trace count {available}; "
                f"retained={len(records)}, requested={self._count}."
            )

        count = available - len(records)

        if count == 0:
            return []

        response = self._query(
            f":TRAC{self.channel}:DATA? {len(records)},{count}"
        )

        # Preserve well-formed fault records for policy evaluation.
        try:
            batch = parse_buffer(response, self.channel, count)
        except BufferedStatusError as error:
            batch = list(error.readings)

        if (
            records
            and batch[0].timestamp_s <= records[-1].timestamp_s
        ):
            raise RuntimeError(
                "Trace timestamps did not increase across batches."
            )

        records.extend(batch)
        self.readings = tuple(records)
        return batch

    def _preflight(self):
        if not self._trace_ready or self._count is None:
            raise RuntimeError("Call configure() successfully first.")

        if not self._selected.get_output_state():
            raise RuntimeError(
                "Selected output must already be ON."
            )
        self._smu.check_errors()

        if self._selected.get_auto_output_on():
            raise RuntimeError("Automatic output-on must be OFF.")
        self._smu.check_errors()

        other = (
            self._smu.chan2
            if self.channel == 1
            else self._smu.chan1
        )

        if other.get_output_state():
            raise RuntimeError("Other channel must remain OFF.")
        self._smu.check_errors()

        if self._query(":FORM:DATA?") != "ASC":
            raise RuntimeError("ASCII result format is required.")

        elements = tuple(
            item.strip().upper()
            for item in self._query(
                ":FORM:ELEM:SENS?"
            ).split(",")
        )

        if elements != ("VOLT", "CURR", "TIME", "STAT"):
            raise RuntimeError("Unexpected result elements.")

        if self._query(
            f":TRAC{self.channel}:FEED?"
        ).upper() != "SENS":
            raise RuntimeError("Trace feed must be SENS.")

        if self._query(
            f":TRAC{self.channel}:FEED:CONT?"
        ).upper() != "NEXT":
            raise RuntimeError("Trace storage must be enabled.")

        if self._query(
            f":TRAC{self.channel}:TST:FORM?"
        ).upper() != "ABS":
            raise RuntimeError(
                "Absolute trace timestamps required."
            )

        if self._trace_count() != 0:
            raise RuntimeError(
                "Trace contains pre-existing records."
            )

        self._check_lost_events()

    def acquire(
        self,
        limits=None,
        *,
        timeout_s=None,
        poll_interval_s=0.05,
    ):
        """Run once; attempt shutdown before returning or raising.

        Reconfigure with outputs OFF before each subsequent run.

        Current limits are signed, not absolute-magnitude limits.
        Extra records acquired before shutdown are retained.

        On an exception:
        - partial_readings contains retained parsed records.
        - shutdown_report describes exception-handler cleanup.
        """
        records = []
        self.readings = ()
        started = time.monotonic()

        try:
            if limits is None:
                limits = StopLimits()

            if not isinstance(limits, StopLimits):
                raise TypeError("limits must be StopLimits.")

            poll = self._positive_interval(
                poll_interval_s,
                "poll_interval_s",
            )

            self._preflight()

            if timeout_s is None:
                timeout_s = (
                    self._count * self._interval_s + 5.0
                )

            timeout = self._positive_interval(
                timeout_s,
                "timeout_s",
            )

            self._trace_ready = False
            requested_count = self._count

            self._write(
                f":INIT:ACQ (@{self.channel})"
            )

            deadline = started + timeout
            idle_bit = 4 if self.channel == 1 else 10

            reason = None
            trigger = None

            while reason is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Monitored acquisition timed out."
                    )

                state = self._register(
                    ":STAT:OPER:COND?"
                )
                self._check_lost_events()
                batch = self._read_new(records)

                for reading in batch:
                    candidate = limits.reason_for(reading)

                    if candidate is not None:
                        reason = candidate
                        trigger = reading
                        break

                if reason is not None:
                    break

                if state & (1 << idle_bit):
                    reason = "completed"
                    break

                time.sleep(poll)

            decision_elapsed = time.monotonic() - started

            # Disable output before final retrieval or reporting.
            self._smu.shutdown()
            shutdown_elapsed = time.monotonic() - started

            self._write(
                f":TRAC{self.channel}:FEED:CONT NEV"
            )
            self._read_new(records)

            # A final batch can arrive between polling and shutdown.
            if reason == "completed":
                for reading in records:
                    candidate = limits.reason_for(reading)

                    if candidate is not None:
                        reason = candidate
                        trigger = reading
                        break

                if len(records) != requested_count:
                    raise RuntimeError(
                        "Acquisition became idle with "
                        f"{len(records)}/{requested_count} records."
                    )

            return MonitoredResult(
                readings=tuple(records),
                stop_reason=reason,
                triggering_reading=trigger,
                decision_elapsed_s=decision_elapsed,
                shutdown_elapsed_s=shutdown_elapsed,
                requested_count=requested_count,
            )

        except BaseException as original_error:
            self.readings = tuple(records)
            original_error.partial_readings = self.readings

            attempt_shutdown_with_report(
                self._smu,
                original_error,
            )
            raise

        finally:
            self._trace_ready = False

    @staticmethod
    def _positive_interval(value, name):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(
                f"{name} must be finite and positive."
            )

        return float(value)