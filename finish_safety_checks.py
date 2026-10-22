"""Apply the final deadline/history patches to the current source tree.

No instrument communication.

Checks all replacements and syntax before modifying existing files.
Creates a backup directory before writing changes.
"""

from datetime import datetime, timezone
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parent


PATCHES = {
    "experiments/charge_controller.py": [
        (
            "    def update(\n",
            (
                "    @property\n"
                "    def first_cv_elapsed_s(self):\n"
                '        """First observed CV time in the controller clock."""\n'
                "        return self._first_cv_elapsed_s\n"
                "\n"
                "    def update(\n"
            ),
        ),
    ],
    "experiments/first_sample_trace_monitor.py": [
        (
            "    @property\n"
            "    def instrument_timestamp_origin_s(self):\n",
            (
                "    @property\n"
                "    def policy_monitor(self):\n"
                '        """Underlying monitor owning the endpoint controller."""\n'
                "        return self._monitor\n"
                "\n"
                "    @property\n"
                "    def instrument_timestamp_origin_s(self):\n"
            ),
        ),
        (
            "    if isinstance(monitor, FirstSampleTraceMonitor):\n"
            "        return monitor.timing_evidence\n",
            (
                "    if hasattr(monitor, \"measurement_monitor\"):\n"
                "        return monitor.timing_evidence\n"
                "\n"
                "    if isinstance(monitor, FirstSampleTraceMonitor):\n"
                "        return monitor.timing_evidence\n"
            ),
        ),
    ],
    "experiments/phase_polling.py": [
        (
            "        return timeout - elapsed\n",
            (
                "        remaining = timeout - elapsed\n"
                "\n"
                "        cv_deadline = getattr(\n"
                "            monitor, \"host_cv_deadline_s\", None\n"
                "        )\n"
                "\n"
                "        if cv_deadline is not None:\n"
                "            cv_deadline = _finite_number(\n"
                "                cv_deadline, \"host CV deadline\"\n"
                "            )\n"
                "\n"
                "            if now >= cv_deadline:\n"
                "                raise TimeoutError(\n"
                "                    \"Host CV deadline reached.\"\n"
                "                )\n"
                "\n"
                "            remaining = min(\n"
                "                remaining, cv_deadline - now\n"
                "            )\n"
                "\n"
                "        return remaining\n"
            ),
        ),
    ],
    "acquisition/charge_trace_transport.py": [
        (
            "import time\n",
            (
                "import time\n"
                "\n"
                "from experiments.phase_safety_monitor import (\n"
                "    PhaseSafetyMonitor,\n"
                ")\n"
            ),
        ),
        (
            "        records = []\n",
            (
                "        monitor = PhaseSafetyMonitor(\n"
                "            monitor,\n"
                "            phase_started_s=phase_started_s,\n"
                "            maximum_duration_s=phase[\"maximum_duration_s\"],\n"
                "            clock=clock,\n"
                "            measurement_time_basis=measurement_time_basis,\n"
                "            instrument_timestamp_origin_s=(\n"
                "                instrument_timestamp_origin_s\n"
                "            ),\n"
                "            phase_elapsed_at_origin_s=phase_elapsed_at_origin_s,\n"
                "        )\n"
                "\n"
                "        records = []\n"
            ),
        ),
        (
            "        def start():\n"
            "            acquisition._preflight()\n",
            (
                "        def start():\n"
                "            acquisition._preflight()\n"
                "            monitor.before_initiation()\n"
            ),
        ),
    ],
    "acquisition/discharge_trace_transport.py": [
        (
            "import time\n",
            (
                "import time\n"
                "\n"
                "from experiments.phase_safety_monitor import (\n"
                "    PhaseSafetyMonitor,\n"
                ")\n"
            ),
        ),
        (
            "        records = []\n",
            (
                "        monitor = PhaseSafetyMonitor(\n"
                "            monitor,\n"
                "            phase_started_s=phase_started_s,\n"
                "            maximum_duration_s=phase[\"maximum_duration_s\"],\n"
                "            clock=clock,\n"
                "            measurement_time_basis=measurement_time_basis,\n"
                "            instrument_timestamp_origin_s=(\n"
                "                instrument_timestamp_origin_s\n"
                "            ),\n"
                "            phase_elapsed_at_origin_s=phase_elapsed_at_origin_s,\n"
                "        )\n"
                "\n"
                "        records = []\n"
            ),
        ),
        (
            "        def start():\n"
            "            acquisition._preflight()\n",
            (
                "        def start():\n"
                "            acquisition._preflight()\n"
                "            monitor.before_initiation()\n"
            ),
        ),
    ],
    "recording/experiment_phase_run.py": [
        (
            '            "activation_report",\n',
            (
                '            "activation_report",\n'
                '            "executor_previous_shutdown_report",\n'
            ),
        ),
    ],
}


def main():
    required = ROOT / "experiments" / "phase_safety_monitor.py"

    if not required.is_file():
        raise RuntimeError(
            "Create experiments/phase_safety_monitor.py first."
        )

    compile(
        required.read_text(encoding="utf-8"),
        str(required),
        "exec",
    )

    pending = {}

    for relative, replacements in PATCHES.items():
        path = ROOT / relative
        original = path.read_text(encoding="utf-8")
        updated = original

        for old, new in replacements:
            # Recognize an already-applied replacement.
            if new in updated:
                continue

            count = updated.count(old)

            if count != 1:
                raise RuntimeError(
                    f"{relative}: expected exactly one match, "
                    f"found {count}. No files have been changed."
                )

            updated = updated.replace(old, new, 1)

        compile(updated, str(path), "exec")

        if updated != original:
            pending[relative] = updated

    if not pending:
        print("All safety patches are already applied.")
        return

    stamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )
    backup = ROOT / f"safety_patch_backup_{stamp}"
    backup.mkdir()

    # Back up every affected file before writing any replacement.
    for relative in pending:
        destination = backup / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)

    for relative, updated in pending.items():
        (ROOT / relative).write_text(
            updated,
            encoding="utf-8",
        )

    print(f"Patched {len(pending)} files.")
    print(f"Backups: {backup.name}")
    print("Run the offline tests before proceeding.")


if __name__ == "__main__":
    main()