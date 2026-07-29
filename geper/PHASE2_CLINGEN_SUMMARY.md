# Phase 2 — ClinGen Validation & Defect Fix Summary

This is a follow-up to `CLINGEN_INTEGRATION_SUMMARY.md`. That document
covers the original ClinGen integration (provider/lookup/cache/ACMG
wiring, all "already complete" per that summary). This document covers
a **validation pass** run against the completed integration, the one
genuine defect it found, the fix, and the regression tests added to
catch it going forward. No ClinGen module was redesigned or rewritten;
only the two lines described below were changed to close a data-mapping
gap.

## 1. Symptom reported

Running the pipeline against a 3-variant VCF, every variant's `clingen`
result showed:

```json
"clingen": { "clingen_gene_id": null, ... }
```

and the Markdown report showed:

```
### ClinGen (Clinical Evidence)
- **Status:** No ClinGen curation found for this gene.
```

...even though `clingen calls=3` in the stage timing confirmed the
ClinGen stage was executing on every variant, not being skipped.

## 2. Root-cause analysis

Two independent things were checked, since the symptom is consistent
with either:

### 2a. Is `clingen_gene_id` itself a bug? — **Yes, confirmed.**

`LocalDatasetClinGenProvider` (`pipeline/clingen/provider.py`) is the
primary, "download once, query locally" data source (see that module's
docstring). ClinGen's own Gene-Disease Validity flat-file download
includes a `GENE ID (HGNC)` column. That column *was* being read by
`csv.DictReader` in `parse_gene_validity_row()`
(`pipeline/clingen/utils.py`) — but the parsed value was discarded: it
was never stored on the `GeneDiseaseValidity` dataclass, and
`LocalDatasetClinGenProvider.query()` never set
`ClinGenGeneEvidence.clingen_gene_id` at all on the local-dataset path
(it was only ever populated on the *live API* path, in
`_evidence_from_api_payload()`).

**Proof:** querying the bundled fixture
(`testdata/clingen_fixture/gene_validity_test.tsv`, which contains
BRCA1, TTN, SCN5A, GJB2, MTHFR — each with full curation data) returned
`found: true` with a fully populated `gene_disease_validity` and
`dosage_sensitivity`, but `clingen_gene_id: null` for every single one
of them, including BRCA1. This is a genuine gap: the identifier was
sitting unused in an already-loaded row. The existing test suite did
not catch this because `test_clingen_provider.py`'s only
`clingen_gene_id` assertion exercised the `LiveAPIClinGenProvider` path
(a mocked JSON payload with `"clingenGeneId"`), never the local-dataset
path.

This is the bug described in the task's "HGNC identifier mapping" /
"local dataset lookup" candidate causes.

### 2b. Is the "no curation found" result for the *specific 3 VCF
variants* also a bug?

Using positive-control genes confirmed the local-dataset lookup
mechanism itself (symbol normalization, file parsing, gene→evidence
lookup, caching, and the composite provider's local-first/API-fallback
routing) all work correctly end-to-end once a local dataset file is
configured and the gene is present in it — see Section 4.

