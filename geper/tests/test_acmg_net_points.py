"""
Report review round 10: `ACMGRuleEngine._combine` used to compute
`pathogenic_points`/`benign_points`/`net_points` purely to pick a
classification, then discard them -- the number that actually decides
every ACMG classification GEPER produces was never recorded anywhere.
Meanwhile a completely separate, pre-ACMG scoring system
(`InterpretationEngine`'s own `significance_score`, unrelated to the
Tavtigian 28-criteria point table) was serialized into
`geper_results.json` right next to `acmg_classification`, reading as
if it were the ACMG score to a JSON consumer with no repo access --
confirmed as a real mistake, not a hypothetical one.

Round 11: this file's first version proved `_combine`'s arithmetic
against the five real, Colab-verified nuclear variants in
`test_data/nuclear_test_with_mt.vcf`, but did so by hand-copying WHICH
ACMG criteria triggered for each variant into a `_REAL_RUN_CASES`
dict, rather than deriving that from real evidence run through the
real per-criterion rule methods. That test could never fail if a rule
method's behavior changed (e.g. `_pm2`'s gnomAD-frequency threshold),
because the "triggered" list was typed by a human, not computed -- the
exact class of bug round 8 shipped once already (a hand-copied dict
shape production never produced, caught only by a Colab diff against
real `_process_variant` output). A test that cannot fail when the
engine it's testing changes is worse than no test: it reads as
coverage without providing any.

`TestRealEvidenceDrivesRealClassification` below replaces that
hand-copied version entirely. It loads the raw per-stage evidence
dicts (ClinVar, gnomAD, ClinGen, transcript structure, ...) verbatim
from `tests/fixtures/offline_evidence/nuclear_test_with_mt_evidence.json`
-- itself extracted from a real, network- and model-backed GEPER run
(`extract_fixture.py`, never hand-authored) -- and runs them through
the REAL `InterpretationEngine.interpret()` / `ACMGRuleEngine.evaluate()`
/ `report.json_builder.build_variant_result()`, exactly as
`pipeline/orchestrator.py::_process_variant` does. Nothing about which
criteria trigger is asserted or assumed here; only the final
`net_points`/`classification` are checked against the same verified
run's own recorded output. If any per-criterion rule method's behavior
changes, this test fails -- there is no hand-typed intermediate value
standing between the evidence and the assertion.

SCOPE BOUNDARY -- READ BEFORE TRUSTING THIS FILE AS FULL COVERAGE:
this offline path only exercises code downstream of evidence
collection (the ACMG rule engine and the report renderers). Both
`InterpretationEngine.interpret()` and `ACMGRuleEngine.evaluate()` are
pure functions of plain dicts -- no model instance or network client
ever appears in their call graph -- which is what makes replaying them
against recorded evidence valid and fast (no GPU, no network, no
Ensembl/ClinVar/gnomAD round-trip). It does NOT verify anything about
whether ClinVar/gnomAD/Ensembl/model inference itself still returns
the right dict shape or the right values for these variants -- a
change to an evidence-*gathering* stage still requires a real Colab
run to verify, same as before this file existed. See
`tests/fixtures/offline_evidence/README.md` for this same boundary
stated at the fixture-directory level, and
`tests/fixtures/offline_evidence/loader.py` for the schema-validation
guard that fails loudly if the recorded evidence has drifted from what
production's stages currently emit.

The `TestCombineArithmetic*` classes below are a separate, narrower
kind of test: pure unit tests of `_combine`'s point-tallying and
short-circuit logic against small, deliberately synthetic
`CriterionResult` sets, independent of any real variant's evidence --
they test the arithmetic in isolation, not "does the engine derive
these criteria from evidence," so hand-constructing their inputs is
not the round-8 failure mode.
"""

import os
import sys
import unittest

from pipeline.acmg_rules import ACMGRuleEngine, CriterionResult
from pipeline.interpretation import InterpretationEngine
from report import clinical_report_builder as crb
from report import report_generator as report_generator_module
from report import summary as summary_module
from report import summary_short as summary_short_module
from report.json_builder import build_variant_result

_FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "offline_evidence")
if _FIXTURE_DIR not in sys.path:
    sys.path.insert(0, _FIXTURE_DIR)

from loader import load_fixture  # noqa: E402 -- must follow the sys.path insert above

# Loaded once at collection time -- a stale/malformed fixture (see
# `loader.py`'s schema-validation guard) fails the whole module loudly
# rather than each test individually reporting the same root cause.
_FIXTURE = load_fixture("nuclear_test_with_mt_evidence.json")

