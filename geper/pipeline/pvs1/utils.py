"""
Adapters between GEPER's existing provider-result dicts and the pure
PVS1 decision tree in `decision_tree.py`.

Nothing here makes a clinical judgement; every function either reshapes
data (Ensembl payload -> `TranscriptContext`) or reads a fact that some
already-integrated provider reported (ClinGen dosage score -> LOF
mechanism state, gnomAD -> allele frequency, InterPro -> functional
regions). Each returns a neutral/unknown value rather than a guess when
its input is missing, skipped or errored -- the decision tree then
records that as an unchecked caveat instead of silently taking a branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from config import CONFIG
from pipeline.pvs1.models import (
    LOF_ESTABLISHED,
    LOF_ESTABLISHED_RECESSIVE,
    LOF_NOT_ESTABLISHED,
    LOF_REFUTED,
    LOF_UNKNOWN,
    ExonSpan,
    NULL_CANONICAL_SPLICE,
    NULL_EXON_DELETION,
    NULL_FRAMESHIFT,
    NULL_INITIATION_CODON,
    NULL_NONSENSE,
    NULL_WHOLE_GENE_DELETION,
    TranscriptContext,
)

# ClinGen dosage-sensitivity scores. Mirrors
# `pipeline/clingen/models.py::DOSAGE_SCORE_LABELS`; re-stated as the
# PVS1-relevant subset so the mapping from "curation score" to "is LOF
# an established disease mechanism?" lives in exactly one place.
_DOSAGE_SUFFICIENT = 3  # "Sufficient evidence for dosage pathogenicity"
_DOSAGE_AUTOSOMAL_RECESSIVE = 30  # "Gene associated with autosomal recessive phenotype"
_DOSAGE_UNLIKELY = 40  # "Dosage sensitivity unlikely"


# ---------------------------------------------------------------------------
# Transcript structure
# ---------------------------------------------------------------------------


def transcript_context_from_ensembl(
    payload: Dict[str, Any],
    gene_symbol: Optional[str] = None,
    source: str = "ensembl_api",
    cds_sequence: Optional[str] = None,
) -> Optional[TranscriptContext]:
    """
    Build a `TranscriptContext` from an Ensembl REST `lookup/id/
    {transcript}?expand=1` response (the endpoint that returns a
    transcript together with its `Exon` list and `Translation` block).

    Returns None for anything that cannot support the PVS1 location
    caveats: a non-coding transcript, a missing exon list, or a missing
    translation. PVS1 is never evaluated off a partial structure.
    """
    if not isinstance(payload, dict):
        return None
    exons = payload.get("Exon") or []
    translation = payload.get("Translation") or {}
    if not exons or not translation.get("start") or not translation.get("end"):
        return None
    try:
        strand = int(payload.get("strand", 1))
        spans = [ExonSpan(start=int(e["start"]), end=int(e["end"]), rank=0) for e in exons]
    except (KeyError, TypeError, ValueError):
        return None

    return TranscriptContext(
        transcript_id=str(payload.get("id") or "unknown"),
        gene_symbol=gene_symbol or payload.get("display_name"),
        chrom=str(payload.get("seq_region_name") or ""),
        strand=1 if strand >= 0 else -1,
        exons=spans,
        cds_genomic_start=int(translation["start"]),
        cds_genomic_end=int(translation["end"]),
        protein_length=translation.get("length"),
        is_mane_select=bool(payload.get("is_mane_select")),
        is_canonical=bool(payload.get("is_canonical")),
        source=source,
        cds_sequence=cds_sequence,
    )


def transcript_context_from_dict(record: Dict[str, Any]) -> Optional[TranscriptContext]:
    """
    Build a `TranscriptContext` from GEPER's own flattened transcript
    dict (what `pipeline/pvs1/lookup.py::TranscriptLookup` returns and
    what the test fixtures store), as opposed to a raw Ensembl payload.
    """
    if not record or not record.get("exons"):
        return None
    try:
        spans = [ExonSpan(start=int(e["start"]), end=int(e["end"]), rank=0) for e in record["exons"]]
        return TranscriptContext(
            transcript_id=str(record.get("transcript_id") or "unknown"),
            gene_symbol=record.get("gene_symbol"),
            chrom=str(record.get("chrom") or ""),
            strand=1 if int(record.get("strand", 1)) >= 0 else -1,
            exons=spans,
            cds_genomic_start=int(record["cds_genomic_start"]),
            cds_genomic_end=int(record["cds_genomic_end"]),
            protein_length=record.get("protein_length"),
            is_mane_select=bool(record.get("is_mane_select")),
            is_canonical=bool(record.get("is_canonical")),
            biologically_relevant=record.get("biologically_relevant"),
            source=str(record.get("source") or "dict"),
            cds_sequence=record.get("cds_sequence"),
        )
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Gene-level LOF mechanism (ClinGen dosage sensitivity)
# ---------------------------------------------------------------------------


def dosage_sensitivity_verdict(
    clingen_result: Optional[Dict[str, Any]],
    *,
    sufficient_scores: Optional[Any] = None,
    autosomal_recessive_score: int = _DOSAGE_AUTOSOMAL_RECESSIVE,
    unlikely_score: int = _DOSAGE_UNLIKELY,
) -> Tuple[str, Optional[int], Optional[str]]:
    """
    Map ClinGen's dosage-sensitivity curation onto PVS1's precondition,
    "a gene where LOF is a known mechanism of disease" -- UNCONDITIONALLY,
    for every score, not just the ones a caller intends to act on.

    HIGH 3 / T3-F2a (2026-08-28, ruled): extracted out of
    `lof_mechanism_from_clingen` below so `InterpretationEngine.
    _clingen_acmg_evidence` (legacy, `pipeline/interpretation.py`) can
    resolve the identical verdict instead of independently re-deriving
    it with its own whitelist that silently produced NO evidence line
    at all for scores 0/1/2 ("curated, but not established") -- the
    missing-versus-empty collapse this ruling names as the HIGH 1
    defect class in the evidence layer: a reviewer seeing nothing
    couldn't tell whether ClinGen looked and found little, or was never
    asked. `lof_mechanism_from_clingen` never had that gap (see its own
    docstring below); this fix is legacy adopting `lof_mechanism_from_
    clingen`'s existing unconditional behavior, not new behavior
    invented here.

    Returns `(mechanism_state, raw_score, raw_label)` -- a pure
    resolution, no sentence-building and no scoring/weighting decision:
    callers phrase their own evidence sentence, and where they score
    evidence at all (legacy does; PVS1 itself does not), decide their
    own weight from `mechanism_state`. `raw_score`/`raw_label` are
    ClinGen's own fields, passed through unchanged so a caller's
    sentence can cite the real curated label rather than a value this
    function invents:
      - score 3  ("sufficient evidence for dosage pathogenicity")
                 -> established
      - score 30 ("gene associated with autosomal recessive phenotype")
                 -> established for the recessive phenotype. ClinGen
                 assigns 30 precisely because haploinsufficiency is not
                 the disease model, not because LOF is not the
                 mechanism; a null allele in such a gene still qualifies
                 for PVS1 when the disorder is biallelic.
      - score 40 ("dosage sensitivity unlikely") -> refuted
      - scores 0/1/2 -> curated, but not established
      - no record / skipped / errored / no dosage curation -> unknown
        (`raw_score` is None only in this case -- it is ClinGen's only
        source of LOF_UNKNOWN, so a caller can tell "never curated"
        apart from every other verdict by checking `raw_score is None`)

    Scores 1 and 2 ("little"/"some evidence") are deliberately *not*
    mapped onto a reduced PVS1 strength: neither ACMG/AMP 2015 nor the
    ClinGen SVI recommendation defines such a mapping, and inventing one
    would put a number on a judgement no guideline makes. They are
    reported as "not established", with the curated label carried
    through so a reviewer can override.

    `sufficient_scores`/`autosomal_recessive_score`/`unlikely_score`
    default to this module's own hardcoded ClinGen codes (PVS1's
    existing, unchanged behavior) -- legacy's caller passes its own
    `CONFIG.clingen`-sourced values instead, so legacy's existing
    "all thresholds/configuration must remain configurable" guarantee
    (`tests/test_clingen_acmg.py::test_threshold_is_configurable`) is
    preserved through this shared function, not lost to it.
    """
    if sufficient_scores is None:
        sufficient_scores = frozenset({_DOSAGE_SUFFICIENT})
    if not clingen_result or clingen_result.get("skipped") or clingen_result.get("error") is not None:
        return LOF_UNKNOWN, None, None
    if not clingen_result.get("found"):
        return LOF_UNKNOWN, None, None

    dosage = clingen_result.get("dosage_sensitivity") or {}
    score = dosage.get("haploinsufficiency_score")
    if score is None:
        return LOF_UNKNOWN, None, None
    label = dosage.get("haploinsufficiency_label") or "n/a"

    if score in sufficient_scores:
        return LOF_ESTABLISHED, score, label
    if score == autosomal_recessive_score:
        return LOF_ESTABLISHED_RECESSIVE, score, label
    if score == unlikely_score:
        return LOF_REFUTED, score, label
    return LOF_NOT_ESTABLISHED, score, label


def lof_mechanism_from_clingen(clingen_result: Optional[Dict[str, Any]]) -> Tuple[str, List[str]]:
    """
    Map ClinGen's dosage-sensitivity curation onto PVS1's precondition,
    "a gene where LOF is a known mechanism of disease", AND build the
    two-line evidence disclosure PVS1's own decision tree carries
    (gene-disease validity, then haploinsufficiency curation).

    Returns `(mechanism_state, evidence_lines)` -- see
    `dosage_sensitivity_verdict` above for the score -> mechanism_state
    mapping this delegates to (using this module's own hardcoded
    ClinGen codes, unchanged from before the refactor); this function's
    own job is only the two sentences and their ordering.
    """
    if not clingen_result or clingen_result.get("skipped") or clingen_result.get("error") is not None:
        return LOF_UNKNOWN, []
    if not clingen_result.get("found"):
        return LOF_UNKNOWN, []

    gene = clingen_result.get("gene_symbol") or "this gene"
    evidence: List[str] = []

    validity = clingen_result.get("clinical_validity_summary")
    if validity:
        evidence.append(f"ClinGen gene-disease validity for {gene}: {validity}.")

    mechanism, score, label = dosage_sensitivity_verdict(clingen_result)
    if score is not None:
        evidence.append(f"ClinGen haploinsufficiency curation for {gene}: {label} (score {score}).")
    return mechanism, evidence


# ---------------------------------------------------------------------------
# Population frequency
# ---------------------------------------------------------------------------


def population_af_from_gnomad(gnomad_result: Optional[Dict[str, Any]]) -> Tuple[Optional[float], Optional[str]]:
    """
    Highest gnomAD allele frequency available for this variant (popmax
    across the reported population breakdown, falling back to global),
    plus a human-readable label. Returns (None, None) when gnomAD was
    skipped/errored -- "not found" is *not* the same as "frequency 0",
    but a variant genuinely absent from gnomAD is reported as 0.0 so the
    frequency caveat can be recorded as checked rather than unchecked.
    """
    # `error` by presence, not truthiness: the 0.0 below is only
    # honest for a variant GENUINELY absent from gnomAD, and a failed
    # lookup whose exception carried no message (error="") used to
    # reach it -- fabricating a frequency for a variant nobody looked up.
    if not gnomad_result or gnomad_result.get("skipped") or gnomad_result.get("error") is not None:
        return None, None
    if not gnomad_result.get("found"):
        return 0.0, "absent from gnomAD"

    popmax = None
    for pop in (gnomad_result.get("population_breakdown") or {}).values():
        af = pop.get("af") if isinstance(pop, dict) else None
        if af is not None and (popmax is None or af > popmax):
            popmax = af
    global_af = gnomad_result.get("global_af")

    if popmax is not None and (global_af is None or popmax >= global_af):
        return popmax, f"{popmax:.2e} (gnomAD popmax)"
    if global_af is not None:
        return global_af, f"{global_af:.2e} (gnomAD global)"
    return None, None


# ---------------------------------------------------------------------------
# Functional regions
# ---------------------------------------------------------------------------

# InterPro entry types that localise a *specific* functional unit, and
# so can support the SVI tree's "truncated region is critical to protein
# function" node. Deliberately excludes "family" and
# "homologous_superfamily": those classify the protein as a whole
# ("this is BRCA2") and are routinely annotated across the full length,
# so treating them as evidence about a particular region would mark
# every truncation in every well-annotated gene as critical. Observed
# directly: the only InterPro entries overlapping BRCA2's last 53
# codons are two whole-protein family entries spanning 1-3418, which
# would otherwise have promoted a ClinVar-Benign last-exon frameshift
# from PVS1_Moderate to PVS1_Strong.
_LOCALISING_INTERPRO_TYPES = ("domain", "active_site", "binding_site", "conserved_site", "ptm")

# An entry covering essentially the whole protein says nothing about
# which part of it matters, whatever its declared type.
_MAX_REGION_COVERAGE_FRACTION = 0.90


def functional_regions_from_interpro(
    interpro_result: Optional[Dict[str, Any]], transcript: Optional[TranscriptContext]
) -> Optional[List[Dict[str, Any]]]:
    """
    InterPro/Pfam spans, in canonical-protein residue coordinates, that
    are specific enough to answer the decision tree's "region critical to
    protein function" node.

    Two filters, both load-bearing (see `_LOCALISING_INTERPRO_TYPES`):
    only region-localising entry types count, and an entry spanning
    almost the entire protein is dropped regardless of type.

    Only returned for a MANE Select / Ensembl canonical transcript:
    InterPro coordinates are relative to UniProt's canonical isoform, and
    comparing them against a non-canonical transcript's codon numbering
    would silently mis-locate every domain. Returning None instead makes
    the tree record the node as unchecked, which is the honest outcome.

    Note this remains a *proxy* for the SVI's intent, which is a
    curated critical region (a documented functional requirement), not
    merely an annotated domain. It can therefore still over-call; the
    tree reports the domain names it matched so a reviewer can judge.
    """
    if not interpro_result or interpro_result.get("skipped") or interpro_result.get("error") is not None:
        return None
    if not interpro_result.get("found"):
        return None
    if transcript is None or not (transcript.is_mane_select or transcript.is_canonical):
        return None

    protein_length = transcript.total_codons
    regions = []
    for domain in interpro_result.get("domains") or []:
        start, end = domain.get("start"), domain.get("end")
        if start is None or end is None:
            continue
        entry_type = (domain.get("type") or domain.get("entry_type") or "").strip().lower()
        if entry_type not in _LOCALISING_INTERPRO_TYPES:
            continue
        if protein_length:
            coverage = (int(end) - int(start) + 1) / protein_length
            if coverage > _MAX_REGION_COVERAGE_FRACTION:
                continue
        regions.append(
            {
                "start": int(start),
                "end": int(end),
                "label": domain.get("name")
                or domain.get("short_name")
                or domain.get("member_accession")
                or "unnamed domain",
            }
        )
    return regions or None


# ---------------------------------------------------------------------------
# Null-variant classification
# ---------------------------------------------------------------------------


def _deleted_interval(pos: int, ref: str, alt: str) -> Optional[Tuple[int, int]]:
    """
    Genomic interval actually removed by a deletion in VCF
    representation (`pos` is the anchor base shared by REF and ALT, so
    the deleted bases start at `pos + 1`). None if this is not a
    net deletion.
    """
    if len(ref) <= len(alt):
        return None
    return pos + 1, pos + len(ref) - 1


_COMPLEMENT = {"A": "T", "T": "A", "G": "C", "C": "G", "N": "N"}

CONSEQUENCE_NONSENSE = "nonsense"
CONSEQUENCE_MISSENSE = "missense"
CONSEQUENCE_SYNONYMOUS = "synonymous"
CONSEQUENCE_STOP_LOST = "stop_lost"


@dataclass(frozen=True)
class CodingConsequenceDetail:
    """
    Full result of calling a substitution's consequence in a
    transcript's real reading frame: the category `coding_consequence`
    alone reports, plus the actual reference/alternate amino acids and
    the codon (residue) number. `pipeline/ps1_pm5/` needs the amino
    acids themselves (to compare against a ClinVar anchor's protein
    change); `pipeline/pvs1/` only ever needed the category, which is
    why `coding_consequence` originally returned just that -- it is
    kept returning only the category for every existing caller, now
    computed as a thin wrapper over this.
    """

    category: str
    codon_number: int
    ref_aa: str  # single-letter amino acid code, or "*" for a stop
    alt_aa: str


def coding_consequence_detail(
    transcript: Optional[TranscriptContext], pos: int, ref: str, alt: str
) -> Optional[CodingConsequenceDetail]:
    """
    Full detail (category + actual amino acids + codon number) of a
    single-nucleotide substitution's consequence, computed in the
    transcript's real reading frame.

    This exists because GEPER's protein stage cannot answer the question.
    `pipeline/protein_translator.py` translates a short flanking window
    starting at the first ATG it happens to find in that window, which is
    almost never the transcript's true frame -- for BRCA1 c.1687C>T
    (p.Gln563Ter, ClinVar Pathogenic by expert panel) it returns the
    identical out-of-frame peptide for both ref and alt, so a textbook
    nonsense variant reads as "no protein change" and PVS1 silently
    declines to fire. Calling the codon directly from the CDS removes
    that failure mode.

    Returns None -- never a guess -- when this is not a substitution,
    when no CDS sequence was fetched, when the position is not in the
    CDS, or when the reference base at that codon offset disagrees with
    the variant's REF allele (which means the transcript and the variant
    are not describing the same locus/build, and no consequence should be
    inferred from the mismatch).
    """
    if transcript is None or not transcript.cds_sequence:
        return None
    if len(ref) != 1 or len(alt) != 1:
        return None

    cds_pos = transcript.cds_position(pos)
    if cds_pos is None:
        return None

    codon_index, offset = (cds_pos - 1) // 3, (cds_pos - 1) % 3
    codon = transcript.cds_sequence[codon_index * 3 : codon_index * 3 + 3].upper()
    if len(codon) != 3:
        return None

    # VCF REF/ALT are on the forward genomic strand; the CDS is in
    # transcript orientation, so a minus-strand transcript needs both
    # complemented before they can be compared or substituted.
    ref_base, alt_base = ref.upper(), alt.upper()
    if transcript.strand < 0:
        ref_base, alt_base = _COMPLEMENT.get(ref_base, ref_base), _COMPLEMENT.get(alt_base, alt_base)

    if codon[offset] != ref_base:
        return None  # build/transcript mismatch -- do not infer a consequence

    mutated = codon[:offset] + alt_base + codon[offset + 1 :]
    table = CONFIG.CODON_TABLE  # RNA-keyed; reuse the project's single codon table
    ref_aa = table.get(codon.replace("T", "U"))
    alt_aa = table.get(mutated.replace("T", "U"))
    if ref_aa is None or alt_aa is None:
        return None

    if ref_aa == alt_aa:
        category = CONSEQUENCE_SYNONYMOUS
    elif alt_aa == "*":
        category = CONSEQUENCE_NONSENSE
    elif ref_aa == "*":
        category = CONSEQUENCE_STOP_LOST
    else:
        category = CONSEQUENCE_MISSENSE

    return CodingConsequenceDetail(category=category, codon_number=codon_index + 1, ref_aa=ref_aa, alt_aa=alt_aa)


def coding_consequence(transcript: Optional[TranscriptContext], pos: int, ref: str, alt: str) -> Optional[str]:
    """Amino-acid consequence *category* of a substitution -- see `coding_consequence_detail` for the full result this wraps."""
    detail = coding_consequence_detail(transcript, pos, ref, alt)
    return detail.category if detail else None


def alphamissense_evidence_sentence(alphamissense_result: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    Full evidence sentence for an AlphaMissense call. HIGH 3 / AM-07
    (2026-08-28, ruled): shared between `InterpretationEngine.
    interpret()` (legacy) and `ACMGRuleEngine._pp3_bp4` (real ACMG),
    which independently worded the same fact until this fix -- both
    call sites already carried the "not clinically validated; not
    approved for clinical use" caveat (an earlier, per-site fix), but
    kept two different sentences for it, differing only in surface
    formatting.

    Keeps legacy's richer format: the `protein_variant` clause, and a
    `.3f`-formatted score with an explicit "n/a" fallback for a missing/
    non-numeric `am_pathogenicity`. The ruling names real ACMG's bare,
    un-fallback'd print of that field a latent None-formatting defect
    this unification fixes, not a house-style choice to pick between.

    Why the caveat exists at all (moved here from `acmg_rules.py`'s
    former per-site comment, now the single source of this wording):
    AlphaMissense's own README (github.com/google-deepmind/
    alphamissense) states plainly that it is "not approved for clinical
    use", "not intended to be a substitute for professional medical
    advice", and that "predictions have varying levels of confidence"
    -- the same class of limitation `ACMGRuleEngine._bp7` already
    discloses for the splice models, and nothing about AlphaMissense's
    own documentation exempts it. This is a distinct claim from
    "uncalibrated": AlphaMissense's score is reported as calibrated
    against a curated benchmark in Cheng et al. 2023 -- the caveat here
    is about clinical-use approval status, not about calibration, so it
    is worded differently from `_bp7`'s "uncalibrated" phrasing rather
    than copied verbatim. The model name is bound ahead of the score,
    not trailing, so a reader sees the limitation before the number and
    PDF line-wrapping cannot split it across a break.

    Returns None (no sentence) when AlphaMissense was skipped, errored,
    or found nothing -- callers are expected to gate on that themselves
    before deciding whether/where to use the sentence (e.g. real ACMG
    still needs `am_class` itself to route the sentence into its own
    damaging/benign evidence lists), so this restates the same gate
    defensively rather than assuming every caller checked first.
    """
    if not alphamissense_result or alphamissense_result.get("skipped") or not alphamissense_result.get("found"):
        return None
    am_class = (alphamissense_result.get("am_class") or "").strip().lower()
    am_score = alphamissense_result.get("am_pathogenicity")
    am_score_str = f"{am_score:.3f}" if isinstance(am_score, (int, float)) else "n/a"
    return (
        f"AlphaMissense (not clinically validated; not approved for clinical use) predicts "
        f"'{am_class or 'unclassified'}' (am_pathogenicity={am_score_str}) for "
        f"{alphamissense_result.get('protein_variant', 'this substitution')}. This is a raw model "
        f"score, not a validated clinical pathogenicity measure."
    )


