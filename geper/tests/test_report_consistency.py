"""
Regression tests for a real report-quality bug found via a live Colab
end-to-end run: the "Clinical Interpretation Report" section of both
the JSON and Markdown output (built by
`report/clinical_report_builder.py::build_clinical_report`) claimed
"UniProt: no entry resolved" while the same report's own Annotation
Detail audit trail (built directly from the real `uniprot_result`
stage output) showed a fully resolved entry (accession A0A3G1DJQ2,
"Small humanin-like peptide 3", reviewed=True) a few sections later.

Root cause: `InterpretationResult.to_dict()`
(`pipeline/interpretation_result.py`) deliberately omits the
`raw_evidence` field from its serialized form (to avoid duplicating
data already emitted under the top-level `uniprot`/`interpro`/`gnomad`/
etc. keys). `build_clinical_report` used to read `raw_evidence`
*only* from that serialized dict (`ir.get("raw_evidence") or {}`),
which was therefore *always* an empty dict -- so every one of its
raw-evidence-derived sections (protein knowledge, structural knowledge,
population evidence, clinical evidence, sequence context) always
reported "not found"/"unavailable", regardless of what the
corresponding stage actually returned. This is systemic, not
UniProt-specific: it silently broke the gnomAD/dbSNP/ClinVar/ClinGen/
AlphaFold/BLAST sections of the same report the exact same way.

Fix: `build_clinical_report` now accepts an explicit `raw_evidence`
kwarg, and `report/json_builder.py::build_variant_result` builds it
from the same raw provider dicts it already received (and is about to
emit verbatim under the top-level keys), guaranteeing the two can
never drift apart again.

A second, related fix covered here: a failed external lookup (UniProt/
InterPro/ClinVar/ClinGen/AlphaFold erroring out) must never be
reported the same way as "queried successfully, found nothing" --
conflating the two misreports an external-service outage as a
negative biological finding.
"""

import unittest

from pipeline.interpretation_result import build_interpretation_result
from report.clinical_report_builder import build_clinical_report
from report.json_builder import build_variant_result
from report.report_generator import ReportGenerator

_VARIANT_DICT = {
    "chrom": "MT",
    "pos": 100,
    "ref": "A",
    "alt": "G",
    "variant_type": "SNV",
    "id": ".",
    "filter": "PASS",
}


def _legacy_interpretation(overrides=None):
    base = {
        "summary": "s",
        "confidence": "Low",
        "legacy_pre_acmg_significance_score": 0,
        "supporting_evidence": [],
        "acmg_evaluation": {
            "classification": "Uncertain Significance",
            "triggered_criteria": [],
            "not_triggered_criteria": [],
            "not_evaluated_criteria": [],
            "combining_rule_trace": [],
        },
    }
    base.update(overrides or {})
    return base


def _build_variant_result_with_serialized_interpretation(**raw_results):
    """Mirrors exactly what `pipeline/orchestrator.py` does: build the
    real `InterpretationResult`, serialize it via `.to_dict()` (which
    drops `raw_evidence`), attach it as `interpretation["interpretation_result"]`,
    then call `build_variant_result` the same way the orchestrator does
    -- with the raw per-stage dicts passed as their own kwargs, NOT
    re-extracted from the (already-stripped) interpretation_result."""
    interpretation = _legacy_interpretation()
    result_obj = build_interpretation_result(
        variant_dict=_VARIANT_DICT,
        interpretation=interpretation,
        **raw_results,
    )
    interpretation["interpretation_result"] = result_obj.to_dict()

    return build_variant_result(
        variant_dict=_VARIANT_DICT,
        sequence_context={},
        dna_model_results={},
        rna_result=raw_results.get("rna_result", {}),
        protein_result=raw_results.get("protein_result", {}),
        blast_result=raw_results.get("blast_result", {}),
        clinvar_result=raw_results.get("clinvar_result", {}),
        dbsnp_result=raw_results.get("dbsnp_result", {}),
        interpretation=interpretation,
        errors=[],
        alphamissense_result=raw_results.get("alphamissense_result"),
        mmsplice_result=raw_results.get("mmsplice_result"),
        gnomad_result=raw_results.get("gnomad_result"),
        clingen_result=raw_results.get("clingen_result"),
        uniprot_result=raw_results.get("uniprot_result"),
        interpro_result=raw_results.get("interpro_result"),
        alphafold_result=raw_results.get("alphafold_result"),
    )


