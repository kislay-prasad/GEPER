"""
Card DEFECT-two-component-trees-each-self-identify-as-the-WHOLE-product-
under-a-ratified-combined-name -- RULED Option B, 2026-09-11.

The human's words: "...the Bij AI sequencing-analysis component (Kim)";
and for the version string: "scope it -- 'Bij AI sequencing-analysis
component v8' ... the version field must name the same thing the scope
line does."

Before this, kim's report said it was "produced by the Kim pipeline" --
the same sentence typed out twice, once in the HTML template and once in
the ReportLab PDF -- and PIPELINE_VERSION ("Bij AI v8") put the whole
product's name on every JSON, HTML and PDF report this tree emits alone.

Pinned VERBATIM (ratified clinician-facing text). Checked on RENDERED
output for the HTML and PDF, and on the SOURCE of the shipped pipeline
package for "one copy, and no old phrase survives".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pipeline as _pipeline_pkg
from pipeline.reporting import clinical_sections
from pipeline.reporting.stage import PIPELINE_VERSION

NEW_SCOPE_LINE = (
    "Sequencing analysis from FASTQ — produced by the Bij AI sequencing-analysis "
    "component (Kim): QC, alignment and variant calling, with ACMG classification "
    "of the variants called here."
)
NEW_PIPELINE_VERSION = "Bij AI sequencing-analysis component v8"
OLD_PHRASES = ("Kim pipeline", "Bij AI v8")


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _scope_constant():
    # Looked up at call time, so a missing constant is a test FAILURE with
    # a message rather than a collection error that hides the other tests.
    return getattr(clinical_sections, "SCOPE_LINE", None)


def _run_stage(tmp_path):
    # Same fixture ReportingStage's own suite drives (tests/test_reporting_stage.py).
    from tests.test_reporting_stage import TestIssue3ClinicalReportOverhaul

    return TestIssue3ClinicalReportOverhaul()._run(tmp_path)


class TestConstantsAreTheRatifiedStrings:
    def test_scope_line_is_one_constant_and_verbatim(self):
        assert _scope_constant() == NEW_SCOPE_LINE

    def test_pipeline_version_is_scoped(self):
        assert PIPELINE_VERSION == NEW_PIPELINE_VERSION


class TestEverySurfaceRendersTheNewName:
    def test_html_report_renders_scope_line_and_version(self, tmp_path):
        result = _run_stage(tmp_path)
        html = Path(result.html_path).read_text()
        assert NEW_SCOPE_LINE in html
        assert f"{NEW_PIPELINE_VERSION} — Clinical Genomic Report" in html
        for old in OLD_PHRASES:
            assert old not in html

    def test_json_report_carries_scoped_version(self, tmp_path):
        result = _run_stage(tmp_path)
        payload = json.loads(Path(result.json_path).read_text())
        assert payload["pipeline"] == NEW_PIPELINE_VERSION

    def test_pdf_report_renders_scope_line_and_version(self, tmp_path):
        from pypdf import PdfReader

        from pipeline.reporting.pdf_report import render_clinical_pdf
        from tests.test_pdf_report import _minimal_kwargs

        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", **_minimal_kwargs())
        reader = PdfReader(out)
        text = "\n".join(p.extract_text() for p in reader.pages)
        flat = _collapse(text)
        assert NEW_SCOPE_LINE in flat
        assert f"{NEW_PIPELINE_VERSION} — Clinical Genomic Report" in flat
        assert f"Pipeline: {NEW_PIPELINE_VERSION}" in flat
        for old in OLD_PHRASES:
            assert old not in text


class TestSourceOfTheShippedPipeline:
    """The whole shipped `pipeline` package, not just the branches the
    fixtures above reach."""

    def _sources(self):
        root = Path(_pipeline_pkg.__file__).resolve().parent
        sources = sorted(root.rglob("*.py"))
        assert sources, f"no pipeline sources under {root}"
        return sources

    def test_no_old_phrase_survives(self):
        hits = []
        for path in self._sources():
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for old in OLD_PHRASES:
                    if old in line:
                        hits.append(f"{path.name}:{lineno}: {line.strip()}")
        assert hits == [], "superseded component name still in shipped pipeline code"

    def test_scope_sentence_is_written_once(self):
        # The fold: HTML and PDF previously each carried their own copy.
        marker = "Sequencing analysis from FASTQ"
        hits = [
            p.name
            for p in self._sources()
            for line in p.read_text(encoding="utf-8").splitlines()
            if marker in line
        ]
        assert hits == ["clinical_sections.py"], hits
