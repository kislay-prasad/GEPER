#!/usr/bin/env bash
# Tests for bin/guard-before-tree-move.sh
#
# Run from the repository root:   bash bin/guard-tests.sh
# Exits 0 when every expectation is met, 1 otherwise.
#
# WHY THIS FILE EXISTS: this guard has now shipped four times, and three of the first
# three versions were wrong in a way that READ AS WORKING --
#   v1  could never fire at all (checked an empty .held/ directory)          -> silent all-clear
#   v2  tested file EXISTENCE, so it could never stop blocking               -> permanent block
#   v3  a trailing space on a manifest entry produced "safe to proceed"      -> silent all-clear
#   v4  blocked operations that could not reach a held path                  -> permanent block
#   v5  validated every entry on every call, so a manifest naming an untracked
#       held item blocked EVERY operation in EVERY other clone                -> permanent block
#       ...and `git status --porcelain` reported an ignored held item as
#       nothing at all, which the guard read as clean                         -> silent all-clear
# Every one of those was found by running the guard, not by reading it. Do the same:
# if you change the guard, make it go red on purpose in BOTH directions before trusting it.
#
# The suite runs on its OWN fixtures rather than on whatever happens to be dirty in the
# working tree today, so it stays meaningful after the held items are finally committed.
# The few checks that do read the real manifest state their precondition and SKIP loudly
# rather than passing silently when it does not hold.

set -u

GUARD="${GUARD_PATH:-bin/guard-before-tree-move.sh}"
REPO="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$REPO" ]; then echo "FATAL: not inside a git repository" >&2; exit 1; fi
cd "$REPO" || exit 1
if [ ! -f "$GUARD" ]; then echo "FATAL: guard not found at $GUARD" >&2; exit 1; fi

TMPDIR_REL=".guard-test-tmp"
cleanup() { rm -rf "$REPO/$TMPDIR_REL"; }
trap cleanup EXIT
rm -rf "$TMPDIR_REL"; mkdir -p "$TMPDIR_REL/man"
FIXDIR="$TMPDIR_REL/nested"
mkdir -p "$FIXDIR"

# A probe copy of the guard with EXACTLY ONE LINE changed -- the manifest path -- so the
# suite can point it at synthetic manifests without ever editing .held/manifest.
PROBE="$TMPDIR_REL/guard_probe.sh"
sed 's|^MANIFEST=".held/manifest"$|MANIFEST="$GUARD_TEST_MANIFEST"|' "$GUARD" > "$PROBE"
if [ "$(diff "$GUARD" "$PROBE" | grep -c '^[<>]')" -ne 2 ]; then
    echo "FATAL: probe differs from the guard by more than the MANIFEST line:" >&2
    diff "$GUARD" "$PROBE" >&2
    exit 1
fi

# Fixtures. DIRTY_FIXTURE is untracked, so git reports it as '??' -- the same state the
# real untracked held item is in, and the state a naive `git diff` check would miss.
DIRTY_FIXTURE="$FIXDIR/dirty_fixture.tmp"
echo "uncommitted content" > "$DIRTY_FIXTURE"

# PRECONDITION, ASSERTED RATHER THAN ASSUMED. Every "must block" case below depends on git
# reporting this fixture as uncommitted. If it does not -- most likely because someone added
# .guard-test-tmp/ to .gitignore -- then `git status --porcelain` returns EMPTY for it, the
# guard reads empty as clean, and the whole suite would go GREEN BY GOING BLIND. Do NOT
# gitignore this directory; the trap above removes it, and an untracked leftover is visible
# litter rather than a silent hole in the tests.
if [ -n "$(git check-ignore "$DIRTY_FIXTURE" 2>/dev/null)" ] \
   || [ -z "$(git status --porcelain -- "$DIRTY_FIXTURE")" ]; then
    echo "FATAL: git does not report the suite's own fixture as uncommitted." >&2
    echo "  fixture: $DIRTY_FIXTURE" >&2
    echo "  Is .guard-test-tmp/ gitignored? An ignored path is invisible to git status," >&2
    echo "  so every blocking case here would pass for the wrong reason. Refusing to run." >&2
    exit 1
fi
CLEAN_FIXTURE="$(git ls-files | while IFS= read -r f; do
    if [ -z "$(git status --porcelain -- "$f")" ]; then echo "$f"; break; fi
