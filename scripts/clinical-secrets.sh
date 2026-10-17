#!/bin/sh
# scripts/clinical-secrets.sh
# ───────────────────────────
# Creates, and checks, the secret files that docker-compose.clinical.yml mounts.
#
#   scripts/clinical-secrets.sh                 create whatever is missing
#   scripts/clinical-secrets.sh --check         verify; change nothing
#   scripts/clinical-secrets.sh --dir /path     use another directory (default: ./secrets)
#   scripts/clinical-secrets.sh --db-host H --db-name N
#                                               host/database written into the two DSNs
#                                               (defaults: clinical-db, clinical)
#
# GUARANTEES, each of which is tested (clinical/tests/test_w171_secrets_script.py):
#   * IT NEVER OVERWRITES. A file that already exists is left exactly as it is,
#     including its mode; the script says "kept". Rotating a secret is a
#     deliberate act: delete that one file and run the script again. Files are
#     created with `set -C` (noclobber), so even a race cannot replace one.
#   * IT NEVER PRINTS A SECRET. Output names files and statuses, never contents.
#   * IT CREATES NOTHING WORLD-READABLE. umask 077 for the directory (0700) and
#     the files (0600), and the script confirms the mode afterwards.
#   * IT REFUSES TO WRITE WHERE GIT WOULD TRACK THE RESULT. If the directory is
#     inside a git work tree and is not ignored, or already holds tracked files,
#     it stops before creating anything. Outside a work tree (or with no git
#     installed) it says so and carries on; --check reports the same.
#   * IT NEEDS NOTHING BUT POSIX tools: sh, head, od, base64, tr, stat, chmod.
#
# THE FILES (names are what the Compose file and the container entrypoint use):
#   postgres_password             bundled database's superuser password
#   clinical_app_password         clinical_app login role (set by the init job)
#   clinical_retention_password   clinical_retention login role (set by the init job)
#   clinical_first_admin_password the first administrator; delete after provisioning
#   clinical_totp_secret_keys     Fernet key list that encrypts stored MFA secrets
#   clinical_dsn                  postgresql://clinical_app:<pw>@HOST:5432/NAME
#   clinical_bootstrap_dsn        postgresql://postgres:<pw>@HOST:5432/NAME   (init job only)
#
# USING AN EXTERNALLY MANAGED POSTGRESQL: replace clinical_dsn and
# clinical_bootstrap_dsn with the real connection strings before the first
# `up`, and leave postgres_password unused. Percent-encode any reserved
# character in a password you choose yourself; generated values are hex and need
# no escaping.
#
# ON FILE OWNERSHIP. Docker Compose ignores uid/gid/mode on file secrets (it
# bind-mounts the host file as it is), so the container user must be able to
# read a 0600 file by being its owner. Set CLINICAL_UID/CLINICAL_GID in
# .env.clinical to the owner's ids (`id -u`, `id -g`); --check prints them.
set -eu

DIR="${CLINICAL_SECRETS_DIR:-secrets}"
DB_HOST="clinical-db"
DB_NAME="clinical"
MODE="create"

say() { printf '%s\n' "$*"; }
die() { printf 'clinical-secrets: %s\n' "$*" >&2; exit "${2:-1}"; }

usage() {
    cat <<'USAGE'
usage: clinical-secrets.sh [--check] [--dir DIR] [--db-host HOST] [--db-name NAME]

  (no flags)  create whatever secret files are missing; never overwrite or print
  --check     verify the files and change nothing
  --dir DIR   secrets directory (default: ./secrets, or $CLINICAL_SECRETS_DIR)
  --db-host   host written into the two connection strings (default: clinical-db)
  --db-name   database written into the two connection strings (default: clinical)
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --check) MODE="check" ;;
        --dir) [ $# -ge 2 ] || die "--dir needs a value" 2; DIR="$2"; shift ;;
        --db-host) [ $# -ge 2 ] || die "--db-host needs a value" 2; DB_HOST="$2"; shift ;;
        --db-name) [ $# -ge 2 ] || die "--db-name needs a value" 2; DB_NAME="$2"; shift ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; die "unknown argument: $1" 2 ;;
    esac
    shift
done

# The host and database name are written into connection strings, so they are
# restricted to characters that cannot change the shape of one.
case "$DB_HOST$DB_NAME" in
    *[!A-Za-z0-9._-]*) die "--db-host/--db-name may contain only letters, digits, '.', '_' and '-'" 2 ;;
