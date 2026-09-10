"""
Tests for ClinGen gene-disease clinical validity integration:

  - `pipeline/clingen/provider.py::_clingen_export_rows` parsing real
    ClinGen download shapes (both a preamble'd export and a bare
    deployer-provisioned flat file).
  - `pipeline/clingen/provider.py::LocalDatasetClinGenProvider` end to
    end against frozen real-data fixtures.
  - `pipeline/acmg_rules.py::ACMGRuleEngine._pp1` / `._bs4`, the two
    criteria this integration newly wires up.

Ground truth, not synthetic guesses. `tests/fixtures/clingen_gene_validity.csv`
and `tests/fixtures/clingen_dosage_sensitivity.tsv` are verbatim slices
of ClinGen's live downloads (`https://search.clinicalgenome.org/kb/
gene-validity/download` and `https://ftp.clinicalgenome.org/
ClinGen_gene_curation_list_GRCh38.tsv`, fetched during development) --
same preamble, same column names, same values ClinGen itself publishes
for these genes. Every expected classification below can be checked
directly against ClinGen's own KB pages
(https://search.clinicalgenome.org/kb/gene-validity/<GENE>).
"""

import os
import unittest

from pipeline.acmg_rules import ACMGRuleEngine
from pipeline.clingen.provider import LocalDatasetClinGenProvider, _clingen_export_rows

_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
_GENE_VALIDITY_FIXTURE = os.path.join(_FIXTURE_DIR, "clingen_gene_validity.csv")
_DOSAGE_FIXTURE = os.path.join(_FIXTURE_DIR, "clingen_dosage_sensitivity.tsv")


def _provider():
    return LocalDatasetClinGenProvider(
        gene_validity_path=_GENE_VALIDITY_FIXTURE,
        dosage_sensitivity_path=_DOSAGE_FIXTURE,
        auto_fetch=False,
    )


def _clingen_result(gene_symbol, classification, expert_panel=None, found=True):
    return {
        "skipped": False,
        "found": found,
        "error": None,
        "gene_symbol": gene_symbol,
        "clinical_validity_summary": classification,
        "expert_panel": expert_panel,
    }


# ---------------------------------------------------------------------------
# Real-format parsing
# ---------------------------------------------------------------------------


