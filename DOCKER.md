# Bij AI — Docker

Containerizes the whole combined pipeline described in
[README_INTEGRATION.md](README_INTEGRATION.md): Kim (FASTQ → VCF),
Bij AI (VCF → draft clinical report), and the `bridge/` glue that chains them.

**Why this exists:** every fresh Colab runtime this session required
manually reinstalling `bwa`/`samtools`/`bcftools`/`freebayes`/`tabix`
(via conda, which means accepting Anaconda's Terms of Service for the
`defaults` channel) and re-pinning `transformers` — real, recurring
friction. This image bakes all of that in once.

**This has not been built or run yet.** The machine that wrote this
Dockerfile has 8GB RAM and cannot build (would install multi-GB
dependencies) or run (would load real model weights) this image — see
["Verified vs. not yet verified"](#verified-vs-not-yet-verified) below
for exactly what was and wasn't checked, and what to do on Colab/a
bigger machine.

---

## Before you build: three pre-fetched wheels the repository does not contain

**A fresh clone cannot build this image.** The `Dockerfile` `COPY`s three
CPU-only torch wheels from the repository root, and those files are not in
git -- they are 180 MB of binary, and they are ignored (see `.gitignore`).
You must put them there yourself before the first build.

| file | bytes | sha256 |
|---|---|---|
| `torch-2.7.1+cpu-cp312-cp312-manylinux_2_28_x86_64.whl` | 175,833,687 | `8f8b3cfc53010a4b4a3c7ecb88c212e9decc4f5eeb6af75c3c803937d2d60947` |
| `torchvision-0.22.1+cpu-cp312-cp312-manylinux_2_28_x86_64.whl` | 2,016,876 | `b5fa7044bd82c6358e8229351c98070cf3a7bf4a6e89ea46352ae6c65745ef94` |
| `torchaudio-2.7.1+cpu-cp312-cp312-manylinux_2_28_x86_64.whl` | 1,796,388 | `65bf843345ae05629b7f71609bab0808004dabfce6cf48ea508a5d4f5419ca74` |

**What it looks like if you skip this.** The build fails at the `COPY`, and
the error names no prerequisite:

```
ERROR: failed to build: failed to solve: failed to compute cache key:
failed to calculate checksum of ref <id>::<id>:
"/torch-2.7.1+cpu-cp312-cp312-manylinux_2_28_x86_64.whl": not found
```

That reads as a broken Dockerfile rather than a missing input, and the
filename appears nowhere in git history to search for -- which is why this
section exists rather than only the comment at the `COPY` lines.

**How to get them.** They come from PyTorch's CPU index, not PyPI:

```bash
pip download --no-deps --only-binary=:all: \
    --python-version 3.12 --platform manylinux_2_28_x86_64 \
    --index-url https://download.pytorch.org/whl/cpu \
    torch==2.7.1 torchvision==0.22.1 torchaudio==2.7.1
```

or fetch them directly (resumable, which is why the original was done this
way -- these are large and the transfer has failed here before):

```bash
curl -L -C- -O https://download.pytorch.org/whl/cpu/torch-2.7.1%2Bcpu-cp312-cp312-manylinux_2_28_x86_64.whl
```

Then verify -- `sha256sum` against the table above. The three files currently
in the tree were checked against the upstream index on 2026-09-10 and match
byte for byte, so they are reproducible: **it does not matter who originally
downloaded them.**

**Why pre-fetched rather than `pip install` during the build.** The CPU
wheels are 175.8 MB against roughly 821 MB for the default CUDA bundle, and
`requirements.txt` pins `torch==2.7.1` without an index, so an in-build
`pip install` would pull the CUDA build from PyPI and inflate the image.
Installing the wheels first makes the pinned requirement already satisfied.
(CI enforces the same property from the other direction -- see the "Assert
the CUDA stack was not pulled in" step in `.github/workflows/pytest.yml`.)

**Do not add these to `.dockerignore`.** They are ignored by *git* and
required by the *build context*; those are opposite requirements on the same
files, and excluding them would break every build.

---

## Quick start

```bash
# Build (one time; ~10-20 min depending on network speed, mostly torch/
# tensorflow wheel downloads and the freebayes source build)
docker build -t geper:latest .

# Or, with docker-compose (same image, but wires up volumes/env vars for you):
docker compose build
```

```bash
# Run Bij AI standalone against an existing VCF
docker run --rm -it \
  -e GEPER_NCBI_EMAIL=you@example.com \
  -e GEPER_NCBI_API_KEY=your_key_here \
  -v "$(pwd)/data:/data" \
  -v geper_model_cache:/app/geper/model_cache \
  -v geper_hf_cache:/root/.cache/huggingface \
  geper:latest \
  python geper/main.py --vcf /data/sample.vcf --output-dir /data/out
```

Or, the same thing via docker-compose (drop the VCF into `./data/`
first):

```bash
docker compose run --rm geper \
  python geper/main.py --vcf /data/sample.vcf --output-dir /data/out
```

Results land in `./data/out/` on your host either way (bind mount) —
`geper_results.json`, `geper_report.md`, `geper_report_full.pdf` (the
detailed report — a draft until a qualified clinician reviews and signs
off, see `geper/review/signoff.py`), and `geper_report_short.pdf` (its
one-page-style summary companion).

---

## Base image choice (asked for explicitly — not picked silently)

`python:3.12-slim-bookworm` (Debian), **not** a bioconda/miniconda-based
image. Full reasoning is in the Dockerfile's own header comment; short
version:

- Bij AI's own `requirements.txt` forces Python 3.11 or 3.12 exactly
  (evo2's PyPI metadata). `python:3.12-slim` gives that by construction.
- `bwa`/`samtools`/`bcftools`/`tabix` are all plain Debian apt packages;
  only `freebayes` needs a source build (Debian/Ubuntu don't package a
  recent version reliably) — this Dockerfile reuses
  `kim_pipeline/install_dependencies.sh`'s own already-documented
  apt + source-build recipe for that, rather than inventing a new one.
- This avoids conda/Anaconda-ToS entirely, which is the actual friction
  this image exists to remove — even though
  `kim_pipeline/docs/INSTALL_DEPENDENCIES.md`'s own existing "Docker"
  section currently recommends `continuumio/miniconda3` + bioconda for
  this same tool set.

**Tradeoff, not hidden:** a bioconda-based image gets all five tools
from one already-curated, version-tested channel in far fewer
Dockerfile lines — less to get wrong, at the cost of the ToS friction
and a second version-pinning mechanism (conda envs) layered on top of
pip's. Given Python 3.11/3.12 is already a hard requirement either way,
that convenience doesn't buy much here.

## Multi-stage build

Yes, and it's a meaningful win, not forced for its own sake: the
`builder` stage needs `build-essential`/`cmake`/`git`/several `-dev`
header packages to compile `freebayes` from source; none of that is
needed to *run* it. The final `runtime` stage copies only the built
`freebayes` binary, the Python venv, and the pre-cloned HyenaDNA
source — no compiler toolchain in the shippable image at all.

---

## The transformers pin — a real, verified conflict

This task's instruction was to pin `transformers==4.56.2` as the
"confirmed known-good version" for `enformer-pytorch`/`borzoi-pytorch`.
That instruction turned out to be **more precisely true, and more
complicated, than the committed repo currently reflects.**

Checked live against real PyPI package metadata this session (not
assumed):

| Package | Version | Declared `transformers` requirement |
|---|---|---|
| `geper/requirements.txt` (committed) | — | `>=5.12.1,<6.0.0` |
| `geper/GEPER_Colab.ipynb` (committed) | — | `>=5.12.1,<6.0.0` |
| `enformer-pytorch` | 0.8.12 (latest) | `==4.56.2` **exactly** |
| `borzoi-pytorch` | 0.5.1 (latest) | `>=4.57.6,<5.0.0` |

**These three are mutually incompatible.** No single `transformers`
version satisfies all of them. Checking `borzoi-pytorch`'s full release
history (0.4.0 → 0.5.1) turned up a real gap: versions 0.4.0/0.4.1 had
an unbounded `>=4.34.1` floor (would have allowed 4.56.2), but 0.4.3
onward tightened to `<4.51.0`, and 0.5.0 onward requires `>=4.57.6` —
**4.56.2 falls in a gap no released `borzoi-pytorch` version actually
declares support for.**

**What this Dockerfile does about it:**
1. Installs `enformer-pytorch==0.8.12` normally — its own exact pin is
   exactly what's wanted.
2. Installs `borzoi-pytorch==0.5.1` with `--no-deps`, so pip's resolver
   never evaluates its (currently unsatisfiable-alongside-
   enformer-pytorch) `transformers>=4.57.6` floor. This is the same
   `--no-deps`-around-an-incompatible-upstream-pin technique
   `geper/requirements.txt` already documents and uses for MMSplice's
   `cyvcf2` pin — not a new pattern invented here.
3. Pins `transformers==4.56.2` explicitly as the *last* pip step, so it
   wins regardless of what anything above resolved to.
4. Runs `pip check` (non-fatal) as a build-log-visible sanity gate — it
   *will* flag the `borzoi-pytorch` mismatch (expected, see above); any
   *other* conflict listed there is new and worth investigating.

**Action needed from you, not resolved here:**
- `geper/requirements.txt` and `geper/GEPER_Colab.ipynb` should probably
  be updated to `transformers==4.56.2` (or a compatible range) to match
  reality — left untouched in this pass since it's a substantive,
  debatable change outside "containerize with Docker."
  `geper/pipeline/models/enformer_plugin.py`'s `all_tied_weights_keys`
  monkey-patch (in its `_load_impl`) is a no-op guard under
  transformers 4.x either way, so it doesn't conflict with this pin.
- **Borzoi specifically has not been verified working under
  transformers 4.56.2** (below its own declared floor) — this sandbox
  has never been able to reach `huggingface.co` to load real weights.
  Enformer is the half of this pair the task's own instruction states
  was already confirmed; Borzoi needs its own real check on Colab.

---

## What's baked into the image vs. what's still a first-run cost

| Item | Baked in? | Why |
|---|---|---|
| `bwa`, `samtools`, `bcftools`, `tabix`, `minimap2`, `ncbi-blast+` | ✅ apt, in the image | System binaries, small, no reason not to |
| `freebayes` | ✅ built from source, in the image | Not reliably packaged; source build is small |
| All Python deps (`geper/requirements.txt` + `kim_pipeline/requirements.txt`) | ✅ in the image (one shared venv) | See "One shared venv" below |
| `enformer-pytorch`, `borzoi-pytorch`, `mmsplice` (pip packages) | ✅ pre-installed in the image | Default-enabled plugins; see the transformers writeup above for why this needed care |
| HyenaDNA **source** (`geper/hyena-dna/`) | ✅ pre-cloned, in the image | Code only, ~50MB, not weights |
| `git`, `git-lfs`, `r-base-core` | ✅ apt, in the image | Genuine runtime deps (HyenaDNA checkpoint download is a real `git lfs clone`; SPiP shells out to `Rscript`) — found by grepping every `subprocess` call across `pipeline/models/`, `models/`, `database/`, not assumed |
| HyenaDNA/Enformer/Borzoi/SpliceBERT/SpliceFormer/SPiP **weights** (`geper/model_cache/hyenadna/`, `geper/model_cache/plugin_model_cache/` -- consolidated under `GEPER_CACHE_DIR`, G2 report review round 5) | ❌ volume, downloaded on first real use | Multi-GB (~3.1GB for the plugin weights alone, confirmed against a real local checkout); baking this in would multiply the image size for zero benefit over a persistent volume — see below |
| SPiP's 3 CRAN packages (`foreach`/`doParallel`/`randomForest`) | ❌ still auto-installs via `install.packages()` on first SPiP use | Would need R's own compiler toolchain in the image for `randomForest`'s compiled code; SPiP degrades gracefully without it, so this was judged not worth the extra image weight — revisit if SPiP becomes load-bearing |
| AlphaMissense's full catalogue (multi-GB, GCS-hosted) | ❌ downloaded on first use, or point `GEPER_ALPHAMISSENSE_HG38_LOCAL`/`_HG19_LOCAL` at a pre-downloaded file | Multi-GB, same reasoning as the weight caches above |
| Evo 2 | ❌ not installed at all | GPU-only, no practical CPU path (`models/evo2.py`'s own docstring) — needs a CUDA-matched `torch`/`flash-attn` build this general-purpose image doesn't attempt; a separate CUDA-base variant would be needed if Evo 2 support is wanted in a container |

### Why model weights are a volume, not baked into the image

They're large (Enformer alone is 1.9GB; `plugin_model_cache/` totals
~3.1GB; the AlphaMissense catalogue is multi-GB on its own) and
Bij AI's own loaders already handle "auto-download on first use,
idempotent, cached thereafter" — baking them into the image would (a)
make every `docker pull`/`docker build` multiple times larger for
every user, even ones who only need a subset of models, and (b) still
need updating whenever a checkpoint changes upstream, which a runtime
download already handles for free. A named Docker volume (see
`docker-compose.yml`) gets the same "download once, reuse forever"
property without either cost — you pay the download once, on first
real run, and never again across container restarts/recreations as
long as the volume isn't deleted.

**Named plainly (2026-09-11, human ruling, option C): this section
describes the DEFAULT image — the one `docker build` actually produces
from this repo's `Dockerfile` (confirmed: zero hits for
`model_cache_seed`, `COPY.*model`, or `ARG.*seed` in it). It needs live
network access on first real use, full stop.** That is a real
requirement gap against `docs/BIJ_AI_CAPABILITY_AUDIT.md`'s own
"standing on-premise packaging requirement" (a no-internet deployment
must ship the weights baked in) — this section's own reasoning for why
weights are a volume here does not resolve that gap, it explains why
*this* image chose not to close it. The next section describes the
*other*, offline-capable image (`geper:bridge-ready`) this design
tradeoff does not apply to — but that image has no build path in this
repository (see below): a 4.6 GB artefact that exists on one machine
only. Building a repo-buildable, weights-baked variant is tracked as an
open deliverable, not started by this note (see
PACKAGING-offline-sites-shipped-image-must-contain-model-weights-
huggingface-acquisition-is-not-an-install-path) — this is a
documentation correction, not authorization to build one.

### The offline model-cache seed (`model_cache_seed/`) -- where it lives and how to rebuild it

Everything above describes the *volume* path, which needs a live
network on first use. There is a second, offline path, and it depends
on a 4.6 GB artefact that exists in **no repository**. This section is
that artefact's only record.

**Where it lives, and why it is untracked.**

| | |
|---|---|
| Path | `C:\Users\kisla\GEPER\model_cache_seed\` (the primary working tree, not a worktree) |
| Size | 4,832,167,646 bytes (~4.6 GiB), 57 regular files + 5 symlinks |
| Transport copy | `C:\Users\kisla\GEPER\model_cache_seed.tar.gz`, 3,391,920,924 bytes (~3.2 GiB) |
| Tracked in git? | **No -- by decision, not by oversight.** Ruled: too large to commit, and *derived rather than authored*. |

That distinction is the point of writing this down: an untracked file
nobody decided to leave untracked looks identical on disk to one that
was ruled on. Both artefacts are therefore named in `.gitignore`, under
a comment quoting the ruling -- **that entry is what makes the decision
machine-readable; this section is where the reasoning lives.** Without
the entry they would sit in `git status` among ten scratch
`Dockerfile.*` variants, indistinguishable from clutter; without this
section the entry would record *that* they are ignored and nothing about
*why*, or what they are.

**Caveat, because "the ignore is in the repo" and "the ignore is
protecting the artefact" are two different claims:** the `.gitignore`
entry is only in force in a working tree checked out on a branch that
contains it. The seed physically lives in the primary working tree,
which is routinely on a different branch, and there `git status` still
lists both artefacts as untracked. **The entry takes effect in that tree
at merge, not when it was written.**

**The two artefacts are the same content, and the tarball is not a
stale copy.** Established by comparing the tarball's member list
(`tar -tvzf`, no extraction) against the tree: every path and every
member size matches, except that the tree has one file the tarball
does not --
`models--facebook--esm2_t33_650M_UR50D/refs/main` (40 bytes, created
after the tarball was written). Its content is the ESM2 commit sha
that `geper/models/esm2.py`'s `_ESM2_REVISION` already pins.
**That sentence used to end "so a restore from the tarball should be
functionally complete", reasoning that
`from_pretrained(..., revision=<sha>)` resolves `snapshots/<sha>`
directly rather than through `refs/main`. It was read from the pin and
never measured, and the rebuild below measured it: the image that
works resolves by DEFAULT with no `cache_dir` and no explicit
revision, and on that path `refs/main` is required -- its absence is a
CACHE MISS with every blob intact.** So the honest statement is the
opposite of the old one: **a restore from the tarball alone is NOT
complete, and `refs/main` must be written afterwards.** The claim is
left visible rather than quietly swapped, because the reasoning that
produced it is the kind that will look convincing again.
**A further caveat stated rather than glossed:
paths and sizes were compared, contents were not hashed** (that costs
two full 4.6 GB reads). A same-size, different-bytes file would not
have been caught.
The tarball exists because `Dockerfile.bridge-ready-tarball` copies a
single file into the build context instead of a 4.6 GB tree; it is a
transport form, not a backup generation.

**What it is for.** It seeds `geper:bridge-ready`
(`sha256:a8a5fe67749e38206cbd188487cc34b95bd7609cc99f3e234030c707c5f6af94`),
the image the offline end-to-end proof runs in, in which all four
models load with no network. The floor's measured figures for that
image: **~36 s to load all four from the seed, against ~47 minutes and
a live network without it.** That ratio is the reason this procedure
is written down rather than reconstructed later.

**Before rebuilding this image, read
`docs/BRIDGE_READY_REQUIREMENTS.md`** -- seven conditions a future
`bridge-ready` Dockerfile must satisfy, each with the evidence that it
bites and what a violating build looks like. Two of them are not about
symlinks and bite first, and one of them the shipped image violates.

**How the image actually consumes it** -- read from `docker history`,
not from the `Dockerfile.bridge-ready*` variants, **none of which built
this image**. The seed was copied into a running container and
`docker commit`-ed:

```
sh -c mkdir -p /app/model_cache_seed && cp -a /seed/. /app/model_cache_seed/ && ...verify...
ENV GEPER_CACHE_DIR=/app/model_cache_seed
ENV HF_HUB_CACHE=/app/model_cache_seed
ENV HF_HOME=/app/model_cache_seed
```

The three env vars are re-pointed at `/app/model_cache_seed` rather
than reusing the base image's `GEPER_CACHE_DIR=/app/geper/model_cache`
because that path is a `VOLUME`, **and `docker commit` does not
capture volume contents** -- a seed written there would vanish from the
committed image. Anyone restoring must preserve that, or the models
appear absent.

**A correction to this paragraph, left visible because the mistake is
instructive:** it previously said the seed was **`docker cp`**-ed into
the container. `docker history` shows the `cp -a /seed/.` that ran
*inside* the container; it does not show how `/seed` got there, and
`docker cp` was an inference filling that gap. It is the wrong one --
`docker cp` cannot carry these symlinks at all, which is exactly why
the *previous* image was the one tagged
`geper:bridge-ready-BROKEN-esm2-offline`. `/seed` was a **read-only
bind mount**, as set out below. **`docker history` records the command
a layer ran, never the `docker run` flags that produced it, so it
cannot answer "how did this file arrive" -- and the answer is the
entire difficulty here.**

**What a working cache has to look like -- stated as a REQUIRED END
STATE, not as a sequence of steps.** That framing is deliberate and is
Kelly's, who ran the repair and then checked the file mtimes rather
than trusting her own account of it: the two halves are **three days
apart** (the five symlinks at `2026-09-05 07:09 UTC`, `refs/main` at
`2026-09-08 04:51 UTC`), and each appears at the same instant in the
host seed and in the image. So this was never one atomic recipe --
anything that reads as a single tidy procedure, including the one-line
summary that started this section, is a reconstruction. **The end state
is what can be evidenced; the sequence is not, so only the end state is
written down as fact.**

The evidence is an A/B between two images that are both still on this
machine, which is stronger than any recollection: `3cf41395`
(`geper:bridge-ready-BROKEN-esm2-offline`) is the *before* state of
exactly this repair, and `a8a5fe67` (`geper:bridge-ready`) is the
*after*. Under
`/app/model_cache_seed/models--facebook--esm2_t33_650M_UR50D/`:

| | BROKEN `3cf41395` | FIXED `a8a5fe67` |
|---|---|---|
| `refs/` | **directory absent entirely** | `refs/main`, 40 bytes |
| `snapshots/08e4846e.../` | **exists and is empty** | 5 symlinks |
| `blobs/` | all 5, incl. the 2,609,506,392-byte safetensors | **identical, same sizes** |
| `.no_exist/08e4846e.../` | 3 zero-byte files | **identical** |

**The blobs are the same in both. 2.6 GB of correct weights sat in the
broken image the whole time.** That single row is why a directory
listing lies here, and it is the reason this section exists.

**1 -- `refs/main`.** `models--facebook--esm2_t33_650M_UR50D/refs/main`
containing `08e4846e537177426273712802403f7ba8261b6c` --
**40 bytes, no trailing newline** (read with `xxd`, not `cat`, because a
trailing newline is exactly what a document loses). The parent `refs/`
directory must be created; in the broken image it does not exist at
all. This is the sha `_ESM2_REVISION` pins in `geper/models/esm2.py`.

**2 -- five symlinks**, all in
`snapshots/08e4846e537177426273712802403f7ba8261b6c/` -- note the
snapshot directory name *is* the commit sha, the same string as
`refs/main`:

```
config.json             -> ../../blobs/a956a25d277f30bd870d3760b9a116f19ead885e
model.safetensors       -> ../../blobs/a08adabb949fa67ad3c14b509d04fd60368b35007b0095e3358f81200c4f4db0
special_tokens_map.json -> ../../blobs/ba0f9b53dbbf27934f7555e5d31e37bdea9317f1
tokenizer_config.json   -> ../../blobs/3f0d47e841e1cb75257aeaf76d156802899a217e
vocab.txt               -> ../../blobs/6b946952cc35537226f07fd70957ee2f848880d2
```

**The targets are RELATIVE (`../../blobs/...`), and that is what lets
the tree survive being moved to a different root at all. A rebuild that
writes absolute targets will pass on the machine that made it and fail
inside the image.** `model.safetensors`' target is a 64-hex sha256
while the other four are 40-hex git blob shas -- do not "normalise"
them. And a number worth having: `find model_cache_seed -type l` over
the whole 4.6 GB tree returns **exactly these five**. The entire
symlink hazard is concentrated in one directory.

**There is no third thing.** An earlier summary of this repair --
Kelly's own, and quoted here because it is the kind of line that
propagates -- said "two metadata writes + five symlinks". The A/B shows
**one** metadata file. `.no_exist/<revision>/`'s three zero-byte markers
(`added_tokens.json`, `chat_template.jinja`, `tokenizer.json`) are
**identical in the broken image**, and on the host tree their mtimes sit
in the original download window rather than the repair window. They are
HuggingFace's negative-cache markers, they arrived with the download,
and they are **not part of the repair**.

**The mechanisms that do not work.** Two of them lose the symlinks in
transit; two more defeat a cache that is otherwise correct.

**(A) `docker cp` -- and this is the one that hides.** It cannot encode
a POSIX symlink from a Windows host: it prints
`unknown file mode ?rw-rw-rw-`. **That line is the failure, not a
symptom of it.** It reads like a warning, the copy continues, the exit
status is fine -- and at the far end `refs/` and the snapshot contents
are simply gone while `blobs/` arrives whole. The result is the BROKEN
column above: complete-looking directory, matching file count, matching
`du`, no usable cache.

**(B) The build context** -- `COPY`, and equally BuildKit's
`RUN --mount=type=bind` with `cp -a`. The context loader rejects the
same links outright:
`ERROR: invalid file request model_cache_seed/models--facebook--esm2_t33_650M_UR50D/snapshots/08e4846e.../config.json`.
This one fails **loudly**, but it is the expensive failure, because it
is the *clean* route -- one derived layer, no `docker commit` -- and so
it is the route a careful person picks first.

**(C) `VOLUME` shadowing, which is not about symlinks at all and will
bite a rebuilder before either of the above.**
`docker inspect --format '{{json .Config.Volumes}}'` on **both** images
returns `/app/geper/geper_output`, `/app/geper/model_cache`, and
`/root/.cache/huggingface`. **The two obvious places to put a model
cache are both declared volumes, and `docker commit` does not capture
volume contents** -- so a seed written to either builds cleanly, commits
cleanly, and is **absent at run time**. That is why the working image
re-points all three variables away from both, measured off the image
rather than read off a Dockerfile:
`GEPER_CACHE_DIR=`, `HF_HUB_CACHE=` and `HF_HOME=`, all
`/app/model_cache_seed`.

**(D) `HF_HOME` used where `HF_HUB_CACHE` is meant.** The correct
variable for the model cache **root** is `HF_HUB_CACHE`. Setting only
`HF_HOME` makes HuggingFace look in `$HF_HOME/hub`, one level off --
the older image had `HF_HOME=/app/model_cache_seed/hf` and therefore
searched a directory that does not exist while the models sat at the
root. The symptom is, again, a full-looking directory and a miss. The
three variables above are **not** interchangeable.

**What does work for getting an existing seed into an image: a runtime
bind mount.** `docker run -v <seed>:/seed:ro` and then `cp -a /seed/.`
*inside* the container, so the copy is executed by Linux, which handles
the links natively; then `docker commit` to a path that is **not** a
`VOLUME`. This is the route that produced `a8a5fe67`, and it is
corroborated by that image's own `docker history`, which records the
`cp -a /seed/.` and the three `ENV` lines.

**The acceptance test -- and it deliberately carries no time
threshold.**

```
docker run --rm --network none -e PYTHONPATH=/app:/app/geper \
  -v <probe>:/probe.py:ro --entrypoint python <image-id> /probe.py
# probe: from geper.models.esm2 import ESM2Model; m = ESM2Model(); m.load()
#        then sum(p.numel() for p in m.model.parameters())
```

PASS is: **it loads at all under `--network none`, and reports
`651.0M` parameters.** The parameter count is in the probe on purpose --
a load that returned a randomly-initialised model would otherwise also
"succeed".

FAIL, run against `3cf41395` today, is the signature to recognise:

```
utils.exceptions.ModelLoadError: Failed to load model 'esm2': We couldn't connect to
'https://huggingface.co' to load the files, and couldn't find them in the cached files.
```

**Said with 2.6 GB of correct weights on disk in that image.** "Couldn't
find them in the cached files" is what a complete-looking `blobs/` says
when the pointers are missing.

Two deliberate omissions, both of which would make this test worse:

- **No seconds figure.** Measurements of this same load exist at 7.7 s,
  12.1 s and 27.9 s, and they cannot be reconciled. A threshold would
  fail on a busy machine and teach the next person to distrust a working
  cache.
- **The probe does not set `HF_HUB_OFFLINE`.** Telling the library not
  to try is a weaker test than letting it try and be unable to reach the
  network.

And one thing not to misread: Transformers prints a `pooler.dense`
re-initialisation notice **on a hit and on a miss alike**. It is not a
cache warning, and treating it as one will scare somebody off a passing
run.

**The rule this whole section reduces to, and the reason it is written
as an end state plus a load rather than as a recipe: A CACHE IS PROVEN
BY A LOAD, NEVER BY A DIRECTORY LISTING.** Every cheaper check --
listing, file count, `du`, even total byte size -- passes on the broken
image.

**Which artefact to restore from, because the two are not
interchangeable in the way their names suggest.** `model_cache_seed/`
is the one to use. The tarball preserves the five symlinks correctly --
they appear as `lrwxrwxrwx ... -> ../../blobs/...` in `tar -tvzf`, so
transporting the seed through it does not destroy them -- but it was
written before the `refs/main` repair and **does not contain that
file**. Restoring from the tarball alone therefore reproduces the
broken state above unless `refs/main` is written afterwards. Re-read on
2026-09-09: the tree still holds it (40 bytes, correct sha) and the
tarball still does not (`tar -tzf | grep -c refs/main` -> 0).

**Still UNKNOWN, with the method that would settle each** -- stated
because a confident procedure that has never run is discovered wrong
only after the seed is already lost:

- **The command that created the five symlinks on Windows.** Searched
  the tracked tree for anything carrying `refs/main` or the sha: only
  this file and `geper/models/esm2.py` match, and neither records a
  command. One constraint narrows it -- host tree and image carry the
  links at the same instant, so one was copied from the other
  preserving mtimes (`cp -a`-shaped) rather than created twice -- but
  **which side was authored is not on disk.** Nothing in the repository
  will settle this; it needs that session's shell history, or it stays
  unknown.
- **Whether `.no_exist` is required.** It was present in every state
  tested, so no passing run speaks to its necessity. Settled by deleting
  it from a copy of the seed and re-running the acceptance test.
- **The with-network failure mode.** The broken cache has been shown to
  fail *loudly* under `--network none`. The genuinely dangerous case --
  a network **is** available and the miss is absorbed by a silent
  re-download rather than an error -- **has not been measured and is not
  asserted here.** Settled by running the same probe on `3cf41395`
  *without* `--network none` and watching for download progress and a
  wall-clock in the tens of minutes.
- **Whether any of this reproduces the seed from nothing.** It does not
  claim to: everything above restores or repairs an *existing* seed.
  Cold-cache acquisition is the separate question below.

**How to regenerate it if lost.** No single script populates this
cache -- searched the tracked file list, not assumed; acquisition is
spread across each loader and the per-source `*/bootstrap.py` modules --
and **how this tree was
actually accumulated is not recorded anywhere, so it is UNKNOWN.** What
*is* established is the layout a regeneration has to reproduce, which is
read out of `geper/config.py` and each loader rather than reconstructed
from memory. Every destination below is relative to `$GEPER_CACHE_DIR`;
each loader writes its own subpath when that variable points at a
directory lacking it.

| Subpath | Source | Pinned by | Routed there by |
|---|---|---|---|
| `models--facebook--esm2_t33_650M_UR50D/`, `.locks/` | HF hub, `facebook/esm2_t33_650M_UR50D` | `_ESM2_REVISION` = `08e4846e537177426273712802403f7ba8261b6c` | `esm2.py` passes `cache_dir=CONFIG.CACHE_DIR` to `from_pretrained` |
| `torch/hub/checkpoints/RNA-FM_pretrained.pth` (1.19 GB) | HF mirror `_HF_MIRROR_REPO_ID` first, then the upstream `proj.cse.cuhk.edu.hk` endpoint | unpinned (no revision in either path) | `config.py` derives `TORCH_HOME=$GEPER_CACHE_DIR/torch` when `GEPER_CACHE_DIR` is set and `TORCH_HOME` is not |
| `hyenadna/hyenadna-medium-450k-seqlen/` (316 MB, incl. `.git/lfs`) | `git lfs clone https://huggingface.co/LongSafari/{model_name}` | `_HYENADNA_CHECKPOINT_REVISIONS[model_name]`; the loader **refuses an unpinned checkpoint** | `CONFIG.models.HYENADNA_CHECKPOINT_DIR` = `$GEPER_CACHE_DIR/hyenadna`, `HYENADNA_MODEL_NAME` = `hyenadna-medium-450k-seqlen` |
| `alphamissense/AlphaMissense_hg38.tsv.gz` (+`.tbi`, +`.provenance.json`) | GCS -- URL and `gcs-etag-md5` recorded in the sidecar `.provenance.json`, which is the authoritative source record | etag/md5 in the sidecar | `CACHE_SUBDIR` (`GEPER_ALPHAMISSENSE_CACHE_SUBDIR`), default `alphamissense` |
| `ensembl/ensembl_transcripts.jsonl` (+`.provenance.json`) | derived from `ftp.ensembl.org/.../Homo_sapiens.GRCh38.116.gtf.gz`; sha256 in the sidecar | Ensembl release 116 | `pipeline/ensembl/bootstrap.py` -> `$GEPER_CACHE_DIR/ensembl` |
| `plugin_model_cache/spliceformer/` (4.7 MB) | SpliceFormer plugin weights | UNKNOWN | `PLUGIN_CACHE_DIR` = `$GEPER_CACHE_DIR/plugin_model_cache` |
| `blast_cache.sqlite` (12 KB) | not a model -- a runtime BLAST cache, created empty | n/a | `database/blast_client.py`, `$GEPER_CACHE_DIR/blast_cache.sqlite` |

**MMSplice is deliberately absent from this table and from the seed.**
Its five `.h5` weights ship *inside the pip package*, in the image's
site-packages, not in the cache -- `pipeline/models/mmsplice/loader.py`
resolves them by file path without importing the package. A restored
seed does not carry MMSplice; the image does.

**What is UNKNOWN here, with the method that would settle it** (rather
than a confident procedure that has never run -- this one would be
discovered wrong only after the seed is already lost):

- **The exact invocation** that populates a cold cache in one pass.
  Settled by running the pipeline once against an empty
  `GEPER_CACHE_DIR` on a live network and diffing the resulting tree
  against the manifest above.
- **Whether regeneration is byte-reproducible.** The AlphaMissense and
  Ensembl artefacts have content hashes in their sidecars and should
  be; ESM2 and HyenaDNA are revision-pinned; **RNA-FM's checkpoint is
  pinned by nothing at all**, and its two sources are a HF mirror and a
  single academic host. Settled by hashing a fresh fetch against
  `RNA-FM_pretrained.pth`.
- **SpliceFormer's source URL and pin.** Settled by reading the
  SpliceFormer plugin's own download path.

### One shared venv, not the `--kim-python`/`--geper-python` split

`README_INTEGRATION.md` documents the bridge as supporting two
*separate* Python environments because Bij AI's heavier PyTorch/AI stack
was assumed to conflict with Kim's lighter toolchain. Checked by hand
against both `requirements.txt` files (not assumed): they don't
actually conflict today. Every package they share (`pydantic`,
`reportlab`, `biopython`, `pyyaml`, `requests`) has overlapping version
ranges, and Kim's own `torch`/`transformers`/`numpy` stack is an
*optional* `[ai]` extra (`kim_pipeline/pyproject.toml`) that's commented
out of `kim_pipeline/requirements.txt` and never installed here — which
lines up with how this image actually uses Kim anyway:
`bridge/combined_pipeline.py` always runs Kim with `mode="vcf_only"`,
so Kim's own AI/annotation/reporting stages (the ones that would want
`[ai]`) are structurally never reached through this image.

If `kim_pipeline`'s `[ai]` extra is ever added to its base
requirements, or Kim starts running standalone in full mode from this
image, revisit this — the bridge's `--kim-python`/`--geper-python`
flags already support a two-venv split with zero other code changes.

---

## Running each of the three workflows

All three are already documented in `README_INTEGRATION.md`; here's the
Docker form of each.

### 1. Bij AI standalone (VCF already in hand)

```bash
docker run --rm -it \
  -e GEPER_NCBI_EMAIL=you@example.com -e GEPER_NCBI_API_KEY=your_key \
  -v "$(pwd)/data:/data" \
  -v geper_model_cache:/app/geper/model_cache \
  -v geper_hf_cache:/root/.cache/huggingface \
  geper:latest \
  python geper/main.py --vcf /data/sample.vcf --output-dir /data/out
```

### 2. Kim standalone (FASTQ → VCF only)

```bash
docker run --rm -it \
  -v "$(pwd)/data:/data" \
  geper:latest \
  python kim_pipeline/main.py analyze \
    --r1 /data/R1.fastq.gz --r2 /data/R2.fastq.gz --ref /data/GRCh38.fasta \
    --output-dir /data/work --sample-id sample01 --mode vcf_only
```

### 3. Combined (FASTQ → draft report for clinician review, one command)

```bash
docker run --rm -it \
  -e GEPER_NCBI_EMAIL=you@example.com -e GEPER_NCBI_API_KEY=your_key \
  -v "$(pwd)/data:/data" \
  -v geper_model_cache:/app/geper/model_cache \
  -v geper_hf_cache:/root/.cache/huggingface \
  geper:latest \
  python bridge/run_combined.py \
    --r1 /data/R1.fastq.gz --r2 /data/R2.fastq.gz --ref /data/GRCh38.fasta \
    --sample-id sample01 \
    --kim-output-dir /data/work/kim --geper-output-dir /data/work/geper \
    --blast-mode auto --species human --assembly GRCh38
```

`--kim-python`/`--geper-python` are omitted deliberately — both default
to whichever interpreter is running the bridge, which is correct here
since this image uses one shared venv (see above).

With docker-compose, prefix any of the three `python ...` commands
above with `docker compose run --rm geper` instead of the full `docker
run -e ... -v ...` invocation — the volumes and env vars are already
wired up in `docker-compose.yml`.

---

## Environment variables

### Required for real use

| Variable | What it's for |
|---|---|
| `GEPER_NCBI_EMAIL` | Real, monitored contact email for NCBI E-utilities (ClinVar/dbSNP). The committed default is a placeholder NCBI's usage policy explicitly warns against (`geper/utils/ncbi_eutils.py`'s own startup warning) — set this before any real run. |
| `GEPER_NCBI_API_KEY` | NCBI API key (raises E-utilities rate limits). Optional but strongly recommended for anything beyond a handful of variants. |

