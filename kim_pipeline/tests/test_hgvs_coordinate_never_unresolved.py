"""A ``c.``/``n.`` coordinate is never rendered from a value the guard
did not establish.

*** WHAT THIS PINS, AND IT IS NOT "None IS NOT PRINTED". *** The property
is that when ``_build_hgvs`` commits to transcript-relative notation, the
position it prints came from ``cds_pos``/``end_cds_pos`` -- values
``use_transcript_coords`` has already established are present. The
substring ``"None"`` is only the *symptom this instance would produce*; a
future change that rendered an empty string, or a ``0``, or a float where
a coordinate belongs would leave the substring check green and the
property broken. So the assertions below are on the SHAPE of every
rendered coordinate, and the ``"None"`` check is kept as the cheap,
legible one on top.

=== WHY THIS FILE EXISTS: THE INSTRUMENT POINTED AT THE WRONG LINES ===

mypy reports five ``Optional``-flowing sites in ``_build_hgvs``
(``transcript_id.upper()``, and four ``use_pos + ...`` arithmetic sites).
*** ALL FIVE WERE TRIAGED 2026-09-10 AND ALL FIVE ARE UNREACHABLE ***:
each sits behind ``use_transcript_coords``, and mypy cannot narrow an
``Optional`` across an intermediate boolean variable. They are one
finding, not five, and none of them is a defect.

*** THE SITE THAT WOULD ACTUALLY HARM A READER IS THE ONE MYPY DOES NOT
FLAG: the SNV return, ``f"{ref_prefix}{use_pos}{disp_ref}>{disp_alt}"``.
An f-string interpolates ``None`` perfectly happily -- there is no
operator, so there is no type error to report. *** With the guard broken,
the five flagged sites RAISE (loud, traceable, a failed run); the
unflagged one returns the string ``NM_000059.4:c.NoneA>T`` -- a coding
coordinate, formatted and printed with no qualifier beside it, in a
report that otherwise looks complete. SNVs are the common case, so the
unflagged site is also the likeliest.

*** A STATIC-ANALYSIS TOOL RANKS BY WHAT BREAKS THE PROGRAM. A CLINICAL
PIPELINE MUST RANK BY WHAT REACHES THE READER. THOSE ARE DIFFERENT
ORDERINGS. *** This file pins the second one, because nothing did: the
nearest existing test (``test_annotation_stage.py``, "never pair a c.
prefix with the raw genomic position") pins a DIFFERENT property, which
is worse than no test there at all -- it makes the slot look occupied.

=== THE RED WAS WATCHED, NOT ASSUMED ===
Both directions were driven before this file was committed, by editing
``_build_hgvs``'s guard and re-running:
  - neutering the ``cds_pos`` conjunct  -> 24 outputs like ``c.NoneA>T``
  - inverting the ``transcript_id`` conjunct -> 16 AttributeErrors
Both edits were reverted. A test whose red has not been seen is a test
its author is hoping about.
"""

import itertools
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.annotation.stage import _build_hgvs

# The whole domain the signature permits. `pos` is `int` (not Optional)
# at the signature, on the dataclass, and at the single production call
# site, so it is not varied here; every OTHER parameter that can be None
# is.
_REF_ALT = [
    ("A", "T"),  # SNV
    ("A", "AGG"),  # insertion
    ("ATG", "A"),  # deletion
    ("ATG", "GC"),  # delins, ref longer
    ("AT", "GCC"),  # delins, alt longer
]
_TRANSCRIPTS = [None, "NM_000059.4", "NR_003051.3"]
_CDS_POS = [None, 50]
_END_CDS_POS = [None, 52]
_STRANDS = [None, "+", "-"]

# Every coordinate slot the renderer can emit, in every shape it emits.
# A coordinate is a non-empty run of digits and nothing else -- this is
# what "rendered from an established value" means in observable terms.
_COORD = r"\d+"
_BASES = r"[ACGTN]+"
_PREFIX = r"(?:[\w.]+:[cn]\.|[\w]+:g\.)"
_RENDERED = re.compile(
    "^"
    + _PREFIX
    + "(?:"
    + rf"{_COORD}{_BASES}>{_BASES}"  # substitution
    + rf"|{_COORD}_{_COORD}ins{_BASES}"  # insertion
    + rf"|{_COORD}del"  # single-base deletion
    + rf"|{_COORD}_{_COORD}del"  # multi-base deletion
    + rf"|{_COORD}_{_COORD}delins{_BASES}"  # delins
    + rf"|{_COORD}delins{_BASES}"  # single-base delins
    + ")$"
)


def _drive_declared_domain():
    """Yield (inputs, output) for every combination the signature allows."""
    for (ref, alt), tid, cds, end_cds, strand in itertools.product(
        _REF_ALT, _TRANSCRIPTS, _CDS_POS, _END_CDS_POS, _STRANDS
    ):
        inputs = (ref, alt, tid, cds, end_cds, strand)
        yield inputs, _build_hgvs("chr1", 100, ref, alt, tid, cds, end_cds, strand)


