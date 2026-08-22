"""
tests/test_all_fixes_regression.py
────────────────────────────────────
Regression tests for FIX 1 – FIX 15.

Every test is self-contained and does NOT require external databases,
network access, a FASTA file, or running binary tools.
"""

from __future__ import annotations

import sys
import os
import textwrap
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─── FIX 1: VEP → ACMG evidence propagation via CSQ parsing ─────────────────


class TestFix1VepCsqParsing:
    """_parse_csq_header + _extract_csq_scores correctly pull scores from VEP CSQ."""

    def _import(self):
        from pipeline.annotation.stage import _parse_csq_header, _extract_csq_scores

        return _parse_csq_header, _extract_csq_scores

    def test_parse_csq_header_extracts_fields(self):
        _parse_csq_header, _ = self._import()
        line = (
            '##INFO=<ID=CSQ,Number=.,Type=String,Description="Consequence annotations from '
            "Ensembl VEP. Format: Allele|Consequence|IMPACT|SYMBOL|Gene|Feature_type|Feature|"
            "BIOTYPE|EXON|INTRON|HGVSc|HGVSp|cDNA_position|CDS_position|Protein_position|"
            "Amino_acids|Codons|Existing_variation|DISTANCE|STRAND|FLAGS|VARIANT_CLASS|"
            'SYMBOL_SOURCE|HGNC_ID|CADD_PHRED|REVEL|SpliceAI_pred|AM_PATHOGENICITY">'
        )
        fields = _parse_csq_header(line)
        assert "CADD_PHRED" in fields
        assert "REVEL" in fields
        assert "SpliceAI_pred" in fields
        assert "AM_PATHOGENICITY" in fields
        assert "Consequence" in fields

    def test_extract_csq_scores_missense(self):
        """
        UPDATED 2026-08-22: this test originally pinned SpliceAI extraction
        (`spliceai == 0.85`) as correct behaviour. SpliceAI parsing has
        since been deliberately removed from `_extract_csq_scores`
        (licence -- Illumina's plugin is CC BY-NC 4.0 pretrained models /
        GPL-3.0 code, the same class of blocker as OMIM; see
        `pipeline/vep/stage.py`'s docstring and `LICENSE_AUDIT.md`'s
        "SpliceAI (Illumina)" row). The CSQ fixture still includes a
        SpliceAI_pred column (matching a real VEP CSQ header, in case the
        plugin is ever requested by some other caller) so this test keeps
        exercising the same realistic input shape -- only the expected
        `spliceai` value changed, from the old pinned score to `None`,
        because the code under test no longer reads that column at all.
        """
        _, _extract_csq_scores = self._import()
        # Build a minimal CSQ INFO string with known scores
        csq_fields = [
            "Allele",
            "Consequence",
            "IMPACT",
            "SYMBOL",
            "Gene",
            "Feature_type",
            "Feature",
            "BIOTYPE",
            "EXON",
            "INTRON",
            "HGVSc",
            "HGVSp",
            "cDNA_position",
            "CDS_position",
            "Protein_position",
            "Amino_acids",
            "Codons",
            "Existing_variation",
            "DISTANCE",
            "STRAND",
            "FLAGS",
            "VARIANT_CLASS",
            "SYMBOL_SOURCE",
            "HGNC_ID",
            "CADD_PHRED",
            "REVEL",
            "SpliceAI_pred",
            "AM_PATHOGENICITY",
        ]
        # values for each field
        vals = [
            "G",
            "missense_variant",
            "HIGH",
            "BRCA1",
            "1234",
            "Transcript",
            "NM_007294.4",
            "protein_coding",
            "5",
            ".",
            "c.5266dupC",
            "p.Gln1756ProfsTer74",
            "5444",
            "5266",
            "1756",
            "Q/X",
            "Caa/Xaa",
            "rs80357906",
            ".",
            "-1",
            ".",
            "SNV",
            "HGNC",
            "1100",
            "28.4",
            "0.92",
            "0.85",
            "0.98",
        ]
        csq_value = "|".join(vals)
        info = f"CSQ={csq_value}"
        cadd, revel, spliceai, am = _extract_csq_scores(info, csq_fields)
        assert cadd == pytest.approx(28.4)
        assert revel == pytest.approx(0.92)
        assert spliceai is None  # SpliceAI parsing removed (licence) -- see docstring above
        assert am == pytest.approx(0.98)

    def test_extract_csq_scores_no_csq_returns_nones(self):
        _, _extract_csq_scores = self._import()
        cadd, revel, spliceai, am = _extract_csq_scores("DP=100;MQ=60", ["Consequence"])
        assert cadd is None and revel is None and spliceai is None and am is None

    def test_parse_vcf_reads_csq_scores(self):
        """_parse_vcf sets cadd_phred from CSQ when standalone INFO key absent."""
        from pipeline.annotation.stage import _parse_vcf

        csq_fields_str = "Allele|Consequence|CADD_PHRED|REVEL|SpliceAI_pred|AM_PATHOGENICITY"
        csq_val = "T|missense_variant|32.1|0.88|0.7|0|0|0|0.95"
        # Build a minimal VCF in a temp file
        vcf_content = textwrap.dedent(f"""\
            ##fileformat=VCFv4.2
            ##INFO=<ID=CSQ,Number=.,Type=String,Description="Format: {csq_fields_str}">
            #CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO
            chr7\t140453136\t.\tA\tT\t100\tPASS\tCSQ={csq_val}
        """)
        with tempfile.NamedTemporaryFile(suffix=".vcf", mode="w", delete=False) as tf:
            tf.write(vcf_content)
            vcf_path = tf.name
        try:
            variants = _parse_vcf(vcf_path)
            assert len(variants) == 1
            v = variants[0]
            assert v.cadd_phred == pytest.approx(32.1)
            assert v.revel_score == pytest.approx(0.88)
        finally:
            os.unlink(vcf_path)

    def test_parse_vcf_multi_allelic_expansion(self):
        """FIX 12: multi-allelic records expand into separate AnnotatedVariant objects."""
        from pipeline.annotation.stage import _parse_vcf

        vcf_content = textwrap.dedent("""\
            ##fileformat=VCFv4.2
            #CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO
            chr1\t12345\t.\tA\tG,T\t100\tPASS\t.
        """)
        with tempfile.NamedTemporaryFile(suffix=".vcf", mode="w", delete=False) as tf:
            tf.write(vcf_content)
            vcf_path = tf.name
        try:
            variants = _parse_vcf(vcf_path)
            assert len(variants) == 2, "Multi-allelic must expand to 2 AnnotatedVariants"
            alts = {v.alt for v in variants}
            assert "G" in alts
            assert "T" in alts
        finally:
            os.unlink(vcf_path)


