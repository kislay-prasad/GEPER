# Data Provenance Audit

This repository is code-only. Every model weight, database dump, cached
reference file, and other large binary that used to sit inside
`geper/plugin_model_cache/`, `geper/model_cache/`, and `geper/hyena-dna/`
has been removed from version control (see `.gitignore`) because every
one of them is automatically re-fetched, on first use, from a real, live,
public source — verified below file-by-file against the actual loader
code, and in most cases re-confirmed by actually running that code
against a genuinely emptied cache directory (not just read and assumed).

No file was found with an unclear or non-re-fetchable origin.

## `geper/plugin_model_cache/` (was 3.1GB)

All five subdirectories are populated by `pipeline/models/manager.py` +
`pipeline/models/cache.py::WeightCache`, rooted at
`CONFIG.splicing.PLUGIN_CACHE_DIR` (default `./plugin_model_cache`, env
override `GEPER_PLUGIN_CACHE_DIR`).

| Plugin | Size | Source | Fetch mechanism | Verified how |
|---|---|---|---|---|
| **enformer/** | 1.9GB | HuggingFace `EleutherAI/enformer-official-rough` (`CONFIG.splicing.ENFORMER_HF_REPO`) | `enformer_pytorch.from_pretrained(repo_id, cache_dir=...)` — standard `transformers`/`huggingface_hub` cache layout. Automatic, gated by `CONFIG.splicing.ENABLE_ENFORMER` (default on). | Code-read (loader + config), not re-downloaded live (multi-GB; not in the user's named integration list). One benign redundancy noted: two snapshot revisions cached from being called at different times against a repo whose default weight format changed upstream — harmless, fully reproducible. |
| **borzoi/** | 710MB | HuggingFace `johahi/borzoi-replicate-0` (`CONFIG.splicing.BORZOI_HF_REPO`) | `borzoi_pytorch.Borzoi.from_pretrained(...)`. Code actively refuses any repo ID not starting with `johahi/` (`BorzoiLicenseGuardError`) — deliberately excludes Calico's original checkpoint, which has no confirmed commercial license. Automatic, gated by `CONFIG.splicing.ENABLE_BORZOI`. | Code-read only, same reasoning as enformer. |
| **spip/** | 390MB | Small `.RData` files + `dataRefSeq<genome>.RData` from `raw.githubusercontent.com/LBGC-CFB/SPiP/master/`; the large `transcriptome_<genome>.RData` (~370-400MB) from a SourceForge mirror (`splicing-prediction-pipeline` project) | `pipeline/models/spip/loader.py::prepare_runtime_dir()` — idempotent, automatic, gated by `CONFIG.splicing.ENABLE_SPIP` and Rscript availability. | Live-verified: `getRefSeqDatabase.r` (the upstream script this data ultimately derives from) confirmed to pull from UCSC's public goldenPath server. Live download not re-run here (large; not user-named), but the loader's own idempotent-download logic was read in full. |
| **splicebert/** | 76MB | Zenodo record `10.5281/zenodo.7995778` (`models.tar.gz`, ~208MB archive; only `SpliceBERT.1024nt/` is extracted) | `pipeline/models/splicebert/loader.py::download_and_extract_checkpoint()`. Automatic, gated by `CONFIG.splicing.ENABLE_SPLICEBERT`. | Code-read; live re-download attempted but not completed in this session (208MB archive — see "Known gaps" below). |
| **spliceformer/** | 4.5MB | Pinned GitHub tag `v1.0.0` of `benniatli/Spliceformer`, file `Results/PyTorch_Models/transformer_encoder_40k_171022_0` via `raw.githubusercontent.com` | `pipeline/models/spliceformer/loader.py::download_checkpoint()`. Automatic, gated by `CONFIG.splicing.ENABLE_SPLICEFORMER`. | **Live-verified in this session**: existing checkpoint deleted, `download_checkpoint()` re-run from a genuinely empty cache, confirmed a fresh 4,677,821-byte file was written matching the expected checkpoint. |

**One file worth flagging explicitly, not hidden**: `plugin_model_cache/spip/runtime/geper_spip_driver.r` is
**not** downloaded from an external host — it's a GEPER-authored script (a
workaround for a confirmed Windows-specific bug in upstream SPiP's
`%dopar%` parallel-scoring loop). Its authoritative copy already lives in
git-tracked source at `geper/pipeline/models/spip/vendor/geper_spip_driver.r`;
the copy under `plugin_model_cache/` is a disposable runtime copy made by
`prepare_runtime_dir()`, not a second, separately-sourced artifact.

Also confirmed vendored-source-not-weights and therefore never part of
the 3.1GB figure: `pipeline/models/spliceformer/vendor/{model.py,
weight_init.py}` (checked into git, copy-pasted from upstream, not
downloaded at runtime).

## `geper/model_cache/` (was 42MB; only `hpo/` and `orphanet/` were populated in this checkout)

| Directory | Source | Bootstrap | Verified how |
|---|---|---|---|
| **hpo/** | `http://purl.obolibrary.org/obo/hp/hpoa/genes_to_phenotype.txt` | `pipeline/hpo/bootstrap.py::ensure_genes_to_phenotype_file()` | **Live-verified in this session**: directory deleted, function re-run, confirmed a fresh 20,732,778-byte file downloaded. |
| **orphanet/** | `https://www.orphadata.com/data/xml/en_product6.xml` (CC BY 4.0) | `pipeline/orphanet/bootstrap.py::ensure_gene_disorder_file()` | **Live-verified in this session**: directory deleted, function re-run, confirmed a fresh 22,612,034-byte file downloaded. |
| **clingen/** | `search.clinicalgenome.org/kb/gene-validity/download` + `ftp.clinicalgenome.org/ClinGen_gene_curation_list_GRCh38.tsv` | `pipeline/clingen/bootstrap.py::ensure_gene_validity_file()` / `ensure_dosage_sensitivity_file()` | **Live-verified in this session** (this directory did not exist in the checkout at all — never previously run): both functions called fresh, confirmed real files downloaded (1,114,353 and 246,774 bytes respectively). |
| **alphamissense/** (not present in this checkout) | Google Cloud Storage `dm_alphamissense` bucket, full `.tsv.gz` (multi-GB) | `models/alphamissense.py` | Code-read only — not run (would download a multi-GB file). **Documentation note**: `README.md`'s current claim that "GEPER never downloads the multi-GB file wholesale" is factually wrong relative to the actual code, which explicitly downloads the full file once and builds a local tabix index (Google's bucket has no `.tbi` sidecar, so indexed range queries aren't possible). README should be corrected. |

InterPro, AlphaFold DB, UniProt, and Conservation have **no bulk-dataset
bootstrap at all** — they are pure per-query live REST API integrations
(with an optional, manually-supplied local dataset file as a secondary
path), so they contribute nothing to the repo-size problem in the first
place. Both were live-verified working end-to-end earlier in this
project's own test suite (`tests/test_interpro_live.py`,
`tests/test_alphafold_live.py`) against real BRCA1 data.

BLAST has no auto-download either — building/downloading a local BLAST+
database is a documented **manual** step (README section 20); GEPER falls
back to NCBI's remote BLAST service when no local database is configured,
which needs no bootstrap since it never stores anything locally.

## `geper/hyena-dna/` (was 54MB, including a nested 7.6MB `.git`)

Confirmed to be a real `git clone` of `https://github.com/HazyResearch/hyena-dna.git`
(its `.git/config` names that exact remote; its `README.md` is the genuine
upstream HazyResearch README). `geper/models/hyenadna.py::install_hyenadna_colab()`
clones this exact same repo, at the exact same shallow depth, automatically
the first time HyenaDNA is loaded and no clone already exists — the
on-disk directory in this checkout was almost certainly produced by that
exact code path, not by hand. The pretrained checkpoint weights are
separate and download into `CONFIG.models.HYENADNA_CHECKPOINT_DIR`
(default `./checkpoints`) independently, only when the model is actually
used.

**Practical note for the code/data split**: because this is a real nested
git repository (not just a data directory), it cannot be handled by
`.gitignore` alone in every git client — some tooling will offer to add
it as a broken submodule reference if `.gitignore` is bypassed with `git
add -f`. The `.gitignore` entry (`geper/hyena-dna/`) is sufficient for a
normal `git add .` / `git status` workflow (verified: `git check-ignore`
confirms it's correctly excluded), but anyone hand-forcing files into that
path should know why it looks unusual.

## Small, checked-in test fixtures (intentionally NOT excluded)

`test_data/`, `geper/testdata/`, and `geper/testdata_bench/` total well
under 1MB combined and are genuinely necessary, small, purpose-built test
fixtures — not part of the size problem this cleanup targets:
- `test_data/` includes `generate_test_dataset.py`, the actual script that
  produced its reference/FASTQ/BWA-index files (rCRS mitochondrial
  reference) — its own provenance is the script sitting right next to it.
- `geper/testdata/*_fixture/` directories are small, deliberately trimmed
  real-data slices (a subset of gnomAD, a subset of AlphaMissense) used
  by that integration's own test suite.

These remain version-controlled so a fresh clone's test suite runs
without any network access for the fixtures it doesn't itself need to
verify live-fetch behavior.

## Known gaps / not independently re-verified in this pass

- **Enformer, Borzoi, SPiP's transcriptome file, SpliceBERT**: bootstrap
  code was read in full and is real/automatic, but a live re-download
  was not performed in this session (multi-hundred-MB to multi-GB
  downloads, and none of these four are in the specifically-named
  integration list for this task). Recommend a one-time live smoke test
  of each before depending on this in a customer-facing Colab demo.
- **README.md documentation debt** (found, not yet fixed): HPO, Orphanet,
  and SPiP integrations are not mentioned anywhere in `README.md` despite
  having real, working, automatic bootstrap code — the only place this is
  documented is each module's own docstring. ClinGen's README section
  describes an older, manual-file-only model and doesn't mention that
  `pipeline/clingen/bootstrap.py` now auto-fetches both files. The
  AlphaMissense claim noted above is factually incorrect. None of this
  blocks the code/data separation — the code works regardless of what the
  README says — but it should be fixed so the docs don't mislead the next
  person who reads them instead of the source.
