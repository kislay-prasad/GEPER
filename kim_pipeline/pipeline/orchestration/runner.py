"""
pipeline/orchestration/runner.py
──────────────────────────────────
PipelineRunner — single, authoritative FASTQ → Report orchestrator (Task 7).

This is the **only** top-level orchestration class.  All stage-level
orchestrators and duplicate pipeline-entry-point functions that existed
in earlier GEPER iterations are superseded by this module.

Execution path (single source of truth):
  FASTQ validation
    → Alignment (BWA-MEM or Minimap2)
      → Variant Calling (FreeBayes + bcftools filter)
        → Annotation (GFF3 gene/transcript resolution + HGVS + zygosity)
          → Report generation (JSON + HTML ± PDF)

Checkpointing:
  A ``checkpoint.json`` file is written to ``output_dir/<sample_id>/`` after
  each successfully completed stage.  On a subsequent run with the same
  ``output_dir`` and ``sample_id``, completed stages are **skipped** and
  their results are reloaded from disk.  Pass ``resume=False`` to force a
  full re-run.

CLI entry point:
  This module is importable as a library and also invocable directly::

      python -m pipeline.orchestration.runner \\
          --r1 sample_R1.fastq.gz \\
          --r2 sample_R2.fastq.gz \\
          --ref GRCh38.fasta \\
          --output-dir ./work \\
          --sample-id sample01 \\
          --config config/production.yaml

Alternatively use the top-level ``run_pipeline`` CLI at the repo root::

      python run_pipeline.py --r1 ... --r2 ... --ref ... --output-dir ...
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.alignment.stage import AlignmentStage
from pipeline.annotation.stage import AnnotationStage
from pipeline.config_validator import validate_config
from pipeline.fastq.errors import FastqPipelineError
from pipeline.fastq.validator import FastqValidator
from pipeline.qc.stage import QCStage, QCThresholdError
from pipeline.reporting.stage import ReportingStage
from shared.process_control import _CURRENT_KILL_CALLBACK
from pipeline.utils.reference_cache import ReferenceCacheError, resolve_reference
from pipeline.variant_calling.stage import VariantCallingStage
from pipeline.vep.stage import VEPAnnotationStage

logger = logging.getLogger("geper.pipeline.orchestration.runner")


def NO_KILL_TRACKING(popen_obj) -> None:  # noqa: N802 (reads as a named constant, not a verb)
    """Pass to register_kill_callback() to explicitly opt a run out of
    cancellation tracking (standalone CLI/script use with no DELETE
    endpoint in the picture -- see main()/run_pipeline.py below).

    A real no-op function object, not None, on purpose: run() below treats
    an *unregistered* callback (self._kill_callback is None) as a bug --
    silently proceeding with no cancellation path is exactly the failure
    mode this whole mechanism exists to prevent. Passing this constant is
    how a caller says "I've considered it and killability genuinely
    doesn't apply here," which is different from never having considered
    it at all.
    """
    return None


# ─── Checkpoint helpers ───────────────────────────────────────────────────────

_STAGES_IN_ORDER = [
    "fastq_validation",
    "alignment",
    "variant_calling",
    "annotation",
    "reporting",
]

# FIX 12: this exists as a constant, not an inline literal, so that
# test_defect_regression.py can IMPORT it rather than retype it. Both
# blast_summary tests used to hardcode this string themselves and assert
# against their own copy -- a tautology that stayed green even after this
# text was deleted from here. Importing this constant is what makes those
# tests fail when the disclaimer changes; inlining it back "as a tidy-up"
# would silently restore that exact defect. Do not inline it.
BLAST_SUMMARY_NOTE = (
    "BLAST results are supporting information only and do not override ACMG evidence."
)


def _checkpoint_path(work_dir: Path) -> Path:
    return work_dir / "checkpoint.json"


def _load_checkpoint(work_dir: Path) -> dict[str, Any]:
    cp = _checkpoint_path(work_dir)
    if cp.exists():
        try:
            return json.loads(cp.read_text())
        except Exception:
            return {}
    return {}


def _save_checkpoint(work_dir: Path, data: dict[str, Any]) -> None:
    _checkpoint_path(work_dir).write_text(json.dumps(data, indent=2))


def _ensure_analysis_started_at(checkpoint: dict[str, Any]) -> str:
    """Set `checkpoint["analysis_started_at"]` once, on the FIRST run of
    this sample, and return it. A resume must keep the ORIGINAL start
    time from the loaded checkpoint, not overwrite it with the resume
    moment -- the reporting stage's "Generated" field reads this value,
    and stamping resume time there answers a question nobody asked
    (DEFECT-kim-reporting-stage-658).
    """
    if "analysis_started_at" not in checkpoint:
        checkpoint["analysis_started_at"] = datetime.now(timezone.utc).isoformat()
    return checkpoint["analysis_started_at"]


def _collect_reference_versions(cfg: dict, reference_fasta: str) -> dict[str, str]:
    """Collect file paths / MD5 prefixes for reference databases.

    FIX 14: Records the versions of all reference data used in a run so that
    checkpoint resumes can detect mixed-version inconsistencies.
    Only captures what is available without network access; values are
    path + mtime so a database replacement is always detected.
    """

    def _file_stamp(path: str | None) -> str | None:
        """Return 'path@mtime' or None if path is absent/unconfigured."""
        if not path:
            return None
        try:
            mtime = os.path.getmtime(path)
            return f"{path}@{int(mtime)}"
        except OSError:
            return f"{path}@MISSING"

    ann_cfg = cfg.get("annotation", {}) or {}
    cv_cfg = cfg.get("clinvar", {}) or {}
    gn_cfg = cfg.get("gnomad", {}) or {}
    rna_cfg = cfg.get("rna_analysis", {}) or {}
    up_cfg = cfg.get("uniprot", {}) or {}

    versions: dict[str, str] = {}

    ref_stamp = _file_stamp(reference_fasta)
    if ref_stamp:
        versions["reference_fasta"] = ref_stamp

    gff_path = ann_cfg.get("refseq_gff") or rna_cfg.get("refseq_gff")
    gff_stamp = _file_stamp(gff_path)
    if gff_stamp:
        versions["gff3"] = gff_stamp

    cv_stamp = _file_stamp(cv_cfg.get("tsv_gz_path"))
    if cv_stamp:
        versions["clinvar"] = cv_stamp

    gn_stamp = _file_stamp(gn_cfg.get("vcf_path") or gn_cfg.get("tsv_path"))
    if gn_stamp:
        versions["gnomad"] = gn_stamp

    up_stamp = _file_stamp(up_cfg.get("dat_path") or up_cfg.get("xml_path"))
    if up_stamp:
        versions["uniprot"] = up_stamp

    return versions


# ─── Pipeline result ──────────────────────────────────────────────────────────


@dataclass
class PipelineResult:
    """Complete result of a FASTQ → Report run."""

    sample_id: str = ""
    work_dir: str = ""
    fastq_r1: str = ""
    fastq_r2: str | None = None
    reference_fasta: str = ""

    # Per-stage results
    qc_r1: dict | None = None
    qc_r2: dict | None = None
    alignment: dict | None = None
    variant_calling: dict | None = None
    annotation: dict | None = None
    acmg_results: list[dict] | None = None
    report: dict | None = None

    # Bookkeeping
    stages_completed: list[str] = field(default_factory=list)
    stages_skipped: list[str] = field(default_factory=list)
    total_elapsed_seconds: float = 0.0
    success: bool = False
    error: str | None = None
    errors: list[str] = field(default_factory=list)  # non-fatal per-stage errors
    log_path: str = ""  # path to per-sample pipeline.log file
    reference_versions: dict[str, str] = field(default_factory=dict)  # FIX 14

    # Set when the run was intentionally halted early (e.g. mode="vcf_only").
    # None for a normal, full FASTQ -> Report run.
    stopped_after: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# ─── Runner ───────────────────────────────────────────────────────────────────


class PipelineRunner:
    """Single orchestrator for the full GEPER genomic pipeline.

    Args:
        cfg:    Full configuration dict (all sub-keys; see config/default.yaml).
        resume: If True (default), skip already-completed stages using
                checkpoint state. Set False to force a full re-run.
    """

    def __init__(
        self,
        cfg: dict | None = None,
        resume: bool = True,
    ) -> None:
        self._cfg = cfg or {}
        self._resume = resume
        self._kill_callback: Callable | None = None

    def register_kill_callback(self, callback: Callable) -> None:
        """Register a callback called with each Popen object at stage start.

        The API layer uses this to store popen objects keyed by run_id so
        it can kill the subprocess group on DELETE.

        run() below REQUIRES this to have been called (with a real callback
        or an explicit no-op -- see the module-level NO_KILL_TRACKING
        comment) before it will execute at all. This is deliberate: the
        original defect here (2026-09-02 "Windows process killing" dispatch)
        was a callback that was silently never invoked, so a run had no
        working cancellation path and nothing said so. Making the absence
        of a registered callback a loud failure at run() entry, instead of
        a silent one discovered only when someone tries DELETE, is the fix
        for that specific shape of bug, not just for this one instance of
        it -- ruled 2026-09-02, "silence must fail, not pass silently."
        """
        self._kill_callback = callback

    _VALID_MODES = ("full", "vcf_only")
    _VALID_STOP_AFTER = ("variant_calling",)

    def run(
        self,
        fastq_r1: str,
        reference_fasta: str,
        output_dir: str,
        fastq_r2: str | None = None,
        sample_id: str = "SAMPLE",
        acmg_results: dict | None = None,
        pedigree_json: str | None = None,
        mode: str = "full",
        stop_after: str | None = None,
    ) -> PipelineResult:
        """Execute the pipeline: FASTQ → QC → Alignment → Variant Calling
        → Annotation → Report (default), or a truncated subset of it.

        Args:
            fastq_r1:         R1 FASTQ path (plain or .gz).
            reference_fasta:  Reference genome FASTA path.
            output_dir:       Root output directory; a sub-directory named
                              ``sample_id`` is created inside it.
            fastq_r2:         R2 FASTQ path for paired-end (optional).
            sample_id:        Sample identifier string.
            acmg_results:     Pre-computed ACMG classification dict.  If None,
                              reporting stage will note "Not available".
            mode:             ``"full"`` (default) runs every stage through
                              Report generation, exactly as before. Pass
                              ``"vcf_only"`` to stop immediately after
                              Variant Calling and return the filtered VCF
                              without running Kim's own annotation/ACMG/
                              ancestry/reporting stages — this is the
                              mode used when Kim is acting purely as the
                              FASTQ-to-VCF engine in front of another
                              interpretation pipeline (e.g. GEPER).
            stop_after:       Explicit stage name to stop after. Currently
                              only ``"variant_calling"`` is supported and is
                              equivalent to ``mode="vcf_only"``. Takes
                              precedence over ``mode`` when both are given
                              (they must agree if both are set).

        Returns:
            ``PipelineResult`` summarising all stage outputs. When execution
            stops early, ``result.stopped_after`` names the last stage run
            and ``result.variant_calling["filtered_vcf_path"]`` holds the
            output VCF; ``result.report`` remains ``None`` in that case.
        """
        if mode not in self._VALID_MODES:
            raise ValueError(f"Invalid mode {mode!r} — must be one of {self._VALID_MODES}.")
        if stop_after is not None and stop_after not in self._VALID_STOP_AFTER:
            raise ValueError(
                f"Invalid stop_after {stop_after!r} — must be one of {self._VALID_STOP_AFTER}."
            )
        if mode == "vcf_only":
            stop_after = stop_after or "variant_calling"

        # "Silence must fail, not pass silently" (ruled 2026-09-02): the
        # original Windows-process-killing defect was a callback that
        # simply never fired, with nothing to say so -- a run with no
        # working cancellation path looked identical to one with a working
        # one, right up until someone tried DELETE. Refuse to proceed if
        # register_kill_callback() was never called, instead of silently
        # running an unkillable pipeline. A caller that has deliberately
        # decided killability doesn't apply (standalone CLI/script use, no
        # DELETE endpoint in the picture) must say so explicitly via
        # register_kill_callback(NO_KILL_TRACKING) -- see that constant's
        # docstring.
        if self._kill_callback is None:
            raise RuntimeError(
                "PipelineRunner.run() called without register_kill_callback() "
                "having been called first. A run with no registered kill "
                "callback cannot be cancelled via DELETE, and that must be a "
                "loud failure here, not a silent gap discovered later. Call "
                "register_kill_callback(<callback>) before run(), or "
                "register_kill_callback(NO_KILL_TRACKING) if this run "
                "genuinely doesn't need to be cancellable (e.g. a "
                "standalone CLI/script run with no API/DELETE involved)."
            )

        # Make self._kill_callback reachable from every subprocess spawned
        # anywhere below in this call -- pipeline/fastq/errors.py::_run()
        # and pipeline/utils/reference_cache.py's index-build helper read
        # this contextvar via spawn_tracked() without needing a callback
        # parameter threaded through either of their own many call sites.
        # Not reset in a finally: this run() call executes entirely inside
        # one dedicated thread-pool worker thread (api/main.py's
        # ThreadPoolExecutor), and every run() call sets this again at its
        # own entry, so a stale value between runs on a reused thread is
        # never read by anything (nothing spawns a subprocess outside an
        # active run() call in that thread).
        _CURRENT_KILL_CALLBACK.set(self._kill_callback)

        t_total = time.time()
        work_dir = Path(output_dir) / sample_id
        work_dir.mkdir(parents=True, exist_ok=True)

        # Validate config before any stage runs — let ConfigValidationError propagate
        validate_config(self._cfg)

        # ── Reference decompression (once, up front) ─────────────────────────
        # A plain `.gz` reference must be decompressed before *either*
        # downstream stage can use it: `samtools faidx` (called by
        # VariantCallingStage via freebayes_runner) cannot open a
        # standard-gzip FASTA at all (only uncompressed or bgzip). Resolving
        # this here, once, means both AlignmentStage and VariantCallingStage
        # below receive the same already-decompressed path -- neither stage
        # needs its own decompression logic, and a `.gz` reference is
        # decompressed exactly once per source file (cached by fingerprint;
        # re-running against an unchanged `.gz` reuses the existing copy).
        ref_cfg = self._cfg.get("reference") or {}
        try:
            reference_fasta = resolve_reference(
                reference_fasta,
                cache_dir=ref_cfg.get("local_cache_dir"),
            )
        except ReferenceCacheError as exc:
            raise FastqPipelineError(str(exc), stage="reference_cache") from exc

        result = PipelineResult(
            sample_id=sample_id,
            work_dir=str(work_dir),
            fastq_r1=fastq_r1,
            fastq_r2=fastq_r2,
            reference_fasta=reference_fasta,
        )

        # ── Per-sample file log ──────────────────────────────────────────────
        log_file_path = work_dir / "pipeline.log"
        _geper_logger = logging.getLogger("geper")
        _file_handler = logging.FileHandler(str(log_file_path), encoding="utf-8")
        _file_handler.setLevel(logging.DEBUG)
        _file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s \u2014 %(message)s")
        )
        _prev_geper_level = _geper_logger.level
        if _geper_logger.level == logging.NOTSET or _geper_logger.level > logging.INFO:
            _geper_logger.setLevel(logging.INFO)
        _geper_logger.addHandler(_file_handler)
        result.log_path = str(log_file_path)

        checkpoint = _load_checkpoint(work_dir) if self._resume else {}
        completed_stages = set(checkpoint.get("completed_stages", []))

        # Saved immediately (not deferred to the next stage's own
        # _save_checkpoint call) so a crash before stage 1 completes
        # doesn't lose it -- see _ensure_analysis_started_at's docstring.
        _ensure_analysis_started_at(checkpoint)
        _save_checkpoint(work_dir, checkpoint)

        logger.info(
            "=== PipelineRunner START [%s] resume=%s completed=%s ===",
            sample_id,
            self._resume,
            sorted(completed_stages),
        )

        # FIX 14: Capture database/reference versions and record in checkpoint.
        # On resume, verify versions match to prevent mixed-version outputs.
        _current_versions = _collect_reference_versions(self._cfg, reference_fasta)
        if self._resume and checkpoint.get("reference_versions"):
            _saved_versions = checkpoint["reference_versions"]
            _mismatches = {
                k: (v, _current_versions.get(k))
                for k, v in _saved_versions.items()
                if _current_versions.get(k) and _current_versions[k] != v
            }
            if _mismatches:
                _mm_str = "; ".join(
                    f"{k}: saved={sv!r} current={cv!r}" for k, (sv, cv) in _mismatches.items()
                )
                raise RuntimeError(
                    f"[{sample_id}] Reference version mismatch on resume — "
                    f"restart without --resume or ensure databases match. Mismatches: {_mm_str}"
                )
        checkpoint["reference_versions"] = _current_versions
        result.reference_versions = _current_versions

        try:
            # ── Stage 1: FASTQ Validation ─────────────────────────────────
            if "fastq_validation" not in completed_stages:
                _stage_t0_fastq_validation = time.monotonic()
                logger.info("[%s] Stage 1/5: FASTQ validation", sample_id)
                validator = FastqValidator()
                if fastq_r2:
                    r1_stats, r2_stats = validator.validate_paired(fastq_r1, fastq_r2)
                    result.qc_r1 = r1_stats.to_dict()
                    result.qc_r2 = r2_stats.to_dict()
                else:
                    r1_stats = validator.validate_single(fastq_r1)
                    result.qc_r1 = r1_stats.to_dict()

                completed_stages.add("fastq_validation")
                checkpoint["completed_stages"] = list(completed_stages)
                checkpoint["qc_r1"] = result.qc_r1
                checkpoint["qc_r2"] = result.qc_r2
                _save_checkpoint(work_dir, checkpoint)
                result.stages_completed.append("fastq_validation")
                logger.info(
                    "[TIMING] stage=%s elapsed=%.2fs",
                    "fastq_validation",
                    time.monotonic() - _stage_t0_fastq_validation,
                )
            else:
                logger.info("[%s] Stage 1/5: FASTQ validation — SKIPPED (checkpoint)", sample_id)
                result.qc_r1 = checkpoint.get("qc_r1")
                result.qc_r2 = checkpoint.get("qc_r2")
                result.stages_skipped.append("fastq_validation")

            # ── Stage 1b: QC Analysis ─────────────────────────────────────
            qc_out = str(work_dir / "qc")
            if "qc" not in completed_stages:
                _stage_t0_qc = time.monotonic()
                logger.info("[%s] Stage 1b/5: QC Analysis", sample_id)
                try:
                    qc_stage = QCStage(self._cfg)
                    qc_result = qc_stage.run(
                        fastq_r1=fastq_r1,
                        output_dir=qc_out,
                        fastq_r2=fastq_r2,
                        sample_id=sample_id,
                    )
                    result.qc_r1 = qc_result.metrics_r1.to_dict()
                    if qc_result.metrics_r2:
                        result.qc_r2 = qc_result.metrics_r2.to_dict()
                    completed_stages.add("qc")
                    checkpoint["completed_stages"] = list(completed_stages)
                    checkpoint["qc_r1"] = result.qc_r1
                    checkpoint["qc_r2"] = result.qc_r2
                    checkpoint["qc_report_json"] = qc_result.report_json_path
                    checkpoint["qc_report_html"] = qc_result.report_html_path
                    _save_checkpoint(work_dir, checkpoint)
                    result.stages_completed.append("qc")
                    logger.info(
                        "[TIMING] stage=%s elapsed=%.2fs", "qc", time.monotonic() - _stage_t0_qc
                    )
                    if not qc_result.qc_passed:
                        logger.warning(
                            "[%s] QC did not pass all thresholds — check %s",
                            sample_id,
                            qc_result.report_json_path,
                        )
                except QCThresholdError as qc_exc:
                    logger.error("[%s] QC threshold failure: %s", sample_id, qc_exc)
                    raise
                except Exception as qc_exc:
                    logger.warning(
                        "[%s] QC stage encountered an error (continuing): %s", sample_id, qc_exc
                    )
            else:
                logger.info("[%s] Stage 1b/5: QC Analysis — SKIPPED (checkpoint)", sample_id)
                result.stages_skipped.append("qc")

            # ── Stage 2: Alignment ────────────────────────────────────────
            align_out = str(work_dir / "alignment")
            if "alignment" not in completed_stages:
                _stage_t0_alignment = time.monotonic()
                logger.info("[%s] Stage 2/5: Alignment", sample_id)
                align_stage = AlignmentStage(self._cfg)
                align_result = align_stage.run(
                    fastq_r1=fastq_r1,
                    reference_fasta=reference_fasta,
                    output_dir=align_out,
                    fastq_r2=fastq_r2,
                    sample_id=sample_id,
                )
                result.alignment = align_result.to_dict()
                completed_stages.add("alignment")
                checkpoint["completed_stages"] = list(completed_stages)
                checkpoint["alignment"] = result.alignment
                _save_checkpoint(work_dir, checkpoint)
                result.stages_completed.append("alignment")
                logger.info(
                    "[TIMING] stage=%s elapsed=%.2fs",
                    "alignment",
                    time.monotonic() - _stage_t0_alignment,
                )
            else:
                logger.info("[%s] Stage 2/5: Alignment — SKIPPED (checkpoint)", sample_id)
                result.alignment = checkpoint.get("alignment", {})
                if not Path(result.alignment["sorted_bam_path"]).exists():
                    raise FastqPipelineError(
                        f"Checkpoint references BAM that no longer exists: "
                        f"{result.alignment['sorted_bam_path']} — delete checkpoint and re-run.",
                        stage="alignment",
                    )
                result.stages_skipped.append("alignment")

            sorted_bam = result.alignment["sorted_bam_path"]  # type: ignore[index]

            # ── Stage 3: Variant Calling ──────────────────────────────────
            vc_out = str(work_dir / "variant_calling")
            if "variant_calling" not in completed_stages:
                _stage_t0_variant_calling = time.monotonic()
                logger.info("[%s] Stage 3/5: Variant Calling", sample_id)
                vc_stage = VariantCallingStage(self._cfg)
                vc_result = vc_stage.run(
                    bam_path=sorted_bam,
                    reference_fasta=reference_fasta,
                    output_dir=vc_out,
                    sample_id=sample_id,
                )
                result.variant_calling = vc_result.to_dict()
                completed_stages.add("variant_calling")
                checkpoint["completed_stages"] = list(completed_stages)
                checkpoint["variant_calling"] = result.variant_calling
                _save_checkpoint(work_dir, checkpoint)
                result.stages_completed.append("variant_calling")
                logger.info(
                    "[TIMING] stage=%s elapsed=%.2fs",
                    "variant_calling",
                    time.monotonic() - _stage_t0_variant_calling,
                )
            else:
                logger.info("[%s] Stage 3/5: Variant Calling — SKIPPED (checkpoint)", sample_id)
                result.variant_calling = checkpoint.get("variant_calling", {})
                if not Path(result.variant_calling["filtered_vcf_path"]).exists():
                    raise FastqPipelineError(
                        f"Checkpoint references VCF that no longer exists: "
                        f"{result.variant_calling['filtered_vcf_path']} — delete checkpoint and re-run.",
                        stage="variant_calling",
                    )
                result.stages_skipped.append("variant_calling")

            filtered_vcf = result.variant_calling["filtered_vcf_path"]  # type: ignore[index]

            # ── Genome build detection (item 2) ────────────────────────────
            # Report review round 8, Task C: moved ahead of the vcf_only
            # early-stop below (was previously positioned after it,
            # meaning this detection never ran at all for a `--mode
            # vcf_only` invocation — exactly the mode
            # `bridge/combined_pipeline.py::run_kim_fastq_to_vcf` always
            # uses). This is real, already-existing detection logic
            # (`pipeline/utils/genome_build.py`, header-based, the same
            # ##reference/##contig-length signals GEPER's own
            # `pipeline/assembly_validator.py::detect_vcf_assembly`
            # uses independently) -- it was simply never given the
            # chance to run for the one workflow that actually needs its
            # result downstream. The result is now captured (not just
            # logged) and recorded in the checkpoint so
            # `bridge/combined_pipeline.py` can forward it to GEPER via
            # the existing `--assembly` flag, the same
            # checkpoint-field-forwarding shape the QC-metrics sidecar
            # (report review round 7) already established -- see
            # `qc_metrics_from_kim_checkpoint`'s docstring there for the
            # precedent this mirrors.
            #
            # Still best-effort and non-fatal either way: a VCF this
            # can't determine a build for (no ##reference/##contig
            # markers, e.g. a single-contig mitochondrial-only VCF,
            # where "build" is nearly meaningless anyway since rCRS
            # numbering doesn't vary between GRCh37/GRCh38) legitimately
            # yields `build=None`, which the checkpoint records
            # honestly rather than fabricating a default.
            detected_build = None
            try:
                from pipeline.utils.genome_build import warn_if_unsupported_build

                detection = warn_if_unsupported_build(filtered_vcf, sample_id=sample_id)
                detected_build = {
                    "build": detection.build,
                    "confidence": detection.confidence,
                    "source": detection.source,
                }
            except Exception as _build_exc:
                logger.debug("[%s] Genome build detection failed: %s", sample_id, _build_exc)
            checkpoint["detected_genome_build"] = detected_build
            _save_checkpoint(work_dir, checkpoint)

            # ── Early stop (mode="vcf_only" / stop_after="variant_calling") ──
            # Kim acting purely as the FASTQ-to-VCF engine: QC, Alignment, and
            # Variant Calling have already completed above. Do NOT continue
            # into Kim's own VEP/annotation/ACMG/ancestry/reporting
            # stages — those modules are left fully intact and still run
            # normally for standalone ("full") Kim runs, they are simply
            # skipped here so that filtered_variants.vcf can be handed off
            # to another interpretation pipeline (e.g. GEPER) unmodified.
            if stop_after == "variant_calling":
                logger.info(
                    "[%s] mode=vcf_only — stopping after Stage 3/5 Variant "
                    "Calling. filtered_vcf=%s",
                    sample_id,
                    filtered_vcf,
                )
                result.stopped_after = "variant_calling"
                result.success = True
                return result

            # ── Preflight: VCF vs GFF3 genome build ───────────────────────
            # Deliberately placed AFTER the vcf_only early return above,
            # not with the VCF build detection before it. In vcf_only
            # mode kim never loads the GFF3 -- annotation and reporting
            # do not run -- so a build disagreement between the VCF and a
            # configured-but-unused GFF3 is not a defect of that run, and
            # blocking it here would refuse bridge runs for a file they
            # never open. From this point on the GFF3's transcript model
            # IS used, to build every HGVS c. position.
            #
            # Raises ConfigValidationError on a definite disagreement;
            # deliberately NOT wrapped in a try/except, unlike the
            # detection above -- that block swallows failures to keep an
            # advisory warning advisory, and swallowing this one would
            # reinstate exactly the silence this check exists to end.
            _gff_for_build_check = (self._cfg.get("annotation", {}) or {}).get("refseq_gff") or (
                self._cfg.get("rna_analysis", {}) or {}
            ).get("refseq_gff")
            if _gff_for_build_check:
                from pipeline.utils.genome_build import check_vcf_gff3_build_consistency

                check_vcf_gff3_build_consistency(filtered_vcf, _gff_for_build_check)

            # ── Stage 2b: VEP Annotation ──────────────────────────────────
            vep_out = str(work_dir / "vep_annotation")
            vep_result = None
            if "vep_annotation" not in completed_stages:
                _stage_t0_vep_annotation = time.monotonic()
                logger.info("[%s] Stage 2b: VEP Annotation", sample_id)
                try:
                    vep_stage = VEPAnnotationStage(self._cfg)
                    vep_result = vep_stage.run(
                        filtered_vcf_path=filtered_vcf,
                        output_dir=vep_out,
                        sample_id=sample_id,
                    )
                    if (
                        vep_result.annotated_vcf_path
                        and vep_result.annotated_vcf_path != filtered_vcf
                    ):
                        filtered_vcf = vep_result.annotated_vcf_path
                    completed_stages.add("vep_annotation")
                    checkpoint["completed_stages"] = list(completed_stages)
                    checkpoint["vep_annotation"] = {
                        "annotated_vcf_path": vep_result.annotated_vcf_path,
                        "variant_count": vep_result.variant_count,
                    }
                    _save_checkpoint(work_dir, checkpoint)
                    result.stages_completed.append("vep_annotation")
                    logger.info(
                        "[TIMING] stage=%s elapsed=%.2fs",
                        "vep_annotation",
                        time.monotonic() - _stage_t0_vep_annotation,
                    )
                    logger.info(
                        "[%s] VEP annotation complete: %d variants",
                        sample_id,
                        vep_result.variant_count,
                    )
                except FastqPipelineError as vep_exc:
                    logger.warning("[%s] VEP annotation failed (non-fatal): %s", sample_id, vep_exc)
                    result.stages_skipped.append("vep_annotation")
                    result.errors.append(f"VEP annotation: {vep_exc}")
                except Exception as vep_exc:
                    logger.warning("[%s] VEP annotation error (non-fatal): %s", sample_id, vep_exc)
                    result.stages_skipped.append("vep_annotation")
            else:
                logger.info("[%s] Stage 2b: VEP Annotation — SKIPPED (checkpoint)", sample_id)
                _vep_cp = checkpoint.get("vep_annotation", {})
                _vep_vcf = _vep_cp.get("annotated_vcf_path", filtered_vcf)
                if _vep_vcf and Path(_vep_vcf).exists():
                    filtered_vcf = _vep_vcf
                result.stages_skipped.append("vep_annotation")

            blast_cfg = self._cfg.get("blast", {}) or {}
            blast_enabled = bool(blast_cfg.get("enabled", False))
            blast_result_data: dict | None = None

            if blast_enabled and "blast" not in completed_stages:
                _stage_t0_blast = time.monotonic()
                logger.info("[%s] Stage 3b: BLAST alignment", sample_id)
                try:
                    from pipeline.blast.stage import (
                        BLASTDatabaseError,
                        BLASTNotInstalledError,
                        BLASTStage,
                    )

                    blast_stage = BLASTStage(cfg=self._cfg)
                    if blast_stage.is_available():
                        blast_sequences: dict[str, str] = {}
                        # FIX 8: provide meaningful flanking context from FASTA
                        _flank = blast_cfg.get("flank_bp", 100)
                        _ref_fasta = self._cfg.get("reference_fasta") or self._cfg.get(
                            "annotation", {}
                        ).get("reference_fasta")
                        try:
                            import gzip as _gz

                            open_fn = _gz.open if filtered_vcf.endswith(".gz") else open
                            with open_fn(filtered_vcf, "rt") as _vcf_fh:
                                for _line in _vcf_fh:
                                    if _line.startswith("#"):
                                        continue
                                    _cols = _line.rstrip("\n").split("\t")
                                    if len(_cols) < 5:
                                        continue
                                    _chrom, _pos_s, _ref, _alt = (
                                        _cols[0],
                                        _cols[1],
                                        _cols[3],
                                        _cols[4],
                                    )
                                    _id = f"{_chrom}:{_pos_s}:{_ref}:{_alt.split(',')[0]}"
                                    _seq = None
                                    # FIX 8: extract flanking sequence from FASTA if available
                                    if _ref_fasta:
                                        try:
                                            from pipeline.annotation.codon_provider import (
                                                FastaCodonContextProvider,
                                            )

                                            _pos_i = int(_pos_s)
                                            _seq = FastaCodonContextProvider.fetch_sequence(
                                                _ref_fasta,
                                                _chrom,
                                                _pos_i - _flank,
                                                _pos_i + len(_ref) + _flank - 1,
                                            )
                                        except Exception:
                                            _seq = None
                                    # Fall back: REF allele alone is biologically useless for BLAST;
                                    # skip unless at least 50 bp are available (flank or long REF)
                                    if not _seq:
                                        if len(_ref) >= 50:
                                            _seq = _ref
                                        else:
                                            logger.debug(
                                                "[%s] BLAST: skipping %s — REF too short and no FASTA configured",
                                                sample_id,
                                                _id,
                                            )
                                            continue
                                    if len(_seq) >= 11:
                                        blast_sequences[_id] = _seq
                        except Exception as _seq_exc:
                            logger.warning(
                                "[%s] BLAST seq extraction failed: %s", sample_id, _seq_exc
                            )

                        if blast_sequences:
                            _br = blast_stage.run(
                                sequences=blast_sequences,
                                sample_id=sample_id,
                            )
                            blast_result_data = _br.to_dict()
                            completed_stages.add("blast")
                            checkpoint["completed_stages"] = list(completed_stages)
                            checkpoint["blast_hit_count"] = _br.hit_count
                            _save_checkpoint(work_dir, checkpoint)
                            result.stages_completed.append("blast")
                            logger.info(
                                "[TIMING] stage=%s elapsed=%.2fs",
                                "blast",
                                time.monotonic() - _stage_t0_blast,
                            )
                            logger.info("[%s] BLAST done: %d hits", sample_id, _br.hit_count)
                        else:
                            logger.info("[%s] BLAST skipped: no sequences long enough", sample_id)
                            result.stages_skipped.append("blast")
                    else:
                        logger.info("[%s] BLAST skipped: blastn binary not found", sample_id)
                        result.stages_skipped.append("blast")
                except (BLASTNotInstalledError, BLASTDatabaseError) as _blast_err:
                    logger.warning("[%s] BLAST unavailable (non-fatal): %s", sample_id, _blast_err)
                    result.stages_skipped.append("blast")
                except Exception as _blast_exc:
                    logger.warning("[%s] BLAST stage failed (non-fatal): %s", sample_id, _blast_exc)
                    result.stages_skipped.append("blast")
            elif blast_enabled:
                logger.info("[%s] Stage 3b: BLAST — SKIPPED (checkpoint)", sample_id)
                result.stages_skipped.append("blast")
            else:
                logger.debug(
                    "[%s] Stage 3b: BLAST disabled (set blast.enabled=true to activate)", sample_id
                )

            # ── Stage 4: Annotation ───────────────────────────────────────
            ann_out = str(work_dir / "annotation")
            if "annotation" not in completed_stages:
                _stage_t0_annotation = time.monotonic()
                logger.info("[%s] Stage 4/5: Annotation", sample_id)
                ann_stage = AnnotationStage(self._cfg)
                ann_result = ann_stage.run(
                    filtered_vcf_path=filtered_vcf,
                    output_dir=ann_out,
                    sample_id=sample_id,
                )
                result.annotation = ann_result.to_dict()

                # FIX 12: Merge BLAST results into annotation variants.
                # BLAST hits are supporting information only — they do NOT override
                # ACMG evidence. They are attached as a 'blast_hits' field on each
                # variant whose sequence was submitted to BLAST.
                if blast_result_data and isinstance(result.annotation.get("variants"), list):
                    blast_hits_by_id = {}
                    for hit in blast_result_data.get("hits", []):
                        seq_id = hit.get("sequence_id", "")
                        # seq_id format: "chrom:pos:ref:alt"
                        blast_hits_by_id.setdefault(seq_id, []).append(hit)

                    for var in result.annotation["variants"]:
                        _bid = f"{var.get('chrom', '')}:{var.get('pos', '')}:{var.get('ref', '')}:{var.get('alt', '')}"
                        _hits = blast_hits_by_id.get(_bid, [])
                        if _hits:
                            var["blast_hits"] = _hits

                    result.annotation["blast_summary"] = {
                        "total_hits": blast_result_data.get("hit_count", 0),
                        "blast_db": blast_result_data.get("database", ""),
                        "note": BLAST_SUMMARY_NOTE,
                    }
                    logger.info(
                        "[%s] BLAST results integrated into annotation: %d hits",
                        sample_id,
                        blast_result_data.get("hit_count", 0),
                    )

                completed_stages.add("annotation")
                checkpoint["completed_stages"] = list(completed_stages)
                checkpoint["annotation"] = {
                    k: v for k, v in result.annotation.items() if k != "variants"
                }
                _save_checkpoint(work_dir, checkpoint)
                result.stages_completed.append("annotation")
                logger.info(
                    "[TIMING] stage=%s elapsed=%.2fs",
                    "annotation",
                    time.monotonic() - _stage_t0_annotation,
                )
            else:
                logger.info("[%s] Stage 4/5: Annotation — SKIPPED (checkpoint)", sample_id)
                # Reload annotation from its JSON output
                ann_json = Path(ann_out) / "annotation.json"
                if ann_json.exists():
                    result.annotation = json.loads(ann_json.read_text())
                else:
                    result.annotation = checkpoint.get("annotation", {})
                result.stages_skipped.append("annotation")

            # ── Stage 4b: ACMG + Evidence Scoring ────────────────────────
            if "acmg_evidence" not in completed_stages:
                _stage_t0_acmg_evidence = time.monotonic()
                logger.info("[%s] Stage 4b: ACMG + Evidence Scoring", sample_id)
                try:
                    from pipeline.orchestration.shared import run_acmg_evidence_batch

                    ann_variants: list[dict] = []
                    if result.annotation and isinstance(result.annotation.get("variants"), list):
                        ann_variants = result.annotation["variants"]

                    # ── FIX 11: load pedigree/phenotype sidecar ───────────
                    _pedigree_data: dict = {}
                    _global_inheritance: str | None = None
                    if pedigree_json:
                        _pedigree_path = Path(pedigree_json)
                        if _pedigree_path.exists():
                            try:
                                _ped_raw = json.loads(_pedigree_path.read_text())
                                _global_inheritance = _ped_raw.get("inheritance_pattern")
                                _pedigree_data = _ped_raw.get("variants", {})
                                logger.info(
                                    "[%s] Loaded pedigree sidecar: %d variant entries, inheritance=%s",
                                    sample_id,
                                    len(_pedigree_data),
                                    _global_inheritance,
                                )
                            except Exception as _ped_exc:
                                logger.warning(
                                    "[%s] Pedigree sidecar malformed — skipping: %s",
                                    sample_id,
                                    _ped_exc,
                                )
                        else:
                            logger.warning(
                                "[%s] Pedigree sidecar not found at %s — skipping",
                                sample_id,
                                pedigree_json,
                            )

                    # FIX (vcf-wiring refactor): the per-variant ClinVar/gnomAD/
                    # constraint/hotspot/ACMG/AI logic now lives in
                    # pipeline/orchestration/shared.run_acmg_evidence_batch so
                    # that `python main.py vcf` can reuse the identical,
                    # already-tested logic instead of skipping these stages.
                    acmg_batch: list[dict] = run_acmg_evidence_batch(
                        ann_variants=ann_variants,
                        cfg=self._cfg,
                        sample_id=sample_id,
                        pedigree_data=_pedigree_data,
                        global_inheritance=_global_inheritance,
                    )

                    result.acmg_results = acmg_batch
                    acmg_results = acmg_batch  # pass to reporting stage below
                    completed_stages.add("acmg_evidence")
                    checkpoint["completed_stages"] = list(completed_stages)
                    checkpoint["acmg_evidence_count"] = len(acmg_batch)
                    _save_checkpoint(work_dir, checkpoint)
                    result.stages_completed.append("acmg_evidence")
                    logger.info(
                        "[TIMING] stage=%s elapsed=%.2fs",
                        "acmg_evidence",
                        time.monotonic() - _stage_t0_acmg_evidence,
                    )
                    logger.info(
                        "[%s] Stage 4b complete: %d variants classified", sample_id, len(acmg_batch)
                    )
                except Exception as exc:
                    # This except only fires for setup failures (classifier construction,
                    # missing imports) — per-variant errors are already caught above.
                    logger.error(
                        "[%s] Stage 4b ACMG/Evidence setup failed: %s",
                        sample_id,
                        exc,
                        exc_info=True,
                    )
                    result.acmg_results = []
                    acmg_results = []
                    result.errors.append(f"Stage 4b setup error: {exc}")
            else:
                logger.info("[%s] Stage 4b: ACMG/Evidence — SKIPPED (checkpoint)", sample_id)
                result.acmg_results = result.acmg_results or []
                acmg_results = result.acmg_results

            # ── Stage 4d: Ancestry Inference ─────────────────────────────
            ancestry_result = None
            ancestry_out = str(work_dir / "ancestry")
            if "ancestry" not in completed_stages:
                _stage_t0_ancestry = time.monotonic()
                logger.info("[%s] Stage 4d: Ancestry Inference", sample_id)
                try:
                    from pipeline.ancestry.stage import AncestryStage

                    vcf_for_ancestry = (
                        result.variant_calling.get("filtered_vcf_path", "")
                        if result.variant_calling
                        else ""
                    )
                    ancestry_stage = AncestryStage(cfg=self._cfg)
                    ancestry_result = ancestry_stage.run(
                        vcf_path=vcf_for_ancestry,
                        output_dir=ancestry_out,
                        sample_id=sample_id,
                    )
                    completed_stages.add("ancestry")
                    checkpoint["completed_stages"] = list(completed_stages)
                    checkpoint["ancestry_json_path"] = ancestry_result.report_json_path
                    _save_checkpoint(work_dir, checkpoint)
                    result.stages_completed.append("ancestry")
                    logger.info(
                        "[TIMING] stage=%s elapsed=%.2fs",
                        "ancestry",
                        time.monotonic() - _stage_t0_ancestry,
                    )
                    logger.info(
                        "[%s] Stage 4d complete: Ancestry=%s (confidence=%s)",
                        sample_id,
                        ancestry_result.primary_population,
                        ancestry_result.confidence,
                    )
                except Exception as exc:
                    logger.warning("[%s] Stage 4d Ancestry failed (non-fatal): %s", sample_id, exc)
                    result.stages_skipped.append("ancestry")
            else:
                logger.info("[%s] Stage 4d: Ancestry — SKIPPED (checkpoint)", sample_id)
                result.stages_skipped.append("ancestry")

            # ── Stage 5: Report Generation ────────────────────────────────
            report_out = str(work_dir / "reporting")
            if "reporting" not in completed_stages:
                _stage_t0_reporting = time.monotonic()
                logger.info("[%s] Stage 5/5: Report Generation", sample_id)
                report_stage = ReportingStage(self._cfg)
                report_result = report_stage.run(
                    sample_id=sample_id,
                    output_dir=report_out,
                    qc_summary=result.qc_r1,
                    alignment_stats=result.alignment,
                    variant_stats=result.variant_calling,
                    annotation_result=result.annotation,
                    acmg_results=acmg_results,
                    ancestry_result=ancestry_result,
                    reference_versions=result.reference_versions,  # FIX 14
                    analysis_started_at=checkpoint.get("analysis_started_at"),
                )
                result.report = report_result.to_dict()
                completed_stages.add("reporting")
                checkpoint["completed_stages"] = list(completed_stages)
                checkpoint["report"] = result.report
                _save_checkpoint(work_dir, checkpoint)
                result.stages_completed.append("reporting")
                logger.info(
                    "[TIMING] stage=%s elapsed=%.2fs",
                    "reporting",
                    time.monotonic() - _stage_t0_reporting,
                )
            else:
                logger.info("[%s] Stage 5/5: Report — SKIPPED (checkpoint)", sample_id)
                result.report = checkpoint.get("report", {})
                result.stages_skipped.append("reporting")

            result.success = True

        except Exception as exc:
            result.success = False
            result.error = str(exc)
            logger.error("[%s] Pipeline failed: %s", sample_id, exc, exc_info=True)
            raise

        finally:
            result.total_elapsed_seconds = round(time.time() - t_total, 3)
            logger.info(
                "=== PipelineRunner END [%s] success=%s elapsed=%.2fs ===",
                sample_id,
                result.success,
                result.total_elapsed_seconds,
            )
            # ── Flush, close, and remove per-sample file handler ─────────────
            try:
                _file_handler.flush()
                _file_handler.close()
                _geper_logger.removeHandler(_file_handler)
            except Exception:
                pass
            # Write log_path into checkpoint
            try:
                cp = _load_checkpoint(work_dir)
                cp["log_path"] = result.log_path
                _save_checkpoint(work_dir, cp)
            except Exception:
                pass

        return result


# ─── CLI entry point ──────────────────────────────────────────────────────────


def _build_cli_parser():
    import argparse

    from pipeline.reporting.component_identity import PIPELINE_VERSION

    # ASCII "->", not U+2192: this help must print on a default Windows
    # (cp1252) console, which cannot encode the arrow (human ruling 2026-09-11).
    p = argparse.ArgumentParser(
        description=f"{PIPELINE_VERSION}: FASTQ -> QC -> Alignment -> Variant Calling "
        "-> Annotation -> Report",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--r1", required=True, metavar="FASTQ", help="R1 FASTQ path (plain or .gz)")
    p.add_argument("--r2", metavar="FASTQ", default=None, help="R2 FASTQ path (paired-end)")
    p.add_argument("--ref", required=True, metavar="FASTA", help="Reference genome FASTA path")
    p.add_argument("--output-dir", required=True, metavar="DIR", help="Output root directory")
    p.add_argument("--sample-id", default="SAMPLE", metavar="ID", help="Sample identifier")
    p.add_argument(
        "--config",
        default=None,
        metavar="YAML",
        help="Path to config YAML (default: config/default.yaml)",
    )
    p.add_argument("--no-resume", action="store_true", help="Disable checkpoint/resume")
    p.add_argument(
        "--mode",
        default="full",
        choices=["full", "vcf_only"],
        help=(
            "'full' (default) runs FASTQ through Report generation. "
            "'vcf_only' stops after Variant Calling and returns "
            "filtered_variants.vcf, skipping Kim's own annotation/ACMG/"
            "ancestry/reporting stages — used when Kim is feeding "
            "another interpretation pipeline (e.g. the Bij AI "
            "variant-interpretation component (GEPER))."
        ),
    )
    p.add_argument(
        "--stop-after",
        default=None,
        choices=["variant_calling"],
        help="Explicit stage name to stop after (equivalent to --mode vcf_only).",
    )
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p


def main() -> None:
    """CLI entry point: ``python -m pipeline.orchestration.runner``."""
    import sys

    import yaml  # type: ignore  # graceful error below if absent

    parser = _build_cli_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    )

    # Load config
    cfg: dict = {}
    config_path = args.config
    if config_path is None:
        default_cfg = Path(__file__).resolve().parents[3] / "config" / "default.yaml"
        if default_cfg.exists():
            config_path = str(default_cfg)

    if config_path:
        try:
            with open(config_path) as fh:
                cfg = yaml.safe_load(fh) or {}
            logger.info("Config loaded from %s", config_path)
        except ImportError:
            logger.warning(
                "PyYAML not installed — config file ignored. Install it: pip install pyyaml"
            )
        except Exception as exc:
            logger.error("Failed to load config %s: %s", config_path, exc)
            sys.exit(1)

    runner = PipelineRunner(cfg=cfg, resume=not args.no_resume)
    # CLI run, no API/DELETE endpoint involved -- explicitly opt out of
    # cancellation tracking rather than leaving it unregistered (run()
    # refuses to proceed with neither, see NO_KILL_TRACKING's docstring).
    runner.register_kill_callback(NO_KILL_TRACKING)
    try:
        result = runner.run(
            fastq_r1=args.r1,
            reference_fasta=args.ref,
            output_dir=args.output_dir,
            fastq_r2=args.r2,
            sample_id=args.sample_id,
            mode=args.mode,
            stop_after=args.stop_after,
        )
        if result.stopped_after:
            print(
                f"\nPipeline stopped after {result.stopped_after} — "
                f"filtered VCF: {result.variant_calling.get('filtered_vcf_path')}"
            )
        else:
            print(f"\nPipeline complete — report: {result.report}")
        sys.exit(0)
    except Exception as exc:
        print(f"\nPipeline FAILED: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