# ─── FIX 2: PS1 implementation ───────────────────────────────────────────────


class TestFix2PS1:
    """check_same_codon_pathogenic returns same_aa_pathogenic=True for identical AA change."""

    def test_aa3to1_three_letter(self):
        from pipeline.clinvar.lookup import _aa3to1

        assert _aa3to1("Val") == "V"
        assert _aa3to1("Glu") == "E"
        assert _aa3to1("Ter") == "*"

    def test_aa3to1_one_letter_passthrough(self):
        from pipeline.clinvar.lookup import _aa3to1

        assert _aa3to1("V") == "V"
        assert _aa3to1("E") == "E"

    def test_aa3to1_unknown(self):
        from pipeline.clinvar.lookup import _aa3to1

        assert _aa3to1("Xyz") == ""

    def test_ps1_fires_on_same_aa_change(self):
        """PS1: same_aa_pathogenic=True when ClinVar has matching p. notation."""
        from pipeline.clinvar.lookup import ClinVarLookup, ClinVarHit

        lkp = ClinVarLookup.__new__(ClinVarLookup)
        lkp._backend = "local"
        lkp._cache = {}
        lkp._cache_lock = __import__("threading").Lock()
        lkp._cache_hits = 0
        lkp._cache_misses = 0
        # Simulate a ClinVar pathogenic entry at position 140453136 with different allele
        # that produces Val600Glu (same as what we're querying)
        hit = ClinVarHit(
            significance="Pathogenic",
            review_stars=4,
            submitter_count=10,
            allele_id="12345",
            conflicting=False,
            gene="BRAF",
            hgvs_p="p.Val600Glu",
        )
        lkp._by_coord = {
            # Different allele at same pos (e.g. different nucleotide making same AA)
            ("7", 140453136, "A", "C"): hit,
        }

        same_aa, novel_aa = lkp.check_same_codon_pathogenic(
            chrom="7",
            pos=140453136,
            ref="A",
            alt="T",  # our variant: different ALT
            wildtype_aa="Val",  # V600
            mutant_aa="Glu",  # E → p.Val600Glu
        )
        assert same_aa is True, "PS1 must fire when ClinVar p. matches our AA change"

    def test_ps1_does_not_fire_different_aa(self):
        """PS1 must NOT fire when ClinVar AA change differs from ours."""
        from pipeline.clinvar.lookup import ClinVarLookup, ClinVarHit

        lkp = ClinVarLookup.__new__(ClinVarLookup)
        lkp._backend = "local"
        lkp._cache = {}
        lkp._cache_lock = __import__("threading").Lock()
        lkp._cache_hits = 0
        lkp._cache_misses = 0
        hit = ClinVarHit(
            significance="Pathogenic",
            review_stars=3,
            submitter_count=5,
            allele_id="99999",
            conflicting=False,
            gene="BRAF",
            hgvs_p="p.Val600Lys",  # different AA substitution
        )
        lkp._by_coord = {("7", 140453136, "A", "C"): hit}

        same_aa, novel_aa = lkp.check_same_codon_pathogenic(
            chrom="7",
            pos=140453136,
            ref="A",
            alt="T",
            wildtype_aa="Val",
            mutant_aa="Glu",
        )
        # CORRECTED (test-integrity dispatch, 2026-08-21): this assertion
        # used to require `same_aa is None` here -- but this is a case
        # where the codon window WAS scanned, wildtype/mutant AA WERE
        # supplied, and a P/LP record WAS found at the codon with a
        # confirmed *different* amino-acid change. That is a genuine,
        # checked negative for PS1 ("searched, and the answer is no"),
        # not "insufficient data" -- and it was the test itself pinning
        # the bug as a requirement: `check_same_codon_pathogenic` could
        # never return `False`, only `True` or `None`, so a real checked
        # negative and "never checked at all" were indistinguishable to
        # every caller (see `pipeline/acmg/classifier.py::_ps1`'s own
        # comment, which already anticipated a real `False` distinct
        # from `None` -- the producer just never delivered one).
        assert same_aa is False, (
            "PS1 must report a checked negative (False), not 'unknown', when AA changes differ"
        )
        assert novel_aa is True, "PM5 SHOULD fire when different P/LP variant at same codon"

    def test_ps1_pm5_report_checked_negative_not_unknown_when_scan_finds_no_plp_at_all(self):
        """
        NEW (test-integrity dispatch: check-same-codon-pathogenic-cannot
        -return-a-checked-negative). Before this fix, a codon window that
        was genuinely scanned (local backend available, wildtype/mutant AA
        supplied) but contained NO Pathogenic/Likely-Pathogenic record at
        all -- the strongest possible negative result -- was reported
        identically to "never scanned" (both `None`). That means
        `_ps1`/`_pm5` in `pipeline/acmg/classifier.py` could never
        distinguish "we looked and there is nothing" from "we never
        looked", even though both criteria's own status logic was already
        written to handle a real `False` (STATUS_NOT_MET) as distinct from
        `None` (STATUS_NOT_EVALUATED) -- the bug was entirely in the
        producer, not the consumer.
        """
        from pipeline.clinvar.lookup import ClinVarLookup, ClinVarHit

        lkp = ClinVarLookup.__new__(ClinVarLookup)
        lkp._backend = "local"
        lkp._cache = {}
        lkp._cache_lock = __import__("threading").Lock()
        lkp._cache_hits = 0
        lkp._cache_misses = 0
        # A local backend that IS populated (so the "could not search at
        # all" early-return does not apply) but has no record whatsoever
        # near this codon -- the clean "searched, found nothing" case.
        lkp._by_coord = {
            ("7", 999999999, "A", "C"): ClinVarHit(
                significance="Benign",
                review_stars=2,
                submitter_count=3,
                allele_id="1",
                conflicting=False,
                gene="OTHERGENE",
                hgvs_p="p.Ala1Gly",
            ),
        }

        same_aa, novel_aa = lkp.check_same_codon_pathogenic(
            chrom="7",
            pos=140453136,
            ref="A",
            alt="T",
            wildtype_aa="Val",
            mutant_aa="Glu",
        )
        assert same_aa is False, (
            "PS1 was genuinely scanned (local backend populated, AA supplied) and found no matching "
            "P/LP record at all -- this is a checked negative and must be False, not None ('unknown')."
        )
        assert novel_aa is False, (
            "PM5 was genuinely scanned and found no P/LP record at this codon at all -- checked "
            "negative, must be False, not None."
        )

    def test_ps1_pm5_still_unknown_when_query_amino_acids_not_supplied(self):
        """
        Companion to the above: when the CALLER never supplied
        `wildtype_aa`/`mutant_aa` (e.g. amino-acid translation itself
        failed upstream), PS1/PM5 genuinely cannot be evaluated even
        though the codon window scan runs -- this must stay `None`
        ('not evaluated'), not become a fabricated `False`. Guards
        against an overcorrection of the bug above: "always return a
        real bool" would be just as wrong as "never return one".
        """
        from pipeline.clinvar.lookup import ClinVarLookup, ClinVarHit

        lkp = ClinVarLookup.__new__(ClinVarLookup)
        lkp._backend = "local"
        lkp._cache = {}
        lkp._cache_lock = __import__("threading").Lock()
        lkp._cache_hits = 0
        lkp._cache_misses = 0
        lkp._by_coord = {
            ("7", 140453136, "A", "C"): ClinVarHit(
                significance="Pathogenic",
                review_stars=4,
                submitter_count=10,
                allele_id="12345",
                conflicting=False,
                gene="BRAF",
                hgvs_p="p.Val600Glu",
            ),
        }

        same_aa, novel_aa = lkp.check_same_codon_pathogenic(
            chrom="7",
            pos=140453136,
            ref="A",
            alt="T",
            wildtype_aa=None,
            mutant_aa=None,
        )
        assert same_aa is None, (
            "Without query amino acids, PS1 genuinely cannot be evaluated -- must stay None"
        )
        assert novel_aa is None, (
            "Without query amino acids, PM5 genuinely cannot be evaluated -- must stay None"
        )

    def test_ps1_no_dead_code_after_return(self):
        """FIX 10: No dead code block exists after the return in check_same_codon_pathogenic."""
        import inspect
        from pipeline.clinvar.lookup import ClinVarLookup

        src = inspect.getsource(ClinVarLookup.check_same_codon_pathogenic)
        # Dead code was a docstring appearing after the return statement
        assert '"""Look up a variant by genomic coordinates' not in src, (
            "Dead code block still present after return in check_same_codon_pathogenic"
        )


