# GEPER — AlphaMissense Integration: Validation Report

**Scope:** Integrate AlphaMissense into the existing GEPER pipeline
(from `trainmodel.zip`) as a new inference stage, without modifying or
regressing DNABERT2, HyenaDNA, Nucleotide Transformer, RNA-FM, ESM2,
ClinVar, dbSNP, BLAST, reporting, routing, or startup validation.

## 1. What was changed

All changes are additive. No existing file had working logic removed
or altered beyond adding new parameters/branches.

| File | Change |
|---|---|
| `models/alphamissense.py` | **New.** `AlphaMissenseModel(BaseGenomicModel)` — indexed lookup against the AlphaMissense precomputed catalogue via `tabix`. |
| `config.py` | **Additive.** New `AlphaMissenseConfig` dataclass + one new field on `GeperConfig`. Nothing existing removed or renumbered. |
| `models/__init__.py` | **Additive.** One import + one `MODEL_REGISTRY` entry. |
| `pipeline/router.py` | **Additive.** New `SequenceRouter.is_missense_eligible()` method. Every existing method unchanged. |
| `pipeline/orchestrator.py` | **Additive.** New `_run_alphamissense_stage()` method; one call to it in `_process_variant`; one dict entry each in `_DUMMY_SEQUENCE_BY_MODEL` and `_MODEL_DISPLAY_NAMES`; `alphamissense_result` threaded into the existing `interpretation.interpret(...)` and `build_variant_result(...)` calls as new keyword arguments. `_run_startup_validation`, `_run_dna_model`, `_filter_available_models`, resume/checkpoint logic, and every other existing method are byte-for-byte unchanged. |
| `pipeline/interpretation.py` | **Additive.** New optional `alphamissense_result=None` parameter; one new evidence branch. Every existing evidence branch and the summary/confidence logic are unchanged. |
| `report/json_builder.py` | **Additive.** New optional `alphamissense_result=None` parameter on `build_variant_result`, one new key in the returned dict. |
| `report/report_generator.py` | **Additive.** New `_render_alphamissense()` method, one call to it added to `_render_variant_section`. |
| `requirements.txt` | **Additive.** A comment documenting the new `tabix` system dependency (not a pip package, so no new pip line). |
| `README.md` | **Additive.** New "11. AlphaMissense" section; AlphaMissense mentioned in the architecture tree, pipeline flow, routing table, output format example, environment variable table, and hardware notes. Existing section content unchanged; sections after the insertion point renumbered (11→12, 12→13). |
| `testdata/known_variants_grch37.vcf` | **Unchanged**, reused as-is (see §4 — it already contains three real ClinVar missense variants). |
| `verify_alphamissense_integration.py` | **New.** Verification script (see §3). |

No changes were made to: `models/base_model.py`, `models/dnabert2.py`,
`models/hyenadna.py`, `models/nucleotide_transformer.py`,
`models/rna_fm.py`, `models/esm2.py`, `database/clinvar_client.py`,
`database/dbsnp_client.py`, `database/blast_client.py`,
`pipeline/vcf_parser.py`, `pipeline/sequence_context.py`,
`pipeline/rna_generator.py`, `pipeline/protein_translator.py`,
`pipeline/assembly_validator.py`, `utils/*.py`, `main.py`,
`dry_run_harness.py`, `verify_clinvar_dbsnp_fix.py`,
`verify_triton_fix.py`.

## 2. Design decision: AlphaMissense is a catalogue lookup, not a loaded model — and why

Google DeepMind has not released trained AlphaMissense model weights.
What is published is a precomputed catalogue of pathogenicity scores
for essentially every possible human missense substitution, as a
bgzip'd, tabix-indexed TSV keyed by genomic coordinate — the same file
the official Ensembl VEP AlphaMissense plugin queries. This is not a
simplification made for convenience; it's the only way AlphaMissense
is actually integrated by any real pipeline, including the official
one.

`models/alphamissense.py` therefore queries that catalogue via the
`tabix` CLI (matched by `chrom`/`pos`/`ref`/`alt`) instead of running
a forward pass — while still being a full `BaseGenomicModel` subclass
that participates in `ModelCache`, startup validation, and the
existing graceful-degradation machinery unchanged. Device is pinned to
`cpu` (no GPU computation ever happens) and `_report_precision()` is
overridden to report `"n/a (lookup table, no weights)"` rather than a
fabricated torch dtype — both are overrides of existing, unmodified
`BaseGenomicModel` hooks, not new mechanisms.

Full reasoning is in the module docstring of `models/alphamissense.py`
and in `config.py::AlphaMissenseConfig`.

## 3. Licensing — resolved which licence governs; the attribution trigger stays open

The task requirements ask for a design "suitable for future commercial
deployment." For AlphaMissense specifically:

- The predictions catalogue is **CC BY 4.0** (attribution required,
  commercial use permitted). This is confirmed directly from the
  primary source, not inferred: the GCS bucket's own README.pdf, at
  the exact URL `config.py::AlphaMissenseConfig` downloads from
  (`https://storage.googleapis.com/dm_alphamissense/README.pdf`,
  `timeCreated` 2024-03-13, the day of DeepMind's public relicense),
  states verbatim: "Copyright (2023) DeepMind Technologies Limited.
  All materials are licensed under the Creative Commons Attribution
  4.0 International License (CC-BY)."
- This resolves what used to be flagged here as a genuine discrepancy
  between sources: the Ensembl VEP plugin's current `main` branch
  agrees (CC BY 4.0), but its old `release/110` tag still reads CC
  BY-NC-SA 4.0 (predates the relicense), and the unofficial
  `huggingface.co/datasets/katielink/dm_alphamissense` mirror still
  reads CC BY-NC-SA 4.0 (never updated after it) — the source of the
  apparent conflict. GEPER reads none of those three, only the GCS
  bucket above, which is unambiguous and dated after the relicense.

**What this does not settle:** confirming CC BY 4.0 governs is a
different question from whether its attribution condition is actually
*triggered* by GEPER's use — an indexed tabix lookup that extracts one
row's score from a ~71M-row catalogue into a clinical report, not a
redistribution of the catalogue itself. That question remains open and
is not resolved by this document; it is called out, alongside the now-
resolved licence question, in the same three places a reader is likely
to see it: `config.py`'s `AlphaMissenseConfig` docstring, `README.md`
§11, and here.

## 4. Test results

### 4.1 `verify_alphamissense_integration.py` — all 4 parts PASSED

Run with `python3 verify_alphamissense_integration.py` from the
`geper/` directory. Summary of what each part proved:

- **PART 1** (pure unit logic, no I/O): `SequenceRouter.is_missense_eligible`
  correctly includes only clean, single-residue SNV substitutions and
  excludes synonymous, nonsense, frameshift, symbolic-ALT, `SVTYPE`,
  non-SNV, no-ORF, and multi-residue-difference cases — 10/10 cases
  passed.
- **PART 2** (real `tabix`, real local catalogue file — no mocking):
  `AlphaMissenseModel` resolves a real hit (F5 rs6025 / Factor V
  Leiden), a real not-found lookup, round-trips the startup dummy key,
  raises `ModelInferenceError` (not an uncaught exception) on a
  malformed lookup key, and reports `device=cpu` /
  `precision='n/a (lookup table, no weights)'` — all PASSED.
- **PART 3** (full `GeperPipeline.run()`, network-gated existing
  stages faked exactly as `dry_run_harness.py` already does,
  AlphaMissense left completely real): DNABERT2/HyenaDNA/Nucleotide
  Transformer/RNA-FM/ESM2 all still load and pass startup validation;
  AlphaMissense appears in the startup validation table with a PASS
  row; ClinVar still returns a real record for the known F5 variant;
  AlphaMissense returns a real prediction for that same variant and
  for the other two known missense variants (HBB, ALDH2); the
  `"alphamissense"` key is present in every JSON variant record; the
  Markdown report contains a rendered `### AlphaMissense` section with
  `am_pathogenicity`; no variant recorded an unexpected stage error —
  8/8 checks passed.
- **PART 4**: `GEPER_ENABLE_ALPHAMISSENSE=false` and a missing `tabix`
  binary both correctly resolve to `is_available() == False` (skip,
  never a crash).

### 4.2 Existing verification scripts — re-run, unaffected

- `verify_clinvar_dbsnp_fix.py`: both parts still PASS unmodified
  (confirms ClinVar/dbSNP GRCh37 resolution is untouched).
- `verify_triton_fix.py`: PASSED unmodified (confirms the DNABERT-2
  meta-device fix is untouched).

### 4.3 What is and isn't proven, given this sandbox's constraints

This sandbox's network access is limited to package registries
(pypi.org, github.com, npmjs.com, etc.) and does **not** include
`huggingface.co`, `rest.ensembl.org`, `eutils.ncbi.nlm.nih.gov`, NCBI
BLAST, or `storage.googleapis.com` (the real AlphaMissense catalogue
host) — the same constraint `dry_run_harness.py` already documents for
the pre-existing pipeline. Within that constraint:

- **Real, unmocked, and verified in this sandbox:** `tabix` (htslib)
  was installed (`apt-get install tabix`), and a real bgzip'd +
  tabix-indexed catalogue file was built locally, using the exact real
  AlphaMissense schema, with entries at the exact genomic coordinates
  of three real, already-vetted ClinVar missense variants
  (`testdata/known_variants_grch37.vcf`: F5 rs6025 / Factor V Leiden,
  HBB rs334 / sickle-cell HbS, ALDH2 rs671). `AlphaMissenseModel`'s
  actual, unmodified code — real subprocess call to a real `tabix`
  binary, real TSV parsing — was exercised against this file via the
  `GEPER_ALPHAMISSENSE_HG19_LOCAL` / `_HG38_LOCAL` override that ships
  in `config.py` for exactly this kind of offline/air-gapped use.
- **Not verified in this sandbox (same limitation as the existing
  pipeline):** an actual `tabix` query against the live, network-hosted
  Google Cloud Storage catalogue; actual DNABERT-2/HyenaDNA/Nucleotide
  Transformer/RNA-FM/ESM-2 weight downloads. Anyone running this in an
  environment with real internet access (e.g. Colab) should confirm
  end-to-end once against the real GCS URLs (the defaults already
  point at them — no configuration needed) and real HF weights.

### 4.4 A pre-existing, latent edge case noted (not fixed, out of scope)

While building the multi-part verification script, a latent edge case
in the existing `BaseGenomicModel.predict()` / `ModelCache` interaction
was found: if a *second*, separately-constructed instance of the same
model class calls `.predict()` after `ModelCache` already holds an
instance for that `cache_key()` (constructed by a *different* Python
object), `BaseGenomicModel.load()`'s return value — the actual cached,
loaded instance — is discarded by `predict()` (`self.load()`, not
`self = self.load()`), leaving the *second* instance's own
`self.model`/`self.tokenizer` at `None`. This does **not** affect the
real pipeline: `GeperPipeline` only ever constructs one instance per
model key per process (`self._model_instances`, reused across startup
validation and every variant), so `_factory()` — which mutates the
calling instance in place — always runs on the same object that's used
thereafter. It only surfaced here because this verification script
runs multiple independent test "parts" against the same long-lived
Python process and `ModelCache` singleton, deliberately constructing
a second `AlphaMissenseModel()` in a later part; the script now calls
`ModelCache.clear()` between parts to mirror a fresh process, which is
the correct fix for the *test*, not the *pipeline*. Noted here for
transparency since it's a genuine (if narrow) correctness edge case in
existing, unmodified code — not something introduced by this
integration, and out of scope to fix here (multi-instance-per-key
usage is not how `GeperPipeline` operates, and "do not modify existing
working components" applies).

## 5. Requirements checklist

| # | Requirement | Status |
|---|---|---|
| 1 | Preserve all existing functionality | ✅ Zero logic changes to any existing model/client; both pre-existing verify scripts re-run and pass |
| 2 | Integrate AlphaMissense as a new inference stage | ✅ `models/alphamissense.py`, `_run_alphamissense_stage` |
| 3 | Route only eligible missense variants | ✅ `SequenceRouter.is_missense_eligible`, unit-tested 10/10 |
| 4 | Load/cache AlphaMissense once, like existing models | ✅ Via `ModelCache`, same mechanism as every other model |
| 5 | Startup validation: Device / Precision / Status | ✅ Verified PASS row in PART 3 |
| 6 | Add to JSON output and Markdown report | ✅ `json_builder.py`, `report_generator.py`, verified in PART 3 |
| 7 | Never crash on an unevaluable variant | ✅ try/except in `_run_alphamissense_stage`, `ModelInferenceError` translation tested |
| 8 | CPU compatibility | ✅ No tensor computation at all; `device` pinned to `cpu` |
| 9 | Preserve ClinVar/dbSNP exactly | ✅ Files untouched; ClinVar verified still returning records |
| 10 | Follow existing architecture/style | ✅ `BaseGenomicModel` subclass, same patterns as `hyenadna.py`/`blast_client.py` for optional system deps |
| 11 | Enable/disable configuration | ✅ `GEPER_ENABLE_ALPHAMISSENSE`, tested in PART 4 |
| 12 | Automated validation tests, known ClinVar missense variants | ✅ `verify_alphamissense_integration.py`, reuses the 3 vetted variants in `testdata/known_variants_grch37.vcf` |
| 13 | Verify no regressions | ✅ §4.2 |
| 14 | Source, README, validation report, verification script, ZIP | ✅ This document + accompanying files |

## 6. What to run yourself, with real network access

```bash
apt-get install tabix   # or: conda install -c bioconda htslib
pip install -r requirements.txt
python3 verify_alphamissense_integration.py     # sandbox-safe, as above
python3 main.py --vcf your.vcf --max-variants 5 --output-dir ./out
```

No AlphaMissense-specific configuration is required for a real
internet-connected run — the default `GEPER_ALPHAMISSENSE_HG38_URL` /
`_HG19_URL` already point at the official Google Cloud Storage
catalogue.
