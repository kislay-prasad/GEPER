"""
EnsembleManager: combines every currently *available* splicing/
regulatory AI plugin (Enformer, Borzoi -- see pipeline/models/
enformer_plugin.py and borzoi_plugin.py) into one consensus
assessment, via the same `ModelManager` the rest of the plugin
framework uses.

SpliceFormer/SpliceBERT/SPiP are registered in the same plugin
framework but deliberately excluded from this ensemble -- see
pipeline/models/pending_plugins.py's module docstring for why.

Routing rules (used directly by pipeline/acmg_rules.py's PP3/BP4
integration -- see that module for how `evaluate()`'s output is
consumed):
    0 models available -> `models_used` is empty; no consensus is
        produced. Callers must treat this as "no evidence", not as a
        neutral/benign result.
    1 model available   -> that model's own score/classification is
        used directly as the "consensus" (labeled as a single-model
        result, not a true consensus, throughout `reasoning` and the
        returned dict, so downstream auditors can tell the two cases
        apart).
    2 models available  -> a true consensus: the mean score, the
        classification of that mean score, and an agreement
        percentage describing how closely the two models' scores (and
        classifications) actually agreed.

Every field returned here is meant to be directly auditable: nothing
is asserted without the concrete per-model numbers that produced it
also being present in `individual_scores` and `reasoning`.
"""

from typing import Any, Dict, Optional

import config
from pipeline.models.manager import ModelManager
from pipeline.models.pending_plugins import build_default_registry
from utils.logger import get_logger

logger = get_logger(__name__)

# Only these two participate in the ensemble (see module docstring).
_ENSEMBLE_MODEL_KEYS = ("enformer", "borzoi")

# Classification thresholds come from config's SHARED splice-family pair, the
# same one EnformerPlugin/BorzoiPlugin each already use individually for their
# own single-model classification (see pipeline/models/enformer_plugin.py::
# _infer_impl and pipeline/models/borzoi_plugin.py::_infer_impl) -- so the
# ensemble's consensus classification is drawn from the identical,
# already-documented scale, not a second, inconsistent one.
#
# Score-spread beyond this (on the same 0..~1+ scale the individual
# plugins use) is treated as complete disagreement (0% score
# agreement); linearly scaled between 0 and this value.
_MAX_MEANINGFUL_SCORE_SPREAD = 1.0

# When the two models' classifications disagree, agreement is capped
# at this percentage even if their raw scores happen to be numerically
# close -- classification disagreement is a stronger, clinically more
# visible signal than raw score closeness, so it should always pull
# the reported agreement below "mostly agreeing".
_CLASSIFICATION_DISAGREEMENT_CAP = 50.0


def _classify(score: float) -> str:
    """
    Same bucketing EnformerPlugin/BorzoiPlugin each use individually.

    READ AT CALL TIME, NOT BOUND AT IMPORT: a module-level copy of
    `config.SPLICE_DELTA_*` would be a private literal again by another
    route -- it would stop tracking config the moment anything changed it.
    """
    if score < config.SPLICE_DELTA_NO_EFFECT_THRESHOLD:
        return "no_significant_effect"
    if score < config.SPLICE_DELTA_MODERATE_EFFECT_THRESHOLD:
        return "moderate_effect"
    return "large_effect"


