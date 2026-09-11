# What a future `bridge-ready` Dockerfile must satisfy

Measured 2026-09-11 against `7ea6827` and against the images on the build
host. Every condition below is stated with the evidence that it bites and
with **what a build that violates it actually looks like**, because that is
what the next person will be staring at — not this list.

**Read this before concluding a step is missing from `Dockerfile.bridge-ready`.**
That file cannot produce `bridge-ready` on its own, and its own header explains
why. This document is the checklist; the reasoning lives next to the code.

---

## The headline, because it changes how you read the rest

**Six of the seven conditions below were already written down** — five in
`Dockerfile.bridge-ready`'s header and one in `scripts/verify_offline_models.py`'s
docstring. They are consolidated here because they are spread across two files
and because three of them are "do not do X" rules that read as under-specification
to anyone who has not been bitten.

**Condition 7 is documented nowhere, and the shipped image violates it.**
That is the one to read if you read only one.

---

## The conditions

### 1. The build context cannot carry the seed — it is refused outright

**Evidence it bites.** The HuggingFace layout contains five relative symlinks
(`models--facebook--esm2_t33_650M_UR50D/snapshots/<sha>/* -> ../../blobs/<sha>`).
BuildKit refuses to load a context containing them:

```
ERROR: invalid file request model_cache_seed/models--facebook--esm2_t33_650M_UR50D/snapshots/08e4846e.../config.json
```

Measured three times, by three agents, on three different Dockerfiles.

**What a violating build looks like.** It does not build at all, and the error
names a file rather than the mechanism — so the instinct is to chase that one
file. This is the friendly failure: it is loud and it stops.

**Consequence for `.dockerignore`.** The seed exclusion is load-bearing. Without
it the repository does not build from any checkout that has the seed on disk.
Do not "tidy" it away.

### 2. `docker cp` does not work either, and it fails *quietly*

**Evidence it bites.** `docker cp` cannot encode POSIX symlinks from Windows. It
prints `unknown file mode ?rw-rw-rw-`, **continues, exits zero**, and leaves the
blobs whole and the pointers gone.

**What a violating build looks like.** A green build, a full-looking cache
directory, a matching file count, a matching `du` — and a cache miss at load
time. `geper:bridge-ready-BROKEN-esm2-offline` is exactly this state, preserved:
all 2.6 GB of correct ESM2 weights, missing five symlinks and one 40-byte
`refs/main`. **Every cheap check passes on it.**

**What works instead:** a runtime bind mount, `cp -a` *inside* the container,
then commit. `-a` is what reproduces the symlinks; a plain `cp -r` dereferences
them and silently inflates the image by the size of every duplicated blob.

### 3. `VOLUME` shadowing — and this one bites *before* any symlink question

**Evidence it bites.** `docker inspect` on the working image returns three
declared volumes:

```
/app/geper/geper_output   /app/geper/model_cache   /root/.cache/huggingface
```

**Two of the three obvious places to put a model cache are declared volumes, and
`docker commit` does not capture volume contents.** A seed written there builds
clean, commits clean, and is **absent at run time**.

**This is live in the tracked file, not hypothetical.** `Dockerfile:562` sets
`ENV GEPER_CACHE_DIR=/app/geper/model_cache` and `Dockerfile:597` declares
`VOLUME ["/app/geper/model_cache"]`. The same file points its cache variable at
a path it declares as a volume. Verified at `7ea6827`.

**What a violating build looks like.** Everything succeeds. The image is the
right size. The seed is provably in the layer. At run time the directory is
empty, and the failure surfaces as a network fetch — so it reads as "offline
mode is broken" rather than "the cache went to a volume".

**The working image avoids it by re-pointing to `/app/model_cache_seed`, which is
deliberately not a declared volume.** Measured off the image, not read off a
Dockerfile.

### 4. `HF_HUB_CACHE` is the variable for the cache **root** — the three are not interchangeable

**Evidence it bites.** `HF_HOME` alone sends the library to `$HF_HOME/hub`, one
level too deep. An earlier image had `HF_HOME=/app/model_cache_seed/hf` and
therefore missed.

**Measured on the working image**, all three set to the same path:

```
GEPER_CACHE_DIR=/app/model_cache_seed
HF_HUB_CACHE=/app/model_cache_seed
HF_HOME=/app/model_cache_seed
```

**The tracked `Dockerfile` sets `HF_HUB_CACHE` zero times.** Verified at `7ea6827`.

**What a violating build looks like.** A full-looking directory and a miss — the
same symptom as condition 2, from a different cause, which is why the two get
confused. A rebuilder who sees all three variables set to one path will assume
they are aliases and collapse them. They are not.

