#!/usr/bin/env python3
"""
Standalone pre-flight validator for raw sequencing input files
(FASTQ/BAM/CRAM), gating BEFORE any expensive downstream processing
(alignment, variant calling) starts.

Why this is a standalone script rather than a `main.py` flag: GEPER's
implemented pipeline (`main.py --vcf`) does not itself ingest FASTQ/
BAM/CRAM or run alignment/variant calling -- it consumes an
already-called VCF only. See `pipeline/raw_input_validator.py`'s
docstring for the full architecture note. This script is meant to run
ahead of whichever external aligner/caller a lab actually uses (or
ahead of a future GEPER intake stage), as its own pre-flight gate.

Usage:
    python validate_input.py --file sample.fastq.gz
    python validate_input.py --file sample.bam --index sample.bam.bai
    python validate_input.py --file sample.cram --format cram
    python validate_input.py --file sample.fastq --json

Exit code 0 = valid (safe to proceed), 1 = invalid (do not proceed).
"""

from __future__ import annotations

import argparse
import json
import sys

from pipeline.raw_input_validator import validate_raw_input
from utils.logger import get_logger

logger = get_logger(__name__)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GEPER pre-flight validator for FASTQ/BAM/CRAM input files.")
    parser.add_argument("--file", required=True, help="Path to the FASTQ, BAM, or CRAM file to validate.")
    parser.add_argument(
        "--format",
        choices=["fastq", "bam", "cram"],
        default=None,
        help="Expected format. If omitted, inferred from the file's extension -- either way, the actual "
        "check is content-based (magic bytes), not extension-trusting, so a mismatch is still caught.",
    )
    parser.add_argument(
        "--index",
        default=None,
        help="Path to the BAM/CRAM index file (.bai/.crai), if not the default '<file>.bai'/'<file>.crai' sibling.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output instead of a human-readable report."
    )
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    result = validate_raw_input(args.file, expected_format=args.format, index_path=args.index)

    if args.json:
        print(
            json.dumps(
                {
                    "path": result.path,
                    "detected_format": result.detected_format,
                    "is_valid": result.is_valid,
                    "errors": result.errors,
                    "warnings": result.warnings,
                },
                indent=2,
            )
        )
    else:
        print(result.summary_line())
        for warning in result.warnings:
            print(f"  WARNING: {warning}")
        for error in result.errors:
            print(f"  ERROR: {error}")

    return 0 if result.is_valid else 1


if __name__ == "__main__":
    sys.exit(main())
