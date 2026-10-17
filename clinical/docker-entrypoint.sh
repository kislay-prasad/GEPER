#!/bin/sh
# clinical/docker-entrypoint.sh
# ─────────────────────────────
# Reads file-based secrets into the environment, then runs the command.
#
# WHY IT EXISTS. The clinical code reads its credentials from environment
# variables (CLINICAL_DSN, CLINICAL_TOTP_SECRET_KEYS, ...) and was deliberately
# not changed to read files. Docker and Compose deliver secrets as files under
# /run/secrets, which keeps them out of `docker inspect`, out of the image and
# out of the Compose file. This script is the whole adapter: for each variable
# on the list below, if NAME_FILE is set, the file's contents become NAME.
#
# RULES, each of which is a refusal rather than a guess:
#   * ONLY THE VARIABLES LISTED HERE are read from files. An arbitrary
#     NAME_FILE in the environment is never followed.
#   * NAME and NAME_FILE both set is an ERROR (exit 64). Silently preferring one
#     would hide a misconfiguration that can leave a service running on a stale
#     credential.
#   * A NAME_FILE that cannot be read, or is empty, is an ERROR (exit 66).
#   * THE VALUE IS NEVER PRINTED. Messages name the variable, never its content.
#   * Trailing newlines are stripped (command substitution does this), so a
#     secret file written with `echo` or an editor works.
#
# Nothing else happens here: no defaults are invented (CLINICAL_DSN stays unset
# if nothing sets it, and /readyz then says so by name), and the command is
# exec'd so the service is PID 1's direct child with signals delivered to it.
set -eu

SECRET_VARIABLES="
CLINICAL_DSN
CLINICAL_BOOTSTRAP_DSN
CLINICAL_APP_PASSWORD
CLINICAL_RETENTION_PASSWORD
CLINICAL_TOTP_SECRET_KEYS
CLINICAL_FIRST_ADMIN_PASSWORD
"

for name in $SECRET_VARIABLES; do
    file_var="${name}_FILE"
    eval "file_path=\${$file_var:-}"
    [ -n "$file_path" ] || continue

    eval "current=\${$name:-}"
    if [ -n "$current" ]; then
        echo "clinical entrypoint: both $name and $file_var are set; set exactly one." >&2
        exit 64
    fi
    if [ ! -f "$file_path" ] || [ ! -r "$file_path" ]; then
        echo "clinical entrypoint: $file_var points at a file that is missing or unreadable." >&2
        exit 66
    fi
    value=$(cat "$file_path")
    if [ -z "$value" ]; then
        echo "clinical entrypoint: the file named by $file_var is empty." >&2
        exit 66
    fi
    export "$name=$value"
    unset "$file_var"
done

exec "$@"
