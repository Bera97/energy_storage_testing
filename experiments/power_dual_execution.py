"""Supervised two-channel constant-power execution for ``python -m smu``.
Experimental: every result keeps physical_qualification false. Importing this module never
connects to hardware; connections happen only inside run_configured_dual_power_sweep.
The shared engine records native acquisition; run --analyze afterwards to replay and
validate the recording.
"""
import json
from pathlib import Path
import sys
import time
from experiments.dual_instrument_io import DualInstrumentSession
from experiments.dual_preconditioning import build_dual_plan
from experiments.power_execution import _RUN_NAME, probe_current_ceiling
from experiments.power_planning import build_dual_constant_power_sweep_plan
from experiments.power_shared_coordinator import run_shared_power_sweep
from experiments.power_shared_transport import SharedPowerSession
MAXIMUM_SCHEDULER_STEPS = 10_000_000
def _release(session, owner, primary):
    """Shut down and close a connected session that the shared owner never claimed."""
    if (owner is not None and owner.claimed) or session is None or session.smu is None:
        return
    steps = []
    if session.connected:
        steps.append(("shutdown", session.shutdown_all))
    steps.append(("close", session.close))
    for label, operation in steps:
        try:
            operation()
        except BaseException as error:
            if primary is not None:
                primary.add_note(f"Unclaimed session {label}: {type(error).__name__}: {error}")
def run_configured_dual_power_sweep(documents, run_name, *, timeout_ms=2000, visa_library=None,
                                    clock=time.perf_counter, sleep=time.sleep, smu_factory=None,
                                    maximum_scheduler_steps=MAXIMUM_SCHEDULER_STEPS):
    """Run one recorded two-channel CP sweep on one shared session. Returns a process status."""
    destination = None
    session = owner = primary = None
    status = 0
    try:
        if not isinstance(run_name, str) or _RUN_NAME.fullmatch(run_name) is None:
            raise ValueError("Run name requires 1-80 ASCII letters, digits, underscores or hyphens.")
        ordered = sorted(documents, key=lambda document: document["instrument"]["channel"])
        plan = build_dual_constant_power_sweep_plan(ordered)
        serial = plan["channels"][0]["execution_settings"]["expected_serial"]
        probes = {document["instrument"]["channel"]: probe_current_ceiling(document)
                  for document in ordered}
        root = Path(ordered[0]["output"]["root_directory"]).absolute()
        if any(p.is_symlink() for p in (root, *root.parents)) or ".." in root.parts:
            raise ValueError("Recording root must not contain symlinks or parent traversal.")
        destination = root / run_name
        if destination.exists():
            raise ValueError("Run destination already exists; no retry, resume or overwrite.")
        root.mkdir(parents=True, exist_ok=True)
        base = build_dual_plan([item["initial_preparation"]["configuration"]
                                for item in plan["channels"]])
        extra = {} if smu_factory is None else {"smu_factory": smu_factory}
        session = DualInstrumentSession(base, serial, timeout_ms=timeout_ms,
                                        visa_library=visa_library, **extra)
        print(f"Dual CP sweep starting in {destination}. Ctrl+C stops the run; outputs of both "
              "channels are shut down on every exit. Run --analyze afterwards.", file=sys.stderr)
        session.connect()
        owner = SharedPowerSession(ordered, session=session, clock=clock, output_authorized=True)
        result = run_shared_power_sweep(
            owner, destination, sleep=sleep, maximum_probe_current_a=probes,
            execute_requested=True, maximum_scheduler_steps=maximum_scheduler_steps)
        print(json.dumps({
            "state": result["state"],
            "directory": str(destination),
            "channels": [{key: item[key] for key in
                          ("channel", "specimen_id", "state", "completed_stage_count")}
                         for item in result["channels"]],
            "outputs_reported_off": result["owner_cleanup"]["outputs_reported_off"],
            "completion_qualified": result["completion_qualified"],
            "physical_qualification": False,
            "execution_mode": "supervised_experimental_bench",
            "next_step": "Run --analyze RUN_NAME to replay and validate the recording.",
        }, indent=2, allow_nan=False))
    except BaseException as error:
        if isinstance(error, SystemExit):
            raise
        primary = error
        status = 130 if isinstance(error, KeyboardInterrupt) else 1
    finally:
        _release(session, owner, primary)
    if primary is not None:
        cleanup = getattr(primary, "shared_power_cleanup_report", None) or {}
        print(json.dumps({
            "state": "raised", "error_type": type(primary).__name__,
            "error_message": str(primary), "notes": list(getattr(primary, "__notes__", ())),
            "directory": None if destination is None else str(destination),
            "outputs_reported_off": cleanup.get("outputs_reported_off"),
        }, indent=2), file=sys.stderr)
    return status
