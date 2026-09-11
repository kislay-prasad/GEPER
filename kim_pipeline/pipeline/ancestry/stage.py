"""
pipeline/ancestry/stage.py
───────────────────────────
AncestryStage: infer genetic ancestry superpopulation from a VCF.

Uses a panel of 101 ancestry-informative markers (AIMs) embedded in markers.py.
Runs maximum-likelihood estimation over 1000 Genomes superpopulations:
  AFR, AMR, EAS, EUR, SAS

Writes JSON and HTML ancestry reports to output_dir.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional, Tuple

from pipeline.ancestry.markers import AIM_PANEL, POPULATIONS

logger = logging.getLogger("geper.pipeline.ancestry")

_MIN_MARKERS_HIGH = 30  # fraction of panel called → "High" confidence
_MIN_MARKERS_MED = 10  # fraction → "Medium"
_EPSILON = 1e-10  # avoid log(0)


# ─── Result dataclass ─────────────────────────────────────────────────────────


@dataclass
class AncestryResult:
    """Ancestry inference result for a sample."""

    sample_id: str = ""
    vcf_path: str = ""
    primary_population: str = "Unknown"
    population_probabilities: Dict[str, float] = field(default_factory=dict)
    confidence: str = "Low"
    markers_evaluated: int = 0
    markers_called: int = 0
    report_json_path: str = ""
    report_html_path: str = ""
    elapsed_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def is_json_serialisable(self) -> bool:
        try:
            json.dumps(self.to_dict())
            return True
        except (TypeError, ValueError):
            return False


# ─── VCF genotype reader ──────────────────────────────────────────────────────


def _parse_vcf_genotypes(
    vcf_path: str,
) -> Dict[Tuple[str, int], Tuple[str, str]]:
    """Read a VCF and return genotypes at AIM positions.

    Returns:
        Dict mapping (chrom_no_prefix, pos) → (ref, alt) for each
        variant record present in the VCF. Both homozygous ref, het, and
        hom-alt records are included.  Missing / monomorphic positions are
        simply absent from the dict.
    """
    genotypes: Dict[Tuple[str, int], Tuple[str, str]] = {}
    if not vcf_path or not os.path.isfile(vcf_path):
        return genotypes

    try:
        with open(vcf_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 5:
                    continue
                chrom = parts[0].lstrip("chr")
                try:
                    pos = int(parts[1])
                except ValueError:
                    continue
                ref = parts[3].upper()
                alt = parts[4].upper()
                if alt in (".", ""):
                    continue
                # Take first alt allele only
                alt_main = alt.split(",")[0].strip()
                genotypes[(chrom, pos)] = (ref, alt_main)
    except Exception as exc:
        logger.warning("[Ancestry] Error parsing VCF %s: %s", vcf_path, exc)

    return genotypes


# ─── Genotype dosage ──────────────────────────────────────────────────────────


def _alt_dosage(gt_present: bool) -> int:
    """Return alt-allele dosage: 2 if variant call present (hom-alt assumption),
    1 for heterozygous (treated as 1 alt copy), 0 for absent.

    Since we only have presence/absence from the VCF here (no FORMAT field
    parsing), we treat presence of the alt allele record as dosage=1 (het).
    This is a simplification appropriate for AIM-based estimation.
    """
    return 1 if gt_present else 0


# ─── Maximum-likelihood estimation ────────────────────────────────────────────


def _mle_ancestry(
    called_markers: Dict[str, bool],  # rsid → True if alt allele observed
) -> Dict[str, float]:
    """Compute log-likelihood per population and return normalised probabilities.

    For each called marker:
      L(pop) += log(p_alt * dosage + p_ref * (1-dosage))   [simplified]

    We use the log-sum-exp trick to compute normalised probabilities.

    Args:
        called_markers: rsid → True (alt observed) / False (ref-only).

    Returns:
        Dict mapping population code → probability (sums to ~1.0).
    """
    log_likelihoods: Dict[str, float] = {pop: 0.0 for pop in POPULATIONS}

    for rsid, alt_observed in called_markers.items():
        marker = AIM_PANEL.get(rsid)
        if marker is None:
            continue
        freqs = marker["allele_frequencies"]
        for pop in POPULATIONS:
            p_alt = freqs.get(pop, 0.5)
            p_ref = 1.0 - p_alt
            if alt_observed:
                # alt allele observed (dosage 1 out of 2 → het)
                # P(het) = 2 * p_alt * p_ref; P(hom_alt) = p_alt^2
                # Use simplified: P(alt seen) = p_alt
                prob = max(p_alt, _EPSILON)
            else:
                # Only ref allele observed
                prob = max(p_ref, _EPSILON)
            log_likelihoods[pop] += math.log(prob)

    # Normalise via log-sum-exp
    if not log_likelihoods:
        return {pop: 1.0 / len(POPULATIONS) for pop in POPULATIONS}

    max_ll = max(log_likelihoods.values())
    exp_vals = {pop: math.exp(ll - max_ll) for pop, ll in log_likelihoods.items()}
    total = sum(exp_vals.values())
    if total <= 0:
        return {pop: 1.0 / len(POPULATIONS) for pop in POPULATIONS}

    return {pop: round(v / total, 6) for pop, v in exp_vals.items()}


# ─── Confidence level ─────────────────────────────────────────────────────────


def _confidence_level(markers_called: int) -> str:
    if markers_called >= _MIN_MARKERS_HIGH:
        return "High"
    if markers_called >= _MIN_MARKERS_MED:
        return "Medium"
    return "Low"


# ─── Report generators ────────────────────────────────────────────────────────

# Ratified 2026-09-11 (card REG-the-ANCESTRY-INFERENCE-REPORT-carries-the-same-
# superseded-research-use-only-claim); replaces "For research use only.", the
# superseded whole-product positioning. Written for THIS artefact, which infers
# ancestry and classifies nothing -- the classifier disclaimers do not fit it.
# Clinician-facing text: change only with human sign-off.
ANCESTRY_SCOPE_STATEMENT = (
    "This inference does not classify or interpret any variant, does not contribute to or "
    "feed any variant classification produced elsewhere in this pipeline, and does not by "
    "itself constitute clinical advice; always validate results with certified diagnostic "
    "tools and qualified clinical professionals."
)


def _write_json_report(result: AncestryResult, output_dir: str) -> str:
    path = os.path.join(output_dir, "ancestry_report.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result.to_dict(), fh, indent=2, default=str)
    return path


def _write_html_report(result: AncestryResult, output_dir: str) -> str:
    path = os.path.join(output_dir, "ancestry_report.html")
    sorted_pops = sorted(result.population_probabilities.items(), key=lambda x: -x[1])
    pop_rows = "\n".join(
        f"<tr><td>{pop}</td><td>{prob:.4f}</td>"
        f"<td><div style='background:#2980b9;height:14px;width:{int(prob * 300)}px'></div></td></tr>"
        for pop, prob in sorted_pops
    )
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Ancestry Report — {result.sample_id}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 2em; }}
    h1 {{ color: #2c3e50; }}
    table {{ border-collapse: collapse; margin-top: 1em; }}
    th {{ background: #2980b9; color: white; padding: 8px 14px; text-align: left; }}
    td {{ padding: 6px 14px; border-bottom: 1px solid #ddd; }}
    .confidence-High   {{ color: #155724; font-weight: bold; }}
    .confidence-Medium {{ color: #856404; font-weight: bold; }}
    .confidence-Low    {{ color: #721c24; font-weight: bold; }}
  </style>
</head>
<body>
  <h1>Ancestry Inference Report</h1>
  <p><strong>Sample:</strong> {result.sample_id}</p>
  <p><strong>Primary Population:</strong> {result.primary_population}</p>
  <p><strong>Confidence:</strong>
    <span class="confidence-{result.confidence}">{result.confidence}</span>
  </p>
  <p><strong>AIM markers called:</strong> {result.markers_called} / {result.markers_evaluated}</p>
  <h2>Population Probabilities</h2>
  <table>
    <tr><th>Population</th><th>Probability</th><th>Bar</th></tr>
    {pop_rows}
  </table>
  <p style="color:#777;font-size:0.85em;margin-top:2em">
    Populations: AFR=African, AMR=Admixed American, EAS=East Asian, EUR=European, SAS=South Asian.<br>
    Based on {result.markers_evaluated} ancestry-informative markers (1000 Genomes superpopulations, GRCh38).
    {ANCESTRY_SCOPE_STATEMENT}
  </p>
</body>
</html>"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return path


# ─── AncestryStage ────────────────────────────────────────────────────────────


class AncestryStage:
    """Infer superpopulation ancestry from a VCF using AIM-based MLE.

    Panel: 101 ancestry-informative markers from 1000 Genomes Project.
    Populations: AFR, AMR, EAS, EUR, SAS.
    """

    def __init__(self, cfg: Optional[Dict] = None) -> None:
        self._cfg = cfg or {}

    def run(
        self,
        vcf_path: str,
        output_dir: str,
        sample_id: str = "SAMPLE",
    ) -> AncestryResult:
        """Run ancestry inference.

        Args:
            vcf_path:   Path to called VCF.
            output_dir: Directory for output reports.
            sample_id:  Sample identifier.

        Returns:
            AncestryResult — never raises; logs errors and returns gracefully.
        """
        t0 = time.time()
        result = AncestryResult(sample_id=sample_id, vcf_path=vcf_path)
        os.makedirs(output_dir, exist_ok=True)

        logger.info("[Ancestry] Starting inference for sample %s", sample_id)

        # ── Build AIM position lookup ─────────────────────────────────────────
        # (chrom, pos) → rsid
        aim_positions: Dict[Tuple[str, int], str] = {}
        for rsid, info in AIM_PANEL.items():
            aim_positions[(info["chrom"], info["pos"])] = rsid

        result.markers_evaluated = len(AIM_PANEL)

        # ── Parse VCF ────────────────────────────────────────────────────────
        try:
            genotypes = _parse_vcf_genotypes(vcf_path)
        except Exception as exc:
            logger.error("[Ancestry] Failed to parse VCF: %s", exc)
            genotypes = {}

        # ── Match VCF variants to AIM positions ───────────────────────────────
        called_markers: Dict[str, bool] = {}  # rsid → alt observed
        for (chrom, pos), (ref, alt) in genotypes.items():
            rsid = aim_positions.get((chrom, pos))
            if rsid is None:
                continue
            marker = AIM_PANEL[rsid]
            # Check alleles match
            if ref.upper() == marker["ref"].upper() and alt.upper() == marker["alt"].upper():
                called_markers[rsid] = True  # alt allele observed

        # Any AIM position not in VCF → ref-only (dosage 0)
        # Include all markers in likelihood (called + uncalled)
        all_markers: Dict[str, bool] = {}
        for rsid in AIM_PANEL:
            all_markers[rsid] = rsid in called_markers

        result.markers_called = len(called_markers)
        logger.info(
            "[Ancestry] %d/%d AIM markers called", result.markers_called, result.markers_evaluated
        )

        # ── Maximum-likelihood estimation ─────────────────────────────────────
        if result.markers_called == 0:
            # No AIMs called → uniform distribution
            probs = {pop: round(1.0 / len(POPULATIONS), 6) for pop in POPULATIONS}
            primary = "Unknown"
        else:
            probs = _mle_ancestry(all_markers)
            primary = max(probs, key=probs.__getitem__)

        result.population_probabilities = probs
        result.primary_population = primary
        result.confidence = _confidence_level(result.markers_called)

        logger.info(
            "[Ancestry] Primary: %s (%.3f), Confidence: %s",
            primary,
            probs.get(primary, 0.0),
            result.confidence,
        )

        # ── Write reports ─────────────────────────────────────────────────────
        try:
            result.report_json_path = _write_json_report(result, output_dir)
        except Exception as exc:
            logger.error("[Ancestry] Failed to write JSON report: %s", exc)

        try:
            result.report_html_path = _write_html_report(result, output_dir)
        except Exception as exc:
            logger.error("[Ancestry] Failed to write HTML report: %s", exc)

        result.elapsed_seconds = round(time.time() - t0, 3)
        logger.info("[Ancestry] Done in %.2fs", result.elapsed_seconds)
        return result
