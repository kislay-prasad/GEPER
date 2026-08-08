"""
Tests for `pipeline/clingen/utils.py`'s allele-aware gene resolution --
the fifth instance of the "first-record-wins" bug found in this
codebase (after ClinVar's record selection, ClinVar's own retrieval
narrowing, dbSNP's rsID resolution, and now `resolve_gene_symbol`'s
Ensembl gene-overlap `[0]` pick).

Ground truth for the overlapping-gene disambiguation tests: GRCh38
19:1,177,558-1,239,465 -- live-confirmed (2026-07-31) via Ensembl's
`overlap/region?feature=gene` that `STK11` (1,177,558-1,228,431,
+strand, the real Peutz-Jeghers-syndrome tumor suppressor) and `CBARP`
(1,228,282-1,239,465, -strand) are two independent, real,
protein_coding genes overlapping on opposite strands -- a query at
19:1,228,350 (inside STK11's last exon) returns both. The exon/CDS
coordinates used in the mocked transcript fixtures below are the real
Ensembl-reported boundaries for each gene's canonical transcript
(STK11 ENST00000326873, CBARP/C19orf21 ENST00000585748-family),
trimmed to what `TranscriptContext.cds_position` needs.
"""

import unittest
from unittest import mock

from pipeline.clingen.utils import (
    GeneResolutionStatus,
    resolve_gene_symbol,
    resolve_gene_symbol_detail,
)

_STK11_POS = 1228350  # inside STK11's real last coding exon, and CBARP's real first exon (opposite strand)


def _gene_feature(name, start, end, strand, biotype="protein_coding"):
    return {
        "external_name": name,
        "gene_id": f"ENSG_{name}",
        "start": start,
        "end": end,
        "strand": strand,
        "biotype": biotype,
    }


# Real, live-confirmed overlap (see module docstring).
_STK11_FEATURE = _gene_feature("STK11", 1177558, 1228431, 1)
_CBARP_FEATURE = _gene_feature("CBARP", 1228282, 1239465, -1)


def _transcript_record(gene_symbol, cds_start, cds_end, strand, is_mane_select=False):
    """Minimal record shape `pipeline.pvs1.utils.transcript_context_from_dict` accepts."""
    return {
        "transcript_id": f"ENST_{gene_symbol}",
        "gene_symbol": gene_symbol,
        "chrom": "19",
        "strand": strand,
        "exons": [{"start": cds_start - 500, "end": cds_end + 500}],
        "cds_genomic_start": cds_start,
        "cds_genomic_end": cds_end,
        "protein_length": (cds_end - cds_start + 1) // 3,
        "is_mane_select": is_mane_select,
        "is_canonical": True,
        "source": "test_fixture",
    }


class TestVcfGeneHintTakesPriority(unittest.TestCase):
    def test_vcf_gene_hint_used_directly_no_network_call(self):
        with (
            mock.patch("pipeline.clingen.utils._fetch_overlapping_genes") as fake_fetch,
            mock.patch("pipeline.clingen.utils.genes_overlapping") as fake_local_fetch,
        ):
            resolution = resolve_gene_symbol_detail("19", _STK11_POS, build="GRCh38", vcf_gene_hint="STK11")
        fake_fetch.assert_not_called()
        fake_local_fetch.assert_not_called()
        self.assertEqual(resolution.status, GeneResolutionStatus.RESOLVED)
        self.assertEqual(resolution.gene_symbol, "STK11")
        self.assertEqual(resolution.source, "vcf_gene_info")

    def test_vcf_gene_hint_is_case_normalized(self):
        resolution = resolve_gene_symbol_detail("19", _STK11_POS, build="GRCh38", vcf_gene_hint="stk11")
        self.assertEqual(resolution.gene_symbol, "STK11")

    def test_vcf_gene_hint_wins_even_when_it_names_the_other_overlapping_gene(self):
        """Trusting the VCF's own annotation outright means it wins
        even for the less-"obvious" candidate at an overlapping locus
        -- the whole point is that GEPER has no basis to second-guess
        upstream transcript-aware annotation with a bare coordinate
        overlap check."""
        resolution = resolve_gene_symbol_detail("19", _STK11_POS, build="GRCh38", vcf_gene_hint="CBARP")
        self.assertEqual(resolution.gene_symbol, "CBARP")


