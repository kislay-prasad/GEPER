"""
Tests for pipeline/provenance.py -- data-source version pinning and run
provenance, so a GEPER report can be reproduced later.

Lightweight throughout: mocked `requests`/`subprocess` calls only, real
temp files for hashing/sidecar tests (a few bytes, not real downloads),
and duck-typed `self` objects for exercising `GeperPipeline`'s new
provenance methods without constructing a full pipeline instance (which
loads real model registries -- out of scope for a unit test on an 8GB
machine).
"""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from pipeline.provenance import (
    KNOWN_SOURCES,
    DataSourceProvenance,
    RunProvenanceCollector,
    VersionStatus,
    capture_blast_local_tool_versions,
    capture_ensembl_release,
    compute_file_sha256,
    get_geper_code_version,
    get_model_checkpoint_identifiers,
    local_file_provenance,
    read_dataset_provenance_sidecar,
    write_dataset_provenance_sidecar,
)


class TestVersionStatusStructuralDistinction(unittest.TestCase):
    """Task point 6: NOT_CONSULTED and UNKNOWN must never collapse."""

    def test_not_consulted_and_unknown_are_different_states(self):
        self.assertNotEqual(VersionStatus.NOT_CONSULTED, VersionStatus.UNKNOWN)

    def test_fresh_collector_seeds_every_known_source_as_not_consulted(self):
        collector = RunProvenanceCollector()
        records = collector.to_list()
        self.assertEqual(len(records), len(KNOWN_SOURCES))
        self.assertTrue(all(r["status"] == "not_consulted" for r in records))

    def test_a_source_only_leaves_not_consulted_via_an_explicit_record_call(self):
        collector = RunProvenanceCollector()
        self.assertEqual(collector.get("ClinVar").status, VersionStatus.NOT_CONSULTED)
        collector.record("ClinVar", VersionStatus.UNKNOWN, notes="queried but no version available")
        self.assertEqual(collector.get("ClinVar").status, VersionStatus.UNKNOWN)
        # A source GEPER never touched (e.g. ClinGen disabled this run)
        # must remain NOT_CONSULTED, not silently drift to UNKNOWN.
        self.assertEqual(collector.get("Ensembl").status, VersionStatus.NOT_CONSULTED)


class TestDataSourceProvenanceRequiresStatus(unittest.TestCase):
    def test_status_is_required_not_defaulted(self):
        with self.assertRaises(Exception):
            DataSourceProvenance(source="ClinVar")  # missing required `status`

    def test_frozen(self):
        record = DataSourceProvenance(source="ClinVar", status=VersionStatus.UNKNOWN)
        with self.assertRaises(Exception):
            record.status = VersionStatus.VERSION_KNOWN


class TestRunProvenanceCollectorPriority(unittest.TestCase):
    """A later, degraded query must never downgrade an already-known-good record."""

    def test_version_known_is_not_overwritten_by_unknown(self):
        collector = RunProvenanceCollector()
        collector.record("gnomAD", VersionStatus.VERSION_KNOWN, version="gnomad_r4")
        collector.record("gnomAD", VersionStatus.UNKNOWN, notes="a later query failed")
        record = collector.get("gnomAD")
        self.assertEqual(record.status, VersionStatus.VERSION_KNOWN)
        self.assertEqual(record.version, "gnomad_r4")

    def test_unknown_is_upgraded_by_a_later_version_known(self):
        collector = RunProvenanceCollector()
        collector.record("gnomAD", VersionStatus.UNKNOWN, notes="first query failed")
        collector.record("gnomAD", VersionStatus.VERSION_KNOWN, version="gnomad_r4")
        self.assertEqual(collector.get("gnomAD").status, VersionStatus.VERSION_KNOWN)

    def test_equal_status_replaces_with_the_newer_record(self):
        collector = RunProvenanceCollector()
        collector.record("InterPro", VersionStatus.VERSION_KNOWN, version="InterPro 108.0")
        collector.record("InterPro", VersionStatus.VERSION_KNOWN, version="InterPro 109.0")
        self.assertEqual(collector.get("InterPro").version, "InterPro 109.0")

    def test_unrecognized_source_is_ignored_not_added(self):
        collector = RunProvenanceCollector()
        collector.record("Some Made Up Source", VersionStatus.VERSION_KNOWN, version="1.0")
        self.assertIsNone(collector.get("Some Made Up Source"))
        self.assertEqual(len(collector.to_list()), len(KNOWN_SOURCES))

    def test_to_list_is_alphabetically_sorted(self):
        collector = RunProvenanceCollector()
        sources = [r["source"] for r in collector.to_list()]
        self.assertEqual(sources, sorted(sources))


