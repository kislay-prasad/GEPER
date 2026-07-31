"""
Tests for the ACMG/AMP BP1, BP3, BP6, and BP7 rules
(`pipeline/acmg_rules.py::ACMGRuleEngine._bp1` / `._bp3` / `._bp6` /
`._bp7`).

Ground truth, not synthetic guesses, for each rule's core positive
example:

  - BP1 (missense in a gene where LOF is the established disease
    mechanism): BRCA1 p.Ser1613Gly (NM_007294.4:c.4837A>G, genomic
    17:43071077 T>C on the minus strand), real ClinVar VCV000041827,
    "Benign", reviewed by expert panel. Residue 1613 confirmed against
    BRCA1's real canonical UniProt sequence (P38398: residue 1613 =
    Ser) and, as of this module's rewrite, also confirmed directly
    against the real BRCA1 transcript CDS in
    `tests/fixtures/pvs1_transcripts.json` via
    `coding_consequence_detail` (codon 1613, S->G) -- the same
    transcript-CDS-frame call `pipeline/acmg_rules.py::
    ACMGRuleEngine._protein_effect_flags` now uses in production,
    replacing this module's old `protein_result=missense_protein_result(...)`
    mocking of `pipeline/protein_translator.py`'s frame-unaware local
    translation window. Paired with BRCA1's real ClinGen
    dosage-sensitivity curation (haploinsufficiency score 3,
    "Sufficient evidence for dosage pathogenicity" -- verbatim from
    `tests/fixtures/clingen_dosage_sensitivity.tsv`, the same live
    ClinGen download `test_pvs1.py`'s `_CLINGEN_DOSAGE` table uses).
    A second real example, CFTR p.Gly551Asp (c.1652G>A, genomic
    7:117587806 G>A, VCV000007120, "Pathogenic", practice guideline --
    CFTR's most clinically famous gating mutation), exercises the
    LOF_ESTABLISHED_RECESSIVE (dosage score 30) branch, likewise
    confirmed against the real CFTR fixture CDS (codon 551, G->D).
    Using a real *pathogenic* missense here is deliberate, not an
    error: BP1 is evaluated as one independent piece of supporting
    evidence, exactly as real curation does -- a gene's LOF mechanism
    does not stop being established just because one particular
    missense variant in it turns out pathogenic through other
    evidence; that is precisely why BP1 is only ever "supporting"
    strength.

  - BP3 (in-frame indel in a UniProt-annotated repeat region): reuses
    the exact same real data pair `test_pm4_ps4.py`'s
    `TestPM4RepeatRegionCaveat` already established and documented --
    TP53 c.792_794del (p.Leu265del), real ClinVar "Likely pathogenic"
    in-frame deletion, paired with Titin/Q8WZ42's real UniProt "PEVK 1"
    repeat feature (`tests/fixtures/pm4_uniprot_ttn_repeat_features.json`)
    remapped onto TP53's own codon range for an isolated caveat test.
    See that file's docstring for why this pairing (not a single
    cross-referenced real record) is the honest choice: TTN's
    clinically-used transcripts number residues differently from the
    canonical UniProt isoform, so no confident real TTN ClinVar+UniProt
    coordinate match was made. Both halves of the pair are independently
    real; only the pairing is constructed.

  - BP6 (ClinVar reports Benign/Likely benign): three real, live-fetched
    ClinVar records in `tests/fixtures/bp6_clinvar_records.json` --
    BRCA1 p.Ser1613Gly (Benign, reviewed by expert panel), BRCA1
    p.Glu23fs/c.68_69del (Pathogenic, reviewed by expert panel -- the
    real BRCA1 founder frameshift, "185delAG"), and BRCA1 c.5467+200G>A
    (Likely benign, but only "no assertion criteria provided" review
    status -- tests that a weakly-reviewed source doesn't count as
    "reputable" even under BP6's own wording).

  - BP7 (synonymous, no splice impact predicted): grounded by a real
    synonymous TP53 ClinVar-associated substitution, c.525C>T
    (p.Arg175=, genomic 17:7675087 G>A) -- the same real record
    `tests/test_ps1_pm5.py::test_ps1_requires_a_missense_query_not_synonymous`
    already validates via `coding_consequence_detail` against
    `tests/fixtures/ps1_pm5_transcript.json`'s real TP53 transcript
    CDS. BP7 now takes `variant_dict` + `transcript_result` (the same
    transcript-CDS-frame inputs PS1/PM5/PVS1 already use) rather than
    the old `protein_result=synonymous_protein_result()` mock of
    `pipeline/protein_translator.py`'s frame-unaware local window --
    see this module's BP1 section and
    `pipeline/acmg_rules.py::ACMGRuleEngine._protein_effect_flags`'s
    docstring for why that window was replaced (it produced false
    "synonymous" calls for real missense variants, e.g. PRNP
    p.Pro102Leu). The "not synonymous" negative case reuses TP53's own
    real p.Arg248Trp missense (c.742C>T, genomic 17:7674221 G>A,
    ClinVar Pathogenic, expert panel -- the exact variant that exposed
    this bug). The MMSplice/SpliceFormer/SpliceBERT result dicts below
    mirror each plugin's own real, documented output shape (MMSplice:
    `interpretation_category`; SpliceFormer/SpliceBERT: `classification`
    in the shared no_significant_effect/moderate_effect/large_effect
    buckets, per `pipeline/models/spliceformer_plugin.py`/
    `splicebert_plugin.py`'s own `_infer_impl`) rather than running the
    actual models, which this session's scoped-tests-only, no-heavy-
    model-loading requirement rules out.
"""

