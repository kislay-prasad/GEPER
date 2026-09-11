"""
Tests for the clinician review workflow (`geper/review/signoff.py` /
`geper/review/cli.py`).

Synthetic 3-variant fixtures only (Pathogenic, VUS-with-a-conflict,
Likely Benign) -- no model weights, no real pipeline run. Each
variant's `clinical_report` is built via the real
`report/clinical_report_builder.py::build_clinical_report()` (not a
hand-rolled partial dict) so it carries every key the real report
renderers (`report/report_generator.py`/`report/summary.py`/
`report/summary_short.py`) actually expect -- a hand-rolled fixture
missing a key like `"priority"` would crash `ReportGenerator.generate()`
with a `KeyError` that has nothing to do with the signoff logic under
test (caught once already while developing this suite).

PDF assertions use real ReportLab renders (lightweight, no model
weights) + pypdf text extraction, the same technique
`tests/test_icmr_footer.py` already established.
"""

import json
import os
import tempfile
import unittest

from report.clinical_report_builder import build_clinical_report
from report.summary import _ICMR_OVERRIDDEN_FOOTER_TEXT
from review import signoff as s
from review.cli import build_arg_parser, main as cli_main
from utils.exceptions import SignoffError

try:
    from pypdf import PdfReader

    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False


# ---------------------------------------------------------------------------
# Synthetic fixture: 3 variants (Pathogenic, VUS w/ conflict, Likely Benign)
# ---------------------------------------------------------------------------

_VARIANT_SPECS = [
    ("17", 43106534, "C", "A", "BRCA1", "Pathogenic", None, "High", 92, False),
    ("2", 500, "G", "T", "MYH7", "Uncertain significance", "Major", None, None, True),
    ("1", 999, "A", "G", "TTN", "Likely Benign", None, "Moderate", 60, False),
]


def _make_interpretation_result(gene, classification, conflict_severity, confidence_label, confidence_score, pending):
    return {
        "variant": {},
        "gene_symbol": gene,
        "acmg_classification": classification,
        "triggered_rules": [],
        "not_triggered_rules": [],
        "not_evaluated_rules": [],
        "combining_rule_trace": [],
        "supporting_evidence": [f"Evidence for {gene}"],
        "conflicting_evidence": [],
        "ai_consensus": [],
        "ai_context_models": [],
        "confidence_pending": pending,
        "confidence_score": confidence_score,
        "confidence_label": confidence_label,
        "priority_pending": True,
        "priority_explanation": [],
        "conflict_list": [],
        "conflict_score": 0.0,
        "conflict_severity": conflict_severity,
        "recommendations": [],
        "evidence_sources": [],
        "ai_model_errors": [],
    }


def _make_document():
    variants = []
    for chrom, pos, ref, alt, gene, classification, severity, conf_label, conf_score, pending in _VARIANT_SPECS:
        variant_dict = {"chrom": chrom, "pos": pos, "ref": ref, "alt": alt}
        ir = _make_interpretation_result(gene, classification, severity, conf_label, conf_score, pending)
        clinical_report = build_clinical_report(ir, variant_dict=variant_dict, raw_evidence={})
        variants.append(
            {
                "variant": variant_dict,
                "interpretation_result": {"gene_symbol": gene},
                "candidate_interpretation": clinical_report,
                "interpretation": {},
                "errors": [],
            }
        )
    return {
        "geper_version": "test",
        "generated_at": "2026-08-06T00:00:00+00:00",
        "input_vcf": "signoff_test.vcf",
        "assembly": "GRCh38",
        "vcf_samples": ["SAMPLE01"],
        "variant_count": len(variants),
        # Round 30 part 2: mirrors JSONResultBuilder.build()'s own
        # defaults, so this fixture matches what a real GEPER run
        # actually writes before any review/signoff.py action.
        "review_status": "draft",
        "reviewed_by": None,
        "reviewed_at": None,
        "variants": variants,
    }


def _write_run(root: str, name: str = "run1") -> str:
    """Writes a fresh 3-variant geper_results.json into root/name and returns that directory."""
    output_dir = os.path.join(root, name)
    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, s.RESULTS_FILENAME), "w", encoding="utf-8") as fh:
        json.dump(_make_document(), fh)
    return output_dir


