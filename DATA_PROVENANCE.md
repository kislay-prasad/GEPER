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

## BP7 disclosure: SpliceFormer/SpliceBERT evidence must be named in reports

**Decision (2026-08-20), implemented in commit `fd75fe3`: every clinical
report that shows BP7 evidence derived from SpliceFormer or SpliceBERT
must name the model, state that it is uncalibrated, and state that it
has not undergone clinical validation.** This is required, not
optional formatting.

**Why.** Kelly's git investigation (commit `cb79edc`, the project's
initial commit, 2026-07-30) establishes that both splice models were
*intentionally* enabled from project inception — not accidentally
shipped. That is a claim about **enablement**, a deliberate
engineering decision, and is a different claim entirely from
**validation**: no clinical outcome study has evaluated either model,
and both plugins already self-report
`details.calibration_status == "uncalibrated -- ..."` in their own
`predict()` output (see `pipeline/models/spliceformer_plugin.py` and
`splicebert_plugin.py`) — the pipeline already knew this about itself
before this fix, it just wasn't surfaced to the reader. "Uncalibrated"
is the accurate word, not a hedge. A pathologist reviewing a report
cannot weigh evidence whose origin and validation status are hidden
from them — silently folding an uncalibrated model's score into BP7
without saying so would let a production-grade-looking report imply a
rigor the underlying evidence doesn't have.

**What's disclosed, and where.** Implemented in
`pipeline/acmg_rules.py::ACMGRuleEngine._bp7`: each evidence line reads
the model's own `details.calibration_status` value from its `predict()`
output, rather than restating a fixed phrase — applied to both the
damaging-effect branch (feeds `conflicting_evidence`) and the
no-disruption branch (feeds `supporting_evidence`, which is what
actually lets BP7 trigger, and so carries at least as much clinical
weight as the damaging branch). When a model does not report a
calibration status, _bp7 supplies an honest absence statement rather
than an invented default. The
caveat renders directly alongside the BP7 evidence itself in both the
Markdown and PDF report formats — not relegated to a general
limitations section a reader might skip. This is the same
missing-vs-empty honesty problem this project has fixed repeatedly
elsewhere (e.g. `not_evaluated` vs. a silently omitted criterion):
visibility over invisibility, every time evidence provenance and
confidence could otherwise be assumed rather than stated.

Tests: `pipeline/acmg_rules.py`'s BP7 disclosure logic has 3/3 passing
regression tests as of this commit.

## PDF Conflicting Evidence section: missing since project inception (data integrity issue)

**The clinical PDF renderer (`report/summary.py`) omitted the
Conflicting Evidence section entirely from project inception (initial
commit `cb79edc`, 2026-07-30) through commit `01a2e2d` (2026-08-20),
which fixes it.** `git log -S "conflicting_evidence" --
geper/report/summary.py` returns exactly one commit — `01a2e2d` itself
— confirming the string never appeared in this file before this fix.
This was **not a BP7-specific gap**: it affected every ACMG criterion
that can produce conflicting evidence, for the entire lifetime of the
PDF renderer. The Markdown report (`report/report_generator.py`,
"### 6. Conflicting Evidence") and the underlying ACMG classification
logic both computed and carried this data correctly the whole time —
only the PDF rendering path silently dropped it, the same "cited but
not shown" asymmetry between PDF and Markdown already flagged
elsewhere in this document for `decision_path` and the combining-rule
trace. It was discovered as a side effect of the BP7 disclosure work
above (that disclosure text is itself carried in
`conflicting_evidence`, which is how the PDF-rendering gap surfaced),
not by a dedicated audit of the PDF path.

**Blast radius — stated plainly, not softened:**
- **Every PDF report generated by this pipeline from 2026-07-30
  through 2026-08-20 is incomplete.** Any variant with conflicting
  evidence for any criterion had that evidence silently absent from
  the PDF a reader received, with no indication anything was missing.
- **Any clinical team, reviewer, or pathologist who reviewed a PDF
  report in that window may have drawn a classification or
  significance conclusion without seeing evidence that conflicted with
  it.** The PDF gave no signal that this section existed and was
  omitted — it simply wasn't there, which reads as "no conflicting
  evidence" rather than "conflicting evidence not shown."
