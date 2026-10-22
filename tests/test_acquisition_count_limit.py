"""Offline tests for the finite acquisition count guard.

Allowed counts must pass the argument guard and reach preflight.
These tests do not establish real instrument buffer capability.
"""

import unittest

from acquisition.buffered_acquisition import BufferedAcquisition


class PreflightReached(RuntimeError):
    """Sentinel showing the count guard accepted the request."""


class FakeChannel:
    def __init__(self):
        self.queries = 0

    def get_output_state(self):
        self.queries += 1
        raise PreflightReached("Instrument preflight reached.")


class FakeSMU:
    def __init__(self):
        self.chan1 = FakeChannel()
        self.chan2 = FakeChannel()


class AcquisitionCountLimitTests(unittest.TestCase):
    def test_software_limit_is_100000(self):
        self.assertEqual(BufferedAcquisition.MAX_RECORDS, 100000)

    def test_allowed_counts_reach_instrument_preflight(self):
        for count in (1, 2500, 2501, 72001, 100000):
            with self.subTest(count=count):
                smu = FakeSMU()
                acquisition = BufferedAcquisition(smu, 1)

                with self.assertRaises(PreflightReached):
                    acquisition.configure(
                        count=count,
                        interval_s=0.2,
                        nplc=1.0,
                    )

                self.assertEqual(smu.chan1.queries, 1)

    def test_invalid_counts_never_access_instrument(self):
        for count in (
            True,
            False,
            None,
            0,
            -1,
            1.0,
            "100",
            100001,
        ):
            with self.subTest(count=count):
                smu = FakeSMU()
                acquisition = BufferedAcquisition(smu, 1)

                with self.assertRaises(ValueError):
                    acquisition.configure(
                        count=count,
                        interval_s=0.2,
                        nplc=1.0,
                    )

                self.assertEqual(smu.chan1.queries, 0)
                self.assertEqual(smu.chan2.queries, 0)


if __name__ == "__main__":
    unittest.main()