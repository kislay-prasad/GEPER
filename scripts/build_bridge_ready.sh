#!/usr/bin/env bash
# =============================================================================
# scripts/build_bridge_ready.sh -- build the offline-capable GEPER image
# =============================================================================
# TWO HALVES, AND THE SECOND ONE IS WHY THIS SCRIPT EXISTS AT ALL.
#
#   1. `docker build -f Dockerfile.bridge-ready` -- everything a Dockerfile
#      can do: the cache location and the three environment variables.
#   2. THE SEED STEP, which a Dockerfile cannot express. The seed's five
#      HuggingFace symlinks cannot enter an image through the build context
#      (BuildKit refuses them) and cannot be carried by `docker cp` (which
#      prints a warning, continues, exits zero, and silently drops them).
#      The only mechanism that works is a RUNTIME BIND MOUNT: the copy runs
#      inside Linux, which handles the links natively. Then `docker commit`.
#
# This step is a written, reviewable script rather than a remembered sequence
# BECAUSE THE IMAGE IT REPLACES WAS ASSEMBLED BY HAND. `docker history` on
# that image records the `cp -a` that ran inside the container but not how the
# source arrived, so the question "how were these files put here" had no
# answer on disk. It has one now: this file.
#
# NOTHING HERE WRITES TO THE SEED. It is mounted READ-ONLY (`:ro`) at every
# step, including the verification pass.
#
# USAGE
#   scripts/build_bridge_ready.sh [--base IMAGE] [--tag NAME] [--seed PATH]
#                                 [--skip-verify]
# =============================================================================

set -euo pipefail

BASE_IMAGE="geper:latest"
TAG="geper:bridge-ready"
SEED="${GEPER_SEED_DIR:-}"
SKIP_VERIFY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --base) BASE_IMAGE="$2"; shift 2 ;;
    --tag) TAG="$2"; shift 2 ;;
    --seed) SEED="$2"; shift 2 ;;
    --skip-verify) SKIP_VERIFY=1; shift ;;
    -h|--help) sed -n '1,30p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -z "$SEED" ]; then
  SEED="$REPO_ROOT/model_cache_seed"
fi

# --- Preconditions, checked rather than assumed --------------------------
# Each of these has been a real failure, not a hypothetical one.

if [ ! -d "$SEED" ]; then
  echo "ERROR: no seed directory at '$SEED'." >&2
  echo "       The seed is untracked and exists in no repository; see the" >&2
  echo "       'offline model-cache seed' section of DOCKER.md for what it" >&2
  echo "       is and what is known about regenerating it." >&2
  exit 1
fi

# `refs/main` is a 40-byte file whose absence produces a CACHE MISS with every
# blob present and the directory looking complete. It is the single most
# likely thing to be missing from a restored seed -- the transport tarball
# does not contain it.
REFS_MAIN="$SEED/models--facebook--esm2_t33_650M_UR50D/refs/main"
if [ ! -f "$REFS_MAIN" ]; then
  echo "ERROR: '$REFS_MAIN' is missing." >&2
  echo "       ESM2 will MISS with every weight present. Write the pinned" >&2
  echo "       revision sha into it as 40 bytes with NO trailing newline" >&2
  echo "       (see DOCKER.md); a text-mode write adds a byte and is wrong." >&2
  exit 1
fi

# Five symlinks, counted rather than trusted: a seed that has been through
# `docker cp` or a Windows-native copy arrives with these silently converted
# or dropped, and everything else about it looks correct.
SYMLINK_COUNT="$(find "$SEED" -type l | wc -l | tr -d ' ')"
if [ "$SYMLINK_COUNT" -ne 5 ]; then
  echo "ERROR: expected 5 symlinks in the seed, found $SYMLINK_COUNT." >&2
  echo "       A seed that lost them looks complete and will not load." >&2
  exit 1
fi

# --- The mount path, which is not the same string as the shell path ------
# On Git Bash / MSYS (the machine this is built on) a POSIX path handed to
# `docker run -v` is rewritten by MSYS path conversion before Docker sees it,
# and THE MOUNT COMES UP SILENTLY EMPTY: the container starts, exits zero from
# Docker's point of view, and `cp -a /seed/.` fails with "No such file or
# directory" while every path printed in the log looks correct. Converting to
# a Windows-style path and switching the rewriting off is the fix. On Linux
# and macOS the path is used unchanged.
# Note it converts EVERY path handed to docker, not only the mount:
# disabling the rewriting for one argument disables it for all of them, so a
# half-converted invocation fails on the build context instead
# ("unable to prepare context: path /c/... not found").
SEED_MOUNT="$SEED"
REPO_CTX="$REPO_ROOT"
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    SEED_MOUNT="$(cygpath -m "$SEED")"
    REPO_CTX="$(cygpath -m "$REPO_ROOT")"
    export MSYS_NO_PATHCONV=1
    ;;
