"""Experimental supervised dual preconditioning and evidence checking."""
import argparse
from copy import deepcopy
import json
import math
from pathlib import Path
import re
import sys
import time
from uuid import uuid4
from analysis.validate_preconditioning_experiment import _read_json
from analysis.validate_recorded_run import validate_run
from experiments.config import load_config
from experiments.dual_coordinator import _run_backend
from experiments.dual_instrument_io import DualInstrumentSession
from experiments.dual_phase import DualPhase
from experiments.dual_preconditioning import build_dual_plan
from experiments.phase_outcome import validate_transport_outcome
def require(condition, message):
    if not condition:
        raise ValueError(message)
def write_manifest(directory, document):
    text = json.dumps(document, indent=2, allow_nan=False) + "\n"
    temporary = directory / (uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(text)
        temporary.replace(directory / "dual_experiment.json")
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
class RealBackend:
    def __init__(
        self, plan, directory, serial, *,
        clock, session_factory=None, phase_factory=None,
    ):
        self.plan = deepcopy(plan)
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=False)
        self.clock = clock
        self.phase_factory = (
            DualPhase if phase_factory is None else phase_factory
        )
        self.active = {}
        self.events = []
        self.close_returned = False
        self.last_shutdown = None
        self.document = {
            "dual_manifest_schema_version": 1,
            "state": "running",
            "expected_serial": serial,
            "identity": None,
            "plan": self.plan,
            "progress": [],
            "report": None,
            "experimental_qualification": True,
            "operating_envelope_verified": False,
        }
        self.save()
        factory = (
            DualInstrumentSession
            if session_factory is None
            else session_factory
        )
        self.session = factory(plan, serial)
    def save(self):
        write_manifest(self.directory, self.document)
    def check_deadlines(self):
        for task in self.active.values():
            if task.state == "active":
                task._deadline()
            elif (
                task.state == "draining"
                and task._now() >= task.drain_deadline
            ):
                raise TimeoutError(
                    "Final trace drain deadline reached."
                )
    def phase_deadline(self, channel, default):
        task = self.active.get(channel)
        if task is not None and task.state == "draining":
            return task.drain_deadline
        return default
    def connect(self, resource):
        require(
            resource == self.plan["resource"],
            "Resource differs from plan.",
        )
        self.session.connect()
        self.document["identity"] = self.session.identity
        self.events.append({"action": "connect"})
    def configure_shared(self, plan):
        require(plan == self.plan, "Shared plan changed.")
        self.session.configure_shared()
        self.document["trigger_group"] = deepcopy(
            self.session.trigger_group_evidence
        )
        self.document["trace_clock"] = deepcopy(
            self.session.trace_clock_evidence
        )
        self.events.append({"action": "configure_shared"})
        self.save()
    def start_phase(self, channel, phase):
        self.check_deadlines()
        require(
            channel not in self.active,
            "Channel already owns a phase.",
        )
        task = self.phase_factory(
            self.session,
            channel,
            phase,
            self.directory / f"channel_{channel}",
            f"phase_{phase['phase_index']:04d}",
            clock=self.clock,
            allow_output=True,
        )
        self.active[channel] = task
        task.start()
        self.events.append({
            "action": "start",
            "channel": channel,
            "phase_index": phase["phase_index"],
        })
        self.check_deadlines()
    def poll_phase(self, channel):
        self.check_deadlines()
        result = self.active[channel].poll()
        self.check_deadlines()
        return result
    def stop_channel(self, channel):
        result = self.session.stop_channel(channel)
        self.events.append({
            "action": "stop_channel",
            "channel": channel,
        })
        return result
    def finalize_phase(self, channel, phase, outcome):
        self.check_deadlines()
        if phase["type"] != "rest":
            task = self.active[channel]
            require(
                task.state == "completed" and task.run.finalized,
                "Phase recording was not finalized.",
            )
            require(
                outcome["run_directory"]
                == str(task.run.directory.resolve()),
                "Recording reference mismatch.",
            )
            del self.active[channel]
        self.document["progress"].append({
            "channel": channel,
            **deepcopy(outcome),
        })
        self.save()
        self.check_deadlines()
        print(
            f"CH{channel}: phase {phase['phase_index']} "
            f"{phase['type']} completed",
            file=sys.stderr,
            flush=True,
        )
    def shutdown_all(self):
        report = {
            "attempted": True,
            "verified": None,
            "evidence": "shutdown_attempt_started",
            "error_type": None,
            "error_message": None,
        }
        try:
            result = self.session.shutdown_all()
            require(
                result is True,
                "Global shutdown was not verified.",
            )
        except BaseException as error:
            report.update(
                verified=False,
                evidence="smu_shutdown_raised",
                error_type=type(error).__name__,
                error_message=str(error),
            )
            self.last_shutdown = report
            raise
        report.update(
            verified=True,
            evidence="smu_shutdown_returned",
        )
        self.last_shutdown = report
        self.events.append({"action": "shutdown_all"})
        return True
    def abort_unfinished(self, primary, verified):
        failures = []
        for channel, task in self.active.items():
            if task.state == "completed":
                continue
            error = RuntimeError(
                "Dual experiment interrupted: " + str(primary)
            )
            try:
                task.abort_after_stop(error, self.last_shutdown)
                if task.run is not None and not task.run.finalized:
                    raise RuntimeError(
                        "Failure metadata did not finalize."
                    )
            except BaseException as export_error:
                failures.append(str(export_error))
            finally:
                if task.run is not None:
                    try:
                        task.run.close()
                    except BaseException as close_error:
                        failures.append(str(close_error))
            self.document["progress"].append({
                "channel": channel,
                "phase_index": task.phase["phase_index"],
                "state": "aborted",
                "run_directory": (
                    str(task.run.directory.resolve())
                    if task.run is not None
                    else None
                ),
            })
        if failures:
            raise RuntimeError("; ".join(failures))
    def close(self):
        self.session.close()
        self.close_returned = True
        self.events.append({"action": "close"})