class TestEnsemblFallbackSingleCandidate(unittest.TestCase):
    def test_single_protein_coding_gene_resolves(self):
        with (
            mock.patch("pipeline.clingen.utils._fetch_overlapping_genes", return_value=[_STK11_FEATURE]),
            mock.patch("pipeline.clingen.utils.genes_overlapping", return_value=None),
        ):
            resolution = resolve_gene_symbol_detail("19", _STK11_POS, build="GRCh38")
        self.assertEqual(resolution.status, GeneResolutionStatus.RESOLVED)
        self.assertEqual(resolution.gene_symbol, "STK11")
        self.assertEqual(resolution.source, "ensembl_single_candidate")

    def test_no_features_at_all_is_not_found(self):
        with (
            mock.patch("pipeline.clingen.utils._fetch_overlapping_genes", return_value=[]),
            mock.patch("pipeline.clingen.utils.genes_overlapping", return_value=None),
        ):
            resolution = resolve_gene_symbol_detail("1", 999999999, build="GRCh38")
        self.assertEqual(resolution.status, GeneResolutionStatus.NOT_FOUND)
        self.assertIsNone(resolution.gene_symbol)

    def test_request_failure_is_not_found_not_a_crash(self):
        with (
            mock.patch("pipeline.clingen.utils._fetch_overlapping_genes", return_value=None),
            mock.patch("pipeline.clingen.utils.genes_overlapping", return_value=None),
        ):
            resolution = resolve_gene_symbol_detail("19", _STK11_POS, build="GRCh38")
        self.assertEqual(resolution.status, GeneResolutionStatus.NOT_FOUND)

    def test_single_non_coding_feature_still_resolves_via_fallback_pool(self):
        """No protein_coding candidate at all -- falls back to whatever
        Ensembl returned (preserves the pre-fix behavior for this
        case: a single non-coding gene is still reported rather than
        discarded, since 'skipped ClinGen lookup entirely' is a worse
        outcome than 'looked it up for a non-coding gene symbol')."""
        lncrna = _gene_feature("SOME-AS1", 1177000, 1179000, 1, biotype="lncRNA")
        with (
            mock.patch("pipeline.clingen.utils._fetch_overlapping_genes", return_value=[lncrna]),
            mock.patch("pipeline.clingen.utils.genes_overlapping", return_value=None),
        ):
            resolution = resolve_gene_symbol_detail("19", _STK11_POS, build="GRCh38")
        self.assertEqual(resolution.status, GeneResolutionStatus.RESOLVED)
        self.assertEqual(resolution.gene_symbol, "SOME-AS1")