# ─── FIX 3: Strand-aware splice annotation ───────────────────────────────────


class TestFix3StrandAwareSplice:
    """classify_region returns correct donor/acceptor for ± strand genes."""

    def _make_analyser(self) -> "RnaTranscriptAnalyser":
        from pipeline.rna.transcript import RnaTranscriptAnalyser

        ra = RnaTranscriptAnalyser.__new__(RnaTranscriptAnalyser)
        ra._available = True
        ra._transcripts = {}
        ra._gene_index = {}
        return ra

    def _add_transcript(self, ra, tid, strand, exons, cds_start=None, cds_end=None):
        from pipeline.rna.transcript import TranscriptRecord

        all_starts = [s for s, e in exons]
        all_ends = [e for s, e in exons]
        rec = TranscriptRecord(
            transcript_id=tid,
            gene_id="GENE1",
            chrom="chr1",
            start=min(all_starts),
            end=max(all_ends),
            strand=strand,
            exons=sorted(exons),
            cds_start=cds_start,
            cds_end=cds_end,
        )
        ra._transcripts[tid] = rec

    # ── Positive strand ───────────────────────────────────────────────────────

    def test_plus_strand_donor_at_exon_right_edge(self):
        """+ strand: position at right edge of non-terminal exon → splice_donor."""
        ra = self._make_analyser()
        self._add_transcript(ra, "T1", "+", [(100, 200), (300, 400)])
        # pos 200 is the right edge of exon 1 → donor on + strand
        assert ra.classify_region("chr1", 200, "T1") == "splice_donor"

    def test_plus_strand_acceptor_at_exon_left_edge(self):
        """+ strand: position at left edge of non-first exon → splice_acceptor."""
        ra = self._make_analyser()
        self._add_transcript(ra, "T1", "+", [(100, 200), (300, 400)])
        # pos 300 is the left edge of exon 2 → acceptor on + strand
        assert ra.classify_region("chr1", 300, "T1") == "splice_acceptor"

    def test_minus_strand_donor_at_exon_left_edge(self):
        """− strand: genomic left edge of non-first exon → splice_donor."""
        ra = self._make_analyser()
        self._add_transcript(ra, "T2", "-", [(100, 200), (300, 400)])
        # On − strand, exon 2 (300-400) is the FIRST exon in transcript order.
        # Its LEFT genomic edge (300) is the 3' end of the exon → donor.
        assert ra.classify_region("chr1", 300, "T2") == "splice_donor"

    def test_minus_strand_acceptor_at_exon_right_edge(self):
        """− strand: genomic right edge of non-terminal exon → splice_acceptor."""
        ra = self._make_analyser()
        self._add_transcript(ra, "T2", "-", [(100, 200), (300, 400)])
        # exon 1 (100-200) is the second exon in transcript order.
        # Its RIGHT genomic edge (200) → acceptor on − strand.
        assert ra.classify_region("chr1", 200, "T2") == "splice_acceptor"

    def test_single_exon_gene_no_splice_sites(self):
        """Single-exon genes have no splice sites — position returns exonic."""
        ra = self._make_analyser()
        self._add_transcript(ra, "T3", "+", [(100, 500)], cds_start=150, cds_end=450)
        result = ra.classify_region("chr1", 200, "T3")
        assert result == "exonic"

    def test_intronic_position(self):
        """Position between exons is intronic."""
        ra = self._make_analyser()
        self._add_transcript(ra, "T4", "+", [(100, 200), (300, 400)])
        assert ra.classify_region("chr1", 250, "T4") == "intronic"

    def test_out_of_range_is_intergenic(self):
        """Position outside transcript bounds is intergenic."""
        ra = self._make_analyser()
        self._add_transcript(ra, "T5", "+", [(100, 200)])
        assert ra.classify_region("chr1", 500, "T5") == "intergenic"


