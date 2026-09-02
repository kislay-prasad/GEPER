"""
AI model status tracking (Objective 6).

GEPER routes each variant to a *subset* of its AI models (see
`pipeline/router.py`), and some models are optional/config-gated
(Enformer, Borzoi) or environment-dependent (HyenaDNA/Evo2/RNA-FM/
ESM-2/MMSplice, each auto-installed and independently available or
not). Historically a model that didn't run for a given variant simply
had no entry anywhere in that variant's result -- which is
indistinguishable, from a report reader's point of view, from "this
model doesn't exist" or "this model silently failed". That ambiguity
is exactly what this module removes: every model GEPER knows about
gets an explicit status for every variant, one of:

    USED     -- ran and returned a real result for this variant.
    SKIPPED  -- available, but not applicable to this variant (wrong
                variant type, non-coding, not routed here, etc).
    DISABLED -- not available in this environment/configuration at
                all (missing optional dependency, or a feature flag
                off).
    FAILED   -- was attempted for this variant and raised/errored.

This module is presentation/bookkeeping only: it reads the same
result dicts every pipeline stage already produced
(`pipeline/orchestrator.py._process_variant`) -- it never runs
inference itself, never mutates any input, and never changes any
existing pipeline behavior. It is a pure function of its inputs, so
it is independently unit-tested without needing a real model, VCF, or
network access (see tests/test_ai_model_status.py).
"""

from typing import Any, Dict, List, Optional

# Canonical, fixed model list + display order for every "AI Models"
# status table GEPER renders. Any model key that exists in the
# pipeline but is missing from this dict is a bug -- see
# tests/test_ai_model_status.py::test_covers_every_known_model.
DISPLAY_NAMES: Dict[str, str] = {
    "hyenadna": "HyenaDNA",
    "evo2": "Evo2",
    "enformer": "Enformer",
    "borzoi": "Borzoi",
    "rna_fm": "RNA-FM",
    "esm2": "ESM-2",
    "alphamissense": "AlphaMissense",
    "mmsplice": "MMSplice",
    "spliceformer": "SpliceFormer",
    "splicebert": "SpliceBERT",
}

# Fixed rendering order (matches the project's own example status
# table): the two DNA sequence-context models, the two new splicing/
# regulatory models, RNA/protein embedding models, then the two
# evidence-scoring predictors.
DISPLAY_ORDER: List[str] = [
    "hyenadna",
    "evo2",
    "enformer",
    "borzoi",
    "rna_fm",
    "esm2",
    "alphamissense",
    "mmsplice",
    "spliceformer",
    "splicebert",
]

USED = "used"
SKIPPED = "skipped"
DISABLED = "disabled"
FAILED = "failed"

# Priority for collapsing one model's per-variant statuses (see
# `rollup_run_status`) into a single run-level status: USED beats
# everything (it genuinely contributed to at least one finding this
# run, even if it was skipped/failed for others), FAILED beats
# DISABLED/SKIPPED (a real problem occurred this run, worth surfacing
# even if it never happened to succeed), DISABLED beats SKIPPED (the
# environment/config gap is worth naming over a plain "not applicable").
_ROLLUP_PRIORITY: Dict[str, int] = {SKIPPED: 0, DISABLED: 1, FAILED: 2, USED: 3}

# Per-model calibration-status vocabulary (sub-pattern B). Distinct
# from USED/SKIPPED/DISABLED/FAILED above -- that quartet answers "did
# this model run for this variant", this one answers "was its output
# a validated clinical measure or a raw model score", and only applies
# to the models that actually address the question (see
# `_CALIBRATION_MODEL_KEYS` below).
#
# All four are non-empty strings -- the same truthiness trap
# `_scope_reason` warns about for `reason` applies here: `if
# cal_status:` is true for every one of them, including
# NOT_EVALUATED. Any read site must compare by equality
# (`== CALIBRATED`, `== UNCALIBRATED`, ...), never by truthiness.
CALIBRATED = "calibrated"
UNCALIBRATED = "uncalibrated"
NOT_EVALUATED = "not_evaluated"
NOT_REPORTED = "not_reported"

