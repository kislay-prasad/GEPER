"""
Packaging Part 3, mechanism (see `PACKAGING_OFFLINE_DATABASE_CACHES.md`):
a manifest naming each bundled source's version/checksum/date-fetched, a
staleness signal that reaches the report (not just the log), and a
checksum-verified update path for an air-gapped site.

Covers the three parts named in the dispatch:

  1. `pipeline.database_manifest.build_manifest`/`verify_manifest` --
     one entry per known cache source with version/checksum/date
     fetched, and a verifier that is shown REJECTING a corrupted file
     (break-it-on-purpose).
  2. Staleness reaching the report: every "Using stale cached ... after
     a failed refresh" call site now also calls
     `pipeline.provenance.record_stale_fallback`, and that reaches
     `JSONResultBuilder`'s document and `ReportGenerator`'s rendered
     text -- not just the log line the seven bootstrap modules already
     had.
  3. The update path: `apply_verified_update` accepts a new snapshot
     only when its checksum matches what the caller expects, and is
     shown REJECTING a tampered one, leaving the existing cache file
     untouched.
"""

import hashlib
import os
import tempfile
import unittest
from unittest import mock

from pipeline import provenance
from pipeline.provenance import get_stale_fallbacks, record_stale_fallback, reset_stale_fallbacks
from report.json_builder import JSONResultBuilder


class TestStaleFallbackRegistry(unittest.TestCase):
    def setUp(self):
        reset_stale_fallbacks()

    def test_empty_by_default(self):
        self.assertEqual(get_stale_fallbacks(), [])

    def test_record_stale_fallback_is_visible_to_readers(self):
        record_stale_fallback(source="HPO", path="/x/y.tsv", reason="failed refresh")
        fallbacks = get_stale_fallbacks()
        self.assertEqual(len(fallbacks), 1)
        self.assertEqual(fallbacks[0]["source"], "HPO")
        self.assertEqual(fallbacks[0]["reason"], "failed refresh")
        self.assertIn("recorded_at", fallbacks[0])


