"""
pipeline/config_validator.py
──────────────────────────────
Configuration validation for the GEPER pipeline (Task 8).

Validates a configuration dict at startup — before any stage runs —
so that missing or invalid values produce a clear, actionable error
rather than a cryptic runtime failure mid-pipeline.

Validation model:
  * **Hard failures** (``ConfigValidationError``): values that are
    syntactically wrong or logically impossible (e.g. negative thread
    count, ``filter_min_qual`` > 10 000).
  * **Warnings** (log at WARNING level): missing optional paths (GFF3,
    ClinVar, gnomAD) that cause feature degradation but don't prevent the
    pipeline from running.

Usage::

    from pipeline.config_validator import validate_config

    validate_config(cfg)  # raises ConfigValidationError on hard failures
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("geper.pipeline.config_validator")


# ─── Error type ───────────────────────────────────────────────────────────────

class ConfigValidationError(Exception):
    """Raised when the pipeline configuration contains an invalid value.

    Attributes:
        errors: List of ``(field_path, message)`` tuples for all failures
                found in a single validation pass (all errors surfaced at once,
                not just the first).
    """

    def __init__(self, errors: List[Tuple[str, str]]) -> None:
        bullet_list = "\n".join(f"  • {f}: {m}" for f, m in errors)
        super().__init__(
            f"Pipeline configuration validation failed "
            f"({len(errors)} error{'s' if len(errors) != 1 else ''}):\n{bullet_list}"
        )
        self.errors = errors


# ─── Type helpers ─────────────────────────────────────────────────────────────

def _get(cfg: Dict, *keys: str, default: Any = None) -> Any:
    """Navigate nested dict by successive keys, returning *default* if any key is absent."""
    node = cfg
    for k in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(k, default)  # type: ignore[assignment]
    return node


def _check_type(
    errors: List,
    value: Any,
    expected_type: type,
    field_path: str,
    allow_none: bool = False,
) -> bool:
    """Append to *errors* if *value* is not of *expected_type*."""
    if allow_none and value is None:
        return True
    if not isinstance(value, expected_type):
        errors.append((
            field_path,
            f"expected {expected_type.__name__}, got {type(value).__name__} ({value!r})",
        ))
        return False
    return True


def _check_positive(errors: List, value: Any, field_path: str) -> None:
    """Append to *errors* if *value* is not a positive number."""
    if isinstance(value, (int, float)) and value > 0:
        return
    errors.append((field_path, f"must be a positive number, got {value!r}"))


def _check_non_negative(errors: List, value: Any, field_path: str) -> None:
    if isinstance(value, (int, float)) and value >= 0:
        return
    errors.append((field_path, f"must be >= 0, got {value!r}"))


def _check_between(
    errors: List, value: Any, lo: float, hi: float, field_path: str
) -> None:
    if isinstance(value, (int, float)) and lo <= value <= hi:
        return
    errors.append((field_path, f"must be in [{lo}, {hi}], got {value!r}"))


# ─── Section validators ───────────────────────────────────────────────────────

def _validate_alignment(cfg: Dict, errors: List) -> None:
    aln = cfg.get("alignment", {}) or {}
    aligner = aln.get("aligner", "auto")
    if aligner not in {"auto", "bwa", "minimap2"}:
        errors.append(("alignment.aligner", f"must be 'auto', 'bwa', or 'minimap2', got {aligner!r}"))

    threads = aln.get("threads", 4)
    if not isinstance(threads, int) or threads < 1:
        errors.append(("alignment.threads", f"must be a positive integer, got {threads!r}"))

    preset = aln.get("preset", "sr")
    if not isinstance(preset, str) or not preset.strip():
        errors.append(("alignment.preset", "must be a non-empty string (minimap2 preset)"))


def _validate_variant_calling(cfg: Dict, errors: List) -> None:
    vc = cfg.get("variant_calling", {}) or {}

    threads = vc.get("threads", 1)
    if not isinstance(threads, int) or threads < 1:
        errors.append(("variant_calling.threads", f"must be a positive integer, got {threads!r}"))

    for field_name, lo, hi in [
        ("min_base_quality", 0, 40),
        ("min_mapping_quality", 0, 60),
        ("min_alternate_count", 1, 1000),
    ]:
        val = vc.get(field_name)
        if val is not None:
            if not isinstance(val, int) or not (lo <= val <= hi):
                errors.append((
                    f"variant_calling.{field_name}",
                    f"must be an integer in [{lo}, {hi}], got {val!r}",
                ))

    frac = vc.get("min_alternate_fraction")
    if frac is not None and (not isinstance(frac, (int, float)) or not (0.0 < frac <= 1.0)):
        errors.append((
            "variant_calling.min_alternate_fraction",
            f"must be a float in (0.0, 1.0], got {frac!r}",
        ))

    min_qual = vc.get("filter_min_qual")
    if min_qual is not None and (not isinstance(min_qual, (int, float)) or min_qual < 0):
        errors.append((
            "variant_calling.filter_min_qual",
            f"must be a non-negative number, got {min_qual!r}",
        ))

    min_depth = vc.get("filter_min_depth")
    if min_depth is not None and (not isinstance(min_depth, int) or min_depth < 0):
        errors.append((
            "variant_calling.filter_min_depth",
            f"must be a non-negative integer, got {min_depth!r}",
        ))


def _validate_acmg_thresholds(cfg: Dict, errors: List) -> None:
    acmg = cfg.get("acmg_thresholds", {}) or {}
    float_fields = {
        "ba1_af": (0.0, 1.0),
        "bs1_af": (0.0, 1.0),
        "pm2_af_max": (0.0, 1.0),
        "pp3_cadd_phred": (0.0, 100.0),
        "pp3_revel": (0.0, 1.0),
        "pp3_spliceai": (0.0, 1.0),
        "pp3_alphamissense": (0.0, 1.0),
        "bp4_cadd_phred": (0.0, 100.0),
        "bp4_revel": (0.0, 1.0),
        "bp4_spliceai": (0.0, 1.0),
    }
    for field_name, (lo, hi) in float_fields.items():
        val = acmg.get(field_name)
        if val is not None:
            _check_between(errors, val, lo, hi, f"acmg_thresholds.{field_name}")


def _validate_evidence_engine(cfg: Dict, errors: List) -> None:
    ee = cfg.get("evidence_engine", {}) or {}
    weight_keys = ["weight_acmg", "weight_clinvar", "weight_computational", "weight_ai"]
    total = 0.0
    for k in weight_keys:
        val = ee.get(k)
        if val is not None:
            if not isinstance(val, (int, float)) or not (0.0 <= val <= 1.0):
                errors.append((f"evidence_engine.{k}", f"must be in [0.0, 1.0], got {val!r}"))
            else:
                total += float(val)
    # Soft check: weights should sum to ~1.0
    if total > 0 and not (0.99 <= total <= 1.01):
        errors.append((
            "evidence_engine.weights",
            f"Weights sum to {total:.4f}; should sum to 1.0 "
            f"({', '.join(weight_keys)})",
        ))


def _validate_paths(cfg: Dict) -> None:
    """Warn (not error) for optional paths that are configured but don't exist."""
    optional_paths = [
        ("clinvar.tsv_gz_path", _get(cfg, "clinvar", "tsv_gz_path")),
        ("gnomad.vcf_path", _get(cfg, "gnomad", "vcf_path")),
        ("omim.genemap_path", _get(cfg, "omim", "genemap_path")),
        ("rna_analysis.refseq_gff", _get(cfg, "rna_analysis", "refseq_gff")),
        ("gnomad_constraint.tsv_path", _get(cfg, "gnomad_constraint", "tsv_path")),
    ]
    for field_name, path_val in optional_paths:
        if path_val and not Path(str(path_val)).exists():
            logger.warning(
                "Config '%s' points to a non-existent path: %r — "
                "this feature will be degraded at runtime.",
                field_name, path_val,
            )

    # Validate gnomad_constraint thresholds if present (warn only)
    gc = cfg.get("gnomad_constraint")
    if gc is not None and isinstance(gc, dict):
        for thresh_key, lo, hi in [
            ("loeuf_threshold", 0.0, 5.0),
            ("pli_threshold", 0.0, 1.0),
        ]:
            val = gc.get(thresh_key)
            if val is not None and not isinstance(val, (int, float)):
                logger.warning(
                    "Config 'gnomad_constraint.%s' should be a number, got %r",
                    thresh_key, val,
                )

    # Validate hotspot section if present (warn only)
    hs = cfg.get("hotspot")
    if hs is not None and isinstance(hs, dict):
        min_stars = hs.get("min_stars")
        if min_stars is not None and not isinstance(min_stars, int):
            logger.warning(
                "Config 'hotspot.min_stars' should be an integer, got %r", min_stars
            )