esac
[ -n "$DB_HOST" ] && [ -n "$DB_NAME" ] || die "--db-host/--db-name must not be empty" 2

PASSWORD_FILES="postgres_password clinical_app_password clinical_retention_password clinical_first_admin_password"
ALL_FILES="$PASSWORD_FILES clinical_totp_secret_keys clinical_dsn clinical_bootstrap_dsn"

mode_of() { stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1" 2>/dev/null || echo unknown; }
owner_of() { stat -c '%u' "$1" 2>/dev/null || stat -f '%u' "$1" 2>/dev/null || echo unknown; }

rand_hex() { head -c "$1" /dev/urandom | od -An -v -tx1 | tr -d ' \n'; }
# Fernet: 32 random bytes, URL-safe base64 with padding = 44 characters.
rand_fernet() { head -c 32 /dev/urandom | base64 | tr -d '\n' | tr '+/' '-_'; }

# ── git: never create secrets where they could be committed ────────────────────
# prints one of: ignored | not-ignored | tracked | outside | no-git
git_state() {
    command -v git >/dev/null 2>&1 || { echo no-git; return; }
    tracked=$(git ls-files -- "$DIR" 2>/dev/null || true)
    if [ -n "$tracked" ]; then echo tracked; return; fi
    set +e
    git check-ignore -q -- "$DIR/clinical_dsn" 2>/dev/null
    rc=$?
    set -e
    case "$rc" in
        0) echo ignored ;;
        1) echo not-ignored ;;
        *) echo outside ;;
    esac
}

# Does this filesystem honour POSIX modes at all? (NTFS under Git Bash does not.)
modes_enforced() {
    probe="$DIR/.mode-probe.$$"
    ( umask 077; : > "$probe" ) 2>/dev/null || return 1
    chmod 640 "$probe" 2>/dev/null || true
    got=$(mode_of "$probe")
    rm -f "$probe"
    [ "$got" = "640" ]
}

# Verify without ever reading a secret out loud. Prints a line per finding.
# Returns the number of FAILs through $FAILS.
FAILS=0
ok()   { say "  ok          $*"; }
fail() { say "  FAIL        $*"; FAILS=$((FAILS + 1)); }
unver() { say "  UNVERIFIED  $*"; }

verify() {
    FAILS=0
    say "directory: $DIR"
    [ -d "$DIR" ] || { fail "directory does not exist"; return; }

    if modes_enforced; then
        enforced=yes
        dm=$(mode_of "$DIR")
        if [ "$dm" = "700" ]; then ok "directory mode 700"; else fail "directory mode is $dm, expected 700"; fi
    else
        enforced=no
        unver "this filesystem does not report POSIX modes, so file permissions cannot be checked here"
    fi

    case "$(git_state)" in
        ignored) ok "ignored by git" ;;
        tracked) fail "git TRACKS files in this directory" ;;
        not-ignored) fail "inside a git work tree and NOT ignored by git" ;;
        outside) say "  note        directory is outside this git work tree" ;;
        no-git) unver "git is not installed; cannot confirm the directory is ignored" ;;
    esac

    owners=""
    for name in $ALL_FILES; do
        f="$DIR/$name"
        if [ ! -f "$f" ]; then fail "$name is missing"; continue; fi
        if [ ! -s "$f" ]; then fail "$name is empty"; continue; fi
        if [ -L "$f" ]; then fail "$name is a symlink"; continue; fi
        lines=$(wc -l < "$f" | tr -d ' ')
        if [ "$lines" -gt 1 ]; then fail "$name has more than one line"; continue; fi
        if [ "$enforced" = yes ]; then
            fm=$(mode_of "$f")
            case "$fm" in
                600 | 400) ok "$name mode $fm" ;;
                *) fail "$name mode is $fm, expected 600 (or 400)" ;;
            esac
        fi
        owners="$owners $(owner_of "$f")"
    done

    # Content shape and cross-file consistency: still no secret is printed.
    if [ -s "$DIR/clinical_totp_secret_keys" ]; then
        # Every comma-separated entry must look like a Fernet key.
        bad=0
        for key in $(tr ',' ' ' < "$DIR/clinical_totp_secret_keys"); do
            printf '%s' "$key" | grep -Eq '^[A-Za-z0-9_-]{43}=$' || bad=1
        done
        if [ "$bad" = 0 ]; then ok "clinical_totp_secret_keys has the shape of Fernet keys"; else fail "clinical_totp_secret_keys is not a list of Fernet keys"; fi
    fi
    for pair in "clinical_dsn:clinical_app_password" "clinical_bootstrap_dsn:postgres_password"; do
        dsn_file="$DIR/${pair%%:*}"
        pw_file="$DIR/${pair##*:}"
        [ -s "$dsn_file" ] && [ -s "$pw_file" ] || continue
        if ! grep -Eq '^postgresql://[^:@/ ]+:[^@ ]+@[A-Za-z0-9._-]+(:[0-9]+)?/[A-Za-z0-9._-]+$' "$dsn_file"; then
            fail "${pair%%:*} is not of the form postgresql://user:password@host[:port]/database"
        elif grep -qF -f "$pw_file" "$dsn_file"; then  # -f: the password never appears on a command line
            ok "${pair%%:*} agrees with ${pair##*:}"
        else
            say "  note        ${pair%%:*} does not contain ${pair##*:} (expected when using an external database)"
        fi
    done

    first=""
    for o in $owners; do first="$o"; break; done
    if [ -n "$first" ] && [ "$first" != unknown ]; then
        say "  note        files are owned by uid $first: set CLINICAL_UID=$first (and CLINICAL_GID) in .env.clinical so the container user can read them"
    fi
}

