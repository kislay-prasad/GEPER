"""
tests/test_phase2_data_sources.py
───────────────────────────────────
Tests for Phase 2 real data sources:
  - GnomadConstraintLookup  (pLI/LOEUF → lof_gene_intolerant)
  - HotspotLookup           (ClinVar P/LP counts + UniProt domains → in_hotspot)
  - FastaCodonContextProvider (missense/synonymous/stop_gained/stop_lost/start_lost)
"""
from __future__ import annotations

import gzip
import os
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.constraint.lookup import GnomadConstraintLookup, ConstraintRecord
from pipeline.hotspot.lookup import HotspotLookup
from pipeline.annotation.codon_provider import (
    FastaCodonContextProvider,
    _translate,
    _reverse_complement,
)
from pipeline.annotation.stage import NoCdsCodonContextProvider


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _write_constraint_tsv(path: Path, rows: list[dict]) -> None:
    """Write a minimal gnomAD constraint TSV."""
    header = "gene\ttranscript\tpLI\toe_lof_upper\n"
    lines = [
        f"{r['gene']}\t{r.get('transcript','NM_000')}\t{r['pLI']}\t{r['oe_lof_upper']}\n"
        for r in rows
    ]
    path.write_text(header + "".join(lines))


def _write_clinvar_tsv(path: Path, rows: list[dict]) -> None:
    """Write a minimal ClinVar variant_summary TSV (gzipped)."""
    header = (
        "#Chromosome\tPositionVCF\tReferenceAlleleVCF\tAlternateAlleleVCF\t"
        "ClinicalSignificance\tType\tGeneSymbol\n"
    )
    lines = [
        (
            f"{r['chrom']}\t{r['pos']}\t{r.get('ref','A')}\t{r.get('alt','T')}\t"
            f"{r['sig']}\t{r.get('type','single nucleotide variant')}\t"
            f"{r.get('gene','GENE1')}\n"
        )
        for r in rows
    ]
    with gzip.open(str(path), "wt") as fh:
        fh.write(header + "".join(lines))


def _write_domains_bed(path: Path, intervals: list[tuple]) -> None:
    """Write a minimal UniProt domains BED file (0-based coords)."""
    lines = [
        f"{chrom}\t{start}\t{end}\t{name}\n"
        for chrom, start, end, name in intervals
    ]
    path.write_text("".join(lines))


def _write_fasta(path: Path, seqs: dict[str, str]) -> None:
    """Write a minimal FASTA file."""
    lines = []
    for name, seq in seqs.items():
        lines.append(f">{name}\n")
        for i in range(0, len(seq), 60):
            lines.append(seq[i:i+60] + "\n")
    path.write_text("".join(lines))


def _write_gff3_with_cds(path: Path) -> None:
    """Minimal GFF3 with both exon and CDS features for a + strand gene on chr1.

    Gene: 1000–1099 (one exon/CDS block, 99 bp from 1001)
    Transcript: NM_CODON1
    exon + CDS: 1001–1099 (1-based)
    First codon = pos 1001-1003 (ATG = Met start)
    """
    path.write_text(
        "##gff-version 3\n"
        "chr1\t.\tgene\t1000\t1099\t.\t+\t.\tID=gene-G1;Name=G1\n"
        "chr1\t.\tmRNA\t1000\t1099\t.\t+\t.\t"
        "ID=NM_CODON1;Parent=gene-G1;transcript_id=NM_CODON1\n"
        "chr1\t.\texon\t1001\t1099\t.\t+\t.\tParent=NM_CODON1\n"
        "chr1\t.\tCDS\t1001\t1099\t.\t+\t0\tParent=NM_CODON1\n"
    )