done)"
if [ -z "$CLEAN_FIXTURE" ]; then echo "FATAL: no committed-clean tracked file to test with" >&2; exit 1; fi

# A second fixture that git will report as IGNORED, used by section 9. The ignore is applied
# through a private excludes file passed per-command, so .gitignore is never touched -- and
# so the main fixture in section 1 stays visible, which the precondition above requires.
IGNORED_FIXTURE="$FIXDIR/ignored_fixture.tmp"
echo "ignored content" > "$IGNORED_FIXTURE"
EXCL="$TMPDIR_REL/excludes"
printf 'ignored_fixture.tmp\n' > "$EXCL"
if [ -z "$(GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.excludesFile GIT_CONFIG_VALUE_0="$EXCL" \
           git status --porcelain --ignored -- "$IGNORED_FIXTURE" 2>/dev/null | grep '^!!')" ]; then
    echo "FATAL: could not make a fixture gitignored, so section 9 would test nothing." >&2
    exit 1
fi

FAILED=0
PASSED=0
SKIPPED=0

run_probe() {  # run_probe <manifest> <args...>  -> prints exit code
    local man="$1"; shift
    ( GUARD_TEST_MANIFEST="$man" bash -c 'source "$1" "${@:2}" >/dev/null 2>&1; echo $?' _ "$PROBE" "$@" )
}

expect() {  # expect <manifest> <rc> <label> -- <args...>
    local man="$1"; shift
    local want="$1"; shift
    local label="$1"; shift
    shift  # the literal --
    local got; got=$(run_probe "$man" "$@")
    if [ "$got" = "$want" ]; then
        PASSED=$((PASSED + 1)); printf '  ok    rc=%s  %s\n' "$got" "$label"
    else
        FAILED=$((FAILED + 1)); printf '  FAIL  rc=%s want=%s  %s\n' "$got" "$want" "$label"
    fi
}

expect_ig() {  # like expect, but runs with IGNORED_FIXTURE gitignored
    local man="$1"; shift
    local want="$1"; shift
    local label="$1"; shift
    shift  # the literal --
    local got
    got=$( GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.excludesFile GIT_CONFIG_VALUE_0="$EXCL" \
           GUARD_TEST_MANIFEST="$man" bash -c 'source "$1" "${@:2}" >/dev/null 2>&1; echo $?' _ "$PROBE" "$@" )
    if [ "$got" = "$want" ]; then
        PASSED=$((PASSED + 1)); printf '  ok    rc=%s  %s\n' "$got" "$label"
    else
        FAILED=$((FAILED + 1)); printf '  FAIL  rc=%s want=%s  %s\n' "$got" "$want" "$label"
    fi
}

M="$TMPDIR_REL/man"
printf '%s\n' "$DIRTY_FIXTURE"                  > "$M/dirty"
printf '%s\n' "$CLEAN_FIXTURE"                  > "$M/clean"
printf '%s\n%s\n' "$CLEAN_FIXTURE" "$DIRTY_FIXTURE" > "$M/mixed"
printf '# only comments\n'                      > "$M/comments_only"
printf '   \n'                                  > "$M/whitespace_line"
printf '  # indented comment\n'                 > "$M/indented_comment"
printf '%s \n' "$DIRTY_FIXTURE"                 > "$M/trailing_space"
printf '%s\n' "no/such/file/anywhere.py"        > "$M/typo"
printf './%s\n' "$DIRTY_FIXTURE"                > "$M/dotslash"
printf '%s\n' "${DIRTY_FIXTURE//\//\\}"         > "$M/backslash"
printf '%s\n' "$FIXDIR"                         > "$M/dir_entry"
printf '%s\n%s\n' "$CLEAN_FIXTURE" "no/such/file/anywhere.py" > "$M/fresh_clone_shape"
printf '%s\n' "$IGNORED_FIXTURE"                > "$M/ignored"
printf '%s\n%s\n' "$DIRTY_FIXTURE" "$IGNORED_FIXTURE" > "$M/dirty_plus_ignored"