import json
import os
import unittest

from pipeline.acmg_rules import ACMGRuleEngine

_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")

with open(os.path.join(_FIXTURE_DIR, "bp6_clinvar_records.json"), "r", encoding="utf-8") as _fh:
    _BP6_RECORDS = json.load(_fh)

with open(os.path.join(_FIXTURE_DIR, "pm4_uniprot_ttn_repeat_features.json"), "r", encoding="utf-8") as _fh:
    _TTN_UNIPROT = json.load(_fh)

with open(os.path.join(_FIXTURE_DIR, "ps1_pm5_transcript.json"), "r", encoding="utf-8") as _fh:
    _TP53_RECORD = json.load(_fh)["transcript"]

with open(os.path.join(_FIXTURE_DIR, "pm4_hba2_transcript.json"), "r", encoding="utf-8") as _fh:
    _HBA2_RECORD = json.load(_fh)["transcript"]

with open(os.path.join(_FIXTURE_DIR, "pvs1_transcripts.json"), "r", encoding="utf-8") as _fh:
    _PVS1_TRANSCRIPTS = json.load(_fh)["transcripts"]


def tp53_transcript_result():
    return {"skipped": False, "found": True, "transcript": _TP53_RECORD}


def hba2_transcript_result():
    return {"skipped": False, "found": True, "transcript": _HBA2_RECORD}


def gene_transcript_result(gene):
    """Real transcript structure (BRCA1/BRCA2/MYH7/CFTR), from the same
    live-fetched fixture `test_pvs1.py` uses -- these carry `cds_sequence`,
    so `coding_consequence_detail`/`protein_effect_flags` can call a real
    substitution's consequence in this transcript's actual reading frame."""
    return {"skipped": False, "found": True, "transcript": _PVS1_TRANSCRIPTS[gene]}


def uniprot_result(features=None, found=True):
    return {"skipped": False, "found": found, "gene_symbol": "TEST", "features": features or []}


# Real ClinGen dosage-sensitivity curations -- identical table to
# `test_pvs1.py`'s `_CLINGEN_DOSAGE`, from the same live
# ClinGen_gene_curation_list_GRCh38.tsv download.
_CLINGEN_DOSAGE = {
    "BRCA1": (3, "Sufficient evidence for dosage pathogenicity"),
    "CFTR": (30, "Gene associated with autosomal recessive phenotype"),
    "MYH7": (0, "No evidence available"),
}


