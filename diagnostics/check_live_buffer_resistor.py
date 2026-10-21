"""Validate live trace retrieval and early termination on a resistor.

Channel 1: existing 6.93 kOhm resistor.
DMM: across the resistor in DC-voltage mode.
Channel 2: disconnected.
No external supply, battery, or capacitor connected.

Changes instrument configuration; does not restore prior settings.
"""

import logging
import time

from diagnostics.check_dc_resistor import (
    VISA_ADDRESS,
    configure_channel,
)
from acquisition.buffered_acquisition import (
    BufferedAcquisition,
    parse_buffer,
)
from drivers.dc_output import enable_dc_output
from drivers.keysight_b2902b import KeysightB2902B


INTERVAL_S = 0.2
POLL_INTERVAL_S = 0.05


def write_checked(smu, command):
    smu._write(command)
    smu.check_errors()


def query_checked(smu, command):
    response = smu._query(command)
    smu.check_errors()
    return response


def query_integer(smu, command):
    response = query_checked(smu, command)

    try:
        value = int(response)
    except ValueError as error:
        raise RuntimeError(
            f"{command}: invalid integer response {response!r}"
        ) from error

    if value < 0:
        raise RuntimeError(
            f"{command}: unexpected negative value {value}"
        )

    return value


def configure_trace(smu, count):
    for command in (
        ":TRAC1:FEED:CONT NEV",
        ":TRAC1:CLE",
        ":TRAC1:POIN " + str(count),
        ":TRAC1:FEED SENS",
        ":TRAC1:TST:FORM ABS",
        ":TRAC1:FEED:CONT NEXT",
    ):
        write_checked(smu, command)

    actual = query_integer(smu, ":TRAC1:POIN:ACT?")

    if actual != 0:
        raise RuntimeError(
            f"Trace buffer was not empty before start: {actual}"
        )


def retrieve_new_records(smu, readings, requested_count):
    available = query_integer(smu, ":TRAC1:POIN:ACT?")

    if not len(readings) <= available <= requested_count:
        raise RuntimeError(
            f"Unexpected trace count {available}; "
            f"already retrieved {len(readings)}, "
            f"requested {requested_count}."
        )

    new_count = available - len(readings)

    if new_count == 0:
        return []

    response = query_checked(
        smu,
        f":TRAC1:DATA? {len(readings)},{new_count}",
    )
    new_readings = parse_buffer(response, 1, new_count)

    if (
        readings
        and new_readings[0].timestamp_s <= readings[-1].timestamp_s
    ):
        raise RuntimeError(
            "Timestamps did not increase across trace retrievals."
        )

    readings.extend(new_readings)
    return new_readings


def check_records(readings):
    for index, reading in enumerate(readings):
        if reading.current_source:
            raise RuntimeError(
                f"Record {index}: unexpected current-source status."
            )

        if reading.in_compliance:
            raise RuntimeError(
                f"Record {index}: unexpected compliance."
            )

        if not 0.95 <= reading.voltage_v <= 1.05:
            raise RuntimeError(
                f"Record {index}: unexpected voltage "
                f"{reading.voltage_v} V."
            )

        if not 135e-6 <= reading.current_a <= 155e-6:
            raise RuntimeError(
                f"Record {index}: unexpected current "
                f"{reading.current_a} A."
            )