class EnsembleManager:
    """Runs every available splicing/regulatory AI plugin and combines
    the results. Never raises for a missing/failed model -- that's
    already `ModelManager.predict()`'s job (see pipeline/models/
    manager.py); this class only has to handle "0, 1, or 2 results
    came back", which is a plain, fully-testable function of what
    `ModelManager.predict()` returns.
    """

    def __init__(self, manager: Optional[ModelManager] = None):
        self.manager = manager or ModelManager(registry=build_default_registry())

    def evaluate(self, ref_seq: str, alt_seq: str, **kwargs) -> Dict[str, Any]:
        individual: Dict[str, Dict[str, Any]] = {}
        for key in _ENSEMBLE_MODEL_KEYS:
            result = self.manager.predict(key, ref_seq, alt_seq, **kwargs)
            if result is not None:
                individual[key] = result

        n = len(individual)

        if n == 0:
            # `ModelManager.predict()` already distinguishes "plugin
            # unavailable" (disabled/not installed -- `get()` raises
            # `PluginUnavailableError` before `instance.predict()` is
            # ever reached) from "plugin genuinely crashed during
            # inference" (`ModelInferenceError`, caught inside
            # `predict()` and recorded in `_last_inference_error`,
            # retrievable via `last_inference_errors()`) -- its own
            # docstring says so explicitly: "Callers that need to
            # distinguish 'unavailable' from 'failed' should call
            # `get()` themselves." This method used to never call it,
            # so a genuine double crash collapsed into the same generic
            # "disabled, not installed, or failed to load/run" text as
            # a clean disable, and `_render_ai_splicing_ensemble`
            # (report/report_generator.py) then returned an empty list
            # for `models_used == []` regardless of which was true --
            # the entire "AI Splicing Analysis" section vanished on a
            # crash, not even a "Failed" line (2026-09-11 fix).
            inference_errors = self.manager.last_inference_errors()
            failed = {key: inference_errors[key] for key in _ENSEMBLE_MODEL_KEYS if key in inference_errors}
            if failed:
                detail = "; ".join(f"{key}: {message}" for key, message in failed.items())
                return {
                    "models_used": [],
                    "individual_scores": {},
                    "consensus_score": None,
                    "confidence": None,
                    "agreement_percentage": None,
                    "classification": None,
                    "basis": "no_models",
                    "error": detail,
                    "reasoning": (
                        f"AI splicing ensemble inference failed ({detail}) -- this is a failed "
                        "run, not evidence that Enformer/Borzoi found no splicing effect."
                    ),
                }
            return {
                "models_used": [],
                "individual_scores": {},
                "consensus_score": None,
                "confidence": None,
                "agreement_percentage": None,
                "classification": None,
                "basis": "no_models",
                "error": None,
                "reasoning": (
                    "No splicing/regulatory AI model was available "
                    "(Enformer and Borzoi are both disabled or not "
                    "installed) -- no ensemble evidence was produced for this variant."
                ),
            }

        scores = {k: v["score"] for k, v in individual.items()}
        classifications = {k: v["classification"] for k, v in individual.items()}
        confidences = [v.get("confidence") for v in individual.values() if v.get("confidence") is not None]

        consensus_score = sum(scores.values()) / n
        consensus_confidence = (sum(confidences) / len(confidences)) if confidences else None
        consensus_classification = _classify(consensus_score)

        if n == 1:
            model_name = next(iter(individual))
            return {
                "models_used": [model_name],
                "individual_scores": individual,
                "consensus_score": consensus_score,
                "confidence": consensus_confidence,
                "agreement_percentage": None,  # not meaningful with a single model
                "classification": consensus_classification,
                "basis": "single_model",
                "reasoning": (
                    f"Only one splicing/regulatory AI model ('{model_name}') was "
                    f"available; its own prediction (score={consensus_score:.3f}, "
                    f"classification='{consensus_classification}') is used "
                    "directly. This is a single-model result, not a two-model "
                    "consensus -- agreement percentage is not applicable."
                ),
            }

        # n == 2 (the only other case, since exactly two plugins ever
        # participate -- see _ENSEMBLE_MODEL_KEYS).
        agreement_percentage = self._agreement_percentage(scores, classifications)
        reasoning = self._build_consensus_reasoning(
            individual, consensus_score, consensus_classification, agreement_percentage
        )
        return {
            "models_used": list(individual),
            "individual_scores": individual,
            "consensus_score": consensus_score,
            "confidence": consensus_confidence,
            "agreement_percentage": agreement_percentage,
            "classification": consensus_classification,
            "basis": "two_model_consensus",
            "reasoning": reasoning,
        }

    @staticmethod
    def _agreement_percentage(scores: Dict[str, float], classifications: Dict[str, str]) -> float:
        """
        Two-part agreement metric, always in [0, 100]:
          1. Score agreement: 100% when both models produced the exact
             same score, scaling linearly down to 0% at a spread of
             `_MAX_MEANINGFUL_SCORE_SPREAD` or more.
          2. Classification cap: if the two models landed in different
             classification buckets (no_significant_effect / moderate_effect
             / large_effect), the result is capped at
             `_CLASSIFICATION_DISAGREEMENT_CAP` regardless of how close
             the raw scores were -- a visible classification
             disagreement is a stronger signal than score-closeness
             alone.
        """
        values = list(scores.values())
        score_spread = max(values) - min(values)
        score_agreement = max(
            0.0, 100.0 * (1.0 - min(score_spread, _MAX_MEANINGFUL_SCORE_SPREAD) / _MAX_MEANINGFUL_SCORE_SPREAD)
        )

        classes = list(classifications.values())
        classes_agree = len(set(classes)) == 1

        if not classes_agree:
            return round(min(score_agreement, _CLASSIFICATION_DISAGREEMENT_CAP), 1)
        return round(score_agreement, 1)

    @staticmethod
    def _build_consensus_reasoning(
        individual: Dict[str, Dict[str, Any]],
        consensus_score: float,
        consensus_classification: str,
        agreement_percentage: float,
    ) -> str:
        per_model = "; ".join(
            f"{name}(score={result['score']:.3f}, classification='{result['classification']}')"
            for name, result in individual.items()
        )
        return (
            f"Ensemble of {len(individual)} splicing/regulatory AI models: {per_model}. "
            f"Consensus score={consensus_score:.3f} ('{consensus_classification}'); "
            f"agreement={agreement_percentage}% between models."
        )
