#!/usr/bin/env python3
"""
main.py
────────
Bij AI v8 — unified command-line interface.

Subcommands
───────────
  analyze     Full FASTQ → Report pipeline (GEPER primary workflow)
  vcf         Annotate + classify an existing VCF (skip alignment & variant calling)
  classify    Classify a single variant from CLI flags (ACMG + evidence score)
  serve       Start the FastAPI REST server
  validate    Validate a config file without running the pipeline
  test        Run the GEPER test suite

Examples
────────
  # Full pipeline
  python main.py analyze \\
      --r1 sample_R1.fastq.gz --r2 sample_R2.fastq.gz \\
      --ref GRCh38.fasta --output-dir ./work --sample-id sample01

  # Annotate an existing VCF
  python main.py vcf \\
      --input my_variants.vcf --output-dir ./reports \\
      --config config/production.yaml

  # Classify a single variant
  python main.py classify \\
      --chrom chr17 --pos 43057051 --ref A --alt T \\
      --gene BRCA1 --cadd 40.5 --revel 0.92 --output-dir ./reports

  # Start API server
  python main.py serve --host 0.0.0.0 --port 8000

  # Validate config
  python main.py validate --config config/production.yaml

  # Run tests
  python main.py test
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("geper.main")


# ─── Config loading ───────────────────────────────────────────────────────────


def _load_config(config_path: Optional[str] = None) -> Dict:
    """Load YAML config from path, env var, or default.yaml (in that order)."""
    resolved = (
        config_path
        or os.environ.get("GEPER_CONFIG")
        or str(Path(__file__).parent / "config" / "default.yaml")
    )
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not installed — using empty config. Run: pip install pyyaml")
        return {}

    path = Path(resolved)
    if not path.exists():
        logger.warning("Config file not found: %s — using empty config.", resolved)
        return {}

    try:
        with open(path) as fh:
            cfg = yaml.safe_load(fh) or {}
        logger.info("Config loaded from %s", resolved)
        return cfg
    except Exception as exc:
        logger.error("Failed to load config %s: %s", resolved, exc)
        sys.exit(1)


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )


def _print_startup_validation(args: argparse.Namespace, cfg: Dict) -> None:
    """Print the GEPER startup validation banner and run the consolidated
    dependency check before Stage 1. Raises DependencyValidationError if a
    required external tool is missing, or FileNotFoundError if --ref/--r1
    don't exist (both are caught by the caller for a clean CLI exit).

    This does not modify PipelineRunner or any pipeline stage; it is a
    CLI-layer pre-flight gate only.
    """
    from pipeline.utils.dependency_validator import assert_required_dependencies

    print("=" * 30)
    print("Bij AI Environment Validation")
    print("=" * 30)

    py_ok = sys.version_info[:2] >= (3, 10)
    print(f"Python     : {sys.version.split()[0]} {'OK' if py_ok else 'UNSUPPORTED (<3.10)'}")

    print(
        f"Config     : {'loaded' if cfg else 'empty/default'}"
        f" ({args.config or os.environ.get('GEPER_CONFIG') or 'config/default.yaml'})"
    )

    ref_ok = bool(args.ref) and Path(args.ref).exists()
    print(f"Reference  : {args.ref} — {'found' if ref_ok else 'NOT FOUND'}")

    r1_ok = bool(args.r1) and Path(args.r1).exists()
    print(f"FASTQ      : {args.r1} — {'found' if r1_ok else 'NOT FOUND'}")
    if getattr(args, "r2", None):
        r2_ok = Path(args.r2).exists()
        print(f"FASTQ (R2) : {args.r2} — {'found' if r2_ok else 'NOT FOUND'}")

    aligner_cfg = str((cfg.get("alignment") or {}).get("aligner", "auto")).lower()
    require_minimap2 = aligner_cfg == "minimap2"
    vep_cfg = cfg.get("vep", {}) or {}
    require_vep = bool(vep_cfg.get("enabled", True)) and bool(vep_cfg.get("required", False))

    report = assert_required_dependencies(
        require_minimap2=require_minimap2,
        require_vep=require_vep,
    )
    display_names = {
        "bwa": "BWA",
        "minimap2": "minimap2",
        "samtools": "samtools",
        "freebayes": "FreeBayes",
        "bcftools": "bcftools",
        "vep": "VEP",
    }
    for tool_name in ("bwa", "minimap2", "samtools", "freebayes", "bcftools", "vep"):
        match = next((r for r in report.results if r.name == tool_name), None)
        if match is None:
            continue
        version = f" ({match.version})" if match.version else ""
        print(f"{display_names[tool_name]:<11}: {'found' if match.found else 'MISSING'}{version}")

    print("=" * 30)

    if not ref_ok:
        raise FileNotFoundError(f"Reference genome not found: {args.ref}")
    if not r1_ok:
        raise FileNotFoundError(f"FASTQ R1 not found: {args.r1}")


def cmd_analyze(args: argparse.Namespace) -> int:
    """Run the full FASTQ → Report pipeline."""
    _setup_logging(args.log_level)
    cfg = _load_config(args.config)

    # Allow GEPER_OUTPUT_DIR env var as fallback
    output_dir = args.output_dir or os.environ.get("GEPER_OUTPUT_DIR")
    if not output_dir:
        print("ERROR: --output-dir is required (or set GEPER_OUTPUT_DIR).", file=sys.stderr)
        return 1

    from pipeline.utils.dependency_validator import DependencyValidationError

    try:
        _print_startup_validation(args, cfg)
    except DependencyValidationError as exc:
        # Fail fast, before Stage 1 — one consolidated error rather than a
        # mid-pipeline crash inside alignment/variant-calling.
        print(f"\n{exc}", file=sys.stderr)
        return 3
    except FileNotFoundError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1

    from pipeline.orchestration.runner import NO_KILL_TRACKING, PipelineRunner
    from pipeline.fastq.errors import FastqPipelineError
    from pipeline.config_validator import ConfigValidationError

    # ── Fix #5: wire --cadd/--revel/--spliceai CLI overrides into acmg_thresholds ──
    score_overrides = {}
    if getattr(args, "cadd", None) is not None:
        score_overrides["pp3_cadd_phred"] = args.cadd
        score_overrides["bp4_cadd_phred"] = args.cadd
    if getattr(args, "revel", None) is not None:
        score_overrides["pp3_revel"] = args.revel
        score_overrides["bp4_revel"] = args.revel
    if getattr(args, "spliceai", None) is not None:
        score_overrides["pp3_spliceai"] = args.spliceai
        score_overrides["bp4_spliceai"] = args.spliceai
    if score_overrides:
        cfg.setdefault("acmg_thresholds", {}).update(score_overrides)
        logger.info("ACMG threshold overrides from CLI: %s", score_overrides)

    if getattr(args, "bwa_index_dir", None):
        cfg.setdefault("alignment", {})["index_dir"] = args.bwa_index_dir
        logger.info("Persistent BWA index directory (CLI override): %s", args.bwa_index_dir)
    if getattr(args, "ref_cache_dir", None):
        cfg.setdefault("reference", {})["local_cache_dir"] = args.ref_cache_dir
        logger.info(
            "Reference decompression cache directory (CLI override): %s", args.ref_cache_dir
        )

    runner = PipelineRunner(cfg=cfg, resume=not args.no_resume)
    # CLI run, no API/DELETE endpoint involved -- explicitly opt out of
    # cancellation tracking rather than leaving it unregistered (run()
    # refuses to proceed with neither, see NO_KILL_TRACKING's docstring).
    runner.register_kill_callback(NO_KILL_TRACKING)
    try:
        result = runner.run(
            fastq_r1=args.r1,
            reference_fasta=args.ref,
            output_dir=output_dir,
            fastq_r2=args.r2,
            sample_id=args.sample_id or Path(args.r1).stem.replace("_R1", ""),
            mode=getattr(args, "mode", "full"),
            stop_after=getattr(args, "stop_after", None),
        )
        print(f"\n✓ Pipeline complete in {result.total_elapsed_seconds:.1f}s")
        if result.stopped_after:
            print(f"  Stopped after: {result.stopped_after} (mode={args.mode})")
            print(f"  Filtered VCF : {result.variant_calling.get('filtered_vcf_path')}")
        else:
            print(f"  Report : {result.report}")
        print(f"  Stages : {', '.join(result.stages_completed)}")
        if result.stages_skipped:
            print(f"  Skipped: {', '.join(result.stages_skipped)} (checkpoint)")
        return 0
    except ConfigValidationError as exc:
        print(f"\nConfig validation failed:\n{exc}", file=sys.stderr)
        return 2
    except FastqPipelineError as exc:
        print(f"\nPipeline error [{exc.stage}]: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\nUnexpected error: {exc}", file=sys.stderr)
        logger.exception("Unhandled exception in analyze")
        return 1


# ─── Subcommand: vcf ─────────────────────────────────────────────────────────


def cmd_vcf(args: argparse.Namespace) -> int:
    """Run the complete VCF→Report workflow on an existing VCF:
    VEP -> Annotation -> ClinVar -> gnomAD -> ACMG -> Evidence Aggregation
    -> AI (if enabled) -> Final Report.

    This reuses the exact same stage implementations and the same
    ClinVar/gnomAD/ACMG/AI evidence logic (pipeline/orchestration/shared.py)
    as the `analyze` workflow — nothing here is duplicated or reimplemented.
    """
    _setup_logging(args.log_level)
    cfg = _load_config(args.config)

    output_dir = args.output_dir or os.environ.get("GEPER_OUTPUT_DIR")
    if not output_dir:
        print("ERROR: --output-dir is required.", file=sys.stderr)
        return 1

    vcf_path = Path(args.input)
    if not vcf_path.exists():
        print(f"ERROR: VCF not found: {args.input}", file=sys.stderr)
        return 1

    sample_id = args.sample_id or vcf_path.stem
    out_dir = Path(output_dir) / sample_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Genome build detection (item 2) ─────────────────────────────────────
    # GEPER's bundled gnomAD resources are GRCh38-only; warn loudly rather
    # than silently mis-mapping coordinates for GRCh37/hg19 input.
    from pipeline.utils.genome_build import warn_if_unsupported_build, SUPPORTED_BUILD

    build_detection = warn_if_unsupported_build(str(vcf_path), sample_id=sample_id)
    if build_detection.build and build_detection.build != SUPPORTED_BUILD:
        print(
            f"  [WARN] Detected genome build {build_detection.build}, but Bij AI only "
            f"supports {SUPPORTED_BUILD}. ClinVar/gnomAD results will be "
            f"coordinate-mismatched. Liftover to {SUPPORTED_BUILD} first.",
            file=sys.stderr,
        )

    stages_completed: list = []
    stages_skipped: list = []
    current_vcf = str(vcf_path)

    # ── Stage 1: VEP annotation (optional, non-fatal) ──────────────────────
    print(f"Annotating VCF: {vcf_path}")
    vep_cfg = cfg.get("vep", {}) or {}
    vep_enabled = bool(vep_cfg.get("enabled", True))
    if not vep_enabled:
        msg = "  [WARN] VEP annotation SKIPPED: vep.enabled is false in config."
        print(msg, file=sys.stderr)
        logger.warning("[%s] VEP annotation skipped — vep.enabled is false", sample_id)
        stages_skipped.append("vep_annotation")
    else:
        try:
            from pipeline.vep.stage import VEPAnnotationStage

            vep_stage = VEPAnnotationStage(cfg)
            vep_result = vep_stage.run(
                filtered_vcf_path=current_vcf,
                output_dir=str(out_dir / "vep_annotation"),
                sample_id=sample_id,
            )
            if vep_result.annotated_vcf_path and vep_result.annotated_vcf_path != current_vcf:
                current_vcf = vep_result.annotated_vcf_path
            stages_completed.append("vep_annotation")
            print(f"  [OK] VEP annotation: {vep_result.variant_count} variants")
        except Exception as vep_exc:
            # FIX (Issue 3): a skipped VEP stage must never be silent — print
            # a clear, user-facing warning in addition to the log message,
            # since gene-dependent ACMG criteria (PM1, PM5, PP2, BP1, ...)
            # depend on VEP's CSQ-embedded gene/transcript annotations.
            msg = (
                f"  [WARN] VEP annotation SKIPPED (non-fatal): {vep_exc}\n"
                f"    Gene-dependent ACMG criteria (PM1, PM5, PP2, BP1, etc.) "
                f"may be unavailable without VEP annotation."
            )
            print(msg, file=sys.stderr)
            logger.warning("[%s] VEP annotation skipped (non-fatal): %s", sample_id, vep_exc)
            stages_skipped.append("vep_annotation")

    # ── Stage 2: Annotation (gene/transcript/HGVS; gracefully degrades) ────
    from pipeline.annotation.stage import AnnotationStage

    ann_stage = AnnotationStage(cfg=cfg)
    ann_result = ann_stage.run(
        filtered_vcf_path=current_vcf,
        output_dir=str(out_dir),
        sample_id=sample_id,
    )
    stages_completed.append("annotation")
    ann_variants = [
        (v.to_dict() if hasattr(v, "to_dict") else dict(v)) for v in ann_result.variants
    ]
    print(f"  [OK] Annotation: {len(ann_variants)} variants")

    # ── Stage 3: ClinVar + gnomAD + ACMG + Evidence + AI ────────────────────
    # Shared with `analyze` mode — see pipeline/orchestration/shared.py.
    from pipeline.orchestration.shared import run_acmg_evidence_batch

    acmg_results = []
    try:
        acmg_results = run_acmg_evidence_batch(
            ann_variants=ann_variants,
            cfg=cfg,
            sample_id=sample_id,
        )
        stages_completed.append("clinvar")
        stages_completed.append("gnomad")
        stages_completed.append("acmg_evidence")
        n_errors = sum(1 for r in acmg_results if "error" in r)
        print(
            f"  [OK] ClinVar + gnomAD + ACMG + Evidence: {len(acmg_results)} variants "
            f"classified ({n_errors} errors)"
        )
    except Exception as exc:
        logger.error("ACMG/Evidence stage setup failed: %s", exc, exc_info=True)
        stages_skipped.extend(["clinvar", "gnomad", "acmg_evidence"])
        print(f"  [FAIL] ClinVar/gnomAD/ACMG stage failed: {exc}", file=sys.stderr)

    # AI engine status is folded into the ACMG/Evidence stage above
    # (pipeline/orchestration/shared.py instantiates pipeline.ai.engine.AiEngine
    # when torch/transformers are installed and scores per-variant; it is a
    # silent no-op otherwise, matching `analyze` mode behavior).
    try:
        import pipeline.ai.engine  # noqa: F401

        stages_completed.append("ai")
    except Exception:
        stages_skipped.append("ai")

    # ── Stage 5: Final report ───────────────────────────────────────────────
    try:
        from pipeline.reporting.stage import ReportingStage

        report_stage = ReportingStage(cfg)
        report_result = report_stage.run(
            sample_id=sample_id,
            output_dir=str(out_dir / "reporting"),
            variant_stats={"filtered_vcf_path": current_vcf},
            annotation_result=ann_result,
            acmg_results=acmg_results,
        )
        stages_completed.append("reporting")
        print(
            f"  [OK] Report generated -> {report_result.to_dict().get('report_json_path', out_dir / 'reporting')}"
        )
    except Exception as exc:
        logger.warning("Report generation failed (non-fatal): %s", exc)
        stages_skipped.append("reporting")

    # ── Classified-variants JSON (kept for backward compatibility) ─────────
    out_json = out_dir / "classified_variants.json"
    out_json.write_text(json.dumps(acmg_results, indent=2))

    print(f"\n[OK] {len(acmg_results)} variants classified -> {out_json}")
    print(f"  Stages completed: {', '.join(stages_completed) or '(none)'}")
    if stages_skipped:
        print(f"  Stages skipped:   {', '.join(stages_skipped)}")
    return 0


# ─── Subcommand: classify ────────────────────────────────────────────────────


def cmd_classify(args: argparse.Namespace) -> int:
    """Classify a single variant from CLI flags."""
    _setup_logging(args.log_level)
    cfg = _load_config(args.config)

    from pipeline.acmg.classifier import AcmgClassifier, VariantEvidence
    from pipeline.evidence.aggregator import EvidenceAggregator

    evidence = VariantEvidence(
        chrom=args.chrom,
        pos=args.pos,
        ref=args.ref,
        alt=args.alt,
        gene=args.gene,
        cadd_phred=args.cadd,
        revel_score=args.revel,
        spliceai_score=args.spliceai,
        gnomad_af=args.gnomad_af,
        is_lof=args.lof,
        lof_gene_intolerant=args.lof_intolerant,
        is_missense=args.missense,
    )

    acmg_clf = AcmgClassifier(cfg=cfg)
    aggregator = EvidenceAggregator(cfg=cfg)

    acmg_result = acmg_clf.classify(evidence)

    comp_score = EvidenceAggregator.compute_computational_score(
        cadd_phred=args.cadd,
        revel_score=args.revel,
        spliceai_score=args.spliceai,
    )
    ev_result = aggregator.aggregate_from_acmg(
        acmg_result,
        computational_score=comp_score,
    )

    print(f"\n{'─' * 55}")
    print(f"  Variant   : {args.chrom}:{args.pos} {args.ref}>{args.alt}")
    if args.gene:
        print(f"  Gene      : {args.gene}")
    print(f"  ACMG      : {acmg_result.classification}")
    print(f"  ACMG score: {acmg_result.score:.4f}")
    print(f"  Criteria  : {', '.join(acmg_result.criteria_met) or 'none'}")
    print(f"  Evidence  : {ev_result.final_tier} (composite={ev_result.composite_score:.4f})")
    print(f"{'─' * 55}")

    if args.output_dir:
        out = Path(args.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        tag = f"{args.chrom}_{args.pos}_{args.ref}_{args.alt}"
        out_file = out / f"classify_{tag}.json"
        out_file.write_text(
            json.dumps(
                {
                    "acmg": acmg_result.to_dict(),
                    "evidence": ev_result.to_dict(),
                },
                indent=2,
            )
        )
        print(f"  Saved     : {out_file}")
    return 0


# ─── Subcommand: serve ───────────────────────────────────────────────────────


def cmd_serve(args: argparse.Namespace) -> int:
    """Start the GEPER FastAPI REST server."""
    _setup_logging(args.log_level)
    try:
        import uvicorn
    except ImportError:
        print("ERROR: uvicorn not installed. Run: pip install uvicorn[standard]", file=sys.stderr)
        return 1

    # FastAPI app is in api/main.py (app = FastAPI(...)) — check it exists.
    #
    # CORRECTED: this used to check for geper/api/app.py with a create_app()
    # factory, which has never existed -- kim_pipeline/geper/ has no api/
    # submodule at all (it's a local package holding kim_pipeline/geper/
    # pipeline/ only). The path that actually works, and that
    # serve_api.py already launches successfully, is api/main.py's
    # plain `app = FastAPI(...)` instance. Deliberately NOT
    # "geper.api...": kim_pipeline's own local `geper` subpackage and the
    # top-level `geper/` package at the repo root share that name, so an
    # import string built on it is ambiguous depending on what else is on
    # sys.path when this runs (e.g. the combined bridge workflow, which
    # needs the top-level `geper` package importable too) -- using
    # "api.main:app" instead sidesteps that collision entirely rather than
    # picking a winner between kim_pipeline's local geper/ and the
    # top-level geper/ (the latter now also growing its own api/main.py
    # for an unrelated endpoint -- see that module for its own scope).
    # FIX #4: --config is accepted here (added to every subcommand via
    # _common()) but until now was never read -- silently ignored.
    # api/main.py is loaded by uvicorn from the bare string "api.main:app"
    # below; cmd_serve never gets a handle on the app object to configure
    # directly, so GEPER_CONFIG_PATH (api/main.py's own env var for this,
    # read at its own import time) is the only channel available to pass
    # --config through. Only set when --config was actually given, so a
    # deployer's own already-exported GEPER_CONFIG_PATH is never clobbered
    # by a bare `serve` with no --config.
    if args.config:
        os.environ["GEPER_CONFIG_PATH"] = args.config

    api_module = Path(__file__).parent / "api" / "main.py"
    if not api_module.exists():
        print(
            "ERROR: API app not found at api/main.py\n"
            "The REST API module is not yet implemented. "
            "Run 'python main.py analyze' for the CLI pipeline.",
            file=sys.stderr,
        )
        return 1

    print(f"Starting Bij AI API server on {args.host}:{args.port} …")
    uvicorn.run(
        "api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level.lower(),
    )
    return 0


# ─── Subcommand: validate ────────────────────────────────────────────────────


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate a GEPER config file without running anything."""
    _setup_logging(args.log_level)
    cfg = _load_config(args.config)

    from pipeline.config_validator import validate_config, ConfigValidationError

    try:
        validate_config(cfg)
        print(f"✓ Config is valid: {args.config or 'config/default.yaml'}")
        return 0
    except ConfigValidationError as exc:
        print(f"✗ Config validation failed:\n{exc}", file=sys.stderr)
        return 2