# ─── FIX 4: Proper UTR annotation ────────────────────────────────────────────


class TestFix4UTRAnnotation:
    """classify_region returns utr5/utr3; _map_consequence maps them correctly."""

    def _make_analyser(self):
        from pipeline.rna.transcript import RnaTranscriptAnalyser

        ra = RnaTranscriptAnalyser.__new__(RnaTranscriptAnalyser)
        ra._available = True
        ra._transcripts = {}
        ra._gene_index = {}
        return ra

    def _add_transcript(self, ra, tid, strand, exons, cds_start, cds_end):
        from pipeline.rna.transcript import TranscriptRecord

        all_starts = [s for s, e in exons]
        all_ends = [e for s, e in exons]
        rec = TranscriptRecord(
            transcript_id=tid,
            gene_id="G1",
            chrom="chr2",
            start=min(all_starts),
            end=max(all_ends),
            strand=strand,
            exons=sorted(exons),
            cds_start=cds_start,
            cds_end=cds_end,
        )
        ra._transcripts[tid] = rec

    def test_plus_strand_5utr(self):
        """+ strand: exonic position before CDS start → utr5."""
        ra = self._make_analyser()
        self._add_transcript(ra, "T1", "+", [(100, 500)], cds_start=200, cds_end=450)
        assert ra.classify_region("chr2", 150, "T1") == "utr5"

    def test_plus_strand_3utr(self):
        """+ strand: exonic position after CDS end → utr3."""
        ra = self._make_analyser()
        self._add_transcript(ra, "T1", "+", [(100, 500)], cds_start=200, cds_end=450)
        assert ra.classify_region("chr2", 480, "T1") == "utr3"

    def test_minus_strand_5utr(self):
        """− strand: exonic position with genomic coord > cds_end → utr5."""
        ra = self._make_analyser()
        # On − strand CDS is at 200–450 (lower numbers are 3'UTR in genomic coords)
        self._add_transcript(ra, "T2", "-", [(100, 500)], cds_start=200, cds_end=450)
        # pos 480 > cds_end=450 → 5'UTR on minus strand
        assert ra.classify_region("chr2", 480, "T2") == "utr5"

    def test_minus_strand_3utr(self):
        """− strand: exonic position with genomic coord < cds_start → utr3."""
        ra = self._make_analyser()
        self._add_transcript(ra, "T2", "-", [(100, 500)], cds_start=200, cds_end=450)
        # pos 150 < cds_start=200 → 3'UTR on minus strand
        assert ra.classify_region("chr2", 150, "T2") == "utr3"

    def test_map_consequence_utr5(self):
        """_map_consequence maps 'utr5' → '5_prime_UTR_variant' (not intergenic)."""
        from pipeline.annotation.stage import _map_consequence
        from pipeline.annotation.stage import NoCdsCodonContextProvider

        result = _map_consequence(
            region="utr5",
            ref="A",
            alt="G",
            codon_provider=NoCdsCodonContextProvider(),
            chrom="chr2",
            pos=150,
            transcript_id="T1",
        )
        assert result == "5_prime_UTR_variant", f"Expected 5_prime_UTR_variant, got {result!r}"

    def test_map_consequence_utr3(self):
        """_map_consequence maps 'utr3' → '3_prime_UTR_variant' (not intergenic)."""
        from pipeline.annotation.stage import _map_consequence
        from pipeline.annotation.stage import NoCdsCodonContextProvider

        result = _map_consequence(
            region="utr3",
            ref="A",
            alt="G",
            codon_provider=NoCdsCodonContextProvider(),
            chrom="chr2",
            pos=480,
            transcript_id="T1",
        )
        assert result == "3_prime_UTR_variant", f"Expected 3_prime_UTR_variant, got {result!r}"


# ─── FIX 5: GFF3 phase ───────────────────────────────────────────────────────


