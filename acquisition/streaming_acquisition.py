"""Monitored acquisition with incremental CSV batch saving.

Reuses MonitoredAcquisition's acquisition loop and shutdown policy.
No output enabling is performed.

The caller creates the CSV writer before enabling output and owns
its lifetime. This class does not close the writer.

CSV saving is synchronous. Disk I/O can delay software termination
checks. This is not independent hardware protection.

Measurements are still retained in memory; this is not a
bounded-memory acquisition implementation.
"""

from recording.incremental_csv import IncrementalCSVWriter
from acquisition.monitored_acquisition import MonitoredAcquisition


class StreamingMonitoredAcquisition(MonitoredAcquisition):
    """Write every newly retrieved batch to an incremental CSV.

    Use a fresh writer for each acquisition.

    On CSV failure:
      - The error propagates through the inherited acquire() method.
      - SMU shutdown is attempted by that method.
      - Parsed records remain available as partial_readings.
      - The failed batch must not be automatically retried.
    """

    def __init__(self, smu, channel, *, writer):
        if not isinstance(writer, IncrementalCSVWriter):
            raise TypeError(
                "writer must be an IncrementalCSVWriter."
            )

        super().__init__(smu, channel)
        self.writer = writer

    def _check_writer(self):
        if self.writer.closed:
            raise RuntimeError("Incremental CSV writer is closed.")

        if self.writer.failed:
            raise RuntimeError(
                "Incremental CSV writer has already failed."
            )

        if self.writer.records_flushed != 0:
            raise RuntimeError(
                "A fresh CSV writer is required for each acquisition."
            )

    def _preflight(self):
        self._check_writer()
        super()._preflight()

    def _read_new(self, records):
        if self.writer.records_flushed != len(records):
            raise RuntimeError(
                "CSV/acquisition record counts are inconsistent "
                "before retrieval."
            )

        batch = super()._read_new(records)

        if batch:
            written = self.writer.write_batch(batch)

            if written != len(batch):
                raise RuntimeError(
                    "CSV writer reported an unexpected batch count."
                )

        if self.writer.records_flushed != len(records):
            raise RuntimeError(
                "CSV/acquisition record counts are inconsistent "
                "after retrieval."
            )

        return batch