def _all_pdf_text(path: str) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(path).pages)


# ---------------------------------------------------------------------------
# variant_id / parse_variant_key / find_variant
# ---------------------------------------------------------------------------


class TestVariantKeyParsing(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(s.parse_variant_key("17:43106534:C>A"), ("17", 43106534, "C", "A"))
        self.assertEqual(s.variant_id({"chrom": "17", "pos": 43106534, "ref": "C", "alt": "A"}), "17:43106534:C>A")

    def test_malformed_key_raises_signoff_error(self):
        for bad in ("17-43106534-C-A", "17:43106534", "17:notanumber:C>A", "17:43106534:CA"):
            with self.assertRaises(SignoffError):
                s.parse_variant_key(bad)

    def test_find_variant_matches_regardless_of_chr_prefix(self):
        document = _make_document()
        found = s.find_variant(document, "chr17", 43106534, "C", "A")
        self.assertIsNotNone(found)
        self.assertEqual(found["interpretation_result"]["gene_symbol"], "BRCA1")

    def test_find_variant_no_match_is_none(self):
        document = _make_document()
        self.assertIsNone(s.find_variant(document, "5", 1, "A", "T"))

    def test_find_variant_requires_allele_match_not_just_position(self):
        document = _make_document()
        self.assertIsNone(s.find_variant(document, "17", 43106534, "C", "G"))  # right position, wrong alt


# ---------------------------------------------------------------------------
# approve()
# ---------------------------------------------------------------------------


class TestApprove(unittest.TestCase):
    def test_errors_clearly_when_no_results_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SignoffError) as ctx:
                s.approve(tmp, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            self.assertIn("geper_results.json", str(ctx.exception))

    @unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
    def test_approve_produces_reviewed_footer_on_both_pdfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")

            full_text = _all_pdf_text(os.path.join(output_dir, s.FULL_PDF_FILENAME))
            self.assertIn("reviewed by Dr. Rajesh Sharma", full_text)
            self.assertNotIn("DRAFT", full_text)

            short_text = _all_pdf_text(os.path.join(output_dir, s.SHORT_PDF_FILENAME))
            self.assertIn("reviewed by Dr. Rajesh Sharma", short_text)
            self.assertNotIn("DRAFT", short_text)

    def test_approve_writes_patient_meta_with_physician_only_no_patient_name(self):
        # Confirms approve relies on the physician/patient_name
        # decoupling (report/summary.py::_parse_patient_meta) rather
        # than fabricating a patient identity just to unlock sign-off.
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            with open(s._patient_meta_path(output_dir), encoding="utf-8") as fh:
                patient_meta = json.load(fh)
            self.assertIn("Dr. Rajesh Sharma", patient_meta["physician"])
            self.assertIn("MCI-12345", patient_meta["physician"])
            self.assertIn("AIIMS Delhi", patient_meta["physician"])
            self.assertNotIn("patient_name", patient_meta)

    def test_approve_preserves_existing_patient_meta_fields(self):
        # A pre-existing patient_meta file (e.g. real patient_name/dob
        # a lab already attached) must survive approve's merge, not be
        # clobbered -- only "physician" is set/updated.
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            with open(s._patient_meta_path(output_dir), "w", encoding="utf-8") as fh:
                json.dump({"patient_name": "Jane Doe", "dob": "1990-01-15"}, fh)
            s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            with open(s._patient_meta_path(output_dir), encoding="utf-8") as fh:
                patient_meta = json.load(fh)
            self.assertEqual(patient_meta["patient_name"], "Jane Doe")
            self.assertEqual(patient_meta["dob"], "1990-01-15")
            self.assertIn("Dr. Rajesh Sharma", patient_meta["physician"])

    def test_manifest_matches_regenerated_file_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            manifest = s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")

            self.assertEqual(manifest["pdf_sha256"], s._sha256_file(os.path.join(output_dir, s.FULL_PDF_FILENAME)))
            self.assertEqual(
                manifest["short_pdf_sha256"], s._sha256_file(os.path.join(output_dir, s.SHORT_PDF_FILENAME))
            )
            self.assertEqual(
                manifest["results_json_sha256"], s._sha256_file(os.path.join(output_dir, s.RESULTS_FILENAME))
            )
            manifest_on_disk = json.load(open(s._manifest_path(output_dir), encoding="utf-8"))
            self.assertEqual(manifest_on_disk, manifest)

    def test_manifest_lists_all_three_variants(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            manifest = s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            variant_ids = {v["variant_id"] for v in manifest["variants"]}
            self.assertEqual(variant_ids, {"17:43106534:C>A", "2:500:G>T", "1:999:A>G"})
            by_id = {v["variant_id"]: v for v in manifest["variants"]}
            self.assertEqual(by_id["17:43106534:C>A"]["final_classification"], "Pathogenic")
            self.assertEqual(by_id["17:43106534:C>A"]["gene"], "BRCA1")
            self.assertEqual(by_id["2:500:G>T"]["confidence"], "Pending")

    def test_approve_appends_to_audit_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            with open(s._audit_log_path(output_dir), encoding="utf-8") as fh:
                lines = [json.loads(line) for line in fh if line.strip()]
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["action"], "approved")
            self.assertEqual(lines[0]["clinician"], "Dr. Rajesh Sharma")


# ---------------------------------------------------------------------------
# approve() -- model-weight hash MISMATCH refusal (human-ruled 2026-09-10:
# "a signer approving a report without visibility into a mismatch is
# signing something they cannot vouch for; the signature becomes
# decorative"). No override/flag/env var exists for this check, by design.
# ---------------------------------------------------------------------------


def _write_run_with_model_checkpoints(root: str, model_checkpoints: dict, name: str = "run1") -> str:
    """Same as `_write_run`, plus an explicit `model_checkpoints` key -- the
    real orchestrator's own shape (`pipeline/provenance.py::
    finalize_model_checkpoint_provenance`), not hand-abbreviated."""
    output_dir = os.path.join(root, name)
    os.makedirs(output_dir, exist_ok=True)
    document = _make_document()
    document["model_checkpoints"] = model_checkpoints
    with open(os.path.join(output_dir, s.RESULTS_FILENAME), "w", encoding="utf-8") as fh:
        json.dump(document, fh)
    return output_dir


_MISMATCH_CHECKPOINTS = {
    "esm2": {
        "identifier": "facebook/esm2_t33_650M_UR50D@main",
        "status": "used",
        "reason": "",
        "resolved_version": "main",
        "loaded_artifact": {
            "requested_path": "/cache/esm2/model.safetensors",
            "resolved_path": "/cache/esm2/blobs/deadbeef00000000000000000000000000000000000000000000000000000000",
            "served_revision": "main",
            "cache_declared_sha256": "deadbeef00000000000000000000000000000000000000000000000000000000",
            "hash_verification": "mismatch",
            "version_status": "unknown",
            "note": "MISMATCH: bytes do not match the cache's declared sha256 -- corrupted or substituted model cache",
        },
    }
}

_MISMATCH_CHECKPOINTS_WITH_OBSERVED_HASH = {
    "esm2": {
        "identifier": "facebook/esm2_t33_650M_UR50D@main",
        "status": "used",
        "reason": "",
        "resolved_version": "main",
        "loaded_artifact": {
            "requested_path": "/cache/esm2/model.safetensors",
            "resolved_path": "/cache/esm2/blobs/deadbeef00000000000000000000000000000000000000000000000000000000",
            "served_revision": "main",
            "cache_declared_sha256": "deadbeef00000000000000000000000000000000000000000000000000000000",
            "observed_sha256": "c0ffee0000000000000000000000000000000000000000000000000000000000",
            "hash_verification": "mismatch",
            "version_status": "unknown",
            "note": "MISMATCH: bytes do not match the cache's declared sha256 -- corrupted or substituted model cache",
        },
    }
}

_VERIFIED_CHECKPOINTS = {
    "esm2": {
        "identifier": "facebook/esm2_t33_650M_UR50D@main",
        "status": "used",
        "reason": "",
        "resolved_version": "main",
        "loaded_artifact": {
            "requested_path": "/cache/esm2/model.safetensors",
            "resolved_path": "/cache/esm2/blobs/cafebabe00000000000000000000000000000000000000000000000000000000",
            "served_revision": "main",
            "cache_declared_sha256": "cafebabe00000000000000000000000000000000000000000000000000000000",
            "hash_verification": "verified",
            "version_status": "hash_only",
            "note": "bytes streamed and matched the hash declared by the cache",
        },
    }
}


class TestApproveModelHashMismatch(unittest.TestCase):
    def test_mismatch_refuses_and_names_model_and_expected_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run_with_model_checkpoints(tmp, _MISMATCH_CHECKPOINTS)
            with self.assertRaises(SignoffError) as ctx:
                s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            message = str(ctx.exception)
            self.assertIn("esm2", message)
            self.assertIn("deadbeef", message)  # the expected sha256
            self.assertIn("MISMATCH", message)
            self.assertIn("re-run", message)
            self.assertIn(
                "not present in this record",
                message,
                "a record without observed_sha256 (pre-change, or MISMATCH some other way) must say so, not print None",
            )

    def test_mismatch_names_the_observed_hash_when_the_record_has_one(self):
        # THE CARD: a refusal that names only the expected hash is half a
        # message -- the reader cannot tell what was actually loaded.
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run_with_model_checkpoints(tmp, _MISMATCH_CHECKPOINTS_WITH_OBSERVED_HASH)
            with self.assertRaises(SignoffError) as ctx:
                s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            message = str(ctx.exception)
            self.assertIn("esm2", message)
            self.assertIn("deadbeef", message)  # the expected sha256
            self.assertIn("c0ffee", message)  # the observed sha256
            self.assertIn("MISMATCH", message)

    def test_mismatch_leaves_nothing_on_disk_changed(self):
        # A block that half-writes is worse than no block -- the check must
        # run before ANY side effect (patient_meta, the results JSON
        # rewrite, PDF regeneration, the manifest).
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run_with_model_checkpoints(tmp, _MISMATCH_CHECKPOINTS)
            results_path = os.path.join(output_dir, s.RESULTS_FILENAME)
            with open(results_path, "rb") as fh:
                results_before = fh.read()
            files_before = sorted(os.listdir(output_dir))

            with self.assertRaises(SignoffError):
                s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")

            files_after = sorted(os.listdir(output_dir))
            self.assertEqual(files_before, files_after, "approve() must not create any new file on refusal")
            with open(results_path, "rb") as fh:
                results_after = fh.read()
            self.assertEqual(results_before, results_after, "approve() must not modify geper_results.json on refusal")

    def test_verified_hash_does_not_block_approval(self):
        # Negative control: a model_checkpoints entry that HAS a
        # hash_verification result, but not a mismatch, must not trip the
        # new check -- proves the check discriminates rather than refusing
        # on the mere presence of the field.
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run_with_model_checkpoints(tmp, _VERIFIED_CHECKPOINTS)
            manifest = s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            self.assertTrue(os.path.exists(s._manifest_path(output_dir)))
            self.assertEqual(manifest["clinician_name"], "Dr. Rajesh Sharma")

    def test_absent_model_checkpoints_still_approves(self):
        # Every pre-existing TestApprove test already exercises this
        # (`_make_document()` has no `model_checkpoints` key at all), but
        # made explicit here as the backward-compatibility case this new
        # check must not break: a document with nothing recorded for
        # models is not the same as a document with a mismatch.
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            self.assertTrue(os.path.exists(s._manifest_path(output_dir)))


# ---------------------------------------------------------------------------
# override()
# ---------------------------------------------------------------------------


class TestOverride(unittest.TestCase):
    def test_errors_clearly_when_no_results_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SignoffError):
                s.override(tmp, "17:43106534:C>A", "Benign", "reason", "clinician@example.com")

    def test_errors_clearly_when_variant_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            with self.assertRaises(SignoffError) as ctx:
                s.override(output_dir, "5:1:A>T", "Benign", "reason", "clinician@example.com")
            self.assertIn("5:1:A>T", str(ctx.exception))

    def test_override_adds_record_without_deleting_original_classification(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.override(output_dir, "2:500:G>T", "Likely Pathogenic", "Family history", "rajesh.sharma@aiims.edu")

            with open(os.path.join(output_dir, s.RESULTS_FILENAME), encoding="utf-8") as fh:
                document = json.load(fh)
            variant_result = s.find_variant(document, "2", 500, "G", "T")
            self.assertEqual(
                variant_result["candidate_interpretation"]["acmg_classification"]["classification"],
                "Uncertain significance",
            )
            self.assertEqual(len(variant_result["overrides"]), 1)
            record = variant_result["overrides"][0]
            self.assertEqual(record["original_classification"], "Uncertain significance")
            self.assertEqual(record["new_classification"], "Likely Pathogenic")
            self.assertEqual(record["reason"], "Family history")
            self.assertEqual(record["clinician_id"], "rajesh.sharma@aiims.edu")

    def test_effective_classification_reflects_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.override(output_dir, "2:500:G>T", "Likely Pathogenic", "Family history", "rajesh.sharma@aiims.edu")
            with open(os.path.join(output_dir, s.RESULTS_FILENAME), encoding="utf-8") as fh:
                document = json.load(fh)
            variant_result = s.find_variant(document, "2", 500, "G", "T")
            self.assertEqual(s.effective_classification(variant_result), "Likely Pathogenic")
            # An un-overridden variant's effective classification is
            # still just GEPER's own.
            brca1 = s.find_variant(document, "17", 43106534, "C", "A")
            self.assertEqual(s.effective_classification(brca1), "Pathogenic")

    def test_override_regenerates_markdown_showing_both_classifications(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.override(output_dir, "2:500:G>T", "Likely Pathogenic", "Family history", "rajesh.sharma@aiims.edu")
            with open(os.path.join(output_dir, s.MARKDOWN_REPORT_FILENAME), encoding="utf-8") as fh:
                md = fh.read()
            self.assertIn("Clinician override", md)
            self.assertIn("Uncertain significance", md)
            self.assertIn("Likely Pathogenic", md)
            self.assertIn("Family history", md)

    @unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
    def test_override_regenerates_pdfs_showing_both_classifications(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.override(output_dir, "2:500:G>T", "Likely Pathogenic", "Family history", "rajesh.sharma@aiims.edu")
            full_text = _all_pdf_text(os.path.join(output_dir, s.FULL_PDF_FILENAME))
            self.assertIn("Uncertain significance", full_text)
            self.assertIn("Likely Pathogenic", full_text)

    @unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
    def test_override_alone_does_not_touch_patient_meta(self):
        # Subject under test is patient_meta being untouched (the file
        # is never written) -- the PDF-text expectation below only
        # confirms an override alone produces its own dedicated footer
        # (card review-status-two-sources-of-truth), not DRAFT as it
        # did before that footer existed.
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            self.assertFalse(os.path.exists(s._patient_meta_path(output_dir)))
            s.override(output_dir, "2:500:G>T", "Likely Pathogenic", "Family history", "rajesh.sharma@aiims.edu")
            self.assertFalse(os.path.exists(s._patient_meta_path(output_dir)))
            full_text = _all_pdf_text(os.path.join(output_dir, s.FULL_PDF_FILENAME))
            self.assertIn(_ICMR_OVERRIDDEN_FOOTER_TEXT, full_text)

    # test_override_after_approve_preserves_reviewed_footer retired
    # (card review-status-two-sources-of-truth): it pinned the PDF
    # footer staying "reviewed by {physician}" after an approve() then
    # override() sequence, which was the exact known divergence the fix
    # closes. That scenario now has a superset of coverage (JSON,
    # Markdown, and PDF, all checked for agreement) in
    # tests/test_review_status_governance.py::
    # TestPdfMarkdownReviewStatusConsistency::
    # test_override_after_approve_pdf_and_markdown_agree_overridden --
    # keeping both would just be duplicate assertions on the same
    # approve()+override() call sequence.

    def test_override_appends_to_audit_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.override(output_dir, "2:500:G>T", "Likely Pathogenic", "Family history", "rajesh.sharma@aiims.edu")
            with open(s._audit_log_path(output_dir), encoding="utf-8") as fh:
                lines = [json.loads(line) for line in fh if line.strip()]
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["action"], "override")
            self.assertEqual(lines[0]["variant"], "2:500:G>T")
            self.assertEqual(lines[0]["original"], "Uncertain significance")
            self.assertEqual(lines[0]["new"], "Likely Pathogenic")

    def test_missing_clinical_report_errors_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            results_path = os.path.join(output_dir, s.RESULTS_FILENAME)
            with open(results_path, encoding="utf-8") as fh:
                document = json.load(fh)
            document["variants"][0]["candidate_interpretation"] = None
            with open(results_path, "w", encoding="utf-8") as fh:
                json.dump(document, fh)
            with self.assertRaises(SignoffError):
                s.override(output_dir, "17:43106534:C>A", "Benign", "reason", "clinician@example.com")


# ---------------------------------------------------------------------------
# list_pending()
# ---------------------------------------------------------------------------


class TestListPending(unittest.TestCase):
    def test_unreviewed_run_shown_as_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            rows = s.list_pending(tmp)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["output_dir"], output_dir)
            self.assertEqual(rows[0]["status"], "DRAFT")
            self.assertEqual(rows[0]["num_variants"], 3)
            self.assertTrue(rows[0]["has_conflicting_evidence"])  # the VUS variant has severity="Major"

    def test_reviewed_run_excluded_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            self.assertEqual(s.list_pending(tmp), [])

    def test_reviewed_run_included_with_show_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            rows = s.list_pending(tmp, show_all=True)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "REVIEWED")

    def test_multiple_runs_mixed_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            draft_dir = _write_run(tmp, "draft_run")
            reviewed_dir = _write_run(tmp, "reviewed_run")
            s.approve(reviewed_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")

            draft_only = s.list_pending(tmp)
            self.assertEqual([r["output_dir"] for r in draft_only], [draft_dir])

            everything = s.list_pending(tmp, show_all=True)
            statuses = {r["output_dir"]: r["status"] for r in everything}
            self.assertEqual(statuses[draft_dir], "DRAFT")
            self.assertEqual(statuses[reviewed_dir], "REVIEWED")

    def test_no_runs_found_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(s.list_pending(tmp), [])

    def test_corrupt_results_json_is_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            good_dir = _write_run(tmp, "good_run")
            bad_dir = os.path.join(tmp, "bad_run")
            os.makedirs(bad_dir)
            with open(os.path.join(bad_dir, s.RESULTS_FILENAME), "w", encoding="utf-8") as fh:
                fh.write("{not valid json")
            rows = s.list_pending(tmp)
            self.assertEqual([r["output_dir"] for r in rows], [good_dir])

    def test_has_conflicting_evidence_false_when_no_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            results_path = os.path.join(output_dir, s.RESULTS_FILENAME)
            with open(results_path, encoding="utf-8") as fh:
                document = json.load(fh)
            for vr in document["variants"]:
                vr["candidate_interpretation"]["conflict_resolution"]["severity"] = None
            with open(results_path, "w", encoding="utf-8") as fh:
                json.dump(document, fh)
            rows = s.list_pending(tmp)
            self.assertFalse(rows[0]["has_conflicting_evidence"])


class TestRequireReviewed(unittest.TestCase):
    """A-2 option 2: the LIMS gate generalized into signoff.py so any
    future automated consumer can call it, not just export_lims.py."""

    def test_raises_when_not_reviewed(self):
        with self.assertRaises(SignoffError) as ctx:
            s.require_reviewed({"review_status": "draft"})
        self.assertIn("draft", str(ctx.exception))
        self.assertIn("reviewed", str(ctx.exception))

    def test_raises_when_overridden(self):
        # "overridden" must not be good enough -- same rule export_lims.py
        # already enforced, now shared rather than duplicated.
        with self.assertRaises(SignoffError):
            s.require_reviewed({"review_status": "overridden"})

    def test_raises_when_key_missing(self):
        with self.assertRaises(SignoffError):
            s.require_reviewed({})

    def test_passes_when_reviewed(self):
        s.require_reviewed({"review_status": "reviewed"})  # must not raise

    def test_consumer_name_appears_in_message(self):
        with self.assertRaises(SignoffError) as ctx:
            s.require_reviewed({"review_status": "draft"}, consumer="Some Future Consumer")
        self.assertIn("Some Future Consumer", str(ctx.exception))


class TestWithdraw(unittest.TestCase):
    """A-2 option 3: an explicit erasure of a standing sign-off."""

    def test_withdraw_removes_manifest_and_resets_to_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            self.assertTrue(os.path.exists(s._manifest_path(output_dir)))

            result = s.withdraw(output_dir, "Signed off in error", "rajesh.sharma@aiims.edu")

            self.assertFalse(os.path.exists(s._manifest_path(output_dir)))
            self.assertEqual(result["previous_review_status"], "reviewed")
            with open(os.path.join(output_dir, s.RESULTS_FILENAME), encoding="utf-8") as fh:
                document = json.load(fh)
            self.assertEqual(document["review_status"], "draft")

    def test_withdraw_fixes_list_pending_after_a_hypothetical_stale_manifest(self):
        # Regression pin for B2 (review-status-two-sources-of-truth):
        # withdraw() must make list_pending() agree with review_status
        # again, the same disagreement override() now prevents from ever
        # occurring in the first place (see TestOverride below).
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            self.assertEqual(s.list_pending(tmp, show_all=True)[0]["status"], "REVIEWED")

            s.withdraw(output_dir, "Signed off in error", "rajesh.sharma@aiims.edu")

            self.assertEqual(s.list_pending(tmp)[0]["status"], "DRAFT")

    def test_withdraw_raises_when_no_manifest_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)  # never approved
            with self.assertRaises(SignoffError):
                s.withdraw(output_dir, "reason", "actor")

    def test_withdraw_raises_when_no_results_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SignoffError):
                s.withdraw(tmp, "reason", "actor")

    def test_withdraw_appends_audit_log_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            s.withdraw(output_dir, "Signed off in error", "rajesh.sharma@aiims.edu")
            with open(s._audit_log_path(output_dir), encoding="utf-8") as fh:
                lines = [json.loads(line) for line in fh if line.strip()]
            actions = [line["action"] for line in lines]
            self.assertIn("withdrawn", actions)
            withdrawn_entry = next(line for line in lines if line["action"] == "withdrawn")
            self.assertEqual(withdrawn_entry["previous_review_status"], "reviewed")
            self.assertEqual(withdrawn_entry["reason"], "Signed off in error")


