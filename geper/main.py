"""
GEPER command-line entry point.

Usage:
    python main.py --vcf path/to/input.vcf [--output-dir ./geper_output]
                    [--blast-mode auto|remote|local] [--blast-db /path/to/db]
                    [--blast-reference-fasta /path/to/ref.fasta]
                    [--ai-only] [--no-blast-cache]
                    [--species human] [--assembly GRCh38]

Example (Colab or local shell):
    python main.py --vcf sample.vcf
"""

import sys

# Fail fast on an unsupported interpreter, before any other import runs.
# `verify_environment.py`'s EXPECTED dict is the single source of truth for
# this range (evo2's own PyPI metadata: requires-python >=3.11,<3.13) --
# reusing its check here instead of hardcoding a second copy of the range
# keeps this in exactly one place, per that file's own documented intent.
from verify_environment import check_python

_python_check = check_python()
if _python_check.status == "FAIL":
    sys.stderr.write(f"ERROR: {_python_check.detail}\n")
    sys.exit(1)

import argparse  # noqa: E402 -- after the version guard, deliberately
import os  # noqa: E402
from datetime import datetime  # noqa: E402

from config import CONFIG  # noqa: E402
from pipeline.hpo.utils import build_phenotype_result  # noqa: E402
from pipeline.orchestrator import GeperPipeline  # noqa: E402
from utils.exceptions import PipelineError  # noqa: E402
from utils.logger import get_logger  # noqa: E402
from utils.service_health import HEALTH, default_service_checks  # noqa: E402

logger = get_logger(__name__)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bij AI - Genetic Evaluation & Prediction Engine")
    parser.add_argument("--vcf", required=True, help="Path to the input VCF file (.vcf or .vcf.gz)")
    parser.add_argument(
        "--output-dir",
        default=None,
        help=(
            "Directory to write geper_results.json, geper_report.md, and both "
            "PDFs -- geper_report_full.pdf (detailed) and geper_report_short.pdf "
            "(one-page-style clinical summary), generated from the same run "
            "(default: ./geper_output)"
        ),
    )
    parser.add_argument(
        "--blast-mode",
        choices=["auto", "remote", "local"],
        default=None,
        help=(
            "'auto' (default) prefers local BLAST+ (blastn + a database) "
            "first, falls back to NCBI-hosted remote BLAST second, and skips "
            "gracefully if neither is usable. Use 'remote' or 'local' to "
            "force one explicitly. Also configurable via GEPER_BLAST_MODE "
            f"(currently: '{CONFIG.api.BLAST_MODE}')."
        ),
    )
    parser.add_argument(
        "--blast-db",
        default=None,
        help=(
            "Path/prefix of a local BLAST database (the production default "
            "backend). Required when --blast-mode local; when --blast-mode "
            "auto, enables the local fast-path if a blastn binary is also "
            "found on PATH. Also configurable via GEPER_BLAST_DATABASE "
            "(or, for backward compatibility, GEPER_BLAST_LOCAL_DB / the "
            "standard BLASTDB environment variable)."
        ),
    )
    parser.add_argument(
        "--blast-reference-fasta",
        default=None,
        help=(
            "Path to a FASTA reference. If no local BLAST database exists "
            "yet at --blast-db/GEPER_BLAST_DATABASE, one is built there "
            "automatically with makeblastdb the first time it's needed "
            "(never rebuilt on later runs). Also configurable via "
            "GEPER_BLAST_REFERENCE_FASTA."
        ),
    )
    parser.add_argument(
        "--ai-only",
        action="store_true",
        help=(
            "Disable BLAST entirely and rely only on the DNA/RNA/protein "
            "foundation models plus ClinVar/dbSNP/AlphaMissense. Useful when "
            "remote BLAST's queue latency is unacceptable and no local BLAST+ "
            "database is available."
        ),
    )
    parser.add_argument(
        "--no-blast-cache",
        action="store_true",
        help=(
            "Disable the persistent, cross-run on-disk BLAST result cache "
            "(enabled by default). BLAST results are still deduplicated "
            "in-memory within a single run either way."
        ),
    )
    parser.add_argument(
        "--no-profiling",
        action="store_true",
        help=(
            "Disable per-stage wall-clock profiling and skip writing geper_benchmark.json/.md at the end of the run."
        ),
    )
    parser.add_argument("--species", default="human", help="Ensembl species name. Default: human.")
    parser.add_argument(
        "--assembly",
        default=None,
        help="Genome assembly / coord system version (e.g. GRCh38). Default: Ensembl's current default.",
    )
    parser.add_argument(
        "--max-variants",
        type=int,
        default=None,
        help=(
            "Stop after processing this many variants (useful for debugging "
            "against a large VCF without a full run). Example: --max-variants 20."
        ),
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help=(
            "Disable resume-from-checkpoint. By default, if geper_results.json "
            "already exists in --output-dir, Bij AI skips variants already "
            "recorded there and continues from where a previous (e.g. "
            "disconnected Colab) run left off."
        ),
    )
    parser.add_argument(
        "--patient-meta",
        default=None,
        help=(
            "Path to a JSON file with patient metadata (patient_name, dob, "
            "gender, physician) for the clinical PDF report's header "
            "(report/summary.py). See patient_metadata.example.json. "
            "Optional -- if omitted, invalid, or corrupt, the report falls "
            "back to a safe 'De-identified / Research Sample' header "
            "showing only Sample ID and Run ID (never crashes the pipeline). "
            "India DPDP Act 2023 note: this flag only feeds the PDF's "
            "header renderer -- it does not itself implement DPDP consent/"
            "retention requirements; see report/summary.py::_parse_patient_meta."
        ),
    )
    parser.add_argument(
        "--qc-metrics-json",
        default=None,
        help=(
            "Path to a JSON file describing this run's upstream sequencing/alignment QC metrics "
            "(mean_coverage_depth, bases_at_20x, q30_score), for the clinical PDF's Sequencing "
            "Quality Control Metrics table. Bij AI's own VCF-only pipeline cannot compute these "
            "itself -- this flag exists for callers like bridge/combined_pipeline.py that ran "
            "kim_pipeline's real alignment/QC stages first. Optional -- if omitted (the default "
            "for a bare `--vcf` invocation), the table renders each metric 'Not applicable' "
            "rather than a placeholder value, since Bij AI genuinely never touched any upstream "
            "FASTQ/BAM in that case. See report/summary.py::_parse_qc_metrics for the required "
            'per-metric {"status": "found"|"not_run"|"error", "value": float|null, '
            '"reason": str|null} shape; a missing/corrupt file or a bare number where that '
            "shape is expected is logged as a warning and rendered as not-applicable/failed, "
            "never fatal and never coerced into a number."
        ),
    )
    parser.add_argument(
        "--hpo-terms",
        default=None,
        help=(
            "Comma-separated patient-observed HPO phenotype term IDs, e.g. "
            "--hpo-terms 'HP:0001250,HP:0002011', used as evidence for the "
            "ACMG PP4 rule (compared against each variant's gene via the "
            "already-loaded HPO gene-to-phenotype dataset). Optional -- "
            "if omitted (the default), PP4 continues to report "
            "'not_evaluated' exactly as before. Malformed IDs (anything "
            "not matching 'HP:#######') are logged as a warning and "
            "skipped, never fatal. Combines with --phenotype-file if both "
            "are given."
        ),
    )
    parser.add_argument(
        "--phenotype-file",
        default=None,
        help=(
            "Path to a file with patient-observed HPO phenotype term IDs "
            "for the ACMG PP4 rule: either a plain text file with one "
            "'HP:#######' ID per line, or a JSON file containing a list "
            "of HPO ID strings. Alternative (or addition) to --hpo-terms "
            "for real clinical use where a clinician needs to paste in "
            "several observed phenotypes at once. A missing/unreadable/"
            "malformed file is logged as a warning, never fatal."
        ),
    )
    return parser


