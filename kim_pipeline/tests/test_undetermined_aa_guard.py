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
        # *** PINNED, NOT ENDORSED. RULED 2026-08-28. ***
        # The guard does not choose "missense"; it lets the undetermined
        # amino acid fall THROUGH the elimination cascade, whose `else` is
        # `missense`. The effect is the same: a codon nobody could read is
        # now reported as a determined protein change. That consequence was
        # invented against an explicit instruction not to invent one, and
        # this line is what makes it costly to change, so it is recorded as
        # a pin rather than as agreement.
        #
        # It is also load-bearing, not cosmetic: "missense_variant" is
        # exactly what routes this variant into the PS1/PM5 lookup at
        # orchestration/shared.py:352, which pre-guard it never reached.
        # See TestTheTwoGuardsAreOneFix below.
        #
        # WHEN MEREDITH'S not-evaluated STATE FOR THE ANNOTATION LAYER
        # LANDS, THIS PIN IS THE FIRST THING TO REASSESS -- it is expected
        # to change, and changing it is not a regression.
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
        # PM5 FIRST, DELIBERATELY. This is the misread the file exists for,
        # and an assertion only names a defect if it is the one that fails.
        assert novel_aa is None, (
            "PM5 (novel_aa_at_known_pathogenic_codon) was decided from an "
            f"amino acid nobody determined: got {novel_aa!r}, expected None. "
            "Pre-guard, the bare truthiness check at clinvar/lookup.py:356-358 "
            "returned True here against a real curated P/LP record, on nothing "
            "but both strings being non-empty."
        )
        assert same_aa is None, (
            "PS1 (same_aa_pathogenic) must also report insufficient data rather "
            f"than a checked negative: got {same_aa!r}. Pre-guard this was False "
            '-- accidentally safe only because _aa3to1("?") returns "" -- '
            "which is why it must NOT be the first assertion here: red on this "
            "line names PS1's correct negative and never reaches the PM5 check "
            "above, so nothing in the output names the actual defect."
        )

    def test_unknown_mutant_aa_also_guarded(self):
        """Symmetry: an unresolved mutant_aa must be caught the same way
        an unresolved wildtype_aa is."""
        lkp = _make_clinvar_lookup(self._BY_COORD)
        same_aa, novel_aa = lkp.check_same_codon_pathogenic(
            chrom="7", pos=140453136, ref="A", alt="T", wildtype_aa="Val", mutant_aa="?"
        )
        # PM5 FIRST, DELIBERATELY. This is the misread the file exists for,
        # and an assertion only names a defect if it is the one that fails.
        assert novel_aa is None, (
            "PM5 (novel_aa_at_known_pathogenic_codon) was decided from an "
            f"amino acid nobody determined: got {novel_aa!r}, expected None. "
            "Pre-guard, the bare truthiness check at clinvar/lookup.py:356-358 "
            "returned True here against a real curated P/LP record, on nothing "
            "but both strings being non-empty."
        )
        assert same_aa is None, (
            "PS1 (same_aa_pathogenic) must also report insufficient data rather "
            f"than a checked negative: got {same_aa!r}. Pre-guard this was False "
            '-- accidentally safe only because _aa3to1("?") returns "" -- '
            "which is why it must NOT be the first assertion here: red on this "
            "line names PS1's correct negative and never reaches the PM5 check "
            "above, so nothing in the output names the actual defect."
        )

    def test_both_unknown_is_guarded(self):
        lkp = _make_clinvar_lookup(self._BY_COORD)
        same_aa, novel_aa = lkp.check_same_codon_pathogenic(
            chrom="7", pos=140453136, ref="A", alt="T", wildtype_aa="?", mutant_aa="?"
        )
        # PM5 FIRST, DELIBERATELY. This is the misread the file exists for,
        # and an assertion only names a defect if it is the one that fails.
        assert novel_aa is None, (
            "PM5 (novel_aa_at_known_pathogenic_codon) was decided from an "
            f"amino acid nobody determined: got {novel_aa!r}, expected None. "
            "Pre-guard, the bare truthiness check at clinvar/lookup.py:356-358 "
            "returned True here against a real curated P/LP record, on nothing "
            "but both strings being non-empty."
        )
        assert same_aa is None, (
            "PS1 (same_aa_pathogenic) must also report insufficient data rather "
            f"than a checked negative: got {same_aa!r}. Pre-guard this was False "
            '-- accidentally safe only because _aa3to1("?") returns "" -- '
            "which is why it must NOT be the first assertion here: red on this "
            "line names PS1's correct negative and never reaches the PM5 check "
            "above, so nothing in the output names the actual defect."
        )


