"""
tests/test_config_validator.py
────────────────────────────────
Tests for pipeline/config_validator.py.
All tests run without any external tools.
"""

from __future__ import annotations

import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.config_validator import validate_config, ConfigValidationError


def _base_cfg() -> dict:
    """Return a minimal valid configuration dict."""
    return {
        "alignment": {"aligner": "auto", "threads": 4, "preset": "sr"},
        "variant_calling": {
            "threads": 1,
            "min_base_quality": 20,
            "min_mapping_quality": 20,
            "min_alternate_fraction": 0.2,
            "min_alternate_count": 2,
            "filter_min_qual": 20.0,
            "filter_min_depth": 10,
        },
        "acmg_thresholds": {
            "ba1_af": 0.05,
            "bs1_af": 0.01,
            "pm2_af_max": 0.0001,
            "pp3_cadd_phred": 20.0,
            "pp3_revel": 0.5,
            "pp3_spliceai": 0.2,
            "pp3_alphamissense": 0.564,
            "bp4_cadd_phred": 10.0,
            "bp4_revel": 0.15,
            "bp4_spliceai": 0.1,
        },
        "evidence_engine": {
            "weight_acmg": 0.45,
            "weight_clinvar": 0.25,
            "weight_computational": 0.20,
            "weight_ai": 0.10,
        },
        "reporting": {"output_dir": "/tmp/geper_reports", "generate_pdf": True},
    }


class TestValidConfig:
    def test_valid_config_passes(self):
        validate_config(_base_cfg())  # must not raise

    def test_empty_config_passes(self):
        """An empty config is valid — all sections are optional."""
        validate_config({})

    def test_partial_config_passes(self):
        """Only some sections defined — others use defaults at runtime."""
        validate_config({"alignment": {"aligner": "bwa", "threads": 2, "preset": "sr"}})


class TestAlignmentValidation:
    def test_invalid_aligner_raises(self):
        cfg = _base_cfg()
        cfg["alignment"]["aligner"] = "bowtie"
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(cfg)
        assert "alignment.aligner" in str(exc_info.value)

    def test_zero_threads_raises(self):
        cfg = _base_cfg()
        cfg["alignment"]["threads"] = 0
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(cfg)
        assert "alignment.threads" in str(exc_info.value)

    def test_negative_threads_raises(self):
        cfg = _base_cfg()
        cfg["alignment"]["threads"] = -1
        with pytest.raises(ConfigValidationError):
            validate_config(cfg)

    def test_float_threads_raises(self):
        cfg = _base_cfg()
        cfg["alignment"]["threads"] = 2.5
        with pytest.raises(ConfigValidationError):
            validate_config(cfg)


class TestVariantCallingValidation:
    def test_invalid_alternate_fraction_raises(self):
        cfg = _base_cfg()
        cfg["variant_calling"]["min_alternate_fraction"] = 1.5
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(cfg)
        assert "min_alternate_fraction" in str(exc_info.value)

    def test_negative_filter_qual_raises(self):
        cfg = _base_cfg()
        cfg["variant_calling"]["filter_min_qual"] = -5.0
        with pytest.raises(ConfigValidationError):
            validate_config(cfg)

    def test_negative_min_depth_raises(self):
        cfg = _base_cfg()
        cfg["variant_calling"]["filter_min_depth"] = -1
        with pytest.raises(ConfigValidationError):
            validate_config(cfg)


class TestEvidenceWeights:
    def test_weights_not_summing_to_one_raises(self):
        cfg = _base_cfg()
        cfg["evidence_engine"]["weight_acmg"] = 0.90  # total will be >> 1.0
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(cfg)
        assert "weights" in str(exc_info.value).lower()

    def test_weight_above_one_raises(self):
        cfg = _base_cfg()
        cfg["evidence_engine"]["weight_acmg"] = 1.5
        with pytest.raises(ConfigValidationError):
            validate_config(cfg)


class TestMultipleErrors:
    def test_multiple_errors_reported_at_once(self):
        """All validation errors are surfaced in a single raise."""
        cfg = _base_cfg()
        cfg["alignment"]["aligner"] = "bad"
        cfg["alignment"]["threads"] = 0
        cfg["variant_calling"]["filter_min_qual"] = -1
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(cfg)
        # At least 3 errors reported
        assert len(exc_info.value.errors) >= 3
