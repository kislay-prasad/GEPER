"""
Tests for the generic LIMS export (`report/export_lims.py`) and the
field mapping documented in `LIMS_EXPORT_MAPPING.md`.

Entirely offline, no pipeline run: every document below is a
hand-built dict matching `geper_results.json`'s real shape (the same
shape `report/json_builder.py::JSONResultBuilder.build()` produces),
mirroring the genes in `geper/test_data/nuclear_test.vcf` (BRCA1,
TP53, PRNP) so this exercises the same real-world shape a Colab run
against that file would produce. No model weights, no network.
"""

import csv
import json
import os
import tempfile
import unittest

from report.export_lims import (
    _CSV_COLUMNS,
    LIMSExport,
    build_lims_export,
    export_lims_csv,
    export_lims_json,
)
from utils.exceptions import LIMSExportBlockedError


def _base_document(variants, review_status="reviewed"):
    """
    `review_status` defaults to `"reviewed"` (round 30 part 2's
    governance gate -- see `report/export_lims.py::_require_reviewed`):
    every test in this file below is about FIELD MAPPING, not the gate
    itself, so its fixtures represent an already-reviewed run unless a
    test explicitly overrides this to exercise the gate (see
    `LIMSExportGovernanceTests`).
    """
    return {
        "geper_version": "1.0.0",
        "generated_at": "2026-08-02T10:00:00+00:00",
        "input_vcf": "test_data/nuclear_test.vcf",
        "assembly": "GRCh38",
        "vcf_samples": ["NA00001"],
        "variant_count": len(variants),
        "code_version": "geper-test",
        "review_status": review_status,
        "variants": variants,
    }


def _clean_variant():
    """A fully-interpreted BRCA1 finding with case-level ranking -- the "everything worked" baseline."""
    return {
        "variant": {
            "chrom": "17",
            "pos": 43106534,
            "id": "BRCA1_PS3_pos",
            "ref": "C",
            "alt": "A",
            "filter": ".",
            "variant_type": "SNV",
        },
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
        "clinical_report": {
            "acmg_classification": {
                "classification": "Pathogenic",
                "triggered_criteria": [{"code": "PS3", "strength": "Strong", "direction": "pathogenic"}],
                "not_evaluated_count": 3,
            },
            "confidence": {"pending": False, "score": 92.0, "label": "High"},
            "priority": {"pending": False, "score": 88.0, "category": "Critical", "rank": 1},
            "supporting_evidence": ["ClinVar: Pathogenic."],
            "conflicting_evidence": [],
            "recommendations": ["Confirm with an orthogonal method."],
            "evidence_sources": ["ClinVar", "dbSNP", "ClinGen"],
        },
        "case_prioritization": {
            "case_rank": 1,
            "case_rank_score": 90.0,
            "phenotype_match": {"score": 100.0, "term_matches": []},
            "tier": "phenotype_matched",
            "reason": "Ranked by combined phenotype-match and priority evidence.",
        },
    }