class TestFileHashingAndSidecar(unittest.TestCase):
    def test_compute_file_sha256_matches_known_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "f.txt")
            with open(path, "w") as fh:
                fh.write("hello world")
            # Known sha256("hello world")
            self.assertEqual(
                compute_file_sha256(path),
                "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9",
            )

    def test_sidecar_roundtrip_computes_hash_when_not_supplied(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "f.txt")
            with open(path, "w") as fh:
                fh.write("some content")
            record = write_dataset_provenance_sidecar(
                path, "https://example.com/f.txt",
                response_headers={"Last-Modified": "Mon, 01 Jan 2026 00:00:00 GMT", "ETag": '"abc"'},
            )
            self.assertIsNotNone(record["content_hash"])
            self.assertEqual(record["hash_algorithm"], "sha256")
            self.assertEqual(record["http_last_modified"], "Mon, 01 Jan 2026 00:00:00 GMT")

            reread = read_dataset_provenance_sidecar(path)
            self.assertEqual(reread["content_hash"], record["content_hash"])

    def test_sidecar_skips_local_hash_when_hash_supplied_and_compute_disabled(self):
        # Mirrors the AlphaMissense catalogue case: a GCS-provided ETag
        # is used directly, never re-hashed locally (would cost real
        # time/IO on a ~9GB file in production).
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "big.tsv.gz")
            with open(path, "w") as fh:
                fh.write("stand-in content")
            record = write_dataset_provenance_sidecar(
                path, "https://storage.googleapis.com/x", content_hash="deadbeef",
                hash_algorithm="gcs-etag-md5", compute_hash_from_file=False,
            )
            self.assertEqual(record["content_hash"], "deadbeef")
            self.assertEqual(record["hash_algorithm"], "gcs-etag-md5")

    def test_missing_sidecar_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(read_dataset_provenance_sidecar(os.path.join(tmp, "nope.txt")))

    def test_local_file_provenance_for_a_manually_configured_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "manual.csv")
            with open(path, "w") as fh:
                fh.write("a,b,c")
            record = local_file_provenance("ClinGen (gene validity)", path)
            self.assertEqual(record.status, VersionStatus.HASH_ONLY)
            self.assertIsNotNone(record.content_hash)

    def test_local_file_provenance_missing_file_is_unknown_not_a_crash(self):
        record = local_file_provenance("ClinGen (gene validity)", "/definitely/not/a/real/path.csv")
        self.assertEqual(record.status, VersionStatus.UNKNOWN)


class TestCodeVersionAndModelCheckpoints(unittest.TestCase):
    def test_get_geper_code_version_returns_a_string_never_raises(self):
        version = get_geper_code_version()
        self.assertIsInstance(version, str)
        self.assertGreater(len(version), 0)

    def test_get_geper_code_version_falls_back_honestly_outside_a_git_repo(self):
        with mock.patch("subprocess.run", side_effect=OSError("git not found")):
            version = get_geper_code_version()
        self.assertIn("unknown", version)

    def test_model_checkpoint_identifiers_always_includes_core_models(self):
        checkpoints = get_model_checkpoint_identifiers()
        self.assertIn("esm2", checkpoints)
        self.assertIn("rna_fm", checkpoints)
        self.assertIn("evo2_variant", checkpoints)


class TestEnsembleAndBlastCaptureHelpers(unittest.TestCase):
    def test_capture_ensembl_release_parses_the_real_response_shape(self):
        fake_response = mock.Mock()
        fake_response.raise_for_status = mock.Mock()
        fake_response.json.return_value = {"releases": [116]}
        with mock.patch("requests.get", return_value=fake_response):
            result = capture_ensembl_release()
        self.assertEqual(result["version"], "Ensembl release 116")
        self.assertIsNone(result["error"])

    def test_capture_ensembl_release_never_raises_on_failure(self):
        with mock.patch("requests.get", side_effect=Exception("connection refused")):
            result = capture_ensembl_release()
        self.assertIsNone(result["version"])
        self.assertIn("connection refused", result["error"])

    def test_capture_blast_local_tool_versions_wraps_existing_helper(self):
        with mock.patch("database.blast_client.get_blast_tool_versions", return_value={"blastn": "2.16.0+"}):
            result = capture_blast_local_tool_versions()
        self.assertEqual(result["version"], "blastn 2.16.0+")

    def test_capture_blast_local_tool_versions_no_tools_is_not_an_error(self):
        with mock.patch("database.blast_client.get_blast_tool_versions", return_value={"blastn": None, "blastp": "not found on PATH"}):
            result = capture_blast_local_tool_versions()
        self.assertIsNone(result["version"])
        self.assertIsNone(result["error"])


