# syntax=docker/dockerfile:1.7
# =============================================================================
# GEPER — combined image: Kim (FASTQ -> VCF) + GEPER (VCF -> Clinical Report)
# + the bridge/ glue code that chains them. See README_INTEGRATION.md for the
# three-project layout this mirrors exactly (geper/, kim_pipeline/, bridge/).
#
# WHY THIS EXISTS: every fresh Colab runtime this session required manually
# reinstalling bwa/samtools/bcftools/freebayes/tabix (via conda, which means
# accepting Anaconda's Terms of Service for the `defaults` channel) and
# re-pinning `transformers` -- real, recurring friction, confirmed multiple
# times. This image bakes all of that in once, so it is a non-issue on any
# target: Colab, a future server, or a hospital's own infrastructure.
#
# NOT YET VERIFIED BY BUILDING/RUNNING (see this repo's DOCKER.md "Verified
# vs. not yet verified" section for the full list) -- this machine has 8GB
# RAM and cannot build or run this image (it needs to install and load real
# multi-GB dependencies). Only static checks were possible here: Dockerfile
# structure/syntax review, and the requirements-conflict analysis documented
# inline below (verified against real, live PyPI package metadata fetched
# during this session -- not assumed). The actual `docker build`/`docker run`
# needs to happen on Colab or another machine with more resources.
# =============================================================================
#
# --- Base image choice (asked for explicitly; not picked silently) ---------
# python:3.12-slim-bookworm (Debian), NOT a bioconda/miniconda-based image.
#
# Reasoning:
#   - geper/requirements.txt forces Python 3.11 or 3.12 EXACTLY (its own
#     comment: forced by evo2's PyPI metadata, requires-python
#     ">=3.11,<3.13"). python:3.12-slim gives that version by construction,
#     with no conda environment/channel-priority bookkeeping layered on top
#     of it.
#   - bwa, samtools, bcftools, and tabix are all in Debian's own apt repos
#     (no bioconda needed for those four); only freebayes is unreliably
#     packaged on Debian/Ubuntu and is built from source below -- the exact
#     apt + source-build path kim_pipeline/install_dependencies.sh's own
#     `install_via_apt()` already uses and documents (see
#     kim_pipeline/docs/INSTALL_DEPENDENCIES.md), just baked into a
#     Dockerfile instead of run by hand.
#   - This sidesteps conda/Anaconda-ToS entirely -- the exact recurring
#     friction this image exists to remove. Note that
#     kim_pipeline/docs/INSTALL_DEPENDENCIES.md's OWN existing "Docker"
#     section currently recommends `continuumio/miniconda3` + bioconda for
#     exactly this tool set; this Dockerfile deliberately does NOT follow
#     that recipe, for the ToS reason above.
#
# Tradeoff acknowledged, not hidden: a bioconda-based image would get all
# five tools from one already-curated, version-tested channel in far fewer
# Dockerfile lines (no freebayes source build, no manual runtime-library
# bookkeeping in the final stage below) -- less to get wrong, at the cost of
# the ToS friction and a second version-pinning mechanism (conda envs) on
# top of pip's. Given Python 3.11/3.12 is already a hard requirement either
# way, that convenience doesn't buy this project much, so the apt + pip path
# wins here.
# =============================================================================


# =============================================================================
# Stage 1/2 -- builder: compiles freebayes from source and installs the full
# Python environment into a venv, so the final runtime image never needs
# gcc/cmake/dev headers/git at build-tool weight at all.
# =============================================================================
FROM python:3.12-slim-bookworm AS builder

