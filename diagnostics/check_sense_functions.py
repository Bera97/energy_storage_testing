"""Inspect raw measurement-function replies with outputs OFF.

Never sends output ON, INIT, READ, or MEAS commands.
Changes configuration using the previously tested resistor setup.
"""

import logging

from diagnostics.check_dc_resistor import (
    configure_channel,
    VISA_ADDRESS,
)
from drivers.keysight_b2902b import KeysightB2902B
from drivers.spot_acquisition import SpotAcquisition


def query_and_print(smu, command):
    response = smu._query(command)

    print(f"  {command}", flush=True)
    print(f"    Raw response: {response!r}", flush=True)

    errors = smu.read_errors()

    if errors:
        for code, message in errors:
            print(
                f"    Instrument error: {code}: {message}",
                flush=True,
            )

        raise RuntimeError(
            f"Instrument reported errors after {command}."
        )

    print("    Error queue: empty", flush=True)


def inspect_functions(smu, label):
    print(f"\n--- {label} ---", flush=True)

    commands = (
        ":OUTP1?",
        ":SENS1:FUNC:ON?",
        ':SENS1:FUNC:STAT? "VOLT"',
        ':SENS1:FUNC:STAT? "CURR"',
        ':SENS1:FUNC:STAT? "RES"',
        ":FORM:DATA?",
        ":FORM:ELEM:SENS?",
    )

    for command in commands:
        query_and_print(smu, command)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    with KeysightB2902B(VISA_ADDRESS) as smu:
        print(f"Instrument: {smu.identity}", flush=True)

        existing_errors = smu.read_errors()

        if existing_errors:
            for code, message in existing_errors:
                print(
                    f"Existing error: {code}: {message}",
                    flush=True,
                )

            print(
                "Stopped before changing configuration.",
                flush=True,
            )
            return

        try:
            configure_channel(smu, "VOLT")

            inspect_functions(
                smu,
                "After existing spot-measurement configuration",
            )

            acquisition = SpotAcquisition(smu, channel=1)
            acquisition.configure(nplc=1.0)

            inspect_functions(
                smu,
                "After paired-record configuration",
            )

            print("\nSending channel-1 trigger abort...", flush=True)
            smu._write(":ABOR:ALL (@1)")
            smu.check_errors()

            inspect_functions(
                smu,
                "After channel-1 trigger abort",
            )

        except BaseException as original_error:
            try:
                smu.shutdown()
            except Exception as cleanup_error:
                original_error.add_note(
                    "Shutdown could not be verified: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            raise

        else:
            smu.shutdown()
            print(
                "\nInspection completed; shutdown verified.",
                flush=True,
            )


if __name__ == "__main__":
    main()