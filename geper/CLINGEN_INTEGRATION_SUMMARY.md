# ClinGen Integration Summary

## 1. Files created

```
pipeline/clingen/__init__.py
pipeline/clingen/models.py      GeneDiseaseValidity, DosageSensitivity, Actionability, ClinGenGeneEvidence dataclasses
pipeline/clingen/utils.py       gene-symbol normalization, Ensembl-based gene resolution, curated-download row parsing
pipeline/clingen/cache.py       bounded LRU + TTL (24h default) + optional on-disk persistence
pipeline/clingen/provider.py    LocalDatasetClinGenProvider, LiveAPIClinGenProvider, CompositeClinGenProvider
pipeline/clingen/lookup.py      ClinGenLookup (orchestrator-facing facade; gene-symbol resolution + caching)

tests/test_clingen_models_utils.py   17 tests
tests/test_clingen_cache.py           8 tests
tests/test_clingen_provider.py       19 tests
tests/test_clingen_lookup.py         10 tests
tests/test_clingen_acmg.py           16 tests
tests/test_clingen_integration.py     7 tests

testdata/clingen_fixture/gene_validity_test.tsv        synthetic 5-gene fixture (BRCA1/TTN/SCN5A/GJB2/MTHFR)
testdata/clingen_fixture/dosage_sensitivity_test.tsv   synthetic dosage-sensitivity fixture, same 5 genes

testdata_bench/clingen_gene_validity_bench.tsv   synthetic 300-gene fixture (used by benchmark_clingen.py)
testdata_bench/clingen_dosage_bench.tsv          synthetic 300-gene dosage fixture

benchmark_clingen.py
CLINGEN_INTEGRATION_SUMMARY.md   (this file)
```

## 2. Files modified

```
config.py                    added ClinGenConfig dataclass; wired into GeperConfig.clingen; env_var_map entries
pipeline/orchestrator.py     import ClinGenLookup; self.clingen_client; new _run_clingen_stage();
                              wired into _process_variant (interpretation + result builder calls)
pipeline/interpretation.py   added clingen_result param + is_predicted_lof tracking +
                              _clingen_acmg_evidence() static method; call site appends evidence,
                              never replaces existing evidence
report/json_builder.py       added clingen_result param (default None -> {"skipped": True, "found": False});
                              new additive "clingen" key in the returned record
report/report_generator.py   added _render_clingen() -- dedicated "### ClinGen (Clinical Evidence)" panel
README.md                    external data sources, env var table, new section 17 (renumbered 17->18)
config.yaml.example          added clingen: block mirroring the mmsplice: block's style
```

**Note on requirement #4 ("Extend `AnnotatedVariant`")**: this codebase
does not have a persistent `AnnotatedVariant` object — each stage
(ClinVar, dbSNP, gnomAD, AlphaMissense, MMSplice, etc.) returns a plain
result dict that `report/json_builder.py::build_variant_result`
assembles into one record per variant. ClinGen follows that exact
existing convention: a `clingen` dict with the same shape the spec's
`AnnotatedVariant.clingen` describes (gene, gene_disease_validity,
disease, evidence_strength, expert_panel, haploinsufficiency,
triplosensitivity, actionability, clinical_validity, source,
last_updated), just keyed into the existing per-variant dict rather
than a class attribute.

## 3. Requirement-by-requirement status

| # | Requirement | Status |
|---|---|---|
| 1 | Create module (`pipeline/clingen/{lookup,provider,cache,models,utils}.py`) | Done |
| 2 | Gene-disease validity, dosage sensitivity (HI/TS), expert panel, actionability, disease associations, clinical validity, evidence strength, ClinGen IDs | Done — HI/TS scores + expert panel + gene-disease validity + actionability fully modeled; ClinGen's numeric "evidence strength" is represented via the classification tier itself (ClinGen's SOP doesn't publish a separate numeric evidence-strength score distinct from the classification — see "Known limitations") |
| 3 | Official APIs, official downloadable datasets, caching, retry, timeout, offline mode, graceful degradation | Done — local-dataset-first + live-API-fallback, retry/backoff/timeout on the API path, `GEPER_CLINGEN_OFFLINE`, never raises |
| 4 | Extend variant object | Done, adapted to this codebase's actual per-stage-dict architecture — see note above |
| 5 | ACMG integration: PVS1 via dosage sensitivity, PP5/BP6-style, gene-level support, never overwrite, configurable | Done — `_clingen_acmg_evidence()`, appends only, both thresholds configurable |
| 6 | Reports (JSON/Markdown/API) include ClinGen evidence | Done — additive `"clingen"` JSON key + dedicated Markdown panel |
| 7 | UI: dedicated ClinGen evidence panel | Done, with the same caveat as gnomAD's Phase 2 (see `PHASE2_GNOMAD_SUMMARY.md` §3) — this codebase's "UI" is its Markdown report; `_render_clingen()` is that panel |
| 8 | Performance: batching, caching, minimal memory, async, avoid duplicate lookups | Done, with one honest finding — see "Benchmark summary" below |
| 9 | Error handling: never crash, return null evidence, log warning | Done — verified with tests exercising a raising provider, a 404, and a network failure |
| 10 | Configuration: enable/disable, offline mode, cache location, timeout, retry count, endpoint override | Done — all via `ClinGenConfig` / `GEPER_CLINGEN_*` env vars |
| 11 | Unit / integration / regression / benchmark tests; validate against known ClinGen curated genes | Done — see "Tests added" and "Benchmark summary" below |
| 12 | Documentation (README, architecture, config, developer docs) | Done — README §17, config.yaml.example, this summary |
| 13 | Preserve existing functionality | Done — verified by re-running the full pre-existing test suite unmodified (see "Final verification") |
| 14 | Final verification | Done — see below |

