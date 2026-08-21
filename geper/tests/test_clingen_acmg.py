"""
Tests for the ClinGen -> ACMG evidence contribution added to
`pipeline/interpretation.py::InterpretationEngine._clingen_acmg_evidence`
(requirement #5: "ClinGen must contribute evidence ... Never overwrite
existing ACMG evidence. Only add evidence. All thresholds/configuration
must remain configurable.").

Mirrors `tests/test_gnomad_acmg.py`'s approach: exercises the real,
non-mocked static method directly with plain evidence dicts, only
patching `CONFIG.clingen`'s thresholds.
"""

import unittest
from unittest import mock

import pytest

from pipeline.clingen.models import DOSAGE_SCORE_LABELS
from pipeline.interpretation import InterpretationEngine


class _FakeClinGenConfig:
    DOSAGE_SUFFICIENT_EVIDENCE_SCORES = frozenset({3})
    DOSAGE_UNLIKELY_SCORE = 40
    DOSAGE_AUTOSOMAL_RECESSIVE_SCORE = 30


def _clingen_result(**overrides):
    base = {
        "skipped": False,
        "found": True,
        "error": None,
        "gene_symbol": "BRCA1",
        "gene_disease_validity": [],
        "clinical_validity_summary": None,
        "expert_panel": None,
        "dosage_sensitivity": None,
    }
    base.update(overrides)
    return base


