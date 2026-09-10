"""
The same defect class as Finding 3, at the sites Finding 3's fix did not
reach -- plus its dormant twin in `annotation/indigenomes.py`.

WHAT FINDING 3 ESTABLISHED (see
test_finding3_mmsplice_lookup_failure_not_absence.py): a lookup that
never got an answer must not render as a completed, negative biological
search. `_null_result`'s own docstring
(pipeline/models/mmsplice/service.py) states the contract in words:

    a caller that could not determine an answer (fetch failure, retries
    exhausted) must pass `error` so a lookup failure never renders as a
    completed, negative biological finding.

THAT FIX WAS APPLIED AT ONE CALL SITE. Measured at c09c9f4, `service.py`
has EIGHT `_null_result` call sites. Exactly one passed `error` -- the
exon-annotation path Finding 3 fixed. Four of the other seven are
correct by construction (an unsupported variant type, a genuinely empty
exon list, no eligible exon within the window): those are real completed
negatives and MUST NOT carry `error`. The controls below pin that.

The remaining three could not determine an answer and said nothing:

  * `_prepare`'s `_build_windows` handler -- **the twin of the site
    Finding 3 fixed, a few lines further down the same function,
    catching the same ExternalAPIError**. It returns `supported=True`,
    so an Ensembl outage renders as "_Skipped: reference sequence fetch
    failed_" -- under the heading the report uses for completed work.
  * `predict`'s outer crash handler.
  * `predict_batch`'s per-variant preparation handler, and its
    "no result produced" backstop.

A FIX APPLIED AT ONE LAYER OF A SUBSYSTEM IS EVIDENCE ABOUT THE OTHER
LAYERS, NOT ASSURANCE ABOUT THEM.

AND THE INDIGENOMES TWIN. `annotation/indigenomes.py::_lookup` returns
`{"status": "error", "error": str(exc)}`. **`str(exc)` is "" for any
exception raised without a message** -- the same fact
`pipeline/orchestrator.py::_run_mmsplice_stage` already guards against
one layer up with `detail = str(exc) or type(exc).__name__` (d1128ee).
An empty error string is indistinguishable from no error at all to any
truthiness-based consumer.

That site is DEAD CODE TODAY, and that is precisely why it is worth
fixing rather than leaving. Measured at c09c9f4: `_run_indigenomes_stage`
has ZERO call sites (only its own definition) -- `process_variant` calls
`_indigenomes_retired_result()` instead -- and `CONFIG.indigenomes.ENABLED`
defaults to False. Nothing structural prevents the site being reached;
the orchestrator's own comment says the stage is "kept for reinstatement,
not deleted". Dormant is not fixed. A defect that is unreachable only
because nobody currently calls it is armed, not disarmed.
"""

import unittest
from unittest import mock

from annotation import indigenomes
from pipeline.models.mmsplice.models import ExonAnnotation
from pipeline.models.mmsplice.service import MMSpliceService
from pipeline.sequence_context import SequenceContextGenerator
from pipeline.vcf_parser import Variant
from report.report_generator import ReportGenerator
from utils.exceptions import ExternalAPIError
from utils.service_health import HEALTH


def _brca1_splice_acceptor_variant() -> Variant:
    # The same variant Finding 3 uses: NC_000017.11:g.43106534C>A ==
    # NM_007294.4:c.135-1G>T, a canonical splice-acceptor variant 1bp
    # into BRCA1 intron 2 -- chosen so that "nothing here" is a claim
    # the rest of the report contradicts.
    return Variant(chrom="17", pos=43106534, variant_id=".", ref="C", alt="A", qual=None, filter_status=None)


def _eligible_exon() -> ExonAnnotation:
    """An exon the variant above falls next to, so eligibility passes and
    the run reaches the sequence-window fetch."""
    return ExonAnnotation(
        chrom="17",
        start=43106533,
        end=43106600,
        strand=-1,
        exon_id="ENSE00003510592",
        transcript_id="ENST00000357654",
        gene_id="ENSG00000012048",
    )


class _ServiceCase(unittest.TestCase):
    def setUp(self):
        HEALTH.reset()
        self.model = mock.Mock()
        self.seq_ctx = SequenceContextGenerator(species="human", assembly="GRCh38")
        self.service = MMSpliceService(model=self.model, sequence_context_generator=self.seq_ctx, species="human")

    def tearDown(self):
        HEALTH.reset()


class TestSequenceWindowFetchFailureIsNotAnAbsence(_ServiceCase):
    """The twin of the site Finding 3 fixed, in the same function."""

    def _result_with_failed_window_fetch(self) -> dict:
        with (
            mock.patch.object(self.service, "_fetch_overlapping_exons", return_value=[_eligible_exon()]),
            mock.patch.object(
                self.service,
                "_build_windows",
                side_effect=ExternalAPIError("Ensembl request to 'sequence/region' failed after 3 attempts: 503"),
            ),
        ):
            return self.service.predict(_brca1_splice_acceptor_variant())

    def test_it_sets_error(self):
        result = self._result_with_failed_window_fetch()
        self.assertTrue(
            result.get("error"),
            "a reference-sequence fetch that exhausted its retries could not determine an answer, "
            "so it must set 'error' -- the contract _null_result's own docstring states",
        )

    def test_the_report_says_failed_not_skipped(self):
        markdown = "\n".join(ReportGenerator._render_mmsplice(self._result_with_failed_window_fetch()))
        self.assertIn("_Failed:", markdown)
        self.assertNotIn("_Skipped:", markdown)
        self.assertNotIn("Not scored:", markdown)


