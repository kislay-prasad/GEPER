# Phase 2 Summary — gnomAD Integration

## 1. Files created

```
pipeline/gnomad/__init__.py
pipeline/gnomad/models.py       GnomadAnnotation, PopulationFrequency dataclasses
pipeline/gnomad/utils.py        build normalization, variant keys, VCF INFO parsing
pipeline/gnomad/cache.py        bounded LRU + TTL + optional on-disk persistence
pipeline/gnomad/provider.py     LocalIndexedGnomadProvider, GraphQLGnomadProvider, CompositeGnomadProvider
pipeline/gnomad/lookup.py       GnomadLookup (orchestrator-facing facade)

tests/test_gnomad_models_utils.py   27 tests
tests/test_gnomad_cache.py          10 tests
tests/test_gnomad_provider.py       15 tests (6 against a REAL tabix-indexed fixture)
tests/test_gnomad_lookup.py          5 tests
tests/test_gnomad_acmg.py           10 tests
tests/test_gnomad_integration.py     6 tests

testdata/gnomad_fixture/gnomad_test.vcf.gz(.tbi)   small hand-built fixture (used by inline smoke tests)
testdata_bench/gnomad_bench.vcf.gz(.tbi)           300-record synthetic fixture (used by benchmark_gnomad.py)

benchmark_gnomad.py
PHASE2_GNOMAD_SUMMARY.md   (this file)
```

## 2. Files modified

```
config.py                    added GnomadConfig dataclass; wired into GeperConfig.gnomad
pipeline/orchestrator.py     import GnomadLookup; self.gnomad_client; new _run_gnomad_stage();
                              wired into _process_variant (interpretation + result builder calls)
pipeline/interpretation.py   added gnomad_result param + _gnomad_acmg_evidence() static method;
                              call site appends evidence, never replaces existing evidence
report/json_builder.py       added gnomad_result param (default None -> {"skipped": True, "found": False});
                              new additive "gnomad" key in the returned record
report/report_generator.py   added _render_gnomad() -- dedicated "### gnomAD (Population Frequency)" panel
README.md                    external data sources, env var table, new sections 16 & 17
```

## 3. Requirement-by-requirement status

| # | Requirement | Status |
|---|---|---|
| 1 | Create module (`pipeline/gnomad/{lookup,provider,cache,models,utils}.py`) | Done |
| 2 | GRCh38 support; auto-detect GRCh37; SNV/insertion/deletion | Done — `normalize_build()`, `classify_variant_type()` |
| 3 | Retrieve genome/exome AF, AC, AN, hom, hemi, 7 populations + remaining | Done |
| 4 | Local indexed DB, GraphQL fallback, caching, retry, timeout, offline mode, batch, async | Done |
| 5 | Extend variant object with gnomad/genome_af/exome_af/ac/an/hom/hemi/highest_population/population_breakdown/source/build | Done — see `GnomadAnnotation.to_dict()` |
| 6 | ACMG: BA1/BS1/PM2, configurable thresholds, never overwrite | Done — `_gnomad_acmg_evidence()`, appends only |
| 7 | Reports: global/highest-pop/genome/exome AF, population table, clinical interpretation, ACMG contribution | Done — dedicated report panel + evidence list |
| 8 | API: expose all evidence, backward compatible | Done — additive `"gnomad"` JSON key, verified via test |
| 9 | UI: dedicated gnomAD evidence panel | Done, with a caveat — see "UI" note below |
| 10 | Performance: batching, caching, minimal memory, async | Done — bounded LRU, thread-pooled batch, asyncio batch |
| 11 | Unit / integration / benchmark tests; validate using ClinVar variants | Done — see "Validation" and "Benchmark" below, with one caveat |
| 12 | Documentation | Done — README sections 16 & 17 |

**UI note:** this codebase has no web front-end (it's a CLI/library +
Colab-notebook pipeline; `report/report_generator.py`'s Markdown output
*is* its existing "UI" — every other evidence source, e.g. "### 11.
AlphaMissense", gets exactly this kind of report-section treatment,
not a separate GUI widget). The gnomAD panel was built as a
`### gnomAD (Population Frequency)` Markdown section following that
same existing pattern, rendered per-variant in `geper_report.md`
alongside the AlphaMissense/MMSplice/BLAST sections. If a real web
front-end exists elsewhere in your deployment (outside this repo),
the additive `"gnomad"` JSON key is what it should read from.

## 4. Tests added — 73 new, all passing

```
$ python -m pytest tests/ -q --ignore=tests/test_evo2.py \
    --ignore=tests/test_mmsplice_predictor.py --ignore=tests/test_mmsplice_service.py
........................................................................ [ 65%]
......................................                                   [100%]
110 passed in 0.41s
```

