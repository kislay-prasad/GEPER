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
