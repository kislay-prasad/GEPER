"""
Tests for the "Indian Population Frequency" report section (India-
deployment feature): `report/clinical_report_builder.py
::_indian_population_frequency` (the shared section data both output
formats render), plus its Markdown (`report/report_generator.py`) and
PDF (`report/summary.py`) renderings -- and, at the bottom, the
`pipeline/orchestrator.py` stage-wiring change that retired IndiGenomes.

AS OF 2026-08-08: IndiGenomes was retired from GEPER's active query
path entirely -- its own terms restrict commercial use ("Commercial
use of the resource would require licensing"), which GEPER has not
obtained (see `DATA_SOURCE_LICENSE_AUDIT.md`). 1000 Genomes SAS
(`annotation/thousand_genomes_sas.py`), originally built as an
offline-only fallback, is now the sole, unconditional source for this
section. This file replaces the prior version's extensive
"IndiGenomes vs. fallback" branching tests with tests for the simpler,
unconditional shape -- there is no longer a branch to test.

PDF tests generate real, small PDFs with ReportLab (lightweight, no
model weights -- safe to run locally), following the same pattern
`tests/test_clinician_summary.py` already established.
"""

import dataclasses
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from report.clinical_report_builder import _indian_population_frequency, build_clinical_report
from report.report_generator import ReportGenerator
from report.summary import generate_pdf

try:
    from pypdf import PdfReader

    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False


def _ir(**overrides):
    base = {
        "variant": {"chrom": "1", "pos": 100, "ref": "A", "alt": "T"},
        "gene_symbol": "BRCA1",
        "acmg_classification": "Uncertain significance",
        "triggered_rules": [],
        "not_triggered_rules": [],
        "not_evaluated_rules": [],
        "combining_rule_trace": [],
        "supporting_evidence": [],
        "conflicting_evidence": [],
        "ai_consensus": [],
        "ai_context_models": [],
        "confidence_pending": True,
        "priority_pending": True,
        "priority_explanation": [],
        "conflict_list": [],
        "conflict_score": 0.0,
        "recommendations": [],
        "evidence_sources": [],
        "ai_model_errors": [],
    }
    base.update(overrides)
    return base


def _patch_threshold(value=0.01):
    # Patches only the one real attribute this suite cares about, not the
    # whole CONFIG object (see A7 fix -- Run Quality Control section):
    # replacing all of `report.clinical_report_builder.CONFIG` with a bare
    # MagicMock used to be harmless here because no code path this suite
    # exercised ever touched `CONFIG.qc_report.*`. Now that both PDF and
    # Markdown render a QC section via
    # `clinical_report_builder._qc_threshold_pass_min` for every document
    # (including these fixtures, none of which set `qc_metrics`, so they
    # hit the honest NOT_RUN branch), that call auto-vivified a MagicMock
    # threshold and crashed the `f"{threshold:g}..."` format -- a fully
    # mocked CONFIG object answers ANY attribute access, not just the one
    # this test intends to control. Patching just the real, already-nested
    # attribute leaves `CONFIG.qc_report.*` at its genuine default values.
    # `GeperConfig`/`IndiGenomesConfig` are frozen dataclasses -- no
    # setattr onto the real instance or its nested fields is possible, so
    # this replaces the module-level `CONFIG` *name* (same mechanism the
    # original MagicMock version used) with a real `dataclasses.replace()`
    # copy of the genuine CONFIG object, differing only in
    # `indigenomes.COMMON_AF_THRESHOLD`. Every other field --
    # including `qc_report.*`, which the A7 fix's new QC section now
    # reads on every render -- stays at its real, valid default instead
    # of auto-vivifying as an unconstrained MagicMock the way a bare
    # `mock.patch("...CONFIG")` would.
    from config import CONFIG as _real_config

    fake_config = dataclasses.replace(
        _real_config, indigenomes=dataclasses.replace(_real_config.indigenomes, COMMON_AF_THRESHOLD=value)
    )
    patcher = mock.patch("report.clinical_report_builder.CONFIG", fake_config)
    patcher.start()
    return patcher


