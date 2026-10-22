"""First-sample-relative clock adapter for a trace monitor.

No hardware communication or file operations.

Measurement elapsed time starts at the first recorded sample.
This is not elapsed time since output enabling.

A separate host phase deadline must cover output enabling,
acquisition startup, polling, and endpoint acceptance.

The factory must:
- Accept the instrument timestamp origin as one argument.
- Construct a fresh monitor without hardware or file side effects.
- Configure phase_elapsed_at_origin_s=0.0.
- Use the same batch-saving callback for every constructed monitor.
"""

import math


def _timestamp(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
    ):
        raise ValueError(
            "First record timestamp must be finite and nonnegative."
        )

    try:
        timestamp = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError(
            "First record timestamp must be finite and nonnegative."
        ) from error

    if not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError(
            "First record timestamp must be finite and nonnegative."
        )

    return timestamp


class FirstSampleTraceMonitor:
    """Single-use adapter exposing the shared polling interface.

    The provisional monitor is discarded before ingesting the first
    valid nonempty batch. No batch is deliberately ingested twice.

    The underlying monitor saves batches, validates timestamps,
    and evaluates measurements.
    """

    def __init__(self, monitor_factory):
        if not callable(monitor_factory):
            raise ValueError("monitor_factory must be callable.")

        self._factory = monitor_factory
        self._monitor = self._new_monitor(0.0)
        self._origin = None
        self._failed = False

    def _new_monitor(self, origin):
        monitor = self._factory(origin)

        if (
            monitor.state != "running"
            or monitor.records_saved != 0
        ):
            raise ValueError(
                "Factory must return a fresh running monitor."
            )

        return monitor

    @property
    def state(self):
        if self._failed:
            return "failed"
        return self._monitor.state

    @property
    def records_saved(self):
        return self._monitor.records_saved

    @property
    def records_evaluated(self):
        return self._monitor.records_evaluated

    @property
    def policy_monitor(self):
        """Underlying monitor owning the endpoint controller."""
        return self._monitor

    @property
    def instrument_timestamp_origin_s(self):
        return self._origin

    @property
    def timing_evidence(self):
        return {
            "measurement_time_basis": "first_recorded_sample",
            "instrument_timestamp_origin_s": self._origin,
            "measurement_elapsed_at_origin_s": (
                0.0 if self._origin is not None else None
            ),
            "output_enable_time_established": False,
            "host_phase_deadline_required": True,
        }

    def ingest(self, readings):
        if self.state != "running":
            raise RuntimeError(
                "Trace monitor is terminal; create a fresh one."
            )

        try:
            batch = tuple(readings)

            if batch and self._origin is None:
                try:
                    origin = _timestamp(batch[0].timestamp_s)
                except (AttributeError, ValueError):
                    # Preserve the batch through the underlying
                    # monitor's save-before-validation path.
                    self._monitor.ingest(batch)
                    raise ValueError(
                        "First record timestamp is invalid."
                    )

                monitor = self._new_monitor(origin)
                self._monitor = monitor
                self._origin = origin

            return self._monitor.ingest(batch)

        except BaseException:
            self._failed = True
            raise


def create_trace_monitor(
    monitor_factory,
    *,
    measurement_time_basis,
    instrument_timestamp_origin_s,
    phase_elapsed_at_origin_s,
    phase_started_s,
):
    """Select explicit mapping or first-sample-relative time.

    monitor_factory accepts:
        (instrument_timestamp_origin_s, measurement_elapsed_origin_s)

    First-sample mode does not claim elapsed time since output
    enabling. Its separate host phase deadline is mandatory.
    """
    if not callable(monitor_factory):
        raise ValueError("monitor_factory must be callable.")

    if measurement_time_basis == "explicit_mapping":
        return monitor_factory(
            instrument_timestamp_origin_s,
            phase_elapsed_at_origin_s,
        )

    if measurement_time_basis != "first_recorded_sample":
        raise ValueError(
            "measurement_time_basis must be explicit_mapping "
            "or first_recorded_sample."
        )

    if (
        instrument_timestamp_origin_s is not None
        or phase_elapsed_at_origin_s is not None
    ):
        raise ValueError(
            "First-sample mode requires both explicit "
            "timestamp origins to be None."
        )

    if (
        isinstance(phase_started_s, bool)
        or not isinstance(phase_started_s, (int, float))
    ):
        raise ValueError(
            "First-sample mode requires a finite host phase start."
        )

    try:
        started = float(phase_started_s)
    except (ValueError, OverflowError) as error:
        raise ValueError(
            "First-sample mode requires a finite host phase start."
        ) from error

    if not math.isfinite(started):
        raise ValueError(
            "First-sample mode requires a finite host phase start."
        )

    return FirstSampleTraceMonitor(
        lambda origin: monitor_factory(origin, 0.0)
    )


def trace_timing_evidence(
    monitor,
    *,
    instrument_timestamp_origin_s,
    phase_elapsed_at_origin_s,
):
    """Snapshot the measurement-time policy without inventing timing."""
    if hasattr(monitor, "measurement_monitor"):
        return monitor.timing_evidence

    if isinstance(monitor, FirstSampleTraceMonitor):
        return monitor.timing_evidence

    return {
        "measurement_time_basis": "explicit_mapping",
        "instrument_timestamp_origin_s": (
            instrument_timestamp_origin_s
        ),
        "phase_elapsed_at_origin_s": phase_elapsed_at_origin_s,
        "mapping_evidence": "caller_supplied",
        "host_phase_deadline_required": True,
    }