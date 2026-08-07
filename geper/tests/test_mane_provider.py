"""
Tests for `pipeline/mane/provider.py::LocalDatasetMANEProvider` and the
module-level `mane_select_transcript_id()` convenience function.

`tests/fixtures/mane_summary_stk11_cbarp.tsv` carries the real header
row and two real data rows -- STK11 and CBARP, the same real,
live-confirmed overlapping-gene pair `tests/test_clingen_gene_resolution.py`
already uses as ground truth -- sliced from a live download of
https://ftp.ncbi.nlm.nih.gov/refseq/MANE/MANE_human/current/MANE.GRCh38.v1.5.summary.txt.gz
(fetched 2026-08-08; `STK11 -> ENST00000326873.12`, `CBARP ->
ENST00000650044.2`, both status "MANE Select"), plus one synthetic
"MANE Plus Clinical" row to exercise the status filter (real "MANE Plus
Clinical" rows exist in the live file -- 74 of them as of v1.5 -- but no
specific one was needed as ground truth here, so this row is fabricated
for test purposes only, not a live-verified record).

Every test here uses `local_file_path=...` and/or `auto_fetch=False`
(the same pattern `tests/test_hpo.py::_local_provider` establishes for
`LocalDatasetHPOProvider`) -- never the module-level default singleton
with its default auto-fetch behavior, so nothing here makes a live
network call.
"""

import os
import unittest
from unittest import mock

from pipeline.mane.provider import LocalDatasetMANEProvider, mane_select_transcript_id

_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "mane_summary_stk11_cbarp.tsv")


def _provider(**overrides) -> LocalDatasetMANEProvider:
    return LocalDatasetMANEProvider(local_file_path=_FIXTURE_PATH, auto_fetch=False)


class TestLocalDatasetMANEProviderRealGroundTruth(unittest.TestCase):
    def test_stk11_resolves_to_its_real_mane_select_transcript_bare_id(self):
        provider = _provider()
        self.assertEqual(provider.mane_select_transcript_id("STK11"), "ENST00000326873")

    def test_cbarp_resolves_to_its_real_mane_select_transcript_bare_id(self):
        provider = _provider()
        self.assertEqual(provider.mane_select_transcript_id("CBARP"), "ENST00000650044")

    def test_version_suffix_is_stripped_not_just_the_gene_symbol_matched(self):
        # The raw fixture row carries "ENST00000326873.12" -- the
        # returned value must be the bare ID, matching what Ensembl's
        # own `lookup/id` response uses (confirmed live: `{"id":
        # "ENST00000326873", "version": 12, ...}`), not the
        # version-suffixed MANE column value verbatim.
        provider = _provider()
        result = provider.mane_select_transcript_id("STK11")
        self.assertNotIn(".", result)

    def test_gene_symbol_lookup_is_case_insensitive(self):
        provider = _provider()
        self.assertEqual(provider.mane_select_transcript_id("stk11"), "ENST00000326873")
        self.assertEqual(provider.mane_select_transcript_id(" Stk11 "), "ENST00000326873")


class TestGeneAbsentFromMane(unittest.TestCase):
    def test_gene_not_in_dataset_returns_none_not_a_guess(self):
        """Mitochondrial genes (MT-ATP8/MT-ATP6, confirmed live absent
        from the real MANE dataset) and any other gene genuinely
        outside MANE's scope must resolve to None, the same honest
        degradation as a genuine 'not found' anywhere else in GEPER."""
        provider = _provider()
        self.assertIsNone(provider.mane_select_transcript_id("MT-ATP8"))
        self.assertIsNone(provider.mane_select_transcript_id("MT-ATP6"))
        self.assertIsNone(provider.mane_select_transcript_id("NOT-A-REAL-GENE"))

    def test_empty_gene_symbol_returns_none(self):
        provider = _provider()
        self.assertIsNone(provider.mane_select_transcript_id(""))
        self.assertIsNone(provider.mane_select_transcript_id(None))


class TestManePlusClinicalIsExcluded(unittest.TestCase):
    def test_mane_plus_clinical_row_is_not_indexed_as_mane_select(self):
        """MANE Plus Clinical is an additional, distinct transcript
        alongside a gene's own MANE Select one -- never a substitute
        for it. The tie-break this feeds asks specifically 'is this
        the gene's MANE Select transcript', so a Plus-Clinical-only
        gene must resolve to None, not the Plus Clinical transcript."""
        provider = _provider()
        self.assertIsNone(provider.mane_select_transcript_id("TESTPLUS"))


class TestAvailability(unittest.TestCase):
    def test_unavailable_when_mane_disabled(self):
        fake_config = mock.Mock()
        fake_config.mane.ENABLED = False
        fake_config.mane.LOCAL_FILE = ""
        with mock.patch("pipeline.mane.provider.CONFIG", fake_config):
            provider = LocalDatasetMANEProvider(local_file_path=_FIXTURE_PATH, auto_fetch=False)
            self.assertFalse(provider.is_available())
            self.assertIsNone(provider.mane_select_transcript_id("STK11"))

    def test_unavailable_without_local_file_or_auto_fetch(self):
        provider = LocalDatasetMANEProvider(local_file_path=None, auto_fetch=False)
        self.assertFalse(provider.is_available())
        self.assertIsNone(provider.mane_select_transcript_id("STK11"))

    def test_missing_local_file_degrades_to_empty_not_a_crash(self):
        provider = LocalDatasetMANEProvider(local_file_path="/does/not/exist.tsv", auto_fetch=False)
        self.assertIsNone(provider.mane_select_transcript_id("STK11"))


class TestAutoFetchNeverTriggeredWithExplicitPath(unittest.TestCase):
    def test_bootstrap_module_never_touched_when_local_file_given(self):
        with mock.patch("pipeline.mane.bootstrap.ensure_summary_file") as fake_ensure:
            provider = LocalDatasetMANEProvider(local_file_path=_FIXTURE_PATH)
            provider.mane_select_transcript_id("STK11")
        fake_ensure.assert_not_called()


class TestLoadedOnlyOnce(unittest.TestCase):
    def test_file_is_parsed_once_across_repeated_lookups(self):
        provider = _provider()
        with mock.patch.object(provider, "_load", wraps=provider._load) as spy_load:
            provider.mane_select_transcript_id("STK11")
            provider.mane_select_transcript_id("CBARP")
            provider.mane_select_transcript_id("STK11")
        spy_load.assert_called_once()


class TestModuleLevelConvenienceFunction(unittest.TestCase):
    def setUp(self):
        # Reset the lazily-constructed process-wide singleton so this
        # test controls exactly what it points at, then restore
        # whatever was there afterward (module singletons persist
        # across tests in the same process otherwise).
        import pipeline.mane.provider as provider_module

        self._provider_module = provider_module
        self._original_default = provider_module._default_provider
        self.addCleanup(setattr, provider_module, "_default_provider", self._original_default)

    def test_uses_configured_local_file(self):
        fake_config = mock.Mock()
        fake_config.mane.ENABLED = True
        fake_config.mane.LOCAL_FILE = _FIXTURE_PATH
        with mock.patch("pipeline.mane.provider.CONFIG", fake_config):
            self._provider_module._default_provider = None
            self.assertEqual(mane_select_transcript_id("STK11"), "ENST00000326873")


if __name__ == "__main__":
    unittest.main()
