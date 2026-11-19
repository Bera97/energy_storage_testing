"""Explicit output-enabled, zero-current rest recording. No I/O on import.

Spot samples use host query brackets. Instrument timestamps are retained,
but continuity between independent MEAS queries is deliberately not assumed.
"""
from copy import deepcopy
import csv
import hashlib
from importlib import import_module
import io
import json
import math
from pathlib import Path
import time

FIELDS = ("host_before_s", "host_after_s", "instrument_timestamp_s",
          "voltage_v", "current_a", "status")


def number(value, name, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite numeric data.")
    value = float(value)
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f"{name} must be finite" + (" and positive." if positive else "."))
    return value


def validate_rest_settings(config):
    rest = config["rest"]
    mode = rest.get("mode", "outputs_off")
    if mode not in ("outputs_off", "zero_current_logging"):
        raise ValueError("rest.mode must be outputs_off or zero_current_logging.")
    if mode == "outputs_off":
        if "maximum_abs_current_a" in rest:
            raise ValueError("rest.maximum_abs_current_a requires zero_current_logging.")
        return
    guard = number(rest.get("maximum_abs_current_a"),
                   "rest.maximum_abs_current_a", True)
    ratings = config["dut"]["ratings"]
    ceiling = min(number(ratings[key], key, True) for key in (
        "maximum_continuous_charge_current_a", "maximum_continuous_discharge_current_a"))
    if guard >= ceiling:
        raise ValueError("Measured-rest current guard must be below both continuous current ratings.")
    interval = number(config["acquisition"]["interval_s"], "interval_s", True)
    gap = number(config["acquisition"].get("maximum_sample_gap_s"),
                 "maximum_sample_gap_s", True)
    if gap < interval:
        raise ValueError("Measured rest requires maximum_sample_gap_s >= interval_s.")
    for key in ("after_charge_s", "after_discharge_s"):
        duration = number(rest[key], key, True)
        if duration / interval > 99998:
            raise ValueError("Measured rest exceeds the 100000 spot-record budget.")


def is_measured(phase):
    return phase.get("type") == "rest" and phase.get("rest_mode") == "zero_current_logging"


def decorate_rest_phases(phases, config):
    if config["rest"].get("mode", "outputs_off") != "zero_current_logging":
        return
    validate_rest_settings(config)
    for phase in phases:
        if phase["type"] == "rest":
            phase.update(
                rest_mode="zero_current_logging", source_current_a=0.0,
                requires_verified_output_off=False,
                maximum_abs_current_a=config["rest"]["maximum_abs_current_a"],
                interval_s=config["acquisition"]["interval_s"],
                maximum_sample_gap_s=config["acquisition"]["maximum_sample_gap_s"],
                minimum_voltage_v=config["dut"]["ratings"]["minimum_voltage_v"],
                maximum_voltage_v=config["dut"]["ratings"]["maximum_voltage_v"],
                rest_record_limit=math.ceil(phase["duration_s"] / config["acquisition"]["interval_s"]) + 2,
            )


def endpoint(phase):
    if is_measured(phase):
        return "rest_elapsed"
    if phase["type"] == "charge_cc_cv":
        from experiments.charge_controller import charge_stop_reason
        return charge_stop_reason(phase)
    return "voltage_min"


def maximum_duration(phase):
    if is_measured(phase):
        return phase["duration_s"] + phase["maximum_sample_gap_s"]
    return phase["maximum_duration_s"]


def config_for_phase(plan, channel, phase):
    preview = next(item for item in plan["channels"] if item["channel"] == channel)
    if "sweep_plan" not in preview:
        return deepcopy(preview["acquisition_plan"]["configuration"])
    conditions = preview["sweep_plan"]["conditions"]
    selected = (conditions[0] if phase["sweep_stage_id"] == "initial_discharge" else
                next(item for item in conditions if item["condition_id"] == phase["sweep_stage_id"]))
    return deepcopy(selected["config"])


def _write_json(path, value):
    text = json.dumps(value, indent=2, allow_nan=False) + "\n"
    temporary = path.with_suffix(".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(text)
    temporary.replace(path)


def _json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key.")
            result[key] = value
        return result
    def invalid(value):
        raise ValueError(f"Invalid JSON constant {value}.")
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs,
                      parse_constant=invalid)


