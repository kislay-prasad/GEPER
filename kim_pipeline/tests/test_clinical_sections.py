"""
tests/test_clinical_sections.py
─────────────────────────────────
Tests for pipeline/reporting/clinical_sections.py (Issue 3): patient
metadata normalization, QC status indicators, the variant summary
dashboard, clinical interpretation text, and the variant/ACMG merge.
"""
from __future__ import annotations

from pipeline.reporting.clinical_sections import (
    normalize_patient_metadata,
    qc_status_summary,
    variant_dashboard,
    clinical_interpretation,
    merge_variants_with_acmg,
    _variant_key,
)


class TestNormalizePatientMetadata:
    def test_none_falls_back_to_deidentified(self):
        meta = normalize_patient_metadata(None)
        assert meta["name"] == "De-identified / Research Sample"
        assert meta["is_deidentified"] is True

    def test_empty_dict_falls_back_to_deidentified(self):
        meta = normalize_patient_metadata({})
        assert meta["is_deidentified"] is True

    def test_full_metadata_used_as_is(self):
        meta = normalize_patient_metadata({
            "name": "Jane Doe", "dob": "1990-01-01", "sex": "F", "physician": "Dr. Smith",
        })
        assert meta["name"] == "Jane Doe"
        assert meta["dob"] == "1990-01-01"
        assert meta["sex"] == "F"
        assert meta["physician"] == "Dr. Smith"
        assert meta["is_deidentified"] is False

    def test_partial_metadata_only_missing_fields_fall_back(self):
        meta = normalize_patient_metadata({"name": "Jane Doe"})
        assert meta["name"] == "Jane Doe"
        assert meta["dob"] == "Not provided"
        assert meta["is_deidentified"] is False  # partial info still counts as identified

    def test_non_dict_input_does_not_raise(self):
        meta = normalize_patient_metadata("not a dict")  # type: ignore[arg-type]
        assert meta["is_deidentified"] is True

    def test_whitespace_only_field_treated_as_missing(self):
        meta = normalize_patient_metadata({"name": "   "})
        assert meta["name"] == "De-identified / Research Sample"


class TestQcStatusSummary:
    def test_all_pass(self):
        rows = qc_status_summary(
            {"q30_fraction": 0.9, "total_records": 1000},
            {"mean_depth": 40, "pct_mapped": 99, "total_reads": 2000},
        )
        statuses = {r["label"]: r["status"] for r in rows}
        assert statuses["Mean Coverage"] == "PASS"
        assert statuses["Mapping Rate"] == "PASS"
        assert statuses["Q30 Bases"] == "PASS"
        assert statuses["Read Count"] == "PASS"

    def test_warning_band(self):
        rows = qc_status_summary(
            {"q30_fraction": 0.75}, {"mean_depth": 15, "pct_mapped": 92},
        )
        statuses = {r["label"]: r["status"] for r in rows}
        assert statuses["Mean Coverage"] == "WARNING"
        assert statuses["Mapping Rate"] == "WARNING"
        assert statuses["Q30 Bases"] == "WARNING"

    def test_fail_band(self):
        rows = qc_status_summary(
            {"q30_fraction": 0.3}, {"mean_depth": 2, "pct_mapped": 50},
        )
        statuses = {r["label"]: r["status"] for r in rows}
        assert statuses["Mean Coverage"] == "FAIL"
        assert statuses["Mapping Rate"] == "FAIL"
        assert statuses["Q30 Bases"] == "FAIL"

    def test_missing_data_is_unknown_not_fail(self):
        rows = qc_status_summary({}, {})
        statuses = {r["label"]: r["status"] for r in rows}
        assert statuses["Mean Coverage"] == "UNKNOWN"
        assert statuses["Mapping Rate"] == "UNKNOWN"
        assert statuses["Q30 Bases"] == "UNKNOWN"

    def test_none_inputs_do_not_raise(self):
        rows = qc_status_summary(None, None)
        assert len(rows) == 4

    def test_custom_thresholds_override_defaults(self):
        rows = qc_status_summary(
            {}, {"mean_depth": 5},
            thresholds={"mean_depth": {"pass_min": 4.0, "warn_min": 1.0}},
        )
        coverage_row = next(r for r in rows if r["label"] == "Mean Coverage")
        assert coverage_row["status"] == "PASS"

    def test_read_count_prefers_alignment_over_qc(self):
        rows = qc_status_summary({"total_records": 100}, {"total_reads": 999})
        read_row = next(r for r in rows if r["label"] == "Read Count")
        assert "999" in read_row["value"]


