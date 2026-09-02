"""
Acceptance criteria for the SpliceAI-removal + denominator fix ruling.

The human: "fix the denominator so removal doesn't move verdicts." These tests
assert INVARIANCE -- that a given variant's PP3/BP4 verdict is unchanged whether
SpliceAI participates as a voter or not -- not a pinned verdict. A fix that
happened to preserve one specific pinned outcome while still moving some OTHER
variant's verdict would pass a pinned-verdict test; it cannot pass an invariance
test built from a variant that actually exercises the boundary.

Mechanism under test: `pipeline/acmg/classifier.py::AcmgClassifier._pp3`/`_bp4`
both compute `met = n_dam >= max(1, len(votes) // 2 + 1)` (PP3, line ~831) /
`n_ben >= max(1, len(votes) // 2 + 1)` (BP4, line ~1188) -- a majority of
*present* voters. Removing a voter shrinks `len(votes)`, which can shrink the
required majority too -- so removing SpliceAI can flip a verdict even when every
other input is unchanged.

Today (2026-08-21), before the fix: these are RED, for exactly that reason -- the
verdict genuinely moves. Kelly/Meredith build the fix (remove SpliceAI, re-tune
the denominator) against these; they must go GREEN with no other assertion here
changed.
"""

from pipeline.acmg.classifier import AcmgClassifier, VariantEvidence


def _clf() -> AcmgClassifier:
    return AcmgClassifier(cfg={})


def _met(evidence: VariantEvidence, code: str) -> bool:
    return code in _clf().classify(evidence).criteria_met


class TestPp3InvariantToSpliceaiRemoval:
    """PP3 (:831) -- Meredith's two worked examples, both at the real configured
    thresholds (pp3_cadd_phred=20.0, pp3_spliceai=0.2, pp3_revel=0.5,
    pp3_alphamissense=0.564)."""

    def test_non_missense_cadd_damaging_spliceai_not_damaging(self):
        """
        Non-missense variant -- REVEL/AlphaMissense are gated off entirely
        (is_missense=False), so CADD and SpliceAI are the ONLY possible voters.
        cadd_phred=25 (>=20, damaging), spliceai_score=0.05 (<0.2, not damaging).

        TODAY: 1 damaging of 2 needed 2 -> PP3 NOT MET.
        WITHOUT SpliceAI: 1 of 1 needed 1 -> PP3 MET.
        The verdict moves -- must fail today, for exactly that reason.
        """
        with_spliceai = VariantEvidence(cadd_phred=25.0, spliceai_score=0.05, is_missense=False)
        without_spliceai = VariantEvidence(cadd_phred=25.0, spliceai_score=None, is_missense=False)

        met_with = _met(with_spliceai, "PP3")
        met_without = _met(without_spliceai, "PP3")
        assert met_with == met_without, (
            f"PP3 verdict moved when SpliceAI was removed: with SpliceAI met={met_with}, "
            f"without SpliceAI met={met_without}. Removing a voter must not move the verdict once "
            "the denominator is fixed to match."
        )

    def test_missense_cadd_revel_damaging_am_spliceai_not_damaging(self):
        """
        Missense variant, all four voters present. CADD damaging, REVEL
        damaging, AlphaMissense not damaging, SpliceAI not damaging.

        TODAY: 2 of 4 needed 3 -> PP3 NOT MET.
        WITHOUT SpliceAI: 2 of 3 needed 2 -> PP3 MET.
        The verdict moves -- must fail today, for exactly that reason.
        """
        with_spliceai = VariantEvidence(
            cadd_phred=25.0,
            revel_score=0.9,
            alphamissense_score=0.1,
            spliceai_score=0.05,
            is_missense=True,
        )
        without_spliceai = VariantEvidence(
            cadd_phred=25.0,
            revel_score=0.9,
            alphamissense_score=0.1,
            spliceai_score=None,
            is_missense=True,
        )

        met_with = _met(with_spliceai, "PP3")
        met_without = _met(without_spliceai, "PP3")
        assert met_with == met_without, (
            f"PP3 verdict moved when SpliceAI was removed: with SpliceAI met={met_with}, "
            f"without SpliceAI met={met_without}. Removing a voter must not move the verdict once "
            "the denominator is fixed to match."
        )


class TestBp4InvariantToSpliceaiRemoval:
    """BP4 (:1188) -- identical mechanism with n_ben, mirrored (not assumed) at
    the real configured thresholds (bp4_cadd_phred=10.0, bp4_spliceai=0.1,
    bp4_revel=0.15, bp4_alphamissense=0.34). Derived independently from Meredith's
    PP3 examples, not copied: BP4's "vote" direction is < threshold (benign),
    the opposite sense of PP3's >= threshold (damaging)."""

    def test_non_missense_cadd_benign_spliceai_not_benign(self):
        """
        Non-missense variant -- CADD and SpliceAI are the only possible voters.
        cadd_phred=5 (<10, benign), spliceai_score=0.5 (>=0.1, not benign).

        TODAY: 1 benign of 2 needed 2 -> BP4 NOT MET.
        WITHOUT SpliceAI: 1 of 1 needed 1 -> BP4 MET.
        The verdict moves -- must fail today, for exactly that reason.
        """
        with_spliceai = VariantEvidence(cadd_phred=5.0, spliceai_score=0.5, is_missense=False)
        without_spliceai = VariantEvidence(cadd_phred=5.0, spliceai_score=None, is_missense=False)

        met_with = _met(with_spliceai, "BP4")
        met_without = _met(without_spliceai, "BP4")
        assert met_with == met_without, (
            f"BP4 verdict moved when SpliceAI was removed: with SpliceAI met={met_with}, "
            f"without SpliceAI met={met_without}. Removing a voter must not move the verdict once "
            "the denominator is fixed to match."
        )

    def test_missense_cadd_revel_benign_am_spliceai_not_benign(self):
        """
        Missense variant, all four voters present. CADD benign, REVEL benign,
        AlphaMissense not benign, SpliceAI not benign.

        TODAY: 2 of 4 needed 3 -> BP4 NOT MET.
        WITHOUT SpliceAI: 2 of 3 needed 2 -> BP4 MET.
        The verdict moves -- must fail today, for exactly that reason.
        """
        with_spliceai = VariantEvidence(
            cadd_phred=5.0,
            revel_score=0.05,
            alphamissense_score=0.9,
            spliceai_score=0.5,
            is_missense=True,
        )
        without_spliceai = VariantEvidence(
            cadd_phred=5.0,
            revel_score=0.05,
            alphamissense_score=0.9,
            spliceai_score=None,
            is_missense=True,
        )

        met_with = _met(with_spliceai, "BP4")
        met_without = _met(without_spliceai, "BP4")
        assert met_with == met_without, (
            f"BP4 verdict moved when SpliceAI was removed: with SpliceAI met={met_with}, "
            f"without SpliceAI met={met_without}. Removing a voter must not move the verdict once "
            "the denominator is fixed to match."
        )
