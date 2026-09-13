"""
Round 16, item 1: the report layer was collapsing four distinct
`not_evaluated` reasons into one false label.

A real run (`geper_report_full.pdf`, `GEPER-RUN-20260814T07360`, code
`7d6dde2`, `test_data/nuclear_test_with_mt.vcf`, Finding 6: MT:3243 A>G,
MT-TL1) showed two contradictions on the same finding:

  1. The mtDNA compartment disclaimer (`pipeline/acmg_rules.py::
     mtdna_interpretation_disclaimer`) correctly partitioned the 28
     criteria into 9 never-evaluated / 7 compartment-inapplicable / 6
     gene-class-inapplicable (MT-TL1 is `Mt_tRNA`) / 6 gene-class-
     independent "evaluated for real" -- but two lines later the
     accounting sentence (`report/clinical_report_builder.py::
     _executive_summary`) and the Limitations paragraph
     (`_limitations`) described ALL 27 not-evaluated criteria with one
     blanket phrase, "could not be evaluated due to missing data
     sources" -- true for a genuine per-variant lookup gap, flatly
     false for a criterion this pipeline structurally never applies to
     the mitochondrial compartment or to a non-protein-coding gene.

  2. Two smaller instances of the identical root cause on the same
     finding: `pipeline/confidence_engine.py`'s "No UniProt entry
     resolved"/"No AlphaFold DB structure resolved" notes (a tRNA gene
     has no protein at all -- not a failed lookup) and "gnomAD lookup
     was skipped or unavailable" (round 14 B2 deliberately never
     queried it for a chrM variant, and said exactly why via
     `mtdna_gnomad_skip_result`'s own `reason` field -- discarded by
     the note-generation code that produced this phrase).

INVESTIGATION FINDING (per the task's own request to check before
fixing): for the ACMG criteria, the engine ALREADY carries a real,
specific, human-readable `rationale` for every `not_evaluated` result
(round 14 B2's own `_MTDNA_STRUCTURALLY_INAPPLICABLE_REASONS`/
`_mtdna_rna_gene_reason`) -- the renderer discarded it, this was NOT
lost upstream. For UniProt/InterPro/AlphaFold, the opposite was true:
`pipeline/orchestrator.py` ran a real (doomed) query for every
mitochondrial gene regardless of biotype, so there was no `reason` to
discard -- that fix (`non_protein_coding_gene_reason`, wired into
`orchestrator.py`) is genuinely new upstream logic, not a renderer fix,
and is NOT covered by this file (orchestrator's module-level imports
pull in a multi-minute TensorFlow/absl chain -- see
`test_mtdna_compartment_gate.py`'s own docstring for the same
constraint; verified by code review + `py_compile` only, per this
machine's hardware limits).

Everything below is offline and pure -- `ACMGRuleEngine`,
`InterpretationEngine`, `report.json_builder.build_variant_result`, and
the Markdown renderer never touch a model or the network (same
scope-boundary discipline as `test_mtdna_compartment_gate.py`, which
this file's fixtures/helpers deliberately mirror rather than duplicate
a second synthetic MT-TL1/MT-ATP6 setup).
"""

import unittest

from pipeline.acmg_rules import (
    ACMGRuleEngine,
    NotEvaluatedReason,
    _NEVER_INTEGRATED_ACMG_CODES,
    mtdna_alphamissense_skip_result,
    mtdna_ensemble_skip_result,
    mtdna_gnomad_skip_result,
    mtdna_interpretation_disclaimer,
    mtdna_mmsplice_skip_result,
    mtdna_splice_plugin_skip_result,
    non_protein_coding_gene_reason,
    not_evaluated_breakdown,
)
from pipeline.confidence_engine import ConfidenceEngine
from pipeline.interpretation import InterpretationEngine
from report.clinical_report_builder import _executive_summary, _limitations, _not_evaluated_reason_clause
from report import report_generator as report_generator_module
from report.json_builder import build_variant_result
from tests.test_mtdna_compartment_gate import (
    _MT_ATP6_TRANSCRIPT_RESULT,
    _MT_ATP6_VARIANT,
    _MT_TL1_TRANSCRIPT_RESULT,
    _MT_TL1_VARIANT,
)

