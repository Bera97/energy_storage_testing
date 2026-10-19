"""Incremental measurement CSV writer.

No instrument communication.

Creates a new file using exclusive mode.
Flushes the header and every non-empty batch.

After a write/flush failure, further batches are rejected.
A failed batch may already be partly or fully present on disk:
do not automatically retry it.

No metadata or COMPLETE marker is managed here.
Flushing does not guarantee power-loss durability.
"""

import csv
from pathlib import Path

from drivers.run_data import CSV_FIELDS
from drivers.spot_acquisition import SMUReading


class IncrementalCSVWriter:
    """Single-threaded CSV writer for batches of SMUReading records.

    The parent directory must already exist.

    records_flushed counts records from batches whose write and flush
    both returned successfully. It is not a disk-durability guarantee.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.records_flushed = 0
        self.failed = False
        self._handle = None
        self._writer = None

        handle = self.path.open(
            "x",
            encoding="utf-8",
            newline="",
        )
        self._handle = handle

        try:
            self._writer = csv.DictWriter(
                handle,
                fieldnames=CSV_FIELDS,
            )
            self._writer.writeheader()
            self._flush()

        except BaseException as original_error:
            self.failed = True

            try:
                handle.close()
            except Exception as close_error:
                original_error.add_note(
                    "CSV close after initialization failure also failed: "
                    f"{type(close_error).__name__}: {close_error}"
                )

            raise

    @property
    def closed(self):
        return self._handle is None or self._handle.closed

    def _flush(self):
        self._handle.flush()

    @staticmethod
    def _row(index, reading):
        return {
            "record_index": index,
            "channel": reading.channel,
            "instrument_timestamp_s": reading.timestamp_s,
            "voltage_v": reading.voltage_v,
            "current_a": reading.current_a,
            "signed_power_w": reading.power_w,
            "status": reading.status,
            "in_compliance": reading.in_compliance,
            "has_fault": reading.has_fault,
        }

    def write_batch(self, readings):
        """Write and flush one batch; return its record count.

        Input is materialized and type-checked before writing.
        Empty batches do not write or flush.

        I/O failures propagate and make this writer unusable
        for further batches.
        """
        if self.closed:
            raise RuntimeError("CSV writer is closed.")

        if self.failed:
            raise RuntimeError(
                "CSV writer previously failed; do not retry batches."
            )

        records = tuple(readings)

        if any(
            not isinstance(record, SMUReading)
            for record in records
        ):
            raise TypeError("Every record must be an SMUReading.")

        if not records:
            return 0

        start_index = self.records_flushed

        try:
            self._writer.writerows(
                self._row(start_index + offset, record)
                for offset, record in enumerate(records)
            )
            self._flush()

        except BaseException:
            self.failed = True
            raise

        self.records_flushed += len(records)
        return len(records)

    def close(self):
        """Close the file; propagate a close failure."""
        if self.closed:
            return

        try:
            self._handle.close()
        except BaseException:
            self.failed = True
            raise

    def __enter__(self):
        if self.closed or self.failed:
            raise RuntimeError("CSV writer is not usable.")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_value is None:
            self.close()
        else:
            try:
                self.close()
            except Exception as close_error:
                exc_value.add_note(
                    "CSV close also failed: "
                    f"{type(close_error).__name__}: {close_error}"
                )

        return False