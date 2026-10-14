import pyvisa

VISA_ADDRESS = "USB0::0x2A8D::0x9201::MY61391173::0::INSTR"


def main():
    rm = pyvisa.ResourceManager()

    try:
        print(f"VISA backend: {rm.visalib}")
        print(f"Connecting to: {VISA_ADDRESS}")

        with rm.open_resource(VISA_ADDRESS) as instrument:
            instrument.timeout = 5000
            instrument.write_termination = "\n"
            instrument.read_termination = "\n"

            identity = instrument.query("*IDN?").strip()

            print(f"Instrument identity: {identity}")
            print("Communication test successful.")

    finally:
        rm.close()


if __name__ == "__main__":
    main()