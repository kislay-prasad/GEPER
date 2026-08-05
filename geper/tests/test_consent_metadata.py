"""
Tests for minimal DPDP Act 2023 consent-metadata capture -- deliberately
small: `report/summary.py::_parse_consent`/`_parse_patient_meta`,
`report/json_builder.py::JSONResultBuilder`'s `patient_consent` output
key, and the optional consent rows both PDF renderers add to their
existing patient header/identity tables.

Explicitly NOT covered here because it doesn't exist by design (see
`_parse_consent`'s docstring): no database table, no IP-address/hash
audit trail, no erasure/withdrawal workflow. If a future change adds
any of those, it does not belong under this module's scope.
"""

import os
import tempfile
import unittest

from report.json_builder import JSONResultBuilder
from report.summary import _consent_value_label, _parse_consent, _parse_patient_meta, generate_pdf
from report.summary_short import generate_short_pdf

try:
    from pypdf import PdfReader

    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False


class TestParseConsent(unittest.TestCase):
    def test_full_consent_object(self):
        result = _parse_consent(
            {"clinical_reporting": True, "research": False, "timestamp": "2026-08-01T09:15:00+05:30"}
        )
        self.assertEqual(
            result, {"clinical_reporting": True, "research": False, "timestamp": "2026-08-01T09:15:00+05:30"}
        )

    def test_none_input_is_none(self):
        self.assertIsNone(_parse_consent(None))

    def test_non_dict_input_is_none(self):
        self.assertIsNone(_parse_consent("true"))
        self.assertIsNone(_parse_consent(["clinical_reporting"]))

    def test_empty_dict_is_none(self):
        self.assertIsNone(_parse_consent({}))

    def test_partial_consent_missing_fields_are_none_not_fabricated(self):
        result = _parse_consent({"research": True})
        self.assertEqual(result, {"clinical_reporting": None, "research": True, "timestamp": None})

    def test_non_bool_value_treated_as_not_stated(self):
        # A stray string like "true" is a type mismatch, not evidence of
        # consent either way -- must not be truthy-coerced.
        result = _parse_consent({"clinical_reporting": "true", "research": True})
        self.assertIsNone(result["clinical_reporting"])
        self.assertTrue(result["research"])

    def test_all_fields_invalid_collapses_to_none(self):
        result = _parse_consent({"clinical_reporting": "yes", "research": "no", "timestamp": 12345})
        self.assertIsNone(result)

    def test_blank_timestamp_string_is_none(self):
        result = _parse_consent({"clinical_reporting": True, "timestamp": "   "})
        self.assertIsNone(result["timestamp"])


class TestParsePatientMetaConsent(unittest.TestCase):
    def test_no_patient_meta_consent_is_none(self):
        self.assertIsNone(_parse_patient_meta(None)["consent"])

    def test_patient_meta_without_consent_key_is_none(self):
        patient = _parse_patient_meta({"patient_name": "Jane Doe", "physician": "Dr. X"})
        self.assertIsNone(patient["consent"])

    def test_named_patient_with_consent(self):
        patient = _parse_patient_meta(
            {"patient_name": "Jane Doe", "consent": {"clinical_reporting": True, "research": False}}
        )
        self.assertFalse(patient["deidentified"])
        self.assertEqual(patient["consent"], {"clinical_reporting": True, "research": False, "timestamp": None})

    def test_deidentified_sample_can_still_carry_consent(self):
        # No usable patient_name -> "deidentified" per existing spec,
        # but consent tracking is independent of patient identification
        # (see _parse_consent's docstring) -- must NOT be dropped.
        patient = _parse_patient_meta({"consent": {"research": True}})
        self.assertTrue(patient["deidentified"])
        self.assertEqual(patient["consent"], {"clinical_reporting": None, "research": True, "timestamp": None})

    def test_corrupt_file_consent_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_path = os.path.join(tmp, "corrupt.json")
            with open(bad_path, "w", encoding="utf-8") as fh:
                fh.write("{not valid json")
            self.assertIsNone(_parse_patient_meta(bad_path)["consent"])

    def test_real_example_file_parses_consent(self):
        # patient_metadata.example.json ships a real consent example --
        # confirms it stays valid JSON and matches the documented shape.
        patient = _parse_patient_meta("patient_metadata.example.json")
        self.assertEqual(patient["consent"]["clinical_reporting"], True)
        self.assertEqual(patient["consent"]["research"], False)
        self.assertIsNotNone(patient["consent"]["timestamp"])


