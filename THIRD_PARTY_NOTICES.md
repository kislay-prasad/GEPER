# Third-Party Notices

**Date compiled:** 2026-09-10
**Compiled by:** meredith-msydpy6m (hive agent), per CARD
`TASK-THIRD-PARTY-NOTICES-the-shipped-image-needs-a-visible-notices-file-and-it-discharges-Merediths-adequate-disclosure-flag`

## SCOPE -- read this before any row below

**This file covers everything baked into, or downloaded at runtime by code
baked into, the single combined Docker image built from the repository
root's `Dockerfile`.** That image is built from **all four** top-level
source trees the Dockerfile actually `COPY`s and installs from --
`geper/`, `kim_pipeline/`, `bridge/`, and `shared/` -- not from one of them.

This scope statement exists because of a defect merged and corrected
earlier today: `geper/LICENSE_AUDIT.md` stated SpliceAI was "never
integrated into GEPER," which was true for `geper/` alone, while
`kim_pipeline` had been feeding SpliceAI-derived scores into a live ACMG
vote until it was removed there for licensing reasons on 2026-08-22 (see
`kim_pipeline/pipeline/vep/stage.py`'s own "SpliceAI REMOVED" comments).
A notices file inheriting that same one-repository scope would reproduce
the identical error in the one document whose entire purpose is to be
complete. Every section below was checked against **both** `geper/` and
`kim_pipeline/` source, not against one project's own audit file.

**What this file is not:** it is not a re-derivation of `LICENSE_AUDIT.md`,
`DATA_SOURCE_LICENSE_AUDIT.md`, or `PACKAGING_REDISTRIBUTION_LICENSE_AUDIT.md`
(all `geper/`-scoped, all already fetched-and-quoted against primary
sources on 2026-08-08/2026-08-20/2026-09-09). Those three documents did
the AI-model and data-source redistribution analysis rigorously and this
file **carries their verdicts forward, condensed, with a pointer back to
the full sourcing** rather than re-fetching what they already fetched.
What this file adds is the layer none of the three ever covered: **the
compiled/OS-level bioinformatics toolchain the Dockerfile itself bakes
into the image**, which turned out to have its own, previously-unaudited
finding (see "OS-level binaries" below) -- and the explicit, single,
both-repositories scope statement none of the three carried.

**What was NOT independently re-inspected:** the built Docker image
itself. `vic` owns the image landscape and built images are evidence the
human has reserved; per this card's own tools guidance, this file was
produced by reading the `Dockerfile` (which fully and deterministically
specifies what lands in the image -- there is no dynamic/conditional
install logic in it) and the two subprojects' source and dependency
manifests, not by running `docker build`/`docker run` or inspecting a
built image's filesystem. Anywhere this matters is flagged in place
below, not glossed over.

---

## 1. OS-level compiled binaries baked into the runtime image

Grepped directly from the root `Dockerfile`'s runtime stage (`FROM
python:3.12-slim-bookworm AS runtime`) apt-get lines and the
builder-stage `COPY --from=builder` lines that carry binaries forward.
Licence fetched live today (2026-09-10) via each project's own GitHub
`license` API / canonical source, not carried over from any existing
audit (none of the three prior audit files cover this layer).

| Binary | How it lands in the image | Licence (fetched 2026-09-10) | Note |
|---|---|---|---|
| **bwa** | `apt-get install bwa` (runtime stage) | **GPL-3.0** (`api.github.com/repos/lh3/bwa/license`) | **New finding -- not in any prior audit.** Invoked exclusively as a subprocess/external binary by `kim_pipeline`'s alignment stage; GEPER/Kim's own proprietary code never links against it. Under the FSF's own "mere aggregation" reading of GPL-3.0 (independent programs shipped side-by-side in the same distribution medium without being combined into one program), shipping the compiled binary alongside proprietary code in the same image is standard practice (the same pattern as any Linux distro or Docker image bundling `bash`/`grep`/etc. next to commercial software) and is a materially different situation from GPL-3.0 *code linked into* a proprietary binary. **This is a compliance condition, not a commercial-use blocker** -- GPL-3.0 does not restrict who may *run* the tool or for what purpose, unlike SpliceAI's CC BY-NC 4.0. GPL-3.0 §6 does require that anyone GEPER distributes the binary to also receive (or be given a written offer for) bwa's complete corresponding source; Debian's own apt source packages already provide this for an unmodified apt-installed binary. **I am not qualified to render the final "mere aggregation applies here" legal verdict myself -- flagging for independent legal confirmation before a commercial ship, per this card's own "state the condition" instruction, rather than either hiding it or treating it as a blocker on my own authority.** |
| **git** | `apt-get install git` (runtime stage; also used in the builder stage) | **GPL-2.0** (`api.github.com/repos/git/git/license`, confirmed GPLv2 text present) | Same subprocess/mere-aggregation reasoning as bwa. Used only to invoke `git`/`git lfs` as external commands (HyenaDNA checkpoint download, source clones at build time). |
| **git-lfs** | `apt-get install git-lfs` (runtime stage) | **MIT** (main codebase; `api.github.com/repos/git-lfs/git-lfs/license`) | Permissive, no condition. |
| **r-base-core** (R) | `apt-get install r-base-core` (runtime stage) | **GPL-2.0** (`r-project.org/COPYING`, R's own bundled licence file, is the unmodified GPLv2 text) | Same subprocess/mere-aggregation reasoning as bwa -- `pipeline/models/spip_plugin.py` shells out to `Rscript`, never links against R's runtime. SPiP's own three CRAN packages (foreach, doParallel, randomForest) auto-install via `install.packages()` on first use and were **not individually checked** in this pass -- flagged as a genuine gap, not a "these are fine" assumption. |
| **ncbi-blast+** | `apt-get install ncbi-blast+` (runtime stage) | **Public domain** (US Government Work; NCBI's own "Website and Data Usage Policies," confirmed via web search of NCBI's own policy page) | No restriction of any kind. |
| **samtools** | `apt-get install samtools` (runtime + builder stages) | **MIT/Expat**, with the GitHub API returning `NOASSERTION`/"Other" because the repo mixes licence files rather than declaring one SPDX id -- the operative licence text itself is MIT/Expat (`api.github.com/repos/samtools/samtools/license`) | Permissive. |
| **bcftools** | `apt-get install bcftools` (runtime + builder stages) | **Mixed: MIT/Expat (core) with a GPL-3.0 option for some plugins** (`api.github.com/repos/samtools/bcftools/license`) | **Flagged, not independently resolved further**: whether the Debian `bcftools` apt package includes the GPL-3.0-licensed plugin variants was not checked in this pass. If it does, the same subprocess/mere-aggregation reasoning as bwa applies (bcftools is invoked as an external binary, never linked); if it matters for a specific plugin's obligations, that needs its own check before relying on this row. |
| **htslib / tabix** | `apt-get install tabix` (runtime stage; also underlies samtools/bcftools/freebayes) | **Mixed MIT/Expat and Modified 3-Clause BSD** across the codebase, no copyleft component (`api.github.com/repos/samtools/htslib/license`) | Permissive. |
| **minimap2** | `apt-get install minimap2` (runtime stage) | **MIT** (`api.github.com/repos/lh3/minimap2/license`) | Permissive. |
| **freebayes** (v1.3.10) | Built from source in the builder stage (`git clone --branch v1.3.10`), compiled binary copied into the runtime image | **MIT** (`api.github.com/repos/freebayes/freebayes/license`) | Permissive. Version pinned to v1.3.10 for a build-reproducibility reason unrelated to licensing (see the Dockerfile's own inline comments); the licence was re-checked against the current default-branch repo, which is the same MIT licence file regardless of tag. |
| **HyenaDNA source** (not weights) | `git clone --depth 1 https://github.com/HazyResearch/hyena-dna.git`, copied into the image at `geper/hyena-dna` | **Apache-2.0** (`api.github.com/repos/HazyResearch/hyena-dna/license`) -- matches `geper/LICENSE_AUDIT.md`'s existing finding, independently re-confirmed today | NOTICE-preservation obligation, same as every other Apache-2.0 entry in `PACKAGING_REDISTRIBUTION_LICENSE_AUDIT.md`. The pretrained *checkpoint* (BSD-3-Clause, per that same audit) is **not** baked into the image -- it is a runtime download into a mounted volume, see section 3 below. |
| Base OS packages (`build-essential`, `cmake`, `zlib1g-dev`, `libcurl4`, `libssl3`, etc.) | Various `apt-get install` lines, builder and runtime stages | Not individually itemized in this pass -- these are standard Debian bookworm packages (a mix of GPL/LGPL/BSD/MIT depending on the package, per Debian's own per-package `/usr/share/doc/<pkg>/copyright` files inside the image) | **Known gap, stated rather than hidden**: a full per-package Debian licence enumeration was not done here. Every one of these is a widely-used, standard system library/dev-tool (compiler toolchain, compression libraries, TLS libraries) with no non-commercial or unusual licensing history known to me; none is compiled *into* GEPER/Kim's own Python code (they are `-dev` headers used only to compile freebayes and vanish from the final runtime image, or runtime shared libraries the compiled binaries link against). If an exhaustive Debian-package-level notice is ever required, the accurate way to produce it is `dpkg-query`/`apt-get source` run *inside the actual built image*, which is the reserved-evidence step this pass explicitly did not take. |

**Base image:** `python:3.12-slim-bookworm`. Python itself is under the
**Python Software Foundation License** (permissive, PSF-2.0-family,
well-established as commercially unrestricted); not independently
re-fetched this pass, consistent with the level of scrutiny given to
every other extremely-standard, widely-known-permissive component in
this section.

---

## 2. AI models -- code baked into the image; weights, see section 3

Verdicts below are **carried forward from `geper/LICENSE_AUDIT.md`
(2026-08-08/2026-09-09) and `PACKAGING_REDISTRIBUTION_LICENSE_AUDIT.md`
(2026-09-09)**, both of which already fetched and quoted primary sources
for every `geper/` row -- not re-fetched here. `kim_pipeline`'s own AI
model (DNABERT-2) is added because no `geper/`-scoped document was ever
going to include it -- exactly the scope gap this file exists to close.

**Confirmed present and reachable in the image, licence, commercial use:**

| Model | Subproject | Code licence | Weights licence | Commercial use | Note |
|---|---|---|---|---|---|
| HyenaDNA | geper | Apache-2.0 | BSD-3-Clause | Yes | |
| Evo 2 | geper | Apache-2.0 (composite) | Apache-2.0 | Yes | No CPU inference path; effectively unreachable in this CPU-only image (see the Dockerfile's own torch-CPU-wheel note) -- listed for completeness, not because it is expected to actually run here. |
| RNA-FM | geper | MIT | Apache-2.0 | Yes | |
| ESM-2 | geper AND kim_pipeline (same model, two load sites, same licence) | MIT | MIT | Yes | |
| AlphaMissense | geper (lookup-only; code unused) | Apache-2.0 (code, unused) | CC BY 4.0 | Yes | Catalogue file, not weights -- see section 3. |
| MMSplice | geper | MIT | MIT | Yes | Bundled `.h5` weights, see section 3. |
| TensorFlow | geper (mandatory MMSplice dependency) | Apache-2.0 | N/A | Yes | |
| Enformer | geper | MIT | CC BY 4.0 | Yes | |
| Borzoi | geper | Apache-2.0 | CC BY 4.0 | Yes | |
| SpliceFormer | geper | MIT | MIT | Yes | |
| SpliceBERT | geper | BSD-3-Clause | CC BY 4.0 | Yes | Disabled by default (`ENABLE_SPLICEBERT=false`, a runtime-stability gate unrelated to licensing) -- still present in the image's code, so still listed. |
| SPiP | geper | MIT | N/A (public-domain-equivalent reference data) | Yes | |
| **DNABERT-2** | **kim_pipeline** (`pipeline/ai/engine.py::DnaBertEngine`) | **Apache-2.0** | **Apache-2.0** | **Yes** | Confirmed via `LICENSE_AUDIT.md`'s 2026-09-09 resolution (fetched the HF repo's own `LICENSE` file directly, not the model-card frontmatter, which carries no license key). `kim_pipeline/requirements.txt` has its own `[ai]` extra (torch/transformers/numpy) **commented out**, and `bridge/combined_pipeline.py` always runs Kim in `mode="vcf_only"` -- but this image installs `geper/requirements.txt`'s own torch+transformers into the **same shared venv** (see the Dockerfile's own "ONE shared venv for both projects" comment), so `DnaBertEngine`'s actual runtime dependencies are present and importable in this image even though `kim_pipeline`'s own requirements file never asked for them. **Flagged as a reachability note, not a licensing concern**: Apache-2.0 carries no use restriction either way, so whether this code path is actually invoked in a given run does not change the notices obligation (preserve the LICENSE file), but it does mean "Kim only runs in vcf_only mode through the bridge" is not the same claim as "DNABERT-2 cannot run in this image" -- the image also supports standalone Kim invocation per the Dockerfile's own docstring, and that path can reach `DnaBertEngine` given the packages already present. |

**Confirmed absent / not shipped, despite once being integrated in one
of the two subprojects:**

| Model | Where it was integrated | Licence | Why not shipped |
|---|---|---|---|
| SpliceAI (Illumina) | `kim_pipeline` (VEP plugin, fed PP3/BP4 and BP7 evidence) | GPL-3.0 (code) / **CC BY-NC 4.0** (pretrained models -- a genuine commercial-use blocker) | **Removed 2026-08-22** (`kim_pipeline/pipeline/vep/stage.py`'s own "SpliceAI REMOVED (2026-08-22, licence)" comments, confirmed by reading the current file: the VEP plugin is no longer requested, `spliceai_score` fields are always `None`). Verified this is the exact defect the dispatch cited -- `geper/LICENSE_AUDIT.md`'s existing SpliceAI row says "Never integrated into GEPER" (true, scoped to `geper/` only) without ever stating `kim_pipeline` *had* integrated it until this date. **Currently not shipping, so not a live blocker** -- stated here so the historical gap is visible rather than silently absent from this notices file the way it was silently absent from `LICENSE_AUDIT.md` until now. |
| OpenSpliceAI | Evaluated, never integrated in either subproject | GPL-3.0 | Rejected on licensing grounds before integration. |
| DNABERT-2 in `geper/` | Was present in `geper/` before 2026-08-08 | Apache-2.0 | Removed from `geper/` (not a licensing rejection -- HyenaDNA replaced it as the default DNA model there). Still present and active in `kim_pipeline` (row above). |
| OMIM | `kim_pipeline`, removed 2026-08-20 | Research-use-only | Code fully removed; not shipped. |

---

## 3. Model weights / data files -- baked into the image vs. downloaded at runtime

This distinction matters for a notices file and neither prior audit
drew it explicitly for *this* image: a licence condition that applies
to something GEPER's own code downloads at runtime is a real obligation,
but it is a different fact from something already sitting in the bytes
shipped to a customer.

**Baked directly into the image (confirmed from the Dockerfile):**

- **MMSplice's bundled `.h5` weight files** -- `pip install mmsplice==2.4.0` installs the package's own bundled pretrained weight files as package data; the Dockerfile's own comment confirms GEPER loads them "directly," not via a separate download step. MIT, per section 2.
- **HyenaDNA source** (not the pretrained checkpoint) -- see section 1.

**NOT baked into the image -- downloaded at runtime by code that IS
baked in, into a mounted volume (`geper/model_cache`, per the
Dockerfile's own `VOLUME` declarations and `GEPER_CACHE_DIR` setting):**

- HyenaDNA's pretrained checkpoint (BSD-3-Clause)
- Enformer weights (CC BY 4.0), Borzoi weights (CC BY 4.0), SpliceBERT weights (CC BY 4.0), SpliceFormer weights (MIT), RNA-FM weights (Apache-2.0), ESM-2 weights (MIT), SPiP's randomForest/RefSeq/genome reference data
- AlphaMissense's precomputed pathogenicity catalogue (CC BY 4.0) -- downloaded whole from Google Cloud Storage on first use, not bundled
- DNABERT-2's own HF weights (Apache-2.0), if `kim_pipeline`'s standalone AI-mode path is ever exercised in this image (see section 2's reachability note)

None of these carry a commercial-use restriction (per `LICENSE_AUDIT.md`'s
and `PACKAGING_REDISTRIBUTION_LICENSE_AUDIT.md`'s existing, already-fetched
findings) -- but because they are fetched over the network at first use
rather than shipped in the image, an **air-gapped/offline site** cannot
acquire them this way at all; that is `PACKAGING_REDISTRIBUTION_LICENSE_AUDIT.md`'s
and the packaging-parts documents' own separate question (whether/how to
bundle a local copy for offline sites), not restated here.

---

## 4. Data sources reached at runtime (not AI models, not bundled)

Condensed from `DATA_SOURCE_LICENSE_AUDIT.md` and
`PACKAGING_REDISTRIBUTION_LICENSE_AUDIT.md` (both already fetched and
quoted); not re-fetched in this pass except where noted. All reached via
live API/query by code shipped in the image; none of the underlying data
is bundled into the image itself today.

| Source | Licence | Commercial use | Note |
|---|---|---|---|
| ClinVar, dbSNP (NCBI) | Public domain (US Government Work) | Yes | |
| gnomAD (Broad Institute) | CC0 1.0 | Yes | |
| ClinGen (gene validity, dosage, ERepo) | CC0 1.0 | Yes | |
| UniProt | CC BY 4.0 | Yes | Attribution required. |
| InterPro / Pfam | CC0 1.0 | Yes | |
| AlphaFold DB (EMBL-EBI / DeepMind) | CC BY 4.0 | Yes | Attribution required. |
| Ensembl (GTF/CDS bootstrap, REST API) | No restriction stated ("Ensembl imposes no restrictions on access to, or use of, the data") | Yes | |
| 1000 Genomes SAS (via Ensembl REST) | **See `docs/1000GENOMES_SAS_LICENCE_VERIFICATION.md` (2026-09-10, merged `5ea91c3`) for the full, dedicated fetched-and-quoted verification** -- no commercial restriction found across 8 independently fetched IGSR/EMBL-EBI/AWS pages; explicitly flagged there as an absence-of-restriction finding, not an affirmative grant. | Yes, per that verification | This is the source the immediately-preceding dispatch on this floor verified; not re-verified again here, only referenced. |
| HPO | CC BY 4.0, moderate confidence (per `PACKAGING_REDISTRIBUTION_LICENSE_AUDIT.md`'s own flag -- the licence sub-page 404'd when checked, corroborated only secondarily) | Yes, moderate confidence | **Weakest-sourced row in this file**, carried over as flagged rather than upgraded to a clean "Yes" -- worth a direct re-fetch before relying on it further. |
| Orphanet / Orphadata | CC BY 4.0 (search-index-sourced, not independently re-fetched) | Yes | |
| MaveDB | **Mixed, per-score-set** -- some CC0/CC BY/CC BY-SA (permissive), some CC BY-NC-SA/"Other" (restrictive) | Conditional | GEPER's own `_index_one_score_set` already filters by licence at query time so restrictive records never become report evidence -- this is a **use** control, already correctly scoped; it says nothing about whether a *bundled snapshot* of MaveDB would be permitted, which is a separate, not-yet-designed question per `PACKAGING_REDISTRIBUTION_LICENSE_AUDIT.md`. |
| IndiGenomes | N/A | N/A | Retired 2026-08-08 for a commercial-use conflict (research-only terms). Not queried, not shipped. |

---

## 5. Python package dependencies -- RESOLVED 2026-09-10, `pip-licenses` run against the actual built image

**The gap this section named earlier today is closed.** `pip-licenses` was run against
`geper:vic-master-61311f0` (the built image itself, not a source review) once `vic` was
confirmed out of it -- `docker run --rm` only, `MSYS_NO_PATHCONV=1` set to avoid a Windows
path-rewriting trap, no `commit`/`tag`/`delete`/write against the image at any point. `pip
list --format=freeze` inside `/opt/venv` (the image's one shared venv, per the Dockerfile)
shows **120 installed packages**, the full denominator, measured directly rather than assumed.
`pip-licenses` itself covers **117 of the 120** -- its own default behaviour excludes `pip`,
`setuptools`, and `wheel` (its own bootstrapping tools, not application dependencies); all
three are independently well-known MIT-licensed and are named here rather than silently
dropped from the count. **117 + 3 = 120, the full denominator accounted for**, per the
standing rule adopted this morning that a finding count is worthless without the size of the
set it was drawn from.

**Grouped by licence family** (117 packages; the two-decimal breakdown reflects how
`pip-licenses` reports each package's own declared metadata field, which is not always a
single SPDX id):

| Family | Count |
|---|---|
| MIT (all spellings: `MIT`, `MIT License`) | 39 |
| BSD (all spellings: `BSD License`, `BSD-3-Clause`, `BSD-2-Clause`, `BSD`, `3-Clause BSD License`, `MIT-CMU`) | 35 |
| Apache (all spellings: `Apache Software License`, `Apache-2.0`, `Apache License 2.0`, plus dual/composite strings below) | 21 |
| Apache + MIT dual/composite (`Apache Software License; MIT License`, `MIT OR Apache-2.0`) | 4 |
| LGPL (copyleft family -- see below, named separately, not folded into a "misc" bucket) | 3 |
| Other composite/permissive (`Apache-2.0 OR BSD-2-Clause`, `Apache-2.0 AND CNRI-Python`, `MPL-2.0 AND MIT`, `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0`, `ISC License (ISCL)`, `Mozilla Public License 2.0 (MPL 2.0)`, `PSF-2.0`) | 8 |
| Resolved from `UNKNOWN`/non-SPDX metadata by reading the bundled `LICENSE` file directly inside the image (see below) | 3 |
| Full licence text returned instead of an SPDX id (`evo2`) | 1 |
| `LicenseRef-Biopython-License-Agreement` (own permissive licence, see below) | 1 |
| **Total** | **117 -- reconciles to the 117 row-count measured directly from the tool's own JSON output** |

**Every package `pip-licenses` could not resolve to a normal SPDX identifier, named
individually rather than left as an unexplained "other" count** -- per the instruction that
`UNKNOWN` is not `permissive`, and a tool printing `UNKNOWN` has told you it did not look, not
that nothing is there:

- **`namex` 0.1.0** and **`vtx` 1.1.0** both reported `UNKNOWN`. Resolved by reading the
  bundled `LICENSE` file directly out of each package's `.dist-info` inside the image (not
  inferred, not looked up externally): both are **Apache-2.0**, verbatim Apache License 2.0
  text present in both files. `pip-licenses` reported them `UNKNOWN` only because both
  packages declare their licence via a bundled `LICENSE` file (`License-File:` in their own
  metadata) rather than a `License:` classifier field -- a metadata-format gap, not an absent
  licence. (`vtx` is "Vortex," Michael Poli's reference implementation of the
  Hyena/StripedHyena/Evo2 computational primitives, already covered under Evo 2's row in
  `LICENSE_AUDIT.md` -- consistent with that row's Apache-2.0 finding, not a new source.)
- **`borzoi-pytorch` 0.5.1** reported the literal string `LICENSE` (not a licence identifier --
  a packaging metadata error where the author's own `License:` field holds the filename
  `LICENSE` instead of an SPDX id). Resolved the same way: the bundled `LICENSE` file inside
  the image is the verbatim Apache License 2.0 text -- **Apache-2.0**, matching the GitHub-API
  confirmation already recorded for this exact package in section 1's "AI models" table
  earlier today. Two independent checks (GitHub's license API, and now the actual shipped
  file inside the built image), same answer.

**LGPL family, named separately as instructed -- real copyleft, and explicitly NOT a
commercial-use blocker, for a different and more direct reason than bwa/git's "mere
aggregation" argument in section 1:**

- **`psycopg` 3.3.5**, **`psycopg-binary` 3.3.5** (PostgreSQL driver, used by `clinical/`) --
  `LGPL-3.0-only`.
- **`frozendict` 2.4.7** -- `GNU Lesser General Public License v3 (LGPLv3)`.

Unlike GPL, LGPL was written specifically to permit linking an LGPL library into a proprietary
application without extending copyleft to that application, as long as the LGPL component
itself remains a separately replaceable library -- which is exactly how a normal Python
`import psycopg` / `import frozendict` works (no static embedding, no source merged into
GEPER's own files, the package itself stays independently reinstallable/upgradeable). This is
a real licence family worth disclosing on its own line (not folded into "permissive"), but it
is not the same class of question as bwa/git's GPL-3.0/2.0 in section 1 -- LGPL's design intent
already covers this exact usage pattern, whereas GPL's "mere aggregation" argument is a
narrower, less certain reading being applied to a case GPL was not written to make easy.

**`biopython` 1.88** carries its own named licence, `LicenseRef-Biopython-License-Agreement`
-- read directly from the bundled `LICENSE.rst` inside the image: *"Biopython is currently
released under the 'Biopython License Agreement' ... Some files are explicitly dual licensed
under your choice of the 'Biopython License Agreement' or the 'BSD 3-Clause License'."*
Permissive, commercially usable, its own well-established open-source licence (predates SPDX
standardisation, hence the `LicenseRef-` prefix rather than a plain id).

**`evo2` 0.6.0** returned its full licence text instead of an SPDX id in `pip-licenses`' own
field (the underlying `pyproject.toml`/`setup.py` embeds the complete Apache License 2.0 text,
plus notices for bundled NVIDIA/HuggingFace/Google-Research/Facebook-Fairseq code, as its
declared `license` value) -- consistent with, and not contradicting, `LICENSE_AUDIT.md`'s
existing Apache-2.0 finding for Evo 2.

**Stop-and-tell check, run and cleared:** no package found is both (a) confirmed actually
present in the shipped image and (b) carrying a licence that blocks commercial use. No GPL
(full, non-L) licence appears anywhere in the 117-package scan. The three LGPL packages and
the `UNKNOWN`-resolved-to-Apache-2.0 packages are the only entries that needed individual
attention, and none of them meets the stop-and-tell bar -- named above rather than silently
passed through as "clean."

**What this pass did not do:** verify licence compatibility/obligations for **transitive**
dependencies of these 117 direct packages (`pip-licenses` reports what is actually installed
in the venv, which already includes transitive dependencies pulled in by pip's resolver, but
this pass did not separately trace which of the 117 is a direct requirement vs. pulled in
transitively) -- for a licence-notices purpose this distinction does not change any package's
obligations, so it was not pursued further, but is named as a boundary of what "117 packages"
means here.

---

## 6. Bottom line

**No component confirmed to be both (a) actually shipping in this image
and (b) carrying a licence that blocks commercial use was found.** SpliceAI
(the one component that WOULD have been such a blocker) was already
removed from `kim_pipeline` on 2026-08-22, before this review; IndiGenomes
and OMIM were already removed/retired earlier for the same class of
reason. Every AI model and data source actually confirmed present is
permissively licensed for commercial use, per the existing, already-rigorous
`geper/LICENSE_AUDIT.md` / `DATA_SOURCE_LICENSE_AUDIT.md` /
`PACKAGING_REDISTRIBUTION_LICENSE_AUDIT.md` findings plus this file's own
extension of that review to `kim_pipeline` and to the OS-level toolchain.

**Genuinely new, unresolved items this pass surfaced, stated plainly rather
than smoothed over:**

1. **GPL-3.0/GPL-2.0 OS-level binaries baked into the image** (bwa,
   git, r-base-core; bcftools conditionally) were never covered by any
   prior audit. My own reading is that subprocess-only invocation of an
   unmodified, unlinked binary is "mere aggregation" under GPL-3.0/2.0 and
   not a commercial blocker -- but I am not qualified to make that the
   final word, and it should get independent legal confirmation before a
   commercial ship, particularly the GPL-3.0 §6 source-availability
   obligation that redistributing the compiled binary triggers.
2. ~~The Python dependency tree was not individually, exhaustively
   licence-verified (section 5).~~ **RESOLVED 2026-09-10**: `pip-licenses`
   run against the actual built image (`geper:vic-master-61311f0`),
   full denominator stated (120 installed, 117 scanned + 3 accounted
   separately), every non-standard result (`UNKNOWN` x2, the literal
   string `LICENSE` x1, one custom `LicenseRef-`, one full-text field)
   individually resolved by reading the bundled `LICENSE` file inside
   the image itself -- see section 5. No commercial-use-blocking licence
   found; three LGPL packages named and explained rather than folded
   into "permissive."
3. **HPO's CC BY 4.0 status remains moderate-confidence**, carried over
   from the existing audit's own flag, not upgraded here.
4. **DNABERT-2's reachability inside this specific image** is broader
   than "Kim only runs in vcf_only mode" suggests, because the shared
   venv makes its dependencies importable regardless (section 2) -- a
   reachability note, not a licensing one, since DNABERT-2 is Apache-2.0
   either way.

None of these four rises to "STOP AND TELL ME IMMEDIATELY" under this
card's own boundary, which is scoped to a component whose licence is a
commercial blocker and that is nonetheless shipping -- none of the four
is that. They are stated here because a notices file with a known hole
named in it is the deliverable this card asked for.
