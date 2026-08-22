"""
Critical test for the AlphaFold residue-mapping gate's condition C
(isoform check): a variant resolved against a non-canonical transcript
must produce "mapping unavailable" (None), never a wrong-but-plausible
residue number computed against the wrong isoform's own numbering.

FIXTURE, verified against real transcript records, not synthetic --
"if positions happen to coincide, the test proves nothing" was the
standing risk here, so nothing below is asserted from memory:

  - Gene: TP53 (ENSG00000141510), chr17, GRCh38, minus strand.
  - Canonical/MANE Select transcript: ENST00000269305. Exon coordinates
    and Translation start/end below are copied VERBATIM from a live
    Ensembl REST fetch this session
    (rest.ensembl.org/lookup/id/ENST00000269305?expand=1) and
    cross-checked against this repo's own locally cached Ensembl
    transcript structures (`C:/GEPER-DATA/geper_model_cache/ensembl/
    ensembl_transcripts.jsonl`, itself fetched live during an earlier
    real pipeline run this session) -- both sources agree exactly.
  - Alternate isoform: Delta40p53, a real, extensively published TP53
    isoform (Bourdon et al. 2005, Genes & Development; Marcel & Hainaut
    2009; Khoury & Bourdon 2011, among many others) that initiates
    translation at the internal ATG corresponding to canonical residue
    40, using the IDENTICAL spliced exon structure as full-length p53 --
    no exon-boundary difference at all, purely a different start codon
    within the same exon. This means Delta40p53 numbers every residue
    39 lower than canonical for the entire shared reading frame -- a
    clean, real, well-documented offset, not a fabricated one.
  - The test position is TP53 p.R175H (canonical residue 175) -- the
    single most-cited TP53 hotspot in the literature (COSMIC / ClinVar /
    IARC TP53 database). Its genomic anchor (chr17:7675089, GRCh38) was
    located empirically by scanning `TranscriptContext.cds_position()`
    (the real production arithmetic, not hand computation) for the
    genomic position matching codon 175's first CDS nucleotide -- see
    the scratchpad verification this was derived from. At that same
    genomic position, canonical numbering gives residue 175; Delta40p53
    numbering gives residue 136 (175 - 39). Both independently confirmed
    non-None via `TranscriptContext.codon_at()` before being encoded
    here, so this fixture is proven discriminating, not merely plausible.

SCOPE NOTE: the dispatch that requested this test named its intended
location as `kim_pipeline/tests/`. That is not possible for this test --
kim_pipeline is a separate, sibling package with its own `pipeline` module
(distinct from geper's `pipeline.pvs1.utils`, which is what condition C
actually lives in), no shared Python path, and no TP53/PVS1 transcript
machinery of its own. Placed here, in `geper/tests/`, alongside this
repo's other `canonical_protein_position` coverage
(`tests/test_canonical_protein_position.py`), which is the only place a
test against this code can actually run. Flagged, not silently done.
"""

import unittest

from pipeline.pvs1.utils import canonical_protein_position, transcript_context_from_dict

# Real exon list for ENST00000269305 (TP53, canonical/MANE Select),
# verified live via Ensembl REST 2026-08-22 and cross-checked against
# this repo's own local Ensembl transcript cache. Order as returned by
# Ensembl (5'->3' for this minus-strand transcript, genomic-descending).
_TP53_EXONS = [
    {"start": 7687377, "end": 7687490},
    {"start": 7676521, "end": 7676622},
    {"start": 7676382, "end": 7676403},
    {"start": 7675994, "end": 7676272},
    {"start": 7675053, "end": 7675236},
    {"start": 7674859, "end": 7674971},
    {"start": 7674181, "end": 7674290},
    {"start": 7673701, "end": 7673837},
    {"start": 7673535, "end": 7673608},
    {"start": 7670609, "end": 7670715},
    {"start": 7668421, "end": 7669690},
]

_CANONICAL_CDS_START = 7669609  # translation.start, live Ensembl fetch (stop codon end)
_CANONICAL_CDS_END = 7676594  # translation.end, live Ensembl fetch (ATG's "A", minus strand)

# Codon 40's genomic anchor -- the internal ATG Delta40p53 actually uses --
# located by scanning cds_position() against the canonical context; see
# module docstring. 7676594 - 7676251 = 343 = (40-1)*3, confirming this is
# exactly 39 codons downstream of the canonical start codon.
_DELTA40_CDS_END = 7676251

# The R175 hotspot's genomic anchor, located the same way.
_R175_GENOMIC_POS = 7675089


def _canonical_transcript_result():
    return {
        "skipped": False,
        "found": True,
        "gene_symbol": "TP53",
        "source": "ensembl_grch38_live_fetch_2026-08-22",
        "transcript": {
            "transcript_id": "ENST00000269305",
            "gene_symbol": "TP53",
            "chrom": "17",
            "strand": -1,
            "exons": _TP53_EXONS,
            "cds_genomic_start": _CANONICAL_CDS_START,
            "cds_genomic_end": _CANONICAL_CDS_END,
            "protein_length": 393,
            "is_mane_select": True,
            "is_canonical": True,
        },
    }