# The only models this applies to: four plugins that write
# `details.calibration_status` on every result (Enformer, Borzoi,
# SpliceFormer, SpliceBERT -- each labels its own score "uncalibrated"
# in that field) plus MMSplice, which runs but never writes the field
# at all (NOT_REPORTED, not a fifth state).
#
# Deliberately excludes HyenaDNA, Evo2, RNA-FM, ESM-2, and
# AlphaMissense: none of them return the plugin `details` structure
# this reads from, none are in `provenance.py::
# get_model_checkpoint_identifiers()`'s model-checkpoint set, and they
# have no calibration claim to qualify in the first place. A model
# outside this set simply has no "calibration_status" key in its
# per-variant/run-level entry -- absence, not a fifth NOT_EVALUATED-
# like state. Future readers: do not add a model here just because it
# also happens to be uncalibrated -- it must actually write the field.
#
# SPiP is ALSO deliberately excluded, but for a different reason than
# the five above, worth spelling out separately so it isn't mistaken
# for an oversight: SPiP's own plugin (`spip_plugin.py`) DOES write
# `details.calibration_status` (in fact the only plugin in this family
# that reports itself calibrated, not uncalibrated), but SPiP has no
# per-variant status entry anywhere to attach that value to -- it is
# not called from any orchestrator per-variant stage, is absent from
# `DISPLAY_NAMES`/`DISPLAY_ORDER` above, and never reaches
# `build_ai_model_status()`'s inputs at all (see
# `pipeline/models/pending_plugins.py`'s own module docstring: "SPiP
# alone is genuinely unwired ... absent from the status table"). Wiring
# SPiP into a live per-variant pipeline stage just to make it eligible
# here would be a materially larger, separate change (an R-subprocess
# model entering the per-variant hot path) -- intentionally not done
# as part of this aggregation work. Until that happens, SPiP's own
# calibration claim exists only in its own plugin output and is never
# recorded in any run document.
_CALIBRATION_MODEL_KEYS = frozenset({"enformer", "borzoi", "spliceformer", "splicebert", "mmsplice"})

# Worst-case priority for collapsing one model's per-variant
# calibration_status values (see `_aggregate_calibration_status`) into
# one run-level value: a claim of calibration must never survive being
# contradicted by even a single variant, so UNCALIBRATED outranks
# CALIBRATED. NOT_REPORTED also outranks CALIBRATED -- "the field was
# never written" is not evidence of calibration, so it must not be
# silently read as a calibrated result just because it isn't an
# explicit "uncalibrated" one. NOT_EVALUATED is lowest: any real
# signal (even one uncalibrated/not_reported variant) means the model
# ran and must not be reported as though it never did.
_CALIBRATION_PRIORITY: Dict[str, int] = {NOT_EVALUATED: 0, CALIBRATED: 1, NOT_REPORTED: 2, UNCALIBRATED: 3}


def _classify_calibration_text(raw: Optional[str]) -> str:
    """
    Classifies one fired model's raw `details.calibration_status`
    prose into the run's canonical vocabulary (CALIBRATED/
    UNCALIBRATED/NOT_REPORTED -- never NOT_EVALUATED, which is a
    property of `status`, not of this text; callers only invoke this
    when the model actually fired for the variant).

    Reads the one convention every uncalibrated plugin's own text
    already follows -- `enformer_plugin.py`, `borzoi_plugin.py`,
    `spliceformer_plugin.py`, and `splicebert_plugin.py` each start
    their string with the literal word "uncalibrated". SPiP's plugin
    is the only one in the family that writes a differently-shaped
    calibrated string ("SPiPscore is SPiP's own calibrated..."), which
    is exactly why anything other than a startswith check would need
    per-model special-casing -- SPiP itself is out of scope here (see
    module-level note: unwired, no per-variant status exists to attach
    this to yet).

    A model whose result carries no calibration_status text at all
    (MMSplice, the only model in `_CALIBRATION_MODEL_KEYS` that never
    writes the field) is NOT_REPORTED, not silently treated as
    calibrated -- see `acmg_rules.py`'s own "honest absence, not a
    guess" comment at its calibration_status read site for the same
    principle applied one level up, in report prose.
    """
    if not raw:
        return NOT_REPORTED
    return UNCALIBRATED if raw.strip().lower().startswith("uncalibrated") else CALIBRATED


