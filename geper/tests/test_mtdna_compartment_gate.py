"""
Round 14, B2/B3: the mtDNA compartment gate.

Replaces round 8's whole-variant rejection (`is_mitochondrial_chrom` at
the top of `_process_variant`, returning a bare `out_of_scope` record --
see git history / `tests/test_mitochondrial_out_of_scope.py`, removed
this round) with per-criterion, per-evidence-source gating: a chrM
variant now runs `ACMGRuleEngine.evaluate()` for real, but PVS1 / PM2 /
BA1 / BS1 / PP3 / BP4 / BP7 are pre-marked `not_evaluated` with their own
honest reason before their real logic would run, and PS1 / PM1 / PM4 /
PM5 / BP1 / BP3 are additionally gated -- but only for confirmed
non-protein-coding mitochondrial genes (22 tRNA + 2 rRNA), derived from
the real Ensembl `biotype` on `transcript_result["gene_biotype"]`
(round 14, B1), never a hardcoded gene list.

B3 correction: the per-finding disclaimer originally said "of the
remaining 12, only those with a protein-coding transcript for this gene
actually ran" -- false for 6 of those 12 (PS3, BS3, PP1, PP4, BS4, BP6
apply to any mitochondrial gene class and ran on the RNA-gene case too).
`mtdna_interpretation_disclaimer(transcript_result)` is now a function of
gene class, not a fixed string; the tests below assert its actual
factual claims per gene class, not just its presence.

B3 also resolved a real missing-vs-empty ambiguity: PM1 previously
landed on the generic "InterPro domain annotation was unavailable"
message for the MT-ATP6 case purely because this file's own fixture
passed empty `uniprot_result`/`interpro_result` dicts -- an artifact of
an incomplete test fixture, not a real compartment gap. Confirmed live
against the real InterPro REST API this round: MT-ATP6 (P00846), MT-CO1
(P00395), and MT-ND1 (P03886) all have real, substantial domain/
active-site/conserved-site annotations (12, 13, and 7 entries
respectively) -- InterPro genuinely covers mitochondrially-encoded
proteins. m.8993T>G's real residue, 156, falls inside MT-ATP6's real
InterPro active-site region IPR023011/PS00449 (155-164), confirmed via
the same live query. `_MT_ATP6_INTERPRO_RESULT` below uses that real
data, so PM1 now genuinely triggers in `TestProteinCodingMtVariant`
rather than landing on an artifact "unavailable" message -- no gating
change was needed (PM1 was never moved into the compartment-inapplicable
set), only the test fixture's own completeness.

Real facts used below, not invented: MT-ATP6's real GRCh38 transcript
structure (`ENST00000361899`, single exon 8527-9207, real CDS sequence),
m.8993T>G's real, textbook consequence (p.Leu156Arg, the NARP/Leigh
variant), and its real InterPro/UniProt domain data (all independently
confirmed live in round 14, B1/B3). MT-TL1's real biotype (`Mt_tRNA`,
confirmed live against Ensembl) and m.3243A>G, the real
ClinVar-pathogenic MELAS variant already used by the (now-removed)
round-8 test fixture.

SCOPE BOUNDARY: `pipeline.orchestrator` is deliberately NOT imported
here -- confirmed this round (`import pipeline.orchestrator` alone took
218s / ~275MB peak, a TensorFlow/absl import chain) to be exactly the
risk the removed round-8 test file's own docstring already warned
against on this machine. `_process_variant`'s own `is_mtdna_variant`
gate (which decides whether `_run_gnomad_stage`/`_run_alphamissense_stage`/
etc. are ever CALLED at all) can only be verified end-to-end in Colab.
What IS tested here, safely, is everything `pipeline/acmg_rules.py`
itself owns: the small, standalone `mtdna_*_skip_result()` functions
`_process_variant` calls instead of the real stage methods (proving what
each one WOULD produce, and that it's an honest, reasoned skip, never a
silent absence), and -- the stronger, complementary check -- that
`ACMGRuleEngine.evaluate()` refuses to consume gnomAD/AlphaMissense/
MMSplice/ensemble evidence for a chrM variant even when such evidence IS
supplied, so a bug that accidentally let one of those stages run for an
MT variant still could not corrupt the classification.
"""