class TestConsentValueLabel(unittest.TestCase):
    def test_true_is_yes(self):
        self.assertEqual(_consent_value_label(True), "Yes")

    def test_false_is_no(self):
        self.assertEqual(_consent_value_label(False), "No")

    def test_none_is_not_stated(self):
        self.assertEqual(_consent_value_label(None), "Not stated")


class TestJsonResultBuilderPatientConsent(unittest.TestCase):
    def test_default_is_explicit_null(self):
        doc = JSONResultBuilder(input_vcf_path="x.vcf").build()
        self.assertIn("patient_consent", doc)
        self.assertIsNone(doc["patient_consent"])

    def test_supplied_consent_surfaces_verbatim(self):
        consent = {"clinical_reporting": True, "research": False, "timestamp": "2026-08-01T09:15:00+05:30"}
        doc = JSONResultBuilder(input_vcf_path="x.vcf", patient_consent=consent).build()
        self.assertEqual(doc["patient_consent"], consent)

    def test_partial_consent_surfaces_with_none_fields_intact(self):
        consent = {"clinical_reporting": None, "research": True, "timestamp": None}
        doc = JSONResultBuilder(input_vcf_path="x.vcf", patient_consent=consent).build()
        self.assertEqual(doc["patient_consent"], consent)


def _minimal_document():
    return {
        "geper_version": "test",
        "generated_at": "2026-08-06T00:00:00+00:00",
        "input_vcf": "consent_test.vcf",
        "assembly": "GRCh38",
        "vcf_samples": ["SAMPLE01"],
        "variant_count": 1,
        "variants": [
            {
                "variant": {"chrom": "1", "pos": 100, "ref": "A", "alt": "T"},
                "interpretation_result": {"gene_symbol": "BRCA1"},
                "clinical_report": {
                    "executive_summary": "Test summary.",
                    "acmg_classification": {"classification": "Uncertain significance", "triggered_criteria": []},
                    "confidence": {"pending": True},
                    "supporting_evidence": [],
                    "limitations": [],
                    "evidence_sources": [],
                    "conflict_resolution": {"severity": None},
                },
            }
        ],
    }


def _all_text(path: str) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(path).pages)


def _normalized(text: str) -> str:
    import re

    return re.sub(r"\s+", " ", text)


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestFullReportConsentRows(unittest.TestCase):
    def test_consent_rows_present_when_supplied(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            generate_pdf(_minimal_document(), out, patient_meta="patient_metadata.example.json")
            text = _normalized(_all_text(out))
            self.assertIn("Consent -- Clinical Reporting", text)
            self.assertIn("Yes", text)
            self.assertIn("Consent -- Research Use", text)
            self.assertIn("Consent Recorded", text)
            self.assertIn("2026-08-01", text)

    def test_consent_rows_absent_without_patient_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            generate_pdf(_minimal_document(), out)
            text = _all_text(out)
            self.assertNotIn("Consent --", text)

    def test_consent_rows_absent_when_patient_meta_has_no_consent_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            generate_pdf(_minimal_document(), out, patient_meta={"patient_name": "Jane Doe", "physician": "Dr. X"})
            text = _all_text(out)
            self.assertNotIn("Consent --", text)

    def test_consent_rows_present_for_deidentified_sample_with_consent(self):
        # No patient_name -> de-identified header, but consent is
        # independent of identity and must still render.
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            generate_pdf(_minimal_document(), out, patient_meta={"consent": {"research": True}})
            text = _normalized(_all_text(out))
            self.assertIn("De-identified", text)
            self.assertIn("Consent -- Research Use", text)
            self.assertIn("Consent -- Clinical Reporting", text)
            self.assertIn("Not stated", text)  # clinical_reporting wasn't stated


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestShortReportConsentRows(unittest.TestCase):
    def test_consent_rows_present_when_supplied(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "short.pdf")
            generate_short_pdf(_minimal_document(), out, patient_meta="patient_metadata.example.json")
            text = _normalized(_all_text(out))
            self.assertIn("Consent -- Clinical Reporting", text)
            self.assertIn("Consent -- Research Use", text)
            self.assertIn("Consent Recorded", text)

    def test_consent_rows_absent_without_patient_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "short.pdf")
            generate_short_pdf(_minimal_document(), out)
            text = _all_text(out)
            self.assertNotIn("Consent --", text)


if __name__ == "__main__":
    unittest.main()