def _aggregate_calibration_status(counts: Dict[str, int]) -> str:
    """
    Worst-case collapse of one model's per-variant calibration_status
    tally into a single run-level value (sub-pattern B boundary 2).
    Mirrors `_ROLLUP_PRIORITY`'s status collapse, but counts every
    fired variant rather than picking one winning variant's entry --
    a model that produced calibrated output on 99 variants and
    uncalibrated on 1 must still report uncalibrated for the run, so
    every count has to be inspected, not just the highest-priority
    status's own single entry.
    """
    fired_total = sum(n for state, n in counts.items() if state != NOT_EVALUATED)
    if fired_total == 0:
        return NOT_EVALUATED
    uncalibrated_n = counts.get(UNCALIBRATED, 0)
    if uncalibrated_n:
        return f"{UNCALIBRATED} ({uncalibrated_n} of {fired_total} variants)"
    if counts.get(NOT_REPORTED, 0):
        return NOT_REPORTED
    if counts.get(CALIBRATED, 0):
        return CALIBRATED
    return NOT_EVALUATED


def _scope_reason(reason: str, n: int, total: int) -> str:
    """
    Rescopes a `reason` string that `rollup_run_status` is about to
    promote from a single winning variant's per-variant status onto a
    run-level field.

    Every per-variant reason in this module is written in the singular
    ("Ran for THIS variant...", "Not routed to THIS variant...") --
    correct at the per-variant callsite (`build_ai_model_status`,
    untouched by this function), but at one variant that referent is
    invisible and at more than one it has no antecedent, or is
    outright false for the run as a whole (MEDIUM-1: a run-level
    "used, reason: ran for this variant" next to per-variant statuses
    of used/skipped/used/skipped/skipped names an experience only 2 of
    the 5 variants actually had).

    Per the ruling, a run-level reason "must ... name how many
    variants it applied to, or ... use run-level language" -- never
    per-variant wording on a run-level fact. This picks the first
    option (a count is strictly more informative than a vaguer
    run-level rewrite, and unlike hand-rewriting each of this module's
    ~10 distinct per-variant phrasings individually, replacing the
    literal referent works uniformly across all of them and any reason
    text added here in future without per-model special-casing).

    Replaces the "this variant"/"this variant's" referent, when the
    text contains one, with the scope count itself, so the winning
    status's own descriptive detail survives unmodified (e.g. "Ran for
    2 of 5 variants and produced a splicing prediction.") rather than
    being discarded in favor of a generic templated sentence. A reason
    with no such referent (an environment-level DISABLED fact, a
    fallback default, or an arbitrary FAILED exception string) has
    nothing wrong to replace, but the field is still run-level and the
    reader still has no other way to know how many variants it covers,
    so the same scope count is appended instead.
    """
    if total <= 0 or not reason:
        return reason
    word = "variant" if total == 1 else "variants"
    scope = f"{n} of {total} {word}"
    if "this variant's" in reason:
        # Possessive suffix agrees with `word`: plural "variants" takes
        # a bare apostrophe ("variants'"), singular "variant" takes 's
        # ("variant's") -- dropping the "s" for the singular case would
        # silently produce "variant'", which is not a word.
        possessive = f"{scope}'" if word == "variants" else f"{scope}'s"
        return reason.replace("this variant's", possessive)
    if "this variant" in reason:
        return reason.replace("this variant", scope)
    return f"{reason} ({scope} in this run)"


