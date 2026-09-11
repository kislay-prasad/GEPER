"""
Revision-traceability banners (card HUMAN-CLINICAL-report-revision-
traceability-..., ruled 2026-09-10 "SURFACE IT", wording signed off
2026-09-11 with banner 3 revised).

The clinical platform records amendments (`clinical/data_access.py
::create_amendment`, reason NOT NULL) and re-analyses (`create_reanalysis`,
`interpretations.parent_interpretation_id`, NO reason column), but until
this change no renderer read either: an amended report, the stale original
it replaced, and a re-analysis reissue all rendered byte-for-byte like a
first-ever report. These tests pin the human-approved wording VERBATIM, as
string literals rather than via the module constants, so a later rewording
of a constant fails here instead of silently changing a signed-off text.

Four states, rendered on all three document surfaces (Markdown, full PDF,
short PDF) and flagged in the JSON output:
  1. this report IS an amendment
  2. this report HAS BEEN SUPERSEDED by an amendment
  3. this report IS a re-analysis
  4. this report HAS SINCE BEEN RE-ANALYSED
A document carrying none of them renders no banner at all.
"""

import json
import os
import tempfile
import unittest

from report.clinical_report_builder import build_clinical_report
from report.json_builder import JSONResultBuilder
from report.report_generator import ReportGenerator
from report.summary import generate_pdf
from report.summary_short import generate_short_pdf

try:
    from pypdf import PdfReader

    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False


ORIGINAL_REPORT_ID = "0b6f1c9e-2a41-4d8e-9c55-1e7a3f0d2b11"
AMENDMENT_REPORT_ID = "7d2e4a10-58c3-4f6b-a9e2-3c1d0b8f6e42"
PARENT_INTERPRETATION_ID = "c4a9e7f2-1b3d-4e5f-8a6c-9d0e1f2a3b4c"

# The raw `document["report_revision"]` shape a caller holding the clinical
# database fills in (see `clinical_report_builder.normalize_report_revision`).
AMENDS = {
    "original_report_id": ORIGINAL_REPORT_ID,
    "original_issued_at": "2026-08-01T09:00:00+00:00",
    "reason": "ClinVar reclassified the BRCA1 variant",
    "amended_by": "Dr A. Rao",
    "amended_at": "2026-09-01T10:30:00+00:00",
}
SUPERSEDED_BY = {
    "amendment_report_id": AMENDMENT_REPORT_ID,
    "amended_at": "2026-09-01T10:30:00+00:00",
    "reason": "ClinVar reclassified the BRCA1 variant",
}
REANALYSIS_OF = {
    "parent_interpretation_id": PARENT_INTERPRETATION_ID,
    "parent_interpreted_at": "2026-07-15T04:00:00+00:00",
}
REANALYSED_SINCE = [
    {"interpretation_id": "e1f2a3b4-c5d6-4e7f-8091-a2b3c4d5e6f7", "created_at": "2026-09-05T00:00:00+00:00"},
]

# VERBATIM, as signed off by the human (2026-09-11). Dates are the fixtures
# above rendered in IST, the display zone every other report timestamp uses
# (utils/timezone_utils.py): 09:00Z -> 2026-08-01, 10:30Z -> 16:00 IST,
# 04:00Z on 07-15 -> 2026-07-15.
BANNER_AMENDED = (
    "THIS IS AN AMENDED REPORT. It amends a report originally issued on 2026-08-01. "
    "Reason for amendment: ClinVar reclassified the BRCA1 variant. "
    "Amended by Dr A. Rao on 2026-09-01 16:00 IST."
)
BANNER_SUPERSEDED = (
    "*** THIS REPORT HAS BEEN SUPERSEDED. An amended report was issued on 2026-09-01 16:00 IST for the "
    "reason below; do not act on this document without first obtaining the amended report "
    f"({AMENDMENT_REPORT_ID}). Reason for the amendment: ClinVar reclassified the BRCA1 variant. ***"
)
BANNER_REANALYSIS = (
    f"THIS REPORT IS BASED ON A RE-ANALYSIS of the VCF data underlying interpretation {PARENT_INTERPRETATION_ID}, "
    "originally interpreted on 2026-07-15. The system does not record reasons for re-analysis."
)
BANNER_REANALYSED_SINCE = (
    "One or more re-analyses of the underlying VCF data exist since this report was issued. This report "
    "reflects the original analysis only; it has not been retracted or superseded by the re-analysis "
    "(see spec 15.3 -- re-analysis creates a new branch, it does not replace this one)."
)

ALL_BANNERS = (BANNER_AMENDED, BANNER_SUPERSEDED, BANNER_REANALYSIS, BANNER_REANALYSED_SINCE)

