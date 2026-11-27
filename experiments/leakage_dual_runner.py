"""Constant-voltage leakage holds on BOTH channels of one instrument session.
Both channels run the same profile (only the channel and the specimen differ) on one host
loop: at every scheduled slot channel 1 is read, then channel 2. Any failure stops BOTH
channels. The single-channel runner (experiments.leakage_runner) is not changed by this module.
A completed run is a record of logging, not evidence of instrument or DUT qualification.
"""
import csv
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import time
from types import SimpleNamespace
from acquisition.spot_acquisition import MeasurementStatusError
from experiments.leakage_planning import validate_leakage_test
from experiments.leakage_ranging import StepDownRanger
from experiments.leakage_runner import (
    COLUMNS, CSV_NAME, CURRENT_MARGIN, FSYNC_INTERVAL_S, MAXIMUM_LOGGED_EVENTS,
    METADATA_INTERVAL_S, METADATA_NAME, MINIMUM_LATENESS_LIMIT_S, RETRY_DELAY_S,
    LeakageRunError, _error, _overflow_only, _sha256, _write_json, planned_samples,
)
BATCH_NAME = "dual_leakage.json"
MARKER_NAME = "DUAL_LEAKAGE_COMPLETE"
def _comparable(document):
    copy = json.loads(json.dumps(document))
    copy["instrument"]["channel"] = 0
    copy["dut"]["specimen_id"] = ""
    copy["experiment"]["name"] = ""
    copy["output"].pop("file_name", None)
    return copy
def validate_dual_documents(documents):
    """Return (documents ordered by channel, validated leakage plans) for a channel 1 + 2 pair."""
    if not isinstance(documents, (list, tuple)) or len(documents) != 2:
        raise ValueError("Dual leakage needs exactly two profiles.")
    ordered = sorted(documents, key=lambda document: document["instrument"]["channel"])
    if [document["instrument"]["channel"] for document in ordered] != [1, 2]:
        raise ValueError("Dual leakage needs one profile for channel 1 and one for channel 2.")
    specimens = [document["dut"]["specimen_id"] for document in ordered]
    if specimens[0] == specimens[1] or any(str(item).startswith("REPLACE_") for item in specimens):
        raise ValueError("Dual leakage needs two different, non-placeholder specimen IDs.")
    if _comparable(ordered[0]) != _comparable(ordered[1]):
        raise ValueError(
            "Dual leakage profiles must be identical except instrument.channel, "
            "dut.specimen_id, experiment.name and output.file_name.")
    validated = [validate_leakage_test(document) for document in ordered]
    if any(item["leakage"]["early_stop"]["enabled"] for item in validated):
        raise ValueError("Early stop is not supported for dual leakage runs.")
    return ordered, validated
def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number.")
    return float(value)
