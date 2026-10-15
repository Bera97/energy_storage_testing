"""Check output-state readback and output-off commands.

No output-on command is sent.
Run with no cell/circuit connected and no ongoing acquisition.
"""

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

        existing_errors = smu.read_errors()

        if existing_errors:
            print("Existing errors:", flush=True)

            for code, message in existing_errors:
                print(f"  {code}: {message}", flush=True)

            print(
                "Stopped before changing output states.",
                flush=True,
            )
            return

        for channel in (smu.chan1, smu.chan2):
            print(
                f"\nChannel {channel.channel}",
                flush=True,
            )

            off_mode = channel.get_output_off_mode()
            smu.check_errors()

            before = channel.get_output_state()
            smu.check_errors()

            print(f"  Off mode: {off_mode}", flush=True)
            print(
                f"  State before: {'ON' if before else 'OFF'}",
                flush=True,
            )

            channel.disable_output()
            smu.check_errors()

            after = channel.get_output_state()
            smu.check_errors()

            print(
                f"  State after:  {'ON' if after else 'OFF'}",
                flush=True,
            )

            if after:
                raise RuntimeError(
                    f"Channel {channel.channel} still reports output ON."
                )

        print(
            "\nOutput-disable diagnostic passed.",
            flush=True,
        )


if __name__ == "__main__":
    main()