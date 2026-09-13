"""
w118: the human's rulings R2, R4, R5, R7 and R8 on the revision banners.

The banner WORDING was signed off 2026-09-11 and merged (d28da0a); these are
the five rulings made 2026-09-13 about WHICH FACTS the approved wording is
allowed to stand on. Every assertion below is on rendered banner text (the
same `report_revision_banners()` every renderer prints from), not on the
internal block, so a rule that is satisfied in the data but lost on the way to
the page still fails here.

  R2  "originally issued on" is the RELEASE date. Approved-but-never-released
      shows the APPROVAL date and says "approved", never "issued".
  R4  banner 2's "issued on" uses the same basis, so the two banners cannot
      disagree about what "issued" means.
  R5  several amendments: name the latest, but never print a reason that
      explains a change from a document the reader has never seen -- print the
      count instead.
  R7  a revision record that cannot be read is an ERROR, not a marker and not
      a silent omission.
  R8  a superseded report stays superseded after retention deletes its
      successor, but the banner must not send the reader to obtain it.

The last class, TestTheBannersTheClinicalReaderFeeds, is the RENDER PROOF for
`clinical.data_access.DataAccess.get_report_revision`: the exact dicts that
reader returns, rendered into the signed-off wording. It lives here rather
than in the clinical suite because importing this module from there pulls in
`pipeline.acmg_rules -> pipeline.gnomad -> requests`, which the clinical CI
job does not install. The two halves are joined by
clinical/report_revision_shape.py, which imports nothing and is pinned from
both sides.
"""

import unittest

from report.clinical_report_builder import normalize_report_revision, report_revision_banners

ORIGINAL_REPORT_ID = "0b6f1c9e-2a41-4d8e-9c55-1e7a3f0d2b11"
AMENDMENT_REPORT_ID = "7d2e4a10-58c3-4f6b-a9e2-3c1d0b8f6e42"
PARENT_INTERPRETATION_ID = "c4a9e7f2-1b3d-4e5f-8a6c-9d0e1f2a3b4c"

_AMENDS = {
    "original_report_id": ORIGINAL_REPORT_ID,
    "original_issued_at": "2026-08-01T09:00:00+00:00",
    "reason": "ClinVar reclassified the BRCA1 variant",
    "amended_by": "Dr A. Rao (MCI-12345), Apollo Hospitals",
    "amended_at": "2026-09-01T10:30:00+00:00",
}
_SUPERSEDED = {
    "amendment_report_id": AMENDMENT_REPORT_ID,
    "amended_at": "2026-09-01T10:30:00+00:00",
    "reason": "ClinVar reclassified the BRCA1 variant",
}


def _banners(**revision):
    """Render through the same path every document surface uses."""
    return report_revision_banners({"report_revision": revision})


def _one(**revision):
    banners = _banners(**revision)
    assert len(banners) == 1, banners
    return banners[0]


class TestR2OriginalDateBasis(unittest.TestCase):
    """R2: the RELEASE date is what "originally issued on" means."""

    def test_released_original_says_issued(self):
        text = _one(amends=dict(_AMENDS, original_issued_basis="released"))
        self.assertIn("It amends a report originally issued on 2026-08-01.", text)
        self.assertNotIn("originally approved on", text)

    def test_approved_but_never_released_original_says_approved_not_issued(self):
        # The failure this forbids: printing the approval date under the word
        # "issued", which claims a release that never happened.
        text = _one(amends=dict(_AMENDS, original_issued_basis="approved"))
        self.assertIn("It amends a report originally approved on 2026-08-01.", text)
        self.assertNotIn("originally issued on", text)

    def test_the_two_bases_render_different_sentences(self):
        # Guards against a "cannot-fail" pass: both cases above could be
        # satisfied by one template that happens to contain both phrases.
        released = _one(amends=dict(_AMENDS, original_issued_basis="released"))
        approved = _one(amends=dict(_AMENDS, original_issued_basis="approved"))
        self.assertNotEqual(released, approved)

    def test_omitted_basis_keeps_the_pre_ruling_meaning(self):
        self.assertEqual(
            _one(amends=dict(_AMENDS)),
            _one(amends=dict(_AMENDS, original_issued_basis="released")),
        )


