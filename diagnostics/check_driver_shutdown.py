"""Verify the driver's explicit shutdown on the actual B2902B.

Run with:
    No cell or external circuit connected.
    Both outputs already OFF.
    No other program controlling the instrument.

Never sends output ON, INIT, READ, or MEAS commands.
"""

import logging

from drivers.keysight_b2902b import KeysightB2902B


VISA_ADDRESS = "USB0::0x2A8D::0x9201::MY61391173::0::INSTR"


def print_channel_state(smu, channel):
    """Read and print output-related configuration."""
    output_on = channel.get_output_state()
    smu.check_errors()

    auto_on = channel.get_auto_output_on()
    smu.check_errors()

    off_mode = channel.get_output_off_mode()
    smu.check_errors()

    print(f"\nChannel {channel.channel}", flush=True)
    print(
        f"  Output:             {'ON' if output_on else 'OFF'}",
        flush=True,
    )
    print(
        f"  Automatic output-on: {'ON' if auto_on else 'OFF'}",
        flush=True,
    )
    print(f"  Output-off mode:    {off_mode}", flush=True)

    return output_on, auto_on, off_mode


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    with KeysightB2902B(VISA_ADDRESS) as smu:
        print(f"Instrument: {smu.identity}", flush=True)

        existing_errors = smu.read_errors()

        if existing_errors:
            print("\nExisting instrument errors:", flush=True)

            for code, message in existing_errors:
                print(f"  {code}: {message}", flush=True)

            print(
                "Stopped before issuing shutdown commands.",
                flush=True,
            )
            return

        print("\nChecking initial output states...", flush=True)

        for channel in (smu.chan1, smu.chan2):
            output_on = channel.get_output_state()
            smu.check_errors()

            if output_on:
                raise RuntimeError(
                    f"Channel {channel.channel} reports output ON. "
                    "This diagnostic requires both outputs already OFF."
                )

        print("Both outputs report OFF.", flush=True)

        print("\nCalling driver shutdown()...", flush=True)
        smu.shutdown()
        print("shutdown() completed without reported failures.", flush=True)

        print("\nVerifying final configuration...", flush=True)

        for channel in (smu.chan1, smu.chan2):
            output_on, auto_on, _ = print_channel_state(smu, channel)

            if output_on or auto_on:
                raise RuntimeError(
                    f"Channel {channel.channel}: "
                    "output or automatic output-on still reports ON."
                )

        print(
            f"\nVISA session remains open: {smu.is_connected}",
            flush=True,
        )
        print("\nCalling shutdown() a second time...", flush=True)
        smu.shutdown()

        print(
            "Repeated shutdown completed without reported failures.",
            flush=True,
        )

    print("\nVISA session closed.", flush=True)
    print("Driver shutdown diagnostic passed.", flush=True)


if __name__ == "__main__":
    main()