class TheDriveActuallyReachesEveryRenderedShape(unittest.TestCase):
    """*** THE PRECONDITION, AND IT IS THE ONE MOST EASILY SKIPPED:
    A ZERO-FAILURE RESULT OVER CODE THAT NEVER RAN IS VACUOUS, AND IS
    INDISTINGUISHABLE FROM A REAL PASS. ***

    Asserted by BEHAVIOUR rather than by line number, deliberately: line
    numbers pinned to no revision claim to be about HEAD forever, and
    this file's whole subject is a claim that went stale. If a future
    change stops producing one of these shapes, this fails loudly rather
    than quietly shrinking the domain the assertions below cover.
    """

    def test_every_rendered_shape_is_produced_at_least_once(self):
        rendered = {out for _, out in _drive_declared_domain()}

        # transcript-relative, both flavours
        self.assertTrue(any(":c." in o and o.endswith((">T", ">A")) for o in rendered))
        self.assertTrue(any(":n." in o for o in rendered))
        # every structural shape
        self.assertTrue(any("ins" in o and "delins" not in o for o in rendered))
        self.assertTrue(any(o.endswith("del") for o in rendered))
        self.assertTrue(any("delins" in o for o in rendered))
        # the genomic fallback, which is CORRECT behaviour and must keep
        # happening -- this class asserts coverage, not that g. is a fault
        self.assertTrue(any(":g." in o for o in rendered))

    def test_the_domain_is_not_empty_or_trivially_small(self):
        driven = list(_drive_declared_domain())
        self.assertEqual(len(driven), 180)


class ACoordinateIsNeverRenderedFromAnUnestablishedValue(unittest.TestCase):
    """*** THE PROPERTY. Not "None is absent" -- that is one symptom. ***"""

    def test_every_rendered_coordinate_is_a_bare_integer(self):
        """The shape assertion, which is the real one.

        A position slot that came from a value the guard did not
        establish cannot render as a run of digits -- it renders as
        ``None``, or empty, or something else entirely. Pinning the shape
        catches all of those; pinning the substring catches only today's.
        """
        for inputs, out in _drive_declared_domain():
            with self.subTest(inputs=inputs):
                self.assertRegex(out, _RENDERED, f"unrenderable coordinate in {out!r}")

    def test_no_output_ever_contains_the_string_none(self):
        """The cheap, legible check, kept on top of the shape assertion.

        *** THIS IS THE EXACT ASSERTION THAT GOES RED WHEN THE GUARD AT
        `use_transcript_coords` BREAKS -- watched, not assumed: neutering
        the `cds_pos` conjunct turns 24 of these 180 outputs into
        `NM_000059.4:c.NoneA>T` and the like. ***
        """
        for inputs, out in _drive_declared_domain():
            with self.subTest(inputs=inputs):
                self.assertNotIn("None", out)

    def test_a_transcript_prefix_always_carries_a_transcript_coordinate(self):
        """The two halves must agree.

        If the output committed to ``c.``/``n.`` then every position it
        shows must be DERIVED FROM the transcript-relative coordinate the
        caller supplied, never from the genomic position passed alongside
        it. (The converse -- that a ``c.`` prefix is never paired with
        the raw genomic position -- is pinned in
        ``test_annotation_stage.py``; this is the same coin, asserted
        from the coordinate's side.)

        *** "DERIVED FROM", NOT "EQUAL TO", AND THE DIFFERENCE IS REAL
        HGVS: a deletion anchors on ``cds_pos`` but PRINTS ``cds_pos +
        1`` as its first deleted base, so ``cds_pos=50`` with ``ref=ATG``
        correctly renders ``c.51_52del``. An earlier draft of this test
        asserted equality, went red on exactly those four cases, and was
        WRONG -- the code was right. Recorded because the corrected
        assertion is the weaker-looking one, and a later reader would
        otherwise be tempted to "tighten" it back into a false claim. ***
        """
        for inputs, out in _drive_declared_domain():
            ref, alt, tid, cds, end_cds, strand = inputs
            if ":c." not in out and ":n." not in out:
                continue
            with self.subTest(inputs=inputs):
                self.assertIsNotNone(cds)
                shown = [int(m) for m in re.findall(r"\d+", out.split(":", 1)[1])]
                self.assertTrue(shown, "a transcript prefix with no coordinate at all")
                for coord in shown:
                    # inside the window the supplied transcript coordinate
                    # spans -- and provably not the genomic position, which
                    # is 100 and is 50 away from any legitimate value here
                    self.assertGreaterEqual(coord, cds)
                    self.assertLessEqual(coord, cds + len(ref))
                    self.assertNotEqual(coord, 100)


class TheGenomicFallbackIsStillReachedAndIsNotAFailure(unittest.TestCase):
    """*** A GATE THAT ALWAYS FIRES AND ONE THAT NEVER FIRES LOOK
    IDENTICAL FROM ONE SIDE. *** This is the other side: the absence of a
    transcript coordinate must produce ``g.`` notation, not an error and
    not a fabricated ``c.``.
    """

    def test_no_transcript_coordinate_gives_genomic_notation(self):
        out = _build_hgvs("chr1", 100, "A", "T", "NM_000059.4", cds_pos=None)
        self.assertIn(":g.", out)
        self.assertNotIn(":c.", out)
        self.assertIn("100", out)

    def test_no_transcript_id_gives_genomic_notation(self):
        out = _build_hgvs("chr1", 100, "A", "T", None, cds_pos=50)
        self.assertIn(":g.", out)
        self.assertNotIn(":c.", out)


if __name__ == "__main__":
    unittest.main()
