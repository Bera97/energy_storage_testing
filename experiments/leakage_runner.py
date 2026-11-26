"""Constant-voltage leakage-current hold over an ALREADY CONNECTED driver.
The caller owns session opening, identity checks and closing. This module sets
up the source with the supplied helpers, enables the output, logs readings on
the planned host schedule and ALWAYS attempts shutdown on every exit.
A completed run is a record of logging, not evidence of instrument or DUT
qualification.
"""
from bisect import bisect_left
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import time
from acquisition.spot_acquisition import MeasurementStatusError
from experiments.leakage_planning import validate_leakage_test
CSV_NAME = "leakage_samples.csv"
METADATA_NAME = "leakage_metadata.json"
COLUMNS = (
    "sample_index", "scheduled_s", "host_start_s", "host_end_s", "lateness_s",
    "instrument_time_s", "voltage_v", "current_a", "status", "in_compliance",
    "read_failures",
)
RETRY_DELAY_S = 0.5
FSYNC_INTERVAL_S = 30.0
METADATA_INTERVAL_S = 600.0
MINIMUM_LATENESS_LIMIT_S = 2.0
CURRENT_MARGIN = 1.1
MAXIMUM_LOGGED_EVENTS = 500
class LeakageRunError(RuntimeError):
    """The hold stopped because a safety, timing or read condition failed."""
def planned_samples(segments):
    """Yield (offset_s, interval_s) for every planned sample, starting at 0."""
    yield 0.0, segments[0]["interval_s"]
    for segment in segments:
        for step in range(1, segment["sample_count"] + 1):
            yield segment["start_s"] + step * segment["interval_s"], segment["interval_s"]
def evaluate_stabilization(times, currents, now_s, window_s):
    """Return (recent_mean, previous_mean, relative_change) or None."""
    if now_s < 2 * window_s:
        return None
    recent_start = bisect_left(times, now_s - window_s)
    previous_start = bisect_left(times, now_s - 2 * window_s)
    recent = currents[recent_start:]
    previous = currents[previous_start:recent_start]
    if not recent or not previous:
        return None
    recent_mean = sum(recent) / len(recent)
    previous_mean = sum(previous) / len(previous)
    if previous_mean == 0:
        return None
    return recent_mean, previous_mean, abs(recent_mean - previous_mean) / abs(previous_mean)
def _write_json(path, payload):
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
def _error(error):
    return {"type": type(error).__name__, "message": str(error)}