class TestStalenessReachesEachBootstrapModule(unittest.TestCase):
    """
    The seven call sites named in the dispatch. Each must call
    `record_stale_fallback` on the exact branch that logs "Using stale
    cached ... after a failed refresh" -- proven by driving each real
    `_ensure`-shaped function through a failed refresh with an existing
    stale file present, not by asserting the source code contains a
    particular line.
    """

    def setUp(self):
        reset_stale_fallbacks()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)

    def _stale_file(self, name="stale.dat"):
        path = os.path.join(self._tmpdir.name, name)
        with open(path, "wb") as fh:
            fh.write(b"stale-bytes")
        return path

    def test_clingen_gene_validity_stale_fallback_reaches_registry(self):
        from pipeline.clingen import bootstrap as clingen_bootstrap

        dest_path = self._stale_file()
        with (
            mock.patch.object(clingen_bootstrap, "_is_fresh", return_value=False),
            mock.patch.object(clingen_bootstrap, "_download", return_value=False),
            mock.patch.object(clingen_bootstrap, "_cache_dir", return_value=self._tmpdir.name),
        ):
            result = clingen_bootstrap._ensure(
                "https://example.invalid/x", os.path.basename(dest_path), "gene validity"
            )

        self.assertEqual(result, dest_path)
        sources = [f["source"] for f in get_stale_fallbacks()]
        self.assertTrue(any("ClinGen" in s for s in sources), sources)

    def test_hpo_genes_to_phenotype_stale_fallback_reaches_registry(self):
        from pipeline.hpo import bootstrap as hpo_bootstrap

        dest_path = self._stale_file("genes_to_phenotype.txt")
        with (
            mock.patch.object(hpo_bootstrap, "_is_fresh", return_value=False),
            mock.patch.object(hpo_bootstrap, "_download", return_value=False),
            mock.patch.object(hpo_bootstrap, "_cache_dir", return_value=self._tmpdir.name),
            mock.patch.object(hpo_bootstrap, "_CACHE_FILENAME", os.path.basename(dest_path)),
        ):
            result = hpo_bootstrap.ensure_genes_to_phenotype_file()

        self.assertEqual(result, dest_path)
        sources = [f["source"] for f in get_stale_fallbacks()]
        self.assertTrue(any("HPO" in s for s in sources), sources)

    def test_hpo_ontology_stale_fallback_reaches_registry(self):
        from pipeline.hpo import ontology as hpo_ontology

        dest_path = self._stale_file("hp.json")
        with (
            mock.patch.object(hpo_ontology, "_is_fresh", return_value=False),
            mock.patch.object(hpo_ontology, "_download", return_value=False),
            mock.patch.object(hpo_ontology, "ontology_cache_path", return_value=dest_path),
        ):
            result = hpo_ontology.ensure_ontology_file()

        self.assertEqual(result, dest_path)
        sources = [f["source"] for f in get_stale_fallbacks()]
        self.assertTrue(any("HPO" in s for s in sources), sources)

    def test_mane_stale_fallback_reaches_registry(self):
        from pipeline.mane import bootstrap as mane_bootstrap

        dest_path = self._stale_file("mane_summary.txt")
        with (
            mock.patch.object(mane_bootstrap, "_is_fresh", return_value=False),
            mock.patch.object(mane_bootstrap, "_discover_current_summary_url", return_value=(None, None)),
            mock.patch.object(mane_bootstrap, "summary_cache_path", return_value=dest_path),
        ):
            result = mane_bootstrap.ensure_summary_file()

        self.assertEqual(result, dest_path)
        sources = [f["source"] for f in get_stale_fallbacks()]
        self.assertTrue(any("MANE" in s for s in sources), sources)

    def test_orphanet_stale_fallback_reaches_registry(self):
        from pipeline.orphanet import bootstrap as orphanet_bootstrap

        dest_path = self._stale_file("en_product6.xml")
        with (
            mock.patch.object(orphanet_bootstrap, "_is_fresh", return_value=False),
            mock.patch.object(orphanet_bootstrap, "_download", return_value=False),
            mock.patch.object(orphanet_bootstrap, "_cache_dir", return_value=self._tmpdir.name),
            mock.patch.object(orphanet_bootstrap, "_CACHE_FILENAME", os.path.basename(dest_path)),
        ):
            result = orphanet_bootstrap.ensure_gene_disorder_file()

        self.assertEqual(result, dest_path)
        sources = [f["source"] for f in get_stale_fallbacks()]
        self.assertTrue(any("Orphanet" in s for s in sources), sources)

    def test_uniprot_stale_fallback_reaches_registry(self):
        from pipeline.uniprot import bootstrap as uniprot_bootstrap

        dest_path = self._stale_file("uniprot_sprot.dat")
        with (
            mock.patch.object(uniprot_bootstrap, "_is_fresh", return_value=False),
            mock.patch.object(uniprot_bootstrap, "_download_and_convert", return_value=False),
            mock.patch.object(uniprot_bootstrap, "_discover_version", return_value=None),
            mock.patch.object(uniprot_bootstrap, "dataset_cache_path", return_value=dest_path),
        ):
            result = uniprot_bootstrap.ensure_dataset_file()

        self.assertEqual(result, dest_path)
        sources = [f["source"] for f in get_stale_fallbacks()]
        self.assertTrue(any("UniProt" in s for s in sources), sources)

    def test_ensembl_stale_fallback_reaches_registry(self):
        from pipeline.ensembl import bootstrap as ensembl_bootstrap

        dest_path = self._stale_file("ensembl_dataset.gtf")
        with (
            mock.patch.object(ensembl_bootstrap, "_is_fresh", return_value=False),
            mock.patch.object(ensembl_bootstrap, "_discover_current_gtf_url", return_value=(None, None)),
            mock.patch.object(ensembl_bootstrap, "dataset_cache_path", return_value=dest_path),
        ):
            result = ensembl_bootstrap.ensure_dataset_file()

        self.assertEqual(result, dest_path)
        sources = [f["source"] for f in get_stale_fallbacks()]
        self.assertTrue(any("Ensembl" in s for s in sources), sources)


class TestStalenessReachesJSONDocumentAndReport(unittest.TestCase):
    """
    The acceptance criterion, verbatim: 'the reader needs to know an
    interpretation was built on a snapshot from a date, not just that
    one exists.' Checked at both hops -- the JSON document AND the
    rendered report text.
    """

    def setUp(self):
        reset_stale_fallbacks()
        self.addCleanup(reset_stale_fallbacks)

    def test_json_document_carries_no_warnings_when_none_recorded(self):
        builder = JSONResultBuilder(input_vcf_path="x.vcf")
        doc = builder.build()
        self.assertEqual(doc.get("data_freshness_warnings"), [])

    def test_json_document_surfaces_recorded_stale_fallback(self):
        record_stale_fallback(source="ClinGen (gene validity)", path="/cache/x.csv", reason="failed refresh")
        builder = JSONResultBuilder(input_vcf_path="x.vcf")
        doc = builder.build()
        warnings = doc.get("data_freshness_warnings")
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["source"], "ClinGen (gene validity)")

    def test_report_renders_data_freshness_warning_with_date(self):
        from report.report_generator import ReportGenerator

        record_stale_fallback(source="ClinGen (gene validity)", path="/cache/x.csv", reason="failed refresh")
        builder = JSONResultBuilder(input_vcf_path="x.vcf")
        doc = builder.build()

        lines = ReportGenerator._render_data_freshness_warnings(doc)
        text = "\n".join(lines)
        self.assertIn("ClinGen (gene validity)", text)
        self.assertIn("failed refresh", text)
        # The date it was recorded stale must reach the report -- the
        # dispatch's acceptance criterion is "a snapshot from a date,
        # not just that one exists".
        recorded_at = doc["data_freshness_warnings"][0]["recorded_at"]
        self.assertIn(recorded_at, text)


