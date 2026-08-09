"""
Tests for the ACMG/AMP PVS1 rule (`pipeline/pvs1/`, wired into
`pipeline/acmg_rules.py::ACMGRuleEngine._pvs1`).

Ground truth, not synthetic guesses. Every variant exercised here is a
real, well-documented ClinVar record; every transcript structure is the
real Ensembl MANE Select structure for the gene (frozen in
`tests/fixtures/pvs1_transcripts.json`); every ClinGen dosage score is
the real published value from ClinGen's `ClinGen_gene_curation_list`
download; and every gnomAD frequency is the real gnomAD v4 value. Each
case below records its ClinVar accession-level classification and the
review status behind it, so a reviewer can re-derive the expectation
from the primary sources rather than from this file.

The expectations are *PVS1 outcomes*, not ClinVar classifications --
PVS1 is one criterion, and a variant can be Pathogenic in ClinVar on
other evidence while PVS1 itself applies at reduced strength or not at
all. Where those two diverge, the case comments say why.
"""

import json
import os
import unittest

from pipeline.acmg_rules import ACMGRuleEngine
from pipeline.pvs1.decision_tree import PVS1DecisionTree
from pipeline.pvs1.models import (
    LOF_ESTABLISHED,
    LOF_ESTABLISHED_RECESSIVE,
    LOF_NOT_ESTABLISHED,
    LOF_UNKNOWN,
    NULL_CANONICAL_SPLICE,
    NULL_FRAMESHIFT,
    NULL_INITIATION_CODON,
    NULL_NONSENSE,
    STRENGTH_MODERATE,
    STRENGTH_NOT_APPLICABLE,
    STRENGTH_STRONG,
    STRENGTH_SUPPORTING,
    STRENGTH_VERY_STRONG,
)
from pipeline.pvs1.utils import (
    CONSEQUENCE_MISSENSE,
    CONSEQUENCE_NONSENSE,
    build_pvs1_input,
    classify_null_variant,
    coding_consequence,
    lof_mechanism_from_clingen,
    transcript_context_from_dict,
)

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "pvs1_transcripts.json")

with open(_FIXTURE, "r", encoding="utf-8") as _fh:
    _TRANSCRIPTS = json.load(_fh)["transcripts"]


# ---------------------------------------------------------------------------
# Fixture builders -- each returns the exact dict shape the corresponding
# GEPER provider stage returns, so the tests exercise the real adapters.
# ---------------------------------------------------------------------------


def transcript_result(gene: str):
    """The dict shape `pipeline/pvs1/lookup.py::TranscriptLookup` returns."""
    record = dict(_TRANSCRIPTS[gene])
    return {"skipped": False, "found": True, "gene_symbol": gene, "source": "fixture", "transcript": record}


def transcript(gene: str):
    return transcript_context_from_dict(_TRANSCRIPTS[gene])


# Real ClinGen dosage-sensitivity curations, from ClinGen's published
# ClinGen_gene_curation_list_GRCh38.tsv download (verified against the
# live file). These drive PVS1's "is LOF a known disease mechanism?"
# precondition.
_CLINGEN_DOSAGE = {
    "BRCA1": (3, "Sufficient evidence for dosage pathogenicity"),
    "BRCA2": (3, "Sufficient evidence for dosage pathogenicity"),
    "CFTR": (30, "Gene associated with autosomal recessive phenotype"),
    "MYH7": (0, "No evidence available"),
}


def clingen_result(gene: str, validity: str = "Definitive"):
    score, label = _CLINGEN_DOSAGE[gene]
    return {
        "skipped": False,
        "found": True,
        "source": "fixture",
        "gene_symbol": gene,
        "clinical_validity_summary": validity,
        "dosage_sensitivity": {
            "gene_symbol": gene,
            "haploinsufficiency_score": score,
            "haploinsufficiency_label": label,
        },
    }


def gnomad_result(af=None):
    """gnomAD stage dict. `af=None` means the variant is absent from gnomAD."""
    if af is None:
        return {"skipped": False, "found": False}
    return {"skipped": False, "found": True, "global_af": af, "population_breakdown": {}}


def nonsense_protein_result():
    """
    Protein-translation stage output for a variant that introduces a
    premature stop. Stands in for `pipeline/protein_translator.py`'s
    window translation; the codon that actually drives the rule is
    derived from the real transcript coordinates, not from this stub.
    """
    return {"skipped": False, "translation": {"ref_protein": "MAAAQ", "alt_protein": "MAA*"}}


def missense_protein_result():
    return {"skipped": False, "translation": {"ref_protein": "MAAAQ", "alt_protein": "MAAAH"}}


def variant(chrom, pos, ref, alt):
    return {"chrom": chrom, "pos": pos, "ref": ref, "alt": alt}


