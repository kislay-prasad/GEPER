"""
w122: the retraction banner, on every rendered surface, and the flag.

THE OTHER HALF OF THE RENDER PROOF for
`clinical.data_access.DataAccess._read_retracted` -- the exact dicts that
reader returns, rendered into the signed-off wording. It lives here rather
than in the clinical suite for the reason stated in
`clinical/report_revision_shape.py`: importing the renderer from there pulls
`pipeline.acmg_rules -> pipeline.gnomad -> requests`, which the clinical CI
job does not install. The two halves are joined by that contract module, which
imports nothing, and each side pins its own half against it.

WHAT THIS FILE PROVES
  E3  the machine-readable `is_retracted` flag is in the JSON block, not only
      in banner prose. The human: "Not shipping without the flag." A consumer
      that reads structure rather than reading a page has something to check.
  E9  the three signed-off retraction forms, byte for byte, and the narrowed
      re-analysis sentence that replaces the one retraction made false.
  ORDER  retraction renders FIRST, ahead of superseded.
  SURFACES  all three renderers -- Markdown, full PDF, short PDF -- carry it,
      asserted on RENDERED OUTPUT rather than on the source constants.
"""

import os
import tempfile
import unittest

# pypdf: declared in geper/requirements.txt and already used by
# tests/test_report_revision_banners.py, which runs in the same CI job.
from pypdf import PdfReader

from report.clinical_report_builder import (
    REPORT_REVISION_RETRACTED_BANNER,
    REPORT_REVISION_RETRACTED_REPLACED_BANNER,
    REPORT_REVISION_RETRACTED_REPLACEMENT_NOT_RETAINED_BANNER,
    build_clinical_report,
    normalize_report_revision,
    report_revision_banners,
)
from report.report_generator import ReportGenerator
from report.summary import generate_pdf
from report.summary_short import generate_short_pdf

REPORT_ID = "0b6f1c9e-2a41-4d8e-9c55-1e7a3f0d2b11"
REPLACEMENT_REPORT_ID = "7d2e4a10-58c3-4f6b-a9e2-3c1d0b8f6e42"
AMENDMENT_REPORT_ID = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
WHO = "Dr A. Rao (MCI-12345), Apollo Hospitals"
REASON = "Sample mix-up: this report was issued against the wrong patient's sample"


def _load_revision_shape():
    """The contract module, loaded BY PATH -- see test_w118_banner_rulings.py."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "clinical" / "report_revision_shape.py"
    spec = importlib.util.spec_from_file_location("_w122_revision_shape", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SHAPE = _load_revision_shape()

# THE EXACT DICTS `DataAccess._read_retracted` RETURNS, transcribed with real
# values. Checked against SHAPE before they are rendered, so a key renamed on
# the clinical side (which fails clinical/tests/test_w122_retraction.py's own
# shape assertion) cannot leave these fixtures quietly stale.
READER_RETRACTED = {
    "issued_at": "2026-08-01T09:00:00+00:00",
    "issued_basis": "released",
    "retracted_at": "2026-09-05T10:30:00+00:00",
    "retracted_by": WHO,
    "reason": REASON,
}
READER_RETRACTED_APPROVED_ONLY = dict(READER_RETRACTED, issued_basis="approved")
READER_RETRACTED_WITH_REPLACEMENT = dict(
    READER_RETRACTED, replacement_report_id=REPLACEMENT_REPORT_ID, replacement_retained=True
)
READER_RETRACTED_REPLACEMENT_GONE = dict(
    READER_RETRACTED, replacement_report_id=REPLACEMENT_REPORT_ID, replacement_retained=False
)

SUPERSEDED_BY = {
    "amendment_report_id": AMENDMENT_REPORT_ID,
    "amended_at": "2026-09-06T10:30:00+00:00",
    "amended_at_basis": "released",
    "retained": True,
    "reason": "ClinVar reclassified the BRCA1 variant",
}

# The signed-off wording, rendered. Written out in full rather than formatted
# from the constants: a test that builds its expectation from the thing it is
# testing agrees with it by construction and would not notice a reworded
# template.
BANNER_NO_REPLACEMENT = (
    "*** THIS REPORT HAS BEEN RETRACTED. It was issued on 2026-08-01 and was retracted on "
    f"2026-09-05 16:00 IST by {WHO}; do not act on this document. No replacement report has been issued. "
    f"Reason for the retraction: {REASON}. ***"
)
BANNER_APPROVED_BASIS = (
    "*** THIS REPORT HAS BEEN RETRACTED. It was approved on 2026-08-01 and was retracted on "
    f"2026-09-05 16:00 IST by {WHO}; do not act on this document. No replacement report has been issued. "
    f"Reason for the retraction: {REASON}. ***"
)
BANNER_WITH_REPLACEMENT = (
    "*** THIS REPORT HAS BEEN RETRACTED. It was issued on 2026-08-01 and was retracted on "
    f"2026-09-05 16:00 IST by {WHO}; do not act on this document without first obtaining the "
    f"replacement report ({REPLACEMENT_REPORT_ID}). Reason for the retraction: {REASON}. ***"
)
BANNER_REPLACEMENT_GONE = (
    "*** THIS REPORT HAS BEEN RETRACTED. It was issued on 2026-08-01 and was retracted on "
    f"2026-09-05 16:00 IST by {WHO}. A replacement report ({REPLACEMENT_REPORT_ID}) was issued and is "
    "NO LONGER RETAINED: it has passed its retention period and cannot be obtained. This document "
    f"remains retracted; do not act on it. Reason for the retraction: {REASON}. ***"
)


def _banners(**revision):
    return report_revision_banners({"report_revision": revision})


def _one(**revision):
    banners = _banners(**revision)
    assert len(banners) == 1, banners
    return banners[0]


class TestTheShapeTheReaderEmits(unittest.TestCase):
    """Pinned against the contract module, from the renderer's side."""

    def test_the_reader_fixtures_match_the_declared_shape(self):
        self.assertEqual(set(READER_RETRACTED), set(SHAPE.RETRACTED_REQUIRED))
        self.assertIn(READER_RETRACTED["issued_basis"], SHAPE.DATE_BASES)
        self.assertEqual(
            set(READER_RETRACTED_WITH_REPLACEMENT),
            set(SHAPE.RETRACTED_REQUIRED) | set(SHAPE.RETRACTED_REPLACEMENT_FIELDS),
        )

    def test_retracted_is_a_declared_revision_key(self):
        self.assertIn("retracted", SHAPE.REVISION_KEYS)


