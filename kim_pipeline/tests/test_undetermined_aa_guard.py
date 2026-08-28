"""
tests/test_undetermined_aa_guard.py
────────────────────────────────────
Guard: an undetermined amino acid ("?", from `_translate`'s codon-table
fallback -- typically an "N" surviving into the reference FASTA at a CDS
position) must never enter PS1/PM5 evidence, and two "?" must never compare
equal for a synonymous determination.

Both guards were dispatched after execution against the real,
post-phase-fix (21a49cd) `FastaCodonContextProvider` and the real
`ClinVarLookup.check_same_codon_pathogenic` established two concrete
failure modes:

  SITE 1 (clinvar/lookup.py::check_same_codon_pathogenic): a bare
    truthiness check (`if wildtype_aa and mutant_aa:`) let a genuinely
    unknown "?" wildtype_aa make PM5 (novel_aa_at_known_pathogenic_codon)
    fire True against a real curated ClinVar hotspot (reproduced here with
    the same BRAF p.Val600Glu fixture already used by
    tests/test_all_fixes_regression.py's TestFix2PS1). PS1 (same_aa) was
    only accidentally safe, via `_aa3to1("?") == ""`.

  SITE 2 (annotation/codon_provider.py::_classify_snv_full): when an "N"
    sits elsewhere in the same codon (not at the variant's own position),
    it survives into BOTH ref_codon and mut_codon, so ref_aa == alt_aa ==
    "?" satisfied the classifier's first branch by plain string equality
    and reported "synonymous" -- which then fed BP7 (supporting, benign)
    for a protein change that was never actually determined. The
    discipline this guard encodes: an unknown resolving to a pathogenic-
    leaning call gets challenged by a reviewer; an unknown resolving to a
    benign one closes the case. Only one of those gets caught downstream,
    so the benign direction is the one that must not be reachable from
    "?" at all.
"""

from __future__ import annotations

import threading

from pipeline.annotation.codon_provider import CdsRecord, FastaCodonContextProvider, _FastaReader
from pipeline.clinvar.lookup import ClinVarHit, ClinVarLookup


def _make_provider(fasta_dict, cds_map):
    provider = FastaCodonContextProvider.__new__(FastaCodonContextProvider)
    provider._gff_path = "/fake.gff"
    provider._fasta_path = "/fake.fasta"
    provider._available = True
    reader = _FastaReader.__new__(_FastaReader)
    reader._path = "/fake.fasta"
    reader._has_samtools = False
    reader._in_memory = {k: v.upper() for k, v in fasta_dict.items()}
    provider._fasta = reader
    provider._cds_map = cds_map
    return provider


def _cds(chrom, start, end, strand, phase, tx):
    return CdsRecord(
        chrom=chrom, start=start, end=end, strand=strand, phase=phase, transcript_id=tx
    )


def _make_clinvar_lookup(by_coord):
    lkp = ClinVarLookup.__new__(ClinVarLookup)
    lkp._backend = "local"
    lkp._cache = {}
    lkp._cache_lock = threading.Lock()
    lkp._cache_hits = 0
    lkp._cache_misses = 0
    lkp._by_coord = by_coord
    return lkp


class TestSite2SynonymousGuard:
    """The N-elsewhere-in-codon case: ref_aa == alt_aa == '?' must not
    reach 'synonymous'."""

    def test_n_elsewhere_in_codon_does_not_reach_synonymous(self):
        """THE RED-FIRST CASE. Variant at codon_index=0 (pos 201), 'N' at
        codon_index=2 (pos 203) -- untouched by the ALT substitution, so it
        survives into both ref_codon and mut_codon. Before the guard this
        was ('synonymous', 'ATN', 'TTN', '?', '?')."""
        fasta = {"chr1": "N" * 200 + "ATN" + "N" * 20}
        cds_map = {"TX2": [_cds("chr1", 201, 203, "+", 0, "TX2")]}
        provider = _make_provider(fasta, cds_map)

        consequence, ref_codon, alt_codon, ref_aa, alt_aa = provider.get_codon_and_aa(
            "chr1", 201, "A", "T", "TX2"
        )
        assert ref_aa == "?" and alt_aa == "?", (
            "fixture regression: both amino acids should still be undetermined "
            f"(codon={ref_codon!r}/{alt_codon!r}) -- if this assertion fails, the "
            "case being guarded against no longer reproduces at all"
        )
        assert consequence != "synonymous", (
            f"an undetermined-vs-undetermined amino-acid pair was classified {consequence!r} "
            "and must never be 'synonymous' -- two unknowns are not evidence of no protein change"
        )
        # Falls through the elimination cascade to "missense" -- a separate,
        # already-reported, narrower defect (Pam's forced case) that this
        # guard does not attempt to fix; only "cannot be synonymous" is in
        # scope here.
        assert consequence == "missense"

    def test_n_at_the_variants_own_position_is_unaffected_by_this_guard(self):
        """Control: Pam's original case. The ALT substitution overwrites
        the 'N' at the variant's own position, so alt_aa resolves to a real
        amino acid and ref_aa == alt_aa never held true here in the first
        place ('?' != 'L') -- this guard changes nothing about it."""
        fasta = {"chr1": "N" * 200 + "NTG" + "N" * 20}
        cds_map = {"TX1": [_cds("chr1", 201, 203, "+", 0, "TX1")]}
        provider = _make_provider(fasta, cds_map)

        result = provider.get_codon_and_aa("chr1", 201, "A", "T", "TX1")
        assert result == ("missense", "NTG", "TTG", "?", "L")

    def test_a_real_synonymous_call_still_fires(self):
        """Control: two REAL, resolved, genuinely-identical amino acids
        must still classify as synonymous -- the guard must not swallow
        the ordinary case."""
        # chr1:104-106 "GAC" -> Asp (D). Third-position wobble G>C: "GAC"
        # is the same codon before/after; synonymous SNV within GAC/GAT/
        # GAC/GAT (Asp) family, and we only change ref>alt within GAC->GAT.
        fasta = {"chr1": "N" * 103 + "GAC" + "N" * 20}
        cds_map = {"TX3": [_cds("chr1", 104, 106, "+", 0, "TX3")]}
        provider = _make_provider(fasta, cds_map)

        consequence, ref_codon, alt_codon, ref_aa, alt_aa = provider.get_codon_and_aa(
            "chr1", 106, "C", "T", "TX3"
        )
        assert ref_aa == alt_aa == "D"
        assert consequence == "synonymous"


