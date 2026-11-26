"""Supervised single-channel constant-power execution for ``python -m smu``.
Experimental: every result keeps physical_qualification false. Importing this
module never connects to hardware; connections happen only inside
run_configured_power_sweep, which the public CLI calls for --execute.
"""
import json
import math
from pathlib import Path
import re
import sys
import time
_RUN_NAME = re.compile(r"[A-Za-z0-9_-]{1,80}")
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
def probe_current_ceiling(document):
    """Zero-current probe ceiling: the profile's rest.maximum_abs_current_a."""
    value = document.get("rest", {}).get("maximum_abs_current_a")
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError(
            "Set a positive rest.maximum_abs_current_a; it is the "
            "zero-current probe ceiling for constant-power execution."
        )
    return float(value)
def run_configured_power_sweep(document, run_name, *, timeout_ms=500,
                               visa_library=None, clock=time.perf_counter,
                               sleep=time.sleep):
    """Run one recorded single-channel CP sweep. Returns a process status."""
    try:
        if not isinstance(run_name, str) or _RUN_NAME.fullmatch(run_name) is None:
            raise ValueError("Run name requires 1-80 ASCII letters, digits, underscores or hyphens.")
        ceiling = probe_current_ceiling(document)
        root = Path(document["output"]["root_directory"]).absolute()
        destination = root / run_name
        if any(p.is_symlink() for p in (root, *root.parents)) or ".." in root.parts:
            raise ValueError("Recording root must not contain symlinks or parent traversal.")
        if destination.exists():
            raise ValueError("Run destination already exists; no retry, resume or overwrite.")
        from experiments.power_sweep_backend import RecordedPowerSweepBackend
        from experiments.recorded_power_sweep import run_recorded_power_sweep
        def factory(request, configuration):
            return open_owned_session(request, configuration,
                timeout_ms=timeout_ms, visa_library=visa_library)
        backend = RecordedPowerSweepBackend(document, open_session=factory,
            maximum_probe_current_a=ceiling, clock=clock, sleep=sleep,
            output_authorized=True)
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