class TestTheThreeSignedOffForms(unittest.TestCase):
    def test_form_1_no_replacement_never_sends_the_reader_to_obtain_one(self):
        text = _one(retracted=READER_RETRACTED)
        self.assertEqual(text, BANNER_NO_REPLACEMENT)
        # R8's rule generalised: E4 made a replacement optional, so a reader
        # must never be told to wait for a document nobody has undertaken to
        # produce.
        self.assertNotIn("obtaining the replacement report", text)

    def test_form_2_a_live_replacement_is_named_and_obtainable(self):
        text = _one(retracted=READER_RETRACTED_WITH_REPLACEMENT)
        self.assertEqual(text, BANNER_WITH_REPLACEMENT)
        self.assertNotIn("No replacement report has been issued", text)

    def test_form_3_a_destroyed_replacement_is_named_as_gone_not_pointed_at(self):
        text = _one(retracted=READER_RETRACTED_REPLACEMENT_GONE)
        self.assertEqual(text, BANNER_REPLACEMENT_GONE)
        self.assertIn("NO LONGER RETAINED", text)
        self.assertNotIn("without first obtaining", text)

    def test_the_three_forms_are_three_different_sentences(self):
        """
        Guards the cannot-fail pass: all three assertions above would be
        satisfied by one template that happened to contain every phrase.
        """
        rendered = {
            _one(retracted=READER_RETRACTED),
            _one(retracted=READER_RETRACTED_WITH_REPLACEMENT),
            _one(retracted=READER_RETRACTED_REPLACEMENT_GONE),
        }
        self.assertEqual(len(rendered), 3)

    def test_e1_the_basis_changes_the_verb_and_nothing_else(self):
        """
        HUMAN RULING E1: the row records WHICH state it was retracted from, so
        a report approved but never issued is never described as issued.
        """
        text = _one(retracted=READER_RETRACTED_APPROVED_ONLY)
        self.assertEqual(text, BANNER_APPROVED_BASIS)
        self.assertIn("It was approved on 2026-08-01", text)
        self.assertNotIn("It was issued on", text)

    def test_the_wording_is_the_constant_and_the_constant_is_the_wording(self):
        """
        The pin the signed-off templates carry: every other banner in this
        file is asserted as rendered text, so the constants themselves are
        pinned once, here, against the sign-off.
        """
        self.assertEqual(
            REPORT_REVISION_RETRACTED_BANNER,
            "*** THIS REPORT HAS BEEN RETRACTED. It was {verb} on {date} and was retracted on {when} by "
            "{who}; do not act on this document. No replacement report has been issued. "
            "Reason for the retraction: {reason}. ***",
        )
        self.assertEqual(
            REPORT_REVISION_RETRACTED_REPLACED_BANNER,
            "*** THIS REPORT HAS BEEN RETRACTED. It was {verb} on {date} and was retracted on {when} by "
            "{who}; do not act on this document without first obtaining the replacement report ({id}). "
            "Reason for the retraction: {reason}. ***",
        )
        self.assertEqual(
            REPORT_REVISION_RETRACTED_REPLACEMENT_NOT_RETAINED_BANNER,
            "*** THIS REPORT HAS BEEN RETRACTED. It was {verb} on {date} and was retracted on {when} by "
            "{who}. A replacement report ({id}) was issued and is NO LONGER RETAINED: it has passed its "
            "retention period and cannot be obtained. This document remains retracted; do not act on it. "
            "Reason for the retraction: {reason}. ***",
        )


