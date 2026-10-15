"""Inspect raw configuration responses without changing SMU settings."""

import logging

from drivers.keysight_b2902b import KeysightB2902B


VISA_ADDRESS = "USB0::0x2A8D::0x9201::MY61391173::0::INSTR"


def print_errors(smu: KeysightB2902B) -> bool:
    """Print consumed error entries; return True if errors were present."""
    errors = smu.read_errors()

    if not errors:
        print("  Error queue: empty", flush=True)
        return False

    for code, message in errors:
        print(
            f"  Instrument error: {code}, {message}",
            flush=True,
        )

    return True


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    with KeysightB2902B(VISA_ADDRESS) as smu:
        print(f"Instrument: {smu.identity}", flush=True)

        print("\nExisting error queue:", flush=True)
        if print_errors(smu):
            print(
                "\nExisting errors were recorded and consumed. "
                "Stopped before issuing configuration queries.",
                flush=True,
            )
            return

        for channel_number in (1, 2):
            print(
                f"\n--- Channel {channel_number} ---",
                flush=True,
            )

            commands = (
                f":SOUR{channel_number}:FUNC:MODE?",
                f":SENS{channel_number}:CURR:PROT?",
                f":SENS{channel_number}:VOLT:PROT?",
            )

            for command in commands:
                print(f"\nQuery: {command}", flush=True)

                # Deliberately bypass channel-level response validation.
                response = smu._query(command)

                print(f"  Raw response: {response!r}", flush=True)
                had_errors = print_errors(smu)

                if had_errors:
                    print(
                        "\nStopped because the instrument reported "
                        "an error after this query.",
                        flush=True,
                    )
                    return

        print(
            "\nRaw configuration diagnostic completed.",
            flush=True,
        )


if __name__ == "__main__":
    main()