class TestClinGenAcmgEvidence(unittest.TestCase):
    def setUp(self):
        self.engine = InterpretationEngine()
        self.patcher = mock.patch("pipeline.interpretation.CONFIG")
        fake_config = self.patcher.start()
        fake_config.clingen = _FakeClinGenConfig()
        self.addCleanup(self.patcher.stop)

    # -- gating -----------------------------------------------------

    def test_none_result_contributes_no_evidence(self):
        self.assertEqual(self.engine._clingen_acmg_evidence(None, is_predicted_lof=True), [])

    def test_skipped_result_contributes_no_evidence(self):
        self.assertEqual(self.engine._clingen_acmg_evidence({"skipped": True}, is_predicted_lof=True), [])

    def test_errored_result_contributes_no_evidence(self):
        result = _clingen_result(error="ClinGen API unreachable")
        self.assertEqual(self.engine._clingen_acmg_evidence(result, is_predicted_lof=True), [])

    def test_not_found_contributes_no_evidence(self):
        result = _clingen_result(found=False)
        self.assertEqual(self.engine._clingen_acmg_evidence(result, is_predicted_lof=True), [])

    def test_found_but_no_curation_data_contributes_nothing(self):
        result = _clingen_result(found=True)
        self.assertEqual(self.engine._clingen_acmg_evidence(result, is_predicted_lof=True), [])

    # -- PVS1 / dosage sensitivity ------------------------------------

    def test_sufficient_haploinsufficiency_supports_pvs1_for_lof_variant(self):
        result = _clingen_result(
            dosage_sensitivity={
                "haploinsufficiency_score": 3,
                "haploinsufficiency_label": "Sufficient evidence for dosage pathogenicity",
            }
        )
        evidence = self.engine._clingen_acmg_evidence(result, is_predicted_lof=True)
        self.assertEqual(len(evidence), 1)
        text, weight = evidence[0]
        self.assertIn("PVS1", text)
        self.assertGreater(weight, 0)

    def test_sufficient_haploinsufficiency_does_not_fire_for_non_lof_variant(self):
        result = _clingen_result(
            dosage_sensitivity={"haploinsufficiency_score": 3, "haploinsufficiency_label": "Sufficient evidence"}
        )
        evidence = self.engine._clingen_acmg_evidence(result, is_predicted_lof=False)
        self.assertEqual(evidence, [])

    def test_dosage_unlikely_cautions_against_pvs1_for_lof_variant(self):
        result = _clingen_result(
            dosage_sensitivity={
                "haploinsufficiency_score": 40,
                "haploinsufficiency_label": "Dosage sensitivity unlikely",
            }
        )
        evidence = self.engine._clingen_acmg_evidence(result, is_predicted_lof=True)
        self.assertEqual(len(evidence), 1)
        text, weight = evidence[0]
        self.assertIn("caution", text.lower())
        self.assertLess(weight, 0)

    def test_dosage_autosomal_recessive_score_is_not_silent_and_carries_no_haploinsufficiency_claim(self):
        """THE CASE THIS FILE HAD ZERO COVERAGE OF: score 30 (57e561c).
        Must NOT assert `evidence == []` -- that would re-pin the
        collapse this fix removed (Kelly's own warning: silence here
        would make 'curated autosomal recessive' indistinguishable
        from 'no curation at all'). Score 30 emits exactly one entry,
        weight 0.0, and its text must NEVER contain the sufficient-
        evidence sentence fragment -- that fragment appearing here,
        with an interpolated 'autosomal recessive' label, was the live
        self-refuting sentence this dispatch exists to prevent."""
        result = _clingen_result(
            dosage_sensitivity={
                "haploinsufficiency_score": 30,
                "haploinsufficiency_label": "Gene associated with autosomal recessive phenotype",
            }
        )
        evidence = self.engine._clingen_acmg_evidence(result, is_predicted_lof=True)
        self.assertEqual(len(evidence), 1)
        text, weight = evidence[0]
        self.assertEqual(weight, 0.0)
        self.assertNotIn("sufficient curated evidence for haploinsufficiency", text)
        self.assertNotIn("supporting a PVS1-style interpretation", text)
        self.assertIn("does not establish", text)
        self.assertIn("single loss-of-function allele", text)

    def test_dosage_autosomal_recessive_score_does_not_fire_for_non_lof_variant(self):
        result = _clingen_result(
            dosage_sensitivity={
                "haploinsufficiency_score": 30,
                "haploinsufficiency_label": "Gene associated with autosomal recessive phenotype",
            }
        )
        evidence = self.engine._clingen_acmg_evidence(result, is_predicted_lof=False)
        self.assertEqual(evidence, [])

    def test_score_not_in_the_sufficient_set_and_not_a_special_code_contributes_nothing(self):
        result = _clingen_result(
            dosage_sensitivity={"haploinsufficiency_score": 1, "haploinsufficiency_label": "Little evidence"}
        )
        evidence = self.engine._clingen_acmg_evidence(result, is_predicted_lof=True)
        self.assertEqual(evidence, [])

    def test_threshold_is_configurable(self):
        self.engine  # keep reference; reconfigure the set to exclude the score provided
        with mock.patch("pipeline.interpretation.CONFIG") as fake_config:
            fake_config.clingen = _FakeClinGenConfig()
            fake_config.clingen.DOSAGE_SUFFICIENT_EVIDENCE_SCORES = frozenset({99})  # unreachable qualifying score
            result = _clingen_result(
                dosage_sensitivity={"haploinsufficiency_score": 3, "haploinsufficiency_label": "x"}
            )
            evidence = self.engine._clingen_acmg_evidence(result, is_predicted_lof=True)
        self.assertEqual(evidence, [])

    # -- gene-disease clinical validity (PP5/BP6-style) -----------------

    def test_definitive_classification_supports(self):
        result = _clingen_result(clinical_validity_summary="Definitive", expert_panel="Hereditary Cancer GCEP")
        evidence = self.engine._clingen_acmg_evidence(result, is_predicted_lof=False)
        self.assertEqual(len(evidence), 1)
        text, weight = evidence[0]
        self.assertIn("Definitive", text)
        self.assertGreater(weight, 0)

    def test_strong_classification_supports(self):
        result = _clingen_result(clinical_validity_summary="Strong")
        evidence = self.engine._clingen_acmg_evidence(result, is_predicted_lof=False)
        text, weight = evidence[0]
        self.assertGreater(weight, 0)

    def test_disputed_classification_argues_against(self):
        result = _clingen_result(clinical_validity_summary="Disputed")
        evidence = self.engine._clingen_acmg_evidence(result, is_predicted_lof=False)
        text, weight = evidence[0]
        self.assertIn("Disputed", text)
        self.assertLess(weight, 0)

    def test_refuted_classification_argues_against(self):
        result = _clingen_result(clinical_validity_summary="Refuted")
        evidence = self.engine._clingen_acmg_evidence(result, is_predicted_lof=False)
        text, weight = evidence[0]
        self.assertLess(weight, 0)

    def test_limited_classification_is_neutral(self):
        result = _clingen_result(clinical_validity_summary="Limited")
        evidence = self.engine._clingen_acmg_evidence(result, is_predicted_lof=False)
        text, weight = evidence[0]
        self.assertEqual(weight, 0.0)

    # -- both contributions can fire together --------------------------

    def test_pvs1_and_clinical_validity_both_contribute(self):
        result = _clingen_result(
            clinical_validity_summary="Definitive",
            dosage_sensitivity={"haploinsufficiency_score": 3, "haploinsufficiency_label": "Sufficient evidence"},
        )
        evidence = self.engine._clingen_acmg_evidence(result, is_predicted_lof=True)
        self.assertEqual(len(evidence), 2)
        total_weight = sum(w for _, w in evidence)
        self.assertGreater(total_weight, 0)


