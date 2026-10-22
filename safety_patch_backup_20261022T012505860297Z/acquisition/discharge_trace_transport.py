"""Discharge polling bound to an existing monitored acquisition.

No output enabling is performed.

Caller responsibilities:
- Create the CSV writer before enabling output.
- Configure acquisition successfully with both outputs OFF.
- Verify source configuration and operating limits.
- Select an explicit measurement-time policy.
- Establish the host phase start before output enabling.
- Retain an outer shutdown boundary.
- Close the writer and finalize run metadata.

This adapter finalizes trace retrieval, not the complete recording.
Measurements remain retained in memory.
"""

import time

from experiments.discharge_polling import (
    TracePoll,
    run_discharge_polling,
)
from experiments.discharge_trace_monitor import (
    DischargeTraceMonitor,
)
from experiments.first_sample_trace_monitor import (
    create_trace_monitor,
    trace_timing_evidence,
)


class DischargeTraceTransport:
    """Bind one configured acquisition and one fresh CSV writer."""

    def __init__(self, acquisition, writer):
        self.acquisition = acquisition
        self.writer = writer
        self._saved_count = 0
        self._used = False

    def _check_writer(self, *, fresh=False):
        if self.writer.closed:
            raise RuntimeError("CSV writer is closed.")

        if self.writer.failed:
            raise RuntimeError(
                "CSV writer has already failed."
            )

        if fresh and self.writer.records_flushed != 0:
            raise RuntimeError(
                "A fresh CSV writer is required."
            )

        if self.writer.records_flushed != self._saved_count:
            raise RuntimeError(
                "CSV writer count differs from transport count."
            )

    def _save_batch(self, batch):
        if not batch:
            return

        self._check_writer()
        written = self.writer.write_batch(batch)

        if written != len(batch):
            raise RuntimeError(
                "CSV writer returned an unexpected batch count."
            )

        self._saved_count += written

        if self.writer.records_flushed != self._saved_count:
            raise RuntimeError(
                "CSV flushed count differs after saving."
            )

    def acquire_discharge(
        self,
        phase,
        *,
        instrument_timestamp_origin_s,
        phase_elapsed_at_origin_s,
        maximum_sample_gap_s,
        maximum_voltage_v,
        poll_interval_s=0.05,
        phase_started_s=None,
        clock=time.monotonic,
        sleep=time.sleep,
        measurement_time_basis="explicit_mapping",
    ):
        """Acquire one discharge phase without enabling output.

        Timing policies:
        - explicit_mapping: caller supplies both timestamp origins.
        - first_recorded_sample: both origins must be None and a
          finite host phase start must be supplied.

        In first-sample mode, controller elapsed time is relative
        to the first measurement. The host deadline covers the
        full phase using phase_started_s.

        Execution errors retain partial_readings and receive
        discharge_loop_report when polling has started.

        Setup can fail before execution handling. The caller's
        outer shutdown boundary remains mandatory.
        """
        if self._used:
            raise RuntimeError(
                "Use a fresh transport for each discharge phase."
            )

        acquisition = self.acquisition

        if (
            acquisition._count is None
            or not acquisition._trace_ready
        ):
            raise RuntimeError(
                "Configure monitored acquisition successfully first."
            )

        self._check_writer(fresh=True)

        def monitor_factory(instrument_origin, elapsed_origin):
            return DischargeTraceMonitor(
                phase,
                save_batch=self._save_batch,
                instrument_timestamp_origin_s=instrument_origin,
                phase_elapsed_at_origin_s=elapsed_origin,
                maximum_sample_gap_s=maximum_sample_gap_s,
                maximum_voltage_v=maximum_voltage_v,
            )

        monitor = create_trace_monitor(
            monitor_factory,
            measurement_time_basis=measurement_time_basis,
            instrument_timestamp_origin_s=(
                instrument_timestamp_origin_s
            ),
            phase_elapsed_at_origin_s=phase_elapsed_at_origin_s,
            phase_started_s=phase_started_s,
        )

        def timing_evidence():
            return trace_timing_evidence(
                monitor,
                instrument_timestamp_origin_s=(
                    instrument_timestamp_origin_s
                ),
                phase_elapsed_at_origin_s=(
                    phase_elapsed_at_origin_s
                ),
            )

        records = []
        self._used = True
        acquisition.readings = ()

        def start():
            acquisition._preflight()
            acquisition._trace_ready = False
            acquisition._write(
                f":INIT:ACQ (@{acquisition.channel})"
            )

        def poll():
            state = acquisition._register(":STAT:OPER:COND?")
            acquisition._check_lost_events()
            batch = acquisition._read_new(records)

            idle_bit = 4 if acquisition.channel == 1 else 10

            return TracePoll(
                readings=tuple(batch),
                idle=bool(state & (1 << idle_bit)),
            )

        def shutdown_verified():
            acquisition._smu.shutdown()
            return True

        def finalize_trace():
            acquisition._write(
                f":TRAC{acquisition.channel}:FEED:CONT NEV"
            )

            batch = acquisition._read_new(records)

            if batch:
                self._save_batch(tuple(batch))

            if self._saved_count != len(records):
                raise RuntimeError(
                    "Final CSV count differs from retained records."
                )

        try:
            report = run_discharge_polling(
                monitor=monitor,
                start=start,
                poll=poll,
                shutdown_verified=shutdown_verified,
                finalize=finalize_trace,
                requested_count=acquisition._count,
                timeout_s=phase["maximum_duration_s"],
                poll_interval_s=poll_interval_s,
                phase_started_s=phase_started_s,
                clock=clock,
                sleep=sleep,
            )

            report["retained_count"] = len(records)
            report["records_flushed"] = (
                self.writer.records_flushed
            )
            report["trailing_record_count"] = (
                len(records)
                - report["records_saved_before_finalization"]
            )
            report["fault_record_indices"] = [
                index
                for index, reading in enumerate(records)
                if reading.has_fault
            ]
            report["measurement_timing"] = timing_evidence()

            return report

        except BaseException as original_error:
            original_error.partial_readings = tuple(records)

            loop_report = getattr(
                original_error,
                "discharge_loop_report",
                None,
            )

            if isinstance(loop_report, dict):
                loop_report["retained_count"] = len(records)
                loop_report["records_flushed"] = (
                    self.writer.records_flushed
                )
                loop_report["measurement_timing"] = (
                    timing_evidence()
                )

            raise

        finally:
            acquisition.readings = tuple(records)
            acquisition._trace_ready = False