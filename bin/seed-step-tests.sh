#!/usr/bin/env bash
# DEMONSTRATION, NOT A JUSTIFICATION. The card's standard: for every check kept,
# produce an actual case where it came back dirty.
#
# NO DOCKER IS INVOLVED and none is needed: the defect and the fix are both SHELL
# SEMANTICS -- whether the step's last command can carry a failure out. The `sh -c`
# bodies below are copied from the script with the container paths rebased onto a
# temp tree; `docker run --entrypoint sh IMAGE -c 'SCRIPT' sh N` runs exactly this.
#
# *** STATED LIMIT, MEASURED NOT ASSUMED: THIS BOX CANNOT CREATE SYMLINKS. ***
# Git Bash `ln -s` needs Windows Developer Mode or admin; with
# MSYS=winsymlinks:nativestrict it fails "Operation not permitted", and without it
# it silently copies the file, so `find -type l` counts zero either way. Every
# fixture below therefore holds ZERO symlinks, and the comparison is exercised by
# varying THE HOST COUNT instead. That still drives both directions -- equal and
# unequal -- which is the whole of what the check does.
# The first version of this file ignored that and "built" 5-link fixtures that were
# empty; the good-case assertion failed and read like a broken fix.
# A FIXTURE THAT SILENTLY FAILS TO BUILD ACCUSES THE CODE UNDER TEST.
set -u
W="$(mktemp -d)"
trap 'rm -rf "$W"' EXIT
D_REL=models--facebook--esm2_t33_650M_UR50D

# *** DRIFT DETECTOR. *** The step under test is embedded in a `docker run -c` string,
# so this file necessarily holds a COPY of it. A copy that drifts from the original
# is a suite that passes while testing something nobody ships. These three lines are
# the whole of the fix; if any of them stops appearing in the script, say so LOUDLY
# rather than going green against a stale transcription.
SCRIPT="${BUILD_SCRIPT:-scripts/build_bridge_ready.sh}"
if [ ! -f "$SCRIPT" ]; then
  echo "FATAL: $SCRIPT not found -- run this from the repository root." >&2
  exit 2
fi
for needle in \
  'if [ ! -f "$D/refs/main" ]; then' \
  'N=$(find /app/model_cache_seed -type l | wc -l)' \
  'if [ "$N" -ne "$1" ]; then'
do
  if ! grep -qF -- "$needle" "$SCRIPT"; then
    echo "FATAL: the step this suite tests is no longer in $SCRIPT:" >&2
    echo "       missing: $needle" >&2
    echo "       Update both, or this suite is green against a copy nobody runs." >&2
    exit 2
  fi
done
# And the shape the fix replaced must be GONE, not merely joined:
if grep -qF -- 'echo "symlinks in image: $(find /app/model_cache_seed -type l | wc -l)"' "$SCRIPT"; then
  echo "FATAL: the unchecked echo is back in $SCRIPT -- the count is printed, not asserted." >&2
  exit 2
fi

fixture() {  # fixture <refs-main:yes|no>
  rm -rf "$W/seed" "$W/app"
  mkdir -p "$W/seed/$D_REL/refs" "$W/seed/$D_REL/blobs" "$W/seed/$D_REL/snapshots/deadbeef" "$W/app"
  [ "$1" = yes ] && echo deadbeef > "$W/seed/$D_REL/refs/main"
  built=$(find "$W/seed" -type l | wc -l)
  if [ "$built" -ne 0 ]; then
    echo "FATAL: fixture expected 0 symlinks (this box cannot make them) but found $built." >&2
    exit 2
  fi
  return 0
}

# --- THE STEP AS IT SHIPPED UNTIL TODAY -------------------------------------
old_step() {
  sh -c 'set -e
      mkdir -p '"$W"'/app/model_cache_seed
      cp -a '"$W"'/seed/. '"$W"'/app/model_cache_seed/
      D='"$W"'/app/model_cache_seed/'"$D_REL"'
      echo "refs/main: $(cat "$D/refs/main")"
      echo "symlinks in image: $(find '"$W"'/app/model_cache_seed -type l | wc -l)"'
}