# Exactly `InterpretationEngine.interpret()`'s own keyword parameters
# (`pipeline/interpretation.py`) -- the fixture's "evidence" dict
# carries several additional keys (`sequence_context`,
# `dna_model_results`, `indigenomes_result`, ...) that `interpret()`
# does not accept, so this whitelist is required rather than
# `interpret(**evidence)`.
_INTERPRET_KWARGS = (
    "variant_dict",
    "dna_models_used",
    "clinvar_result",
    "dbsnp_result",
    "protein_result",
    "blast_result",
    "alphamissense_result",
    "mmsplice_result",
    "gnomad_result",
    "conservation_result",
    "clingen_result",
    "uniprot_result",
    "interpro_result",
    "alphafold_result",
    "rna_result",
    "ensemble_result",
    "transcript_result",
    "clinvar_codon_result",
    "spliceformer_result",
    "splicebert_result",
    "hpo_result",
    "phenotype_result",
    "functional_evidence_result",
)


def _run_real(label: str):
    """
    Replays the real `interpret()` -> `build_variant_result()` path
    (the same two calls `pipeline/orchestrator.py::_process_variant`
    makes) against one variant's recorded evidence. Returns
    `(interpretation_dict, variant_result_dict)`.
    """
    entry = _FIXTURE[label]
    evidence = entry["evidence"]

    interpret_kwargs = {name: evidence[name] for name in _INTERPRET_KWARGS}
    interpretation = InterpretationEngine().interpret(**interpret_kwargs)

    variant_result = build_variant_result(
        variant_dict=evidence["variant_dict"],
        sequence_context=evidence["sequence_context"],
        dna_model_results=evidence["dna_model_results"],
        rna_result=evidence["rna_result"],
        protein_result=evidence["protein_result"],
        blast_result=evidence["blast_result"],
        clinvar_result=evidence["clinvar_result"],
        dbsnp_result=evidence["dbsnp_result"],
        interpretation=interpretation,
        errors=evidence["errors"] or [],
        alphamissense_result=evidence["alphamissense_result"],
        mmsplice_result=evidence["mmsplice_result"],
        gnomad_result=evidence["gnomad_result"],
        indigenomes_result=evidence["indigenomes_result"],
        thousand_genomes_sas_result=evidence["thousand_genomes_sas_result"],
        conservation_result=evidence["conservation_result"],
        clingen_result=evidence["clingen_result"],
        uniprot_result=evidence["uniprot_result"],
        interpro_result=evidence["interpro_result"],
        alphafold_result=evidence["alphafold_result"],
        transcript_result=evidence["transcript_result"],
        clinvar_codon_result=evidence["clinvar_codon_result"],
        hpo_result=evidence["hpo_result"],
        orphanet_result=evidence["orphanet_result"],
        functional_evidence_result=evidence["functional_evidence_result"],
        normalization_result=evidence["normalization_result"],
        spliceformer_result=evidence["spliceformer_result"],
        splicebert_result=evidence["splicebert_result"],
        ai_splicing_ensemble_result=evidence["ensemble_result"],
        ai_model_status=evidence["ai_model_status"],
    )
    return interpretation, variant_result


class TestRealEvidenceDrivesRealClassification(unittest.TestCase):
    """
    Every triggered criterion below is DERIVED by the real
    `ACMGRuleEngine.evaluate()` from recorded real evidence -- nothing
    here is a hand-typed "these are the criteria that fire" list. Only
    the final numbers are compared against the same verified run's own
    recorded output.
    """

    def test_all_five_real_variants_match_verified_run(self):
        for label, entry in _FIXTURE.items():
            with self.subTest(variant=label):
                interpretation, _ = _run_real(label)
                acmg_evaluation = interpretation["acmg_evaluation"]
                expected = entry["expected"]
                self.assertEqual(acmg_evaluation["net_points"], expected["acmg_net_points"])
                self.assertEqual(acmg_evaluation["pathogenic_points"], expected["acmg_pathogenic_points"])
                self.assertEqual(acmg_evaluation["benign_points"], expected["acmg_benign_points"])
                self.assertEqual(acmg_evaluation["classification"], expected["acmg_classification"])

    def test_net_points_threads_into_interpretation_result(self):
        # `interpretation_result` (Phase 2, `build_interpretation_result`)
        # must expose the exact same numbers `acmg_evaluation` computed --
        # a second, independent read of the same real run, not a repeat
        # of the assertion above via a different hand-typed value.
        for label, entry in _FIXTURE.items():
            with self.subTest(variant=label):
                interpretation, _ = _run_real(label)
                ir = interpretation["interpretation_result"]
                expected = entry["expected"]
                self.assertEqual(ir["acmg_net_points"], expected["acmg_net_points"])
                self.assertEqual(ir["acmg_classification"], expected["acmg_classification"])
                self.assertNotIn("significance_score", interpretation)
                self.assertIn("legacy_pre_acmg_significance_score", interpretation)

    def test_net_points_band_label_matches_real_classification(self):
        _EXPECTED_BAND = {
            "Pathogenic": "≥10",
            "Uncertain Significance": "0–5",
            "Likely Benign": "≤−1",
        }
        for label, entry in _FIXTURE.items():
            with self.subTest(variant=label):
                classification = entry["expected"]["acmg_classification"]
                band = crb.acmg_net_points_band_label(classification)
                self.assertEqual(band, _EXPECTED_BAND[classification])