def enable_dual_dc_output(smu, channel, *, max_voltage_v, max_current_a):
    """Enable one channel while its peer may already be ON; the peer is never commanded."""
    if type(channel) is not int or channel not in (1, 2):
        raise ValueError("channel must be integer 1 or 2.")
    voltage_ceiling = _positive(max_voltage_v, "max_voltage_v")
    current_ceiling = _positive(max_current_a, "max_current_a")
    selected, peer = (smu.chan1, smu.chan2) if channel == 1 else (smu.chan2, smu.chan1)
    def checked(operation, *arguments):
        result = operation(*arguments)
        smu.check_errors()
        return result
    def require_text(command, expected):
        response = checked(smu._query, command)
        if response != expected:
            raise RuntimeError(f"{command}: expected {expected!r}, received {response!r}.")
    smu.check_errors()
    if checked(selected.get_output_state):
        raise RuntimeError(f"Channel {channel} already reports output ON.")
    peer_before = checked(peer.get_output_state)
    if type(peer_before) is not bool:
        raise RuntimeError("Peer output state must be an actual boolean.")
    checked(smu._write, f":ABOR:ALL (@{channel})")
    if checked(selected.get_auto_output_on):
        raise RuntimeError("Automatic output-on must be OFF.")
    if checked(selected.get_output_off_mode) != "HIZ":
        raise RuntimeError("Output-off mode must be HIZ.")
    require_text(f":SOUR{channel}:FUNC:SHAP?", "DC")
    source_function = checked(selected.get_source_mode)
    require_text(f":SOUR{channel}:{source_function}:MODE?", "FIX")
    if source_function == "VOLT":
        voltage = abs(checked(selected.get_voltage_setpoint))
        current = checked(selected.get_current_limit)
        if current <= 0:
            raise RuntimeError("Current compliance must be greater than zero.")
    else:
        current = abs(checked(selected.get_current_setpoint))
        voltage = checked(selected.get_voltage_limit)
        if voltage <= 0:
            raise RuntimeError("Voltage compliance must be greater than zero.")
    if voltage > voltage_ceiling:
        raise RuntimeError(
            f"Voltage setting/compliance {voltage:.12g} V exceeds the caller ceiling "
            f"{voltage_ceiling:.12g} V.")
    if current > current_ceiling:
        raise RuntimeError(
            f"Current setting/compliance {current:.12g} A exceeds the caller ceiling "
            f"{current_ceiling:.12g} A.")
    try:
        checked(smu._write, f":OUTP{channel} ON")
        if not checked(selected.get_output_state):
            raise RuntimeError(f"Channel {channel} did not report output ON.")
        if checked(peer.get_output_state) is not peer_before:
            raise RuntimeError(f"Peer output changed while enabling channel {channel}.")
    except BaseException as original_error:
        try:
            selected.disable_output()
            smu.check_errors()
            if checked(selected.get_output_state):
                raise RuntimeError("Output still reports ON after rollback.")
        except Exception as cleanup_error:
            original_error.add_note(
                f"Output-off rollback could not be verified: {type(cleanup_error).__name__}: "
                f"{cleanup_error}")
        raise
