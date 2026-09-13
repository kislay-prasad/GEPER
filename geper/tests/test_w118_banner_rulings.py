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
"""

import unittest

from report.clinical_report_builder import normalize_report_revision, report_revision_banners

ORIGINAL_REPORT_ID = "0b6f1c9e-2a41-4d8e-9c55-1e7a3f0d2b11"
AMENDMENT_REPORT_ID = "7d2e4a10-58c3-4f6b-a9e2-3c1d0b8f6e42"

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


if __name__ == "__main__":
    unittest.main()
