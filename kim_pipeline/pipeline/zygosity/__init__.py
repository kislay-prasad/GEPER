"""
pipeline/zygosity
─────────────────
Zygosity extraction utilities for VCF genotype fields.
"""

from pipeline.zygosity.extractor import ZygosityExtractor, ZygosityResult

__all__ = ["ZygosityExtractor", "ZygosityResult"]
