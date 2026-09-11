"""
tests/test_ai_models_used_disclosure.py
─────────────────────────────────────────
HUMAN-DECISION-kim-s-TWO-AI-MODELS-ARE-LIVE-IN-THE-SHIPPED-IMAGE-AND-FEED-
THE-EVIDENCE-SCORE-WHILE-ITS-OWN-REQUIREMENTS-CALL-THEM-OPTIONAL -- ruled
(C), 2026-09-11: "the report should disclose which models contributed,
because a score whose inputs vary silently is not reproducible from the
report."

DNABERT-2 and ESM-2 fire independently per variant
(pipeline/orchestration/shared.py::run_acmg_evidence_batch): DNABERT-2 needs
ref_sequence/alt_sequence, ESM-2 needs wildtype_aa/mutant_aa. These tests
drive the real function and assert `ai_models_used` on the returned
per-variant record reflects what ACTUALLY ran for that variant -- not
whether the AI engine was merely constructable.

FIXED 2026-09-11 (kelly, then god, then this file): the first version of
this harness mocked GnomadLookup and AiEngine but left
pipeline.constraint.lookup.GnomadConstraintLookup live -- it posts to
gnomad.broadinstitute.org (constraint/lookup.py:39, :362), so every run of
this file hit gnomAD's real API and CI's result depended on a third party's
rate limit (kelly reproduced this locally: HTTP 429). Now mocked the same
way test_consequence_disagreement_report_caveat.py mocks it, AND
requests.get/requests.post are patched and asserted UNCALLED in every test
-- a swallowed network error looks exactly like a clean offline run
otherwise, so the assertion is what actually proves offline rather than
merely intending it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pipeline.constraint.lookup import GnomadConstraintLookup  # noqa: E402
from pipeline.gnomad.lookup import GnomadLookup, GnomadLookupOutcome  # noqa: E402
from pipeline.orchestration.shared import run_acmg_evidence_batch  # noqa: E402

_CFG = {"gnomad": {"enabled": True}, "clinvar": {"enabled": False}, "vep": {"enabled": False}}


def _run(variant: dict, ai_engine_factory) -> dict:
    """Runs the real run_acmg_evidence_batch for one variant, with gnomAD
    short-circuited to UNAVAILABLE (irrelevant to this card), the gnomAD
    constraint lookup stubbed (same three seams
    test_consequence_disagreement_report_caveat.py stubs), and
    pipeline.ai.engine.AiEngine replaced by `ai_engine_factory`, exactly the
    seam shared.py itself imports through (`from pipeline.ai.engine import
    AiEngine`, inside the function -- so patching the module attribute is
    what the real call site actually sees). requests.get/requests.post are
    patched and asserted uncalled -- this run must be offline, provably,
    not just by omission."""
    base = {"chrom": "17", "pos": 43057051, "ref": "A", "alt": "T", "gene_name": "BRCA1"}
    base.update(variant)
    with (
        patch.object(GnomadLookup, "lookup", return_value=GnomadLookupOutcome.UNAVAILABLE),
        patch.object(GnomadLookup, "lookup_batch", return_value=None),
        patch.object(GnomadConstraintLookup, "lookup", return_value=None),
        patch.object(GnomadConstraintLookup, "is_lof_intolerant", return_value=False),
        patch.object(GnomadConstraintLookup, "is_missense_constrained", return_value=False),
        patch("pipeline.ai.engine.AiEngine", ai_engine_factory),
        patch("requests.get") as net_get,
        patch("requests.post") as net_post,
    ):
        batch = run_acmg_evidence_batch([base], _CFG, sample_id="TESTSAMPLE")
    assert not net_get.called and not net_post.called, "this test must be offline"
    assert len(batch) == 1
    return batch[0]


def _working_engine(dna_return=0.7, protein_return=0.6):
    engine = MagicMock()
    engine.score_dna.return_value = dna_return
    engine.score_protein.return_value = protein_return
    engine.combined_score.return_value = 0.65
    return MagicMock(return_value=engine)


class TestAiModelsUsedReflectsWhatActuallyRan:
    def test_dna_and_protein_data_both_present_both_models_listed(self):
        record = _run(
            {
                "ref_sequence": "ACGT",
                "alt_sequence": "ACGA",
                "wildtype_aa": "M",
                "mutant_aa": "V",
            },
            _working_engine(),
        )
        assert record["ai_models_used"] == ["DNABERT-2", "ESM-2"]

    def test_only_dna_sequence_present_only_dnabert2_listed(self):
        """THE CARD: a run-level 'AI available' flag would still claim ESM-2
        contributed here. It must not -- no wildtype/mutant AA means ESM-2
        was never called, regardless of engine availability."""
        record = _run(
            {"ref_sequence": "ACGT", "alt_sequence": "ACGA"},
            _working_engine(),
        )
        assert record["ai_models_used"] == ["DNABERT-2"]

    def test_only_protein_data_present_only_esm2_listed(self):
        record = _run(
            {"wildtype_aa": "M", "mutant_aa": "V"},
            _working_engine(),
        )
        assert record["ai_models_used"] == ["ESM-2"]

    def test_neither_sequence_nor_protein_data_no_models_listed(self):
        """NEGATIVE CONTROL 1: AI engine is constructable and working, but
        this variant supplies neither model's required input -- the list
        must stay empty, not silently claim a model ran."""
        record = _run({}, _working_engine())
        assert record["ai_models_used"] == []

    def test_engine_unavailable_no_models_listed_even_with_full_input_data(self):
        """NEGATIVE CONTROL 2, THE ONE THAT MATTERS MOST FOR THIS CARD:
        AiEngine() raising (the torch/transformers-not-installed path
        shared.py's own try/except swallows) must produce an EMPTY list even
        when the variant has every field either model would need -- proving
        this isn't an echo of input presence but a record of whether a
        model actually returned a score."""
        raising_ctor = MagicMock(side_effect=RuntimeError("torch not installed"))
        record = _run(
            {
                "ref_sequence": "ACGT",
                "alt_sequence": "ACGA",
                "wildtype_aa": "M",
                "mutant_aa": "V",
            },
            raising_ctor,
        )
        assert record["ai_models_used"] == []

    def test_model_returning_none_is_not_counted_as_used(self):
        """A model call that completes but returns None (e.g. a sequence it
        could not score) must not be reported as having contributed."""
        engine = MagicMock()
        engine.score_dna.return_value = None
        engine.score_protein.return_value = 0.4
        engine.combined_score.return_value = 0.4
        record = _run(
            {
                "ref_sequence": "ACGT",
                "alt_sequence": "ACGA",
                "wildtype_aa": "M",
                "mutant_aa": "V",
            },
            MagicMock(return_value=engine),
        )
        assert record["ai_models_used"] == ["ESM-2"]


class TestAiModelsUsedRenderedOnTheReport:
    """_acmg_to_html_table is the actual HTML the report ships -- these
    drive it directly, the same way the zygosity malformed-disclosure card
    did, rather than only asserting on the upstream dict."""

    def _row(self, **overrides) -> dict:
        row = {
            "chrom": "17",
            "pos": 43057051,
            "ref": "A",
            "alt": "T",
            "gene": "BRCA1",
            "classification": "Uncertain_Significance",
            "score": 0,
            "criteria_met": [],
            "criteria_unknown": [],
            "gnomad_unavailable_reason": "gnomAD disabled for this run",
        }
        row.update(overrides)
        return row

    def test_both_models_used_renders_both_names(self):
        from pipeline.reporting.stage import _acmg_to_html_table

        html = _acmg_to_html_table([self._row(ai_models_used=["DNABERT-2", "ESM-2"])])
        assert "<td>DNABERT-2, ESM-2</td>" in html

    def test_one_model_used_renders_only_that_name(self):
        from pipeline.reporting.stage import _acmg_to_html_table

        html = _acmg_to_html_table([self._row(ai_models_used=["ESM-2"])])
        assert "<td>ESM-2</td>" in html
        assert "DNABERT-2" not in html

    def test_ai_ran_but_neither_model_contributed_renders_none_not_blank(self):
        from pipeline.reporting.stage import _acmg_to_html_table

        html = _acmg_to_html_table([self._row(ai_models_used=[])])
        assert "<td>None</td>" in html

    def test_ai_scoring_never_attempted_renders_not_attempted_not_none(self):
        """The error-record shape (ai_models_used absent entirely) must read
        differently from 'AI ran, contributed nothing' -- collapsing the two
        into the same cell text would be the exact absence-vs-error collapse
        this floor has spent the day removing on other surfaces."""
        from pipeline.reporting.stage import _acmg_to_html_table

        row = self._row()
        assert "ai_models_used" not in row
        html = _acmg_to_html_table([row])
        assert "<td>Not attempted</td>" in html
