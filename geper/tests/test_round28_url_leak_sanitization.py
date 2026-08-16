"""
Round 28: closes out the raw-URL/local-path-into-raised-error leak
class round 27's audit found alive in six more modules beyond
`pipeline/ps1_pm5/lookup.py` (fixed round 27) and
`database/clinvar_client.py` (fixed round 26) -- same shape rounds
20-27 already fixed for SpliceBERT, MMSplice, UniProt, InterPro,
ClinGen, AlphaFold DB, ClinVar client, and the PS1/PM5 ClinVar-codon
lookup: full detail to `logger.warning`, a short honest message raised,
no URL/local-path/raw-exception-text in what a report can render.

Each provider below has its own `errors.append(...)` reach into
`pipeline/orchestrator.py`'s shared `errors` list, rendered
unconditionally into every Markdown report's "### ⚠ Stage Warnings /
Errors" section by `report/report_generator.py`, and/or its own
`_error`/`error` key embedded verbatim in `geper_results.json` by
`report/json_builder.py` -- traced per-module in `ROUND_CANDIDATES.md`'s
round 27/28 entries, not re-traced here; these tests only verify the
sanitization itself.

`database/blast_client.py` is a distinct sub-class: local BLAST leaks
a filesystem path (`self.local_db_path`) via subprocess stderr/timeout
text, not a bare request URL. Same fix shape (log the detail, raise a
short message), different sensitive substring asserted against.
"""

import unittest
from unittest import mock

from utils.exceptions import ExternalAPIError


class TestDbSNPClientIsSanitized(unittest.TestCase):
    def test_retry_exhausted_raises_sanitized_message(self):
        import requests as real_requests

        from database.dbsnp_client import DbSNPClient

        client = DbSNPClient()
        with (
            mock.patch(
                "database.dbsnp_client.requests.get",
                side_effect=real_requests.ConnectionError("Failed to establish a new connection"),
            ),
            mock.patch("database.dbsnp_client.time.sleep"),
        ):
            with self.assertRaises(ExternalAPIError) as ctx:
                client._esearch("17[chr] AND 7674221[chrpos38]")

        message = str(ctx.exception)
        self.assertNotIn("http", message)
        self.assertNotIn("eutils.ncbi.nlm.nih.gov", message)
        self.assertNotIn("Failed to establish a new connection", message)
        self.assertEqual(message, "dbSNP request failed after 3 attempts")

    def test_offline_skip_raises_sanitized_message(self):
        from database.dbsnp_client import DbSNPClient

        client = DbSNPClient()
        with mock.patch("database.dbsnp_client.HEALTH") as fake_health:
            fake_health.is_offline.return_value = True
            with self.assertRaises(ExternalAPIError) as ctx:
                client._esearch("17[chr] AND 7674221[chrpos38]")

        message = str(ctx.exception)
        self.assertNotIn("http", message)
        self.assertEqual(message, "dbSNP request skipped: dbSNP was confirmed offline at startup.")


class TestUCSCConservationProviderIsSanitized(unittest.TestCase):
    def test_retry_exhausted_raises_sanitized_message(self):
        import requests as real_requests

        from pipeline.conservation.provider import UCSCApiProvider

        provider = UCSCApiProvider(score_type="phylop")
        with (
            mock.patch(
                "pipeline.conservation.provider.requests.get",
                side_effect=real_requests.ConnectionError("Failed to establish a new connection"),
            ),
            mock.patch("pipeline.conservation.provider.time.sleep"),
        ):
            with self.assertRaises(ExternalAPIError) as ctx:
                provider._get("hg38", "phyloP100way", "chr17", 43094297, 43094298)

        message = str(ctx.exception)
        self.assertNotIn("http", message)
        self.assertNotIn("api.genome.ucsc.edu", message)
        self.assertNotIn("Failed to establish a new connection", message)
        self.assertEqual(message, "UCSC conservation API request failed after 3 attempts")


class TestMyVariantGerpProviderIsSanitized(unittest.TestCase):
    def test_retry_exhausted_raises_sanitized_message(self):
        import requests as real_requests

        from pipeline.conservation.provider import MyVariantGerpProvider

        provider = MyVariantGerpProvider()
        with (
            mock.patch(
                "pipeline.conservation.provider.requests.get",
                side_effect=real_requests.ConnectionError("Failed to establish a new connection"),
            ),
            mock.patch("pipeline.conservation.provider.time.sleep"),
        ):
            with self.assertRaises(ExternalAPIError) as ctx:
                provider._get("chr17:g.7674221G>A", {"fields": "dbnsfp.gerp"})

        message = str(ctx.exception)
        self.assertNotIn("http", message)
        self.assertNotIn("myvariant.info", message)
        self.assertNotIn("Failed to establish a new connection", message)
        self.assertEqual(message, "MyVariant.info GERP request failed after 3 attempts")


