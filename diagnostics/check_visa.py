import os
import sys
from pathlib import Path

import pyvisa


def main():
    visa_directory = Path(
        r"C:\Program Files\IVI Foundation\VISA\Win64\ktvisa\ktbin"
    )
    keysight_bin = Path(
        r"C:\Program Files\Keysight\IO Libraries Suite\bin"
    )
    visa_library = Path(r"C:\Windows\System32\visa32.dll")

    resource_address = (
        "USB0::0x2A8D::0x9201::MY61391173::0::INSTR"
    )

    dll_handles = []
    rm = None
    instrument = None

    print(f"Python interpreter: {sys.executable}", flush=True)
    print(f"PyVISA version: {pyvisa.__version__}", flush=True)
    print(f"Requested VISA library: {visa_library}", flush=True)

    try:
        if not visa_library.is_file():
            raise FileNotFoundError(
                f"VISA library does not exist: {visa_library}"
            )

        for directory in (visa_directory, keysight_bin):
            if directory.is_dir():
                dll_handles.append(
                    os.add_dll_directory(str(directory))
                )
                print(
                    f"Added DLL directory: {directory}",
                    flush=True,
                )
            else:
                print(
                    f"DLL directory not present: {directory}",
                    flush=True,
                )

        print("Opening VISA resource manager...", flush=True)
        rm = pyvisa.ResourceManager(str(visa_library))
        print(f"Loaded VISA backend: {rm.visalib}", flush=True)

        print("Listing VISA resources...", flush=True)
        resources = rm.list_resources()
        print(f"Detected VISA resources: {resources}", flush=True)

        print("A: Opening instrument...", flush=True)
        instrument = rm.open_resource(resource_address)
        print("B: Instrument opened.", flush=True)

        print("C: Setting I/O timeout...", flush=True)
        instrument.timeout = 5000
        print("D: Timeout configured.", flush=True)

        print("E: Setting write termination...", flush=True)
        instrument.write_termination = "\n"
        print("F: Write termination configured.", flush=True)

        print("G: Sending *IDN?...", flush=True)
        instrument.write("*IDN?")
        print("H: Query sent; reading response...", flush=True)

        identity = instrument.read().strip()
        print(f"I: Instrument identity: {identity}", flush=True)

    except KeyboardInterrupt:
        print("\nInterrupted by user.", flush=True)
        raise

    except Exception as error:
        print(
            f"\nERROR: {type(error).__name__}: {error}",
            flush=True,
        )
        raise

    finally:
        try:
            if instrument is not None:
                print("J: Closing instrument...", flush=True)
                try:
                    instrument.close()
                    print("K: Instrument closed.", flush=True)
                except Exception as error:
                    print(
                        f"Instrument cleanup failed: {error}",
                        flush=True,
                    )
        finally:
            try:
                if rm is not None:
                    print(
                        "L: Closing VISA resource manager...",
                        flush=True,
                    )
                    try:
                        rm.close()
                        print(
                            "M: VISA resource manager closed.",
                            flush=True,
                        )
                    except Exception as error:
                        print(
                            f"Resource-manager cleanup failed: {error}",
                            flush=True,
                        )
            finally:
                for handle in reversed(dll_handles):
                    handle.close()

                print("DLL directory handles closed.", flush=True)


if __name__ == "__main__":
    main()