"""
Pins that a SUCCESSFUL AlphaFold query is never reported as one that
never happened.

THE DEFECT. `orchestrator.py::_capture_stage_provenance` had exactly two
branches for AlphaFold and no `else`:

    if alphafold_result.get("model_version"):   -> VERSION_KNOWN
    elif alphafold_result.get("error"):         -> UNKNOWN

So a query that SUCCEEDED but carried no version -- found=True,
model_version=None, error=None -- fell through both and recorded
nothing.

WHY THAT IS WORSE THAN "THE ROW IS MISSING". `RunProvenanceCollector`
pre-seeds every name in `KNOWN_SOURCES` at `VersionStatus.NOT_CONSULTED`
(provenance.py), and "AlphaFold DB" is one of them. Recording nothing
therefore does not leave a gap a reader would notice -- it leaves the
table ACTIVELY ASSERTING "never queried this run" about a source that
was queried, answered, and contributed real data to the call. That is a
false statement in the one artifact whose entire job is saying what was
consulted.

Which is why each test below asserts BOTH that the status is UNKNOWN and
that it is not NOT_CONSULTED. Because of the pre-seeding those are two
different failures -- "recorded the wrong thing" and "recorded nothing
at all" -- and only the first is what a reader of a bare `assertIs`
would assume had happened.

REACHABILITY IS NOT HYPOTHETICAL. `provider.py::_build_annotation`
yields `model_version=None` whenever the summary lacks `latestVersion`,
and `LocalDatasetAlphaFoldProvider.query` routes local records through
that same builder. A configured local dataset whose record has no
`latestVersion` produces exactly this shape.

`VersionStatus.UNKNOWN` is defined in provenance.py as "queried, but no
version/hash/anything beyond a timestamp was obtainable" -- this case,
verbatim. The type already had the state; the branch was simply never
written. The precedent sits twelve lines above the defect, where the
InterPro branch records TIMESTAMP_ONLY for a local-dataset hit.
"""

import unittest

from pipeline.orchestrator import GeperPipeline
from pipeline.provenance import RunProvenanceCollector, VersionStatus


class _CaptureTestCase(unittest.TestCase):
    """
    Drives `_capture_stage_provenance` directly. `GeperPipeline.__new__`
    skips __init__ deliberately: the method under test touches only
    `self.provenance`, and constructing a real pipeline would drag in
    model loading and network clients that have nothing to do with this
    branch.
    """

    def setUp(self):
        self.pipeline = GeperPipeline.__new__(GeperPipeline)
        self.pipeline.provenance = RunProvenanceCollector()

    def capture(self, alphafold_result):
        self.pipeline._capture_stage_provenance(
            clinvar_result={},
            dbsnp_result={},
            gnomad_result=None,
            uniprot_result=None,
            interpro_result=None,
            alphafold_result=alphafold_result,
            functional_evidence_result=None,
        )
        return self.pipeline.provenance.get("AlphaFold DB")


class TestASuccessfulQueryWithoutAVersionIsStillRecorded(_CaptureTestCase):
    def test_found_without_version_or_error_records_unknown(self):
        """The exact shape the local-dataset provider produces."""
        record = self.capture({"found": True, "model_version": None, "error": None})

        self.assertIsNotNone(record)
        self.assertIs(record.status, VersionStatus.UNKNOWN)

    def test_it_does_not_stay_pre_seeded_as_not_consulted(self):
        """
        The distinct failure the pre-seeding creates: not "wrong status"
        but "the table says we never asked", about a query that ran.
        """
        record = self.capture({"found": True, "model_version": None, "error": None})

        self.assertIsNot(record.status, VersionStatus.NOT_CONSULTED)

    def test_it_appears_in_the_rendered_table(self):
        """`to_list()` is what a report actually renders."""
        self.capture({"found": True, "model_version": None, "error": None})

        rows = self.pipeline.provenance.to_list()
        alphafold = [r for r in rows if r["source"] == "AlphaFold DB"]
        self.assertEqual(len(alphafold), 1)
        self.assertNotEqual(alphafold[0]["status"], VersionStatus.NOT_CONSULTED.value)

    def test_a_successful_query_that_found_no_structure_is_also_recorded(self):
        """
        Same defect, second shape. found=False means AlphaFold was asked
        and answered "no structure for this protein" -- it was still
        consulted, so NOT_CONSULTED is still a false report.
        """
        record = self.capture({"found": False, "model_version": None, "error": None})

        self.assertIsNot(record.status, VersionStatus.NOT_CONSULTED)
        self.assertIs(record.status, VersionStatus.UNKNOWN)


class TestTheExistingBranchesAreUnchanged(_CaptureTestCase):
    """The third branch must not eat the two that already worked."""

    def test_a_real_version_still_records_version_known(self):
        record = self.capture({"found": True, "model_version": "4", "error": None})

        self.assertIs(record.status, VersionStatus.VERSION_KNOWN)
        self.assertEqual(record.version, "AlphaFold DB v4")

    def test_an_error_still_records_unknown_with_its_note(self):
        record = self.capture({"found": False, "model_version": None, "error": "boom"})

        self.assertIs(record.status, VersionStatus.UNKNOWN)
        self.assertIn("boom", record.notes)

    def test_a_skipped_stage_is_still_left_untouched(self):
        """
        `skipped` means the stage never ran, so NOT_CONSULTED is the
        honest answer and the new branch must not overwrite it.
        """
        record = self.capture({"skipped": True, "found": False, "model_version": None, "error": None})

        self.assertIs(record.status, VersionStatus.NOT_CONSULTED)

    def test_no_alphafold_result_at_all_is_still_left_untouched(self):
        record = self.capture(None)

        self.assertIs(record.status, VersionStatus.NOT_CONSULTED)


if __name__ == "__main__":
    unittest.main()
