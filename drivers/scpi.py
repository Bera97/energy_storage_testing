"""SCPI response parsing helpers."""

import re


def parse_scpi_bool(response: str, command: str = "") -> bool:
    """Parse an NR1 Boolean response: 0, +0, 1, or +1.

    Signed integer representations are accepted.
    Decimal/exponent notation and values other than 0/1 are rejected.
    """
    text = response.strip()

    if not re.fullmatch(r"[+-]?[0-9]+", text):
        raise RuntimeError(
            f"Invalid Boolean response to {command!r}: {response!r}"
        )

    value = int(text)

    if value not in (0, 1):
        raise RuntimeError(
            f"Invalid Boolean response to {command!r}: {response!r}"
        )

    return bool(value)