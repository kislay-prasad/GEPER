"""
pipeline/variant_calling/stage.py
────────────────────────────────────
VariantCallingStage — BAM + reference genome → variants.vcf (raw
FreeBayes calls) + filtered_variants.vcf (PASS-only).

Same naming convention as `pipeline/alignment/stage.py:AlignmentStage`
and the other sibling `<Name>Stage` classes in this package.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from geper.pipeline.fastq.pipeline import FastqPipelineError

from shared.process_control import spawn_tracked, kill_process_tree_now

from . import freebayes_runner
from .filtering import FilterThresholds, apply_pass_filter

logger = logging.getLogger("geper.pipeline.variant_calling.stage")


@dataclass
class VariantCallingResult:
    sample_id: str = ""
    caller: str = "freebayes"
    bam_path: str = ""
    reference_fasta: str = ""
    raw_vcf_path: str = ""
    filtered_vcf_path: str = ""
    total_variants: int = 0
    pass_variants: int = 0
    snvs_pass: int = 0
    indels_pass: int = 0
    filter_thresholds: Dict = field(default_factory=dict)
    elapsed_seconds: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "sample_id": self.sample_id,
            "caller": self.caller,
            "bam_path": self.bam_path,
            "reference_fasta": self.reference_fasta,
            "raw_vcf_path": self.raw_vcf_path,
            "filtered_vcf_path": self.filtered_vcf_path,
            "total_variants": self.total_variants,
            "pass_variants": self.pass_variants,
            "snvs_pass": self.snvs_pass,
            "indels_pass": self.indels_pass,
            "filter_thresholds": self.filter_thresholds,
            "elapsed_seconds": self.elapsed_seconds,
        }


class VariantCallingStage:
    """
    Args (config, under cfg["variant_calling"]):
        threads               int    (default 1)
        min_base_quality      int    (default 20)
        min_mapping_quality   int    (default 20)
        min_alternate_fraction float (default 0.2)
        min_alternate_count   int    (default 2)
        filter_min_qual       float  (default 20.0)
        filter_min_depth      int    (default 10)
    """

    def __init__(self, cfg: Optional[Dict] = None):
        self._cfg = (cfg or {}).get("variant_calling", {}) or {}

    def run(
        self,
        bam_path: str,
        reference_fasta: str,
        output_dir: str,
        sample_id: str = "SAMPLE",
        threads: Optional[int] = None,
        thresholds: Optional[FilterThresholds] = None,
    ) -> VariantCallingResult:
        t0 = time.time()

        if not freebayes_runner.is_available():
            raise FastqPipelineError(
                "FreeBayes is not installed. Install it (e.g. "
                "'conda install -c bioconda freebayes' or build from "
                "https://github.com/freebayes/freebayes) before running "
                "the variant_calling stage — no other caller is "
                "substituted automatically.",
                stage="variant_calling",
                tool="freebayes",
            )

        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        raw_vcf_path = str(out / "variants.vcf")
        filtered_vcf_path = str(out / "filtered_variants.vcf")

        threads = threads if threads is not None else int(self._cfg.get("threads", 1))
        thresholds = thresholds or FilterThresholds(
            min_qual=float(self._cfg.get("filter_min_qual", 20.0)),
            min_depth=int(self._cfg.get("filter_min_depth", 10)),
        )

        logger.info(
            "[%s] VariantCallingStage: caller=freebayes threads=%d bam=%s",
            sample_id,
            threads,
            bam_path,
        )

        freebayes_runner.run_freebayes(
            bam_path,
            reference_fasta,
            raw_vcf_path,
            sample_id=sample_id,
            threads=threads,
            min_base_quality=int(self._cfg.get("min_base_quality", 20)),
            min_mapping_quality=int(self._cfg.get("min_mapping_quality", 20)),
            min_alternate_fraction=float(self._cfg.get("min_alternate_fraction", 0.2)),
            min_alternate_count=int(self._cfg.get("min_alternate_count", 2)),
        )

        # FIX 2: bcftools norm — left-align and right-trim indels before annotation.
        # This ensures canonical indel representation so ClinVar, gnomAD, and PGx
        # matching work correctly on indels. Requires bcftools ≥ 1.10.
        normalised_vcf_path = str(out / "variants.norm.vcf")
        _norm_performed = False
        try:
            _bcftools_check = spawn_tracked(
                ["bcftools", "--version"], capture_output=True, timeout=5
            )
            try:
                _bcftools_check.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                kill_process_tree_now(_bcftools_check)
                # Version check timed out; skip normalization
                logger.warning(
                    "[%s] bcftools version check timed out — skipping normalisation",
                    sample_id,
                )
            else:
                if _bcftools_check.returncode == 0:
                    _norm_cmd = [
                        "bcftools",
                        "norm",
                        "--fasta-ref",
                        reference_fasta,  # left-align against reference
                        "--multiallelics",
                        "-",  # split multi-allelic records
                        "--output-type",
                        "v",  # uncompressed VCF output
                        "--output",
                        normalised_vcf_path,
                        raw_vcf_path,
                    ]
                    _norm_result = spawn_tracked(
                        _norm_cmd, capture_output=True, text=True, timeout=300
                    )
                    try:
                        _norm_result.communicate(timeout=300)
                    except subprocess.TimeoutExpired:
                        kill_process_tree_now(_norm_result)
                        logger.warning(
                            "[%s] bcftools norm timed out after 300s",
                            sample_id,
                        )
                    else:
                        if _norm_result.returncode == 0:
                            logger.info(
                                "[%s] bcftools norm complete: left-aligned and trimmed indels → %s",
                                sample_id,
                                normalised_vcf_path,
                            )
                            _norm_performed = True
                        else:
                            logger.warning(
                                "[%s] bcftools norm failed (rc=%d): %s — using raw VCF",
                                sample_id,
                                _norm_result.returncode,
                                _norm_result.stderr[:200] if _norm_result.stderr else "(no stderr)",
                            )
        except (FileNotFoundError, Exception) as _norm_exc:
            logger.warning(
                "[%s] bcftools norm unavailable (%s) — skipping normalisation",
                sample_id,
                _norm_exc,
            )

        # Use normalised VCF if available, else fall back to raw FreeBayes output
        vcf_for_filtering = normalised_vcf_path if _norm_performed else raw_vcf_path

        filter_summary = apply_pass_filter(vcf_for_filtering, filtered_vcf_path, thresholds)

        result = VariantCallingResult(
            sample_id=sample_id,
            caller="freebayes",
            bam_path=bam_path,
            reference_fasta=reference_fasta,
            raw_vcf_path=raw_vcf_path,
            filtered_vcf_path=filtered_vcf_path,
            total_variants=filter_summary.total_input,
            pass_variants=filter_summary.total_pass,
            snvs_pass=filter_summary.snvs_pass,
            indels_pass=filter_summary.indels_pass,
            filter_thresholds=filter_summary.thresholds_used,
            elapsed_seconds=round(time.time() - t0, 3),
        )

        logger.info(
            "[%s] Variant calling complete: %d/%d PASS (%d SNVs, %d indels) in %.2fs",
            sample_id,
            result.pass_variants,
            result.total_variants,
            result.snvs_pass,
            result.indels_pass,
            result.elapsed_seconds,
        )
        return result
