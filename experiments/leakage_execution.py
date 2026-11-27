"""Supervised single-channel leakage-current execution for ``python -m smu``.

Experimental: every result keeps hardware_qualified false. Importing this module
never connects to hardware; connections happen only inside
run_configured_leakage, which the public CLI calls for --execute.
"""
import json
from pathlib import Path
import re
import sys
import time

from experiments.leakage_ranging import apply_current_range, enable_auto_range
from experiments.leakage_runner import run_leakage_test
from experiments.power_execution import open_owned_session

_RUN_NAME = re.compile(r"[A-Za-z0-9_-]{1,80}")
DEFAULT_TIMEOUT_MS = 2000


def verify_identity(smu, expected_serial):
    identity = smu.identity
    if not isinstance(identity, str):
        raise ValueError("Instrument identity must be text.")
    parts = [part.strip() for part in identity.split(",")]
    if (len(parts) != 4 or not all(parts) or parts[1].upper() != "B2902B"
            or parts[2] != expected_serial):
        raise ValueError("Instrument model/serial identity mismatch.")


def run_configured_leakage(document, run_name, *, timeout_ms=DEFAULT_TIMEOUT_MS,
                           visa_library=None, clock=time.monotonic, sleep=time.sleep):
    """Run one recorded constant-voltage leakage hold. Returns a process status."""
    destination = None
    try:
        if not isinstance(run_name, str) or _RUN_NAME.fullmatch(run_name) is None:
            raise ValueError("Run name requires 1-80 ASCII letters, digits, underscores or hyphens.")
        from experiments.leakage_planning import validate_leakage_test
        from experiments.settings import settings_for
        base = validate_leakage_test(document)["preparation_config"]
        settings = settings_for(base)
        if any(settings[key] is None for key in ("expected_serial", "maximum_voltage_v", "maximum_current_a")):
            raise ValueError(
                "Set execution.expected_serial, execution.maximum_voltage_v and "
                "execution.maximum_current_a (or use a concrete USB resource and DUT ratings)."
            )
        root = Path(base["output"]["root_directory"]).absolute()
        destination = root / run_name
        if any(p.is_symlink() for p in (root, *root.parents)) or ".." in root.parts:
            raise ValueError("Recording root must not contain symlinks or parent traversal.")
        if destination.exists():
            raise ValueError("Run destination already exists; no retry, resume or overwrite.")
        from acquisition.spot_acquisition import SpotAcquisition
        from drivers.dc_output import enable_dc_output
        from experiments.charge_setup import configure_charge_source
        print(f"Leakage hold starting in {destination}. Ctrl+C stops the run; "
              "outputs are shut down on every exit.", file=sys.stderr)
        smu = open_owned_session(None, base, timeout_ms=timeout_ms, visa_library=visa_library)
        try:
            try:
                verify_identity(smu, settings["expected_serial"])
            except BaseException:
                try:
                    smu.shutdown()
                except BaseException:
                    pass
                raise
            result = run_leakage_test(
                document, smu=smu,
                maximum_voltage_v=settings["maximum_voltage_v"],
                maximum_current_a=settings["maximum_current_a"],
                configure_source=configure_charge_source,
                spot_factory=SpotAcquisition, enable_output=enable_dc_output,
                set_range=apply_current_range,
                restore_auto_range=enable_auto_range,
                run_directory=destination, clock=clock, sleep=sleep,
            )
        finally:
            try:
                smu.close()
            except BaseException as error:
                print(f"Session close failed: {error}", file=sys.stderr)
        print(json.dumps({
            "state": result["state"],
            "directory": str(destination),
            "recorded_sample_count": result["recorded_sample_count"],
            "planned_sample_count": result["planned_sample_count"],
            "elapsed_s": result["elapsed_s"],
            "read_retry_count": result["read_retry_count"],
            "early_stop_triggered": result["early_stop"]["triggered"],
            "shutdown_verified": result["shutdown_verified"],
            "hardware_qualified": False,
            "execution_mode": "supervised_experimental_bench",
        }, indent=2, allow_nan=False))
        return 0
    except BaseException as error:
        if isinstance(error, SystemExit):
            raise
        print(json.dumps({
            "state": "raised", "error_type": type(error).__name__,
            "error_message": str(error), "notes": list(getattr(error, "__notes__", ())),
            "directory": None if destination is None else str(destination),
        }, indent=2), file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1