(110 = 37 pre-existing MMSplice-utils tests, unaffected by this work,
plus 73 new gnomAD tests. The 3 ignored files require a real `torch`
install this sandbox doesn't have — a pre-existing condition unrelated
to this integration; see `PHASE1_VERIFICATION_REPORT.md` section 9.)

**`tests/test_gnomad_provider.py`'s 6 `LocalIndexedGnomadProvider`
tests run against a REAL bgzip'd + tabix-indexed fixture VCF, built and
queried with the real `tabix`/`bgzip` binaries** (not mocked) — these
genuinely exercise the subprocess-invocation and INFO-field-parsing
code, including a chr-prefix-vs-bare-chromosome case and a
hemizygote-on-chrX case.

**GraphQL-path tests mock `requests.post`** (retry/backoff/error-
translation logic verified; gnomAD's live current API response shape
was *not* independently re-verified against the network — see
"Known limitations" below).

## 5. Validation using ClinVar variants

**What this sandbox could do:** `tests/test_gnomad_integration.py`
verifies the full evidence-combination path using a synthetic
ClinVar-shaped record (`{"records": [{"clinical_significance":
"Pathogenic", ...}]}`) run through the real
`InterpretationEngine.interpret()`, confirming gnomAD's BA1 evidence is
appended *alongside* — not instead of — the ClinVar Pathogenic
evidence already present.

**What this sandbox could NOT do:** run this against real ClinVar
variant records fetched live from NCBI cross-referenced against real
gnomAD frequencies fetched live from Broad — both require outbound
network access this sandbox's egress allowlist doesn't include (see
`verify_environment.py`'s "Internet connectivity" check; a live
attempt to gnomAD's GraphQL endpoint during this work returned an
HTTP 403 from this sandbox's egress proxy, confirming the block is
real and the graceful-degradation path was genuinely exercised, not
just written).

**To close this gap in a real deployment:** run
`python main.py --vcf <a VCF of known ClinVar Pathogenic/Benign
variants>` with network access, and manually spot-check that
well-known common benign variants (e.g. a gnomAD AF > 5% variant)
correctly pick up BA1 and well-known rare pathogenic variants pick up
PM2, in the resulting `geper_results.json`.

## 6. Benchmark summary

`benchmark_gnomad.py`, run for real against a 300-variant synthetic
gnomAD-format fixture through the real `GnomadLookup` code path
(`GEPER_GNOMAD_OFFLINE=true`, local tabix index only — see the script's
own docstring for why the GraphQL path isn't benchmarked here):

```
Strategy                            Wall time   Variants/sec
------------------------------------------------------------
A: sequential, cold cache             700.8ms        428.1/s
B: batch (thread-pooled)              451.0ms        665.1/s
C: async batch                        485.9ms        617.4/s
D: sequential, warm cache               0.4ms     671058.4/s

Batch (thread-pooled) vs sequential speedup: 1.6x
Warm-cache vs cold-cache speedup: 1567.6x
```

These numbers are this sandbox's own CPU/disk and a synthetic fixture
— useful for confirming the *relative* behavior (batching helps,
caching helps a lot) and that the code paths function correctly, not
as a production-hardware capacity-planning number. Re-run
`benchmark_gnomad.py` on your actual deployment target for that.

## 7. Known limitations

- GraphQL-path correctness against gnomAD's actual, current live API
  response shape is untested here (mocked only) — this sandbox cannot
  reach the live endpoint (confirmed HTTP 403 from the egress proxy
  during this work). Recommend one live smoke-test call against a
  known variant (e.g. `1-55516888-G-GA`, a well-known common gnomAD
  variant) before relying on the fallback path in production.
- No real gnomAD sites VCF was available to test the local-index path
  against (a synthetic fixture was used instead, built with the real
  `tabix`/`bgzip` tools) — the INFO-field key conventions
  (`AC_afr`, `nhomalt_nfe`, etc.) match Broad's published VCF format
  documentation but haven't been cross-checked against an actual
  downloaded gnomAD release file in this pass.
- The GraphQL query's exact field names (`populations { id ac an
  homozygote_count hemizygote_count }`, dataset IDs `gnomad_r4` /
  `gnomad_r2_1`) reflect gnomAD's publicly documented GraphQL schema as
  of this project's training-era knowledge; gnomAD's API has changed
  its schema before and could again — if the live fallback path
  returns unexpected empty results in production, check
  https://gnomad.broadinstitute.org/api (or their GraphQL introspection
  endpoint) for schema drift first.
- Async batch querying (`async_query_variants_batch`) wraps the
  synchronous provider calls via `asyncio.to_thread`/a thread pool
  rather than using a native async HTTP client — this matches every
  other external client in this codebase (all `requests`-based) for
  consistency, but means the GraphQL fallback path's async mode still
  consumes one thread per in-flight request rather than being
  fully non-blocking at the socket level.