def rollup_run_status(
    all_variant_statuses: List[Optional[Dict[str, Dict[str, str]]]],
) -> Dict[str, Dict[str, str]]:
    """
    Collapses every variant's per-variant `build_ai_model_status()`
    output into one run-level status per model key. This is what
    `pipeline/provenance.py`'s "AI model checkpoints" section reports
    against (F1a, report review round 4): before this, that section
    listed a model's checkpoint identifier purely from config flags,
    with no way to tell a model that actually ran this run from one
    that was configured but never available or never loaded --
    SpliceBERT (load failure) and Evo2 (skipped on unsupported
    hardware) both rendered identically to a model that worked.

    Same USED > FAILED > DISABLED > SKIPPED priority as this module's
    per-variant reasoning (see e.g. `_ensemble_model_status`'s
    docstring): a real success or failure this run must never be
    diluted by averaging against unrelated variants where the model
    simply wasn't applicable. MEDIUM-1: the winning entry's `reason`
    is additionally rescoped (see `_scope_reason`) before being
    returned -- the winning STATUS is deliberately still whichever one
    the priority above picks (out of scope, per the ruling: "the
    defect is not that the rollup is lossy"), but the reason text that
    rides along with it must state how many of the run's variants it
    actually describes, not read as if it were the only variant.
    """
    best: Dict[str, Dict[str, str]] = {}
    status_counts: Dict[str, Dict[str, int]] = {}
    calibration_counts: Dict[str, Dict[str, int]] = {}
    for variant_status in all_variant_statuses or []:
        for key, entry in (variant_status or {}).items():
            status = (entry or {}).get("status", SKIPPED)
            counts = status_counts.setdefault(key, {})
            counts[status] = counts.get(status, 0) + 1
            existing = best.get(key)
            if existing is None or _ROLLUP_PRIORITY.get(status, 0) > _ROLLUP_PRIORITY.get(existing["status"], 0):
                best[key] = dict(entry)
            calibration_status = (entry or {}).get("calibration_status")
            if calibration_status is not None:
                ccounts = calibration_counts.setdefault(key, {})
                ccounts[calibration_status] = ccounts.get(calibration_status, 0) + 1
    for key, entry in best.items():
        counts = status_counts.get(key, {})
        n = counts.get(entry["status"], 0)
        total = sum(counts.values())
        entry["reason"] = _scope_reason(entry.get("reason", ""), n, total)
        # Only models in `_CALIBRATION_MODEL_KEYS` ever have a
        # "calibration_status" key on their per-variant entries (see
        # that constant's own docstring) -- `key in calibration_counts`
        # is exactly that membership test, restated by construction
        # rather than duplicated, so a model added to per-variant
        # tracking later doesn't need this loop touched to stay
        # correctly excluded.
        if key in calibration_counts:
            entry["calibration_status"] = _aggregate_calibration_status(calibration_counts[key])
    return best


def _entry(status: str, reason: str, calibration_status: Optional[str] = None) -> Dict[str, str]:
    entry = {"status": status, "reason": reason}
    if calibration_status is not None:
        entry["calibration_status"] = calibration_status
    return entry


def _dna_context_model_status(
    key: str,
    dna_models_used: List[str],
    dna_model_results: Dict[str, Any],
    model_availability: Dict[str, bool],
    model_stage_errors: Dict[str, str],
) -> Dict[str, str]:
    """HyenaDNA / Evo2: routed per-variant by pipeline/router.py."""
    if key in dna_models_used and dna_model_results.get(key) is not None:
        return _entry(USED, "Routed to this variant and returned a result.")
    if not model_availability.get(key, True):
        return _entry(
            DISABLED,
            "Not available in this environment (missing optional dependency, or -- for Evo2 -- unsupported hardware).",
        )
    if key in model_stage_errors:
        return _entry(FAILED, model_stage_errors[key])
    return _entry(SKIPPED, "Not routed to this variant (see pipeline/router.py).")


def _rna_fm_status(
    rna_result: Optional[Dict[str, Any]],
    model_availability: Dict[str, bool],
    model_stage_errors: Dict[str, str],
) -> Dict[str, str]:
    rna_result = rna_result or {}
    if not rna_result.get("skipped", False) and rna_result.get("embedding_mean") is not None:
        return _entry(USED, "Ran for this variant's RNA context.")
    if "rna_fm" in model_stage_errors:
        return _entry(FAILED, model_stage_errors["rna_fm"])
    if not model_availability.get("rna_fm", True):
        return _entry(DISABLED, rna_result.get("reason", "RNA-FM is not available in this environment."))
    return _entry(SKIPPED, rna_result.get("reason", "Variant not flagged as transcript-relevant."))