def evaluate(gene, variant_dict, protein=None, gnomad=None, interpro=None, clingen=None):
    """Run the full adapter + decision tree, exactly as `_pvs1` does."""
    return PVS1DecisionTree().evaluate(
        build_pvs1_input(
            variant_dict=variant_dict,
            protein_result=protein,
            clingen_result=clingen if clingen is not None else clingen_result(gene),
            gnomad_result=gnomad,
            interpro_result=interpro,
            transcript_result=transcript_result(gene),
        )
    )


# ---------------------------------------------------------------------------
# Coordinate mapping -- the arithmetic every caveat below depends on
# ---------------------------------------------------------------------------


class TestTranscriptCoordinateMapping(unittest.TestCase):
    """
    Validates the genomic -> CDS mapping against the `c.` positions real
    ClinVar records report, on both strands. If this drifts, every
    location caveat below is silently wrong, so it is asserted directly
    rather than only implied by the rule outcomes.
    """

    def test_maps_real_clinvar_positions_to_their_hgvs_cds_coordinates(self):
        cases = [
            # (gene, GRCh38 position, expected c. position, ClinVar record)
            ("BRCA1", 43093844, 1687, "NM_007294.4(BRCA1):c.1687C>T (p.Gln563Ter)"),
            ("BRCA1", 43049192, 5335, "NM_007294.4(BRCA1):c.5335C>T (p.Gln1779Ter)"),
            ("BRCA1", 43124096, 1, "NM_007294.4(BRCA1):c.1A>G (p.Met1Val)"),
            ("BRCA2", 32398489, 9976, "NM_000059.4(BRCA2):c.9976A>T (p.Lys3326Ter)"),
            ("BRCA2", 32398607, 10094, "NM_000059.4(BRCA2):c.10094_10095insGAATTATATC"),
            ("CFTR", 117587739, 1585, "NM_000492.4(CFTR):c.1585-1G>A (acceptor of c.1585)"),
        ]
        for gene, pos, expected_cds, label in cases:
            with self.subTest(label):
                self.assertEqual(transcript(gene).cds_position(pos), expected_cds)

    def test_transcript_structures_match_their_known_protein_lengths(self):
        for gene, expected_aa in (("BRCA1", 1863), ("BRCA2", 3418), ("MYH7", 1935), ("CFTR", 1480)):
            with self.subTest(gene):
                self.assertEqual(transcript(gene).total_codons, expected_aa)

    def test_nmd_boundary_uses_last_exon_plus_50bp_of_the_penultimate_exon(self):
        # BRCA1: 22 coding exons, 5592 bp CDS, last coding exon 125 bp,
        # penultimate 61 bp -> cutoff = (5592 - 125) - 50 = 5417.
        t = transcript("BRCA1")
        self.assertEqual(t.coding_exon_sizes()[-1], 125)
        self.assertEqual(t.coding_exon_sizes()[-2], 61)
        self.assertEqual(t.nmd_cutoff_cds(), 5417)

    def test_penultimate_window_is_capped_by_a_short_penultimate_exon(self):
        # MYH7's last coding exon is 18 bp and its penultimate is 135 bp;
        # the min(50, penultimate) cap keeps the boundary inside the
        # penultimate exon instead of spilling into the one before it.
        t = transcript("MYH7")
        sizes = t.coding_exon_sizes()
        self.assertEqual(t.nmd_cutoff_cds(), sum(sizes[:-1]) - min(50, sizes[-2]))

    def test_canonical_splice_sites_are_recognised_on_both_strands(self):
        # BRCA1 is on the minus strand: the donor of the exon ending at
        # c.5074 sits at the *lower* genomic coordinate.
        self.assertEqual(transcript("BRCA1").splice_site_at(43067607)[1], "donor")
        # CFTR is on the plus strand.
        self.assertEqual(transcript("CFTR").splice_site_at(117587738)[1], "acceptor")

    def test_position_inside_an_exon_is_not_a_splice_site(self):
        self.assertIsNone(transcript("BRCA1").splice_site_at(43093844))


# ---------------------------------------------------------------------------
# The known-variant set
# ---------------------------------------------------------------------------


