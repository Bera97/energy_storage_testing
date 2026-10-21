"""Standalone export of measurement records and run metadata.

No instrument communication.

Each export creates a new run directory and refuses reuse.
COMPLETE is written last. Without it, an export is incomplete.

An I/O failure is propagated and partial files are retained.
This is not a transactional or power-loss-durable storage system.
"""

import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from acquisition.spot_acquisition import SMUReading


CSV_FIELDS = (
    "record_index",
    "channel",
    "instrument_timestamp_s",
    "voltage_v",
    "current_a",
    "signed_power_w",
    "status",
    "in_compliance",
    "has_fault",
)


def save_run(
    base_directory,
    run_id,
    readings,
    *,
    metadata,
    outcome,
):
    """Export a snapshot and return its new directory as a Path.

    run_id: 1-80 ASCII letters, digits, underscores, or hyphens.
    metadata/outcome: dictionaries containing JSON-compatible data.

    outcome should later include stop reason and shutdown outcome.
    COMPLETE means export completed, not that the experiment passed.
    """
    if (
        not isinstance(run_id, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id) is None
    ):
        raise ValueError(
            "run_id must contain 1-80 ASCII letters, digits, "
            "underscores, or hyphens."
        )

    if not isinstance(metadata, dict) or not isinstance(outcome, dict):
        raise TypeError("metadata and outcome must be dictionaries.")

    records = tuple(readings)

    if any(not isinstance(record, SMUReading) for record in records):
        raise TypeError("Every record must be an SMUReading.")

    document = {
        "schema_version": 1,
        "run_id": run_id,
        "exported_at_utc": datetime.now(timezone.utc).isoformat(),
        "record_count": len(records),
        "timestamp_basis": "raw_instrument_time",
        "metadata": metadata,
        "outcome": outcome,
    }

    # Reject unsupported values before creating the run directory.
    metadata_text = json.dumps(
        document,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"

    run_directory = Path(base_directory) / run_id

    # A pre-existing directory/file is an error, never overwritten.
    run_directory.mkdir(parents=True, exist_ok=False)

    csv_path = run_directory / "measurements.csv"

    with csv_path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()

        for index, record in enumerate(records):
            writer.writerow({
                "record_index": index,
                "channel": record.channel,
                "instrument_timestamp_s": record.timestamp_s,
                "voltage_v": record.voltage_v,
                "current_a": record.current_a,
                "signed_power_w": record.power_w,
                "status": record.status,
                "in_compliance": record.in_compliance,
                "has_fault": record.has_fault,
            })

    metadata_path = run_directory / "metadata.json"

    with metadata_path.open("x", encoding="utf-8") as handle:
        handle.write(metadata_text)

    marker_path = run_directory / "COMPLETE"

    with marker_path.open("x", encoding="utf-8") as handle:
        handle.write("Export completed.\n")

    return run_directory