class TestE3TheMachineReadableFlag(unittest.TestCase):
    """
    "Not shipping without the flag." A consumer that takes the JSON and never
    renders a page must still be able to see that the report is retracted.
    """

    def test_a_retracted_document_carries_is_retracted_true(self):
        block = normalize_report_revision({"retracted": READER_RETRACTED})
        self.assertIs(block["is_retracted"], True)

    def test_a_document_in_another_state_carries_is_retracted_false(self):
        # The positive twin: a flag hardcoded True satisfies the test above.
        block = normalize_report_revision({"superseded_by": SUPERSEDED_BY})
        self.assertIs(block["is_retracted"], False)
        self.assertIs(block["is_superseded"], True)

    def test_the_flag_travels_with_the_facts_not_only_the_prose(self):
        """
        The failure E3 was ruled against: a downstream system that reads
        structure and ignores banners acting on a retracted report. The facts
        are in the block, addressable by key, not only inside a sentence.
        """
        block = normalize_report_revision({"retracted": READER_RETRACTED_WITH_REPLACEMENT})
        self.assertEqual(block["retracted"]["reason"], REASON)
        self.assertEqual(block["retracted"]["replacement_report_id"], REPLACEMENT_REPORT_ID)
        self.assertIs(block["retracted"]["replacement_retained"], True)

    def test_an_unsupplied_revision_is_still_none_not_a_false_flag(self):
        # A pipeline run cannot know whether the clinical platform will later
        # record a retraction, so an unsupplied block must not read as "not
        # retracted".
        self.assertIsNone(normalize_report_revision(None))


class TestFailClosed(unittest.TestCase):
    def test_a_retraction_with_no_reason_refuses_the_document(self):
        with self.assertRaises(ValueError):
            normalize_report_revision({"retracted": dict(READER_RETRACTED, reason="   ")})

    def test_a_replacement_id_without_its_retention_state_refuses(self):
        with self.assertRaises(ValueError):
            normalize_report_revision(
                {"retracted": dict(READER_RETRACTED, replacement_report_id=REPLACEMENT_REPORT_ID)}
            )

    def test_a_retention_state_without_a_replacement_id_refuses(self):
        with self.assertRaises(ValueError):
            normalize_report_revision({"retracted": dict(READER_RETRACTED, replacement_retained=False)})

    def test_an_unknown_basis_refuses_rather_than_defaulting(self):
        with self.assertRaises(ValueError):
            normalize_report_revision({"retracted": dict(READER_RETRACTED, issued_basis="withdrawn")})

    def test_the_good_block_still_renders(self):
        # The paired positive for all four refusals above: without it, a
        # normalize that raised on everything would pass every one of them.
        self.assertEqual(_one(retracted=READER_RETRACTED), BANNER_NO_REPLACEMENT)


class TestBannerOrder(unittest.TestCase):
    def test_retraction_renders_first_ahead_of_superseded(self):
        banners = _banners(retracted=READER_RETRACTED, superseded_by=SUPERSEDED_BY)
        self.assertEqual(len(banners), 2)
        self.assertEqual(banners[0], BANNER_NO_REPLACEMENT)
        self.assertIn("THIS REPORT HAS BEEN SUPERSEDED", banners[1])

    def test_the_order_is_not_an_accident_of_the_input(self):
        """
        Dict order cannot be what decides this. The same two states supplied
        in the other order render in the same order.
        """
        banners = _banners(superseded_by=SUPERSEDED_BY, retracted=READER_RETRACTED)
        self.assertEqual(banners[0], BANNER_NO_REPLACEMENT)