### Optional

| Variable | What it's for |
|---|---|
| `GEPER_BLAST_DATABASE` | Path to a pre-built local BLAST database (mount it in, point this at it) instead of falling back to remote NCBI BLAST or Bij AI auto-building one from `GEPER_BLAST_REFERENCE_FASTA`. |
| `GEPER_ALPHAMISSENSE_HG38_LOCAL` / `GEPER_ALPHAMISSENSE_HG19_LOCAL` | Path to a pre-downloaded AlphaMissense catalogue file, to skip the multi-GB GCS download on first use. |
| `GEPER_ASSEMBLY`, `GEPER_ENABLE_*`, every other `GEPER_*` variable `geper/config.py` defines | Passed through normally — this image doesn't restrict or override any of them beyond the `transformers` pin above. |

Put these in a `.env` file next to `docker-compose.yml` (Compose loads
it automatically) rather than typing them on the command line every
time:

```bash
# .env  (never commit this — see .gitignore's existing .env* exclusion)
GEPER_NCBI_EMAIL=you@example.com
GEPER_NCBI_API_KEY=your_key_here
```

---

## Getting data in and out

- **Input** (VCF, or FASTQ + reference for Kim/the combined workflow):
  drop it in `./data/` on the host; it's bind-mounted to `/data` in the
  container (both the plain `docker run` examples and
  `docker-compose.yml` do this).
