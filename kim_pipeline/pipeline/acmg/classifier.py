"""
pipeline/acmg/classifier.py
─────────────────────────────
ACMG/AMP 2015 variant pathogenicity classification engine.

Implements the 28 evidence criteria from:
  Richards et al. (2015) Genetics in Medicine 17:405–424
  doi:10.1038/gim.2015.30

Classification tiers (in order of decreasing pathogenicity):
  Pathogenic (P)          PVS1 + ≥1 Strong, or ≥2 Strong, or ...
  Likely Pathogenic (LP)  1 Strong + 1-2 Moderate, or ≥3 Moderate, or ...
  Uncertain Significance  Does not meet P/LP/LB/B thresholds
  Likely Benign (LB)      BS1 + BP1, or ≥2 BP, or ...
  Benign (B)              Stand-alone BA1, or ≥2 Strong Benign, or ...

Evidence strengths:
  Pathogenic:  PVS (Very Strong), PS (Strong), PM (Moderate), PP (Supporting)
  Benign:      BA (Stand-alone), BS (Strong), BP (Supporting)

This module only evaluates criteria from data passed in — it does NOT
make live network calls. Callers supply ClinVar hits, gnomAD AF, in-silico
scores, etc. from their own lookup modules. This keeps the classifier
pure and testable without any external dependencies.

Usage::

    from pipeline.acmg.classifier import AcmgClassifier, VariantEvidence
    from pipeline.acmg.criteria import AcmgCriteria

    evidence = VariantEvidence(
        chrom="chr17", pos=43057051, ref="A", alt="T",
        gene="BRCA1",
        gnomad_af=0.000001,
        cadd_phred=38.5,
        revel_score=0.94,
        clinvar_significance="Pathogenic",
        is_lof=False,
        lof_gene_intolerant=True,
        in_hotspot=True,
    )
    clf = AcmgClassifier(cfg={"acmg_thresholds": {...}})
    result = clf.classify(evidence)
    print(result.classification)   # "Pathogenic"
    print(result.criteria_met)     # ["PM2", "PP3", "PP5"]
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field

logger = logging.getLogger("geper.pipeline.acmg.classifier")


# ─── Evidence input ───────────────────────────────────────────────────────────


@dataclass
class VariantEvidence:
    """All evidence fields needed to evaluate ACMG/AMP criteria.

    Every field is Optional — pass None for any evidence that was not
    available or not computed. Criteria that require a missing field
    are simply not evaluated (not counted as met or not-met).
    """

    # Variant identity
    chrom: str = ""
    pos: int = 0
    ref: str = ""
    alt: str = ""
    gene: str | None = None
    transcript_id: str | None = None

    # Population frequency
    gnomad_af: float | None = None  # gnomAD allele frequency
    gnomad_af_popmax: float | None = None  # max AF across any subpopulation
    # True when gnomAD lookup confirmed the variant is genuinely absent.
    # None/False means the lookup was unavailable — PM2 must NOT fire in that case.
    gnomad_af_absent: bool | None = None

    # In-silico predictors
    cadd_phred: float | None = None
    revel_score: float | None = None  # 0–1; higher = more damaging
    spliceai_score: float | None = None  # 0–1; higher = splice disrupting
    alphamissense_score: float | None = None  # 0–1; higher = more pathogenic

    # Functional / molecular evidence
    is_lof: bool = False  # stop-gain, frameshift, splice±1/2, start-loss
    is_inframe_indel: bool = False  # in-frame insertion/deletion (length change % 3 == 0, not LoF)
    lof_gene_intolerant: bool = False  # pLI > 0.9 or haploinsufficiency known
    is_missense: bool = False
    # True/False when hotspot status is known (checked against ClinVar TSV /
    # UniProt domains BED). None means the hotspot lookup was unavailable or
    # failed — PM1 must NOT read that as a confirmed "not a hotspot".
    in_hotspot: bool | None = None
    functional_study_damaging: bool | None = None  # in vitro / in vivo functional assay
    functional_study_benign: bool | None = None

    # Segregation / de novo
    confirmed_de_novo: bool | None = None  # confirmed with both parents
    assumed_de_novo: bool | None = None  # de novo not confirmed with parents
    segregates_with_disease: bool | None = None
    segregates_away_from_disease: bool | None = None
    # PP4: patient phenotype highly specific for single-gene disease
    phenotype_specific_for_gene: bool | None = None

    # BP3: in-frame indel in repeat region without known function
    in_repeat_region: bool | None = None
    # Inheritance pattern of the gene ("AD"/"AR"/"XL"/None)
    inheritance_pattern: str | None = None
    # Variant observed in trans with a known pathogenic variant (recessive context)
    in_trans_with_pathogenic: bool | None = None
    # Variant in trans with pathogenic (dominant) or in cis with pathogenic — unexpected
    in_trans_or_cis_with_pathogenic_unexpected: bool | None = None
    # An alternate molecular basis fully explains the patient's phenotype
    alternate_molecular_basis_found: bool | None = None
    # PM5: novel missense change at a codon where a DIFFERENT pathogenic missense is known.
    # Distinct from same_aa_pathogenic (PS1): here it is a DIFFERENT amino-acid substitution.
    # When novel_aa_at_known_pathogenic_codon=True and is_missense=True → PM5 (not PS1).
    novel_aa_at_known_pathogenic_codon: bool | None = None

    # ClinVar / database evidence
    clinvar_significance: str | None = None  # "Pathogenic", "Benign", "VUS", etc.
    clinvar_stars: int = 0  # 0–4 review stars
    clinvar_conflicting: bool = False

    # Protein / domain evidence
    same_aa_pathogenic: bool | None = None  # diff AA change at same codon = pathogenic
    synonymous_or_intronic: bool = False  # non-splice synonymous / deep intronic
    bp1_reputable_source_benign: bool | None = None  # reputable source = benign
    missense_constrained: bool = False  # FIX 15: gene missense-constrained (for PP2)

    def to_dict(self) -> dict:
        return asdict(self)


# ─── Criteria evaluation result ───────────────────────────────────────────────


# FIX (Issue 6): ACMG criteria must distinguish "evaluated and found not to
# apply" from "could not be evaluated because required annotation/data is
# unavailable" (e.g. VEP disabled, ClinVar disabled, gnomAD unreachable,
# no pedigree data). Reporting either of these as a bare "Not Met" is
# misleading — a clinician cannot tell confident-negative apart from
# no-data. STATUS_* below is the authoritative three-state result; `met`
# is derived from it and kept only for backward compatibility with code
# that reads `CriteriaResult.met` directly.
STATUS_MET = "met"
STATUS_NOT_MET = "not_met"
STATUS_NOT_EVALUATED = "not_evaluated"  # a.k.a. "Unknown / Insufficient Data"


@dataclass
class CriteriaResult:
    """One evaluated ACMG criterion."""

    code: str  # e.g. "PVS1", "PM2"
    met: bool
    strength: str  # "very_strong" | "strong" | "moderate" | "supporting" | "stand_alone"
    direction: str  # "pathogenic" | "benign"
    reason: str  # human-readable explanation
    # FIX (Issue 6): "met" | "not_met" | "not_evaluated". Only "met" criteria
    # count toward the ACMG classification (unchanged behavior — this field
    # is additive for transparent reporting, not a change to classification
    # logic, since "not_met" and "not_evaluated" were already both excluded
    # from the pathogenicity-combination rules).
    status: str = STATUS_NOT_MET

    def __post_init__(self) -> None:
        # Keep `met` and `status` consistent regardless of which one the
        # caller set explicitly.
        if self.status == STATUS_MET:
            self.met = True
        elif self.met and self.status == STATUS_NOT_MET:
            self.status = STATUS_MET


@dataclass
class AcmgResult:
    """Full ACMG/AMP classification result for one variant."""

    chrom: str = ""
    pos: int = 0
    ref: str = ""
    alt: str = ""
    gene: str | None = None

    classification: str = "Uncertain_Significance"  # P | LP | VUS | LB | B
    score: float = 0.0  # numeric evidence score (for evidence engine)
    criteria_met: list[str] = field(default_factory=list)
    criteria_not_met: list[str] = field(default_factory=list)
    # FIX (Issue 6): criteria that could NOT be evaluated because required
    # annotation/data was unavailable (VEP/ClinVar/gnomAD disabled, no
    # pedigree, etc.) — distinct from criteria_not_met, which means the
    # criterion WAS evaluated and genuinely does not apply.
    criteria_unknown: list[str] = field(default_factory=list)
    all_criteria: list[CriteriaResult] = field(default_factory=list)
    explanation: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ─── Classifier ───────────────────────────────────────────────────────────────


class AcmgClassifier:
    """Evaluate ACMG/AMP 2015 criteria and assign a pathogenicity class.

    Args:
        cfg: Full pipeline config dict. Thresholds are read from
             ``cfg["acmg_thresholds"]``. If absent, defaults from
             Richards et al. 2015 are used.
    """

    # Default thresholds (Richards et al. 2015 / ClinGen recommendations)
    _DEFAULTS = {
        "ba1_af": 0.05,
        "bs1_af": 0.01,
        "pm2_af_max": 0.0001,
        "pp3_cadd_phred": 20.0,
        "pp3_revel": 0.5,
        "pp3_alphamissense": 0.564,
        "bp4_cadd_phred": 10.0,
        "bp4_revel": 0.15,
        "bp4_alphamissense": 0.34,  # AlphaMissense < 0.34 = likely benign
    }

    # Numeric weights per strength tier (for score computation)
    _STRENGTH_SCORE = {
        ("pathogenic", "very_strong"): 8.0,
        ("pathogenic", "strong"): 4.0,
        ("pathogenic", "moderate"): 2.0,
        ("pathogenic", "supporting"): 1.0,
        ("benign", "stand_alone"): -8.0,
        ("benign", "strong"): -4.0,
        ("benign", "supporting"): -1.0,
    }

    def __init__(self, cfg: dict | None = None) -> None:
        raw = (cfg or {}).get("acmg_thresholds", {}) or {}
        self._t: dict = {k: raw.get(k, v) for k, v in self._DEFAULTS.items()}
        # AUDIT NOTE (Issue 4): see _pp5()'s docstring — default False
        # preserves existing behavior/tests; set True to follow the
        # current ClinGen SVI recommendation against PP5/BP6.
        self._disable_pp5_bp6: bool = bool(raw.get("disable_pp5_bp6", False))

    # ── public API ────────────────────────────────────────────────────────────

    def classify(self, evidence: VariantEvidence) -> AcmgResult:
        """Evaluate all criteria and return a full ``AcmgResult``."""
        criteria = self._evaluate_all(evidence)

        # FIX (Issue 6): three-way split. Classification logic below is
        # unchanged — it has always only consumed the "met" list, so
        # re-labeling some previously-"not met" results as "not evaluated"
        # does not alter any classification outcome, only reporting.
        met = [c for c in criteria if c.status == STATUS_MET]
        not_met = [c for c in criteria if c.status == STATUS_NOT_MET]
        unknown = [c for c in criteria if c.status == STATUS_NOT_EVALUATED]

        classification, explanation = self._classify(met)

        # Numeric score: sum of strength weights for met criteria
        score = sum(self._STRENGTH_SCORE.get((c.direction, c.strength), 0.0) for c in met)
        score_norm = round(1.0 / (1.0 + math.exp(-score / 4.0)), 4)

        result = AcmgResult(
            chrom=evidence.chrom,
            pos=evidence.pos,
            ref=evidence.ref,
            alt=evidence.alt,
            gene=evidence.gene,
            classification=classification,
            score=score_norm,
            criteria_met=[c.code for c in met],
            criteria_not_met=[c.code for c in not_met],
            criteria_unknown=[c.code for c in unknown],
            all_criteria=criteria,
            explanation=explanation,
        )
        logger.info(
            "[ACMG] %s:%d %s>%s gene=%s -> %s (score=%.4f) criteria_met=%s criteria_unknown=%s",
            evidence.chrom,
            evidence.pos,
            evidence.ref,
            evidence.alt,
            evidence.gene or "?",
            classification,
            score_norm,
            [c.code for c in met],
            [c.code for c in unknown],
        )
        return result

    # ── criterion evaluation ──────────────────────────────────────────────────

    def _evaluate_all(self, e: VariantEvidence) -> list[CriteriaResult]:
        """Return one CriteriaResult per ACMG code."""
        return [
            # ── Pathogenic Very Strong ──────────────────────────────────────
            self._pvs1(e),
            # ── Pathogenic Strong ───────────────────────────────────────────
            self._ps1(e),
            self._ps2(e),
            self._ps3(e),
            self._ps4(e),
            # ── Pathogenic Moderate ─────────────────────────────────────────
            self._pm1(e),
            self._pm2(e),
            self._pm3(e),  # FIX 1.5
            self._pm4(e),
            self._pm5(e),
            self._pm6(e),
            # ── Pathogenic Supporting ───────────────────────────────────────
            self._pp1(e),  # FIX 1.5
            self._pp2(e),
            self._pp3(e),
            self._pp4(e),
            self._pp5(e),
            # ── Benign Stand-alone ──────────────────────────────────────────
            self._ba1(e),
            # ── Benign Strong ───────────────────────────────────────────────
            self._bs1(e),
            self._bs2(e),
            self._bs3(e),
            self._bs4(e),  # FIX 1.5
            # ── Benign Supporting ───────────────────────────────────────────
            self._bp1(e),
            self._bp2(e),  # FIX 1.5
            self._bp3(e),
            self._bp4(e),
            self._bp5(e),  # FIX 1.5
            self._bp6(e),
            self._bp7(e),
        ]

    # ── PVS (Very Strong Pathogenic) ─────────────────────────────────────────

    def _pvs1(self, e: VariantEvidence) -> CriteriaResult:
        # FIX (Issue 6): lof_gene_intolerant and the consequence-derived
        # is_lof flag both require gene/transcript annotation (VEP/GFF) to
        # have resolved a gene. Without a gene, "not met" would falsely
        # imply we confirmed this is not a LoF-in-intolerant-gene variant.
        if not e.gene:
            return CriteriaResult(
                code="PVS1",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="very_strong",
                direction="pathogenic",
                reason="Unknown / Insufficient Data — gene annotation unavailable",
            )
        met = e.is_lof and e.lof_gene_intolerant
        return CriteriaResult(
            code="PVS1",
            met=met,
            status=(STATUS_MET if met else STATUS_NOT_MET),
            strength="very_strong",
            direction="pathogenic",
            reason=(
                "LoF variant in haploinsufficient gene"
                if met
                else "Not a LoF variant in haploinsufficient gene"
            ),
        )

    # ── PS (Strong Pathogenic) ────────────────────────────────────────────────

    def _ps1(self, e: VariantEvidence) -> CriteriaResult:
        # FIX (Issue 6): same_aa_pathogenic is None when ClinVar is disabled,
        # unreachable, or the local backend is unavailable for a codon-window
        # scan — NOT the same as "confirmed no matching pathogenic AA change".
        if e.same_aa_pathogenic is None:
            return CriteriaResult(
                code="PS1",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="strong",
                direction="pathogenic",
                reason="Unknown / Insufficient Data — ClinVar codon-level lookup unavailable",
            )
        met = bool(e.same_aa_pathogenic)
        return CriteriaResult(
            code="PS1",
            met=met,
            status=(STATUS_MET if met else STATUS_NOT_MET),
            strength="strong",
            direction="pathogenic",
            reason=(
                "Same amino-acid change as established pathogenic variant"
                if met
                else "No known pathogenic same-AA change"
            ),
        )

    def _ps2(self, e: VariantEvidence) -> CriteriaResult:
        # FIX (Issue 6): confirmed_de_novo is None when no trio/pedigree
        # data was supplied — distinct from "trio data confirmed this is
        # NOT de novo".
        if e.confirmed_de_novo is None:
            return CriteriaResult(
                code="PS2",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="strong",
                direction="pathogenic",
                reason="Unknown / Insufficient Data — no trio/pedigree data provided",
            )
        met = bool(e.confirmed_de_novo)
        return CriteriaResult(
            code="PS2",
            met=met,
            status=(STATUS_MET if met else STATUS_NOT_MET),
            strength="strong",
            direction="pathogenic",
            reason=(
                "Confirmed de novo (paternity/maternity confirmed)"
                if met
                else "De novo not confirmed"
            ),
        )

    def _ps3(self, e: VariantEvidence) -> CriteriaResult:
        # FIX (Issue 6): functional_study_damaging is None when no
        # functional-assay data was supplied at all.
        if e.functional_study_damaging is None:
            return CriteriaResult(
                code="PS3",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="strong",
                direction="pathogenic",
                reason="Unknown / Insufficient Data — no functional study data provided",
            )
        met = bool(e.functional_study_damaging)
        return CriteriaResult(
            code="PS3",
            met=met,
            status=(STATUS_MET if met else STATUS_NOT_MET),
            strength="strong",
            direction="pathogenic",
            reason=(
                "Well-established functional study shows damaging effect"
                if met
                else "No damaging functional study evidence"
            ),
        )

    def _ps4(self, e: VariantEvidence) -> CriteriaResult:
        # PS4 requires case/control prevalence data, which is never
        # available in fully-automated mode. FIX (Issue 6): this is
        # "not evaluated", not a confirmed "not met" — the previous
        # hardcoded met=False misleadingly implied prevalence data had
        # been checked and found insufficient.
        return CriteriaResult(
            code="PS4",
            met=False,
            status=STATUS_NOT_EVALUATED,
            strength="strong",
            direction="pathogenic",
            reason="Unknown / Insufficient Data — case/control prevalence data not available in automated mode",
        )

    # ── PM (Moderate Pathogenic) ──────────────────────────────────────────────

    def _pm1(self, e: VariantEvidence) -> CriteriaResult:
        # FIX (Issue 6): hotspot/domain lookup requires a resolved gene
        # (see pipeline/orchestration/shared.py, which only calls the
        # hotspot lookup `if gene:`). Without a gene, "not in a hotspot"
        # cannot be confirmed — it was never checked.
        if not e.gene:
            return CriteriaResult(
                code="PM1",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="moderate",
                direction="pathogenic",
                reason="Unknown / Insufficient Data — gene annotation unavailable",
            )
        # e.in_hotspot is None when the hotspot lookup itself never ran
        # (no local ClinVar TSV / UniProt domains BED loaded, or the lookup
        # raised) — that is "not evaluated", not a confirmed non-hotspot.
        if e.in_hotspot is None:
            return CriteriaResult(
                code="PM1",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="moderate",
                direction="pathogenic",
                reason="Unknown / Insufficient Data — hotspot/domain data unavailable",
            )
        met = e.in_hotspot and e.is_missense
        return CriteriaResult(
            code="PM1",
            met=met,
            status=(STATUS_MET if met else STATUS_NOT_MET),
            strength="moderate",
            direction="pathogenic",
            reason=(
                "Missense in mutational hotspot / critical functional domain"
                if met
                else "Not in a known hotspot or critical domain"
            ),
        )

    def _pm2(self, e: VariantEvidence) -> CriteriaResult:
        """PM2 — Absent from controls in gnomAD (or extremely low frequency).

        PM2 is awarded ONLY when:
          1. gnomad_af is explicitly 0.0 AND gnomad_af_absent flag is True
             (set by callers that resolved ABSENT from gnomAD), OR
          2. a frequency value is present and is below the PM2 threshold.

        PM2 is NOT awarded when:
          - gnomad_af is None and gnomad_af_absent is False/None
            (this means the lookup was unavailable, not that the variant is absent).

        The caller populates VariantEvidence.gnomad_af = None when the lookup
        *failed* (network error / tabix missing) and should set
        gnomad_af_absent = True only when the lookup confirmed absence.
        """
        absent_confirmed = getattr(e, "gnomad_af_absent", None)
        status = STATUS_NOT_MET

        if e.gnomad_af is None and e.gnomad_af_popmax is None:
            if absent_confirmed:
                # Explicitly confirmed absent — award PM2
                met = True
                status = STATUS_MET
                reason = "Absent from gnomAD (confirmed absent by lookup)"
            else:
                # FIX (Issue 6): lookup unavailable (gnomAD disabled,
                # unreachable, or tabix/network failure) — this is
                # "Unknown / Insufficient Data", not a confirmed non-match.
                met = False
                status = STATUS_NOT_EVALUATED
                reason = "Unknown / Insufficient Data — gnomAD lookup unavailable; PM2 not awarded to avoid false positives"
        else:
            af = e.gnomad_af_popmax if e.gnomad_af_popmax is not None else e.gnomad_af
            met = af < self._t["pm2_af_max"]  # type: ignore[operator]
            status = STATUS_MET if met else STATUS_NOT_MET
            reason = (
                f"AF={af:.2e} < PM2 threshold {self._t['pm2_af_max']:.2e}"
                if met
                else f"AF={af:.2e} ≥ PM2 threshold {self._t['pm2_af_max']:.2e}"
            )
        return CriteriaResult(
            code="PM2",
            met=met,
            status=status,
            strength="moderate",
            direction="pathogenic",
            reason=reason,
        )

    def _pm3(self, e: VariantEvidence) -> CriteriaResult:
        """PM3 — For recessive disorders: detected in trans with a pathogenic variant.

        FIX 1.5: Requires both in_trans_with_pathogenic=True AND
        inheritance_pattern indicating autosomal recessive or X-linked ("AR"/"XL").
        FIX (Issue 6): if the pedigree data (trans configuration) or the
        gene's inheritance pattern was never supplied, this is
        "not evaluated", not a confirmed non-match.
        """
        if e.in_trans_with_pathogenic is None:
            return CriteriaResult(
                code="PM3",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="moderate",
                direction="pathogenic",
                reason="Unknown / Insufficient Data — trans-configuration/pedigree data not provided",
            )
        if e.inheritance_pattern is None:
            return CriteriaResult(
                code="PM3",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="moderate",
                direction="pathogenic",
                reason="Unknown / Insufficient Data — gene inheritance pattern unavailable",
            )
        is_recessive = e.inheritance_pattern in ("AR", "XL")
        met = bool(e.in_trans_with_pathogenic) and is_recessive
        return CriteriaResult(
            code="PM3",
            met=met,
            status=(STATUS_MET if met else STATUS_NOT_MET),
            strength="moderate",
            direction="pathogenic",
            reason=(
                "Variant detected in trans with a pathogenic variant in a recessive disorder"
                if met
                else "Not detected in trans with pathogenic variant (or gene not recessive)"
            ),
        )

    def _pm4(self, e: VariantEvidence) -> CriteriaResult:
        """PM4 — Protein length changes due to in-frame indels or stop-loss in non-repeat region.

        Mutually exclusive with BP3: if the variant is in a repeat region (BP3),
        PM4 does not apply per ACMG 2015 specification.
        FIX (Issue 6): in_repeat_region is None when repeat-region
        annotation was never supplied — for an in-frame indel this means
        PM4 genuinely cannot be resolved, not that it's "not met".
        """
        if not e.is_inframe_indel:
            return CriteriaResult(
                code="PM4",
                met=False,
                status=STATUS_NOT_MET,
                strength="moderate",
                direction="pathogenic",
                reason="Not an in-frame indel",
            )
        if e.in_repeat_region is None:
            return CriteriaResult(
                code="PM4",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="moderate",
                direction="pathogenic",
                reason="Unknown / Insufficient Data — repeat-region annotation unavailable",
            )
        met = not e.in_repeat_region
        return CriteriaResult(
            code="PM4",
            met=met,
            status=(STATUS_MET if met else STATUS_NOT_MET),
            strength="moderate",
            direction="pathogenic",
            reason=(
                "In-frame indel in non-repeat region"
                if met
                else "In-frame indel in repeat region — PM4 not applied (see BP3)"
            ),
        )

    def _pm5(self, e: VariantEvidence) -> CriteriaResult:
        """PM5 — Novel missense change at a codon where a DIFFERENT pathogenic missense is known.

        FIX 11 (ACMG guidance): PM5 must NOT fire when PS1 is active.
        - PS1 fires when same_aa_pathogenic=True (same amino-acid substitution as a known pathogenic).
        - PM5 fires when novel_aa_at_known_pathogenic_codon=True (DIFFERENT amino-acid substitution).
        These are mutually exclusive by definition: the same variant cannot simultaneously produce
        the same AND a different amino-acid change.  The explicit guard prevents any edge case
        where upstream data populates both fields.

        FIX (Issue 6): novel_aa_at_known_pathogenic_codon is None when the
        ClinVar codon-window scan was never run (ClinVar disabled/unavailable) —
        distinct from a scan that ran and found nothing.
        """
        if e.novel_aa_at_known_pathogenic_codon is None:
            return CriteriaResult(
                code="PM5",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="moderate",
                direction="pathogenic",
                reason="Unknown / Insufficient Data — ClinVar codon-level lookup unavailable",
            )
        # Explicit guard: if PS1 would fire, PM5 must not (double-counting prevention)
        ps1_active = bool(e.same_aa_pathogenic)
        met = (
            bool(e.novel_aa_at_known_pathogenic_codon)
            and e.is_missense
            and not ps1_active  # FIX 11: PS1 and PM5 are mutually exclusive
        )
        return CriteriaResult(
            code="PM5",
            met=met,
            status=(STATUS_MET if met else STATUS_NOT_MET),
            strength="moderate",
            direction="pathogenic",
            reason=(
                "Novel missense at codon with known pathogenic missense (different AA change)"
                if met
                else (
                    "PM5 suppressed: PS1 active (same amino-acid substitution)"
                    if ps1_active and bool(e.novel_aa_at_known_pathogenic_codon)
                    else "No novel-missense at known-pathogenic codon"
                )
            ),
        )

    def _pm6(self, e: VariantEvidence) -> CriteriaResult:
        """PM6 — Assumed de novo (parentage not confirmed).

        ISSUE 7 FIX: PM6 must NOT fire when PS2 is active. Confirmed de
        novo (PS2) and assumed de novo (PM6) both describe the SAME
        underlying observation — a variant present in the proband but
        not the parents — at two different confidence levels. They are
        not independent pieces of evidence. If upstream pedigree data
        ever populates both confirmed_de_novo and assumed_de_novo for
        the same variant (e.g. ambiguous trio analysis output), the
        confirmed (stronger) observation must take precedence and the
        weaker, redundant PM6 must be suppressed — otherwise a single
        de novo event is double-counted as both Strong and Moderate
        evidence. This mirrors the existing PS1/PM5 guard (FIX 11) in
        this same file, which addresses the identical double-counting
        pattern for same-codon ClinVar evidence.

        FIX (Issue 6): assumed_de_novo is None when no pedigree data was
        supplied at all.
        """
        if e.assumed_de_novo is None:
            return CriteriaResult(
                code="PM6",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="moderate",
                direction="pathogenic",
                reason="Unknown / Insufficient Data — no pedigree/trio data provided",
            )
        ps2_active = bool(e.confirmed_de_novo)
        met = bool(e.assumed_de_novo) and not ps2_active
        return CriteriaResult(
            code="PM6",
            met=met,
            status=(STATUS_MET if met else STATUS_NOT_MET),
            strength="moderate",
            direction="pathogenic",
            reason=(
                "Assumed de novo (parentage not confirmed)"
                if met
                else (
                    "PM6 suppressed: PS2 active (de novo already confirmed)"
                    if ps2_active and bool(e.assumed_de_novo)
                    else "Not an assumed de novo"
                )
            ),
        )

    # ── PP (Supporting Pathogenic) ────────────────────────────────────────────

    def _pp1(self, e: VariantEvidence) -> CriteriaResult:
        """PP1 — Co-segregation with disease in multiple affected family members.

        FIX 1.5: Met when segregates_with_disease is explicitly True.
        PP4 is distinct — it uses phenotype_specific_for_gene, not segregation.
        FIX (Issue 6): None means no segregation data was supplied.
        """
        if e.segregates_with_disease is None:
            return CriteriaResult(
                code="PP1",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="supporting",
                direction="pathogenic",
                reason="Unknown / Insufficient Data — segregation data not provided",
            )
        met = e.segregates_with_disease is True
        return CriteriaResult(
            code="PP1",
            met=met,
            status=(STATUS_MET if met else STATUS_NOT_MET),
            strength="supporting",
            direction="pathogenic",
            reason=(
                "Variant co-segregates with disease in affected family members"
                if met
                else "No co-segregation with disease evidence"
            ),
        )

    def _pp2(self, e: VariantEvidence) -> CriteriaResult:
        """PP2 — Missense variant in a gene that has a low rate of benign missense variation.

        FIX 15: Per ACMG/ClinGen guidance PP2 requires missense constraint
        (low rate of benign missense, e.g. gnomAD oe_mis < 0.8 or mis_z > 3.09),
        NOT LoF intolerance (pLI). Using lof_gene_intolerant for PP2 was incorrect.
        Use missense_constrained field, populated from gnomAD constraint metrics.
        """
        met = e.is_missense and e.missense_constrained
        return CriteriaResult(
            code="PP2",
            met=met,
            status=(STATUS_MET if met else STATUS_NOT_MET),
            strength="supporting",
            direction="pathogenic",
            reason=(
                "Missense in gene with low rate of benign missense variation (missense-constrained)"
                if met
                else "Not a missense-constrained gene or not a missense variant"
            ),
        )

    def _pp3(self, e: VariantEvidence) -> CriteriaResult:
        """Multiple in-silico predictors agree on damaging.

        FIX (Issue 4 audit): REVEL and AlphaMissense are missense-specific
        predictors by design — they are not defined for non-missense
        consequences. Counting them as a "vote" for a non-missense variant
        (e.g. a stray/mismatched score from an upstream annotation bug)
        would be scientifically invalid, not just imprecise. CADD is
        variant-type-agnostic (used here unconditionally, matching
        standard practice) and SpliceAI is relevant regardless of
        missense status (a splice-disrupting variant can be missense,
        synonymous, or intronic), so neither is gated on is_missense.
        """
        votes: list[tuple[str, bool]] = []
        if e.cadd_phred is not None:
            votes.append(("CADD", e.cadd_phred >= self._t["pp3_cadd_phred"]))
        if e.revel_score is not None and e.is_missense:
            votes.append(("REVEL", e.revel_score >= self._t["pp3_revel"]))
        if e.alphamissense_score is not None and e.is_missense:
            votes.append(("AlphaMissense", e.alphamissense_score >= self._t["pp3_alphamissense"]))

        if not votes:
            # FIX (Issue 6): no in-silico scores at all is "not evaluated",
            # not a confirmed "not damaging".
            return CriteriaResult(
                code="PP3",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="supporting",
                direction="pathogenic",
                reason="Unknown / Insufficient Data — no in-silico scores available",
            )
        n_dam = sum(1 for _, d in votes if d)
        met = n_dam >= max(1, len(votes) // 2 + 1)  # majority damaging
        detail = ", ".join(f"{n}={'damaging' if d else 'benign'}" for n, d in votes)
        return CriteriaResult(
            code="PP3",
            met=met,
            status=(STATUS_MET if met else STATUS_NOT_MET),
            strength="supporting",
            direction="pathogenic",
            reason=f"In-silico: {detail} ({'majority damaging' if met else 'not majority damaging'})",
        )

    def _pp4(self, e: VariantEvidence) -> CriteriaResult:
        """PP4 — Patient's phenotype or family history is highly specific for a disease
        with a single genetic etiology.

        Uses phenotype_specific_for_gene field (not segregation — that is PP1/BS4).
        FIX (Issue 6): None means no phenotype data was supplied.
        """
        if e.phenotype_specific_for_gene is None:
            return CriteriaResult(
                code="PP4",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="supporting",
                direction="pathogenic",
                reason="Unknown / Insufficient Data — phenotype data not provided",
            )
        met = bool(e.phenotype_specific_for_gene)
        return CriteriaResult(
            code="PP4",
            met=met,
            status=(STATUS_MET if met else STATUS_NOT_MET),
            strength="supporting",
            direction="pathogenic",
            reason=(
                "Patient phenotype highly specific for a single-gene disease"
                if met
                else "No highly specific phenotype-gene association"
            ),
        )

    def _pp5(self, e: VariantEvidence) -> CriteriaResult:
        """PP5 — reputable source (ClinVar) reports pathogenic.

        AUDIT NOTE (Issue 4): PP5 (and its benign mirror, BP6) are part
        of the original Richards et al. 2015 criteria set, but the
        ClinGen Sequence Variant Interpretation (SVI) Working Group has
        since recommended AGAINST using them in automated pipelines:
        a ClinVar submission's own classification is very often itself
        derived from ACMG/AMP criteria (sometimes including a prior
        PP5/BP6 call from an earlier submitter), so using "ClinVar says
        pathogenic" as independent supporting evidence risks a circular,
        self-reinforcing loop across submitters/labs rather than
        genuinely independent evidence. Most modern automated tools
        (e.g. InterVar) exclude PP5/BP6 entirely for this reason.

        This implementation keeps PP5/BP6 ON by default to preserve
        existing behavior and test expectations, but honors
        `acmg_thresholds.disable_pp5_bp6: true` for labs that want to
        follow the current SVI recommendation and rely only on PS1
        (same amino-acid change) / PM5 / BP1 / direct computational and
        functional evidence instead of raw ClinVar significance.
        """
        if self._disable_pp5_bp6:
            return CriteriaResult(
                code="PP5",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="supporting",
                direction="pathogenic",
                reason=(
                    "PP5 disabled via acmg_thresholds.disable_pp5_bp6 "
                    "(ClinGen SVI recommends against using ClinVar significance "
                    "as independent ACMG evidence due to circularity risk)"
                ),
            )
        if e.clinvar_significance is None:
            return CriteriaResult(
                code="PP5",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="supporting",
                direction="pathogenic",
                reason="Unknown / Insufficient Data — ClinVar unavailable or variant not found in ClinVar",
            )
        met = (
            "pathogenic" in e.clinvar_significance.lower()
            and not e.clinvar_conflicting
            and e.clinvar_stars >= 1
        )
        return CriteriaResult(
            code="PP5",
            met=met,
            status=(STATUS_MET if met else STATUS_NOT_MET),
            strength="supporting",
            direction="pathogenic",
            reason=(
                f"ClinVar: {e.clinvar_significance} ({e.clinvar_stars}★, no conflicts)"
                if met
                else f"ClinVar: {e.clinvar_significance}"
                + (" (conflicting)" if e.clinvar_conflicting else "")
            ),
        )

    # ── BA (Benign Stand-alone) ───────────────────────────────────────────────

    def _ba1(self, e: VariantEvidence) -> CriteriaResult:
        af = e.gnomad_af_popmax if e.gnomad_af_popmax is not None else e.gnomad_af
        if af is None:
            # FIX (Issue 6): no frequency at all (gnomAD disabled/unavailable
            # and not confirmed absent) — cannot evaluate BA1.
            return CriteriaResult(
                code="BA1",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="stand_alone",
                direction="benign",
                reason="Unknown / Insufficient Data — gnomAD frequency unavailable",
            )
        met = af >= self._t["ba1_af"]
        return CriteriaResult(
            code="BA1",
            met=met,
            status=(STATUS_MET if met else STATUS_NOT_MET),
            strength="stand_alone",
            direction="benign",
            reason=(
                f"AF={af:.3f} ≥ BA1 threshold {self._t['ba1_af']}"
                if met
                else f"AF={af:.2e} < BA1 threshold"
            ),
        )

    # ── BS (Strong Benign) ────────────────────────────────────────────────────

    def _bs1(self, e: VariantEvidence) -> CriteriaResult:
        af = e.gnomad_af_popmax if e.gnomad_af_popmax is not None else e.gnomad_af
        if af is None:
            # FIX (Issue 6): unavailable frequency data, not a confirmed
            # "below threshold" result.
            return CriteriaResult(
                code="BS1",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="strong",
                direction="benign",
                reason="Unknown / Insufficient Data — gnomAD frequency unavailable",
            )
        met = self._t["pm2_af_max"] <= af < self._t["ba1_af"] and af >= self._t["bs1_af"]
        return CriteriaResult(
            code="BS1",
            met=met,
            status=(STATUS_MET if met else STATUS_NOT_MET),
            strength="strong",
            direction="benign",
            reason=(
                f"AF={af:.4f} ≥ BS1 threshold {self._t['bs1_af']}"
                if met
                else "AF below BS1 threshold"
            ),
        )

    def _bs2(self, e: VariantEvidence) -> CriteriaResult:
        # Observed in healthy adult in recessive/dominant context — needs
        # external data never available in fully-automated mode.
        # FIX (Issue 6): "not evaluated", not a confirmed "not met".
        return CriteriaResult(
            code="BS2",
            met=False,
            status=STATUS_NOT_EVALUATED,
            strength="strong",
            direction="benign",
            reason="Unknown / Insufficient Data — healthy-adult observation data not provided",
        )

    def _bs3(self, e: VariantEvidence) -> CriteriaResult:
        # FIX (Issue 6): None means no functional-assay data was supplied.
        if e.functional_study_benign is None:
            return CriteriaResult(
                code="BS3",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="strong",
                direction="benign",
                reason="Unknown / Insufficient Data — no functional study data provided",
            )
        met = bool(e.functional_study_benign)
        return CriteriaResult(
            code="BS3",
            met=met,
            status=(STATUS_MET if met else STATUS_NOT_MET),
            strength="strong",
            direction="benign",
            reason=(
                "Well-established functional study shows no damaging effect"
                if met
                else "No benign functional study evidence"
            ),
        )

    def _bs4(self, e: VariantEvidence) -> CriteriaResult:
        """BS4 — Lack of segregation in affected members of a family.

        FIX 1.5: Met when segregates_away_from_disease is explicitly True
        (i.e. family members with disease do NOT carry the variant).
        FIX (Issue 6): None means no segregation data was supplied.
        """
        if e.segregates_away_from_disease is None:
            return CriteriaResult(
                code="BS4",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="strong",
                direction="benign",
                reason="Unknown / Insufficient Data — segregation data not provided",
            )
        met = e.segregates_away_from_disease is True
        return CriteriaResult(
            code="BS4",
            met=met,
            status=(STATUS_MET if met else STATUS_NOT_MET),
            strength="strong",
            direction="benign",
            reason=(
                "Variant does not segregate with disease in affected family members"
                if met
                else "No non-segregation with disease evidence"
            ),
        )

    # ── BP (Supporting Benign) ────────────────────────────────────────────────

    def _bp1(self, e: VariantEvidence) -> CriteriaResult:
        # FIX (Issue 6): None means no reputable-source classification was
        # ever supplied for this variant.
        if e.bp1_reputable_source_benign is None:
            return CriteriaResult(
                code="BP1",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="supporting",
                direction="benign",
                reason="Unknown / Insufficient Data — no reputable-source benign classification provided",
            )
        met = bool(e.bp1_reputable_source_benign)
        return CriteriaResult(
            code="BP1",
            met=met,
            status=(STATUS_MET if met else STATUS_NOT_MET),
            strength="supporting",
            direction="benign",
            reason=(
                "Reputable source reports variant as benign"
                if met
                else "No reputable-source benign report"
            ),
        )

    def _bp2(self, e: VariantEvidence) -> CriteriaResult:
        """BP2 — Observed in trans with a pathogenic variant for AD disorder,
        or in cis with pathogenic variant (either way: unexpected for pathogenicity).

        FIX 1.5: Met when in_trans_or_cis_with_pathogenic_unexpected is True.
        FIX (Issue 6): None means no trans/cis co-occurrence data was supplied.
        """
        if e.in_trans_or_cis_with_pathogenic_unexpected is None:
            return CriteriaResult(
                code="BP2",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="supporting",
                direction="benign",
                reason="Unknown / Insufficient Data — trans/cis co-occurrence data not provided",
            )
        met = bool(e.in_trans_or_cis_with_pathogenic_unexpected)
        return CriteriaResult(
            code="BP2",
            met=met,
            status=(STATUS_MET if met else STATUS_NOT_MET),
            strength="supporting",
            direction="benign",
            reason=(
                "Observed in trans with pathogenic variant (dominant) or in cis with "
                "pathogenic variant — unexpected for pathogenicity"
                if met
                else "No unexpected trans/cis pathogenic co-occurrence"
            ),
        )

    def _bp3(self, e: VariantEvidence) -> CriteriaResult:
        """BP3 — In-frame deletions/insertions in a repetitive region without known function.

        Met when is_inframe_indel=True AND in_repeat_region=True.
        Requires repeat-region annotation (e.g. RepeatMasker BED).
        FIX (Issue 6): if it IS an in-frame indel but repeat-region status
        is unknown, BP3 cannot be resolved either way.
        """
        if not e.is_inframe_indel:
            return CriteriaResult(
                code="BP3",
                met=False,
                status=STATUS_NOT_MET,
                strength="supporting",
                direction="benign",
                reason="Not an in-frame indel",
            )
        if e.in_repeat_region is None:
            return CriteriaResult(
                code="BP3",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="supporting",
                direction="benign",
                reason="Unknown / Insufficient Data — repeat-region annotation unavailable",
            )
        met = bool(e.in_repeat_region)
        return CriteriaResult(
            code="BP3",
            met=met,
            status=(STATUS_MET if met else STATUS_NOT_MET),
            strength="supporting",
            direction="benign",
            reason=(
                "In-frame indel in repeat region without known function"
                if met
                else "Not an in-frame indel in a repeat region"
            ),
        )

    def _bp4(self, e: VariantEvidence) -> CriteriaResult:
        """Multiple in-silico predictors agree on benign.

        FIX (Issue 4 audit): same missense-only gating as PP3 — see its
        docstring for rationale.
        """
        votes: list[tuple[str, bool]] = []
        if e.cadd_phred is not None:
            votes.append(("CADD", e.cadd_phred < self._t["bp4_cadd_phred"]))
        if e.revel_score is not None and e.is_missense:
            votes.append(("REVEL", e.revel_score < self._t["bp4_revel"]))
        if e.alphamissense_score is not None and e.is_missense:
            # AlphaMissense < bp4_alphamissense threshold → likely benign
            votes.append(("AlphaMissense", e.alphamissense_score < self._t["bp4_alphamissense"]))

        if not votes:
            # FIX (Issue 6): no in-silico scores at all is "not evaluated".
            return CriteriaResult(
                code="BP4",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="supporting",
                direction="benign",
                reason="Unknown / Insufficient Data — no in-silico scores available",
            )
        n_ben = sum(1 for _, b in votes if b)
        met = n_ben >= max(1, len(votes) // 2 + 1)
        detail = ", ".join(f"{n}={'benign' if b else 'damaging'}" for n, b in votes)
        return CriteriaResult(
            code="BP4",
            met=met,
            status=(STATUS_MET if met else STATUS_NOT_MET),
            strength="supporting",
            direction="benign",
            reason=f"In-silico: {detail} ({'majority benign' if met else 'not majority benign'})",
        )

    def _bp5(self, e: VariantEvidence) -> CriteriaResult:
        """BP5 — Variant found in a case with an alternate molecular basis for disease.

        FIX 1.5: Met when alternate_molecular_basis_found is True, meaning another
        fully explanatory pathogenic variant was identified for the patient's phenotype,
        making it unlikely this variant is causative.
        FIX (Issue 6): None means this was never assessed.
        """
        if e.alternate_molecular_basis_found is None:
            return CriteriaResult(
                code="BP5",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="supporting",
                direction="benign",
                reason="Unknown / Insufficient Data — alternate molecular basis not assessed",
            )
        met = bool(e.alternate_molecular_basis_found)
        return CriteriaResult(
            code="BP5",
            met=met,
            status=(STATUS_MET if met else STATUS_NOT_MET),
            strength="supporting",
            direction="benign",
            reason=(
                "Variant found in case where alternate molecular basis fully explains phenotype"
                if met
                else "No alternate molecular basis identified"
            ),
        )

    def _bp6(self, e: VariantEvidence) -> CriteriaResult:
        """BP6 — reputable source (ClinVar) reports benign. See _pp5's
        docstring for the ClinGen SVI circularity note this mirrors."""
        if self._disable_pp5_bp6:
            return CriteriaResult(
                code="BP6",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="supporting",
                direction="benign",
                reason=(
                    "BP6 disabled via acmg_thresholds.disable_pp5_bp6 "
                    "(ClinGen SVI recommends against using ClinVar significance "
                    "as independent ACMG evidence due to circularity risk)"
                ),
            )
        # FIX (Issue 6): mirrors PP5 — ClinVar unavailable or variant not
        # found in ClinVar is "not evaluated", not a confirmed non-benign result.
        if e.clinvar_significance is None:
            return CriteriaResult(
                code="BP6",
                met=False,
                status=STATUS_NOT_EVALUATED,
                strength="supporting",
                direction="benign",
                reason="Unknown / Insufficient Data — ClinVar unavailable or variant not found in ClinVar",
            )
        met = (
            "benign" in e.clinvar_significance.lower()
            and not e.clinvar_conflicting
            and e.clinvar_stars >= 1
        )
        return CriteriaResult(
            code="BP6",
            met=met,
            status=(STATUS_MET if met else STATUS_NOT_MET),
            strength="supporting",
            direction="benign",
            reason=f"ClinVar: {e.clinvar_significance} ({e.clinvar_stars}★)",
        )

    def _bp7(self, e: VariantEvidence) -> CriteriaResult:
        met = e.synonymous_or_intronic
        return CriteriaResult(
            code="BP7",
            met=met,
            status=(STATUS_MET if met else STATUS_NOT_MET),
            strength="supporting",
            direction="benign",
            reason=(
                "Synonymous / non-splice intronic variant with no predicted splice impact"
                if met
                else "Not a synonymous/non-splice intronic variant"
            ),
        )

    # ── Classification logic (Richards et al. 2015 Table 5) ──────────────────

    def _classify(self, met: list[CriteriaResult]) -> tuple[str, str]:
        """Apply the ACMG/AMP combination rules to the list of met criteria.

        FIX 9: Detects conflicting evidence (strong pathogenic + strong benign)
        per ClinGen recommendations and returns Uncertain_Significance with a
        conflict explanation rather than silently classifying.
        """

        def _count(direction: str, strength: str) -> int:
            return sum(1 for c in met if c.direction == direction and c.strength == strength)

        pvs = _count("pathogenic", "very_strong")
        ps = _count("pathogenic", "strong")
        pm = _count("pathogenic", "moderate")
        pp = _count("pathogenic", "supporting")
        ba = _count("benign", "stand_alone")
        bs = _count("benign", "strong")
        bp = _count("benign", "supporting")

        # FIX 9: Conflict detection per ClinGen recommendations.
        # When ≥1 strong/very_strong pathogenic criterion co-occurs with
        # ≥1 strong/stand-alone benign criterion the evidence is genuinely
        # contradictory.  Do NOT silently classify — report VUS-Conflicting.
        _path_strong = pvs + ps
        _ben_strong = ba + bs
        if _path_strong >= 1 and _ben_strong >= 1:
            path_codes = [
                c.code
                for c in met
                if c.direction == "pathogenic" and c.strength in ("very_strong", "strong")
            ]
            ben_codes = [
                c.code
                for c in met
                if c.direction == "benign" and c.strength in ("stand_alone", "strong")
            ]
            conflict_msg = (
                f"Conflicting_Evidence: strong pathogenic criteria ({', '.join(path_codes)}) "
                f"and strong benign criteria ({', '.join(ben_codes)}) co-occur. "
                "Classification deferred — requires expert review per ClinGen conflict resolution guidelines."
            )
            return "Uncertain_Significance", conflict_msg

        # ── Pathogenic ────────────────────────────────────────────────────────
        if (
            (pvs >= 1 and ps >= 1)
            or (pvs >= 1 and pm >= 2)
            or (pvs >= 1 and pm >= 1 and pp >= 1)
            or (pvs >= 1 and pp >= 2)
            or (ps >= 2)
            or (ps >= 1 and pm >= 3)
            or (ps >= 1 and pm >= 2 and pp >= 2)
            or (ps >= 1 and pm >= 1 and pp >= 4)
        ):
            return "Pathogenic", self._explain(met, "Pathogenic")

        # ── Likely Pathogenic ─────────────────────────────────────────────────
        if (
            (pvs >= 1 and pm == 1)
            or (ps >= 1 and pm >= 1 and pm <= 2)
            or (ps >= 1 and pp >= 2)
            or (pm >= 3)
            or (pm == 2 and pp >= 2)
            or (pm == 1 and pp >= 4)
        ):
            return "Likely_Pathogenic", self._explain(met, "Likely_Pathogenic")

        # ── Benign ────────────────────────────────────────────────────────────
        if ba >= 1 or bs >= 2:
            return "Benign", self._explain(met, "Benign")

        # ── Likely Benign ─────────────────────────────────────────────────────
        if (bs >= 1 and bp >= 1) or bp >= 2:
            return "Likely_Benign", self._explain(met, "Likely_Benign")

        return (
            "Uncertain_Significance",
            "Criteria met do not fulfil any P/LP/LB/B combination rule.",
        )

    @staticmethod
    def _explain(met: list[CriteriaResult], classification: str) -> str:
        codes = [c.code for c in met]
        return f"{classification} based on criteria: {', '.join(codes) if codes else 'none'}."