def _write_gff3_minus_strand(path: Path) -> None:
    """GFF3 with CDS on minus strand.

    Gene: chr1 2000–2099, minus strand
    CDS: 2001–2099 (reading right-to-left in genome)
    First codon (5' of transcript) = genome pos 2097-2099 rev-comp
    """
    path.write_text(
        "##gff-version 3\n"
        "chr1\t.\tgene\t2000\t2099\t.\t-\t.\tID=gene-G2;Name=G2\n"
        "chr1\t.\tmRNA\t2000\t2099\t.\t-\t.\t"
        "ID=NM_MINUS1;Parent=gene-G2;transcript_id=NM_MINUS1\n"
        "chr1\t.\tCDS\t2001\t2099\t.\t-\t0\tParent=NM_MINUS1\n"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# GnomadConstraintLookup
# ═══════════════════════════════════════════════════════════════════════════════

class TestGnomadConstraintLookup:

    def test_loads_tsv_and_returns_record(self, tmp_path):
        tsv = tmp_path / "constraint.tsv"
        _write_constraint_tsv(tsv, [
            {"gene": "BRCA1", "pLI": "0.99", "oe_lof_upper": "0.25"},
        ])
        lkp = GnomadConstraintLookup({"gnomad_constraint": {"tsv_path": str(tsv)}})
        rec = lkp.lookup("BRCA1")
        assert rec is not None
        assert rec.pli == pytest.approx(0.99)
        assert rec.loeuf == pytest.approx(0.25)
        assert rec.backend_used == "local"

    def test_case_insensitive_lookup(self, tmp_path):
        tsv = tmp_path / "c.tsv"
        _write_constraint_tsv(tsv, [{"gene": "TP53", "pLI": "0.98", "oe_lof_upper": "0.10"}])
        lkp = GnomadConstraintLookup({"gnomad_constraint": {"tsv_path": str(tsv)}})
        assert lkp.lookup("tp53") is not None
        assert lkp.lookup("Tp53") is not None

    def test_is_lof_intolerant_high_pli(self, tmp_path):
        tsv = tmp_path / "c.tsv"
        _write_constraint_tsv(tsv, [{"gene": "GENE1", "pLI": "0.95", "oe_lof_upper": "0.50"}])
        lkp = GnomadConstraintLookup({"gnomad_constraint": {"tsv_path": str(tsv)}})
        assert lkp.is_lof_intolerant("GENE1") is True

    def test_is_lof_intolerant_low_loeuf(self, tmp_path):
        tsv = tmp_path / "c.tsv"
        _write_constraint_tsv(tsv, [{"gene": "GENE2", "pLI": "0.1", "oe_lof_upper": "0.20"}])
        lkp = GnomadConstraintLookup({"gnomad_constraint": {"tsv_path": str(tsv)}})
        assert lkp.is_lof_intolerant("GENE2") is True

    def test_is_lof_tolerant(self, tmp_path):
        tsv = tmp_path / "c.tsv"
        _write_constraint_tsv(tsv, [{"gene": "CFTR", "pLI": "0.01", "oe_lof_upper": "0.80"}])
        lkp = GnomadConstraintLookup({"gnomad_constraint": {"tsv_path": str(tsv)}})
        assert lkp.is_lof_intolerant("CFTR") is False

    def test_unknown_gene_returns_false(self, tmp_path):
        tsv = tmp_path / "c.tsv"
        _write_constraint_tsv(tsv, [{"gene": "BRCA1", "pLI": "0.99", "oe_lof_upper": "0.25"}])
        lkp = GnomadConstraintLookup({"gnomad_constraint": {"tsv_path": str(tsv)}})
        assert lkp.is_lof_intolerant("FAKEGENE999") is False

    def test_missing_tsv_returns_false(self, tmp_path):
        # No OMIM fallback exists any more (retired) -- a missing/unreadable
        # TSV must fall back to the documented "unavailable" default (False)
        # rather than raising.
        lkp = GnomadConstraintLookup(
            {"gnomad_constraint": {"tsv_path": str(tmp_path / "nonexistent.tsv")}}
        )
        assert lkp.is_lof_intolerant("BRCA1") is False

    def test_gz_tsv_loaded(self, tmp_path):
        tsv_gz = tmp_path / "constraint.tsv.gz"
        header = "gene\ttranscript\tpLI\toe_lof_upper\n"
        row = "BRCA2\tNM_000059\t0.97\t0.19\n"
        with gzip.open(str(tsv_gz), "wt") as fh:
            fh.write(header + row)
        lkp = GnomadConstraintLookup({"gnomad_constraint": {"tsv_path": str(tsv_gz)}})
        rec = lkp.lookup("BRCA2")
        assert rec is not None
        assert rec.pli == pytest.approx(0.97)

    def test_dot_in_loeuf_treated_as_missing(self, tmp_path):
        tsv = tmp_path / "c.tsv"
        tsv.write_text("gene\ttranscript\tpLI\toe_lof_upper\nGENE3\tNM_X\t0.95\t.\n")
        lkp = GnomadConstraintLookup({"gnomad_constraint": {"tsv_path": str(tsv)}})
        rec = lkp.lookup("GENE3")
        assert rec is not None
        assert rec.loeuf is None
        # pLI=0.95 still passes threshold
        assert lkp.is_lof_intolerant("GENE3") is True

    def test_api_fallback_called_when_no_tsv(self):
        api_rec = ConstraintRecord(gene="BRCA1", pli=0.99, loeuf=0.22, backend_used="api")
        lkp = GnomadConstraintLookup(cfg={})
        with patch.object(lkp, "_api_lookup", return_value=api_rec):
            result = lkp.is_lof_intolerant("BRCA1")
        assert result is True

    def test_custom_thresholds_respected(self, tmp_path):
        tsv = tmp_path / "c.tsv"
        _write_constraint_tsv(tsv, [{"gene": "GENE4", "pLI": "0.85", "oe_lof_upper": "0.40"}])
        # With default thresholds (pLI≥0.9, LOEUF≤0.35): not intolerant
        lkp = GnomadConstraintLookup({"gnomad_constraint": {"tsv_path": str(tsv)}})
        assert lkp.is_lof_intolerant("GENE4") is False
        # With relaxed thresholds: intolerant
        lkp2 = GnomadConstraintLookup({
            "gnomad_constraint": {
                "tsv_path": str(tsv),
                "pli_threshold": 0.8,
                "loeuf_threshold": 0.45,
            }
        })
        assert lkp2.is_lof_intolerant("GENE4") is True

    def test_constraint_record_is_lof_intolerant_method(self):
        rec = ConstraintRecord(gene="G", pli=0.95, loeuf=0.50)
        assert rec.is_lof_intolerant() is True  # pLI passes
        rec2 = ConstraintRecord(gene="G", pli=0.05, loeuf=0.30)
        assert rec2.is_lof_intolerant() is True  # LOEUF passes
        rec3 = ConstraintRecord(gene="G", pli=0.05, loeuf=0.80)
        assert rec3.is_lof_intolerant() is False


# ═══════════════════════════════════════════════════════════════════════════════
# HotspotLookup
# ═══════════════════════════════════════════════════════════════════════════════

class TestHotspotLookup:

    def test_position_with_3_plp_is_hotspot(self, tmp_path):
        tsv_gz = tmp_path / "clinvar.tsv.gz"
        _write_clinvar_tsv(tsv_gz, [
            {"chrom": "17", "pos": "43057051", "sig": "Pathogenic"},
            {"chrom": "17", "pos": "43057051", "sig": "Pathogenic"},
            {"chrom": "17", "pos": "43057051", "sig": "Likely pathogenic"},
        ])
        lkp = HotspotLookup({"hotspot": {"clinvar_tsv_gz_path": str(tsv_gz)}})
        rec = lkp.lookup("chr17", 43057051)
        assert rec.plp_count == 3
        assert rec.is_hotspot is True

    def test_position_with_2_plp_not_hotspot(self, tmp_path):
        tsv_gz = tmp_path / "clinvar.tsv.gz"
        _write_clinvar_tsv(tsv_gz, [
            {"chrom": "17", "pos": "43057051", "sig": "Pathogenic"},
            {"chrom": "17", "pos": "43057051", "sig": "Pathogenic"},
        ])
        lkp = HotspotLookup({"hotspot": {"clinvar_tsv_gz_path": str(tsv_gz)}})
        rec = lkp.lookup("chr17", 43057051)
        assert rec.plp_count == 2
        assert rec.is_hotspot is False

    def test_benign_submissions_not_counted(self, tmp_path):
        tsv_gz = tmp_path / "clinvar.tsv.gz"
        _write_clinvar_tsv(tsv_gz, [
            {"chrom": "17", "pos": "100", "sig": "Benign"},
            {"chrom": "17", "pos": "100", "sig": "Likely benign"},
            {"chrom": "17", "pos": "100", "sig": "Pathogenic"},
        ])
        lkp = HotspotLookup({"hotspot": {"clinvar_tsv_gz_path": str(tsv_gz)}})
        rec = lkp.lookup("17", 100)
        assert rec.plp_count == 1
        assert rec.is_hotspot is False

    def test_chrom_normalisation_chr_prefix(self, tmp_path):
        tsv_gz = tmp_path / "clinvar.tsv.gz"
        _write_clinvar_tsv(tsv_gz, [
            {"chrom": "1", "pos": "200", "sig": "Pathogenic"},
            {"chrom": "1", "pos": "200", "sig": "Pathogenic"},
            {"chrom": "1", "pos": "200", "sig": "Pathogenic"},
        ])
        lkp = HotspotLookup({"hotspot": {"clinvar_tsv_gz_path": str(tsv_gz)}})
        # Query with chr prefix
        assert lkp.lookup("chr1", 200).is_hotspot is True
        # Query without chr prefix
        assert lkp.lookup("1", 200).is_hotspot is True

    def test_custom_min_plp_count(self, tmp_path):
        tsv_gz = tmp_path / "clinvar.tsv.gz"
        _write_clinvar_tsv(tsv_gz, [
            {"chrom": "1", "pos": "500", "sig": "Pathogenic"},
            {"chrom": "1", "pos": "500", "sig": "Pathogenic"},
        ])
        lkp = HotspotLookup({"hotspot": {
            "clinvar_tsv_gz_path": str(tsv_gz),
            "min_plp_count": 2,
        }})
        assert lkp.lookup("1", 500).is_hotspot is True

    def test_uniprot_domain_overlap(self, tmp_path):
        bed = tmp_path / "domains.bed"
        # chr1: 1000-2000 (0-based)
        _write_domains_bed(bed, [("chr1", 1000, 2000, "BRCA1:BRCT")])
        lkp = HotspotLookup({"hotspot": {"uniprot_domains_bed": str(bed)}})
        # VCF pos 1500 (1-based) → 0-based 1499 → inside [1000,2000)
        rec = lkp.lookup("chr1", 1500)
        assert rec.in_uniprot_domain is True
        assert rec.domain_name == "BRCA1:BRCT"
        assert rec.is_hotspot is True

    def test_position_outside_domain_not_hotspot(self, tmp_path):
        bed = tmp_path / "domains.bed"
        _write_domains_bed(bed, [("chr1", 1000, 2000, "BRCA1:BRCT")])
        lkp = HotspotLookup({"hotspot": {"uniprot_domains_bed": str(bed)}})
        rec = lkp.lookup("chr1", 5000)
        assert rec.in_uniprot_domain is False
        assert rec.is_hotspot is False

    def test_is_in_hotspot_none_when_no_data(self):
        # No OMIM fallback exists any more (retired), and no local data
        # source is loaded -- correct result is None (not-evaluated, so
        # PM1 shows not_evaluated), not a False that would read as a
        # confirmed negative.
        lkp = HotspotLookup(cfg={})
        assert lkp.is_in_hotspot("chr1", 100, gene="FAKEGENE") is None

    def test_reuses_clinvar_cfg_key(self, tmp_path):
        """hotspot.clinvar_tsv_gz_path should also accept clinvar.tsv_gz_path."""
        tsv_gz = tmp_path / "clinvar.tsv.gz"
        _write_clinvar_tsv(tsv_gz, [
            {"chrom": "2", "pos": "9999", "sig": "Pathogenic"},
            {"chrom": "2", "pos": "9999", "sig": "Pathogenic"},
            {"chrom": "2", "pos": "9999", "sig": "Pathogenic"},
        ])
        # Pass via clinvar config key
        lkp = HotspotLookup({"clinvar": {"tsv_gz_path": str(tsv_gz)}})
        assert lkp.lookup("2", 9999).is_hotspot is True


# ═══════════════════════════════════════════════════════════════════════════════
# FastaCodonContextProvider
# ═══════════════════════════════════════════════════════════════════════════════

class TestCodonHelpers:
    def test_translate_met(self):
        assert _translate("ATG") == "M"

    def test_translate_stop(self):
        assert _translate("TAA") == "*"
        assert _translate("TAG") == "*"
        assert _translate("TGA") == "*"

    def test_translate_phe(self):
        assert _translate("TTT") == "F"

    def test_reverse_complement(self):
        assert _reverse_complement("ATGC") == "GCAT"
        assert _reverse_complement("ATG") == "CAT"


class TestFastaCodonContextProvider:
    """Tests using a tiny in-memory FASTA + GFF3."""

    # Reference sequence for chr1 positions 1–200 (1-based).
    # We construct it so positions 1001–1099 contain known codons.
    # The CDS starts at 1001 (ATG = Met), next codon 1004-1006 = TTT (Phe), etc.
    # We build a 1200-base sequence: first 1000 bases = A, then codons.

    @staticmethod
    def _build_sequence() -> str:
        """Build 1200-char reference: 1000×A, then ATG TTT TAA + filler."""
        # pos 1001-1003: ATG (Met, start codon)
        # pos 1004-1006: TTT (Phe)
        # pos 1007-1009: GAG (Glu)
        # pos 1010-1012: TAA (Stop)
        # rest: AAAAAA...
        prefix = "A" * 1000
        coding = "ATG" + "TTT" + "GAG" + "TAA" + "A" * 87  # 99 bases total = CDS
        suffix = "A" * 101
        return prefix + coding + suffix  # 1200 bases total

    def _setup(self, tmp_path: Path):
        """Write FASTA and GFF3, return a ready provider."""
        fasta = tmp_path / "ref.fa"
        seq = self._build_sequence()
        _write_fasta(fasta, {"chr1": seq})

        gff = tmp_path / "test.gff3"
        _write_gff3_with_cds(gff)

        return FastaCodonContextProvider(str(gff), str(fasta))

    def test_provider_available(self, tmp_path):
        p = self._setup(tmp_path)
        assert p._available is True

    def test_missense_at_second_codon(self, tmp_path):
        p = self._setup(tmp_path)
        # pos 1004 = first base of TTT (Phe)
        # TTT → ATT = Ile → missense
        result = p.get_codon_change("chr1", 1004, "T", "A", "NM_CODON1")
        assert result == "missense"

    def test_synonymous_at_third_codon_position(self, tmp_path):
        p = self._setup(tmp_path)
        # pos 1006 = third base of TTT (Phe)
        # TTT → TTC = Phe → synonymous
        result = p.get_codon_change("chr1", 1006, "T", "C", "NM_CODON1")
        assert result == "synonymous"

    def test_stop_gained(self, tmp_path):
        p = self._setup(tmp_path)
        # pos 1004-1006 = TTT (Phe); change pos 1004 T→A, 1005 T→A, 1006 T→A = TAA = stop
        # Single base: TTT → TAT = Tyr (not stop). Use GAG → TAG at pos 1007.
        # pos 1007 = G (first base of GAG = Glu); G→T → TAG = stop_gained
        result = p.get_codon_change("chr1", 1007, "G", "T", "NM_CODON1")
        assert result == "stop_gained"

    def test_start_lost(self, tmp_path):
        p = self._setup(tmp_path)
        # pos 1001 = A of ATG (Met, codon 0 = start codon)
        # A→T → TTG = Leu → start_lost
        result = p.get_codon_change("chr1", 1001, "A", "T", "NM_CODON1")
        assert result == "start_lost"

    def test_stop_lost(self, tmp_path):
        p = self._setup(tmp_path)
        # pos 1010-1012 = TAA (stop codon)
        # pos 1010 = T; T→C → CAA = Gln → stop_lost
        result = p.get_codon_change("chr1", 1010, "T", "C", "NM_CODON1")
        assert result == "stop_lost"

    def test_mnv_raises_not_implemented(self, tmp_path):
        p = self._setup(tmp_path)
        with pytest.raises(NotImplementedError):
            p.get_codon_change("chr1", 1004, "TT", "AA", "NM_CODON1")

    def test_unknown_transcript_returns_none(self, tmp_path):
        p = self._setup(tmp_path)
        result = p.get_codon_change("chr1", 1004, "T", "A", "NM_DOESNOTEXIST")
        assert result is None

    def test_missing_fasta_provider_not_available(self, tmp_path):
        gff = tmp_path / "test.gff3"
        _write_gff3_with_cds(gff)
        p = FastaCodonContextProvider(str(gff), str(tmp_path / "nonexistent.fa"))
        assert p._available is False

    def test_missing_gff_provider_not_available(self, tmp_path):
        fasta = tmp_path / "ref.fa"
        _write_fasta(fasta, {"chr1": "A" * 1200})
        p = FastaCodonContextProvider(str(tmp_path / "nonexistent.gff"), str(fasta))
        assert p._available is False

    def test_unavailable_provider_raises(self, tmp_path):
        p = FastaCodonContextProvider(
            str(tmp_path / "no.gff"), str(tmp_path / "no.fa")
        )
        with pytest.raises(NotImplementedError):
            p.get_codon_change("chr1", 1, "A", "T", "NM_X")


class TestMakeCodonProviderFromCfg:
    """make_codon_provider_from_cfg returns the right provider based on config."""

    def test_returns_no_cds_when_no_fasta(self, tmp_path):
        from pipeline.annotation.codon_provider import make_codon_provider_from_cfg
        gff = tmp_path / "test.gff3"
        _write_gff3_with_cds(gff)
        cfg = {"rna_analysis": {"refseq_gff": str(gff)}}
        # No reference_fasta configured → NoCdsCodonContextProvider
        provider = make_codon_provider_from_cfg(cfg)
        assert isinstance(provider, NoCdsCodonContextProvider)

    def test_returns_fasta_provider_when_both_configured(self, tmp_path):
        from pipeline.annotation.codon_provider import make_codon_provider_from_cfg
        gff = tmp_path / "test.gff3"
        _write_gff3_with_cds(gff)

        fasta = tmp_path / "ref.fa"
        seq = "A" * 1000 + "ATG" + "TTT" + "GAG" + "TAA" + "A" * 87 + "A" * 101
        _write_fasta(fasta, {"chr1": seq})

        cfg = {
            "rna_analysis": {"refseq_gff": str(gff)},
            "alignment": {"reference_fasta": str(fasta)},
        }
        provider = make_codon_provider_from_cfg(cfg)
        assert isinstance(provider, FastaCodonContextProvider)
        assert provider._available is True


# ═══════════════════════════════════════════════════════════════════════════════
# Integration: AnnotationStage consequence with real codon provider
# ═══════════════════════════════════════════════════════════════════════════════

class TestAnnotationStageWithCodonProvider:
    """End-to-end: AnnotationStage resolves missense/synonymous when FASTA is present."""

    def _write_vcf(self, path: Path, records: list) -> None:
        path.write_text(
            "##fileformat=VCFv4.2\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
            + "".join(records)
        )

    @staticmethod
    def _build_sequence() -> str:
        return "A" * 1000 + "ATG" + "TTT" + "GAG" + "TAA" + "A" * 87 + "A" * 101

    def test_missense_resolved_end_to_end(self, tmp_path):
        from pipeline.annotation.stage import AnnotationStage

        gff = tmp_path / "test.gff3"
        _write_gff3_with_cds(gff)
        fasta = tmp_path / "ref.fa"
        _write_fasta(fasta, {"chr1": self._build_sequence()})
        vcf = tmp_path / "test.vcf"
        # pos 1004 = first T of TTT(Phe); T→A = Ile → missense
        self._write_vcf(vcf, ["chr1\t1004\t.\tT\tA\t100\tPASS\t.\n"])

        cfg = {
            "rna_analysis": {"refseq_gff": str(gff)},
            "alignment": {"reference_fasta": str(fasta)},
        }
        stage = AnnotationStage(cfg)
        result = stage.run(str(vcf), str(tmp_path / "out"))
        v = result.variants[0]
        assert v.consequence == "missense_variant"
        assert v.is_inframe_indel is False

    def test_synonymous_resolved_end_to_end(self, tmp_path):
        from pipeline.annotation.stage import AnnotationStage

        gff = tmp_path / "test.gff3"
        _write_gff3_with_cds(gff)
        fasta = tmp_path / "ref.fa"
        _write_fasta(fasta, {"chr1": self._build_sequence()})
        vcf = tmp_path / "test.vcf"
        # pos 1006 = third T of TTT (Phe); T→C = TTC = Phe → synonymous
        self._write_vcf(vcf, ["chr1\t1006\t.\tT\tC\t100\tPASS\t.\n"])

        cfg = {
            "rna_analysis": {"refseq_gff": str(gff)},
            "alignment": {"reference_fasta": str(fasta)},
        }
        stage = AnnotationStage(cfg)
        result = stage.run(str(vcf), str(tmp_path / "out"))
        v = result.variants[0]
        assert v.consequence == "synonymous_variant"