class TestOverlappingGeneDisambiguation(unittest.TestCase):
    """The real STK11/CBARP scenario -- multiple protein_coding candidates, no VCF hint."""

    def test_cds_containment_resolves_to_the_gene_actually_coding_here(self):
        """At 19:1228350, only STK11's real CDS (1177558-1228431)
        contains the position; CBARP's real CDS starts later. Must
        resolve to STK11 via CDS containment, not response order."""
        stk11_transcript = _transcript_record("STK11", 1177558, 1228431, 1)
        cbarp_transcript = _transcript_record("CBARP", 1230000, 1239465, -1)  # CDS starts after this position

        def fake_query_gene(symbol, build="GRCh38"):
            record = stk11_transcript if symbol == "STK11" else cbarp_transcript
            return {"skipped": False, "found": True, "transcript": record}

        with (
            mock.patch(
                "pipeline.clingen.utils._fetch_overlapping_genes", return_value=[_STK11_FEATURE, _CBARP_FEATURE]
            ),
            mock.patch("pipeline.clingen.utils.genes_overlapping", return_value=None),
            mock.patch("pipeline.pvs1.lookup.TranscriptLookup.query_gene", side_effect=fake_query_gene),
            mock.patch("pipeline.clingen.utils.mane_select_transcript_id", return_value=None),
        ):
            resolution = resolve_gene_symbol_detail("19", _STK11_POS, build="GRCh38")

        self.assertEqual(resolution.status, GeneResolutionStatus.RESOLVED)
        self.assertEqual(resolution.gene_symbol, "STK11")
        self.assertEqual(resolution.source, "ensembl_cds_containment")

    def test_both_cds_contain_position_falls_back_to_mane_select(self):
        """Genuine coding-overlap case (like the real MT-ATP8/MT-ATP6
        mitochondrial overlap): both candidates' CDS cover this exact
        base. MANE Select breaks the tie when exactly one qualifies."""
        gene_a = _transcript_record("GENEA", 1177558, 1230000, 1, is_mane_select=False)
        gene_b = _transcript_record("GENEB", 1228000, 1239465, -1, is_mane_select=True)

        def fake_query_gene(symbol, build="GRCh38"):
            record = gene_a if symbol == "GENEA" else gene_b
            return {"skipped": False, "found": True, "transcript": record}

        features = [_gene_feature("GENEA", 1177558, 1230000, 1), _gene_feature("GENEB", 1228000, 1239465, -1)]
        with (
            mock.patch("pipeline.clingen.utils._fetch_overlapping_genes", return_value=features),
            mock.patch("pipeline.clingen.utils.genes_overlapping", return_value=None),
            mock.patch("pipeline.pvs1.lookup.TranscriptLookup.query_gene", side_effect=fake_query_gene),
            mock.patch("pipeline.clingen.utils.mane_select_transcript_id", return_value=None),
        ):
            resolution = resolve_gene_symbol_detail("19", _STK11_POS, build="GRCh38")

        self.assertEqual(resolution.status, GeneResolutionStatus.RESOLVED)
        self.assertEqual(resolution.gene_symbol, "GENEB")
        self.assertEqual(resolution.source, "ensembl_mane_select")

    def test_genuinely_tied_candidates_are_ambiguous_never_a_guess(self):
        """Neither CDS-containment nor MANE Select breaks the tie ->
        AMBIGUOUS, gene_symbol=None, both candidates named in the
        reason -- never a coin-flip pick."""
        gene_a = _transcript_record("GENEA", 1177558, 1230000, 1, is_mane_select=False)
        gene_b = _transcript_record("GENEB", 1228000, 1239465, -1, is_mane_select=False)

        def fake_query_gene(symbol, build="GRCh38"):
            record = gene_a if symbol == "GENEA" else gene_b
            return {"skipped": False, "found": True, "transcript": record}

        features = [_gene_feature("GENEA", 1177558, 1230000, 1), _gene_feature("GENEB", 1228000, 1239465, -1)]
        with (
            mock.patch("pipeline.clingen.utils._fetch_overlapping_genes", return_value=features),
            mock.patch("pipeline.clingen.utils.genes_overlapping", return_value=None),
            mock.patch("pipeline.pvs1.lookup.TranscriptLookup.query_gene", side_effect=fake_query_gene),
            mock.patch("pipeline.clingen.utils.mane_select_transcript_id", return_value=None),
        ):
            resolution = resolve_gene_symbol_detail("19", _STK11_POS, build="GRCh38")

        self.assertEqual(resolution.status, GeneResolutionStatus.AMBIGUOUS)
        self.assertIsNone(resolution.gene_symbol)
        self.assertEqual(set(resolution.candidates), {"GENEA", "GENEB"})
        self.assertIn("GENEA", resolution.reason)
        self.assertIn("GENEB", resolution.reason)

    def test_neither_cds_contains_position_falls_back_to_full_candidate_pool_for_mane_check(self):
        """Real case: a position can fall in pure intron/UTR of both
        overlapping genes' gene bodies without being in either's CDS
        (e.g. a deep-intronic variant). MANE Select is still tried
        across the full candidate pool in that case."""
        gene_a = _transcript_record("GENEA", 1177558, 1177600, 1, is_mane_select=True)  # CDS far from query pos
        gene_b = _transcript_record("GENEB", 1239000, 1239465, -1, is_mane_select=False)  # CDS far from query pos

        def fake_query_gene(symbol, build="GRCh38"):
            record = gene_a if symbol == "GENEA" else gene_b
            return {"skipped": False, "found": True, "transcript": record}

        features = [_gene_feature("GENEA", 1177558, 1230000, 1), _gene_feature("GENEB", 1228000, 1239465, -1)]
        with (
            mock.patch("pipeline.clingen.utils._fetch_overlapping_genes", return_value=features),
            mock.patch("pipeline.clingen.utils.genes_overlapping", return_value=None),
            mock.patch("pipeline.pvs1.lookup.TranscriptLookup.query_gene", side_effect=fake_query_gene),
            mock.patch("pipeline.clingen.utils.mane_select_transcript_id", return_value=None),
        ):
            resolution = resolve_gene_symbol_detail("19", _STK11_POS, build="GRCh38")

        self.assertEqual(resolution.status, GeneResolutionStatus.RESOLVED)
        self.assertEqual(resolution.gene_symbol, "GENEA")
        self.assertEqual(resolution.source, "ensembl_mane_select")

    def test_transcript_lookup_failure_does_not_crash_falls_through_to_ambiguous(self):
        features = [_STK11_FEATURE, _CBARP_FEATURE]
        with (
            mock.patch("pipeline.clingen.utils._fetch_overlapping_genes", return_value=features),
            mock.patch("pipeline.clingen.utils.genes_overlapping", return_value=None),
            mock.patch("pipeline.pvs1.lookup.TranscriptLookup.query_gene", side_effect=RuntimeError("network boom")),
        ):
            resolution = resolve_gene_symbol_detail("19", _STK11_POS, build="GRCh38")
        self.assertEqual(resolution.status, GeneResolutionStatus.AMBIGUOUS)


