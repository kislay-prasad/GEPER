"""
tests/test_consequence_not_determined_sentinel.py
────────────────────────────────────────────────────
Tests for the CONSEQUENCE_NOT_DETERMINED sentinel (design:
pipeline/annotation/stage.py, pipeline/reporting/clinical_sections.py,
pipeline/reporting/pdf_report.py, pipeline/reporting/stage.py).

CONSEQUENCE_NOT_DETERMINED is a non-empty string, so the pre-existing
`raw or "—"` idiom at each render site does NOT let it fall through to
the dash placeholder -- it would print the raw internal sentinel string
verbatim in a clinician-facing report column. consequence_display_label()
is the single explicit-mapping function all render sites now call instead.

The hazard this file guards against is not the helper misbehaving (that
is cheap to get right and cheap to test in isolation) -- it is a render
SITE quietly regressing back to the `or` idiom. Tests 4 and 5 exercise
the actual render functions (not just the helper) and fail specifically
in that regression, which is why they are the point of this file.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.annotation.stage import AnnotationStage, CONSEQUENCE_NOT_DETERMINED
from pipeline.reporting.clinical_sections import consequence_display_label
from pipeline.reporting.pdf_report import render_clinical_pdf
from pipeline.reporting.stage import _annotation_summary_to_html, _variants_to_html_table


# ─── 1-3: the fallback rule itself ──────────────────────────────────────────────


class TestConsequenceDisplayLabel:
    def test_naive_or_idiom_lets_the_sentinel_through_control(self):
        """Red-first, measured not argued: proves the trap is real. This
        is the exact idiom every render site used before this change --
        CONSEQUENCE_NOT_DETERMINED is a non-empty string, so `or` never
        fires and the raw sentinel passes through unchanged."""
        naive_result = CONSEQUENCE_NOT_DETERMINED or "—"
        assert naive_result == CONSEQUENCE_NOT_DETERMINED
        assert naive_result != "Not determined"

    def test_sentinel_maps_to_not_determined(self):
        assert consequence_display_label(CONSEQUENCE_NOT_DETERMINED) == "Not determined"

    def test_ordinary_values_unaffected(self):
        assert consequence_display_label("missense_variant") == "missense_variant"
        assert consequence_display_label(None) == "—"
        assert consequence_display_label("") == "—"


# ─── 4-5: the render sites themselves, not just the helper ─────────────────────


class TestHtmlRenderSiteUsesTheLabel:
    def test_sentinel_absent_and_label_present_in_rendered_html(self):
        variants = [
            {
                "chrom": "17",
                "pos": 100,
                "ref": "A",
                "alt": "T",
                "gene_name": "TP53",
                "transcript_id": "NM_000546.6",
                "hgvs": "NM_000546.6:c.1A>T",
                "consequence": CONSEQUENCE_NOT_DETERMINED,
                "zygosity": "Heterozygous",
                "gt": "0/1",
                "dp": 30,
                "ad": "15,15",
            }
        ]
        html = _variants_to_html_table(variants)
        # This is what fails if pipeline/reporting/stage.py's render site
        # (the `cells = [...]` list) regresses to `v.get("consequence") or
        # "—"` -- the raw sentinel would appear verbatim in the cell.
        assert CONSEQUENCE_NOT_DETERMINED not in html
        assert "Not determined" in html


class TestPdfRenderSiteUsesTheLabel:
    def _minimal_kwargs(self, consequence: str):
        from pipeline.reporting.clinical_sections import (
            normalize_patient_metadata,
            qc_status_summary,
            variant_dashboard,
            clinical_interpretation,
            merge_variants_with_acmg,
        )

        patient = normalize_patient_metadata(None)
        qc_rows = qc_status_summary({"q30_fraction": 0.9}, {"mean_depth": 30, "pct_mapped": 98})
        dashboard = variant_dashboard([{"classification": "Uncertain_Significance"}], {})
        interpretation = clinical_interpretation(dashboard)
        merged = merge_variants_with_acmg(
            [
                {
                    "chrom": "17",
                    "pos": 100,
                    "ref": "A",
                    "alt": "T",
                    "gene_name": "TP53",
                    "transcript_id": "NM_000546.6",
                    "hgvs": "NM_000546.6:c.1A>T",
                    "consequence": consequence,
                }
            ],
            [
                {
                    "chrom": "17",
                    "pos": 100,
                    "ref": "A",
                    "alt": "T",
                    "gene": "TP53",
                    "classification": "Uncertain_Significance",
                    "gnomad_af": 0.0001,
                }
            ],
        )
        return dict(
            patient_meta=patient,
            qc_rows=qc_rows,
            dashboard=dashboard,
            interpretation=interpretation,
            merged_variants=merged,
            reference_genome="GRCh38",
            pipeline_version="Bij AI v8",
        )

    def test_sentinel_absent_and_label_present_in_rendered_pdf(self, tmp_path):
        from pypdf import PdfReader

        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(
            out, sample_id="S01", **self._minimal_kwargs(CONSEQUENCE_NOT_DETERMINED)
        )
        text = "".join(p.extract_text() for p in PdfReader(out).pages)
        # This is what fails if pipeline/reporting/pdf_report.py:254
        # regresses to `v.get("consequence") or "—"` -- the raw sentinel
        # string would print verbatim in the Consequence column.
        assert CONSEQUENCE_NOT_DETERMINED not in text
        assert "Not determined" in text

    def test_ordinary_consequence_still_renders_unchanged(self, tmp_path):
        from pypdf import PdfReader

        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", **self._minimal_kwargs("missense_variant"))
        text = "".join(p.extract_text() for p in PdfReader(out).pages)
        assert "missense_variant" in text


# ─── 6-8: run-level disclosure ──────────────────────────────────────────────────


class TestRunLevelDisclosure:
    def _write_vcf(self, path: Path) -> None:
        path.write_text(
            "##fileformat=VCFv4.2\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"
            "chr17\t43057051\t.\tA\tT\t200\tPASS\t.\tGT:AD:DP\t0/1:15,10:25\n"
        )

    def test_no_gff_or_fasta_configured_is_unavailable(self, tmp_path):
        vcf = tmp_path / "filtered.vcf"
        self._write_vcf(vcf)
        stage = AnnotationStage({"annotation": {"require_gff": False}})
        result = stage.run(str(vcf), str(tmp_path / "ann_out"), sample_id="TEST")
        assert result.codon_resolution_available is False

    def test_gff_and_fasta_configured_is_available(self, tmp_path):
        # Proving the check can return dirty AND clean, per the floor
        # standard -- test 6a above is the dirty case, this is the clean
        # one. Neither file needs real CDS/sequence content: _load_cds_from_gff
        # tolerates zero CDS lines, and _FastaReader falls back to loading
        # whatever is on disk -- both just need to exist and parse without
        # raising, which is all `make_codon_provider_from_cfg` checks
        # before setting `_available = True`.
        # Reuses tests/test_annotation_stage.py::TestGffIndex._write_mini_gff3's
        # exact fixture -- AnnotationStage.run() loads a GffIndex (a
        # separate parser from the codon provider's own _load_cds_from_gff)
        # that requires at least one parseable gene/mRNA feature, unrelated
        # to codon resolution itself; reusing the known-working minimal
        # fixture avoids re-deriving GFF3 format requirements from scratch.
        gff = tmp_path / "ref.gff3"
        gff.write_text(
            "##gff-version 3\n"
            "NC_000017.11\tRefSeq\tgene\t43044295\t43125364\t.\t-\t.\t"
            "ID=gene-BRCA1;Name=BRCA1;gene=BRCA1;Dbxref=GeneID:672\n"
            "NC_000017.11\tRefSeq\tmRNA\t43044295\t43125364\t.\t-\t.\t"
            "ID=rna-NM_007294.4;Parent=gene-BRCA1;transcript_id=NM_007294.4;gene=BRCA1\n"
        )
        fasta = tmp_path / "ref.fasta"
        fasta.write_text(">chr17\nACGTACGTACGT\n")
        vcf = tmp_path / "filtered.vcf"
        self._write_vcf(vcf)
        stage = AnnotationStage(
            {
                "annotation": {"require_gff": False},
                "rna_analysis": {"refseq_gff": str(gff)},
                "alignment": {"reference_fasta": str(fasta)},
            }
        )
        result = stage.run(str(vcf), str(tmp_path / "ann_out"), sample_id="TEST")
        assert result.codon_resolution_available is True

    def test_html_disclaimer_present_when_unavailable(self):
        html = _annotation_summary_to_html(
            {"codon_resolution_available": False, "sample_id": "S01"}
        )
        assert "Codon-level consequence resolution" in html
        assert "was not available for this run" in html

    def test_html_disclaimer_absent_when_available(self):
        html = _annotation_summary_to_html({"codon_resolution_available": True, "sample_id": "S01"})
        assert "Codon-level consequence resolution" not in html

    def test_html_disclaimer_absent_when_key_missing_matches_default(self):
        # Field default is True (codon_resolution_available: bool = True),
        # so an older annotation.json written before this field existed
        # must not retroactively show the disclaimer.
        html = _annotation_summary_to_html({"sample_id": "S01"})
        assert "Codon-level consequence resolution" not in html

    def test_pdf_disclaimer_paragraph_present_when_unavailable(self, tmp_path):
        from pypdf import PdfReader

        kwargs = TestPdfRenderSiteUsesTheLabel()._minimal_kwargs("missense_variant")
        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(
            out,
            sample_id="S01",
            codon_resolution_disclaimer=(
                "Codon-level consequence resolution was not available for this run."
            ),
            **kwargs,
        )
        text = "".join(p.extract_text() for p in PdfReader(out).pages)
        assert "Codon-level consequence resolution was not available" in text

    def test_pdf_disclaimer_paragraph_absent_when_none(self, tmp_path):
        from pypdf import PdfReader

        kwargs = TestPdfRenderSiteUsesTheLabel()._minimal_kwargs("missense_variant")
        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", codon_resolution_disclaimer=None, **kwargs)
        text = "".join(p.extract_text() for p in PdfReader(out).pages)
        assert "Codon-level consequence resolution was not available" not in text


# ─── 9: the correct-by-luck dependency, protected without touching it ──────────


def test_sentinel_never_collides_with_a_real_consequence_term():
    """Protects every equality-miss / set-membership site at once
    (stage.py:409, :1144; shared.py:315, :323, :437) without editing any
    of them -- they all compare against this same set of real SO terms,
    and stage.py:1144's "correct by luck" behaviour rests on the sentinel
    never being a member of it. Any future normalisation, prefix scheme,
    or case-fold that collides with a real term breaks THIS test first,
    before it silently breaks stage.py:1144 the way the deferral ruling
    warned it could."""
    real_consequence_terms = {
        "missense_variant",
        "synonymous_variant",
        "stop_gained",
        "stop_lost",
        "start_lost",
        "inframe_insertion",
        "inframe_deletion",
        "intron_variant",
        "5_prime_UTR_variant",
        "3_prime_UTR_variant",
        "intergenic_variant",
        "gene_region_variant",
        "coding_sequence_variant",
    }
    assert CONSEQUENCE_NOT_DETERMINED not in real_consequence_terms
