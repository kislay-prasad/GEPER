"""
Tests for export_lims.py's caveats wiring -- follow-up to the
export-lims-fifth-consumer-audit finding (surfaced while testing card
run-caveats-missing-from-markdown-and-json-renderers): geper_results.json's
document-level "caveats" field (added 428a77d) reached every report
renderer but never the LIMS-facing export, so a downstream LIMS
integration built purely against `LIMSExport`/CSV output -- not raw
`geper_results.json` -- was still in the exact blind spot the original
human complaint ("a LIMS consuming JSON with no trace that a source
was unreachable") was about.

Written against OBSERVABLE OUTPUT (the built LIMSExport object and the
written CSV file's actual rows), except for the "one source, not two
copies" class, which necessarily checks object identity for the same
reason every other "one source" test this session has (see
tests/test_disclaimer_consistency.py::TestDisclaimerHasOneSourceNotFourCopies's
docstring for the full rationale).
"""

import csv
import os
import tempfile
import unittest

from report.clinical_report_builder import RESEARCH_USE_DISCLAIMER
from report.export_lims import build_lims_export, export_lims_csv, export_lims_json

_OFFLINE_CAVEAT = (
    "The following data source(s) were unreachable during this analysis run and were not queried for "
    'any variant in this report: Ensembl. Any finding reported as "not evaluated" or lacking data from '
    "these sources reflects a data-collection gap for this run, not a confirmed absence -- it should not "
    "be treated as a negative result."
)


def _document(caveats=None, n_variants: int = 1, review_status: str = "reviewed") -> dict:
    document = {
        "review_status": review_status,
        "variants": [
            {"variant": {"chrom": str(i + 1), "pos": 1000 + i, "ref": "C", "alt": "A"}} for i in range(n_variants)
        ],
        "input_vcf": "export_lims_caveats_test.vcf",
        "assembly": "GRCh38",
        "variant_count": n_variants,
        "code_version": "test",
        "generated_at": "2026-08-21T00:00:00+00:00",
        "vcf_samples": ["SAMPLE01"],
    }
    if caveats is not None:
        document["caveats"] = caveats
    return document


def _all_csv_cell_values(path: str):
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    values = []
    for row in rows:
        values.extend(row.values())
    return values


class TestCaveatsPresentInLimsExport(unittest.TestCase):
    """The regression that matters most: document["caveats"] must
    reach the built LIMSExport, on LIMSRun specifically (the dispatch's
    own stated design -- confirmed observable via the built object, not
    assumed)."""

    def test_caveats_present_on_lims_run(self):
        caveats = [RESEARCH_USE_DISCLAIMER, _OFFLINE_CAVEAT]
        export = build_lims_export(_document(caveats=caveats))
        run_caveats = getattr(export.run, "caveats", None)
        self.assertIsNotNone(run_caveats, "LIMSRun has no 'caveats' attribute -- the field never reached it")
        self.assertIn(RESEARCH_USE_DISCLAIMER, run_caveats)
        self.assertIn(_OFFLINE_CAVEAT, run_caveats)

    def test_caveats_present_via_json_export_file(self):
        # End-to-end through the actual file-writing entry point, not
        # just the in-memory build_lims_export() -- proves the field
        # actually survives model_dump_json(), not just construction.
        caveats = [RESEARCH_USE_DISCLAIMER]
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "lims_export.json")
            export_lims_json(_document(caveats=caveats), out)
            with open(out, encoding="utf-8") as fh:
                written = fh.read()
        # Parse the JSON and compare Python string to parsed document,
        # not raw serialized text. The exported JSON correctly escapes \n
        # as \\n, so substring matching against raw JSON will fail when
        # the disclaimer contains newlines. Compare the parsed referent.
        import json

        parsed = json.loads(written)
        self.assertIn(RESEARCH_USE_DISCLAIMER, parsed["run"]["caveats"])


class TestCaveatsCarryTheRightContent(unittest.TestCase):
    """Not just present -- specifically the canonical research-use
    disclaimer, not a placeholder or a reworded stand-in."""

    def test_research_use_disclaimer_specifically_present(self):
        export = build_lims_export(_document(caveats=[RESEARCH_USE_DISCLAIMER]))
        run_caveats = getattr(export.run, "caveats", None) or []
        self.assertIn(RESEARCH_USE_DISCLAIMER, run_caveats)
        self.assertNotIn("intended for clinical use", " ".join(run_caveats))


class TestOneSourceNotTwoCopies(unittest.TestCase):
    """LIMSRun must READ document["caveats"], not reimplement or
    re-derive its own copy. Identity (not equality) so a freshly
    retyped, coincidentally-identical local copy would still fail
    this -- the same reasoning as every prior "one source" test this
    session (A4's RESEARCH_USE_DISCLAIMER, the JSON run-level caveats
    list, the relocated Markdown helpers)."""

    def test_lims_run_caveats_is_the_same_list_content_as_document(self):
        caveats = [RESEARCH_USE_DISCLAIMER, _OFFLINE_CAVEAT]
        document = _document(caveats=caveats)
        export = build_lims_export(document)
        run_caveats = getattr(export.run, "caveats", None)
        self.assertIsNotNone(run_caveats)
        # Strings are immutable -- a straight `document.get("caveats")`
        # passthrough preserves the exact string OBJECTS, not just
        # equal text. A local reimplementation (even a byte-identical
        # one) would construct fresh string objects instead.
        for original, exported in zip(caveats, run_caveats):
            self.assertIs(
                exported,
                original,
                "LIMSRun.caveats holds a different string object than document['caveats'] -- "
                "looks like a copy, not a read-through",
            )

    def test_lims_run_caveats_changes_when_document_caveats_changes(self):
        # Behavioural counterpart to the identity check: swap in a
        # completely different caveats list and confirm the export
        # reflects it, rather than a hardcoded/cached value.
        custom = ["A completely different, test-only caveat string."]
        export = build_lims_export(_document(caveats=custom))
        run_caveats = getattr(export.run, "caveats", None) or []
        self.assertIn("A completely different, test-only caveat string.", run_caveats)
        self.assertNotIn(RESEARCH_USE_DISCLAIMER, run_caveats)


class TestCsvHandlesCaveats(unittest.TestCase):
    """CSV is strictly one row per finding (LIMS_EXPORT_MAPPING.md's
    documented contract) -- a run-level caveat list has no natural
    single cell. Tolerant to whichever column name/shape Kelly chose
    (a new named column, or omission) by searching every cell's value
    rather than assuming a column name in advance."""

    def test_csv_contains_caveat_text_somewhere(self):
        caveats = [RESEARCH_USE_DISCLAIMER]
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "lims_export.csv")
            export_lims_csv(_document(caveats=caveats, n_variants=2), out)
            values = _all_csv_cell_values(out)
        joined = " ".join(v or "" for v in values)
        self.assertIn(
            RESEARCH_USE_DISCLAIMER,
            joined,
            "No CSV cell (across any column, any row) contains the research-use disclaimer -- "
            "if CSV was deliberately scoped to omit run-level caveats, that's a design decision "
            "to confirm, not something this test assumes",
        )


if __name__ == "__main__":
    unittest.main()