def _esm2_status(
    protein_result: Optional[Dict[str, Any]],
    model_availability: Dict[str, bool],
    model_stage_errors: Dict[str, str],
) -> Dict[str, str]:
    protein_result = protein_result or {}
    if protein_result.get("skipped") is False and protein_result.get("esm2") is not None:
        return _entry(USED, "Ran for this variant's translated protein context.")
    if "esm2" in model_stage_errors:
        return _entry(FAILED, model_stage_errors["esm2"])
    if not model_availability.get("esm2", True):
        return _entry(DISABLED, protein_result.get("reason", "ESM-2 is not available in this environment."))
    return _entry(SKIPPED, protein_result.get("reason", "Variant flagged as non-coding, or no ORF found."))


def _alphamissense_status(
    alphamissense_result: Optional[Dict[str, Any]],
    model_availability: Dict[str, bool],
    model_stage_errors: Dict[str, str],
) -> Dict[str, str]:
    alphamissense_result = alphamissense_result or {}
    if alphamissense_result.get("skipped") is False:
        return _entry(USED, "Ran for this variant (eligible missense substitution).")
    reason = alphamissense_result.get("reason", "")
    if "alphamissense" in model_stage_errors:
        return _entry(FAILED, model_stage_errors["alphamissense"])
    if not model_availability.get("alphamissense", True):
        # DISABLED means AlphaMissense itself is unavailable this run -- true
        # for every variant, regardless of what THIS variant's own `reason`
        # says. `_run_alphamissense_stage` checks missense-eligibility
        # *before* availability, so a non-eligible variant's `reason` is a
        # per-variant applicability message ("variant is not an eligible
        # missense substitution...") that must never be shown under a
        # DISABLED label -- that's SKIPPED semantics leaking into DISABLED.
        # Only trust `reason` here when it already names unavailability.
        if "not available" in reason:
            return _entry(DISABLED, reason)
        return _entry(DISABLED, "AlphaMissense is not available in this environment (not installed).")
    if "not available" in reason:
        return _entry(DISABLED, reason)
    return _entry(
        SKIPPED,
        reason or "Variant is not an eligible missense substitution.",
    )


def _mmsplice_status(
    mmsplice_result: Optional[Dict[str, Any]],
    model_availability: Dict[str, bool],
    model_stage_errors: Dict[str, str],
) -> Dict[str, str]:
    mmsplice_result = mmsplice_result or {}
    if mmsplice_result.get("predicted"):
        # MMSplice is the one model in `_CALIBRATION_MODEL_KEYS` that
        # never writes `details.calibration_status` at all -- fired,
        # so not NOT_EVALUATED, but nothing to classify as
        # calibrated/uncalibrated either. `_classify_calibration_text`
        # already returns NOT_REPORTED for an absent/empty string, so
        # this reads the same way every other USED-branch calibration
        # read does rather than hand-writing NOT_REPORTED here.
        raw = (mmsplice_result.get("details") or {}).get("calibration_status")
        return _entry(
            USED,
            "Ran for this variant and produced a splicing prediction.",
            calibration_status=_classify_calibration_text(raw),
        )
    reason = mmsplice_result.get("skip_reason", "")
    if "mmsplice" in model_stage_errors:
        return _entry(FAILED, model_stage_errors["mmsplice"], calibration_status=NOT_EVALUATED)
    if not model_availability.get("mmsplice", True) or "not available" in reason:
        return _entry(
            DISABLED,
            reason or "MMSplice is not available in this environment.",
            calibration_status=NOT_EVALUATED,
        )
    return _entry(
        SKIPPED,
        reason or "Variant is outside MMSplice's supported splice window.",
        calibration_status=NOT_EVALUATED,
    )