class TestSite1ClinVarLookupGuard:
    """PS1/PM5 must not evaluate against an undetermined '?' amino acid."""

    # Same real, curated pathogenic hotspot already exercised by
    # tests/test_all_fixes_regression.py's TestFix2PS1 -- not a fabricated
    # codon.
    _HIT = ClinVarHit(
        significance="Pathogenic",
        review_stars=4,
        submitter_count=10,
        allele_id="12345",
        conflicting=False,
        gene="BRAF",
        hgvs_p="p.Val600Glu",
    )
    _BY_COORD = {("7", 140453136, "A", "C"): _HIT}

    def test_control_known_good_aa_pair_still_works(self):
        """The fixture and lookup still behave exactly as
        test_all_fixes_regression.py's TestFix2PS1 already proved, before
        touching the '?' case."""
        lkp = _make_clinvar_lookup(self._BY_COORD)
        same_aa, novel_aa = lkp.check_same_codon_pathogenic(
            chrom="7", pos=140453136, ref="A", alt="T", wildtype_aa="Val", mutant_aa="Glu"
        )
        assert same_aa is True
        assert novel_aa is True

    def test_unknown_wildtype_aa_no_longer_fires_pm5(self):
        """THE RED-FIRST CASE. Before the guard: same_aa=False,
        novel_aa=True -- PM5 fired from a genuinely unresolved wildtype AA
        against a real curated pathogenic codon, on nothing but string
        truthiness."""
        lkp = _make_clinvar_lookup(self._BY_COORD)
        same_aa, novel_aa = lkp.check_same_codon_pathogenic(
            chrom="7", pos=140453136, ref="A", alt="T", wildtype_aa="?", mutant_aa="Glu"
        )
        assert same_aa is None
        assert novel_aa is None

    def test_unknown_mutant_aa_also_guarded(self):
        """Symmetry: an unresolved mutant_aa must be caught the same way
        an unresolved wildtype_aa is."""
        lkp = _make_clinvar_lookup(self._BY_COORD)
        same_aa, novel_aa = lkp.check_same_codon_pathogenic(
            chrom="7", pos=140453136, ref="A", alt="T", wildtype_aa="Val", mutant_aa="?"
        )
        assert same_aa is None
        assert novel_aa is None

    def test_both_unknown_is_guarded(self):
        lkp = _make_clinvar_lookup(self._BY_COORD)
        same_aa, novel_aa = lkp.check_same_codon_pathogenic(
            chrom="7", pos=140453136, ref="A", alt="T", wildtype_aa="?", mutant_aa="?"
        )
        assert same_aa is None
        assert novel_aa is None


class TestEndToEndNoLongerFeedsBenignOrModerateEvidenceFromUnknown:
    """Full path: the real classifier must not receive PM5-met from a
    guarded lookup, and BP7 must not receive synonymous_or_intronic=True
    from a guarded consequence."""

    def test_pm5_reports_not_evaluated_not_met_from_guarded_lookup(self):
        from pipeline.acmg.classifier import AcmgClassifier, VariantEvidence

        clf = AcmgClassifier()
        # novel_aa_at_known_pathogenic_codon as the guarded lookup now
        # actually returns (None), not the pre-guard measured value (True).
        ev = VariantEvidence(
            chrom="chr1",
            pos=201,
            ref="A",
            alt="T",
            gene="TESTGENE",
            is_missense=True,
            novel_aa_at_known_pathogenic_codon=None,
            same_aa_pathogenic=None,
        )
        result = clf.classify(ev)
        pm5 = next(c for c in result.all_criteria if c.code == "PM5")
        assert pm5.met is False
        assert pm5.status == "not_evaluated"
        assert "PM5" not in result.criteria_met

    def test_bp7_does_not_fire_from_the_n_elsewhere_consequence(self):
        from pipeline.acmg.classifier import AcmgClassifier, VariantEvidence

        clf = AcmgClassifier()
        # synonymous_or_intronic as shared.py would now derive it: the
        # guarded consequence is "missense_variant", not "synonymous_variant".
        ev = VariantEvidence(
            chrom="chr1",
            pos=201,
            ref="A",
            alt="T",
            gene="TESTGENE",
            synonymous_or_intronic=False,
        )
        result = clf.classify(ev)
        bp7 = next(c for c in result.all_criteria if c.code == "BP7")
        assert bp7.met is False
        assert "BP7" not in result.criteria_met
