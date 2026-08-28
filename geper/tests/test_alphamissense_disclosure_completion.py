"""
Tests for the AlphaMissense clinical-use-disclosure completion.
83e2093 caveated PP3/BP4 only; a full enumeration (content-mode grep
across every render path, traced back to its actual producer function --
never trusting a field name) found four more sites where an
AlphaMissense score/class/pathogenicity call reaches a reader with no
caveat:
  - BP1 (`acmg_rules.py::_bp1_opposing_missense_evidence`)
  - the legacy pre-ACMG evidence sentence (`interpretation.py::
    InterpretationEngine.interpret`) -- THE SEVERE ONE: this sentence
    and PP3's own sentence can both land in the same variant's
    `supporting_evidence` list (same guard condition -- any variant
    where AlphaMissense returns a real hit populates both), worded
    differently enough that exact-string dedup never merges them.
  - the raw Markdown "### AlphaMissense" block (`report_generator.py::
    _render_alphamissense`)
  - the "### 7. AI Consensus" bullet (`report_generator.py::generate`)
  - the conflict-resolution direction-only mention
    (`conflict_resolution_engine.py::_ai_conflict`)

THE DANGEROUS CASE this whole card exists to catch: a report where
AlphaMissense appears in more than one place. Every test below asserts
EVERY AlphaMissense mention it finds is caveated -- never merely that
one is, or that the caveat appears "somewhere" -- because an assertion
of the weaker shape would have PASSED even before this fix, which is
the same cannot-fail defect class 83e2093 itself was found to have.
`TestDangerousCaseBothSentencesCaveated` is the concrete instance the
dispatch specifically asked for: real production code (not a hand-typed
fixture standing in for it) produces two independently-worded
AlphaMissense sentences for one variant, and both must carry the
caveat.
"""

import unittest

from pipeline.acmg_rules import ACMGRuleEngine
from pipeline.conflict_resolution_engine import ConflictResolutionEngine
from pipeline.interpretation import InterpretationEngine
from pipeline.interpretation_result import build_interpretation_result
from report.clinical_report_builder import build_clinical_report
from report.report_generator import ReportGenerator

# Both established by 83e2093's `_pp3_bp4` caveat -- every site below
# must reuse this wording (a shortened form is only acceptable where a
# site's own docstring/comment explains why the full sentence doesn't
# fit, e.g. the conflict-resolution mention, which never carries a
# score to begin with).
_CAVEAT_MARKERS = ("not clinically validated", "not approved for clinical use")


def _assert_all_caveated(test, mentions):
    """`mentions`: list of (label, text) pairs, each `text` a full
    sentence/line that mentions AlphaMissense. Fails on the FIRST
    uncaveated one and names which site produced it, so a regression at
    any one of the five sites fails specifically, not just "somewhere
    is missing a caveat"."""
    test.assertTrue(mentions, "fixture produced no AlphaMissense mentions to check -- test would be vacuous")
    for label, text in mentions:
        for marker in _CAVEAT_MARKERS:
            test.assertIn(marker, text, f"{label}: missing '{marker}'. Text was: {text!r}")


def _am(am_class, score=0.997, protein_variant="p.Trp88Cys"):
    return {
        "skipped": False,
        "found": True,
        "am_class": am_class,
        "am_pathogenicity": score,
        "protein_variant": protein_variant,
        "transcript_id": "ENST00000000000",
        "uniprot_id": "P00000",
        "genome": "hg38",
    }


class TestBP1EvidenceCaveated(unittest.TestCase):
    def test_bp1_opposing_evidence_is_caveated(self):
        text = ACMGRuleEngine._bp1_opposing_missense_evidence(_am("likely_pathogenic"), None, None)
        _assert_all_caveated(self, [("BP1 opposing evidence", text)])


class TestLegacyEvidenceSentenceCaveated(unittest.TestCase):
    """Site B -- the severe one."""

    def test_legacy_interpretation_sentence_is_caveated(self):
        engine = InterpretationEngine()
        result = engine.interpret(
            variant_dict={"chrom": "3", "pos": 10141852, "ref": "G", "alt": "T"},
            dna_models_used=[],
            clinvar_result={},
            dbsnp_result={},
            protein_result={},
            blast_result={},
            alphamissense_result=_am("likely_pathogenic"),
        )
        am_lines = [line for line in result["supporting_evidence"] if "AlphaMissense" in line]
        _assert_all_caveated(self, [("legacy interpretation.py evidence", line) for line in am_lines])


class TestRawMarkdownBlockCaveated(unittest.TestCase):
    def test_render_alphamissense_includes_caveat(self):
        lines = ReportGenerator._render_alphamissense(_am("likely_pathogenic"))
        text = "\n".join(lines)
        _assert_all_caveated(self, [("raw '### AlphaMissense' block", text)])


def _minimal_ir(**overrides):
    base = {
        "variant": {"chrom": "3", "pos": 10141852, "ref": "G", "alt": "T"},
        "gene_symbol": "VHL",
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
        "confidence_score": None,
        "confidence_label": None,
        "priority_pending": True,
        "priority_explanation": [],
        "conflict_list": [],
        "conflict_score": 0.0,
        "conflict_severity": None,
        "recommendations": [],
        "evidence_sources": [],
        "ai_model_errors": [],
    }
    base.update(overrides)
    return base