class TestClinicalReportNeverContradictsRawStageOutput(unittest.TestCase):
    """The exact bug: a resolved UniProt entry must never be reported
    as 'no entry resolved' in the clinical_report summary section."""

    def test_resolved_uniprot_entry_is_reflected_in_protein_knowledge(self):
        uniprot_result = {
            "found": True,
            "skipped": False,
            "error": None,
            "accession": "A0A3G1DJQ2",
            "protein_name": "Small humanin-like peptide 3",
            "reviewed": True,
            "organism": "Homo sapiens",
            "gene_symbol": "MTRNR2L3",
        }
        vr = _build_variant_result_with_serialized_interpretation(uniprot_result=uniprot_result)

        prot = vr["clinical_report"]["protein_knowledge"]
        self.assertTrue(prot["uniprot_available"])
        self.assertEqual(prot["uniprot"]["accession"], "A0A3G1DJQ2")
        self.assertEqual(prot["uniprot"]["protein_name"], "Small humanin-like peptide 3")
        self.assertTrue(prot["uniprot"]["reviewed"])
        # Must agree with the top-level key the Annotation Detail trail renders from.
        self.assertEqual(vr["uniprot"], uniprot_result)

    def test_genuinely_unresolved_uniprot_entry_still_reports_not_resolved(self):
        """The fix must not flip every case to 'resolved' -- a real
        not-found result still renders as not-found."""
        uniprot_result = {"found": False, "skipped": False, "error": None, "reason": "no reviewed entry"}
        vr = _build_variant_result_with_serialized_interpretation(uniprot_result=uniprot_result)
        prot = vr["clinical_report"]["protein_knowledge"]
        self.assertFalse(prot["uniprot_available"])
        self.assertIsNone(prot["uniprot_error"])

    def test_gnomad_dbsnp_clinvar_clingen_alphafold_all_reflect_real_stage_output(self):
        """Same systemic bug, same fix -- confirm every other
        raw-evidence-derived section agrees with real stage output too,
        not just UniProt."""
        gnomad_result = {"skipped": False, "error": None, "found": True, "global_af": 0.0001}
        clinvar_primary = {
            "clinical_significance": "Pathogenic",
            "review_status": "criteria provided",
            "variant_match": True,
        }
        clinvar_result = {"records": [clinvar_primary], "match_status": "matched", "primary_record": clinvar_primary}
        clingen_result = {"found": True, "gene_symbol": "BRCA1", "clinical_validity_summary": "Definitive"}
        alphafold_result = {
            "found": True,
            "mean_plddt": 91.2,
            "model_version": "v4",
            "affected_residue_band": "Very high",
        }

        vr = _build_variant_result_with_serialized_interpretation(
            gnomad_result=gnomad_result,
            clinvar_result=clinvar_result,
            clingen_result=clingen_result,
            alphafold_result=alphafold_result,
        )
        cr = vr["clinical_report"]

        self.assertTrue(cr["population_evidence"]["gnomad"]["found"])
        self.assertEqual(cr["population_evidence"]["gnomad"]["global_af"], 0.0001)
        self.assertTrue(cr["clinical_evidence"]["clinvar_available"])
        self.assertEqual(cr["clinical_evidence"]["clinvar"]["clinical_significance"], "Pathogenic")
        self.assertTrue(cr["clinical_evidence"]["clingen_available"])
        self.assertEqual(cr["clinical_evidence"]["clingen"]["gene_symbol"], "BRCA1")
        self.assertTrue(cr["structural_knowledge"]["available"])
        self.assertEqual(cr["structural_knowledge"]["mean_plddt"], 91.2)

    def test_markdown_report_shows_the_same_uniprot_entry_in_both_sections(self):
        """End-to-end: the exact contradiction the user found (Protein
        Knowledge section vs. Annotation Detail section disagreeing)
        must not reproduce in the rendered Markdown."""
        uniprot_result = {
            "found": True,
            "skipped": False,
            "error": None,
            "accession": "A0A3G1DJQ2",
            "protein_name": "Small humanin-like peptide 3",
            "reviewed": True,
            "organism": "Homo sapiens",
            "gene_symbol": "MTRNR2L3",
        }
        vr = _build_variant_result_with_serialized_interpretation(uniprot_result=uniprot_result)
        doc = {"generated_at": "now", "input_vcf": "x.vcf", "variant_count": 1, "variants": [vr]}
        markdown = ReportGenerator().generate(doc)

        self.assertNotIn("no entry resolved", markdown)
        self.assertIn("A0A3G1DJQ2", markdown)
        # Both the summary section and the audit-trail section must
        # name the same protein -- not one saying resolved and the
        # other contradicting it.
        self.assertEqual(markdown.count("Small humanin-like peptide 3"), 2)