class TestKnownClinVarVariants(unittest.TestCase):
    """One test per real ClinVar variant, with the expected PVS1 outcome."""

    # -- required case 1: clear-cut nonsense early in the gene ---------

    def test_brca1_early_nonsense_triggers_full_pvs1(self):
        """
        NM_007294.4(BRCA1):c.1687C>T (p.Gln563Ter), chr17:43093844 G>A.
        ClinVar: Pathogenic, reviewed by expert panel (ENIGMA).
        Codon 563 of 1863 -- far 5' of the NMD boundary (codon 1805) --
        in a gene ClinGen curates as haploinsufficient (score 3).
        Expect PVS1 at full Very Strong strength, SVI leaf NF1.
        """
        result = evaluate(
            "BRCA1",
            variant("17", 43093844, "G", "A"),
            protein=nonsense_protein_result(),
            gnomad=gnomad_result(9.58e-06),
        )
        self.assertTrue(result.applies)
        self.assertEqual(result.strength, STRENGTH_VERY_STRONG)
        self.assertEqual(result.criterion_code, "NF1")
        self.assertEqual(result.null_variant_type, NULL_NONSENSE)
        self.assertEqual(result.termination_codon, 563)
        self.assertIs(result.nmd_predicted, True)
        self.assertEqual(result.lof_mechanism, LOF_ESTABLISHED)

    def test_brca1_nonsense_just_inside_the_nmd_boundary_still_triggers_full_pvs1(self):
        """
        NM_007294.4(BRCA1):c.5335C>T (p.Gln1779Ter), chr17:43049192 G>A.
        ClinVar: Pathogenic, reviewed by expert panel.
        A deliberate near-boundary case: codon 1779 (CDS nt 5337) sits
        just 5' of the NMD cutoff at CDS nt 5417, so NMD is still
        predicted and PVS1 stays Very Strong. Guards against an
        off-by-one in the last-exon/50 bp arithmetic, which would flip
        this expert-panel Pathogenic variant to Moderate.
        """
        result = evaluate("BRCA1", variant("17", 43049192, "G", "A"), protein=nonsense_protein_result())
        self.assertTrue(result.applies)
        self.assertEqual(result.strength, STRENGTH_VERY_STRONG)
        self.assertEqual(result.termination_codon, 1779)

    # -- required case 2: frameshift in the last exon ------------------

    def test_brca2_last_exon_frameshift_does_not_trigger_full_pvs1(self):
        """
        NM_000059.4(BRCA2):c.10094_10095insGAATTATATC (p.Ser3366fs),
        chr13:32398607 C>CGAATTATATC.
        ClinVar: Benign/Likely benign (multiple submitters, no conflicts).

        The last-exon caveat in full: the frameshift lands in BRCA2's
        3'-most exon, so the transcript escapes NMD; the truncation
        removes only ~1.6% of the 3418-aa protein, well under the SVI's
        10% threshold. PVS1 must NOT apply at Very Strong -- the tree
        terminates at NF6 (Moderate). BRCA2 itself is haploinsufficient
        (ClinGen score 3), so this is the location caveat firing, not the
        gene-level gate.
        """
        result = evaluate("BRCA2", variant("13", 32398607, "C", "CGAATTATATC"))
        self.assertEqual(result.null_variant_type, NULL_FRAMESHIFT)
        self.assertNotEqual(result.strength, STRENGTH_VERY_STRONG)
        self.assertEqual(result.strength, STRENGTH_MODERATE)
        self.assertEqual(result.criterion_code, "NF6")
        self.assertIs(result.nmd_predicted, False)
        self.assertTrue(
            any("last) exon" in c or "NMD is NOT predicted" in c for c in result.caveats_checked),
            result.caveats_checked,
        )

    def test_brca2_common_last_exon_nonsense_does_not_trigger_pvs1_at_all(self):
        """
        NM_000059.4(BRCA2):c.9976A>T (p.Lys3326Ter), chr13:32398489 A>T.
        ClinVar: Benign, reviewed by expert panel. gnomAD v4 exome AF
        0.0081 (real value).

        Two caveats stack: NMD escape in the last exon (codon 3326 of
        3418, CDS nt 9978 vs the 9598 boundary), and a null allele far
        too common to carry PVS1-strength evidence. Expect PVS1 withheld
        entirely.
        """
        result = evaluate(
            "BRCA2",
            variant("13", 32398489, "A", "T"),
            protein=nonsense_protein_result(),
            gnomad=gnomad_result(0.008115),
        )
        self.assertFalse(result.applies)
        self.assertEqual(result.strength, STRENGTH_NOT_APPLICABLE)
        self.assertEqual(result.criterion_code, "FREQ")
        self.assertIs(result.nmd_predicted, False)
        # Without the frequency evidence the same variant reaches the
        # NMD-escape leaf: the frequency gate is what removes it.
        without_af = evaluate("BRCA2", variant("13", 32398489, "A", "T"), protein=nonsense_protein_result())
        self.assertEqual(without_af.strength, STRENGTH_MODERATE)

    # -- required case 3: splice site in a gene with established LOF ---

    def test_brca1_canonical_donor_triggers_full_pvs1(self):
        """
        NM_007294.4(BRCA1):c.5074+1G>A, chr17:43067607 C>T.
        ClinVar: Pathogenic, reviewed by expert panel.
        Loss of the donor of an 88-bp coding exon shifts the reading
        frame; NMD is predicted on the post-skip transcript with a wide
        margin. Expect Very Strong, SVI leaf SS1.
        """
        result = evaluate("BRCA1", variant("17", 43067607, "C", "T"), gnomad=gnomad_result(None))
        self.assertTrue(result.applies)
        self.assertEqual(result.strength, STRENGTH_VERY_STRONG)
        self.assertEqual(result.criterion_code, "SS1")
        self.assertEqual(result.null_variant_type, NULL_CANONICAL_SPLICE)
        self.assertIs(result.nmd_predicted, True)

    def test_cftr_canonical_acceptor_triggers_pvs1_in_a_recessive_gene(self):
        """
        NM_000492.4(CFTR):c.1585-1G>A, chr7:117587738 G>A.
        ClinVar: Pathogenic, practice guideline (CFTR2).

        ClinGen curates CFTR's haploinsufficiency score as 30 ("gene
        associated with autosomal recessive phenotype") -- that score
        means haploinsufficiency is not the disease model, NOT that loss
        of function is not the mechanism. A null allele in CFTR must
        still qualify for PVS1, so this case guards the recessive branch
        of the mechanism gate.
        """
        result = evaluate("CFTR", variant("7", 117587738, "G", "A"))
        self.assertTrue(result.applies)
        self.assertEqual(result.strength, STRENGTH_VERY_STRONG)
        self.assertEqual(result.criterion_code, "SS1")
        self.assertEqual(result.lof_mechanism, LOF_ESTABLISHED_RECESSIVE)

    # -- gene-level mechanism caveat -----------------------------------

    def test_myh7_early_nonsense_is_withheld_because_lof_is_not_the_mechanism(self):
        """
        NM_000257.4(MYH7):c.56C>G (p.Ser19Ter), chr14:23433677 G>C.
        ClinVar: Uncertain significance.

        Structurally this is the textbook PVS1 variant -- an early
        nonsense, deep inside the NMD zone. It must still be withheld:
        ClinGen curates MYH7's haploinsufficiency score as 0 ("no
        evidence available"), and MYH7 disease is driven by
        dominant-negative missense variants, not haploinsufficiency.
        This is the caveat that separates PVS1 from a variant-type
        lookup, and it is why ClinVar leaves the variant at VUS.
        """
        result = evaluate("MYH7", variant("14", 23433677, "G", "C"), protein=nonsense_protein_result())
        self.assertFalse(result.applies)
        self.assertEqual(result.strength, STRENGTH_NOT_APPLICABLE)
        self.assertEqual(result.lof_mechanism, LOF_NOT_ESTABLISHED)
        # The provisional strength is still reported, so a reviewer sees
        # that only the gene-level gate held it back.
        self.assertEqual(result.provisional_strength, STRENGTH_VERY_STRONG)
        self.assertIn("does not establish loss of function as a disease mechanism", result.rationale.lower())
        self.assertTrue(
            any("No evidence available" in line for line in result.supporting_evidence),
            result.supporting_evidence,
        )

    # -- initiation codon caveat ---------------------------------------

    def test_brca1_start_loss_is_capped_at_supporting(self):
        """
        NM_007294.4(BRCA1):c.1A>G (p.Met1Val), chr17:43124096 T>C.
        ClinVar: Pathogenic/Likely pathogenic (multiple submitters).

        The SVI recommendation forbids PVS1 and PVS1_Strong for
        initiation-codon variants because translation can reinitiate at a
        downstream in-frame ATG. With no catalogue of pathogenic variants
        upstream of the closest potential in-frame start codon
        integrated, the conservative leaf (IC4, Supporting) is taken and
        the missing evidence is reported as a gap. ClinVar's Pathogenic
        call rests on other criteria, not on PVS1 alone.
        """
        result = evaluate("BRCA1", variant("17", 43124096, "T", "C"))
        self.assertTrue(result.applies)
        self.assertEqual(result.null_variant_type, NULL_INITIATION_CODON)
        self.assertEqual(result.strength, STRENGTH_SUPPORTING)
        self.assertEqual(result.criterion_code, "IC4")
        self.assertTrue(any("reinitiate" in c for c in result.caveats_checked))

    # -- negative control: not a null variant --------------------------

    def test_missense_variant_is_not_a_pvs1_null_class(self):
        """
        NM_007294.4(BRCA1):c.2020C>T (p.Pro674Ser), chr17:43093511 G>A.
        ClinVar: Conflicting classifications of pathogenicity.
        A missense variant is outside PVS1's scope entirely.
        """
        result = evaluate("BRCA1", variant("17", 43093511, "G", "A"), protein=missense_protein_result())
        self.assertFalse(result.applies)
        self.assertIsNone(result.null_variant_type)
        self.assertEqual(result.criterion_code, "NF0")