class TestDatabaseManifest(unittest.TestCase):
    """Part 1: version/checksum/date-fetched, per source."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)

    def _write_fixture_dataset(self, name, content=b"fixture-bytes"):
        path = os.path.join(self._tmpdir.name, name)
        with open(path, "wb") as fh:
            fh.write(content)
        return path

    def test_manifest_entry_names_version_checksum_and_date_fetched(self):
        from pipeline.database_manifest import build_manifest_entry

        path = self._write_fixture_dataset("hpo.tsv")
        provenance.write_dataset_provenance_sidecar(
            path, "https://example.invalid/hpo.tsv", version=None, release_date=None
        )
        entry = build_manifest_entry("HPO", configured_local_file="", auto_fetch_path=path)
        self.assertEqual(entry["source"], "HPO")
        self.assertEqual(entry["checksum"], provenance.compute_file_sha256(path))
        self.assertIsNotNone(entry["date_fetched"])

    def test_manifest_entry_is_honest_about_an_absent_file(self):
        from pipeline.database_manifest import build_manifest_entry

        missing_path = os.path.join(self._tmpdir.name, "does_not_exist.tsv")
        entry = build_manifest_entry("HPO", configured_local_file="", auto_fetch_path=missing_path)
        self.assertEqual(entry["status"], "absent")
        self.assertIsNone(entry["checksum"])
        self.assertIsNone(entry["version"])

    def test_verify_manifest_accepts_an_untampered_file(self):
        from pipeline.database_manifest import build_manifest_entry, verify_manifest_entry

        path = self._write_fixture_dataset("clean.tsv")
        entry = build_manifest_entry("HPO", configured_local_file="", auto_fetch_path=path)
        problem = verify_manifest_entry(entry, auto_fetch_path=path)
        self.assertIsNone(problem)

    def test_verify_manifest_rejects_a_corrupted_file(self):
        """Break it on purpose: corrupt the file after manifesting it and
        show the verifier refuses, by name, not merely returning False."""
        from pipeline.database_manifest import build_manifest_entry, verify_manifest_entry

        path = self._write_fixture_dataset("corruptme.tsv")
        entry = build_manifest_entry("HPO", configured_local_file="", auto_fetch_path=path)
        with open(path, "wb") as fh:
            fh.write(b"TAMPERED-CONTENT-DOES-NOT-MATCH-MANIFEST")

        problem = verify_manifest_entry(entry, auto_fetch_path=path)
        self.assertIsNotNone(problem)
        self.assertIn("HPO", problem)
        self.assertIn("checksum", problem.lower())


class TestVerifiedUpdatePath(unittest.TestCase):
    """Part 3: how a new snapshot reaches an air-gapped site, verified
    before use."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)

    def test_matching_checksum_is_applied(self):
        from pipeline.database_manifest import apply_verified_update

        dest_path = os.path.join(self._tmpdir.name, "dataset.tsv")
        with open(dest_path, "wb") as fh:
            fh.write(b"old-content")

        new_snapshot = os.path.join(self._tmpdir.name, "incoming.tsv")
        with open(new_snapshot, "wb") as fh:
            fh.write(b"new-content-from-a-verified-vendor-snapshot")
        expected_checksum = hashlib.sha256(open(new_snapshot, "rb").read()).hexdigest()

        result = apply_verified_update(
            source="HPO",
            new_file_path=new_snapshot,
            expected_checksum=expected_checksum,
            dest_path=dest_path,
        )

        self.assertTrue(result.accepted)
        with open(dest_path, "rb") as fh:
            self.assertEqual(fh.read(), b"new-content-from-a-verified-vendor-snapshot")

    def test_tampered_snapshot_is_rejected_and_dest_left_untouched(self):
        """Break it on purpose: hand it a snapshot whose bytes don't match
        the expected checksum and show it declines."""
        from pipeline.database_manifest import ChecksumMismatchError, apply_verified_update

        dest_path = os.path.join(self._tmpdir.name, "dataset.tsv")
        with open(dest_path, "wb") as fh:
            fh.write(b"old-content-must-survive")

        tampered_snapshot = os.path.join(self._tmpdir.name, "tampered.tsv")
        with open(tampered_snapshot, "wb") as fh:
            fh.write(b"attacker-supplied-content")
        wrong_checksum = hashlib.sha256(b"what-the-vendor-actually-published").hexdigest()

        with self.assertRaises(ChecksumMismatchError):
            apply_verified_update(
                source="HPO",
                new_file_path=tampered_snapshot,
                expected_checksum=wrong_checksum,
                dest_path=dest_path,
            )

        with open(dest_path, "rb") as fh:
            self.assertEqual(fh.read(), b"old-content-must-survive")


if __name__ == "__main__":
    unittest.main()
