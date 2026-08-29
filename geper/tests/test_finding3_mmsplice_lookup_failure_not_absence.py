"""
Finding 3: an Ensembl exon-annotation lookup FAILURE rendered as a
biological finding, not as a lookup failure.

THE DEFECT (observed 2026-08-29, real run). Ensembl probed Online at
startup, then failed 3/3 retry attempts on `overlap/region` (exon
annotation) mid-run. `MMSpliceService._fetch_overlapping_exons`
(pipeline/models/mmsplice/service.py) had exactly one return value for
"Ensembl answered and there is genuinely no exon nearby" and "Ensembl
never answered": both paths returned `[]`, so `_prepare` could not tell
them apart and unconditionally wrote

    "no exon annotation found near this variant (Ensembl overlap/region
    returned none)"

into `skip_reason`, which `report/report_generator.py::_render_mmsplice`
then rendered verbatim as "_Not scored: no exon annotation found..._".

THAT CLAIM IS FALSE FOR NM_007294.4:c.135-1G>T
(NC_000017.11:g.43106534C>A, BRCA1, minus strand): ENIGMA/ClinVar curate
this as a canonical splice-acceptor variant 1bp into intron 2 -- it has
a real, known, nearby exon. The report's own splice-acceptor-distance
math elsewhere in the same run agrees. So the MMSplice section told the
reader "no exon annotation found near this variant" about a variant the
report itself identifies as being 1bp from one -- a lookup failure
rendered as a completed, negative biological search.

THE FIX. `_fetch_overlapping_exons` now RAISES `ExternalAPIError` for
both failure paths (offline-at-startup skip, retries-exhausted) instead
of returning `[]` -- mirroring the established pattern already used one
file over for the identical distinction, `SequenceContextGenerator.
_fetch_region` (pipeline/sequence_context.py). `[]` is now reserved for
what it always should have meant: Ensembl answered and there is
genuinely nothing there. `_prepare` catches `ExternalAPIError` and
returns a result carrying `error` (not just `skip_reason`) -- the same
key `_run_mmsplice_stage`'s own defense-in-depth crash handler already
sets (pipeline/orchestrator.py:2093) and that `_render_mmsplice` already
checks FIRST, before its "Not scored" (`supported=False`) branch. The
renderer-side distinction pre-existed this fix; only the producer was
silently collapsing the two cases before reaching it.

Both directions matter: `TestGenuineEmptyOverlapStillReadsAsAbsence`
below is the control that stops this fix from blurring a real,
completed "no exon here" result into "lookup failed".
"""

import unittest
from unittest import mock

import requests

from pipeline.models.mmsplice.service import MMSpliceService
from pipeline.sequence_context import SequenceContextGenerator
from pipeline.vcf_parser import Variant
from report.report_generator import ReportGenerator
from utils.service_health import HEALTH, ServiceCheck, ServiceStatus


def _resp_503():
    """A response object whose raise_for_status() raises like a real 503."""
    response = mock.Mock()
    response.status_code = 503
    response.raise_for_status.side_effect = requests.HTTPError("503 Server Error: Service Unavailable")
    return response


def _brca1_splice_acceptor_variant() -> Variant:
    # NC_000017.11:g.43106534C>A == NM_007294.4:c.135-1G>T (BRCA1 is
    # minus-strand): a canonical splice-acceptor variant, 1bp into
    # intron 2. Coordinates confirmed against geper/test_data/README.md
    # and geper/verify_ps3_bs3_integration.py.
    return Variant(chrom="17", pos=43106534, variant_id=".", ref="C", alt="A", qual=None, filter_status=None)


def _predict_with_forced_ensembl_503(service: MMSpliceService) -> dict:
    """
    Drive the REAL retry loop in `_fetch_overlapping_exons` to
    exhaustion with a forced 503 (not a mocked-out method), so this
    exercises the exact path that produced the observed false claim --
    same technique as `tests/test_service_health_reaches_outputs.py`'s
    `_force_ensembl_503` for the sequence-region lookup.
    """
    with (
        mock.patch.object(service._session, "get", return_value=_resp_503()),
        mock.patch("pipeline.models.mmsplice.service.time.sleep", return_value=None),
    ):
        return service.predict(_brca1_splice_acceptor_variant())


