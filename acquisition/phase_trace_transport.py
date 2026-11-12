"""Phase polling bound to configured monitored trace acquisition.
No output enabling is performed. The caller must configure acquisition
and source settings with outputs OFF, create the writer, establish the
host phase start before output enabling, and retain an outer shutdown
boundary. This transport finalizes trace retrieval, not the complete
recording. The caller owns writer closure and metadata finalization.
Only one controller/thread may use the instrument.
"""
import time
from experiments.charge_trace_monitor import ChargeTraceMonitor
from experiments.discharge_trace_monitor import DischargeTraceMonitor
from experiments.phase_polling import (
    TracePoll,
    run_charge_polling,
    run_discharge_polling,
)
from experiments.trace_timing import (
    PhaseSafetyMonitor,
    create_trace_monitor,
    trace_timing_evidence,
)


class _PhaseTraceTransport:
    """Shared single-use acquisition and CSV lifecycle."""

    def __init__(self, acquisition, writer):
        self.acquisition = acquisition
        self.writer = writer
        self._saved_count = 0
        self._used = False

    def _check_writer(self, *, fresh=False):
        if self.writer.closed:
            raise RuntimeError('CSV writer is closed.')
        if self.writer.failed:
            raise RuntimeError('CSV writer has already failed.')
        if fresh and self.writer.records_flushed != 0:
            raise RuntimeError('A fresh CSV writer is required.')
        if self.writer.records_flushed != self._saved_count:
            raise RuntimeError('CSV writer count differs from transport count.')

    def _save_batch(self, batch):
        if not batch:
            return
        self._check_writer()
        written = self.writer.write_batch(batch)
        if written != len(batch):
            raise RuntimeError('CSV writer returned an unexpected batch count.')
        self._saved_count += written
        if self.writer.records_flushed != self._saved_count:
            raise RuntimeError('CSV flushed count differs after saving.')

    def _acquire(
        self,
        phase,
        *,
        instrument_timestamp_origin_s,
        phase_elapsed_at_origin_s,
        maximum_sample_gap_s,
        poll_interval_s=0.05,
        phase_started_s=None,
        clock=time.monotonic,
        sleep=time.sleep,
        measurement_time_basis='explicit_mapping',
        _monitor_factory,
        _polling,
        _loop_report_attribute,
        _reuse_error,
    ):
        """Run one configured trace lifecycle without enabling output. Preserve complete retrieved batches and retain the caller's outer shutdown boundary."""
        if self._used:
            raise RuntimeError(_reuse_error)
        acquisition = self.acquisition
        if acquisition._count is None or not acquisition._trace_ready:
            raise RuntimeError('Configure monitored acquisition successfully first.')
        self._check_writer(fresh=True)
        monitor = create_trace_monitor(
            _monitor_factory,
            measurement_time_basis=measurement_time_basis,
            instrument_timestamp_origin_s=instrument_timestamp_origin_s,
            phase_elapsed_at_origin_s=phase_elapsed_at_origin_s,
            phase_started_s=phase_started_s,
        )

        def timing_evidence():
            return trace_timing_evidence(
                monitor,
                instrument_timestamp_origin_s=instrument_timestamp_origin_s,
                phase_elapsed_at_origin_s=phase_elapsed_at_origin_s,
            )
        monitor = PhaseSafetyMonitor(
            monitor,
            phase_started_s=phase_started_s,
            maximum_duration_s=phase['maximum_duration_s'],
            clock=clock,
            measurement_time_basis=measurement_time_basis,
            instrument_timestamp_origin_s=instrument_timestamp_origin_s,
            phase_elapsed_at_origin_s=phase_elapsed_at_origin_s,
        )
        records = []
        self._used = True
        acquisition.readings = ()

        def start():
            acquisition._preflight()
            monitor.before_initiation()
            acquisition._trace_ready = False
            acquisition._write(f':INIT:ACQ (@{acquisition.channel})')

        def poll():
            state = acquisition._register(':STAT:OPER:COND?')
            acquisition._check_lost_events()
            batch = acquisition._read_new(records)
            idle_bit = 4 if acquisition.channel == 1 else 10
            return TracePoll(readings=tuple(batch), idle=bool(state & 1 << idle_bit))

        def shutdown_verified():
            acquisition._smu.shutdown()
            return True

        def finalize_trace():
            acquisition._write(f':TRAC{acquisition.channel}:FEED:CONT NEV')
            batch = acquisition._read_new(records)
            if batch:
                self._save_batch(tuple(batch))
            if self._saved_count != len(records):
                raise RuntimeError('Final CSV count differs from retained records.')
        try:
            report = _polling(
                monitor=monitor,
                start=start,
                poll=poll,
                shutdown_verified=shutdown_verified,
                finalize=finalize_trace,
                requested_count=acquisition._count,
                timeout_s=phase['maximum_duration_s'],
                poll_interval_s=poll_interval_s,
                phase_started_s=phase_started_s,
                clock=clock,
                sleep=sleep,
            )
            report['retained_count'] = len(records)
            report['records_flushed'] = self.writer.records_flushed
            report['trailing_record_count'] = len(records) - report['records_saved_before_finalization']
            report['fault_record_indices'] = [index for index, reading in enumerate(records) if reading.has_fault]
            report['measurement_timing'] = timing_evidence()
            return report
        except BaseException as original_error:
            original_error.partial_readings = tuple(records)
            loop_report = getattr(original_error, _loop_report_attribute, None)
            if isinstance(loop_report, dict):
                loop_report['retained_count'] = len(records)
                loop_report['records_flushed'] = self.writer.records_flushed
                loop_report['measurement_timing'] = timing_evidence()
            raise
        finally:
            acquisition.readings = tuple(records)
            acquisition._trace_ready = False


