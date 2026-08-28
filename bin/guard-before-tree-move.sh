#!/usr/bin/env bash
# Manual guard to check for held items before tree-moving operations
# Agents MUST call this before: git checkout, git reset, git rebase, git cherry-pick
#
# Usage: source bin/guard-before-tree-move.sh "<operation>" "<target-ref>" [path ...]
#   e.g. source bin/guard-before-tree-move.sh "checkout" "master"
#        source bin/guard-before-tree-move.sh "mv" "site/product" site/index.html site/product/
#
# SCOPE ARGUMENT (paths, optional):
#   Pass the paths the operation will actually touch. The guard then blocks only if an
#   uncommitted held item is REACHABLE from one of those paths. An operation that cannot
#   reach a held path is not a hazard, so it passes.
#   Pass NO paths and the operation is treated as unbounded (it could rewrite the whole
#   working tree), which is what a bare `git checkout <ref>` actually is -- so it blocks
#   while any held item is uncommitted. That is the guard working, not the guard stuck.
#
# THE SCOPE IS DECLARED BY THE CALLER; THIS GUARD CANNOT VERIFY IT.
#   It has no way to know what command you run after it returns. If you declare `site/`
#   and then run an unscoped checkout, the all-clear does not cover you. Declare the widest
#   set of paths your operation can touch, not the narrowest you hope it will.
#
# NOTE: Must be sourced (source bin/guard...), not run directly (bash bin/guard...).
# Direct execution exits 2 (failure, fails closed). Do not "fix" that into exit 0.
#
# THERE IS NO AUTOMATIC ENFORCEMENT. GIT CANNOT BLOCK A CHECKOUT.
#   git has no pre-checkout hook. post-checkout exists, but it fires only AFTER the working
#   tree has already been rewritten, so no hook can stop a checkout from clobbering a held
#   file. This script is therefore the ONLY coverage there is, and it is coverage only on
#   the runs where somebody actually calls it.
#   A .git/hooks/pre-checkout file used to sit in this repository reading like automatic
#   enforcement of exactly this policy. It had never run and could never run -- measured,
#   not assumed: a pre-checkout hook exiting 1 does not stop `git checkout`, while a
#   post-checkout hook in the same repository does fire. It was removed, because a
#   mechanism that cannot fire is worse than an absent one: an absent guard leaves people
#   careful, and a broken guard lets them proceed with a clean check behind them.
#   Verify this script with `bash bin/guard-tests.sh`. Do not assume something upstream is
#   catching this for you. Nothing is.

