"""
tests/test_consequence_disagreement_report_caveat.py
─────────────────────────────────────────────────────
T3-F3 (finding: andy), ruled (b) REPORT CAVEAT, 2026-09-11: "(a) hides a
disagreement that gates six criteria. (c) is too strong -- a disagreement
isn't an inability to evaluate, and forcing not_evaluated would discard
evidence that may be sound. Disclose it and let the reviewer weigh it."

kim_pipeline derives a protein consequence twice for the same variant: its
own GFF3 + codon derivation (`consequence`, the one ACMG's is_missense/is_lof
gates read) and VEP's most-severe CSQ term (`vep_consequence`). Andy's
detector `consequence_classes_disagree` existed but ran nowhere. These tests
pin the three halves of the ruling:

  1. DISCLOSED -- the per-variant record carries the disagreement, and the
     report renders it, naming the variant and both terms.
  2. NOT GATED -- the is_lof / is_missense gates the classifier is handed
     follow kim's term whatever VEP says. A caveat that changed the call
     would be option (c).
  3. NOT A FALSE ALARM -- same-class terms (stop_gained vs
     frameshift_variant) and a missing VEP term produce no caveat.

The batch is driven through the real `run_acmg_evidence_batch`. gnomAD and the
AI engine are mocked as in test_ai_models_used_disclosure.py, AND the gnomAD
CONSTRAINT lookup too -- that harness leaves it live, and a first draft of
this file copied it and was answered by gnomad.broadinstitute.org (HTTP 429).
`requests.get`/`requests.post` are patched and asserted UNCALLED, so a
swallowed network error cannot pass for an offline run.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pipeline.acmg.classifier import AcmgClassifier  # noqa: E402
from pipeline.constraint.lookup import GnomadConstraintLookup  # noqa: E402
from pipeline.gnomad.lookup import GnomadLookup, GnomadLookupOutcome  # noqa: E402
from pipeline.orchestration.shared import run_acmg_evidence_batch  # noqa: E402
from pipeline.reporting.stage import _acmg_to_html_table  # noqa: E402

_CFG = {"gnomad": {"enabled": True}, "clinvar": {"enabled": False}, "vep": {"enabled": False}}

# The caveat's heading. Asserted on by the "no caveat" tests, so it must be
# text that appears ONLY when a disagreement is disclosed.
_CAVEAT_MARK = "Protein consequence: two derivations disagree"


def _run_capturing_evidence(variant: dict):
    """Run the real batch for one variant, offline. Returns (record, the
    VariantEvidence the classifier was actually handed)."""
    base = {"chrom": "17", "pos": 43057051, "ref": "A", "alt": "T", "gene_name": "BRCA1"}
    base.update(variant)
    real_classify = AcmgClassifier.classify
    seen = []

    def _capture(self, ev):
        seen.append(ev)
        return real_classify(self, ev)

    with (
        patch.object(GnomadLookup, "lookup", return_value=GnomadLookupOutcome.UNAVAILABLE),
        patch.object(GnomadLookup, "lookup_batch", return_value=None),
        patch.object(GnomadConstraintLookup, "lookup", return_value=None),
        patch.object(GnomadConstraintLookup, "is_lof_intolerant", return_value=False),
        patch.object(GnomadConstraintLookup, "is_missense_constrained", return_value=False),
        patch(
            "pipeline.ai.engine.AiEngine", MagicMock(side_effect=RuntimeError("AI not under test"))
        ),
        patch.object(AcmgClassifier, "classify", _capture),
        patch("requests.get") as net_get,
        patch("requests.post") as net_post,
    ):
        batch = run_acmg_evidence_batch([base], _CFG, sample_id="TESTSAMPLE")
    assert not net_get.called and not net_post.called, "this test must be offline"
    assert len(batch) == 1 and len(seen) == 1
    return batch[0], seen[0]


def _run(variant: dict) -> dict:
    return _run_capturing_evidence(variant)[0]


class TestTheRecordCarriesTheDisagreement:
    def test_cross_class_disagreement_is_recorded_with_both_terms(self):
        record = _run({"consequence": "missense_variant", "vep_consequence": "stop_gained"})
        assert record["consequence_disagreement"] == {
            "kim_consequence": "missense_variant",
            "vep_consequence": "stop_gained",
        }

    def test_same_class_terms_are_not_a_disagreement(self):
        # Both LOF. A naive string compare would flag this; the detector
        # compares by class, and so must the record.
        record = _run({"consequence": "stop_gained", "vep_consequence": "frameshift_variant"})
        assert record["consequence_disagreement"] is None

    def test_no_vep_term_is_nothing_to_compare_not_a_disagreement(self):
        record = _run({"consequence": "missense_variant", "vep_consequence": ""})
        assert record["consequence_disagreement"] is None


class TestTheDisagreementDoesNotGateTheCriteria:
    """Ruled (b), not (c): disclosed, never withheld.

    Pinned at the GATE INPUTS -- the is_lof / is_missense the classifier is
    handed -- not at the final call. A first draft compared classification
    and criteria with and without the disagreement; a mutation that fed
    VEP's term into the gates left it GREEN, because for this input the
    call does not move either way. Comparing outcomes could not see gating.
    """

    def test_gates_follow_kims_term_whatever_vep_says(self):
        record, ev = _run_capturing_evidence(
            {"consequence": "missense_variant", "vep_consequence": "stop_gained"}
        )
        assert record["consequence_disagreement"] is not None, (
            "precondition: this pair must disagree"
        )
        assert ev.is_missense is True
        assert ev.is_lof is False, "VEP's stop_gained must not reach the LOF gate"

    def test_gates_follow_kims_term_in_the_other_direction(self):
        record, ev = _run_capturing_evidence(
            {"consequence": "stop_gained", "vep_consequence": "missense_variant"}
        )
        assert record["consequence_disagreement"] is not None, (
            "precondition: this pair must disagree"
        )
        assert ev.is_lof is True
        assert ev.is_missense is False, "VEP's missense_variant must not reach the missense gate"


def _row(**overrides) -> dict:
    row = {
        "chrom": "17",
        "pos": 43057051,
        "ref": "A",
        "alt": "T",
        "gene": "BRCA1",
        "classification": "Uncertain_Significance",
        "consequence_disagreement": None,
    }
    row.update(overrides)
    return row


_DISAGREE = {"kim_consequence": "missense_variant", "vep_consequence": "stop_gained"}


class TestTheReportDisclosesIt:
    def test_disagreeing_variant_is_named_with_both_terms(self):
        html = _acmg_to_html_table([_row(consequence_disagreement=_DISAGREE)])
        assert _CAVEAT_MARK in html
        assert "17:43057051 A&gt;T" in html
        assert "missense_variant" in html
        assert "stop_gained" in html

    def test_the_caveat_says_which_derivation_the_criteria_used(self):
        html = _acmg_to_html_table([_row(consequence_disagreement=_DISAGREE)])
        assert "ACMG criteria were evaluated on kim_pipeline" in html

    def test_no_disagreement_no_caveat(self):
        assert _CAVEAT_MARK not in _acmg_to_html_table([_row()])

    def test_record_without_the_field_no_caveat(self):
        # The per-variant error-record shape never reached the comparison.
        row = _row()
        del row["consequence_disagreement"]
        assert _CAVEAT_MARK not in _acmg_to_html_table([row])

    def test_only_the_disagreeing_variant_is_listed(self):
        html = _acmg_to_html_table(
            [
                _row(pos=100, consequence_disagreement=_DISAGREE),
                _row(pos=200),
            ]
        )
        caveat = html[html.index(_CAVEAT_MARK) :]
        assert "17:100 A&gt;T" in caveat
        assert "17:200" not in caveat

    def test_terms_from_the_input_vcf_are_escaped(self):
        # vep_consequence is read straight out of the input VCF's CSQ field.
        hostile = {"kim_consequence": "missense_variant", "vep_consequence": "<script>x</script>"}
        html = _acmg_to_html_table([_row(consequence_disagreement=hostile)])
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