def check_batch(directory, *, require_marker=True):
    root = Path(directory).resolve()
    result = {
        "valid": False,
        "phase_recordings_checked": 0,
        "errors": [],
        "warnings": [],
    }
    try:
        doc = _read_json(root / "dual_experiment.json")
        require(
            doc["dual_manifest_schema_version"] == 1,
            "Unsupported dual manifest.",
        )
        require(
            doc["state"] == "completed",
            "Dual experiment is not completed.",
        )
        if require_marker:
            require(
                (root / "DUAL_COMPLETE").is_file(),
                "Missing DUAL_COMPLETE.",
            )
        plan = doc["plan"]
        rebuilt = build_dual_plan(
            [
                preview["acquisition_plan"]["configuration"]
                for preview in plan["channels"]
            ],
            maximum_records=plan["channels"][0][
                "acquisition_plan"
            ]["acquisition_planning_limit"],
        )
        require(plan == rebuilt, "Stored plan does not validate.")
        report = doc["report"]
        require(
            report["state"] == "completed"
            and report["simulation_only"] is False
            and report["hardware_execution_enabled"] is True
            and report.get("error") is None,
            "Report does not establish real-backend completion.",
        )
        require(
            report["startup_stop_verified"] is True
            and report["final_stop_verified"] is True,
            "Global shutdown evidence is incomplete.",
        )
        require(
            report["session_close_returned"] is True
            and not report["cleanup_errors"],
            "Cleanup did not complete.",
        )
        identity = doc["identity"].split(",")
        require(
            len(identity) == 4
            and identity[1].strip().upper() == "B2902B"
            and identity[2].strip() == doc["expected_serial"],
            "Instrument identity mismatch.",
        )
        require(
            len(report["channels"]) == 2,
            "Expected two channel reports.",
        )
        seen = set()
        for preview, channel in zip(
            plan["channels"], report["channels"]
        ):
            require(
                channel["channel"] == preview["channel"]
                and channel["specimen_id"] == preview["specimen_id"]
                and channel["state"] == "completed",
                "Channel report mismatch.",
            )
            phases = preview["acquisition_plan"]["phases"]
            require(
                len(phases) == len(channel["outcomes"]),
                "Incomplete phase outcomes.",
            )
            previous = None
            for phase, outcome in zip(
                phases, channel["outcomes"]
            ):
                require(
                    all(
                        outcome[key] == phase[key]
                        for key in ("phase_index", "cycle", "type")
                    ),
                    "Phase identity mismatch.",
                )
                require(
                    outcome["state"] == "completed"
                    and outcome["stop_verified"] is True,
                    "Phase stop not verified.",
                )
                start = outcome["started_s"]
                finish = outcome["finished_s"]
                require(
                    all(
                        type(value) in (int, float)
                        and math.isfinite(value)
                        for value in (start, finish)
                    )
                    and finish >= start
                    and (previous is None or start >= previous),
                    "Invalid phase timing.",
                )
                previous = finish
                if phase["type"] == "rest":
                    require(
                        outcome["stop_reason"] == "rest_elapsed"
                        and finish - start + 1e-9
                        >= phase["duration_s"],
                        "Invalid rest evidence.",
                    )
                    continue
                reference = Path(outcome["run_directory"])
                require(
                    reference.is_absolute(),
                    "Recording reference must be absolute.",
                )
                reference = reference.resolve(strict=True)
                require(
                    reference.is_relative_to(root)
                    and reference != root
                    and reference not in seen,
                    "Invalid or duplicate recording path.",
                )
                seen.add(reference)
                validation = validate_run(reference)
                require(
                    validation["valid"]
                    and validation["state"] == "returned",
                    "Invalid phase recording: "
                    + str(validation["errors"]),
                )
                require(
                    validation.get("fault_flagged_rows", 0) == 0,
                    "Recording contains measurement faults.",
                )
                metadata = _read_json(
                    reference / "metadata.json"
                )
                require(
                    metadata["metadata"]["channel"]
                    == channel["channel"]
                    and metadata["experiment_phase"] == phase,
                    "Recorded phase differs from plan.",
                )
                transport = metadata["phase_transport_report"]
                validate_transport_outcome(phase, transport)
                require(
                    transport["retained_count"]
                    == validation["rows_read"]
                    and outcome["stop_reason"]
                    == transport["stop_reason"],
                    "Recording count or endpoint mismatch.",
                )
                result["phase_recordings_checked"] += 1
                result["warnings"].extend(
                    f"CH{channel['channel']} "
                    f"phase {phase['phase_index']}: {warning}"
                    for warning in validation["warnings"]
                )
        result["valid"] = True
    except (
        OSError, ValueError, TypeError, KeyError, RuntimeError
    ) as error:
        result["errors"].append(
            f"{type(error).__name__}: {error}"
        )
    return result
