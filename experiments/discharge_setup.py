"""Outputs-off phase setup: source configuration and preparation.

Outputs-OFF configuration of constant-current discharge.

No output-ON or acquisition-initiate command is sent.

Current convention:
    Negative current into the DUT means discharge.

Voltage compliance is an upper voltage setting. It is not the
lower discharge-voltage cutoff.

This helper checks configuration consistency with supplied DUT
ratings, not the complete instrument source/sink operating envelope.

Caller must retain an outer shutdown boundary. Configuration is
not transactional and can leave partially changed settings.

Assemble one discharge phase while both outputs remain OFF.

No output enabling or acquisition initiation is performed.

The caller supplies and owns the CSV writer.
A successful return does not authorize cell execution."""

import math
from experiments.config import validate_config
from copy import deepcopy
from dataclasses import dataclass
from acquisition.buffered_acquisition import BufferedAcquisition
from acquisition.phase_trace_transport import DischargeTraceTransport
from acquisition.monitored_acquisition import MonitoredAcquisition
from drivers.trace_timebase import configure_trace_timebase
from experiments.acquisition_budget import build_acquisition_plan
from experiments.config import require_charge_monitoring_settings


class DischargeConfigurationError(RuntimeError):
    """Instrument readback differs from required discharge settings."""


def _finite_readback(value, name):
    if isinstance(value, bool) or not isinstance(
        value, (int, float)
    ):
        raise DischargeConfigurationError(
            f"{name} readback must be a finite number."
        )

    try:
        result = float(value)
    except (ValueError, OverflowError) as error:
        raise DischargeConfigurationError(
            f"{name} readback must be a finite number."
        ) from error

    if not math.isfinite(result):
        raise DischargeConfigurationError(
            f"{name} readback must be a finite number."
        )

    return result


def configure_discharge_source(smu, document):
    """Configure and verify discharge settings without enabling output.

    Validation failures occur before instrument access.

    Returns:
        JSON-compatible configuration evidence. A successful return
        is not permission to begin a cell experiment.
    """
    validated = validate_config(document)
    config = validated["config"]
    currents = validated["resolved_currents"]

    number = config["instrument"]["channel"]
    selected = smu.chan1 if number == 1 else smu.chan2

    magnitude = currents["discharge_current_a"]
    setpoint = -magnitude

    ratings = config["dut"]["ratings"]
    voltage_compliance = float(
        ratings["maximum_voltage_v"]
    )
    rated_discharge_current = float(
        ratings["maximum_continuous_discharge_current_a"]
    )

    cutoff = float(
        config["discharge"]["cutoff_voltage_v"]
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
                raise DischargeConfigurationError(
                    f"Channel {channel.channel} must explicitly "
                    "report output OFF."
                )

    def require_text(command, expected):
        response = checked(smu._query, command)

        if response.strip().upper() != expected:
            raise DischargeConfigurationError(
                f"{command}: expected {expected!r}, "
                f"received {response!r}."
            )

    smu.check_errors()
    require_outputs_off()

    checked(smu.abort)

    for channel in (smu.chan1, smu.chan2):
        checked(channel.set_auto_output_on, False)

    checked(selected.set_output_off_mode, "HIZ")
    checked(selected.configure_fixed_dc, "CURR")
    checked(selected.set_current, setpoint)
    checked(selected.set_voltage_limit, voltage_compliance)
    checked(selected.set_four_wire, four_wire)

    source_mode = checked(selected.get_source_mode)

    if source_mode != "CURR":
        raise DischargeConfigurationError(
            "Selected channel did not report CURR source mode."
        )

    require_text(f":SOUR{number}:FUNC:SHAP?", "DC")
    require_text(f":SOUR{number}:CURR:MODE?", "FIX")

    actual_current = _finite_readback(
        checked(selected.get_current_setpoint),
        "current setpoint",
    )
    actual_voltage = _finite_readback(
        checked(selected.get_voltage_limit),
        "voltage compliance",
    )

    if not math.isclose(
        actual_current,
        setpoint,
        rel_tol=1e-9,
        abs_tol=1e-15,
    ):
        raise DischargeConfigurationError(
            "Current setpoint readback differs from "
            "the negative discharge setpoint."
        )

    if not math.isclose(
        actual_voltage,
        voltage_compliance,
        rel_tol=1e-9,
        abs_tol=1e-12,
    ):
        raise DischargeConfigurationError(
            "Voltage compliance readback differs from "
            "the configured upper voltage limit."
        )

    if not (
        actual_current < 0
        and abs(actual_current) <= rated_discharge_current
    ):
        raise DischargeConfigurationError(
            "Current readback has incorrect polarity or exceeds "
            "the supplied DUT discharge-current rating."
        )

    if not 0 < actual_voltage <= voltage_compliance:
        raise DischargeConfigurationError(
            "Voltage compliance readback is invalid or exceeds "
            "the supplied DUT maximum voltage."
        )

    actual_four_wire = checked(selected.get_four_wire)

    if actual_four_wire is not four_wire:
        raise DischargeConfigurationError(
            "Sense-wiring configuration readback differs."
        )

    off_mode = checked(selected.get_output_off_mode)

    if off_mode != "HIZ":
        raise DischargeConfigurationError(
            "Selected output-off mode did not read back as HIZ."
        )

    for channel in (smu.chan1, smu.chan2):
        automatic = checked(channel.get_auto_output_on)

        if automatic is not False:
            raise DischargeConfigurationError(
                f"Channel {channel.channel}: automatic "
                "output-on did not read back as OFF."
            )

    require_outputs_off()

    return {
        "channel": number,
        "source_function": source_mode,
        "source_shape": "DC",
        "source_operation": "FIX",
        "current_setpoint_a": actual_current,
        "voltage_compliance_v": actual_voltage,
        "software_discharge_cutoff_v": cutoff,
        "four_wire": actual_four_wire,
        "selected_output_off_mode": off_mode,
        "both_outputs_reported_off": True,
        "both_auto_output_on_reported_off": True,
        "operating_envelope_verified": False,
        "hardware_execution_authorized": False,
    }


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