class TestOrchestratorStageProvenanceCapture(unittest.TestCase):
    """
    Exercises `GeperPipeline._capture_stage_provenance` via a duck-typed
    `self` (just `provenance`/`sequence_context_gen.assembly`/`ai_only`)
    rather than constructing a real `GeperPipeline` -- that requires
    loading model registries, out of scope for a lightweight unit test.
    """

    def _fake_self(self):
        from pipeline.provenance import RunProvenanceCollector
        return SimpleNamespace(
            provenance=RunProvenanceCollector(),
            sequence_context_gen=SimpleNamespace(assembly="GRCh38"),
            ai_only=False,
        )

    def _capture(self, fake_self, **kwargs):
        from pipeline.orchestrator import GeperPipeline
        defaults = dict(
            clinvar_result=None, dbsnp_result=None, gnomad_result=None,
            uniprot_result=None, interpro_result=None, alphafold_result=None,
            functional_evidence_result=None,
        )
        defaults.update(kwargs)
        GeperPipeline._capture_stage_provenance(fake_self, **defaults)

    def test_gnomad_dataset_id_is_the_version(self):
        fake_self = self._fake_self()
        self._capture(fake_self, gnomad_result={"found": True, "skipped": False})
        record = fake_self.provenance.get("gnomAD")
        self.assertEqual(record.status, VersionStatus.VERSION_KNOWN)
        self.assertEqual(record.version, "gnomad_r4")

    def test_clinvar_has_no_source_wide_version_but_is_consulted(self):
        fake_self = self._fake_self()
        self._capture(fake_self, clinvar_result={"found": True, "records": []})
        record = fake_self.provenance.get("ClinVar")
        self.assertEqual(record.status, VersionStatus.TIMESTAMP_ONLY)
        self.assertIsNotNone(record.notes)

    def test_clinvar_query_failure_is_unknown_not_timestamp_only(self):
        fake_self = self._fake_self()
        self._capture(fake_self, clinvar_result={"error": "NCBI E-utilities request failed"})
        self.assertEqual(fake_self.provenance.get("ClinVar").status, VersionStatus.UNKNOWN)

    def test_dbsnp_build_is_the_version(self):
        fake_self = self._fake_self()
        self._capture(fake_self, dbsnp_result={"found": True, "detail": {"dbsnp_build": "157"}})
        record = fake_self.provenance.get("dbSNP")
        self.assertEqual(record.status, VersionStatus.VERSION_KNOWN)
        self.assertIn("157", record.version)

    def test_uniprot_release_header_is_the_version(self):
        fake_self = self._fake_self()
        self._capture(fake_self, uniprot_result={"found": True, "skipped": False, "release": "2026_02", "release_date": "10-June-2026"})
        record = fake_self.provenance.get("UniProt")
        self.assertEqual(record.status, VersionStatus.VERSION_KNOWN)
        self.assertIn("2026_02", record.version)
        self.assertEqual(record.release_date, "10-June-2026")

    def test_interpro_version_header_is_the_version(self):
        fake_self = self._fake_self()
        self._capture(fake_self, interpro_result={"found": True, "skipped": False, "api_version": "109.0"})
        record = fake_self.provenance.get("InterPro")
        self.assertEqual(record.status, VersionStatus.VERSION_KNOWN)
        self.assertIn("109.0", record.version)

    def test_alphafold_model_version_is_the_version(self):
        fake_self = self._fake_self()
        self._capture(fake_self, alphafold_result={"found": True, "skipped": False, "model_version": "4"})
        record = fake_self.provenance.get("AlphaFold DB")
        self.assertEqual(record.status, VersionStatus.VERSION_KNOWN)

    def test_functional_evidence_source_routes_to_the_right_record(self):
        fake_self = self._fake_self()
        self._capture(fake_self, functional_evidence_result={"found": True, "source": "mavedb"})
        self.assertEqual(fake_self.provenance.get("Functional evidence (MaveDB)").status, VersionStatus.TIMESTAMP_ONLY)
        self.assertEqual(fake_self.provenance.get("Functional evidence (ClinGen ERepo)").status, VersionStatus.NOT_CONSULTED)

    def test_a_stage_result_left_untouched_stays_not_consulted(self):
        fake_self = self._fake_self()
        self._capture(fake_self, gnomad_result={"found": True, "skipped": False})
        # Nothing passed for ClinGen ERepo/MaveDB/InterPro/etc.
        self.assertEqual(fake_self.provenance.get("Functional evidence (MaveDB)").status, VersionStatus.NOT_CONSULTED)
        self.assertEqual(fake_self.provenance.get("InterPro").status, VersionStatus.NOT_CONSULTED)

    def test_never_raises_on_malformed_results(self):
        fake_self = self._fake_self()
        # Deliberately malformed shapes -- must not propagate an exception.
        self._capture(
            fake_self,
            clinvar_result="not-a-dict",
            dbsnp_result={"detail": "also-not-a-dict"},
            gnomad_result={"skipped": False},
        )
        # No assertion beyond "didn't raise" -- this is the contract test.


if __name__ == "__main__":
    unittest.main()
