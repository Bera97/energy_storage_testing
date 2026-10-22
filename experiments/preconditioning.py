"""Hardware-free planning of cell preconditioning sequences."""

from copy import deepcopy

from experiments.config import validate_config


def build_plan(document):
    """Return a validated, JSON-compatible preconditioning plan.

    cycles is the total number of charge/rest/discharge/rest sequences.
    No files are written and no hardware is accessed.

    Missing explicit monitoring settings are reported rather than
    silently defaulted. Hardware execution remains disabled.
    """
    validated = validate_config(document)
    config = validated["config"]
    currents = validated["resolved_currents"]
    monitoring = validated["charge_monitoring"]

    charge = config["charge"]
    discharge = config["discharge"]
    rest = config["rest"]

    phases = []

    def append_phase(cycle, phase_type, **settings):
        phases.append({
            "phase_index": len(phases),
            "cycle": cycle,
            "type": phase_type,
            **settings,
        })

    for cycle in range(
        1,
        config["experiment"]["cycles"] + 1,
    ):
        charge_settings = {
            "current_magnitude_a": currents[
                "charge_current_a"
            ],
            "target_voltage_v": charge[
                "target_voltage_v"
            ],
            "termination_current_a": currents[
                "charge_termination_current_a"
            ],
            "termination_current_reference": charge[
                "termination"
            ]["current_reference"],
            "termination_current_factor": charge[
                "termination"
            ]["current_factor"],
            "termination_hold_time_s": charge[
                "termination"
            ]["hold_time_s"],
            "termination_requires_cv": True,
            "maximum_duration_s": charge[
                "maximum_duration_s"
            ],
            "maximum_cv_duration_s": charge[
                "maximum_cv_duration_s"
            ],
        }

        if monitoring is not None:
            charge_settings["monitoring"] = deepcopy(
                monitoring
            )

        append_phase(
            cycle,
            "charge_cc_cv",
            **charge_settings,
        )

        append_phase(
            cycle,
            "rest",
            follows="charge",
            duration_s=rest["after_charge_s"],
            requires_verified_output_off=True,
        )

        append_phase(
            cycle,
            "discharge_cc",
            current_magnitude_a=currents[
                "discharge_current_a"
            ],
            cutoff_voltage_v=discharge[
                "cutoff_voltage_v"
            ],
            maximum_duration_s=discharge[
                "maximum_duration_s"
            ],
        )

        append_phase(
            cycle,
            "rest",
            follows="discharge",
            duration_s=rest["after_discharge_s"],
            requires_verified_output_off=True,
        )

    return {
        "plan_schema_version": 1,
        "experiment_type": "preconditioning",
        "experiment_name": config["experiment"]["name"],
        "cycles": config["experiment"]["cycles"],
        "phase_count": len(phases),
        "execution_enabled": False,
        "current_convention": (
            "Positive magnitudes only; hardware polarity "
            "must be assigned by the future runner."
        ),
        "timeout_policy": (
            "Charge or discharge timeout is an unsuccessful "
            "phase; do not continue to subsequent phases."
        ),
        "resolved_currents": currents,
        "charge_monitoring": deepcopy(monitoring),
        "missing_charge_monitoring_fields": list(
            validated["missing_charge_monitoring_fields"]
        ),
        "configuration": config,
        "phases": phases,
    }