## 4. Tests added — 77 new, all passing

```
$ python -m pytest tests/ -q --ignore=tests/test_evo2.py \
    --ignore=tests/test_mmsplice_predictor.py --ignore=tests/test_mmsplice_service.py
........................................................................ [ 37%]
...............................................................ssssss... [ 75%]
................................................                          [100%]
186 passed, 6 skipped in 0.68s
```

(186 = 109 pre-existing tests, unaffected by this work, plus 77 new
ClinGen tests. The 3 ignored files require a real `torch` install this
sandbox doesn't have — a pre-existing condition confirmed identical on
an untouched copy of the original codebase, unrelated to this
integration.)

**`tests/test_clingen_provider.py`'s `LocalDatasetClinGenProvider`
tests run against REAL fixture TSV files**
(`testdata/clingen_fixture/gene_validity_test.tsv` /
`dosage_sensitivity_test.tsv`), parsed by the real `csv.DictReader`
code path — not mocked. These fixtures use ClinGen's real, documented
column-name schema (`GENE SYMBOL`, `CLASSIFICATION`, `HAPLOINSUFFICIENCY
SCORE`, etc.) with **synthetic classification values** for 5
well-known ClinGen-curated genes (BRCA1, TTN, SCN5A, GJB2, MTHFR) —
these are genes ClinGen genuinely curates, but the specific
classification/score values in the fixture are illustrative test data,
not scraped from ClinGen's live database (this sandbox cannot reach
clinicalgenome.org to fetch them — see "Known limitations").

**Live-API-path tests mock `requests.get`** (retry/backoff/404-vs-
transient-failure/error-translation logic verified; ClinGen's actual
live API response shape was *not* independently re-verified against
the network — see "Known limitations" below, and the extensive caveat
already in `pipeline/clingen/provider.py`'s module docstring).

## 5. Validation summary

**What this sandbox could do:** `tests/test_clingen_integration.py`
and a manual end-to-end run (`_run_clingen_stage` -> real
`ClinGenLookup` -> real `LocalDatasetClinGenProvider` against the
fixture files, with only Ensembl's gene-symbol-resolution HTTP call
mocked) confirm the full evidence-combination path: a BRCA1 variant
predicted loss-of-function correctly picks up both a PVS1-supporting
haploinsufficiency contribution and a Definitive-classification
PP5-style contribution from `InterpretationEngine.interpret()`,
appended *alongside* — never replacing — the ClinVar/dbSNP/protein-
translation/gnomAD evidence already present in the same call.

**What this sandbox could NOT do:** query ClinGen's real gene-validity
or dosage-sensitivity downloads live, or its live API, both of which
require outbound network access this sandbox's egress allowlist
doesn't include. A direct attempt during this work
(`requests.get("https://search.clinicalgenome.org/api/gene/BRCA1")`)
returned `HTTP 403` with header `x-deny-reason: host_not_allowed` from
the egress proxy — confirming the block is real, and that the
graceful-degradation path (local dataset present but API unreachable)
is genuinely exercised by the composite provider's fallback logic, not
just written and assumed to work.

**To close this gap in a real deployment:**
1. Download ClinGen's actual current Gene-Disease Validity and Dosage
   Sensitivity files from https://search.clinicalgenome.org/kb/gene-validity
   and .../kb/dosage/download, point `GEPER_CLINGEN_GENE_VALIDITY_FILE`
   / `GEPER_CLINGEN_DOSAGE_FILE` at them, and re-run
   `tests/test_clingen_provider.py`'s local-dataset tests against a
   handful of real rows to confirm the column-name assumptions in
   `pipeline/clingen/utils.py::_GENE_VALIDITY_COLUMNS` /
   `_DOSAGE_COLUMNS` still match ClinGen's current export format
   exactly.
2. Make one live smoke-test call to
   `GEPER_CLINGEN_API_ENDPOINT` for a well-known curated gene (e.g.
   BRCA1 or SCN5A) and compare the actual response JSON against
   `_evidence_from_api_payload()`'s field-name assumptions in
   `pipeline/clingen/provider.py`, adjusting them if ClinGen's live API
   uses different key names than assumed.
3. Run `python main.py --vcf <a VCF of variants in ClinGen-curated
   genes>` with network access (or the local dataset provisioned) and
   spot-check that a known-pathogenic LOF variant in a gene like SCN5A
   (ClinGen: Definitive, haploinsufficiency: sufficient evidence)
   correctly picks up both ClinGen ACMG contributions in the resulting
   `geper_results.json`.

## 6. Benchmark summary

`benchmark_clingen.py`, run for real against a 300-gene synthetic
ClinGen-format fixture through the real `ClinGenLookup` code path
(`GEPER_CLINGEN_API_ENABLED=false`, local dataset only):

```
ClinGen benchmark -- 300 genes, local dataset only (no network)

Strategy                              Wall time      Genes/sec
--------------------------------------------------------------
A: sequential, cold cache                14.5ms      20757.4/s
B: batch (thread-pooled)                 17.4ms      17276.8/s
C: sequential, warm cache                 0.5ms     631415.4/s

Batch (thread-pooled) vs sequential speedup: 0.8x
Warm-cache vs cold-cache speedup: 30.4x
```

**Honest finding, not smoothed over:** unlike gnomAD's benchmark (where
thread-pooled batching wins because each lookup is a real I/O-bound
subprocess/HTTP call), ClinGen's local-dataset lookups are already an
in-memory dict access — thread-pool *overhead* here slightly exceeds
the (already tiny) per-lookup cost, so batching is measured at 0.8x
sequential, not a speedup, for this fixture. This is expected and
correctly reported rather than adjusted to look better: thread-pooled
batching in `CompositeClinGenProvider.batch_query` earns its keep on
the *live-API* fallback path (genuinely I/O-bound HTTP round trips),
not on the local-dataset path, which is why it's still implemented and
used automatically in `ClinGenLookup.query_variants_batch` — a
deployment relying primarily on the live API for genes not in its
local dataset will see the same kind of speedup gnomAD's benchmark
shows for its GraphQL-comparable path. Warm-cache reuse is the clear,
large win regardless of source (30x here on a purely in-memory
lookup), and is why `ClinGenLookup` still caches gene-level results by
default even though most lookups already hit a fast local dataset.

