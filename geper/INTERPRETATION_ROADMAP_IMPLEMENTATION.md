# GEPER Interpretation Roadmap -- Implementation & Validation Report

This document covers the seven-phase clinical interpretation upgrade
built on top of GEPER's existing biological evidence layer (DNA/RNA/
protein models, ClinVar/dbSNP/BLAST/Ensembl/gnomAD, ClinGen, UniProt/
InterPro/Pfam, AlphaFold DB). It does **not** re-document those
pre-existing integrations -- see `VALIDATION_REPORT.md`,
`CLINGEN_INTEGRATION_SUMMARY.md`, and `PHASE2_GNOMAD_SUMMARY.md` for
that prior work.

## Architecture overview

Seven engines, wired together in `pipeline/interpretation.py`'s
`InterpretationEngine.interpret()`, in this order:

1. **`pipeline/acmg_rules.py` -- `ACMGRuleEngine`** (Phase 1): evaluates
   all 28 ACMG/AMP 2015 criteria individually from existing provider
   evidence. Each criterion reports `triggered` / `not_triggered` /
   `not_evaluated`, with rationale, supporting/conflicting evidence,
   sources, and confidence. Combines into a classification via the
   standard Richards et al. point-based rules.
2. **`pipeline/interpretation_result.py` -- `InterpretationResult` /
   `build_interpretation_result()`** (Phase 2): the single canonical
   object every downstream engine and report builder consumes. Carries
   the ACMG result, deduped supporting/conflicting evidence, AI
   consensus, biological evidence, and raw per-provider evidence by
   reference (`raw_evidence`) so later engines never re-fetch or
   re-parse it.
3. **`pipeline/confidence_engine.py` -- `ConfidenceEngine`** (Phase 3):
   independent 0-100 confidence score across 7 weighted evidence
   categories (clinical, population, AI, protein, structural, sequence
   context, additional), with a conflict penalty. Never overrides ACMG.
4. **`pipeline/prioritization_engine.py` -- `PrioritizationEngine`**
   (Phase 4): independent 0-100 priority score/category, using ACMG
   classification and confidence as two of 11 weighted, direction-aware
   factors. `rank_batch()` assigns batch-relative rank once per run
   (called from `orchestrator.py` after all variants are scored).
5. **`pipeline/conflict_resolution_engine.py` --
   `ConflictResolutionEngine`** (Phase 6): detects and documents
   disagreements across Clinical/Population/AI/Protein/Structure/
   Sequence categories (plus ACMG-criterion-level caveats). Never
   changes the classification -- every resolution states so explicitly.
6. **`pipeline/explainability_engine.py` -- `ExplainabilityEngine`**
   (Phase 7): pure, read-only reorganization of everything the above
   five already produced into 14 explainability views (decision
   summary, reasoning chain, evidence contributed/not-contributed, AI
   influential/contextual, highest-weight evidence, conflicts, remaining
   uncertainties, limitations, confidence/priority rationale, full
   28-criterion evidence trace). Computes nothing new.
7. **`report/clinical_report_builder.py` -- `build_clinical_report()`**
   (Phase 5): the single function consumed by both
   `report/json_builder.py` (additive `clinical_report` JSON key) and
   `report/report_generator.py` (Markdown "Clinical Interpretation
   Report" section), so JSON and Markdown output can never disagree.

All five scoring/analysis engines (1, 3, 4, 6, 7) are wrapped in their
own `try/except` inside `interpretation.py`; a failure in any one
degrades gracefully (the corresponding `*_pending` flag stays `True`,
or the field stays `None`/empty) without affecting the others or the
pre-existing legacy `summary`/`confidence`/`significance_score`/
`supporting_evidence` fields.

## Configuration

Every weight, threshold, and penalty introduced by Phases 3, 4, and 6
lives in `config.py` as a documented, environment-overridable field:
`ConfidenceConfig`, `PrioritizationConfig`, `ConflictConfig`. No magic
numbers are embedded in the engine code itself.

## Backward compatibility

- `InterpretationEngine.interpret()`'s original four return keys
  (`summary`, `confidence`, `significance_score`, `supporting_evidence`)
  are untouched; everything from Phase 1 onward is additive under a new
  `acmg_evaluation` / `interpretation_result` key.
- `report/json_builder.py`'s `build_variant_result()` gained two new
  optional, defaulted kwargs (`interpretation_result`,
  `clinical_report`) and two new additive output keys of the same name;
  no existing key changed shape.
- `report/report_generator.py`'s original per-provider Markdown
  sections are preserved verbatim under a new "Annotation Detail (Audit
  Trail)" divider; the new Clinical Interpretation Report (16+2
  sections) is added above them, not in place of them.

## Known, honest limitations

- **Sequence-category conflicts (Phase 6):** BLAST returns a homology
  hit count with no pathogenic/benign direction, and Ensembl is not
  exposed as a separate per-variant evidence dict in this pipeline (it
  contributes internally to sequence-context retrieval only). The
  Conflict Resolution Engine reports this honestly as "Not evaluable"
  rather than fabricating a conflict signal that doesn't exist.
- **Priority factor "gene clinical relevance":** the roadmap listed this
  as a distinct prioritization factor. In this implementation it is
  folded into the "Clinical Evidence" factor (which already averages
  ClinVar classification with ClinGen's gene-disease validity rating --
  ClinGen's validity curation *is* the gene-clinical-relevance signal
  GEPER has), rather than being duplicated as a second, overlapping
  factor.
- **This sandbox cannot import `pipeline/orchestrator.py` directly**
  (it pulls in the full model registry, which requires `torch`, not
  installed here) or run the live end-to-end pipeline against a real
  VCF. Verification therefore relied on: (a) the full pytest regression
  suite (torch-dependent test files excluded, same as every prior
  phase), and (b) direct unit-level exercises of the complete
  `InterpretationEngine.interpret()` -> `build_variant_result()` ->
  `ReportGenerator.generate()` chain with realistic synthetic evidence
  covering pathogenic, benign, conflicting-evidence, and fully-empty/
  graceful-fallback scenarios. This is a real gap relative to "verify
  pipeline execution" on an actual VCF file, disclosed rather than
  glossed over.
- **Two pre-existing bugs found and fixed during this work** (both
  narrow, both disclosed at the time): `ai_context_models` never
  actually included RNA-FM/ESM2 despite its own docstring promising it
  would (fixed in Phase 5 prep); AlphaFold confidence-band matching
  compared against space-separated strings (`"very high"`) while the
  real provider returns underscored values (`"very_high"`), silently
  defaulting the two extreme bands to a mid-value in the Phase 3/4
  scoring (fixed in Phase 6 prep, before it could affect the new
  structural-conflict check).

## Regression results (final, this checkpoint)

```
251 passed, 6 skipped, 0 failed
```

Skipped/excluded tests are all `torch`-dependent (`test_evo2.py`,
`test_mmsplice_predictor.py`, `test_mmsplice_service.py`'s collection,
plus 6 skipped subtests) -- a pre-existing sandbox limitation (no GPU/
`torch` install here), unrelated to and unaffected by this work. This
count has been identical after every one of the seven phases -- no
regression was ever introduced.