class TestFix5GFF3Phase:
    """get_cds_frame_at_position returns correct frame using CDS phase."""

    def _make_analyser(self):
        from pipeline.rna.transcript import RnaTranscriptAnalyser

        ra = RnaTranscriptAnalyser.__new__(RnaTranscriptAnalyser)
        ra._available = True
        ra._transcripts = {}
        ra._gene_index = {}
        return ra

    def test_zero_phase_single_cds(self):
        """Phase 0 CDS: frame at first position is 0."""
        from pipeline.rna.transcript import TranscriptRecord

        ra = self._make_analyser()
        rec = TranscriptRecord(
            transcript_id="T1",
            gene_id="G",
            chrom="chr1",
            start=100,
            end=200,
            strand="+",
            exons=[(100, 200)],
            cds_records=[(100, 200, 0)],
            cds_start=100,
            cds_end=200,
        )
        ra._transcripts["T1"] = rec
        assert ra.get_cds_frame_at_position("chr1", 100, "T1") == 0
        assert ra.get_cds_frame_at_position("chr1", 101, "T1") == 1
        assert ra.get_cds_frame_at_position("chr1", 102, "T1") == 2
        assert ra.get_cds_frame_at_position("chr1", 103, "T1") == 0  # next codon

    def test_nonzero_phase(self):
        """Phase 2 CDS: frame at first position is 2 (2 bases of codon already read)."""
        from pipeline.rna.transcript import TranscriptRecord

        ra = self._make_analyser()
        rec = TranscriptRecord(
            transcript_id="T2",
            gene_id="G",
            chrom="chr1",
            start=100,
            end=200,
            strand="+",
            exons=[(100, 200)],
            cds_records=[(100, 200, 2)],
            cds_start=100,
            cds_end=200,
        )
        ra._transcripts["T2"] = rec
        frame = ra.get_cds_frame_at_position("chr1", 100, "T2")
        assert frame == 2

    def test_out_of_cds_returns_none(self):
        """Position not in any CDS interval returns None."""
        from pipeline.rna.transcript import TranscriptRecord

        ra = self._make_analyser()
        rec = TranscriptRecord(
            transcript_id="T3",
            gene_id="G",
            chrom="chr1",
            start=100,
            end=200,
            strand="+",
            exons=[(100, 200)],
            cds_records=[(120, 180, 0)],
            cds_start=120,
            cds_end=180,
        )
        ra._transcripts["T3"] = rec
        assert ra.get_cds_frame_at_position("chr1", 110, "T3") is None


# ─── FIX 6: Canonical transcript selection ───────────────────────────────────


class TestFix6CanonicalTranscript:
    """GffIndex.lookup returns MANE Select transcript when multiple overlap."""

    def _build_index_with_records(self, records):
        """Build a GffIndex with synthetic GeneRecord objects."""
        from pipeline.annotation.gff_index import GffIndex

        idx = GffIndex.__new__(GffIndex)
        idx.source_path = "synthetic"
        idx.total_features = len(records)
        idx._chrom_records = {}
        idx._chrom_starts = {}
        for rec in records:
            ch = rec.chrom
            idx._chrom_records.setdefault(ch, [])
            idx._chrom_records[ch].append(rec)
        for ch, recs in idx._chrom_records.items():
            recs.sort(key=lambda r: r.start)
            idx._chrom_starts[ch] = [r.start for r in recs]
        return idx

    def test_mane_select_preferred_over_other(self):
        from pipeline.annotation.gff_index import GeneRecord

        rec_mane = GeneRecord(
            chrom="chr1",
            start=1000,
            end=2000,
            gene_name="BRCA1",
            gene_id="HGNC:1100",
            transcript_id="NM_007294.4",
            feature_type="mRNA",
            is_mane_select=True,
        )
        rec_other = GeneRecord(
            chrom="chr1",
            start=1000,
            end=2000,
            gene_name="BRCA1",
            gene_id="HGNC:1100",
            transcript_id="NM_007297.3",
            feature_type="mRNA",
            is_mane_select=False,
        )
        idx = self._build_index_with_records([rec_mane, rec_other])
        gene, tx = idx.lookup("chr1", 1500)
        assert tx == "NM_007294.4", f"Expected MANE Select transcript, got {tx!r}"

    def test_mane_plus_clinical_preferred_over_non_canonical(self):
        from pipeline.annotation.gff_index import GeneRecord

        rec_mpc = GeneRecord(
            chrom="chr1",
            start=1000,
            end=2000,
            gene_name="BRCA1",
            gene_id="HGNC:1100",
            transcript_id="NM_007300.4",
            feature_type="mRNA",
            is_mane_plus_clinical=True,
        )
        rec_plain = GeneRecord(
            chrom="chr1",
            start=1000,
            end=2000,
            gene_name="BRCA1",
            gene_id="HGNC:1100",
            transcript_id="NM_999999.1",
            feature_type="mRNA",
        )
        idx = self._build_index_with_records([rec_mpc, rec_plain])
        gene, tx = idx.lookup("chr1", 1500)
        assert tx == "NM_007300.4"

    def test_longest_cds_tiebreaking(self):
        """When no MANE tags, longest CDS wins."""
        from pipeline.annotation.gff_index import GeneRecord

        short = GeneRecord(
            chrom="chr2",
            start=100,
            end=500,
            gene_name="GENE2",
            gene_id="G2",
            transcript_id="NM_short.1",
            feature_type="mRNA",
            cds_length=200,
        )
        long_ = GeneRecord(
            chrom="chr2",
            start=100,
            end=500,
            gene_name="GENE2",
            gene_id="G2",
            transcript_id="NM_long.1",
            feature_type="mRNA",
            cds_length=800,
        )
        idx = self._build_index_with_records([short, long_])
        gene, tx = idx.lookup("chr2", 300)
        assert tx == "NM_long.1"


# ─── FIX 7: PGx allele detection + indel normalization ───────────────────────