import unittest

from pipeline.acmg_rules import (
    ACMGRuleEngine,
    mtdna_alphamissense_skip_result,
    mtdna_ensemble_skip_result,
    mtdna_gnomad_skip_result,
    mtdna_interpretation_disclaimer,
    mtdna_interpretation_disclaimer_short,
    mtdna_mmsplice_skip_result,
    mtdna_splice_plugin_skip_result,
)
from pipeline.interpretation import InterpretationEngine
from report import report_generator as report_generator_module
from report import summary as summary_module
from report import summary_short as summary_short_module
from report.json_builder import build_variant_result

# Real GRCh38 transcript structure for MT-ATP6 (ENST00000361899),
# confirmed live against Ensembl in round 14, B1.
_MT_ATP6_CDS_SEQUENCE = (
    "ATGAACGAAAATCTGTTCGCTTCATTCATTGCCCCCACAATCCTAGGCCTACCCGCCGCAGTACTGATCATTCTATTTCCCCCTCTATTGATCCCCACC"
    "TCCAAATATCTCATCAACAACCGACTAATCACCACCCAACAATGACTAATCAAACTAACCTCAAAACAAATGATAACCATACACAACACTAAAGGACGA"
    "ACCTGATCTCTTATACTAGTATCCTTAATCATTTTTATTGCCACAACTAACCTCCTCGGACTCCTGCCTCACTCATTTACACCAACCACCCAACTATCT"
    "ATAAACCTAGCCATGGCCATCCCCTTATGAGCGGGCACAGTGATTATAGGCTTTCGCTCTAAGATTAAAAATGCCCTAGCCCACTTCTTACCACAAGGC"
    "ACACCTACACCCCTTATCCCCATACTAGTTATTATCGAAACCATCAGCCTACTCATTCAACCAATAGCCCTGGCCGTACGCCTAACCGCTAACATTACT"
    "GCAGGCCACCTACTCATGCACCTAATTGGAAGCGCCACCCTAGCAATATCAACCATTAACCTTCCCTCTACACTTATCATCTTCACAATTCTAATTCTA"
    "CTGACTATCCTAGAAATCGCTGTCGCCTTAATCCAAGCCTACGTTTTCACACTTCTAGTAAGCCTCTACCTGCACGACAACACATAA"
)

_MT_ATP6_TRANSCRIPT_RESULT = {
    "skipped": False,
    "found": True,
    "gene_symbol": "MT-ATP6",
    "assembly": "GRCh38",
    "source": "ensembl_gtf_cache",
    "gene_biotype": "protein_coding",
    "transcript_span": [8527, 9207],
    "transcript": {
        "transcript_id": "ENST00000361899",
        "gene_symbol": "MT-ATP6",
        "chrom": "MT",
        "strand": 1,
        "exons": [{"start": 8527, "end": 9207}],
        "cds_genomic_start": 8527,
        "cds_genomic_end": 9207,
        "protein_length": 226,
        "is_mane_select": False,
        "is_canonical": True,
        "source": "ensembl_gtf_cache",
        "cds_sequence": _MT_ATP6_CDS_SEQUENCE,
    },
}

# MT-ATP6's real UniProt accession and a real subset of its InterPro
# domain/site annotations (confirmed live against the InterPro REST API,
# `entry/all/protein/uniprot/P00846/`, round 14 B3) -- see this module's
# docstring. `IPR000568` is a `family`-type entry (excluded from PM1's
# domain-overlap check, same as every other criterion's family-entry
# exclusion); `IPR023011`/`PS00449` is the real active-site region
# (155-164) that m.8993T>G's real residue, 156, falls inside.
_MT_ATP6_UNIPROT_RESULT = {
    "skipped": False,
    "found": True,
    "accession": "P00846",
    "entry_name": "ATP6_HUMAN",
    "protein_name": "ATP synthase F0 subunit 6",
    "reviewed": True,
}