# Build-time system packages: kept 1:1 with
# kim_pipeline/install_dependencies.sh's own `install_via_apt()` list
# (already documented/tested for building freebayes on Debian/Ubuntu) --
# see that script for the source of truth this line is copied from.
#
# FLAGGED, NOT SILENTLY ASSUMED: that script was written/tested against
# Ubuntu; `libncurses5-dev` in particular has been renamed on some newer
# Debian releases (Debian 12 "bookworm", what this image's base tag
# resolves to, may prefer `libncurses-dev`). This apt-get was not run
# against a live daemon during this pass (see the file-level note above) --
# if it fails, this line is the first thing to check. See DOCKER.md
# "Known open questions."
RUN apt-get -o Acquire::Retries=15 -o Acquire::http::Pipeline-Depth=0 -o Acquire::Queue-Mode=access -o Acquire::ForceIPv4=true update && apt-get -o Acquire::Retries=15 -o Acquire::http::Pipeline-Depth=0 -o Acquire::Queue-Mode=access -o Acquire::ForceIPv4=true install -y --no-install-recommends \
        build-essential cmake git \
        zlib1g-dev libbz2-dev liblzma-dev \
        libcurl4-openssl-dev libssl-dev libncurses5-dev pkg-config \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# --- freebayes, built from source -------------------------------------------