def null_variant_term(is_frameshift: bool) -> str:
    """
    Canonical two-word term for a protein-truncating null consequence.
    HIGH 3 / T3-F3 (2026-08-28, ruled): shared by `classify_null_variant`
    below, `pipeline/interpretation.py`'s legacy evidence line, and
    `pipeline/acmg_rules.py::ACMGRuleEngine._pp3_bp4_inapplicability_reason`
    -- all three independently reworded the same frameshift-vs-nonsense
    fact. Both halves of the gloss are kept deliberately (not
    consolidated to one): "stop-gained" is the term a reader may
    recognize from VEP output, "nonsense" is the clinical term. This
    function makes only that wording call; each caller keeps its own
    surrounding sentence template.
    """
    return "frameshift" if is_frameshift else "nonsense (stop-gained)"


def classify_null_variant(
    variant_dict: Optional[Dict[str, Any]],
    protein_result: Optional[Dict[str, Any]] = None,
    transcript: Optional[TranscriptContext] = None,
) -> Tuple[Optional[str], List[str]]:
    """
    Classify a variant into one of PVS1's qualifying null classes, or
    None if it is not a null variant.

    Order matters and follows the ACMG/AMP definition's own ordering of
    evidence: a position-based class that the transcript structure can
    prove (canonical splice site, initiation codon, exon deletion) wins
    over one inferred from a local protein translation, because the
    former is derived from annotated coordinates while the latter comes
    from `pipeline/protein_translator.py`'s short sequence window.

    Returns `(null_type, notes)`; `notes` records how the call was made.
    """
    notes: List[str] = []
    if not variant_dict:
        return None, notes

    pos = variant_dict.get("pos")
    ref = (variant_dict.get("ref") or "").upper()
    alt = (variant_dict.get("alt") or "").upper()

    if transcript is not None and pos is not None:
        deleted = _deleted_interval(int(pos), ref, alt)
        if deleted is not None:
            low, high = deleted
            coding_spans = [s for s in transcript.coding_spans() if s.length > 0]
            fully_removed = [s for s in coding_spans if low <= s.start and s.end <= high]
            if coding_spans and len(fully_removed) == len(coding_spans):
                notes.append("Deletion removes every coding exon of the transcript.")
                return NULL_WHOLE_GENE_DELETION, notes
            if fully_removed:
                notes.append(
                    f"Deletion fully removes {len(fully_removed)} coding exon(s) of {transcript.transcript_id}."
                )
                return NULL_EXON_DELETION, notes

        site = transcript.splice_site_at(int(pos))
        if site is not None:
            exon_rank, side, offset = site
            notes.append(f"Position is the canonical {side} +-{offset} site of exon {exon_rank}.")
            return NULL_CANONICAL_SPLICE, notes

        if transcript.is_initiation_codon(int(pos)):
            notes.append("Position falls within the initiation codon (c.1-c.3).")
            return NULL_INITIATION_CODON, notes

    # Indel length settles frameshift vs in-frame without needing a
    # translation window at all.
    if ref and alt and len(ref) != len(alt):
        if (len(alt) - len(ref)) % 3 != 0:
            notes.append(f"Indel changes coding length by {len(alt) - len(ref)} bp (not a multiple of 3): frameshift.")
            return NULL_FRAMESHIFT, notes
        notes.append(
            f"Indel changes coding length by {len(alt) - len(ref)} bp (a multiple of 3): in-frame, not a PVS1 null class."
        )
        return None, notes

    # Substitutions: call the consequence in the transcript's real
    # reading frame when the CDS is available, and only fall back to the
    # local translation window when it is not (see
    # `coding_consequence` for why the window alone is not sufficient).
    if transcript is not None and pos is not None:
        consequence = coding_consequence(transcript, int(pos), ref, alt)
        if consequence is not None:
            notes.append(f"CDS-frame consequence at c.{transcript.cds_position(int(pos))}: {consequence}.")
            if consequence == CONSEQUENCE_NONSENSE:
                return NULL_NONSENSE, notes
            return None, notes

    nonsense, frameshift = _protein_null_flags(protein_result)
    if nonsense:
        notes.append(f"Local protein translation shows {null_variant_term(is_frameshift=False)}.")
        return NULL_NONSENSE, notes
    if frameshift:
        notes.append(f"Local protein translation shows {null_variant_term(is_frameshift=True)}.")
        return NULL_FRAMESHIFT, notes
    return None, notes