_NUCLEAR_VARIANT = {"chrom": "17", "pos": 43106534, "ref": "C", "alt": "A"}
# Undetermined transcript (no biotype known at all) -- distinct from a
# confirmed protein-coding OR confirmed non-coding gene; a nuclear
# variant with a real protein-coding transcript reaches the same
# "everything not NOT_INTEGRATED ran for real" shape as MT-ATP6, just
# without any COMPARTMENT_INAPPLICABLE/GENE_CLASS_INAPPLICABLE codes.
_NUCLEAR_TRANSCRIPT_RESULT = {
    "skipped": False,
    "found": True,
    "gene_symbol": "BRCA1",
    "transcript": {"id": "ENST00000357654"},
    "gene_biotype": "protein_coding",
}


def _evaluate(variant_dict, transcript_result):
    return ACMGRuleEngine().evaluate(variant_dict=variant_dict, transcript_result=transcript_result)


class TestNotEvaluatedCategoryPartition(unittest.TestCase):
    """The core investigation finding: `CriterionResult.category` (round
    16) is a real, structural partition of `not_evaluated_criteria`, not
    an afterthought -- exact counts for both mtDNA gene classes and for
    a nuclear variant, proving the categorization itself is correct
    before any report-layer wording is tested."""

    def test_mt_tl1_tRNA_gene_partitions_into_9_7_6(self):
        result = _evaluate(_MT_TL1_VARIANT, _MT_TL1_TRANSCRIPT_RESULT)
        breakdown = not_evaluated_breakdown(result["not_evaluated_criteria"])
        self.assertEqual(len(breakdown[NotEvaluatedReason.NOT_INTEGRATED.value]), 9)
        self.assertEqual(len(breakdown[NotEvaluatedReason.COMPARTMENT_INAPPLICABLE.value]), 7)
        self.assertEqual(len(breakdown[NotEvaluatedReason.GENE_CLASS_INAPPLICABLE.value]), 6)
        # 28 - 9 - 7 - 6 = 6 gene-class-independent codes; each landed
        # not_evaluated here (no real clinvar/functional/hpo evidence was
        # supplied), so all 6 fall into DATA_UNAVAILABLE -- a genuine
        # per-variant gap, correctly distinct from the 13 structurally
        # gated codes above it.
        self.assertEqual(len(breakdown[NotEvaluatedReason.DATA_UNAVAILABLE.value]), 6)

    def test_mt_atp6_protein_coding_gene_partitions_into_9_7_0(self):
        result = _evaluate(_MT_ATP6_VARIANT, _MT_ATP6_TRANSCRIPT_RESULT)
        breakdown = not_evaluated_breakdown(result["not_evaluated_criteria"])
        self.assertEqual(len(breakdown[NotEvaluatedReason.NOT_INTEGRATED.value]), 9)
        self.assertEqual(len(breakdown[NotEvaluatedReason.COMPARTMENT_INAPPLICABLE.value]), 7)
        self.assertEqual(len(breakdown[NotEvaluatedReason.GENE_CLASS_INAPPLICABLE.value]), 0)

    def test_nuclear_variant_has_no_compartment_or_gene_class_codes(self):
        result = _evaluate(_NUCLEAR_VARIANT, _NUCLEAR_TRANSCRIPT_RESULT)
        breakdown = not_evaluated_breakdown(result["not_evaluated_criteria"])
        self.assertEqual(len(breakdown[NotEvaluatedReason.NOT_INTEGRATED.value]), 9)
        self.assertEqual(breakdown[NotEvaluatedReason.COMPARTMENT_INAPPLICABLE.value], [])
        self.assertEqual(breakdown[NotEvaluatedReason.GENE_CLASS_INAPPLICABLE.value], [])

    def test_ps4_is_always_not_integrated_never_data_unavailable(self):
        # PS4 always returns not_evaluated regardless of gnomad_result
        # content (no case-frequency source exists at all) -- must be
        # NOT_INTEGRATED in every branch of `_ps4`, not the DATA_UNAVAILABLE
        # default, in all three of its own internal branches.
        for gnomad_result in (None, {"skipped": True}, {"found": False}, {"found": True, "global_af": 0.001}):
            with self.subTest(gnomad_result=gnomad_result):
                result = ACMGRuleEngine().evaluate(
                    variant_dict=_NUCLEAR_VARIANT,
                    transcript_result=_NUCLEAR_TRANSCRIPT_RESULT,
                    gnomad_result=gnomad_result,
                )
                ps4 = result["all_criteria"]["PS4"]
                self.assertEqual(ps4["status"], "not_evaluated")
                self.assertEqual(ps4["category"], NotEvaluatedReason.NOT_INTEGRATED.value)

    def test_never_integrated_constant_matches_the_real_rule_set(self):
        # T4-F1: mtdna_interpretation_disclaimer() used to print the "9"
        # in "GEPER never evaluates 9 for any variant" as a hand-typed
        # literal, disconnected from the actual rule implementations --
        # the exact divergence shape round 16 already fixed once for this
        # function's middle clause. The disclaimer now derives the count
        # from _NEVER_INTEGRATED_ACMG_CODES instead of a literal digit;
        # THIS test is what keeps that constant honest. If a rule
        # implementation ever adds, removes, or reclassifies a
        # NOT_INTEGRATED code, this fails here -- not silently, three
        # tests up, leaving the disclaimer sentence to keep asserting a
        # stale count to a clinical reader with zero red test elsewhere.
        # Checked against three independent contexts (mt-tRNA,
        # mt-protein-coding, nuclear) since NOT_INTEGRATED is defined as
        # "never evaluated for ANY variant" -- a constant that only
        # matched one context wouldn't actually prove that claim.
        for variant_dict, transcript_result in (
            (_MT_TL1_VARIANT, _MT_TL1_TRANSCRIPT_RESULT),
            (_MT_ATP6_VARIANT, _MT_ATP6_TRANSCRIPT_RESULT),
            (_NUCLEAR_VARIANT, _NUCLEAR_TRANSCRIPT_RESULT),
        ):
            with self.subTest(gene=transcript_result.get("gene_symbol")):
                result = _evaluate(variant_dict, transcript_result)
                breakdown = not_evaluated_breakdown(result["not_evaluated_criteria"])
                actual = set(breakdown[NotEvaluatedReason.NOT_INTEGRATED.value])
                self.assertEqual(actual, set(_NEVER_INTEGRATED_ACMG_CODES))

    def test_disclaimer_never_evaluates_count_is_derived_not_hardcoded(self):
        # Proves the fix at the point a clinical reader actually sees it:
        # the disclaimer string's count must equal the constant's length,
        # for both the data-driven path (not_evaluated_rules supplied)
        # and the gene-class-only fallback path (older callers).
        expected = f"GEPER never evaluates {len(_NEVER_INTEGRATED_ACMG_CODES)} for any variant"
        result = _evaluate(_NUCLEAR_VARIANT, _NUCLEAR_TRANSCRIPT_RESULT)
        data_driven_text = mtdna_interpretation_disclaimer(_NUCLEAR_TRANSCRIPT_RESULT, result["not_evaluated_criteria"])
        fallback_text = mtdna_interpretation_disclaimer(_NUCLEAR_TRANSCRIPT_RESULT)
        self.assertIn(expected, data_driven_text)
        self.assertIn(expected, fallback_text)