def _document(variants):
    return {
        "geper_version": "test",
        "generated_at": "2026-08-27T00:00:00+00:00",
        "input_vcf": "am_disclosure_test.vcf",
        "assembly": "GRCh38",
        "vcf_samples": ["SAMPLE01"],
        "variant_count": len(variants),
        "variants": list(variants),
    }


class TestAIConsensusBulletCaveated(unittest.TestCase):
    def test_ai_consensus_alphamissense_bullet_includes_caveat(self):
        variant_dict = {"chrom": "3", "pos": 10141852, "ref": "G", "alt": "T"}
        ir = _minimal_ir(
            ai_consensus=[
                {"source": "AlphaMissense", "prediction": "likely_pathogenic", "score": 0.997, "target": "p.Trp88Cys"}
            ]
        )
        clinical_report = build_clinical_report(ir, variant_dict)
        document = _document(
            [
                {
                    "variant": variant_dict,
                    "interpretation_result": {"gene_symbol": "VHL"},
                    "candidate_interpretation": clinical_report,
                }
            ]
        )
        md = ReportGenerator().generate(document)
        am_lines = [line for line in md.splitlines() if line.startswith("- **AlphaMissense:**")]
        self.assertTrue(am_lines, "AI Consensus section did not render an AlphaMissense bullet at all")
        _assert_all_caveated(self, [("AI Consensus bullet", line) for line in am_lines])


class TestConflictMentionCaveated(unittest.TestCase):
    def test_ai_conflict_alphamissense_mention_includes_caveat(self):
        ai_consensus = [
            {"source": "AlphaMissense", "prediction": "likely_pathogenic"},
            # Any `_ai_directions`-recognized "benign" prediction disagrees with
            # AlphaMissense's "damaging" direction above -- that mismatch is all
            # `_ai_conflict` needs to fire; the literal string doesn't need to be
            # an MMSplice-specific category name.
            {"source": "MMSplice", "prediction": "benign"},
        ]
        conflict = ConflictResolutionEngine._ai_conflict(ai_consensus, 0.0, "n/a", 0.0, "n/a")
        self.assertIsNotNone(conflict, "fixture did not actually produce an AI-consensus conflict")
        _assert_all_caveated(self, [("conflict-resolution mention", conflict.evidence_a["statement"])])


class TestFormerlyDangerousCaseNowUnifiedByConstruction(unittest.TestCase):
    """THE fixture the original dispatch specifically asked for: real
    production code (not hand-typed strings) makes a single variant's
    actual `supporting_evidence` list contain both the legacy sentence
    and the PP3 sentence for the same AlphaMissense call -- the common
    production case, not a contrived edge case, since both fire off the
    exact same `found`/`not skipped` guard on the same
    `alphamissense_result`.

    HIGH 3 / AM-07 (2026-08-28, ruled): both sites now source that
    sentence from the same shared `alphamissense_evidence_sentence`
    (pvs1/utils.py), so they are byte-identical, and the EXISTING
    exact-match `_dedupe` in `interpretation_result.py` -- unmodified,
    no new code there -- collapses them to one line on its own. Before
    this fix, this test asserted `len(am_lines) >= 2` (two differently-
    worded, individually-caveated duplicates survived dedup, which is
    what made this "dangerous": a reader could see a caveated and an
    uncaveated version side by side if the per-site caveat fix had
    lapsed on either side). That assertion would now be WRONG -- it
    would demand the regression this fix removes. Renamed and inverted:
    this now pins that construction (one shared source) is what keeps
    the duplicate from recurring, not vigilance on two copies."""

    def test_single_variant_produces_exactly_one_deduped_am_sentence(self):
        variant_dict = {"chrom": "3", "pos": 10141852, "ref": "G", "alt": "T"}
        am_result = _am("likely_pathogenic")

        legacy = InterpretationEngine().interpret(
            variant_dict=variant_dict,
            dna_models_used=[],
            clinvar_result={},
            dbsnp_result={},
            protein_result={},
            blast_result={},
            alphamissense_result=am_result,
        )

        pp3, _bp4 = ACMGRuleEngine._pp3_bp4(am_result, None)
        self.assertEqual(
            pp3.status, "triggered", "fixture must actually trigger PP3 for this to be the real dangerous case"
        )

        legacy["acmg_evaluation"] = {
            "classification": "Uncertain significance",
            "triggered_criteria": [
                {
                    "code": "PP3",
                    "strength": pp3.strength,
                    "direction": pp3.direction,
                    "rationale": pp3.rationale,
                    "supporting_evidence": pp3.supporting_evidence,
                    "evidence_sources": pp3.evidence_sources,
                }
            ],
            "not_triggered_criteria": [],
            "not_evaluated_criteria": [],
        }

        ir = build_interpretation_result(
            variant_dict=variant_dict, interpretation=legacy, alphamissense_result=am_result
        )

        am_lines = [line for line in ir.supporting_evidence if "AlphaMissense" in line]
        self.assertEqual(
            len(am_lines),
            1,
            "unification should make the legacy and PP3 AlphaMissense sentences byte-identical, so "
            f"the existing exact-match dedup collapses them to exactly one line: {am_lines!r}",
        )
        _assert_all_caveated(self, [(f"supporting_evidence[{i}]", line) for i, line in enumerate(am_lines)])


if __name__ == "__main__":
    unittest.main()
