"""
pipeline/vep/stage.py
──────────────────────
VEP (Ensembl Variant Effect Predictor) annotation stage (GAP 1).

Shells out to the ``vep`` CLI with comprehensive flags to obtain:
  - Most-severe consequence term
  - HGVS c. and p. notations
  - CADD_PHRED, REVEL, AlphaMissense pre-computed scores (each requires its
    plugin data file to be configured below -- see "Plugin data" note)
  - Gene symbol and transcript ID

SpliceAI REMOVED (2026-08-22, licence): Illumina's SpliceAI plugin was
requested unconditionally here and its score fed live ACMG evidence
(PP3/BP4, and `pipeline/orchestration/shared.py`'s `synonymous_or_intronic`
-> BP7), but its pretrained models are CC BY-NC 4.0 (non-commercial) and
its code GPL-3.0 -- the same class of licence blocker as OMIM, and never
covered by `LICENSE_AUDIT.md`'s own "never integrated into GEPER"
verdict for SpliceAI, which was scoped to `geper/` only. `spliceai_score`
fields remain on `VEPVariantAnnotation`/downstream dataclasses (always
`None` now) rather than being deleted outright, so nothing that reads
them via `.get(...)`/attribute access needs a separate null-check added --
see `orchestration/shared.py::synonymous_or_intronic`'s fix for the one
place absence needed to be handled explicitly rather than relying on the
default.

Config section::

    vep:
      enabled: true
      cache_dir: ""              # e.g. /data/vep_cache
      dir_plugins: ""            # e.g. /opt/vep/Plugins (VEP's plugin .pm dir)
      cadd_data: ""               # path to CADD's whole_genome_SNVs.tsv.gz
      revel_data: ""              # path to REVEL's revel.tsv.gz
      alphamissense_data: ""      # path to AlphaMissense_hg38.tsv.gz
      fasta: ""                  # reference FASTA for HGVS
      extra_flags: ""             # any additional VEP flags
      timeout: 600

Plugin data: CADD, REVEL, and AlphaMissense are Ensembl VEP plugins that
each require a separate, per-file data download -- installing the ``vep``
binary and cache (docs/INSTALL_DEPENDENCIES.md) is not sufficient to get
these scores. Unlike ``vep`` itself, a bare ``--plugin CADD`` with no data
file is a hard VEP startup error, not a "run with defaults" request, so
each plugin above is only requested when its ``*_data`` key is configured;
left unset, VEP still runs but that score comes back absent (already
tolerated downstream -- ``cadd_phred``/``revel_score``/``am_pathogenicity``
are ``Optional[float]``, checked ``is not None`` before voting in
``acmg/classifier.py``). See docs/INSTALL_DEPENDENCIES.md for where to
obtain each data file.

Checkpoint key: ``vep_annotation``
"""

from __future__ import annotations

import gzip
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from pipeline.fastq.errors import FastqPipelineError, _require, _run

logger = logging.getLogger("geper.pipeline.vep.stage")

_STAGE = "vep_annotation"

# Consequences ordered roughly by severity (most severe first)
_SEVERITY_ORDER = [
    "transcript_ablation",
    "splice_acceptor_variant",
    "splice_donor_variant",
    "stop_gained",
    "frameshift_variant",
    "stop_lost",
    "start_lost",
    "transcript_amplification",
    "inframe_insertion",
    "inframe_deletion",
    "missense_variant",
    "protein_altering_variant",
    "splice_region_variant",
    "incomplete_terminal_codon_variant",
    "start_retained_variant",
    "stop_retained_variant",
    "synonymous_variant",
    "coding_sequence_variant",
    "mature_miRNA_variant",
    "5_prime_UTR_variant",
    "3_prime_UTR_variant",
    "non_coding_transcript_exon_variant",
    "intron_variant",
    "NMD_transcript_variant",
    "non_coding_transcript_variant",
    "upstream_gene_variant",
    "downstream_gene_variant",
    "TFBS_ablation",
    "TFBS_amplification",
    "TF_binding_site_variant",
    "regulatory_region_ablation",
    "regulatory_region_amplification",
    "feature_elongation",
    "regulatory_region_variant",
    "feature_truncation",
    "intergenic_variant",
]
_SEVERITY_RANK = {c: i for i, c in enumerate(_SEVERITY_ORDER)}


@dataclass
class VEPVariantAnnotation:
    """Per-variant fields extracted from VEP output."""

    chrom: str = ""
    pos: int = 0
    ref: str = ""
    alt: str = ""
    consequence: str = ""
    hgvs_c: str = ""
    hgvs_p: str = ""
    gene_symbol: str = ""
    transcript_id: str = ""
    cadd_phred: Optional[float] = None
    revel_score: Optional[float] = None
    spliceai_score: Optional[float] = None
    am_pathogenicity: Optional[float] = None