### 5. Do **not** put the vendored HyenaDNA source on `PYTHONPATH`

This reads as under-specification. It is not. It is the one condition here whose
violation makes the acceptance test *stronger-looking and weaker*.

**Evidence it bites.** With only `/app` and `/app/geper` on the path, a HyenaDNA
load under `--network none` once failed with
`Could not resolve host: github.com` — the image reaching for GitHub at
model-load time. Three of four models loaded; this was the fourth.

**Why the obvious fix was removed.** Adding `/app/geper/hyena-dna` to `PYTHONPATH`
makes `find_spec("standalone_hyenadna")` succeed *no matter where the process was
launched from*. The real defect was in the loader, and it is fixed there:
`geper/models/hyenadna.py` now resolves the vendored checkout from `__file__`
rather than the working directory.

**What a violating build looks like.** It looks like a pass. The entry silently
absorbs any future regression of the module's path handling — **the offline proof
stays green while the loader is broken again.** Removing it is precisely what
gives that proof the ability to fail.

### 6. The acceptance test must be able to tell a hit from a miss

Each of these was paid for; see `scripts/verify_offline_models.py`'s docstring.

- **A load, never a listing.** A cache is proven by constructing the model.
  `bridge-ready-BROKEN-esm2-offline` passes every listing-based check.
- **No time threshold.** The same ESM2 load measured 7.7 s, 12.1 s and 27.9 s on
  this hardware. A threshold fails on a busy machine and teaches the next person
  to distrust a working cache. Durations are printed as observations.
- **Do not set `HF_HUB_OFFLINE`.** Telling the library not to try is weaker than
  letting it try and be unable to reach the network. The isolation belongs to
  `docker run --network none`.
- **Report a parameter count.** A load returning a randomly-initialised model
  would otherwise also "succeed". ESM2 reports ~651.0M.
- **`pooler.dense` re-initialisation is not a failure.** transformers prints it
  on a cache hit and a cache miss alike. Do not document it as a cache warning
  or it will scare somebody off a passing run.

### 7. The verification must be in **exit-code position** — nothing after it that cannot fail

**This condition is documented nowhere else, and the shipped image violates it.**

**Evidence it bites — `geper:bridge-ready`, measured 2026-09-11.** Its `Config.Cmd`
is a **13-element mangled argv**. `sh -c SCRIPT [$0 [$1 ...]]` executes **only
argv[2]**; everything from argv[3] on becomes positional parameters:

```
EXECUTED  (82 chars):  mkdir -p /app/model_cache_seed && cp -a /seed/. /app/model_cache_seed/ && echo ---
DISCARDED (162 chars): verify --- && D=... && cat $D/refs/main && echo " && ls -l $D/snapshots/*/ && du -sh ...
```

Verification verbs in the executed half: **none**. In the discarded half:
`cat`, `ls`, `du` — all three. **The copy ran. The check that the copy was
correct did not.** And the last executed command is `echo ---`, which cannot
fail, so the container exited 0 having checked nothing.

**What a violating build looks like — and this is the important part.**
`docker history` renders that argv **space-joined**, reassembling it into one
coherent-looking command that appears to seed *and verify*. The argv boundaries
are the only thing that determined what ran, and the join erases them.

> **Read `docker inspect` and count the `Cmd` elements. Never cite `docker history`
> as evidence that a command ran — it is a rendering, not a record.**

A correctly-produced image has a two-element `Cmd` (`["-c", "set -e\n..."]`) with
`Entrypoint=["sh"]`. Confirmed on four sibling images and on the evidence
container `bridge-ready-seed-744`. `scripts/build_bridge_ready.sh` produces that
shape; the shipped tag predates it by a day and was hand-assembled.

**The same shape, one level up, in the current tooling.** `build_bridge_ready.sh`
satisfies this condition on its main path — `set -euo pipefail` at `:30`, and the
acceptance run is the last command, so its status is the script's status. But
`--skip-verify` (`:149-155`) prints *"a skipped verification is not a pass"* and
then **`exit 0`**, which every caller and every CI reads as exactly that. The
prose and the exit code disagree, and the exit code is the half machines read.

---

## What none of this is checked by

**No CI job builds or inspects an image.** `.github/workflows/` contains one
file, `pytest.yml`, and it does not mention docker. No automated check on this
project has ever looked at an image's `Cmd`, its volumes, or its cache variables
— which is how a 13-element argv sat on the shipped tag. Every condition above
is enforced by a person reading this or by nothing.