class TestVariantDashboard:
    def test_counts_by_classification(self):
        acmg = [
            {"classification": "Pathogenic"},
            {"classification": "Pathogenic"},
            {"classification": "Likely_Pathogenic"},
            {"classification": "Uncertain_Significance"},
            {"classification": "Benign"},
            {"classification": "Likely_Benign"},
        ]
        d = variant_dashboard(acmg, {}, {})
        assert d["total_variants"] == 6
        assert d["pathogenic"] == 2
        assert d["likely_pathogenic"] == 1
        assert d["vus"] == 1
        assert d["benign"] == 1
        assert d["likely_benign"] == 1

    def test_unknown_classification_not_counted_but_not_dropped_from_total(self):
        d = variant_dashboard([{"classification": "SomethingWeird"}], {}, {})
        assert d["total_variants"] == 1
        assert d["pathogenic"] == 0

    def test_pgx_findings_count(self):
        d = variant_dashboard([], {"annotations": [{}, {}]}, {})
        assert d["pgx_findings"] == 2

    def test_pgx_findings_zero_when_missing(self):
        d = variant_dashboard([], {}, {})
        assert d["pgx_findings"] == 0

    def test_ancestry_summary_present(self):
        d = variant_dashboard([], {}, {"primary_population": "EUR", "confidence": "High"})
        assert d["ancestry_summary"] == "EUR (confidence: High)"

    def test_ancestry_summary_not_performed(self):
        d = variant_dashboard([], {}, {})
        assert d["ancestry_summary"] == "Not performed"

    def test_none_inputs_do_not_raise(self):
        d = variant_dashboard(None, None, None)
        assert d["total_variants"] == 0


class TestClinicalInterpretation:
    def test_no_variants(self):
        d = variant_dashboard([], {}, {})
        result = clinical_interpretation(d)
        assert "No variants" in result["summary"]

    def test_actionable_findings_recommend_genetic_counseling(self):
        d = variant_dashboard([{"classification": "Pathogenic"}], {}, {})
        result = clinical_interpretation(d)
        assert "actionable" in result["summary"]
        assert "genetic counseling" in result["follow_up"].lower()

    def test_vus_only_recommends_periodic_review(self):
        d = variant_dashboard([{"classification": "Uncertain_Significance"}], {}, {})
        result = clinical_interpretation(d)
        assert "Uncertain Significance" in result["summary"]
        assert "re-review" in result["follow_up"] or "periodic" in result["follow_up"].lower()

    def test_all_benign_no_actionable_language(self):
        d = variant_dashboard([{"classification": "Benign"}], {}, {})
        result = clinical_interpretation(d)
        assert "No specific follow-up" in result["follow_up"]

    def test_pgx_findings_mentioned_in_summary(self):
        d = variant_dashboard([{"classification": "Benign"}], {"annotations": [{}]}, {})
        result = clinical_interpretation(d)
        assert "pharmacogenomic" in result["summary"].lower()

    def test_never_raises_on_empty_dashboard(self):
        clinical_interpretation({})  # must not raise


class TestMergeVariantsWithAcmg:
    def test_basic_merge_by_position(self):
        variants = [{"chrom": "MT", "pos": 3243, "ref": "A", "alt": "G", "gene_name": "MT-TL1"}]
        acmg = [{"chrom": "MT", "pos": 3243, "ref": "A", "alt": "G", "classification": "Uncertain_Significance"}]
        merged = merge_variants_with_acmg(variants, acmg)
        assert len(merged) == 1
        assert merged[0]["gene_name"] == "MT-TL1"
        assert merged[0]["acmg"]["classification"] == "Uncertain_Significance"

    def test_chr_prefix_normalized(self):
        variants = [{"chrom": "chr17", "pos": 1, "ref": "A", "alt": "T"}]
        acmg = [{"chrom": "17", "pos": 1, "ref": "A", "alt": "T", "classification": "Benign"}]
        merged = merge_variants_with_acmg(variants, acmg)
        assert merged[0]["acmg"]["classification"] == "Benign"

    def test_case_insensitive_ref_alt(self):
        variants = [{"chrom": "17", "pos": 1, "ref": "a", "alt": "t"}]
        acmg = [{"chrom": "17", "pos": 1, "ref": "A", "alt": "T", "classification": "Benign"}]
        merged = merge_variants_with_acmg(variants, acmg)
        assert merged[0]["acmg"]["classification"] == "Benign"

    def test_annotation_only_variant_kept_without_acmg_key(self):
        variants = [{"chrom": "17", "pos": 1, "ref": "A", "alt": "T", "gene_name": "X"}]
        merged = merge_variants_with_acmg(variants, [])
        assert len(merged) == 1
        assert "acmg" not in merged[0]

    def test_acmg_only_variant_still_included(self):
        acmg = [{"chrom": "17", "pos": 999, "ref": "C", "alt": "G", "gene": "Y", "classification": "Benign"}]
        merged = merge_variants_with_acmg([], acmg)
        assert len(merged) == 1
        assert merged[0]["gene_name"] == "Y"
        assert merged[0]["acmg"]["classification"] == "Benign"

    def test_no_data_returns_empty_list(self):
        assert merge_variants_with_acmg(None, None) == []

    def test_variant_key_format(self):
        assert _variant_key("chr17", 100, "a", "t") == "17:100:A:T"