@dataclass
class VEPAnnotationResult:
    """Result returned by VEPAnnotationStage.run()."""

    annotated_vcf_path: str = ""
    variants: List[VEPVariantAnnotation] = field(default_factory=list)
    variant_count: int = 0

    def to_dict(self) -> Dict:
        return {
            "annotated_vcf_path": self.annotated_vcf_path,
            "variant_count": self.variant_count,
            "variants": [vars(v) for v in self.variants],
        }


class VEPAnnotationStage:
    """Stage 2b: run Ensembl VEP on the filtered VCF.

    Args:
        cfg: Full pipeline configuration dict.
    """

    def __init__(self, cfg: Optional[Dict] = None) -> None:
        self._cfg = cfg or {}
        self._vep_cfg: Dict = self._cfg.get("vep", {}) or {}

    def run(
        self,
        filtered_vcf_path: str,
        output_dir: str,
        sample_id: str = "SAMPLE",
    ) -> VEPAnnotationResult:
        """Annotate *filtered_vcf_path* with VEP.

        Args:
            filtered_vcf_path: Path to the filtered VCF from variant calling.
            output_dir:        Directory to write VEP-annotated VCF into.
            sample_id:         Sample identifier for logging.

        Returns:
            VEPAnnotationResult with per-variant annotations.

        Raises:
            FastqPipelineError: if ``vep`` binary is absent, or VEP exits
                non-zero.
        """
        enabled = bool(self._vep_cfg.get("enabled", True))
        if not enabled:
            logger.info("[%s] VEP annotation disabled — skipping", sample_id)
            return VEPAnnotationResult(annotated_vcf_path=filtered_vcf_path)

        vep_bin = _require("vep", stage=_STAGE)
        logger.info("[%s] VEPAnnotationStage: annotating %s", sample_id, filtered_vcf_path)

        Path(output_dir).mkdir(parents=True, exist_ok=True)
        annotated_vcf = str(Path(output_dir) / "vep_annotated.vcf")

        cmd = self._build_vep_cmd(vep_bin, filtered_vcf_path, annotated_vcf)
        timeout = int(self._vep_cfg.get("timeout", 600))

        # Routed through the shared _run() helper (not a direct
        # subprocess.run) so a VEP annotation in progress -- which can run
        # for minutes on a large VCF -- is reachable by DELETE the same way
        # every other stage's subprocess is. See
        # pipeline/utils/process_control.py.
        try:
            _run(cmd, stage=_STAGE, timeout_seconds=timeout)
        except FastqPipelineError as exc:
            if "timed out" in exc.message:
                raise FastqPipelineError(
                    f"vep timed out after {timeout}s on {filtered_vcf_path}",
                    stage=_STAGE,
                    tool="vep",
                ) from exc
            raise FastqPipelineError(
                f"vep failed: {exc.message}",
                stage=_STAGE,
                tool="vep",
            ) from exc

        variants = self._parse_vep_vcf(annotated_vcf)
        logger.info("[%s] VEP annotated %d variants", sample_id, len(variants))

        return VEPAnnotationResult(
            annotated_vcf_path=annotated_vcf,
            variants=variants,
            variant_count=len(variants),
        )

    def _build_vep_cmd(self, vep_bin: str, input_vcf: str, output_vcf: str) -> List[str]:
        """Construct the vep command line."""
        cmd = [
            vep_bin,
            "--input_file",
            input_vcf,
            "--output_file",
            output_vcf,
            "--format",
            "vcf",
            "--vcf",
            "--everything",
            "--fork",
            "4",
            "--cache",
            "--offline",
            "--hgvs",
            "--sift",
            "b",
            "--polyphen",
            "b",
            "--af",
            "--af_gnomadg",
            # SpliceAI REMOVED (licence): Ensembl VEP's official SpliceAI
            # plugin wraps Illumina's own precomputed scores, GPL-3.0
            # code / CC BY-NC 4.0 pretrained models as of Illumina's Dec
            # 2023 relicense -- the same class of commercial-licence
            # blocker as OMIM, and untenable for a commercial product.
            # See LICENSE_AUDIT.md's "SpliceAI (Illumina)" row.
            "--no_stats",
            "--force_overwrite",
        ]

        cache_dir = self._vep_cfg.get("cache_dir", "")
        if cache_dir:
            cmd += ["--dir_cache", str(cache_dir)]

        dir_plugins = self._vep_cfg.get("dir_plugins", "")
        if dir_plugins:
            cmd += ["--dir_plugins", str(dir_plugins)]

        # CADD, REVEL, and AlphaMissense all require a per-file data
        # argument -- `--plugin CADD` with no data file is not a "use
        # defaults" request, it's a VEP startup error. Request each plugin
        # only when its data file is configured; otherwise omit it and let
        # that score come back absent (cadd_phred/revel_score/
        # am_pathogenicity are Optional[float], already tolerated as
        # `is not None` checks by the ACMG classifier -- see
        # docs/INSTALL_DEPENDENCIES.md's VEP section for how to obtain
        # each data file).
        cadd_data = self._vep_cfg.get("cadd_data", "")
        if cadd_data:
            cmd += ["--plugin", f"CADD,{cadd_data}"]

        revel_data = self._vep_cfg.get("revel_data", "")
        if revel_data:
            cmd += ["--plugin", f"REVEL,{revel_data}"]

        alphamissense_data = self._vep_cfg.get("alphamissense_data", "")
        if alphamissense_data:
            cmd += ["--plugin", f"AlphaMissense,file={alphamissense_data}"]

        fasta = self._vep_cfg.get("fasta", "")
        if fasta:
            cmd += ["--fasta", str(fasta)]

        extra = self._vep_cfg.get("extra_flags", "")
        if extra:
            cmd += extra.split()

        return cmd

    def _parse_vep_vcf(self, vcf_path: str) -> List[VEPVariantAnnotation]:
        """Parse VEP-annotated VCF and extract per-variant fields."""
        variants: List[VEPVariantAnnotation] = []

        if not Path(vcf_path).exists():
            return variants

        open_fn = gzip.open if vcf_path.endswith(".gz") else open
        csq_fields: List[str] = []

        with open_fn(vcf_path, "rt") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if line.startswith("##INFO=<ID=CSQ"):
                    # Parse VEP CSQ format descriptor
                    # e.g. Description="...Format: Allele|Consequence|IMPACT|..."
                    fmt_part = ""
                    # Try to find "Format: ..." in the Description value
                    import re as _re

                    m = _re.search(r'Format:\s*([^"]+)', line)
                    if m:
                        fmt_part = m.group(1).strip().rstrip('">').strip()
                    if fmt_part:
                        csq_fields = [f.strip() for f in fmt_part.split("|")]
                    continue

                if line.startswith("#"):
                    continue

                cols = line.split("\t")
                if len(cols) < 8:
                    continue

                chrom, pos_s, _, ref, alt_field = cols[0], cols[1], cols[2], cols[3], cols[4]
                alt = alt_field.split(",")[0]  # take first ALT allele
                info = cols[7]

                ann = VEPVariantAnnotation(
                    chrom=chrom,
                    pos=int(pos_s),
                    ref=ref,
                    alt=alt,
                )

                if csq_fields:
                    self._parse_csq(info, csq_fields, ann)

                variants.append(ann)

        return variants

    def _parse_csq(
        self,
        info: str,
        csq_fields: List[str],
        ann: VEPVariantAnnotation,
    ) -> None:
        """Extract the most severe CSQ entry and populate *ann* in place."""
        csq_raw = ""
        for part in info.split(";"):
            if part.startswith("CSQ="):
                csq_raw = part[4:]
                break
        if not csq_raw:
            return

        # Each CSQ entry is pipe-delimited; multiple transcripts are comma-separated
        best_rank = len(_SEVERITY_ORDER) + 1
        best_entry: Dict[str, str] = {}

        for entry_str in csq_raw.split(","):
            values = entry_str.split("|")
            entry: Dict[str, str] = {}
            for i, val in enumerate(values):
                if i < len(csq_fields):
                    entry[csq_fields[i]] = val

            # Determine rank by most-severe consequence term
            consequences = entry.get("Consequence", "").split("&")
            rank = min(
                (_SEVERITY_RANK.get(c, len(_SEVERITY_ORDER)) for c in consequences),
                default=len(_SEVERITY_ORDER),
            )
            if rank < best_rank:
                best_rank = rank
                best_entry = entry

        if not best_entry:
            return

        # Pull consequence (first/most severe term)
        consequences = best_entry.get("Consequence", "").split("&")
        ann.consequence = consequences[0] if consequences else ""

        # HGVS
        ann.hgvs_c = best_entry.get("HGVSc", "") or ""
        ann.hgvs_p = best_entry.get("HGVSp", "") or ""

        # Gene symbol / transcript
        ann.gene_symbol = best_entry.get("SYMBOL", "") or best_entry.get("Gene", "") or ""
        ann.transcript_id = best_entry.get("Feature", "") or ""

        # Pre-computed scores
        def _float(val: str) -> Optional[float]:
            try:
                return float(val) if val and val != "." else None
            except ValueError:
                return None

        ann.cadd_phred = _float(best_entry.get("CADD_PHRED", ""))
        ann.revel_score = _float(best_entry.get("REVEL", ""))
        # SpliceAI parsing REMOVED (licence) -- the plugin is no longer
        # requested (see _build_vep_cmd), so this field would never be
        # present in VEP's output anyway. ann.spliceai_score keeps its
        # dataclass default of None.
        ann.am_pathogenicity = _float(
            best_entry.get("AM_PATHOGENICITY", "") or best_entry.get("am_pathogenicity", "")
        )