# ── production wiring these tests reproduce, cited line by line ─────────────
# Duplicated rather than imported, for the same reason
# test_gff3_phase_flips_classification_tier.py duplicates shared.py's LOF set:
# the point is to reproduce what the orchestrator DOES with a provider result.
# If any of these move, this file should be reviewed, not silently follow.

# annotation/stage.py:145-154 -- provider short name -> SO term
_SO_TERM = {
    "missense": "missense_variant",
    "synonymous": "synonymous_variant",
    "stop_gained": "stop_gained",
    "stop_lost": "stop_lost",
    "start_lost": "start_lost",
}


def _so_term(provider_consequence):
    return _SO_TERM.get(provider_consequence, "coding_sequence_variant")


def _is_missense(so_term):
    """orchestration/shared.py:323."""
    return so_term == "missense_variant"


def _synonymous_or_intronic(so_term, spliceai_score):
    """orchestration/shared.py:436-439. Note the second and third clauses --
    they are why BP7 is unreachable from this defect on an input that was not
    already SpliceAI-annotated. See the BP7 test below."""
    return (
        so_term in {"synonymous_variant", "intron_variant"}
        and spliceai_score is not None
        and spliceai_score < 0.2
    )


def _aa_pair_as_stage_py_would_store_it(so_term, ref_aa, alt_aa):
    """annotation/stage.py:1143-1158. Two gates, and BOTH matter here: the
    block only runs for a missense_variant, and `if ref_aa and alt_aa:` is a
    bare truthiness test -- the undetermined marker is a truthy string, so it
    is stored as a real amino acid. That is the site that lets the unknown
    into the variant record at all. Reported, not fixed (it is a truthiness
    site, and the wider enumeration is scoped separately)."""
    if so_term == "missense_variant" and ref_aa and alt_aa:
        return ref_aa, alt_aa
    return None, None


# The both-undetermined fixture: the variant is at codon_index 0 and the N is
# at codon_index 2, so the ALT substitution never overwrites it and it survives
# into BOTH codons. Small synthetic coordinates rather than BRAF's real ones
# because a FASTA fixture at chr7:140453136 would need a 140 Mb string; the
# ClinVar record's SHAPE (significance, stars, submitter count) is copied from
# the real curated entry the other tests use, and its hgvs_p is carried over
# only so the pre-guard PS1 branch has a well-formed string to parse.
_BOTH_UNKNOWN_FASTA = {"chr1": "N" * 200 + "ATN" + "N" * 20}
_BOTH_UNKNOWN_CDS = {"TXE": [_cds("chr1", 201, 203, "+", 0, "TXE")]}


def _plp_at_the_fixture_codon():
    return {
        ("1", 201, "A", "C"): ClinVarHit(
            significance="Pathogenic",
            review_stars=4,
            submitter_count=10,
            allele_id="12345",
            conflicting=False,
            gene="TESTGENE",
            hgvs_p="p.Val600Glu",
        )
    }


