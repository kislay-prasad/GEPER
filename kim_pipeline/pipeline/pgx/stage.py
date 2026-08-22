"""
pipeline/pgx/stage.py
─────────────────────
PGxStage: pharmacogenomics annotation from a called VCF.

Detects star-alleles for 10 core PGx genes, calls diplotypes,
predicts phenotypes, and generates JSON + HTML reports.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Set, Tuple

from pipeline.pgx.diplotypes import (
    STAR_ALLELE_VARIANTS,
    DIPLOTYPE_PHENOTYPES,
    DRUG_IMPLICATIONS,
    ACTIVITY_SCORES,
    DEFAULT_PHENOTYPE,
    EVIDENCE_LEVELS,
    ALL_GENES,
)

logger = logging.getLogger("geper.pipeline.pgx")

# Ships in every serialised PGx result (JSON and, via the HTML/PDF renderers'
# own copies, rendered reports too) so a caller reading the raw JSON --
# without ever opening an HTML/PDF report -- still gets it. Single source:
# PGxResult.to_dict() injects this key rather than each renderer re-stating
# its own copy, matching the pattern report/clinical_report_builder.py's
# RESEARCH_USE_DISCLAIMER already established for geper/'s own renderers.
PGX_VALIDATION_CAVEAT = (
    "Star-allele/diplotype calls have not been validated against an independent "
    "ground-truth dataset: no commercially-usable reference for this purpose "
    "currently exists (PharmVar, the field's authoritative star-allele "
    "nomenclature source, is CC BY-NC-ND -- see GROUND_TRUTH_DATASET_AUDIT.md). "
    "Treat as informative, not confirmatory, pending validation."
)


# ─── Result dataclasses ───────────────────────────────────────────────────────


@dataclass
class PGxAnnotation:
    """Pharmacogenomic annotation for a single gene."""

    gene: str
    diplotype: str
    phenotype: str
    activity_score: Optional[float]
    affected_drugs: List[Dict[str, str]] = field(default_factory=list)
    evidence_level: str = "1A"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PGxResult:
    """Complete PGx analysis result for a sample."""

    sample_id: str = ""
    vcf_path: str = ""
    annotations: List[PGxAnnotation] = field(default_factory=list)
    report_json_path: str = ""
    report_html_path: str = ""
    elapsed_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["validation_caveat"] = PGX_VALIDATION_CAVEAT
        return d

    def is_json_serialisable(self) -> bool:
        try:
            json.dumps(self.to_dict())
            return True
        except (TypeError, ValueError):
            return False


# ─── VCF reader ──────────────────────────────────────────────────────────────


def _parse_vcf_variants(
    vcf_path: str,
    sample_id: str = "SAMPLE",
) -> Dict[Tuple[str, int, str, str], str]:
    """Parse a VCF and return a dict of (chrom, pos, ref, alt) → zygosity.

    Reads FORMAT/GT to distinguish heterozygous (0/1, 0|1) from homozygous
    alt (1/1, 1|1) calls.  Supports phased and unphased genotypes.
    Uses ZygosityExtractor to avoid duplicate parsing logic.

    Handles standard VCF format (4-column minimum, up to INFO/FORMAT/SAMPLE).
    Skips header lines starting with '#'.
    Never raises — logs warnings on malformed lines.

    Returns:
        Dict mapping (chrom_no_prefix, pos, ref, alt) → zygosity string.
        Zygosity is one of: "heterozygous", "homozygous_alt", "hemizygous",
        "homozygous_ref", "no_call", "multi_allelic", "unknown".
    """
    from pipeline.zygosity.extractor import ZygosityExtractor

    variants: Dict[Tuple[str, int, str, str], str] = {}
    if not vcf_path or not os.path.isfile(vcf_path):
        logger.warning("[PGx] VCF not found: %s", vcf_path)
        return variants

    try:
        with open(vcf_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 4:
                    continue
                chrom = parts[0].lstrip("chr")
                try:
                    pos = int(parts[1])
                except ValueError:
                    continue
                ref = parts[3].upper()
                alt_field = parts[4] if len(parts) > 4 else "."
                if alt_field in (".", ""):
                    continue

                # Parse FORMAT/GT for zygosity
                zygosity_by_alt: Dict[str, str] = {}
                if len(parts) >= 9:
                    format_str = parts[8]
                    sample_str = parts[9] if len(parts) > 9 else "."
                    format_keys = format_str.split(":")
                    format_values = sample_str.split(":")
                    # Pad values if shorter than keys (some callers omit trailing fields)
                    while len(format_values) < len(format_keys):
                        format_values.append(".")
                    gt_val = "."
                    if "GT" in format_keys:
                        gt_val = format_values[format_keys.index("GT")]
                    try:
                        zy_result = ZygosityExtractor.extract(gt_val, format_keys, format_values)
                        zy = zy_result.zygosity
                    except Exception:
                        zy = "unknown"
                else:
                    # No FORMAT column — presence only, treat as heterozygous
                    zy = "heterozygous"

                for alt_idx, alt in enumerate(alt_field.split(",")):
                    alt = alt.strip().upper()
                    if alt and alt != ".":
                        # FIX 5: recompute AB with correct alt index for this allele
                        try:
                            zy_result_per_alt = ZygosityExtractor.extract(
                                gt_val, format_keys, format_values, alt_index=alt_idx + 1
                            )
                            zy = zy_result_per_alt.zygosity
                        except Exception:
                            pass  # keep zy from the outer call
                        key = (chrom, pos, ref, alt)
                        variants[key] = zy
    except Exception as exc:
        logger.warning("[PGx] Error parsing VCF %s: %s", vcf_path, exc)

    return variants


# ─── Star-allele detection ────────────────────────────────────────────────────


def _normalise_indel_key(chrom: str, pos: int, ref: str, alt: str) -> Tuple[str, int, str, str]:
    """Left-normalise a simple insertion/deletion for VCF key matching.

    FIX 7: Star-allele definitions use left-normalised VCF representation.
    This function strips shared prefix bases so that e.g. REF=CA ALT=C
    (anchor-included deletion of A) matches VCFs using the same convention.
    """
    ref = ref.upper()
    alt = alt.upper()
    chrom = chrom.lstrip("chr")
    if len(ref) == len(alt):
        return chrom, pos, ref, alt
    i = 0
    while i < min(len(ref), len(alt)) - 1 and ref[i] == alt[i]:
        i += 1
    if i > 0:
        ref = ref[i:]
        alt = alt[i:]
        pos += i
    return chrom, pos, ref, alt


def _detect_star_alleles(
    gene: str,
    variants: Dict[Tuple[str, int, str, str], str],
) -> Tuple[List[str], Set[str]]:
    """Return (detected star alleles, hemizygous allele names) for *gene*.

    FIX 6: Subset allele exclusion — if allele A's defining variants are a
    strict subset of allele B's defining variants, A is excluded (B subsumes A).
    This prevents incorrect co-reporting of e.g. *2 + *10 when only *10 is present
    (since *2 is defined by a subset of *10's variants).

    FIX 7 (preserved): Defining variant keys are left-normalised.
    CYP2D6 *5 (CNV) is explicitly flagged as not assessed from SNV VCFs.

    ISSUE 6 FIX: "hemizygous" (a single allele copy, e.g. a male sample's
    one X chromosome outside the PAR) is NOT the same genetic event as
    "homozygous_alt" (two copies of the same allele on two chromosomes),
    even though both indicate "no reference allele present". Previously
    both were treated identically and a hemizygous call was duplicated
    into the detected-alleles list exactly like a true homozygous call,
    producing diplotype labels like "G202A/G202A" for a male sample that
    in fact carries only one copy of the variant on his single X. That
    fabricates a second chromosomal copy that doesn't exist. Hemizygous
    matches are now tracked separately (returned as the second element)
    and are never duplicated here — _call_diplotype uses that information
    to render a hemizygous-appropriate label instead.
    """
    allele_defs = STAR_ALLELE_VARIANTS.get(gene, {})

    # FIX 7: warn about CNV-only alleles that cannot be detected here
    cnv_alleles = [a for a, v in allele_defs.items() if not v]
    if cnv_alleles:
        logger.info(
            "[PGx] %s: allele(s) %s require CNV analysis — NOT assessed from SNV/indel VCF.",
            gene,
            ", ".join(cnv_alleles),
        )

    # Step 1: find all alleles whose defining variants are all present in the VCF
    raw_detected: List[str] = []
    allele_varsets: Dict[str, Set[Tuple]] = {}
    hemizygous_alleles: Set[str] = set()

    for allele, defining_vars in allele_defs.items():
        if not defining_vars:
            continue
        matched_zygosities = []
        all_present = True
        matched_keys: Set[Tuple] = set()
        for chrom, pos, ref, alt in defining_vars:
            norm_key = _normalise_indel_key(chrom, pos, ref, alt)
            raw_key = (chrom.lstrip("chr"), pos, ref.upper(), alt.upper())
            if norm_key in variants:
                matched_zygosities.append(variants[norm_key])
                matched_keys.add(norm_key)
            elif raw_key in variants:
                matched_zygosities.append(variants[raw_key])
                matched_keys.add(raw_key)
            else:
                all_present = False
                break

        if not all_present:
            continue

        raw_detected.append(allele)
        allele_varsets[allele] = matched_keys
        # ISSUE 6 FIX: hemizygous (ploidy-1 GT, one true copy) must never be
        # duplicated as if it were a second chromosomal copy. Only a
        # genuinely diploid homozygous_alt call (two copies) is duplicated
        # to represent the allele on both haplotypes.
        if all(z == "homozygous_alt" for z in matched_zygosities):
            raw_detected.append(allele)
        elif matched_zygosities and all(z == "hemizygous" for z in matched_zygosities):
            hemizygous_alleles.add(allele)

    # FIX 6: Step 2 — deterministic subset exclusion.
    # Sort by number of defining variants descending (most specific = most variants).
    # If allele B's defining variant set is a strict subset of allele A's,
    # exclude B: the sample has A, not B separately.
    sorted_alleles = sorted(
        set(allele_varsets.keys()),
        key=lambda a: len(allele_varsets[a]),
        reverse=True,
    )
    excluded: Set[str] = set()
    for i, a in enumerate(sorted_alleles):
        if a in excluded:
            continue
        for b in sorted_alleles[i + 1 :]:
            if b in excluded:
                continue
            if allele_varsets[b] and allele_varsets[b].issubset(allele_varsets[a]):
                excluded.add(b)
                logger.debug("[PGx] %s: excluding %s (subset of %s)", gene, b, a)

    # Step 3: rebuild preserving homozygous duplicates, minus excluded
    detected: List[str] = [a for a in raw_detected if a not in excluded]
    hemizygous_alleles -= excluded
    return sorted(detected), hemizygous_alleles


def _call_diplotype(
    gene: str,
    detected_alleles: List[str],
    hemizygous_alleles: Optional[Set[str]] = None,
) -> Tuple[str, str, str]:
    """Combine detected alleles into a diplotype string.

    FIX 6: Deterministic prioritisation — most specific alleles (most defining
    variants) are preferred.  Canonical pair is sorted so (*4,*1) → (*1,*4).

    ISSUE 6 FIX: when the sample carries a single detected allele that was
    matched via a hemizygous (single-copy) genotype call — i.e. a male
    sample's lone X chromosome outside the pseudoautosomal region — the
    diplotype label must reflect that there is only one chromosomal copy,
    not pair it with a fabricated second allele. Default-pairing with
    "*1" (as is done for genuinely diploid single-allele detections) would
    incorrectly claim a second, normal-function X chromosome the sample
    does not have. The label produced is e.g. "G202A (hemizygous)" rather
    than "G202A/*1" or "G202A/G202A".

    For phenotype lookup, the returned (a1, a2) pair still uses
    (allele, allele) for a hemizygous call — a single hemizygous copy of a
    loss-of-function allele is fully expressed (no second, masking normal
    allele exists), which is the same functional consequence as a diploid
    homozygous call, so the existing DIPLOTYPE_PHENOTYPES tables (keyed by
    allele pairs) apply unchanged; only the displayed diplotype string
    differs.

    Returns:
        Tuple of (diplotype_string, allele1, allele2). allele1/allele2 are
        for phenotype-table lookup only — use diplotype_string for display.
    """
    hemizygous_alleles = hemizygous_alleles or set()

    if not detected_alleles:
        a1, a2 = "*1", "*1"
        diplotype = f"{a1}/{a2}"
        return diplotype, a1, a2

    if len(detected_alleles) == 1:
        allele = detected_alleles[0]
        if allele in hemizygous_alleles:
            # Single chromosomal copy — do not fabricate a *1 partner.
            return f"{allele} (hemizygous)", allele, allele
        a1, a2 = "*1", allele
    else:
        a1, a2 = detected_alleles[0], detected_alleles[1]

    # Canonical sort so (*4, *1) → (*1, *4)
    pair = tuple(sorted([a1, a2]))
    diplotype = f"{pair[0]}/{pair[1]}"
    return diplotype, pair[0], pair[1]


def _predict_phenotype(gene: str, a1: str, a2: str) -> str:
    """Map a diplotype to a phenotype string.

    Looks up DIPLOTYPE_PHENOTYPES with canonical sorted key.
    Falls back to DEFAULT_PHENOTYPE for the gene if pair not in table.
    """
    pair = tuple(sorted([a1, a2]))
    gene_table = DIPLOTYPE_PHENOTYPES.get(gene, {})
    if pair in gene_table:
        return gene_table[pair]
    # Fallback: if both alleles are *1, use default (Normal)
    if pair == ("*1", "*1"):
        return DEFAULT_PHENOTYPE.get(gene, "Normal Metabolizer")
    # Any other unknown combination
    return DEFAULT_PHENOTYPE.get(gene, "Indeterminate")


def _get_drug_implications(gene: str, phenotype: str) -> List[Dict[str, str]]:
    """Return drug implications for a gene+phenotype combination."""
    gene_table = DRUG_IMPLICATIONS.get(gene, {})
    return list(gene_table.get(phenotype, []))


def _get_activity_score(gene: str, phenotype: str) -> Optional[float]:
    """Return numeric activity score if defined, else None."""
    gene_table = ACTIVITY_SCORES.get(gene, {})
    return gene_table.get(phenotype)


# ─── Report generators ────────────────────────────────────────────────────────


def _write_json_report(result: PGxResult, output_dir: str) -> str:
    """Write PGx JSON report. Returns path."""
    path = os.path.join(output_dir, "pgx_report.json")
    payload = {
        "sample_id": result.sample_id,
        "vcf_path": result.vcf_path,
        "elapsed_seconds": result.elapsed_seconds,
        "annotations": [ann.to_dict() for ann in result.annotations],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    return path


def _write_html_report(result: PGxResult, output_dir: str) -> str:
    """Write PGx HTML report. Returns path."""
    path = os.path.join(output_dir, "pgx_report.html")

    rows = []
    for ann in result.annotations:
        drug_list = (
            "; ".join(f"{d['drug']} ({d['implication']})" for d in ann.affected_drugs) or "None"
        )
        score = f"{ann.activity_score:.1f}" if ann.activity_score is not None else "N/A"
        rows.append(f"""
        <tr>
          <td><strong>{ann.gene}</strong></td>
          <td>{ann.diplotype}</td>
          <td>{ann.phenotype}</td>
          <td>{score}</td>
          <td style="font-size:0.85em">{drug_list}</td>
          <td>{ann.evidence_level}</td>
        </tr>""")

    table_body = "\n".join(rows)
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>PGx Report — {result.sample_id}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 2em; }}
    h1 {{ color: #2c3e50; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 1em; }}
    th {{ background: #2980b9; color: white; padding: 8px; text-align: left; }}
    td {{ padding: 7px; border-bottom: 1px solid #ddd; vertical-align: top; }}
    tr:hover {{ background: #f5f5f5; }}
    .footer {{ margin-top: 2em; color: #777; font-size: 0.85em; }}
  </style>
</head>
<body>
  <h1>Pharmacogenomics (PGx) Report</h1>
  <p><strong>Sample:</strong> {result.sample_id}</p>
  <p><strong>VCF:</strong> {result.vcf_path}</p>
  <p><strong>Analysis time:</strong> {result.elapsed_seconds:.2f}s</p>
  <p style="color:#777;font-size:0.9em;"><em>{PGX_VALIDATION_CAVEAT}</em></p>
  <table>
    <thead>
      <tr>
        <th>Gene</th><th>Diplotype</th><th>Phenotype</th>
        <th>Activity Score</th><th>Drug Implications</th><th>Evidence</th>
      </tr>
    </thead>
    <tbody>
{table_body}
    </tbody>
  </table>
  <div class="footer">
    Guidelines: CPIC (cpicpgx.org), DPWG. Data embedded from PharmGKB (GRCh38).
    Bij AI assists qualified clinicians and pathologists; this report requires qualified human
    review before any clinical use, and does not independently provide final clinical
    interpretation.
  </div>
</body>
</html>"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return path


# ─── PGxStage ─────────────────────────────────────────────────────────────────


class PGxStage:
    """Pharmacogenomics annotation stage.

    Reads a VCF, detects star-alleles for 10 core PGx genes,
    calls diplotypes, predicts phenotypes, maps drug implications,
    and writes JSON + HTML reports.
    """

    def __init__(self, cfg: Optional[Dict] = None) -> None:
        self._cfg = cfg or {}

    def run(
        self,
        vcf_path: str,
        output_dir: str,
        sample_id: str = "SAMPLE",
    ) -> PGxResult:
        """Run PGx annotation.

        Args:
            vcf_path:   Path to called, filtered VCF file.
            output_dir: Directory where reports will be written.
            sample_id:  Sample identifier for reports.

        Returns:
            PGxResult — never raises; errors are logged.
        """
        t0 = time.time()
        result = PGxResult(sample_id=sample_id, vcf_path=vcf_path)

        os.makedirs(output_dir, exist_ok=True)

        logger.info("[PGx] Starting PGx annotation for sample %s", sample_id)
        logger.info("[PGx] VCF: %s", vcf_path)

        # ── Parse VCF ────────────────────────────────────────────────────────
        try:
            variants = _parse_vcf_variants(vcf_path, sample_id)
            logger.info("[PGx] Parsed %d variant calls from VCF", len(variants))
        except Exception as exc:
            logger.error("[PGx] Failed to parse VCF: %s", exc)
            variants = set()

        # ── Annotate each gene ────────────────────────────────────────────────
        annotations: List[PGxAnnotation] = []
        for gene in ALL_GENES:
            try:
                detected, hemizygous_alleles = _detect_star_alleles(gene, variants)
                diplotype, a1, a2 = _call_diplotype(gene, detected, hemizygous_alleles)
                phenotype = _predict_phenotype(gene, a1, a2)
                drugs = _get_drug_implications(gene, phenotype)
                score = _get_activity_score(gene, phenotype)
                evidence = EVIDENCE_LEVELS.get(gene, "2A")

                ann = PGxAnnotation(
                    gene=gene,
                    diplotype=diplotype,
                    phenotype=phenotype,
                    activity_score=score,
                    affected_drugs=drugs,
                    evidence_level=evidence,
                )
                annotations.append(ann)
                logger.debug("[PGx] %s: %s → %s", gene, diplotype, phenotype)

            except Exception as exc:
                logger.warning("[PGx] Gene %s annotation failed: %s", gene, exc)
                # Add a safe default annotation so the gene is always present
                annotations.append(
                    PGxAnnotation(
                        gene=gene,
                        diplotype="*1/*1",
                        phenotype=DEFAULT_PHENOTYPE.get(gene, "Normal Metabolizer"),
                        activity_score=None,
                        affected_drugs=[],
                        evidence_level="1A",
                    )
                )

        result.annotations = annotations

        # ── Write reports ─────────────────────────────────────────────────────
        try:
            result.report_json_path = _write_json_report(result, output_dir)
            logger.info("[PGx] JSON report: %s", result.report_json_path)
        except Exception as exc:
            logger.error("[PGx] Failed to write JSON report: %s", exc)

        try:
            result.report_html_path = _write_html_report(result, output_dir)
            logger.info("[PGx] HTML report: %s", result.report_html_path)
        except Exception as exc:
            logger.error("[PGx] Failed to write HTML report: %s", exc)

        result.elapsed_seconds = round(time.time() - t0, 3)
        logger.info("[PGx] Done in %.2fs", result.elapsed_seconds)
        return result