class TestR4AmendmentDateBasisAgrees(unittest.TestCase):
    """R4: banner 2 uses the same release-then-approval basis as banner 1."""

    def test_released_amendment_says_issued(self):
        text = _one(superseded_by=dict(_SUPERSEDED, amended_at_basis="released"))
        self.assertIn("An amended report was issued on 2026-09-01 16:00 IST", text)

    def test_approved_but_never_released_amendment_says_approved(self):
        text = _one(superseded_by=dict(_SUPERSEDED, amended_at_basis="approved"))
        self.assertIn("An amended report was approved on 2026-09-01 16:00 IST", text)
        self.assertNotIn("was issued on", text)

    def test_both_banners_use_the_same_word_for_the_same_basis(self):
        # The point of R4: one document carrying both states must not call the
        # approval date "issued" in one banner and "approved" in the other.
        both = _banners(
            amends=dict(_AMENDS, original_issued_basis="approved"),
            superseded_by=dict(_SUPERSEDED, amended_at_basis="approved"),
        )
        self.assertEqual(len(both), 2)
        joined = " ".join(both)
        self.assertNotIn("issued on", joined)
        self.assertEqual(joined.count("approved on"), 2)


class TestR5SeveralAmendments(unittest.TestCase):
    """R5: never print a reason explaining a change the reader cannot see."""

    def test_single_amendment_still_prints_its_reason(self):
        text = _one(superseded_by=dict(_SUPERSEDED))
        self.assertIn("Reason for the amendment: ClinVar reclassified the BRCA1 variant.", text)

    def test_several_amendments_print_the_count_and_no_reason(self):
        block = {
            "amendment_report_id": AMENDMENT_REPORT_ID,
            "amended_at": "2026-09-01T10:30:00+00:00",
            "amendment_count": 3,
        }
        text = _one(superseded_by=block)
        self.assertIn("It has been amended 3 times", text)
        self.assertIn(AMENDMENT_REPORT_ID, text)
        self.assertNotIn("Reason for the amendment", text)
        self.assertNotIn("ClinVar", text)

    def test_reason_and_count_together_is_refused(self):
        with self.assertRaises(ValueError):
            _banners(superseded_by=dict(_SUPERSEDED, amendment_count=3))

    def test_neither_reason_nor_count_is_refused(self):
        with self.assertRaises(ValueError):
            _banners(
                superseded_by={
                    "amendment_report_id": AMENDMENT_REPORT_ID,
                    "amended_at": "2026-09-01T10:30:00+00:00",
                }
            )

    def test_a_count_of_one_is_refused(self):
        # One amendment must go down the reason path; a count of 1 here would
        # mean the caller had a reason and dropped it.
        with self.assertRaises(ValueError):
            _banners(
                superseded_by={
                    "amendment_report_id": AMENDMENT_REPORT_ID,
                    "amended_at": "2026-09-01T10:30:00+00:00",
                    "amendment_count": 1,
                }
            )


class TestR7FailClosed(unittest.TestCase):
    """R7: an unreadable revision record raises; it never degrades."""

    def test_unrecognised_original_basis_raises(self):
        with self.assertRaises(ValueError):
            _banners(amends=dict(_AMENDS, original_issued_basis="created"))

    def test_unrecognised_amendment_basis_raises(self):
        with self.assertRaises(ValueError):
            _banners(superseded_by=dict(_SUPERSEDED, amended_at_basis="created"))

    def test_a_bad_basis_does_not_fall_back_to_released(self):
        # The specific fail-closed property: no marker text, no silent
        # omission, and above all no quiet default to the released wording.
        try:
            banners = _banners(amends=dict(_AMENDS, original_issued_basis="created"))
        except ValueError:
            return
        self.fail(f"rendered instead of failing closed: {banners}")