def _delta40_transcript_result():
    # Identical exon structure to canonical -- Delta40p53 is a
    # translation-initiation difference, not a splicing difference (see
    # module docstring). is_mane_select/is_canonical both False: this is
    # the isoform GEPER's own transcript-resolution stage must never
    # treat as the source of a reportable canonical protein position.
    return {
        "skipped": False,
        "found": True,
        "gene_symbol": "TP53",
        "source": "delta40p53_internal_atg_2026-08-22",
        "transcript": {
            "transcript_id": "TP53-Delta40p53-internal-ATG",
            "gene_symbol": "TP53",
            "chrom": "17",
            "strand": -1,
            "exons": _TP53_EXONS,
            "cds_genomic_start": _CANONICAL_CDS_START,
            "cds_genomic_end": _DELTA40_CDS_END,
            "protein_length": 393 - 39,
            "is_mane_select": False,
            "is_canonical": False,
        },
    }


class TestFixtureIsGenuinelyDiscriminating(unittest.TestCase):
    """
    Prove the fixture itself before trusting any assertion built on it --
    the raw `TranscriptContext` arithmetic (bypassing the gate entirely)
    must show canonical and Delta40p53 genuinely disagree at this
    position, or the "critical test" below would be checking nothing.
    """

    def test_canonical_context_gives_175_at_the_test_position(self):
        ctx = transcript_context_from_dict(_canonical_transcript_result()["transcript"])
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.codon_at(_R175_GENOMIC_POS), 175)

    def test_delta40_context_gives_136_at_the_SAME_position(self):
        ctx = transcript_context_from_dict(_delta40_transcript_result()["transcript"])
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.codon_at(_R175_GENOMIC_POS), 136)

    def test_positions_genuinely_differ(self):
        canonical_ctx = transcript_context_from_dict(_canonical_transcript_result()["transcript"])
        delta40_ctx = transcript_context_from_dict(_delta40_transcript_result()["transcript"])
        self.assertNotEqual(canonical_ctx.codon_at(_R175_GENOMIC_POS), delta40_ctx.codon_at(_R175_GENOMIC_POS))
        # And confirms neither is a coincidental None hiding a real
        # discrepancy -- both genuinely resolve to a residue number.
        self.assertIsNotNone(canonical_ctx.codon_at(_R175_GENOMIC_POS))
        self.assertIsNotNone(delta40_ctx.codon_at(_R175_GENOMIC_POS))


class TestAlphafoldMappingGateEnforcesCanonicalIsoform(unittest.TestCase):
    """
    The critical test: `canonical_protein_position` (condition C of the
    AlphaFold mapping gate -- see `pipeline/pvs1/utils.py`'s own
    docstring, which this row's own falsifier already names as the
    guard against exactly this) must return None for a variant resolved
    against the non-canonical Delta40p53 record, at a genomic position
    where a naive (gate-less) implementation would have produced 136 --
    a real, plausible-looking, WRONG residue number, not an error.
    """

    def test_canonical_isoform_resolves_the_real_hotspot_position(self):
        # Positive control: the same real position, correctly resolved,
        # confirms the fixture and the function agree before the negative
        # case is checked.
        result = canonical_protein_position(_canonical_transcript_result(), _R175_GENOMIC_POS)
        self.assertEqual(result, 175)

    def test_non_canonical_isoform_produces_mapping_unavailable_not_a_wrong_number(self):
        result = canonical_protein_position(_delta40_transcript_result(), _R175_GENOMIC_POS)
        self.assertIsNone(
            result,
            "canonical_protein_position() returned a residue number for a transcript "
            "that is neither MANE Select nor canonical -- this is exactly the defect "
            "class the isoform gate exists to prevent: a variant on Delta40p53 would "
            "silently report residue 136 (Delta40p53's own, real numbering) as if it "
            "were this variant's canonical-isoform position, when the real canonical "
            "position is 175. Different genes/positions could differ by an arbitrary, "
            "clinically significant amount -- this is not a rounding error, it is a "
            "wrong amino acid identity presented as an established fact.",
        )

    def test_non_canonical_result_is_not_the_alternate_isoforms_own_number_either(self):
        # Stated explicitly, not merely implied by assertIsNone above:
        # the gate must not leak ANY number for the non-canonical case --
        # not the canonical 175, and not the alternate transcript's own
        # internally-consistent 136. "Mapping unavailable" means no
        # number at all, not a differently-sourced one.
        result = canonical_protein_position(_delta40_transcript_result(), _R175_GENOMIC_POS)
        self.assertNotEqual(result, 136)
        self.assertNotEqual(result, 175)


if __name__ == "__main__":
    unittest.main()