class TestRealEvidenceDrivesRealReports(unittest.TestCase):
    """Same real `interpret()` -> `build_variant_result()` replay as
    above, checked one layer further downstream: the three renderers
    that read `clinical_report`."""

    def test_markdown_shows_net_points_and_band(self):
        label = next(lbl for lbl, e in _FIXTURE.items() if e["expected"]["acmg_classification"] == "Pathogenic")
        _, variant_result = _run_real(label)
        gen = report_generator_module.ReportGenerator()
        lines = gen._render_clinical_report(variant_result["candidate_interpretation"], {})
        text = "\n".join(lines)
        self.assertIn("Net points", text)
        net_points = _FIXTURE[label]["expected"]["acmg_net_points"]
        self.assertIn(str(int(net_points)), text)

    def test_full_pdf_variant_section_shows_net_points(self):
        label = next(lbl for lbl, e in _FIXTURE.items() if e["expected"]["acmg_classification"] == "Pathogenic")
        _, variant_result = _run_real(label)
        styles = summary_module._build_stylesheet()
        flow = summary_module._build_variant_section(1, variant_result, styles)
        text = "\n".join(getattr(f, "text", "") for f in flow if hasattr(f, "text"))
        self.assertIn("Net points", text)

    def test_full_pdf_clinician_summary_table_shows_net_points_inline(self):
        label = next(lbl for lbl, e in _FIXTURE.items() if e["expected"]["acmg_classification"] == "Pathogenic")
        _, variant_result = _run_real(label)
        net_points = _FIXTURE[label]["expected"]["acmg_net_points"]
        styles = summary_module._build_stylesheet()
        table = summary_module._build_clinician_summary_table([variant_result], styles)
        row = table._cellvalues[1]
        classification_cell_text = getattr(row[2], "text", "")
        self.assertIn("Pathogenic", classification_cell_text)
        self.assertIn(f"net {int(net_points)}", classification_cell_text)

    def test_short_pdf_classification_text_includes_net_points(self):
        label = next(lbl for lbl, e in _FIXTURE.items() if e["expected"]["acmg_classification"] == "Likely Benign")
        _, variant_result = _run_real(label)
        net_points = _FIXTURE[label]["expected"]["acmg_net_points"]
        text = summary_short_module._classification_text(variant_result["candidate_interpretation"])
        self.assertIn("Likely Benign", text)
        self.assertIn(f"net {int(net_points)}", text)


def _cr(code, direction, strength, status="triggered"):
    return CriterionResult(code=code, direction=direction, strength=strength, status=status, rationale="test")


class TestCombineArithmeticStandalone(unittest.TestCase):
    """Pure unit tests of `_combine`'s point-tallying and short-circuit
    logic against small, deliberately synthetic criterion sets -- see
    this module's docstring for why hand-constructing these particular
    inputs is not the round-8 failure mode: nothing here claims to
    represent what any real variant's evidence would actually trigger."""

    def test_bp6_structurally_excluded_from_net(self):
        criteria = {
            code: _cr(code, direction, strength)
            for code, direction, strength in [
                ("BS3", "benign", "strong"),
                ("BP1", "benign", "supporting"),
                ("BP4", "benign", "supporting"),
                ("BP6", "benign", "supporting"),
                ("PM1", "pathogenic", "moderate"),
            ]
        }
        result = ACMGRuleEngine._combine(criteria)
        # BS3 (4) + BP1 (1) + BP4 (1) = 6 -- BP6's own supporting point
        # must NOT be added, even though it triggered.
        self.assertEqual(result.benign_points, 6.0)
        self.assertEqual(result.pathogenic_points, 2.0)

    def test_ba1_short_circuit_leaves_points_none_not_fabricated_zero(self):
        criteria = {"BA1": _cr("BA1", "benign", "stand_alone")}
        result = ACMGRuleEngine._combine(criteria)
        self.assertEqual(result.classification, "Benign")
        self.assertIsNone(result.net_points)
        self.assertIsNone(result.pathogenic_points)
        self.assertIsNone(result.benign_points)


class TestNetPointsBandLabel(unittest.TestCase):
    def test_benign_via_ba1_has_no_band(self):
        # BA1's short-circuit never runs the point tally -- there is no
        # band to name.
        self.assertIsNone(crb.acmg_net_points_band_label("Benign"))

    def test_unclassified_has_no_band(self):
        self.assertIsNone(crb.acmg_net_points_band_label(None))


if __name__ == "__main__":
    unittest.main()