class TestRealManeDatasetBreaksTheTie(unittest.TestCase):
    """
    End-to-end verification that `pipeline/mane/provider.py`'s dataset
    lookup -- not just the pre-existing (permanently-inert-in-production,
    since Ensembl's `lookup/id` response never carries a MANE field)
    `transcript.is_mane_select` flag -- actually breaks a tie. Mocks
    `pipeline.clingen.utils.mane_select_transcript_id` directly (the
    same seam `TestOverlappingGeneDisambiguation`'s other tests already
    patch to `None`), simulating the real dataset having an answer for
    exactly one of two tied candidates.
    """

    def test_mane_dataset_resolves_a_tie_the_ensembl_flag_alone_cannot(self):
        # Both candidates report is_mane_select=False (the real,
        # always-false-in-production Ensembl-payload state) -- if the
        # wiring only consulted that flag, this would stay AMBIGUOUS
        # exactly as it did before this feature. GENEA's own transcript_id
        # ("ENST_GENEA", see `_transcript_record`) matches what the
        # (mocked) MANE dataset reports as GENEA's MANE Select
        # transcript; GENEB's does not.
        gene_a = _transcript_record("GENEA", 1177558, 1230000, 1, is_mane_select=False)
        gene_b = _transcript_record("GENEB", 1228000, 1239465, -1, is_mane_select=False)

        def fake_query_gene(symbol, build="GRCh38"):
            record = gene_a if symbol == "GENEA" else gene_b
            return {"skipped": False, "found": True, "transcript": record}

        def fake_mane_lookup(symbol):
            return "ENST_GENEA" if symbol == "GENEA" else None

        features = [_gene_feature("GENEA", 1177558, 1230000, 1), _gene_feature("GENEB", 1228000, 1239465, -1)]
        with (
            mock.patch("pipeline.clingen.utils._fetch_overlapping_genes", return_value=features),
            mock.patch("pipeline.clingen.utils.genes_overlapping", return_value=None),
            mock.patch("pipeline.pvs1.lookup.TranscriptLookup.query_gene", side_effect=fake_query_gene),
            mock.patch("pipeline.clingen.utils.mane_select_transcript_id", side_effect=fake_mane_lookup),
        ):
            resolution = resolve_gene_symbol_detail("19", _STK11_POS, build="GRCh38")

        self.assertEqual(resolution.status, GeneResolutionStatus.RESOLVED)
        self.assertEqual(resolution.gene_symbol, "GENEA")
        self.assertEqual(resolution.source, "ensembl_mane_select")

    def test_mane_answer_that_does_not_match_this_candidates_own_transcript_does_not_count(self):
        """The MANE dataset having *an* entry for a gene isn't enough --
        it must match the specific transcript TranscriptLookup returned
        for that gene. A mismatched transcript ID (e.g. TranscriptLookup
        resolved a different, non-MANE transcript for this gene) must
        not be treated as a MANE Select hit."""
        gene_a = _transcript_record("GENEA", 1177558, 1230000, 1, is_mane_select=False)
        gene_b = _transcript_record("GENEB", 1228000, 1239465, -1, is_mane_select=False)

        def fake_query_gene(symbol, build="GRCh38"):
            record = gene_a if symbol == "GENEA" else gene_b
            return {"skipped": False, "found": True, "transcript": record}

        # MANE's real transcript for GENEA is some other ID entirely --
        # not the one TranscriptLookup happened to return here.
        def fake_mane_lookup(symbol):
            return "ENST_SOME_OTHER_TRANSCRIPT" if symbol == "GENEA" else None

        features = [_gene_feature("GENEA", 1177558, 1230000, 1), _gene_feature("GENEB", 1228000, 1239465, -1)]
        with (
            mock.patch("pipeline.clingen.utils._fetch_overlapping_genes", return_value=features),
            mock.patch("pipeline.clingen.utils.genes_overlapping", return_value=None),
            mock.patch("pipeline.pvs1.lookup.TranscriptLookup.query_gene", side_effect=fake_query_gene),
            mock.patch("pipeline.clingen.utils.mane_select_transcript_id", side_effect=fake_mane_lookup),
        ):
            resolution = resolve_gene_symbol_detail("19", _STK11_POS, build="GRCh38")

        self.assertEqual(resolution.status, GeneResolutionStatus.AMBIGUOUS)

    def test_gene_absent_from_mane_dataset_still_falls_through_to_ambiguous(self):
        """Honest degradation: when neither tied candidate has any
        MANE Select entry at all (mitochondrial-gene-style gap, e.g.
        the real MT-ATP8/MT-ATP6 overlap -- confirmed live absent from
        the real MANE dataset), the tie-break must fall through to
        AMBIGUOUS exactly as it did before this dataset was wired in,
        never guess."""
        gene_a = _transcript_record("GENEA", 1177558, 1230000, 1, is_mane_select=False)
        gene_b = _transcript_record("GENEB", 1228000, 1239465, -1, is_mane_select=False)

        def fake_query_gene(symbol, build="GRCh38"):
            record = gene_a if symbol == "GENEA" else gene_b
            return {"skipped": False, "found": True, "transcript": record}

        features = [_gene_feature("GENEA", 1177558, 1230000, 1), _gene_feature("GENEB", 1228000, 1239465, -1)]
        with (
            mock.patch("pipeline.clingen.utils._fetch_overlapping_genes", return_value=features),
            mock.patch("pipeline.clingen.utils.genes_overlapping", return_value=None),
            mock.patch("pipeline.pvs1.lookup.TranscriptLookup.query_gene", side_effect=fake_query_gene),
            mock.patch("pipeline.clingen.utils.mane_select_transcript_id", return_value=None),
        ):
            resolution = resolve_gene_symbol_detail("19", _STK11_POS, build="GRCh38")

        self.assertEqual(resolution.status, GeneResolutionStatus.AMBIGUOUS)


