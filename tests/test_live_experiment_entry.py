"""Offline tests for the explicit live experiment entry point."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

import test_preconditioning_plan as plan_tests
import test_preconditioning_runner as runner_tests

from experiments.run_live import main


class FakeSMU:
    def __init__(self, events, directory):
        self.events = events
        self.directory = directory
        self.is_connected = False
        self.identity = (
            "Keysight Technologies,B2902B,TESTSERIAL,TESTFW"
        )
        self.connect_error = None
        self.shutdown_error = None

    def connect(self):
        if not (
            self.directory / "experiment.json"
        ).is_file():
            raise AssertionError(
                "Manifest was not reserved before connection."
            )

        self.events.append("connect")

        if self.connect_error is not None:
            raise self.connect_error

        self.is_connected = True
        return self

    def shutdown(self):
        self.events.append("session_shutdown")

        if self.shutdown_error is not None:
            raise self.shutdown_error

    def close(self):
        self.events.append("close")
        self.is_connected = False


class LiveExperimentEntryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = plan_tests.PreconditioningPlanTests(
            methodName="test_two_cycles_have_eight_ordered_phases"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

        self.fixture.document["charge"][
            "cv_voltage_tolerance_v"
        ] = 0.02
        self.fixture.document["acquisition"][
            "maximum_sample_gap_s"
        ] = 0.25

        self.fixture.document["output"]["root_directory"] = str(
            self.fixture.root / "live_runs"
        )
        self.fixture.save()

        self.directory = (
            self.fixture.root
            / "live_runs"
            / "offline_live_test"
        )
        self.events = []
        self.smu = FakeSMU(
            self.events,
            self.directory,
        )
        self.backend = runner_tests.FakeBackend()
        self.backend.phase_outcomes = []

        factory_patcher = patch(
            "experiments.run_live._new_smu",
            return_value=self.smu,
        )
        self.factory = factory_patcher.start()
        self.addCleanup(factory_patcher.stop)

        executor_patcher = patch(
            "experiments.run_live.RecordedPhaseExecutor"
        )
        self.executor_factory = executor_patcher.start()
        self.addCleanup(executor_patcher.stop)

        backend_patcher = patch(
            "experiments.run_live.RecordedPreconditioningBackend",
            return_value=self.backend,
        )
        self.backend_factory = backend_patcher.start()
        self.addCleanup(backend_patcher.stop)

    def invoke(self, *, live=False, extra=()):
        arguments = [
            "--config",
            str(self.fixture.path),
        ]

        if live:
            arguments += [
                "--execute",
                "--experiment-id",
                "offline_live_test",
                "--confirm-specimen",
                "TEST-001",
                "--expected-serial",
                "TESTSERIAL",
                "--max-voltage-v",
                "4.2",
                "--max-current-a",
                "2.0",
            ]

        arguments += list(extra)

        output = io.StringIO()
        errors = io.StringIO()

        with redirect_stdout(output), redirect_stderr(errors):
            code = main(arguments)

        return code, output.getvalue(), errors.getvalue()

    def manifest(self):
        return json.loads(
            (
                self.directory / "experiment.json"
            ).read_text(encoding="utf-8")
        )

    def test_default_preview_does_not_construct_instrument(self):
        code, output, errors = self.invoke()

        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        self.factory.assert_not_called()
        self.assertFalse(self.directory.exists())

        document = json.loads(output)
        self.assertFalse(document["will_connect"])
        self.assertFalse(document["will_enable_output"])

    def test_invalid_config_does_not_construct_instrument(self):
        self.fixture.document["experiment"]["cycles"] = 0
        self.fixture.save()

        code, _, _ = self.invoke(live=True)

        self.assertEqual(code, 1)
        self.factory.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_wrong_specimen_prevents_connection_and_reservation(self):
        code, _, _ = self.invoke(
            live=True,
            extra=("--confirm-specimen", "WRONG"),
        )

        self.assertEqual(code, 1)
        self.factory.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_insufficient_current_ceiling_prevents_connection(self):
        code, _, _ = self.invoke(
            live=True,
            extra=("--max-current-a", "0.1"),
        )

        self.assertEqual(code, 1)
        self.factory.assert_not_called()

    def test_success_reserves_before_connect_and_closes_before_finish(self):
        code, output, errors = self.invoke(live=True)

        self.assertEqual(code, 0, errors)
        self.assertEqual(
            json.loads(output)["state"],
            "completed",
        )
        self.assertEqual(
            self.events,
            ["connect", "session_shutdown", "close"],
        )

        document = self.manifest()
        self.assertEqual(document["state"], "completed")
        self.assertTrue(
            document["session_outcome"]["serial_verified"]
        )
        self.assertTrue(
            document["session_outcome"]["shutdown_verified"]
        )
        self.assertTrue(
            document["session_outcome"]["close_succeeded"]
        )

        self.executor_factory.assert_called_once()
        self.backend_factory.assert_called_once()

    def test_wrong_instrument_serial_prevents_backend_execution(self):
        self.smu.identity = (
            "Keysight Technologies,B2902B,OTHER_SERIAL,TESTFW"
        )

        code, _, _ = self.invoke(live=True)

        self.assertEqual(code, 1)
        self.executor_factory.assert_not_called()
        self.backend_factory.assert_not_called()
        self.assertEqual(
            self.events,
            ["connect", "session_shutdown", "close"],
        )
        self.assertEqual(
            self.manifest()["state"],
            "raised",
        )

    def test_existing_destination_prevents_instrument_construction(self):
        self.directory.mkdir(parents=True)

        code, _, _ = self.invoke(live=True)

        self.assertEqual(code, 1)
        self.factory.assert_not_called()

    def test_phase_interrupt_is_recorded_and_session_closed(self):
        self.backend.phase_error = KeyboardInterrupt(
            "synthetic cancellation"
        )

        code, _, errors = self.invoke(live=True)

        self.assertEqual(code, 130)
        self.assertEqual(
            json.loads(errors)["error_type"],
            "KeyboardInterrupt",
        )
        self.assertEqual(
            self.manifest()["state"],
            "raised",
        )
        self.assertEqual(self.events[-1], "close")

    def test_final_shutdown_failure_prevents_completed_manifest(self):
        self.smu.shutdown_error = OSError(
            "synthetic final shutdown failure"
        )

        code, _, errors = self.invoke(live=True)

        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(errors)["runner_state"],
            "completed",
        )

        document = self.manifest()
        self.assertEqual(document["state"], "raised")
        self.assertFalse(
            document["session_outcome"]["shutdown_verified"]
        )
        self.assertTrue(
            document["session_outcome"]["close_succeeded"]
        )

    def test_connection_failure_does_not_retry(self):
        self.smu.connect_error = OSError(
            "synthetic connection failure"
        )

        code, _, _ = self.invoke(live=True)

        self.assertEqual(code, 1)
        self.assertEqual(
            self.events,
            ["connect", "close"],
        )
        self.executor_factory.assert_not_called()

    def test_execute_requires_explicit_arguments(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main([
                    "--config",
                    str(self.fixture.path),
                    "--execute",
                ])

        self.assertEqual(caught.exception.code, 2)
        self.factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()