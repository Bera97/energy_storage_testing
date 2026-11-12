"""Experimental supervised dual preconditioning and evidence checking."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import sys
import time
from experiments.settings import settings_for, require_ceilings, new_id
from uuid import uuid4
from experiments.config import load_config
from experiments.dual_coordinator import _run_backend
from experiments.dual_instrument_io import DualInstrumentSession
from experiments.dual_phase import DualPhase
from experiments.dual_preconditioning import build_dual_plan
from analysis.dual_experiment import check_batch
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
            "execution_mode": "supervised_dual",
            "specimen_confirmation_required": False,
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
def execute(
    plan,
    directory,
    serial,
    *,
    clock=time.monotonic,
    sleep=time.sleep,
    session_factory=None,
    phase_factory=None,
):
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
def run_configured(
    documents=None,
    *,
    config_paths=None,
    execute_requested=False,
    batch_id=None,
    expected_serial=None,
    confirm_specimen=None,
):
    """Preview or execute two profiles without parsing command arguments."""
    try:
        if documents is None:
            documents = [
                load_config(path)["config"] for path in config_paths
            ]
        else:
            documents = list(documents)
        require(len(documents) == 2, "Supply exactly two profiles.")
        settings = [
            settings_for(document, expected_serial=expected_serial)
            for document in documents
        ]
        require(
            settings[0]["expected_serial"] == settings[1]["expected_serial"],
            "Both profiles must specify the same expected instrument serial.",
        )
        plan = build_dual_plan(documents)
        for setting, preview in zip(settings, plan["channels"]):
            require_ceilings(setting, preview)
        if not execute_requested:
            print(json.dumps(plan, indent=2, allow_nan=False))
            return 0
        serial = settings[0]["expected_serial"]
        require(
            isinstance(serial, str) and bool(serial.strip()),
            "Set execution.expected_serial in both YAML profiles.",
        )
        batch_id = batch_id if batch_id is not None else new_id("dual")
        require(
            re.fullmatch(r"[A-Za-z0-9_-]{1,80}", batch_id) is not None,
            "Invalid batch ID.",
        )
        if confirm_specimen is not None:
            require(
                confirm_specimen == [item["specimen_id"] for item in plan["channels"]],
                "Legacy specimen arguments differ from the configured specimens.",
            )
        for preview in plan["channels"]:
            specimen = preview["specimen_id"]
            require(
                not specimen.endswith("_CH2_REVIEW") and not specimen.startswith("REPLACE_"),
                "Replace placeholder specimen IDs before live execution.",
            )
            require(
                preview["acquisition_plan"]["configuration"]["discharge"]["cutoff_voltage_v"] > 0,
                "Live dual execution requires a positive discharge cutoff.",
            )
        roots = [Path(doc["output"]["root_directory"]).resolve() for doc in documents]
        require(roots[0] == roots[1], "Use the same output root in both profiles.")
        directory = roots[0] / batch_id
        report = execute(plan, directory, serial)
        print(json.dumps({
            "state": report["state"],
            "batch_directory": str(directory),
            "final_stop_verified": report["final_stop_verified"],
            "session_close_returned": report["session_close_returned"],
        }, indent=2))
        return 0
    except BaseException as error:
        if isinstance(error, SystemExit):
            raise
        print(json.dumps({
            "state": "raised",
            "error_type": type(error).__name__,
            "error_message": str(error),
            "error_notes": list(getattr(error, "__notes__", ())),
            "batch_directory": getattr(error, "batch_directory", None),
        }, indent=2), file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "dual-check":
        parser = argparse.ArgumentParser(prog="dual-batch validation backend")
        parser.add_argument("directory")
        args = parser.parse_args(arguments[1:])
        result = check_batch(args.directory)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0 if result["valid"] else 1
    parser = argparse.ArgumentParser(
        prog="dual-execution backend",
        description="Supervised YAML-configured dual preconditioning. Defaults to preview.",
    )
    parser.add_argument("--config", action="append", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--batch-id", help="Optional unique ID; generated by default")
    parser.add_argument("--expected-serial", help="Legacy option; must agree with YAML")
    parser.add_argument("--confirm-specimen", action="append", help=argparse.SUPPRESS)
    args = parser.parse_args(
        arguments[1:] if arguments and arguments[0] == "dual-run" else arguments
    )
    if len(args.config) != 2:
        parser.error("Supply --config exactly twice.")
    return run_configured(
        config_paths=args.config,
        execute_requested=args.execute,
        batch_id=args.batch_id,
        expected_serial=args.expected_serial,
        confirm_specimen=args.confirm_specimen,
    )
