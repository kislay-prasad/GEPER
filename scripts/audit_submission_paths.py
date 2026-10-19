#!/usr/bin/env python3
"""
scripts/audit_submission_paths.py
─────────────────────────────────
READ-ONLY audit: which queued / running / interrupted submissions in a SQLite submission store hold a `vcf_path` that the
current path policy (geper/api/vcf_paths.py) would refuse. Procedure and background: docs/VCF_STORAGE.md.

    GEPER_VCF_ROOT=... python scripts/audit_submission_paths.py /path/to/COPY-of/.submissions.db

The store is opened with SQLite `mode=ro`. Nothing is written, retried, deleted or migrated. Output carries submission id,
status, organisation and the refusal code, never a path or file content. Exit status: 0 = every audited row passes,
1 = at least one would be refused, 2 = the audit could not run.
"""

from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "geper"))

from api.vcf_paths import VcfPathError, resolve_vcf_path  # noqa: E402

AUDITED_STATUSES = ("queued", "running", "interrupted")


def audit(db_file: Path):
    """(rows audited, [(submission id, status, organisation id, refusal code)]) for rows the policy would refuse."""
    uri = f"file:{db_file.resolve().as_posix()}?mode=ro"
    placeholders = ",".join("?" * len(AUDITED_STATUSES))
    with sqlite3.connect(uri, uri=True) as conn:
        rows = conn.execute(
            f"SELECT id, status, org_id, vcf_path FROM submissions WHERE status IN ({placeholders}) ORDER BY created_at",  # noqa: S608
            AUDITED_STATUSES,
        ).fetchall()
    refused = []
    for sub_id, status, org_id, vcf_path in rows:
        try:
            resolve_vcf_path(vcf_path, org_id)
        except VcfPathError as exc:
            refused.append((sub_id, status, org_id, exc.code))
    return len(rows), refused


def main(argv) -> int:
    if len(argv) != 2:
        print("usage: audit_submission_paths.py <path to a COPY of the .submissions.db>", file=sys.stderr)
        return 2
    db_file = Path(argv[1])
    if not db_file.is_file():
        print("audit could not run: the given file does not exist", file=sys.stderr)
        return 2
    try:
        audited, refused = audit(db_file)
    except sqlite3.Error as exc:
        print(f"audit could not run: {type(exc).__name__}", file=sys.stderr)
        return 2
    for sub_id, status, org_id, code in refused:
        print(f"{sub_id}\t{status}\torg={org_id}\t{code}")
    by_code = Counter(code for *_rest, code in refused)
    print(
        f"audited={audited} would_be_refused={len(refused)} " + " ".join(f"{k}={v}" for k, v in sorted(by_code.items()))
    )
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
