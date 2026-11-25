"""INTERNAL PHYSICAL-CAPABLE recorded CC/CV/rest leases for SharedPowerSession.

Cooperative phase adapters, NOT a coordinator, public execution API, dual replay
or hardware qualification. The future coordinator must guard BOTH channels'
deadlines before/after every callback (including file work) and bound sleeps.
No session creation, shared reconfiguration, timer reset or session close here.
"""
from functools import wraps
from pathlib import Path
from experiments import zero_current_rest as zr
from experiments.constant_power import _number
from experiments.dual_phase import DualPhase
from experiments.phase_outcome import validate_transport_outcome
from experiments.power_sweep_contract import (
    build_sequence, digest, read_json, require, same, snapshot, tree_files, write_json,
)

MARKER = "NATIVE_STAGE_COMPLETE"


def _files(directory):
    return {name: value for name, value in tree_files(directory).items() if name != MARKER}


def _path(directory, *, new):
    path = Path(directory).absolute()
    require(".." not in path.parts and not any(p.is_symlink() for p in (path, *path.parents)),
            "Native stage path must not contain parent traversal or symlinks.")
    require(not path.exists() if new else path.is_dir(), "Expected a fresh/existing native stage directory.")
    return path


def _canonical(configuration, request):
    plan, requests = build_sequence(configuration)
    require(isinstance(request, dict) and any(request == entry for entry in requests), "Native request is not canonical.")
    canonical = next(entry for entry in requests if entry == request)
    same(request, canonical, "Native request types differ.")
    require(request["phase"]["type"] in ("charge_cc_cv", "discharge_cc", "rest"), "CP phases require a CP lease.")
    config = plan["initial_preparation"]["configuration"]
    phase = snapshot(canonical["phase"])  # Includes the validated acquisition/rest budget.
    return plan, config, phase


