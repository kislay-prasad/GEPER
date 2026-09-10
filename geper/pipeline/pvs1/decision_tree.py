"""
The PVS1 decision tree.

ACMG/AMP 2015 (Richards et al., Genet Med 17:405) states PVS1 as a
single binary rule:

    "null variant (nonsense, frameshift, canonical +-1 or 2 splice
     sites, initiation codon, single or multiexon deletion) in a gene
     where LOF is a known mechanism of disease"

with four written caveats: beware of genes where LOF is not a known
mechanism; use caution interpreting LOF variants at the extreme 3' end
of a gene; use caution with splice variants predicted to lead to exon
skipping but leave the remainder of the protein intact; and use caution
in the presence of multiple transcripts.

The ClinGen Sequence Variant Interpretation (SVI) Working Group turned
those caveats into an explicit decision tree with graded outcomes
(Abou Tayoun et al., Hum Mutat 2018;39:1517-1524, PMID 30192042):
PVS1 / PVS1_Strong / PVS1_Moderate / PVS1_Supporting / not applicable.
This module implements that tree. Leaf codes (NF1-NF6, SS1-SS10,
IC3/IC4, DEL0-DEL3) follow the same naming used by AutoPVS1
(Xiang et al. 2020; https://github.com/JiguangPeng/autopvs1), the
reference implementation of the SVI tree, so a leaf reported here can
be compared against that tool's output node-for-node.

Where GEPER has no integrated source for a node the tree asks about
(e.g. "have LoF variants 3' of this position been reported as
pathogenic?"), the node is *not* guessed: the evaluation takes the
branch that assumes no such evidence -- which is the conservative
direction, since every one of those nodes can only ever *raise* the
strength -- and the unanswered question is recorded in
`unchecked_caveats` so it shows up in the report as a gap rather than
disappearing. This mirrors the "never fabricate evidence" policy the
rest of `pipeline/acmg_rules.py` already follows.

This module is pure: it takes a `PVS1Input` of already-collected facts
and returns a `PVS1Evaluation`. All IO lives in `lookup.py`, and all
adaptation to GEPER's provider-result dicts lives in `utils.py`.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from pipeline.pvs1.models import (
    LOF_ESTABLISHED,
    LOF_ESTABLISHED_RECESSIVE,
    LOF_NOT_ESTABLISHED,
    LOF_REFUTED,
    LOF_UNKNOWN,
    NULL_CANONICAL_SPLICE,
    NULL_FRAMESHIFT,
    NULL_INITIATION_CODON,
    NULL_NONSENSE,
    NULL_WHOLE_GENE_DELETION,
    PVS1Evaluation,
    QUALIFYING_NULL_TYPES,
    STRENGTH_MODERATE,
    STRENGTH_NOT_APPLICABLE,
    STRENGTH_STRONG,
    STRENGTH_SUPPORTING,
    STRENGTH_VERY_STRONG,
    TranscriptContext,
    cap_strength,
)

# Mechanism states in which PVS1 may be applied at the strength the
# decision tree returns. Everything else either blocks PVS1 (a curated
# negative) or leaves it unevaluated (no curation at all).
_MECHANISM_ESTABLISHED = (LOF_ESTABLISHED, LOF_ESTABLISHED_RECESSIVE)


@dataclass
class PVS1Input:
    """
    Everything the decision tree needs, already resolved. Built from
    GEPER's provider-result dicts by `pipeline/pvs1/utils.py::
    build_pvs1_input`; constructed directly in tests.
    """

    null_variant_type: Optional[str]
    transcript: Optional[TranscriptContext] = None
    pos: Optional[int] = None

    # Human-readable protein consequence GEPER actually determined for
    # this variant when it is NOT a qualifying null class (e.g.
    # "missense (amino acid substitution)", "synonymous (no amino acid
    # change)", "in-frame insertion/deletion") -- same transcript-
    # verified classification `pipeline/interpretation.py`'s evidence
    # line already uses (`ProteinEffectFlags`, see
    # `pipeline/pvs1/utils.py::protein_effect_flags`). Populated only
    # when `null_variant_type` is None; used solely to make the NF0
    # not-applicable rationale name the real consequence instead of the
    # word "undetermined" when GEPER in fact determined one (D2, report
    # review round 3).
    observed_consequence: Optional[str] = None

    # -- gene-level LOF mechanism (ClinGen dosage sensitivity) --------
    lof_mechanism: str = LOF_UNKNOWN
    lof_mechanism_evidence: List[str] = field(default_factory=list)

    # -- variant-level facts ------------------------------------------
    # Codon of the new stop, when a caller knows it (e.g. parsed from an
    # HGVS.p string). When absent the tree derives it from the
    # transcript structure and says so in the decision path.
    termination_codon: Optional[int] = None
    population_af: Optional[float] = None
    population_af_label: Optional[str] = None

    # Canonical-protein coordinates of annotated functional regions,
    # e.g. [{"start": 1650, "end": 1863, "label": "BRCT domain"}].
    functional_regions: Optional[List[Dict[str, Any]]] = None

    # Splice-consequence override from a splice predictor, when one
    # produced a usable call: "frameshift" | "frame_preserving".
    splice_frame_consequence: Optional[str] = None

    # Tri-state: True/False when a caller has positively determined
    # whether the affected exon is present in the biologically relevant
    # transcript(s); None when unknown (reported, never assumed False).
    exon_biologically_relevant: Optional[bool] = None

    # Deleted genomic interval (inclusive) for exon/gene deletions.
    deleted_interval: Optional[Tuple[int, int]] = None

    # SVI initiation-codon node: are there known pathogenic variants
    # upstream of the closest potential in-frame start codon?
    pathogenic_variants_upstream_of_alt_start: Optional[bool] = None

    # -- thresholds (defaults mirror the published rule) --------------
    nmd_penultimate_window_bp: int = 50
    protein_loss_strong_fraction: float = 0.10
    lof_population_af_max: float = 0.001
    nmd_uncertainty_margin_codons: float = 50.0
    initiation_codon_max_strength: str = STRENGTH_MODERATE


class PVS1DecisionTree:
    """Evaluates the ClinGen SVI PVS1 decision tree for one variant."""

    def evaluate(self, inp: PVS1Input) -> PVS1Evaluation:
        path: List[str] = []
        checked: List[str] = []
        unchecked: List[str] = []
        supporting: List[str] = []
        conflicting: List[str] = []
        sources: List[str] = []

        # -- Step 1: is this a qualifying null variant class? ----------
        if inp.null_variant_type not in QUALIFYING_NULL_TYPES:
            consequence_label = inp.null_variant_type or inp.observed_consequence or "undetermined"
            return PVS1Evaluation(
                applies=False,
                strength=STRENGTH_NOT_APPLICABLE,
                criterion_code="NF0",
                null_variant_type=inp.null_variant_type,
                rationale=(
                    "PVS1 applies only to null variants (nonsense, frameshift, canonical +-1/+-2 splice "
                    "site, initiation codon, single/multi-exon deletion). This variant's predicted "
                    f"consequence ({consequence_label}) is not one of them."
                ),
                lof_mechanism=inp.lof_mechanism,
                decision_path=["Qualifying null variant class? -> No."],
            )
        path.append(f"Qualifying null variant class? -> Yes ({inp.null_variant_type}).")

        # -- Step 2: does the gene have an established LOF mechanism? --
        # Recorded here, enforced after the tree runs, so the report can
        # always show what strength the variant itself would carry.
        mechanism = inp.lof_mechanism
        sources.append("ClinGen")
        if mechanism in _MECHANISM_ESTABLISHED:
            path.append(f"Loss of function is an established disease mechanism for this gene? -> Yes ({mechanism}).")
            checked.append("Gene-level LOF disease mechanism: established by ClinGen dosage curation.")
        elif mechanism == LOF_REFUTED:
            path.append("Loss of function is an established disease mechanism for this gene? -> No (curated against).")
            checked.append(
                "Gene-level LOF disease mechanism: ClinGen curates this gene as dosage-sensitivity unlikely."
            )
        elif mechanism == LOF_NOT_ESTABLISHED:
            path.append("Loss of function is an established disease mechanism for this gene? -> Not established.")
            checked.append(
                "Gene-level LOF disease mechanism: ClinGen dosage curation exists but does not establish haploinsufficiency."
            )
        else:
            path.append(
                "Loss of function is an established disease mechanism for this gene? -> Unknown (no curation available)."
            )
            unchecked.append(
                "Gene-level LOF disease mechanism: no ClinGen dosage curation was available for this gene."
            )
        supporting.extend(inp.lof_mechanism_evidence)

        # -- Step 3: transcript structure ------------------------------
        # Every location caveat below (last exon, NMD boundary, fraction
        # of protein lost, exon-skipping frame effect) needs it. Without
        # it PVS1 cannot be responsibly applied at any strength, because
        # the single most consequential caveat -- "is this in the last
        # exon?" -- is exactly the one that cannot be checked.
        transcript = inp.transcript
        if transcript is None and inp.null_variant_type != NULL_WHOLE_GENE_DELETION:
            return PVS1Evaluation(
                applies=False,
                strength=STRENGTH_NOT_APPLICABLE,
                criterion_code="NF0",
                null_variant_type=inp.null_variant_type,
                rationale=(
                    "Variant is a qualifying null variant, but no transcript structure was available, so "
                    "the ClinGen SVI location caveats (last exon / last 50 bp of the penultimate exon, "
                    "NMD prediction, fraction of protein lost, exon-skipping frame effect) could not be "
                    "checked. PVS1 is not applied on an unchecked decision tree."
                ),
                lof_mechanism=mechanism,
                decision_path=path + ["Transcript structure available? -> No."],
                caveats_checked=checked,
                unchecked_caveats=unchecked
                + [
                    "Variant location relative to the last exon / NMD boundary: transcript structure unavailable.",
                    "Fraction of the protein removed: transcript structure unavailable.",
                ],
                supporting_evidence=supporting,
                evidence_sources=sources + ["transcript_structure"],
                confidence="Low",
            )
        if transcript is not None:
            sources.append("transcript_structure")
            path.append(
                f"Transcript structure available? -> Yes ({transcript.transcript_id}, "
                f"{len(transcript.coding_exon_sizes())} coding exons, {transcript.cds_length} bp CDS)."
            )

        # -- Step 4: biologically relevant transcript / exon -----------
        relevant = self._exon_relevance(inp, transcript, checked, unchecked)

        # -- Step 5: the type-specific decision tree -------------------
        # `transcript` is guaranteed non-None in both branches below:
        # the only way to reach here with `transcript is None` is
        # `null_variant_type == NULL_WHOLE_GENE_DELETION` (the early
        # return above), which is mutually exclusive with both
        # `null_variant_type` checks immediately following -- spelled
        # out as an assert (not just implied by control flow) so mypy
        # can narrow the type here instead of flagging `Optional[
        # TranscriptContext]` against a non-Optional parameter (G1,
        # report review round 5).
        if inp.null_variant_type in (NULL_NONSENSE, NULL_FRAMESHIFT):
            assert transcript is not None
            outcome = self._nonsense_frameshift(inp, transcript, relevant, path, checked, unchecked, supporting)
        elif inp.null_variant_type == NULL_CANONICAL_SPLICE:
            assert transcript is not None
            outcome = self._canonical_splice(inp, transcript, relevant, path, checked, unchecked, supporting)
        elif inp.null_variant_type == NULL_INITIATION_CODON:
            outcome = self._initiation_codon(inp, transcript, path, checked, unchecked, supporting)
        else:  # exon / whole-gene deletion
            outcome = self._deletion(inp, transcript, relevant, path, checked, unchecked, supporting)

        provisional, code, termination_codon, nmd_predicted = outcome

        # -- Step 6: population-frequency disqualifier -----------------
        # The SVI tree asks, in its NMD-escape branches, whether LoF
        # variants in this exon are frequent in the general population.
        # GEPER has variant-level gnomAD frequencies rather than a
        # per-exon LoF frequency track, so this is applied as a
        # variant-level approximation of that node -- and it is applied
        # to every branch, since a null allele carried by more than
        # `lof_population_af_max` of the population cannot support very
        # strong evidence of pathogenicity regardless of where it sits.
        provisional, code = self._apply_frequency_gate(
            inp, provisional, code, path, checked, unchecked, conflicting, sources
        )

        # -- Step 7: enforce the gene-level mechanism gate -------------
        applies, final_strength, rationale, confidence = self._apply_mechanism_gate(
            inp, mechanism, provisional, path, code
        )

        return PVS1Evaluation(
            applies=applies,
            strength=final_strength,
            provisional_strength=provisional,
            criterion_code=code,
            null_variant_type=inp.null_variant_type,
            lof_mechanism=mechanism,
            rationale=rationale,
            decision_path=path,
            caveats_checked=checked,
            unchecked_caveats=unchecked,
            supporting_evidence=supporting,
            conflicting_evidence=conflicting,
            evidence_sources=sources,
            confidence=confidence,
            transcript_id=transcript.transcript_id if transcript else None,
            termination_codon=termination_codon,
            nmd_predicted=nmd_predicted,
        )

    # ------------------------------------------------------------------
    # Shared nodes
    # ------------------------------------------------------------------

    @staticmethod
    def _exon_relevance(
        inp: PVS1Input,
        transcript: Optional[TranscriptContext],
        checked: List[str],
        unchecked: List[str],
    ) -> Optional[bool]:
        """
        Resolve the SVI tree's "exon is present in biologically-relevant
        transcript(s)" node. An explicit False from the caller (the exon
        is alternatively spliced out of the disease-relevant isoform)
        blocks PVS1 entirely; True applies it; None leaves the question
        open and is recorded as an unchecked caveat.
        """
        if inp.exon_biologically_relevant is not None:
            checked.append(
                "Exon present in biologically-relevant transcript(s): "
                + ("yes." if inp.exon_biologically_relevant else "NO -- exon is skipped in the relevant isoform.")
            )
            return inp.exon_biologically_relevant
        if transcript is not None and (transcript.biologically_relevant is not None):
            checked.append(
                "Biologically-relevant transcript: " + ("yes." if transcript.biologically_relevant else "NO.")
            )
            return transcript.biologically_relevant
        if transcript is not None and (transcript.is_mane_select or transcript.is_canonical):
            # Report review round 4, I8 (acmg_rules.py::_pm1) established
            # that "MANE Select transcript" reads as "the NCBI MANE Select
            # dataset was consulted" when transcript.is_mane_select is in
            # fact sourced only from Ensembl's own GTF tag -- Provenance's
            # separately-tracked "MANE Select (NCBI)" entry is the only
            # consumer of that dataset, and this is not it. Reusing PM1's
            # already-corrected phrase verbatim rather than inventing new
            # wording, so a future grep finds both.
            checked.append(
                f"Biologically-relevant transcript: {transcript.transcript_id} is this gene's "
                "Ensembl-canonical/MANE-tagged transcript, taken as biologically relevant."
            )
            unchecked.append(
                "Alternative-isoform rescue: no isoform-level expression source is integrated, so it "
                "was not verified that the affected exon is present in every disease-relevant isoform."
            )
            return True
        unchecked.append(
            "Exon present in biologically-relevant transcript(s): not determined (no MANE/canonical flag "
            "and no isoform-expression source integrated)."
        )
        return None

    def _truncation_subtree(
        self,
        inp: PVS1Input,
        transcript: TranscriptContext,
        first_lost_codon: Optional[int],
        removed_fraction: Optional[float],
        codes: Tuple[str, str, str],
        path: List[str],
        checked: List[str],
        unchecked: List[str],
        supporting: List[str],
        region_label: str,
        last_lost_codon: Optional[int] = None,
    ) -> Tuple[str, str]:
        """
        The subtree shared by every NMD-escaping / in-frame branch:

            Truncated or altered region critical to protein function?
              -> Yes: PVS1_Strong
              -> No : removes >10% of the protein?
                        -> Yes: PVS1_Strong
                        -> No : PVS1_Moderate

        `codes` supplies the three leaf codes for the calling branch
        (critical, >10%, else) so the reported code identifies the exact
        node in the published tree.

        `last_lost_codon`: the end of the altered/removed codon range.
        Defaults to the end of the protein (`transcript.total_codons`)
        when omitted -- correct for a truncating variant (nonsense/
        frameshift/NMD-escaping), where everything downstream of
        `first_lost_codon` really is lost. An in-frame exon skip must
        pass the *skipped exon's own* last codon here instead: only
        that exon's codons are removed, and the callers 3' of it are
        still translated normally, so treating the whole rest of the
        protein as "removed" would spuriously overlap any downstream
        functional domain (D3, report review round 3).
        """
        code_critical, code_ten_percent, code_else = codes
        if last_lost_codon is None:
            last_lost_codon = transcript.total_codons

        critical, critical_note = self._is_critical_region(inp, transcript, first_lost_codon, last_lost_codon)
        if critical is True:
            path.append(f"{region_label} critical to protein function? -> Yes.")
            checked.append(f"Critical-region check: {critical_note}")
            supporting.append(critical_note)
            return STRENGTH_STRONG, code_critical
        if critical is False:
            path.append(f"{region_label} critical to protein function? -> No.")
            checked.append(f"Critical-region check: {critical_note}")
        else:
            path.append(f"{region_label} critical to protein function? -> Not determined; treated as 'no'.")
            unchecked.append(
                "Critical-region check: no functional-domain annotation was available for the affected "
                "region, so the SVI 'region critical to protein function' node could not be answered. "
                "The conservative branch (treat as not critical) was taken; a curated critical region "
                "would raise this to PVS1_Strong."
            )
        unchecked.append(
            "Role of region in disease: GEPER does not integrate a catalogue of reported pathogenic LoF "
            "variants 3' of this position, so that SVI node was not evaluated."
        )

        if removed_fraction is None:
            path.append("Removes >10% of the protein? -> Not determined; treated as 'no'.")
            unchecked.append("Fraction of the protein removed could not be computed for this variant.")
            return STRENGTH_MODERATE, code_else

        threshold = inp.protein_loss_strong_fraction
        pct = removed_fraction * 100.0
        if removed_fraction > threshold:
            path.append(f"Removes >{threshold * 100:.0f}% of the protein? -> Yes ({pct:.1f}%).")
            checked.append(f"Fraction of protein removed: {pct:.1f}% (> {threshold * 100:.0f}% threshold).")
            supporting.append(f"The variant removes approximately {pct:.1f}% of the protein product.")
            return STRENGTH_STRONG, code_ten_percent
        path.append(f"Removes >{threshold * 100:.0f}% of the protein? -> No ({pct:.1f}%).")
        checked.append(f"Fraction of protein removed: {pct:.1f}% (at or below the {threshold * 100:.0f}% threshold).")
        return STRENGTH_MODERATE, code_else

    @staticmethod
    def _is_critical_region(
        inp: PVS1Input,
        transcript: Optional[TranscriptContext],
        first_lost_codon: Optional[int],
        last_lost_codon: Optional[int] = None,
    ) -> Tuple[Optional[bool], str]:
        """
        Does the truncated/altered region overlap an annotated functional
        region? Returns (True/False/None, explanation) -- None when no
        annotation was supplied at all, which is a different statement
        from "no domain overlaps".

        `last_lost_codon` defaults to the end of the protein (matching
        every truncating caller); an in-frame exon-skip caller passes
        the skipped exon's own last codon instead, since only that
        exon's codons are actually removed (see `_truncation_subtree`'s
        docstring).
        """
        if not inp.functional_regions:
            return None, "no functional-region annotation supplied."
        if last_lost_codon is None:
            last_lost_codon = transcript.total_codons if transcript is not None else None
        if first_lost_codon is None or last_lost_codon is None:
            return None, "functional regions supplied but the affected codon range is unknown."
        lost_start, lost_end = first_lost_codon, last_lost_codon
        hits = []
        for region in inp.functional_regions:
            start, end = region.get("start"), region.get("end")
            if start is None or end is None:
                continue
            if start <= lost_end and end >= lost_start:
                hits.append(region.get("label") or region.get("name") or f"region {start}-{end}")
        if hits:
            return True, (
                f"the removed/altered region (codons {lost_start}-{lost_end}) overlaps "
                f"{len(hits)} annotated functional region(s): {', '.join(hits[:3])}."
            )
        return (
            False,
            f"the removed/altered region (codons {lost_start}-{lost_end}) overlaps no annotated functional region.",
        )

    # ------------------------------------------------------------------
    # Branch: nonsense / frameshift
    # ------------------------------------------------------------------

    def _nonsense_frameshift(
        self,
        inp: PVS1Input,
        transcript: TranscriptContext,
        relevant: Optional[bool],
        path: List[str],
        checked: List[str],
        unchecked: List[str],
        supporting: List[str],
    ) -> Tuple[str, str, Optional[int], Optional[bool]]:
        termination_codon = inp.termination_codon
        if termination_codon is None and inp.pos is not None:
            termination_codon = transcript.codon_at(inp.pos)
            if termination_codon is not None and inp.null_variant_type == NULL_FRAMESHIFT:
                unchecked.append(
                    "Exact position of the new stop codon: not supplied, so the variant's own codon was "
                    "used as the premature-termination-codon position. The true stop lies at or 3' of it, "
                    "so this can only make the NMD call more conservative toward NMD, never less."
                )
        if termination_codon is None:
            path.append("Premature termination codon position? -> Could not be determined.")
            unchecked.append("Premature-termination-codon position could not be mapped onto the transcript CDS.")
            return STRENGTH_NOT_APPLICABLE, "NF0", None, None

        path.append(f"Premature termination codon at codon {termination_codon} of {transcript.total_codons}.")

        cutoff = transcript.nmd_cutoff_cds(inp.nmd_penultimate_window_bp)
        nmd = transcript.is_nmd_predicted(termination_codon, inp.nmd_penultimate_window_bp)
        in_last_exon = inp.pos is not None and transcript.is_in_last_exon(inp.pos)

        if cutoff is None:
            checked.append(
                "NMD prediction: this transcript has a single coding exon, so there is no downstream "
                "exon-exon junction and NMD cannot occur."
            )
        else:
            checked.append(
                f"NMD prediction: the premature stop is at CDS nt {termination_codon * 3} and the NMD "
                f"boundary (last exon + last {inp.nmd_penultimate_window_bp} bp of the penultimate exon) "
                f"is at CDS nt {cutoff} -- NMD {'IS' if nmd else 'is NOT'} predicted."
            )
        if in_last_exon:
            checked.append("Last-exon caveat: the variant lies in the 3'-most (last) exon of the transcript.")

        if nmd:
            path.append("Predicted to undergo NMD? -> Yes.")
            if relevant is False:
                path.append("Exon present in biologically-relevant transcript(s)? -> No.")
                return STRENGTH_NOT_APPLICABLE, "NF2", termination_codon, True
            path.append("Exon present in biologically-relevant transcript(s)? -> Yes (or not contradicted).")
            supporting.append(
                f"Premature termination at codon {termination_codon} is predicted to trigger "
                "nonsense-mediated decay, eliminating the transcript."
            )
            return STRENGTH_VERY_STRONG, "NF1", termination_codon, True

        path.append("Predicted to undergo NMD? -> No (transcript escapes NMD).")
        if relevant is False:
            path.append("Exon present in biologically-relevant transcript(s)? -> No.")
            return STRENGTH_NOT_APPLICABLE, "NF4", termination_codon, False

        fraction = transcript.fraction_of_protein_lost(termination_codon)
        strength, code = self._truncation_subtree(
            inp,
            transcript,
            termination_codon,
            fraction,
            ("NF3", "NF5", "NF6"),
            path,
            checked,
            unchecked,
            supporting,
            region_label="Truncated region",
        )
        return strength, code, termination_codon, False

    # ------------------------------------------------------------------
    # Branch: canonical +-1/+-2 splice site
    # ------------------------------------------------------------------

    def _canonical_splice(
        self,
        inp: PVS1Input,
        transcript: TranscriptContext,
        relevant: Optional[bool],
        path: List[str],
        checked: List[str],
        unchecked: List[str],
        supporting: List[str],
    ) -> Tuple[str, str, Optional[int], Optional[bool]]:
        site = transcript.splice_site_at(inp.pos) if inp.pos is not None else None
        if site is None:
            path.append("Canonical splice site of this transcript? -> Position does not map to a +-1/+-2 site.")
            unchecked.append(
                "Splice-site branch: the variant position did not map to a canonical +-1/+-2 dinucleotide "
                "of this transcript, so the exon whose splicing is disrupted is unknown."
            )
            return STRENGTH_NOT_APPLICABLE, "SS0", None, None

        exon_rank, side, offset = site
        path.append(f"Canonical splice site? -> Yes ({side} {offset} bp into the intron, exon {exon_rank}).")
        checked.append(f"Canonical splice site: disrupts the {side} site of exon {exon_rank}.")

        # Which exon is lost if the disrupted junction causes skipping:
        # the exon whose own donor/acceptor was destroyed.
        skipped_rank = exon_rank
        preserves_frame = transcript.exon_skip_preserves_frame(skipped_rank)
        if inp.splice_frame_consequence in ("frameshift", "frame_preserving"):
            preserves_frame = inp.splice_frame_consequence == "frame_preserving"
            checked.append(
                f"Reading-frame effect: taken from the supplied splice consequence ('{inp.splice_frame_consequence}')."
            )
        elif preserves_frame is not None:
            span = transcript.coding_span_for_rank(skipped_rank)
            # `coding_span_for_rank` can return None if `skipped_rank`
            # doesn't match any exon this transcript actually has (a
            # stale/out-of-range rank) -- previously would have raised
            # `AttributeError: 'NoneType' object has no attribute
            # 'length'` here instead of degrading gracefully like every
            # other "could not be determined" branch in this tree (G1,
            # report review round 5; caught by mypy, not observed live).
            removed_bp = f"{span.length}" if span is not None else "an unknown number of"
            checked.append(
                f"Reading-frame effect: skipping exon {skipped_rank} removes {removed_bp} coding bp, "
                f"which {'preserves' if preserves_frame else 'disrupts'} the reading frame."
            )
            unchecked.append(
                "Cryptic splice-site usage: GEPER does not run a cryptic-splice-site search (e.g. "
                "MaxEntScan) for this rule, so the reading-frame effect is modelled as whole-exon "
                "skipping only."
            )

        if preserves_frame is None:
            path.append("Reading frame preserved by exon skipping? -> Could not be determined.")
            unchecked.append("Reading-frame effect of exon skipping could not be computed.")
            return STRENGTH_NOT_APPLICABLE, "SS0", None, None

        span = transcript.coding_span_for_rank(skipped_rank)
        first_lost_codon = (span.cds_start + 2) // 3 if span and span.cds_start else None

        if preserves_frame:
            path.append("Reading frame preserved by exon skipping? -> Yes (in-frame exon skip).")
            if relevant is False:
                path.append("Exon present in biologically-relevant transcript(s)? -> No.")
                return STRENGTH_NOT_APPLICABLE, "SS7", None, None
            # For an in-frame skip only the skipped exon is removed, so
            # the ">10% of the protein" node is answered with the size of
            # that exon -- not with everything 3' of it, which is what a
            # frameshift would remove.
            removed_fraction = None
            if span and transcript.total_codons:
                removed_fraction = (span.length / 3.0) / transcript.total_codons
            last_lost_codon = (span.cds_end + 2) // 3 if span and span.cds_end else None
            strength, code = self._truncation_subtree(
                inp,
                transcript,
                first_lost_codon,
                removed_fraction,
                ("SS10", "SS8", "SS9"),
                path,
                checked,
                unchecked,
                supporting,
                region_label="In-frame deleted region",
                last_lost_codon=last_lost_codon,
            )
            return strength, code, None, False

        path.append("Reading frame preserved by exon skipping? -> No (frameshift).")
        nmd_info = transcript.nmd_after_exon_skip(skipped_rank, inp.nmd_penultimate_window_bp)
        nmd = bool(nmd_info and nmd_info.get("nmd_predicted"))
        if nmd_info and nmd_info.get("cutoff_cds") is not None:
            checked.append(
                f"NMD prediction (post-skip transcript): the frameshift begins at CDS nt "
                f"{nmd_info['ptc_lower_bound_cds']} and the post-skip NMD boundary is at CDS nt "
                f"{nmd_info['cutoff_cds']} -- NMD {'IS' if nmd else 'is NOT'} predicted."
            )
            margin = nmd_info.get("margin_codons")
            if margin is not None and abs(margin) < inp.nmd_uncertainty_margin_codons:
                unchecked.append(
                    f"NMD prediction margin: the new stop codon's earliest possible position is only "
                    f"{abs(margin):.0f} codons from the NMD boundary, so this NMD call is sensitive to "
                    "where the new stop actually falls; confirm with RNA studies."
                )
        elif nmd_info and nmd_info.get("reason"):
            checked.append(f"NMD prediction (post-skip transcript): {nmd_info['reason']}")

        if nmd:
            if relevant is False:
                path.append("Exon present in biologically-relevant transcript(s)? -> No.")
                return STRENGTH_NOT_APPLICABLE, "SS2", None, True
            path.append("Predicted to undergo NMD? -> Yes.")
            supporting.append(
                f"Loss of the canonical {side} site of exon {skipped_rank} shifts the reading frame and "
                "the resulting transcript is predicted to undergo nonsense-mediated decay."
            )
            return STRENGTH_VERY_STRONG, "SS1", None, True

        path.append("Predicted to undergo NMD? -> No (transcript escapes NMD).")
        if relevant is False:
            path.append("Exon present in biologically-relevant transcript(s)? -> No.")
            return STRENGTH_NOT_APPLICABLE, "SS4", None, False
        removed_fraction = transcript.fraction_of_protein_lost(first_lost_codon) if first_lost_codon else None
        strength, code = self._truncation_subtree(
            inp,
            transcript,
            first_lost_codon,
            removed_fraction,
            ("SS3", "SS5", "SS6"),
            path,
            checked,
            unchecked,
            supporting,
            region_label="Truncated region",
        )
        return strength, code, None, False

    # ------------------------------------------------------------------
    # Branch: initiation codon
    # ------------------------------------------------------------------

    def _initiation_codon(
        self,
        inp: PVS1Input,
        transcript: Optional[TranscriptContext],
        path: List[str],
        checked: List[str],
        unchecked: List[str],
        supporting: List[str],
    ) -> Tuple[str, str, Optional[int], Optional[bool]]:
        """
        The SVI recommendation is explicit that initiation-codon variants
        should not receive PVS1 or PVS1_Strong: translation frequently
        reinitiates at a downstream in-frame methionine, so the null
        effect is not established by the variant class alone. The
        strongest available outcome is PVS1_Moderate, and that only when
        pathogenic variants are known upstream of the closest potential
        in-frame start codon.
        """
        path.append("Initiation codon variant -- SVI caps PVS1 at Moderate for this class.")
        checked.append(
            "Alternative-start rescue caveat: translation can reinitiate at a downstream in-frame ATG, "
            "so the ClinGen SVI recommendation forbids PVS1 (Very Strong) and PVS1_Strong for "
            "initiation-codon variants."
        )
        if inp.pathogenic_variants_upstream_of_alt_start is True:
            path.append("Known pathogenic variants upstream of the closest potential in-frame start codon? -> Yes.")
            supporting.append(
                "Pathogenic variants have been reported upstream of the closest potential in-frame start "
                "codon, indicating the N-terminal region lost here is required for function."
            )
            return cap_strength(STRENGTH_MODERATE, inp.initiation_codon_max_strength), "IC3", None, None
        if inp.pathogenic_variants_upstream_of_alt_start is False:
            path.append("Known pathogenic variants upstream of the closest potential in-frame start codon? -> No.")
            checked.append(
                "No pathogenic variants are reported upstream of the closest potential in-frame start codon."
            )
        else:
            path.append("Known pathogenic variants upstream of the closest potential in-frame start codon? -> Unknown.")
            unchecked.append(
                "Initiation-codon node: GEPER does not integrate a catalogue of pathogenic variants "
                "upstream of the closest potential in-frame start codon (the same gap that leaves PS1/PM5 "
                "unevaluated), so the conservative branch was taken. That evidence would raise this to "
                "PVS1_Moderate."
            )
        return cap_strength(STRENGTH_SUPPORTING, inp.initiation_codon_max_strength), "IC4", None, None

    # ------------------------------------------------------------------
    # Branch: single / multi-exon and whole-gene deletion
    # ------------------------------------------------------------------

    def _deletion(
        self,
        inp: PVS1Input,
        transcript: Optional[TranscriptContext],
        relevant: Optional[bool],
        path: List[str],
        checked: List[str],
        unchecked: List[str],
        supporting: List[str],
    ) -> Tuple[str, str, Optional[int], Optional[bool]]:
        if inp.null_variant_type == NULL_WHOLE_GENE_DELETION:
            path.append("Whole-gene deletion? -> Yes.")
            checked.append("Whole-gene deletion removes the entire coding sequence; no location caveat applies.")
            supporting.append("The deletion removes the complete gene.")
            return STRENGTH_VERY_STRONG, "DEL0", None, None

        if transcript is None or inp.deleted_interval is None:
            path.append("Deleted interval mapped onto the transcript? -> No.")
            unchecked.append(
                "Exon-deletion branch: the deleted genomic interval could not be mapped onto the transcript CDS."
            )
            return STRENGTH_NOT_APPLICABLE, "DEL0", None, None

        low, high = min(inp.deleted_interval), max(inp.deleted_interval)
        deleted_coding = 0
        first_lost_codon = None
        last_lost_codon = None
        for span in transcript.coding_spans():
            if span.length == 0:
                continue
            overlap_low, overlap_high = max(span.start, low), min(span.end, high)
            if overlap_low > overlap_high:
                continue
            deleted_coding += overlap_high - overlap_low + 1
            near_edge = overlap_low if transcript.strand > 0 else overlap_high
            far_edge = overlap_high if transcript.strand > 0 else overlap_low
            codon = transcript.codon_at(near_edge)
            if codon is not None and (first_lost_codon is None or codon < first_lost_codon):
                first_lost_codon = codon
            far_codon = transcript.codon_at(far_edge)
            if far_codon is not None and (last_lost_codon is None or far_codon > last_lost_codon):
                last_lost_codon = far_codon

        if deleted_coding == 0:
            path.append("Deletion removes coding sequence? -> No.")
            checked.append("The deleted interval does not overlap any coding exon of this transcript.")
            return STRENGTH_NOT_APPLICABLE, "DEL0", None, None

        path.append(f"Deletion removes {deleted_coding} coding bp starting at codon {first_lost_codon}.")
        preserves_frame = deleted_coding % 3 == 0
        checked.append(
            f"Reading-frame effect: the deletion removes {deleted_coding} coding bp, which "
            f"{'preserves' if preserves_frame else 'disrupts'} the reading frame."
        )

        if relevant is False:
            path.append("Exon(s) present in biologically-relevant transcript(s)? -> No.")
            return STRENGTH_NOT_APPLICABLE, "DEL0", None, None

        if not preserves_frame:
            path.append("Reading frame preserved? -> No (frameshift).")
            nmd = first_lost_codon is not None and transcript.is_nmd_predicted(
                first_lost_codon, inp.nmd_penultimate_window_bp
            )
            checked.append(
                f"NMD prediction: NMD {'IS' if nmd else 'is NOT'} predicted for the frameshifted transcript."
            )
            if nmd:
                path.append("Predicted to undergo NMD? -> Yes.")
                supporting.append(
                    "The out-of-frame deletion produces a transcript predicted to undergo nonsense-mediated decay."
                )
                return STRENGTH_VERY_STRONG, "DEL1", first_lost_codon, True
            path.append("Predicted to undergo NMD? -> No.")
            fraction = transcript.fraction_of_protein_lost(first_lost_codon) if first_lost_codon else None
            strength, code = self._truncation_subtree(
                inp,
                transcript,
                first_lost_codon,
                fraction,
                ("DEL2", "DEL2", "DEL2"),
                path,
                checked,
                unchecked,
                supporting,
                region_label="Truncated region",
            )
            return strength, code, first_lost_codon, False

        path.append("Reading frame preserved? -> Yes (in-frame deletion).")
        removed_fraction = None
        if transcript.total_codons:
            removed_fraction = (deleted_coding / 3.0) / transcript.total_codons
        strength, code = self._truncation_subtree(
            inp,
            transcript,
            first_lost_codon,
            removed_fraction,
            ("DEL3", "DEL3", "DEL3"),
            path,
            checked,
            unchecked,
            supporting,
            region_label="In-frame deleted region",
            last_lost_codon=last_lost_codon,
        )
        return strength, code, first_lost_codon, False

    # ------------------------------------------------------------------
    # Gates applied after the tree
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_frequency_gate(
        inp: PVS1Input,
        strength: str,
        code: str,
        path: List[str],
        checked: List[str],
        unchecked: List[str],
        conflicting: List[str],
        sources: List[str],
    ) -> Tuple[str, str]:
        if strength == STRENGTH_NOT_APPLICABLE:
            return strength, code
        if inp.population_af is None:
            unchecked.append(
                "Population-frequency node ('LoF variants in this exon are frequent in the general "
                "population'): no gnomAD frequency was available for this variant."
            )
            return strength, code
        sources.append("gnomAD")
        label = inp.population_af_label or f"{inp.population_af:.2e}"
        if inp.population_af > inp.lof_population_af_max:
            path.append(
                f"Null variant frequent in the general population? -> Yes (allele frequency {label} > "
                f"{inp.lof_population_af_max:.0e})."
            )
            checked.append(
                f"Population-frequency caveat: this null variant is present at allele frequency {label} in "
                f"gnomAD, above the {inp.lof_population_af_max:.0e} ceiling for a variant claimed to be a "
                "very strong pathogenic null. PVS1 is not applied."
            )
            conflicting.append(f"gnomAD allele frequency {label} is too high for a PVS1-strength null variant.")
            # "FREQ" rather than the tree's own NF4/SS4/SS7 codes: those
            # nodes ask about per-exon LoF frequency and sit only in the
            # NMD-escape branches, whereas this gate is a variant-level
            # approximation applied to every branch. Reporting it under
            # its own code keeps the distinction visible to a reviewer.
            return STRENGTH_NOT_APPLICABLE, "FREQ"
        path.append(f"Null variant frequent in the general population? -> No (allele frequency {label}).")
        checked.append(
            f"Population-frequency caveat: gnomAD allele frequency {label} is below the LoF-tolerance ceiling."
        )
        return strength, code

    @staticmethod
    def _apply_mechanism_gate(
        inp: PVS1Input, mechanism: str, provisional: str, path: List[str], code: str = ""
    ) -> Tuple[bool, str, str, Optional[str]]:
        strength_label = provisional.replace("_", " ")
        # The single decision_path entry that actually determined this
        # leaf/strength (e.g. "Predicted to undergo NMD? -> Yes." for a
        # very_strong nonsense call, or "Removes >10% of the protein?
        # -> No (4.2%)." for a downgraded moderate call) -- named
        # explicitly in the rationale below rather than left for the
        # reader to dig out of `decision_path` themselves. Previously
        # every mechanism-established outcome shared one boilerplate
        # sentence regardless of strength, identical for a very_strong
        # and a strong call apart from the one word "very" -- a
        # reviewer had no way to tell WHY a given finding was
        # downgraded without reading the raw decision_path array, which
        # (see C5) the PDF didn't even render.
        leaf_reason = path[-1].rstrip(".") if path else "no further decision-tree detail was recorded"
        code_clause = f" (ClinGen SVI leaf {code})" if code else ""

        if provisional == STRENGTH_NOT_APPLICABLE:
            return (
                False,
                STRENGTH_NOT_APPLICABLE,
                f"PVS1 does not apply{code_clause}: the decision tree terminated at a 'not applicable' "
                f"leaf -- {leaf_reason}.",
                "Moderate",
            )

        if mechanism in _MECHANISM_ESTABLISHED:
            note = (
                " (loss of function is the established mechanism for this gene's autosomal recessive phenotype)"
                if mechanism == LOF_ESTABLISHED_RECESSIVE
                else ""
            )
            return (
                True,
                provisional,
                f"PVS1 applies at {strength_label} strength{note}{code_clause}. The variant is a "
                "qualifying null variant, loss of function is an established disease mechanism for this "
                f"gene, and the decision tree reached this strength because: {leaf_reason}.",
                "High" if provisional == STRENGTH_VERY_STRONG else "Moderate",
            )

        if mechanism == LOF_REFUTED:
            path.append("Mechanism gate: ClinGen curates this gene as dosage-sensitivity unlikely -> PVS1 withheld.")
            return (
                False,
                STRENGTH_NOT_APPLICABLE,
                "PVS1 is not applied: ClinGen curates this gene as 'dosage sensitivity unlikely', so loss "
                "of function is not an established disease mechanism for it. The decision tree would "
                f"otherwise have supported {strength_label} strength.",
                "Moderate",
            )

        if mechanism == LOF_NOT_ESTABLISHED:
            path.append(
                "Mechanism gate: ClinGen dosage curation does not establish haploinsufficiency -> PVS1 withheld."
            )
            return (
                False,
                STRENGTH_NOT_APPLICABLE,
                "PVS1 is not applied: ClinGen has curated this gene's dosage sensitivity but the evidence "
                "does not establish loss of function as a disease mechanism, which is PVS1's stated "
                f"precondition. The decision tree would otherwise have supported {strength_label} strength.",
                "Moderate",
            )

        path.append("Mechanism gate: no ClinGen dosage curation available -> PVS1 left unevaluated.")
        return (
            False,
            STRENGTH_NOT_APPLICABLE,
            "PVS1 is not applied: no ClinGen dosage-sensitivity curation was available for this gene, so "
            "its stated precondition -- that loss of function is a known mechanism of disease -- could not "
            f"be confirmed. The decision tree would otherwise have supported {strength_label} strength.",
            "Low",
        )
