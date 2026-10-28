"""Dual-channel session primitives. No output enabling or acquisition start."""
import argparse
from copy import deepcopy
import json
import math
import threading
from acquisition.spot_acquisition import SpotAcquisition
from experiments.config import load_config
from experiments.dual_preconditioning import build_dual_plan
def _new_smu(resource, *, timeout_ms, visa_library):
    from drivers.keysight_b2902b import KeysightB2902B
    return KeysightB2902B(
        resource,
        timeout_ms=timeout_ms,
        visa_library=visa_library,
    )
class ChannelStopError(RuntimeError):
    def __init__(self, channel, failures):
        self.channel = channel
        self.failures = tuple(failures)
        super().__init__(
            f"Channel {channel} stop failed: " + "; ".join(failures)
        )
class DualInstrumentSession:
    """Single-thread session owner; not a live coordinator backend."""
    def __init__(
        self,
        plan,
        expected_serial,
        *,
        timeout_ms=2000,
        visa_library=None,
        smu_factory=None,
    ):
        if type(timeout_ms) is not int or timeout_ms <= 0:
            raise ValueError("timeout_ms must be a positive integer.")
        if not isinstance(expected_serial, str) or not expected_serial.strip():
            raise ValueError("expected_serial is required.")
        previews = plan["channels"]
        verified = build_dual_plan(
            [
                item["acquisition_plan"]["configuration"]
                for item in previews
            ],
            maximum_records=previews[0]["acquisition_plan"][
                "acquisition_planning_limit"
            ],
        )
        if plan != verified:
            raise ValueError("Plan differs from freshly validated profiles.")
        self.plan = deepcopy(verified)
        self.expected_serial = expected_serial.strip()
        self.timeout_ms = timeout_ms
        self.visa_library = visa_library
        self.factory = _new_smu if smu_factory is None else smu_factory
        self.owner = threading.get_ident()
        self.smu = None
        self.identity = None
        self.stop_evidence = {}
    def _thread(self):
        if threading.get_ident() != self.owner:
            raise RuntimeError(
                "The session must have one controlling thread."
            )
    @property
    def connected(self):
        return self.smu is not None and self.smu.is_connected is True
    def _require_connected(self):
        self._thread()
        if not self.connected:
            raise RuntimeError("Session is not connected.")
    def _channel(self, channel):
        if type(channel) is not int or channel not in (1, 2):
            raise ValueError("channel must be integer 1 or 2.")
        self._require_connected()
        return self.smu.chan1 if channel == 1 else self.smu.chan2
    def connect(self):
        self._thread()
        if self.smu is not None:
            raise RuntimeError("Session instances must not be reconnected.")
        self.smu = self.factory(
            self.plan["resource"],
            timeout_ms=self.timeout_ms,
            visa_library=self.visa_library,
        )
        self.smu.connect()
        self.identity = self.smu.identity
        fields = [part.strip() for part in self.identity.split(",")]
        if (
            len(fields) != 4
            or fields[1].upper() != "B2902B"
            or fields[2] != self.expected_serial
        ):
            raise RuntimeError("Instrument model or serial does not match.")
    def stop_channel(self, channel):
        selected = self._channel(channel)
        failures = []
        self.stop_evidence.pop(channel, None)
        def attempt(label, operation, *args):
            try:
                return True, operation(*args)
            except Exception as error:
                failures.append(
                    f"{label}: {type(error).__name__}: {error}"
                )
                return False, None
        attempt(
            "abort",
            self.smu._write,
            f":ABOR:ALL (@{channel})",
        )
        attempt(
            "automatic output-on OFF",
            selected.set_auto_output_on,
            False,
        )
        attempt("output OFF", selected.disable_output)
        attempt("command errors", self.smu.check_errors)
        success, state = attempt(
            "output state",
            selected.get_output_state,
        )
        if success and state is not False:
            failures.append("Output did not explicitly report OFF.")
        success, automatic = attempt(
            "automatic output-on",
            selected.get_auto_output_on,
        )
        if success and automatic is not False:
            failures.append(
                "Automatic output-on did not explicitly report OFF."
            )
        attempt("verification errors", self.smu.check_errors)
        if failures:
            raise ChannelStopError(channel, failures)
        self.stop_evidence[channel] = {
            "verified": True,
            "evidence_kind": "instrument_readback",
            "output_reported_off": True,
            "automatic_output_on_reported_off": True,
        }
        return True
    def configure_shared(self):
        self._require_connected()
        for channel in (1, 2):
            state = self._channel(channel).get_output_state()
            self.smu.check_errors()
            if state is not False:
                raise RuntimeError(
                    "Both outputs must report OFF before shared setup."
                )
        self.smu._write(":FORM:SREG ASC")
        self.smu.check_errors()
        for preview in self.plan["channels"]:
            channel = preview["channel"]
            nplc = preview["acquisition_plan"]["configuration"][
                "instrument"
            ]["nplc"]
            SpotAcquisition(self.smu, channel).configure(nplc)
            actual = self._channel(channel).get_nplc()
            self.smu.check_errors()
            if (
                isinstance(actual, bool)
                or not math.isfinite(actual)
                or not math.isclose(
                    actual, float(nplc), rel_tol=1e-6, abs_tol=1e-9
                )
            ):
                raise RuntimeError(
                    f"Channel {channel} NPLC readback mismatch."
                )

        from acquisition.dual_trace import configure_shared_trace_clock
        self.trace_clock_evidence = configure_shared_trace_clock(self)
    def shutdown_all(self):
        self._require_connected()
        self.smu.shutdown()
        return True
    def close(self):
        self._thread()
        if self.smu is not None:
            self.smu.close()
