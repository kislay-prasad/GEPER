# GEPER AI Model License Audit

Every AI model GEPER loads, plus every AI model that was evaluated and
explicitly **not** integrated, with source, license, and commercial-use
status. This is the deliverable for Objective 8 ("commercial
compliance"). Sourcing for each row is cited so this can be
independently re-verified; nothing here is asserted from memory alone
without a primary or peer-reviewed secondary source.

**Bottom line: every model GEPER actually loads and runs is
commercially usable with no copyleft/redistribution obligation.**
The one model considered and rejected (OpenSpliceAI) was rejected
*specifically* for licensing reasons, and DNABERT-2 has been removed
from GEPER entirely (see below) as part of this change.

**Scope note (2026-08-20): "removed from GEPER entirely" below refers
only to the `geper/` pipeline.** kim_pipeline (the integrated product's
FASTQ-to-VCF engine) retains and actively uses DNABERT-2 independently
(`kim_pipeline/pipeline/ai/engine.py::DnaBertEngine`), with a pinned
`revision` on `from_pretrained()` to mitigate the `trust_remote_code=True`
RCE surface -- see root `DATA_PROVENANCE.md` for the security review.
This audit file does not cover kim_pipeline's own model/dependency
license posture generally (that remains a separate, real, open card --
not undertaken here). kim_pipeline's DNABERT-2 usage was reviewed for
**security** on 2026-08-20 (the `trust_remote_code=True` RCE surface,
mitigated with a pinned `revision` -- see root `DATA_PROVENANCE.md`);
as of 2026-08-28 it had **not been reviewed for licensing**: no one
had independently fetched and confirmed `zhihan1996/DNABERT-2-117M`'s
license -- the security review fetched the model's *code* to read for
RCE risk, not its license metadata -- and `kim_pipeline/docs/
DATA_PROVENANCE.md`, the only license/provenance document kim_pipeline
has, does not mention DNABERT-2 or its license at all. That was an
open, unreviewed gap, not a completed review.

**RESOLVED 2026-09-09: DNABERT-2 is Apache-2.0.** The 2026-08-28 gap
above stood because the one thing fetched to date was the HuggingFace
**model card body** (the rendered README) -- its own YAML frontmatter
carries no `license:` key, and reading that absence as "unlicensed" or
"unreviewable" was the mistake: a rendered card is a *view*, and it
does not necessarily surface every field the underlying repository
metadata holds. Fetching the **model-info API endpoint**
(`huggingface.co/api/models/zhihan1996/DNABERT-2-117M`) instead shows
a `siblings` array that lists a file literally named `LICENSE` --
present in the repository, absent from the card. Fetching that file
directly (`huggingface.co/zhihan1996/DNABERT-2-117M/raw/main/LICENSE`)
returns the complete, unmodified canonical **Apache License, Version
2.0** text. Cross-checked independently against the code repository:
GitHub's own license-detection API for `github.com/MAGICS-LAB/
DNABERT_2` returns `spdx_id: Apache-2.0`, consistent with the weights
repo, not contradicting it. No `NOTICE` file exists in either repo.
Both code and weights are therefore Apache-2.0 -- permissive,
commercially usable, with the same NOTICE/LICENSE-preservation
obligation as every other Apache-2.0 entry in this document. Full
redistribution analysis (does Apache-2.0 permit *bundling* the weights
into a customer-shipped image, not just using them) is in
`PACKAGING_REDISTRIBUTION_LICENSE_AUDIT.md`, a separate file scoped to
that separate question; this entry records the underlying licence
fact the redistribution analysis depends on, not the redistribution
verdict itself.

## Models GEPER loads and runs

Code license and weights license are listed **separately** because they
can differ (confirmed to actually differ, not just hypothetically, for
several rows below -- see "Full-catalogue re-verification" for the
primary-source fetch behind every cell). "Version/weights used" is the
literal source URL/repo id GEPER's own loader code downloads from,
grepped from that code, not inferred from the project's README.

| Model | Version / weights used | Repository | Code license | Weights license | Commercial use permitted | Notes |
|---|---|---|---|---|---|---|
| HyenaDNA | `hyenadna-medium-450k-seqlen` (`huggingface.co/LongSafari/hyenadna-medium-450k-seqlen-hf`, git-lfs clone -- `models/hyenadna.py`) | github.com/HazyResearch/hyena-dna | **Apache-2.0** | **BSD-3-Clause** | Yes | Corrected 2026-08-08: the code repo's own license badge/LICENSE is Apache-2.0, distinct from the LongSafari HF weights card's `license: bsd-3-clause` tag -- the previous row only listed the weights license as if it covered both. Both permissive, no conflict. |
| Evo 2 | 7B checkpoint (`arcinstitute/evo2_7b`, loaded via the official `evo2` pip package -- `models/evo2.py`) | github.com/ArcInstitute/evo2 | **Apache-2.0** (composite: repo's `LICENSE` bundles Apache-2.0 as the primary terms plus BSD-3-Clause-licensed NVIDIA code and MIT-licensed Facebook/fairseq code carried in from upstream dependencies, all permissive) | **Apache-2.0** | Yes | Confirmed via the repo's own composite `LICENSE` file and the HF model card (`arcinstitute/evo2_7b`, frontmatter `license: apache-2.0`). No practical CPU inference path -- GEPER already treats it as CUDA-only and skips it gracefully otherwise. |
| RNA-FM | Official `ml4bio/RNA-FM`; weights tried in order: (1) official HF mirror `huggingface.co/cuhkaih/rnafm`, (2) upstream CUHK endpoint as last-resort fallback -- both the same authors' own official distribution (`models/rna_fm.py`) | github.com/ml4bio/RNA-FM | **MIT** | **Apache-2.0** (`cuhkaih/rnafm` HF frontmatter, confirmed directly) | Yes | GEPER deliberately integrates the *official* `rna-fm` package, not the `multimolecule` mirror (**AGPL-3.0-or-later**, confirmed rejected -- see `MIGRATION_RNA_FM.md`). Both weight sources checked here are the RNA-FM authors' own (no third-party mirror involved), so this rules out the SpliceBERT/multimolecule pattern for RNA-FM too. |
| ESM-2 | `esm2_t33_650M_UR50D` (Meta FAIR, via `transformers.from_pretrained`) | github.com/facebookresearch/esm | **MIT** | **MIT** (`facebook/esm2_t33_650M_UR50D` HF frontmatter, confirmed directly) | Yes | Confirmed via the repo's own `LICENSE` and the HF model card. (The separate ESM Metagenomic Atlas *dataset* is CC-BY-4.0 and is not used by GEPER.) |
| AlphaMissense | Precomputed pathogenicity catalogue, downloaded directly from `storage.googleapis.com/dm_alphamissense/AlphaMissense_hg{19,38}.tsv.gz` (`models/alphamissense.py`) | github.com/google-deepmind/alphamissense | **Apache-2.0** (code; GEPER does not use the code, lookup-only) | **CC BY 4.0** | Yes | **Genuine internal discrepancy flagged in `config.py::AlphaMissenseConfig`'s own docstring, now resolved -- see "Full-catalogue re-verification" below.** GEPER uses an indexed `tabix` lookup against the catalogue file itself, not the model weights (none are published). |
| MMSplice | `mmsplice` PyPI package (`--no-deps` install, `.h5` files + `layers.py` loaded directly, `mmsplice/__init__.py` never executed -- see `pipeline/models/mmsplice/loader.py`) | github.com/gagneurlab/MMSplice_MTSplice | **MIT** | **MIT** | Yes | Confirmed directly against the repo's own `LICENSE` (Jun Cheng, 2018) and the MMSplice paper's own data-availability statement. |
| TensorFlow | Required, unconditional dependency of `pipeline/models/mmsplice/` (MMSplice's Keras submodels; see `requirements.txt`) -- not a model GEPER trains or loads weights into directly, but a mandatory runtime dependency | github.com/tensorflow/tensorflow | **Apache-2.0** | N/A -- framework, not a trained-weights artifact | Yes | Used indirectly via MMSplice's dependency chain, not invoked by GEPER's own code directly. Added to this audit 2026-08-20 (was previously missing from the table despite being a required dependency). |
| Enformer | `enformer-pytorch` wrapper (`CONFIG.splicing.ENFORMER_HF_REPO`, default `EleutherAI/enformer-official-rough`, loaded via `enformer_pytorch.from_pretrained` -- `pipeline/models/enformer_plugin.py`) | github.com/lucidrains/enformer-pytorch | **MIT** | **CC BY 4.0** | Yes | **Corrected 2026-08-08** -- see "Full-catalogue re-verification" below: the exact HF repo GEPER downloads from declares `license: cc-by-4.0` in its own frontmatter, not Apache-2.0 (the previous row's Apache-2.0 claim cited Kaggle's separately-hosted `deepmind/enformer` listing, a different distribution channel than the one GEPER's code actually points at). Still fully permissive/commercial-safe; the correction is about which permissive license applies, not a new risk. Enabled by default (`CONFIG.splicing.ENABLE_ENFORMER=true`). |
| Borzoi | `borzoi-pytorch` wrapper (`CONFIG.splicing.BORZOI_HF_REPO`, default `johahi/borzoi-replicate-0`, `_load_impl` hard-refuses any repo id outside the `johahi/` namespace -- `pipeline/models/borzoi_plugin.py`) | github.com/johahi/borzoi-pytorch | **Apache-2.0** | **CC BY 4.0** | Yes | **Corrected 2026-08-08** -- see "Full-catalogue re-verification" below: both the `johahi/borzoi-pytorch` GitHub `LICENSE` and the `johahi/borzoi-replicate-0` HF frontmatter were fetched directly; neither says MIT (the previous row's MIT claim, cited to the Flashzoi paper's wording, does not match either primary source directly). Still fully permissive/commercial-safe. The code-level guard restricting weight loads to the `johahi/` namespace (never Calico's unlicensed original `.h5` files) is unaffected by this correction and was independently confirmed still in place. |
| SpliceFormer | Vendored official source (`Code/src/model.py`/`weight_init.py`) + one official pretrained replicate checkpoint (`transformer_encoder_45k_171022_0`), pinned to release tag `v1.0.0` | github.com/benniatli/Spliceformer | **MIT** | **MIT** | Yes | Confirmed directly against the repo's own `LICENSE` (Benedikt Atli Jónsson, 2024). Distinct from the Zenodo archive deposit's separate, archive-level CC-BY-4.0 metadata tag (deposit metadata, not the operative code license). Enabled by default (`CONFIG.splicing.ENABLE_SPLICEFORMER=true`). |
| SpliceBERT | Official pretrained checkpoint (`SpliceBERT.1024nt`) fetched from the author's own Zenodo archive (DOI 10.5281/zenodo.7995778, `models.tar.gz`) | github.com/biomed-AI/SpliceBERT | **BSD-3-Clause** | **CC-BY-4.0** (confirmed directly via Zenodo's own record API, not inferred from the GitHub repo) | Yes | **Verified 2026-08-08 in response to a specific licensing concern** -- see "SpliceBERT source verification" below. GEPER downloads exclusively from `zenodo.org/record/7995778/...`, the same URL the official repo's own `download.sh` fetches; never touches `huggingface.co/multimolecule/...` (a different, AGPL-3.0-licensed re-hosting under the same model name). `multimolecule` is not a dependency anywhere in `requirements.txt`. **Disabled by default as of 2026-08-20** (`CONFIG.splicing.ENABLE_SPLICEBERT=false`) due to a known load-timeout failure against `transformers` 5.13.1 (see DATA_PROVENANCE.md); this is a runtime-stability gate, not a licensing concern -- the licensing findings in this row are unaffected. |
| SPiP | Vendored official, unmodified R source (`SPiPv2.1_main.r`, run as an `Rscript` subprocess) + reference data (trained randomForest model, RefSeq transcript annotation, genome sequence) from the official repo/its linked SourceForge host (`pipeline/models/spip_plugin.py`, `pipeline/models/spip/loader.py`) | github.com/LBGC-CFB/SPiP (mirrored at github.com/raphaelleman/SPiP, same content, primary author's own account) | **MIT** | N/A -- reference data (NCBI RefSeq transcript annotation + hg19/hg38 genome sequence), not a third party's trained model weights; public-domain/unrestricted, same category as the RefSeq/genome-coordinate data every other GEPER stage already reads | Yes | **Added to this audit 2026-08-08 -- was missing from the table entirely, same gap SpliceBERT had before today.** Confirmed directly against `raphaelleman/SPiP`'s own `LICENSE` (raphaelleman, 2020). Enabled by default (`CONFIG.splicing.ENABLE_SPIP=true`); not wired into ensemble/ACMG evaluation, same as SpliceFormer/SpliceBERT. |

## Models evaluated and explicitly not integrated

| Model | Repository | License | Commercial use permitted | Reason not integrated |
|---|---|---|---|---|
| OpenSpliceAI | github.com/Kuanhao-Chao/OpenSpliceAI | **GPL-3.0** (code and bundled pretrained models) | **No** (for a proprietary commercial codebase) | Linking GPL-3.0 code/weights into GEPER's proprietary codebase would require distributing the combined work under GPL-3.0. Was registered as a hard-disabled placeholder (`pipeline/models/pending_plugins.py::OpenSpliceAIPlugin`) so it reported **Disabled** in every report rather than silently not existing; that placeholder was removed from the codebase entirely (no longer revisited -- SpliceFormer, SpliceBERT, and MMSplice already cover splice prediction). Listed here for audit-trail completeness only. |
| DNABERT-2 | github.com/MAGICS-LAB/DNABERT_2 | Apache-2.0 (permissive) | Yes (was) | **Removed from `geper/` entirely as part of this change** -- not a licensing rejection. HyenaDNA now serves as the universal default DNA model in its place (see `pipeline/router.py`). Listed here only for audit-trail completeness, since it was present in the codebase before this change. **Not removed product-wide**: kim_pipeline (the integrated product) retains and actively uses DNABERT-2 -- see scope note above. |
| SpliceAI (Illumina) | github.com/Illumina/SpliceAI | **GPL-3.0** (code) / **CC BY-NC 4.0** (pretrained models, as of Illumina's Dec 2023 relicense) | **No** | Evaluated in an earlier audit (see `PERFORMANCE_REPORT.md` / project memory); both the GPL-3.0 code license and the CC BY-NC 4.0 model license are commercial blockers. **Never integrated into `geper/`** -- this row's scope, like the rest of this file, is `geper/` only. **Not the same claim product-wide**: kim_pipeline (the other half of the two-repo product) also removed SpliceAI's VEP-plugin path for the same licence reason (2026-08-22). **Corrected 2026-09-10** -- this row previously said a separate fallback in `kim_pipeline/pipeline/annotation/stage.py` "still parses SpliceAI's own `SpliceAI=`/`DS_AG`/`DS_AL`/`DS_DG`/`DS_DL` delta-score fields directly out of an input VCF's INFO and feeds a live score to PP3/BP4 when the caller has pre-annotated with SpliceAI upstream." That fallback has since been closed: commit `b394bfa` ("close the SpliceAI INPUT, not another route") removed it, and `kim_pipeline/tests/test_spliceai_input_is_closed.py` (9 tests) now asserts on the ACMG classification outcome itself -- a benign SpliceAI score no longer moves PP3/BP4 -- not merely on parsing, so no SpliceAI-derived score can reach an ACMG classification through this path any longer. See `docs/BIJ_AI_CAPABILITY_AUDIT.md`'s combined-scope entry for that half of the product. Not the same project as OpenSpliceAI (a different, GPL-3.0-only reimplementation, above) -- listed separately to avoid conflating the two. |

## Full-catalogue re-verification (2026-08-08)

Following the SpliceBERT investigation above (which found LICENSE_AUDIT.md
had been incomplete -- SpliceBERT was missing from the table entirely),
every other model GEPER loads was re-verified against primary sources with
the same rigor: fetching the actual `LICENSE` file / HF model-card
frontmatter directly, confirming which exact source URL GEPER's own loader
code downloads from (not just the project's README), and explicitly
checking each weight source for a same-content-different-license mirror
(the SpliceBERT/multimolecule pattern) -- not just re-typing prior claims.

Model registry cross-checked against `pipeline/orchestrator.py`
(`MODEL_REGISTRY`, `_MODEL_LABELS`) and `pipeline/models/pending_plugins.py`
(`build_default_registry`): confirmed complete, and found one
previously-undocumented model (**SPiP**) that this audit had never listed.

**No AGPL/GPL/non-commercial code path was found for any model.** Three
real inaccuracies in the *previous* version of this audit were found and
corrected (table above); none of them change the commercial-use verdict,
but all three are worth stating plainly since the whole point of this
exercise is not softening or burying what's actually found:

1. **Enformer's weights license was wrong.** The previous row cited
   Apache-2.0 via Google's Kaggle Models listing for `deepmind/enformer`.
   But GEPER's actual code (`CONFIG.splicing.ENFORMER_HF_REPO`, default
   `EleutherAI/enformer-official-rough`) downloads from a *different*
   distribution channel than that Kaggle listing. Fetched
   `huggingface.co/EleutherAI/enformer-official-rough/raw/main/README.md`
   directly: frontmatter reads `license: cc-by-4.0`. The model card body
   confirms these are "the official weights released by Deepmind, ported
   over to Pytorch" -- so this is still officially DeepMind's own weights,
   just under CC-BY-4.0 (attribution required) at this specific host,
   not Apache-2.0. Still fully commercial-safe; the correction is which
   permissive license's attribution terms actually apply.
2. **Borzoi's code AND weights licenses were both wrong.** The previous
   row claimed MIT for both, cited to wording in the Flashzoi paper.
   Fetched `github.com/johahi/borzoi-pytorch`'s own license badge and its
   raw `LICENSE` file directly: **Apache-2.0**, not MIT (confirmed twice,
   via two independent fetches of the same repo). Fetched
   `huggingface.co/johahi/borzoi-replicate-0/raw/main/README.md`
   directly: frontmatter reads `license: cc-by-4.0`, not MIT. Both are
   still fully permissive and commercially usable -- but Apache-2.0
   carries a NOTICE-preservation obligation MIT doesn't, and CC-BY-4.0
   carries an attribution-on-redistribution obligation, neither of which
   `BorzoiPlugin.metadata()`'s current `license_name="MIT..."` field
   would prompt anyone to think about. The code-level guard in
   `BorzoiPlugin._load_impl` that refuses any HF repo id outside the
   `johahi/` namespace (never Calico's unlicensed original `.h5` files)
   is a separate, correct control and is unaffected by this correction.
3. **HyenaDNA's code license was missing, not wrong.** The previous row
   listed only BSD-3-Clause, sourced from the LongSafari HF weights
   card. That's correct for the *weights*, but the previous row didn't
   separately check the *code* repo (`github.com/HazyResearch/hyena-dna`,
   which GEPER `git clone`s directly in `models/hyenadna.py`) -- fetched
   its GitHub license badge directly: **Apache-2.0**, a different
   (still permissive) license than the weights.

**AlphaMissense's discrepancy, already flagged by GEPER's own code, is
now resolved with direct primary-source confirmation:**
`config.py::AlphaMissenseConfig`'s docstring already stated this
honestly as "a genuine, unresolved discrepancy" -- the official GitHub
repo says predictions are CC BY 4.0, but the Ensembl VEP plugin docs, the
EBI announcement, and a HuggingFace dataset mirror all instead described
CC BY-NC-SA 4.0 (non-commercial only), and GEPER's own comment said it
did not know which currently governs. Investigated directly:
  - `github.com/google-deepmind/alphamissense`'s current README: CC BY
    4.0 for predictions, Apache-2.0 for code.
  - `Ensembl/VEP_plugins`' **current `main` branch** `AlphaMissense.pm`:
    CC BY 4.0 -- but the **`release/110` tag** (an older snapshot) still
    reads CC BY-NC-SA 4.0. This is the source of the apparent conflict:
    the VEP plugin was updated after DeepMind's relicense, and the older
    tagged release simply predates it.
  - `huggingface.co/datasets/katielink/dm_alphamissense` (a third-party,
    unofficial mirror, not DeepMind's own): still reads CC BY-NC-SA 4.0
    -- consistent with an unofficial mirror that was never updated after
    the relicense, not evidence of an ongoing dual license.
  - **Decisive check: the actual GCS bucket GEPER downloads from.**
    Listed `storage.googleapis.com/dm_alphamissense/` directly (the
    exact host `AlphaMissenseConfig.HG38_URL`/`HG19_URL` point at): its
    `README.pdf` is dated **13 March 2024** -- the exact date of
    DeepMind's public relicense -- and fetched that PDF directly: "The
    AlphaMissense predictions data files in this bucket are available
    under the Creative Commons Attribution 4.0 International License,"
    linking `creativecommons.org/licenses/by/4.0/legalcode`. This is a
    primary-source confirmation from the literal bucket GEPER's own
    `models/alphamissense.py` downloads from, not an inference from the
    GitHub repo or a secondhand announcement.
  - **Conclusion: CC BY 4.0 governs the files GEPER actually downloads.**
    The discrepancy `config.py` flagged was real (a genuinely stale
    third-party mirror and an outdated tagged VEP release both do say
    CC BY-NC-SA 4.0), but the exact bucket/files GEPER's code points at
    carry a same-dated CC BY 4.0 README, resolving the "which one
    applies to what I actually download" question the code comment left
    open. `config.py::AlphaMissenseConfig`'s docstring still recommends
    independent legal/DeepMind confirmation before commercial reliance,
    which remains sound practice regardless of this finding -- this
    audit entry does not remove that recommendation, only adds a
    stronger primary-source data point to it.

**Models confirmed matching the previous audit's claims exactly** (code
and weights license both independently re-fetched and re-confirmed, no
correction needed): Evo 2, RNA-FM, ESM-2, MMSplice, SpliceFormer,
SpliceBERT.

**Same-content-different-license mirror explicitly checked and ruled out
for every model**, not just SpliceBERT: RNA-FM (official HF mirror
`cuhkaih/rnafm`, Apache-2.0, confirmed to be the *same authors'* own
distribution, not the AGPL `multimolecule/rnafm` reimplementation);
Enformer (`EleutherAI/enformer-official-rough` confirmed to be an
authorized PyTorch port of DeepMind's own official weights, not a
third-party retrain); Borzoi (`johahi/` namespace confirmed via the
peer-reviewed Flashzoi paper to be ported with Calico's own permission,
and the code-level namespace guard was independently re-confirmed
in `_load_impl`); HyenaDNA, ESM-2, MMSplice, SpliceFormer, SpliceBERT,
SPiP (each has exactly one official distribution channel for its weights
-- no alternate mirror exists to check).

**Re-verify this section if:** any model's loader/plugin download logic
changes (a new fallback source, a changed default HF repo id, a changed
Zenodo/GCS record), a new model plugin is added, or as part of general
periodic license-compliance review before a commercial release.

## SpliceBERT source verification (2026-08-08)

**Concern investigated:** SpliceBERT has two distribution sources
under different licenses -- the original author's own release
(github.com/biomed-AI/SpliceBERT, BSD-3-Clause) versus a separate
re-hosting on `huggingface.co/multimolecule/splicebert`
(AGPL-3.0-or-later, per the `multimolecule` library's own license --
the same library GEPER already migrated *away from* for RNA-FM, see
`MIGRATION_RNA_FM.md`). Using the AGPL distribution in a networked
commercial service would carry a copyleft obligation to open-source
GEPER's codebase. A real Colab run's download log showed the Zenodo
URL, which is the expected/safe outcome, but given the severity of
being wrong this was re-traced end-to-end and confirmed against
primary sources rather than resting on that one log line.

**Findings:**

1. **Every SpliceBERT code path traced.** The only places GEPER
   references SpliceBERT are `pipeline/models/splicebert_plugin.py`
   (`SpliceBERTPlugin`) and `pipeline/models/splicebert/loader.py`
   (the download/cache/load helpers it calls). The download URL is
   hardcoded to `_ARCHIVE_URL_TEMPLATE =
   "https://zenodo.org/record/{record}/files/models.tar.gz?download=1"`
   (`loader.py:32-34`), with `record` defaulting to
   `DEFAULT_ZENODO_RECORD = "7995778"` and overridable only via
   `CONFIG.splicing.SPLICEBERT_ZENODO_RECORD` /
   `GEPER_SPLICEBERT_ZENODO_RECORD` (`config.py:2432`) -- a deployment
   pin knob, not a fallback. There is no other download URL, no
   `try`/`except`-driven alternate-source fallback, and no reference
   to `huggingface.co`, `multimolecule`, or any package/import that
   would pull multimolecule's model weights, anywhere in
   `splicebert_plugin.py` or `loader.py` (confirmed by grep, not
   sampling). The model/tokenizer are loaded via plain
   `transformers.AutoModelForMaskedLM`/`AutoTokenizer` against the
   locally-extracted Zenodo checkpoint (`loader.py:198-202`) -- no
   `multimolecule` import anywhere in that load path.
2. **`multimolecule` is not a GEPER dependency.** `requirements.txt`
   has no `multimolecule` package line (confirmed by grep across the
   file). The only repo-wide references to `multimolecule` are in
   `models/rna_fm.py`'s and
   `scripts/compare_rna_fm_embeddings.py`'s own docstrings/code, both
   about a *different* model (RNA-FM, not SpliceBERT) and both
   documenting the same AGPL-3.0-or-later rejection this audit is
   confirming for SpliceBERT independently (see the RNA-FM row
   above). `compare_rna_fm_embeddings.py` is a standalone, manually-run
   comparison script (not imported by `orchestrator.py` or any plugin)
   -- installing `multimolecule` is required to even run it, and it is
   never invoked as part of a GEPER pipeline run. Since loading
   multimolecule's own checkpoint format requires the `multimolecule`
   library, its absence as a dependency independently rules out that
   code path entirely for SpliceBERT.
3. **No fallback-source logic exists.** `SpliceBERTPlugin._load_impl`
   downloads from the one hardcoded Zenodo URL; on a network error it
   demotes the plugin to unavailable for the run (the same
   graceful-degradation path every GEPER model plugin uses), it does
   not retry against a different host.
4. **Zenodo archive's own license, confirmed directly, not inferred
   from the GitHub repo.** Fetched `https://zenodo.org/api/records/7995778`
   directly: `metadata.license.id == "cc-by-4.0"` (record title
   "Self-supervised learning on millions of pre-mRNA sequences from 72
   vertebrates improves sequence-based RNA splicing prediction", DOI
   10.5281/zenodo.7995778, creators Chen/Zhou/Ding/Wang/Ren/Yang).
   This is **CC-BY-4.0, not BSD-3-Clause** -- the two are not
   automatically the same thing, and this audit deliberately did not
   assume they were: the *code* at github.com/biomed-AI/SpliceBERT is
   BSD-3-Clause (its own `LICENSE` file, fetched directly, states
   "Copyright (c) 2023, Ken Chen" under the standard BSD-3-Clause
   text), but the *weights* are published only on Zenodo, under
   Zenodo's own separately-declared CC-BY-4.0 terms. Both are
   commercially usable and redistributable; CC-BY-4.0 additionally
   requires attribution wherever the weights (or predictions derived
   from them) are redistributed, which `SpliceBERTPlugin.metadata()`'s
   `license_notes` field already carries through to any report/audit
   reader. Also fetched the official repo's own `download.sh`
   directly: it fetches the identical
   `https://zenodo.org/record/7995778/files/models.tar.gz?download=1`
   URL GEPER's loader uses, confirming GEPER's download path matches
   the *upstream authors' own* documented distribution mechanism, not
   an independently-discovered or third-party mirror URL.

**Conclusion:** No AGPL-linked code path exists anywhere in GEPER for
SpliceBERT. GEPER exclusively downloads from the original authors'
Zenodo archive (BSD-3-Clause code, CC-BY-4.0 weights, both
commercially usable), never from `huggingface.co/multimolecule/...`.

**Re-verify this note if:** `pipeline/models/splicebert/loader.py` or
`pipeline/models/splicebert_plugin.py`'s download/load logic is ever
changed (particularly if a fallback source or alternate checkpoint
host is ever added), `multimolecule` is ever added as a dependency for
any reason, or as part of general periodic license-compliance review
before a commercial release (see "Outstanding items" below).

## Data sources used alongside these models (not AI models, listed for completeness)

ClinVar, dbSNP, gnomAD, ClinGen, UniProt, InterPro, and the AlphaFold
Protein Structure Database are public reference/annotation lookups,
not AI models, and are out of scope for this AI-model audit. gnomAD's
terms and AlphaFold DB's CC-BY-4.0 status were already covered in
earlier project audits (see `VALIDATION_REPORT.md`).

## Outstanding items for a production commercial release

1. **Independent legal review.** This audit was compiled by
   cross-referencing each project's own `LICENSE`/`NOTICE` files,
   official model cards, and (for AlphaMissense) DeepMind's own
   relicensing announcement and the exact GCS bucket's own README --
   not by outside counsel. Before a commercial release, GEPER's actual
   legal/compliance team should independently verify each row.
2. ~~`BorzoiPlugin.metadata()` and `EnformerPlugin.metadata()`'s
   in-code `license_name` fields are inaccurate.`~~ **Fixed 2026-08-08.**
   Corrected in `pipeline/models/enformer_plugin.py` and
   `pipeline/models/borzoi_plugin.py` (`metadata()`, module docstrings,
   and -- for Borzoi -- the `BorzoiLicenseGuardError` message, which
   also echoed the old "MIT-licensed" claim to anyone who mis-configured
   `BORZOI_HF_REPO`) to match this table: Borzoi's code is now labeled
   Apache-2.0, its weights CC BY 4.0; Enformer's weights are now
   labeled CC BY 4.0. Confirmed no other module reads `license_name`/
   `license_notes` downstream (grepped the whole repo -- neither
   `status.py`'s "AI Models" table nor `provenance.py` surfaces these
   fields; only the plugin files themselves and their own tests do), so
   fixing `metadata()` alone was sufficient, with no propagation gap.
   Regression coverage added: `tests/test_enformer_plugin.py::
   TestEnformerMetadataAndAvailability::
   test_metadata_does_not_leak_the_old_incorrect_weights_license` and
   `tests/test_borzoi_plugin.py::TestBorzoiMetadataAndAvailability::
   test_metadata_does_not_leak_the_old_incorrect_mit_label` each assert
   the old, wrong license substring is now absent, so it can't silently
   regress.
3. **Attribution/NOTICE compliance.** Apache-2.0 (Evo 2, RNA-FM's HF
   mirror, HyenaDNA's and Borzoi's code) and CC BY 4.0 (AlphaMissense,
   Enformer's weights, Borzoi's weights, SpliceBERT's weights) all
   require attribution in the shipped product; GEPER does not yet have
   a consolidated third-party-notices file for the AI layer. This
   audit should be linked from (or merged into) that file once it
   exists.
4. **RNA-FM's single download endpoint** is a licensing non-issue but
   an operational one -- see the TODOs list in the final summary.