_TGS_FOUND = {
    "skipped": False,
    "found": True,
    "rsid": "rs699",
    "sas_pooled": {"af": 0.636, "ac": 622, "an": 978},
    "sub_populations": {
        "GIH": {"af": 0.592, "ac": 122, "an": 206},
        "PJL": {"af": 0.609, "ac": 117, "an": 192},
        "BEB": {"af": 0.680, "ac": 117, "an": 172},
        "STU": {"af": 0.681, "ac": 139, "an": 204},
        "ITU": {"af": 0.622, "ac": 127, "an": 204},
    },
    "population_labels": {
        "GIH": "Gujarati Indian in Houston, TX, USA (diaspora, not India-resident)",
        "PJL": "Punjabi in Lahore, Pakistan (not an Indian cohort)",
        "BEB": "Bengali in Bangladesh (not an Indian cohort)",
        "STU": "Sri Lankan Tamil in the UK (diaspora, not India-resident, not Indian)",
        "ITU": "Indian Telugu in the UK (diaspora, not India-resident)",
    },
    "sample_sizes": {"GIH": 106, "PJL": 96, "BEB": 86, "STU": 103, "ITU": 103},
    "total_sample_size": 494,
}

_TGS_NOT_FOUND = {"skipped": False, "found": False, "rsid": None}
_TGS_ERROR = {"skipped": False, "found": False, "error": "Ensembl request failed after 3 attempts"}


class TestIndianPopulationFrequencySection(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)

    def test_gnomad_sas_at_or_above_threshold_sets_common_flag(self):
        _patch_threshold(0.01)
        raw = {"gnomad": {"skipped": False, "found": True, "population_breakdown": {"sas": {"af": 0.02}}}}
        section = _indian_population_frequency(raw, None)
        self.assertEqual(section["gnomad_af_sas"], 0.02)
        self.assertTrue(section["common_in_indian_population"])

    def test_gnomad_sas_below_threshold_no_common_flag(self):
        _patch_threshold(0.01)
        raw = {"gnomad": {"skipped": False, "found": True, "population_breakdown": {"sas": {"af": 0.0001}}}}
        section = _indian_population_frequency(raw, None)
        self.assertFalse(section["common_in_indian_population"])

    def test_gnomad_missing_sas_entry(self):
        _patch_threshold(0.01)
        raw = {"gnomad": {"skipped": False, "found": True, "population_breakdown": {"eas": {"af": 0.3}}}}
        section = _indian_population_frequency(raw, None)
        self.assertIsNone(section["gnomad_af_sas"])
        self.assertFalse(section["common_in_indian_population"])  # eas doesn't count toward the Indian-population flag

    def test_huge_1000g_sas_af_never_counted_toward_common_flag(self):
        """The flag is driven by gnomAD SAS alone now (IndiGenomes is
        gone; 1000 Genomes SAS was never eligible for this flag either,
        for the same 'small diaspora cohort shouldn't drive a clinical
        flag' reasoning the prior fallback version already documented)."""
        _patch_threshold(0.01)
        raw = {"gnomad": {"skipped": False, "found": True, "population_breakdown": {"sas": {"af": 0.001}}}}
        huge_af = dict(_TGS_FOUND, sas_pooled={"af": 0.99, "ac": 900, "an": 978})
        section = _indian_population_frequency(raw, huge_af)
        self.assertFalse(section["common_in_indian_population"])


class TestThousandGenomesSasUnconditional(unittest.TestCase):
    """
    `_indian_population_frequency`'s 1000 Genomes SAS handling: as of
    2026-08-08 it is shown whenever a result dict was actually passed
    (`sas_shown`), regardless of found/not-found/error -- never gated
    on any other source's availability, since IndiGenomes no longer
    exists in the active query path to gate on.
    """

    def setUp(self):
        self.addCleanup(mock.patch.stopall)

    def test_shown_and_available_when_found(self):
        _patch_threshold(0.01)
        raw = {"gnomad": {}}
        section = _indian_population_frequency(raw, _TGS_FOUND)
        self.assertTrue(section["sas_shown"])
        self.assertTrue(section["sas_available"])
        self.assertEqual(section["sas_rsid"], "rs699")
        self.assertEqual(section["sas_sub_populations"]["GIH"]["af"], 0.592)
        self.assertEqual(section["sas_total_sample_size"], 494)
        self.assertIn("Houston", section["sas_population_labels"]["GIH"])
        self.assertIsNone(section["sas_error"])

    def test_shown_but_not_available_when_genuinely_not_found(self):
        _patch_threshold(0.01)
        raw = {"gnomad": {}}
        section = _indian_population_frequency(raw, _TGS_NOT_FOUND)
        self.assertTrue(section["sas_shown"])
        self.assertFalse(section["sas_available"])
        self.assertIsNone(section["sas_error"])

    def test_shown_with_its_own_error(self):
        _patch_threshold(0.01)
        raw = {"gnomad": {}}
        section = _indian_population_frequency(raw, _TGS_ERROR)
        self.assertTrue(section["sas_shown"])
        self.assertFalse(section["sas_available"])
        self.assertEqual(section["sas_error"], "Ensembl request failed after 3 attempts")

    def test_not_shown_only_when_result_is_none(self):
        """The only way this section doesn't show the 1000 Genomes SAS
        source at all is if the caller never even ran the stage (passed
        `None`) -- in real pipeline operation this stage always runs,
        so this path is effectively test-only."""
        _patch_threshold(0.01)
        raw = {"gnomad": {}}
        section = _indian_population_frequency(raw, None)
        self.assertFalse(section["sas_shown"])
        self.assertFalse(section["sas_available"])
        self.assertIsNone(section["sas_pooled"])