# Not reliably packaged as a recent version on Debian/Ubuntu apt (see
# kim_pipeline/docs/INSTALL_DEPENDENCIES.md).
#
# CORRECTED: a prior version of this Dockerfile assumed a plain top-level
# `make` build and failed ("No targets specified and no makefile found").
# freebayes has since switched its build system to Meson + Ninja -- there
# is no Makefile OR CMakeLists.txt at the repo root at all. Confirmed
# directly against the live freebayes/freebayes repo (master branch) this
# session: meson.build is the only build definition present, its own
# `executable('freebayes', ...)` target is what produces the binary, and
# it contains no CMake usage anywhere -- so `cmake -S . -B build` (one
# candidate fix considered) would have been just as wrong as the `make`
# it was meant to replace. `cmake` itself stays in the apt package line
# above regardless -- it was already there (inherited from
# kim_pipeline/install_dependencies.sh's own list) and removing it is
# out of scope for this fix -- but nothing in this Dockerfile actually
# uses it: every pip package installed below is a prebuilt wheel (no
# source compilation), and freebayes itself needs meson/ninja, not
# cmake. Flagged rather than silently left implying otherwise.
#
# Both the exact apt package list below and the meson/ninja invocation
# are copied verbatim from freebayes' own actually-exercised CI workflow
# (.github/workflows/ci_test.yml on freebayes/freebayes, fetched this
# session), not guessed -- that repo's CI runs on Ubuntu, so every one of
# these package names was individually re-confirmed present on Debian
# bookworm (this image's base) via packages.debian.org before being
# copied here, rather than assumed to carry over.
#
# `-Dprefer_system_deps=false` makes meson build freebayes' own vendored
# copies of vcflib/fastahack/smithwaterman from the git submodules under
# ./contrib/ (already fetched by --recursive above) instead of linking
# the system libs the apt-get line below also installs -- kept exactly
# as CI has it (a combination actually proven to work) rather than
# switched to `true` now that the system libs are present, since nothing
# confirms that combination also works. `--buildtype release`, not CI's
# own `--buildtype debug`, since this is a production image, not a CI
# test run -- matches what freebayes' own README recommends for a
# non-development build. `meson test` (CI's own correctness-verification
# step) is deliberately skipped here -- it validates freebayes itself,
# which is out of scope for building this image.
RUN apt-get -o Acquire::Retries=15 -o Acquire::http::Pipeline-Depth=0 -o Acquire::Queue-Mode=access -o Acquire::ForceIPv4=true update && apt-get -o Acquire::Retries=15 -o Acquire::http::Pipeline-Depth=0 -o Acquire::Queue-Mode=access -o Acquire::ForceIPv4=true install -y --no-install-recommends \
        samtools bc parallel \
        libvcflib-tools libvcflib-dev \
        libseqlib2 libseqlib-dev \
        libfastahack-dev fastahack \
        smithwaterman \
        libwfa2-dev libsimde-dev \
        meson ninja-build \
    && rm -rf /var/lib/apt/lists/*

# CORRECTED again: cloning the default branch's unpinned HEAD (as this
# Dockerfile did until now) got past `meson setup` but failed the actual
# compile with a genuine C++ syntax bug in freebayes' OWN source --
#     src/Parameters.cpp:178:46: error: expected ';' before 'endl'
#        178 |    << "                   default: 0.01"  endl
# (a missing `<<` before `endl` in a chained stream-output usage string)
# -- not anything this Dockerfile introduced.
#
# Checked directly against GitHub, not assumed: the exact text
# `"default: 0.01"` does not appear anywhere in src/Parameters.cpp at
# tag v1.3.10 (the release the build log's own "Project version: 1.3.10"
# message refers to -- confirmed that tag's meson.build declares the
# identical version string, and does use Meson, not an older
# Makefile-based build) -- that broken line was added to the default
# branch sometime AFTER v1.3.10 was tagged, as part of later,
# unreleased/untagged development, and was never part of any released
# version. Pinning to v1.3.10 therefore avoids this bug by construction
# (the buggy code doesn't exist at this commit), not by patching around
# it -- no sed/source-patch fallback is needed. Pinning also directly
# fixes the reproducibility gap an unpinned clone had: this build no
# longer silently breaks again if freebayes' default branch changes
# under us, which is exactly how this specific bug was hit in the first
# place.
#
# CORRECTED a third time: even pinned to v1.3.10, a plain `git clone
# --branch v1.3.10` failed at the CHECKOUT step (not the clone/fetch
# itself) with:
#     error: invalid path 'test/splice/1:883884-887618.bam'
#     fatal: unable to checkout working tree
# freebayes' own test fixtures name BAM files after genomic coordinates
# (chrom:start-end), which is a legal filename on Linux but not
# writable through whatever filesystem layer handled this checkout on
# the reporting machine (Windows/Docker Desktop) -- either way, this
# repo's test/ directory is genuinely not needed to build the
# `freebayes` binary: confirmed directly against v1.3.10's own
# meson.build that none of its five test() definitions are evaluated
# by `meson setup` (they're only read at `meson test`/`ninja test`
# run time, which this Dockerfile never invokes), and there is no
# `subdir('test')` call that would need that directory to exist at
# configure time either. So rather than depend on figuring out exactly
# why that one file's checkout failed on some (but evidently not all)
# platforms, this sidesteps the problem by never checking test/ out at
# all -- a sparse, non-cone checkout that includes everything except
# it: clone with --no-checkout (fetches all objects, writes nothing),
# set the sparse-checkout pattern, then check out v1.3.10 for real --
# `error: invalid path` cannot recur for a path that's never written.
# `--recursive` moved off the initial clone (meaningless combined with
# --no-checkout -- there's no working tree yet for submodules to
# populate into) to an explicit `git submodule update --init
# --recursive` after the sparse checkout, so contrib/'s vendored
# vcflib-min/fastahack/smithwaterman sources (needed by
# -Dprefer_system_deps=false below) are still fully populated.
# `advice.detachedHead=false` only silences git's own informational
# notice that checking out a tag leaves HEAD detached (expected and
# harmless here -- this Dockerfile never commits anything in this
# checkout) -- purely cosmetic build-log cleanup, not a fix for
# anything that was actually failing.
RUN git config --global advice.detachedHead false \
    && git clone --branch v1.3.10 --no-checkout https://github.com/freebayes/freebayes.git /tmp/freebayes \
    && cd /tmp/freebayes \
    && git sparse-checkout init --no-cone \
    && printf '/*\n!/test/\n' > .git/info/sparse-checkout \
    && git checkout v1.3.10 \
    && git submodule update --init --recursive \
    && meson setup build/ -Dprefer_system_deps=false --buildtype release \
    && ninja -C build/ -v \
    && cp build/freebayes /usr/local/bin/freebayes \
    && cd / && rm -rf /tmp/freebayes

# --- Python environment: ONE shared venv for both projects -------------------
# README_INTEGRATION.md documents bridge/run_combined.py's
# `--kim-python`/`--geper-python` flags as supporting two SEPARATE Python
# environments, because GEPER's own heavier PyTorch/AI stack was assumed to
# conflict with Kim's lighter toolchain. Verified by hand against both
# requirements files (not assumed) that this is not actually necessary
# today: every package the two share (pydantic, reportlab, biopython,
# pyyaml, requests) has overlapping version ranges, and kim_pipeline's own
# torch/transformers/numpy stack is an OPTIONAL `[ai]` extra
# (kim_pipeline/pyproject.toml) that is commented out of kim_pipeline's own
# requirements.txt and is never installed here. This is also consistent
# with how this image actually uses Kim: bridge/combined_pipeline.py always
# runs Kim with `mode="vcf_only"`, so Kim's own AI/annotation/reporting
# stages (the ones that would want the `[ai]` extra) are never invoked
# through this image at all -- see README_INTEGRATION.md.
#
# If kim_pipeline's `[ai]` extra is ever added to its own requirements.txt,
# or Kim starts being run standalone in full mode from this same image,
# revisit this and split into two venvs -- `--kim-python`/`--geper-python`
# already support that split with zero other code changes needed.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
RUN pip install --no-cache-dir --upgrade pip

# Copy only the requirements files first so this (expensive) layer is
# cached across rebuilds that touch pipeline code but not dependencies.
COPY geper/requirements.txt /tmp/geper-requirements.txt
COPY kim_pipeline/requirements.txt /tmp/kim-requirements.txt

# Pre-fetched CPU-only torch wheel (175.8 MB vs 821 MB CUDA bundle).
# Host downloaded with curl -L -C- (resumable) to bypass network transfer issues.
# Verify byte count: exactly 175,833,687 bytes before building.
# CAVEAT: resulting image is CPU-only; would need torch reinstall to use GPU host.
COPY torch-2.7.1+cpu-cp312-cp312-manylinux_2_28_x86_64.whl /tmp/torch-2.7.1+cpu-cp312-cp312-manylinux_2_28_x86_64.whl
COPY torchvision-0.22.1+cpu-cp312-cp312-manylinux_2_28_x86_64.whl /tmp/torchvision-0.22.1+cpu-cp312-cp312-manylinux_2_28_x86_64.whl
COPY torchaudio-2.7.1+cpu-cp312-cp312-manylinux_2_28_x86_64.whl /tmp/torchaudio-2.7.1+cpu-cp312-cp312-manylinux_2_28_x86_64.whl

RUN pip install --no-cache-dir /tmp/torch-2.7.1+cpu-cp312-cp312-manylinux_2_28_x86_64.whl /tmp/torchvision-0.22.1+cpu-cp312-cp312-manylinux_2_28_x86_64.whl /tmp/torchaudio-2.7.1+cpu-cp312-cp312-manylinux_2_28_x86_64.whl && \
    pip install --no-cache-dir --retries 10 --timeout 3600 --resume-retries 50 -r /tmp/geper-requirements.txt \
    && pip install --no-cache-dir --retries 10 --timeout 3600 --resume-retries 50 -r /tmp/kim-requirements.txt

# -----------------------------------------------------------------------
# Enformer / Borzoi (CONFIG.splicing.ENABLE_ENFORMER / ENABLE_BORZOI both
# default true -- geper/config.py) -- pre-installed here (rather than left
# to GEPER's own runtime `ensure_pip_package_available()` auto-install,
# which needs PyPI network access the first time either model is used)
# because doing so is exactly what surfaces the transformers conflict
# below -- better to hit it once here, at build time, than mid-run.
#
# ==== VERIFIED transformers version conflict (live PyPI metadata check,
# this session -- pypi.org/pypi/enformer-pytorch/json and
# pypi.org/pypi/borzoi-pytorch/json) ====
#   - enformer-pytorch==0.8.12 (latest) requires_dist:
#       "transformers[torch]==4.56.2"   <- EXACT pin, no range
#   - borzoi-pytorch==0.5.1   (latest) requires_dist:
#       "transformers<5.0.0,>=4.57.6"   <- floor is ABOVE 4.56.2
#   - geper/requirements.txt (and geper/GEPER_Colab.ipynb) both currently
#     pin "transformers>=5.12.1,<6.0.0" -- satisfies NEITHER of the above.
#
# These three constraints are mutually exclusive: no single transformers
# version satisfies enformer-pytorch's exact ==4.56.2 pin AND
# borzoi-pytorch's >=4.57.6 floor at the same time. This fully explains
# (and confirms, against live upstream data rather than just this
# session's Colab experience) this task's instruction that 4.56.2 is the
# known-good version for enformer-pytorch specifically. It also reveals a
# genuine, currently-unresolved gap this pass found: no released
# borzoi-pytorch version's declared metadata actually covers 4.56.2 --
# checked its full release history (0.4.0 through 0.5.1): 0.4.0/0.4.1 had
# an unbounded ">=4.34.1" floor (would have allowed 4.56.2), but 0.4.3
# onward tightened to "<4.51.0" and 0.5.0 onward requires ">=4.57.6" --
# 4.56.2 falls in an undeclared gap no release actually claims to support.
#
# RESOLUTION: install enformer-pytorch normally (its own exact pin is
# exactly what we want); install borzoi-pytorch with --no-deps so pip's
# resolver never evaluates its (currently unsatisfiable-alongside-
# enformer-pytorch) transformers floor, then force transformers to
# exactly 4.56.2 as the final, authoritative step below -- same
# `--no-deps`-around-an-incompatible-upstream-pin technique
# geper/requirements.txt already documents (and this Dockerfile already
# uses, below) for MMSplice's cyvcf2 pin, so this is a precedented
# pattern in this exact codebase, not a new one invented here.
#
# NOT VERIFIED: whether Borzoi's actual runtime behavior is correct under
# transformers 4.56.2 (below its own declared floor) -- this sandbox has
# never been able to reach huggingface.co to load real weights and check.
# Enformer is the one half of this pair the task's own instruction states
# was confirmed working under 4.56.2; Borzoi specifically needs its own
# real-weight verification on Colab. See DOCKER.md.
RUN pip install --no-cache-dir "enformer-pytorch==0.8.12"
RUN pip install --no-cache-dir --no-deps "borzoi-pytorch==0.5.1"

# --- borzoi-pytorch's own undeclared-by-omission runtime dependency ---
# `--no-deps` above (needed to dodge the transformers conflict) means
# pip never installs anything from borzoi-pytorch's OWN requires_dist
# beyond what's already present. Checked that full list against live
# PyPI metadata (pypi.org/pypi/borzoi-pytorch/json): einops, numpy,
# torch, transformers, pandas are already installed via
# geper/requirements.txt above -- except `intervaltree~=3.1.0`, which
# is not. `borzoi_pytorch/__init__.py` unconditionally does `from
# borzoi_pytorch.gene_utils import Transcriptome`, and gene_utils.py
# does `from intervaltree import IntervalTree` at module level --
# confirmed directly against the downloaded wheel's source, not
# assumed -- so a bare `import borzoi_pytorch` fails immediately
# without this. This never surfaces on Colab/bare-metal because
# GEPER's runtime auto-install path (utils/auto_install.py::
# ensure_pip_package_available) runs a plain `pip install
# borzoi-pytorch` with no `--no-deps`, which pulls intervaltree in
# transitively same as any other dependency -- this gap is specific to
# the --no-deps workaround this Dockerfile uses above. Pinned to
# borzoi-pytorch's own declared `~=3.1.0` constraint exactly, not a
# newer intervaltree release, since 3.2.x falls outside that range.
RUN pip install --no-cache-dir "intervaltree~=3.1.0"

# --- MMSplice's bundled pretrained weights -----------------------------
# geper/requirements.txt's own comment recommends exactly this for a
# Docker/CI image (baked in here instead of GEPER's runtime auto-install
# on first use, which needs PyPI network access the first time MMSplice is
# invoked): --no-deps avoids pulling in cyvcf2/kipoiseq/pyranges, none of
# which GEPER's own code imports (it only reads the package's bundled .h5
# weight files), and cyvcf2 cannot even build on Python 3.12+ (see
# geper/requirements.txt for the full explanation). tensorflow itself is
# already installed above via geper/requirements.txt's own unconditional
# pin.
RUN pip install --no-cache-dir "mmsplice==2.4.0" --no-deps

# --- Final, authoritative transformers pin ------------------------------
# Must be the LAST pip install touching transformers: guarantees this
# exact version wins regardless of what any package installed above
# (geper/requirements.txt's own >=5.12.1 pin, enformer-pytorch's own
# ==4.56.2 pin which should already land here anyway, or anything else's
# transitive resolution) actually resolved to.
#
# ACTION NEEDED, flagged for the user rather than silently done here:
# geper/requirements.txt and geper/GEPER_Colab.ipynb should probably be
# updated to match this pin (or this override removed, if a future
# decision goes the other way) -- see this Dockerfile's file-level
# docstring and DOCKER.md "Known open questions" for the full writeup.
# geper/pipeline/models/enformer_plugin.py's `all_tied_weights_keys`
# monkey-patch (its own `_load_impl`) is an `if not hasattr(...)` no-op
# guard and does nothing under transformers 4.x -- harmless either way,
# not something this pin conflicts with.
RUN pip install --no-cache-dir "transformers==4.56.2"

# Build-time dependency sanity gate -- surfaces conflicts in the build log
# rather than silently shipping a broken environment. Not a hard failure:
# borzoi-pytorch's own declared ">=4.57.6" is EXPECTED to be flagged here
# (see the verified-conflict writeup above) and must not block the build;
# any OTHER conflict listed is genuinely new and worth investigating.
RUN pip check || echo "pip check reported the conflict(s) above -- if the ONLY one listed is borzoi-pytorch's own declared transformers>=4.57.6 (vs the intentionally-pinned 4.56.2), that is the known, accepted conflict documented above. Any other conflict needs investigating before trusting this image."

# --- HyenaDNA source (not weights) --------------------------------------
# Official repo has no PyPI package; geper/models/hyenadna.py normally
# `git clone`s it at runtime the first time HyenaDNA is loaded (see
# `install_hyenadna_colab` in that module). Pre-cloning here removes that
# one runtime network dependency -- this is source code (~50MB on disk,
# confirmed against an existing local checkout), not the multi-GB
# checkpoint weights (those stay a runtime download into a mounted
# volume -- see "Model weights" in DOCKER.md). Cloned to a staging
# location and copied into the exact relative path
# (geper/hyena-dna) that loader already looks for in the runtime stage
# below, so it is picked up as "already installed" with zero further
# action -- see `install_hyenadna_colab`'s own `os.path.isdir(repo_dir)`
# check.
RUN git clone --depth 1 https://github.com/HazyResearch/hyena-dna.git /opt/hyena-dna-src \
    && rm -rf /opt/hyena-dna-src/.git


# =============================================================================
# Stage 2/2 -- runtime: only what's needed to actually run GEPER/Kim/bridge.
# No compiler, no build headers.
# =============================================================================
FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="GEPER" \
      org.opencontainers.image.description="Combined Kim (FASTQ->VCF) + GEPER (VCF->Clinical Report) genomic variant pipeline"

# Runtime system packages -- every one here is a REAL, verified-in-code
# dependency (grepped for every subprocess call to an external binary
# across pipeline/models/, models/, and database/ this session), not a
# defensive guess:
#   bwa, samtools, bcftools  -- Kim's alignment/variant-normalization
#                                toolchain (required; kim_pipeline/docs/
#                                INSTALL_DEPENDENCIES.md)
#   tabix                    -- AlphaMissense catalogue lookups
#                                (models/alphamissense.py; required per
#                                geper/requirements.txt's own comment)
#   minimap2                 -- Kim's optional alternate aligner
#   git, git-lfs              -- HyenaDNA's actual pretrained-checkpoint
#                                download is a `git lfs install && git
#                                clone` against a HuggingFace repo
#                                (models/hyenadna.py::_load_hyenadna_checkpoint)
#                                -- NOT a huggingface_hub API call. This
#                                is a genuine runtime dependency, not just
#                                a build-time one (the source-only clone
#                                above doesn't need it; the checkpoint
#                                download does).
#   r-base-core                -- SPiP (CONFIG.splicing.ENABLE_SPIP
#                                defaults true) shells out to `Rscript`
#                                (pipeline/models/spip_plugin.py); its
#                                three CRAN packages (foreach, doParallel,
#                                randomForest) still auto-install via
#                                `install.packages()` on first use --
#                                NOT pre-installed here (would need R's
#                                own build toolchain for randomForest's
#                                compiled code, a meaningfully bigger
#                                addition to this Dockerfile for a plugin
#                                that degrades gracefully without it) --
#                                see DOCKER.md's "what's baked in vs.
#                                still a first-run cost" table.
#   ncbi-blast+                -- local BLAST+ (blastn/makeblastdb/
#                                blastdbcmd), GEPER's preferred production
#                                backend (README.md section 20). NOT in
#                                this task's literal required-tool list --
#                                included anyway since GEPER degrades
#                                gracefully to remote BLAST or skips
#                                entirely without it, so it costs one apt
#                                line to close a real production gap.
#   libcurl4, libssl3, zlib1g,
#   libbz2-1.0, liblzma5,
#   libncurses6               -- runtime shared libraries the
#                                builder-compiled freebayes binary (and
#                                samtools/bcftools's own htslib) link
#                                against. FLAGGED, not fully verified:
#                                exact package names/ABIs for Debian 12
#                                bookworm were not confirmed against a
#                                live apt-get in this pass (see this
#                                file's top docstring) -- if `freebayes
#                                --version` fails at runtime with a
#                                missing-.so error, this line is the
#                                first thing to check.
#   ca-certificates            -- every provider lookup in this pipeline
#                                is HTTPS (ClinVar, gnomAD, ClinGen, ...)
RUN apt-get -o Acquire::Retries=15 -o Acquire::http::Pipeline-Depth=0 -o Acquire::Queue-Mode=access -o Acquire::ForceIPv4=true update \
    && apt-get -o Acquire::Retries=15 -o Acquire::http::Pipeline-Depth=0 -o Acquire::Queue-Mode=access -o Acquire::ForceIPv4=true install -y --no-install-recommends bwa samtools bcftools tabix minimap2 libtabixpp0 libseqlib2 \
    && rm -rf /var/lib/apt/lists/*
RUN apt-get -o Acquire::Retries=15 -o Acquire::http::Pipeline-Depth=0 -o Acquire::Queue-Mode=access -o Acquire::ForceIPv4=true update \
    && apt-get -o Acquire::Retries=15 -o Acquire::http::Pipeline-Depth=0 -o Acquire::Queue-Mode=access -o Acquire::ForceIPv4=true install -y --no-install-recommends libcurl4 libssl3 zlib1g libbz2-1.0 liblzma5 libncurses6 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN apt-get -o Acquire::Retries=15 -o Acquire::http::Pipeline-Depth=0 -o Acquire::Queue-Mode=access -o Acquire::ForceIPv4=true update \
    && apt-get -o Acquire::Retries=15 -o Acquire::http::Pipeline-Depth=0 -o Acquire::Queue-Mode=access -o Acquire::ForceIPv4=true install -y --no-install-recommends git git-lfs r-base-core ncbi-blast+ \
    && git lfs install --system \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /usr/local/bin/freebayes /usr/local/bin/freebayes
COPY --from=builder /opt/hyena-dna-src /opt/hyena-dna-src
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /app
COPY . /app

# HyenaDNA source lands exactly where geper/models/hyenadna.py's
# `install_hyenadna_colab` already looks for it (see builder-stage
# comment above) -- this makes the pre-baked source "already installed"
# from that function's point of view with zero further action needed.
RUN rm -rf /app/geper/hyena-dna && cp -r /opt/hyena-dna-src /app/geper/hyena-dna \
    && rm -rf /opt/hyena-dna-src

# --- Environment ---------------------------------------------------------
# Unbuffered stdout/stderr so `docker logs`/Colab cell output shows
# progress in real time during a long run instead of only at exit.
ENV PYTHONUNBUFFERED=1

# GEPER_NCBI_EMAIL / GEPER_NCBI_API_KEY are deliberately NOT baked in with
# real values here (they're per-user credentials, and geper/config.py's
# own committed default is a placeholder NCBI's usage policy explicitly
# warns against using in production -- see geper/utils/ncbi_eutils.py's
# startup warning). Pass them at `docker run -e ...` / via
# docker-compose.yml's `environment:`/`env_file:` -- see DOCKER.md.

# GEPER_CACHE_DIR (G2, report review round 5): explicitly set here so
# every cache that `geper/config.py` roots under it when it's set --
# HyenaDNA's checkpoint dir, the plugin weight cache (Enformer/Borzoi/
# SpliceBERT/SpliceFormer/SPiP), and RNA-FM's TORCH_HOME -- actually
# does, inside this container. Consolidates what used to be three
# separate named volumes (geper_model_cache/geper_checkpoints/
# geper_plugin_model_cache) into subdirectories of this one path, so
# there is exactly one volume to mount for "persist every model/dataset
# cache across container runs" instead of three that had to be kept in
# sync by hand. Unset, every one of those would silently fall back to
# its own pre-consolidation container-local default path and re-download
# on every fresh container -- this ENV line is what activates the
# fallback logic those config fields already have (see config.py's own
# comments on HYENADNA_CHECKPOINT_DIR/PLUGIN_CACHE_DIR for the exact
# rule: an explicit GEPER_HYENADNA_CKPT_DIR/GEPER_PLUGIN_CACHE_DIR/
# TORCH_HOME still always wins over this).
ENV GEPER_CACHE_DIR=/app/geper/model_cache

# All of these are auto-created by GEPER's own bootstrap/download logic
# on first use and are declared as volumes rather than baked into the
# image -- see "Model weights" in DOCKER.md for the full reasoning
# (multi-GB, re-fetchable, and baking them in would make this image
# multiple times larger for zero benefit over a persistent volume) and
# docker-compose.yml for the concrete named-volume wiring:
#   geper/model_cache          -- ONE consolidated cache root (F2 +
#                                  G2, report review rounds 4-5):
#                                  ClinGen/HPO/Orphanet/AlphaMissense
#                                  bootstrap datasets, BLAST/gnomAD/etc.
#                                  disk caches, PLUS -- now that
#                                  GEPER_CACHE_DIR is set above --
#                                  hyenadna/ (checkpoint), torch/
#                                  (RNA-FM), and plugin_model_cache/
#                                  (Enformer/Borzoi/SpliceBERT/
#                                  SpliceFormer/SPiP weights, ~3.1GB
#                                  combined, verified against this
#                                  session's own local checkout) as
#                                  subdirectories under this same root.
#   geper/geper_output          -- JSON/Markdown/PDF results, per-run
#   /root/.cache/huggingface    -- kept as a defensive fallback for any
#                                  HF-backed load path that does NOT
#                                  take an explicit cache_dir (none
#                                  identified in this pass -- ESM-2,
#                                  Enformer, and Borzoi all pass one
#                                  explicitly, and SpliceBERT/RNA-FM
#                                  don't touch HF's default cache at
#                                  all -- but not verified live, so this
#                                  volume stays rather than being
#                                  removed on an unconfirmed assumption)
# One VOLUME instruction per path (not a single multi-line JSON array) --
# unambiguous, no line-continuation-inside-JSON-array edge case to worry
# about.
VOLUME ["/app/geper/model_cache"]
VOLUME ["/app/geper/geper_output"]
VOLUME ["/root/.cache/huggingface"]

# No single ENTRYPOINT/CMD is forced: this image supports three
# independently valid, already-documented ways to run this pipeline (see
# README_INTEGRATION.md and DOCKER.md) -- GEPER standalone against an
# existing VCF, Kim standalone from FASTQ, or the combined
# bridge/run_combined.py workflow -- and hard-coding one as THE entrypoint
# would silently make the other two second-class. Defaults to an
# interactive shell; DOCKER.md documents the exact `docker run` command
# for each of the three modes.
CMD ["/bin/bash"]
