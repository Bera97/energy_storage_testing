"""Hardware-free bridge from trace batches to discharge decisions.

No driver imports, hardware communication, or file creation.

The saving callback must preserve the whole batch and raise on any
unsuccessful write. Failed batches must not be automatically retried.

This component does not enforce deadlines while no measurements
arrive. The acquisition loop must enforce a host-monotonic deadline
and attempt shutdown on every exit.

Timestamp mapping:
    phase elapsed =
        phase_elapsed_at_origin_s
        + record timestamp
        - instrument_timestamp_origin_s
"""

from dataclasses import dataclass
import math

from experiments.discharge_controller import (
    DischargeController,
    DischargeVoltageError,
)


class DischargeMeasurementGapError(RuntimeError):
    """Recorded sample spacing exceeds the permitted gap."""


class DischargeSourceModeError(RuntimeError):
    """A record does not report current-source selection."""


def _finite_number(value, name):
    if isinstance(value, bool) or not isinstance(
        value, (int, float)
    ):
        raise ValueError(f"{name} must be a finite number.")

    try:
        result = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError(
            f"{name} must be a finite number."
        ) from error

    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number.")

    return result


def _nonnegative_number(value, name):
    result = _finite_number(value, name)

    if result < 0:
        raise ValueError(f"{name} must be nonnegative.")

    return result


def _positive_number(value, name):
    result = _finite_number(value, name)

    if result <= 0:
        raise ValueError(f"{name} must be positive.")

    return result


@dataclass(frozen=True)
class DischargeBatchDecision:
    records_saved: int
    records_evaluated: int
    stop_reason: str | None
    triggering_record_index: int | None


class DischargeTraceMonitor:
    """Single-use discharge monitor with full-batch preservation.

    Expected reading attributes are supplied by SMUReading:
        timestamp_s
        voltage_v
        current_a
        status
        in_compliance
        has_fault
    """

    def __init__(
        self,
        phase,
        *,
        save_batch,
        instrument_timestamp_origin_s,
        phase_elapsed_at_origin_s,
        maximum_sample_gap_s,
        maximum_voltage_v,
    ):
        if not callable(save_batch):
            raise ValueError("save_batch must be callable.")

        self.controller = DischargeController(phase)
        self._save_batch = save_batch

        self._instrument_origin = _nonnegative_number(
            instrument_timestamp_origin_s,
            "instrument_timestamp_origin_s",
        )
        self._phase_origin = _nonnegative_number(
            phase_elapsed_at_origin_s,
            "phase_elapsed_at_origin_s",
        )
        self._maximum_gap = _positive_number(
            maximum_sample_gap_s,
            "maximum_sample_gap_s",
        )
        self._maximum_voltage = _positive_number(
            maximum_voltage_v,
            "maximum_voltage_v",
        )

        if (
            self._maximum_voltage
            <= self.controller.cutoff_voltage_v
        ):
            raise ValueError(
                "maximum_voltage_v must exceed "
                "the discharge cutoff."
            )

        self.state = "running"
        self.records_saved = 0
        self.records_evaluated = 0
        self._last_timestamp = None

    def ingest(self, readings):
        """Save a whole batch, then evaluate its endpoint policy.

        Timestamp validation covers the complete saved batch.
        Endpoint policy evaluates only through the first accepted
        cutoff. Later records remain saved for outcome review.
        """
        if self.state != "running":
            raise RuntimeError(
                "Discharge trace monitor is terminal; "
                "create a fresh one."
            )

        try:
            return self._ingest(tuple(readings))
        except BaseException:
            self.state = "failed"
            raise

    def _ingest(self, batch):
        if not batch:
            return self._decision(None, None)

        first_index = self.records_saved

        self._save_batch(batch)
        self.records_saved += len(batch)

        previous = self._last_timestamp
        elapsed_values = []

        for reading in batch:
            timestamp = _nonnegative_number(
                reading.timestamp_s,
                "record timestamp_s",
            )

            if previous is not None:
                gap = timestamp - previous

                if gap <= 0:
                    raise ValueError(
                        "Trace timestamps must increase strictly."
                    )

                if gap > self._maximum_gap:
                    raise DischargeMeasurementGapError(
                        f"Recorded sample gap {gap:.12g} s exceeds "
                        f"{self._maximum_gap:.12g} s."
                    )

            elapsed = (
                self._phase_origin
                + (timestamp - self._instrument_origin)
            )

            elapsed_values.append(
                _nonnegative_number(
                    elapsed,
                    "mapped phase elapsed_s",
                )
            )
            previous = timestamp

        self._last_timestamp = previous

        for offset, (reading, elapsed) in enumerate(
            zip(batch, elapsed_values)
        ):
            # Let the controller give a reported fault priority.
            if reading.has_fault:
                self.controller.update(
                    elapsed_s=elapsed,
                    voltage_v=reading.voltage_v,
                    current_a=reading.current_a,
                    in_compliance=reading.in_compliance,
                    has_fault=reading.has_fault,
                )

            status = reading.status

            if (
                type(status) is not int
                or not 0 <= status <= 0xFFFFFFFF
            ):
                raise ValueError(
                    "Reading status must be an unsigned "
                    "32-bit integer."
                )

            if not status & 1:
                raise DischargeSourceModeError(
                    "Discharge record does not report "
                    "current-source selection."
                )

            voltage = _finite_number(
                reading.voltage_v,
                "reading.voltage_v",
            )

            if voltage > self._maximum_voltage:
                raise DischargeVoltageError(
                    "Discharge voltage exceeds the configured "
                    "software ceiling."
                )

            decision = self.controller.update(
                elapsed_s=elapsed,
                voltage_v=voltage,
                current_a=reading.current_a,
                in_compliance=reading.in_compliance,
                has_fault=reading.has_fault,
            )
            self.records_evaluated += 1

            if decision.stop_reason is not None:
                self.state = "completed"
                return self._decision(
                    decision.stop_reason,
                    first_index + offset,
                )

        return self._decision(None, None)

    def _decision(self, stop_reason, triggering_index):
        return DischargeBatchDecision(
            records_saved=self.records_saved,
            records_evaluated=self.records_evaluated,
            stop_reason=stop_reason,
            triggering_record_index=triggering_index,
        )