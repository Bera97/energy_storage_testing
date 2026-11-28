"""Read-only power-phase validation with controller/event replay.

Hashes detect changes, not authorship. Consistently fabricated evidence can pass.
Read/re-read checks are not a transactional snapshot or physical qualification.
No driver, connection factory, output command or analysis export is invoked.
"""
import csv
from datetime import datetime, timedelta
import hashlib
import io
import json
import math
from pathlib import Path
from experiments.constant_power import ConstantPowerController, PowerLimits, _number
from experiments.profile_control import ProfileController, ProfileLimits
from experiments.power_adapter import PowerFrame, _validate_setup
from recording.power_phase import FIELDS, marker_text

NAMES = ("power_phase.json", "power_events.jsonl", "power_samples.csv", "POWER_PHASE_COMPLETE")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _json(text):
    def invalid(value):
        raise ValueError(f"Nonfinite JSON constant: {value}")
    return json.loads(text, object_pairs_hook=_pairs, parse_constant=invalid)


def _integer(value, name, minimum=0):
    _require(type(value) is int and value >= minimum, f"Invalid {name}.")
    return value


def _same(actual, expected, name):
    _require(json.dumps(actual, sort_keys=True, allow_nan=False) ==
             json.dumps(expected, sort_keys=True, allow_nan=False), f"{name} mismatch.")


