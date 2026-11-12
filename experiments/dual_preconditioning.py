"""Hardware-free dual-channel preconditioning planning. No live backend."""
from copy import deepcopy
import json
import sys
from experiments.config import load_config
from experiments.run_live import execution_preview
def build_dual_plan(documents, *, maximum_records=100000):
    """Validate two independent profiles without I/O or execution."""
    if not isinstance(documents, (list, tuple)) or len(documents) != 2:
        raise ValueError("Exactly two configuration documents are required.")
    previews = [
        execution_preview(deepcopy(document), maximum_records=maximum_records)
        for document in documents
    ]
    if {item["channel"] for item in previews} != {1, 2}:
        raise ValueError(
            "Use exactly one profile for channel 1 and one for channel 2."
        )
    resources = [item["resource"].strip() for item in previews]
    if resources[0] != resources[1]:
        raise ValueError(
            "Both profiles must use the same exact VISA resource address."
        )
    specimens = [item["specimen_id"] for item in previews]
    if specimens[0] == specimens[1]:
        raise ValueError("The channels must have different specimen IDs.")
    previews.sort(key=lambda item: item["channel"])
    return {
        "dual_plan_schema_version": 1,
        "state": "preview",
        "execution_enabled": False,
        "live_backend_available": False,
        "will_connect": False,
        "will_enable_output": False,
        "resource": resources[0],
        "channel_count": 2,
        "intended_phase_progression": "independent",
        "intended_fault_policy": "stop_both",
        "joint_timing_qualified": False,
        "electrical_setup_verified": False,
        "channels": previews,
        "limitations": [
            "Planning only: concurrent execution is not implemented.",
            "Joint timing, operating envelopes and wiring require separate qualification.",
            "Do not run two single-channel controllers against this instrument.",
        ],
    }
def preview_configured(
    documents=None,
    *,
    config_paths=None,
    maximum_records=100000,
):
    """Print a dual-channel plan without parsing command arguments."""
    try:
        if documents is None:
            documents = [
                load_config(path)["config"]
                for path in config_paths
            ]
        result = build_dual_plan(
            documents,
            maximum_records=maximum_records,
        )
        print(json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        ))
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(
            f"Dual-channel planning failed: {error}",
            file=sys.stderr,
        )
        return 1
    return 0
