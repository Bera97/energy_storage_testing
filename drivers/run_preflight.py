"""Check a post-run export destination before hardware operation.

No instrument communication.

Does not reserve the run directory or guarantee future write success.
The exporter must still enforce exclusive creation.
"""

import json
from pathlib import Path
import re
import tempfile


def prepare_run_destination(base_directory, run_id, *, metadata):
    """Validate input and perform a temporary write/read check.

    Returns the proposed run directory without creating it.
    Temporary probe files are removed on context-manager exit.
    """
    if (
        not isinstance(run_id, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id) is None
    ):
        raise ValueError(
            "run_id must contain 1-80 ASCII letters, digits, "
            "underscores, or hyphens."
        )

    if not isinstance(metadata, dict):
        raise TypeError("metadata must be a dictionary.")

    # Match the exporter's JSON restrictions.
    json.dumps(
        metadata,
        ensure_ascii=False,
        allow_nan=False,
    )

    base = Path(base_directory)
    destination = base / run_id

    if destination.exists() or destination.is_symlink():
        raise FileExistsError(
            f"Run destination already exists: {destination}"
        )

    base.mkdir(parents=True, exist_ok=True)

    probe = b"SMU run destination write check\n"

    with tempfile.TemporaryFile(mode="w+b", dir=base) as handle:
        handle.write(probe)
        handle.flush()
        handle.seek(0)

        if handle.read() != probe:
            raise OSError("Destination write/read check failed.")

    # Recheck after the probe; this still does not reserve the name.
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(
            f"Run destination appeared during preflight: {destination}"
        )

    return destination