esac

echo "Seed:  $SEED  (refs/main present, $SYMLINK_COUNT symlinks)"
if [ "$SEED_MOUNT" != "$SEED" ]; then
  echo "Mount: $SEED_MOUNT  (MSYS path conversion disabled for docker run)"
fi
echo "Base:  $BASE_IMAGE"
echo "Tag:   $TAG"

# --- Half 1: the Dockerfile ----------------------------------------------
echo
echo "=== 1/3  docker build (Dockerfile.bridge-ready) ==="
ENV_TAG="${TAG}-env"
docker build \
  -f "$REPO_CTX/Dockerfile.bridge-ready" \
  --build-arg "BASE_IMAGE=$BASE_IMAGE" \
  -t "$ENV_TAG" \
  "$REPO_CTX"

# --- Half 2: the seed step, which a Dockerfile cannot express -------------
echo
echo "=== 2/3  seed via runtime bind mount, then commit ==="
CONTAINER="bridge-ready-seed-$$"
# `cp -a` inside the container: the copy is performed by Linux, so the five
# relative symlinks are reproduced as symlinks. `-a` is what preserves them
# and their mtimes; a plain `cp -r` would dereference them and silently
# inflate the image by the size of every duplicated blob.
# THE TWO VALUES BELOW ARE ASSERTED, NOT PRINTED. They were echoed until
# 2026-09-11, and an echo cannot fail: with refs/main absent and zero symlinks
# this step printed "refs/main: " and "symlinks in image: 0" and EXITED 0 --
# measured directly. `set -e` does not fire on a failing `cat` inside a command
# substitution that is an argument to a successful `echo`, and the step's exit
# status is the status of its LAST command, which was that echo. The one
# condition this script exists to prevent was the one it could not report.
#
# The count is compared to the HOST-MEASURED $SYMLINK_COUNT, not to a second
# literal 5: a literal here would be derived from the same place as the check
# above it, and a comparison whose two sides cannot disagree is not a check.
# It arrives as a POSITIONAL PARAMETER -- `sh -c SCRIPT sh ARG` executes only
# SCRIPT and binds the rest as $0 and $1. Getting that wrong is what truncated
# geper:bridge-ready's recorded CMD to its first 82 characters.
docker run --name "$CONTAINER" \
  -v "$SEED_MOUNT":/seed:ro \
  --entrypoint sh "$ENV_TAG" \
  -c 'set -e
      mkdir -p /app/model_cache_seed
      cp -a /seed/. /app/model_cache_seed/
      D=/app/model_cache_seed/models--facebook--esm2_t33_650M_UR50D
      if [ ! -f "$D/refs/main" ]; then
        echo "FATAL: refs/main is absent from the image after the copy." >&2
        exit 1
      fi
      echo "refs/main: $(cat "$D/refs/main")"
      N=$(find /app/model_cache_seed -type l | wc -l)
      echo "symlinks in image: $N (host measured $1 in the seed)"
      if [ "$N" -ne "$1" ]; then
        echo "FATAL: the copy did not preserve the symlinks: $1 in the seed, $N in the image." >&2
        echo "       An image that lost them looks complete and will not load offline." >&2
        exit 1
      fi' sh "$SYMLINK_COUNT"

docker commit "$CONTAINER" "$TAG" >/dev/null
docker rm "$CONTAINER" >/dev/null
echo "committed: $TAG"

# --- Half 3: the acceptance test, which is not a green build --------------
if [ "$SKIP_VERIFY" -eq 1 ]; then
  echo
  echo "SKIPPED verification at your request. NOTE: a build that succeeds and"
  echo "produces an image that cannot load offline is the failure this exists"
  echo "to prevent, so a skipped verification is not a pass."
  exit 0
fi

echo
echo "=== 3/3  acceptance: all four models under --network none ==="
# Run against the IMAGE ID, not the tag. Tags on this floor have moved under
# a build before, and a proof that names a tag proves nothing about the image
# that was actually tested.
IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$TAG")"
echo "image id: $IMAGE_ID"
# No -e PYTHONPATH here on purpose: the image sets it, and passing one from
# outside would test a path the image does not actually ship with. An image
# that only loads offline when the caller supplies the right environment is
# not an offline image.
docker run --rm --network none \
  --entrypoint python "$IMAGE_ID" /app/scripts/verify_offline_models.py