Whether the *original* 3-variant VCF's specific genes (later confirmed
by position to correspond to F5/Factor V Leiden, HBB/sickle-cell, and
ALDH2/rs671) show `found: false` because (a) no local dataset file was
configured for that run (`GEPER_CLINGEN_GENE_VALIDITY_FILE` /
`GEPER_CLINGEN_DOSAGE_FILE` default to empty strings — a deployer must
provision them, by design, per `ClinGenConfig`'s docstring) and the code
fell through to the live API fallback, or (b) those specific genes are
not present in whatever file *was* configured, could not be
distinguished further from inside this environment. The live API path
is explicitly flagged in the code's own docstring as unverified against
a real response (`pipeline/clingen/provider.py`'s module docstring,
`LiveAPIClinGenProvider`'s docstring): *"this environment has no
network route to clinicalgenome.org ... should be re-verified against
a live call before this fallback is relied on in production."* That
caveat was already known and intentionally scoped out of this
integration; re-verifying or replacing the live endpoint is out of
scope for this validation pass (see Section 6, remaining limitations),
and was not touched.

**Conclusion:** the `clingen_gene_id: null` symptom was a genuine
implementation defect (Section 2a) and has been fixed. The "no
curation found" outcome for the original VCF's 3 specific genes is
most consistent with a configuration/data-availability gap (no local
file configured, or those particular genes absent from it) compounded
by the known, previously-documented live-API-fallback limitation —
not a new defect in the lookup/routing/cache mechanism itself, which
Section 4 verifies works correctly against genuinely ClinGen-curated
genes.

## 3. Files modified

```
pipeline/clingen/models.py     GeneDiseaseValidity: added `gene_id` field + to_dict() key
pipeline/clingen/utils.py      _GENE_VALIDITY_COLUMNS: added "gene_id" -> "GENE ID (HGNC)" mapping;
                                parse_gene_validity_row(): passes gene_id through
pipeline/clingen/provider.py   LocalDatasetClinGenProvider.query(): now sets
                                ClinGenGeneEvidence.clingen_gene_id from the first available
                                GeneDiseaseValidity.gene_id among that gene's validity rows
tests/test_clingen_provider.py       + test_clingen_gene_id_populated_from_hgnc_column
                                      + test_unknown_gene_clingen_gene_id_is_none
tests/test_clingen_models_utils.py   + test_parses_hgnc_gene_id_column
                                      + test_missing_gene_id_column_defaults_to_none
PHASE2_CLINGEN_SUMMARY.md      (this file)
```

No other files were touched. `pipeline/clingen/cache.py`,
`pipeline/clingen/lookup.py`, the ACMG wiring
(`pipeline/interpretation.py`), the orchestrator's `_run_clingen_stage`,
and both report builders were left exactly as they were — the defect
was entirely contained to the local-dataset parse/query path, and the
fix is additive (a new optional field with a `None` default; every
existing caller and every existing test kept working unmodified).

## 4. Positive-control validation

Ran the fixed `LocalDatasetClinGenProvider` against a fixture built to
mirror ClinGen's real Gene-Disease Validity + Dosage Sensitivity
download shape (same columns as the bundled
`testdata/clingen_fixture/*.tsv`) for all six genes named in the
validation task, each with a real, documented ClinGen classification:

| Gene  | found | clingen_gene_id | expert_panel                        | classification | haploinsufficiency score |
|-------|-------|------------------|--------------------------------------|-----------------|--------------------------|
| BRCA1 | true  | HGNC:1100        | Hereditary Cancer GCEP               | Definitive      | 3                        |
| BRCA2 | true  | HGNC:1101        | Hereditary Cancer GCEP               | Definitive      | 3                        |
| TP53  | true  | HGNC:11998       | Hereditary Cancer GCEP               | Definitive      | 3                        |
| CFTR  | true  | HGNC:1884        | Cystic Fibrosis GCEP                 | Definitive      | 3                        |
| LDLR  | true  | HGNC:6547        | Familial Hypercholesterolemia GCEP   | Definitive      | 3                        |
| MYH7  | true  | HGNC:7577        | Cardiomyopathy GCEP                  | Definitive      | 1                        |

Every gene populated `clingen_gene_id`, `gene_disease_validity`
(disease label, classification, MOI, GCEP, SOP version, classification
date, online report URL), `expert_panel`, `dosage_sensitivity`
(`haploinsufficiency`/`triplosensitivity` scores, labels, and
descriptions), and `clinical_validity_summary`. `actionability` stayed
an empty list, matching the input fixture (no actionability rows were
provided for these synthetic files; the field-mapping code path for it
in `_evidence_from_api_payload`/`ClinGenGeneEvidence.to_dict()` was not
touched and is unaffected by this fix).

Also re-ran the bundled 5-gene fixture (BRCA1, TTN, SCN5A, GJB2,
MTHFR) — same result: all five now return their real HGNC id instead
of `null`.

### End-to-end report validation

Fed real `ClinGenLookup` output for BRCA1/TP53/MYH7 through the actual
`report.json_builder.build_variant_result` and
`report.report_generator.ReportGenerator.generate` (the same functions
the orchestrator calls) — confirmed:
- **JSON report:** `clingen_gene_id`, full `gene_disease_validity`
  list, `dosage_sensitivity` block, `clinical_validity_summary`, and
  `expert_panel` all present and correctly populated.
- **Markdown report:** `### ClinGen (Clinical Evidence)` panel renders
  the gene, source, disease/classification/MOI/GCEP table, and
  haploinsufficiency/triplosensitivity labels correctly for all three
  genes.

### Backward-compatibility / graceful-degradation checks

Confirmed unaffected by the fix:
- A gene absent from the local file *and* unreachable via the live API
  fallback still returns a clean `found: false` with `error` set (via
  `ClinGenGeneEvidence.from_error`) — no exception raised, no change in
  shape.
- An empty/missing gene symbol still returns
  `{"skipped": False, "found": False, "error": "no gene symbol provided"}`
  exactly as before.
- `LiveAPIClinGenProvider`'s own `clingen_gene_id` mapping (from a
  live/mocked API JSON payload) is untouched and still covered by its
  existing test (`test_successful_response_is_parsed`).

## 5. Regression test results

```
tests/test_clingen_provider.py
tests/test_clingen_cache.py
tests/test_clingen_lookup.py
tests/test_clingen_acmg.py
tests/test_clingen_integration.py
tests/test_clingen_models_utils.py
  -> 81 passed, 5 subtests passed (was 77 passed before this fix's new tests)
```

Full test suite (excluding `test_evo2.py`, `test_mmsplice_predictor.py`,
`test_mmsplice_service.py` — these fail to *collect* in this sandbox
purely because `torch` is not installed here; unrelated to ClinGen and
unrelated to this fix):

```
190 passed, 6 skipped, 5 subtests passed
```

(186 passed before this change; the delta is exactly the 4 new
regression tests added. The 6 skips are pre-existing and unrelated to
ClinGen. Zero failures, zero new errors.)

Confirmed via direct inspection that none of the three excluded
torch-dependent files reference ClinGen at all — they exercise the
Evo2 and MMSplice model-loading paths only.

## 6. Remaining limitations (unchanged from Phase 1, not addressed here — out of scope)

- **`LiveAPIClinGenProvider`'s endpoint/response-schema assumptions are
  still unverified against a real ClinGen API response.** This was a
  known limitation before this validation pass (see that module's
  docstring) and remains one: this sandbox has no network route to
  `clinicalgenome.org`, so the `/gene/{symbol}` endpoint shape and the
  `geneValidityCurations` / `dosageSensitivity` / `clingenGeneId`
  field names could not be exercised against a live call here either.
  Public documentation for ClinGen's actual programmatic access
  describes a Linked Data Hub (LDH) API (`ldh.clinicalgenome.org`)
  rather than a `search.clinicalgenome.org/api` REST convention, which
  suggests the current live-API fallback's endpoint/shape should be
  re-verified — and very possibly updated — against ClinGen's current
  LDH API documentation before it is relied on in production. Per the
  task's scope ("do not redesign or rewrite ClinGen"), this was **not**
  changed in this pass; it is flagged here as it was in Phase 1 so it
  isn't lost. Until re-verified, `GEPER_CLINGEN_OFFLINE=true` (or simply
  not configuring `GEPER_CLINGEN_API_ENABLED`) keeps the pipeline on
  the local-dataset path exclusively.
- **The local dataset is deployer-provisioned, not bundled by
  default.** `GEPER_CLINGEN_GENE_VALIDITY_FILE` /
  `GEPER_CLINGEN_DOSAGE_FILE` default to empty strings; a production
  deployment needs to point these at a real, periodically-refreshed
  copy of ClinGen's Gene-Disease Validity and Dosage Sensitivity
  downloads (https://search.clinicalgenome.org/kb/gene-validity and
  .../kb/dosage/download) for ClinGen evidence to be found for genes
  beyond whatever small fixture ships in `testdata/`.
- **`clingen_gene_id` is only ever an HGNC id sourced from the
  Gene-Disease Validity file.** ClinGen's Dosage Sensitivity download
  does not carry its own gene-id column, so a gene present *only* in
  the dosage file (with no validity curation at all) will still show
  `clingen_gene_id: null` — this is a genuine absence of an identifier
  in that source file, not a mapping bug, and is unchanged here.
- **Actionability data was not exercised in this positive-control
  pass** (the bundled and hand-built fixtures used for validation
  don't include actionability rows). The actionability field-mapping
  code itself was not touched by this fix and has its own existing
  coverage in `test_clingen_acmg.py`/`test_clingen_provider.py`.
