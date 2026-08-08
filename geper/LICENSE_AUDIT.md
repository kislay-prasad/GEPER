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

## Models GEPER loads and runs

| Model | Version / weights used | Repository | License | Commercial use permitted | Redistribution permitted | Notes |
|---|---|---|---|---|---|---|
| HyenaDNA | `hyenadna-medium-450k-seqlen` (LongSafari HF mirror) | github.com/HazyResearch/hyena-dna | **BSD-3-Clause** | Yes | Yes (BSD-3-Clause: attribution + retained notice) | Confirmed on the model's own Hugging Face card (`LongSafari/hyenadna-*-hf`, `License: bsd-3-clause`). |
| Evo 2 | 7B checkpoint (arcinstitute/evo2_7b) | github.com/ArcInstitute/evo2 | **Apache-2.0** (code and weights) | Yes | Yes (Apache-2.0: attribution + NOTICE + state changes) | Confirmed via the repo's own `LICENSE`/`NOTICE` files and the HF model card (`arcinstitute/evo2_7b`, `License: apache-2.0`). No practical CPU inference path -- GEPER already treats it as CUDA-only and skips it gracefully otherwise (`models/evo2.py`). |
| RNA-FM | Official `ml4bio/RNA-FM` (via the `rna-fm` PyPI package) | github.com/ml4bio/RNA-FM | **MIT** | Yes | Yes | GEPER deliberately integrates the *official* `rna-fm` package, not the `multimolecule` mirror, which is **AGPL-3.0-or-later** and was rejected for exactly this reason (see `MIGRATION_RNA_FM.md`, `models/rna_fm.py`). Residual operational risk: the official weights are hosted on a single, non-mirrored CUHK download endpoint that occasionally 403s -- a licensing non-issue, but see the TODOs below. |
| ESM-2 | `esm2_t33_650M_UR50D` (Meta FAIR) | github.com/facebookresearch/esm | **MIT** | Yes | Yes | Confirmed via the repo's own `LICENSE`/`setup.py`/source-file headers. (The separate ESM Metagenomic Atlas *dataset* is CC-BY-4.0 and is not used by GEPER.) |
| AlphaMissense | Precomputed pathogenicity catalogue (DeepMind) | github.com/google-deepmind/alphamissense | **CC BY 4.0** (predictions) | Yes | Yes (attribution required) | The predictions were originally CC BY-NC-SA 4.0 (non-commercial only); DeepMind **relicensed them to CC BY 4.0 on 13 March 2024**, lifting the non-commercial restriction (confirmed on DeepMind's own announcement page and the repo's `README.md`/data license notice). GEPER uses an indexed `tabix` lookup against this catalogue, not the model weights (`models/alphamissense.py`). |
| MMSplice | `mmsplice` PyPI package + Keras submodels | github.com/gagneurlab/MMSplice_MTSplice | **MIT** | Yes | Yes | Confirmed in the MMSplice paper's own data-availability statement ("available ... under the MIT License") and the repo itself. |
| Enformer | `enformer-pytorch` wrapper + `EleutherAI/enformer-official-rough` weights | github.com/lucidrains/enformer-pytorch | **MIT** (wrapper) / **Apache-2.0** (official DeepMind weights) | Yes | Yes | See `pipeline/models/enformer_plugin.py::EnformerPlugin.metadata()` for the full sourcing note (Kaggle Models license field for `deepmind/enformer`, corroborated by third-party model documentation). Disabled by default (`CONFIG.splicing.ENABLE_ENFORMER=false`); see TODOs. |
| Borzoi | `borzoi-pytorch` (johahi) wrapper + `johahi/borzoi-replicate-0` weights | github.com/johahi/borzoi-pytorch | **MIT** (wrapper and johahi-mirrored weights only) | Yes | Yes | See `pipeline/models/borzoi_plugin.py::BorzoiPlugin.metadata()`. The plugin deliberately never touches Calico's original `.h5` checkpoints (no equivalent explicit weight license) -- enforced in `_load_impl`, not just documented. Disabled by default (`CONFIG.splicing.ENABLE_BORZOI=false`); see TODOs. |
| SpliceFormer | Vendored official source (`Code/src/model.py`/`weight_init.py`) + one official pretrained replicate checkpoint (`transformer_encoder_45k_171022_0`), pinned to release tag `v1.0.0` | github.com/benniatli/Spliceformer | **MIT** (code and weights -- confirmed via the repo's own `LICENSE` file) | Yes | Yes | See `pipeline/models/spliceformer_plugin.py::SpliceFormerPlugin.metadata()`, including the note distinguishing the code's own MIT `LICENSE` from the Zenodo archive deposit's separate, archive-level CC-BY-4.0 metadata tag. Not added to `pipeline/models/ensemble.py`'s Enformer+Borzoi consensus and not wired into ACMG evaluation or report generation -- see that module's docstring. Enabled by default (`CONFIG.splicing.ENABLE_SPLICEFORMER=true`); gated on `einops` (already an unconditional GEPER dependency -- see requirements.txt) being importable. |
| SpliceBERT | Official pretrained checkpoint (`SpliceBERT.1024nt`) fetched from the author's own Zenodo archive (DOI 10.5281/zenodo.7995778, `models.tar.gz`) | github.com/biomed-AI/SpliceBERT | **BSD-3-Clause** (code) / **CC-BY-4.0** (Zenodo-hosted weights) | Yes | Yes (CC-BY-4.0 requires attribution, carried through in `SpliceBERTPlugin.metadata().license_notes`) | **Re-verified 2026-08-08 in response to a specific licensing concern** -- see "SpliceBERT source verification" below for the full trace. GEPER downloads exclusively from `zenodo.org/record/7995778/...`, the same URL the official repo's own `download.sh` fetches; it never touches `huggingface.co/multimolecule/...`, which distributes a *different*, AGPL-3.0-licensed re-hosting of SpliceBERT under the same model name. `multimolecule` is not a dependency anywhere in `requirements.txt` (a required package to load that distribution's checkpoint format, so its absence independently rules out that code path). Enabled by default (`CONFIG.splicing.ENABLE_SPLICEBERT=true`); not wired into ensemble/ACMG evaluation, same as SpliceFormer above. |

## Models evaluated and explicitly not integrated

| Model | Repository | License | Commercial use permitted | Reason not integrated |
|---|---|---|---|---|
| OpenSpliceAI | github.com/Kuanhao-Chao/OpenSpliceAI | **GPL-3.0** (code and bundled pretrained models) | **No** (for a proprietary commercial codebase) | Linking GPL-3.0 code/weights into GEPER's proprietary codebase would require distributing the combined work under GPL-3.0. Was registered as a hard-disabled placeholder (`pipeline/models/pending_plugins.py::OpenSpliceAIPlugin`) so it reported **Disabled** in every report rather than silently not existing; that placeholder was removed from the codebase entirely (no longer revisited -- SpliceFormer, SpliceBERT, and MMSplice already cover splice prediction). Listed here for audit-trail completeness only. |
| DNABERT-2 | github.com/MAGICS-LAB/DNABERT_2 | Apache-2.0 (permissive) | Yes (was) | **Removed from GEPER entirely as part of this change** -- not a licensing rejection. HyenaDNA now serves as the universal default DNA model in its place (see `pipeline/router.py`). Listed here only for audit-trail completeness, since it was present in the codebase before this change. |
| SpliceAI (Illumina) | github.com/Illumina/SpliceAI | **GPL-3.0** (code) / **CC BY-NC 4.0** (pretrained models, as of Illumina's Dec 2023 relicense) | **No** | Evaluated in an earlier audit (see `PERFORMANCE_REPORT.md` / project memory); both the GPL-3.0 code license and the CC BY-NC 4.0 model license are commercial blockers. Never integrated into GEPER. Not the same project as OpenSpliceAI (a different, GPL-3.0-only reimplementation, above) -- listed separately to avoid conflating the two. |

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
   relicensing announcement -- not by outside counsel. Before a
   commercial release, GEPER's actual legal/compliance team should
   independently verify each row, particularly AlphaMissense's 2024
   relicense (make sure the *specific* catalogue file GEPER's
   `tabix` index points at was published under the new CC BY 4.0
   terms, not an older CC BY-NC-SA-licensed mirror) and Borzoi's
   johahi-mirrored-weights provenance claim.
2. **Attribution/NOTICE compliance.** Apache-2.0 (Evo 2, Enformer's
   DeepMind weights) and CC BY 4.0 (AlphaMissense) both require
   attribution in the shipped product; GEPER does not yet have a
   consolidated third-party-notices file for the AI layer. This
   audit should be linked from (or merged into) that file once it
   exists.
3. **RNA-FM's single download endpoint** is a licensing non-issue but
   an operational one -- see the TODOs list in the final summary.
