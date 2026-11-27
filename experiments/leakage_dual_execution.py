"""Supervised two-channel leakage-current execution for ``python -m smu``.
Experimental: every result keeps hardware_qualified false. Importing this module never
connects to hardware; connections happen only inside run_configured_dual_leakage.
"""
import json
from pathlib import Path
import sys
import time
from experiments.leakage_dual_runner import (
    enable_dual_dc_output, run_dual_leakage_test, validate_dual_documents,
)
from experiments.leakage_execution import DEFAULT_TIMEOUT_MS, _RUN_NAME, verify_identity
from experiments.leakage_ranging import apply_current_range, enable_auto_range
from experiments.power_execution import open_owned_session
def run_configured_dual_leakage(documents, run_name, *, timeout_ms=DEFAULT_TIMEOUT_MS,
                                visa_library=None, clock=time.monotonic, sleep=time.sleep):
    """Run one recorded two-channel leakage hold. Returns a process status."""
    destination = None
    try:
        if not isinstance(run_name, str) or _RUN_NAME.fullmatch(run_name) is None:
            raise ValueError("Run name requires 1-80 ASCII letters, digits, underscores or hyphens.")
        from experiments.settings import settings_for
        ordered, validated = validate_dual_documents(documents)
        bases = [item["preparation_config"] for item in validated]
        settings = [settings_for(base) for base in bases]
        keys = ("expected_serial", "maximum_voltage_v", "maximum_current_a")
        if any(item[key] is None for item in settings for key in keys):
            raise ValueError(
                "Set execution.expected_serial, execution.maximum_voltage_v and "
                "execution.maximum_current_a (or use a concrete USB resource and DUT ratings).")
        if len({item["expected_serial"] for item in settings}) != 1:
            raise ValueError("Both profiles must specify the same expected serial.")
        root = Path(bases[0]["output"]["root_directory"]).absolute()
        destination = root / run_name
        if any(p.is_symlink() for p in (root, *root.parents)) or ".." in root.parts:
            raise ValueError("Recording root must not contain symlinks or parent traversal.")
        if destination.exists():
            raise ValueError("Run destination already exists; no retry, resume or overwrite.")
        from acquisition.spot_acquisition import SpotAcquisition
        from experiments.charge_setup import configure_charge_source
        print(f"Dual leakage hold starting in {destination}. Ctrl+C stops the run; "
              "outputs of both channels are shut down on every exit.", file=sys.stderr)
        smu = open_owned_session(None, bases[0], timeout_ms=timeout_ms, visa_library=visa_library)
        try:
            try:
                verify_identity(smu, settings[0]["expected_serial"])
            except BaseException:
                try:
                    smu.shutdown()
                except BaseException:
                    pass
                raise
            result = run_dual_leakage_test(
                ordered, smu=smu,
                maximum_voltage_v=settings[0]["maximum_voltage_v"],
                maximum_current_a=settings[0]["maximum_current_a"],
                configure_source=configure_charge_source,
                spot_factory=SpotAcquisition, enable_output=enable_dual_dc_output,
                set_range=apply_current_range, restore_auto_range=enable_auto_range,
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
            "elapsed_s": result["elapsed_s"],
            "shutdown_verified": result["shutdown_verified"],
            "channels": result["channels"],
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