class TestR8SuccessorNoLongerRetained(unittest.TestCase):
    """R8: still superseded, but never pointed at as obtainable."""

    def test_says_no_longer_retained_and_does_not_send_the_reader_to_obtain_it(self):
        text = _one(superseded_by=dict(_SUPERSEDED, retained=False))
        self.assertIn("THIS REPORT HAS BEEN SUPERSEDED", text)
        self.assertIn("NO LONGER RETAINED", text)
        self.assertNotIn("without first obtaining", text)
        self.assertNotIn("obtaining the amended report", text)

    def test_still_flagged_superseded(self):
        block = normalize_report_revision({"superseded_by": dict(_SUPERSEDED, retained=False)})
        self.assertTrue(block["is_superseded"])

    def test_retained_true_keeps_the_obtain_instruction(self):
        text = _one(superseded_by=dict(_SUPERSEDED, retained=True))
        self.assertIn("without first obtaining the amended report", text)
        self.assertNotIn("NO LONGER RETAINED", text)

    def test_not_retained_outranks_the_count_wording(self):
        text = _one(
            superseded_by={
                "amendment_report_id": AMENDMENT_REPORT_ID,
                "amended_at": "2026-09-01T10:30:00+00:00",
                "amendment_count": 2,
                "retained": False,
            }
        )
        self.assertIn("NO LONGER RETAINED", text)
        self.assertNotIn("without first obtaining", text)