def clingen_result(gene, validity="Definitive"):
    score, label = _CLINGEN_DOSAGE[gene]
    return {
        "skipped": False, "found": True, "source": "fixture", "gene_symbol": gene,
        "clinical_validity_summary": validity,
        "dosage_sensitivity": {"gene_symbol": gene, "haploinsufficiency_score": score, "haploinsufficiency_label": label},
    }


# Real variant_dict/transcript_result pairs, replacing the old
# `protein_result=missense_protein_result(...)` mocks of
# `pipeline/protein_translator.py`'s frame-unaware local translation
# window (see module docstring -- that window is no longer what BP1/
# BP7 read from; each of these was independently confirmed via
# `coding_consequence_detail` against the cited real transcript CDS
# fixture before being hardcoded here).

# BRCA1 p.Ser1613Gly = c.4837A>G, genomic 17:43071077 T>C (minus
# strand) -- VCV000041827, Benign, expert panel. Confirmed: codon
# 1613, S->G.
_BRCA1_S1613G = {"chrom": "17", "pos": 43071077, "ref": "T", "alt": "C"}

# CFTR p.Gly551Asp = c.1652G>A, genomic 7:117587806 G>A -- VCV000007120,
# Pathogenic, practice guideline. Confirmed: codon 551, G->D.
_CFTR_G551D = {"chrom": "7", "pos": 117587806, "ref": "G", "alt": "A"}

# MYH7 codon 50, K->Q -- not tied to a specific ClinVar record (this
# test only exercises the "dosage not established" negative branch,
# same as the original synthetic "MAAAQ"->"MAAAH" it replaces); still a
# real substitution against the real MYH7 transcript CDS, confirmed:
# codon 50, K->Q.
_MYH7_MISSENSE = {"chrom": "14", "pos": 23433585, "ref": "T", "alt": "G"}

# BRCA1 codon 50, K->* (nonsense) -- not tied to a specific ClinVar
# record (only used for the "nonsense doesn't trigger BP1" negative
# check); confirmed against the real BRCA1 transcript CDS.
_BRCA1_NONSENSE = {"chrom": "17", "pos": 43106520, "ref": "T", "alt": "A"}

# TP53 p.Arg248Trp = c.742C>T, genomic 17:7674221 G>A -- ClinVar
# Pathogenic, expert panel. Confirmed against
# `tests/fixtures/ps1_pm5_transcript.json`: codon 248, R->W. The exact
# variant that exposed the BP7 bug this rewrite fixes.
_TP53_R248W = {"chrom": "17", "pos": 7674221, "ref": "G", "alt": "A"}

# TP53 p.Arg175= (synonymous) = c.525C>T, genomic 17:7675087 G>A --
# same real record `test_ps1_pm5.py` already validates. Confirmed:
# codon 175, R->R.
_TP53_SYNONYMOUS = {"chrom": "17", "pos": 7675087, "ref": "G", "alt": "A"}


# ---------------------------------------------------------------------------
# BP1 -- real BRCA1/CFTR/MYH7 ClinGen dosage ground truth
# ---------------------------------------------------------------------------

