"""Assemble one discharge phase while both outputs remain OFF.

No output enabling or acquisition initiation is performed.

The caller supplies and owns the CSV writer.
A successful return does not authorize cell execution.
"""

from copy import deepcopy
from dataclasses import dataclass

from acquisition.buffered_acquisition import BufferedAcquisition
from acquisition.discharge_trace_transport import (
    DischargeTraceTransport,
)
from acquisition.monitored_acquisition import MonitoredAcquisition
from drivers.trace_timebase import configure_trace_timebase
from experiments.acquisition_budget import build_acquisition_plan
from experiments.config import require_charge_monitoring_settings
from experiments.discharge_source import (
    configure_discharge_source,
)


@dataclass(frozen=True)
class PreparedDischarge:
    """Prepared components; hardware execution has not been started."""

    phase: dict
    evidence: dict
    acquisition: object
    transport: DischargeTraceTransport


def _require_fresh_writer(writer):
    if writer.closed:
        raise ValueError("CSV writer must be open.")

    if writer.failed:
        raise ValueError("CSV writer must not be failed.")

    if (
        type(writer.records_flushed) is not int
        or writer.records_flushed != 0
    ):
        raise ValueError("CSV writer must be fresh.")


def prepare_discharge(
    smu,
    document,
    writer,
    *,
    cycle=1,
    acquisition_factory=MonitoredAcquisition,
):
    """Prepare one discharge phase without enabling output.

    Configuration/count/writer validation occurs before instrument
    access. Instrument-side failures receive a preparation report
    and trigger a shutdown attempt.

    The caller must still retain an outer shutdown boundary.
    """
    if type(cycle) is not int or cycle < 1:
        raise ValueError("cycle must be a positive integer.")

    if not callable(acquisition_factory):
        raise ValueError("acquisition_factory must be callable.")

    plan = build_acquisition_plan(
        document,
        maximum_records=BufferedAcquisition.MAX_RECORDS,
    )
    config = plan["configuration"]
    settings = require_charge_monitoring_settings(config)

    if cycle > plan["cycles"]:
        raise ValueError(
            "Requested cycle exceeds the configured cycle count."
        )

    _require_fresh_writer(writer)

    phase = deepcopy(
        next(
            item
            for item in plan["phases"]
            if item["cycle"] == cycle
            and item["type"] == "discharge_cc"
        )
    )
    phase["monitoring"] = {
        "maximum_sample_gap_s": settings[
            "maximum_sample_gap_s"
        ],
        "maximum_voltage_v": settings[
            "maximum_voltage_v"
        ],
    }

    number = config["instrument"]["channel"]
    budget = phase["acquisition_budget"]

    evidence = {
        "state": "preparing",
        "cycle": cycle,
        "phase_index": phase["phase_index"],
        "source": None,
        "acquisition": None,
        "timebase": None,
        "both_outputs_reported_off": None,
        "shutdown_verified": None,
        "hardware_execution_authorized": False,
        "error": None,
        "cleanup_error": None,
    }

    try:
        evidence["source"] = configure_discharge_source(
            smu,
            config,
        )

        acquisition = acquisition_factory(smu, number)
        acquisition.configure(
            count=budget["record_count"],
            interval_s=budget["interval_s"],
            nplc=config["instrument"]["nplc"],
        )

        evidence["acquisition"] = {
            "channel": number,
            "requested_count": budget["record_count"],
            "requested_interval_s": budget["interval_s"],
            "requested_nplc": config["instrument"]["nplc"],
            "nominal_span_s": budget["nominal_span_s"],
            "maximum_phase_duration_s": phase[
                "maximum_duration_s"
            ],
        }

        evidence["timebase"] = configure_trace_timebase(
            smu,
            number,
        )

        for channel in (smu.chan1, smu.chan2):
            state = channel.get_output_state()
            smu.check_errors()

            if state is not False:
                raise RuntimeError(
                    "Both outputs must still report OFF "
                    "after discharge preparation."
                )

        evidence["both_outputs_reported_off"] = True
        _require_fresh_writer(writer)

        transport = DischargeTraceTransport(
            acquisition,
            writer,
        )
        evidence["state"] = "prepared"

        return PreparedDischarge(
            phase=phase,
            evidence=evidence,
            acquisition=acquisition,
            transport=transport,
        )

    except BaseException as original_error:
        evidence["state"] = "raised"
        evidence["error"] = {
            "type": type(original_error).__name__,
            "message": str(original_error),
        }

        try:
            smu.shutdown()
        except BaseException as cleanup_error:
            evidence["shutdown_verified"] = False
            evidence["cleanup_error"] = {
                "type": type(cleanup_error).__name__,
                "message": str(cleanup_error),
            }
            original_error.add_note(
                "Discharge preparation cleanup could not verify "
                "shutdown: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
        else:
            evidence["shutdown_verified"] = True

        original_error.discharge_preparation_report = deepcopy(
            evidence
        )
        raise