"""
Parity test for hive card
COMPLETENESS-the-PDF-RENDERERS-ARE-MISSING-FOUR-PER-VARIANT-DISCLOSURE-SURFACES-THAT-MARKDOWN-HAS
(human ruling: "(A), ALL NINE... fixing one section reproduces it eight
times"; acceptance criterion: "for one run, every per-variant section
Markdown renders must appear in the full PDF. The AI Consensus 'not
clinically validated' caveat is the one he named; assert it by its
text, not by a section heading.").

Builds ONE synthetic `clinical_report` (via the real
`build_clinical_report()`, so this test exercises the same producer
both renderers read from) carrying real content for all nine
previously-PDF-missing sections plus the partial (conflict_resolution
structured detail), renders both the Markdown and the full PDF from
the identical document, and asserts every one of those content strings
is present in the extracted PDF text. The AI Consensus caveat is
asserted by its literal text, per the acceptance criterion, not by the
presence of a heading.
"""

import os
import re
import tempfile
import unittest

from report.clinical_report_builder import build_clinical_report
from report.report_generator import ReportGenerator
from report.summary import generate_pdf

try:
    from pypdf import PdfReader

    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False

AM_CAVEAT = (
    "not clinically validated; not approved for clinical use. This is a raw model "
    "score, not a validated clinical pathogenicity measure."
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _all_pdf_text(path: str) -> str:
    reader = PdfReader(path)
    return "\n".join(page.extract_text() for page in reader.pages)


def _ir() -> dict:
    return {
        "variant": {"chrom": "1", "pos": 1001, "ref": "C", "alt": "A"},
        "gene_symbol": "GENE1",
        "acmg_classification": "Uncertain significance",
        "triggered_rules": [],
        "not_triggered_rules": [],
        "not_evaluated_rules": [],
        "combining_rule_trace": [],
        "supporting_evidence": [],
        "conflicting_evidence": ["Free-text conflict bullet ZULU9."],
        # (4) Priority Score
        "priority_pending": False,
        "priority_score": 87.5,
        "priority_category": "High",
        "priority_rank": 1,
        "priority_explanation": ["Priority reason bullet FOXTROT7."],
        # (6, partial) structured conflict_resolution detail
        "conflict_summary": "Conflict summary text KILO3.",
        "conflict_score": 0.6,
        "conflict_severity": "Major",
        "conflict_resolution": "Resolved in favour of clinical evidence.",
        "conflict_list": [
            {
                "category": "Population vs Computational",
                "conflict_type": "PopulationVsComputational",
                "evidence_a": {"source": "gnomAD", "statement": "Rare"},
                "evidence_b": {"source": "AlphaMissense", "statement": "Benign"},
                "severity": "Major",
                "resolution": "Resolution text ROMEO4.",
                "resolution_rationale": "Rationale text SIERRA5.",
                "confidence_impact": "Lowered",
                "priority_impact": "Raised",
            }
        ],
        # (7) AI Consensus
        "ai_consensus": [
            {"source": "AlphaMissense", "prediction": "Likely pathogenic", "score": 0.91},
            {"source": "MMSplice", "prediction": "No splice effect", "score": 0.02},
        ],
        "ai_context_models": ["HyenaDNA"],
        "ai_model_errors": [],
        "evidence_sources": ["gnomAD", "ClinVar", "UniProt EVSRC1"],
        "explainability": {
            "decision_summary": "Decision summary text ECHO6.",
            "reasoning_chain": ["Reasoning step ALFA1."],
            "evidence_contributed": ["gnomAD"],
            "evidence_not_contributed": [],
            "ai_models_influential": [],
            "ai_models_contextual_only": [],
            "conflicts_detected": [],
            "conflicts_resolved": "No conflicts to resolve.",
            "remaining_uncertainties": [],
            "limitations": [],
            "confidence_rationale": "Confidence rationale text BRAVO2.",
            "priority_rationale": "Priority rationale text CHARLIE3.",
            "evidence_trace": [],
        },
    }


def _raw_evidence() -> dict:
    return {
        # (8) Protein Knowledge
        "uniprot": {
            "found": True,
            "accession": "P99999UNIPROTACC",
            "protein_name": "Test Protein DELTA8",
            "reviewed": True,
        },
        "interpro": {
            "found": True,
            "protein_position": 42,
            "affected_domains": [{"name": "KinaseDomainGOLF9"}],
        },
        # (9) Structural Knowledge
        "alphafold": {
            "found": True,
            "affected_residue_band": "High",
            "model_version": "AFDBv4HOTEL0",
            "pdb_url": "https://example.invalid/structure/INDIA1",
        },
        # (10) Population Evidence (main)
        "gnomad": {
            "found": True,
            "global_af": 0.00012,
            "population_breakdown": {"sas": {"af": 0.0002}},
        },
        "dbsnp": {"found": True, "rsid": "rs999888JULIETT2"},
        # (12) Clinical Evidence
        "clinvar": {
            "match_status": "matched",
            "primary_record": {
                "clinical_significance": "Uncertain significance KILO3CLINVAR",
                "review_status": "criteria provided",
            },
        },
        "clingen": {
            "found": True,
            "gene_symbol": "GENE1",
            "clinical_validity_summary": "Definitive LIMA4CLINGEN",
        },
        # (13) Sequence Context
        "blast": {"hit_count": 3},
    }


def _document() -> dict:
    variant_dict = {"chrom": "1", "pos": 1001, "ref": "C", "alt": "A"}
    ir = _ir()
    clinical_report = build_clinical_report(ir, variant_dict=variant_dict, raw_evidence=_raw_evidence())
    variant = {
        "variant": variant_dict,
        "interpretation_result": {"gene_symbol": "GENE1"},
        "candidate_interpretation": clinical_report,
    }
    return {
        "geper_version": "test",
        "generated_at": "2026-09-11T00:00:00+00:00",
        "input_vcf": "nine_section_parity_test.vcf",
        "assembly": "GRCh38",
        "vcf_samples": ["SAMPLE01"],
        "variant_count": 1,
        "variants": [variant],
    }


# Content strings unique to each of the nine previously-missing sections
# (plus the partial), keyed by name for a legible per-section assertion.
_EXPECTED = {
    "priority": "Priority reason bullet FOXTROT7.",
    "conflict_resolution_partial": "Rationale text SIERRA5.",
    "ai_consensus_caveat": AM_CAVEAT,
    "ai_consensus_mmsplice": "MMSplice",
    "protein_knowledge": "Test Protein DELTA8",
    "protein_knowledge_domain": "KinaseDomainGOLF9",
    "structural_knowledge": "HOTEL0",
    "population_evidence_dbsnp": "JULIETT2",
    "clinical_evidence_clinvar": "KILO3CLINVAR",
    "clinical_evidence_clingen": "LIMA4CLINGEN",
    "sequence_context": "3 homology hit(s)",
    "evidence_sources": "UniProt EVSRC1",
    "explainability": "Decision summary text ECHO6.",
}


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestNineSectionPdfMarkdownParity(unittest.TestCase):
    def test_markdown_actually_renders_all_expected_content(self):
        # Sanity check on the fixture itself: if Markdown doesn't render
        # this content either, the PDF assertions below would be
        # vacuously true rather than a real parity check.
        md = ReportGenerator().generate(_document())
        norm_md = _normalize(md)
        for name, needle in _EXPECTED.items():
            self.assertIn(_normalize(needle), norm_md, f"fixture gap: Markdown itself doesn't render {name!r}")

    def test_full_pdf_renders_all_nine_sections_and_the_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            generate_pdf(_document(), out)
            text = _normalize(_all_pdf_text(out))

        for name, needle in _EXPECTED.items():
            self.assertIn(_normalize(needle), text, f"PDF missing content for section {name!r}")

    def test_ai_consensus_caveat_asserted_by_its_literal_text(self):
        # Acceptance criterion, verbatim: assert the caveat BY ITS TEXT,
        # not by presence of a heading like "AI Consensus".
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            generate_pdf(_document(), out)
            text = _normalize(_all_pdf_text(out))
        self.assertIn(_normalize(AM_CAVEAT), text)


if __name__ == "__main__":
    unittest.main()