class TestBP1(unittest.TestCase):
    def test_brca1_real_missense_triggers_bp1(self):
        """Real: BRCA1 p.Ser1613Gly (VCV000041827, Benign), dosage score 3 (LOF_ESTABLISHED)."""
        result = ACMGRuleEngine().evaluate(
            variant_dict=_BRCA1_S1613G,
            transcript_result=gene_transcript_result("BRCA1"),
            clingen_result=clingen_result("BRCA1"),
        )
        bp1 = result["all_criteria"]["BP1"]
        self.assertEqual(bp1["status"], "triggered")
        self.assertEqual(bp1["strength"], "supporting")
        self.assertEqual(bp1["details"]["lof_mechanism"], "established")

    def test_cftr_real_pathogenic_missense_in_recessive_lof_gene_triggers_bp1(self):
        """Real: CFTR p.Gly551Asp (VCV000007120, Pathogenic), dosage score 30 (LOF_ESTABLISHED_RECESSIVE)."""
        result = ACMGRuleEngine().evaluate(
            variant_dict=_CFTR_G551D,
            transcript_result=gene_transcript_result("CFTR"),
            clingen_result=clingen_result("CFTR"),
        )
        bp1 = result["all_criteria"]["BP1"]
        self.assertEqual(bp1["status"], "triggered")
        self.assertEqual(bp1["details"]["lof_mechanism"], "established_autosomal_recessive")

    def test_myh7_missense_does_not_trigger_bp1_dosage_not_established(self):
        """MYH7 dosage score 0 -- real biology: MYH7 cardiomyopathy is a missense/dominant-negative mechanism gene, not a truncating one, so BP1 correctly does not apply."""
        result = ACMGRuleEngine().evaluate(
            variant_dict=_MYH7_MISSENSE,
            transcript_result=gene_transcript_result("MYH7"),
            clingen_result=clingen_result("MYH7"),
        )
        bp1 = result["all_criteria"]["BP1"]
        self.assertEqual(bp1["status"], "not_triggered")
        self.assertEqual(bp1["details"]["lof_mechanism"], "not_established")

    def test_nonsense_variant_does_not_trigger_bp1(self):
        result = ACMGRuleEngine().evaluate(
            variant_dict=_BRCA1_NONSENSE,
            transcript_result=gene_transcript_result("BRCA1"),
            clingen_result=clingen_result("BRCA1"),
        )
        self.assertEqual(result["all_criteria"]["BP1"]["status"], "not_triggered")

    def test_no_clingen_data_is_not_evaluated(self):
        result = ACMGRuleEngine().evaluate(
            variant_dict=_BRCA1_S1613G,
            transcript_result=gene_transcript_result("BRCA1"),
        )
        self.assertEqual(result["all_criteria"]["BP1"]["status"], "not_evaluated")

    def test_undetermined_consequence_is_not_evaluated(self):
        """No transcript structure at all -- BP1 must report a gap (not_evaluated), never guess missense/not-missense."""
        result = ACMGRuleEngine().evaluate(
            variant_dict=_BRCA1_S1613G,
            clingen_result=clingen_result("BRCA1"),
        )
        self.assertEqual(result["all_criteria"]["BP1"]["status"], "not_evaluated")

    def test_bp1_contributes_supporting_point_to_combining_rules(self):
        result = ACMGRuleEngine().evaluate(
            variant_dict=_BRCA1_S1613G,
            transcript_result=gene_transcript_result("BRCA1"),
            clingen_result=clingen_result("BRCA1"),
        )
        self.assertTrue(any("BP1 triggered (supporting, benign) contributes 1 point" in t for t in result["combining_rule_trace"]))


# ---------------------------------------------------------------------------
# BP3 -- reuses PM4's real TP53/TTN ground truth pairing
# ---------------------------------------------------------------------------

