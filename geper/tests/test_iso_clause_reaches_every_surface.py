"""
ISO 7.4.1.6 i)'s SECOND assertion must reach every rendered surface, not just
the constant.

*** THE GAP THIS CLOSES IS THIS MORNING'S DEFECT ONE CLAUSE OVER, IN THE SAME
SENTENCE. *** The "and"/"or" fix was defended by eight pins that all asserted
the FIRST half of the ISO element -- "research or development programme". The
SECOND half, "no specific claims on measurement performance are available", is
rendered on four surfaces and was pinned on NONE of them. Truncate the sentence
at a renderer tomorrow and every existing guard still passes: they cover the
half that was already correct.

That is not hypothetical here, for two specific reasons:
  1. The constant is ASSEMBLED FROM TWO ADJACENT STRING LITERALS, so the
     phrase has no contiguous existence in the source that defines it. An edit
     can delete the second literal without touching the first.
  2. The sentence WRAPS in the PDF ("...no specific claims on\\nmeasurement
     performance are available."), so the tail lives on its own line.
Both are places a future change loses the tail while the head, and every pin
guarding the head, stays green.

WHITESPACE IS NORMALISED BEFORE MATCHING, AND THAT IS NOT A CONVENIENCE. A
contiguous-substring check against rendered output IS ITSELF A PROXY: the first
probe written for this file reported the phrase ABSENT from the full PDF, and
it was present -- broken by a line wrap mid-phrase. Matching raw rendered text
would have re-introduced exactly the false negative this file exists to prevent.

PINNED AGAINST ISO, NOT AGAINST THE CONSTANT. The phrase below is transcribed
from the standard. Importing `ISO_RESEARCH_ELEMENT` and asserting the surfaces
contain it would only prove the surfaces agree with the code -- it could never
say the code was right, which is how the paraphrase survived eight pins.

Test helpers `_document` / `_all_pdf_text` are reused from
`test_disclaimer_consistency.py` rather than duplicated, so a change to the
sample document cannot make these two files disagree about what was rendered.
"""

import os
import re
import tempfile
import unittest

from tests.test_disclaimer_consistency import _all_pdf_text, _document

#: ISO 15189:2022 7.4.1.6 i), transcribed from the clause -- NOT read from the
#: constant under test.
ISO_ASSERTION_B = "no specific claims on measurement performance are available"


def _flatten(text: str) -> str:
    """Collapse every whitespace run to a single space.

    Rendered surfaces wrap, indent and re-flow prose; none of that changes what
    the reader is told. Matching raw text would fail on a line break inside the
    phrase -- a false negative about a clinical report.
    """
    return re.sub(r"\s+", " ", text)


class ISOAssertionBReachesEverySurfaceTest(unittest.TestCase):
    """One test per rendered surface. Four surfaces is four chances to lose it."""

    def test_short_pdf_signoff_block(self):
        from reportlab.platypus import Paragraph

        from report.summary_short import _build_short_stylesheet, _build_signoff_block

        rendered = " ".join(
            flowable.text
            for flowable in _build_signoff_block(_build_short_stylesheet())
            if isinstance(flowable, Paragraph)
        )
        self.assertIn(ISO_ASSERTION_B, _flatten(rendered))

    def test_markdown_report(self):
        from report.report_generator import ReportGenerator

        self.assertIn(ISO_ASSERTION_B, _flatten(ReportGenerator().generate(_document())))

    def test_clinical_report_builder_output(self):
        from report.clinical_report_builder import build_clinical_report

        self.assertIn(ISO_ASSERTION_B, _flatten(repr(build_clinical_report(_document()))))

    def test_full_pdf(self):
        """The surface where the phrase demonstrably wraps mid-sentence."""
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            from report.summary import generate_pdf

            generate_pdf(_document(), out)
            rendered = _flatten(_all_pdf_text(out))

        self.assertIn(ISO_ASSERTION_B, rendered)

    def test_raw_matching_would_miss_most_occurrences_in_the_full_pdf(self):
        """
        *** THE STATED, CHECKABLE REASON `_flatten` EXISTS. *** Without this,
        the normalisation looks like ceremony and the next reader deletes it.

        MEASURED, NOT ASSUMED: the ISO element is rendered THREE times in the
        full PDF and only ONE of those occurrences is contiguous -- the other
        two are broken by a line wrap inside the phrase. So a raw substring
        check does not merely risk a false negative; IT WOULD HAVE PASSED ON
        THE ONE CONTIGUOUS OCCURRENCE WHILE BEING BLIND TO THE OTHER TWO, which
        is worse than failing, because it reads as coverage.

        Asserted as a strict inequality rather than as fixed counts, so that
        adding or removing a rendering of the disclaimer does not make this
        test wrong -- only removing the WRAP would, and that is the condition
        it is here to detect.
        """
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            from report.summary import generate_pdf

            generate_pdf(_document(), out)
            raw = _all_pdf_text(out)

        self.assertGreater(
            _flatten(raw).count(ISO_ASSERTION_B),
            raw.count(ISO_ASSERTION_B),
            "the full PDF no longer breaks this phrase across lines anywhere -- _flatten may be "
            "removable here, but verify every other surface before touching it",
        )


if __name__ == "__main__":
    unittest.main()
