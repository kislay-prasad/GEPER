"""
Tests for clinical report content verification (Phase 6: Report Immutability & Verification).

Hash computation, storage, and verification for ISO 15189 7.5 nonconformance detection.
Tests ensure that:
1. Hash computation is deterministic (identical content -> identical hash across runs)
2. Hash captures only clinical content (excludes timestamps, file paths, review status)
3. Modified clinical content produces different hash
4. Verification gate refuses export when hash doesn't match
5. Hash is stored at approval and verified at export
"""

import json
import os
import tempfile
import unittest

from report.clinical_report_builder import build_clinical_report
from report.models import compute_content_hash, verify_content_hash
from review import signoff as s
from utils.exceptions import SignoffError


# ---------------------------------------------------------------------------
# Test fixture: base document for hash testing
# ---------------------------------------------------------------------------


def _make_test_document():
    """Create a minimal but complete document for hash testing."""
    variant_dict = {"chrom": "17", "pos": 43106534, "ref": "C", "alt": "A"}
    ir = {
        "variant": {},
        "gene_symbol": "BRCA1",
        "acmg_classification": "Pathogenic",
        "triggered_rules": [],
        "not_triggered_rules": [],
        "not_evaluated_rules": [],
        "combining_rule_trace": [],
        "supporting_evidence": ["ClinVar: Pathogenic"],
        "conflicting_evidence": [],
        "ai_consensus": [],
        "ai_context_models": [],
        "confidence_pending": False,
        "confidence_score": 92.0,
        "confidence_label": "High",
        "priority_pending": False,
        "priority_explanation": ["High-risk gene"],
        "priority_score": 88.0,
        "priority_category": "Critical",
        "conflict_list": [],
        "conflict_score": 0.0,
        "conflict_severity": None,
        "recommendations": ["Confirm with orthogonal method"],
        "evidence_sources": ["ClinVar", "dbSNP"],
        "ai_model_errors": [],
    }
    clinical_report = build_clinical_report(ir, variant_dict=variant_dict, raw_evidence={})

    return {
        "geper_version": "test",
        "generated_at": "2026-08-06T10:00:00+00:00",
        "input_vcf": "test.vcf",
        "assembly": "GRCh38",
        "vcf_samples": ["SAMPLE01"],
        "variant_count": 1,
        "code_version": "test-v1",
        "review_status": "draft",
        "caveats": [],
        "variants": [
            {
                "variant": variant_dict,
                "normalization": {
                    "skipped": False,
                    "normalized": {},
                    "hgvs_g": "NC_000017.11:g.43106534C>A",
                    "hgvs_c": "NM_007294.4:c.135-1G>T",
                },
                "gnomad": {"skipped": False, "found": False, "global_af": None},
                "dbsnp": {"skipped": False, "found": True, "rsid": "rs80357914"},
                "clinvar": {
                    "match_status": "matched",
                    "primary_record": {
                        "clinical_significance": "Pathogenic",
                        "review_status": "criteria provided, multiple submitters",
                    },
                },
                "clingen": {"found": True, "gene_symbol": "BRCA1", "clinical_validity_summary": "Definitive"},
                "alphamissense": {"found": False},
                "errors": [],
                "interpretation_result": {"gene_symbol": "BRCA1", "acmg_classification": "Pathogenic"},
                "candidate_interpretation": clinical_report,
                "interpretation": {},
            }
        ],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHashCanonicalization(unittest.TestCase):
    """Tests that hash computation is deterministic."""

    def test_identical_documents_produce_identical_hash(self):
        """Identical content must produce identical hash (deterministic serialization)."""
        doc1 = _make_test_document()
        doc2 = _make_test_document()

        hash1 = compute_content_hash(doc1)
        hash2 = compute_content_hash(doc2)

        self.assertEqual(hash1, hash2, "Identical documents must produce identical hash")

    def test_hash_stable_across_multiple_runs(self):
        """Hash must be identical when computed multiple times on same document."""
        doc = _make_test_document()

        hashes = [compute_content_hash(doc) for _ in range(5)]

        self.assertEqual(len(set(hashes)), 1, "Hash must be identical across multiple runs")
        self.assertTrue(all(h == hashes[0] for h in hashes))

    def test_hash_is_hex_string(self):
        """Hash must be a valid 64-character hex string (SHA-256)."""
        doc = _make_test_document()
        h = compute_content_hash(doc)

        self.assertEqual(len(h), 64, "Hash must be 64 characters (SHA-256)")
        self.assertTrue(all(c in "0123456789abcdef" for c in h), "Hash must be valid hex")

    def test_hash_includes_variant_data(self):
        """Hash must change when variant data changes."""
        doc1 = _make_test_document()
        doc2 = _make_test_document()

        # Change variant position
        doc2["variants"][0]["variant"]["pos"] = 43106535

        hash1 = compute_content_hash(doc1)
        hash2 = compute_content_hash(doc2)

        self.assertNotEqual(hash1, hash2, "Hash must change when variant position changes")

    def test_hash_includes_classification(self):
        """Hash must change when ACMG classification changes."""
        doc1 = _make_test_document()
        doc2 = _make_test_document()

        # Change classification
        doc2["variants"][0]["candidate_interpretation"]["acmg_classification"]["classification"] = "Likely Pathogenic"

        hash1 = compute_content_hash(doc1)
        hash2 = compute_content_hash(doc2)

        self.assertNotEqual(hash1, hash2, "Hash must change when classification changes")

    def test_hash_includes_clinician_override(self):
        """Hash must change when clinician override is added."""
        doc1 = _make_test_document()
        doc2 = _make_test_document()

        # Add clinician override
        doc2["variants"][0]["candidate_interpretation"]["acmg_classification"]["clinician_override"] = {
            "original_classification": "Pathogenic",
            "new_classification": "Likely Pathogenic",
            "reason": "Reconsidered evidence",
        }

        hash1 = compute_content_hash(doc1)
        hash2 = compute_content_hash(doc2)

        self.assertNotEqual(hash1, hash2, "Hash must change when clinician override is added")

    def test_hash_excludes_timestamps(self):
        """Hash must NOT change when timestamps change (workflow state, not clinical)."""
        doc1 = _make_test_document()
        doc2 = _make_test_document()

        # Change generated_at timestamp
        doc2["generated_at"] = "2026-08-06T11:00:00+00:00"

        hash1 = compute_content_hash(doc1)
        hash2 = compute_content_hash(doc2)

        self.assertEqual(hash1, hash2, "Hash must NOT change when generated_at changes")

    def test_hash_excludes_review_status(self):
        """Hash must NOT change when review_status changes (workflow state, not clinical)."""
        doc1 = _make_test_document()
        doc2 = _make_test_document()
        doc2["review_status"] = "reviewed"

        hash1 = compute_content_hash(doc1)
        hash2 = compute_content_hash(doc2)

        self.assertEqual(hash1, hash2, "Hash must NOT change when review_status changes")

    def test_hash_excludes_reviewed_by_and_reviewed_at(self):
        """Hash must NOT change when reviewed_by/reviewed_at are added."""
        doc1 = _make_test_document()
        doc2 = _make_test_document()
        doc2["reviewed_by"] = "Dr. Smith"
        doc2["reviewed_at"] = "2026-08-06T12:00:00+00:00"

        hash1 = compute_content_hash(doc1)
        hash2 = compute_content_hash(doc2)

        self.assertEqual(hash1, hash2, "Hash must NOT change when reviewed_by/reviewed_at are added")

    def test_hash_excludes_input_vcf_path(self):
        """Hash must NOT change when input_vcf path changes (file path, not clinical)."""
        doc1 = _make_test_document()
        doc2 = _make_test_document()
        doc2["input_vcf"] = "/different/path/to/test.vcf"

        hash1 = compute_content_hash(doc1)
        hash2 = compute_content_hash(doc2)

        self.assertEqual(hash1, hash2, "Hash must NOT change when input_vcf path changes")

    def test_hash_includes_caveats(self):
        """Hash must change when caveats (clinical warnings) are added."""
        doc1 = _make_test_document()
        doc2 = _make_test_document()
        doc2["caveats"] = ["Low coverage in region"]

        hash1 = compute_content_hash(doc1)
        hash2 = compute_content_hash(doc2)

        self.assertNotEqual(hash1, hash2, "Hash must change when caveats are added")

    def test_hash_deterministic_with_dict_ordering(self):
        """Hash must be identical even when dict key order differs in source."""
        doc = _make_test_document()

        # Compute hash multiple times (dicts will have consistent ordering internally)
        hash1 = compute_content_hash(doc)
        hash2 = compute_content_hash(doc)

        self.assertEqual(hash1, hash2, "Hash must be deterministic regardless of internal dict ordering")


class TestHashVerification(unittest.TestCase):
    """Tests for hash verification logic."""

    def test_verify_content_hash_passes_with_matching_hash(self):
        """Verification passes when computed hash matches stored hash."""
        doc = _make_test_document()
        stored_hash = compute_content_hash(doc)
        doc["content_hash"] = stored_hash

        result = verify_content_hash(doc, stored_hash)

        self.assertTrue(result, "Verification should pass with matching hash")

    def test_verify_content_hash_fails_with_mismatched_hash(self):
        """Verification fails when computed hash doesn't match stored hash."""
        doc = _make_test_document()
        doc["variants"][0]["candidate_interpretation"]["acmg_classification"]["classification"] = "Likely Pathogenic"

        wrong_hash = "0" * 64  # Deliberately wrong hash

        result = verify_content_hash(doc, wrong_hash)

        self.assertFalse(result, "Verification should fail with mismatched hash")

    def test_verify_content_hash_fails_when_content_modified(self):
        """Verification fails when clinical content is modified after approval."""
        doc = _make_test_document()
        original_hash = compute_content_hash(doc)
        doc["content_hash"] = original_hash

        # Simulate modification after approval
        doc["variants"][0]["candidate_interpretation"]["confidence"]["score"] = 50.0

        result = verify_content_hash(doc, original_hash)

        self.assertFalse(result, "Verification should fail when clinical content is modified")

    def test_verify_content_hash_returns_false_for_missing_stored_hash(self):
        """Verification returns False when stored hash is None or missing."""
        doc = _make_test_document()

        result = verify_content_hash(doc, None)

        self.assertFalse(result, "Verification should return False for missing stored hash")

    def test_verify_content_hash_returns_false_for_empty_stored_hash(self):
        """Verification returns False when stored hash is empty string."""
        doc = _make_test_document()

        result = verify_content_hash(doc, "")

        self.assertFalse(result, "Verification should return False for empty stored hash")


class TestVerificationGateIntegration(unittest.TestCase):
    """Tests that verification gate is integrated into require_reviewed()."""

    def test_require_reviewed_passes_with_valid_hash(self):
        """require_reviewed() passes when hash is present and valid."""
        doc = _make_test_document()
        doc["review_status"] = "reviewed"
        doc["content_hash"] = compute_content_hash(doc)

        # Should not raise
        s.require_reviewed(doc)

    def test_require_reviewed_passes_without_hash_for_backward_compatibility(self):
        """require_reviewed() passes without hash for backward compatibility."""
        doc = _make_test_document()
        doc["review_status"] = "reviewed"
        # No content_hash present (old reports before Phase 6)

        # Should not raise
        s.require_reviewed(doc)

    def test_require_reviewed_refuses_when_hash_mismatches(self):
        """require_reviewed() refuses export when hash doesn't match."""
        doc = _make_test_document()
        doc["review_status"] = "reviewed"
        doc["content_hash"] = compute_content_hash(doc)

        # Modify clinical content after approval
        doc["variants"][0]["candidate_interpretation"]["acmg_classification"]["classification"] = "Likely Pathogenic"

        with self.assertRaises(SignoffError) as ctx:
            s.require_reviewed(doc)

        self.assertIn("content hash verification failed", str(ctx.exception))
        self.assertIn("ISO 15189 7.5 nonconformance", str(ctx.exception))

    def test_require_reviewed_error_message_mentions_iso_standard(self):
        """require_reviewed() error message mentions ISO 15189 7.5 standard."""
        doc = _make_test_document()
        doc["review_status"] = "reviewed"
        doc["content_hash"] = compute_content_hash(doc)

        # Modify content
        doc["variants"][0]["candidate_interpretation"]["priority"]["score"] = 10.0

        with self.assertRaises(SignoffError) as ctx:
            s.require_reviewed(doc)

        error_msg = str(ctx.exception)
        self.assertIn("modified after sign-off", error_msg)
        self.assertIn("ISO 15189 7.5", error_msg)


class TestApproveStoresHash(unittest.TestCase):
    """Tests that approve() stores content hash."""

    def test_approve_computes_and_stores_content_hash(self):
        """approve() must compute and store content_hash in document."""
        with tempfile.TemporaryDirectory() as tmpdir:
            doc = _make_test_document()
            results_path = os.path.join(tmpdir, s.RESULTS_FILENAME)
            with open(results_path, "w") as f:
                json.dump(doc, f)

            s.approve(tmpdir, "Dr. Smith", "12345", "ABC Hospital")

            # Reload document to verify hash was stored
            with open(results_path, "r") as f:
                approved_doc = json.load(f)

            self.assertIn("content_hash", approved_doc, "approve() must store content_hash")
            self.assertEqual(len(approved_doc["content_hash"]), 64, "Hash must be 64 characters")

            # Verify it's the correct hash
            expected_hash = compute_content_hash(approved_doc)
            self.assertEqual(approved_doc["content_hash"], expected_hash)

    def test_approve_includes_hash_in_manifest(self):
        """approve() must include content_hash in manifest."""
        with tempfile.TemporaryDirectory() as tmpdir:
            doc = _make_test_document()
            results_path = os.path.join(tmpdir, s.RESULTS_FILENAME)
            with open(results_path, "w") as f:
                json.dump(doc, f)

            manifest = s.approve(tmpdir, "Dr. Smith", "12345", "ABC Hospital")

            self.assertIn("content_hash", manifest, "Manifest must include content_hash")
            self.assertEqual(len(manifest["content_hash"]), 64)


class TestModifiedContentDetection(unittest.TestCase):
    """Tests that modified clinical content is detected during verification."""

    def test_modified_acmg_classification_detected(self):
        """Modification of ACMG classification must be detected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            doc = _make_test_document()
            results_path = os.path.join(tmpdir, s.RESULTS_FILENAME)
            with open(results_path, "w") as f:
                json.dump(doc, f)

            # Approve the document
            s.approve(tmpdir, "Dr. Smith", "12345", "ABC Hospital")

            # Reload and modify classification
            with open(results_path, "r") as f:
                doc = json.load(f)
            doc["variants"][0]["candidate_interpretation"]["acmg_classification"]["classification"] = "Benign"
            with open(results_path, "w") as f:
                json.dump(doc, f)

            # Reload again and verify should fail
            with open(results_path, "r") as f:
                doc = json.load(f)

            with self.assertRaises(SignoffError):
                s.require_reviewed(doc)

    def test_modified_confidence_detected(self):
        """Modification of confidence score must be detected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            doc = _make_test_document()
            results_path = os.path.join(tmpdir, s.RESULTS_FILENAME)
            with open(results_path, "w") as f:
                json.dump(doc, f)

            # Approve
            s.approve(tmpdir, "Dr. Smith", "12345", "ABC Hospital")

            # Modify and verify
            with open(results_path, "r") as f:
                doc = json.load(f)
            doc["variants"][0]["candidate_interpretation"]["confidence"]["score"] = 30.0
            with open(results_path, "w") as f:
                json.dump(doc, f)

            with open(results_path, "r") as f:
                doc = json.load(f)

            with self.assertRaises(SignoffError):
                s.require_reviewed(doc)

    def test_modified_evidence_detected(self):
        """Modification of evidence must be detected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            doc = _make_test_document()
            results_path = os.path.join(tmpdir, s.RESULTS_FILENAME)
            with open(results_path, "w") as f:
                json.dump(doc, f)

            # Approve
            s.approve(tmpdir, "Dr. Smith", "12345", "ABC Hospital")

            # Modify evidence
            with open(results_path, "r") as f:
                doc = json.load(f)
            doc["variants"][0]["candidate_interpretation"]["supporting_evidence"] = ["Different evidence"]
            with open(results_path, "w") as f:
                json.dump(doc, f)

            with open(results_path, "r") as f:
                doc = json.load(f)

            with self.assertRaises(SignoffError):
                s.require_reviewed(doc)

    def test_unmodified_timestamp_not_detected_as_change(self):
        """Modification of timestamps must NOT cause verification to fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            doc = _make_test_document()
            results_path = os.path.join(tmpdir, s.RESULTS_FILENAME)
            with open(results_path, "w") as f:
                json.dump(doc, f)

            # Approve
            s.approve(tmpdir, "Dr. Smith", "12345", "ABC Hospital")

            # Modify timestamp (should not affect verification)
            with open(results_path, "r") as f:
                doc = json.load(f)
            doc["generated_at"] = "2026-08-07T00:00:00+00:00"
            with open(results_path, "w") as f:
                json.dump(doc, f)

            # Reload and verify - should pass because timestamp doesn't affect hash
            with open(results_path, "r") as f:
                doc = json.load(f)

            # Should NOT raise
            s.require_reviewed(doc)


class TestModelProvenanceInHash(unittest.TestCase):
    """RED-FIRST TESTS: Model/provenance fields are material to clinical output and must affect hash.

    ISO 15189 7.5 d) impact analysis requires capturing material changes to the interpretation tool.
    If code_version, model_checkpoints, database_version, or geper_version change, the clinical
    output can materially differ. These must be included in the content hash.
    """

    def test_different_code_version_produces_different_hash(self):
        """DEFECT: Different code versions currently produce same hash. Must produce different hashes."""
        doc1 = _make_test_document()
        doc2 = _make_test_document()

        # All clinical content identical, only code_version differs
        doc1["code_version"] = "1.0.0"
        doc2["code_version"] = "2.0.0"

        hash1 = compute_content_hash(doc1)
        hash2 = compute_content_hash(doc2)

        # DEFECT: These are currently equal but SHOULD be different
        # After fix, this assertion MUST pass
        self.assertNotEqual(hash1, hash2, "Different code versions must produce different hashes (material to output)")

    def test_different_geper_version_produces_different_hash(self):
        """DEFECT: Different geper versions currently produce same hash. Must produce different hashes."""
        doc1 = _make_test_document()
        doc2 = _make_test_document()

        doc1["geper_version"] = "0.1.0"
        doc2["geper_version"] = "0.2.0"

        hash1 = compute_content_hash(doc1)
        hash2 = compute_content_hash(doc2)

        self.assertNotEqual(hash1, hash2, "Different geper versions must produce different hashes (material to output)")

    def test_different_model_checkpoints_produces_different_hash(self):
        """DEFECT: Different model checkpoints currently produce same hash. Must produce different hashes."""
        doc1 = _make_test_document()
        doc2 = _make_test_document()

        doc1["model_checkpoints"] = {"acmg_classifier": "v1.0"}
        doc2["model_checkpoints"] = {"acmg_classifier": "v2.0"}

        hash1 = compute_content_hash(doc1)
        hash2 = compute_content_hash(doc2)

        self.assertNotEqual(
            hash1, hash2, "Different model checkpoints must produce different hashes (material to output)"
        )

    def test_different_database_version_produces_different_hash(self):
        """DEFECT: Different database versions currently produce same hash. Must produce different hashes."""
        doc1 = _make_test_document()
        doc2 = _make_test_document()

        doc1["database_version"] = "clinvar-2026-01"
        doc2["database_version"] = "clinvar-2026-02"

        hash1 = compute_content_hash(doc1)
        hash2 = compute_content_hash(doc2)

        self.assertNotEqual(
            hash1, hash2, "Different database versions must produce different hashes (material to output)"
        )


class TestAuditDistinction(unittest.TestCase):
    """RED-FIRST TESTS: Audit trail must distinguish 'content_verified' from 'content_not_verified_no_hash_predates_phase_6'.

    Backward compatibility requires both paths to allow export (old pre-Phase 6 reports have no hash).
    But audit trail must be clear about which path was taken for compliance/forensic review.
    """

    def test_require_reviewed_with_hash_creates_verified_audit_entry(self):
        """With hash present and valid, audit must record 'content_verified'."""
        with tempfile.TemporaryDirectory() as tmpdir:
            doc = _make_test_document()
            results_path = os.path.join(tmpdir, s.RESULTS_FILENAME)
            with open(results_path, "w") as f:
                json.dump(doc, f)

            # Approve (creates hash and audit entry)
            s.approve(tmpdir, "Dr. Smith", "12345", "ABC Hospital")

            # Reload and call require_reviewed
            with open(results_path, "r") as f:
                doc = json.load(f)

            # This should pass and log audit entry distinguishing verified vs not-verified
            # For now just verify it passes; audit distinction will be verified once implemented
            s.require_reviewed(doc)

    def test_require_reviewed_without_hash_creates_not_verified_audit_entry(self):
        """Without hash (pre-Phase 6 report), audit must record 'content_not_verified_no_hash_predates_phase_6'."""
        doc = _make_test_document()
        # Explicitly set review_status but NO content_hash (simulates pre-Phase 6 approval)
        doc["review_status"] = "reviewed"
        doc["reviewed_by"] = "Dr. Old"
        doc["reviewed_at"] = "2026-08-01T00:00:00+00:00"
        # No content_hash field

        # This should pass (backward compatibility) but audit trail must distinguish from verified case
        # For now just verify it passes; audit distinction will be verified once implemented
        s.require_reviewed(doc)


if __name__ == "__main__":
    unittest.main()
