"""Source and measurement range control for the Keysight B2902B.

Manual references:
    Source range: sections 4-117 to 4-118
    Measurement range: sections 4-99 and 4-103
"""

import math
from numbers import Real
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from drivers.keysight_b2902b import KeysightB2902B


class SMURanges:
    """Range configuration using an existing SMU connection.

    All range changes require the selected channel's output OFF.
    Instrument errors are checked after each command/query.

    Requested ranges must be positive finite numbers. Instrument-
    specific valid selections and setting conflicts are checked by
    the instrument and reported through its error queue.

    This is not full source voltage/current/power-envelope validation.
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
    def _quantity(quantity: str) -> str:
        if not isinstance(quantity, str):
            raise ValueError("quantity must be VOLT or CURR.")

        quantity = quantity.strip().upper()

        if quantity not in ("VOLT", "CURR"):
            raise ValueError("quantity must be VOLT or CURR.")

        return quantity

    @staticmethod
    def _range_value(value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("range value must be a real number.")

        try:
            value = float(value)
        except (ValueError, OverflowError) as error:
            raise ValueError(
                "range value must be finite and greater than zero."
            ) from error

        if not math.isfinite(value) or value <= 0:
            raise ValueError(
                "range value must be finite and greater than zero."
            )

        return value

    def _require_output_off(self) -> None:
        channel = (
            self._smu.chan1
            if self.channel == 1
            else self._smu.chan2
        )

        state = channel.get_output_state()
        self._smu.check_errors()

        if state:
            raise RuntimeError(
                f"Channel {self.channel}: output must be OFF "
                "before changing ranges."
            )

    def _write(self, command: str) -> None:
        self._smu._write(command)
        self._smu.check_errors()

    def _query(self, command: str) -> str:
        response = self._smu._query(command)
        self._smu.check_errors()
        return response

    def _set_range(
        self,
        subsystem: str,
        quantity: str,
        value: float,
    ) -> None:
        quantity = self._quantity(quantity)
        value = self._range_value(value)
        self._require_output_off()

        # Manual range selection also disables autoranging.
        self._write(
            f":{subsystem}{self.channel}:{quantity}:RANG {value:.12g}"
        )

    def _get_range(self, subsystem: str, quantity: str) -> float:
        quantity = self._quantity(quantity)
        command = f":{subsystem}{self.channel}:{quantity}:RANG?"
        response = self._query(command)

        try:
            value = float(response)
        except ValueError as error:
            raise RuntimeError(
                f"Invalid range response to {command}: {response!r}"
            ) from error

        if (
            not math.isfinite(value)
            or value <= 0
            or value >= 9e37
        ):
            raise RuntimeError(
                f"Invalid range response to {command}: {response!r}"
            )

        return value

    def _set_autorange(
        self,
        subsystem: str,
        quantity: str,
        enabled: bool,
    ) -> None:
        quantity = self._quantity(quantity)

        if not isinstance(enabled, bool):
            raise ValueError("enabled must be True or False.")

        self._require_output_off()
        state = "ON" if enabled else "OFF"

        self._write(
            f":{subsystem}{self.channel}:{quantity}:RANG:AUTO {state}"
        )

    def _get_autorange(
        self,
        subsystem: str,
        quantity: str,
    ) -> bool:
        quantity = self._quantity(quantity)
        command = (
            f":{subsystem}{self.channel}:{quantity}:RANG:AUTO?"
        )
        response = self._query(command)

        if response == "1":
            return True
        if response == "0":
            return False

        raise RuntimeError(
            f"Invalid autorange response to {command}: {response!r}"
        )

    def set_source_range(self, quantity: str, value: float) -> None:
        """Request a manual source range in volts or amperes."""
        self._set_range("SOUR", quantity, value)

    def get_source_range(self, quantity: str) -> float:
        return self._get_range("SOUR", quantity)

    def set_source_autorange(
        self,
        quantity: str,
        enabled: bool,
    ) -> None:
        self._set_autorange("SOUR", quantity, enabled)

    def get_source_autorange(self, quantity: str) -> bool:
        return self._get_autorange("SOUR", quantity)

    def set_measurement_range(
        self,
        quantity: str,
        value: float,
    ) -> None:
        """Request a manual measurement range in volts or amperes."""
        self._set_range("SENS", quantity, value)

    def get_measurement_range(self, quantity: str) -> float:
        return self._get_range("SENS", quantity)

    def set_measurement_autorange(
        self,
        quantity: str,
        enabled: bool,
    ) -> None:
        self._set_autorange("SENS", quantity, enabled)

    def get_measurement_autorange(self, quantity: str) -> bool:
        return self._get_autorange("SENS", quantity)