echo "== 1. THE CASE THE GUARD EXISTS FOR: a reachable uncommitted held item BLOCKS =="
expect "$M/dirty" 1 "bare call, no declared paths (operation is unbounded)"                 -- checkout master
expect "$M/dirty" 1 "scope is the held file itself"                                         -- checkout master "$DIRTY_FIXTURE"
expect "$M/dirty" 1 "scope is the directory containing the held file"                       -- checkout master "$FIXDIR/"
expect "$M/dirty" 1 "scope is an ancestor directory"                                        -- checkout master "$TMPDIR_REL"
expect "$M/dirty" 1 "scope is the repository root as ."                                     -- reset "--hard HEAD~1" .
expect "$M/dirty" 1 "rebase with no declared paths"                                         -- rebase origin/master
expect "$M/dirty" 1 "cherry-pick with no declared paths"                                    -- cherry-pick abc1234
expect "$M/mixed" 1 "mixed clean+dirty manifest, bare call"                                 -- checkout master

echo "== 2. FAIL CLOSED: a scope this guard cannot evaluate is treated as reaching all =="
expect "$M/dirty" 1 "glob in the declared path"                                             -- checkout master "some*"
expect "$M/dirty" 1 "pathspec magic"                                                        -- checkout master ":(exclude)kim_pipeline"
expect "$M/dirty" 1 "parent traversal"                                                      -- checkout master "../GEPER/site"
expect "$M/dirty" 1 "posix absolute path"                                                   -- checkout master "/c/Users/kisla/GEPER/site"
expect "$M/dirty" 1 "windows absolute path"                                                 -- checkout master 'C:\Users\kisla\GEPER\site'
expect "$M/dirty" 1 "one safe path alongside one unevaluable path"                          -- checkout master "site/" "kim_*"

echo "== 3. THE v4 FIX: an operation that CANNOT reach a held path passes =="
expect "$M/dirty" 0 "scope is an unrelated subtree"                                         -- checkout master "site/"
expect "$M/dirty" 0 "the dispatched rename: git mv site/*.html site/product/"               -- mv site/product site/index.html site/about.html site/style.css site/product/
expect "$M/dirty" 0 "scope is a sibling file in the same directory"                         -- checkout master "$FIXDIR/some_other_file.tmp"
expect "$M/dirty" 0 "prefix match is component-wise, not string-wise"                       -- checkout master "${FIXDIR}_other/"
expect "$M/dirty" 0 "leading ./ is normalised"                                              -- checkout master "./site"
expect "$M/dirty" 0 "windows separators inside a relative path"                             -- checkout master 'site\product'
expect "$M/mixed" 0 "mixed clean+dirty manifest, scope cannot reach the dirty one"          -- checkout master "site/"

echo "== 4. SELF-CLEARING (v2): a committed-clean held item never blocks =="
expect "$M/clean" 0 "clean held item, bare call"                                            -- checkout master
expect "$M/clean" 0 "clean held item, scope is the item itself"                             -- checkout master "$CLEAN_FIXTURE"

echo "== 5. A BROKEN MANIFEST IS LOUD, NOT A QUIET ALL-CLEAR (v3) =="
expect "$M/nonexistent"      1 "manifest file missing, even with a narrow scope"             -- checkout master "site/"
expect "$M/comments_only"    1 "manifest holds only comments"                                -- checkout master "site/"
expect "$M/whitespace_line"  1 "manifest holds a whitespace-only line"                       -- checkout master "site/"
expect "$M/indented_comment" 1 "manifest holds only an indented comment"                     -- checkout master "site/"
expect "$M/trailing_space"   1 "manifest entry has a trailing space, reachable scope"        -- checkout master "$FIXDIR/"
expect "$M/trailing_space"   1 "manifest entry has a trailing space, bare call"              -- checkout master

echo "== 6. A HELD PATH MUST NOT SLIP THROUGH ON THE FORM IT IS WRITTEN IN =="
expect "$M/dotslash"   1 "manifest entry written with a leading ./"                          -- checkout master "$FIXDIR/"
expect "$M/backslash"  1 "manifest entry written with backslashes"                           -- checkout master "$FIXDIR/"
expect "$M/dir_entry"  1 "manifest entry is a directory, scope is a file inside it"          -- checkout master "$DIRTY_FIXTURE"
expect "$M/dir_entry"  0 "same directory entry, scope cannot reach it"                       -- checkout master "site/"

