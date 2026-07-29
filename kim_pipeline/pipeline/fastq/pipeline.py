"""
pipeline/fastq/pipeline.py
───────────────────────────
Compatibility shim.

The existing alignment and variant-calling stage files import
``FastqPipelineError``, ``_require``, and ``_run`` from
``geper.pipeline.fastq.pipeline``.  When running from within this
extract (where ``pipeline/`` is the top-level package, not a sub-package
of ``geper``), those symbols are re-exported from here so no existing
stage file needs to be modified.

The real implementations live in ``pipeline.fastq.errors``.
"""

from pipeline.fastq.errors import (  # noqa: F401 — re-export
    FastqPipelineError,
    _require,
    _run,
    _RunResult,
)