class TestNotEvaluatedBreakdownPureFunction(unittest.TestCase):
    def test_empty_list_returns_all_categories_empty(self):
        breakdown = not_evaluated_breakdown([])
        self.assertEqual(set(breakdown.keys()), {r.value for r in NotEvaluatedReason})
        self.assertTrue(all(codes == [] for codes in breakdown.values()))

    def test_none_input_treated_as_empty(self):
        self.assertEqual(not_evaluated_breakdown(None), not_evaluated_breakdown([]))

    def test_missing_category_key_defaults_to_data_unavailable(self):
        # A CriterionResult built without going through `_not_evaluated`
        # (or an older cached result predating round 16) must not be
        # silently dropped or crash -- it lands in the same default
        # category `_not_evaluated` itself uses.
        breakdown = not_evaluated_breakdown([{"code": "PP1"}])
        self.assertEqual(breakdown[NotEvaluatedReason.DATA_UNAVAILABLE.value], ["PP1"])


class TestDisclaimerAndAccountingSingleSourced(unittest.TestCase):
    """The actual reported contradiction: same finding, two report
    surfaces. Both must now be built from `not_evaluated_breakdown` of
    the SAME `not_evaluated_criteria` list."""

    def test_disclaimer_never_overclaims_beyond_what_breakdown_shows(self):
        result = _evaluate(_MT_TL1_VARIANT, _MT_TL1_TRANSCRIPT_RESULT)
        not_evaluated = result["not_evaluated_criteria"]
        text = mtdna_interpretation_disclaimer(_MT_TL1_TRANSCRIPT_RESULT, not_evaluated)
        breakdown = not_evaluated_breakdown(not_evaluated)
        for code in breakdown[NotEvaluatedReason.COMPARTMENT_INAPPLICABLE.value]:
            self.assertIn(code, text)
        for code in breakdown[NotEvaluatedReason.GENE_CLASS_INAPPLICABLE.value]:
            self.assertIn(code, text)
        self.assertIn("Mt_tRNA", text)

    def test_executive_summary_does_not_call_structurally_inapplicable_criteria_missing_data(self):
        # The literal reported bug: a criterion inapplicable by
        # mitochondrial biology must never be described with the same
        # "missing data sources" phrase used for a genuine lookup gap.
        result = _evaluate(_MT_TL1_VARIANT, _MT_TL1_TRANSCRIPT_RESULT)
        ir = {
            "triggered_rules": result["triggered_criteria"],
            "not_triggered_rules": result["not_triggered_criteria"],
            "not_evaluated_rules": result["not_evaluated_criteria"],
            "acmg_classification": "Uncertain significance",
            "variant": _MT_TL1_VARIANT,
        }
        summary = _executive_summary(ir)
        self.assertNotIn("could not be evaluated due to missing data sources", summary)
        self.assertIn("structurally inapplicable", summary)
        self.assertIn("gene's biotype/class", summary)

    def test_limitations_paragraph_also_distinguishes_categories(self):
        result = _evaluate(_MT_TL1_VARIANT, _MT_TL1_TRANSCRIPT_RESULT)
        ir = {
            "triggered_rules": result["triggered_criteria"],
            "not_triggered_rules": result["not_triggered_criteria"],
            "not_evaluated_rules": result["not_evaluated_criteria"],
        }
        limitations_text = "\n".join(_limitations(ir))
        self.assertNotIn("could not be evaluated for this variant due to missing evidence sources", limitations_text)
        self.assertIn("structurally inapplicable", limitations_text)

    def test_nuclear_variant_accounting_still_reads_as_missing_data(self):
        # No compartment/gene-class codes for a nuclear variant -- the
        # clause should read as "no data source GEPER integrates" /
        # "missing/unavailable evidence source", never claim a
        # structural inapplicability that doesn't apply here.
        result = _evaluate(_NUCLEAR_VARIANT, _NUCLEAR_TRANSCRIPT_RESULT)
        clause = _not_evaluated_reason_clause(result["not_evaluated_criteria"])
        self.assertNotIn("structurally inapplicable", clause)
        self.assertNotIn("gene's biotype/class", clause)

    def test_clause_omits_zero_categories(self):
        # Nuclear variant has zero compartment/gene-class codes -- the
        # rendered clause must not print "0 structurally inapplicable...".
        result = _evaluate(_NUCLEAR_VARIANT, _NUCLEAR_TRANSCRIPT_RESULT)
        clause = _not_evaluated_reason_clause(result["not_evaluated_criteria"])
        self.assertNotIn("0 ", clause)

    def test_empty_not_evaluated_list_produces_empty_clause(self):
        self.assertEqual(_not_evaluated_reason_clause([]), "")