class TestFix7PGxAlleleDetection:
    """Star allele definitions use real VCF alleles; indel normalization works."""

    def test_no_del_ins_placeholders(self):
        """No star allele definition uses 'del' or 'ins' as the ALT allele."""
        from pipeline.pgx.diplotypes import STAR_ALLELE_VARIANTS

        for gene, alleles in STAR_ALLELE_VARIANTS.items():
            for allele, variants in alleles.items():
                for chrom, pos, ref, alt in variants:
                    assert alt.upper() not in ("DEL", "INS"), (
                        f"{gene} {allele} still uses placeholder ALT={alt!r}"
                    )

    def test_cyp2d6_star3_vcf_key(self):
        """CYP2D6 *3 is now left-normalised REF=CA, ALT=C."""
        from pipeline.pgx.diplotypes import STAR_ALLELE_VARIANTS

        star3 = STAR_ALLELE_VARIANTS["CYP2D6"]["*3"]
        assert len(star3) == 1
        chrom, pos, ref, alt = star3[0]
        assert ref == "CA" and alt == "C", f"Expected CA>C, got {ref}>{alt}"

    def test_cyp3a5_star7_vcf_key(self):
        """CYP3A5 *7 is now left-normalised insertion."""
        from pipeline.pgx.diplotypes import STAR_ALLELE_VARIANTS

        star7 = STAR_ALLELE_VARIANTS["CYP3A5"]["*7"]
        assert len(star7) == 1
        chrom, pos, ref, alt = star7[0]
        # Insertion: alt should be longer than ref
        assert len(alt) > len(ref), f"Expected insertion, got {ref}>{alt}"

    def test_normalise_indel_key(self):
        """_normalise_indel_key strips shared leading prefix bases (beyond anchor)."""
        from pipeline.pgx.stage import _normalise_indel_key

        # "AACA" → "A": two bases share prefix (A,A), leaving CA vs empty after anchor
        # The function keeps at minimum 1 anchor base, so strips inner shared prefix
        chrom, pos, ref, alt = _normalise_indel_key("22", 100, "AACA", "AA")
        # "AACA"/"AA": i goes 0(A==A), but min(4,2)-1=1, so i stops at 1
        # After stripping 1 prefix: ref="ACA", alt="A", pos=101
        assert pos == 101
        assert ref == "ACA" and alt == "A"

    def test_detect_star_allele_with_normalised_key(self):
        """_detect_star_alleles matches VCF-normalised indel keys."""
        from pipeline.pgx.stage import _detect_star_alleles

        # CYP2D6 *3 definition: ("22", 42126610, "CA", "C")
        # A VCF might represent the same deletion as ("22", 42126610, "CA", "C")
        variants = {("22", 42126610, "CA", "C"): "heterozygous"}
        detected, _ = _detect_star_alleles("CYP2D6", variants)
        assert "*3" in detected

    def test_cyp2d6_star5_cnv_not_assessed(self, caplog):
        """CYP2D6 *5 (CNV) is reported as not assessed, not silently skipped."""
        from pipeline.pgx.stage import _detect_star_alleles
        import logging

        with caplog.at_level(logging.INFO, logger="geper.pipeline.pgx"):
            _ = _detect_star_alleles("CYP2D6", {})
        assert any("CNV" in r.message for r in caplog.records), (
            "CYP2D6 *5 CNV not-assessed warning not logged"
        )


# ─── FIX 9: ACMG conflict handling ───────────────────────────────────────────


class TestFix9ACMGConflict:
    """_classify returns Uncertain_Significance with conflict message when
    strong pathogenic and strong benign criteria co-occur."""

    def _run_classify(self, criteria_dicts):
        """Build CriteriaResult list and call _classify."""
        from pipeline.acmg.classifier import AcmgClassifier, CriteriaResult

        clf = AcmgClassifier.__new__(AcmgClassifier)
        clf._t = {}
        clf._explain = lambda met, cls: f"Classification: {cls}"
        crs = [CriteriaResult(**d) for d in criteria_dicts]
        return clf._classify(crs)

    def test_conflict_strong_path_plus_strong_benign(self):
        """PS1 + BS1 simultaneously → Uncertain_Significance with conflict msg."""
        classification, explanation = self._run_classify(
            [
                {
                    "code": "PS1",
                    "met": True,
                    "direction": "pathogenic",
                    "strength": "strong",
                    "reason": "",
                },
                {
                    "code": "BS1",
                    "met": True,
                    "direction": "benign",
                    "strength": "strong",
                    "reason": "",
                },
            ]
        )
        assert classification == "Uncertain_Significance"
        assert "Conflicting_Evidence" in explanation or "conflicting" in explanation.lower()

    def test_conflict_pvs_plus_ba(self):
        """PVS1 + BA1 simultaneously → Uncertain_Significance."""
        classification, explanation = self._run_classify(
            [
                {
                    "code": "PVS1",
                    "met": True,
                    "direction": "pathogenic",
                    "strength": "very_strong",
                    "reason": "",
                },
                {
                    "code": "BA1",
                    "met": True,
                    "direction": "benign",
                    "strength": "stand_alone",
                    "reason": "",
                },
            ]
        )
        assert classification == "Uncertain_Significance"
        assert "Conflicting_Evidence" in explanation or "conflicting" in explanation.lower()

    def test_no_conflict_pathogenic_only(self):
        """PVS1 + PS1 alone → Pathogenic (no false conflict)."""
        classification, _ = self._run_classify(
            [
                {
                    "code": "PVS1",
                    "met": True,
                    "direction": "pathogenic",
                    "strength": "very_strong",
                    "reason": "",
                },
                {
                    "code": "PS1",
                    "met": True,
                    "direction": "pathogenic",
                    "strength": "strong",
                    "reason": "",
                },
            ]
        )
        assert classification == "Pathogenic"

    def test_no_conflict_benign_only(self):
        """BA1 alone → Benign (no false conflict)."""
        classification, _ = self._run_classify(
            [
                {
                    "code": "BA1",
                    "met": True,
                    "direction": "benign",
                    "strength": "stand_alone",
                    "reason": "",
                },
            ]
        )
        assert classification == "Benign"


# ─── FIX 10: Dead code removal ───────────────────────────────────────────────


