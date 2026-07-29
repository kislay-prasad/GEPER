"""Unit tests for pipeline/clingen/models.py and pipeline/clingen/utils.py."""

import unittest

from pipeline.clingen.models import (
    ClinGenGeneEvidence,
    DosageSensitivity,
    GeneDiseaseValidity,
    classification_rank,
)
from pipeline.clingen.utils import (
    normalize_gene_symbol,
    parse_dosage_row,
    parse_gene_validity_row,
)


class TestClassificationRank(unittest.TestCase):
    def test_definitive_ranks_above_limited(self):
        self.assertGreater(classification_rank("Definitive"), classification_rank("Limited"))

    def test_refuted_ranks_below_no_known(self):
        self.assertLess(classification_rank("Refuted"), classification_rank("No Known Disease Relationship"))

    def test_unknown_classification_defaults_to_zero(self):
        self.assertEqual(classification_rank("Not A Real Classification"), 0)

    def test_none_defaults_to_zero(self):
        self.assertEqual(classification_rank(None), 0)


class TestGeneDiseaseValidityStrongest(unittest.TestCase):
    def test_strongest_validity_picks_highest_ranked(self):
        evidence = ClinGenGeneEvidence(
            gene_symbol="BRCA1",
            source="local_dataset",
            found=True,
            gene_disease_validities=[
                GeneDiseaseValidity(gene_symbol="BRCA1", disease_label="Disease A", classification="Limited"),
                GeneDiseaseValidity(gene_symbol="BRCA1", disease_label="Disease B", classification="Definitive"),
                GeneDiseaseValidity(gene_symbol="BRCA1", disease_label="Disease C", classification="Moderate"),
            ],
        )
        self.assertEqual(evidence.strongest_validity.disease_label, "Disease B")
        self.assertEqual(evidence.clinical_validity_summary, "Definitive")

    def test_strongest_validity_none_when_empty(self):
        evidence = ClinGenGeneEvidence(gene_symbol="X", source="local_dataset", found=False)
        self.assertIsNone(evidence.strongest_validity)
        self.assertIsNone(evidence.clinical_validity_summary)


class TestNormalizeGeneSymbol(unittest.TestCase):
    def test_lowercase_is_upcased(self):
        self.assertEqual(normalize_gene_symbol("brca1"), "BRCA1")

    def test_whitespace_stripped(self):
        self.assertEqual(normalize_gene_symbol("  TTN  "), "TTN")

    def test_none_stays_none(self):
        self.assertIsNone(normalize_gene_symbol(None))

    def test_empty_string_is_none(self):
        self.assertIsNone(normalize_gene_symbol(""))


class TestParseGeneValidityRow(unittest.TestCase):
    def test_parses_full_row(self):
        row = {
            "GENE SYMBOL": "BRCA1",
            "DISEASE LABEL": "Hereditary breast and ovarian cancer syndrome",
            "DISEASE ID (MONDO)": "MONDO:0003582",
            "MOI": "Autosomal dominant",
            "SOP": "SOP9",
            "CLASSIFICATION": "Definitive",
            "ONLINE REPORT": "https://example.org/report",
            "CLASSIFICATION DATE": "2024-03-01",
            "GCEP": "Hereditary Cancer GCEP",
        }
        parsed = parse_gene_validity_row(row)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.gene_symbol, "BRCA1")
        self.assertEqual(parsed.classification, "Definitive")
        self.assertEqual(parsed.gcep, "Hereditary Cancer GCEP")

    def test_missing_gene_symbol_returns_none(self):
        row = {"DISEASE LABEL": "Some disease"}
        self.assertIsNone(parse_gene_validity_row(row))

    def test_missing_disease_label_returns_none(self):
        row = {"GENE SYMBOL": "BRCA1"}
        self.assertIsNone(parse_gene_validity_row(row))

    def test_parses_hgnc_gene_id_column(self):
        """
        Regression test: ClinGen's own Gene-Disease Validity download
        includes a "GENE ID (HGNC)" column that must be captured onto
        `GeneDiseaseValidity.gene_id` -- previously this column was read
        by the CSV DictReader but silently discarded, leaving
        `clingen_gene_id` null downstream even for fully-curated genes.
        """
        row = {
            "GENE SYMBOL": "BRCA1",
            "GENE ID (HGNC)": "HGNC:1100",
            "DISEASE LABEL": "Hereditary breast and ovarian cancer syndrome",
            "CLASSIFICATION": "Definitive",
        }
        parsed = parse_gene_validity_row(row)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.gene_id, "HGNC:1100")
        self.assertEqual(parsed.to_dict()["gene_id"], "HGNC:1100")

    def test_missing_gene_id_column_defaults_to_none(self):
        row = {"GENE SYMBOL": "BRCA1", "DISEASE LABEL": "Some disease"}
        parsed = parse_gene_validity_row(row)
        self.assertIsNotNone(parsed)
        self.assertIsNone(parsed.gene_id)


class TestParseDosageRow(unittest.TestCase):
    def test_parses_full_row(self):
        row = {
            "GENE SYMBOL": "SCN5A",
            "HAPLOINSUFFICIENCY SCORE": "3",
            "HAPLOINSUFFICIENCY DESCRIPTION": "Sufficient evidence",
            "TRIPLOSENSITIVITY SCORE": "0",
            "TRIPLOSENSITIVITY DESCRIPTION": "No evidence available",
        }
        parsed = parse_dosage_row(row)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.haploinsufficiency_score, 3)
        self.assertEqual(parsed.haploinsufficiency_label, "Sufficient evidence for dosage pathogenicity")

    def test_non_numeric_score_becomes_none(self):
        row = {"GENE SYMBOL": "X", "HAPLOINSUFFICIENCY SCORE": "not a number"}
        parsed = parse_dosage_row(row)
        self.assertIsNotNone(parsed)
        self.assertIsNone(parsed.haploinsufficiency_score)

    def test_missing_gene_symbol_returns_none(self):
        self.assertIsNone(parse_dosage_row({"HAPLOINSUFFICIENCY SCORE": "3"}))


class TestDosageSensitivityLabels(unittest.TestCase):
    def test_dosage_unlikely_label(self):
        d = DosageSensitivity(gene_symbol="GJB2", haploinsufficiency_score=40)
        self.assertEqual(d.haploinsufficiency_label, "Dosage sensitivity unlikely")

    def test_unknown_score_falls_back_to_generic_label(self):
        d = DosageSensitivity(gene_symbol="X", haploinsufficiency_score=99)
        self.assertEqual(d.haploinsufficiency_label, "Score 99")

    def test_none_score_has_no_label(self):
        d = DosageSensitivity(gene_symbol="X")
        self.assertIsNone(d.haploinsufficiency_label)


if __name__ == "__main__":
    unittest.main()