class BuildLimsExportTests(unittest.TestCase):
    def test_clean_variant_maps_every_field_correctly(self):
        doc = _base_document([_clean_variant()])
        export = build_lims_export(doc)

        self.assertEqual(export.export_format, "geper_lims_export")
        self.assertEqual(export.run.input_vcf, "test_data/nuclear_test.vcf")
        self.assertEqual(export.run.sample_id, "NA00001")
        self.assertTrue(export.case_phenotype_ranking_active)

        f = export.findings[0]
        self.assertEqual(f.finding_number, 1)
        self.assertEqual(f.variant.chromosome, "17")
        self.assertEqual(f.variant.genomic_hgvs, "NC_000017.11:g.43106534C>A")
        self.assertEqual(f.variant.coding_hgvs, "NM_007294.4:c.135-1G>T")
        self.assertIsNone(f.variant.protein_hgvs)  # no AlphaMissense hit -- never fabricated
        self.assertEqual(f.gene.symbol, "BRCA1")
        self.assertEqual(f.classification.acmg_classification, "Pathogenic")
        self.assertEqual(f.classification.triggered_criteria[0].code, "PS3")
        self.assertEqual(f.confidence.score, 92.0)
        self.assertEqual(f.priority.rank, 1)
        self.assertEqual(f.case_prioritization.case_rank, 1)
        self.assertEqual(f.clinical_database.clinvar_significance, "Pathogenic")
        self.assertEqual(f.population_frequency.dbsnp_rsid, "rs80357914")
        self.assertTrue(f.interpretation_available)

    def test_protein_hgvs_sourced_only_from_alphamissense_catalogue(self):
        v = _clean_variant()
        v["alphamissense"] = {"found": True, "protein_variant": "p.Val73Met", "am_class": "likely_benign"}
        export = build_lims_export(_base_document([v]))
        f = export.findings[0]
        self.assertEqual(f.variant.protein_hgvs, "p.Val73Met")
        self.assertEqual(f.variant.protein_hgvs_source, "alphamissense_catalogue")

    def test_protein_hgvs_null_when_alphamissense_not_found(self):
        v = _clean_variant()
        v["alphamissense"] = {"found": False}
        export = build_lims_export(_base_document([v]))
        f = export.findings[0]
        self.assertIsNone(f.variant.protein_hgvs)
        self.assertIsNone(f.variant.protein_hgvs_source)

    def test_errored_interpretation_result_produces_honest_empty_record(self):
        v = _clean_variant()
        v["interpretation_result"] = {"error": "ACMG evaluation failed: transcript structure unavailable"}
        v["clinical_report"] = None
        v["errors"] = ["BLAST search failed: connection timeout"]
        export = build_lims_export(_base_document([v]))
        f = export.findings[0]

        self.assertFalse(f.interpretation_available)
        self.assertIsNone(f.classification.acmg_classification)
        self.assertTrue(f.confidence.pending)
        self.assertIsNone(f.confidence.score)
        self.assertTrue(f.priority.pending)
        self.assertEqual(f.stage_errors, ["BLAST search failed: connection timeout"])
        # variant/gene identity (independent of interpretation success)
        # and clinical-database/population fields (raw stage dicts, also
        # independent) must still be populated where available.
        self.assertEqual(f.variant.chromosome, "17")
        self.assertEqual(f.clinical_database.clinvar_significance, "Pathogenic")

    def test_case_prioritization_still_surfaced_when_interpretation_errored(self):
        # Regression guard for a real bug caught during manual
        # verification: case_prioritization is a genuinely separate
        # top-level key (pipeline/orchestrator.py::_apply_case_phenotype_ranking
        # reads it independently of interpretation_result), so a
        # variant whose ACMG interpretation failed can still carry an
        # honest case_prioritization record (typically tier="not_scored").
        # Dropping it unconditionally alongside classification/confidence/
        # priority would silently hide that honest signal.
        v = _clean_variant()
        v["interpretation_result"] = {"error": "boom"}
        v["clinical_report"] = None
        v["case_prioritization"] = {
            "case_rank": None,
            "case_rank_score": None,
            "phenotype_match": {"score": None, "term_matches": []},
            "tier": "not_scored",
            "reason": "Priority score unavailable for this variant; not included in case-level ranking.",
        }
        export = build_lims_export(_base_document([v]))
        f = export.findings[0]
        self.assertFalse(f.interpretation_available)
        self.assertIsNotNone(f.case_prioritization)
        self.assertEqual(f.case_prioritization.tier, "not_scored")
        self.assertIsNone(f.case_prioritization.case_rank)

    def test_clinvar_position_only_never_leaks_co_located_variant_classification(self):
        v = _clean_variant()
        v["clinvar"] = {"match_status": "position_only", "records": [{"clinical_significance": "Benign"}]}
        export = build_lims_export(_base_document([v]))
        f = export.findings[0]
        self.assertIsNone(f.clinical_database.clinvar_significance)
        self.assertIsNone(f.clinical_database.clinvar_review_status)

    def test_no_case_prioritization_anywhere_means_feature_inactive(self):
        v = _clean_variant()
        del v["case_prioritization"]
        export = build_lims_export(_base_document([v]))
        self.assertFalse(export.case_phenotype_ranking_active)
        self.assertIsNone(export.findings[0].case_prioritization)

    def test_no_hpo_data_for_gene_tier_preserved(self):
        v = _clean_variant()
        v["case_prioritization"] = {
            "case_rank": 2,
            "case_rank_score": None,
            "phenotype_match": {"score": None, "term_matches": []},
            "tier": "no_gene_hpo_data",
            "reason": "No HPO-curated phenotype data available for this variant's gene. Ranked after every phenotype-matched variant.",
        }
        export = build_lims_export(_base_document([v]))
        cp = export.findings[0].case_prioritization
        self.assertEqual(cp.tier, "no_gene_hpo_data")
        self.assertIsNone(cp.phenotype_match_score)
        self.assertIsNone(cp.case_rank_score)

    def test_empty_variants_list_produces_valid_zero_finding_export(self):
        export = build_lims_export(_base_document([]))
        self.assertEqual(export.findings, [])
        self.assertFalse(export.case_phenotype_ranking_active)

    def test_sample_id_and_run_id_reuse_summary_module_derivation(self):
        # Must match report/summary.py's own PDF derivation exactly (not
        # reimplemented) so a JSON export and a PDF generated from the
        # same geper_results.json agree on sample/run identity.
        from report.summary import _derive_run_id, _derive_sample_id

        doc = _base_document([_clean_variant()])
        export = build_lims_export(doc)
        self.assertEqual(export.run.sample_id, _derive_sample_id(doc))
        self.assertEqual(export.run.run_id, _derive_run_id(doc, None))

    def test_explicit_run_id_override_is_honored(self):
        doc = _base_document([_clean_variant()])
        export = build_lims_export(doc, run_id="LAB-ACCESSION-42")
        self.assertEqual(export.run.run_id, "LAB-ACCESSION-42")


