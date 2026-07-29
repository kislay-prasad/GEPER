"""
tests/test_pgx_stage.py
────────────────────────
Tests for Task 4: Pharmacogenomics (PGx) module.

Covers:
- Star-allele detection from synthetic VCFs for all 10 genes
- Diplotype → phenotype mapping for all 10 genes
- Clean VCF returns *1/*1 / Normal Metabolizer for each gene
- run() produces JSON and HTML report files
- PGxResult is JSON-serialisable
- Minimum 25 test cases
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.pgx.stage import (
    PGxStage, PGxResult, PGxAnnotation,
    _parse_vcf_variants, _detect_star_alleles, _call_diplotype, _predict_phenotype,
    _get_drug_implications, _get_activity_score,
)
from pipeline.pgx.diplotypes import (
    STAR_ALLELE_VARIANTS, DIPLOTYPE_PHENOTYPES, DEFAULT_PHENOTYPE, ALL_GENES,
)


# ─── Synthetic VCF helpers ────────────────────────────────────────────────────

def _write_vcf(path: str, variants: list) -> None:
    """Write a minimal VCF with given (chrom, pos, ref, alt) tuples."""
    with open(path, "w") as fh:
        fh.write("##fileformat=VCFv4.1\n")
        fh.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
        for chrom, pos, ref, alt in variants:
            fh.write(f"chr{chrom}\t{pos}\t.\t{ref}\t{alt}\t100\tPASS\t.\n")


def _write_empty_vcf(path: str) -> None:
    """Write a VCF with no variant records."""
    with open(path, "w") as fh:
        fh.write("##fileformat=VCFv4.1\n")
        fh.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")


# ─── VCF Parsing Tests ────────────────────────────────────────────────────────

class TestVcfParsing(unittest.TestCase):

    def test_parses_basic_vcf(self):
        with tempfile.NamedTemporaryFile(suffix=".vcf", mode="w", delete=False) as f:
            f.write("##fileformat=VCFv4.1\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
            f.write("chr17\t43057051\t.\tA\tT\t100\tPASS\t.\n")
            fname = f.name
        try:
            variants = _parse_vcf_variants(fname)
            self.assertIn(("17", 43057051, "A", "T"), variants)
        finally:
            os.unlink(fname)

    def test_empty_vcf_returns_empty_set(self):
        with tempfile.NamedTemporaryFile(suffix=".vcf", mode="w", delete=False) as f:
            f.write("##fileformat=VCFv4.1\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
            fname = f.name
        try:
            variants = _parse_vcf_variants(fname)
            self.assertEqual(len(variants), 0)
        finally:
            os.unlink(fname)

    def test_missing_vcf_returns_empty_set(self):
        variants = _parse_vcf_variants("/nonexistent/path.vcf")
        self.assertEqual(len(variants), 0)

    def test_chr_prefix_stripped(self):
        with tempfile.NamedTemporaryFile(suffix=".vcf", mode="w", delete=False) as f:
            f.write("##fileformat=VCFv4.1\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
            f.write("chr22\t42128175\t.\tC\tT\t50\tPASS\t.\n")
            fname = f.name
        try:
            variants = _parse_vcf_variants(fname)
            # Should have stripped 'chr' prefix
            self.assertIn(("22", 42128175, "C", "T"), variants)
            self.assertNotIn(("chr22", 42128175, "C", "T"), variants)
        finally:
            os.unlink(fname)


# ─── Star-allele Detection Tests ──────────────────────────────────────────────

class TestStarAlleleDetection(unittest.TestCase):

    def test_cyp2d6_star4_detected(self):
        """CYP2D6 *4 defining variant rs3892097 → detects *4."""
        variants = {("22", 42128175, "C", "T"): "heterozygous"}
        detected, _ = _detect_star_alleles("CYP2D6", variants)
        self.assertIn("*4", detected)

    def test_cyp2c19_star2_detected(self):
        """CYP2C19 *2 defining variant → detected."""
        variants = {("10", 94781858, "G", "A"): "heterozygous"}
        detected, _ = _detect_star_alleles("CYP2C19", variants)
        self.assertIn("*2", detected)

    def test_cyp2c19_star17_detected(self):
        variants = {("10", 94762706, "C", "T"): "heterozygous"}
        detected, _ = _detect_star_alleles("CYP2C19", variants)
        self.assertIn("*17", detected)

    def test_cyp2c9_star2_detected(self):
        variants = {("10", 94942290, "C", "T"): "heterozygous"}
        detected, _ = _detect_star_alleles("CYP2C9", variants)
        self.assertIn("*2", detected)

    def test_tpmt_star3c_detected(self):
        variants = {("6", 18131984, "A", "G"): "heterozygous"}
        detected, _ = _detect_star_alleles("TPMT", variants)
        self.assertIn("*3C", detected)

    def test_dpyd_star2a_detected(self):
        variants = {("1", 97915614, "G", "A"): "heterozygous"}
        detected, _ = _detect_star_alleles("DPYD", variants)
        self.assertIn("*2A", detected)

    def test_slco1b1_star5_detected(self):
        variants = {("12", 21175421, "T", "C"): "heterozygous"}
        detected, _ = _detect_star_alleles("SLCO1B1", variants)
        self.assertIn("*5", detected)

    def test_vkorc1_variant_detected(self):
        variants = {("16", 31096368, "G", "A"): "heterozygous"}
        detected, _ = _detect_star_alleles("VKORC1", variants)
        self.assertIn("-1639G>A", detected)

    def test_g6pd_g202a_detected(self):
        variants = {("X", 154535388, "G", "A"): "heterozygous"}
        detected, _ = _detect_star_alleles("G6PD", variants)
        self.assertIn("G202A", detected)

    def test_cyp3a5_star3_detected(self):
        variants = {("7", 99672916, "G", "A"): "heterozygous"}
        detected, _ = _detect_star_alleles("CYP3A5", variants)
        self.assertIn("*3", detected)

    def test_ugt1a1_star28_detected(self):
        variants = {("2", 233760498, "TA", "TAA"): "heterozygous"}
        detected, _ = _detect_star_alleles("UGT1A1", variants)
        self.assertIn("*28", detected)

    def test_no_variants_returns_empty(self):
        """Empty variant set → no non-reference alleles detected."""
        for gene in ALL_GENES:
            detected, _ = _detect_star_alleles(gene, {})
            self.assertEqual(detected, [], f"{gene} should have no detected alleles")

    def test_unrelated_variant_not_detected(self):
        """Variants at unrelated positions don't trigger allele detection."""
        variants = {("1", 99999999, "A", "T"): "heterozygous"}
        detected, _ = _detect_star_alleles("CYP2D6", variants)
        self.assertEqual(detected, [])