# ---------------------------------------------------------------------------
# Individual decision-tree caveats
# ---------------------------------------------------------------------------


class TestDecisionTreeCaveats(unittest.TestCase):
    def test_nmd_escaping_truncation_in_a_critical_region_is_strong_not_moderate(self):
        """
        NM_007294.4(BRCA1):c.5467+1G>A, chr17:43047642 C>T (ClinVar:
        Pathogenic). Skipping the 61-bp penultimate coding exon shifts
        the frame, but the new stop falls in the last exon, so the
        transcript escapes NMD and only ~3% of the protein is lost.
        Without domain evidence the tree stops at Moderate; supplying
        BRCA1's BRCT domains (InterPro/Pfam PF00533, ~aa 1646-1736 and
        1760-1855) moves it to the critical-region leaf, PVS1_Strong.
        This is the branch that lets an expert panel reach Pathogenic on
        a terminal-exon splice variant.
        """
        interpro = {
            "skipped": False,
            "found": True,
            "domains": [
                {"start": 1646, "end": 1736, "type": "domain", "name": "BRCT domain", "member_accession": "PF00533"},
                {"start": 1760, "end": 1855, "type": "domain", "name": "BRCT domain", "member_accession": "PF00533"},
            ],
        }
        no_domain = evaluate("BRCA1", variant("17", 43047642, "C", "T"))
        self.assertIs(no_domain.nmd_predicted, False)
        self.assertEqual(no_domain.strength, STRENGTH_MODERATE)
        self.assertEqual(no_domain.criterion_code, "SS6")

        with_domain = evaluate("BRCA1", variant("17", 43047642, "C", "T"), interpro=interpro)
        self.assertEqual(with_domain.strength, STRENGTH_STRONG)
        self.assertEqual(with_domain.criterion_code, "SS3")

    def test_post_skip_nmd_is_judged_on_the_post_skip_junctions(self):
        """
        The same c.5467+1G>A variant, asserted at the arithmetic level:
        against the *reference* junctions the frameshift start (CDS nt
        5406) falls inside the NMD zone (cutoff 5417), but once the
        skipped exon is removed the boundary moves to 5356 and the
        variant correctly escapes NMD. Evaluating against the reference
        junctions is the classic way to get terminal-exon splice
        variants wrong.
        """
        t = transcript("BRCA1")
        rank = t.splice_site_at(43047642)[0]
        info = t.nmd_after_exon_skip(rank)
        self.assertFalse(info["nmd_predicted"])
        self.assertLess(info["cutoff_cds"], t.nmd_cutoff_cds())

    def test_in_frame_exon_skip_critical_region_is_bounded_to_the_skipped_exon(self):
        """
        NM_007294.4(BRCA1):c.135-2A>T-equivalent, chr17:43106534 C>A --
        the canonical acceptor +1 of exon 4 (ClinVar: Pathogenic,
        reviewed by expert panel). Skipping exon 4 in-frame removes only
        codons 45-71 (report review round 3, D3): the SVI "in-frame
        deleted region critical to protein function?" node must be
        answered against THAT range, not against codons 45-(end of
        protein), which is what the pre-fix code did.

        Two real BRCA1 domain layouts distinguish "the fix changed the
        answer" from "the fix changed the mechanism but the real answer
        is unchanged":

        1. BRCA1's real N-terminal RING domain (UniProt P38398, "Zinc
           finger, RING-type", aa 24-64) genuinely overlaps codons
           45-71 -- so with BRCA1's real domain annotation, PVS1_Strong
           (SS10) is still correct after the fix, matching the live,
           InterPro-backed Colab run (GEPER-RUN-20260809T06513) exactly:
           no classification downgrade occurred. A prior version of
           this verification asserted "no overlap" using ONLY a distal
           domain (BRCT, aa ~1650-1855) and never checked against
           BRCA1's real N-terminal annotation -- that assertion was an
           artifact of an incomplete fixture, not of the fix itself.
        2. With only the distal BRCT domains supplied (a synthetic,
           not-biologically-complete domain set, kept here specifically
           to isolate the bug this fix addresses), codons 45-71 do NOT
           overlap 1646-1855 -- proving the fix actually stopped the
           range from being silently extended to the end of the
           protein (that synthetic case would have wrongly matched
           BRCT under the pre-fix code, which always used
           `transcript.total_codons` as the range's end).
        """
        v = variant("17", 43106534, "C", "A")
        interpro_real = {
            "skipped": False,
            "found": True,
            "domains": [
                {
                    "start": 24,
                    "end": 64,
                    "type": "domain",
                    "name": "Zinc finger, RING-type",
                    "member_accession": "PF00097",
                },
                {"start": 1646, "end": 1736, "type": "domain", "name": "BRCT domain", "member_accession": "PF00533"},
                {"start": 1760, "end": 1855, "type": "domain", "name": "BRCT domain", "member_accession": "PF00533"},
            ],
        }
        interpro_distal_only = {
            "skipped": False,
            "found": True,
            "domains": [
                {"start": 1646, "end": 1736, "type": "domain", "name": "BRCT domain", "member_accession": "PF00533"},
                {"start": 1760, "end": 1855, "type": "domain", "name": "BRCT domain", "member_accession": "PF00533"},
            ],
        }

        with_ring = evaluate("BRCA1", v, interpro=interpro_real)
        self.assertEqual(with_ring.strength, STRENGTH_STRONG)
        self.assertEqual(with_ring.criterion_code, "SS10")

        distal_only = evaluate("BRCA1", v, interpro=interpro_distal_only)
        self.assertEqual(distal_only.strength, STRENGTH_MODERATE)
        self.assertEqual(distal_only.criterion_code, "SS9")

    def test_exon_absent_from_the_biologically_relevant_transcript_blocks_pvs1(self):
        """The alternative-isoform caveat: an exon spliced out of the disease-relevant transcript disqualifies PVS1."""
        inp = build_pvs1_input(
            variant_dict=variant("17", 43093844, "G", "A"),
            protein_result=nonsense_protein_result(),
            clingen_result=clingen_result("BRCA1"),
            transcript_result=transcript_result("BRCA1"),
        )
        inp.exon_biologically_relevant = False
        result = PVS1DecisionTree().evaluate(inp)
        self.assertFalse(result.applies)
        self.assertEqual(result.criterion_code, "NF2")

    def test_single_coding_exon_transcript_always_escapes_nmd(self):
        """NMD needs a downstream exon-exon junction; a single-coding-exon gene has none."""
        t = transcript("BRCA1")
        single = transcript_context_from_dict(
            {
                "transcript_id": "SINGLE",
                "gene_symbol": "TEST",
                "chrom": t.chrom,
                "strand": 1,
                "cds_genomic_start": 1000,
                "cds_genomic_end": 1999,
                "protein_length": 333,
                "exons": [{"start": 1000, "end": 1999}],
            }
        )
        self.assertIsNone(single.nmd_cutoff_cds())
        self.assertFalse(single.is_nmd_predicted(10))

    def test_unknown_gene_mechanism_is_reported_as_unknown_not_as_a_negative(self):
        """No ClinGen curation must not masquerade as a curated 'LOF is not the mechanism'."""
        mechanism, evidence = lof_mechanism_from_clingen({"skipped": True, "found": False})
        self.assertEqual(mechanism, LOF_UNKNOWN)
        self.assertEqual(evidence, [])

        result = evaluate(
            "BRCA1",
            variant("17", 43093844, "G", "A"),
            protein=nonsense_protein_result(),
            clingen={"skipped": True, "found": False},
        )
        self.assertFalse(result.applies)
        self.assertEqual(result.lof_mechanism, LOF_UNKNOWN)
        self.assertEqual(result.provisional_strength, STRENGTH_VERY_STRONG)

    def test_missing_transcript_structure_withholds_pvs1_rather_than_assuming_full_strength(self):
        """Without transcript structure the last-exon caveat cannot be checked, so PVS1 is not applied."""
        result = PVS1DecisionTree().evaluate(
            build_pvs1_input(
                variant_dict=variant("17", 43093844, "G", "A"),
                protein_result=nonsense_protein_result(),
                clingen_result=clingen_result("BRCA1"),
                transcript_result={"skipped": False, "found": False},
            )
        )
        self.assertFalse(result.applies)
        self.assertTrue(any("transcript structure unavailable" in c for c in result.unchecked_caveats))

    def test_dosage_unlikely_curation_blocks_pvs1(self):
        """ClinGen score 40 ('dosage sensitivity unlikely') is a curated negative for the LOF mechanism."""
        refuted = clingen_result("BRCA1")
        refuted["dosage_sensitivity"]["haploinsufficiency_score"] = 40
        refuted["dosage_sensitivity"]["haploinsufficiency_label"] = "Dosage sensitivity unlikely"
        result = evaluate(
            "BRCA1", variant("17", 43093844, "G", "A"), protein=nonsense_protein_result(), clingen=refuted
        )
        self.assertFalse(result.applies)
        self.assertIn("dosage sensitivity unlikely", result.rationale.lower())

    def test_unanswerable_nodes_are_reported_as_gaps_not_guessed(self):
        """Every node GEPER cannot answer must surface in `unchecked_caveats`."""
        result = evaluate("BRCA2", variant("13", 32398607, "C", "CGAATTATATC"))
        joined = " ".join(result.unchecked_caveats)
        self.assertIn("Critical-region check", joined)
        self.assertIn("Role of region in disease", joined)

    def test_in_frame_indel_is_not_a_null_variant(self):
        null_type, _ = classify_null_variant(variant("13", 32398607, "C", "CGAATTA"), None, transcript("BRCA2"))
        self.assertIsNone(null_type)