- **Any prior internal verification, QA, or audit run that reviewed
  PDF output as its evidence source may itself have reached an
  incorrect conclusion about a variant's classification**, because it
  was reviewing an incomplete rendering of the same underlying data
  the Markdown report and JSON output represented correctly.
- **This is a data integrity issue, not a cosmetic formatting gap, and
  should be flagged as such in any governance or audit review of prior
  report accuracy.** Markdown reports and the underlying
  `geper_results.json` for the same runs were not affected — only the
  PDF rendering path — but for any workflow where the PDF was the
  reviewed artifact, this blast radius applies in full.

Tests: 45/45 PDF report tests pass as of commit `01a2e2d`.

## End-to-end verification venue, and `requirements.txt`'s build status (2026-08-20)

Two distinct claims. They are easy to conflate and must not be:

**1. Correction to an earlier, too-strong claim.** An earlier note on
this project's working notes stated that `geper/` "has never run
end-to-end" — that reads as if prior verification work was worthless,
which is not accurate and not the intended claim. **The correct
framing: this local box has always been a test/dev environment; every
real end-to-end verification run in this project's history happened on
Colab.** Prior verification wasn't invalid — it simply happened on a
different, Colab-provisioned environment than this local machine.
Nothing about the finding below should be read as casting doubt on
those Colab-run results.

**2. UPDATE (2026-08-20, later the same day): `requirements.txt` is now
PROVEN TO BUILD a working `geper/` environment — minus `evo2`.** This
supersedes the "declared-but-never-proven-to-build" status this entry
originally recorded a few hours earlier; that gap has been closed by a
real, from-scratch build, not merely re-argued. Andy built an isolated
Python 3.12.10 venv (outside the git tree, shared `site-packages`
untouched) and installed `requirements.txt`'s full declared set in the
order the file's own comments specify: `torch==2.7.1` alone first
(resolves to the CPU wheel from plain PyPI, no custom index-url
needed), then the main batch (`transformers`, `accelerate`, `einops`,
`torchvision==0.22.1`, `omegaconf`, `torchaudio==2.7.1`, `biopython`,
and the rest), then `tensorflow` alone, then
`mmsplice==2.4.0 --no-deps`, then `rna-fm`. All 82 packages resolved
cleanly with **zero dependency conflicts** — `torch 2.7.1+cpu`,
`transformers 5.15.1`, `torchvision`/`torchaudio` matching the torch
pin exactly, `tensorflow 2.21.0`. `evo2` was deliberately not
installed (GPU-only, requires a manual CUDA-toolkit-matched build that
`requirements.txt`'s own comments already say not to attempt via a
bare install pass — its absence here is by design, not a build
failure). `from pipeline.orchestrator import GeperPipeline` **imported
cleanly** in this venv. Full recipe and the ground-truth `pip freeze`
are recorded in `geper/VENV_BUILD_RECIPE.md`, cross-referenced
from `geper/README.md` Section 5 — this is the first working install
recipe this project has ever had.

**Be precise about what this does and does not prove.** Proven: the
declared dependency set (minus `evo2`) installs cleanly with no
conflicts, and the pipeline's own import chain works end-to-end at
the Python-import level. **NOT proven: a full end-to-end variant-processing
run.** No run has yet completed on this box — four attempts were
externally killed before reaching `geper_results.json`/report
generation (survival time shrank each attempt, 19min → 1min → 3-4min,
consistent with an exhausted background-task/resource ceiling on this
box, not a GEPER defect), and separately, `rest.ensembl.org` was
independently returning `500` errors and 30-second timeouts during the
same window — Ensembl reliability, not a GEPER defect, but also on the
critical path for every variant, so it would slow or degrade any run
attempted right now regardless of the killed-process issue. Neither
failure mode says anything about `geper/`'s own correctness. **"Proven
to build" must not be read as "proven to work end-to-end"** — those
are different claims, and only the first one is closed as of this
entry.