def run_phase(smu, *, early_stop):
    requested_count = 100 if early_stop else 20
    label = "early voltage termination" if early_stop else "normal completion"

    print(f"\n=== {label} ===", flush=True)

    # Both outputs are OFF after this helper returns.
    configure_channel(smu, "VOLT")

    acquisition = BufferedAcquisition(smu, channel=1)
    acquisition.configure(
        count=requested_count,
        interval_s=INTERVAL_S,
        nplc=1.0,
    )
    configure_trace(smu, requested_count)

    input(
        "\nConfirm resistor/DMM wiring and external supply disconnected. "
        "Press Enter to enable channel 1, or Ctrl+C to cancel: "
    )

    readings = []
    live_records = 0
    reason = None
    triggering_reading = None

    try:
        enable_dc_output(
            smu,
            channel=1,
            max_voltage_v=1.2,
            max_current_a=0.001,
        )

        started = time.monotonic()
        deadline = started + requested_count * INTERVAL_S + 5.0

        write_checked(smu, ":INIT:ACQ (@1)")

        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("Live acquisition timed out.")

            # Channel 1 acquisition-idle bit, as used by the
            # existing buffered-acquisition driver.
            operation = query_integer(smu, ":STAT:OPER:COND?")
            idle = bool(operation & (1 << 4))

            questionable = query_integer(smu, ":STAT:QUES:COND?")
            if questionable & (1 << 12):
                raise RuntimeError(
                    "Instrument reports a lost acquisition event."
                )

            new_readings = retrieve_new_records(
                smu, readings, requested_count
            )

            if new_readings:
                if not idle:
                    live_records += len(new_readings)

                # Evaluate termination before console reporting.
                for reading in new_readings:
                    if early_stop and reading.voltage_v >= 0.9:
                        triggering_reading = reading
                        reason = "voltage_at_or_above_0.9_V"
                        break

                if reason is not None:
                    break

                check_records(new_readings)
                print(
                    f"Retrieved {len(readings)}/{requested_count}; "
                    f"last instrument time "
                    f"{readings[-1].timestamp_s:.6g} s; "
                    f"idle before retrieval={idle}",
                    flush=True,
                )

            if idle:
                reason = "completed"
                break

            time.sleep(POLL_INTERVAL_S)

        decision_elapsed = time.monotonic() - started

        # Abort and disable outputs before final retrieval/reporting.
        # shutdown() verifies reported output states.
        smu.shutdown()
        shutdown_elapsed = time.monotonic() - started

        # Freeze trace storage, then retain any records that arrived
        # between the last retrieval and shutdown.
        write_checked(smu, ":TRAC1:FEED:CONT NEV")
        retrieve_new_records(smu, readings, requested_count)

        check_records(readings)

        if early_stop:
            if triggering_reading is None:
                raise RuntimeError(
                    "Acquisition completed without voltage termination."
                )

            if len(readings) >= requested_count:
                raise RuntimeError(
                    "Threshold was detected, but acquisition did not "
                    "terminate before the requested record count."
                )

        elif len(readings) != requested_count:
            raise RuntimeError(
                f"Expected {requested_count} records; "
                f"received {len(readings)}."
            )

        if live_records == 0:
            raise RuntimeError(
                "No records were retrieved after an active-state "
                "observation. Live retrieval is not established."
            )

        print(f"\nStop reason: {reason}", flush=True)
        print(
            f"Records retained: {len(readings)}/{requested_count}",
            flush=True,
        )
        print(
            f"Records retrieved after active-state observations: "
            f"{live_records}",
            flush=True,
        )
        print(
            f"PC time to stop decision: {decision_elapsed:.6f} s",
            flush=True,
        )
        print(
            f"PC time through verified shutdown: "
            f"{shutdown_elapsed:.6f} s",
            flush=True,
        )

        if triggering_reading is not None:
            print(
                "Triggering record: "
                f"t={triggering_reading.timestamp_s:.6g} s, "
                f"V={triggering_reading.voltage_v:.9g} V, "
                f"I={triggering_reading.current_a * 1e6:.9g} uA",
                flush=True,
            )

        print(
            f"Retained instrument times: "
            f"{[r.timestamp_s for r in readings]}",
            flush=True,
        )
        print("Phase passed; both outputs report OFF.", flush=True)

    except BaseException as original_error:
        # Retain successfully parsed records for inspection.
        original_error.partial_readings = tuple(readings)

        try:
            smu.shutdown()
        except Exception as cleanup_error:
            original_error.add_note(
                "Shutdown could not be verified: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )

        raise


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    print(
        "Required: 6.93 kOhm resistor on channel 1; "
        "DMM across it in DC volts; channel 2 disconnected.\n"
        "No external supply, battery, or capacitor connected.",
        flush=True,
    )

    input(
        "Confirm both outputs are OFF. "
        "Press Enter to connect, or Ctrl+C to cancel: "
    )

    with KeysightB2902B(VISA_ADDRESS) as smu:
        print(f"\nInstrument: {smu.identity}", flush=True)

        existing_errors = smu.read_errors()
        if existing_errors:
            raise RuntimeError(
                f"Existing instrument errors: {existing_errors!r}. "
                "Stopped before configuration."
            )

        run_phase(smu, early_stop=False)
        run_phase(smu, early_stop=True)

        print(
            "\nLive-buffer resistor diagnostic passed.",
            flush=True,
        )


if __name__ == "__main__":
    main()