#!/usr/bin/env python3
"""
run_pipeline.py
────────────────
Top-level CLI entry point for the GEPER genomic pipeline.

Delegates to ``pipeline.orchestration.runner.main()`` so all flag
definitions, config loading, and error handling live in one place.

Usage::

    python run_pipeline.py \\
        --r1 sample_R1.fastq.gz \\
        --r2 sample_R2.fastq.gz \\
        --ref GRCh38.fasta \\
        --output-dir ./work \\
        --sample-id sample01 \\
        --config config/production.yaml

Run ``python run_pipeline.py --help`` for the full flag list.
"""

from pipeline.orchestration.runner import main

if __name__ == "__main__":
    main()