# ---------------------------------------------------------------------------
# Wiring into the ACMG rule engine
# ---------------------------------------------------------------------------


class TestRuleEngineIntegration(unittest.TestCase):
    def test_engine_reports_pvs1_triggered_at_very_strong(self):
        result = ACMGRuleEngine().evaluate(
            variant_dict=variant("17", 43093844, "G", "A"),
            protein_result=nonsense_protein_result(),
            clingen_result=clingen_result("BRCA1"),
            transcript_result=transcript_result("BRCA1"),
        )
        pvs1 = result["all_criteria"]["PVS1"]
        self.assertEqual(pvs1["status"], "triggered")
        self.assertEqual(pvs1["strength"], "very_strong")
        self.assertEqual(pvs1["details"]["criterion_code"], "NF1")
        self.assertIn(
            "PVS1 triggered (very_strong, pathogenic) contributes 8 point(s).", result["combining_rule_trace"]
        )

    def test_downgraded_pvs1_contributes_fewer_points_to_the_combining_rules(self):
        result = ACMGRuleEngine().evaluate(
            variant_dict=variant("13", 32398607, "C", "CGAATTATATC"),
            clingen_result=clingen_result("BRCA2"),
            transcript_result=transcript_result("BRCA2"),
        )
        pvs1 = result["all_criteria"]["PVS1"]
        self.assertEqual(pvs1["status"], "triggered")
        self.assertEqual(pvs1["strength"], "moderate")
        self.assertIn("PVS1 triggered (moderate, pathogenic) contributes 2 point(s).", result["combining_rule_trace"])

    def test_uncurated_gene_is_not_evaluated_rather_than_not_triggered(self):
        result = ACMGRuleEngine().evaluate(
            variant_dict=variant("17", 43093844, "G", "A"),
            protein_result=nonsense_protein_result(),
            transcript_result=transcript_result("BRCA1"),
        )
        self.assertEqual(result["all_criteria"]["PVS1"]["status"], "not_evaluated")

    def test_curated_negative_gene_is_not_triggered_rather_than_not_evaluated(self):
        result = ACMGRuleEngine().evaluate(
            variant_dict=variant("14", 23433677, "G", "C"),
            protein_result=nonsense_protein_result(),
            clingen_result=clingen_result("MYH7"),
            transcript_result=transcript_result("MYH7"),
        )
        self.assertEqual(result["all_criteria"]["PVS1"]["status"], "not_triggered")

    def test_engine_still_works_for_callers_that_pass_no_pvs1_inputs(self):
        """Backwards compatibility: existing callers pass neither variant_dict nor transcript_result."""
        result = ACMGRuleEngine().evaluate(protein_result=missense_protein_result())
        pvs1 = result["all_criteria"]["PVS1"]
        self.assertEqual(pvs1["status"], "not_triggered")
        self.assertEqual(pvs1["strength"], "very_strong")  # nominal strength when not triggered