# ── check mode ─────────────────────────────────────────────────────────────────
if [ "$MODE" = check ]; then
    verify
    if [ "$FAILS" -gt 0 ]; then say "result: $FAILS problem(s)"; exit 1; fi
    say "result: no problems found"
    exit 0
fi

# ── create mode ────────────────────────────────────────────────────────────────
for tool in head od tr base64 chmod; do
    command -v "$tool" >/dev/null 2>&1 || die "required tool not found: $tool"
done
[ -r /dev/urandom ] || die "/dev/urandom is not readable; cannot generate secrets"

case "$(git_state)" in
    tracked) die "git tracks files under $DIR; refusing to write secrets there" ;;
    not-ignored) die "$DIR is inside a git work tree and is not ignored by git; refusing to write secrets there (add it to .gitignore)" ;;
    ignored) say "git: $DIR is ignored" ;;
    outside) say "note: $DIR is outside this git work tree" ;;
    no-git) say "note: git is not installed; cannot confirm $DIR is ignored" ;;
esac

if [ -e "$DIR" ] && [ ! -d "$DIR" ]; then die "$DIR exists and is not a directory"; fi
if [ ! -d "$DIR" ]; then
    ( umask 077; mkdir -p "$DIR" )
    chmod 700 "$DIR"
    say "created  directory $DIR (mode 700)"
fi

create() { # name, content
    path="$DIR/$1"
    if [ -e "$path" ] || [ -L "$path" ]; then
        say "kept     $1 (already exists; not modified)"
        return 0
    fi
    ( umask 077; set -C; printf '%s\n' "$2" > "$path" ) 2>/dev/null || die "could not create $1 (it may have appeared just now); nothing was overwritten"
    chmod 600 "$path"
    say "created  $1"
}

for name in $PASSWORD_FILES; do
    create "$name" "$(rand_hex 24)"
done
create clinical_totp_secret_keys "$(rand_fernet)"

# The DSNs are derived from the password FILES (new or pre-existing), so they
# stay consistent with whatever is on disk. Reading a password here never prints it.
app_pw=$(cat "$DIR/clinical_app_password")
pg_pw=$(cat "$DIR/postgres_password")
create clinical_dsn "postgresql://clinical_app:${app_pw}@${DB_HOST}:5432/${DB_NAME}"
create clinical_bootstrap_dsn "postgresql://postgres:${pg_pw}@${DB_HOST}:5432/${DB_NAME}"
unset app_pw pg_pw

say ""
say "verifying:"
verify
if [ "$FAILS" -gt 0 ]; then say "result: $FAILS problem(s); see above"; exit 1; fi
say "result: ok"
say "Back these files up somewhere other than this machine (see docs/CLINICAL_DEPLOYMENT.md):"
say "losing clinical_totp_secret_keys locks every enrolled user out of their second factor."
