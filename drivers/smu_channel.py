"""Channel configuration and spot measurement for the Keysight B2902B."""

import math
from numbers import Real
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from drivers.keysight_b2902b import KeysightB2902B


class SMUChannel:
    """Configuration and measurement interface for one SMU channel.

    Basic setters do not yet validate the full operating envelope.
    Basic setter error checking remains explicit.

    New multi-command configuration and measurement methods check
    the instrument error queue.

    Closing the driver does not automatically disable outputs.
    Do not use this incremental driver for cell experiments yet.
    """

    def __init__(self, smu: "KeysightB2902B", channel: int):
        if type(channel) is not int or channel not in (1, 2):
            raise ValueError("channel must be integer 1 or 2.")

        self._smu = smu
        self._channel = channel

    @property
    def channel(self) -> int:
        return self._channel

    @staticmethod
    def _finite_number(value: float, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError(f"{name} must be a real number.")

        try:
            number = float(value)
        except (OverflowError, ValueError) as error:
            raise ValueError(
                f"{name} must be a finite real number."
            ) from error

        if not math.isfinite(number):
            raise ValueError(f"{name} must be finite.")

        return number

    @staticmethod
    def _positive_number(value: float, name: str) -> float:
        number = SMUChannel._finite_number(value, name)

        if number <= 0:
            raise ValueError(
                f"{name} must be finite and greater than zero."
            )

        return number

    @staticmethod
    def _parse_number(response: str, command: str) -> float:
        try:
            value = float(response)
        except ValueError as error:
            raise RuntimeError(
                f"Invalid response to {command}: {response!r}"
            ) from error

        # Keysight uses approximately 9.9E37 for invalid/infinite data.
        # No supported B2902B physical reading approaches this magnitude.
        if not math.isfinite(value) or abs(value) >= 9e37:
            raise RuntimeError(
                f"Invalid numeric response to {command}: "
                f"{response!r}"
            )

        return value

    def _query_number(self, command: str) -> float:
        response = self._smu._query(command)
        return self._parse_number(response, command)

    def _query_boolean(self, command: str) -> bool:
        response = self._smu._query(command)

        if response == "1":
            return True
        if response == "0":
            return False

        raise RuntimeError(
            f"Unexpected response to {command}: {response!r}"
        )

    def _query_compliance(self, quantity: str) -> float:
        command = f":SENS{self.channel}:{quantity}:PROT?"
        value = self._query_number(command)

        if value < 0:
            raise RuntimeError(
                f"Invalid compliance response to {command}: {value!r}"
            )

        return value

    def _checked_write(self, command: str) -> None:
        self._smu._write(command)
        self._smu.check_errors()

    def _checked_query(self, command: str) -> str:
        response = self._smu._query(command)
        self._smu.check_errors()
        return response

    def _require_output_off(self) -> None:
        state = self.get_output_state()
        self._smu.check_errors()

        if state:
            raise RuntimeError(
                f"Channel {self.channel}: output must be OFF "
                "before this configuration operation."
            )

    def set_mode_voltage_source(self) -> None:
        """Select voltage sourcing without enabling output."""
        self._smu._write(f":SOUR{self.channel}:FUNC:MODE VOLT")

    def set_mode_current_source(self) -> None:
        """Select current sourcing without enabling output."""
        self._smu._write(f":SOUR{self.channel}:FUNC:MODE CURR")

    def get_source_mode(self) -> str:
        command = f":SOUR{self.channel}:FUNC:MODE?"
        response = self._smu._query(command)

        if response not in ("VOLT", "CURR"):
            raise RuntimeError(
                f"Unexpected response to {command}: {response!r}"
            )

        return response

    def set_current_limit(self, current_a: float) -> None:
        """Set current-compliance magnitude in amperes."""
        value = self._positive_number(current_a, "current_a")
        self._smu._write(
            f":SENS{self.channel}:CURR:PROT {value:.12g}"
        )

    def get_current_limit(self) -> float:
        return self._query_compliance("CURR")

    def set_voltage_limit(self, voltage_v: float) -> None:
        """Set voltage compliance, not a lower discharge cutoff."""
        value = self._positive_number(voltage_v, "voltage_v")
        self._smu._write(
            f":SENS{self.channel}:VOLT:PROT {value:.12g}"
        )

    def get_voltage_limit(self) -> float:
        return self._query_compliance("VOLT")

    def set_voltage(self, voltage_v: float) -> None:
        """Set immediate voltage level; can change an active output."""
        value = self._finite_number(voltage_v, "voltage_v")
        self._smu._write(
            f":SOUR{self.channel}:VOLT {value:.12g}"
        )

    def get_voltage_setpoint(self) -> float:
        return self._query_number(f":SOUR{self.channel}:VOLT?")

    def set_current(self, current_a: float) -> None:
        """Set immediate current level; can change an active output."""
        value = self._finite_number(current_a, "current_a")
        self._smu._write(
            f":SOUR{self.channel}:CURR {value:.12g}"
        )

    def get_current_setpoint(self) -> float:
        return self._query_number(f":SOUR{self.channel}:CURR?")

    def get_output_state(self) -> bool:
        """Read output state; not an electrical isolation test."""
        return self._query_boolean(f":OUTP{self.channel}?")

    def disable_output(self) -> None:
        """Request OFF; does not abort triggers or verify state."""
        self._smu._write(f":OUTP{self.channel} OFF")

    def set_auto_output_on(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be True or False.")

        state = "ON" if enabled else "OFF"
        self._smu._write(
            f":OUTP{self.channel}:ON:AUTO {state}"
        )

    def get_auto_output_on(self) -> bool:
        return self._query_boolean(
            f":OUTP{self.channel}:ON:AUTO?"
        )

    def set_output_off_mode(self, mode: str) -> None:
        if not isinstance(mode, str):
            raise ValueError("mode must be NORM, HIZ, or ZERO.")

        mode = mode.strip().upper()

        if mode not in ("NORM", "HIZ", "ZERO"):
            raise ValueError("mode must be NORM, HIZ, or ZERO.")

        self._smu._write(
            f":OUTP{self.channel}:OFF:MODE {mode}"
        )

    def get_output_off_mode(self) -> str:
        command = f":OUTP{self.channel}:OFF:MODE?"
        response = self._smu._query(command)

        if response not in ("NORM", "HIZ", "ZERO"):
            raise RuntimeError(
                f"Unexpected response to {command}: {response!r}"
            )

        return response

    def configure_fixed_dc(self, source_function: str) -> None:
        """Select DC shape, fixed operation, and source function.

        Requires output OFF and trigger activity already stopped.
        Does not set source level, compliance, or source range.
        Does not enable output.

        Error checks occur after every write. A failure can leave
        a partially changed configuration; this is not transactional.
        """
        if not isinstance(source_function, str):
            raise ValueError("source_function must be VOLT or CURR.")

        source_function = source_function.strip().upper()

        if source_function not in ("VOLT", "CURR"):
            raise ValueError("source_function must be VOLT or CURR.")

        self._require_output_off()

        self._checked_write(
            f":SOUR{self.channel}:FUNC:SHAP DC"
        )
        self._checked_write(
            f":SOUR{self.channel}:{source_function}:MODE FIX"
        )
        self._checked_write(
            f":SOUR{self.channel}:FUNC:MODE {source_function}"
        )

    def set_four_wire(self, enabled: bool) -> None:
        """Configure remote sensing; requires output OFF.

        Enable only when the physical sense wiring is correct.
        """
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be True or False.")

        self._require_output_off()

        state = "ON" if enabled else "OFF"
        self._checked_write(
            f":SENS{self.channel}:REM {state}"
        )

    def get_four_wire(self) -> bool:
        return self._query_boolean(f":SENS{self.channel}:REM?")

    def set_nplc(self, nplc: float) -> None:
        """Set channel integration time in power-line cycles.

        Queries supported bounds. Disables automatic NPLC.
        Requires output OFF.
        """
        value = self._positive_number(nplc, "nplc")
        self._require_output_off()

        base = f":SENS{self.channel}:VOLT:NPLC"

        minimum = self._parse_number(
            self._checked_query(f"{base}? MIN"),
            f"{base}? MIN",
        )
        maximum = self._parse_number(
            self._checked_query(f"{base}? MAX"),
            f"{base}? MAX",
        )

        if minimum <= 0 or maximum < minimum:
            raise RuntimeError(
                f"Unexpected NPLC bounds: {minimum}, {maximum}."
            )

        if not minimum <= value <= maximum:
            raise ValueError(
                f"nplc must be between {minimum} and {maximum}."
            )

        self._checked_write(f"{base}:AUTO OFF")
        self._checked_write(f"{base} {value:.12g}")

    def get_nplc(self) -> float:
        return self._query_number(
            f":SENS{self.channel}:VOLT:NPLC?"
        )

    def configure_spot_measurement(self, nplc: float = 1.0) -> None:
        """Enable voltage/current sensing with ASCII responses.

        Requires output OFF. Disables resistance sensing and enables
        voltage/current sensing. Leaves measurement ranges unchanged.

        FORM:DATA is instrument-wide, affecting both channels.
        """
        self.set_nplc(nplc)

        self._checked_write(
            f':SENS{self.channel}:FUNC:OFF "RES"'
        )
        self._checked_write(
            f':SENS{self.channel}:FUNC:ON "VOLT","CURR"'
        )
        self._checked_write(":FORM:DATA ASC")

    def _measure_scalar(self, quantity: str) -> float:
        """Perform one spot acquisition with explicit-output policy."""
        self._smu.check_errors()

        state = self.get_output_state()
        self._smu.check_errors()

        if not state:
            raise RuntimeError(
                f"Channel {self.channel}: enable output explicitly "
                "before requesting a spot measurement."
            )

        automatic = self.get_auto_output_on()
        self._smu.check_errors()

        if automatic:
            raise RuntimeError(
                "Automatic output-on must be disabled "
                "for these spot-measurement methods."
            )

        data_format = self._checked_query(":FORM:DATA?")

        if data_format != "ASC":
            raise RuntimeError(
                "Spot measurement requires ASCII format. "
                "Run configure_spot_measurement() with output OFF."
            )

        command = f":MEAS:{quantity}? (@{self.channel})"
        response = self._checked_query(command)

        return self._parse_number(response, command)

    def measure_voltage(self) -> float:
        """Acquire a voltage spot reading in volts.

        Requires output already ON; does not measure open-circuit
        voltage with the output relay open.
        """
        return self._measure_scalar("VOLT")

    def measure_current(self) -> float:
        """Acquire a current spot reading in amperes.

        This is a separate acquisition from measure_voltage().
        """
        return self._measure_scalar("CURR")