class TestBP3(unittest.TestCase):
    def test_in_frame_deletion_inside_real_uniprot_repeat_triggers_bp3(self):
        pevk_1 = _TTN_UNIPROT["features"][2]  # "PEVK 1", residues 10216-10242 (real UniProt annotation)
        self.assertEqual(pevk_1["description"], "PEVK 1")
        features = [dict(pevk_1, begin=260, end=270)]  # remapped onto TP53's codon range, see module docstring
        result = ACMGRuleEngine().evaluate(
            variant_dict={"chrom": "17", "pos": 7673826, "ref": "AGTAG", "alt": "AG"},  # real: TP53 c.792_794del, codon 265
            transcript_result=tp53_transcript_result(),
            uniprot_result=uniprot_result(features),
        )
        bp3 = result["all_criteria"]["BP3"]
        self.assertEqual(bp3["status"], "triggered")
        self.assertEqual(bp3["strength"], "supporting")
        self.assertIn("PEVK 1", bp3["rationale"])
        self.assertEqual(bp3["details"]["variant_detail"]["codon_number"], 265)

    def test_repeat_elsewhere_in_gene_does_not_trigger_bp3(self):
        features = [{"feature_type": "Repeat", "description": "unrelated repeat", "begin": 1, "end": 50}]
        result = ACMGRuleEngine().evaluate(
            variant_dict={"chrom": "17", "pos": 7673826, "ref": "AGTAG", "alt": "AG"},
            transcript_result=tp53_transcript_result(),
            uniprot_result=uniprot_result(features),
        )
        self.assertEqual(result["all_criteria"]["BP3"]["status"], "not_triggered")

    def test_stop_loss_variant_does_not_trigger_bp3(self):
        """Real: HBA2 c.427T>C (Hemoglobin Constant Spring) is PM4's stop-loss example -- BP3 is in-frame-indel-only and must not apply to a substitution."""
        pevk_1 = _TTN_UNIPROT["features"][2]
        features = [dict(pevk_1, begin=140, end=150)]
        result = ACMGRuleEngine().evaluate(
            variant_dict={"chrom": "16", "pos": 173598, "ref": "T", "alt": "C"},
            transcript_result=hba2_transcript_result(),
            uniprot_result=uniprot_result(features),
        )
        self.assertEqual(result["all_criteria"]["BP3"]["status"], "not_triggered")

    def test_frameshift_does_not_trigger_bp3(self):
        result = ACMGRuleEngine().evaluate(
            variant_dict={"chrom": "17", "pos": 7673826, "ref": "AG", "alt": "A"},
            transcript_result=tp53_transcript_result(),
        )
        self.assertEqual(result["all_criteria"]["BP3"]["status"], "not_triggered")

    def test_in_frame_indel_without_uniprot_data_is_not_evaluated(self):
        result = ACMGRuleEngine().evaluate(
            variant_dict={"chrom": "17", "pos": 7673826, "ref": "AGTAG", "alt": "AG"},
            transcript_result=tp53_transcript_result(),
        )
        self.assertEqual(result["all_criteria"]["BP3"]["status"], "not_evaluated")

    def test_missing_transcript_is_not_evaluated(self):
        result = ACMGRuleEngine().evaluate(
            variant_dict={"chrom": "17", "pos": 7673826, "ref": "AGTAG", "alt": "AG"},
        )
        self.assertEqual(result["all_criteria"]["BP3"]["status"], "not_evaluated")


# ---------------------------------------------------------------------------
# BP6 -- three real, live-fetched ClinVar records
# ---------------------------------------------------------------------------

