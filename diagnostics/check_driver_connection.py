"""Verify the driver's connection and context-manager cleanup."""

import logging

from drivers.keysight_b2902b import KeysightB2902B


VISA_ADDRESS = "USB0::0x2A8D::0x9201::MY61391173::0::INSTR"


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    smu = KeysightB2902B(
        resource_address=VISA_ADDRESS,
        timeout_ms=5000,
    )

    print(f"Connected before opening: {smu.is_connected}", flush=True)

    with smu:
        print(f"Instrument identity: {smu.identity}", flush=True)
        print(f"Connected inside context: {smu.is_connected}", flush=True)

    print(f"Connected after closing: {smu.is_connected}", flush=True)
    print("Driver connection check completed.", flush=True)


if __name__ == "__main__":
    main()