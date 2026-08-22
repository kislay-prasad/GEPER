"""
Tests for `compare_reports.py` -- the two-run report-comparison CLI.

Exercises `diff_reports()` (the pure diff-computation function) directly
against small, hand-built `geper_results.json`-shaped dicts, plus one
filesystem round-trip test for `load_document()`/`main()` to confirm the
CLI itself wires everything together correctly. No real pipeline run, no
model weights -- lightweight, safe to run locally.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compare_reports import diff_reports, load_document, render_markdown  # noqa: E402


def _variant(chrom, pos, ref, alt, classification=None, confidence_label=None, evidence_sources=None, pending=False):
    return {
        "variant": {"chrom": chrom, "pos": pos, "ref": ref, "alt": alt},
        "candidate_interpretation": {
            "acmg_classification": {"classification": classification},
            "confidence": {"pending": pending, "label": confidence_label, "score": 80},
            "evidence_sources": evidence_sources or [],
        }
        if classification is not None or evidence_sources is not None
        else None,
    }


def _document(variants, provenance=None, code_version="v1"):
    return {
        "geper_version": "1.0.0",
        "generated_at": "2026-08-01T00:00:00+00:00",
        "input_vcf": "test.vcf",
        "code_version": code_version,
        "provenance": provenance or [],
        "variants": variants,
    }


class TestDiffReportsIdentity(unittest.TestCase):
    def test_identical_runs_produce_no_diffs(self):
        variants = [_variant("17", 100, "A", "T", "Benign", "High", ["ClinVar"])]
        doc = _document(variants)
        diff = diff_reports(doc, doc)
        self.assertEqual(diff["classification_changes"], [])
        self.assertEqual(diff["unexplained_changes"], [])
        self.assertEqual(diff["evidence_changes"], [])
        self.assertEqual(diff["provenance_diffs"], [])
        self.assertEqual(diff["added_variants"], [])
        self.assertEqual(diff["removed_variants"], [])
        self.assertEqual(diff["common_variant_count"], 1)


class TestClassificationChanges(unittest.TestCase):
    def test_detects_a_classification_change(self):
        doc_a = _document([_variant("17", 100, "A", "T", "Uncertain significance", "Moderate", ["ClinVar"])])
        doc_b = _document([_variant("17", 100, "A", "T", "Pathogenic", "High", ["ClinVar"])])
        diff = diff_reports(doc_a, doc_b)
        self.assertEqual(len(diff["classification_changes"]), 1)
        change = diff["classification_changes"][0]
        self.assertEqual(change["classification_a"], "Uncertain significance")
        self.assertEqual(change["classification_b"], "Pathogenic")
        self.assertEqual(change["confidence_a"], "Moderate")
        self.assertEqual(change["confidence_b"], "High")

    def test_no_classification_key_at_all_is_not_a_change(self):
        variants = [_variant("17", 100, "A", "T", None, None, [])]
        doc = _document(variants)
        diff = diff_reports(doc, doc)
        self.assertEqual(diff["classification_changes"], [])


class TestUnexplainedChanges(unittest.TestCase):
    def test_change_with_no_provenance_diff_and_same_code_version_is_unexplained(self):
        prov = [{"source": "ClinVar", "status": "unknown", "version": None, "release_date": None, "content_hash": None}]
        doc_a = _document(
            [_variant("17", 100, "A", "T", "Likely benign", "Moderate", ["ClinVar"])],
            provenance=prov,
            code_version="same",
        )
        doc_b = _document(
            [_variant("17", 100, "A", "T", "Pathogenic", "High", ["ClinVar"])],
            provenance=prov,
            code_version="same",
        )
        diff = diff_reports(doc_a, doc_b)
        self.assertEqual(len(diff["unexplained_changes"]), 1)
        self.assertEqual(diff["unexplained_changes"][0]["cited_sources"], ["ClinVar"])

    def test_change_explained_by_cited_source_provenance_diff_is_not_flagged(self):
        doc_a = _document(
            [_variant("17", 100, "A", "T", "Likely benign", "Moderate", ["ClinVar"])],
            provenance=[{"source": "ClinVar", "status": "hash_only", "content_hash": "AAA"}],
            code_version="same",
        )
        doc_b = _document(
            [_variant("17", 100, "A", "T", "Pathogenic", "High", ["ClinVar"])],
            provenance=[{"source": "ClinVar", "status": "hash_only", "content_hash": "BBB"}],
            code_version="same",
        )
        diff = diff_reports(doc_a, doc_b)
        self.assertEqual(len(diff["classification_changes"]), 1)
        self.assertEqual(diff["unexplained_changes"], [])

    def test_change_explained_by_code_version_change_is_not_flagged(self):
        prov = [{"source": "ClinVar", "status": "unknown"}]
        doc_a = _document(
            [_variant("17", 100, "A", "T", "Likely benign", "Moderate", ["ClinVar"])],
            provenance=prov,
            code_version="v1",
        )
        doc_b = _document(
            [_variant("17", 100, "A", "T", "Pathogenic", "High", ["ClinVar"])],
            provenance=prov,
            code_version="v2",
        )
        diff = diff_reports(doc_a, doc_b)
        self.assertEqual(len(diff["classification_changes"]), 1)
        self.assertEqual(diff["unexplained_changes"], [])
        self.assertTrue(diff["code_version_changed"])

    def test_unrelated_source_provenance_diff_does_not_explain_the_change(self):
        # The variant only ever cites ClinVar -- a gnomAD provenance
        # diff is irrelevant to it and must not suppress the flag.
        doc_a = _document(
            [_variant("17", 100, "A", "T", "Likely benign", "Moderate", ["ClinVar"])],
            provenance=[
                {"source": "ClinVar", "status": "unknown"},
                {"source": "gnomAD", "status": "version_known", "version": "gnomad_r3"},
            ],
            code_version="same",
        )
        doc_b = _document(
            [_variant("17", 100, "A", "T", "Pathogenic", "High", ["ClinVar"])],
            provenance=[
                {"source": "ClinVar", "status": "unknown"},
                {"source": "gnomAD", "status": "version_known", "version": "gnomad_r4"},
            ],
            code_version="same",
        )
        diff = diff_reports(doc_a, doc_b)
        self.assertEqual(len(diff["unexplained_changes"]), 1)


class TestEvidenceSourceChanges(unittest.TestCase):
    def test_newly_matched_and_unmatched_sources(self):
        doc_a = _document([_variant("17", 100, "A", "T", "Benign", "High", ["ClinVar", "gnomAD"])])
        doc_b = _document([_variant("17", 100, "A", "T", "Benign", "High", ["ClinVar", "InterPro"])])
        diff = diff_reports(doc_a, doc_b)
        self.assertEqual(len(diff["evidence_changes"]), 1)
        change = diff["evidence_changes"][0]
        self.assertEqual(change["newly_matched"], ["InterPro"])
        self.assertEqual(change["newly_unmatched"], ["gnomAD"])

    def test_no_evidence_change_when_sources_identical(self):
        doc = _document([_variant("17", 100, "A", "T", "Benign", "High", ["ClinVar"])])
        diff = diff_reports(doc, doc)
        self.assertEqual(diff["evidence_changes"], [])


class TestAddedRemovedVariants(unittest.TestCase):
    def test_variant_only_in_a_is_removed(self):
        doc_a = _document([_variant("17", 100, "A", "T", "Benign", "High", ["ClinVar"])])
        doc_b = _document([])
        diff = diff_reports(doc_a, doc_b)
        self.assertEqual(diff["removed_variants"], ["17:100 A>T"])
        self.assertEqual(diff["added_variants"], [])

    def test_variant_only_in_b_is_added(self):
        doc_a = _document([])
        doc_b = _document([_variant("17", 100, "A", "T", "Benign", "High", ["ClinVar"])])
        diff = diff_reports(doc_a, doc_b)
        self.assertEqual(diff["added_variants"], ["17:100 A>T"])
        self.assertEqual(diff["removed_variants"], [])

    def test_added_removed_variants_never_appear_as_classification_changes(self):
        doc_a = _document([_variant("1", 1, "A", "T", "Benign", "High", [])])
        doc_b = _document([_variant("2", 2, "C", "G", "Pathogenic", "High", [])])
        diff = diff_reports(doc_a, doc_b)
        self.assertEqual(diff["classification_changes"], [])
        self.assertEqual(diff["common_variant_count"], 0)


class TestProvenanceDiffs(unittest.TestCase):
    def test_detects_version_difference(self):
        doc_a = _document(
            [], provenance=[{"source": "ClinGen (gene validity)", "status": "hash_only", "content_hash": "AAA"}]
        )
        doc_b = _document(
            [], provenance=[{"source": "ClinGen (gene validity)", "status": "hash_only", "content_hash": "BBB"}]
        )
        diff = diff_reports(doc_a, doc_b)
        self.assertEqual(len(diff["provenance_diffs"]), 1)
        self.assertEqual(diff["provenance_diffs"][0]["source"], "ClinGen (gene validity)")

    def test_source_present_in_only_one_run_is_a_diff(self):
        doc_a = _document([], provenance=[{"source": "ClinVar", "status": "unknown"}])
        doc_b = _document([], provenance=[])
        diff = diff_reports(doc_a, doc_b)
        self.assertEqual(len(diff["provenance_diffs"]), 1)
        self.assertIsNone(diff["provenance_diffs"][0]["b"])

    def test_identical_provenance_is_not_a_diff(self):
        prov = [{"source": "ClinVar", "status": "unknown", "version": None, "release_date": None, "content_hash": None}]
        doc_a = _document([], provenance=prov)
        doc_b = _document([], provenance=prov)
        diff = diff_reports(doc_a, doc_b)
        self.assertEqual(diff["provenance_diffs"], [])


class TestRenderMarkdown(unittest.TestCase):
    def test_render_produces_a_string_with_expected_sections(self):
        doc_a = _document([_variant("17", 100, "A", "T", "Benign", "High", ["ClinVar"])])
        doc_b = _document([_variant("17", 100, "A", "T", "Pathogenic", "High", ["ClinVar"])])
        diff = diff_reports(doc_a, doc_b)
        md = render_markdown(diff, doc_a, doc_b, "/dir/a", "/dir/b")
        self.assertIn("# GEPER Report Comparison", md)
        self.assertIn("Unexplained Classification Changes", md)
        self.assertIn("Classification Changes", md)
        self.assertIn("Added / Removed Variants", md)
        self.assertIn("Evidence Source Changes", md)
        self.assertIn("Data-Source Provenance Differences", md)
        self.assertIn("/dir/a", md)
        self.assertIn("/dir/b", md)


class TestLoadDocumentAndCli(unittest.TestCase):
    def test_load_document_missing_directory_raises_filenotfound(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty_dir = os.path.join(tmp, "no_results_here")
            os.makedirs(empty_dir)
            with self.assertRaises(FileNotFoundError):
                load_document(empty_dir)

    def test_load_document_malformed_json_raises_valueerror(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "geper_results.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not valid json")
            with self.assertRaises(ValueError):
                load_document(tmp)

    def test_load_document_round_trips_a_real_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = _document([_variant("17", 100, "A", "T", "Benign", "High", ["ClinVar"])])
            path = os.path.join(tmp, "geper_results.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            loaded = load_document(tmp)
            self.assertEqual(loaded["variants"][0]["variant"]["chrom"], "17")

    def test_cli_end_to_end_writes_output_file(self):
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "compare_reports.py")
        with tempfile.TemporaryDirectory() as tmp:
            dir_a = os.path.join(tmp, "a")
            dir_b = os.path.join(tmp, "b")
            os.makedirs(dir_a)
            os.makedirs(dir_b)
            doc_a = _document([_variant("17", 100, "A", "T", "Benign", "High", ["ClinVar"])])
            doc_b = _document([_variant("17", 100, "A", "T", "Pathogenic", "High", ["ClinVar"])])
            with open(os.path.join(dir_a, "geper_results.json"), "w", encoding="utf-8") as fh:
                json.dump(doc_a, fh)
            with open(os.path.join(dir_b, "geper_results.json"), "w", encoding="utf-8") as fh:
                json.dump(doc_b, fh)

            out_path = os.path.join(tmp, "diff.md")
            result = subprocess.run(
                [sys.executable, script, dir_a, dir_b, "--output", out_path],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue(os.path.exists(out_path))
            with open(out_path, "r", encoding="utf-8") as fh:
                content = fh.read()
            self.assertIn("Benign", content)
            self.assertIn("Pathogenic", content)

    def test_cli_missing_run_directory_exits_nonzero_with_clear_message(self):
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "compare_reports.py")
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, script, os.path.join(tmp, "does_not_exist_a"), os.path.join(tmp, "does_not_exist_b")],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("geper_results.json", result.stderr)


if __name__ == "__main__":
    unittest.main()
