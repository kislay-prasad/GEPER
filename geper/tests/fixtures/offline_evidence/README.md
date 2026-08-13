# Offline evidence fixtures

Recorded evidence dicts for `InterpretationEngine.interpret()` /
`ACMGRuleEngine.evaluate()` / `report.json_builder.build_variant_result()`,
extracted from real, network- and model-backed GEPER runs. See
`extract_fixture.py`'s module docstring for why this is possible at all
(both functions are pure over plain dicts -- no model or network call
appears anywhere in their call graph) and `loader.py`'s docstring for
the schema-validation guard every fixture is loaded through.

**Scope boundary:** this fixture family only supports verifying
changes to the ACMG rule engine (`pipeline/acmg_rules.py`,
`pipeline/interpretation.py`) and the report renderers
(`report/json_builder.py`, `report/report_generator.py`,
`report/summary.py`, `report/summary_short.py`). It cannot verify a
change to an evidence-*gathering* stage (ClinVar/gnomAD/Ensembl
lookups, model inference) -- those still require a real run. See
`tests/test_acmg_net_points.py`'s module docstring for this stated at
the test-file level.

## Current fixtures

### `nuclear_test_with_mt_evidence.json`

| | |
|---|---|
| Source VCF | `test_data/nuclear_test_with_mt.vcf` |
| Source commit | `3c854c9` |
| Source run ID | `GEPER-RUN-20260813` |
| Variants captured | 5 nuclear (BRCA1×2, TP53×2, PRNP×1) |
| Variants skipped | 1 (MT:3243, mitochondrial out-of-scope -- never reaches `interpret()`; covered separately by `tests/test_mitochondrial_out_of_scope.py`) |
| Known-correct `acmg_net_points` | 10, 11, 5, −4, −4 (hand-computed and Colab-verified against this same run at round 9/10) |

Consumed by `tests/test_acmg_net_points.py`.

## Regenerating a fixture

Extraction is a pure JSON transform -- no model weights, no network,
safe to run on this machine's hardware constraints. It only needs a
real `geper_results.json` that was already produced elsewhere (Colab).

1. Get a real `geper_results.json` from a verified run (Colab; this
   machine cannot run the pipeline itself -- see the repo's hardware
   constraints).
2. Drop it anywhere locally, e.g. this directory as
   `raw_geper_results.json` -- **do not `git add` the raw file**; only
   the extracted fixture below is meant to be committed. (The repo's
   `.gitignore` already excludes `tests/fixtures/offline_evidence/raw_*.json`.)
3. Run:

   ```bash
   cd geper/tests/fixtures/offline_evidence
   python extract_fixture.py \
       --source raw_geper_results.json \
       --output nuclear_test_with_mt_evidence.json \
       --commit <git sha the source run was produced at> \
       --run-id <the run's own GEPER-RUN-... ID>
   ```

4. Update the fixture table above with the new commit/run-id and
   whatever the known-correct values now are (read them from the
   source run's own output -- `extract_fixture.py` already copies them
   into each variant's `"expected"` block verbatim, so this table
   should just restate what's already in the extracted file, not
   introduce a second hand-typed copy of the same numbers).
5. Run the consuming test:

   ```bash
   cd geper
   python -m pytest tests/test_acmg_net_points.py -v
   ```

   This alone must never require model weights, network access, or
   more than a few seconds -- if it does, something upstream of this
   fixture broke that guarantee and needs investigating before trusting
   the fixture at all.

## How to tell a fixture is stale

`loader.py::load_fixture` validates every variant's captured evidence
against `pipeline/stage_schemas.py::RawEvidenceBundle` (the same schema
production's own evidence-to-report boundary uses) on every load, and
raises `FixtureSchemaError` loudly if a captured dict no longer matches
what production's stage functions currently emit. That guard covers 11
of this fixture's ~19 evidence dicts -- see `loader.py`'s own docstring
for exactly which, and why the rest have no schema to check against.
A `FixtureSchemaError` means: regenerate the fixture from a fresh real
run, don't patch the old fixture by hand.