def main() -> int:
    # Correlates a harness "[killed]" task-termination tag against a
    # Windows Resource-Exhaustion-Detector event (see hive card
    # harness-task-governor-kills-are-uninspectable and the PID-capture
    # convention it led to) -- without this line, a killed run leaves no
    # attributable PID anywhere. Logged FIRST, before argument parsing
    # or anything else: kills have been observed as early as ~90s into a
    # run, sometimes mid model-load, so anything placed later risks
    # never being written. Uses `logger` (stdlib logging), not print --
    # logging.StreamHandler.emit() flushes unconditionally after every
    # record, which is what lets this line survive a hard kill (no
    # atexit handlers run, no buffer flush); a bare print()'s buffered
    # output would simply be discarded. Timestamp is LOCAL time,
    # deliberately, to share a clock domain with Windows Event Log's own
    # TimeCreated field, which is what the correlation is matched
    # against. PARENT_PID is included because a venv-launched python.exe
    # on this box is not always one process -- the launcher can hand off
    # to a different worker PID under the base interpreter -- so both
    # are logged rather than assuming they're the same.
    logger.info(f"PID={os.getpid()} PARENT_PID={os.getppid()} START={datetime.now().isoformat()}")

    parser = build_arg_parser()
    args = parser.parse_args()

    if args.blast_mode == "local" and not (args.blast_db or CONFIG.api.BLAST_LOCAL_DB_PATH):
        parser.error("--blast-db is required when --blast-mode is 'local' (or set GEPER_BLAST_DATABASE).")
    if args.max_variants is not None and args.max_variants <= 0:
        parser.error("--max-variants must be a positive integer.")

    phenotype_result = build_phenotype_result(args.hpo_terms, args.phenotype_file, logger=logger)

    try:
        # Round 19: GeperPipeline's own __init__ now validates
        # --output-dir is genuinely writable (see
        # utils/output_paths.py::ensure_writable_output_dir) and raises
        # PipelineError immediately if not -- construction is inside
        # this try block, not just pipeline.run() below, so that
        # startup-time failure gets the same clean one-line message as
        # every other PipelineError instead of a raw traceback.
        pipeline = GeperPipeline(
            blast_mode=args.blast_mode,
            blast_local_db=args.blast_db,
            blast_reference_fasta=args.blast_reference_fasta,
            species=args.species,
            assembly=args.assembly,
            output_dir=args.output_dir,
            ai_only=args.ai_only or None,
            enable_profiling=(False if args.no_profiling else None),
            blast_disk_cache=(False if args.no_blast_cache else None),
            patient_meta_path=args.patient_meta,
            qc_metrics_path=args.qc_metrics_json,
            phenotype_result=phenotype_result,
        )

        HEALTH.run_startup_checks(default_service_checks())

        pipeline.run(args.vcf, max_variants=args.max_variants, resume=not args.no_resume)
    except PipelineError as exc:
        logger.error(f"Bij AI pipeline aborted: {exc}")
        return 1
    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
        return 130
    finally:
        # Observation only -- re-probes latched services to record whether
        # each latch was still true at run end, and changes nothing. This
        # is the evidence the bounded-reprobe card needs and that no log
        # can otherwise contain (see service_health.measure_latched_services).
        HEALTH.measure_latched_services()
        HEALTH.print_summary()

    return 0


if __name__ == "__main__":
    sys.exit(main())
