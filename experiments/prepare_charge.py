"""Assemble one charge phase while both outputs remain OFF.

No output enabling or acquisition initiation is performed.

Preparation:
- Validates configuration and acquisition budgets.
- Requires explicit charge-monitoring settings.
- Requires a fresh, open CSV writer.
- Configures and verifies the charge source.
- Configures finite monitored acquisition.
- Configures and verifies the trace timestamp policy.
- Confirms both outputs still report OFF.
- Constructs the classifier and trace transport.

The writer is supplied and owned by the caller. This function does
not create recording directories, close the writer, or finalize
run metadata.

A successful return does not authorize cell execution.
"""

from copy import deepcopy
from dataclasses import dataclass

from acquisition.buffered_acquisition import BufferedAcquisition
from acquisition.charge_trace_transport import ChargeTraceTransport
from acquisition.monitored_acquisition import MonitoredAcquisition
from drivers.trace_timebase import configure_trace_timebase
from experiments.acquisition_budget import build_acquisition_plan
from experiments.charge_cv_classifier import (
    VoltageSourceCVClassifier,
)
from experiments.charge_source import configure_charge_source
from experiments.config import require_charge_monitoring_settings


@dataclass(frozen=True)
class PreparedCharge:
    """Prepared components; no hardware execution has been started."""

    phase: dict
    evidence: dict
    acquisition: object
    transport: ChargeTraceTransport
    cv_classifier: VoltageSourceCVClassifier


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


def prepare_charge(
    smu,
    document,
    writer,
    *,
    cycle=1,
    acquisition_factory=MonitoredAcquisition,
):
    """Prepare one cycle's charge without enabling output.

    acquisition_factory is injectable for offline tests. Real
    integration should use the default MonitoredAcquisition.

    Configuration/count/writer validation failures occur before
    instrument access.

    Instrument-side failures receive charge_preparation_report.
    Shutdown is attempted without replacing the original exception.
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

    monitoring = require_charge_monitoring_settings(config)

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
            and item["type"] == "charge_cc_cv"
        )
    )

    classifier = VoltageSourceCVClassifier(
        target_voltage_v=monitoring["target_voltage_v"],
        voltage_tolerance_v=monitoring["voltage_tolerance_v"],
        maximum_voltage_v=monitoring["maximum_voltage_v"],
    )

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
        evidence["source"] = configure_charge_source(
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
                    "after charge preparation."
                )

        evidence["both_outputs_reported_off"] = True

        # Recheck before handing the writer to the transport.
        _require_fresh_writer(writer)

        transport = ChargeTraceTransport(
            acquisition,
            writer,
        )

        evidence["state"] = "prepared"

        return PreparedCharge(
            phase=phase,
            evidence=evidence,
            acquisition=acquisition,
            transport=transport,
            cv_classifier=classifier,
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
                "Charge preparation cleanup could not verify "
                "shutdown: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
        else:
            evidence["shutdown_verified"] = True

        original_error.charge_preparation_report = deepcopy(
            evidence
        )
        raise