def check_row(row, phase):
    before = number(row["host_before_s"], "host_before_s")
    after = number(row["host_after_s"], "host_after_s")
    timestamp = number(row["instrument_timestamp_s"], "instrument timestamp")
    voltage = number(row["voltage_v"], "voltage")
    current = number(row["current_a"], "current")
    status = row["status"]
    if type(status) is not int or not 0 <= status <= 0xFFFFFFFF:
        raise ValueError("Invalid measured-rest status.")
    if not 0 <= before <= after or timestamp < 0:
        raise ValueError("Invalid measured-rest timing.")
    if not status & 1 or status & (0b110 | (1 << 3) | (1 << 4) | (1 << 5) | (1 << 13)):
        raise RuntimeError("Measured rest requires current-source status, no compliance, and no faults.")
    if not phase["minimum_voltage_v"] <= voltage <= phase["maximum_voltage_v"]:
        raise RuntimeError("Measured-rest voltage is outside the supplied DUT limits.")
    if abs(current) > phase["maximum_abs_current_a"]:
        raise RuntimeError("Measured-rest absolute current guard exceeded.")


class ZeroCurrentRestTask:
    """One cooperative, recorded rest. Caller retains outer shutdown boundary."""
    def __init__(self, smu, configuration, phase, directory, stop, *,
                 clock=time.monotonic, shared=False, spot_factory=None):
        validate_rest_settings(configuration)
        if not is_measured(phase):
            raise ValueError("Expected a planned zero-current rest.")
        original = {key: phase[key] for key in (
            "phase_index", "cycle", "type", "follows", "duration_s")}
        expected = [deepcopy(original)]
        decorate_rest_phases(expected, configuration)
        if any(phase.get(key) != value for key, value in expected[0].items()):
            raise ValueError("Measured-rest phase differs from configuration.")
        duration_key = "after_charge_s" if phase["follows"] == "charge" else "after_discharge_s"
        if phase["follows"] not in ("charge", "discharge") or phase["duration_s"] != configuration["rest"][duration_key]:
            raise ValueError("Measured-rest duration differs from configuration.")
        if not callable(stop) or not callable(clock) or type(shared) is not bool:
            raise ValueError("Invalid measured-rest dependencies.")
        self.smu, self.config, self.phase = smu, deepcopy(configuration), deepcopy(phase)
        self.channel = configuration["instrument"]["channel"]
        self.selected = smu.chan1 if self.channel == 1 else smu.chan2
        self.directory, self.stop, self.clock = Path(directory), stop, clock
        self.shared, self.spot_factory = shared, spot_factory
        self.state, self.run, self.writer, self.stream = "new", None, None, None
        self.last_clock, self.started, self.last_sample = None, None, None
        self.rows, self.finalized, self.outcome = [], False, None
        self.document = {"rest_recording_schema_version": 1, "state": "running",
                         "configuration": self.config, "planned_phase": self.phase,
                         "timing_policy": "host_query_brackets; independent instrument timestamps",
                         "current_meaning": "SMU-terminal current; not internal self-discharge",
                         "source_current_a": 0.0, "outcome": None}

    def _now(self):
        value = number(self.clock(), "host clock")
        if self.last_clock is not None and value < self.last_clock:
            raise RuntimeError("Measured-rest host clock moved backwards.")
        self.last_clock = value
        return value

    def _checked(self, operation, *args):
        result = operation(*args)
        self.smu.check_errors()
        return result

    def _off(self):
        if self._checked(self.selected.get_output_state) is not False:
            raise RuntimeError("Measured-rest setup requires selected output OFF.")

    def _verify(self):
        self._off()
        for actual, expected, label in (
            (self._checked(self.selected.get_source_mode), "CURR", "source mode"),
            (self._checked(self.selected.get_output_off_mode), "HIZ", "off mode"),
            (self._checked(self.selected.get_auto_output_on), False, "auto output"),
            (self._checked(self.selected.get_four_wire), self.config["instrument"]["four_wire"], "sensing"),
        ):
            if actual != expected or type(actual) is not type(expected):
                raise RuntimeError(f"Measured-rest {label} readback mismatch.")
        current = number(self._checked(self.selected.get_current_setpoint), "source current")
        if current != 0.0:
            raise RuntimeError("Measured-rest source must read back exactly zero amperes.")
        for actual, expected, label in (
            (self._checked(self.selected.get_voltage_limit), self.phase["maximum_voltage_v"], "voltage compliance"),
            (self._checked(self.selected.get_nplc), self.config["instrument"]["nplc"], "NPLC"),
        ):
            if not math.isclose(number(actual, label), float(expected), rel_tol=1e-9, abs_tol=1e-12):
                raise RuntimeError(f"Measured-rest {label} readback mismatch.")
        for suffix, expected in (("FUNC:SHAP?", "DC"), ("CURR:MODE?", "FIX")):
            if self._checked(self.smu._query, f":SOUR{self.channel}:{suffix}").strip().upper() != expected:
                raise RuntimeError("Measured-rest source operation readback mismatch.")
        self.document["source_readbacks"] = {"mode": "CURR", "current_a": current,
            "voltage_compliance_v": self.phase["maximum_voltage_v"], "selected_output_off": True,
            "four_wire": self.config["instrument"]["four_wire"], "fixed_dc": True}

    def start(self):
        if self.state != "new":
            raise RuntimeError("Use a fresh measured-rest task.")
        self.directory.mkdir(parents=True, exist_ok=False)
        self.state = "preparing"
        try:
            _write_json(self.directory / "rest.json", self.document)
            self.stream = (self.directory / "measurements.csv").open("x", encoding="utf-8", newline="")
            self.writer = csv.DictWriter(self.stream, fieldnames=FIELDS)
            self.writer.writeheader()
            self.stream.flush()
            self._off()
            self._checked(self.smu._write, f":ABOR:ALL (@{self.channel})")
            self._checked(self.selected.set_auto_output_on, False)
            self._checked(self.selected.set_output_off_mode, "HIZ")
            self._checked(self.selected.configure_fixed_dc, "CURR")
            self._checked(self.selected.set_current, 0.0)
            self._checked(self.selected.set_voltage_limit, self.phase["maximum_voltage_v"])
            self._checked(self.selected.set_four_wire, self.config["instrument"]["four_wire"])
            factory = self.spot_factory
            if factory is None:
                factory = import_module("acquisition.spot_acquisition").SpotAcquisition
            self.spot = factory(self.smu, self.channel)
            if not self.shared:
                self.spot.configure(self.config["instrument"]["nplc"])
            # Channel-local trigger configuration; never change shared formatting here.
            for layer in ("ARM", "TRIG"):
                for setting in ("SOUR AINT", "COUN 1", "DEL 0"):
                    self._checked(self.smu._write, f":{layer}{self.channel}:ACQ:{setting}")
            self._checked(self.smu._write, f":TRAC{self.channel}:FEED:CONT NEV")
            self._verify()
            self.started = self._now()
            self.next_sample = self.started
            self._checked(self.smu._write, f":OUTP{self.channel} ON")
            if self._checked(self.selected.get_output_state) is not True:
                raise RuntimeError("Measured-rest output did not report ON.")
            self.document["activation"] = {"host_before_enable_s": self.started,
                "host_after_readback_s": self._now(), "output_reported_on": True,
                "physical_output_enable_time_established": False}
            self.state = "active"
            self._deadline()
        except BaseException as error:
            self._fail(error)
            raise

    @property
    def rest_deadline_s(self):
        return self.started + self.phase["duration_s"] + self.phase["maximum_sample_gap_s"]

    def _deadline(self):
        now = self._now()
        if now >= self.rest_deadline_s:
            raise TimeoutError("Measured-rest host deadline reached.")
        reference = self.started if self.last_sample is None else self.last_sample
        if now - reference > self.phase["maximum_sample_gap_s"]:
            raise TimeoutError("Measured-rest sample gap exceeded.")
        return now

    def _save(self, reading, before, after):
        row = dict(zip(FIELDS, (before - self.started, after - self.started,
            reading.timestamp_s, reading.voltage_v, reading.current_a, reading.status)))
        self.writer.writerow(row)
        self.stream.flush()
        self.rows.append(row)
        if reading.channel != self.channel:
            raise RuntimeError("Measured-rest reading has wrong channel.")
        if len(self.rows) > self.phase["rest_record_limit"]:
            raise RuntimeError("Measured-rest acquisition budget exceeded.")
        check_row(row, self.phase)

    def poll(self):
        if self.state == "completed":
            return deepcopy(self.outcome)
        if self.state != "active":
            raise RuntimeError("Measured rest is not active.")
        try:
            before = self._deadline()
            elapsed = before - self.started
            if before < self.next_sample and elapsed < self.phase["duration_s"]:
                return None
            try:
                reading = self.spot.read()
            except BaseException as error:
                reading = getattr(error, "reading", None)
                if reading is not None:
                    try:
                        self._save(reading, before, self._now())
                    except BaseException as saving_error:
                        error.add_note(f"Preserving rejected rest sample: {saving_error}")
                raise
            after = self._now()
            self._save(reading, before, after)
            self._deadline()
            self.last_sample = after
            self.next_sample = after + self.phase["interval_s"]
            if after - self.started >= self.phase["duration_s"]:
                return self._finish()
            return None
        except BaseException as error:
            self._fail(error)
            raise

    def _stop(self):
        if self.stop() is not True:
            raise RuntimeError("Measured-rest output stop was not verified.")
        if self._checked(self.selected.get_output_state) is not False:
            raise RuntimeError("Measured-rest output remains enabled.")
        if self._checked(self.selected.get_auto_output_on) is not False:
            raise RuntimeError("Measured-rest automatic output-on remains enabled.")

    def _finish(self):
        self._stop()
        finished = self._now()
        self.stream.close()
        data = (self.directory / "measurements.csv").read_bytes()
        report = {"state": "returned", "mode": "zero_current_logging",
            "run_directory": str(self.directory.resolve()), "source_current_a": 0.0,
            "requested_duration_s": self.phase["duration_s"],
            "elapsed_s": finished - self.started, "record_count": len(self.rows),
            "output_enabled_during_rest": True, "shutdown_verified": True,
            "shutdown_scope": "selected_channel" if self.shared else "both_channels",
            "csv_sha256": hashlib.sha256(data).hexdigest(), "error": None}
        self.document.update(state="completed", outcome=report)
        _write_json(self.directory / "rest.json", self.document)
        with (self.directory / "COMPLETE").open("x", encoding="utf-8") as stream:
            stream.write("Recorded zero-current rest finalized.\n")
        self.finalized, self.state = True, "completed"
        self.outcome = {"state": "completed", "stop_reason": "rest_elapsed",
                        "rest_report": report}
        return deepcopy(self.outcome)

    def _fail(self, error):
        if self.finalized:
            return
        shutdown = False
        try:
            self._stop()
            shutdown = True
        except BaseException as cleanup:
            error.add_note(f"Measured-rest cleanup failed: {cleanup}")
        if self.stream is not None:
            try:
                self.stream.close()
            except BaseException as cleanup:
                error.add_note(f"Measured-rest CSV close failed: {cleanup}")
        self.state = "failed"
        self.document.update(state="raised", error={"type": type(error).__name__,
            "message": str(error)}, shutdown_verified=shutdown, record_count=len(self.rows))
        try:
            _write_json(self.directory / "rest.json", self.document)
        except BaseException as saving:
            error.add_note(f"Measured-rest failure metadata could not be saved: {saving}")
        self.finalized = True

    def abort_after_stop(self, error, shutdown_report):
        if not self.finalized:
            self._fail(error)

    def run_to_completion(self, sleep=time.sleep, poll_interval_s=0.05):
        poll_interval_s = number(poll_interval_s, "poll_interval_s", True)
        try:
            self.start()
            while True:
                result = self.poll()
                if result is not None:
                    return result["rest_report"]
                sleep(poll_interval_s)
        except BaseException as error:
            if not self.finalized and self.state != "new":
                self._fail(error)
            raise


