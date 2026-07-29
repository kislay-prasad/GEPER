"""
geper/pipeline/fastq/pipeline.py
──────────────────────────────────
Compatibility shim for the full-repository import path.

All existing stage files in ``pipeline/alignment/`` and
``pipeline/variant_calling/`` import from:
    ``geper.pipeline.fastq.pipeline``

This shim re-exports the canonical implementations from
``pipeline.fastq.errors`` so those imports resolve without any
modification to the existing stage files.
"""

from pipeline.fastq.errors import (  # noqa: F401
    FastqPipelineError,
    _require,
    _run,
    _RunResult,
)