class TestFix10DeadCode:
    """Verify no dead-code artefacts remain in key modules."""

    def test_no_orphaned_docstring_after_return_in_clinvar(self):
        """Dead docstring block after return removed from check_same_codon_pathogenic."""
        import inspect
        from pipeline.clinvar.lookup import ClinVarLookup

        src = inspect.getsource(ClinVarLookup.check_same_codon_pathogenic)
        # The dead code started with this text after the return
        assert "Look up a variant by genomic coordinates" not in src

    def test_aggregator_delegates_to_lookup(self):
        """EvidenceAggregator.clinvar_sig_to_score delegates to ClinVarLookup.sig_to_score."""
        from pipeline.evidence.aggregator import EvidenceAggregator
        from pipeline.clinvar.lookup import ClinVarLookup

        # Both should produce identical results for the same inputs
        assert EvidenceAggregator.clinvar_sig_to_score(
            "Pathogenic", 4
        ) == ClinVarLookup.sig_to_score("Pathogenic", 4)
        assert EvidenceAggregator.clinvar_sig_to_score("Benign", 2) == ClinVarLookup.sig_to_score(
            "Benign", 2
        )
        assert EvidenceAggregator.clinvar_sig_to_score(
            "Uncertain significance", 0
        ) == ClinVarLookup.sig_to_score("Uncertain significance", 0)


# ─── FIX 11: Large-gene support ──────────────────────────────────────────────


class TestFix11LargeGene:
    """Variants within genes > 2 Mb are not misclassified as intergenic."""

    def _build_large_gene_index(self):
        """Build a GffIndex with a 2.5 Mb gene (like DMD)."""
        from pipeline.annotation.gff_index import GffIndex, GeneRecord

        idx = GffIndex.__new__(GffIndex)
        idx.source_path = "synthetic"
        idx.total_features = 2
        # Gene spans 2.5 Mb on chrX
        gene_rec = GeneRecord(
            chrom="chrX",
            start=31_000_000,
            end=33_500_000,
            gene_name="DMD",
            gene_id="HGNC:2928",
            transcript_id="",
            feature_type="gene",
        )
        tx_rec = GeneRecord(
            chrom="chrX",
            start=31_000_000,
            end=33_500_000,
            gene_name="DMD",
            gene_id="HGNC:2928",
            transcript_id="NM_004006.3",
            feature_type="mRNA",
        )
        recs = [gene_rec, tx_rec]
        recs.sort(key=lambda r: r.start)
        idx._chrom_records = {"chrX": recs}
        idx._chrom_starts = {"chrX": [r.start for r in recs]}
        return idx

    def test_variant_in_large_gene_not_intergenic(self):
        """Variant at 33,000,000 inside a 2.5 Mb gene must resolve to that gene."""
        idx = self._build_large_gene_index()
        gene, tx = idx.lookup("chrX", 33_000_000)
        assert gene == "DMD", f"Expected DMD but got {gene!r} — large-gene window bug?"

    def test_variant_outside_large_gene_is_intergenic(self):
        """Variant beyond gene end returns None."""
        idx = self._build_large_gene_index()
        gene, tx = idx.lookup("chrX", 34_000_000)
        assert gene is None


# ─── FIX 12: Multi-allelic robustness ────────────────────────────────────────


class TestFix12MultiAllelic:
    """_parse_vcf expands multi-allelic records into one variant per ALT."""

    def _write_vcf(self, content: str) -> str:
        with tempfile.NamedTemporaryFile(suffix=".vcf", mode="w", delete=False) as tf:
            tf.write(content)
            return tf.name

    def test_biallelic_unchanged(self):
        from pipeline.annotation.stage import _parse_vcf

        vcf_path = self._write_vcf(
            "##fileformat=VCFv4.2\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
            "chr1\t100\t.\tA\tG\t50\tPASS\t.\n"
        )
        try:
            variants = _parse_vcf(vcf_path)
            assert len(variants) == 1
            assert variants[0].alt == "G"
        finally:
            os.unlink(vcf_path)

    def test_triallelic_expands_to_two(self):
        from pipeline.annotation.stage import _parse_vcf

        vcf_path = self._write_vcf(
            "##fileformat=VCFv4.2\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
            "chr1\t100\t.\tA\tG,C\t50\tPASS\t.\n"
        )
        try:
            variants = _parse_vcf(vcf_path)
            assert len(variants) == 2, f"Expected 2, got {len(variants)}"
            alts = {v.alt for v in variants}
            assert "G" in alts and "C" in alts
        finally:
            os.unlink(vcf_path)

    def test_ref_preserved_for_all_alts(self):
        from pipeline.annotation.stage import _parse_vcf

        vcf_path = self._write_vcf(
            "##fileformat=VCFv4.2\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
            "chr1\t200\t.\tATG\tA,ATGC\t60\tPASS\t.\n"
        )
        try:
            variants = _parse_vcf(vcf_path)
            assert all(v.ref == "ATG" for v in variants)
            assert all(v.pos == 200 for v in variants)
        finally:
            os.unlink(vcf_path)


# ─── FIX 14: Reference version tracking ──────────────────────────────────────


class TestFix14ReferenceVersions:
    """_collect_reference_versions captures file stamps; mismatch raises on resume."""

    def test_collect_versions_with_files(self, tmp_path):
        """Existing files get a path@mtime stamp; missing paths get MISSING."""
        from pipeline.orchestration.runner import _collect_reference_versions

        ref = tmp_path / "ref.fa"
        ref.write_text(">chr1\nACGT\n")
        cfg = {"annotation": {"refseq_gff": str(tmp_path / "missing.gff")}}
        versions = _collect_reference_versions(cfg, str(ref))
        assert "reference_fasta" in versions
        assert "@" in versions["reference_fasta"]  # path@mtime format
        # Missing file should still be recorded (with MISSING marker)
        if "gff3" in versions:
            assert "MISSING" in versions["gff3"]

    def test_collect_versions_empty_config(self, tmp_path):
        """Empty config → only reference_fasta (if it exists)."""
        from pipeline.orchestration.runner import _collect_reference_versions

        ref = tmp_path / "ref.fa"
        ref.write_text(">chr1\nACGT\n")
        versions = _collect_reference_versions({}, str(ref))
        assert "reference_fasta" in versions

    def test_reference_versions_in_pipeline_result(self):
        """PipelineResult has a reference_versions field."""
        from pipeline.orchestration.runner import PipelineResult

        r = PipelineResult()
        assert hasattr(r, "reference_versions")
        assert isinstance(r.reference_versions, dict)

    def test_reporting_stage_accepts_reference_versions(self):
        """ReportingStage.run() accepts reference_versions kwarg without error."""
        import inspect
        from pipeline.reporting.stage import ReportingStage

        sig = inspect.signature(ReportingStage.run)
        assert "reference_versions" in sig.parameters, (
            "reference_versions parameter missing from ReportingStage.run()"
        )