def _ensemble_model_status(
    key: str,
    ensemble_result: Optional[Dict[str, Any]],
    plugin_availability: Dict[str, bool],
    plugin_failures: Dict[str, str],
    model_stage_errors: Dict[str, str],
) -> Dict[str, str]:
    """Enformer / Borzoi, via pipeline/models/ensemble.py::EnsembleManager.

    Checks, in order: did it actually run this variant (USED) -> did
    the *ensemble stage itself* blow up (FAILED, via
    model_stage_errors) -> did *this specific plugin* fail to load or
    fail inference, this process or this variant (FAILED, via
    plugin_failures -- covers `ModelManager.failed_keys()` [permanent
    load failure] and `ModelManager.last_inference_errors()`
    [this-variant inference failure]) -> is it simply not enabled/
    installed (DISABLED) -> otherwise SKIPPED. This ordering is what
    ensures a real failure is never silently reported as a plain skip.
    """
    ensemble_result = ensemble_result or {}
    if key in (ensemble_result.get("models_used") or []):
        # `individual_scores[key]` is that model's own raw `predict()`
        # return (see `EnsembleManager.evaluate`) -- same "details"
        # shape `_standalone_plugin_status` reads for SpliceFormer/
        # SpliceBERT, just reached through the ensemble's own
        # aggregation dict instead of a bare per-model result.
        raw = (
            ((ensemble_result.get("individual_scores") or {}).get(key) or {})
            .get("details", {})
            .get("calibration_status")
        )
        return _entry(
            USED,
            "Ran for this variant as part of the AI splicing/regulatory ensemble.",
            calibration_status=_classify_calibration_text(raw),
        )
    if "ai_splicing_ensemble" in model_stage_errors:
        return _entry(FAILED, model_stage_errors["ai_splicing_ensemble"], calibration_status=NOT_EVALUATED)
    if key in plugin_failures:
        return _entry(FAILED, plugin_failures[key], calibration_status=NOT_EVALUATED)
    if not plugin_availability.get(key, False):
        return _entry(
            DISABLED,
            f"Disabled by default (CONFIG.splicing.ENABLE_{key.upper()}), "
            "or its optional pip package is not installed.",
            calibration_status=NOT_EVALUATED,
        )
    # Available, no load/inference failure on record, and not used --
    # e.g. the other model in the pair produced a result while this
    # one's own predict() call simply returned no result.
    return _entry(SKIPPED, "No result produced for this variant.", calibration_status=NOT_EVALUATED)


def _standalone_plugin_status(
    key: str,
    plugin_result: Optional[Dict[str, Any]],
    plugin_availability: Dict[str, bool],
    plugin_failures: Dict[str, str],
    model_stage_errors: Dict[str, str],
) -> Dict[str, str]:
    """SpliceFormer / SpliceBERT, via `pipeline/orchestrator.py::
    _run_standalone_splice_plugin_stage` -> `self.model_manager.predict(key, ...)`
    directly (NOT the Enformer/Borzoi ensemble -- see
    `_ensemble_model_status` above for that pair; these two plugins are
    deliberately excluded from `EnsembleManager._ENSEMBLE_MODEL_KEYS`).

    Same ordering/rationale as `_ensemble_model_status`: a real
    load/inference failure must never be reported as a plain skip.
    """
    plugin_result = plugin_result or {}
    if plugin_result.get("classification") is not None:
        raw = (plugin_result.get("details") or {}).get("calibration_status")
        return _entry(
            USED,
            "Ran for this variant and produced a splicing prediction.",
            calibration_status=_classify_calibration_text(raw),
        )
    if key in model_stage_errors:
        return _entry(FAILED, model_stage_errors[key], calibration_status=NOT_EVALUATED)
    if key in plugin_failures:
        return _entry(FAILED, plugin_failures[key], calibration_status=NOT_EVALUATED)
    if not plugin_availability.get(key, False):
        return _entry(
            DISABLED,
            plugin_result.get("skip_reason")
            or f"Disabled by default (CONFIG.splicing.ENABLE_{key.upper()}), or its optional pip package is not installed.",
            calibration_status=NOT_EVALUATED,
        )
    return _entry(
        SKIPPED,
        plugin_result.get("skip_reason") or "No result produced for this variant.",
        calibration_status=NOT_EVALUATED,
    )