def _call(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        self.owner._thread()
        try:
            self.owner._usable()
            same(self.owner.session.plan, self.owner._base_plan, "Native session plan changed.")
            require(self.owner._channels.get(self.channel) is self and not self.stopped, "Native lease is terminal/stale.")
            return method(self, *args, **kwargs)
        except BaseException as error:
            self._fault(error)
            raise
    return wrapped


class _OwnedPhase(DualPhase):
    def _fail(self, error):
        self.lease._fault(error)  # Sole owner stops/closes; hook finalizes the recording.


class _OwnedRest(zr.ZeroCurrentRestTask):
    def _fail(self, error):
        self.lease._fault(error)

    def abort_after_stop(self, error, shutdown_report):
        """File-only abort: the owner has already attempted physical cleanup."""
        if self.finalized:
            return
        if self.stream is not None:
            self.stream.close()
        self.state = "failed"
        self.document.update(state="raised", error={"type": type(error).__name__, "message": str(error)},
                             shutdown_verified=shutdown_report["verified"], record_count=len(self.rows))
        if self.directory.exists():
            zr._write_json(self.directory / "rest.json", self.document)
        self.finalized = True


class BorrowedNativePhase:
    """Create only through owner.borrow_native(); one start and bounded polls."""
    simulation_only = False

    def __init__(self, owner, channel, request, directory):
        self.owner, self.channel = owner, channel
        self.configuration = snapshot(owner._profiles[channel]["configuration"])
        plan, self.config, self.phase = _canonical(self.configuration, snapshot(request))
        same(plan, owner._profiles[channel], "Native child plan differs from the owner's validated plan.")
        self.request, self.directory = snapshot(request), _path(directory, new=True)
        self.task = self.outcome = self.started = self.rest_started = None
        self.state, self.stopped = "new", False
        self.document = {"shared_native_stage_schema_version": 1, "state": "running", "channel": channel,
            "specimen_id": plan["specimen_id"], "request": self.request, "configuration": self.configuration,
            "identity": owner._identity, "resource_address": owner._resource, "physical_qualification": False,
            "started_s": None, "finished_s": None, "rest_started_s": None, "off_readbacks": [],
            "evidence": None, "files": {}, "error": None, "owner_cleanup": None}

    def _fault(self, error):
        if not hasattr(error, "shared_native_origin_channel"):
            error.shared_native_origin_channel = self.channel
            error.shared_native_origin_stage_id = self.request["stage_id"]
        self.owner._fail(error)

    def _stop(self):
        self.owner._stop_selected(self.channel)
        return True

    def _off_readback(self):
        selected = self.owner._selected(self.channel)
        state = self.owner._checked(selected.get_output_state)
        automatic = self.owner._checked(selected.get_auto_output_on)
        require(state is False and automatic is False, "Outputs-off rest changed selected output/auto state.")
        self.document["off_readbacks"].append({"host_s": self.owner._time(), "output_state": state, "automatic_output_on": automatic})

    @_call
    def start(self):
        require(self.state == "new", "Native phase start is single-use.")
        self.state = "preparing"
        self.started = self.owner._time()
        self.document["started_s"] = self.started
        self.directory.mkdir(exist_ok=False)
        write_json(self.directory / "stage.json", self.document, create=True)
        def begin():
            if self.phase["type"] == "rest" and not zr.is_measured(self.phase):
                self._stop()
                self.rest_started = self.owner._time()
                self.document["rest_started_s"] = self.rest_started
                self._off_readback()
                return
            if self.phase["type"] == "rest":
                self.task = _OwnedRest(self.owner._smu, self.config, self.phase, self.directory / "rest", self._stop,
                                       clock=self.owner._time, shared=True)
            else:
                self.task = _OwnedPhase(self.owner.session, self.channel, self.phase, self.directory, "phase",
                                        clock=self.owner._time, allow_output=True)
            self.task.lease = self
            self.task.start()
        self.owner._scoped(self.channel, begin)
        self.state = "active"

    @_call
    def check_deadline(self):
        require(self.state == "active", "Native phase is not active.")
        if self.task is not None:
            if self.task.state == "draining":
                require(self.owner._time() < self.task.drain_deadline, "Native final drain deadline reached.")
            elif self.task.state == "active":
                self.task._deadline()
        return self.next_deadline_s

    @property
    def next_deadline_s(self):
        """Off-rest: completion due time; other tasks: next failure deadline."""
        if self.task is None:
            return None if self.rest_started is None else self.rest_started + self.phase["duration_s"]
        if isinstance(self.task, _OwnedRest):
            reference = self.task.started if self.task.last_sample is None else self.task.last_sample
            return min(self.task.rest_deadline_s, reference + self.phase["maximum_sample_gap_s"])
        if self.task.state == "draining":
            return self.task.drain_deadline
        deadlines = [self.task.started + self.phase["maximum_duration_s"]]
        if self.task.monitor.host_cv_deadline_s is not None:
            deadlines.append(self.task.monitor.host_cv_deadline_s)
        return min(deadlines)

    @_call
    def poll(self):
        require(self.state == "active", "Native phase is not active.")
        def advance():
            if self.task is not None:
                return self.task.poll()
            self._off_readback()
            if self.owner._time() - self.rest_started < self.phase["duration_s"]:
                return None
            self._stop()
            return {"state": "completed", "stop_reason": "rest_elapsed"}
        result = self.owner._scoped(self.channel, advance)
        if result is None:
            return None
        require(result.get("state") == "completed", "Native task did not establish completion.")
        evidence = {"kind": "outputs_off_rest", "outcome": snapshot(result)}
        if self.task is not None:
            evidence["kind"] = "measured_rest" if self.phase["type"] == "rest" else "incremental_csv"
        self.document.update(state="completed", finished_s=self.owner._time(), evidence=evidence, files=_files(self.directory))
        write_json(self.directory / "stage.json", self.document)
        validate_borrowed_native_stage(self.directory, self.configuration, self.request, require_marker=False)
        with (self.directory / MARKER).open("x", encoding="utf-8", newline="\n") as stream:
            text = "SHARED_NATIVE_STAGE sha256=" + digest(self.directory / "stage.json") + "\n"
            require(stream.write(text) == len(text), "Short native-stage marker write.")
        self.outcome = {"state": "completed", "channel": self.channel, "specimen_id": self.document["specimen_id"],
                        "stage_id": self.request["stage_id"], "stop_reason": result["stop_reason"],
                        "directory": str(self.directory), "evidence_kind": evidence["kind"]}
        self.state, self.stopped = "completed", True
        del self.owner._channels[self.channel]
        return snapshot(self.outcome)

    def _after_owner_stop(self, primary, cleanup):
        """Owner-only file finalization, never driver I/O or another cleanup pass."""
        if self.state in ("new", "completed", "aborted"):
            self.stopped = True
            return
        error = primary if (getattr(primary, "shared_native_origin_channel", None) == self.channel
                             and getattr(primary, "shared_native_origin_stage_id", None) == self.request["stage_id"]) else RuntimeError(
                                 "Shared owner stopped before native stage completed: " + str(primary))
        shutdown = {"attempted": True, "verified": cleanup["outputs_reported_off"],
                    "evidence": "shared_owner_cleanup", "error_type": None, "error_message": None}
        self.state, self.stopped = "aborted", True
        failures = []
        if self.task is not None:
            try:
                self.task.abort_after_stop(error, shutdown)
                finalized = self.task.finalized if isinstance(self.task, _OwnedRest) else (
                    self.task.run is None or self.task.run.finalized)
                require(finalized is True, "Native failure recording was not finalized.")
            except BaseException as failure:
                failures.append(failure)
            finally:
                target = self.task.stream if isinstance(self.task, _OwnedRest) else self.task.run
                if target is not None:
                    try:
                        target.close()
                    except BaseException as failure:
                        failures.append(failure)
        if self.directory.exists():
            try:
                (self.directory / MARKER).unlink(missing_ok=True)
                self.document.update(state="raised", owner_cleanup=snapshot(cleanup),
                    error={"type": type(error).__name__, "message": str(error)}, files=_files(self.directory))
                write_json(self.directory / "stage.json", self.document)
            except BaseException as failure:
                failures.append(failure)
        if primary is None:
            failures.insert(0, error)
        if failures:
            first = failures[0]
            for failure in failures[1:]:
                first.add_note(f"Additional native file finalization: {type(failure).__name__}: {failure}")
            raise first


def validate_borrowed_native_stage(directory, configuration, request, *, require_marker=True):
    """Read-only per-phase structural/legacy validation, NOT full dual CP replay."""
    root = _path(directory, new=False)
    plan, config, phase = _canonical(configuration, request)
    document = read_json(root / "stage.json")
    require(type(document.get("shared_native_stage_schema_version")) is int and document["shared_native_stage_schema_version"] == 1 and document.get("state") == "completed", "Incomplete native stage.")
    for name, expected in (("request", request), ("configuration", configuration), ("channel", plan["channel"]), ("specimen_id", plan["specimen_id"])):
        same(document[name], expected, "Native stage identity/configuration differs: " + name)
    require(document["resource_address"] == plan["resource"], "Native stage resource differs.")
    identity = document["identity"].split(",")
    require(len(identity) == 4 and identity[1].strip().upper() == "B2902B" and identity[2].strip() == plan["execution_settings"]["expected_serial"], "Native stage identity differs.")
    before = _number(document["started_s"], "native start", zero=True)
    after = _number(document["finished_s"], "native finish", zero=True)
    require(after >= before, "Native stage times are reversed.")
    same(_files(root), document["files"], "Native stage files differ.")
    if require_marker:
        require((root / MARKER).read_text(encoding="utf-8") == "SHARED_NATIVE_STAGE sha256="+digest(root/"stage.json")+"\n", "Native stage marker differs.")
    evidence = document["evidence"]
    outcome = evidence["outcome"]
    require(outcome["state"] == "completed", "Native outcome is incomplete.")
    if phase["type"] == "rest":
        require(outcome["stop_reason"] == "rest_elapsed", "Wrong native rest endpoint.")
        if zr.is_measured(phase):
            require(evidence["kind"] == "measured_rest", "Wrong measured rest evidence kind.")
            same(outcome["rest_report"]["run_directory"], str(root/"rest"), "Measured rest path differs.")
            require(outcome["rest_report"]["shutdown_scope"] == "selected_channel", "Measured rest stop was not channel-local.")
            zr.validate_record(outcome["rest_report"], phase, config, root)
        else:
            require(evidence["kind"] == "outputs_off_rest", "Wrong outputs-off evidence kind.")
            started = _number(document["rest_started_s"], "rest start", zero=True)
            require(before <= started <= after and after-started >= phase["duration_s"], "Outputs-off rest ended early.")
            previous = started
            require(len(document["off_readbacks"]) >= 2, "Missing outputs-off readbacks.")
            for row in document["off_readbacks"]:
                stamp = _number(row["host_s"], "off readback time", zero=True)
                require(previous <= stamp <= after and row["output_state"] is False and row["automatic_output_on"] is False, "Invalid outputs-off readback.")
                previous = stamp
            require(previous-started >= phase["duration_s"], "Outputs-off endpoint was not observed.")
    else:
        from analysis.validate_recorded_run import validate_run
        require(evidence["kind"] == "incremental_csv", "Wrong active native evidence kind.")
        same(outcome["run_directory"], str(root/"phase"), "Active recording path differs.")
        check = validate_run(root/"phase")
        require(check["valid"] and check["state"] == "returned" and check.get("fault_flagged_rows",0) == 0, "Invalid native active recording.")
        metadata = read_json(root/"phase"/"metadata.json")
        for actual, expected in ((metadata["metadata"]["configuration"],config), (metadata["metadata"]["channel"],plan["channel"]), (metadata["experiment_phase"],phase)):
            same(actual, expected, "Native active metadata differs.")
        for name in ("phase_index", "cycle", "type"):
            same(outcome[name], phase[name], "Native active outcome identity differs.")
        transport = metadata["phase_transport_report"]
        validate_transport_outcome(phase, transport)
        require(transport["shutdown_scope"] == "selected_channel" and transport["retained_count"] == check["rows_read"]
                and outcome["stop_reason"] == transport["stop_reason"], "Native active outcome differs.")
    return snapshot(document)