class TestFinding3ExonLookupFailureIsNotRenderedAsAbsence(unittest.TestCase):
    def setUp(self):
        HEALTH.reset()
        self.model = mock.Mock()
        self.seq_ctx = SequenceContextGenerator(species="human", assembly="GRCh38")
        self.service = MMSpliceService(model=self.model, sequence_context_generator=self.seq_ctx, species="human")

    def tearDown(self):
        HEALTH.reset()

    def test_a_forced_503_does_not_claim_no_exon_annotation_found(self):
        result = _predict_with_forced_ensembl_503(self.service)
        self.assertFalse(result["supported"])
        self.assertFalse(result["predicted"])
        self.assertNotIn(
            "no exon annotation found",
            result.get("skip_reason") or "",
            "a lookup that never got an answer from Ensembl must not be reported as a completed "
            "search that found nothing -- that is a false biological claim for a canonical "
            "splice-acceptor variant (NM_007294.4:c.135-1G>T has a real, known, nearby exon)",
        )

    def test_a_forced_503_sets_error_so_the_renderer_takes_the_failed_branch(self):
        result = _predict_with_forced_ensembl_503(self.service)
        self.assertTrue(
            result.get("error"),
            "an exon-annotation lookup that exhausted its retries must set 'error' -- the same key "
            "_run_mmsplice_stage's own crash handler sets and _render_mmsplice checks first",
        )

    def test_markdown_report_says_failed_not_not_scored(self):
        result = _predict_with_forced_ensembl_503(self.service)
        markdown = "\n".join(ReportGenerator._render_mmsplice(result))
        self.assertNotIn("no exon annotation found", markdown)
        self.assertNotIn("Not scored:", markdown)
        self.assertIn("_Failed:", markdown)

    def test_offline_at_startup_is_also_reported_as_a_failure_not_an_absence(self):
        """Same defect, same fix, the other failure path into
        `_fetch_overlapping_exons`: Ensembl confirmed offline before
        this variant was ever queried."""
        HEALTH.run_startup_checks([ServiceCheck("Ensembl", lambda: (ServiceStatus.OFFLINE, "Timeout"))])
        self.assertTrue(HEALTH.is_offline("Ensembl"), "test setup did not actually latch Ensembl offline")
        result = self.service.predict(_brca1_splice_acceptor_variant())
        self.assertNotIn("no exon annotation found", result.get("skip_reason") or "")
        self.assertTrue(result.get("error"))


class TestGenuineEmptyOverlapStillReadsAsAbsence(unittest.TestCase):
    """CONTROL: a real, successful, empty response must still read as
    the biological absence it is -- the fix must not blur every
    empty-exon-list result into 'lookup failed'."""

    def setUp(self):
        HEALTH.reset()
        self.model = mock.Mock()
        self.seq_ctx = SequenceContextGenerator(species="human", assembly="GRCh38")
        self.service = MMSpliceService(model=self.model, sequence_context_generator=self.seq_ctx, species="human")

    def tearDown(self):
        HEALTH.reset()

    def test_a_genuinely_empty_overlap_still_reports_no_exon_annotation(self):
        with mock.patch.object(self.service, "_fetch_overlapping_exons", return_value=[]):
            result = self.service.predict(_brca1_splice_acceptor_variant())
        self.assertFalse(result.get("error"))
        self.assertIn("no exon annotation found", result["skip_reason"])

    def test_markdown_report_still_says_not_scored_for_a_genuine_absence(self):
        with mock.patch.object(self.service, "_fetch_overlapping_exons", return_value=[]):
            result = self.service.predict(_brca1_splice_acceptor_variant())
        markdown = "\n".join(ReportGenerator._render_mmsplice(result))
        self.assertIn("Not scored:", markdown)
        self.assertNotIn("_Failed:", markdown)


if __name__ == "__main__":
    unittest.main()
