"""
Second instance of the SpliceAI-removal defect shape, found while
implementing the ruled removal (`pipeline/vep/stage.py`'s plugin list +
plumbing) -- distinct from the PP3/BP4 majority-vote fix
(`test_pp3_bp4_spliceai_removal_invariance.py`, Kelly's/classifier.py's
half) and not covered by it.

Mechanism: `pipeline/orchestration/shared.py::run_acmg_evidence_batch`
builds `VariantEvidence.synonymous_or_intronic` as:

    consequence in {"synonymous_variant", "intron_variant"}
    and (variant.get("spliceai_score") or 0.0) < 0.2

`(x or 0.0)` treats an ABSENT spliceai_score identically to a CONFIRMED
"no splice impact" score of 0.0 -- so a missing annotation silently
satisfies BP7's "no predicted splice impact" condition
(`pipeline/acmg/classifier.py::_bp7`, `met = e.synonymous_or_intronic`)
rather than leaving it unconfirmed. This is not new today -- any VEP
annotation gap already triggers it intermittently -- but removing the
SpliceAI plugin entirely (per the ruling: licence-driven, same class as
OMIM) makes `spliceai_score` ALWAYS absent going forward, which turns an
intermittent false-benign into a universal one: every synonymous/intronic
variant would satisfy BP7 unconditionally, regardless of actual splice
risk.

Fail-safe direction, matching every other "missing evidence" convention
in this codebase (PP3/BP4's own `if not votes: NOT_EVALUATED`): absence
of a splice-impact score must NOT default to "confirmed no impact." The
fix removes the `or 0.0` fallback so `synonymous_or_intronic` requires an
actually-present score to ever be True -- once SpliceAI's plumbing is
gone, this correctly collapses to always False (BP7 stops firing on
this signal at all) rather than always True.

Today (pre-fix): RED for exactly that reason -- an absent spliceai_score
already reads as "confirmed no splice impact."
"""

from unittest.mock import patch

from pipeline.acmg.classifier import AcmgClassifier, VariantEvidence
from pipeline.orchestration.shared import run_acmg_evidence_batch


def _build_evidence(variant: dict) -> VariantEvidence:
    captured: dict[str, VariantEvidence] = {}
    real_classify = AcmgClassifier.classify

    def _spy(self, evidence):
        captured["evidence"] = evidence
        return real_classify(self, evidence)

    cfg = {"gnomad": {"enabled": False}, "clinvar": {"enabled": False}, "vep": {"enabled": False}}
    with patch.object(AcmgClassifier, "classify", _spy):
        run_acmg_evidence_batch([variant], cfg, sample_id="TESTSAMPLE")
    return captured["evidence"]


class TestSynonymousOrIntronicRequiresAConfirmedSpliceaiScore:
    def test_absent_spliceai_score_does_not_satisfy_no_splice_impact(self):
        """A synonymous variant with NO spliceai_score key at all (the
        real post-removal shape: VEP never emits the field once the
        plugin is gone) must NOT be treated as splice-safe -- absence is
        not confirmation."""
        variant = {
            "chrom": "1",
            "pos": 100,
            "ref": "A",
            "alt": "G",
            "consequence": "synonymous_variant",
        }
        evidence = _build_evidence(variant)
        assert evidence.synonymous_or_intronic is False, (
            "synonymous_or_intronic was True with no spliceai_score present -- an absent "
            "splice-impact score is being treated as a confirmed 'no impact' result, which "
            "makes BP7 fire unconditionally for every synonymous/intronic variant once "
            "SpliceAI is removed."
        )

    def test_confirmed_low_spliceai_score_still_satisfies_it(self):
        """The positive case must survive the fix: an ACTUALLY-present,
        genuinely-low score still confirms no splice impact (this
        exercises the case where an upstream annotator other than the
        removed VEP plugin -- or a future non-SpliceAI splice predictor
        -- legitimately supplies the field)."""
        variant = {
            "chrom": "1",
            "pos": 200,
            "ref": "C",
            "alt": "T",
            "consequence": "intron_variant",
            "spliceai_score": 0.01,
        }
        evidence = _build_evidence(variant)
        assert evidence.synonymous_or_intronic is True

    def test_confirmed_high_spliceai_score_still_fails_it(self):
        """A genuinely damaging score must still block BP7 -- the fix
        only changes what ABSENCE means, not what a real score means."""
        variant = {
            "chrom": "1",
            "pos": 300,
            "ref": "G",
            "alt": "A",
            "consequence": "synonymous_variant",
            "spliceai_score": 0.9,
        }
        evidence = _build_evidence(variant)
        assert evidence.synonymous_or_intronic is False

    def test_non_synonymous_intronic_consequence_unaffected(self):
        """A missense variant must never set this flag regardless of
        spliceai_score -- unrelated to the absence-handling fix, pinned
        so the fix doesn't accidentally widen the consequence gate."""
        variant = {
            "chrom": "1",
            "pos": 400,
            "ref": "A",
            "alt": "T",
            "consequence": "missense_variant",
        }
        evidence = _build_evidence(variant)
        assert evidence.synonymous_or_intronic is False