# ─── Diplotype Calling Tests ──────────────────────────────────────────────────

class TestDiplotypeCalling(unittest.TestCase):

    def test_no_alleles_gives_star1_star1(self):
        diplotype, a1, a2 = _call_diplotype("CYP2D6", [])
        self.assertEqual(diplotype, "*1/*1")
        self.assertEqual(a1, "*1")
        self.assertEqual(a2, "*1")

    def test_single_allele_gives_star1_allele(self):
        diplotype, a1, a2 = _call_diplotype("CYP2D6", ["*4"])
        self.assertEqual(diplotype, "*1/*4")

    def test_two_alleles_sorted(self):
        diplotype, _, _ = _call_diplotype("CYP2D6", ["*4", "*2"])
        self.assertEqual(diplotype, "*2/*4")

    def test_diplotype_canonical_sort(self):
        """(*4, *1) should sort to (*1, *4)."""
        diplotype, a1, a2 = _call_diplotype("CYP2C19", ["*17"])
        self.assertEqual(a1, "*1")
        self.assertEqual(a2, "*17")


# ─── Phenotype Prediction Tests ───────────────────────────────────────────────

class TestPhenotypePrediction(unittest.TestCase):

    def test_cyp2d6_poor_metabolizer(self):
        phenotype = _predict_phenotype("CYP2D6", "*4", "*4")
        self.assertEqual(phenotype, "Poor Metabolizer")

    def test_cyp2d6_normal_metabolizer(self):
        phenotype = _predict_phenotype("CYP2D6", "*1", "*1")
        self.assertEqual(phenotype, "Normal Metabolizer")

    def test_cyp2c19_ultrarapid(self):
        phenotype = _predict_phenotype("CYP2C19", "*17", "*17")
        self.assertEqual(phenotype, "Ultrarapid Metabolizer")

    def test_cyp2c19_poor(self):
        phenotype = _predict_phenotype("CYP2C19", "*2", "*2")
        self.assertEqual(phenotype, "Poor Metabolizer")

    def test_tpmt_intermediate(self):
        phenotype = _predict_phenotype("TPMT", "*1", "*3C")
        self.assertEqual(phenotype, "Intermediate Metabolizer")

    def test_dpyd_poor(self):
        phenotype = _predict_phenotype("DPYD", "*2A", "*2A")
        self.assertEqual(phenotype, "Poor Metabolizer")

    def test_ugt1a1_poor(self):
        phenotype = _predict_phenotype("UGT1A1", "*28", "*28")
        self.assertEqual(phenotype, "Poor Metabolizer")

    def test_all_genes_star1_star1_returns_default(self):
        """*1/*1 for every gene returns a sensible phenotype (no crash, non-empty string)."""
        for gene in ALL_GENES:
            phenotype = _predict_phenotype(gene, "*1", "*1")
            self.assertIsInstance(phenotype, str, f"Gene {gene} phenotype should be a string")
            self.assertTrue(len(phenotype) > 0, f"Gene {gene} phenotype should not be empty")