def validate_record(report, phase, config, allowed_root):
    if not is_measured(phase) or not isinstance(report, dict):
        raise ValueError("Missing measured-rest evidence.")
    path = Path(report.get("run_directory", ""))
    root = Path(allowed_root).resolve(strict=True)
    if not path.is_absolute():
        raise ValueError("Measured-rest reference must be absolute.")
    path = path.resolve(strict=True)
    if path == root or not path.is_relative_to(root):
        raise ValueError("Measured-rest reference escapes recording root.")
    if not (path / "COMPLETE").is_file():
        raise ValueError("Measured-rest COMPLETE is missing.")
    document = _json(path / "rest.json")
    if (document.get("rest_recording_schema_version") != 1 or document.get("state") != "completed"
            or document.get("configuration") != config or document.get("planned_phase") != phase
            or document.get("outcome") != report or document.get("source_current_a") != 0.0):
        raise ValueError("Measured-rest metadata differs from its expected configuration or phase.")
    source = document.get("source_readbacks", {})
    activation = document.get("activation", {})
    if (source.get("mode") != "CURR" or source.get("current_a") != 0.0
            or source.get("voltage_compliance_v") != phase["maximum_voltage_v"]
            or source.get("four_wire") is not config["instrument"]["four_wire"]
            or source.get("selected_output_off") is not True or source.get("fixed_dc") is not True
            or activation.get("output_reported_on") is not True):
        raise ValueError("Measured-rest source/activation evidence is incomplete.")
    before = number(activation.get("host_before_enable_s"), "activation before")
    after = number(activation.get("host_after_readback_s"), "activation after")
    if after < before:
        raise ValueError("Measured-rest activation timing is inconsistent.")
    data = (path / "measurements.csv").read_bytes()
    if hashlib.sha256(data).hexdigest() != report.get("csv_sha256"):
        raise ValueError("Measured-rest CSV hash differs.")
    reader = csv.DictReader(io.StringIO(data.decode("utf-8")))
    if tuple(reader.fieldnames or ()) != FIELDS:
        raise ValueError("Unexpected measured-rest CSV fields.")
    rows = []
    previous = None
    for raw in reader:
        if None in raw or any(value is None for value in raw.values()):
            raise ValueError("Malformed measured-rest CSV row.")
        row = {key: int(value) if key == "status" else float(value) for key, value in raw.items()}
        check_row(row, phase)
        end = row["host_after_s"]
        if previous is None:
            gap = end
        else:
            if row["host_before_s"] < previous or end <= previous:
                raise ValueError("Measured-rest host times are not monotonic.")
            gap = end - previous
        if gap > phase["maximum_sample_gap_s"]:
            raise ValueError("Measured-rest CSV sample gap exceeded.")
        rows.append(row)
        if len(rows) > phase["rest_record_limit"]:
            raise ValueError("Measured-rest record budget exceeded.")
        previous = end
    if (not rows or type(report.get("record_count")) is not int
            or len(rows) != report["record_count"] or report.get("state") != "returned"
            or report.get("mode") != "zero_current_logging" or report.get("source_current_a") != 0.0
            or report.get("requested_duration_s") != phase["duration_s"]
            or report.get("shutdown_verified") is not True or report.get("output_enabled_during_rest") is not True
            or report.get("error") is not None):
        raise ValueError("Measured-rest completion/count evidence is inconsistent.")
    elapsed = number(report.get("elapsed_s"), "rest elapsed")
    if not phase["duration_s"] <= rows[-1]["host_after_s"] <= elapsed:
        raise ValueError("Measured-rest duration endpoint was not recorded.")
    if rows[-1]["host_after_s"] >= maximum_duration(phase):
        raise ValueError("Measured-rest endpoint exceeded its deadline.")
    return path, rows