def _validate(directory):
    root = Path(directory)
    _require(root.is_dir() and not root.is_symlink(), "Expected an explicit nonsymlink phase directory.")
    for name in ("SIMULATION_COMPLETE", "COMPLETE", "DUAL_COMPLETE", "ANALYSIS_COMPLETE"):
        _require(not ((root / name).exists() or (root / name).is_symlink()), "Mixed or ordinary completion markers are not power-phase evidence.")
    def read_all():
        result = {}
        for name in NAMES:
            path = root / name
            _require(path.is_file() and not path.is_symlink(), f"Missing or symlink evidence file: {name}")
            result[name] = path.read_bytes()
        return result
    raw = read_all()
    document = _json(raw["power_phase.json"])
    _require(isinstance(document, dict), "Manifest must be an object.")
    _same(sorted(document), sorted((
        "power_phase_schema_version", "created_at_utc", "simulation_only",
        "hardware_execution_authorized", "physical_qualification", "evidence_origin",
        "metadata", "outcome", "sample_count_flushed", "event_count_flushed",
        "writer_failed", "close_errors", "files",
    )), "Manifest fields")
    _require(type(document["power_phase_schema_version"]) is int and document["power_phase_schema_version"] == 1,
             "Unsupported power phase schema.")
    _require(document["simulation_only"] is False and document["hardware_execution_authorized"] is True
             and document["physical_qualification"] is False and document["evidence_origin"] == "driver_contract",
             "Wrong evidence origin/authorization flags.")
    _require(document["writer_failed"] is False and document["close_errors"] == [], "Failed writer cannot complete.")
    _require(raw["POWER_PHASE_COMPLETE"].decode("utf-8") == marker_text(raw["power_phase.json"]), "Completion digest mismatch.")
    descriptors = document["files"]
    _require(isinstance(descriptors, dict) and set(descriptors) == set(NAMES[1:3]), "Wrong evidence file manifest.")
    for name in NAMES[1:3]:
        descriptor = descriptors[name]
        _require(isinstance(descriptor, dict) and set(descriptor) == {"sha256", "size_bytes"}, "Invalid file descriptor.")
        _integer(descriptor["size_bytes"], "file size", 1)
        _require(descriptor["size_bytes"] == len(raw[name]) and
                 descriptor["sha256"] == hashlib.sha256(raw[name]).hexdigest(), f"File digest/size mismatch: {name}")
    created = document["created_at_utc"]
    _require(isinstance(created, str), "Invalid creation timestamp.")
    created_time = datetime.fromisoformat(created)
    _require(created_time.tzinfo is not None and created_time.utcoffset() == timedelta(0), "Creation time must be UTC-aware.")
    metadata = document["metadata"]
    _require(isinstance(metadata, dict), "Missing phase metadata.")
    _require(set(metadata) == {"limits", "channel", "specimen_id", "configuration_snapshot", "instrument",
                                "maximum_probe_current_a", "maximum_samples", "timestamp_basis", "freshness_basis"},
             "Unexpected phase metadata fields.")
    _require(metadata["configuration_snapshot"] is None or isinstance(metadata["configuration_snapshot"], dict),
             "Invalid configuration snapshot.")
    _require(metadata["timestamp_basis"] == "host_read_brackets; raw instrument timestamps retained unmapped"
             and metadata["freshness_basis"] == "backend fresh paired-read contract; not independently verified",
             "Unsupported timestamp/freshness contract.")
    limits = (ProfileLimits if "profile_kind" in metadata["limits"] else PowerLimits)(**metadata["limits"])
    channel = _integer(metadata["channel"], "channel", 1)
    _require(channel in (1, 2), "Invalid channel.")
    specimen = metadata["specimen_id"]
    _require(isinstance(specimen, str) and bool(specimen.strip()) and not specimen.upper().startswith("REPLACE"), "Invalid specimen.")
    probe = _number(metadata["maximum_probe_current_a"], "probe ceiling")
    _require(probe <= limits.maximum_current_a, "Probe ceiling exceeds phase current ceiling.")
    maximum = _integer(metadata["maximum_samples"], "sample budget", 1)
    instrument = metadata["instrument"]
    _require(isinstance(instrument, dict) and type(instrument["four_wire"]) is bool, "Invalid instrument metadata.")
    for name in ("resource_address", "expected_serial", "driver_type"):
        value = instrument[name]
        _require(isinstance(value, str) and bool(value.strip()) and "REPLACE" not in value.upper(), f"Invalid instrument {name}.")
    _number(instrument["nplc"], "NPLC")
    _require(_number(instrument["maximum_voltage_v"], "execution voltage ceiling") >= limits.maximum_voltage_v
             and _number(instrument["maximum_current_a"], "execution current ceiling") >= limits.maximum_current_a,
             "Phase limits exceed saved execution ceilings.")
    reader = csv.DictReader(io.StringIO(raw["power_samples.csv"].decode("utf-8"), newline=""), strict=True)
    _require(reader.fieldnames == list(FIELDS), "Unexpected sample columns.")
    rows = list(reader)
    _require(1 <= len(rows) <= maximum and len(rows) == _integer(document["sample_count_flushed"], "sample count", 1), "Sample count/budget mismatch.")
    events = [_json(line) for line in raw["power_events.jsonl"].decode("utf-8").splitlines()]
    _require(len(events) == _integer(document["event_count_flushed"], "event count", 1), "Event count mismatch.")
    position = 0
    last_host = None
    def take(name):
        nonlocal position, last_host
        _require(position < len(events), f"Missing {name} event.")
        event = events[position]
        _require(isinstance(event, dict) and type(event.get("event_index")) is int
                 and event["event_index"] == position and event.get("event") == name,
                 f"Unexpected event at position {position}; expected {name}.")
        position += 1
        if name != "cleanup_outcome":
            now = _number(event["host_time_s"], "event host time", zero=True)
            _require(last_host is None or now >= last_host, "Event host clock moved backwards.")
            last_host = now
        return event
    take("setup_requested")
    setup = take("setup_returned")["readbacks"]
    _validate_setup(setup, limits, channel)
    parts = setup.get("identity", "").split(",")
    _require(len(parts) == 4 and parts[1].strip().upper() == "B2902B"
             and parts[2].strip() == instrument["expected_serial"], "Setup identity differs from saved instrument.")
    _require(setup.get("four_wire") is instrument["four_wire"], "Setup sensing differs from saved instrument.")
    _require(math.isclose(_number(setup.get("nplc"), "setup NPLC"), instrument["nplc"], rel_tol=1e-9, abs_tol=1e-12), "Setup NPLC differs.")
    controller = (ProfileController if isinstance(limits, ProfileLimits) else ConstantPowerController)(limits)
    armed = take("controller_armed")
    controller.arm_zero_current_probe(now_s=armed["host_time_s"], outputs_verified_off=True,
                                     current_source_verified=True, zero_current_setpoint_a=setup["current_setpoint_a"],
                                     maximum_probe_current_a=probe)
    controller.watchdog(now_s=take("probe_output_on_requested")["host_time_s"])
    controller.watchdog(now_s=take("probe_output_on_command_returned")["host_time_s"])
    enabled = take("probe_output_on_verified")
    _require(last_host >= enabled["controller_probe_enabled_s"] >= events[position-2]["host_time_s"], "Probe activation time is out of order.")
    controller.confirm_zero_current_probe_enabled(now_s=enabled["controller_probe_enabled_s"], output_reported_on=enabled["output_reported_on"])
    controller.watchdog(now_s=enabled["host_time_s"])
    last_started = None
    stop_time = None
    for index, row in enumerate(rows):
        _require(set(row) == set(FIELDS) and all(value is not None for value in row.values()), "Malformed CSV row.")
        _require(row["record_index"] == str(index), "Sample record index mismatch.")
        _require(row["stage"] == ("zero_current_probe" if index == 0 else "power_discharge"), "Sample stage mismatch.")
        started = _number(float(row["host_read_started_s"]), "read start", zero=True)
        finished = _number(float(row["host_read_finished_s"]), "read finish", zero=True)
        _require(started <= finished, "Reversed sample read bracket.")
        if last_started is not None:
            _require(started + 1e-12 >= last_started + limits.update_interval_s, "Acquisition cadence is faster than configured.")
        last_started = started
        frame = PowerFrame(int(row["channel"]), float(row["voltage_v"]), float(row["current_a"]),
                           float(row["raw_instrument_timestamp_s"]), int(row["status"]))
        _require(frame.channel == channel, "Sample channel mismatch.")
        _require(math.isclose(_number(float(row["requested_power_w"]), "requested power"), limits.power_w, rel_tol=1e-12), "Requested power mismatch.")
        power = _number(float(row["measured_discharge_power_w"]), "measured power", signed=True)
        _require(math.isclose(power, -frame.voltage_v * frame.current_a, rel_tol=1e-12, abs_tol=1e-15), "Measured power arithmetic mismatch.")
        _require(started >= last_host, "Sample acquisition predates preceding control event.")
        event = take("control_decision")
        _require(type(event.get("record_index")) is int and event["record_index"] == index, "Decision/sample linkage mismatch.")
        _require(event["sample_time_s"] == started and finished <= event["host_time_s"], "Decision timestamp does not cover sample.")
        sample = frame.power_sample(started)
        decision = (controller.start_from_zero_current_probe(now_s=event["host_time_s"], sample=sample)
                    if index == 0 else controller.update(now_s=event["host_time_s"], sample=sample))
        _same({key: value for key, value in event.items() if key not in ("event_index", "event", "record_index")},
              decision.to_dict(), "Replayed decision")
        if decision.action == "stop":
            _require(index == len(rows)-1 and decision.stop_reason == "voltage_min", "Cutoff must be the last recorded sample.")
            stop_time = decision.host_time_s
        elif decision.action == "set_current":
            for name in ("current_command_requested", "current_command_returned"):
                command = take(name)
                _same(command["current_setpoint_a"], decision.current_setpoint_a, "Requested/returned current")
                controller.watchdog(now_s=command["host_time_s"])
            readback = take("current_readback")
            controller.watchdog(now_s=readback["host_time_s"])
            ack = take("current_setpoint_acknowledged")
            _same(ack["current_setpoint_a"], readback["current_setpoint_a"], "Acknowledged readback")
            _require(readback["host_time_s"] <= ack["controller_acknowledged_at_s"] <= ack["host_time_s"], "Acknowledgement time is out of order.")
            controller.acknowledge_setpoint(now_s=ack["controller_acknowledged_at_s"], readback_current_a=readback["current_setpoint_a"])
            controller.watchdog(now_s=ack["host_time_s"])
    _require(controller.state == "completed" and stop_time is not None, "Replay did not reach successful cutoff.")
    cleanup_event = take("cleanup_outcome")
    _require(position == len(events), "Extra events after cleanup.")
    outcome = document["outcome"]
    _require(isinstance(outcome, dict) and outcome.get("state") == "completed", "Manifest is not completed.")
    _same({k:v for k,v in cleanup_event.items() if k not in ("event_index", "event", "state")},
          {k:v for k,v in outcome.items() if k != "state"}, "Cleanup event/manifest")
    _require(cleanup_event.get("state") == "running", "Unexpected cleanup lifecycle state.")
    for key in ("shutdown_commands_returned", "outputs_reported_off", "session_close_returned"):
        _require(outcome.get(key) is True, f"Cleanup evidence missing: {key}")
    _same(outcome.get("output_states"), [False, False], "Both output readbacks")
    _require(outcome.get("cleanup_errors") == [] and outcome.get("controller_state") == "completed"
             and outcome.get("stop_reason") == "voltage_min", "Invalid successful outcome.")
    _same(outcome["phase_started_s"], controller.started_s, "Phase start")
    _same(outcome["decision_stop_time_s"], stop_time, "Stop time")
    _require(stop_time <= _number(outcome["cleanup_started_s"], "cleanup start", zero=True)
             <= _number(outcome["cleanup_finished_s"], "cleanup finish", zero=True), "Cleanup clock ordering is invalid.")
    _require(read_all() == raw, "Evidence changed during validation.")
    return {"valid": True, "errors": [], "scope": "recorded_power_phase",
            "physical_qualification": False, "record_count": len(rows), "event_count": len(events),
            "channel": channel, "specimen_id": specimen, "power_w": limits.power_w,
            "stop_reason": "voltage_min"}


def validate_power_phase(directory):
    """Return a read-only validation report; malformed/incomplete inputs are invalid."""
    try:
        return _validate(directory)
    except Exception as error:
        return {"valid": False, "errors": [f"{type(error).__name__}: {error}"],
                "scope": "recorded_power_phase", "physical_qualification": False}
