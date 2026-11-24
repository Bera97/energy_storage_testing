"""Experimental simulation evidence, NOT a normal hardware run/export schema.

Exclusive destination; flush each event/sample; no retry of failed writes.
No instrument imports, shutdown, fsync guarantee or transactional snapshot.
"""
import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path
from uuid import uuid4

FIELDS = (
    "record_index", "stage", "host_read_started_s", "host_read_finished_s",
    "channel", "raw_instrument_timestamp_s", "voltage_v", "current_a",
    "requested_power_w", "measured_discharge_power_w", "status",
)
MARKER_TEXT = "SIMULATED constant-power phase finalized; NOT hardware evidence.\n"


def _snapshot(value):
    return json.loads(json.dumps(value, allow_nan=False))


def _write(handle, text):
    if handle.write(text) != len(text):
        raise OSError("Short evidence write.")
    handle.flush()


def _csv_line(values):
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="\n").writerow(values)
    return buffer.getvalue()


class PowerSimulationRecording:
    def __init__(self, directory, *, metadata):
        metadata = _snapshot(metadata)
        self.directory = Path(directory)
        self.failed = False
        self.closed = False
        self.finalized = False
        self.sample_count = 0
        self.event_count = 0
        self.close_errors = []
        self._events = None
        self._samples = None
        self._document = {
            "power_simulation_schema_version": 1,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "simulation_only": True,
            "hardware_execution_authorized": False,
            "metadata": metadata,
            "outcome": {"state": "unfinalized"},
        }
        self.directory.mkdir(parents=True, exist_ok=False)
        try:
            with (self.directory / "power_simulation.json").open("x", encoding="utf-8") as handle:
                _write(handle, json.dumps(self._document, indent=2, allow_nan=False) + "\n")
            self._events = (self.directory / "power_events.jsonl").open("x", encoding="utf-8", newline="\n")
            self._samples = (self.directory / "power_samples.csv").open("x", encoding="utf-8", newline="")
            _write(self._samples, _csv_line(FIELDS))
        except BaseException as error:
            self.failed = True
            try:
                self.close()
            except BaseException as secondary:
                error.add_note(f"Evidence initialization cleanup: {type(secondary).__name__}: {secondary}")
            raise

    def _usable(self):
        if self.failed or self.closed:
            raise RuntimeError("Evidence writer is terminal; do not retry writes.")

    def event(self, event, **details):
        self._usable()
        try:
            document = {"event_index": self.event_count, "event": event, **details}
            _write(self._events, json.dumps(document, allow_nan=False) + "\n")
        except BaseException:
            self.failed = True
            raise
        self.event_count += 1

    def sample(self, frame, *, stage, started_s, finished_s, requested_power_w):
        self._usable()
        try:
            row = {
                "record_index": self.sample_count, "stage": stage,
                "host_read_started_s": started_s, "host_read_finished_s": finished_s,
                **frame, "requested_power_w": requested_power_w,
                "measured_discharge_power_w": -frame["voltage_v"] * frame["current_a"],
            }
            _snapshot(row)
            _write(self._samples, _csv_line([row[key] for key in FIELDS]))
        except BaseException:
            self.failed = True
            raise
        self.sample_count += 1

    def close(self):
        if self.closed:
            return
        self.closed = True
        first = None
        for handle in (self._samples, self._events):
            if handle is None:
                continue
            try:
                handle.close()
            except BaseException as error:
                self.failed = True
                self.close_errors.append(f"{type(error).__name__}: {error}")
                if first is None:
                    first = error
                else:
                    first.add_note(self.close_errors[-1])
        if first is not None:
            raise first

    def _replace(self, outcome):
        document = {
            **self._document,
            "sample_count_flushed": self.sample_count,
            "event_count_flushed": self.event_count,
            "writer_failed": self.failed,
            "close_errors": list(self.close_errors),
            "outcome": _snapshot(outcome),
        }
        text = json.dumps(document, indent=2, allow_nan=False) + "\n"
        temporary = self.directory / f"power_simulation_{uuid4().hex}.tmp"
        with temporary.open("x", encoding="utf-8") as handle:
            _write(handle, text)
        temporary.replace(self.directory / "power_simulation.json")
        self._document = document

    def complete(self, outcome):
        if self.finalized:
            raise RuntimeError("Simulation evidence is already finalized.")
        if self.failed:
            raise RuntimeError("Cannot complete failed evidence.")
        if (outcome.get("stop_reason") != "voltage_min" or
                outcome.get("controller_state") != "completed" or
                outcome.get("shutdown_commands_returned") is not True or
                outcome.get("outputs_reported_off") is not True or
                outcome.get("session_close_returned") is not True):
            raise ValueError("Simulation completion requires cutoff, shutdown and close evidence.")
        self.close()
        self._replace({**outcome, "state": "completed"})
        marker = self.directory / "SIMULATION_COMPLETE"
        created = False
        try:
            with marker.open("x", encoding="utf-8") as handle:
                created = True
                _write(handle, MARKER_TEXT)
        except BaseException as error:
            if created:
                try:
                    marker.unlink(missing_ok=True)
                except BaseException as secondary:
                    error.add_note(f"Partial marker cleanup: {secondary}")
            raise
        self.finalized = True

    def fail(self, error, outcome):
        if self.finalized:
            raise RuntimeError("Simulation evidence is already finalized.")
        try:
            self.close()
        except BaseException as secondary:
            error.add_note(f"Evidence close: {type(secondary).__name__}: {secondary}")
        self._replace({
            **outcome, "state": "raised", "error_type": type(error).__name__,
            "error_message": str(error), "error_notes": list(getattr(error, "__notes__", ())),
        })
        self.finalized = True