class TestRenderedMarkdownMatchesRealFinding(unittest.TestCase):
    """End-to-end (offline, pure) reproduction of the reported finding:
    MT:3243 A>G, MT-TL1, through `InterpretationEngine` ->
    `build_variant_result` -> the Markdown renderer -- the same call
    chain `test_mtdna_compartment_gate.py::TestPerFindingDisclaimer`
    already exercises, extended to check the accounting/Limitations
    text this round actually touched."""

    def _mt_tl1_variant_result(self):
        interpretation = InterpretationEngine().interpret(
            variant_dict=_MT_TL1_VARIANT,
            dna_models_used=[],
            clinvar_result={"records": []},
            dbsnp_result={"found": False},
            protein_result={},
            blast_result={},
            alphamissense_result=mtdna_alphamissense_skip_result(),
            mmsplice_result=mtdna_mmsplice_skip_result(),
            gnomad_result=mtdna_gnomad_skip_result(),
            conservation_result={},
            clingen_result={},
            uniprot_result={},
            interpro_result={},
            alphafold_result={},
            rna_result={},
            ensemble_result=mtdna_ensemble_skip_result(),
            transcript_result=_MT_TL1_TRANSCRIPT_RESULT,
            clinvar_codon_result={},
            spliceformer_result=mtdna_splice_plugin_skip_result(),
            splicebert_result=mtdna_splice_plugin_skip_result(),
            hpo_result={},
            phenotype_result=None,
            functional_evidence_result={},
        )
        return build_variant_result(
            variant_dict=_MT_TL1_VARIANT,
            sequence_context={"error": "not applicable to this test"},
            dna_model_results={},
            rna_result={},
            protein_result={},
            blast_result={},
            clinvar_result={"records": []},
            dbsnp_result={"found": False},
            interpretation=interpretation,
            errors=[],
            alphamissense_result=mtdna_alphamissense_skip_result(),
            mmsplice_result=mtdna_mmsplice_skip_result(),
            gnomad_result=mtdna_gnomad_skip_result(),
            transcript_result=_MT_TL1_TRANSCRIPT_RESULT,
            ai_splicing_ensemble_result=mtdna_ensemble_skip_result(),
        )

    def test_markdown_report_never_calls_compartment_gate_missing_data(self):
        gen = report_generator_module.ReportGenerator()
        lines = gen._render_variant_section(1, self._mt_tl1_variant_result())
        text = "\n".join(lines)
        self.assertIn("Mitochondrial (mtDNA) compartment notice", text)
        self.assertNotIn("could not be evaluated due to missing data sources", text)
        self.assertNotIn("due to missing evidence sources (PVS1", text)

    def test_markdown_report_disclaimer_and_accounting_agree_on_compartment_codes(self):
        gen = report_generator_module.ReportGenerator()
        variant_result = self._mt_tl1_variant_result()
        text = "\n".join(gen._render_variant_section(1, variant_result))
        not_evaluated = (variant_result.get("interpretation_result") or {}).get("not_evaluated_rules", [])
        breakdown = not_evaluated_breakdown(not_evaluated)
        # Every compartment-inapplicable code the disclaimer claims is
        # inapplicable must also appear somewhere in the same rendered
        # section (the per-criterion not_evaluated table renders every
        # code's own rationale) -- proving the two are reading the same
        # underlying list, not two independently-assembled descriptions.
        for code in breakdown[NotEvaluatedReason.COMPARTMENT_INAPPLICABLE.value]:
            self.assertIn(code, text)