# --- THE STEP AFTER THE FIX --------------------------------------------------
new_step() {  # new_step <host-measured-count>
  sh -c 'set -e
      mkdir -p '"$W"'/app/model_cache_seed
      cp -a '"$W"'/seed/. '"$W"'/app/model_cache_seed/
      D='"$W"'/app/model_cache_seed/'"$D_REL"'
      if [ ! -f "$D/refs/main" ]; then
        echo "FATAL: refs/main is absent from the image after the copy." >&2
        exit 1
      fi
      echo "refs/main: $(cat "$D/refs/main")"
      N=$(find '"$W"'/app/model_cache_seed -type l | wc -l)
      echo "symlinks in image: $N (host measured $1 in the seed)"
      if [ "$N" -ne "$1" ]; then
        echo "FATAL: the copy did not preserve the symlinks: $1 in the seed, $N in the image." >&2
        exit 1
      fi' sh "$1"
}

# --- A MUTANT OF THE FIX: the literal that would have been the easy way -------
# Written to show the difference is real and not a matter of taste.
literal_step() {  # literal_step <host-measured-count, IGNORED as the literal wins>
  sh -c 'set -e
      mkdir -p '"$W"'/app/model_cache_seed
      cp -a '"$W"'/seed/. '"$W"'/app/model_cache_seed/
      D='"$W"'/app/model_cache_seed/'"$D_REL"'
      if [ ! -f "$D/refs/main" ]; then exit 1; fi
      N=$(find '"$W"'/app/model_cache_seed -type l | wc -l)
      if [ "$N" -ne 5 ]; then exit 1; fi' sh "$1"
}

pass=0; fail=0
expect() {  # expect <name> <wanted-rc> <got-rc>
  if [ "$2" = "$3" ]; then echo "  PASS  $1 (rc=$3)"; pass=$((pass+1))
  else echo "  FAIL  $1 -- wanted rc=$2, got rc=$3"; fail=$((fail+1)); fi
}

echo "=== THE DEFECT: THE SHIPPED STEP CANNOT REPORT THE FAILURE IT EXISTS FOR ==="
fixture no
echo "  a copy that lost EVERYTHING -- no refs/main, zero symlinks:"
old_step 2>/dev/null | sed 's/^/    /'
fixture no; old_step >/dev/null 2>&1
expect "*** the OLD step exits 0 on a totally failed copy ***" 0 $?
fixture yes; old_step >/dev/null 2>&1
expect "and exits 0 on a symlink-stripped copy too" 0 $?

echo
echo "=== THE FIX, DRIVEN INTO FAILURE ==="
fixture no;  new_step 0 >/dev/null 2>&1; expect "missing refs/main is fatal"                  1 $?
fixture yes; new_step 5 >/dev/null 2>&1; expect "0 symlinks against a host count of 5"        1 $?
fixture yes; new_step 3 >/dev/null 2>&1; expect "a PARTIAL loss (host 3, image 0) is fatal"   1 $?
fixture yes; new_step 1 >/dev/null 2>&1; expect "even a single lost link is fatal"            1 $?

echo
echo "=== AND IT STILL PASSES WHEN THE COUNTS AGREE (a check that always fires is also useless) ==="
fixture yes; new_step 0 >/dev/null 2>&1; expect "image count equals the host count" 0 $?
fixture yes; echo "  output on the passing path:"; new_step 0 2>&1 | sed 's/^/    /'

echo
echo "=== *** AND IT IS THE HOST MEASUREMENT, NOT A LITERAL 5 *** ==="
fixture yes; literal_step 0 >/dev/null 2>&1; LIT=$?
fixture yes; new_step 0 >/dev/null 2>&1; NEW=$?
expect "the LITERAL-5 mutant FAILS a seed the host measured as 0" 1 "$LIT"
expect "the host-measured version PASSES the same seed"          0 "$NEW"
echo "        The two disagree on identical input, so the choice is not cosmetic:"
echo "        a literal there is a constant derived from the same place as the host"
echo "        check above it -- a comparison whose sides cannot disagree about the"
echo "        thing that actually matters, which is whether the COPY preserved them."

echo
echo "$pass passed, $fail failed."
[ "$fail" -eq 0 ] || exit 1