class TestE9TheReanalysisSentenceNoLongerContradictsItself(unittest.TestCase):
    """
    The live falsehood this feature would otherwise have created. Banner 4 used
    to assert "it has not been retracted", which becomes false the moment a
    retraction exists -- one document contradicting itself between two banners
    on the same page.
    """

    REANALYSED_SINCE = [{"interpretation_id": "e1f2a3b4-c5d6-4e7f-8091-a2b3c4d5e6f7", "created_at": "2026-09-08"}]

    def test_the_clause_no_longer_claims_the_report_is_not_retracted(self):
        text = _one(reanalysed_since=self.REANALYSED_SINCE)
        self.assertIn("the re-analysis has neither retracted nor superseded it", text)
        self.assertNotIn("it has not been retracted", text)

    def test_the_clause_still_does_its_original_job(self):
        """
        Not deleted -- narrowed. The sentence exists to stop a reader treating
        a re-analysis as a withdrawal, and it still says exactly that.
        """
        text = _one(reanalysed_since=self.REANALYSED_SINCE)
        self.assertIn("re-analysis creates a new branch, it does not replace this one", text)

    def test_a_retracted_and_reanalysed_report_does_not_contradict_itself(self):
        banners = _banners(retracted=READER_RETRACTED, reanalysed_since=self.REANALYSED_SINCE)
        joined = " ".join(banners)
        self.assertIn("THIS REPORT HAS BEEN RETRACTED", joined)
        self.assertNotIn("it has not been retracted", joined)


# ── the render proof: all three surfaces ───────────────────────────────────


def _document(report_revision):
    return {
        "geper_version": "test",
        "generated_at": "2026-09-01T00:00:00+00:00",
        "input_vcf": "x.vcf",
        "assembly": "GRCh38",
        "vcf_samples": ["S1"],
        "variant_count": 1,
        "report_revision": report_revision,
        "variants": [
            {
                "variant": {"chrom": "17", "pos": 43106534, "ref": "C", "alt": "A"},
                "interpretation_result": {"gene_symbol": "BRCA1"},
                "candidate_interpretation": build_clinical_report(
                    {
                        "variant": {"chrom": "17", "pos": 43106534, "ref": "C", "alt": "A"},
                        "gene_symbol": "BRCA1",
                        "acmg_classification": "Pathogenic",
                        "evidence_sources": ["ClinVar"],
                    },
                    raw_evidence={},
                ),
                "errors": [],
            }
        ],
    }


def _flat(text):
    return " ".join(text.split())


def _markdown(doc):
    return _flat(ReportGenerator().generate(doc))


def _full_pdf(doc):
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "full.pdf")
        generate_pdf(doc, out)
        return _flat("\n".join(p.extract_text() for p in PdfReader(out).pages))


def _short_pdf(doc):
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "short.pdf")
        generate_short_pdf(doc, out)
        return _flat("\n".join(p.extract_text() for p in PdfReader(out).pages))


class _SurfaceContract:
    """Mixed into one TestCase per surface. `render(doc) -> flattened text`."""

    render = None

    def test_the_retraction_banner_reaches_this_surface(self):
        text = type(self).render(_document({"retracted": READER_RETRACTED}))
        self.assertIn(BANNER_NO_REPLACEMENT, text)

    def test_a_report_that_is_not_retracted_prints_nothing_of_the_kind(self):
        # The negative control, paired with the positive above so that a
        # renderer printing the banner unconditionally cannot pass both.
        text = type(self).render(_document(None))
        self.assertNotIn("THIS REPORT HAS BEEN RETRACTED", text)

    def test_the_replacement_form_reaches_this_surface_too(self):
        text = type(self).render(_document({"retracted": READER_RETRACTED_WITH_REPLACEMENT}))
        self.assertIn(BANNER_WITH_REPLACEMENT, text)
        self.assertNotIn("No replacement report has been issued", text)

    def test_retraction_precedes_supersession_on_the_page(self):
        text = type(self).render(_document({"retracted": READER_RETRACTED, "superseded_by": SUPERSEDED_BY}))
        retracted_at = text.index("THIS REPORT HAS BEEN RETRACTED")
        superseded_at = text.index("THIS REPORT HAS BEEN SUPERSEDED")
        self.assertLess(retracted_at, superseded_at)


class TestMarkdownSurface(_SurfaceContract, unittest.TestCase):
    render = staticmethod(_markdown)


class TestFullPdfSurface(_SurfaceContract, unittest.TestCase):
    render = staticmethod(_full_pdf)


class TestShortPdfSurface(_SurfaceContract, unittest.TestCase):
    render = staticmethod(_short_pdf)


if __name__ == "__main__":
    unittest.main()
