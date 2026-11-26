"""Experimental single-channel CP bench CLI. Preview by default; no qualification claim."""
import argparse
import json
from pathlib import Path
import re
import sys
import time


def open_owned_session(request, document, *, timeout_ms, visa_library):
    from experiments.run_live import _new_smu
    smu = None
    try:
        smu = _new_smu(document["instrument"]["visa_address"], timeout_ms, visa_library)
        smu.connect()
        return smu
    except BaseException as primary:
        if smu is not None:
            try:
                if smu.is_connected:
                    smu.shutdown()
            except BaseException as secondary:
                primary.add_note(f"Failed connection shutdown: {secondary}")
            try:
                smu.close()
            except BaseException as secondary:
                primary.add_note(f"Failed connection close: {secondary}")
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-specimen")
    parser.add_argument("--maximum-probe-current-a", type=float)
    parser.add_argument("--run-name")
    parser.add_argument("--timeout-ms", type=int, default=500)
    parser.add_argument("--visa-library")
    args = parser.parse_args(argv)
    try:
        from experiments.config import load_config
        from experiments.power_planning import build_constant_power_sweep_plan
        document = load_config(args.config)["config"]
        plan = build_constant_power_sweep_plan(document)
        if not args.execute:
            print(json.dumps({"state": "preview", "will_connect": False,
                "will_enable_output": False, "physical_qualification": False,
                "bench_execution_requires_explicit_authorization": True,
                "plan": plan}, indent=2, allow_nan=False))
            return 0
        if args.confirm_specimen != document["dut"]["specimen_id"]:
            raise ValueError("--confirm-specimen must exactly match dut.specimen_id.")
        if args.maximum_probe_current_a is None:
            raise ValueError("An explicitly reviewed --maximum-probe-current-a is required.")
        if args.run_name is None or re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.run_name) is None:
            raise ValueError("--run-name requires 1-80 ASCII letters, digits, underscores or hyphens.")
        if args.timeout_ms <= 0:
            raise ValueError("--timeout-ms must be positive.")
        root = Path(document["output"]["root_directory"]).absolute()
        destination = root / args.run_name
        if any(p.is_symlink() for p in (root, *root.parents)) or ".." in root.parts:
            raise ValueError("Recording root must not contain symlinks or parent traversal.")
        if destination.exists():
            raise ValueError("Run destination already exists; no retry, resume or overwrite.")
        from experiments.power_sweep_backend import RecordedPowerSweepBackend
        from experiments.recorded_power_sweep import run_recorded_power_sweep
        def factory(request, configuration):
            return open_owned_session(request, configuration,
                timeout_ms=args.timeout_ms, visa_library=args.visa_library)
        backend = RecordedPowerSweepBackend(document, open_session=factory,
            maximum_probe_current_a=args.maximum_probe_current_a,
            clock=time.perf_counter, sleep=time.sleep, output_authorized=True)
        result = run_recorded_power_sweep(document, destination,
            backend=backend, execute_requested=True)
        print(json.dumps({**result, "physical_qualification": False,
            "execution_mode": "supervised_experimental_bench"}, indent=2, allow_nan=False))
        return 0
    except BaseException as error:
        if isinstance(error, SystemExit):
            raise
        print(json.dumps({"state": "raised", "error_type": type(error).__name__,
            "error_message": str(error), "notes": list(getattr(error, "__notes__", ())),
            "directory": getattr(error, "power_sweep_directory", None)}, indent=2), file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1


if __name__ == "__main__":
    raise SystemExit(main())
