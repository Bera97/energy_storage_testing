"""Validate explicit output-off setup on the B2902B.

Run with no circuit connected and both outputs already OFF.
Never sends output ON, INIT, READ, or MEAS commands.

Leaves both channels configured with:
    Automatic output-on: OFF
    Output-off mode: HIZ
    Output state: OFF
"""

import logging

from drivers.keysight_b2902b import KeysightB2902B


logger = logging.getLogger(__name__)

VISA_ADDRESS = "USB0::0x2A8D::0x9201::MY61391173::0::INSTR"


def checked_write(smu, command):
    print(f"Write: {command}", flush=True)
    smu._write(command)
    smu.check_errors()


def checked_query(smu, command):
    response = smu._query(command)
    smu.check_errors()

    print(f"Query: {command} -> {response!r}", flush=True)
    return response


def require_response(smu, command, expected):
    response = checked_query(smu, command)

    if response != expected:
        raise RuntimeError(
            f"{command}: expected {expected!r}, "
            f"received {response!r}."
        )


def cleanup_outputs(smu):
    """Attempt output-off and verification for each channel."""
    failures = []

    for channel in (smu.chan1, smu.chan2):
        try:
            channel.disable_output()
            smu.check_errors()

            state = channel.get_output_state()
            smu.check_errors()

            if state:
                raise RuntimeError("Output still reports ON.")

            print(
                f"Cleanup: channel {channel.channel} reports OFF.",
                flush=True,
            )

        except Exception as error:
            failures.append((channel.channel, str(error)))
            logger.exception(
                "Output-off cleanup failed for channel %s.",
                channel.channel,
            )

    return failures


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    with KeysightB2902B(VISA_ADDRESS) as smu:
        print(f"Instrument: {smu.identity}", flush=True)

        existing_errors = smu.read_errors()

        if existing_errors:
            print("Existing instrument errors:", flush=True)

            for code, message in existing_errors:
                print(f"  {code}: {message}", flush=True)

            print(
                "Stopped before changing configuration.",
                flush=True,
            )
            return

        for channel in (smu.chan1, smu.chan2):
            state = channel.get_output_state()
            smu.check_errors()

            if state:
                raise RuntimeError(
                    f"Channel {channel.channel} reports ON. "
                    "This diagnostic requires both outputs "
                    "already OFF."
                )

        print("\nBoth outputs report OFF.", flush=True)

        try:
            checked_write(smu, ":ABOR:ALL (@1,2)")

            for channel_number in (1, 2):
                print(
                    f"\n--- Channel {channel_number} ---",
                    flush=True,
                )

                checked_write(
                    smu,
                    f":OUTP{channel_number}:ON:AUTO OFF",
                )
                require_response(
                    smu,
                    f":OUTP{channel_number}:ON:AUTO?",
                    "0",
                )

                checked_write(
                    smu,
                    f":OUTP{channel_number}:OFF:MODE HIZ",
                )
                require_response(
                    smu,
                    f":OUTP{channel_number}:OFF:MODE?",
                    "HIZ",
                )

                checked_write(
                    smu,
                    f":OUTP{channel_number} OFF",
                )
                require_response(
                    smu,
                    f":OUTP{channel_number}?",
                    "0",
                )

        finally:
            print("\nAttempting output-off cleanup...", flush=True)
            failures = cleanup_outputs(smu)

        if failures:
            raise RuntimeError(
                f"Output-off cleanup could not be verified: "
                f"{failures!r}"
            )

        print(
            "\nOutput-off setup diagnostic passed.",
            flush=True,
        )
        print(
            "Both channels: AUTO-ON OFF, off mode HIZ, output OFF.",
            flush=True,
        )


if __name__ == "__main__":
    main()