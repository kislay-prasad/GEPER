"""The genome build is shown on EVERY surface a GEPER report is rendered to.

Ruled 2026-09-11 (card RESIDUAL-genome-build-consistency-warns-but-never-blocks,
option a): "Block on mismatch AND on an undetectable header, and put the build on
every rendered surface." Earlier wording of the same ruling: "the markdown omitting
what the PDF prints is the same defect as the ISO conjunction, one surface
disclosing and another not".

THE DENOMINATOR -- six surfaces, enumerated from what is actually written, not from
the report/ file list:
  written by `PipelineOrchestrator.run()` (pipeline/orchestrator.py):
    1. geper_results.json        report/json_builder.py::JSONResultBuilder
    2. geper_report.md           report/report_generator.py::ReportGenerator
    3. geper_report_full.pdf     report/summary.py::generate_pdf
    4. geper_report_short.pdf    report/summary_short.py::generate_short_pdf
  written by the LIMS export CLI (report/export_lims.py __main__):
    5. LIMS JSON                 export_lims_json
    6. LIMS CSV                  export_lims_csv
Excluded, named: geper_benchmark.json/.md (a performance profile, not a report);
the API's GET /interpretations/{id} (status fields only -- id, status,
interpretation_id, error_message -- no report content).

Every surface is rendered from ONE document built by the real JSONResultBuilder, so
surface 1 is the production path rather than a hand-written dict. Each assertion is
paired with its negative: the same surface rendered with NO build must not contain
the sentinel, so a pass cannot come from static text that happens to mention a build.
"""

import csv
import json
import os
import tempfile
import unittest

from pypdf import PdfReader

from report.export_lims import export_lims_csv, export_lims_json
from report.json_builder import JSONResultBuilder
from report.report_generator import ReportGenerator
from report.summary import generate_pdf
from report.summary_short import generate_short_pdf
from tests.test_disclaimer_consistency import _document as _renderable_document

# GRCh37 rather than GRCh38: GRCh38 is the default a hard-coded or assumed build
# would produce, so it could pass without the field being read at all.
_SENTINEL = "GRCh37"


def _document(assembly):
    builder = JSONResultBuilder(input_vcf_path="build_surface_test.vcf", assembly=assembly, vcf_samples=["SAMPLE01"])
    document = builder.build()
    # One finding, so the per-row LIMS CSV check below cannot pass over zero
    # rows. Taken from test_disclaimer_consistency's fixture -- the one the ISO
    # every-surface test also renders -- because its variants carry a
    # candidate_interpretation every renderer accepts. Only the variants are
    # borrowed; the run-level fields, including the build, come from the real
    # builder above.
    document["variants"] = _renderable_document(1)["variants"]
    document["variant_count"] = 1
    # The LIMS exporters refuse an unreviewed run (report/export_lims.py::
    # _require_reviewed). That gate has its own tests; this file is about
    # what each surface SHOWS, so -- as test_export_lims does -- the fixture
    # represents an already-reviewed run.
    document["review_status"] = "reviewed"
    return document


def _pdf_text(path):
    return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)


def _render(surface, document, tmp):
    if surface == "results_json":
        return json.dumps(document)
    if surface == "markdown":
        return ReportGenerator().generate(document)
    if surface == "full_pdf":
        path = os.path.join(tmp, "full.pdf")
        generate_pdf(document, path)
        return _pdf_text(path)
    if surface == "short_pdf":
        path = os.path.join(tmp, "short.pdf")
        generate_short_pdf(document, path)
        return _pdf_text(path)
    if surface == "lims_json":
        path = os.path.join(tmp, "lims.json")
        export_lims_json(document, path, run_id="RUN01")
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    if surface == "lims_csv":
        path = os.path.join(tmp, "lims.csv")
        export_lims_csv(document, path, run_id="RUN01")
        with open(path, encoding="utf-8", newline="") as fh:
            return fh.read()
    raise AssertionError(f"unknown surface {surface!r}")


SURFACES = ("results_json", "markdown", "full_pdf", "short_pdf", "lims_json", "lims_csv")


class TheDenominatorIsSix(unittest.TestCase):
    def test_six_surfaces_are_covered(self):
        self.assertEqual(len(SURFACES), 6)


class EverySurfaceShowsTheBuild(unittest.TestCase):
    def test_build_is_shown_on_every_surface(self):
        document = _document(_SENTINEL)
        self.assertEqual(document.get("assembly"), _SENTINEL, "precondition: the builder carries the build")
        # No subTest here on purpose: subTest swallows an exception, so a
        # surface that CRASHED would never reach `missing` and the assertion
        # below would pass over it. A crash must fail this test.
        missing = []
        with tempfile.TemporaryDirectory() as tmp:
            for surface in SURFACES:
                if _SENTINEL not in _render(surface, document, tmp):
                    missing.append(surface)
        self.assertEqual(missing, [], f"build {_SENTINEL!r} not shown on: {missing}")

    def test_negative_no_surface_shows_the_sentinel_when_no_build_is_given(self):
        document = _document(None)
        leaking = []
        with tempfile.TemporaryDirectory() as tmp:
            for surface in SURFACES:
                if _SENTINEL in _render(surface, document, tmp):
                    leaking.append(surface)
        self.assertEqual(
            leaking, [], f"{_SENTINEL!r} appears without being given -- the positive test proves nothing for: {leaking}"
        )


class TheLimsCsvCarriesTheBuildInItsOwnColumn(unittest.TestCase):
    def test_every_row_has_reference_assembly(self):
        from report.export_lims import _CSV_COLUMNS

        self.assertIn("reference_assembly", _CSV_COLUMNS)
        with tempfile.TemporaryDirectory() as tmp:
            text = _render("lims_csv", _document(_SENTINEL), tmp)
        rows = list(csv.DictReader(text.splitlines()))
        self.assertEqual(len(rows), 1, "one finding in, one row out -- else the loop below checks nothing")
        for row in rows:
            self.assertEqual(row["reference_assembly"], _SENTINEL)


if __name__ == "__main__":
    unittest.main()