# Distinctive fragments used by the negative controls: none of these may
# appear anywhere in a document that carries no revision state.
_BANNER_MARKERS = (
    "AMENDED REPORT",
    "SUPERSEDED",
    "RE-ANALYSIS",
    "re-analyses of the underlying VCF",
    "does not record reasons for re-analysis",
)


def _document(report_revision=..., evidence_sources=("ClinVar",)):
    doc = {
        "geper_version": "test",
        "generated_at": "2026-09-01T00:00:00+00:00",
        "input_vcf": "x.vcf",
        "assembly": "GRCh38",
        "vcf_samples": ["S1"],
        "variant_count": 1,
        "variants": [
            {
                "variant": {"chrom": "17", "pos": 43106534, "ref": "C", "alt": "A"},
                "interpretation_result": {"gene_symbol": "BRCA1"},
                "candidate_interpretation": build_clinical_report(
                    {
                        "variant": {"chrom": "17", "pos": 43106534, "ref": "C", "alt": "A"},
                        "gene_symbol": "BRCA1",
                        "acmg_classification": "Pathogenic",
                        "evidence_sources": list(evidence_sources),
                    },
                    raw_evidence={},
                ),
                "errors": [],
            }
        ],
    }
    if report_revision is not ...:
        doc["report_revision"] = report_revision
    return doc


def _flat(text):
    # ReportLab wraps long lines; pypdf renders each wrap as "\n". Collapse
    # whitespace so a banner is matched the way a reader reads it.
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


_STATES = {
    "amended": ({"amends": AMENDS}, BANNER_AMENDED),
    "superseded": ({"superseded_by": SUPERSEDED_BY}, BANNER_SUPERSEDED),
    "reanalysis": ({"reanalysis_of": REANALYSIS_OF}, BANNER_REANALYSIS),
    "reanalysed_since": ({"reanalysed_since": REANALYSED_SINCE}, BANNER_REANALYSED_SINCE),
}


class _SurfaceContract:
    """Mixed into one TestCase per surface. `render(doc) -> flattened text`."""

    render = None

    def _assert_only(self, text, expected_banner):
        self.assertIn(expected_banner, text)
        # Each state renders ITS banner and no other one.
        for other in ALL_BANNERS:
            if other != expected_banner:
                self.assertNotIn(other, text)

    def test_amended_report_carries_the_amendment_banner(self):
        revision, banner = _STATES["amended"]
        self._assert_only(type(self).render(_document(revision)), banner)

    def test_superseded_report_carries_the_superseded_banner_with_its_asterisks(self):
        revision, banner = _STATES["superseded"]
        text = type(self).render(_document(revision))
        self._assert_only(text, banner)
        self.assertIn("*** THIS REPORT HAS BEEN SUPERSEDED.", text)

    def test_reanalysis_report_carries_the_reanalysis_banner(self):
        revision, banner = _STATES["reanalysis"]
        text = type(self).render(_document(revision))
        self._assert_only(text, banner)
        # Banner 3 as REVISED: a structural statement about the system, never
        # the per-report "No reason for the re-analysis is recorded".
        self.assertNotIn("No reason for the re-analysis is recorded", text)

    def test_since_reanalysed_report_carries_the_branch_banner(self):
        revision, banner = _STATES["reanalysed_since"]
        self._assert_only(type(self).render(_document(revision)), banner)

    def test_every_state_at_once_renders_all_four_banners_superseded_first(self):
        revision = {
            "amends": AMENDS,
            "superseded_by": SUPERSEDED_BY,
            "reanalysis_of": REANALYSIS_OF,
            "reanalysed_since": REANALYSED_SINCE,
        }
        text = type(self).render(_document(revision))
        positions = [text.find(b) for b in ALL_BANNERS]
        self.assertTrue(all(p >= 0 for p in positions), positions)
        # "Do not act on this document" outranks everything else on the page.
        self.assertEqual(min(positions), text.find(BANNER_SUPERSEDED))

    # --- negative controls ---------------------------------------------------

    def test_no_revision_key_renders_no_banner(self):
        text = type(self).render(_document())
        for marker in _BANNER_MARKERS:
            self.assertNotIn(marker, text)

    def test_revision_block_with_no_state_renders_no_banner(self):
        empty = {"amends": None, "superseded_by": None, "reanalysis_of": None, "reanalysed_since": []}
        text = type(self).render(_document(empty))
        for marker in _BANNER_MARKERS:
            self.assertNotIn(marker, text)

    def test_explicit_null_revision_renders_no_banner(self):
        text = type(self).render(_document(None))
        for marker in _BANNER_MARKERS:
            self.assertNotIn(marker, text)