class TestEndToEndDerivedThroughTheRealComponents:
    """Full path, with every value DERIVED rather than typed in.

    THIS CLASS REPLACES ONE THAT COULD NOT FAIL. Its two predecessors
    hand-fed the post-fix values -- novel_aa_at_known_pathogenic_codon=None
    and synonymous_or_intronic=False -- straight into VariantEvidence and
    never called the guarded code at all. Measured, not assumed: run against
    the pre-guard tree (427a8e2) BOTH OF THEM PASSED. They asserted that the
    classifier does the right thing with the right input, which was never in
    question, under a class name claiming to prove the full path.

    Everything below comes out of a real FastaCodonContextProvider or a real
    ClinVarLookup and is passed on unmodified.
    """

    def test_pm5_derived_from_the_real_lookup_reports_not_evaluated(self):
        from pipeline.acmg.classifier import AcmgClassifier, VariantEvidence

        lkp = _make_clinvar_lookup(TestSite1ClinVarLookupGuard._BY_COORD)
        same_aa, novel_aa = lkp.check_same_codon_pathogenic(
            chrom="7", pos=140453136, ref="A", alt="T", wildtype_aa="?", mutant_aa="Glu"
        )
        # Derived above; NOT typed in. Pre-guard this pair is (False, True).
        ev = VariantEvidence(
            chrom="chr7",
            pos=140453136,
            ref="A",
            alt="T",
            gene="BRAF",
            is_missense=True,
            same_aa_pathogenic=same_aa,
            novel_aa_at_known_pathogenic_codon=novel_aa,
        )
        result = AcmgClassifier().classify(ev)
        pm5 = next(c for c in result.all_criteria if c.code == "PM5")
        assert "PM5" not in result.criteria_met, (
            "a moderate PATHOGENIC criterion was met for a variant whose "
            f"wildtype amino acid was never determined; criteria_met={sorted(result.criteria_met)!r}"
        )
        assert pm5.met is False
        assert pm5.status == "not_evaluated", (
            "PM5 must be insufficient-data, not a checked negative -- nobody "
            f"looked, because there was nothing to look with; got {pm5.status!r}"
        )

    def test_bp7_derived_from_the_real_provider_on_a_preannotated_input(self):
        """BP7, and WHY THERE IS A SpliceAI SCORE IN THIS TEST.

        808835a's commit message says the synonymous misread was "feeding BP7
        benign evidence", flatly. It cannot, on most inputs: shared.py:436
        also requires `spliceai_score is not None and < 0.2`, and with the
        SpliceAI VEP plugin removed that score is absent, so the conjunction
        is False and the misread reaches nothing. Measured across all four
        guard combinations: BP7 False in every one.

        It is reachable on an input the CALLER already annotated -- stage.py
        :719-729 and :1124-1133 still parse SpliceAI= and DS_AG/AL/DG/DL
        straight out of INFO; only VEP's plugin path was removed
        (vep/stage.py:406-409). So the score is here deliberately: without it
        this test passes in both directions and is another control that
        cannot fire, which is exactly the fault this class was written to
        remove.
        """
        from pipeline.acmg.classifier import AcmgClassifier, VariantEvidence

        provider = _make_provider(_BOTH_UNKNOWN_FASTA, _BOTH_UNKNOWN_CDS)
        consequence, ref_codon, alt_codon, ref_aa, alt_aa = provider.get_codon_and_aa(
            "chr1", 201, "A", "T", "TXE"
        )
        assert ref_aa == "?" and alt_aa == "?", (
            "fixture regression: both amino acids must still be undetermined "
            f"(codons {ref_codon!r}/{alt_codon!r}), or this test proves nothing"
        )

        so = _so_term(consequence)
        synonymous_or_intronic = _synonymous_or_intronic(so, spliceai_score=0.1)
        ev = VariantEvidence(
            chrom="chr1",
            pos=201,
            ref="A",
            alt="T",
            gene="TESTGENE",
            is_missense=_is_missense(so),
            synonymous_or_intronic=synonymous_or_intronic,
        )
        result = AcmgClassifier().classify(ev)
        assert "BP7" not in result.criteria_met, (
            "BP7 -- supporting BENIGN -- was awarded because two undetermined "
            f"amino acids compared equal and the variant was called {so!r}. "
            "An unknown resolving to benign closes the case; that is the "
            "direction that does not get a second look."
        )
        assert next(c for c in result.all_criteria if c.code == "BP7").met is False