class TestOverrideAutoWithdrawsStaleManifest(unittest.TestCase):
    """A-2 option 3's stated side effect: override() no longer leaves
    list_pending() disagreeing with review_status (card
    review-status-two-sources-of-truth's B2 case)."""

    def test_override_after_approve_removes_the_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            self.assertTrue(os.path.exists(s._manifest_path(output_dir)))

            s.override(output_dir, "2:500:G>T", "Likely Pathogenic", "Family history", "rajesh.sharma@aiims.edu")

            self.assertFalse(os.path.exists(s._manifest_path(output_dir)))

    def test_override_after_approve_list_pending_agrees_with_review_status(self):
        # The exact B2 scenario: before this fix, list_pending() kept
        # reporting REVIEWED here because it only checked manifest
        # presence, disagreeing with review_status == "overridden".
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            s.override(output_dir, "2:500:G>T", "Likely Pathogenic", "Family history", "rajesh.sharma@aiims.edu")

            rows = s.list_pending(tmp, show_all=True)
            self.assertEqual(rows[0]["status"], "DRAFT")

            with open(os.path.join(output_dir, s.RESULTS_FILENAME), encoding="utf-8") as fh:
                document = json.load(fh)
            self.assertEqual(document["review_status"], "overridden")

    def test_override_without_prior_approve_is_unaffected(self):
        # No manifest ever existed -- _withdraw_manifest must be a
        # harmless no-op, not an error, when there's nothing to remove.
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.override(output_dir, "2:500:G>T", "Likely Pathogenic", "Family history", "rajesh.sharma@aiims.edu")
            self.assertFalse(os.path.exists(s._manifest_path(output_dir)))


