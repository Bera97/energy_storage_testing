"""Preconditioning backend using recorded active phases and OFF rest.

No default active-phase executor.
No output-enabling implementation.
No CLI integration.

The injected executor owns active-phase preparation, execution,
and recording finalization. It must return ExperimentPhaseRun.

The runner retains the outer experiment shutdown boundary.
"""

from copy import deepcopy
import time

from experiments.rest_outputs_off import rest_outputs_off
from recording.experiment_phase_run import ExperimentPhaseRun


class RecordedPreconditioningBackend:
    """Bind the runner to recorded active phases and outputs-OFF rest."""

    def __init__(
        self,
        smu,
        *,
        execute_active_phase,
        rest_poll_interval_s=0.1,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        if not callable(execute_active_phase):
            raise ValueError(
                "execute_active_phase must be callable."
            )

        if not callable(clock) or not callable(sleep):
            raise ValueError(
                "clock and sleep must be callable."
            )

        self.smu = smu
        self.execute_active_phase = execute_active_phase
        self.rest_poll_interval_s = rest_poll_interval_s
        self.clock = clock
        self.sleep = sleep

        self.phase_outcomes = []

    def shutdown_verified(self):
        """Attempt shutdown and explicitly check both reported states."""
        self.smu.shutdown()

        for channel in (self.smu.chan1, self.smu.chan2):
            state = channel.get_output_state()
            self.smu.check_errors()

            if state is not False:
                raise RuntimeError(
                    "Backend could not verify both outputs OFF."
                )

        return True

    def _active(self, phase, expected_type):
        if not isinstance(phase, dict):
            raise TypeError("phase must be a dictionary.")

        if phase.get("type") != expected_type:
            raise ValueError(
                f"Expected phase type {expected_type!r}."
            )

        planned_phase = deepcopy(phase)

        # Do not hand control to an active executor while reported
        # output state is ON or unknown.
        self.shutdown_verified()

        run = self.execute_active_phase(
            deepcopy(planned_phase)
        )

        if not isinstance(run, ExperimentPhaseRun):
            raise TypeError(
                "Active executor must return ExperimentPhaseRun."
            )

        result = run.result_for_phase(planned_phase)

        self.phase_outcomes.append({
            "phase_index": planned_phase["phase_index"],
            "cycle": planned_phase["cycle"],
            "type": planned_phase["type"],
            "state": "recording_finalized",
            "stop_reason": result.stop_reason,
            "run_directory": str(run.directory.resolve()),
        })

        return result

    def charge(self, phase):
        return self._active(phase, "charge_cc_cv")

    def discharge(self, phase):
        return self._active(phase, "discharge_cc")

    def rest(self, phase):
        if not isinstance(phase, dict):
            raise TypeError("phase must be a dictionary.")

        if phase.get("type") != "rest":
            raise ValueError("Expected a rest phase.")

        report = rest_outputs_off(
            self.smu,
            phase["duration_s"],
            poll_interval_s=self.rest_poll_interval_s,
            clock=self.clock,
            sleep=self.sleep,
        )

        self.phase_outcomes.append({
            "phase_index": phase["phase_index"],
            "cycle": phase["cycle"],
            "type": "rest",
            "state": "rest_completed",
            "stop_reason": "rest_elapsed",
            "rest_report": report,
        })