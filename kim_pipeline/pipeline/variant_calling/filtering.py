"""
pipeline/variant_calling/filtering.py
────────────────────────────────────────
PASS filtering for the raw FreeBayes VCF.

Two-step, both via bcftools (already a hard dependency elsewhere in
GEPER — `geper/pipeline/fastq/pipeline.py` uses it for norm/index, so
this doesn't add a new external dependency):

  1. `bcftools filter -s ... -e ...`  — soft-filter: tags FILTER on
     every record that fails the threshold expression, keeps all
     records (so `variants.vcf` stays the complete, unfiltered call set
     and nothing is silently dropped before this step).
  2. `bcftools view -f PASS`          — subsets to PASS-only records,
     written to `filtered_variants.vcf`.

Thresholds are configurable (`FilterThresholds`), not hardcoded. They
are a reasonable, commonly-used FreeBayes hard-filter starting point —
NOT a clinically validated filter set. Treat them as a default to
tune per assay, the same way the rest of this codebase treats
ACMG/evidence-engine thresholds as configuration, not gospel.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional

from geper.pipeline.fastq.pipeline import FastqPipelineError, _require, _run

logger = logging.getLogger("geper.pipeline.variant_calling.filtering")


@dataclass
class FilterThresholds:
    min_qual: float = 20.0
    min_depth: int = 10
    filter_name: str = "LowQual"

    def to_expression(self) -> str:
        """bcftools filter -e expression: matches records to EXCLUDE
        (i.e. tag as failing) from PASS."""
        return f"QUAL<{self.min_qual} || INFO/DP<{self.min_depth}"

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class FilterSummary:
    total_input: int = 0
    total_pass: int = 0
    total_filtered_out: int = 0
    snvs_pass: int = 0
    indels_pass: int = 0
    thresholds_used: Dict = None
    filtered_vcf_path: str = ""

    def to_dict(self) -> Dict:
        d = asdict(self)
        return d


def _parse_variant_counts(vcf_path: str) -> Dict[str, int]:
    """Real counts from `bcftools stats`, same technique used inline in
    `geper/pipeline/fastq/pipeline.py:run_variant_calling` — pulled out
    as a standalone, reusable function here since the original isn't
    exposed as one.
    """
    bcftools = _require("bcftools", "variant_calling.stats")
    result = _run([bcftools, "stats", vcf_path], stage="variant_calling.stats")
    counts = {"records": 0, "snps": 0, "indels": 0}
    for line in result.stdout.splitlines():
        if not line.startswith("SN"):
            continue
        if "number of records:" in line:
            counts["records"] = int(line.split()[-1])
        elif "number of SNPs:" in line:
            counts["snps"] = int(line.split()[-1])
        elif "number of indels:" in line:
            counts["indels"] = int(line.split()[-1])
    return counts


def apply_pass_filter(
    input_vcf: str,
    output_vcf: str,
    thresholds: Optional[FilterThresholds] = None,
) -> FilterSummary:
    """Apply `thresholds` to `input_vcf` and write the PASS-only subset
    to `output_vcf`. Returns a `FilterSummary` with real before/after
    counts (via `bcftools stats`), not estimated ones.
    """
    thresholds = thresholds or FilterThresholds()
    if not Path(input_vcf).exists():
        raise FastqPipelineError(
            f"Input VCF not found: {input_vcf!r}", stage="variant_calling.filter"
        )

    bcftools = _require("bcftools", "variant_calling.filter")
    Path(output_vcf).parent.mkdir(parents=True, exist_ok=True)

    # Step 0: Normalize — split multi-allelic sites before filtering
    normalized_vcf = str(Path(output_vcf).with_name("normalized.vcf"))
    _run(
        [bcftools, "norm", "-m-any", "-O", "v", "-o", normalized_vcf, input_vcf],
        stage="variant_calling.filter",
    )
    input_counts = _parse_variant_counts(input_vcf)
    norm_counts = _parse_variant_counts(normalized_vcf)
    logger.info(
        "bcftools norm: %d input records -> %d normalized records",
        input_counts["records"],
        norm_counts["records"],
    )

    soft_filtered = str(Path(output_vcf).with_name("soft_filtered.vcf"))
    _run(
        [
            bcftools,
            "filter",
            "-s",
            thresholds.filter_name,
            "-e",
            thresholds.to_expression(),
            "-O",
            "v",
            "-o",
            soft_filtered,
            normalized_vcf,
        ],
        stage="variant_calling.filter",
    )

    _run(
        [bcftools, "view", "-f", "PASS", "-O", "v", "-o", output_vcf, soft_filtered],
        stage="variant_calling.filter",
    )
    Path(soft_filtered).unlink(missing_ok=True)
    Path(normalized_vcf).unlink(missing_ok=True)

    pass_counts = _parse_variant_counts(output_vcf)

    summary = FilterSummary(
        total_input=input_counts["records"],
        total_pass=pass_counts["records"],
        total_filtered_out=input_counts["records"] - pass_counts["records"],
        snvs_pass=pass_counts["snps"],
        indels_pass=pass_counts["indels"],
        thresholds_used=thresholds.to_dict(),
        filtered_vcf_path=output_vcf,
    )
    logger.info(
        "PASS filter: %d/%d records passed (%d SNVs, %d indels) -> %s",
        summary.total_pass,
        summary.total_input,
        summary.snvs_pass,
        summary.indels_pass,
        output_vcf,
    )
    return summary