class TestBP6(unittest.TestCase):
    def test_real_benign_expert_panel_record_triggers_bp6(self):
        """Real: BRCA1 p.Ser1613Gly, VCV000041827, Benign, reviewed by expert panel."""
        result = ACMGRuleEngine().evaluate(clinvar_result=_BP6_RECORDS["brca1_s1613g_benign_expert_panel"])
        bp6 = result["all_criteria"]["BP6"]
        self.assertEqual(bp6["status"], "triggered")
        self.assertEqual(bp6["strength"], "supporting")
        self.assertEqual(bp6["confidence"], "Low")
        self.assertIn("VCV000041827", bp6["rationale"])

    def test_real_pathogenic_expert_panel_record_does_not_trigger_bp6(self):
        """Real: BRCA1 185delAG founder frameshift, VCV000017662, Pathogenic, reviewed by expert panel."""
        result = ACMGRuleEngine().evaluate(clinvar_result=_BP6_RECORDS["brca1_glu23fs_pathogenic_expert_panel"])
        self.assertEqual(result["all_criteria"]["BP6"]["status"], "not_triggered")

    def test_real_weakly_reviewed_benign_record_does_not_trigger_bp6(self):
        """Real: BRCA1 c.5467+200G>A, VCV004856951, Likely benign but only 'no assertion criteria provided' -- too weak a source to be 'reputable'."""
        result = ACMGRuleEngine().evaluate(clinvar_result=_BP6_RECORDS["brca1_intronic_likely_benign_no_assertion_criteria"])
        bp6 = result["all_criteria"]["BP6"]
        self.assertEqual(bp6["status"], "not_triggered")
        self.assertIn("too weak", bp6["rationale"])

    def test_no_clinvar_record_is_not_evaluated(self):
        result = ACMGRuleEngine().evaluate(clinvar_result=None)
        self.assertEqual(result["all_criteria"]["BP6"]["status"], "not_evaluated")

    def test_deprecation_caveat_present_regardless_of_outcome(self):
        """Every BP6 result, triggered or not, must carry the ClinGen SVI 2018 deprecation caveat -- never silently implied away."""
        for key in _BP6_RECORDS:
            if key == "_provenance":
                continue
            with self.subTest(record=key):
                result = ACMGRuleEngine().evaluate(clinvar_result=_BP6_RECORDS[key])
                self.assertIn("SVI", result["all_criteria"]["BP6"]["rationale"])
                self.assertIn("circular", result["all_criteria"]["BP6"]["rationale"])

    def test_bp6_excluded_from_combining_rule_point_totals(self):
        """BP6 is reported as triggered but must not silently move the final classification -- same circularity rationale that already excludes the plain ClinVar cross-reference from these combining rules."""
        result = ACMGRuleEngine().evaluate(clinvar_result=_BP6_RECORDS["brca1_s1613g_benign_expert_panel"])
        self.assertEqual(result["all_criteria"]["BP6"]["status"], "triggered")
        self.assertTrue(any("BP6 triggered, but excluded from point totals" in t for t in result["combining_rule_trace"]))
        self.assertFalse(any(t.startswith("BP6 triggered (") and "contributes" in t for t in result["combining_rule_trace"]))


# ---------------------------------------------------------------------------
# BP7 -- MMSplice + SpliceFormer + SpliceBERT, real output schema
# ---------------------------------------------------------------------------

def no_effect_mmsplice():
    return {"predicted": True, "interpretation_category": "no_significant_effect", "interpretation": "no significant splice effect"}


def damaging_mmsplice():
    return {"predicted": True, "interpretation_category": "exon_skipping", "interpretation": "predicted exon skipping"}


def no_effect_plugin_result(score=0.02):
    return {"score": score, "classification": "no_significant_effect", "confidence": 0.02}


def damaging_plugin_result(classification="large_effect", score=0.8):
    return {"score": score, "classification": classification, "confidence": score}


def _synonymous_kwargs():
    return {"variant_dict": _TP53_SYNONYMOUS, "transcript_result": tp53_transcript_result()}


def _missense_kwargs():
    return {"variant_dict": _TP53_R248W, "transcript_result": tp53_transcript_result()}