class SchemaValidationTests(unittest.TestCase):
    def test_written_json_round_trips_through_schema_validation(self):
        doc = _base_document([_clean_variant()])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "export.json")
            export_lims_json(doc, path)
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            # Re-validates the ON-DISK file against the schema, not
            # just the in-memory object -- proves the written bytes
            # genuinely conform.
            validated = LIMSExport.model_validate(raw)
            self.assertEqual(len(validated.findings), 1)

    def test_malformed_export_is_rejected_by_schema(self):
        doc = _base_document([_clean_variant()])
        export = build_lims_export(doc)
        raw = json.loads(export.model_dump_json())
        raw["findings"][0]["finding_number"] = "not-a-number"
        with self.assertRaises(Exception):
            LIMSExport.model_validate(raw)

    def test_missing_required_run_field_is_rejected(self):
        with self.assertRaises(Exception):
            LIMSExport.model_validate({"run": {}, "case_phenotype_ranking_active": False, "findings": []})


class CsvExportTests(unittest.TestCase):
    def test_csv_row_count_and_header_matches_documented_columns(self):
        doc = _base_document([_clean_variant(), _clean_variant()])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "export.csv")
            export_lims_csv(doc, path)
            with open(path, "r", encoding="utf-8", newline="") as fh:
                reader = csv.reader(fh)
                rows = list(reader)
        header, data_rows = rows[0], rows[1:]
        self.assertEqual(header, _CSV_COLUMNS)
        self.assertEqual(len(data_rows), 2)

    def test_list_fields_are_semicolon_joined_single_cells(self):
        doc = _base_document([_clean_variant()])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "export.csv")
            export_lims_csv(doc, path)
            with open(path, "r", encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
        self.assertEqual(rows[0]["evidence_sources"], "ClinVar;dbSNP;ClinGen")
        self.assertEqual(rows[0]["triggered_acmg_criteria"], "PS3")

    def test_honest_gaps_render_as_empty_csv_cells_not_placeholder_text(self):
        v = _clean_variant()
        v["interpretation_result"] = {"error": "boom"}
        v["clinical_report"] = None
        del v["case_prioritization"]
        doc = _base_document([v])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "export.csv")
            export_lims_csv(doc, path)
            with open(path, "r", encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
        row = rows[0]
        self.assertEqual(row["acmg_classification"], "")
        self.assertEqual(row["case_rank"], "")
        self.assertNotIn("N/A", row.values())
        self.assertNotIn("null", row.values())


class LIMSExportGovernanceTests(unittest.TestCase):
    """
    Round 30 part 2: the LIMS export hard gate
    (`report/export_lims.py::_require_reviewed`). Proves the actual
    blocking/unblocking BEHAVIOR (exception raised, audit entry
    written, file not written on block; export succeeds and is
    readable when reviewed) -- not just that `review_status` exists as
    a field.
    """

    def _audit_lines(self, directory):
        path = os.path.join(directory, "geper_signoff_audit.log")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_draft_json_export_blocked_with_audit_entry(self):
        doc = _base_document([_clean_variant()], review_status="draft")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "export.json")
            with self.assertRaises(LIMSExportBlockedError) as ctx:
                export_lims_json(doc, path)
            self.assertIn("draft", str(ctx.exception))
            self.assertFalse(os.path.exists(path))  # never a silent partial/empty export

            lines = self._audit_lines(tmp)
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["action"], "export_blocked")
            self.assertEqual(lines[0]["format"], "json")
            self.assertEqual(lines[0]["review_status"], "draft")
            self.assertIn("reviewed", lines[0]["reason"])

    def test_draft_csv_export_blocked_with_audit_entry(self):
        doc = _base_document([_clean_variant()], review_status="draft")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "export.csv")
            with self.assertRaises(LIMSExportBlockedError):
                export_lims_csv(doc, path)
            self.assertFalse(os.path.exists(path))

            lines = self._audit_lines(tmp)
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["action"], "export_blocked")
            self.assertEqual(lines[0]["format"], "csv")

    def test_missing_review_status_key_blocked(self):
        # No pre-round-30 geper_results.json will ever have this key --
        # must not be read as reviewed just because the key is absent
        # (same fail-toward-the-less-trusting-claim discipline as
        # run_complete's own missing-key handling).
        doc = _base_document([_clean_variant()])
        del doc["review_status"]
        with self.assertRaises(LIMSExportBlockedError):
            build_lims_export(doc)

    def test_overridden_status_also_blocked(self):
        # "overridden" is NOT good enough for export -- a clinician
        # changed the classification since the run was last approved,
        # so a fresh approve() is required (see review/signoff.py
        # ::override()'s docstring).
        doc = _base_document([_clean_variant()], review_status="overridden")
        with self.assertRaises(LIMSExportBlockedError):
            build_lims_export(doc)

    def test_reviewed_json_export_succeeds(self):
        doc = _base_document([_clean_variant()], review_status="reviewed")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "export.json")
            export_lims_json(doc, path)
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
            self.assertEqual(raw["findings"][0]["gene"]["symbol"], "BRCA1")
            self.assertEqual(self._audit_lines(tmp), [])  # no block, no audit entry

    def test_reviewed_csv_export_succeeds(self):
        doc = _base_document([_clean_variant()], review_status="reviewed")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "export.csv")
            export_lims_csv(doc, path)
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(rows[0]["gene_symbol"], "BRCA1")


if __name__ == "__main__":
    unittest.main()
