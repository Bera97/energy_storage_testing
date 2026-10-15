"""Keysight B2902B connection, error handling, and shutdown."""

import csv
import logging

import pyvisa

from drivers.smu_channel import SMUChannel


logger = logging.getLogger(__name__)


class InstrumentError(RuntimeError):
    """One or more errors were reported by the instrument."""

    def __init__(self, errors: list[tuple[int, str]]):
        self.errors = tuple(errors)
        details = "; ".join(
            f"{code}: {message}"
            for code, message in self.errors
        )
        super().__init__(f"Instrument reported errors: {details}")


class ErrorQueueLimitError(RuntimeError):
    """Queue reading stopped before an empty queue was observed."""

    def __init__(self, errors: list[tuple[int, str]]):
        self.errors = tuple(errors)
        super().__init__(
            "Error queue did not report empty within the query limit. "
            f"Consumed entries: {self.errors!r}. "
            "Additional entries may remain in the instrument."
        )


class ShutdownError(RuntimeError):
    """Shutdown could not be completed and verified without failures."""

    def __init__(self, failures: list[tuple[str, Exception]]):
        self.failures = tuple(failures)
        details = "; ".join(
            f"{operation}: {type(error).__name__}: {error}"
            for operation, error in self.failures
        )
        super().__init__(f"Shutdown encountered failures: {details}")


