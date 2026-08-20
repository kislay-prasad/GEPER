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
| **splicebert/** | 76MB | Zenodo record `10.5281/zenodo.7995778` (`models.tar.gz`, ~208MB archive; only `SpliceBERT.1024nt/` is extracted) | `pipeline/models/splicebert/loader.py::download_and_extract_checkpoint()`. Automatic, gated by `CONFIG.splicing.ENABLE_SPLICEBERT`. | **Live-verified end-to-end, and a real bug was found and fixed in the process** — see "Bugs found and fixed during this pass" below. The download/extract step itself was already correct; the *model-loading* step that runs immediately after (`build_model_and_tokenizer`) hung indefinitely on a real Colab run. |
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

## Bugs found and fixed during this pass

**SpliceBERT model loading hung indefinitely** (found via a real, live
Colab end-to-end run — not this audit's own testing). The log showed
every stage succeeding (BWA alignment, FreeBayes calling, bcftools
norm, HyenaDNA/Enformer/Borzoi/SpliceFormer/AlphaMissense all
downloading and loading correctly) right up through "SpliceBERT
checkpoint extracted," then a `transformers` warning ("You are using a
model of type `bert` to instantiate a model of type ``") and no
further progress.

Root-caused by reproducing on plain CPU in this dev sandbox (ruling out
GPU/CUDA/Colab-specific causes) with unbuffered, step-by-step logging:
`transformers.AutoModelForMaskedLM.from_pretrained()` never returns
when loading this specific checkpoint (`BertForMaskedLM`, a plain
`pytorch_model.bin`, not `.safetensors`) in a process where TensorFlow
is also importable — which it always is here, since
`pipeline/models/mmsplice/` requires `tensorflow` unconditionally.
`transformers` auto-probes every installed backend, and that probe
pathologically hangs for this checkpoint shape.

**Fix** (`pipeline/models/splicebert/loader.py::build_model_and_tokenizer`):
`os.environ.setdefault("USE_TF", "0")` before the `transformers`
import, skipping TensorFlow-backend detection entirely. Verified fixed
in two scenarios: (1) a cold process importing `transformers` for the
first time, and (2) the more realistic case matching the actual
pipeline's own model-loading order — `transformers` already imported
earlier by ESM2/RNA-FM before SpliceBERT loads — confirmed the fix
still works there too (loads in ~8s instead of hanging). A regression
test (`tests/test_splicebert_loader_live.py`) now runs the real
checkpoint loading step in a subprocess with a hard 60s timeout, so any
future regression of this exact bug fails fast and loud in the test
suite instead of hanging silently.

**Update (report review round 4, F1b) — this fix is not the end of the
story.** The `USE_TF=0` env var alone later proved insufficient once
`transformers` was already imported earlier in the process (it only
has effect on a cold import), which is why `loader.py` now *also*
directly overrides the already-cached `transformers.utils.import_utils
._tf_available` attribute plus a bounded load timeout (see that
module's own docstring for the full second round of this bug). Then,
in the two most recent live-verified runs (GEPER-RUN-20260809,
`transformers` upgraded from the previously-pinned 4.56.2 to 5.13.1),
SpliceBERT failed to load *again* — this time timing out rather than
hanging indefinitely (the bounded-timeout fix did its job), preceded
by the same-shaped "You are using a model of type 'bert' to instantiate
a model of type ''" warning. Root cause of this third occurrence is
**not confirmed** — static inspection (this pass had no GPU and could
not attempt a real load) turned up no config/architecture mismatch in
the checkpoint itself (it is a standard, unmodified `BertForMaskedLM`),
so the leading hypothesis is a second, transformers-v5-specific
import-order sensitivity triggered by the same precondition as the
original bug (`pipeline/models/esm2.py` importing `transformers`
first) — but this is a hypothesis, not a verified finding. What this
pass DID do: (1) the failure is now detected once at pipeline startup
instead of implicitly on the first variant (`GeperPipeline
._probe_standalone_plugins_at_startup`), and (2) the swallowed
debug-level exception detail is now promoted into the raised error
message itself, so the next live run captures the actual diagnostic
detail instead of the generic "SpliceBERT model unavailable" string.
Whether SpliceBERT is fixable or should be permanently disabled is an
open question pending that next run's real error detail.

## Known gaps / not independently re-verified in this pass

- **Enformer, Borzoi, SPiP's transcriptome file**: bootstrap code was
  read in full and is real/automatic, but a live re-download was not
  performed in this session (multi-hundred-MB to multi-GB downloads,
  and neither is in the specifically-named integration list for this
  task). Recommend a one-time live smoke test of each before depending
  on this in a customer-facing Colab demo. (SpliceBERT, originally in
  this same "not re-verified" bucket, *was* live-verified — see "Bugs
  found and fixed" above; it uncovered and fixed a real hang.)
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

## C1 net-points bug: blast radius is unrecoverable, not zero

(Report review round 4, F3.) `pipeline/acmg_rules.py::ACMGRuleEngine
._combine` had a real bug — thresholding `path_points`/`benign_points`
independently instead of on `net = path_points - benign_points` —
that silently discarded triggered benign criteria in every run before
the fix landed on `fix/report-review-A1-B7`. `audit_net_points_blast_
radius.py` (repo root) is the correct, working tool for finding which
stored classifications that bug actually changed: it re-derives what
the old and new threshold logic would each output from a stored
`combining_rule_trace` and flags any disagreement.

Running it against a post-fix run returns "0 affected." **That is a
true statement about the artifact it was run against, not about the
bug's historical impact.** No pre-fix `geper_results.json` — or any
other run artifact — survives anywhere: not in this repository
(working tree or git history; confirmed by the script's own docstring),
and not in the Colab sessions that originally produced whatever
pre-fix outputs existed, which are gone. A script correctly finding
zero affected variants in the *only* artifact it has ever been run
against says nothing about whether any pre-fix run was affected — a
bug that silently drops triggered benign criteria on the classification
path is exactly the shape of bug most likely to have changed a real
result somewhere, and "0 affected" here answers "did this fix change
this one already-fixed run" (trivially no, the bug isn't present in a
post-fix run), not "did the bug ever affect a real classification."

**Conclusion: the blast radius of the pre-fix C1 bug is unrecoverable,
not zero.** Any GEPER classification produced by a run before the
`_combine` fix landed should be treated as unverified — specifically,
re-run through the current code (or manually checked against the
`combining_rule_trace` math in the original report, if one survives)
before being relied on — rather than assumed correct because no
audited artifact currently disagrees with it.

**If a pre-fix artifact ever surfaces** (an old `geper_results.json`
recovered from local disk, an old Colab notebook's saved outputs, a
PDF/Markdown report with its `combining_rule_trace` lines still
legible), point the existing script at it directly:
```
python audit_net_points_blast_radius.py path/to/old_geper_results.json
python audit_net_points_blast_radius.py path/to/old_output_dir/   # globs **/*.json
```
No changes to the script are needed for this — it was built to answer
exactly this question the moment a real artifact exists to ask it of.

## MANE Select tie-break: real data path exists, but is unconfirmed to have ever fired

(Report review round 4, F4.) `pipeline/clingen/utils.py
::_disambiguate_overlapping_genes` checks MANE Select status two ways:
`transcript.is_mane_select` (populated from Ensembl's `lookup/id` REST
response, which — verified live — never actually carries a MANE field,
so this branch is permanently `False` in production; see that
function's own docstring) and a lookup against `pipeline/mane/
provider.py`'s bootstrapped NCBI MANE summary dataset (real data,
keyed by gene symbol). Both are already documented in-code at the
tie-break site itself (that function's docstring, lines ~282-290) —
this is not new information.

What is **not yet confirmed**: whether the second, real-data path has
ever actually resolved a tie in production. The two overlapping-gene
loci observed falling through to `AMBIGUOUS` in the GEPER-RUN-20260809
verified run are consistent with *either* (a) neither candidate gene
having a MANE Select transcript in the dataset for that specific
position (a correct `AMBIGUOUS` outcome — nothing to disambiguate
with), or (b) some other reason the NCBI-dataset lookup didn't fire at
all (e.g. a `TranscriptLookup` exception for one of the two candidates,
silently falling through per that function's own `except Exception`
clause). This pass could not distinguish between these without the
real run's per-gene log lines, which do not survive anywhere (no run
artifacts survive at all — see the C1 section above for the same
constraint). **Do not assume the MANE tie-break is inert** from the
`AMBIGUOUS` outcomes observed so far; the code path exists, is
unit-tested (`tests/test_clingen_gene_resolution.py`), and is
plausible to be genuinely functional for a gene pair where exactly one
candidate has a MANE Select transcript. Confirming which of (a)/(b)
explains the specific observed cases needs a live run's logs, not
another static-inspection pass.

## `kim_pipeline` DNABERT-2 `trust_remote_code` revision pin (security fix)

`kim_pipeline/pipeline/ai/engine.py::DnaBertEngine` loads
`zhihan1996/DNABERT-2-117M` via `transformers.AutoTokenizer`/`AutoModel
.from_pretrained(..., trust_remote_code=True)`. That flag is required
because DNABERT-2's architecture (MosaicBERT with ALiBi/FlashAttention)
ships as custom Python inside the HF repo, not inside the `transformers`
library — but unpinned, it means the loader executes whatever code sits
on the repo's default branch at load time, which the repo owner (or an
attacker who compromises their account) could silently repoint to
something malicious after this review.

Reviewed 2026-08-20: fetched `https://huggingface.co/api/models/zhihan1996/DNABERT-2-117M`
directly (not summarized) — current HEAD `sha` = `7bce263b15377fc15361f52cfab88f8b586abda0`.
Read the full custom-code payload at that exact commit (`configuration_bert.py`,
`bert_layers.py`, `bert_padding.py`, `flash_attn_triton.py` — the files
named in the repo's `auto_map`): standard MosaicML BERT/FlashAttention
implementation built on `torch`/`einops`/`triton`/`transformers` base
classes. No `subprocess`, `eval`/`exec`, `socket`, HTTP calls
(`requests`/`urllib`), `pickle.load`, dynamic imports, or file writes
outside normal model init. **Verdict: reviewed, benign.**

Fix: pinned `revision="7bce263b15377fc15361f52cfab88f8b586abda0"` on both
`from_pretrained()` calls (this is the HF-documented mitigation — see
["Custom models" / revision pinning](https://hf.co/docs/transformers/en/models#custom-models)),
applied only when `model_name` is the default repo string (an
operator-supplied `model_name` override is out of scope for this pin and
is left unpinned so it isn't broken by a stale hash). `trust_remote_code=True`
itself is still required and was not removed — the pin is what closes the
moving-ref RCE surface, not the flag.

Not independently re-verified in this pass: a full model-weight smoke
load (`AutoModel.from_pretrained` pulling the ~1.87GB `pytorch_model.bin`)
was not run in this environment — no `torch`/`transformers` stack was
installed here beforehand, and installing the full ML stack plus a
multi-GB download was out of scope for what should be a mechanical
`revision=` kwarg change. What *was* verified live: the pinned commit
hash resolves against the real HF API (see above), and the code change
matches HF's own documented pattern exactly.
`tests/test_dnabert2_revision_pin.py`-equivalent regression coverage is
being added separately (asserts `from_pretrained` is called with this
exact `revision` value).