class TestBP7(unittest.TestCase):
    def test_non_synonymous_variant_does_not_trigger_bp7(self):
        """Real: TP53 p.Arg248Trp (c.742C>T) -- the exact variant that exposed this bug (was misread as 'synonymous' by the old frame-unaware translation window)."""
        result = ACMGRuleEngine().evaluate(**_missense_kwargs())
        self.assertEqual(result["all_criteria"]["BP7"]["status"], "not_triggered")

    def test_undetermined_consequence_is_not_evaluated(self):
        """No transcript structure at all -- BP7 must report a gap (not_evaluated), never guess synonymous/not-synonymous."""
        result = ACMGRuleEngine().evaluate(variant_dict=_TP53_SYNONYMOUS)
        self.assertEqual(result["all_criteria"]["BP7"]["status"], "not_evaluated")

    def test_synonymous_with_no_splice_evidence_at_all_triggers_low_confidence(self):
        """Real: TP53 c.525C>T (p.Arg175=) -- the same real record `test_ps1_pm5.py` validates; BP7 itself only consumes is_synonymous + each predictor's own result dict."""
        result = ACMGRuleEngine().evaluate(**_synonymous_kwargs())
        bp7 = result["all_criteria"]["BP7"]
        self.assertEqual(bp7["status"], "triggered")
        self.assertEqual(bp7["confidence"], "Low")

    def test_synonymous_with_mmsplice_no_effect_triggers(self):
        result = ACMGRuleEngine().evaluate(mmsplice_result=no_effect_mmsplice(), **_synonymous_kwargs())
        bp7 = result["all_criteria"]["BP7"]
        self.assertEqual(bp7["status"], "triggered")
        self.assertIn("MMSplice", bp7["evidence_sources"])

    def test_synonymous_with_mmsplice_damaging_blocks_bp7(self):
        result = ACMGRuleEngine().evaluate(mmsplice_result=damaging_mmsplice(), **_synonymous_kwargs())
        self.assertEqual(result["all_criteria"]["BP7"]["status"], "not_triggered")

    def test_synonymous_with_spliceformer_no_effect_triggers(self):
        result = ACMGRuleEngine().evaluate(
            spliceformer_result=no_effect_plugin_result(),
            **_synonymous_kwargs(),
        )
        bp7 = result["all_criteria"]["BP7"]
        self.assertEqual(bp7["status"], "triggered")
        self.assertIn("SpliceFormer", bp7["evidence_sources"])

    def test_synonymous_with_spliceformer_damaging_blocks_even_when_mmsplice_is_silent(self):
        result = ACMGRuleEngine().evaluate(
            mmsplice_result=no_effect_mmsplice(),
            spliceformer_result=damaging_plugin_result("large_effect"),
            **_synonymous_kwargs(),
        )
        bp7 = result["all_criteria"]["BP7"]
        self.assertEqual(bp7["status"], "not_triggered")
        self.assertTrue(any("SpliceFormer" in c for c in bp7["conflicting_evidence"]))

    def test_synonymous_with_splicebert_damaging_blocks(self):
        result = ACMGRuleEngine().evaluate(
            splicebert_result=damaging_plugin_result("moderate_effect"),
            **_synonymous_kwargs(),
        )
        self.assertEqual(result["all_criteria"]["BP7"]["status"], "not_triggered")

    def test_all_three_predictors_agreeing_no_effect_triggers_with_all_sources_listed(self):
        result = ACMGRuleEngine().evaluate(
            mmsplice_result=no_effect_mmsplice(),
            spliceformer_result=no_effect_plugin_result(),
            splicebert_result=no_effect_plugin_result(),
            **_synonymous_kwargs(),
        )
        bp7 = result["all_criteria"]["BP7"]
        self.assertEqual(bp7["status"], "triggered")
        for src in ("MMSplice", "SpliceFormer", "SpliceBERT"):
            self.assertIn(src, bp7["evidence_sources"])

    def test_ensemble_result_alone_is_not_read_by_bp7(self):
        """ensemble_result is Enformer/Borzoi (PP3/BP4's field) -- BP7 must not treat it as a splice-predictor source."""
        result = ACMGRuleEngine().evaluate(
            ensemble_result={"models_used": ["enformer", "borzoi"], "classification": "large_effect", "consensus_score": 0.9, "basis": "two_model_consensus", "agreement_percentage": 90.0},
            **_synonymous_kwargs(),
        )
        bp7 = result["all_criteria"]["BP7"]
        self.assertEqual(bp7["status"], "triggered")  # no MMSplice/SpliceFormer/SpliceBERT evidence at all -> low-confidence trigger, unaffected by ensemble_result
        self.assertNotIn("ensemble", " ".join(bp7["evidence_sources"]).lower())


if __name__ == "__main__":
    unittest.main()