def run_leakage_test(document, *, smu, maximum_voltage_v, maximum_current_a,
                     configure_source, spot_factory, enable_output,
                     run_directory=None, clock=time.monotonic, sleep=time.sleep,
                     utc_now=None):
    """Run one constant-voltage hold and return the final metadata.
    On failure or interrupt the run is finalized (partial CSV kept, state
    failed/aborted, outputs shut down) and the original exception is raised.
    """
    for name, operation in (("configure_source", configure_source),
                            ("spot_factory", spot_factory), ("enable_output", enable_output),
                            ("clock", clock), ("sleep", sleep)):
        if not callable(operation):
            raise ValueError(f"{name} must be callable.")
    if utc_now is None:
        def utc_now():
            return datetime.now(timezone.utc)
    validated = validate_leakage_test(document)
    base, leak = validated["preparation_config"], validated["leakage"]
    from experiments.config import require_charge_monitoring_settings
    monitoring = require_charge_monitoring_settings(base)
    for name, value in (("maximum_voltage_v", maximum_voltage_v),
                        ("maximum_current_a", maximum_current_a)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a positive finite number.")
    if maximum_voltage_v < leak["hold_voltage_v"] or maximum_current_a < leak["compliance_current_a"]:
        raise ValueError("Execution ceilings must cover the hold voltage and compliance current.")
    channel = base["instrument"]["channel"]
    ratings = base["dut"]["ratings"]
    limits = {
        "minimum_voltage_v": float(ratings["minimum_voltage_v"]),
        "maximum_voltage_v": float(ratings["maximum_voltage_v"]),
        "hold_upper_v": leak["hold_voltage_v"] + float(monitoring["voltage_tolerance_v"]),
        "current_a": leak["compliance_current_a"] * CURRENT_MARGIN,
    }
    early = leak["early_stop"]
    if run_directory is None:
        output = base["output"]
        name = output.get("file_name") or document["experiment"]["name"]
        run_directory = Path(output["root_directory"]) / f"{name}_{utc_now():%Y%m%d_%H%M%S}"
    run_directory = Path(run_directory)
    run_directory.mkdir(parents=True, exist_ok=False)
    csv_path = run_directory / CSV_NAME
    metadata_path = run_directory / METADATA_NAME
    meta = {
        "leakage_run_schema_version": 1,
        "state": "running",
        "experiment_name": document["experiment"]["name"],
        "specimen_id": base["dut"]["specimen_id"],
        "channel": channel,
        "hold_voltage_v": leak["hold_voltage_v"],
        "compliance_current_a": leak["compliance_current_a"],
        "planned_sample_count": leak["total_sample_count"],
        "planned_duration_s": leak["maximum_duration_s"],
        "recorded_sample_count": 0,
        "elapsed_s": None,
        "maximum_lateness_s": 0.0,
        "created_utc": utc_now().isoformat(),
        "hold_started_utc": None,
        "finished_utc": None,
        "source": None,
        "read_retry_count": 0,
        "read_retry_events": [],
        "early_stop": {**early, "triggered": False, "stop_offset_s": None, "checks": []},
        "error": None,
        "cleanup_error": None,
        "shutdown_verified": None,
        "csv_name": CSV_NAME,
        "csv_sha256": None,
        "hardware_qualified": False,
        "configuration": json.loads(json.dumps(document)),
        "leakage": leak,
    }
    handle = open(csv_path, "w", encoding="utf-8", newline="")
    writer = csv.writer(handle)
    writer.writerow(COLUMNS)
    handle.flush()
    _write_json(metadata_path, meta)
    start = None
    error = None
    outcome = "completed"
    def read_sample(spot, index):
        failures = 0
        while True:
            try:
                return spot.read(), failures
            except MeasurementStatusError:
                raise
            except Exception as caught:
                failures += 1
                meta["read_retry_count"] += 1
                if len(meta["read_retry_events"]) < MAXIMUM_LOGGED_EVENTS:
                    meta["read_retry_events"].append(
                        {"sample_index": index, "attempt": failures, "error": _error(caught)})
                if failures > leak["maximum_consecutive_read_failures"]:
                    raise LeakageRunError(
                        f"Sample {index}: {failures} consecutive read failures."
                    ) from caught
                sleep(RETRY_DELAY_S)
    def check(reading, index):
        if reading.channel != channel:
            raise LeakageRunError(f"Sample {index}: reading came from channel {reading.channel}.")
        voltage = reading.voltage_v
        if not limits["minimum_voltage_v"] <= voltage <= limits["maximum_voltage_v"]:
            raise LeakageRunError(f"Sample {index}: {voltage:.6g} V is outside the DUT rating.")
        if voltage > limits["hold_upper_v"]:
            raise LeakageRunError(f"Sample {index}: {voltage:.6g} V exceeds the hold voltage tolerance.")
        if abs(reading.current_a) > limits["current_a"]:
            raise LeakageRunError(f"Sample {index}: {reading.current_a:.6g} A exceeds the compliance margin.")
    def hold(spot):
        times, currents = [], []
        checks_ok = 0
        next_check = early["minimum_duration_s"] if early["enabled"] else None
        last_fsync = last_meta = clock()
        for index, (offset, interval) in enumerate(planned_samples(leak["segments"])):
            due = start + offset
            while True:
                remaining = due - clock()
                if remaining <= 0:
                    break
                sleep(min(remaining, 1.0))
            begin = clock()
            lateness = begin - due
            if lateness > max(2 * interval, MINIMUM_LATENESS_LIMIT_S):
                raise LeakageRunError(f"Sampling overrun: sample {index} was {lateness:.3f} s late.")
            reading, failures = read_sample(spot, index)
            finish = clock()
            check(reading, index)
            writer.writerow([
                index, offset, begin - start, finish - start, lateness,
                reading.timestamp_s, reading.voltage_v, reading.current_a,
                reading.status, int(reading.in_compliance), failures,
            ])
            handle.flush()
            times.append(offset)
            currents.append(reading.current_a)
            meta["recorded_sample_count"] = index + 1
            meta["maximum_lateness_s"] = max(meta["maximum_lateness_s"], lateness)
            if finish - last_fsync >= FSYNC_INTERVAL_S:
                os.fsync(handle.fileno())
                last_fsync = finish
            if finish - last_meta >= METADATA_INTERVAL_S:
                meta["elapsed_s"] = finish - start
                _write_json(metadata_path, meta)
                last_meta = finish
            if next_check is not None and offset >= next_check:
                while next_check <= offset:
                    next_check += early["check_interval_s"]
                result = evaluate_stabilization(times, currents, offset, early["window_s"])
                if result is not None:
                    recent, previous, change = result
                    stable = change < early["relative_change"]
                    checks_ok = checks_ok + 1 if stable else 0
                    if len(meta["early_stop"]["checks"]) < MAXIMUM_LOGGED_EVENTS:
                        meta["early_stop"]["checks"].append({
                            "offset_s": offset, "recent_mean_a": recent,
                            "previous_mean_a": previous, "relative_change": change,
                            "stable": stable,
                        })
                    if checks_ok >= early["consecutive_checks"]:
                        meta["early_stop"].update(triggered=True, stop_offset_s=offset)
                        return "stopped_early"
        return "completed"
    try:
        meta["source"] = configure_source(smu, base)
        spot = spot_factory(smu, channel)
        spot.configure(base["instrument"]["nplc"])
        enable_output(smu, channel, max_voltage_v=maximum_voltage_v,
                      max_current_a=maximum_current_a)
        start = clock()
        meta["hold_started_utc"] = utc_now().isoformat()
        outcome = hold(spot)
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
    try:
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    if error is None and not shutdown_verified:
        outcome = "failed"
        error = LeakageRunError("Shutdown could not be verified.")
        if cleanup_error is not None:
            error.__cause__ = None
    meta.update(
        state=outcome,
        elapsed_s=None if start is None else clock() - start,
        finished_utc=utc_now().isoformat(),
        error=None if error is None else _error(error),
        cleanup_error=cleanup_error,
        shutdown_verified=shutdown_verified,
        csv_sha256=_sha256(csv_path),
    )
    _write_json(metadata_path, meta)
    if error is not None:
        raise error
    return meta
