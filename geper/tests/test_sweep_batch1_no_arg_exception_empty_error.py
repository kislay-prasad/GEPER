"""
21-site str(exc) sweep, Batch 1: the confirmed-negative reader-facing
pair sites (ClinGen, UniProt, InterPro, AlphaFold), the two sites with
no provider.py/orchestrator.py split (BLAST, Transcript-structure), and
the file-level 1000-Genomes-SAS site -- all previously bare `str(exc)`,
now `detail = str(exc) or type(exc).__name__`, one variable feeding
both the log line and the returned field, same shape as d1128ee
(gnomAD) and FIX #10 (dbSNP/ClinVar).

Every pair (provider.py primary path + orchestrator.py defense-in-depth
wrapper for the SAME logical source) is tested at BOTH sites, since
fixing either alone leaves the other still producing the empty string
for the case it catches.

WHAT EVERY TEST BELOW ASSERTS
  A. THE DANGEROUS CASE (red-first): a no-argument exception
     (`ExternalAPIError()`) must not produce an empty `error` string.
     Pre-fix, every one of these fails because `error` comes back "".
  B. THE CONTROL: an exception raised WITH a real message is still
     reported VERBATIM, unchanged -- proving the fix SUPPLIES a message
     when there wasn't one rather than REWRITING every message. Without
     this control, a fix that clobbered every real message with
     `type(exc).__name__` would look identical to a correct one.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from utils.exceptions import ExternalAPIError


def _variant():
    return SimpleNamespace(chrom="17", pos=43057051, ref="A", alt="T", build="GRCh38", gene="BRCA1")


def _pipeline():
    from pipeline.orchestrator import GeperPipeline

    p = GeperPipeline.__new__(GeperPipeline)
    p.enable_profiling = False
    p.sequence_context_gen = SimpleNamespace(assembly="GRCh38")
    return p


# ---------------------------------------------------------------------
# Provider.py primary-path sites
# ---------------------------------------------------------------------


class TestClinGenProviderNoArgException(unittest.TestCase):
    def _query(self, side_effect):
        from pipeline.clingen.provider import LiveAPIClinGenProvider

        provider = LiveAPIClinGenProvider()
        with (
            mock.patch.object(LiveAPIClinGenProvider, "is_available", return_value=True),
            mock.patch.object(LiveAPIClinGenProvider, "_get", side_effect=side_effect),
        ):
            return provider.query("BRCA1")

    def test_no_arg_exception_error_is_not_empty(self):
        result = self._query(ExternalAPIError())
        self.assertFalse(result.found)
        self.assertNotEqual(result.error, "", "a no-argument ExternalAPIError collapsed to an empty error string")

    def test_a_real_message_is_still_reported_verbatim(self):
        result = self._query(ExternalAPIError("ClinGen API returned 503"))
        self.assertEqual(result.error, "ClinGen API returned 503")


class TestUniProtProviderNoArgException(unittest.TestCase):
    def _query(self, side_effect):
        from pipeline.uniprot.provider import LiveAPIUniProtProvider

        provider = LiveAPIUniProtProvider()
        with (
            mock.patch.object(LiveAPIUniProtProvider, "is_available", return_value=True),
            mock.patch.object(LiveAPIUniProtProvider, "_get", side_effect=side_effect),
        ):
            return provider.query("BRCA1")

    def test_no_arg_exception_error_is_not_empty(self):
        result = self._query(ExternalAPIError())
        self.assertFalse(result.found)
        self.assertNotEqual(result.error, "", "a no-argument ExternalAPIError collapsed to an empty error string")

    def test_a_real_message_is_still_reported_verbatim(self):
        result = self._query(ExternalAPIError("UniProt REST API timed out"))
        self.assertEqual(result.error, "UniProt REST API timed out")


class TestInterProProviderNoArgException(unittest.TestCase):
    def _query(self, side_effect):
        from pipeline.interpro.provider import LiveAPIInterProProvider

        provider = LiveAPIInterProProvider()
        with (
            mock.patch.object(LiveAPIInterProProvider, "is_available", return_value=True),
            mock.patch.object(LiveAPIInterProProvider, "_get", side_effect=side_effect),
        ):
            return provider.query("P38398")

    def test_no_arg_exception_error_is_not_empty(self):
        result = self._query(ExternalAPIError())
        self.assertFalse(result.found)
        self.assertNotEqual(result.error, "", "a no-argument ExternalAPIError collapsed to an empty error string")

    def test_a_real_message_is_still_reported_verbatim(self):
        result = self._query(ExternalAPIError("InterPro REST API connection reset"))
        self.assertEqual(result.error, "InterPro REST API connection reset")


class TestAlphaFoldProviderNoArgException(unittest.TestCase):
    def _query(self, side_effect):
        from pipeline.alphafold.provider import LiveAPIAlphaFoldProvider

        provider = LiveAPIAlphaFoldProvider()
        with (
            mock.patch.object(LiveAPIAlphaFoldProvider, "is_available", return_value=True),
            mock.patch.object(LiveAPIAlphaFoldProvider, "_get_json", side_effect=side_effect),
        ):
            return provider.query("P38398")

    def test_no_arg_exception_error_is_not_empty(self):
        result = self._query(ExternalAPIError())
        self.assertFalse(result.found)
        self.assertNotEqual(result.error, "", "a no-argument ExternalAPIError collapsed to an empty error string")

    def test_a_real_message_is_still_reported_verbatim(self):
        result = self._query(ExternalAPIError("AlphaFold DB summary query returned 500"))
        self.assertEqual(result.error, "AlphaFold DB summary query returned 500")


# ---------------------------------------------------------------------
# 1000 Genomes SAS -- file-level site, annotation/thousand_genomes_sas.py
# ---------------------------------------------------------------------


class Test1000GenomesSASNoArgException(unittest.TestCase):
    def _query(self, side_effect):
        from annotation.thousand_genomes_sas import ThousandGenomesSASLookup

        lookup = ThousandGenomesSASLookup()
        with (
            mock.patch("annotation.thousand_genomes_sas.CONFIG") as fake_config,
            mock.patch("annotation.thousand_genomes_sas._fetch_population_frequencies", side_effect=side_effect),
        ):
            fake_config.thousand_genomes_sas.ENABLED = True
            return lookup.query_variant(_variant(), assembly="GRCh38", rsid_hint="rs699")

    def test_no_arg_exception_error_and_reason_are_not_empty(self):
        result = self._query(ExternalAPIError())
        self.assertFalse(result["found"])
        self.assertNotEqual(result["error"], "", "a no-argument ExternalAPIError collapsed to an empty error string")
        self.assertNotEqual(result["reason"], "", "the paired reason field also collapsed to empty")
        self.assertEqual(result["error"], result["reason"], "error and reason must be the same one variable")

    def test_a_real_message_is_still_reported_verbatim(self):
        result = self._query(ExternalAPIError("Ensembl request failed after 3 attempts"))
        self.assertEqual(result["error"], "Ensembl request failed after 3 attempts")
        self.assertEqual(result["reason"], "Ensembl request failed after 3 attempts")


# ---------------------------------------------------------------------
# orchestrator.py defense-in-depth wrappers -- paired sources
# ---------------------------------------------------------------------


class TestClinGenStageNoArgException(unittest.TestCase):
    def test_no_arg_exception_error_field_is_not_empty(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.query_variant.side_effect = ExternalAPIError()
        p.clingen_client = client
        errors: list = []
        result = p._run_clingen_stage(_variant(), errors)
        self.assertFalse(result["found"])
        self.assertNotEqual(result["error"], "")
        self.assertIn("ClinGen stage failed: ", errors[0])
        self.assertNotEqual(errors[0], "ClinGen stage failed: ")

    def test_a_real_message_is_still_reported_verbatim(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.query_variant.side_effect = ExternalAPIError("ClinGen gene-validity endpoint unreachable")
        p.clingen_client = client
        errors: list = []
        result = p._run_clingen_stage(_variant(), errors)
        self.assertEqual(result["error"], "ClinGen gene-validity endpoint unreachable")


class TestUniProtStageNoArgException(unittest.TestCase):
    def test_no_arg_exception_error_field_is_not_empty(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.query_variant.side_effect = ExternalAPIError()
        p.uniprot_client = client
        errors: list = []
        result = p._run_uniprot_stage(_variant(), {}, errors)
        self.assertFalse(result["found"])
        self.assertNotEqual(result["error"], "")

    def test_a_real_message_is_still_reported_verbatim(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.query_variant.side_effect = ExternalAPIError("UniProt search returned 429")
        p.uniprot_client = client
        errors: list = []
        result = p._run_uniprot_stage(_variant(), {}, errors)
        self.assertEqual(result["error"], "UniProt search returned 429")


class TestInterProStageNoArgException(unittest.TestCase):
    def test_no_arg_exception_error_field_is_not_empty(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.query_variant.side_effect = ExternalAPIError()
        p.interpro_client = client
        errors: list = []
        result = p._run_interpro_stage({}, None, errors)
        self.assertFalse(result["found"])
        self.assertNotEqual(result["error"], "")

    def test_a_real_message_is_still_reported_verbatim(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.query_variant.side_effect = ExternalAPIError("InterPro entry lookup DNS failure")
        p.interpro_client = client
        errors: list = []
        result = p._run_interpro_stage({}, None, errors)
        self.assertEqual(result["error"], "InterPro entry lookup DNS failure")


class TestAlphaFoldStageNoArgException(unittest.TestCase):
    def test_no_arg_exception_error_field_is_not_empty(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.query_variant.side_effect = ExternalAPIError()
        p.alphafold_client = client
        errors: list = []
        result = p._run_alphafold_stage({}, None, errors)
        self.assertFalse(result["found"])
        self.assertNotEqual(result["error"], "")

    def test_a_real_message_is_still_reported_verbatim(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.query_variant.side_effect = ExternalAPIError("AlphaFold DB model fetch timed out")
        p.alphafold_client = client
        errors: list = []
        result = p._run_alphafold_stage({}, None, errors)
        self.assertEqual(result["error"], "AlphaFold DB model fetch timed out")


class Test1000GenomesSASStageNoArgException(unittest.TestCase):
    def test_no_arg_exception_error_field_is_not_empty(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.query_variant.side_effect = RuntimeError()
        p.thousand_genomes_sas_client = client
        errors: list = []
        result = p._run_thousand_genomes_sas_stage(_variant(), {"found": False}, errors)
        self.assertFalse(result["found"])
        self.assertNotEqual(result["error"], "")

    def test_a_real_message_is_still_reported_verbatim(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.query_variant.side_effect = RuntimeError("1000 Genomes SAS client raised unexpectedly")
        p.thousand_genomes_sas_client = client
        errors: list = []
        result = p._run_thousand_genomes_sas_stage(_variant(), {"found": False}, errors)
        self.assertEqual(result["error"], "1000 Genomes SAS client raised unexpectedly")


# ---------------------------------------------------------------------
# orchestrator.py sites with no provider.py split
# ---------------------------------------------------------------------


class TestBlastStageNoArgException(unittest.TestCase):
    def test_no_arg_exception_error_and_reason_are_not_empty(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.search.side_effect = ExternalAPIError()
        p.blast_client = client
        errors: list = []
        result = p._run_blast_stage(SimpleNamespace(alt_sequence="ACGT"), errors)
        self.assertNotEqual(result["error"], "")
        self.assertNotEqual(result["reason"], "")
        self.assertEqual(result["error"], result["reason"])

    def test_a_real_message_is_still_reported_verbatim(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.search.side_effect = ExternalAPIError("BLAST+ subprocess exited 1")
        p.blast_client = client
        errors: list = []
        result = p._run_blast_stage(SimpleNamespace(alt_sequence="ACGT"), errors)
        self.assertEqual(result["error"], "BLAST+ subprocess exited 1")
        self.assertEqual(result["reason"], "BLAST+ subprocess exited 1")


class TestTranscriptStructureStageNoArgException(unittest.TestCase):
    def test_no_arg_exception_error_field_is_not_empty(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.query_variant.side_effect = ExternalAPIError()
        p.transcript_client = client
        errors: list = []
        result = p._run_transcript_stage(_variant(), {}, errors)
        self.assertFalse(result["found"])
        self.assertIsNone(result["transcript"])
        self.assertNotEqual(result["error"], "")

    def test_a_real_message_is_still_reported_verbatim(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.query_variant.side_effect = ExternalAPIError("Ensembl exon/CDS lookup returned 502")
        p.transcript_client = client
        errors: list = []
        result = p._run_transcript_stage(_variant(), {}, errors)
        self.assertEqual(result["error"], "Ensembl exon/CDS lookup returned 502")


if __name__ == "__main__":
    unittest.main()