def cmd_verify_environment(args: argparse.Namespace) -> int:
    """Run the full environment/dependency verification and print a report.

    Checks Python version, RAM, CPU, CUDA/GPU, disk space, and every
    required external tool (bwa, minimap2, samtools, freebayes, bcftools,
    plus optional VEP). Returns 0 for PASS/WARNING, 1 for FAIL.
    """
    _setup_logging(args.log_level)
    from verify_environment import run_environment_checks

    report = run_environment_checks()
    if getattr(args, "json", False):
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(report.render())

    return 1 if report.overall_status == "FAIL" else 0


# ─── Subcommand: test ────────────────────────────────────────────────────────


def cmd_test(args: argparse.Namespace) -> int:
    """Run the GEPER test suite via pytest."""
    _setup_logging(args.log_level)
    try:
        import pytest
    except ImportError:
        print("ERROR: pytest not installed. Run: pip install pytest", file=sys.stderr)
        return 1

    pytest_args = ["tests/", "-v"]
    if args.k:
        pytest_args += ["-k", args.k]
    if args.cov:
        pytest_args += ["--cov=pipeline", "--cov-report=term-missing"]

    print(f"Running: pytest {' '.join(pytest_args)}\n")
    return pytest.main(pytest_args)


# ─── Argument parsing ─────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="geper",
        description="Bij AI v8 — Genomic Evidence Pipeline with Evidence-based Risk assessment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version="Bij AI v8.0.0")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    # ── shared flags ─────────────────────────────────────────────────────────
    def _common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--config", metavar="PATH", help="Path to YAML config (default: config/default.yaml)"
        )
        p.add_argument(
            "--log-level",
            default="INFO",
            choices=["DEBUG", "INFO", "WARNING", "ERROR"],
            metavar="LEVEL",
            help="Logging level (default: INFO)",
        )

    # ── analyze ──────────────────────────────────────────────────────────────
    p_analyze = sub.add_parser("analyze", help="Full FASTQ → Report pipeline")
    _common(p_analyze)
    p_analyze.add_argument("--r1", required=True, metavar="FASTQ", help="Read 1 FASTQ (required)")
    p_analyze.add_argument("--r2", metavar="FASTQ", help="Read 2 FASTQ (paired-end)")
    p_analyze.add_argument("--ref", required=True, metavar="FASTA", help="Reference genome FASTA")
    p_analyze.add_argument(
        "--output-dir", metavar="DIR", help="Output directory (or set GEPER_OUTPUT_DIR)"
    )
    p_analyze.add_argument(
        "--sample-id", metavar="ID", help="Sample identifier (default: R1 filename stem)"
    )
    p_analyze.add_argument(
        "--no-resume", action="store_true", help="Ignore checkpoint and re-run all stages"
    )
    p_analyze.add_argument(
        "--bwa-index-dir",
        metavar="DIR",
        help=(
            "Persistent directory for the BWA FM-index (e.g. a Google Drive "
            "mount path). Builds the index once and never rebuilds it on "
            "later runs, even if the local disk is wiped between sessions "
            "(e.g. Colab). Overrides config alignment.index_dir."
        ),
    )
    p_analyze.add_argument(
        "--ref-cache-dir",
        metavar="DIR",
        help=(
            "Directory to decompress a gzip-compressed --ref into (reused "
            "on later runs against an unchanged source file). Overrides "
            "config reference.local_cache_dir."
        ),
    )
    p_analyze.add_argument(
        "--cadd", type=float, metavar="PHRED", help="Override ACMG PP3/BP4 CADD Phred threshold"
    )
    p_analyze.add_argument(
        "--revel", type=float, metavar="0-1", help="Override ACMG PP3/BP4 REVEL score threshold"
    )
    p_analyze.add_argument(
        "--spliceai",
        type=float,
        metavar="0-1",
        help="Override ACMG PP3/BP4 SpliceAI score threshold",
    )
    p_analyze.add_argument(
        "--mode",
        default="full",
        choices=["full", "vcf_only"],
        metavar="MODE",
        help=(
            "'full' (default): FASTQ -> Report using Kim's own annotation/"
            "ACMG/ancestry/reporting stages. 'vcf_only': stop after "
            "Variant Calling and emit filtered_variants.vcf only — use this "
            "when Kim is the FASTQ-to-VCF engine in front of another "
            "interpretation pipeline (e.g. Bij AI)."
        ),
    )
    p_analyze.add_argument(
        "--stop-after",
        default=None,
        choices=["variant_calling"],
        metavar="STAGE",
        help="Explicit stage name to stop after (equivalent to --mode vcf_only).",
    )
    p_analyze.set_defaults(func=cmd_analyze)

    # ── vcf ──────────────────────────────────────────────────────────────────
    p_vcf = sub.add_parser("vcf", help="Annotate + classify an existing VCF")
    _common(p_vcf)
    p_vcf.add_argument("--input", required=True, metavar="VCF", help="Input VCF file")
    p_vcf.add_argument("--output-dir", required=True, metavar="DIR", help="Output directory")
    p_vcf.add_argument(
        "--ref", metavar="FASTA", help="Reference genome FASTA (optional for annotation)"
    )
    p_vcf.add_argument("--sample-id", metavar="ID", help="Sample ID (default: VCF filename stem)")
    p_vcf.set_defaults(func=cmd_vcf)

    # ── classify ─────────────────────────────────────────────────────────────
    p_clf = sub.add_parser("classify", help="Classify a single variant (ACMG + evidence score)")
    _common(p_clf)
    p_clf.add_argument("--chrom", required=True, help="Chromosome (e.g. chr17)")
    p_clf.add_argument("--pos", required=True, type=int, help="Position (1-based)")
    p_clf.add_argument("--ref", required=True, help="Reference allele")
    p_clf.add_argument("--alt", required=True, help="Alternate allele")
    p_clf.add_argument("--gene", metavar="SYMBOL", help="Gene symbol (e.g. BRCA1)")
    p_clf.add_argument("--cadd", type=float, metavar="PHRED", help="CADD Phred score")
    p_clf.add_argument("--revel", type=float, metavar="0-1", help="REVEL score [0-1]")
    p_clf.add_argument("--spliceai", type=float, metavar="0-1", help="SpliceAI delta score [0-1]")
    p_clf.add_argument("--gnomad-af", type=float, metavar="AF", help="gnomAD allele frequency")
    p_clf.add_argument("--lof", action="store_true", help="Flag as loss-of-function variant")
    p_clf.add_argument(
        "--lof-intolerant", action="store_true", help="Gene is LoF intolerant (pLI>0.9)"
    )
    p_clf.add_argument("--missense", action="store_true", help="Flag as missense variant")
    p_clf.add_argument("--output-dir", metavar="DIR", help="Save JSON result here (optional)")
    p_clf.set_defaults(func=cmd_classify)

    # ── serve ─────────────────────────────────────────────────────────────────
    p_serve = sub.add_parser("serve", help="Start the Bij AI FastAPI REST server")
    _common(p_serve)
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    p_serve.add_argument("--port", default=8000, type=int, help="Bind port (default: 8000)")
    p_serve.add_argument("--reload", action="store_true", help="Enable auto-reload (dev mode)")
    p_serve.set_defaults(func=cmd_serve)

    # ── validate ─────────────────────────────────────────────────────────────
    p_val = sub.add_parser("validate", help="Validate a config file")
    _common(p_val)
    p_val.set_defaults(func=cmd_validate)

    # ── verify-environment ──────────────────────────────────────────────────
    p_env = sub.add_parser(
        "verify-environment",
        help="Check Python, RAM, CPU, CUDA/GPU, disk space, and required external tools",
    )
    p_env.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        metavar="LEVEL",
        help="Logging level (default: INFO)",
    )
    p_env.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    p_env.set_defaults(func=cmd_verify_environment)

    # ── test ─────────────────────────────────────────────────────────────────
    p_test = sub.add_parser("test", help="Run the Bij AI test suite")
    _common(p_test)
    p_test.add_argument("-k", metavar="EXPR", help="pytest -k filter expression")
    p_test.add_argument("--cov", action="store_true", help="Run with coverage report")
    p_test.set_defaults(func=cmd_test)

    return parser


# ─── Entry point ──────────────────────────────────────────────────────────────


def cli() -> None:
    """Main entry point for ``geper`` console script (pyproject.toml)."""
    parser = _build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    cli()
