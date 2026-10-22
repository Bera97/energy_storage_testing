"""Hardware-free bridge from trace batches to charge decisions.

No driver imports, hardware communication, or file creation.

The caller supplies:
- A batch-saving callback that preserves the entire batch.
- A validated CV classifier for individual measurement records.
- An explicit mapping from instrument time to phase elapsed time.
- A maximum permitted gap between consecutive recorded samples.

This component does not enforce deadlines while no records arrive.
The future acquisition loop must enforce a separate host-monotonic
phase deadline and must attempt shutdown on every exit.

A saving callback must raise on an unsuccessful write. Failed
batches must not be retried through this monitor.
"""

from dataclasses import dataclass
import math

from experiments.charge_controller import ChargeController


class MeasurementGapError(RuntimeError):
    """Consecutive recorded samples exceed the permitted time gap."""


def _number(value, name, *, positive=False):
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

    if result < 0 or (positive and result == 0):
        raise ValueError(
            f"{name} must be "
            f"{'positive' if positive else 'nonnegative'}."
        )

    return result


@dataclass(frozen=True)
class ChargeBatchDecision:
    records_saved: int
    records_evaluated: int
    stop_reason: str | None
    triggering_record_index: int | None


class ChargeTraceMonitor:
    """Single-use charge monitor with full-batch preservation.

    After successful termination or any ingest error, create a fresh
    monitor for another charge phase.
    """

    def __init__(
        self,
        phase,
        *,
        save_batch,
        cv_classifier,
        instrument_timestamp_origin_s,
        phase_elapsed_at_origin_s,
        maximum_sample_gap_s,
    ):
        if not callable(save_batch):
            raise ValueError("save_batch must be callable.")

        if not callable(cv_classifier):
            raise ValueError("cv_classifier must be callable.")

        self.controller = ChargeController(phase)
        self._save_batch = save_batch
        self._cv_classifier = cv_classifier

        self._instrument_origin = _number(
            instrument_timestamp_origin_s,
            "instrument_timestamp_origin_s",
        )
        self._phase_origin = _number(
            phase_elapsed_at_origin_s,
            "phase_elapsed_at_origin_s",
        )
        self._maximum_gap = _number(
            maximum_sample_gap_s,
            "maximum_sample_gap_s",
            positive=True,
        )

        self.state = "running"
        self.records_saved = 0
        self.records_evaluated = 0
        self._last_timestamp = None

    def ingest(self, readings):
        """Save a whole batch, then evaluate until the first endpoint.

        All records in the batch are saved, including records after a
        triggering measurement.

        Timestamp validation covers the complete saved batch.
        Direction and fault decisions are evaluated chronologically
        only up to the first successful termination.

        The saving callback receives an immutable tuple of records.
        Its return value is ignored; failure must raise.
        """
        if self.state != "running":
            raise RuntimeError(
                "Trace monitor is terminal; create a fresh one."
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

        # Preserve the entire retrieved batch before policy checks.
        self._save_batch(batch)
        self.records_saved += len(batch)

        previous = self._last_timestamp
        elapsed_values = []

        for reading in batch:
            timestamp = _number(
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
                    raise MeasurementGapError(
                        f"Recorded sample gap {gap:.12g} s exceeds "
                        f"{self._maximum_gap:.12g} s."
                    )

            elapsed = (
                self._phase_origin
                + (timestamp - self._instrument_origin)
            )
            elapsed_values.append(
                _number(elapsed, "mapped phase elapsed_s")
            )
            previous = timestamp

        self._last_timestamp = previous

        for offset, (reading, elapsed) in enumerate(
            zip(batch, elapsed_values)
        ):
            cv_active = self._cv_classifier(reading)

            decision = self.controller.update(
                elapsed_s=elapsed,
                current_a=reading.current_a,
                cv_active=cv_active,
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
        return ChargeBatchDecision(
            records_saved=self.records_saved,
            records_evaluated=self.records_evaluated,
            stop_reason=stop_reason,
            triggering_record_index=triggering_index,
        )