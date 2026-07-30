#!/usr/bin/env python3
"""
bridge/run_combined.py
────────────────────────
CLI entry point for the complete combined workflow:

    FASTQ
      -> Kim Pipeline (QC -> Alignment -> Variant Calling)
      -> filtered_variants.vcf
      -> Current GEPER (Annotation -> AI Models -> Databases ->
                         Interpretation Engine -> Clinical Report)

Usage:
    python bridge/run_combined.py \\
        --r1 sample_R1.fastq.gz --r2 sample_R2.fastq.gz \\
        --ref GRCh38.fasta \\
        --sample-id sample01 \\
        --kim-output-dir ./work/kim \\
        --geper-output-dir ./work/geper

This script does not replace either project's own CLI:
  * Kim standalone:   python kim_pipeline/main.py analyze --r1 ... --ref ...
  * GEPER standalone: python geper/main.py --vcf existing.vcf
Both continue to work exactly as before. This script only adds the
combined path.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.combined_pipeline import (
    BridgeError,
    DEFAULT_GEPER_ROOT,
    DEFAULT_KIM_ROOT,
    run_combined,
)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Combined FASTQ -> Clinical Report workflow (Kim + GEPER bridge)"
    )
    p.add_argument("--r1", required=True, metavar="FASTQ", help="R1 FASTQ path (plain or .gz)")
    p.add_argument("--r2", default=None, metavar="FASTQ", help="R2 FASTQ path (paired-end)")
    p.add_argument("--ref", required=True, metavar="FASTA", help="Reference genome FASTA path")
    p.add_argument("--sample-id", required=True, metavar="ID", help="Sample identifier")
    p.add_argument("--kim-output-dir", required=True, metavar="DIR",
                   help="Output directory for Kim's FASTQ->VCF stage")
    p.add_argument("--geper-output-dir", required=True, metavar="DIR",
                   help="Output directory for GEPER's VCF->Report stage")
    p.add_argument("--kim-root", default=str(DEFAULT_KIM_ROOT), metavar="DIR",
                   help="Path to the Kim pipeline project root")
    p.add_argument("--geper-root", default=str(DEFAULT_GEPER_ROOT), metavar="DIR",
                   help="Path to the current GEPER project root")
    p.add_argument("--kim-python", default=None, metavar="PATH",
                   help="Python interpreter to run Kim with (default: current interpreter)")
    p.add_argument("--geper-python", default=None, metavar="PATH",
                   help="Python interpreter to run GEPER with (default: current interpreter)")
    p.add_argument("--kim-config", default=None, metavar="YAML", help="Kim config YAML override")
    p.add_argument("--no-resume", action="store_true", help="Disable checkpoint/resume in both stages")

    # GEPER passthrough options
    p.add_argument("--blast-mode", choices=["auto", "remote", "local"], default=None)
    p.add_argument("--blast-db", default=None)
    p.add_argument("--blast-reference-fasta", default=None)
    p.add_argument("--ai-only", action="store_true")
    p.add_argument("--species", default=None)
    p.add_argument("--assembly", default=None)
    p.add_argument("--max-variants", type=int, default=None)
    p.add_argument(
        "--hpo-terms",
        default=None,
        help="Comma-separated patient-observed HPO term IDs for GEPER's PP4 rule, e.g. 'HP:0001250,HP:0002011'.",
    )
    p.add_argument(
        "--phenotype-file",
        default=None,
        help="Path to a text (one HPO ID per line) or JSON (list of HPO ID strings) file of patient-observed HPO terms for GEPER's PP4 rule.",
    )
    return p


def main() -> int:
    args = build_arg_parser().parse_args()

    try:
        result = run_combined(
            fastq_r1=args.r1,
            fastq_r2=args.r2,
            reference_fasta=args.ref,
            sample_id=args.sample_id,
            kim_output_dir=args.kim_output_dir,
            geper_output_dir=args.geper_output_dir,
            kim_root=Path(args.kim_root),
            geper_root=Path(args.geper_root),
            kim_python=args.kim_python,
            geper_python=args.geper_python,
            kim_config=args.kim_config,
            no_resume=args.no_resume,
            blast_mode=args.blast_mode,
            blast_db=args.blast_db,
            blast_reference_fasta=args.blast_reference_fasta,
            ai_only=args.ai_only,
            species=args.species,
            assembly=args.assembly,
            max_variants=args.max_variants,
            hpo_terms=args.hpo_terms,
            phenotype_file=args.phenotype_file,
        )
    except BridgeError as exc:
        print(f"\nCombined pipeline FAILED: {exc}", file=sys.stderr)
        return 1

    print("\n✓ Combined FASTQ -> Clinical Report workflow complete")
    print(f"  Sample ID           : {result.sample_id}")
    print(f"  Filtered VCF (Kim)  : {result.filtered_vcf_path}")
    print(f"  GEPER results JSON  : {result.geper_results_json}")
    print(f"  GEPER report (MD)   : {result.geper_report_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