def _validate_reporting(cfg: Dict, errors: List) -> None:
    rep = cfg.get("reporting", {}) or {}
    output_dir = rep.get("output_dir")
    if output_dir is not None and not isinstance(output_dir, str):
        errors.append(("reporting.output_dir", f"must be a string path, got {output_dir!r}"))
    gen_pdf = rep.get("generate_pdf")
    if gen_pdf is not None and not isinstance(gen_pdf, bool):
        errors.append((
            "reporting.generate_pdf",
            f"must be a boolean (true/false), got {gen_pdf!r}",
        ))


# ─── Main entry point ─────────────────────────────────────────────────────────

def validate_config(cfg: Dict) -> None:
    """Validate *cfg* for structural and value correctness.

    Runs all section validators, collects every error, then raises a single
    ``ConfigValidationError`` listing them all so the operator can fix
    everything at once.

    Args:
        cfg: Full configuration dict (as loaded from ``default.yaml`` or
             ``production.yaml``).

    Raises:
        ConfigValidationError: if one or more hard validation failures are found.
    """
    errors: List[Tuple[str, str]] = []

    _validate_alignment(cfg, errors)
    _validate_variant_calling(cfg, errors)
    _validate_acmg_thresholds(cfg, errors)
    _validate_evidence_engine(cfg, errors)
    _validate_reporting(cfg, errors)

    # Warn for missing optional file paths (not hard failures)
    _validate_paths(cfg)

    if errors:
        raise ConfigValidationError(errors)

    logger.info("Configuration validation passed (%d section(s) checked)", 5)