- **Output**: `geper/main.py --output-dir` / `--geper-output-dir`
  pointed at a path under `/data` lands directly back in `./data/` on
  the host — no `docker cp` needed. `docker-compose.yml` additionally
  bind-mounts `./output` straight to `/app/geper/geper_output` (Bij AI's
  own default output directory) for the case where you don't pass
  `--output-dir` explicitly.
- **Model weight caches**: named Docker volumes (`geper_model_cache`,
  `geper_hf_cache`) — persist across `docker compose down`/container
  recreation; deleted only by `docker volume rm` or `docker compose
  down -v`. `geper_model_cache` alone covers the bootstrap datasets,
  HyenaDNA's checkpoint, the plugin weight cache, and RNA-FM's torch
  cache (consolidated under `GEPER_CACHE_DIR`, set in the Dockerfile --
  see its own comment on that `ENV` line; G2, report review round 5).
  Previously three separate volumes (`geper_model_cache`,
  `geper_checkpoints`, `geper_plugin_model_cache`) that had to be kept
  in sync by hand across every `docker run`/compose invocation.

---

## Verified vs. not yet verified

This machine has 8GB RAM and cannot build or run this image — building
it installs multi-GB PyTorch/TensorFlow/model-package dependencies, and
running it loads real model weights. Both are exactly the kind of
operation this session's own constraints rule out locally (confirmed:
Docker Desktop is installed on this machine but its daemon isn't
running, and starting it would consume RAM this session was told to
avoid — deliberately not started).

