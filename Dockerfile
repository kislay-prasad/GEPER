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
# with how the bridge path uses Kim: bridge/combined_pipeline.py always
# runs Kim with `mode="vcf_only"`, so Kim's own AI/annotation/reporting
# stages (the ones that would want the `[ai]` extra) are never invoked BY
# THE BRIDGE PATH -- see README_INTEGRATION.md. They ARE reachable from this
# image: kim_pipeline/api/main.py calls `runner.run()` without a mode, so it
# gets the runner's default `mode="full"` (pipeline/orchestration/runner.py),
# and Kim's own CLI defaults to `--mode full` (kim_pipeline/main.py). Both
# are installed in this same venv, so anyone who starts Kim directly in a
# container from this image hits the live AI path. (Corrected 2026-09-11,
# god's ruling under the human's #8: the earlier wording, "never invoked
# through this image at all", was a claim about the image and was false.)
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
#   - geper/requirements.txt (and geper/GEPER_Colab.ipynb) both pinned
#     "transformers>=5.12.1,<6.0.0" -- satisfying NEITHER of the above --
#     until 2026-09-11, when (human ruling) both were changed to
#     "transformers==4.56.2", the version this image ships (see the final
#     pin below; geper/tests/test_transformers_pin_matches_image.py).
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
# (geper/requirements.txt's own pin, enformer-pytorch's own ==4.56.2 pin
# which should already land here anyway, or anything else's transitive
# resolution) actually resolved to.
#
# RESOLVED 2026-09-11 (human ruling: "Bring the requirement in line with
# the image"): geper/requirements.txt and geper/GEPER_Colab.ipynb now pin
# transformers==4.56.2, matching this line. THIS LINE IS THE AUTHORITY:
# geper/tests/test_transformers_pin_matches_image.py reads the version
# from here and fails if requirements.txt, verify_environment.py, the
# Colab notebook or the README matrix disagree with it.
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
#   git, git-lfs             -- HyenaDNA's actual pretrained-checkpoint
#                                download is a `git lfs install && git
#                                clone` against a HuggingFace repo
#                                (models/hyenadna.py::_load_hyenadna_checkpoint)
#                                -- NOT a huggingface_hub API call. This
#                                is a genuine runtime dependency, not just
#                                a build-time one (the source-only clone
#                                above doesn't need it; the checkpoint
#                                download does).
#   libcurl4,libssl3, zlib1g,
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
#
# ── REMOVED 2026-09-11, ON THE HUMAN'S RULING ("DROP THEM"): minimap2,
#    r-base-core, ncbi-blast+. Surface area the shipped product's DEFAULT
#    configuration does not reach, two of them with licence exposure that is
#    with counsel. READ WHAT THIS REMOVAL DOES AND DOES NOT DEMONSTRATE
#    BEFORE YOU CITE A GREEN BUILD AS EVIDENCE ABOUT IT.
#
#    WHAT DOES NOT ESTABLISH IT: A BUILD THAT SUCCEEDS AFTER THIS CHANGE.
#    Package installation is fail-fast and nothing in the image build runs
#    the pipeline, so a green build proves only that the build gets further.
#    It says NOTHING about whether any code path needed these binaries, and
#    nothing that has never been exercised on this hardware has been
#    exercised by it. A GREEN BUILD AFTER A REMOVAL READS AS "NOTHING NEEDED
#    THEM". IT IS NOT THAT.
#
#    WHAT DOES ESTABLISH IT, AS FAR AS IT GOES: a caller census, 2026-09-11,
#    over all 577 tracked .py files (577 parsed by `ast`, 0 refused), both
#    pipeline trees (geper/ and kim_pipeline/), for every string literal
#    naming minimap2, Rscript, blastn, makeblastdb, blastdbcmd, blastp,
#    tblastn or blastx. Every production call site found, and what it does
#    when the binary is absent:
#      minimap2 -- kim_pipeline alignment. config/default.yaml ships
#        `aligner: "auto"`, which picks bwa first (bwa is installed above),
#        so the DEFAULT path never reaches minimap2. `aligner: minimap2` set
#        explicitly now fails LOUDLY: FastqPipelineError "aligner='minimap2'
#        requested but minimap2 is not installed" (alignment/stage.py).
#      ncbi-blast+ -- two callers. kim_pipeline blast stage: `enabled:
#        false` and `db_path: ""` by default, so not reached. geper
#        database/blast_client.py: "local" mode needs BOTH a configured
#        GEPER_BLAST_LOCAL_DB (default "") AND `blastn` on PATH; otherwise
#        it returns "remote". DEFAULT: already "remote", unchanged. BUT A
#        SITE THAT SETS GEPER_BLAST_LOCAL_DB WILL NOW BE MOVED FROM LOCAL TO
#        REMOTE BLAST, and that branch logs nothing -- that is a behaviour
#        change for any deployment configured for local BLAST, not a no-op.
#      r-base-core -- ONE caller, and IT IS REACHED BY DEFAULT: SPiP
#        (pipeline/models/spip_plugin.py) is gated by ENABLE_SPIP, which
#        DEFAULTS TO TRUE (geper/config.py, GEPER_ENABLE_SPIP), and needs
#        `Rscript`. Without R, SPiP is unavailable in every default run. Its
#        absence is ANNOUNCED, not silent: is_available() is False and the
#        orchestrator lists it with the reason "no 'Rscript' executable
#        found in this environment". THIS REMOVAL THEREFORE CHANGES WHAT THE
#        DEFAULT CONFIGURATION PRODUCES: SPiP no longer runs. The human was
#        shown this and ruled "drop SPiP" (2026-09-11); the image therefore
#        sets GEPER_ENABLE_SPIP=false (below), so reports say SPiP was
#        DISABLED BY CONFIGURATION rather than that R is missing.
#        The local-BLAST egress above is being made fail-fast in
#        blast_client.py on a separate card, not here.
#
#    The 143 tracked non-Python files were swept too (prose -- .md/.txt/
#    .rst/.html -- excluded): 8 name one of these binaries, and none adds a
#    caller. They are this file, a CI comment, two config comments, the dev
#    installer kim_pipeline/install_dependencies.sh, and SPiP's own three
#    vendored R scripts -- which are what `Rscript` runs, so they go dark
#    with it.
#
#    WHAT THE CENSUS CANNOT SEE: a binary name built at runtime rather than
#    written as a literal; the operator's own commands inside a container;
#    a deployment's own configuration. It establishes where the CODE reaches
#    these tools. It does not establish that no USER does.
#
#    BUILT 2026-09-11, authorised by the human: geper:vic-trim-htslib-d2fc3ee,
#    from this file at d2fc3ee. Exit 0, 10:56Z-11:05Z. WHAT THAT BUILD SHOWED,
#    AND ONLY THAT:
#      - The RUNTIME stage ran fresh, every step. The three packages are not
#        installed (dpkg-query) and none of minimap2, R, Rscript, blastn,
#        makeblastdb, blastdbcmd, blastp is on PATH -- the same `command -v`
#        loop in the pre-trim image geper:kelly-master-d249a20 finds all 7,
#        so the absence is real, not a blind probe.
#      - GEPER's own verify_environment.py in the new image differs from the
#        pre-trim image in exactly one row: BLAST+ goes from PASS to WARN
#        ("remote NCBI BLAST is used instead"). Its one FAIL -- transformers
#        4.56.2 against geper/requirements.txt's >=5.12.1 -- is IDENTICAL in
#        the pre-trim image: pre-existing, not caused by this removal.
#      - SPiP reports `is_available: False`, "disabled via
#        CONFIG.splicing.ENABLE_SPIP" (pre-trim image: True).
#    WHAT IT DID NOT SHOW: THE BUILDER STAGE CAME ENTIRELY FROM CACHE -- the
#    freebayes binary, the venv and the builder's apt layer were not rebuilt
#    by this build. And NOTHING RAN THE PIPELINE: no FASTQ was aligned, no
#    VCF annotated, no report rendered in this image. So the build still
#    proves only that the build gets further; that nothing in the code needs
#    these binaries rests on the census above, and on nothing else.
RUN apt-get -o Acquire::Retries=15 -o Acquire::http::Pipeline-Depth=0 -o Acquire::Queue-Mode=access -o Acquire::ForceIPv4=true update \
# ── VERSIONS PINNED 2026-09-10. READ THIS BEFORE CHANGING OR REMOVING THEM. ──
# WHY: nothing here was pinned, so the genomics stack was whatever bookworm
# happened to ship on build day, and CI's was whatever ubuntu-latest happened
# to ship on run day. The two drifted independently and NOTHING REPORTED IT.
# For a clinical artefact the question is not which version is newer, it is
# WHICH VERSION THE REPORTS WERE VALIDATED AGAINST -- and that question has no
# answer at all while the version floats.
#
# WHAT MAKES THE PIN LOAD-BEARING RATHER THAN TIDY: this pipeline PARSES
# HUMAN-READABLE TOOL OUTPUT AS IF IT WERE AN API. The duplicate count is
# obtained by string-matching "DUPLICATE TOTAL" in samtools' STDERR
# (kim_pipeline/pipeline/alignment/bam_utils.py, mark_duplicates); flagstat is
# read as `-O tsv` and coverage as its default table. A FORMAT CHANGE BETWEEN
# VERSIONS CHANGES WHAT THIS PIPELINE RECORDS WITHOUT CHANGING A SINGLE CALL
# AND WITHOUT ERRORING. That is why these are pinned and not merely noted.
#
# VALUES: measured with `dpkg -l` inside the shipped image
# geper:kelly-master-d249a20 on 2026-09-10, and confirmed the same day to be
# bookworm's current candidates -- so this pin CHANGES NOTHING TODAY. It fixes
# what is already true in place, which is the only kind of pin that is safe to
# add to a clinical image without re-validating it.
#
# ── AND THE PART THAT IS NOT YET TRUE: THIS PINNED LINE HAS NEVER BEEN
#    RESOLVED BY A BUILD. Measured 2026-09-11 across all 24 images on the
#    build host: NOT ONE CONTAINS A PINNED apt LINE. Every image here was
#    built from a Dockerfile whose genomics packages were unversioned, so no
#    apt anywhere has ever been asked for these exact versions together.
#    (There were five; minimap2 was the fifth and was removed 2026-09-11 --
#    see the REMOVED block above. The four that remain have still never
#    been resolved by a build, so the removal does not change this note.)
#
#    ── SUPERSEDED 2026-09-11 BY A BUILD. The paragraph above was true when
#    written and is kept so the record reads in order. geper:vic-trim-htslib-
#    d2fc3ee (authorised by the human) RESOLVED THIS LINE: the runtime layer
#    built fresh, not from cache, and `dpkg-query` INSIDE THE BUILT IMAGE
#    reports exactly the pinned versions for all five -- bwa 0.7.17-7+b2,
#    samtools 1.16.1-1, bcftools 1.16-1, tabix 1.16+ds-3, libhts3 1.16+ds-3.
#    Installed equals pinned; no difference. `samtools --version` and
#    `bcftools --version` both report "Using htslib 1.16", and freebayes
#    links libhts.so.3 from that package. The rest of this note -- the
#    designed failure when bookworm moves -- still stands.
#
#    WHAT THAT MEANS IF YOU ARE THE ONE BUILDING THIS: you are the first, and
#    a failure will be AMBIGUOUS. "Version '1.16.1-1' for 'samtools' was not
#    found" is the designed failure described below, but at a first build it is
#    indistinguishable from a typo in this line or from the flaky network that
#    produced the original tool trim (see c010ce0). Resolve that ambiguity with
#    `apt-cache policy <pkg>` in a throwaway bookworm container BEFORE
#    concluding the pin is wrong.
#
#    WHAT THIS NOTE DOES NOT SAY: it does not say the pin is WRONG. Two
#    independent reads on 2026-09-10 -- `dpkg -l` inside the shipped image and
#    `apt-cache policy` in a clean bookworm container -- agreed these are
#    bookworm's current candidates. UNVALIDATED IS NOT THE SAME CLAIM AS
#    INCORRECT, and the evidence points the other way.
#
#    WHY IT IS WRITTEN HERE AND NOT ONLY IN THE COMMIT MESSAGE: it WAS in the
#    commit message (7f8158c, "DEMONSTRATED WITHOUT BUILDING THE PROJECT"), and
#    that is the wrong home for it -- `git bisect` and a plain checkout both
#    discard exactly the prose that makes this honest, and the file is what
#    travels. This repository already set the standard it was failing here:
#    VALIDATION_STUDY_DESIGN.md:934 -- "silence is not an option; 'not
#    validated' is a finding to state."
#
#    NOTE ALSO THAT NOTHING AUTOMATED WILL EVER TELL YOU: CI builds no image at
#    all (.github/workflows/ holds one file, pytest.yml, and it does not
#    mention docker). A Dockerfile revision is the one change this project's CI
#    cannot evaluate.
## *** THE COST, STATED SO NOBODY IS SURPRISED BY IT LATER: A PIN NOBODY
# REVISITS IS A VERSION NOBODY UPGRADES. Bookworm WILL move under this line --
# a security update to samtools or bcftools will make `apt-get install` fail
# with "Version '1.16.1-1' for 'samtools' was not found", and the build stops
# dead. THAT FAILURE IS THE FEATURE: it is a prompt to decide, deliberately,
# whether to move a clinical pipeline onto a new aligner build -- which is
# exactly the decision that was being made silently before. ***
#
# NOT PINNED, AND ON PURPOSE: libtabixpp0 and libseqlib2 (below) are runtime
# shared libraries, not tools whose OUTPUT this pipeline reads. Leaving them
# free lets apt apply security updates. If that distinction ever stops being
# true, pin them too.
#
# ── libhts3 IS PINNED, AND IT IS THE EXCEPTION TO THAT RULE (2026-09-11, on
#    the human's ruling "PIN, AND PIN CI TO WHAT SHIPS"). libhts3 IS htslib,
#    and htslib is not a library beside the tools -- IT IS THE PART OF THEM
#    THAT READS AND WRITES THE DATA. samtools, bcftools and tabix link it, and
#    so does the freebayes binary compiled in the builder stage: `ldd
#    /usr/local/bin/freebayes` in geper:kelly-master-d249a20 resolves
#    libhts.so.3 to THIS package. Pinning the four tools while htslib floated
#    would have pinned the version strings a reader checks and left the
#    variant caller's own reads free to move underneath them.
#    VALUE: 1.16+ds-3, measured with dpkg-query in ALL 18 geper and
#    geper-clinical images on the build host (18 of 18 agree), and on
#    2026-09-11 confirmed as bookworm's current candidate by `apt-get install
#    -s` of this exact line in a clean python:3.12-slim-bookworm -- the first
#    time this pin line has been RESOLVED rather than read. So this pin, too,
#    changes nothing today. NOTE bookworm is now Debian OLDSTABLE: the day it
#    moves to archive.debian.org, every pin on this line stops resolving at
#    once. That is the designed failure described above, arriving all at once.
#    CI INSTALLS THESE VERSIONS BY READING THEM FROM THIS LINE (see
#    .github/workflows/pytest.yml) -- change them here and CI follows; there is
#    no second copy to drift.
#    NOT PINNED: libhtscodecs2 (1.3.0-4 in the same image), htslib's CRAM codec
#    library. No production code path in either tree reads CRAM (the only
#    mention outside tests is raw_input_validator.py recognising the magic
#    bytes, which does not decode it). If CRAM input is ever accepted, pin it.
#    AND NOT PINNED: the BUILDER stage's htslib headers, pulled in unversioned
#    by libvcflib-dev/libseqlib-dev to compile freebayes. The runtime library
#    above is what executes; the headers are not measured here, because no
#    builder-stage image is retained on the build host to measure them from.
    && apt-get -o Acquire::Retries=15 -o Acquire::http::Pipeline-Depth=0 -o Acquire::Queue-Mode=access -o Acquire::ForceIPv4=true install -y --no-install-recommends bwa=0.7.17-7+b2 samtools=1.16.1-1 bcftools=1.16-1 tabix=1.16+ds-3 libhts3=1.16+ds-3 libtabixpp0 libseqlib2 \
    && rm -rf /var/lib/apt/lists/*
RUN apt-get -o Acquire::Retries=15 -o Acquire::http::Pipeline-Depth=0 -o Acquire::Queue-Mode=access -o Acquire::ForceIPv4=true update \
    && apt-get -o Acquire::Retries=15 -o Acquire::http::Pipeline-Depth=0 -o Acquire::Queue-Mode=access -o Acquire::ForceIPv4=true install -y --no-install-recommends libcurl4 libssl3 zlib1g libbz2-1.0 liblzma5 libncurses6 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN apt-get -o Acquire::Retries=15 -o Acquire::http::Pipeline-Depth=0 -o Acquire::Queue-Mode=access -o Acquire::ForceIPv4=true update \
    && apt-get -o Acquire::Retries=15 -o Acquire::http::Pipeline-Depth=0 -o Acquire::Queue-Mode=access -o Acquire::ForceIPv4=true install -y --no-install-recommends git git-lfs \
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

# PYTHONPATH must include /app so that when bridge/combined_pipeline.py runs
# kim_pipeline/main.py in a subprocess with cwd=kim_pipeline, the 'shared'
# module at /app/shared/ remains importable. Without this, Kim's subprocess
# fails with "ModuleNotFoundError: No module named 'shared'".
ENV PYTHONPATH=/app:${PYTHONPATH}

# SPiP OFF BY CONFIGURATION (2026-09-11, the human's ruling: "drop SPiP").
# R is not in this image (see the REMOVED block above), so SPiP cannot run
# here either way. Without this line every report would explain its absence
# as "no 'Rscript' executable found in this environment" -- which reads as a
# missing dependency, an accident. With it, the report says SPiP was
# "disabled via CONFIG.splicing.ENABLE_SPIP" -- which is what happened: a
# decision. geper/config.py reads this variable; "false" disables.
ENV GEPER_ENABLE_SPIP=false

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