_MT_ATP6_INTERPRO_DOMAINS = [
    {
        "interpro_accession": "IPR000568",
        "name": "ATP synthase, F0 complex, subunit A",
        "short_name": None,
        "type": "family",
        "member_database": "interpro",
        "member_accession": "IPR000568",
        "start": 1,
        "end": 226,
    },
    {
        "interpro_accession": "IPR023011",
        "name": "ATP synthase, F0 complex, subunit A, active site",
        "short_name": None,
        "type": "active_site",
        "member_database": "prosite_profiles",
        "member_accession": "PS00449",
        "start": 155,
        "end": 164,
    },
    {
        "interpro_accession": "IPR000568",
        "name": "ATP synthase A chain",
        "short_name": None,
        "type": "domain",
        "member_database": "pfam",
        "member_accession": "PF00119",
        "start": 20,
        "end": 222,
    },
]

_MT_ATP6_INTERPRO_RESULT = {
    "skipped": False,
    "found": True,
    "accession": "P00846",
    "source": "interpro_rest_api",
    "domains": _MT_ATP6_INTERPRO_DOMAINS,
    "error": None,
    "api_version": "109.0",
    "protein_position": 156,
    # Mirrors `InterProLookup.query_variant`'s own filter: non-family
    # entries whose [start, end] span contains residue 156.
    "affected_domains": [
        d for d in _MT_ATP6_INTERPRO_DOMAINS if d["type"] != "family" and d["start"] <= 156 <= d["end"]
    ],
    "protein_position_basis": "transcript_cds",
}

# MT-TL1's real biotype, confirmed live against Ensembl this round --
# `_fetch_live`'s own "no protein-coding transcript" shape (no CDS at
# all, by biology, not a lookup gap).
_MT_TL1_TRANSCRIPT_RESULT = {
    "skipped": False,
    "found": False,
    "gene_symbol": "MT-TL1",
    "transcript": None,
    "reason": "no protein-coding transcript with a translation was returned for MT-TL1.",
    "gene_biotype": "Mt_tRNA",
}

_MT_ATP6_VARIANT = {"chrom": "MT", "pos": 8993, "ref": "T", "alt": "G"}
_MT_TL1_VARIANT = {"chrom": "MT", "pos": 3243, "ref": "A", "alt": "G"}

_MTDNA_STRUCTURAL_CODES = ("PVS1", "PM2", "BA1", "BS1", "PP3", "BP4", "BP7")
_MTDNA_PROTEIN_DEPENDENT_CODES = ("PS1", "PM1", "PM4", "PM5", "BP1", "BP3")
_MTDNA_UNGATED_CODES = ("PS3", "BS3", "PP1", "PP4", "BS4", "BP6")


def _evaluate(variant_dict, transcript_result, **overrides):
    kwargs = dict(
        clinvar_result={"records": []},
        dbsnp_result={"found": False},
        protein_result={},
        alphamissense_result=mtdna_alphamissense_skip_result(),
        mmsplice_result=mtdna_mmsplice_skip_result(),
        gnomad_result=mtdna_gnomad_skip_result(),
        conservation_result={},
        clingen_result={},
        interpro_result={},
        ensemble_result=mtdna_ensemble_skip_result(),
        variant_dict=variant_dict,
        transcript_result=transcript_result,
        clinvar_codon_result={},
        uniprot_result={},
        spliceformer_result=mtdna_splice_plugin_skip_result(),
        splicebert_result=mtdna_splice_plugin_skip_result(),
        hpo_result={},
        phenotype_result=None,
        functional_evidence_result={},
    )
    kwargs.update(overrides)
    return ACMGRuleEngine().evaluate(**kwargs)


class TestMtdnaEvidenceSkipResults(unittest.TestCase):
    """The small, standalone functions `_process_variant` calls instead
    of the real stage methods for a chrM variant -- see this module's
    docstring for why these, not `pipeline.orchestrator`, are what's
    testable here."""

    def test_gnomad_skip_names_the_separate_callset(self):
        result = mtdna_gnomad_skip_result()
        self.assertTrue(result["skipped"])
        self.assertFalse(result["found"])
        self.assertIn("mitochondrial callset", result["reason"])
        self.assertIn("_DATASET_BY_BUILD", result["reason"])

    def test_alphamissense_skip_is_honest_not_silent(self):
        result = mtdna_alphamissense_skip_result()
        self.assertTrue(result["skipped"])
        self.assertIn("mitochondrial genome", result["reason"])

    def test_mmsplice_skip_shape_matches_real_stage_convention(self):
        result = mtdna_mmsplice_skip_result()
        self.assertFalse(result["supported"])
        self.assertFalse(result["predicted"])
        self.assertIn("mitochondrial genome", result["skip_reason"])

    def test_ensemble_skip_shape_matches_real_stage_convention(self):
        result = mtdna_ensemble_skip_result()
        self.assertEqual(result["models_used"], [])
        self.assertIsNone(result["classification"])
        self.assertEqual(result["basis"], "mitochondrial_compartment")

    def test_splice_plugin_skip_shape_matches_real_stage_convention(self):
        result = mtdna_splice_plugin_skip_result()
        self.assertFalse(result["available"])
        self.assertIsNone(result["classification"])