class TestNonProteinCodingGeneReason(unittest.TestCase):
    """Pure-function tests for the second reported instance: UniProt/
    AlphaFold's "no entry/structure resolved" must distinguish "this
    gene has no protein by biology" from "we looked and found nothing".
    Deliberately NOT mtDNA-specific (a nuclear ncRNA gene has the exact
    same property) -- see the function's own docstring."""

    def test_confirmed_non_coding_biotype_gives_a_reason(self):
        reason = non_protein_coding_gene_reason(_MT_TL1_TRANSCRIPT_RESULT)
        self.assertIsNotNone(reason)
        self.assertIn("Mt_tRNA", reason)
        self.assertIn("no protein product", reason)
        self.assertIn("not a failed lookup", reason)  # explicitly distinguishes this from a failed lookup

    def test_a_nuclear_noncoding_gene_gets_the_same_treatment(self):
        nuclear_lncrna = {"found": False, "transcript": None, "gene_symbol": "MALAT1", "gene_biotype": "lncRNA"}
        reason = non_protein_coding_gene_reason(nuclear_lncrna)
        self.assertIsNotNone(reason)
        self.assertIn("lncRNA", reason)
        self.assertIn("MALAT1", reason)

    def test_protein_coding_transcript_resolved_gives_no_reason(self):
        self.assertIsNone(non_protein_coding_gene_reason(_MT_ATP6_TRANSCRIPT_RESULT))

    def test_undetermined_gene_class_gives_no_reason_not_overclaiming(self):
        self.assertIsNone(non_protein_coding_gene_reason(None))
        self.assertIsNone(non_protein_coding_gene_reason({}))
        self.assertIsNone(non_protein_coding_gene_reason({"found": False, "gene_biotype": None}))