# ---------------------------------------------------------------------------
# CLI (argument parsing + end-to-end via main())
# ---------------------------------------------------------------------------


class TestCli(unittest.TestCase):
    def test_parser_requires_a_subcommand(self):
        parser = build_arg_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])

    def test_approve_parses_expected_arguments(self):
        parser = build_arg_parser()
        args = parser.parse_args(
            [
                "approve",
                "--output-dir",
                "/tmp/out",
                "--clinician-name",
                "Dr. Rajesh Sharma",
                "--reg-number",
                "MCI-12345",
                "--hospital",
                "AIIMS Delhi",
            ]
        )
        self.assertEqual(args.command, "approve")
        self.assertEqual(args.clinician_name, "Dr. Rajesh Sharma")

    def test_override_parses_expected_arguments(self):
        parser = build_arg_parser()
        args = parser.parse_args(
            [
                "override",
                "--output-dir",
                "/tmp/out",
                "--variant",
                "17:43106534:C>A",
                "--new-classification",
                "Likely Pathogenic",
                "--reason",
                "Family history",
                "--clinician-id",
                "rajesh.sharma@aiims.edu",
            ]
        )
        self.assertEqual(args.command, "override")
        self.assertEqual(args.variant, "17:43106534:C>A")

    def test_list_pending_default_is_draft_only(self):
        parser = build_arg_parser()
        args = parser.parse_args(["list-pending", "--search-root", "/tmp"])
        self.assertFalse(args.show_all)

    def test_withdraw_parses_expected_arguments(self):
        parser = build_arg_parser()
        args = parser.parse_args(
            [
                "withdraw",
                "--output-dir",
                "/tmp/out",
                "--reason",
                "Signed off in error",
                "--actor",
                "rajesh.sharma@aiims.edu",
            ]
        )
        self.assertEqual(args.command, "withdraw")
        self.assertEqual(args.reason, "Signed off in error")
        self.assertEqual(args.actor, "rajesh.sharma@aiims.edu")

    def test_main_approve_returns_zero_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            rc = cli_main(
                [
                    "approve",
                    "--output-dir",
                    output_dir,
                    "--clinician-name",
                    "Dr. Rajesh Sharma",
                    "--reg-number",
                    "MCI-12345",
                    "--hospital",
                    "AIIMS Delhi",
                ]
            )
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(s._manifest_path(output_dir)))

    def test_main_approve_returns_nonzero_on_missing_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = cli_main(
                [
                    "approve",
                    "--output-dir",
                    tmp,
                    "--clinician-name",
                    "Dr. Rajesh Sharma",
                    "--reg-number",
                    "MCI-12345",
                    "--hospital",
                    "AIIMS Delhi",
                ]
            )
            self.assertEqual(rc, 1)

    def test_main_list_pending_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_run(tmp)
            rc = cli_main(["list-pending", "--search-root", tmp])
            self.assertEqual(rc, 0)

    def test_main_withdraw_returns_zero_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            cli_main(
                [
                    "approve",
                    "--output-dir",
                    output_dir,
                    "--clinician-name",
                    "Dr. Rajesh Sharma",
                    "--reg-number",
                    "MCI-12345",
                    "--hospital",
                    "AIIMS Delhi",
                ]
            )
            rc = cli_main(
                [
                    "withdraw",
                    "--output-dir",
                    output_dir,
                    "--reason",
                    "Signed off in error",
                    "--actor",
                    "rajesh.sharma@aiims.edu",
                ]
            )
            self.assertEqual(rc, 0)
            self.assertFalse(os.path.exists(s._manifest_path(output_dir)))

    def test_main_withdraw_returns_nonzero_when_nothing_to_withdraw(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)  # never approved
            rc = cli_main(
                [
                    "withdraw",
                    "--output-dir",
                    output_dir,
                    "--reason",
                    "reason",
                    "--actor",
                    "actor",
                ]
            )
            self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