class TestMarkdownRevisionBanners(_SurfaceContract, unittest.TestCase):
    render = staticmethod(_markdown)

    def test_banner_sits_above_the_findings(self):
        md = ReportGenerator().generate(_document({"amends": AMENDS}))
        banner_at = md.find("THIS IS AN AMENDED REPORT.")
        self.assertGreaterEqual(banner_at, 0)
        self.assertLess(banner_at, md.find("BRCA1"))
        # The review-status banner stays on line 3 (governance tests read
        # it by position); the revision banner goes below it, never above.
        self.assertTrue(md.splitlines()[2].startswith("> **DRAFT"))


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestFullPdfRevisionBanners(_SurfaceContract, unittest.TestCase):
    render = staticmethod(_full_pdf)

    def test_reason_with_markup_characters_is_escaped_not_dropped(self):
        # ReportLab silently deletes "<...>" and mangles "&" -- the reason is
        # free text typed by a person, so it must survive intact.
        amends = {**AMENDS, "reason": "Wrong <HGVS> & gene"}
        self.assertIn("Reason for amendment: Wrong <HGVS> & gene.", _full_pdf(_document({"amends": amends})))


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestShortPdfRevisionBanners(_SurfaceContract, unittest.TestCase):
    render = staticmethod(_short_pdf)


class TestJsonRevisionFlags(unittest.TestCase):
    """The JSON output carries the same state as machine-readable flags."""

    def _built(self, report_revision=...):
        kwargs = {} if report_revision is ... else {"report_revision": report_revision}
        builder = JSONResultBuilder(input_vcf_path="x.vcf", **kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "geper_results.json")
            builder.write(path)
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)

    def test_each_state_sets_exactly_its_flag_and_banner(self):
        flags = ("is_amendment", "is_superseded", "is_reanalysis", "has_been_reanalysed")
        # A plain loop, not subTest: every state must hold for the test to pass.
        for (name, (revision, banner)), flag in zip(_STATES.items(), flags):
            block = self._built(revision)["report_revision"]
            self.assertIs(block[flag], True, name)
            for other in flags:
                if other != flag:
                    self.assertIs(block[other], False, f"{name}: {other}")
            self.assertEqual(block["banners"], [banner], name)

    def test_amendment_block_carries_the_reason_and_traceability_fields(self):
        block = self._built({"amends": AMENDS})["report_revision"]
        self.assertEqual(block["amends"]["reason"], AMENDS["reason"])
        self.assertEqual(block["amends"]["original_report_id"], ORIGINAL_REPORT_ID)
        self.assertEqual(block["amends"]["amended_by"], "Dr A. Rao")

    def test_no_state_is_all_false_and_no_banners(self):
        block = self._built({})["report_revision"]
        self.assertEqual(
            {k: block[k] for k in ("is_amendment", "is_superseded", "is_reanalysis", "has_been_reanalysed")},
            {"is_amendment": False, "is_superseded": False, "is_reanalysis": False, "has_been_reanalysed": False},
        )
        self.assertEqual(block["banners"], [])

    def test_revision_state_not_supplied_is_null_not_false(self):
        # A pipeline run cannot know whether the clinical platform will
        # store it as a re-analysis, so it must not claim "not a
        # re-analysis": unsupplied state is an explicit null.
        document = self._built()
        self.assertIn("report_revision", document)
        self.assertIsNone(document["report_revision"])

    def test_apply_report_revision_rerenders_a_stored_document(self):
        from report.clinical_report_builder import apply_report_revision

        document = apply_report_revision(_document(), {"superseded_by": SUPERSEDED_BY})
        self.assertTrue(document["report_revision"]["is_superseded"])
        self.assertIn(BANNER_SUPERSEDED, _markdown(document))


class TestRevisionInputValidation(unittest.TestCase):
    """An amendment without its reason must never render as a plain report
    or with a blank reason: the reason is NOT NULL in the clinical schema,
    so a block missing it is corrupted input and is refused loudly."""

    def test_amendment_missing_reason_is_refused(self):
        from report.clinical_report_builder import normalize_report_revision

        for missing in ("reason", "original_issued_at", "amended_by", "amended_at"):
            with self.assertRaises(ValueError, msg=missing):
                normalize_report_revision({"amends": {**AMENDS, missing: None}})

    def test_superseded_missing_amended_report_id_is_refused(self):
        from report.clinical_report_builder import normalize_report_revision

        with self.assertRaises(ValueError):
            normalize_report_revision({"superseded_by": {**SUPERSEDED_BY, "amendment_report_id": ""}})

    def test_markdown_refuses_rather_than_rendering_an_amendment_without_its_reason(self):
        with self.assertRaises(ValueError):
            ReportGenerator().generate(_document({"amends": {**AMENDS, "reason": "  "}}))


if __name__ == "__main__":
    unittest.main()