class TestProteinCodingMtVariant(unittest.TestCase):
    """(a) m.8993T>G, MT-ATP6 -- real NARP/Leigh variant, real
    protein-coding transcript."""

    def setUp(self):
        self.acmg = _evaluate(
            _MT_ATP6_VARIANT,
            _MT_ATP6_TRANSCRIPT_RESULT,
            uniprot_result=_MT_ATP6_UNIPROT_RESULT,
            interpro_result=_MT_ATP6_INTERPRO_RESULT,
        )
        self.by_code = self.acmg["all_criteria"]

    def test_pm1_triggers_on_real_interpro_active_site_overlap(self):
        # Resolves round 14 B3's item 2: with real InterPro/UniProt
        # evidence (not the empty dicts an earlier fixture used), PM1
        # genuinely runs and triggers -- residue 156 falls inside
        # MT-ATP6's real active-site region -- rather than landing on
        # the generic "unavailable" message, which was a fixture
        # artifact, not a real compartment gap.
        pm1 = self.by_code["PM1"]
        self.assertEqual(pm1["status"], "triggered")
        self.assertIn("156", pm1["rationale"])
        self.assertNotIn("unavailable", pm1["rationale"])

    def test_seven_structural_criteria_not_evaluated_with_distinct_reasons(self):
        reasons = set()
        for code in _MTDNA_STRUCTURAL_CODES:
            with self.subTest(code=code):
                entry = self.by_code[code]
                self.assertEqual(entry["status"], "not_evaluated")
                reasons.add(entry["rationale"])
        # Not one shared generic string -- PVS1's/PM2's-family/PP3-BP4's-
        # family/BP7's reasons are each their own text (some are shared
        # between the two members of a pair for the same root cause, so
        # this is < 7, but must be > 1).
        self.assertGreater(len(reasons), 1)

    def test_pvs1_reason_names_polyplasmy_not_haploinsufficiency(self):
        rationale = self.by_code["PVS1"]["rationale"]
        self.assertIn("polyplasmic", rationale)
        self.assertIn("McCormick", rationale)

    def test_pm2_ba1_bs1_reason_names_the_query_target_gap(self):
        for code in ("PM2", "BA1", "BS1"):
            with self.subTest(code=code):
                rationale = self.by_code[code]["rationale"]
                self.assertIn("_DATASET_BY_BUILD", rationale)
                self.assertIn("never", rationale)

    def test_pp3_bp4_bp7_reason_names_no_spliceosome(self):
        for code in ("PP3", "BP4", "BP7"):
            with self.subTest(code=code):
                rationale = self.by_code[code]["rationale"]
                self.assertIn("spliceosome", rationale)

    def test_protein_dependent_criteria_are_not_rna_gene_gated(self):
        # A real protein-coding transcript resolved -- none of these six
        # may carry the RNA-gene biology reason; each is free to run its
        # own real logic (whatever status that logic reaches).
        for code in _MTDNA_PROTEIN_DEPENDENT_CODES:
            with self.subTest(code=code):
                rationale = self.by_code[code]["rationale"]
                self.assertNotIn("biotype", rationale)
                self.assertNotIn("not a missing lookup", rationale)

    def test_ungated_criteria_never_carry_mtdna_structural_reasons(self):
        for code in _MTDNA_UNGATED_CODES:
            with self.subTest(code=code):
                rationale = self.by_code[code]["rationale"]
                self.assertNotIn("polyplasmic", rationale)
                self.assertNotIn("_DATASET_BY_BUILD", rationale)
                self.assertNotIn("spliceosome", rationale)

    def test_classification_reflects_only_criteria_that_actually_ran(self):
        # No pathogenic/benign points can come from the 7 structurally
        # gated criteria (none of them may be "triggered").
        triggered_codes = {c["code"] for c in self.acmg["triggered_criteria"]}
        self.assertFalse(triggered_codes & set(_MTDNA_STRUCTURAL_CODES))