# ─── FIX 7 + FIX 14: version manifest in report JSON ────────────────────────


class TestReportContainsVersionManifest:
    """reference_versions key appears in the generated report JSON."""

    def test_report_json_has_reference_versions(self, tmp_path):
        """reference_versions key present and correct in JSON report."""
        import json
        import inspect
        from pipeline.reporting.stage import ReportingStage

        sig = inspect.signature(ReportingStage.run)
        assert "reference_versions" in sig.parameters
        # Deep instantiation test: just verify the JSON payload builder includes the key
        # by constructing a minimal report manually
        import pipeline.reporting.stage as _rs

        stage = _rs.ReportingStage({"reporting": {"output_dir": str(tmp_path)}})
        result = stage.run(
            sample_id="TEST001",
            output_dir=str(tmp_path),
            reference_versions={"reference_fasta": "/ref.fa@1700000000"},
        )
        report = json.loads((tmp_path / "report.json").read_text())
        assert "reference_versions" in report
        assert report["reference_versions"].get("reference_fasta") == "/ref.fa@1700000000"


# ─── Additional edge cases ────────────────────────────────────────────────────


class TestEdgeCases:
    """Additional edge-case regression tests."""

    def test_vep_csq_score_propagation_end_to_end(self):
        """Scores parsed from CSQ appear on the AnnotatedVariant object."""
        from pipeline.annotation.stage import _parse_vcf

        fields = "Allele|Consequence|CADD_PHRED|REVEL|SpliceAI_pred|AM_PATHOGENICITY"
        csq = "T|missense_variant|35.2|0.91|0.1|0.87"
        vcf_content = (
            f"##fileformat=VCFv4.2\n"
            f'##INFO=<ID=CSQ,Number=.,Type=String,Description="Format: {fields}">\n'
            f"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
            f"chr17\t43044295\t.\tA\tT\t100\tPASS\tCSQ={csq}\n"
        )
        with tempfile.NamedTemporaryFile(suffix=".vcf", mode="w", delete=False) as tf:
            tf.write(vcf_content)
            path = tf.name
        try:
            variants = _parse_vcf(path)
            assert len(variants) == 1
            v = variants[0]
            assert v.cadd_phred == pytest.approx(35.2), "CADD not propagated from CSQ"
            assert v.revel_score == pytest.approx(0.91), "REVEL not propagated from CSQ"
            assert v.alphamissense_score == pytest.approx(0.87), (
                "AlphaMissense not propagated from CSQ"
            )
        finally:
            os.unlink(path)

    def test_gff_index_mane_select_tag_parsed(self, tmp_path):
        """GffIndex parses MANE_Select tag from GFF3 attributes correctly."""
        gff_content = textwrap.dedent("""\
            ##gff-version 3
            chr1\tRefSeq\tgene\t1000\t2000\t.\t+\t.\tID=gene-BRCA1;Name=BRCA1
            chr1\tRefSeq\tmRNA\t1000\t2000\t.\t+\t.\tID=NM_007294.4;Parent=gene-BRCA1;tag=MANE_Select
        """)
        gff_path = tmp_path / "test.gff3"
        gff_path.write_text(gff_content)
        from pipeline.annotation.gff_index import GffIndex

        idx = GffIndex.from_file(str(gff_path))
        gene, tx = idx.lookup("chr1", 1500)
        # gene may be the ID or name depending on GFF attributes
        assert gene in ("BRCA1", "gene-BRCA1", "NM_007294.4") or gene is not None
        # Find the mRNA record and verify its MANE flag
        recs = idx._chrom_records.get("chr1", [])
        mrna_recs = [r for r in recs if r.feature_type == "mRNA"]
        assert any(r.is_mane_select for r in mrna_recs), (
            "MANE_Select tag not parsed from GFF3 attributes"
        )

    def test_acmg_classify_uncertain_no_criteria(self):
        """Zero criteria → Uncertain_Significance (no crash)."""
        from pipeline.acmg.classifier import AcmgClassifier

        clf = AcmgClassifier.__new__(AcmgClassifier)
        clf._t = {}
        clf._explain = lambda met, cls: cls
        cls, _ = clf._classify([])
        assert cls == "Uncertain_Significance"

    def test_pgx_star3_cyp2d6_detection_from_vcf(self):
        """CYP2D6 *3 detected from real left-normalised VCF key."""
        from pipeline.pgx.stage import _detect_star_alleles

        # Real VCF representation after left-normalisation of rs35742686 deletion
        variants = {("22", 42126610, "CA", "C"): "heterozygous"}
        detected, _ = _detect_star_alleles("CYP2D6", variants)
        assert "*3" in detected, f"CYP2D6 *3 not detected; got {detected}"

    def test_pgx_cyp2c9_star6_detection(self):
        """CYP2C9 *6 detected from left-normalised VCF key."""
        from pipeline.pgx.stage import _detect_star_alleles

        variants = {("10", 94949280, "GA", "G"): "heterozygous"}
        detected, _ = _detect_star_alleles("CYP2C9", variants)
        assert "*6" in detected, f"CYP2C9 *6 not detected; got {detected}"