## 7. ClinGen integration summary

ClinGen is now a first-class evidence source with the same
architectural shape as gnomAD (Phase 2): a self-contained
`pipeline/clingen/` package (models/cache/provider/lookup/utils),
local-first + live-API-fallback querying, additive-only ACMG evidence,
a dedicated Markdown report panel, and a new additive JSON key. The one
structural difference — ClinGen curation is gene-level, not
variant-level — is handled by resolving each variant's overlapping
gene symbol via the same Ensembl `overlap/region` REST pattern MMSplice
already uses for exon annotation, memoized per genomic position so a
VCF covering many variants in one gene pays that resolution once.

## 8. Known limitations

- **Live API schema unverified against a real call** — this sandbox
  cannot reach `clinicalgenome.org` (confirmed HTTP 403,
  `x-deny-reason: host_not_allowed`); `LiveAPIClinGenProvider`'s
  endpoint path and `_evidence_from_api_payload()`'s field-name
  assumptions follow ClinGen's documented conventions but should be
  spot-checked against one live response before production use. The
  local-dataset path does not have this limitation (it's exercised
  end-to-end against real fixture files with ClinGen's real, documented
  column schema).
- **No numeric "evidence strength" field distinct from classification**
  — ClinGen's Gene-Disease Validity SOP expresses evidence strength
  *through* the classification tier itself (Definitive/Strong/
  Moderate/Limited/Disputed/Refuted), not as a separate numeric score;
  `classification_rank()` in `pipeline/clingen/models.py` provides a
  numeric ordering for internal comparisons (e.g. picking the
  strongest curation for a multiply-curated gene) but this is GEPER's
  own derived ranking, not a field ClinGen itself publishes.
- **Actionability data is API-only** — ClinGen's Clinical Actionability
  curations aren't part of the two flat-file downloads this
  integration's local-dataset path covers (only gene-disease validity
  and dosage sensitivity are), so `Actionability` results only ever
  populate via the live API fallback; a deployment running fully
  offline will never see actionability evidence. If this matters for a
  deployment, ClinGen's Actionability landing page
  (https://clinicalgenome.org/curation-activities/clinical-actionability/)
  should be checked for whether a comparable flat-file export now
  exists.
- **Gene-symbol resolution uses GRCh38 Ensembl gene bodies for both
  builds** — see `pipeline/clingen/utils.py::resolve_gene_symbol`'s
  docstring; a GRCh37-specific Ensembl mirror can be substituted via
  `CONFIG.api.ENSEMBL_REST_BASE` if a deployment needs build-exact gene
  boundaries, though gene *symbols* (unlike exon coordinates) rarely
  differ between builds for the same locus.
- **Multi-allelic / one-variant-many-genes edge case** — a genomic
  position overlapping two protein-coding genes on opposite strands
  resolves to whichever gene Ensembl's `overlap/region` response lists
  first (see `resolve_gene_symbol()`); this is a reasonable default but
  not exhaustive multi-gene evidence attribution.