class TestCodingConsequenceFromCDS(unittest.TestCase):
    """
    Substitution consequences must be called in the transcript's real
    reading frame.

    Regression coverage for a silent production false negative: GEPER's
    protein stage translates a short flanking window from the first ATG
    it finds, which is not the transcript's frame. For BRCA1 c.1687C>T it
    returns the identical out-of-frame peptide for ref and alt, so the
    classifier saw "no protein change" and PVS1 declined to fire on an
    expert-panel Pathogenic nonsense variant. Every expectation below is
    the amino-acid change the ClinVar record itself reports.
    """

    def test_calls_real_clinvar_amino_acid_changes_on_both_strands(self):
        cases = [
            ("BRCA1", 43093844, "G", "A", CONSEQUENCE_NONSENSE, "c.1687C>T p.Gln563Ter (minus strand)"),
            ("BRCA1", 43049192, "G", "A", CONSEQUENCE_NONSENSE, "c.5335C>T p.Gln1779Ter (minus strand)"),
            ("BRCA2", 32398489, "A", "T", CONSEQUENCE_NONSENSE, "c.9976A>T p.Lys3326Ter (plus strand)"),
            ("BRCA1", 43093511, "G", "A", CONSEQUENCE_MISSENSE, "c.2020C>T p.Pro674Ser (minus strand)"),
            ("MYH7", 23433677, "G", "C", CONSEQUENCE_NONSENSE, "c.56C>G p.Ser19Ter (minus strand)"),
        ]
        for gene, pos, ref, alt, expected, label in cases:
            with self.subTest(label):
                self.assertEqual(coding_consequence(transcript(gene), pos, ref, alt), expected)

    def test_nonsense_is_classified_without_any_protein_translation_input(self):
        """The CDS path must stand alone -- this is what the window translation could not do."""
        result = evaluate("BRCA1", variant("17", 43093844, "G", "A"), protein=None)
        self.assertEqual(result.null_variant_type, NULL_NONSENSE)
        self.assertEqual(result.strength, STRENGTH_VERY_STRONG)

    def test_missense_is_rejected_even_when_a_stub_translation_claims_a_stop(self):
        """A CDS-frame call outranks the frame-unaware window, not the other way round."""
        result = evaluate("BRCA1", variant("17", 43093511, "G", "A"), protein=nonsense_protein_result())
        self.assertIsNone(result.null_variant_type)
        self.assertFalse(result.applies)

    def test_reference_mismatch_yields_no_call_rather_than_a_guess(self):
        # REF says T, the BRCA1 CDS has C at that codon offset -- a
        # build/transcript mismatch must not be turned into a consequence.
        self.assertIsNone(coding_consequence(transcript("BRCA1"), 43093844, "T", "A"))

    def test_no_cds_sequence_falls_back_to_the_translation_window(self):
        record = dict(_TRANSCRIPTS["BRCA1"])
        record.pop("cds_sequence", None)
        self.assertIsNone(coding_consequence(transcript_context_from_dict(record), 43093844, "G", "A"))