PM4_IN_FRAME_INDEL = "in_frame_indel"
PM4_STOP_LOSS = "stop_loss"


@dataclass(frozen=True)
class PM4VariantDetail:
    """
    Result of `classify_pm4_variant`: which of PM4's two qualifying
    classes this variant is, and enough detail for
    `pipeline/acmg_rules.py::ACMGRuleEngine._pm4` to build its
    rationale and check the affected codon against a repeat-region
    annotation. Not PVS1-specific despite living alongside
    `classify_null_variant` -- see that function's own precedent (this
    module already hosts `coding_consequence_detail`, used by
    `pipeline/ps1_pm5/` as much as by PVS1) for why shared
    coding-consequence classification lives here rather than being
    duplicated per rule.
    """

    category: str  # PM4_IN_FRAME_INDEL | PM4_STOP_LOSS
    codon_number: Optional[int]  # first affected codon (indel) or the stop codon's own number (stop-loss)
    residues_changed: Optional[
        int
    ]  # |length_delta| // 3 for an indel; None for stop-loss (see classify_pm4_variant's docstring)


def classify_pm4_variant(
    variant_dict: Optional[Dict[str, Any]], transcript: Optional[TranscriptContext]
) -> Tuple[Optional[PM4VariantDetail], List[str]]:
    """
    Classify a variant into one of PM4's two qualifying classes -- an
    in-frame insertion/deletion, or a stop-loss substitution -- or
    `(None, notes)` if it is neither.

    In-frame vs. frameshift is pure REF/ALT length arithmetic (the
    same check `classify_null_variant` uses for PVS1's frameshift
    class) and needs no transcript at all for that part; the codon
    number reported alongside it does need one. Stop-loss needs the
    transcript's CDS to confirm the substituted codon is genuinely the
    reference stop codon -- reuses `coding_consequence_detail`, the
    same CDS-frame codon caller PVS1/PS1/PM5 already rely on instead of
    the frame-unaware local protein-translation window (see that
    function's own docstring for why the window is unusable here).

    KNOWN GAP, stated rather than silently guessed: an indel that
    deletes the reference stop codon outright (rather than a
    single-nucleotide substitution changing it to a sense codon) is
    classified as an in-frame/frameshift indel by the length check
    above, not as stop-loss -- and the number of extra residues a
    stop-loss extension adds before a new downstream stop is reached
    is never computed. Both would require 3' UTR sequence this
    pipeline does not fetch (`TranscriptContext.cds_sequence` covers
    the CDS only). The caller (`ACMGRuleEngine._pm4`) surfaces this as
    an explicit unchecked caveat rather than silently omitting it.
    """
    notes: List[str] = []
    if not variant_dict or transcript is None:
        return None, notes
    pos = variant_dict.get("pos")
    ref = (variant_dict.get("ref") or "").upper()
    alt = (variant_dict.get("alt") or "").upper()
    if pos is None or not ref or not alt:
        return None, notes

    if len(ref) != len(alt):
        length_delta = len(alt) - len(ref)
        if length_delta % 3 != 0:
            notes.append(
                f"Indel changes coding length by {length_delta} bp (not a multiple of 3): frameshift, "
                "not an in-frame indel."
            )
            return None, notes
        codon_number = transcript.first_affected_codon(int(pos), ref, alt)
        notes.append(
            f"Indel changes coding length by {length_delta} bp (a multiple of 3, {abs(length_delta) // 3} "
            f"residue(s)): in-frame {'insertion' if length_delta > 0 else 'deletion'}."
        )
        return PM4VariantDetail(PM4_IN_FRAME_INDEL, codon_number, abs(length_delta) // 3), notes

    detail = coding_consequence_detail(transcript, int(pos), ref, alt)
    if detail is not None and detail.category == CONSEQUENCE_STOP_LOST:
        notes.append(
            f"Substitution at codon {detail.codon_number} converts the reference stop codon to "
            f"'{detail.alt_aa}': stop-loss."
        )
        return PM4VariantDetail(PM4_STOP_LOSS, detail.codon_number, None), notes

    return None, notes


@dataclass(frozen=True)
class ProteinEffectFlags:
    """
    Transcript-CDS-frame classification of a variant's protein-level
    effect: `is_lof` (nonsense or frameshift), `is_inframe_indel`,
    `is_synonymous`, `is_missense`. Exactly one is True when
    `determined` is True; all four are False when `determined` is
    False, which callers must check before trusting any of them.

    This is the transcript-aware replacement for
    `pipeline/acmg_rules.py::ACMGRuleEngine._protein_effect_flags`'s
    old use of `pipeline/protein_translator.py`'s frame-unaware local
    translation window -- see `coding_consequence_detail`'s docstring
    for why that window is unusable for this. Both BP7 (benign,
    synonymous-only) and BP1 (benign, missense-only) fed directly off
    that window before this existed: for a variant far from the
    spurious in-window AUG the window happens to find, the "protein"
    it translates is in an essentially arbitrary reading frame, so
    "ref == alt" (synonymous) or "differs by one residue" (missense)
    were both close to coin flips rather than real calls -- exactly
    how a textbook missense variant (e.g. PRNP P102L) could read as
    synonymous and pick up BP7's benign points.
    """

    is_lof: bool
    is_inframe_indel: bool
    is_synonymous: bool
    is_missense: bool
    determined: bool


def protein_effect_flags(
    variant_dict: Optional[Dict[str, Any]], transcript: Optional[TranscriptContext]
) -> ProteinEffectFlags:
    """
    Classify a variant's protein-level effect using only transcript-
    verified data: REF/ALT length parity for indels (frameshift vs.
    in-frame -- needs no CDS sequence, exactly like
    `classify_null_variant`'s and `classify_pm4_variant`'s identical
    arithmetic), and `coding_consequence_detail`'s CDS-frame codon call
    for single-nucleotide substitutions.

    `determined` is False -- and every flag False with it -- whenever
    no reliable transcript-based call was possible: no variant
    coordinates, no CDS sequence fetched for this transcript, a
    build/transcript mismatch at the variant's codon, or a
    multi-nucleotide substitution (`coding_consequence_detail` only
    calls single-base substitutions; a same-length substitution longer
    than 1 base returns undetermined rather than a guess). Callers must
    treat `determined=False` as "not evaluated", never as a negative
    finding.
    """
    if not variant_dict:
        return ProteinEffectFlags(False, False, False, False, False)

    pos = variant_dict.get("pos")
    ref = (variant_dict.get("ref") or "").upper()
    alt = (variant_dict.get("alt") or "").upper()
    if pos is None or not ref or not alt:
        return ProteinEffectFlags(False, False, False, False, False)

    if len(ref) != len(alt):
        length_delta = len(alt) - len(ref)
        if length_delta % 3 != 0:
            return ProteinEffectFlags(True, False, False, False, True)
        return ProteinEffectFlags(False, True, False, False, True)

    if len(ref) != 1:
        # Multi-nucleotide same-length substitution: not covered by
        # `coding_consequence_detail` (single-base only).
        return ProteinEffectFlags(False, False, False, False, False)

    detail = coding_consequence_detail(transcript, int(pos), ref, alt)
    if detail is None:
        return ProteinEffectFlags(False, False, False, False, False)

    if detail.category == CONSEQUENCE_SYNONYMOUS:
        return ProteinEffectFlags(False, False, True, False, True)
    if detail.category == CONSEQUENCE_NONSENSE:
        return ProteinEffectFlags(True, False, False, False, True)
    if detail.category == CONSEQUENCE_MISSENSE:
        return ProteinEffectFlags(False, False, False, True, True)
    # CONSEQUENCE_STOP_LOST: none of the four classes apply (matches
    # the old window-translation semantics, which never classified
    # stop-loss as any of BP7/BP1's or PVS1's classes -- PM4 handles
    # stop-loss on its own via `classify_pm4_variant`).
    return ProteinEffectFlags(False, False, False, False, True)


def protein_effect_undetermined_reason(
    variant_dict: Optional[Dict[str, Any]], transcript_result: Optional[Dict[str, Any]]
) -> str:
    """
    Human-readable, specific reason `protein_effect_flags` returned
    `determined=False` for this exact variant/transcript pair --
    replaces the old undifferentiated catch-all ("no CDS sequence was
    available for this variant/transcript, or this is a multi-
    nucleotide substitution") that every one of BP7/BP1/the
    `InterpretationEngine` protein-consequence line used to share
    verbatim regardless of which of several genuinely different causes
    actually applied. A reviewer reading "the reference allele does not
    match the transcript's coding sequence at this position" needs to
    do something very different from a reviewer reading "this position
    is intronic" or "transcript lookup was skipped this run" -- lumping
    them together hid that distinction.

    Only call this once `protein_effect_flags(...).determined` is
    already known False; it re-walks the same decision path
    `coding_consequence_detail` takes (rather than having that function
    itself carry a reason out, to keep its contract -- return the call
    or `None`, nothing else -- unchanged for every existing caller).
    """
    if not variant_dict:
        return "no variant coordinates were available for this analysis."
    pos = variant_dict.get("pos")
    ref = (variant_dict.get("ref") or "").upper()
    alt = (variant_dict.get("alt") or "").upper()
    if pos is None or not ref or not alt:
        return "the variant's position, reference allele, or alternate allele was missing."
    if len(ref) != len(alt):
        # protein_effect_flags always determines length-changing
        # (indel) variants via REF/ALT length parity alone -- it never
        # reaches here undetermined for one, so this branch is
        # unreachable in practice, but kept as an honest fallback
        # rather than falling through to the multi-nucleotide message
        # below, which would misdescribe an indel as a substitution.
        return "this variant's protein-level consequence could not be classified from REF/ALT length alone."
    if len(ref) != 1:
        return (
            f"this is a {len(ref)}-base, same-length (multi-nucleotide) substitution, which is not "
            "evaluated for a single-codon consequence call."
        )

    # Single-nucleotide substitution from here on -- walk the same
    # transcript/CDS resolution `coding_consequence_detail` takes.
    if not transcript_result or transcript_result.get("skipped"):
        return "transcript structure lookup was skipped for this run."
    # POSITIVE-POLARITY TRUTHINESS (2026-08-31, sweep Batch 4): `if
    # transcript_result.get("error"):` missed an empty-but-present
    # error, falling through to "no transcript structure was found" --
    # a confirmed negative about the GENE, not the failure of the
    # LOOKUP this branch exists to report (Phase 1's original finding
    # on this exact site). Safe today because orchestrator.py's
    # transcript-structure stage (fixed in Batch 1) never emits an
    # empty string; fixed here anyway so the class cannot reopen if
    # that producer regresses.
    if transcript_result.get("error") is not None:
        return f"transcript structure lookup failed: {transcript_result.get('error')}."
    if not transcript_result.get("found"):
        return "no transcript structure was found for this gene/genome build."
    if transcript_result.get("variant_outside_transcript"):
        return "this variant's genomic position falls outside the resolved transcript's boundaries."

    transcript = transcript_context_from_dict(transcript_result.get("transcript") or {})
    if transcript is None:
        return "the transcript structure record could not be parsed (missing exon/CDS coordinates)."
    if not transcript.cds_sequence:
        return "the resolved transcript has no coding-sequence (CDS) data fetched."

    cds_pos = transcript.cds_position(int(pos))
    if cds_pos is None:
        return (
            "this position does not map into the resolved transcript's coding sequence -- it may be "
            "intronic, in an untranslated region (UTR), or outside this transcript's exons."
        )

    codon_index, offset = (cds_pos - 1) // 3, (cds_pos - 1) % 3
    codon = transcript.cds_sequence[codon_index * 3 : codon_index * 3 + 3].upper()
    if len(codon) != 3:
        return (
            "the affected codon falls at the edge of the transcript's coding sequence and could not be fully resolved."
        )

    ref_base = ref
    if transcript.strand < 0:
        ref_base = _COMPLEMENT.get(ref_base, ref_base)
    if codon[offset] != ref_base:
        return (
            f"the reference allele ('{ref}') does not match the base ('{codon[offset]}') at this position "
            "in the resolved transcript's coding sequence -- this indicates a genome-build or "
            "transcript-coordinate mismatch, not a real substitution at this codon."
        )

    return "the affected codon could not be translated (an unexpected/ambiguous base was present)."


def _protein_null_flags(protein_result: Optional[Dict[str, Any]]) -> Tuple[bool, bool]:
    """
    (is_nonsense, is_frameshift) from GEPER's local protein translation.
    Same source and shape as `pipeline/acmg_rules.py::
    ACMGRuleEngine._protein_effect_flags`, but keeps the two classes
    distinct -- PVS1's tree treats them identically, yet the reported
    variant class must still name the right one.
    """
    if not protein_result or protein_result.get("skipped"):
        return False, False
    translation = protein_result.get("translation") or {}
    ref_p, alt_p = translation.get("ref_protein"), translation.get("alt_protein")
    if not ref_p or not alt_p or ref_p == alt_p:
        return False, False
    if alt_p.endswith("*") and not ref_p.endswith("*"):
        return True, False
    if len(ref_p) != len(alt_p) and abs(len(alt_p) - len(ref_p)) % 3 != 0:
        return False, True
    return False, False


def transcript_from_result(transcript_result: Optional[Dict[str, Any]]) -> Optional[TranscriptContext]:
    """
    Extract a usable `TranscriptContext` from the dict
    `pipeline/pvs1/lookup.py::TranscriptLookup` returns, or None when the
    lookup was skipped/errored/not found, or when the variant was flagged
    as falling outside the transcript that was fetched.
    """
    if not transcript_result or transcript_result.get("skipped") or transcript_result.get("error") is not None:
        return None
    if not transcript_result.get("found") or transcript_result.get("variant_outside_transcript"):
        return None
    return transcript_context_from_dict(transcript_result.get("transcript") or {})


def canonical_protein_position(transcript_result: Optional[Dict[str, Any]], pos: int) -> Optional[int]:
    """
    1-based, transcript-verified canonical protein residue number for
    a genomic position -- what InterPro/Pfam's domain-overlap check
    (PM1, `ACMGRuleEngine._pm1`) and AlphaFold's per-residue pLDDT
    lookup need to compare against UniProt's own canonical-isoform
    coordinates.

    Reuses the same `TranscriptContext` PVS1/PM4/PS1/PM5 already build
    from `_run_transcript_stage` (`transcript_from_result`, above)
    rather than a second lookup. `TranscriptContext.codon_at` maps a
    genomic coordinate straight from the transcript's real, per-exon
    `coding_spans` -- correctly strand-aware (it complements the
    offset direction for minus-strand transcripts) and splice-aware
    (each exon contributes its own CDS span; an intron between them is
    never treated as coding) -- unlike the
    `pipeline/protein_translator.py` window this replaced (as PM1's
    position source; that translator is still used as-is for ESM-2's
    ref/alt protein sequences, which don't need canonical numbering),
    which translated a flat, unspliced +-500bp genomic window starting
    at the first "AUG" substring it happened to find. That approach
    was verified (2026-07-31, see test_data/README.md in the repo
    root's `geper/` package) to produce a position matching neither
    strand nor frame for real variants: BRCA1/TP53 are both
    minus-strand genes it never reverse-complemented, and any variant
    not in the same exon as the annotated CDS start had its window
    cross an unspliced intron.

    Returns `None` -- never a guess -- when: no transcript structure
    was fetched (`transcript_from_result` is None: lookup
    skipped/errored/not-found, or the variant falls outside the
    fetched transcript's span); the resolved transcript is neither
    MANE Select nor Ensembl-canonical (matching
    `functional_regions_from_interpro`'s identical guard, above --
    InterPro/UniProt coordinates are only meaningful against the
    canonical isoform, so a position from any other transcript would
    silently mis-locate every domain); or the position is
    intronic/UTR (outside the CDS entirely -- `codon_at` itself
    returns None in that case).
    """
    transcript = transcript_from_result(transcript_result)
    if transcript is None:
        return None
    if not (transcript.is_mane_select or transcript.is_canonical):
        return None
    return transcript.codon_at(pos)


def build_pvs1_input(
    variant_dict: Optional[Dict[str, Any]] = None,
    protein_result: Optional[Dict[str, Any]] = None,
    clingen_result: Optional[Dict[str, Any]] = None,
    gnomad_result: Optional[Dict[str, Any]] = None,
    interpro_result: Optional[Dict[str, Any]] = None,
    transcript_result: Optional[Dict[str, Any]] = None,
    mmsplice_result: Optional[Dict[str, Any]] = None,
    config: Any = None,
):
    """
    Assemble a `PVS1Input` from the provider-result dicts the
    orchestrator already collects. Imported lazily inside the function
    body to keep `utils` free of a circular import back into
    `decision_tree`, which imports `models` from here's own package.
    """
    from pipeline.pvs1.decision_tree import PVS1Input  # local import: see docstring

    transcript = transcript_from_result(transcript_result)
    null_type, classification_notes = classify_null_variant(variant_dict, protein_result, transcript)
    observed_consequence = None
    if null_type is None:
        # Not a qualifying null class -- name the real transcript-
        # verified consequence GEPER determined (if any), rather than
        # leaving the NF0 rationale to fall back to "undetermined" for
        # a variant whose consequence is, in fact, known (D2, report
        # review round 3). Same classification
        # `pipeline/interpretation.py`'s "Transcript-verified protein
        # consequence" evidence line already uses.
        flags = protein_effect_flags(variant_dict, transcript)
        if flags.determined:
            if flags.is_missense:
                observed_consequence = "missense (amino acid substitution)"
            elif flags.is_synonymous:
                observed_consequence = "synonymous (no amino acid change)"
            elif flags.is_inframe_indel:
                observed_consequence = "in-frame insertion/deletion"
    mechanism, mechanism_evidence = lof_mechanism_from_clingen(clingen_result)
    population_af, af_label = population_af_from_gnomad(gnomad_result)
    functional_regions = functional_regions_from_interpro(interpro_result, transcript)

    pos = (variant_dict or {}).get("pos")
    ref, alt = (variant_dict or {}).get("ref") or "", (variant_dict or {}).get("alt") or ""
    deleted_interval = _deleted_interval(int(pos), ref, alt) if pos is not None else None

    kwargs: Dict[str, Any] = dict(
        null_variant_type=null_type,
        observed_consequence=observed_consequence,
        transcript=transcript,
        pos=int(pos) if pos is not None else None,
        lof_mechanism=mechanism,
        lof_mechanism_evidence=mechanism_evidence + classification_notes,
        population_af=population_af,
        population_af_label=af_label,
        functional_regions=functional_regions,
        splice_frame_consequence=_splice_frame_consequence(mmsplice_result),
        deleted_interval=deleted_interval,
    )
    if config is not None:
        kwargs.update(
            nmd_penultimate_window_bp=config.NMD_PENULTIMATE_WINDOW_BP,
            protein_loss_strong_fraction=config.PROTEIN_LOSS_STRONG_FRACTION,
            lof_population_af_max=config.LOF_POPULATION_AF_MAX,
            nmd_uncertainty_margin_codons=config.NMD_UNCERTAINTY_MARGIN_CODONS,
            initiation_codon_max_strength=config.INITIATION_CODON_MAX_STRENGTH,
        )
    return PVS1Input(**kwargs)


def _splice_frame_consequence(mmsplice_result: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    Whether a splice predictor established that the variant's splice
    consequence keeps or breaks the reading frame.

    MMSplice reports the *magnitude* of a splicing change, not which
    exon is skipped or whether the resulting mRNA stays in frame, so it
    cannot answer this node -- returning None makes the decision tree
    fall back to modelling whole-exon skipping from the transcript
    structure, and say so. This hook exists so a predictor that does
    report the resulting transcript (e.g. a future SpliceAI/Pangolin +
    cryptic-site analysis) can be wired in without touching the tree.
    """
    return None


def termination_codon_from_hgvs_p(hgvs_p: Optional[str]) -> Optional[int]:
    """
    Codon number of the new stop from an HGVS.p string, when a caller
    has one (e.g. `p.Gln563Ter`, `p.Gln563*`, `p.Ser3366fs*12`). The
    frameshift form reports the residue at which the frame shifts plus
    the offset to the new stop; both are combined here so NMD is judged
    against the actual termination site rather than the frameshift's
    start. Returns None when the string is absent or unparseable --
    never a guess.
    """
    if not hgvs_p:
        return None
    import re

    text = hgvs_p.strip()
    fs_match = re.search(r"p\.\D+(\d+)\D*fs(?:\D*?(\d+))?", text)
    if fs_match:
        start = int(fs_match.group(1))
        offset = fs_match.group(2)
        return start + int(offset) - 1 if offset else start
    stop_match = re.search(r"p\.\D+(\d+)(?:Ter|\*|X)", text)
    if stop_match:
        return int(stop_match.group(1))
    return None