class TestClinGenExportParsing(unittest.TestCase):
    """
    Regression coverage for a real, confirmed defect: a bare
    `csv.DictReader(fh)` over either live ClinGen download misparses
    every row, because both carry a preamble before the real header
    (KB export: title/date/webpage lines + '+++' separator rows; ftp
    export: five '#'-prefixed lines, the last of which *is* the header).
    Before this fix, `LocalDatasetClinGenProvider` silently loaded zero
    rows from a real ClinGen download and every gene-disease validity
    lookup returned "not found" even with a real file configured.
    """

    def test_gene_validity_export_skips_its_kb_preamble(self):
        rows = _clingen_export_rows(_GENE_VALIDITY_FIXTURE)
        self.assertEqual(len(rows), 16)
        self.assertTrue(all("GENE SYMBOL" in r and r["GENE SYMBOL"] for r in rows))
        self.assertTrue(all("CLASSIFICATION" in r for r in rows))

    def test_dosage_export_skips_its_hash_prefixed_preamble(self):
        rows = _clingen_export_rows(_DOSAGE_FIXTURE)
        self.assertEqual(len(rows), 4)
        # The real header cell is '#Gene Symbol'; the '#' must be
        # stripped so `_pick()`'s candidate matching in
        # `pipeline/clingen/utils.py` actually finds it.
        self.assertTrue(all("Gene Symbol" in r for r in rows))
        self.assertNotIn("#Gene Symbol", rows[0])

    def test_bare_flat_file_with_no_preamble_still_parses(self):
        # A deployer-authored file that never had a KB/ftp preamble to
        # begin with must keep working -- the header-by-content scan
        # must not assume a preamble is always present.
        bare_fixture = os.path.join(
            os.path.dirname(__file__), "..", "testdata", "clingen_fixture", "gene_validity_test.tsv"
        )
        rows = _clingen_export_rows(bare_fixture)
        self.assertGreater(len(rows), 0)

    def test_unrecognized_file_yields_no_rows_rather_than_garbage(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
            fh.write("not,a,clingen,export\n1,2,3,4\n")
            path = fh.name
        try:
            self.assertEqual(_clingen_export_rows(path), [])
        finally:
            os.remove(path)


# ---------------------------------------------------------------------------
# Ground-truth classifications
# ---------------------------------------------------------------------------


class TestKnownGeneDiseaseValidity(unittest.TestCase):
    """
    One assertion per real ClinGen gene-disease validity curation.
    Verify any of these directly at
    https://search.clinicalgenome.org/kb/gene-validity/<GENE_SYMBOL>.
    """

    @classmethod
    def setUpClass(cls):
        cls.provider = _provider()

    def test_brca1_is_definitive_for_breast_cancer_predisposition(self):
        result = self.provider.query("BRCA1")
        self.assertTrue(result.found)
        self.assertEqual(result.clinical_validity_summary, "Definitive")
        labels = {v.disease_label for v in result.gene_disease_validities}
        self.assertIn("BRCA1-related cancer predisposition", labels)

    def test_cftr_is_definitive_for_cystic_fibrosis(self):
        result = self.provider.query("CFTR")
        self.assertTrue(result.found)
        self.assertEqual(result.clinical_validity_summary, "Definitive")
        self.assertEqual(result.gene_disease_validities[0].disease_label, "cystic fibrosis")
        self.assertEqual(result.gene_disease_validities[0].moi, "AR")

    def test_chek2_strongest_validity_ignores_its_own_refuted_and_limited_entries(self):
        """
        CHEK2 has three real curations: Definitive (CHEK2-related cancer
        predisposition), Refuted (familial ovarian cancer), and Limited
        (HNPCC) -- a genuine multi-disease gene. `strongest_validity`
        must surface the Definitive entry, not average or pick the
        Refuted one just because it's also real ClinGen data.
        """
        result = self.provider.query("CHEK2")
        self.assertEqual(len(result.gene_disease_validities), 3)
        self.assertEqual(result.clinical_validity_summary, "Definitive")
        self.assertEqual(result.strongest_validity.disease_label, "CHEK2-related cancer predisposition")

    def test_akap9_long_qt_syndrome_is_disputed(self):
        result = self.provider.query("AKAP9")
        self.assertTrue(result.found)
        self.assertEqual(result.clinical_validity_summary, "Disputed")

    def test_acat2_has_no_known_disease_relationship(self):
        result = self.provider.query("ACAT2")
        self.assertTrue(result.found)
        self.assertEqual(result.clinical_validity_summary, "No Known Disease Relationship")

    def test_adam17_congenital_heart_disease_is_limited(self):
        result = self.provider.query("ADAM17")
        self.assertTrue(result.found)
        self.assertEqual(result.clinical_validity_summary, "Limited")

    def test_myh7_validity_is_definitive_despite_dosage_score_zero(self):
        """
        Gene-disease clinical validity and dosage sensitivity are two
        independent ClinGen curation axes. MYH7 disease is driven by
        dominant-negative missense variants, not haploinsufficiency, so
        its dosage score is 0 ('no evidence') even though its
        gene-disease validity is Definitive -- PP1/BS4 read validity;
        PVS1 reads dosage. They must not be conflated.
        """
        result = self.provider.query("MYH7")
        self.assertEqual(result.clinical_validity_summary, "Definitive")
        self.assertEqual(result.dosage_sensitivity.haploinsufficiency_score, 0)

    def test_unknown_gene_is_not_found(self):
        result = self.provider.query("NOTAREALGENE123")
        self.assertFalse(result.found)
        self.assertIsNone(result.clinical_validity_summary)


# ---------------------------------------------------------------------------
# PP1
# ---------------------------------------------------------------------------


class TestPP1(unittest.TestCase):
    """
    PP1 can never be *triggered* by this pipeline (no pedigree/linkage
    data is integrated) -- every case here checks that the ClinGen
    prerequisite gate is applied without GEPER ever fabricating a
    segregation conclusion it cannot actually support.
    """

    def test_established_gene_leaves_pp1_not_evaluated(self):
        result = ACMGRuleEngine._pp1(_clingen_result("BRCA1", "Definitive"))
        self.assertEqual(result.status, "not_evaluated")
        self.assertIn("segregation", result.rationale.lower())

    def test_refuted_gene_disease_relationship_rules_out_pp1(self):
        result = ACMGRuleEngine._pp1(_clingen_result("CHEK2", "Refuted"))
        self.assertEqual(result.status, "not_triggered")
        self.assertIn("Refuted", result.rationale)

    def test_disputed_gene_disease_relationship_rules_out_pp1(self):
        result = ACMGRuleEngine._pp1(_clingen_result("AKAP9", "Disputed"))
        self.assertEqual(result.status, "not_triggered")

    def test_no_known_disease_relationship_rules_out_pp1(self):
        result = ACMGRuleEngine._pp1(_clingen_result("ACAT2", "No Known Disease Relationship"))
        self.assertEqual(result.status, "not_triggered")

    def test_limited_validity_still_leaves_pp1_not_evaluated(self):
        # Limited is a positive (if weak) curation, not a negative one --
        # it does not rule PP1 out, unlike Refuted/Disputed/No Known.
        result = ACMGRuleEngine._pp1(_clingen_result("ADAM17", "Limited"))
        self.assertEqual(result.status, "not_evaluated")

    def test_no_clingen_curation_leaves_pp1_not_evaluated(self):
        result = ACMGRuleEngine._pp1({"skipped": False, "found": False})
        self.assertEqual(result.status, "not_evaluated")

    def test_pp1_status_is_never_triggered_regardless_of_input(self):
        """No ClinGen input, however favorable, can make PP1 'triggered' -- that would fabricate segregation evidence GEPER doesn't have."""
        for classification in ("Definitive", "Strong", "Moderate", "Limited", None):
            with self.subTest(classification):
                result = ACMGRuleEngine._pp1(_clingen_result("SOMEGENE", classification))
                self.assertNotEqual(result.status, "triggered")


# ---------------------------------------------------------------------------
# BS4
# ---------------------------------------------------------------------------


class TestBS4(unittest.TestCase):
    """BS4's benign-direction mirror of the PP1 tests above."""

    def test_established_gene_leaves_bs4_not_evaluated(self):
        result = ACMGRuleEngine._bs4(_clingen_result("CFTR", "Definitive"))
        self.assertEqual(result.status, "not_evaluated")

    def test_refuted_gene_disease_relationship_rules_out_bs4(self):
        result = ACMGRuleEngine._bs4(_clingen_result("CHEK2", "Refuted"))
        self.assertEqual(result.status, "not_triggered")

    def test_no_known_disease_relationship_rules_out_bs4(self):
        result = ACMGRuleEngine._bs4(_clingen_result("ACAT2", "No Known Disease Relationship"))
        self.assertEqual(result.status, "not_triggered")

    def test_no_clingen_curation_leaves_bs4_not_evaluated(self):
        result = ACMGRuleEngine._bs4({"skipped": True})
        self.assertEqual(result.status, "not_evaluated")

    def test_bs4_status_is_never_triggered_regardless_of_input(self):
        for classification in ("Definitive", "Strong", "Moderate", "Limited", None):
            with self.subTest(classification):
                result = ACMGRuleEngine._bs4(_clingen_result("SOMEGENE", classification))
                self.assertNotEqual(result.status, "triggered")


# ---------------------------------------------------------------------------
# Wiring into the full engine
# ---------------------------------------------------------------------------


class TestEngineWiring(unittest.TestCase):
    def test_evaluate_reports_pp1_and_bs4_for_a_refuted_gene(self):
        result = ACMGRuleEngine().evaluate(clingen_result=_clingen_result("CHEK2", "Refuted"))
        pp1 = result["all_criteria"]["PP1"]
        bs4 = result["all_criteria"]["BS4"]
        self.assertEqual(pp1["status"], "not_triggered")
        self.assertEqual(bs4["status"], "not_triggered")
        self.assertIn(pp1, result["not_triggered_criteria"])

    def test_evaluate_still_works_for_callers_that_pass_no_clingen_result(self):
        result = ACMGRuleEngine().evaluate()
        self.assertEqual(result["all_criteria"]["PP1"]["status"], "not_evaluated")
        self.assertEqual(result["all_criteria"]["BS4"]["status"], "not_evaluated")


if __name__ == "__main__":
    unittest.main()