echo "== 8. VALIDATION IS GATED ON REACHABILITY, DEFERRED BUT NEVER SKIPPED =="
# An untracked held item exists only in the checkout holding it, so every other clone is
# missing it by definition. Validating every entry on every call therefore blocked every
# operation everywhere else, permanently. The fix examines an entry only when the operation
# could reach it -- and an unscoped operation reaches everything, so the typo check that
# found the v3 silent all-clear still fires exactly where it has to.
expect "$M/typo"              0 "unverifiable entry, scope cannot reach it -> not examined"   -- checkout master "site/"
expect "$M/typo"              1 "unverifiable entry, UNSCOPED call -> still caught, loudly"   -- checkout master
expect "$M/typo"              1 "unverifiable entry, scope CAN reach it -> caught"            -- checkout master "no/such/"
expect "$M/typo"              1 "unverifiable entry, scope is the repo root"                  -- checkout master "."
expect "$M/typo"              1 "unverifiable entry, unevaluable scope (fails closed)"        -- checkout master "some*"
expect "$M/fresh_clone_shape" 0 "THE FRESH-CLONE SHAPE: clean file + absent held file, scoped elsewhere" -- mv site/product site/index.html site/product/
expect "$M/fresh_clone_shape" 1 "same manifest, unscoped call -> blocks on the absent entry"  -- checkout master

echo "== 9. AN IGNORED HELD ITEM IS A HAZARD, NOT A CLEAN RESULT =="
# `git status --porcelain` prints NOTHING for an ignored path, exactly as for a clean one,
# so before --ignored the guard reported "safe to proceed" for a gitignored held file.
# An ignored held item is untracked and unversioned, so a clobbered copy is unrecoverable.
expect_ig "$M/ignored"           1 "gitignored held item, reachable scope -> blocks"          -- checkout master "$FIXDIR/"
expect_ig "$M/ignored"           1 "gitignored held item, unscoped call -> blocks"            -- checkout master
expect_ig "$M/ignored"           0 "gitignored held item, scope cannot reach it -> passes"    -- checkout master "site/"
expect_ig "$M/dirty_plus_ignored" 1 "dirty item plus ignored item, both reachable"            -- checkout master "$FIXDIR/"

echo "== 7. THE REAL MANIFEST (precondition-gated, so it cannot pass by not applying) =="
if [ ! -f .held/manifest ]; then
    echo "  SKIP  .held/manifest does not exist"; SKIPPED=$((SKIPPED + 1))
else
    REAL_DIRTY=0
    REAL_ABSENT=0
    while IFS= read -r item; do
        [ -z "$item" ] && continue
        case "$item" in \#*) continue ;; esac
        if ! git ls-files --error-unmatch "$item" >/dev/null 2>&1 && [ ! -e "$item" ]; then
            REAL_ABSENT=$((REAL_ABSENT + 1))
        elif [ -n "$(git status --porcelain --ignored -- "$item" 2>/dev/null)" ]; then
            REAL_DIRTY=$((REAL_DIRTY + 1))
        fi
    done < <(grep -vE '^[[:space:]]*#' .held/manifest | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | grep -v '^$')

    if [ "$REAL_DIRTY" -gt 0 ]; then
        expect ".held/manifest" 1 "real manifest, bare call ($REAL_DIRTY item(s) uncommitted right now)" -- checkout master
    elif [ "$REAL_ABSENT" -gt 0 ]; then
        # KNOWN RESIDUAL, ASSERTED SO IT CANNOT BE MISTAKEN FOR A BUG OR QUIETLY LOST.
        # This is what a fresh clone looks like: $REAL_ABSENT manifest entry/entries name a
        # held item that exists only in the checkout holding it. The guard cannot tell that
        # apart from a misspelling, so an UNSCOPED call still blocks here. Scoped operations
        # pass, which is what makes the clone usable. Closing this needs the manifest to say
        # which entries are expected to be untracked -- a format change, not a code change.
        echo "  NOTE  fresh-clone shape: $REAL_ABSENT held entry/entries absent from this checkout"
        expect ".held/manifest" 1 "real manifest, bare call (absent entry -> unscoped still blocks: KNOWN RESIDUAL)" -- checkout master
        expect ".held/manifest" 0 "real manifest, SCOPED elsewhere -> passes, which is the point of the fix" -- checkout master "site/"
    else
        echo "  SKIP  every real held item is committed clean, so a block here would be wrong"
        SKIPPED=$((SKIPPED + 1))
        expect ".held/manifest" 0 "real manifest, bare call (all held items clean, so it must pass)" -- checkout master
    fi
fi

echo
echo "passed=$PASSED failed=$FAILED skipped=$SKIPPED"
if [ "$FAILED" -ne 0 ]; then echo "GUARD TESTS: FAILED"; exit 1; fi
echo "GUARD TESTS: OK"
exit 0