def execute(
    plan,
    directory,
    serial,
    *,
    qualification_run=False,
    clock=time.monotonic,
    sleep=time.sleep,
    session_factory=None,
    phase_factory=None,
):
    require(
        qualification_run is True,
        "Explicit qualification-run authorization is required.",
    )
    backend = None
    try:
        backend = RealBackend(
            plan,
            directory,
            serial,
            clock=clock,
            session_factory=session_factory,
            phase_factory=phase_factory,
        )
        try:
            report = _run_backend(
                plan,
                backend,
                clock=clock,
                sleep=sleep,
                simulation_only=False,
            )
        except BaseException as error:
            report = getattr(
                error, "dual_run_report", {"state": "raised"}
            )
            report["session_close_returned"] = (
                backend.close_returned
            )
            report.pop("close_succeeded", None)
            backend.document.update(
                state="raised",
                report=report,
                error={
                    "type": type(error).__name__,
                    "message": str(error),
                },
            )
            try:
                backend.save()
            except BaseException as export_error:
                error.add_note(
                    f"Batch failure export failed: {export_error}"
                )
            raise
        report["session_close_returned"] = (
            backend.close_returned
        )
        report.pop("close_succeeded", None)
        backend.document.update(
            state="completed",
            report=report,
        )
        backend.save()
        validation = check_batch(
            backend.directory,
            require_marker=False,
        )
        require(
            validation["valid"],
            "; ".join(validation["errors"]),
        )
        with (backend.directory / "DUAL_COMPLETE").open(
            "x", encoding="utf-8"
        ) as handle:
            handle.write(
                "Completed experimental dual-channel run.\n"
            )
        return report
    except BaseException as error:
        error.batch_directory = str(
            Path(directory).resolve()
        )
        if backend is not None:
            backend.document["state"] = "raised"
            backend.document["error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
            try:
                backend.save()
            except BaseException as export_error:
                error.add_note(
                    f"Batch failure manifest export failed: "
                    f"{export_error}"
                )
        raise
def main(argv=None):
    arguments = list(
        sys.argv[1:] if argv is None else argv
    )
    if arguments and arguments[0] == "dual-check":
        parser = argparse.ArgumentParser(
            prog="python -m smu dual-check"
        )
        parser.add_argument("directory")
        args = parser.parse_args(arguments[1:])
        result = check_batch(args.directory)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0 if result["valid"] else 1
    parser = argparse.ArgumentParser(
        prog="python -m smu dual-run",
        description=(
            "Experimental supervised dual preconditioning. "
            "Defaults to preview."
        ),
    )
    parser.add_argument(
        "--config", action="append", required=True
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--qualification-run", action="store_true"
    )
    parser.add_argument("--batch-id")
    parser.add_argument("--expected-serial")
    parser.add_argument(
        "--confirm-specimen", action="append"
    )
    args = parser.parse_args(
        arguments[1:]
        if arguments and arguments[0] == "dual-run"
        else arguments
    )
    if len(args.config) != 2:
        parser.error("Supply --config exactly twice.")
    if args.execute and (
        not args.qualification_run
        or not args.batch_id
        or not args.expected_serial
        or not args.confirm_specimen
    ):
        parser.error(
            "Execution requires --qualification-run, --batch-id, "
            "--expected-serial and --confirm-specimen twice."
        )
    try:
        plan = build_dual_plan([
            load_config(path)["config"]
            for path in args.config
        ])
        if not args.execute:
            print(json.dumps(
                plan, indent=2, allow_nan=False
            ))
            return 0
        require(
            re.fullmatch(
                r"[A-Za-z0-9_-]{1,80}", args.batch_id
            ) is not None,
            "Invalid batch ID.",
        )
        require(
            args.confirm_specimen
            == [
                preview["specimen_id"]
                for preview in plan["channels"]
            ],
            "Specimen confirmations must match "
            "channel 1 then channel 2.",
        )
        for preview in plan["channels"]:
            specimen = preview["specimen_id"]
            require(
                not specimen.endswith("_CH2_REVIEW")
                and not specimen.startswith("REPLACE_"),
                "Replace placeholder specimen IDs "
                "before live execution.",
            )
            require(
                preview["acquisition_plan"]["configuration"][
                    "discharge"
                ]["cutoff_voltage_v"] > 0,
                "Qualification requires a positive "
                "discharge cutoff.",
            )
        roots = [
            Path(
                preview["acquisition_plan"]["configuration"][
                    "output"
                ]["root_directory"]
            ).resolve()
            for preview in plan["channels"]
        ]
        require(
            roots[0] == roots[1],
            "Use the same output root in both profiles.",
        )
        directory = roots[0] / args.batch_id
        report = execute(
            plan,
            directory,
            args.expected_serial,
            qualification_run=True,
        )
        print(json.dumps({
            "state": report["state"],
            "batch_directory": str(directory),
            "final_stop_verified": (
                report["final_stop_verified"]
            ),
            "session_close_returned": (
                report["session_close_returned"]
            ),
        }, indent=2))
        return 0
    except BaseException as error:
        if isinstance(error, SystemExit):
            raise
        print(json.dumps({
            "state": "raised",
            "error_type": type(error).__name__,
            "error_message": str(error),
            "error_notes": list(
                getattr(error, "__notes__", ())
            ),
            "batch_directory": getattr(
                error, "batch_directory", None
            ),
        }, indent=2), file=sys.stderr)
        return (
            130
            if isinstance(error, KeyboardInterrupt)
            else 1
        )