def _references(root):
    if (root / "sweep.json").is_file():
        doc = _json(root / "sweep.json")
        for stage in doc["stages"]:
            yield from _references(root / stage["stage_id"])
    elif (root / "experiment.json").is_file():
        doc = _json(root / "experiment.json")
        for phase, evidence in zip(doc["plan"]["phases"], doc["backend_evidence"]):
            if is_measured(phase):
                yield phase, evidence["rest_report"], doc["metadata"]["configuration"]
    elif (root / "dual_experiment.json").is_file():
        doc = _json(root / "dual_experiment.json")
        for preview, channel in zip(doc["plan"]["channels"], doc["report"]["channels"]):
            for phase, outcome in zip(preview["acquisition_plan"]["phases"], channel["outcomes"]):
                if is_measured(phase):
                    yield phase, outcome["rest_report"], config_for_phase(doc["plan"], preview["channel"], phase)


def attach_rest_exports(report, source, destination):
    summaries = []
    for index, (phase, evidence, config) in enumerate(_references(Path(source))):
        path, rows = validate_record(evidence, phase, config, source)
        name = f"rest_{index:04d}_ch{config['instrument']['channel']}_phase_{phase['phase_index']:04d}"
        export = Path(destination) / name
        export.mkdir()
        for filename in ("rest.json", "measurements.csv", "COMPLETE"):
            with (export / filename).open("xb") as stream:
                stream.write((path / filename).read_bytes())
        if hashlib.sha256((export / "measurements.csv").read_bytes()).hexdigest() != evidence["csv_sha256"]:
            raise ValueError("Measured-rest source changed during export.")
        validate_record(evidence, phase, config, source)
        voltages = [row["voltage_v"] for row in rows]
        summaries.append({"channel": config["instrument"]["channel"],
            "specimen_id": config["dut"]["specimen_id"], "phase_index": phase["phase_index"],
            "cycle": phase["cycle"], "follows": phase["follows"], "mode": "zero_current_logging",
            "sample_count": len(rows), "requested_duration_s": phase["duration_s"],
            "host_elapsed_s": evidence["elapsed_s"],
            "recorded_host_span_s": rows[-1]["host_after_s"] - rows[0]["host_after_s"],
            "start_voltage_v": voltages[0], "end_voltage_v": voltages[-1],
            "minimum_voltage_v": min(voltages), "maximum_voltage_v": max(voltages),
            "voltage_change_v": voltages[-1] - voltages[0],
            "maximum_abs_current_a": max(abs(row["current_a"]) for row in rows),
            "source_directory": str(path), "analysis_directory": name})
    if summaries:
        with (Path(destination) / "rest_summary.csv").open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=tuple(summaries[0]))
            writer.writeheader()
            writer.writerows(summaries)
        report["rest_phases"] = summaries