def build_ai_model_status(
    *,
    dna_models_used: List[str],
    dna_model_results: Dict[str, Any],
    rna_result: Optional[Dict[str, Any]],
    protein_result: Optional[Dict[str, Any]],
    alphamissense_result: Optional[Dict[str, Any]],
    mmsplice_result: Optional[Dict[str, Any]],
    ensemble_result: Optional[Dict[str, Any]],
    model_availability: Dict[str, bool],
    plugin_availability: Dict[str, bool],
    model_stage_errors: Dict[str, str],
    plugin_failures: Optional[Dict[str, str]] = None,
    spliceformer_result: Optional[Dict[str, Any]] = None,
    splicebert_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, str]]:
    """
    Returns ``{model_key: {"status": ..., "reason": ...}}`` covering
    every model in `DISPLAY_ORDER` -- never a partial dict, so a
    caller (e.g. the report renderer) never has to guess whether a
    missing key means "disabled" or "forgot to check".
    """
    status: Dict[str, Dict[str, str]] = {}
    plugin_failures = plugin_failures or {}

    for key in ("hyenadna", "evo2"):
        status[key] = _dna_context_model_status(
            key, dna_models_used, dna_model_results, model_availability, model_stage_errors
        )

    status["rna_fm"] = _rna_fm_status(rna_result, model_availability, model_stage_errors)
    status["esm2"] = _esm2_status(protein_result, model_availability, model_stage_errors)
    status["alphamissense"] = _alphamissense_status(alphamissense_result, model_availability, model_stage_errors)
    status["mmsplice"] = _mmsplice_status(mmsplice_result, model_availability, model_stage_errors)
    status["spliceformer"] = _standalone_plugin_status(
        "spliceformer", spliceformer_result, plugin_availability, plugin_failures, model_stage_errors
    )
    status["splicebert"] = _standalone_plugin_status(
        "splicebert", splicebert_result, plugin_availability, plugin_failures, model_stage_errors
    )

    for key in ("enformer", "borzoi"):
        status[key] = _ensemble_model_status(
            key, ensemble_result, plugin_availability, plugin_failures, model_stage_errors
        )

    return status


def render_status_table_lines(status: Dict[str, Dict[str, str]]) -> List[str]:
    """
    Markdown-friendly rendering shared by report_generator.py, grouped
    the way the project's own example table is: a checklist of Used
    models, then Skipped/Disabled/Failed each with their reasons.
    Never raises on an incomplete `status` dict -- a model missing
    from it (should not happen; see `build_ai_model_status`) is
    treated as `SKIPPED` with a generic reason rather than crashing
    report generation.
    """
    lines: List[str] = []
    used = [k for k in DISPLAY_ORDER if status.get(k, {}).get("status") == USED]
    skipped = [k for k in DISPLAY_ORDER if status.get(k, {}).get("status") == SKIPPED]
    disabled = [k for k in DISPLAY_ORDER if status.get(k, {}).get("status") == DISABLED]
    failed = [k for k in DISPLAY_ORDER if status.get(k, {}).get("status") == FAILED]

    for key in used:
        lines.append(f"- \u2714 {DISPLAY_NAMES.get(key, key)}")
    if skipped:
        lines.append("")
        lines.append("Skipped:")
        for key in skipped:
            reason = status.get(key, {}).get("reason", "not applicable")
            lines.append(f"- {DISPLAY_NAMES.get(key, key)}: {reason}")
    if disabled:
        lines.append("")
        lines.append("Disabled:")
        for key in disabled:
            reason = status.get(key, {}).get("reason", "disabled")
            lines.append(f"- {DISPLAY_NAMES.get(key, key)}: {reason}")
    if failed:
        lines.append("")
        lines.append("Failed:")
        for key in failed:
            reason = status.get(key, {}).get("reason", "error")
            lines.append(f"- {DISPLAY_NAMES.get(key, key)}: {reason}")
    return lines