class TestGnomadGraphQLProviderIsSanitized(unittest.TestCase):
    def test_retry_exhausted_raises_sanitized_message(self):
        import requests as real_requests

        from pipeline.gnomad.provider import GraphQLGnomadProvider

        provider = GraphQLGnomadProvider()
        with (
            mock.patch(
                "pipeline.gnomad.provider.requests.post",
                side_effect=real_requests.ConnectionError("Failed to establish a new connection"),
            ),
            mock.patch("pipeline.gnomad.provider.time.sleep"),
        ):
            with self.assertRaises(ExternalAPIError) as ctx:
                provider._post("17-43094298-A-C", "gnomad_r4")

        message = str(ctx.exception)
        self.assertNotIn("http", message)
        self.assertNotIn("gnomad.broadinstitute.org", message)
        self.assertNotIn("Failed to establish a new connection", message)
        self.assertEqual(message, "gnomAD GraphQL request failed after 3 attempts")


class TestHPOProviderIsSanitized(unittest.TestCase):
    def test_retry_exhausted_raises_sanitized_message(self):
        import requests as real_requests

        from pipeline.hpo.provider import LiveAPIHPOProvider

        provider = LiveAPIHPOProvider()
        with (
            mock.patch(
                "pipeline.hpo.provider.requests.get",
                side_effect=real_requests.ConnectionError("Failed to establish a new connection"),
            ),
            mock.patch("pipeline.hpo.provider.time.sleep"),
        ):
            with self.assertRaises(ExternalAPIError) as ctx:
                provider._get_json(f"{provider.endpoint}/network/search/GENE", {"q": "TP53"})

        message = str(ctx.exception)
        self.assertNotIn("http", message)
        self.assertNotIn("Failed to establish a new connection", message)
        self.assertEqual(message, "HPO API request failed after 3 attempts")


class TestMaveDBProviderIsSanitized(unittest.TestCase):
    def test_search_score_sets_offline_skip_raises_sanitized_message(self):
        from pipeline.functional_evidence.mavedb_provider import MaveDBFunctionalEvidenceProvider

        provider = MaveDBFunctionalEvidenceProvider()
        with mock.patch("pipeline.functional_evidence.mavedb_provider.HEALTH") as fake_health:
            fake_health.is_offline.return_value = True
            with self.assertRaises(ExternalAPIError) as ctx:
                provider._search_score_sets("TP53")

        message = str(ctx.exception)
        self.assertNotIn("http", message)
        self.assertEqual(message, "MaveDB search request skipped: MaveDB was confirmed offline at startup.")

    def test_search_score_sets_retry_exhausted_raises_sanitized_message(self):
        import requests as real_requests

        from pipeline.functional_evidence.mavedb_provider import MaveDBFunctionalEvidenceProvider

        provider = MaveDBFunctionalEvidenceProvider()
        with (
            mock.patch("pipeline.functional_evidence.mavedb_provider.HEALTH") as fake_health,
            mock.patch(
                "pipeline.functional_evidence.mavedb_provider.requests.post",
                side_effect=real_requests.ConnectionError("Failed to establish a new connection"),
            ),
            mock.patch("pipeline.functional_evidence.mavedb_provider.time.sleep"),
        ):
            fake_health.is_offline.return_value = False
            with self.assertRaises(ExternalAPIError) as ctx:
                provider._search_score_sets("TP53")

        message = str(ctx.exception)
        self.assertNotIn("http", message)
        self.assertNotIn("Failed to establish a new connection", message)
        self.assertEqual(message, "MaveDB search request failed after 3 attempts")

    def test_get_retry_exhausted_raises_sanitized_message(self):
        import requests as real_requests

        from pipeline.functional_evidence.mavedb_provider import MaveDBFunctionalEvidenceProvider

        provider = MaveDBFunctionalEvidenceProvider()
        with (
            mock.patch("pipeline.functional_evidence.mavedb_provider.HEALTH") as fake_health,
            mock.patch(
                "pipeline.functional_evidence.mavedb_provider.requests.get",
                side_effect=real_requests.ConnectionError("Failed to establish a new connection"),
            ),
            mock.patch("pipeline.functional_evidence.mavedb_provider.time.sleep"),
        ):
            fake_health.is_offline.return_value = False
            with self.assertRaises(ExternalAPIError) as ctx:
                provider._get(f"{provider.endpoint}/score-sets/urn:mavedb:00000001-a-1")

        message = str(ctx.exception)
        self.assertNotIn("http", message)
        self.assertNotIn("Failed to establish a new connection", message)
        self.assertEqual(message, "MaveDB request failed after 3 attempts")