class ChargeTraceTransport(_PhaseTraceTransport):
    """Bind the shared lifecycle to charge policy."""

    def acquire_charge(
        self,
        phase,
        *,
        cv_classifier,
        instrument_timestamp_origin_s,
        phase_elapsed_at_origin_s,
        maximum_sample_gap_s,
        poll_interval_s=0.05,
        phase_started_s=None,
        clock=time.monotonic,
        sleep=time.sleep,
        measurement_time_basis='explicit_mapping',
    ):
        """Acquire one charge phase without enabling output.

        Timing policies:
        - explicit_mapping: caller supplies both timestamp origins.
        - first_recorded_sample: both origins must be None and a
          finite host phase start must be supplied.

        In first-sample mode, controller elapsed time is relative
        to the first measurement. The host deadline covers the
        full phase using phase_started_s.

        Execution errors retain partial_readings and receive
        charge_loop_report when the polling loop has started.

        Setup can fail before execution handling. The caller's
        outer shutdown boundary remains mandatory.
        """

        def monitor_factory(instrument_origin, elapsed_origin):
            return ChargeTraceMonitor(
                phase,
                save_batch=self._save_batch,
                cv_classifier=cv_classifier,
                instrument_timestamp_origin_s=instrument_origin,
                phase_elapsed_at_origin_s=elapsed_origin,
                maximum_sample_gap_s=maximum_sample_gap_s,
            )
        return self._acquire(
            phase,
            instrument_timestamp_origin_s=instrument_timestamp_origin_s,
            phase_elapsed_at_origin_s=phase_elapsed_at_origin_s,
            maximum_sample_gap_s=maximum_sample_gap_s,
            poll_interval_s=poll_interval_s,
            phase_started_s=phase_started_s,
            clock=clock,
            sleep=sleep,
            measurement_time_basis=measurement_time_basis,
            _monitor_factory=monitor_factory,
            _polling=run_charge_polling,
            _loop_report_attribute='charge_loop_report',
            _reuse_error='Use a fresh transport for each charge phase.',
        )


class DischargeTraceTransport(_PhaseTraceTransport):
    """Bind the shared lifecycle to discharge policy."""

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
        measurement_time_basis='explicit_mapping',
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

        def monitor_factory(instrument_origin, elapsed_origin):
            return DischargeTraceMonitor(
                phase,
                save_batch=self._save_batch,
                instrument_timestamp_origin_s=instrument_origin,
                phase_elapsed_at_origin_s=elapsed_origin,
                maximum_sample_gap_s=maximum_sample_gap_s,
                maximum_voltage_v=maximum_voltage_v,
            )
        return self._acquire(
            phase,
            instrument_timestamp_origin_s=instrument_timestamp_origin_s,
            phase_elapsed_at_origin_s=phase_elapsed_at_origin_s,
            maximum_sample_gap_s=maximum_sample_gap_s,
            poll_interval_s=poll_interval_s,
            phase_started_s=phase_started_s,
            clock=clock,
            sleep=sleep,
            measurement_time_basis=measurement_time_basis,
            _monitor_factory=monitor_factory,
            _polling=run_discharge_polling,
            _loop_report_attribute='discharge_loop_report',
            _reuse_error='Use a fresh transport for each discharge phase.',
        )