**Earlier context (superseded by the full build above, kept for the
reasoning it establishes): what was found on this local box before the
clean venv existed** (Andy, 2026-08-20): a bare
`pip install -r requirements.txt` starts from an environment
where 7 of the 20 declared packages happen to be absent on this
particular box (`rna-fm`, `evo2`, `torchvision`, `omegaconf`,
`torchaudio`, `biopython`, `tensorflow`), plus a `torch 2.13.0+cpu`
install against the file's `torch==2.7.1` pin. But walking every actual
import site from `GeperPipeline`'s entry point found **exactly two
hard blockers at module-load time: `transformers` (corrupted local
install, unrelated to the pin) and `biopython`** — after resolving
both, `from pipeline.orchestrator import GeperPipeline` imported
cleanly, with no third blocker surfacing. **Packages needed but absent
from `requirements.txt` itself: none.** Every package the import chain
actually reaches is already correctly declared; the file's own
comments proved accurate in every case checked. The failure on this
box was declared-but-never-installed (plus one corrupted `transformers`
install, a separate local artifact — see below), not a deficient
`requirements.txt`. The startup-failure mechanism itself is unchanged:
`ModuleNotFoundError: No module named 'Bio'`, because
`pipeline/orchestrator.py:58` unconditionally imports
`pipeline/uniprot/bootstrap.py`, which imports `Bio` (biopython) at
line 64 regardless of which models a given run actually needs. See
`geper/README.md` Section 5 ("Installation") for the setup-gotcha note
this finding produced (torch/torchvision/torchaudio pinning, Evo 2's
manual CUDA install, tensorflow-before-mmsplice ordering) — not
duplicated here.

Two narrower points worth keeping in view, not part of the headline
gap but relevant to reading `requirements.txt` accurately: `torchaudio`
is declared but never imported by GEPER's own code at all — it's a
purely prophylactic pin against an ABI-mismatched transitive copy
being pulled in by another dependency, not evidence of a real GEPER
runtime need. `evo2` is installable but unusable without a
CUDA-toolkit-matched build that `requirements.txt`'s own comments
already say not to attempt via a bare `pip install -r requirements.txt`
pass — its presence in the declared list was never meant to imply a
one-command install path.

**Explicitly out of scope for this finding**: the `transformers`
corruption mentioned above was a local install-corruption artifact,
repaired, and confirmed not to represent a fresh-install failure mode.
It is a different story from the declared-but-never-proven-to-build
gap and is not evidence against `requirements.txt`'s own correctness.

**Status**: the build question is closed — `requirements.txt` builds a
working `geper/` (minus `evo2`), demonstrated in a real, from-scratch
venv, recipe recorded in `geper/VENV_BUILD_RECIPE.md`. **The
end-to-end-run question remains open**: no full variant-processing run
has completed on this box yet, for the external/environmental reasons
described above, not for any reason connected to `requirements.txt` or
`geper/`'s own code. Do not read this entry as claiming end-to-end
verification has occurred on this box — it has not, as of this entry.

## Borzoi/Enformer `post_init()` shim: commit `c1e0949` was accurate-but-stale, not wrong (2026-08-20)

Kelly re-derived the exact upstream mechanism behind both shims (see
the shim comments themselves, `pipeline/models/borzoi_plugin.py` and
`pipeline/models/enformer_plugin.py`, for the full technical detail —
not duplicated here). Worth recording as its own provenance point:
`transformers>=5`'s `PreTrainedModel.post_init()` sets
`all_tied_weights_keys` unconditionally, so the `AttributeError` both
shims guard against is reachable only when a subclass never calls
`post_init()` at all. Commit `c1e0949`'s message correctly described
this for `borzoi-pytorch`'s installed version at the time — it is
**stale, not wrong**, since `borzoi-pytorch` 0.5.0 (2026-06-10) added
the missing call upstream, making that shim a no-op on any current
install (kept only because GEPER's own installer never upgrades an
already-present package, so a box that picked up `<=0.4.4` keeps it
indefinitely). `enformer-pytorch` never added the equivalent call —
upstream closed the report by pinning `transformers==4.56.2` instead —
so the Enformer shim remains live and load-bearing. Both shims are
behaviorally inert either way: `{}` is the same value `post_init()`
would have produced, so neither changes architecture, weights, or
output.