class TestRnaGeneMtVariant(unittest.TestCase):
    """(b) m.3243A>G, MT-TL1 -- the real MELAS variant, a tRNA gene with
    no protein-coding transcript."""

    def setUp(self):
        self.acmg = _evaluate(_MT_TL1_VARIANT, _MT_TL1_TRANSCRIPT_RESULT)
        self.by_code = self.acmg["all_criteria"]

    def test_seven_structural_criteria_still_gated(self):
        for code in _MTDNA_STRUCTURAL_CODES:
            with self.subTest(code=code):
                self.assertEqual(self.by_code[code]["status"], "not_evaluated")

    def test_six_protein_dependent_criteria_gated_with_biology_reason(self):
        for code in _MTDNA_PROTEIN_DEPENDENT_CODES:
            with self.subTest(code=code):
                entry = self.by_code[code]
                self.assertEqual(entry["status"], "not_evaluated")
                rationale = entry["rationale"]
                self.assertIn("MT-TL1", rationale)
                self.assertIn("Mt_tRNA", rationale)
                self.assertIn("not a missing lookup", rationale)

    def test_ungated_criteria_can_still_run(self):
        # "Can still run" here means: not blocked by the RNA-gene gate.
        # Each may land on its own ordinary not_evaluated/not_triggered
        # for lack of supplied evidence (empty dicts in this fixture) --
        # the assertion is that NONE of them carry the RNA-gene reason.
        for code in _MTDNA_UNGATED_CODES:
            with self.subTest(code=code):
                rationale = self.by_code[code]["rationale"]
                self.assertNotIn("Mt_tRNA", rationale)
                self.assertNotIn("not a missing lookup", rationale)


class TestMtdnaGateIgnoresEvidenceEvenIfSupplied(unittest.TestCase):
    """(c), the part safely testable without `pipeline.orchestrator`
    (see module docstring): defense-in-depth at the CONSUMPTION layer.
    Even when gnomAD/AlphaMissense/MMSplice/ensemble evidence IS
    supplied -- simulating a bug that let one of those stages run for an
    MT variant anyway -- the compartment gate still refuses to let PM2/
    BA1/BS1/PP3/BP4/BP7 use it."""

    def test_populated_gnomad_evidence_is_still_ignored(self):
        # A gnomAD result that WOULD trigger PM2/BA1 if it were nuclear
        # evidence (very rare -> PM2; absent -> BA1's "novel" branch).
        loud_gnomad = {"found": True, "af": 0.0, "an": 100000, "skipped": False}
        acmg = _evaluate(_MT_ATP6_VARIANT, _MT_ATP6_TRANSCRIPT_RESULT, gnomad_result=loud_gnomad)
        by_code = acmg["all_criteria"]
        for code in ("PM2", "BA1", "BS1"):
            with self.subTest(code=code):
                self.assertEqual(by_code[code]["status"], "not_evaluated")
                self.assertIn("_DATASET_BY_BUILD", by_code[code]["rationale"])

    def test_populated_alphamissense_and_mmsplice_evidence_still_ignored(self):
        loud_alphamissense = {"skipped": False, "am_class": "likely_pathogenic", "am_pathogenicity": 0.95}
        loud_mmsplice = {"supported": True, "predicted": True, "delta_logit_psi": -3.0}
        loud_ensemble = {"models_used": ["enformer", "borzoi"], "classification": "large_effect"}
        acmg = _evaluate(
            _MT_ATP6_VARIANT,
            _MT_ATP6_TRANSCRIPT_RESULT,
            alphamissense_result=loud_alphamissense,
            mmsplice_result=loud_mmsplice,
            ensemble_result=loud_ensemble,
        )
        by_code = acmg["all_criteria"]
        for code in ("PP3", "BP4", "BP7"):
            with self.subTest(code=code):
                self.assertEqual(by_code[code]["status"], "not_evaluated")
                self.assertIn("spliceosome", by_code[code]["rationale"])

    def test_populated_alphamissense_does_not_leak_into_bp1(self):
        loud_alphamissense = {"skipped": False, "am_class": "likely_benign", "am_pathogenicity": 0.05}
        acmg = _evaluate(_MT_ATP6_VARIANT, _MT_ATP6_TRANSCRIPT_RESULT, alphamissense_result=loud_alphamissense)
        # BP1 is protein-dependent, not compartment-structural -- for a
        # real protein-coding gene it runs its OWN real logic, but must
        # never have consulted the supplied AlphaMissense evidence
        # (gated to None at the evaluate() call for chrM -- see
        # ACMGRuleEngine.evaluate's BP1 call site).
        bp1 = acmg["all_criteria"]["BP1"]
        self.assertNotIn("am_class", str(bp1))
        self.assertNotIn("AlphaMissense", bp1["rationale"])