**Verified locally, and how:**
- Dockerfile structure/instruction syntax — careful manual authoring and
  review, plus a small custom Python script (no Dockerfile linter like
  `hadolint` was available in this environment) that joins
  backslash-continued lines the way Docker's parser does, confirms every
  instruction keyword is valid, every `COPY --from=<stage>` references a
  stage that's actually defined, and quotes are balanced per logical
  instruction — this is what caught a real issue in an earlier pass (a
  multi-line `VOLUME [...]` JSON array using backslash continuation,
  which is NOT valid the way it was first written; fixed by splitting
  into one `VOLUME` instruction per path instead).
- `docker-compose.yml` YAML syntax — parses cleanly with Python's
  `yaml.safe_load()`.
- The `transformers`/`enformer-pytorch`/`borzoi-pytorch` version
  conflict — checked against **live, real PyPI package metadata**
  (`pypi.org/pypi/<package>/json`) fetched during this session, not
  assumed. See the dedicated section above.
- The freebayes build system and its exact system-package requirements
  — checked against the **live freebayes/freebayes repo** (its
  `meson.build`, README, and actually-exercised
  `.github/workflows/ci_test.yml`), not assumed or guessed from a
  plausible-looking `cmake` invocation. Every apt package name in that
  CI recipe was then individually re-confirmed present on Debian
  bookworm (this image's base — freebayes' own CI runs on Ubuntu) via
  `packages.debian.org`, since a package existing on Ubuntu doesn't
  guarantee the same name exists on Debian.