class _Channel:
    """One channel's recording, ranging and safety state."""
    def __init__(self, document, validated, directory, context):
        from experiments.config import require_charge_monitoring_settings
        base, leak = validated["preparation_config"], validated["leakage"]
        monitoring = require_charge_monitoring_settings(base)
        ratings = base["dut"]["ratings"]
        self.document, self.base, self.leak, self.context = document, base, leak, context
        self.number = base["instrument"]["channel"]
        control = leak["range_control"]
        self.ranger = StepDownRanger(control) if control["mode"] == "step_down" else None
        self.limits = {
            "minimum_voltage_v": float(ratings["minimum_voltage_v"]),
            "maximum_voltage_v": float(ratings["maximum_voltage_v"]),
            "hold_upper_v": leak["hold_voltage_v"] + float(monitoring["voltage_tolerance_v"]),
            "current_a": leak["compliance_current_a"] * CURRENT_MARGIN,
        }
        self.range_changed = False
        self.spot = None
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=False)
        self.csv_path = directory / CSV_NAME
        self.metadata_path = directory / METADATA_NAME
        early = leak["early_stop"]
        self.meta = {
            "leakage_run_schema_version": 1,
            "state": "running",
            "experiment_name": document["experiment"]["name"],
            "specimen_id": base["dut"]["specimen_id"],
            "channel": self.number,
            "hold_voltage_v": leak["hold_voltage_v"],
            "compliance_current_a": leak["compliance_current_a"],
            "planned_sample_count": leak["total_sample_count"],
            "planned_duration_s": leak["maximum_duration_s"],
            "recorded_sample_count": 0,
            "elapsed_s": None,
            "maximum_lateness_s": 0.0,
            "created_utc": context.utc_now().isoformat(),
            "hold_started_utc": None,
            "finished_utc": None,
            "source": None,
            "read_retry_count": 0,
            "read_retry_events": [],
            "range_control": {**control, "events": [], "increase_count": 0,
                              "current_range_a": None, "auto_range_restored": None},
            "early_stop": {**early, "triggered": False, "stop_offset_s": None, "checks": []},
            "error": None,
            "cleanup_error": None,
            "shutdown_verified": None,
            "csv_name": CSV_NAME,
            "csv_sha256": None,
            "hardware_qualified": False,
            "configuration": json.loads(json.dumps(document)),
            "leakage": leak,
            "dual": {"batch_directory": directory.parent.name, "peer_channel": 3 - self.number,
                     "fault_policy": "stop_both"},
        }
        self.handle = open(self.csv_path, "w", encoding="utf-8", newline="")
        self.writer = csv.writer(self.handle)
        self.writer.writerow(COLUMNS)
        self.handle.flush()
        self.save()
    def save(self):
        _write_json(self.metadata_path, self.meta)
    def log_range(self, action, index, offset, previous, new, readback, current):
        events = self.meta["range_control"]["events"]
        if len(events) < MAXIMUM_LOGGED_EVENTS:
            events.append({"sample_index": index, "offset_s": offset, "action": action,
                           "from_range_a": previous, "to_range_a": new,
                           "readback_a": readback, "current_a": current})
        self.meta["range_control"]["current_range_a"] = new
    def step_up(self, index, offset):
        previous = self.ranger.range_a
        new = self.ranger.overflow(offset)
        readback = self.context.set_range(self.context.smu, self.number, new)
        self.meta["range_control"]["increase_count"] = self.ranger.increases
        self.log_range("step_up", index, offset, previous, new, readback, None)
        self.range_changed = True
    def update_range(self, reading, index, offset):
        previous = self.ranger.range_a
        new = (self.ranger.initial_step(reading.current_a) if index == 0
               else self.ranger.observe(reading.current_a, offset))
        if new is None:
            return
        readback = self.context.set_range(self.context.smu, self.number, new)
        self.log_range("step_down", index, offset, previous, new, readback, reading.current_a)
        self.range_changed = True
    def read_sample(self, index, offset):
        failures = 0
        while True:
            try:
                return self.spot.read(), failures
            except MeasurementStatusError as caught:
                if not (self.ranger is not None and _overflow_only(caught.reading)):
                    raise
                self.step_up(index, offset)
            except Exception as caught:
                if self.ranger is not None and "Invalid current field" in str(caught):
                    self.step_up(index, offset)
                    continue
                failures += 1
                self.meta["read_retry_count"] += 1
                if len(self.meta["read_retry_events"]) < MAXIMUM_LOGGED_EVENTS:
                    self.meta["read_retry_events"].append(
                        {"sample_index": index, "attempt": failures, "error": _error(caught)})
                if failures > self.leak["maximum_consecutive_read_failures"]:
                    raise LeakageRunError(
                        f"Channel {self.number} sample {index}: {failures} consecutive read failures."
                    ) from caught
                self.context.sleep(RETRY_DELAY_S)
    def check(self, reading, index):
        where = f"Channel {self.number} sample {index}"
        if reading.channel != self.number:
            raise LeakageRunError(f"{where}: reading came from channel {reading.channel}.")
        voltage = reading.voltage_v
        limits = self.limits
        if not limits["minimum_voltage_v"] <= voltage <= limits["maximum_voltage_v"]:
            raise LeakageRunError(f"{where}: {voltage:.6g} V is outside the DUT rating.")
        if voltage > limits["hold_upper_v"]:
            raise LeakageRunError(f"{where}: {voltage:.6g} V exceeds the hold voltage tolerance.")
        if abs(reading.current_a) > limits["current_a"]:
            raise LeakageRunError(f"{where}: {reading.current_a:.6g} A exceeds the compliance margin.")
    def record(self, reading, index, offset, begin, finish, lateness, failures, start):
        flagged = self.range_changed
        self.range_changed = False
        self.writer.writerow([
            index, offset, begin - start, finish - start, lateness,
            reading.timestamp_s, reading.voltage_v, reading.current_a,
            reading.status, int(reading.in_compliance), failures,
            self.ranger.range_a if self.ranger is not None else None, int(flagged),
        ])
        self.handle.flush()
        self.meta["recorded_sample_count"] = index + 1
        self.meta["maximum_lateness_s"] = max(self.meta["maximum_lateness_s"], lateness)
    def finalize(self, *, outcome, error, cleanup_error, shutdown_verified, elapsed, finished_utc):
        try:
            self.handle.flush()
            os.fsync(self.handle.fileno())
        finally:
            self.handle.close()
        self.meta.update(
            state=outcome, elapsed_s=elapsed, finished_utc=finished_utc, error=error,
            cleanup_error=cleanup_error, shutdown_verified=shutdown_verified,
            csv_sha256=_sha256(self.csv_path))
        self.save()