class TestErrorStateNeverConflatedWithNotFound(unittest.TestCase):
    """A failed external lookup must be reported as a service issue,
    never as 'no biological evidence' -- covers issue #2's report-side
    requirement and generalizes it to every raw-evidence-derived
    section per the full-report audit."""

    def test_interpro_error_is_distinct_from_no_domains(self):
        interpro_error_result = {"found": False, "error": "InterPro REST API request failed after 3 attempts: boom"}
        vr = _build_variant_result_with_serialized_interpretation(interpro_result=interpro_error_result)
        prot = vr["clinical_report"]["protein_knowledge"]
        self.assertFalse(prot["interpro_available"])
        self.assertIn("boom", prot["interpro_error"])

        doc = {"generated_at": "now", "input_vcf": "x.vcf", "variant_count": 1, "variants": [vr]}
        markdown = ReportGenerator().generate(doc)
        self.assertIn("external service issue", markdown)
        self.assertNotIn("InterPro/Pfam:** no annotation available", markdown)

    def test_uniprot_error_is_distinct_from_no_entry(self):
        uniprot_error_result = {"found": False, "error": "UniProt REST API request failed after 3 attempts: boom"}
        vr = _build_variant_result_with_serialized_interpretation(uniprot_result=uniprot_error_result)
        prot = vr["clinical_report"]["protein_knowledge"]
        self.assertFalse(prot["uniprot_available"])
        self.assertIsNotNone(prot["uniprot_error"])

        doc = {"generated_at": "now", "input_vcf": "x.vcf", "variant_count": 1, "variants": [vr]}
        markdown = ReportGenerator().generate(doc)
        self.assertIn("external service issue", markdown)
        self.assertNotIn("UniProt:** no entry resolved", markdown)

    def test_clinvar_and_clingen_errors_are_distinct_from_no_record(self):
        clinvar_error = {"records": [], "error": "ClinVar E-utilities request failed"}
        clingen_error = {"found": False, "error": "ClinGen API request failed"}
        vr = _build_variant_result_with_serialized_interpretation(
            clinvar_result=clinvar_error, clingen_result=clingen_error
        )
        clin = vr["clinical_report"]["clinical_evidence"]
        self.assertIsNotNone(clin["clinvar_error"])
        self.assertIsNotNone(clin["clingen_error"])

    def test_alphafold_error_is_distinct_from_no_structure(self):
        alphafold_error = {"found": False, "error": "AlphaFold DB request failed"}
        vr = _build_variant_result_with_serialized_interpretation(alphafold_result=alphafold_error)
        struct = vr["clinical_report"]["structural_knowledge"]
        self.assertFalse(struct["available"])
        self.assertIsNotNone(struct["error"])