class TestPredictCrashIsNotAnAbsence(_ServiceCase):
    def _result_with_crashing_prepare(self, exc=None) -> dict:
        with mock.patch.object(self.service, "_prepare", side_effect=exc or RuntimeError("boom")):
            return self.service.predict(_brca1_splice_acceptor_variant())

    def test_it_sets_error(self):
        self.assertTrue(self._result_with_crashing_prepare().get("error"))

    def test_the_report_says_failed_not_not_scored(self):
        markdown = "\n".join(ReportGenerator._render_mmsplice(self._result_with_crashing_prepare()))
        self.assertIn("_Failed:", markdown)
        self.assertNotIn("Not scored:", markdown)

    def test_a_messageless_exception_still_names_something(self):
        """`str(exc)` is "" for an exception raised without a message, and
        an empty error string is indistinguishable from no error to a
        truthiness check -- the fact d1128ee established one layer up."""
        result = self._result_with_crashing_prepare(RuntimeError())
        self.assertTrue(result.get("error"), "an exception with no message must still produce a non-empty error")
        self.assertIn("RuntimeError", result["error"])


class TestBatchFailuresAreNotAbsences(_ServiceCase):
    def test_a_preparation_crash_sets_error(self):
        with mock.patch.object(self.service, "_prepare", side_effect=RuntimeError("boom")):
            results = self.service.predict_batch([_brca1_splice_acceptor_variant()])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].get("error"))

    def test_the_no_result_backstop_sets_error(self):
        """If the predictor returns fewer rows than were sent, `zip`
        truncates and a slot stays None. Whatever that is, it is not a
        completed negative search."""
        with (
            mock.patch.object(self.service, "_fetch_overlapping_exons", return_value=[_eligible_exon()]),
            mock.patch.object(self.service, "_build_windows", return_value=("ACGT", "ACGA", (10, 10))),
            mock.patch.object(self.service.predictor, "predict_batch", return_value=[]),
        ):
            results = self.service.predict_batch([_brca1_splice_acceptor_variant()])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].get("error"))


class TestGenuineNegativesStillReadAsNegatives(_ServiceCase):
    """CONTROL. These are completed, successful, negative answers. They
    MUST NOT carry `error`, or the fix above has simply relabelled every
    unscored variant as a failure."""

    def test_a_genuinely_empty_exon_list_is_not_an_error(self):
        with mock.patch.object(self.service, "_fetch_overlapping_exons", return_value=[]):
            result = self.service.predict(_brca1_splice_acceptor_variant())
        self.assertIsNone(result.get("error"))
        self.assertIn("no exon annotation found", result["skip_reason"])
        markdown = "\n".join(ReportGenerator._render_mmsplice(result))
        self.assertIn("Not scored:", markdown)
        self.assertNotIn("_Failed:", markdown)

    def test_no_eligible_exon_is_not_an_error(self):
        """A real exon that is genuinely too far away: a completed
        search with a negative answer."""
        far_exon = ExonAnnotation(
            chrom="17",
            start=43200000,
            end=43200500,
            strand=-1,
            exon_id="ENSE00000000001",
            transcript_id="ENST00000357654",
        )
        with mock.patch.object(self.service, "_fetch_overlapping_exons", return_value=[far_exon]):
            result = self.service.predict(_brca1_splice_acceptor_variant())
        self.assertIsNone(result.get("error"))
        markdown = "\n".join(ReportGenerator._render_mmsplice(result))
        self.assertNotIn("_Failed:", markdown)


class TestIndigenomesEmptyErrorString(unittest.TestCase):
    """The dormant twin. Dead code today; armed, not disarmed."""

    def test_a_messageless_exception_does_not_produce_an_empty_error(self):
        with mock.patch.object(indigenomes, "_post", side_effect=ExternalAPIError()):
            outcome = indigenomes._lookup("17", 43106534, "C", "A")
        self.assertEqual(outcome["status"], "error")
        self.assertTrue(
            outcome["error"],
            "str(exc) is '' for an exception raised with no message, and an empty error string is "
            "indistinguishable from no error at all to any truthiness-based consumer",
        )
        self.assertIn("ExternalAPIError", outcome["error"])

    def test_a_message_carrying_exception_keeps_its_message(self):
        """CONTROL: the fix must not replace real detail with a type name."""
        with mock.patch.object(indigenomes, "_post", side_effect=ExternalAPIError("endpoint refused after 3 attempts")):
            outcome = indigenomes._lookup("17", 43106534, "C", "A")
        self.assertEqual(outcome["error"], "endpoint refused after 3 attempts")

    def test_a_genuine_absence_is_still_an_absence(self):
        """CONTROL: a successful query that found nothing is not an error."""
        with mock.patch.object(indigenomes, "_post", return_value={"mydata": []}):
            outcome = indigenomes._lookup("17", 43106535, "C", "G")
        self.assertEqual(outcome["status"], "not_found")
        self.assertNotIn("error", outcome)


if __name__ == "__main__":
    unittest.main()
