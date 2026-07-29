"""
pipeline/evidence/aggregator.py
─────────────────────────────────
Weighted evidence aggregation engine (Task evidence_engine).

Combines four evidence streams into a single COMPOSITE RANKING SCORE and
an associated exploratory tier:

  1. ACMG/AMP 2015 score        (weight_acmg        default 0.45)
  2. ClinVar consensus           (weight_clinvar      default 0.25)
  3. Computational / in-silico  (weight_computational default 0.20)
  4. AI model score             (weight_ai           default 0.10)

  ISSUE 2 — IMPORTANT CLINICAL-SAFETY NOTE (double counting)
  ────────────────────────────────────────────────────────────
  The ACMG score (stream 1) already incorporates ClinVar concordance
  (PP5/BP6) and computational/in-silico predictors (PP3/BP4) as inputs
  to the ACMG/AMP classification — see pipeline/acmg/classifier.py.
  This composite metric then weights ClinVar and computational evidence
  AGAIN as independent streams. That is intentional double-counting for
  ranking/triage purposes (it lets strong concordant evidence pull the
  composite further toward the extremes than ACMG criteria alone would),
  but it means the composite score and its derived tier are NOT a second,
  independent confidence estimate.

  The ACMG classification (`AcmgResult.classification` /
  `AcmgResult.score`) is, and remains, the sole clinically-meaningful
  pathogenicity classification produced by this pipeline. The
  `composite_score` / `final_tier` produced here are an EXPLORATORY
  RANKING METRIC ONLY, intended for triage/sorting of variant lists
  (e.g. "which VUS to review first"). They must never be reported,
  displayed, or interpreted as an independent clinical confidence score,
  and must never be used in place of, or to override, the ACMG
  classification. Every EvidenceResult carries an explicit
  `disclaimer` string and `is_exploratory_only=True` flag to make this
  isolation explicit and machine-checkable for any downstream consumer
  (e.g. the reporting stage, which renders this disclaimer alongside
  the score — see pipeline/reporting/stage.py).

All four streams produce a normalised score in [0, 1]:
  0.0 = strongly benign
  0.5 = uncertain
  1.0 = strongly pathogenic

The composite score is a weighted average of whichever streams have
data (weights re-normalised if a stream is missing).  The exploratory
tier is assigned from the composite score using calibrated thresholds:

  ≥ 0.85  →  Pathogenic
  ≥ 0.65  →  Likely_Pathogenic
  ≥ 0.35  →  Uncertain_Significance
  ≥ 0.15  →  Likely_Benign
  <  0.15  →  Benign

These thresholds are defaults — they can be tuned per assay via config.
Note the tier labels intentionally mirror ACMG terminology for
readability, which is exactly why the disclaimer above matters: do not
mistake this exploratory tier for the ACMG classification itself.

Usage::

    from pipeline.evidence.aggregator import EvidenceAggregator, EvidenceInput

    agg = EvidenceAggregator(cfg={"evidence_engine": {...}})
    result = agg.aggregate(EvidenceInput(
        acmg_score=0.92,
        clinvar_score=0.88,
        computational_score=0.75,
        ai_score=0.81,
    ))
    print(result.composite_score)    # 0.865  (exploratory ranking score)
    print(result.final_tier)         # "Pathogenic"  (exploratory tier, NOT an ACMG class)
    print(result.disclaimer)         # explicit non-clinical-confidence warning
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("geper.pipeline.evidence.aggregator")


# ─── Input ────────────────────────────────────────────────────────────────────

@dataclass
class EvidenceInput:
    """Normalised [0, 1] scores from each evidence stream.

    Set a field to None if that stream produced no data — its weight
    will be redistributed among the other streams automatically.
    """
    # From pipeline/acmg/classifier.py → AcmgResult.score
    acmg_score: Optional[float] = None

    # From ClinVar lookup (to be implemented in pipeline/clinvar/)
    # 0 = Benign, 0.25 = Likely Benign, 0.5 = VUS, 0.75 = LP, 1.0 = P
    clinvar_score: Optional[float] = None

    # Composite from in-silico tools: CADD, REVEL, SpliceAI, AlphaMissense
    computational_score: Optional[float] = None

    # From pipeline/ai/ (DNABERT-2 / ESM-2)
    ai_score: Optional[float] = None

    # Variant identity (for logging / output only)
    chrom: str = ""
    pos: int = 0
    ref: str = ""
    alt: str = ""
    gene: Optional[str] = None

    def to_dict(self) -> Dict:
        return asdict(self)


# ─── Result ───────────────────────────────────────────────────────────────────

@dataclass
class EvidenceResult:
    """Full aggregation result for one variant."""
    chrom: str = ""
    pos: int = 0
    ref: str = ""
    alt: str = ""
    gene: Optional[str] = None

    # Per-stream scores (None if stream had no data)
    acmg_score: Optional[float] = None
    clinvar_score: Optional[float] = None
    computational_score: Optional[float] = None
    ai_score: Optional[float] = None

    # Effective weights used (after re-normalisation for missing streams)
    effective_weight_acmg: float = 0.0
    effective_weight_clinvar: float = 0.0
    effective_weight_computational: float = 0.0
    effective_weight_ai: float = 0.0

    composite_score: float = 0.5
    final_tier: str = "Uncertain_Significance"
    streams_used: List[str] = None  # type: ignore[assignment]
    explanation: str = ""

    # ISSUE 2 FIX: explicit, machine-checkable isolation of this metric
    # from clinical confidence. ClinVar/computational evidence already
    # feeds the ACMG score, so this composite double-counts those streams
    # by design for ranking purposes — it is not a second independent
    # confidence estimate. Downstream consumers (e.g. reporting) must
    # surface this disclaimer wherever composite_score/final_tier appear.
    is_exploratory_only: bool = True
    disclaimer: str = (
        "Exploratory ranking metric only — NOT an independent clinical "
        "confidence score. ClinVar concordance and computational/in-silico "
        "evidence already contribute to the ACMG score above, so this "
        "composite intentionally re-weights them for triage/sorting. "
        "The ACMG classification is the clinically-meaningful result; "
        "this composite score/tier must never override or be reported "
        "in place of it."
    )

    def __post_init__(self):
        if self.streams_used is None:
            self.streams_used = []

    def to_dict(self) -> Dict:
        return asdict(self)


# ─── Aggregator ───────────────────────────────────────────────────────────────

class EvidenceAggregator:
    """Combine multiple evidence streams into one composite pathogenicity score.

    Args:
        cfg: Full pipeline config dict. Weights are read from
             ``cfg["evidence_engine"]``. Missing keys fall back to defaults.
    """

    # Default weights (must sum to 1.0)
    _DEFAULT_WEIGHTS = {
        "acmg":          0.45,
        "clinvar":       0.25,
        "computational": 0.20,
        "ai":            0.10,
    }

    # Score → tier thresholds (tunable via config)
    _DEFAULT_THRESHOLDS = {
        "pathogenic":           0.85,
        "likely_pathogenic":    0.65,
        "likely_benign":        0.35,
        "benign":               0.15,
    }

    def __init__(self, cfg: Optional[Dict] = None) -> None:
        ee = (cfg or {}).get("evidence_engine", {}) or {}
        self._weights = {
            "acmg":          float(ee.get("weight_acmg",          self._DEFAULT_WEIGHTS["acmg"])),
            "clinvar":       float(ee.get("weight_clinvar",        self._DEFAULT_WEIGHTS["clinvar"])),
            "computational": float(ee.get("weight_computational",  self._DEFAULT_WEIGHTS["computational"])),
            "ai":            float(ee.get("weight_ai",             self._DEFAULT_WEIGHTS["ai"])),
        }
        thr = (cfg or {}).get("evidence_thresholds", {}) or {}
        self._thresholds = {
            "pathogenic":        float(thr.get("pathogenic",        self._DEFAULT_THRESHOLDS["pathogenic"])),
            "likely_pathogenic": float(thr.get("likely_pathogenic", self._DEFAULT_THRESHOLDS["likely_pathogenic"])),
            "likely_benign":     float(thr.get("likely_benign",     self._DEFAULT_THRESHOLDS["likely_benign"])),
            "benign":            float(thr.get("benign",            self._DEFAULT_THRESHOLDS["benign"])),
        }

    # ── public API ────────────────────────────────────────────────────────────

    def aggregate(self, inp: EvidenceInput) -> EvidenceResult:
        """Compute composite score and assign a final tier.

        Args:
            inp: ``EvidenceInput`` with per-stream scores in [0, 1].

        Returns:
            ``EvidenceResult`` with composite score, tier, and per-stream breakdown.
        """
        stream_map = {
            "acmg":          inp.acmg_score,
            "clinvar":       inp.clinvar_score,
            "computational": inp.computational_score,
            "ai":            inp.ai_score,
        }

        # Re-normalise weights for present streams only
        present = {k: v for k, v in stream_map.items() if v is not None}
        eff_weights = self._effective_weights(present)

        if not present:
            composite = 0.5
            explanation = "No evidence streams provided — defaulting to Uncertain_Significance."
        else:
            composite = sum(
                eff_weights[k] * present[k] for k in present
            )
            composite = round(min(1.0, max(0.0, composite)), 4)
            explanation = self._build_explanation(present, eff_weights, composite)

        tier = self._assign_tier(composite)

        result = EvidenceResult(
            chrom=inp.chrom,
            pos=inp.pos,
            ref=inp.ref,
            alt=inp.alt,
            gene=inp.gene,
            acmg_score=inp.acmg_score,
            clinvar_score=inp.clinvar_score,
            computational_score=inp.computational_score,
            ai_score=inp.ai_score,
            effective_weight_acmg=eff_weights.get("acmg", 0.0),
            effective_weight_clinvar=eff_weights.get("clinvar", 0.0),
            effective_weight_computational=eff_weights.get("computational", 0.0),
            effective_weight_ai=eff_weights.get("ai", 0.0),
            composite_score=composite,
            final_tier=tier,
            streams_used=list(present.keys()),
            explanation=explanation,
        )

        logger.info(
            "[Evidence] %s:%d %s>%s gene=%s composite=%.4f tier=%s streams=%s",
            inp.chrom, inp.pos, inp.ref, inp.alt,
            inp.gene or "?", composite, tier, list(present.keys()),
        )
        return result

    def aggregate_from_acmg(
        self,
        acmg_result,   # AcmgResult from pipeline.acmg.classifier
        clinvar_score: Optional[float] = None,
        computational_score: Optional[float] = None,
        ai_score: Optional[float] = None,
    ) -> EvidenceResult:
        """Convenience wrapper: build EvidenceInput from an AcmgResult directly.

        Args:
            acmg_result:          ``AcmgResult`` from ``AcmgClassifier.classify()``.
            clinvar_score:        ClinVar stream score [0, 1] (None if unavailable).
            computational_score:  Computational stream score [0, 1] (None if unavailable).
            ai_score:             AI model stream score [0, 1] (None if unavailable).
        """
        inp = EvidenceInput(
            acmg_score=acmg_result.score,
            clinvar_score=clinvar_score,
            computational_score=computational_score,
            ai_score=ai_score,
            chrom=acmg_result.chrom,
            pos=acmg_result.pos,
            ref=acmg_result.ref,
            alt=acmg_result.alt,
            gene=acmg_result.gene,
        )
        return self.aggregate(inp)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _effective_weights(self, present: Dict[str, float]) -> Dict[str, float]:
        """Re-normalise weights so they sum to 1.0 over present streams only."""
        if not present:
            return {}
        total = sum(self._weights[k] for k in present)
        if total == 0:
            # Equal weights fallback
            eq = 1.0 / len(present)
            return {k: eq for k in present}
        return {k: self._weights[k] / total for k in present}

    def _assign_tier(self, score: float) -> str:
        """Map composite score to a classification tier."""
        if score >= self._thresholds["pathogenic"]:
            return "Pathogenic"
        if score >= self._thresholds["likely_pathogenic"]:
            return "Likely_Pathogenic"
        if score >= self._thresholds["likely_benign"]:
            return "Uncertain_Significance"
        if score >= self._thresholds["benign"]:
            return "Likely_Benign"
        return "Benign"

    def _build_explanation(
        self,
        present: Dict[str, float],
        eff_weights: Dict[str, float],
        composite: float,
    ) -> str:
        parts = [
            f"{k}={v:.4f}(w={eff_weights[k]:.3f})"
            for k, v in present.items()
        ]
        return f"Composite={composite:.4f} from: {', '.join(parts)}."

    # ── computational score helper ────────────────────────────────────────────

    @staticmethod
    def compute_computational_score(
        cadd_phred: Optional[float] = None,
        revel_score: Optional[float] = None,
        spliceai_score: Optional[float] = None,
        alphamissense_score: Optional[float] = None,
    ) -> Optional[float]:
        """Aggregate in-silico scores into a single [0, 1] computational score.

        Uses a simple equally-weighted mean of whichever scores are available,
        after normalising each to [0, 1]:
          CADD Phred: score / 50  (Phred 50 ≈ top 0.001% most deleterious)
          REVEL:      already in [0, 1]
          SpliceAI:   already in [0, 1]
          AlphaMissense: already in [0, 1]

        Returns None if no scores are available.
        """
        scores: List[float] = []
        if cadd_phred is not None:
            scores.append(min(1.0, cadd_phred / 50.0))
        if revel_score is not None:
            scores.append(min(1.0, max(0.0, float(revel_score))))
        if spliceai_score is not None:
            scores.append(min(1.0, max(0.0, float(spliceai_score))))
        if alphamissense_score is not None:
            scores.append(min(1.0, max(0.0, float(alphamissense_score))))
        if not scores:
            return None
        return round(sum(scores) / len(scores), 4)

    # ── ClinVar score helper ──────────────────────────────────────────────────

    @staticmethod
    def clinvar_sig_to_score(significance: Optional[str], stars: int = 0) -> Optional[float]:
        """Convert a ClinVar significance string to [0, 1].

        FIX 10: Delegates to ClinVarLookup.sig_to_score — single authoritative
        implementation; duplicate scoring function removed.
        """
        if not significance:
            return None
        from pipeline.clinvar.lookup import ClinVarLookup
        return ClinVarLookup.sig_to_score(significance, stars)