class TestErrorVsSkipNeverConflatedForBlastMmspliceAlphaMissense(unittest.TestCase):
    """
    Regression tests for a second real bug, found while building the
    Pydantic schema layer (pipeline/stage_schemas.py): `_run_protein_stage`,
    `_run_blast_stage`, and `_run_mmsplice_stage` in
    pipeline/orchestrator.py used to fold a genuine exception into the
    exact same dict shape as a normal, expected skip (e.g. "no sequence
    context" / "variant not splice-eligible"), with no separate `error`
    key -- so a crashed BLAST search rendered identically to "genuinely
    no homology hits", and a crashed AlphaMissense/MMSplice call
    rendered identically to "not an eligible variant type". RNA-FM and
    AlphaMissense had the identical collapse, found by auditing the
    rest of the orchestrator for the same pattern.

    Fix: those methods now set an explicit `error` key on their
    exception paths (while leaving `skipped`/`supported`/`predicted`
    unchanged, since other stages still gate on them for control flow
    -- see each method's own comment in pipeline/orchestrator.py), and
    that distinction is threaded all the way to both the clinical
    report's summary sections and the Annotation Detail Markdown trail.
    """

    def test_blast_error_is_distinct_from_no_hits(self):
        blast_error = {
            "hits": [],
            "hit_count": 0,
            "skipped": True,
            "reason": "boom",
            "error": "BLAST server unreachable: boom",
        }
        vr = _build_variant_result_with_serialized_interpretation(blast_result=blast_error)
        seq = vr["clinical_report"]["sequence_context"]
        self.assertEqual(seq["blast"]["error"], "BLAST server unreachable: boom")

        doc = {"generated_at": "now", "input_vcf": "x.vcf", "variant_count": 1, "variants": [vr]}
        markdown = ReportGenerator().generate(doc)
        self.assertIn("BLAST:** _lookup failed", markdown)
        self.assertNotIn("**BLAST:** 0 homology hit(s)", markdown)
        # The Annotation Detail trail must agree, not contradict it.
        self.assertIn("### BLAST Results", markdown)
        self.assertNotIn("_No significant BLAST hits._", markdown)

    def test_genuine_no_blast_hits_is_unaffected(self):
        blast_no_hits = {"hits": [], "hit_count": 0, "skipped": False}
        vr = _build_variant_result_with_serialized_interpretation(blast_result=blast_no_hits)
        seq = vr["clinical_report"]["sequence_context"]
        self.assertIsNone(seq["blast"]["error"])

    def test_mmsplice_error_is_distinct_from_ineligible(self):
        mmsplice_error = {
            "supported": False,
            "predicted": False,
            "skip_reason": "Keras inference crashed",
            "interpretation": "Keras inference crashed",
            "error": "Keras inference crashed",
        }
        vr = _build_variant_result_with_serialized_interpretation(mmsplice_result=mmsplice_error)
        ai = vr["clinical_report"]["ai_consensus"]
        self.assertEqual(len(ai["model_errors"]), 1)
        self.assertEqual(ai["model_errors"][0]["source"], "MMSplice")
        self.assertEqual(ai["model_errors"][0]["error"], "Keras inference crashed")

        doc = {"generated_at": "now", "input_vcf": "x.vcf", "variant_count": 1, "variants": [vr]}
        markdown = ReportGenerator().generate(doc)
        self.assertIn("MMSplice:** _lookup failed", markdown)
        self.assertIn("### MMSplice (Splice Effect Prediction)", markdown)
        self.assertIn("_Failed: Keras inference crashed", markdown)

    def test_alphamissense_error_is_distinct_from_ineligible(self):
        am_error = {"skipped": True, "reason": "tabix crashed", "error": "tabix crashed"}
        vr = _build_variant_result_with_serialized_interpretation(alphamissense_result=am_error)
        ai = vr["clinical_report"]["ai_consensus"]
        self.assertEqual(len(ai["model_errors"]), 1)
        self.assertEqual(ai["model_errors"][0]["source"], "AlphaMissense")

        doc = {"generated_at": "now", "input_vcf": "x.vcf", "variant_count": 1, "variants": [vr]}
        markdown = ReportGenerator().generate(doc)
        self.assertIn("AlphaMissense:** _lookup failed", markdown)
        self.assertIn("_Failed: tabix crashed", markdown)

    def test_a_real_verdict_and_a_crash_can_coexist_in_ai_consensus(self):
        # AlphaMissense crashing must never hide MMSplice's real result.
        am_error = {"skipped": True, "reason": "x", "error": "tabix crashed"}
        mmsplice_ok = {
            "supported": True,
            "predicted": True,
            "interpretation_category": "likely_no_effect",
            "delta_logit_psi": 0.1,
        }
        vr = _build_variant_result_with_serialized_interpretation(
            alphamissense_result=am_error, mmsplice_result=mmsplice_ok
        )
        ai = vr["clinical_report"]["ai_consensus"]
        self.assertEqual(len(ai["classifying_models"]), 1)
        self.assertEqual(ai["classifying_models"][0]["source"], "MMSplice")
        self.assertEqual(len(ai["model_errors"]), 1)
        self.assertEqual(ai["model_errors"][0]["source"], "AlphaMissense")

    def test_ineligible_alphamissense_is_still_silent_not_an_error(self):
        """The fix must not flip every skip into an error -- a real,
        legitimate ineligibility skip still produces no error entry."""
        am_ineligible = {"skipped": True, "reason": "variant is not an eligible missense substitution"}
        vr = _build_variant_result_with_serialized_interpretation(alphamissense_result=am_ineligible)
        ai = vr["clinical_report"]["ai_consensus"]
        self.assertEqual(ai["model_errors"], [])


class TestBuildClinicalReportBackwardCompatibility(unittest.TestCase):
    """The new `raw_evidence` kwarg is additive -- a caller that still
    doesn't pass it must not crash (falls back to the old, empty-dict
    behavior)."""

    def test_omitting_raw_evidence_does_not_raise(self):
        ir = {
            "acmg_classification": "Uncertain Significance",
            "triggered_rules": [],
            "confidence_pending": True,
            "priority_pending": True,
        }
        report = build_clinical_report(ir, _VARIANT_DICT)
        self.assertIsNotNone(report)
        self.assertFalse(report["protein_knowledge"]["uniprot_available"])

    def test_none_interpretation_result_still_returns_none(self):
        self.assertIsNone(build_clinical_report(None, _VARIANT_DICT))
        self.assertIsNone(build_clinical_report({"error": "boom"}, _VARIANT_DICT))


if __name__ == "__main__":
    unittest.main()
