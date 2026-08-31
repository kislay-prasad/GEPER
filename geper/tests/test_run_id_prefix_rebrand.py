"""
FIX #13 (already ruled -- executing the rename only): the fallback run-ID
prefix `report/summary.py::_derive_run_id` generates changes from
"GEPER-RUN-" to "BIJ-RUN-", matching the "Bij AI" branding already used
elsewhere in generated reports (e.g. `summary_short.py`'s report title
"Bij AI Clinical Genomic Summary Report").

This is the only LIVE producer of the prefix -- confirmed by grep across
geper/ before touching anything: every other "GEPER-RUN-..." occurrence
in this codebase is a historical, already-generated example ID quoted in
a test docstring or fixture (a real run's actual recorded ID, e.g.
`GEPER-RUN-20260813` in tests/fixtures/offline_evidence/), not a second
place the prefix is constructed -- renaming the constant here does not
and should not change those, since they document what a specific past
run was actually called.
"""

import unittest

from report.summary import _derive_run_id


class TestRunIdPrefixIsBijRunNotGeperRun(unittest.TestCase):
    def test_fallback_run_id_uses_the_bij_prefix(self):
        """
        Before this fix: `_derive_run_id({"generated_at": "2026-08-31T10:00:00+00:00"}, None)`
        returned 'GEPER-RUN-20260831T10000' -- confirmed against the
        unmodified module by a direct probe before any edit was made.
        """
        run_id = _derive_run_id({"generated_at": "2026-08-31T10:00:00+00:00"}, None)
        self.assertTrue(run_id.startswith("BIJ-RUN-"), f"expected BIJ-RUN- prefix, got {run_id!r}")
        self.assertFalse(run_id.startswith("GEPER-RUN-"), f"old GEPER-RUN- prefix still present: {run_id!r}")
        self.assertEqual(run_id, "BIJ-RUN-20260831T10000")

    def test_explicit_run_id_is_still_passed_through_unchanged(self):
        """An explicitly-supplied run_id (the real LIMS/hospital path this
        function's own docstring names as the intended production use) is
        never touched by this rename -- only the synthesized fallback."""
        self.assertEqual(_derive_run_id({}, "LAB-2026-00042"), "LAB-2026-00042")


if __name__ == "__main__":
    unittest.main()