class TestNuclearVariantsUnaffected(unittest.TestCase):
    """(d) -- the one that matters most. Real evidence from the round-10
    offline fixture (extracted from a real, verified run, commit
    3c854c9), replayed through the exact same `interpret()` call this
    file's own docstring and `test_acmg_net_points.py` already use.
    Asserts against the same known-correct net points/classifications a
    real Colab run produced -- if the mtDNA compartment gate touched
    anything on the nuclear path, one of these five would move."""

    @classmethod
    def setUpClass(cls):
        import os
        import sys

        fixture_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "offline_evidence")
        if fixture_dir not in sys.path:
            sys.path.insert(0, fixture_dir)
        from loader import load_fixture

        cls._fixture = load_fixture("nuclear_test_with_mt_evidence.json")

    def test_all_five_real_nuclear_variants_match_verified_run(self):
        interpret_kwargs_names = (
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
        for label, entry in self._fixture.items():
            with self.subTest(variant=label):
                evidence = entry["evidence"]
                kwargs = {name: evidence[name] for name in interpret_kwargs_names}
                interpretation = InterpretationEngine().interpret(**kwargs)
                acmg = interpretation["acmg_evaluation"]
                expected = entry["expected"]
                self.assertEqual(acmg["net_points"], expected["acmg_net_points"])
                self.assertEqual(acmg["classification"], expected["acmg_classification"])

    def test_no_nuclear_criterion_carries_an_mtdna_reason(self):
        # None of the mtDNA-specific reason fragments may appear anywhere
        # in a nuclear variant's criteria -- the gate must be chrom-keyed,
        # never accidentally global.
        for label, entry in self._fixture.items():
            evidence = entry["evidence"]
            acmg = ACMGRuleEngine().evaluate(
                clinvar_result=evidence["clinvar_result"],
                dbsnp_result=evidence["dbsnp_result"],
                protein_result=evidence["protein_result"],
                alphamissense_result=evidence["alphamissense_result"],
                mmsplice_result=evidence["mmsplice_result"],
                gnomad_result=evidence["gnomad_result"],
                conservation_result=evidence["conservation_result"],
                clingen_result=evidence["clingen_result"],
                interpro_result=evidence["interpro_result"],
                ensemble_result=evidence["ensemble_result"],
                variant_dict=evidence["variant_dict"],
                transcript_result=evidence["transcript_result"],
                clinvar_codon_result=evidence["clinvar_codon_result"],
                uniprot_result=evidence["uniprot_result"],
                spliceformer_result=evidence["spliceformer_result"],
                splicebert_result=evidence["splicebert_result"],
                hpo_result=evidence["hpo_result"],
                phenotype_result=evidence["phenotype_result"],
                functional_evidence_result=evidence["functional_evidence_result"],
            )
            for code, entry_dict in acmg["all_criteria"].items():
                with self.subTest(variant=label, code=code):
                    self.assertNotIn("polyplasmic", entry_dict["rationale"])
                    self.assertNotIn("_DATASET_BY_BUILD", entry_dict["rationale"])
                    self.assertNotIn("spliceosome", entry_dict["rationale"])
                    self.assertNotIn("Mt_tRNA", entry_dict["rationale"])
                    self.assertNotIn("Mt_rRNA", entry_dict["rationale"])


class TestPerFindingDisclaimer(unittest.TestCase):
    """(e) the disclaimer appears on chrM findings and NOT on nuclear
    ones, in all three renderers -- and (B3) asserts its actual factual
    claims per gene class, not just its presence. A test that only
    checked the text existed would have passed with the old, wrong
    wording just as easily as with the corrected one."""

    def _variant_result(self, variant_dict, transcript_result, uniprot_result=None, interpro_result=None):
        uniprot_result = uniprot_result or {}
        interpro_result = interpro_result or {}
        interpretation = InterpretationEngine().interpret(
            variant_dict=variant_dict,
            dna_models_used=[],
            clinvar_result={"records": []},
            dbsnp_result={"found": False},
            protein_result={},
            blast_result={},
            alphamissense_result=mtdna_alphamissense_skip_result(),
            mmsplice_result=mtdna_mmsplice_skip_result(),
            gnomad_result=mtdna_gnomad_skip_result(),
            conservation_result={},
            clingen_result={},
            uniprot_result=uniprot_result,
            interpro_result=interpro_result,
            alphafold_result={},
            rna_result={},
            ensemble_result=mtdna_ensemble_skip_result(),
            transcript_result=transcript_result,
            clinvar_codon_result={},
            spliceformer_result=mtdna_splice_plugin_skip_result(),
            splicebert_result=mtdna_splice_plugin_skip_result(),
            hpo_result={},
            phenotype_result=None,
            functional_evidence_result={},
        )
        return build_variant_result(
            variant_dict=variant_dict,
            sequence_context={"error": "not applicable to this test"},
            dna_model_results={},
            rna_result={},
            protein_result={},
            blast_result={},
            clinvar_result={"records": []},
            dbsnp_result={"found": False},
            interpretation=interpretation,
            errors=[],
            alphamissense_result=mtdna_alphamissense_skip_result(),
            mmsplice_result=mtdna_mmsplice_skip_result(),
            gnomad_result=mtdna_gnomad_skip_result(),
            transcript_result=transcript_result,
            ai_splicing_ensemble_result=mtdna_ensemble_skip_result(),
        )

    def _protein_coding_variant_result(self):
        return self._variant_result(
            _MT_ATP6_VARIANT, _MT_ATP6_TRANSCRIPT_RESULT, _MT_ATP6_UNIPROT_RESULT, _MT_ATP6_INTERPRO_RESULT
        )

    def _rna_gene_variant_result(self):
        return self._variant_result(_MT_TL1_VARIANT, _MT_TL1_TRANSCRIPT_RESULT)

    def _nuclear_variant_result(self):
        return {"variant": {"chrom": "17", "pos": 43106534, "ref": "C", "alt": "A"}, "candidate_interpretation": {}}

    def test_markdown_shows_disclaimer_only_on_mt_finding(self):
        gen = report_generator_module.ReportGenerator()
        mt_lines = gen._render_variant_section(1, self._protein_coding_variant_result())
        nuclear_lines = gen._render_variant_section(1, self._nuclear_variant_result())
        self.assertTrue(any("Mitochondrial (mtDNA) compartment notice" in line for line in mt_lines))
        self.assertFalse(any("Mitochondrial (mtDNA) compartment notice" in line for line in nuclear_lines))

    def test_markdown_disclaimer_names_heteroplasmy_and_mitomap(self):
        gen = report_generator_module.ReportGenerator()
        text = "\n".join(gen._render_variant_section(1, self._protein_coding_variant_result()))
        self.assertIn("heteroplasmy", text)
        self.assertIn("MITOMAP", text)
        self.assertIn("McCormick", text)

    def test_protein_coding_disclaimer_says_all_twelve_ran_never_the_old_wrong_claim(self):
        # The B3 fix: must state that PS3/BS3/PP1/PP4/BS4/BP6 (the
        # gene-class-independent 6) ran alongside the protein-dependent
        # 6 -- and must NOT contain the original, factually wrong "only
        # those with a protein-coding transcript ... actually ran"
        # framing that implied the other 6 didn't run.
        text = mtdna_interpretation_disclaimer(_MT_ATP6_TRANSCRIPT_RESULT)
        self.assertIn("PS1, PM1, PM4, PM5, BP1, BP3, PS3, BS3, PP1, PP4, BS4, BP6", text)
        self.assertIn("evaluated for real against this gene's protein-coding transcript", text)
        self.assertNotIn("only those with a protein-coding transcript for this gene actually ran", text)
        self.assertNotIn("biotype", text)

    def test_rna_gene_disclaimer_states_which_six_ran_and_which_six_gated(self):
        # Must explicitly say the gene-class-independent 6 STILL ran
        # (this is exactly what the original wording understated), and
        # separately name the 6 gated for lack of a protein-coding
        # transcript, with the real biotype.
        text = mtdna_interpretation_disclaimer(_MT_TL1_TRANSCRIPT_RESULT)
        self.assertIn("PS3, BS3, PP1, PP4, BS4, BP6", text)
        self.assertIn("were still evaluated for real", text)
        self.assertIn("PS1, PM1, PM4, PM5, BP1, BP3", text)
        self.assertIn("Mt_tRNA", text)
        self.assertNotIn("only those with a protein-coding transcript for this gene actually ran", text)

    def test_disclaimer_with_no_transcript_result_does_not_overclaim(self):
        # Gene class genuinely undetermined (no transcript_result at
        # all) -- must not claim either subset definitively ran/didn't.
        text = mtdna_interpretation_disclaimer(None)
        self.assertIn("could not be confirmed for this variant", text)
        self.assertNotIn("only those with a protein-coding transcript for this gene actually ran", text)

    def test_full_pdf_shows_disclaimer_only_on_mt_finding(self):
        styles = summary_module._build_stylesheet()
        mt_flow = summary_module._build_variant_section(1, self._protein_coding_variant_result(), styles)
        nuclear_flow = summary_module._build_variant_section(1, self._nuclear_variant_result(), styles)
        mt_text = "\n".join(getattr(f, "text", "") for f in mt_flow if hasattr(f, "text"))
        nuclear_text = "\n".join(getattr(f, "text", "") for f in nuclear_flow if hasattr(f, "text"))
        self.assertIn("Mitochondrial (mtDNA) compartment notice", mt_text)
        self.assertNotIn("Mitochondrial (mtDNA) compartment notice", nuclear_text)

    def test_short_pdf_shows_disclaimer_only_on_mt_finding(self):
        styles = summary_short_module._build_short_stylesheet()
        mt_flow = summary_short_module._build_variant_block(1, self._protein_coding_variant_result(), styles)
        nuclear_flow = summary_short_module._build_variant_block(1, self._nuclear_variant_result(), styles)
        mt_text = "\n".join(getattr(f, "text", "") for f in mt_flow if hasattr(f, "text"))
        nuclear_text = "\n".join(getattr(f, "text", "") for f in nuclear_flow if hasattr(f, "text"))
        self.assertIn("Mitochondrial (mtDNA) finding", mt_text)
        self.assertNotIn("Mitochondrial (mtDNA) finding", nuclear_text)

    def test_short_disclaimer_also_distinguishes_gene_class(self):
        protein_coding_text = mtdna_interpretation_disclaimer_short(_MT_ATP6_TRANSCRIPT_RESULT)
        rna_gene_text = mtdna_interpretation_disclaimer_short(_MT_TL1_TRANSCRIPT_RESULT)
        self.assertNotEqual(protein_coding_text, rna_gene_text)
        self.assertIn("Mt_tRNA", rna_gene_text)
        self.assertNotIn("Mt_tRNA", protein_coding_text)

    def test_short_disclaimer_is_shorter_than_full_for_both_gene_classes(self):
        # The short form must actually be shorter -- a copy-paste bug
        # that made them identical would defeat the short PDF's whole
        # space-budget rationale.
        for transcript_result in (_MT_ATP6_TRANSCRIPT_RESULT, _MT_TL1_TRANSCRIPT_RESULT):
            with self.subTest(gene=transcript_result.get("gene_symbol")):
                short_text = mtdna_interpretation_disclaimer_short(transcript_result)
                full_text = mtdna_interpretation_disclaimer(transcript_result)
                self.assertLess(len(short_text), len(full_text))


if __name__ == "__main__":
    unittest.main()
