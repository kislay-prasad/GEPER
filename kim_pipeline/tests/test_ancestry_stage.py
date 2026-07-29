"""
tests/test_ancestry_stage.py
─────────────────────────────
Tests for Task 5: Ancestry Inference module.

Covers:
- EUR bias when all AIMs are hom-ref European alleles
- AFR bias with African-frequency alleles
- Low confidence when no AIMs called
- population_probabilities sum ≈ 1.0
- run() produces JSON and HTML report files
- AncestryResult is JSON-serialisable
- Minimum 15 test cases
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.ancestry.stage import (
    AncestryStage, AncestryResult,
    _parse_vcf_genotypes, _mle_ancestry, _confidence_level,
)
from pipeline.ancestry.markers import AIM_PANEL, POPULATIONS


# ─── Synthetic VCF helpers ────────────────────────────────────────────────────

def _write_vcf(path: str, records: list) -> None:
    """Write minimal VCF. records = [(chrom, pos, ref, alt), ...]"""
    with open(path, "w") as fh:
        fh.write("##fileformat=VCFv4.1\n")
        fh.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
        for chrom, pos, ref, alt in records:
            fh.write(f"chr{chrom}\t{pos}\t.\t{ref}\t{alt}\t100\tPASS\t.\n")


def _write_empty_vcf(path: str) -> None:
    with open(path, "w") as fh:
        fh.write("##fileformat=VCFv4.1\n")
        fh.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")


def _european_alt_markers() -> list:
    """Return VCF records for markers where EUR alt-allele freq is highest."""
    records = []
    for rsid, info in AIM_PANEL.items():
        freqs = info["allele_frequencies"]
        eur_freq = freqs["EUR"]
        # Only include if EUR is dominant
        if eur_freq == max(freqs.values()) and eur_freq > 0.6:
            records.append((info["chrom"], info["pos"], info["ref"], info["alt"]))
    return records


def _african_alt_markers() -> list:
    """Return VCF records for markers where AFR alt-allele freq is highest."""
    records = []
    for rsid, info in AIM_PANEL.items():
        freqs = info["allele_frequencies"]
        afr_freq = freqs["AFR"]
        if afr_freq == max(freqs.values()) and afr_freq > 0.6:
            records.append((info["chrom"], info["pos"], info["ref"], info["alt"]))
    return records


# ─── Unit tests for helper functions ─────────────────────────────────────────

class TestConfidenceLevel(unittest.TestCase):

    def test_high_confidence(self):
        self.assertEqual(_confidence_level(50), "High")

    def test_medium_confidence(self):
        self.assertEqual(_confidence_level(15), "Medium")

    def test_low_confidence(self):
        self.assertEqual(_confidence_level(5), "Low")
        self.assertEqual(_confidence_level(0), "Low")


class TestVcfGenotypeParser(unittest.TestCase):

    def test_parses_alt_record(self):
        with tempfile.NamedTemporaryFile(suffix=".vcf", mode="w", delete=False) as f:
            f.write("##fileformat=VCFv4.1\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
            f.write("chr1\t159175354\t.\tT\tC\t100\tPASS\t.\n")
            fname = f.name
        try:
            gts = _parse_vcf_genotypes(fname)
            self.assertIn(("1", 159175354), gts)
        finally:
            os.unlink(fname)

    def test_empty_vcf_returns_empty(self):
        with tempfile.NamedTemporaryFile(suffix=".vcf", mode="w", delete=False) as f:
            f.write("##fileformat=VCFv4.1\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
            fname = f.name
        try:
            gts = _parse_vcf_genotypes(fname)
            self.assertEqual(gts, {})
        finally:
            os.unlink(fname)

    def test_missing_file_returns_empty(self):
        gts = _parse_vcf_genotypes("/nonexistent.vcf")
        self.assertEqual(gts, {})


class TestMleAncestry(unittest.TestCase):

    def test_probs_sum_to_one(self):
        """With any set of markers, probabilities must sum to ~1.0."""
        called = {rsid: True for rsid in list(AIM_PANEL.keys())[:20]}
        probs = _mle_ancestry(called)
        total = sum(probs.values())
        self.assertAlmostEqual(total, 1.0, places=4)

    def test_empty_markers_gives_uniform(self):
        """Zero markers → uniform distribution."""
        probs = _mle_ancestry({})
        total = sum(probs.values())
        self.assertAlmostEqual(total, 1.0, places=4)
        for pop, p in probs.items():
            self.assertAlmostEqual(p, 1.0 / len(POPULATIONS), places=3)

    def test_eur_dominant_markers_favours_eur(self):
        """Calling alt alleles at high-EUR-frequency positions favours EUR."""
        # Pick markers where EUR alt freq >> others
        eur_rsids = [
            rsid for rsid, info in AIM_PANEL.items()
            if info["allele_frequencies"]["EUR"] == max(info["allele_frequencies"].values())
            and info["allele_frequencies"]["EUR"] > 0.7
        ]
        called = {rsid: True for rsid in eur_rsids}
        probs = _mle_ancestry(called)
        eur_prob = probs.get("EUR", 0.0)
        self.assertEqual(max(probs, key=probs.__getitem__), "EUR",
                         f"EUR should be primary pop; got {probs}")

    def test_afr_dominant_markers_favours_afr(self):
        """Calling alt alleles at high-AFR-frequency positions favours AFR."""
        afr_rsids = [
            rsid for rsid, info in AIM_PANEL.items()
            if info["allele_frequencies"]["AFR"] == max(info["allele_frequencies"].values())
            and info["allele_frequencies"]["AFR"] > 0.7
        ]
        called = {rsid: True for rsid in afr_rsids}
        probs = _mle_ancestry(called)
        self.assertEqual(max(probs, key=probs.__getitem__), "AFR",
                         f"AFR should be primary pop; got {probs}")


# ─── Full stage integration tests ─────────────────────────────────────────────

class TestAncestryStageRun(unittest.TestCase):

    def _run(self, vcf_records: list, sample_id: str = "S01") -> AncestryResult:
        with tempfile.TemporaryDirectory() as tmpdir:
            vcf_path = os.path.join(tmpdir, "test.vcf")
            out_dir = os.path.join(tmpdir, "ancestry_out")
            if vcf_records is None:
                _write_empty_vcf(vcf_path)
            else:
                _write_vcf(vcf_path, vcf_records)
            stage = AncestryStage(cfg={})
            result = stage.run(vcf_path=vcf_path, output_dir=out_dir, sample_id=sample_id)
        return result

    def test_empty_vcf_returns_low_confidence(self):
        """No AIM calls → Low confidence."""
        result = self._run([])
        self.assertEqual(result.confidence, "Low")

    def test_empty_vcf_probabilities_sum_to_one(self):
        """Even with no calls, population_probabilities must sum to ~1.0."""
        result = self._run([])
        total = sum(result.population_probabilities.values())
        self.assertAlmostEqual(total, 1.0, places=4)

    def test_empty_vcf_markers_called_zero(self):
        result = self._run([])
        self.assertEqual(result.markers_called, 0)

    def test_eur_alleles_favour_eur(self):
        """VCF with EUR-dominant AIM alleles → EUR as primary population."""
        eur_records = _european_alt_markers()
        if not eur_records:
            self.skipTest("No strong EUR markers available in panel")
        result = self._run(eur_records[:40])
        self.assertEqual(result.primary_population, "EUR",
                         f"Expected EUR, got {result.primary_population}; probs={result.population_probabilities}")

    def test_afr_alleles_favour_afr(self):
        """VCF with AFR-dominant AIM alleles → AFR as primary population."""
        afr_records = _african_alt_markers()
        if not afr_records:
            self.skipTest("No strong AFR markers available in panel")
        result = self._run(afr_records[:40])
        self.assertEqual(result.primary_population, "AFR",
                         f"Expected AFR, got {result.primary_population}; probs={result.population_probabilities}")

    def test_probs_sum_to_one(self):
        """population_probabilities must always sum to ~1.0."""
        eur_records = _european_alt_markers()
        result = self._run(eur_records[:10])
        total = sum(result.population_probabilities.values())
        self.assertAlmostEqual(total, 1.0, places=4,
                               msg=f"Probs sum to {total}: {result.population_probabilities}")

    def test_all_populations_represented(self):
        """All 5 superpopulations should appear in population_probabilities."""
        result = self._run([])
        for pop in POPULATIONS:
            self.assertIn(pop, result.population_probabilities,
                          f"Population {pop} missing from results")

    def test_run_produces_json_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vcf_path = os.path.join(tmpdir, "test.vcf")
            out_dir = os.path.join(tmpdir, "out")
            _write_empty_vcf(vcf_path)
            result = AncestryStage(cfg={}).run(vcf_path=vcf_path, output_dir=out_dir)
            self.assertTrue(os.path.isfile(result.report_json_path))

    def test_run_produces_html_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vcf_path = os.path.join(tmpdir, "test.vcf")
            out_dir = os.path.join(tmpdir, "out")
            _write_empty_vcf(vcf_path)
            result = AncestryStage(cfg={}).run(vcf_path=vcf_path, output_dir=out_dir)
            self.assertTrue(os.path.isfile(result.report_html_path))

    def test_result_json_serialisable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vcf_path = os.path.join(tmpdir, "test.vcf")
            out_dir = os.path.join(tmpdir, "out")
            _write_empty_vcf(vcf_path)
            result = AncestryStage(cfg={}).run(vcf_path=vcf_path, output_dir=out_dir)
        try:
            json.dumps(result.to_dict())
        except (TypeError, ValueError) as e:
            self.fail(f"AncestryResult not JSON-serialisable: {e}")

    def test_sample_id_in_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vcf_path = os.path.join(tmpdir, "test.vcf")
            out_dir = os.path.join(tmpdir, "out")
            _write_empty_vcf(vcf_path)
            result = AncestryStage(cfg={}).run(vcf_path=vcf_path, output_dir=out_dir, sample_id="MY_ID")
        self.assertEqual(result.sample_id, "MY_ID")

    def test_markers_evaluated_equals_panel_size(self):
        """markers_evaluated should equal the total AIM panel size."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vcf_path = os.path.join(tmpdir, "test.vcf")
            out_dir = os.path.join(tmpdir, "out")
            _write_empty_vcf(vcf_path)
            result = AncestryStage(cfg={}).run(vcf_path=vcf_path, output_dir=out_dir)
        self.assertEqual(result.markers_evaluated, len(AIM_PANEL))

    def test_missing_vcf_returns_result(self):
        """Missing VCF never raises — returns graceful result."""
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = os.path.join(tmpdir, "out")
            result = AncestryStage(cfg={}).run(vcf_path="/nonexistent.vcf", output_dir=out_dir)
        self.assertIsInstance(result, AncestryResult)
        self.assertEqual(result.markers_called, 0)
        self.assertEqual(result.confidence, "Low")

    def test_json_report_contains_primary_population(self):
        """JSON report should include primary_population key."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vcf_path = os.path.join(tmpdir, "test.vcf")
            out_dir = os.path.join(tmpdir, "out")
            _write_empty_vcf(vcf_path)
            result = AncestryStage(cfg={}).run(vcf_path=vcf_path, output_dir=out_dir)
            with open(result.report_json_path) as f:
                data = json.load(f)
        self.assertIn("primary_population", data)
        self.assertIn("population_probabilities", data)
        self.assertIn("confidence", data)

    def test_html_report_contains_population_names(self):
        """HTML report should list all 5 population codes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vcf_path = os.path.join(tmpdir, "test.vcf")
            out_dir = os.path.join(tmpdir, "out")
            _write_empty_vcf(vcf_path)
            result = AncestryStage(cfg={}).run(vcf_path=vcf_path, output_dir=out_dir)
            html = Path(result.report_html_path).read_text()
        for pop in POPULATIONS:
            self.assertIn(pop, html, f"Population {pop} missing from HTML report")

    def test_aim_panel_has_100_plus_markers(self):
        """Sanity check: AIM panel should have ≥100 markers."""
        self.assertGreaterEqual(len(AIM_PANEL), 100)

    def test_aim_panel_all_populations_covered(self):
        """Every marker should have frequencies for all 5 superpopulations."""
        for rsid, info in AIM_PANEL.items():
            freqs = info["allele_frequencies"]
            for pop in POPULATIONS:
                self.assertIn(pop, freqs, f"Marker {rsid} missing {pop} frequency")


if __name__ == "__main__":
    unittest.main()