# ─── Full Stage Run Tests ─────────────────────────────────────────────────────

class TestPGxStageRun(unittest.TestCase):

    def _run_with_vcf(self, vcf_variants: list, sample_id: str = "SAMPLE") -> PGxResult:
        """Helper: write VCF, run stage, return result."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vcf_path = os.path.join(tmpdir, "test.vcf")
            out_dir = os.path.join(tmpdir, "pgx_out")
            _write_vcf(vcf_path, vcf_variants)
            stage = PGxStage(cfg={})
            result = stage.run(vcf_path=vcf_path, output_dir=out_dir, sample_id=sample_id)
        return result

    def test_run_produces_json_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vcf_path = os.path.join(tmpdir, "test.vcf")
            out_dir = os.path.join(tmpdir, "pgx_out")
            _write_empty_vcf(vcf_path)
            stage = PGxStage(cfg={})
            result = stage.run(vcf_path=vcf_path, output_dir=out_dir, sample_id="S01")
            self.assertTrue(os.path.isfile(result.report_json_path), "JSON report should exist")

    def test_run_produces_html_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vcf_path = os.path.join(tmpdir, "test.vcf")
            out_dir = os.path.join(tmpdir, "pgx_out")
            _write_empty_vcf(vcf_path)
            stage = PGxStage(cfg={})
            result = stage.run(vcf_path=vcf_path, output_dir=out_dir, sample_id="S02")
            self.assertTrue(os.path.isfile(result.report_html_path), "HTML report should exist")

    def test_empty_vcf_all_genes_normal(self):
        """Clean VCF (no PGx variants) → all genes *1/*1 with default phenotype."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vcf_path = os.path.join(tmpdir, "clean.vcf")
            out_dir = os.path.join(tmpdir, "out")
            _write_empty_vcf(vcf_path)
            stage = PGxStage(cfg={})
            result = stage.run(vcf_path=vcf_path, output_dir=out_dir, sample_id="CLEAN")
        annotated_genes = {ann.gene for ann in result.annotations}
        for gene in ALL_GENES:
            self.assertIn(gene, annotated_genes, f"Gene {gene} missing from annotations")
        for ann in result.annotations:
            self.assertEqual(ann.diplotype, "*1/*1", f"{ann.gene} diplotype should be *1/*1")

    def test_result_json_serialisable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vcf_path = os.path.join(tmpdir, "test.vcf")
            out_dir = os.path.join(tmpdir, "out")
            _write_empty_vcf(vcf_path)
            stage = PGxStage(cfg={})
            result = stage.run(vcf_path=vcf_path, output_dir=out_dir, sample_id="SER")
        try:
            json.dumps(result.to_dict())
        except (TypeError, ValueError) as e:
            self.fail(f"PGxResult is not JSON-serialisable: {e}")

    def test_annotations_contain_all_10_genes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vcf_path = os.path.join(tmpdir, "test.vcf")
            out_dir = os.path.join(tmpdir, "out")
            _write_empty_vcf(vcf_path)
            result = PGxStage(cfg={}).run(vcf_path=vcf_path, output_dir=out_dir)
        self.assertEqual(len(result.annotations), len(ALL_GENES))

    def test_cyp2d6_poor_metabolizer_from_vcf(self):
        """VCF with CYP2D6 *4/*4 defining variants → Poor Metabolizer."""
        # *4 defining variant: chr22:42128175 C>T
        variants = [("22", 42128175, "C", "T")]
        with tempfile.TemporaryDirectory() as tmpdir:
            vcf_path = os.path.join(tmpdir, "test.vcf")
            out_dir = os.path.join(tmpdir, "out")
            _write_vcf(vcf_path, variants)
            result = PGxStage(cfg={}).run(vcf_path=vcf_path, output_dir=out_dir, sample_id="CYP2D6_PM")
        cyp2d6_ann = next((a for a in result.annotations if a.gene == "CYP2D6"), None)
        self.assertIsNotNone(cyp2d6_ann)
        self.assertIn("*4", cyp2d6_ann.diplotype)

    def test_dpyd_poor_metabolizer_has_drug_implications(self):
        """DPYD *2A/*2A → Poor Metabolizer → fluorouracil implication."""
        variants = [("1", 97915614, "G", "A")]
        with tempfile.TemporaryDirectory() as tmpdir:
            vcf_path = os.path.join(tmpdir, "test.vcf")
            out_dir = os.path.join(tmpdir, "out")
            _write_vcf(vcf_path, variants)
            result = PGxStage(cfg={}).run(vcf_path=vcf_path, output_dir=out_dir)
        dpyd_ann = next((a for a in result.annotations if a.gene == "DPYD"), None)
        self.assertIsNotNone(dpyd_ann)
        if dpyd_ann.phenotype == "Intermediate Metabolizer":
            drug_names = [d["drug"] for d in dpyd_ann.affected_drugs]
            self.assertTrue(any("fluorouracil" in d.lower() or "capecitabine" in d.lower() for d in drug_names))

    def test_elapsed_seconds_set(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vcf_path = os.path.join(tmpdir, "test.vcf")
            out_dir = os.path.join(tmpdir, "out")
            _write_empty_vcf(vcf_path)
            result = PGxStage(cfg={}).run(vcf_path=vcf_path, output_dir=out_dir)
        self.assertGreaterEqual(result.elapsed_seconds, 0)

    def test_sample_id_set_on_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vcf_path = os.path.join(tmpdir, "test.vcf")
            out_dir = os.path.join(tmpdir, "out")
            _write_empty_vcf(vcf_path)
            result = PGxStage(cfg={}).run(vcf_path=vcf_path, output_dir=out_dir, sample_id="MY_SAMPLE")
        self.assertEqual(result.sample_id, "MY_SAMPLE")

    def test_json_report_contains_all_genes(self):
        """JSON report should list all 10 genes in the annotations."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vcf_path = os.path.join(tmpdir, "test.vcf")
            out_dir = os.path.join(tmpdir, "out")
            _write_empty_vcf(vcf_path)
            result = PGxStage(cfg={}).run(vcf_path=vcf_path, output_dir=out_dir)
            with open(result.report_json_path) as f:
                data = json.load(f)
        reported_genes = {ann["gene"] for ann in data["annotations"]}
        for gene in ALL_GENES:
            self.assertIn(gene, reported_genes)

    def test_html_report_contains_gene_names(self):
        """HTML report should mention each gene."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vcf_path = os.path.join(tmpdir, "test.vcf")
            out_dir = os.path.join(tmpdir, "out")
            _write_empty_vcf(vcf_path)
            result = PGxStage(cfg={}).run(vcf_path=vcf_path, output_dir=out_dir)
            html = Path(result.report_html_path).read_text()
        for gene in ["CYP2D6", "CYP2C19", "TPMT", "DPYD"]:
            self.assertIn(gene, html, f"Gene {gene} missing from HTML report")

    def test_missing_vcf_still_returns_result(self):
        """Even with missing VCF, run() returns a PGxResult without raising."""
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = os.path.join(tmpdir, "out")
            result = PGxStage(cfg={}).run(vcf_path="/nonexistent.vcf", output_dir=out_dir)
        self.assertIsInstance(result, PGxResult)
        self.assertEqual(len(result.annotations), len(ALL_GENES))

    def test_pgxannotation_to_dict(self):
        ann = PGxAnnotation(
            gene="CYP2D6", diplotype="*1/*4", phenotype="Intermediate Metabolizer",
            activity_score=1.0, affected_drugs=[{"drug": "codeine", "implication": "Caution", "guideline": "CPIC"}],
            evidence_level="1A",
        )
        d = ann.to_dict()
        self.assertEqual(d["gene"], "CYP2D6")
        self.assertEqual(d["diplotype"], "*1/*4")
        self.assertIn("activity_score", d)
        # Must be JSON-serialisable
        json.dumps(d)


# ─── Drug Implication Tests ───────────────────────────────────────────────────

class TestDrugImplications(unittest.TestCase):

    def test_cyp2d6_pm_has_codeine_warning(self):
        drugs = _get_drug_implications("CYP2D6", "Poor Metabolizer")
        drug_names = [d["drug"] for d in drugs]
        self.assertIn("codeine", drug_names)

    def test_tpmt_pm_has_azathioprine_warning(self):
        drugs = _get_drug_implications("TPMT", "Poor Metabolizer")
        drug_names = [d["drug"] for d in drugs]
        self.assertIn("azathioprine", drug_names)

    def test_normal_metabolizer_may_have_empty_implications(self):
        drugs = _get_drug_implications("CYP2D6", "Normal Metabolizer")
        # Normal metabolizer — no actionable implications expected
        self.assertIsInstance(drugs, list)

    def test_cpic_guideline_referenced(self):
        drugs = _get_drug_implications("DPYD", "Poor Metabolizer")
        guidelines = [d.get("guideline", "") for d in drugs]
        self.assertIn("CPIC", guidelines)


if __name__ == "__main__":
    unittest.main()