def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m smu dual-probe",
        description=(
            "Outputs-OFF session probe. "
            "No output enabling or acquisition."
        ),
    )
    parser.add_argument("--config", action="append", required=True)
    parser.add_argument("--connect", action="store_true")
    parser.add_argument("--expected-serial")
    parser.add_argument("--timeout-ms", type=int, default=2000)
    parser.add_argument("--visa-library")
    args = parser.parse_args(argv)
    if len(args.config) != 2:
        parser.error("Supply --config exactly twice.")
    if args.connect and not args.expected_serial:
        parser.error("--connect requires --expected-serial.")
    session = None
    primary = None
    report = {
        "probe_only": True,
        "will_enable_output": False,
        "concurrent_execution_available": False,
        "state": "dry_run",
        "connected": False,
        "initial_stop_verified": None,
        "final_stop_verified": None,
        "session_close_returned": None,
        "cleanup_errors": [],
    }
    try:
        plan = build_dual_plan([
            load_config(path)["config"]
            for path in args.config
        ])
        report["resource"] = plan["resource"]
        if args.connect:
            session = DualInstrumentSession(
                plan,
                args.expected_serial,
                timeout_ms=args.timeout_ms,
                visa_library=args.visa_library,
            )
            session.connect()
            report["connected"] = True
            report["identity"] = session.identity
            report["initial_stop_verified"] = session.shutdown_all()
            session.configure_shared()

            report["trace_clock"] = deepcopy(session.trace_clock_evidence)
            for channel in (1, 2):
                session.stop_channel(channel)
            report["channel_stop_evidence"] = deepcopy(
                session.stop_evidence
            )
            report["state"] = "outputs_off_verified"
    except BaseException as error:
        primary = error
    finally:
        if session is not None:
            try:
                if session.connected:
                    report["final_stop_verified"] = session.shutdown_all()
            except BaseException as error:
                report["final_stop_verified"] = False
                report["cleanup_errors"].append(str(error))
                if primary is None:
                    primary = error
            try:
                session.close()
                report["session_close_returned"] = True
            except BaseException as error:
                report["session_close_returned"] = False
                report["cleanup_errors"].append(str(error))
                if primary is None:
                    primary = error
    if primary is not None:
        report["state"] = "raised"
        report["error"] = {
            "type": type(primary).__name__,
            "message": str(primary),
        }
    print(json.dumps(report, indent=2, allow_nan=False))
    if primary is not None:
        return 130 if isinstance(primary, KeyboardInterrupt) else 1
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