class TestBuildClinicalReportIncludesSection(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)

    def test_section_present_in_full_clinical_report(self):
        _patch_threshold(0.01)
        raw = {"gnomad": {"skipped": False, "found": True, "population_breakdown": {"sas": {"af": 0.02}}}}
        cr = build_clinical_report(_ir(), raw_evidence=raw, thousand_genomes_sas_result=_TGS_FOUND)
        self.assertIn("indian_population_frequency", cr)
        self.assertTrue(cr["indian_population_frequency"]["common_in_indian_population"])
        self.assertTrue(cr["indian_population_frequency"]["sas_shown"])

    def test_indigenomes_result_kwarg_is_accepted_but_has_no_effect(self):
        """Backward-compatibility check: `build_clinical_report` still
        accepts `indigenomes_result` (so existing callers like
        `report/json_builder.py` don't need a signature-breaking
        change) but it must no longer influence the output -- there's
        no more IndiGenomes-vs-fallback branch for it to feed."""
        _patch_threshold(0.01)
        raw = {"gnomad": {"skipped": False, "found": True, "population_breakdown": {"sas": {"af": 0.001}}}}
        cr_with = build_clinical_report(
            _ir(),
            raw_evidence=raw,
            indigenomes_result={"skipped": False, "found": True, "af": 0.5, "ac": 900, "an": 1000},
            thousand_genomes_sas_result=_TGS_FOUND,
        )
        cr_without = build_clinical_report(_ir(), raw_evidence=raw, thousand_genomes_sas_result=_TGS_FOUND)
        self.assertEqual(cr_with["indian_population_frequency"], cr_without["indian_population_frequency"])
        self.assertNotIn("indigenomes_available", cr_with["indian_population_frequency"])


def _markdown_document(thousand_genomes_sas_result):
    """Shared helper: a one-variant document dict ready for `ReportGenerator().generate()`."""
    _patch_threshold(0.01)
    raw = {"gnomad": {"skipped": False, "found": True, "population_breakdown": {"sas": {"af": 0.02}}}}
    cr = build_clinical_report(_ir(), raw_evidence=raw, thousand_genomes_sas_result=thousand_genomes_sas_result)
    return {
        "geper_version": "t",
        "generated_at": "2026-01-01T00:00:00Z",
        "input_vcf": "x.vcf",
        "assembly": "GRCh38",
        "vcf_samples": [],
        "variant_count": 1,
        "variants": [
            {
                "variant": {"chrom": "1", "pos": 100, "ref": "A", "alt": "T"},
                "interpretation": {},
                "clinical_report": cr,
                "ai_model_status": {},
                "errors": [],
            }
        ],
    }