class TestTheTwoGuardsAreOneFix:
    """*** SITE 1 ALONE IS STRICTLY WORSE THAN NEITHER GUARD. ***

    The two guards look independent and are not. Measured across all four
    combinations, on this fixture, through these same real components:

        neither       synonymous_variant  lookup NOT reached  PM5 not_evaluated
        SITE 1 ONLY   missense_variant    lookup reached      PM5 *** MET ***
        site 2 only   synonymous_variant  lookup NOT reached  PM5 not_evaluated
        both          missense_variant    lookup reached      PM5 not_evaluated

    Site 1 on its own MANUFACTURES a met PM5 -- moderate, pathogenic
    direction -- from an amino acid nobody determined. It is met in no other
    state. The mechanism is structural, not an artefact of this fixture:
    shared.py:352 gates the PS1/PM5 lookup on `is_missense`, and site 1 is
    precisely what flips this case from synonymous_variant to
    missense_variant. Pre-guard it NEVER REACHED THE LOOKUP AT ALL. Site 1
    opens a door; site 2 is the only thing standing behind it.

    Both shipped in 808835a, so the tree is in the bottom row and is safe.
    This class exists because nothing in that commit records that the two
    cannot be separated -- a revert, cherry-pick or bisect that takes one
    lands on the second row. Same shape as the conservation pair
    (conservation/provider.py:248 with orchestrator.py:1810), different files.
    """

    def test_site_1_opens_the_lookup_door_that_only_site_2_closes(self):
        from pipeline.acmg.classifier import AcmgClassifier, VariantEvidence

        provider = _make_provider(_BOTH_UNKNOWN_FASTA, _BOTH_UNKNOWN_CDS)
        consequence, _ref_codon, _alt_codon, ref_aa, alt_aa = provider.get_codon_and_aa(
            "chr1", 201, "A", "T", "TXE"
        )
        so = _so_term(consequence)

        # -- site 1's half, and the reason the rest of this test can run.
        assert _is_missense(so), (
            f"this variant is no longer routed to the PS1/PM5 lookup (so_term={so!r}); "
            "shared.py:352 gates that lookup on is_missense. Either site 1 was "
            "reverted, or the consequence pin changed -- in both cases the coupling "
            "this test exists to prove is no longer being exercised, so treat a red "
            "here as the test going blind, not as the defect returning."
        )
        wildtype_aa, mutant_aa = _aa_pair_as_stage_py_would_store_it(so, ref_aa, alt_aa)
        assert wildtype_aa == "?", (
            "the undetermined amino acid is no longer reaching the lookup "
            f"(wildtype_aa={wildtype_aa!r}); same warning as above -- this test can "
            "only detect the defect while the unknown actually gets this far"
        )

        # -- site 2's half: the only thing that stops it here.
        lkp = _make_clinvar_lookup(_plp_at_the_fixture_codon())
        same_aa, novel_aa = lkp.check_same_codon_pathogenic(
            chrom="1", pos=201, ref="A", alt="T", wildtype_aa=wildtype_aa, mutant_aa=mutant_aa
        )
        assert novel_aa is None, (
            f"PM5 was decided ({novel_aa!r}) from an undetermined amino acid that "
            "site 1 had just routed into the lookup. This is the second row of the "
            "table above: site 1 without site 2. The two guards are one fix and "
            "must not be separated."
        )

        result = AcmgClassifier().classify(
            VariantEvidence(
                chrom="chr1",
                pos=201,
                ref="A",
                alt="T",
                gene="TESTGENE",
                is_missense=_is_missense(so),
                same_aa_pathogenic=same_aa,
                novel_aa_at_known_pathogenic_codon=novel_aa,
            )
        )
        assert "PM5" not in result.criteria_met, (
            "moderate pathogenic evidence manufactured from an undetermined amino "
            f"acid; criteria_met={sorted(result.criteria_met)!r}"
        )