class TestCriticalRegionEvidenceQuality(unittest.TestCase):
    """
    The "region critical to protein function" node must rest on entries
    that actually localise a function.

    Observed on live InterPro data: the only entries overlapping BRCA2's
    truncated tail (codons 3365-3418) are two whole-protein `family`
    entries spanning 1-3418 -- "this is BRCA2". Counting those promoted a
    ClinVar Benign/Likely benign last-exon frameshift from PVS1_Moderate
    to PVS1_Strong.
    """

    def _interpro(self, domains):
        return {"skipped": False, "found": True, "domains": domains}

    def test_whole_protein_family_entries_do_not_make_a_region_critical(self):
        interpro = self._interpro(
            [
                {"start": 1, "end": 3418, "type": "family", "name": "Breast cancer type 2 susceptibility protein"},
                {"start": 1, "end": 3418, "type": "family", "name": "DNA recombination repair protein, BRCA2 type"},
            ]
        )
        result = evaluate("BRCA2", variant("13", 32398607, "C", "CGAATTATATC"), interpro=interpro)
        self.assertEqual(result.strength, STRENGTH_MODERATE)
        self.assertEqual(result.criterion_code, "NF6")

    def test_a_localised_domain_in_the_truncated_region_still_counts(self):
        interpro = self._interpro(
            [
                {"start": 3380, "end": 3410, "type": "domain", "name": "BRCA2, oligonucleotide-binding domain"},
            ]
        )
        result = evaluate("BRCA2", variant("13", 32398607, "C", "CGAATTATATC"), interpro=interpro)
        self.assertEqual(result.strength, STRENGTH_STRONG)
        self.assertEqual(result.criterion_code, "NF3")

    def test_a_domain_outside_the_truncated_region_does_not_count(self):
        interpro = self._interpro(
            [
                {"start": 100, "end": 200, "type": "domain", "name": "an N-terminal domain"},
            ]
        )
        result = evaluate("BRCA2", variant("13", 32398607, "C", "CGAATTATATC"), interpro=interpro)
        self.assertEqual(result.strength, STRENGTH_MODERATE)