class TestMarkdownRendering(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)

    def test_section_11_present_with_gnomad_and_1000g(self):
        doc = _markdown_document(_TGS_FOUND)
        md = ReportGenerator().generate(doc)
        self.assertIn("### 11. Indian Population Frequency", md)
        self.assertIn("gnomAD (South Asian, SAS)", md)
        self.assertIn("1000 Genomes (South Asian, SAS)", md)
        self.assertIn("Common in Indian populations", md)
        # No standalone "IndiGenomes:" evidence line remains -- the
        # only legitimate mention left is the sample-size disclosure's
        # own size comparison ("...much smaller than IndiGenomes'
        # 1000+ India-resident genomes"), which is accurate historical
        # context, not a live source being queried.
        self.assertNotIn("**IndiGenomes:**", md)
        self.assertIn("much smaller than IndiGenomes' 1000+", md)

    def test_subsequent_sections_renumbered_correctly(self):
        doc = _markdown_document(_TGS_NOT_FOUND)
        md = ReportGenerator().generate(doc)
        self.assertIn("### 12. Clinical Evidence", md)
        self.assertIn("### 13. Sequence Context", md)
        self.assertIn("### 14. Recommendations", md)
        self.assertIn("### 15. Limitations", md)
        self.assertIn("### 16. References", md)
        self.assertIn("### 17. Evidence Sources", md)

    def test_1000g_not_found_renders_plainly(self):
        doc = _markdown_document(_TGS_NOT_FOUND)
        md = ReportGenerator().generate(doc)
        self.assertIn("Variant not found in this source.", md)

    def test_1000g_error_renders_distinctly(self):
        doc = _markdown_document(_TGS_ERROR)
        md = ReportGenerator().generate(doc)
        self.assertIn("lookup failed (external service issue: Ensembl request failed after 3 attempts)", md)

    def test_disclosures_present_when_1000g_found(self):
        doc = _markdown_document(_TGS_FOUND)
        md = ReportGenerator().generate(doc)
        self.assertIn("GIH (Gujarati Indian in Houston, TX, USA", md)
        self.assertIn("n=106", md)
        self.assertIn("494 total samples", md)
        self.assertIn("diaspora populations outside India", md)

    def test_disclosures_present_even_when_1000g_not_found(self):
        """Both mandatory disclosures must render even on the
        not-found path, not only when there's data -- a reader seeing
        this source mentioned at all needs the caveats."""
        doc = _markdown_document(_TGS_NOT_FOUND)
        md = ReportGenerator().generate(doc)
        self.assertIn("494 total samples", md)
        self.assertIn("diaspora populations outside India", md)


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
def _pdf_document(thousand_genomes_sas_result):
    """Shared helper: a one-variant document dict ready for `generate_pdf()`."""
    _patch_threshold(0.01)
    raw = {"gnomad": {"skipped": False, "found": True, "population_breakdown": {"sas": {"af": 0.02}}}}
    cr = build_clinical_report(_ir(), raw_evidence=raw, thousand_genomes_sas_result=thousand_genomes_sas_result)
    return {
        "geper_version": "t",
        "generated_at": "2026-01-01T00:00:00Z",
        "input_vcf": "x.vcf",
        "assembly": "GRCh38",
        "vcf_samples": ["S1"],
        "variant_count": 1,
        "variants": [
            {
                "variant": {"chrom": "1", "pos": 100, "ref": "A", "alt": "T"},
                "interpretation": {},
                "clinical_report": cr,
                "ai_model_status": {},
                "errors": [],
            }
        ],
    }


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestPdfRendering(unittest.TestCase):
    """
    Real ReportLab PDF generation + pypdf text extraction, per this
    feature's stated verification requirement -- confirms the section
    (and both its mandatory disclosures) actually appear in rendered
    PDF text, unconditionally, for every variant.
    """

    def setUp(self):
        self.addCleanup(mock.patch.stopall)

    def test_section_appears_in_pdf_with_1000g_and_disclosures(self):
        document = _pdf_document(_TGS_FOUND)
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r.pdf")
            generate_pdf(document, out)
            text = "\n".join(p.extract_text() for p in PdfReader(out).pages)
        self.assertIn("Indian Population Frequency", text)
        self.assertIn("1000 Genomes (South Asian, SAS)", text)
        self.assertIn("GIH", text)
        self.assertIn("Common in Indian populations", text)
        # No standalone "IndiGenomes:" evidence bullet remains -- the
        # only legitimate mention left is the sample-size disclosure's
        # own size comparison, not a live source being queried.
        self.assertNotIn("• IndiGenomes", text)
        # Both mandatory disclosures, verbatim shared text.
        self.assertIn("494 total samples", text)
        self.assertIn("diaspora populations outside India", text)

    def test_disclosures_present_even_when_1000g_not_found_in_pdf(self):
        document = _pdf_document(_TGS_NOT_FOUND)
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r.pdf")
            generate_pdf(document, out)
            text = "\n".join(p.extract_text() for p in PdfReader(out).pages)
        self.assertIn("Indian Population Frequency", text)
        self.assertIn("Variant not found in this source.", text)
        self.assertIn("494 total samples", text)
        self.assertIn("diaspora populations outside India", text)

    def test_section_shows_explicit_not_queried_states_when_no_data_at_all(self):
        # Neither gnomAD SAS nor the 1000 Genomes SAS stage (never run
        # -- None passed) has anything for this variant. Previously the
        # section rendered nothing at all in this case -- indistinguishable
        # from a section that was never checked. B3 (report review round 1)
        # made this section unconditional: it always renders, with an
        # explicit "not queried" state per source, so a missing section is
        # never confused with a queried-and-empty one. In real pipeline
        # operation the 1000 Genomes SAS stage always runs, so this is
        # effectively a test-only edge case.
        _patch_threshold(0.01)
        raw = {"gnomad": {"skipped": True}}
        cr = build_clinical_report(_ir(), raw_evidence=raw, thousand_genomes_sas_result=None)
        document = {
            "geper_version": "t",
            "generated_at": "2026-01-01T00:00:00Z",
            "input_vcf": "x.vcf",
            "assembly": "GRCh38",
            "vcf_samples": ["S1"],
            "variant_count": 1,
            "variants": [
                {
                    "variant": {"chrom": "1", "pos": 100, "ref": "A", "alt": "T"},
                    "interpretation": {},
                    "clinical_report": cr,
                    "ai_model_status": {},
                    "errors": [],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r.pdf")
            generate_pdf(document, out)
            text = "\n".join(p.extract_text() for p in PdfReader(out).pages)
        self.assertIn("Indian Population Frequency", text)
        self.assertIn("gnomAD (South Asian, SAS): not queried for this variant.", text)
        self.assertIn("1000 Genomes Project (South Asian, SAS): not queried for this variant.", text)


class TestIndiGenomesRetiredFromOrchestrator(unittest.TestCase):
    """
    Verifies `pipeline/orchestrator.py`'s stage wiring: IndiGenomes is
    never called during normal pipeline execution, and the 1000 Genomes
    SAS stage runs unconditionally (no `HEALTH.is_offline("IndiGenomes")`
    gate). Uses a duck-typed `self` (matching
    `tests/test_provenance.py::TestOrchestratorStageProvenanceCapture`'s
    established pattern) rather than constructing a real `GeperPipeline`
    -- that requires loading model registries, out of scope for a
    lightweight unit test and this machine's RAM constraint.
    """

    def test_indigenomes_retired_result_has_the_expected_shape(self):
        from pipeline.orchestrator import GeperPipeline

        result = GeperPipeline._indigenomes_retired_result(SimpleNamespace())
        self.assertTrue(result["skipped"])
        self.assertFalse(result["found"])
        self.assertIn("licens", result["reason"].lower())

    def test_indigenomes_query_variant_is_never_called(self):
        """The core compliance guarantee: going through the retired
        stage never reaches `annotation/indigenomes.py`'s query
        function at all, not just internally no-ops it."""
        from pipeline.orchestrator import GeperPipeline

        fake_client = mock.Mock()
        fake_self = SimpleNamespace(indigenomes_client=fake_client)
        GeperPipeline._indigenomes_retired_result(fake_self)
        fake_client.query_variant.assert_not_called()

    def test_thousand_genomes_sas_stage_runs_unconditionally(self):
        """No `HEALTH.is_offline` gate remains -- the stage must run
        (and call the client) regardless of any service's health
        state, since it's confirmed there is no `HEALTH` reference left
        in this method at all (see the source diff), not merely that
        it happens to return non-skipped here."""
        from pipeline.orchestrator import GeperPipeline
        from pipeline.vcf_parser import Variant

        fake_client = mock.Mock()
        fake_client.query_variant.return_value = _TGS_FOUND
        fake_self = SimpleNamespace(
            sequence_context_gen=SimpleNamespace(assembly="GRCh38"),
            thousand_genomes_sas_client=fake_client,
            _timer=mock.MagicMock(
                return_value=mock.MagicMock(__enter__=mock.Mock(), __exit__=mock.Mock(return_value=False))
            ),
        )
        variant = Variant(chrom="1", pos=230710048, variant_id="rs699", ref="A", alt="G", qual=None, filter_status=None)
        result = GeperPipeline._run_thousand_genomes_sas_stage(fake_self, variant, {"found": False}, [])
        fake_client.query_variant.assert_called_once()
        self.assertEqual(result, _TGS_FOUND)

    def test_config_indigenomes_disabled_by_default(self):
        from config import CONFIG

        self.assertFalse(CONFIG.indigenomes.ENABLED)


if __name__ == "__main__":
    unittest.main()