def _load_revision_shape():
    """
    The contract module `clinical/report_revision_shape.py`, loaded BY PATH.

    By path, not `import clinical.report_revision_shape`, so that loading it
    can never run `clinical/__init__.py` or reach anything that wants psycopg
    -- the geper job installs geper/requirements.txt and has no clinical
    dependencies, exactly as the clinical job has no `requests`. The contract
    module itself imports nothing at all, which is what makes this safe in
    both directions.
    """
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "clinical" / "report_revision_shape.py"
    spec = importlib.util.spec_from_file_location("_w118_revision_shape", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SHAPE = _load_revision_shape()

# The EXACT dicts `DataAccess.get_report_revision` returns, transcribed with
# real values. Every one is checked against SHAPE before it is rendered, so a
# key renamed on the clinical side (which fails clinical/tests/test_w118_
# report_revision_reader.py::TestTheShapeTheRendererConsumes) cannot leave
# this fixture quietly stale here.
READER_AMENDS = {
    "original_report_id": ORIGINAL_REPORT_ID,
    "original_issued_at": "2026-08-01T09:00:00+00:00",
    "original_issued_basis": "released",
    "reason": "ClinVar reclassified the BRCA1 variant",
    "amended_by": "Dr A. Rao (MCI-12345), Apollo Hospitals",
    "amended_at": "2026-09-01T10:30:00+00:00",
}
READER_SUPERSEDED_BY_REASON = {
    "amendment_report_id": AMENDMENT_REPORT_ID,
    "amended_at": "2026-09-01T10:30:00+00:00",
    "amended_at_basis": "approved",
    "retained": True,
    "reason": "ClinVar reclassified the BRCA1 variant",
}
READER_SUPERSEDED_BY_COUNT = {
    "amendment_report_id": AMENDMENT_REPORT_ID,
    "amended_at": "2026-09-01T10:30:00+00:00",
    "amended_at_basis": "released",
    "retained": True,
    "amendment_count": 3,
}
READER_SUPERSEDED_BY_NOT_RETAINED = {
    "amendment_report_id": AMENDMENT_REPORT_ID,
    "amended_at": "2026-09-01T10:30:00+00:00",
    "amended_at_basis": "released",
    "retained": False,
    "reason": "ClinVar reclassified the BRCA1 variant",
}
READER_REANALYSIS_OF = {
    "parent_interpretation_id": PARENT_INTERPRETATION_ID,
    "parent_interpreted_at": "2026-07-15T04:00:00+00:00",
}
READER_REANALYSED_SINCE = [
    {"interpretation_id": "e1f2a3b4-c5d6-4e7f-8091-a2b3c4d5e6f7", "created_at": "2026-09-05T00:00:00+00:00"},
]


class TestTheBannersTheClinicalReaderFeeds(unittest.TestCase):
    """
    THE OTHER HALF OF THE RENDER PROOF (see clinical/tests/test_w118_report_
    revision_reader.py::TestTheShapeTheRendererConsumes).

    The clinical suite proves the reader EMITS this shape from a real
    database; this proves the renderer TURNS this shape into the signed-off
    wording. Splitting it this way is the layering, not a workaround: the
    renderer must keep working for callers with no clinical database, and the
    clinical layer must not need the variant pipeline to run its tests.
    """

    def test_the_fixtures_match_the_contract_the_reader_writes(self):
        # Checked FIRST, and separately: if this drifts, every assertion
        # below is rendering a shape the reader no longer produces, and would
        # otherwise keep passing while the real pipe is broken.
        self.assertEqual(set(READER_AMENDS), set(SHAPE.AMENDS_REQUIRED))
        self.assertEqual(set(READER_REANALYSIS_OF), set(SHAPE.REANALYSIS_OF_REQUIRED))
        for item in READER_REANALYSED_SINCE:
            self.assertEqual(set(item), set(SHAPE.REANALYSED_SINCE_ITEM_REQUIRED))
        for block in (
            READER_SUPERSEDED_BY_REASON,
            READER_SUPERSEDED_BY_COUNT,
            READER_SUPERSEDED_BY_NOT_RETAINED,
        ):
            self.assertTrue(SHAPE.SUPERSEDED_BY_REQUIRED <= set(block))
            self.assertEqual(len(set(block) & SHAPE.SUPERSEDED_BY_EXACTLY_ONE_OF), 1)
            self.assertTrue(set(block) <= SHAPE.SUPERSEDED_BY_REQUIRED | SHAPE.SUPERSEDED_BY_EXACTLY_ONE_OF)
        self.assertIn(READER_AMENDS["original_issued_basis"], SHAPE.DATE_BASES)

    def test_a_superseded_original_renders_the_superseded_banner(self):
        text = _one(superseded_by=READER_SUPERSEDED_BY_REASON)
        self.assertIn("THIS REPORT HAS BEEN SUPERSEDED", text)
        self.assertIn("An amended report was approved on 2026-09-01 16:00 IST", text)
        self.assertIn("Reason for the amendment: ClinVar reclassified the BRCA1 variant.", text)

    def test_an_amendment_renders_the_amended_banner(self):
        text = _one(amends=READER_AMENDS)
        self.assertIn("THIS IS AN AMENDED REPORT", text)
        self.assertIn("originally issued on 2026-08-01", text)
        self.assertIn("Amended by Dr A. Rao (MCI-12345), Apollo Hospitals on 2026-09-01 16:00 IST.", text)

    def test_several_amendments_render_the_count_banner(self):
        text = _one(superseded_by=READER_SUPERSEDED_BY_COUNT)
        self.assertIn("It has been amended 3 times", text)
        self.assertNotIn("Reason for the amendment", text)

    def test_a_deleted_successor_renders_the_not_retained_banner(self):
        text = _one(superseded_by=READER_SUPERSEDED_BY_NOT_RETAINED)
        self.assertIn("NO LONGER RETAINED", text)
        self.assertNotIn("without first obtaining", text)

    def test_a_reanalysis_renders_both_re_analysis_banners(self):
        banners = _banners(reanalysis_of=READER_REANALYSIS_OF, reanalysed_since=READER_REANALYSED_SINCE)
        self.assertEqual(len(banners), 2)
        self.assertIn("THIS REPORT IS BASED ON A RE-ANALYSIS", banners[0])
        self.assertIn("re-analyses of the underlying VCF data exist since this report was issued", banners[1])

    def test_an_empty_reader_result_renders_nothing(self):
        # `get_report_revision` returns {} for a report in no revision state;
        # that must render no banner, not a block of all-false wording.
        self.assertEqual(_banners(), [])


if __name__ == "__main__":
    unittest.main()