class TestErepoProviderIsSanitized(unittest.TestCase):
    def test_retry_exhausted_raises_sanitized_message(self):
        import requests as real_requests

        from pipeline.functional_evidence.erepo_provider import ErepoFunctionalEvidenceProvider

        provider = ErepoFunctionalEvidenceProvider()
        with (
            mock.patch("pipeline.functional_evidence.erepo_provider.HEALTH") as fake_health,
            mock.patch(
                "pipeline.functional_evidence.erepo_provider.requests.get",
                side_effect=real_requests.ConnectionError("Failed to establish a new connection"),
            ),
            mock.patch("pipeline.functional_evidence.erepo_provider.time.sleep"),
        ):
            fake_health.is_offline.return_value = False
            with self.assertRaises(ExternalAPIError) as ctx:
                provider._get("TP53")

        message = str(ctx.exception)
        self.assertNotIn("http", message)
        self.assertNotIn("Failed to establish a new connection", message)
        self.assertEqual(message, "ClinGen ERepo request failed after 3 attempts")


class TestSequenceContextGeneratorIsSanitized(unittest.TestCase):
    def test_retry_exhausted_raises_sanitized_message_but_keeps_the_region(self):
        import requests as real_requests

        from pipeline.sequence_context import SequenceContextGenerator

        gen = SequenceContextGenerator(species="human", assembly="GRCh38")
        with (
            mock.patch.object(
                gen._session,
                "get",
                side_effect=real_requests.ConnectionError("Failed to establish a new connection"),
            ),
            mock.patch("pipeline.sequence_context.time.sleep"),
        ):
            with self.assertRaises(ExternalAPIError) as ctx:
                gen._fetch_region("17", 43094297, 43094298)

        message = str(ctx.exception)
        # `region` (bare genomic coordinates) is not sensitive and is
        # intentionally kept -- only `last_error` is sanitized.
        self.assertIn("17:43094297-43094298", message)
        self.assertNotIn("Failed to establish a new connection", message)
        self.assertNotIn("rest.ensembl.org", message)


class TestBlastClientLocalSearchIsSanitized(unittest.TestCase):
    def test_called_process_error_hides_local_db_path(self):
        import subprocess

        from database.blast_client import BLASTClient

        client = BLASTClient(mode="local", local_db_path="/secret/local/blast_db/nt", disabled=False)
        stderr = (
            "BLAST Database error: No alias or index file found for nucleotide database [/secret/local/blast_db/nt]"
        )
        with mock.patch(
            "database.blast_client.subprocess.run",
            side_effect=subprocess.CalledProcessError(returncode=2, cmd=["blastn"], stderr=stderr),
        ):
            with self.assertRaises(ExternalAPIError) as ctx:
                client._search_local("ACGT", "blastn", 10)

        message = str(ctx.exception)
        self.assertNotIn("/secret/local/blast_db/nt", message)
        self.assertNotIn(stderr, message)
        self.assertEqual(message, "Local BLAST search failed (exit code 2).")

    def test_timeout_hides_local_db_path(self):
        import subprocess

        from database.blast_client import BLASTClient

        client = BLASTClient(mode="local", local_db_path="/secret/local/blast_db/nt", disabled=False)
        with mock.patch(
            "database.blast_client.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["blastn", "-db", "/secret/local/blast_db/nt"], timeout=120),
        ):
            with self.assertRaises(ExternalAPIError) as ctx:
                client._search_local("ACGT", "blastn", 10)

        message = str(ctx.exception)
        self.assertNotIn("/secret/local/blast_db/nt", message)
        self.assertIn("timed out", message)


class TestBlastClientRemoteSearchIsSanitized(unittest.TestCase):
    def test_retry_exhausted_raises_sanitized_message(self):
        from database.blast_client import BLASTClient

        client = BLASTClient(mode="remote", disabled=False)
        with (
            mock.patch("database.blast_client.ensure_pip_package_available", return_value=True),
            mock.patch(
                "Bio.Blast.NCBIWWW.qblast", side_effect=ConnectionError("Failed to reach blast.ncbi.nlm.nih.gov")
            ),
            mock.patch("database.blast_client.time.sleep"),
        ):
            with self.assertRaises(ExternalAPIError) as ctx:
                client._search_remote("ACGT", "blastn", "nt", 10)

        message = str(ctx.exception)
        self.assertNotIn("blast.ncbi.nlm.nih.gov", message)
        self.assertEqual(message, "Remote BLAST failed after 3 attempts")


if __name__ == "__main__":
    unittest.main()