- The freebayes `v1.3.10` tag pin — fetched `src/Parameters.cpp` and
  `meson.build` directly from GitHub at that exact tag (not master/HEAD)
  and confirmed by text search that the specific line the real compile
  error pointed at (`"default: 0.01"` next to a missing `<<`) is absent
  from that version's source entirely, and that its `meson.build`
  declares the same `version: '1.3.10'` the build log's own "Project
  version" line already showed.
- The `test/` sparse-checkout exclusion — re-fetched `v1.3.10`'s
  `meson.build` specifically checking for `subdir('test')` calls (none)
  and confirmed every `test()` definition's `workdir`/args are only
  consulted at `meson test`/`ninja test` run time, not `meson setup`
  configure time — before concluding it's safe to never check that
  directory out at all.
- Every system-binary dependency this Dockerfile installs — found by
  grepping every `subprocess` call to an external binary across
  `pipeline/models/`, `models/`, and `database/` in the actual Bij AI
  source, not assumed from documentation alone (this is how the
  `git`/`git-lfs`/`r-base-core` requirements were found — none of them
  were in this task's original required-tool list).
- `geper/requirements.txt` vs. `kim_pipeline/requirements.txt` for
  direct version conflicts — read both files in full and compared every
  overlapping package by hand.
- Directory sizes (`geper/plugin_model_cache/` ≈ 3.1GB,
  `geper/hyena-dna/` ≈ 54MB, `geper/model_cache/` ≈ 43MB) — measured
  against this session's own real local checkout, not estimated.

**NOT verified — needs a real build+run on Colab or another machine
with more resources:**
1. **The build itself succeeds at all** — in particular the apt package
   names flagged inline in the Dockerfile (`libncurses5-dev` in the
   builder stage; `libssl3`/`libncurses6`/etc. in the runtime stage)
   were not checked against a live `apt-get` on `python:3.12-slim-bookworm`
   specifically.
2. **`pip check`'s actual output** — expected to show exactly one
   conflict (`borzoi-pytorch` vs. the pinned `transformers`); confirm
   nothing else shows up.
3. **Enformer loads and produces real output** under the final
   environment this Dockerfile builds (the task states this was already
   confirmed in a prior Colab session, under `transformers==4.56.2` —
   this Dockerfile is designed to reproduce that same pin, but the
   *image* itself has not been used to confirm it).
4. **Borzoi loads and produces real, correct output** under
   `transformers==4.56.2` specifically — this is the one genuinely
   open question this pass surfaced (see the conflict writeup above);
   it has never been confirmed working below its own declared
   `>=4.57.6` floor.
5. **The freebayes source build succeeds** and the resulting binary
   actually runs against the runtime stage's shared libraries. Updated
   three times this pass:
   - The build system itself was initially wrong (assumed a plain
     `make` build; freebayes actually uses Meson + Ninja, confirmed
     against the live repo, and fixed to match).
   - That got past `meson setup` but hit a genuine C++ syntax bug in
     freebayes' own source when built from the unpinned default
     branch's HEAD (`Parameters.cpp:178`, a missing `<<` before `endl`
     in a usage-string). Checked directly against GitHub: that exact
     broken line does not exist at the `v1.3.10` tag — it was added to
     the default branch sometime after that release. The clone is now
     pinned to `--branch v1.3.10` (also confirmed that tag's own
     `meson.build` declares the identical `version: '1.3.10'` the
     build log already showed, and does use Meson), which avoids the
     bug by construction and, as a side effect, fixes the
     reproducibility gap that let an unpinned clone break silently
     like this in the first place.
   - Even pinned to `v1.3.10`, a plain checkout still failed --
     `error: invalid path 'test/splice/1:883884-887618.bam'` -- because
     freebayes' own test fixtures name BAM files after genomic
     coordinates (`chrom:start-end`), and that colon apparently isn't
     writable through whatever filesystem layer handled the checkout on
     the machine that hit this (reported from a Windows/Docker Desktop
     rebuild). Confirmed directly against `v1.3.10`'s own `meson.build`
     that `test/` is never needed to build the binary (its five
     `test()` definitions are only evaluated at `meson test`/`ninja
     test` run time, never invoked here, and there's no `subdir('test')`
     call needing it at configure time), so the fix sidesteps the
     problem rather than root-causing the exact filesystem behavior:
     a non-cone sparse checkout (`clone --no-checkout` + a
     `/*` / `!/test/` pattern + `git checkout v1.3.10`) that never
     writes `test/` to disk at all, followed by an explicit
     `git submodule update --init --recursive` (moved off the initial
     clone, which is meaningless combined with `--no-checkout`) to
     still populate `contrib/`'s vendored dependency sources.
   - See the Dockerfile's own comments at the freebayes build step for
     the full writeup of all three, including the exact CI workflow the
     package list/build invocation was copied from and the
     Debian-bookworm package-name re-verification done for it.
   - Still not build-tested end to end: the `libcurl4`/`libssl3`/etc.
     runtime-stage list is a best-effort match to what the builder
     stage's `-dev` packages would provide at *build* time, not
     independently confirmed against the compiled binary's actual
     `ldd` output, and no compile has actually completed against
     `v1.3.10` yet (this pass only confirmed the buggy line's absence,
     the `meson.build`/version match, and that `test/` isn't needed at
     configure time) — should now get further than either prior
     attempt, not guaranteed to complete cleanly.
6. **HyenaDNA's checkpoint download** via the pre-installed `git-lfs`
   actually works end-to-end inside the container.
7. **SPiP's `Rscript`/CRAN-package auto-install** still works from
   inside the container (network access to CRAN from within Docker,
   `install.packages()` succeeding under whatever R version
   `r-base-core` resolves to on bookworm).
8. **The combined bridge workflow** (`bridge/run_combined.py`) end to
   end, inside the container, against real FASTQ input (e.g.
   `test_data/`) — confirms the one-shared-venv decision and the
   `WORKDIR`/relative-path assumptions both hold up in practice, not
   just on paper.
9. **Final image size** — not measured (couldn't build it); the
   multi-stage split and the volume-not-baked-in decision for weights
   are both aimed at keeping it reasonable, but the actual number is
   unknown until it's built.
