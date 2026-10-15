"""Query-only configuration diagnostic for the actual B2902B."""

import logging

from drivers.keysight_b2902b import KeysightB2902B


VISA_ADDRESS = "USB0::0x2A8D::0x9201::MY61391173::0::INSTR"


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    with KeysightB2902B(VISA_ADDRESS) as smu:
        print(f"Instrument: {smu.identity}", flush=True)

        print("\nReading existing error queue...", flush=True)
        existing_errors = smu.read_errors()

        if existing_errors:
            print("Existing instrument errors:", flush=True)

            for code, message in existing_errors:
                print(f"  {code}: {message}", flush=True)

            print(
                "Stopped after recording existing errors. "
                "No configuration readback performed.",
                flush=True,
            )
            return

        print("Existing error queue is empty.", flush=True)

        for channel in (smu.chan1, smu.chan2):
            print(
                f"\nChannel {channel.channel}",
                flush=True,
            )

            mode = channel.get_source_mode()
            smu.check_errors()

            current_limit = channel.get_current_limit()
            smu.check_errors()

            voltage_limit = channel.get_voltage_limit()
            smu.check_errors()

            print(f"  Source mode:        {mode}", flush=True)
            print(
                f"  Current compliance: {current_limit:.12g} A",
                flush=True,
            )
            print(
                f"  Voltage compliance: {voltage_limit:.12g} V",
                flush=True,
            )

        print(
            "\nConfiguration-readback diagnostic passed.",
            flush=True,
        )


if __name__ == "__main__":
    main()