class KeysightB2902B:
    """Manage a VISA connection to a Keysight B2902B.

    connect(): opens the session and queries identification.
    shutdown(): explicitly attempts instrument shutdown.
    close(): releases VISA resources without changing outputs.

    Context-manager exit currently calls close(), not shutdown().
    Error checking is explicit.
    """

    def __init__(
        self,
        resource_address: str,
        timeout_ms: int = 5000,
        visa_library: str | None = None,
    ):
        if (
            not isinstance(resource_address, str)
            or not resource_address.strip()
        ):
            raise ValueError(
                "resource_address must be a non-empty string."
            )

        if (
            isinstance(timeout_ms, bool)
            or not isinstance(timeout_ms, int)
            or timeout_ms <= 0
        ):
            raise ValueError(
                "timeout_ms must be a positive integer."
            )

        self.resource_address = resource_address.strip()
        self.timeout_ms = timeout_ms
        self.visa_library = visa_library

        self._resource_manager = None
        self._instrument = None
        self._identity = None

        self.chan1 = SMUChannel(self, 1)
        self.chan2 = SMUChannel(self, 2)

    @property
    def is_connected(self) -> bool:
        """Whether a session is held; not a physical-connection test."""
        return self._instrument is not None

    @property
    def identity(self) -> str:
        if self._identity is None:
            raise RuntimeError("Instrument is not connected.")
        return self._identity

    def connect(self) -> "KeysightB2902B":
        if self.is_connected:
            return self

        try:
            if self.visa_library is None:
                self._resource_manager = pyvisa.ResourceManager()
            else:
                self._resource_manager = pyvisa.ResourceManager(
                    self.visa_library
                )

            self._instrument = (
                self._resource_manager.open_resource(
                    self.resource_address
                )
            )

            self._instrument.timeout = self.timeout_ms
            self._instrument.write_termination = "\n"
            self._instrument.read_termination = "\n"

            identity = self._query("*IDN?")
            fields = [
                field.strip()
                for field in identity.split(",")
            ]

            if len(fields) != 4:
                raise RuntimeError(
                    f"Unexpected identification response: {identity!r}"
                )

            model = fields[1].upper()

            if model != "B2902B":
                raise RuntimeError(
                    "Expected B2902B, but connected instrument "
                    f"reports {model!r}. Full response: {identity}"
                )

            self._identity = identity
            return self

        except BaseException:
            self.close()
            raise

    def _write(self, command: str) -> None:
        if self._instrument is None:
            raise RuntimeError("Instrument is not connected.")
        self._instrument.write(command)

    def _query(self, command: str) -> str:
        if self._instrument is None:
            raise RuntimeError("Instrument is not connected.")
        return self._instrument.query(command).strip()

    def read_error(self) -> tuple[int, str]:
        """Read and consume the oldest error, or return zero code."""
        response = self._query(":SYST:ERR?")

        try:
            fields = next(
                csv.reader(
                    [response],
                    skipinitialspace=True,
                    strict=True,
                )
            )

            if len(fields) != 2:
                raise ValueError("Expected code and message.")

            code = int(fields[0].strip())
            message = fields[1].strip()

            if not message:
                raise ValueError("Empty error message.")

        except (csv.Error, ValueError, StopIteration) as error:
            raise RuntimeError(
                f"Malformed instrument-error response: {response!r}"
            ) from error

        return code, message

    def read_errors(
        self,
        max_queries: int = 100,
    ) -> list[tuple[int, str]]:
        """Consume errors until zero code, subject to a query limit."""
        if (
            isinstance(max_queries, bool)
            or not isinstance(max_queries, int)
            or max_queries <= 0
        ):
            raise ValueError(
                "max_queries must be a positive integer."
            )

        errors = []

        for _ in range(max_queries):
            code, message = self.read_error()

            if code == 0:
                return errors

            errors.append((code, message))

        raise ErrorQueueLimitError(errors)

    def check_errors(self, max_queries: int = 100) -> None:
        """Consume errors and raise if any exist.

        Errors may predate the last driver operation.
        This method does not disable outputs.
        """
        errors = self.read_errors(max_queries=max_queries)

        if errors:
            raise InstrumentError(errors)

    def abort(self) -> None:
        """Abort source and acquisition trigger activity on both channels.

        Does not itself turn outputs off or check the error queue.
        SCPI reference: section 4-175.
        """
        self._write(":ABOR:ALL (@1,2)")

    def shutdown(self) -> None:
        """Attempt shutdown of both channels and verify reported state.

        Preserves the configured output-off modes.
        Does not close the VISA connection.

        Independent operations are still attempted after Exceptions.
        Failures are collected and raised as ShutdownError.

        This is not an emergency-stop or isolation guarantee.
        """
        if not self.is_connected:
            raise RuntimeError("Instrument is not connected.")

        failures = []

        def attempt(label, operation, *arguments):
            try:
                return True, operation(*arguments)
            except Exception as error:
                failures.append((label, error))
                return False, None

        attempt("Abort both channels", self.abort)

        # Issue shutdown commands before spending time on queries.
        for channel in (self.chan1, self.chan2):
            number = channel.channel

            attempt(
                f"Channel {number}: disable automatic output-on",
                channel.set_auto_output_on,
                False,
            )
            attempt(
                f"Channel {number}: output OFF",
                channel.disable_output,
            )

        attempt("Read instrument error queue", self.check_errors)

        for channel in (self.chan1, self.chan2):
            number = channel.channel

            success, state = attempt(
                f"Channel {number}: read output state",
                channel.get_output_state,
            )

            if success and state:
                failures.append((
                    f"Channel {number}: verify output OFF",
                    RuntimeError("Instrument still reports output ON."),
                ))

            success, automatic = attempt(
                f"Channel {number}: read automatic output-on",
                channel.get_auto_output_on,
            )

            if success and automatic:
                failures.append((
                    f"Channel {number}: verify automatic output-on OFF",
                    RuntimeError(
                        "Instrument still reports automatic output-on ON."
                    ),
                ))

        attempt(
            "Check errors after shutdown verification",
            self.check_errors,
        )

        if failures:
            raise ShutdownError(failures)

    def close(self) -> None:
        """Release VISA resources without changing output settings."""
        instrument = self._instrument
        resource_manager = self._resource_manager

        self._instrument = None
        self._resource_manager = None
        self._identity = None

        if instrument is not None:
            try:
                instrument.close()
            except Exception:
                logger.exception(
                    "Failed to close the instrument session."
                )

        if resource_manager is not None:
            try:
                resource_manager.close()
            except Exception:
                logger.exception(
                    "Failed to close the VISA resource manager."
                )

    def __enter__(self) -> "KeysightB2902B":
        return self.connect()

    def __exit__(
        self,
        exc_type,
        exc_value,
        traceback,
    ) -> bool:
        self.close()
        return False