class TestTranscriptLookupCaching(unittest.TestCase):
    """
    Exercises `TranscriptLookup`'s own cache round trip with the network
    call stubbed out. The fixture-driven tests above hand the decision
    tree a pre-built transcript dict and so never touch this path -- a
    wrong cache-setter name here would have shipped an AttributeError
    that only fired on the second variant of a real run.
    """

    def test_second_lookup_of_the_same_gene_is_served_from_cache(self):
        from pipeline.pvs1.cache import TranscriptCache
        from pipeline.pvs1.lookup import TranscriptLookup

        lookup = TranscriptLookup(cache=TranscriptCache(max_size=8, ttl_seconds=60))
        calls = []

        def fake_fetch(gene_symbol, build):
            calls.append(gene_symbol)
            return {
                "skipped": False,
                "found": True,
                "gene_symbol": gene_symbol,
                "source": "ensembl_api",
                "transcript": dict(_TRANSCRIPTS[gene_symbol]),
            }

        lookup._fetch = fake_fetch
        first = lookup.query_gene("BRCA1")
        second = lookup.query_gene("BRCA1")

        self.assertTrue(first["found"])
        self.assertTrue(second["found"])
        self.assertEqual(second["source"], "cache")
        self.assertEqual(calls, ["BRCA1"], "the second lookup must not re-issue the Ensembl call")


if __name__ == "__main__":
    unittest.main()
