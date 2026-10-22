"""Regression tests for phase-specific transport arguments."""

import unittest

import test_phase_recorded_acquisition as coordinator_tests


class StrictChargeTransport(coordinator_tests.FakeTransport):
    def acquire_charge(
        self,
        phase,
        *,
        cv_classifier,
        instrument_timestamp_origin_s,
        phase_elapsed_at_origin_s,
        maximum_sample_gap_s,
        poll_interval_s=0.05,
        phase_started_s=None,
        clock=None,
        sleep=None,
    ):
        return self._acquire(
            phase,
            cv_classifier=cv_classifier,
            instrument_timestamp_origin_s=(
                instrument_timestamp_origin_s
            ),
            phase_elapsed_at_origin_s=phase_elapsed_at_origin_s,
            maximum_sample_gap_s=maximum_sample_gap_s,
            poll_interval_s=poll_interval_s,
            phase_started_s=phase_started_s,
            clock=clock,
            sleep=sleep,
        )


class StrictDischargeTransport(coordinator_tests.FakeTransport):
    def acquire_discharge(
        self,
        phase,
        *,
        instrument_timestamp_origin_s,
        phase_elapsed_at_origin_s,
        maximum_sample_gap_s,
        maximum_voltage_v,
        poll_interval_s=0.05,
        phase_started_s=None,
        clock=None,
        sleep=None,
    ):
        return self._acquire(
            phase,
            instrument_timestamp_origin_s=(
                instrument_timestamp_origin_s
            ),
            phase_elapsed_at_origin_s=phase_elapsed_at_origin_s,
            maximum_sample_gap_s=maximum_sample_gap_s,
            maximum_voltage_v=maximum_voltage_v,
            poll_interval_s=poll_interval_s,
            phase_started_s=phase_started_s,
            clock=clock,
            sleep=sleep,
        )


class PhaseTransportDispatchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = (
            coordinator_tests.PhaseRecordedAcquisitionTests(
                methodName=(
                    "test_success_returns_qualified_phase_result"
                )
            )
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def install_transport(self, transport_class):
        fixture = self.fixture

        transport = transport_class(
            fixture.acquisition,
            fixture.run.writer,
            fixture.fixture.readings,
            fixture.fixture.report,
        )

        fixture.transport = transport
        fixture.prepared.transport = transport
        return transport

    def test_charge_receives_classifier_not_maximum_voltage(self):
        transport = self.install_transport(
            StrictChargeTransport
        )

        result, _ = self.fixture.acquire()

        self.assertEqual(
            result.stop_reason,
            "cv_current_termination",
        )

        arguments = transport.calls[0][1]
        self.assertIs(
            arguments["cv_classifier"],
            self.fixture.prepared.cv_classifier,
        )
        self.assertNotIn("maximum_voltage_v", arguments)

    def test_discharge_receives_maximum_voltage_not_classifier(self):
        self.fixture.fixture.phase["type"] = "discharge_cc"
        self.fixture.fixture.report.update({
            "phase_type": "discharge",
            "expected_stop_reason": "voltage_min",
            "stop_reason": "voltage_min",
        })

        transport = self.install_transport(
            StrictDischargeTransport
        )

        result, _ = self.fixture.acquire()

        self.assertEqual(result.stop_reason, "voltage_min")

        arguments = transport.calls[0][1]
        self.assertEqual(arguments["maximum_voltage_v"], 4.2)
        self.assertNotIn("cv_classifier", arguments)

    def test_missing_charge_classifier_fails_before_acquisition(self):
        transport = self.install_transport(
            StrictChargeTransport
        )
        del self.fixture.prepared.cv_classifier

        with self.assertRaises(AttributeError):
            self.fixture.acquire()

        self.assertEqual(transport.calls, [])
        self.assertTrue(self.fixture.run.writer.closed)
        self.assertFalse(
            (
                self.fixture.run.directory / "COMPLETE"
            ).exists()
        )


if __name__ == "__main__":
    unittest.main()