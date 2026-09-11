"""
Card REG-the-ANCESTRY-INFERENCE-REPORT-carries-the-same-superseded-research-
use-only-claim -- ratified by the human 2026-09-11, pinned VERBATIM.

The ancestry report's HTML said "For research use only." -- the superseded
whole-product positioning. It is replaced by a statement written for an
artefact that infers ancestry and classifies nothing.

Checked on the RENDERED HTML from a real AncestryStage.run(), not the source:
the claim is about what the report says.

(A separate file rather than a class in tests/test_ancestry_stage.py: that
file predates the repo's ruff hook and carries lint debt the hook will not
auto-fix, so touching it would drag an unrelated reformat into this change.)
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.ancestry.stage import AncestryStage

RATIFIED_STATEMENT = (
    "This inference does not classify or interpret any variant, does not contribute to or "
    "feed any variant classification produced elsewhere in this pipeline, and does not by "
    "itself constitute clinical advice; always validate results with certified diagnostic "
    "tools and qualified clinical professionals."
)


def _rendered_html() -> str:
    with tempfile.TemporaryDirectory() as tmpdir:
        vcf_path = os.path.join(tmpdir, "test.vcf")
        with open(vcf_path, "w") as fh:
            fh.write("##fileformat=VCFv4.1\n")
            fh.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
        result = AncestryStage(cfg={}).run(
            vcf_path=vcf_path, output_dir=os.path.join(tmpdir, "out")
        )
        return Path(result.report_html_path).read_text(encoding="utf-8")


def test_ratified_statement_renders_verbatim():
    flat = " ".join(_rendered_html().split())
    assert RATIFIED_STATEMENT in flat


def test_research_use_only_line_is_gone():
    assert "research use only" not in _rendered_html().lower()