class TestBackwardCompatibleWrapper(unittest.TestCase):
    def test_resolve_gene_symbol_returns_plain_string_when_resolved(self):
        with (
            mock.patch("pipeline.clingen.utils._fetch_overlapping_genes", return_value=[_STK11_FEATURE]),
            mock.patch("pipeline.clingen.utils.genes_overlapping", return_value=None),
        ):
            self.assertEqual(resolve_gene_symbol("19", _STK11_POS, build="GRCh38"), "STK11")

    def test_resolve_gene_symbol_returns_none_for_ambiguous_never_a_guess(self):
        gene_a = _transcript_record("GENEA", 1177558, 1230000, 1)
        gene_b = _transcript_record("GENEB", 1228000, 1239465, -1)

        def fake_query_gene(symbol, build="GRCh38"):
            record = gene_a if symbol == "GENEA" else gene_b
            return {"skipped": False, "found": True, "transcript": record}

        features = [_gene_feature("GENEA", 1177558, 1230000, 1), _gene_feature("GENEB", 1228000, 1239465, -1)]
        with (
            mock.patch("pipeline.clingen.utils._fetch_overlapping_genes", return_value=features),
            mock.patch("pipeline.clingen.utils.genes_overlapping", return_value=None),
            mock.patch("pipeline.pvs1.lookup.TranscriptLookup.query_gene", side_effect=fake_query_gene),
            mock.patch("pipeline.clingen.utils.mane_select_transcript_id", return_value=None),
        ):
            self.assertIsNone(resolve_gene_symbol("19", _STK11_POS, build="GRCh38"))


if __name__ == "__main__":
    unittest.main()
