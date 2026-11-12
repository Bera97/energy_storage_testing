"""Outputs-off phase setup: source configuration and preparation.

Outputs-OFF configuration of voltage-source CC-CV charging.

No output-ON or acquisition-initiate command is sent.

This helper verifies configuration consistency, not the complete
instrument operating envelope, physical wiring, DUT condition,
protection configuration, or electrical isolation.

A successful return does not authorize cell execution.

Caller must retain an outer shutdown boundary around configuration
and all subsequent operations. Configuration is not transactional:
a failure can leave partially changed settings.

Assemble one charge phase while both outputs remain OFF.

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

A successful return does not authorize cell execution."""

import math
from experiments.config import require_charge_monitoring_settings
from experiments.config import validate_config
from copy import deepcopy
from dataclasses import dataclass
from acquisition.buffered_acquisition import BufferedAcquisition
from acquisition.phase_trace_transport import ChargeTraceTransport
from acquisition.monitored_acquisition import MonitoredAcquisition
from drivers.trace_timebase import configure_trace_timebase
from experiments.acquisition_budget import build_acquisition_plan
from experiments.charge_cv_classifier import VoltageSourceCVClassifier


class ChargeConfigurationError(RuntimeError):
    """Instrument readback does not match required charge settings."""


def _finite_readback(value, name):
    if isinstance(value, bool) or not isinstance(
        value, (int, float)
    ):
        raise ChargeConfigurationError(
            f"{name} readback must be a finite number."
        )

    try:
        result = float(value)
    except (ValueError, OverflowError) as error:
        raise ChargeConfigurationError(
            f"{name} readback must be a finite number."
        ) from error

    if not math.isfinite(result):
        raise ChargeConfigurationError(
            f"{name} readback must be a finite number."
        )

    return result


def configure_charge_source(smu, document):
    """Configure and verify one voltage-source charge channel.

    Configuration validation occurs before any instrument access.

    Returns:
        JSON-compatible configuration evidence. This is not an
        execution authorization or a complete hardware preflight.
    """
    validated = validate_config(document)
    monitoring = require_charge_monitoring_settings(document)

    config = validated["config"]
    currents = validated["resolved_currents"]

    number = config["instrument"]["channel"]
    selected = smu.chan1 if number == 1 else smu.chan2

    target = float(config["charge"]["target_voltage_v"])
    charge_current = currents["charge_current_a"]

    ratings = config["dut"]["ratings"]
    rated_voltage = float(ratings["maximum_voltage_v"])
    rated_charge_current = float(
        ratings["maximum_continuous_charge_current_a"]
    )
    four_wire = config["instrument"]["four_wire"]

    def checked(operation, *arguments):
        result = operation(*arguments)
        smu.check_errors()
        return result

    def require_outputs_off():
        for channel in (smu.chan1, smu.chan2):
            state = checked(channel.get_output_state)

            if state is not False:
                raise ChargeConfigurationError(
                    f"Channel {channel.channel} must explicitly "
                    "report output OFF."
                )

    def require_text(command, expected):
        response = checked(smu._query, command)

        if response.strip().upper() != expected:
            raise ChargeConfigurationError(
                f"{command}: expected {expected!r}, "
                f"received {response!r}."
            )

    smu.check_errors()
    require_outputs_off()

    checked(smu.abort)

    for channel in (smu.chan1, smu.chan2):
        checked(channel.set_auto_output_on, False)

    checked(selected.set_output_off_mode, "HIZ")
    checked(selected.configure_fixed_dc, "VOLT")
    checked(selected.set_voltage, target)
    checked(selected.set_current_limit, charge_current)
    checked(selected.set_four_wire, four_wire)

    source_mode = checked(selected.get_source_mode)

    if source_mode != "VOLT":
        raise ChargeConfigurationError(
            "Selected channel did not report VOLT source mode."
        )

    require_text(f":SOUR{number}:FUNC:SHAP?", "DC")
    require_text(f":SOUR{number}:VOLT:MODE?", "FIX")

    actual_voltage = _finite_readback(
        checked(selected.get_voltage_setpoint),
        "voltage setpoint",
    )
    actual_current = _finite_readback(
        checked(selected.get_current_limit),
        "current compliance",
    )

    if not math.isclose(
        actual_voltage,
        target,
        rel_tol=1e-9,
        abs_tol=1e-12,
    ):
        raise ChargeConfigurationError(
            "Voltage setpoint readback differs from the target."
        )

    if not math.isclose(
        actual_current,
        charge_current,
        rel_tol=1e-9,
        abs_tol=1e-15,
    ):
        raise ChargeConfigurationError(
            "Current compliance readback differs from "
            "the configured charging current."
        )

    if not 0 < actual_voltage <= rated_voltage:
        raise ChargeConfigurationError(
            "Voltage setpoint readback exceeds the supplied "
            "DUT voltage rating or is not positive."
        )

    if not 0 < actual_current <= rated_charge_current:
        raise ChargeConfigurationError(
            "Current compliance readback exceeds the supplied "
            "DUT charge-current rating or is not positive."
        )

    actual_four_wire = checked(selected.get_four_wire)

    if actual_four_wire is not four_wire:
        raise ChargeConfigurationError(
            "Sense-wiring configuration readback differs."
        )

    off_mode = checked(selected.get_output_off_mode)

    if off_mode != "HIZ":
        raise ChargeConfigurationError(
            "Selected output-off mode did not read back as HIZ."
        )

    for channel in (smu.chan1, smu.chan2):
        automatic = checked(channel.get_auto_output_on)

        if automatic is not False:
            raise ChargeConfigurationError(
                f"Channel {channel.channel}: automatic "
                "output-on did not read back as OFF."
            )

    require_outputs_off()

    return {
        "channel": number,
        "source_function": source_mode,
        "source_shape": "DC",
        "source_operation": "FIX",
        "voltage_setpoint_v": actual_voltage,
        "current_compliance_a": actual_current,
        "four_wire": actual_four_wire,
        "selected_output_off_mode": off_mode,
        "both_outputs_reported_off": True,
        "both_auto_output_on_reported_off": True,
        "charge_monitoring": monitoring,
        "operating_envelope_verified": False,
        "hardware_execution_authorized": False,
    }


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