class TestConfidenceEngineUsesSkipReason(unittest.TestCase):
    """The third reported instance: "gnomAD lookup was skipped or
    unavailable" printed even when `mtdna_gnomad_skip_result()` already
    carried a real, specific reason."""

    def test_population_note_uses_the_real_mtdna_skip_reason(self):
        score = ConfidenceEngine._population_quality(mtdna_gnomad_skip_result(), {"found": False}, weight=1.0)
        self.assertIn("gnomAD was not queried:", score.rationale)
        self.assertIn("mitochondrial", score.rationale.lower())
        self.assertNotIn("gnomAD lookup was skipped or unavailable", score.rationale)

    def test_population_note_falls_back_to_generic_when_no_reason_given(self):
        score = ConfidenceEngine._population_quality({"skipped": True}, {"found": False}, weight=1.0)
        self.assertIn("gnomAD lookup was skipped or unavailable", score.rationale)

    def test_protein_note_uses_a_biotype_reason_when_present(self):
        uniprot_skip = {
            "found": False,
            "skipped": True,
            "reason": non_protein_coding_gene_reason(_MT_TL1_TRANSCRIPT_RESULT),
        }
        score = ConfidenceEngine._protein_quality(uniprot_skip, {"found": False}, weight=1.0)
        self.assertIn("Mt_tRNA", score.rationale)
        self.assertNotIn("No UniProt entry resolved for this gene/protein.", score.rationale)

    def test_structural_note_uses_a_biotype_reason_when_present(self):
        alphafold_skip = {
            "found": False,
            "skipped": True,
            "reason": non_protein_coding_gene_reason(_MT_TL1_TRANSCRIPT_RESULT),
        }
        score = ConfidenceEngine._structural_quality(alphafold_skip, weight=1.0)
        self.assertIn("Mt_tRNA", score.rationale)
        self.assertNotIn("No AlphaFold DB structure resolved for this protein.", score.rationale)


if __name__ == "__main__":
    unittest.main()