def run_dual_leakage_test(documents, *, smu, maximum_voltage_v, maximum_current_a,
                          configure_source, spot_factory, enable_output, set_range=None,
                          restore_auto_range=None, run_directory=None, clock=time.monotonic,
                          sleep=time.sleep, utc_now=None):
    """Run one constant-voltage hold on channel 1 and channel 2 together; return the batch summary.
    Any failure or interrupt stops BOTH channels, finalizes both recordings (partial CSV kept,
    state failed/aborted) and raises the original exception.
    """
    for name, operation in (("configure_source", configure_source), ("spot_factory", spot_factory),
                            ("enable_output", enable_output), ("clock", clock), ("sleep", sleep)):
        if not callable(operation):
            raise ValueError(f"{name} must be callable.")
    if utc_now is None:
        def utc_now():
            return datetime.now(timezone.utc)
    ordered, validated = validate_dual_documents(documents)
    first = validated[0]["leakage"]
    if first["range_control"]["mode"] == "step_down" and not callable(set_range):
        raise ValueError("set_range must be callable when leakage.range_control.mode is step_down.")
    for name, value in (("maximum_voltage_v", maximum_voltage_v),
                        ("maximum_current_a", maximum_current_a)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a positive finite number.")
    if maximum_voltage_v < first["hold_voltage_v"] or maximum_current_a < first["compliance_current_a"]:
        raise ValueError("Execution ceilings must cover the hold voltage and compliance current.")
    if run_directory is None:
        output = validated[0]["preparation_config"]["output"]
        name = output.get("file_name") or ordered[0]["experiment"]["name"]
        run_directory = Path(output["root_directory"]) / f"{name}_{utc_now():%Y%m%d_%H%M%S}"
    run_directory = Path(run_directory)
    run_directory.mkdir(parents=True, exist_ok=False)
    context = SimpleNamespace(smu=smu, set_range=set_range, sleep=sleep, utc_now=utc_now)
    channels = [
        _Channel(document, item,
                 run_directory / f"channel_{item['preparation_config']['instrument']['channel']}",
                 context)
        for document, item in zip(ordered, validated)
    ]
    batch = {
        "dual_leakage_schema_version": 1,
        "state": "running",
        "channels": [{"channel": c.number, "specimen_id": c.base["dut"]["specimen_id"],
                      "directory": f"channel_{c.number}"} for c in channels],
        "fault_policy": "stop_both",
        "elapsed_s": None,
        "finished_utc": None,
        "shutdown_verified": None,
        "cleanup_error": None,
        "error": None,
        "culprit_channel": None,
        "hardware_qualified": False,
    }
    _write_json(run_directory / BATCH_NAME, batch)
    start = None
    error = None
    outcome = "completed"
    culprit = None
    def hold():
        nonlocal culprit
        last_fsync = last_meta = clock()
        for index, (offset, interval) in enumerate(planned_samples(first["segments"])):
            due = start + offset
            while True:
                remaining = due - clock()
                if remaining <= 0:
                    break
                sleep(min(remaining, 1.0))
            for channel in channels:
                culprit = channel
                begin = clock()
                lateness = begin - due
                if lateness > max(2 * interval, MINIMUM_LATENESS_LIMIT_S):
                    raise LeakageRunError(
                        f"Sampling overrun: channel {channel.number} sample {index} "
                        f"was {lateness:.3f} s late.")
                reading, failures = channel.read_sample(index, offset)
                finish = clock()
                channel.check(reading, index)
                channel.record(reading, index, offset, begin, finish, lateness, failures, start)
                if channel.ranger is not None:
                    channel.update_range(reading, index, offset)
            culprit = None
            finish = clock()
            if finish - last_fsync >= FSYNC_INTERVAL_S:
                for channel in channels:
                    os.fsync(channel.handle.fileno())
                last_fsync = finish
            if finish - last_meta >= METADATA_INTERVAL_S:
                for channel in channels:
                    channel.meta["elapsed_s"] = finish - start
                    channel.save()
                _write_json(run_directory / BATCH_NAME, batch)
                last_meta = finish
    try:
        for channel in channels:
            culprit = channel
            channel.meta["source"] = configure_source(smu, channel.base)
        for channel in channels:
            culprit = channel
            channel.spot = spot_factory(smu, channel.number)
            channel.spot.configure(channel.base["instrument"]["nplc"])
        for channel in channels:
            if channel.ranger is not None:
                culprit = channel
                new = channel.ranger.takeover(channel.leak["compliance_current_a"])
                readback = set_range(smu, channel.number, new)
                channel.log_range("start", None, None, None, new, readback, None)
        for channel in channels:
            culprit = channel
            enable_output(smu, channel.number, max_voltage_v=maximum_voltage_v,
                          max_current_a=maximum_current_a)
        culprit = None
        start = clock()
        stamp = utc_now().isoformat()
        for channel in channels:
            channel.meta["hold_started_utc"] = stamp
        hold()
    except BaseException as caught:
        error = caught
        outcome = "aborted" if isinstance(caught, KeyboardInterrupt) else "failed"
    shutdown_verified = False
    cleanup_error = None
    try:
        smu.shutdown()
        states = (smu.chan1.get_output_state(), smu.chan2.get_output_state())
        shutdown_verified = all(state is False for state in states)
    except BaseException as caught:
        cleanup_error = _error(caught)
        if error is not None:
            error.add_note(f"Shutdown could not be verified: {type(caught).__name__}: {caught}")
    for channel in channels:
        if (restore_auto_range is not None and channel.ranger is not None
                and channel.ranger.range_a is not None and shutdown_verified):
            try:
                restore_auto_range(smu, channel.number)
                channel.meta["range_control"]["auto_range_restored"] = True
            except Exception as caught:
                channel.meta["range_control"]["auto_range_restored"] = False
                channel.meta["range_control"]["restore_error"] = _error(caught)
    if error is None and not shutdown_verified:
        outcome = "failed"
        error = LeakageRunError("Shutdown could not be verified.")
    elapsed = None if start is None else clock() - start
    finished = utc_now().isoformat()
    for channel in channels:
        if error is None:
            channel_error = None
        elif culprit is None or culprit is channel:
            channel_error = _error(error)
        else:
            channel_error = {"type": "PeerChannelStopped",
                             "message": f"Stopped because channel {culprit.number} failed: {error}"}
        channel.finalize(outcome=outcome, error=channel_error, cleanup_error=cleanup_error,
                         shutdown_verified=shutdown_verified, elapsed=elapsed, finished_utc=finished)
    batch.update(
        state=outcome, elapsed_s=elapsed, finished_utc=finished, shutdown_verified=shutdown_verified,
        cleanup_error=cleanup_error, error=None if error is None else _error(error),
        culprit_channel=None if culprit is None else culprit.number,
        channels=[{"channel": c.number, "specimen_id": c.base["dut"]["specimen_id"],
                   "directory": f"channel_{c.number}", "state": c.meta["state"],
                   "recorded_sample_count": c.meta["recorded_sample_count"],
                   "planned_sample_count": c.meta["planned_sample_count"],
                   "read_retry_count": c.meta["read_retry_count"]} for c in channels])
    _write_json(run_directory / BATCH_NAME, batch)
    if error is not None:
        raise error
    (run_directory / MARKER_NAME).write_text("Completed dual-channel leakage run.\n", encoding="utf-8")
    return batch