OPERATION="${1:-checkout}"
TARGET="${2:-unknown}"
MANIFEST=".held/manifest"
if [ $# -gt 0 ]; then shift; fi
if [ $# -gt 0 ]; then shift; fi
SCOPE_PATHS=("$@")

# Normalise a path for component-wise comparison, or fail closed.
# Prints the normalised path and returns 0; returns 1 when the path cannot be
# reasoned about, in which case the caller must treat the scope as unbounded.
_guard_normalise_path() {
    local p="$1"

    # Pathspec magic (globs, :(exclude), :/, negation) is not something this guard
    # can evaluate. Anything it cannot evaluate is treated as reaching everything.
    case "$p" in
        *'*'*|*'?'*|*'['*|*']'*|:*|!*) return 1 ;;
    esac

    p="${p//\\//}"          # accept Windows-style separators
    while [ "${p#./}" != "$p" ]; do p="${p#./}"; done
    while [ "${p%/}" != "$p" ]; do p="${p%/}"; done

    # Absolute paths and traversal are out of scope for a repo-relative comparison.
    case "$p" in
        /*|[A-Za-z]:*) return 1 ;;
        ..|../*|*/..|*/../*) return 1 ;;
    esac

    # "" and "." both mean the repository root, which reaches every held item.
    if [ -z "$p" ] || [ "$p" = "." ]; then
        printf '%s' "."
        return 0
    fi

    printf '%s' "$p"
    return 0
}

# Is held path $1 reachable from declared scope path $2?
_guard_path_reaches() {
    local held="$1" scope="$2"
    [ "$scope" = "." ] && return 0
    [ "$held" = "$scope" ] && return 0
    # held sits underneath the scope directory
    [ "${held#"$scope"/}" != "$held" ] && return 0
    # the scope sits underneath the held path (held path is itself a directory)
    [ "${scope#"$held"/}" != "$scope" ] && return 0
    return 1
}

# Check if held items are listed in the manifest
if [ ! -f "$MANIFEST" ]; then
    echo "ERROR: $MANIFEST not found. Cannot verify held items." >&2
    return 1
fi

# Strip comments and whitespace, but preserve paths containing '#'
HELD_FILES=$(grep -vE '^[[:space:]]*#' "$MANIFEST" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | grep -v '^$')

if [ -z "$HELD_FILES" ]; then
    echo "ERROR: $MANIFEST is empty or contains only comments. Cannot guard a tree with no recorded held items." >&2
    return 1
fi

# Validate that all manifest entries match actual files (catch typos)
while IFS= read -r item; do
    if ! git ls-files --error-unmatch "$item" >/dev/null 2>&1 && [ ! -e "$item" ]; then
        echo "ERROR: Manifest entry does not match any file: $item" >&2
        echo "This is likely a typo in $MANIFEST. Fix it before proceeding." >&2
        return 1
    fi
done <<< "$HELD_FILES"

# Normalise the declared scope. An operation with no declared paths, or with any path
# this guard cannot evaluate, is unbounded: it is treated as reaching every held item.
GUARD_SCOPE=()
GUARD_UNBOUNDED=0
GUARD_UNBOUNDED_REASON=""
if [ "${#SCOPE_PATHS[@]}" -eq 0 ]; then
    GUARD_UNBOUNDED=1
    GUARD_UNBOUNDED_REASON="no paths were declared, so the operation is assumed to reach the whole working tree"
else
    for raw in "${SCOPE_PATHS[@]}"; do
        if norm=$(_guard_normalise_path "$raw"); then
            GUARD_SCOPE+=("$norm")
        else
            GUARD_UNBOUNDED=1
            GUARD_UNBOUNDED_REASON="path argument cannot be evaluated by this guard: ${raw}"
            break
        fi
    done
fi

# Check whether any held item is BOTH uncommitted AND reachable from the declared scope.
BLOCKED=0
BLOCKED_ITEMS=""
DIRTY_COUNT=0
HELD_COUNT=0
while IFS= read -r item; do
    HELD_COUNT=$((HELD_COUNT + 1))
    # git status --porcelain returns non-empty only for uncommitted changes
    # Matches: " M " (modified), "?? " (untracked), etc.
    if [ -z "$(git status --porcelain -- "$item" 2>/dev/null)" ]; then
        continue
    fi
    DIRTY_COUNT=$((DIRTY_COUNT + 1))

    if [ "$GUARD_UNBOUNDED" -eq 1 ]; then
        BLOCKED=1
        BLOCKED_ITEMS="${BLOCKED_ITEMS}${item}"$'\n'
        continue
    fi

    if held_norm=$(_guard_normalise_path "$item"); then
        :
    else
        # A held path this guard cannot normalise must never be reasoned away.
        BLOCKED=1
        BLOCKED_ITEMS="${BLOCKED_ITEMS}${item} (manifest path cannot be evaluated; blocking)"$'\n'
        continue
    fi

    for scope in "${GUARD_SCOPE[@]}"; do
        if _guard_path_reaches "$held_norm" "$scope"; then
            BLOCKED=1
            BLOCKED_ITEMS="${BLOCKED_ITEMS}${item} (reachable from declared path: ${scope})"$'\n'
            break
        fi
    done
done <<< "$HELD_FILES"

if [ "$BLOCKED" -eq 1 ]; then
    echo "GUARD ERROR: $OPERATION blocked by uncommitted held items" >&2
    echo "Items with uncommitted changes that this operation can reach:" >&2
    echo "$BLOCKED_ITEMS" | sed 's/^/  /' >&2
    if [ "$GUARD_UNBOUNDED" -eq 1 ]; then
        echo "Scope: UNBOUNDED -- ${GUARD_UNBOUNDED_REASON}." >&2
        echo "If this operation really cannot touch the held paths, re-run the guard passing" >&2
        echo "the paths it will touch, e.g.: source bin/guard-before-tree-move.sh \"$OPERATION\" \"$TARGET\" some/dir/" >&2
    fi
    echo "" >&2
    echo "Before running git $OPERATION $TARGET, either:" >&2
    echo "  1. Commit your held items" >&2
    echo "  2. Move them to a temporary location and remove from $MANIFEST" >&2
    echo "  3. Narrow the operation so it cannot reach the held paths, and declare those paths" >&2
    return 1
fi

# Say what was actually examined. A clean result that does not name its subject cannot
# tell you it examined nothing.
echo "GUARD OK: $OPERATION $TARGET -- ${HELD_COUNT} held item(s) checked, ${DIRTY_COUNT} uncommitted, none reachable."
if [ "${#GUARD_SCOPE[@]}" -gt 0 ]; then
    echo "GUARD OK: declared scope: ${GUARD_SCOPE[*]}"
    echo "GUARD OK: this scope was DECLARED, not measured -- it does not cover an operation broader than those paths."
fi
return 0