# -- full-scale coverage, parametrized over the real ClinGen codes -----------


_SUFFICIENT_EVIDENCE_FRAGMENT = "sufficient curated evidence for haploinsufficiency"


def _evidence_for_score(score):
    engine = InterpretationEngine()
    result = _clingen_result(
        dosage_sensitivity={
            "haploinsufficiency_score": score,
            "haploinsufficiency_label": DOSAGE_SCORE_LABELS.get(score, "unknown"),
        }
    )
    with mock.patch("pipeline.interpretation.CONFIG") as fake_config:
        fake_config.clingen = _FakeClinGenConfig()
        return engine._clingen_acmg_evidence(result, is_predicted_lof=True)


class TestDosageSensitivityAcrossTheFullClinGenScale:
    """
    This file previously contained ZERO occurrences of '30' -- the
    exact score that was broken (57e561c, the geper-side twin of
    [[clingen-dosage-score-40-reads-as-sufficient-evidence-for-lof]]).
    Parametrized over every key in DOSAGE_SCORE_LABELS
    (pipeline/clingen/models.py) plus None, per Kelly's own
    suggestion: a test enumerating only 30 has the same blind spot a
    blacklist-shaped fix would have -- enumerating the real labels
    means a future ClinGen code can't enter untested. Not testing by
    arithmetic/ordering (no `score - 1` "just below" case): the scale
    is not ordinal above 3, so there is no "below" any more.

    ASSERTS THE EMITTED TEXT, NOT ONLY THE WEIGHT, per the dispatch's
    explicit instruction: a weight-only test would have passed on a
    report sentence that refuted itself using its own interpolated
    label ('has sufficient curated evidence for haploinsufficiency
    (Gene associated with autosomal recessive phenotype)'). Every case
    below checks that the sufficient-evidence sentence fragment
    appears if and only if the weight is +1.5, so no future regression
    can reintroduce that sentence for a score that isn't genuinely
    sufficient even if it happens to get the weight right.

    NOT testing geper/pipeline/pvs1/utils.py's separate mapping of
    score 30 to LOF_ESTABLISHED_RECESSIVE, which is correct on its own
    terms -- it asks "is LoF a disease mechanism for this gene at
    all", not this module's "does losing ONE allele suffice". No
    assertion here should ever be changed to 30 -> support on the
    strength of that unrelated module.
    """

    @pytest.mark.parametrize("score", sorted(DOSAGE_SCORE_LABELS), ids=lambda s: f"score_{s}")
    def test_weight_and_text_match_expectations_for_every_known_code(self, score):
        evidence = _evidence_for_score(score)

        if score == 3:
            assert len(evidence) == 1
            text, weight = evidence[0]
            assert weight == 1.5
            assert _SUFFICIENT_EVIDENCE_FRAGMENT in text
        elif score == 40:
            assert len(evidence) == 1
            text, weight = evidence[0]
            assert weight == -0.5
            assert _SUFFICIENT_EVIDENCE_FRAGMENT not in text
        elif score == 30:
            assert len(evidence) == 1
            text, weight = evidence[0]
            assert weight == 0.0
            # THE PROPERTY THIS DISPATCH EXISTS TO PIN: no sentence
            # claiming sufficient evidence for haploinsufficiency may
            # ever be produced for a gene ClinGen curated as
            # autosomal-recessive -- that was the live, self-refuting
            # defect this replaces.
            assert _SUFFICIENT_EVIDENCE_FRAGMENT not in text
            assert "does not establish" in text
            assert "single loss-of-function allele" in text
        else:  # 0, 1, 2 -- no dosage-sensitivity evidence at all
            assert evidence == []

    def test_none_score_contributes_nothing(self):
        evidence = _evidence_for_score(None)
        assert evidence == []


if __name__ == "__main__":
    unittest.main()
