"""
clinical/audit_payload_exposure_report.py
─────────────────────────────────────────

READ-ONLY. Counts the audit rows that already contain a PAYLOAD where an
identifier belongs.

WHY THIS EXISTS. Until w121, `@auditable` wrote the decorated method's RETURN
VALUE into `audit_log.resource_id`. For the creators that is the new row's id
and correct. For everything else it is whatever came back:

  * `enrol_totp -> List[str]` returned the eight TOTP backup codes in
    PLAINTEXT, so each enrolment wrote them into resource_id verbatim.
  * `login -> Session` wrote a dataclass repr.
  * the read/query methods wrote a stringified result set.
  * the status/verdict methods wrote 'True', 'passed', and so on.

AND audit_log IS APPEND-ONLY BY GRANT (schema.sql: GRANT SELECT, INSERT ON
audit_log TO clinical_app; REVOKE UPDATE, DELETE ON audit_log FROM
clinical_app / FROM PUBLIC). The application cannot delete or redact any of
it. w121 stops new rows being written; it cannot and must not touch the rows
already there. What this script does is SIZE the existing exposure so the
decision about it can be taken on numbers.

IT IS DELIBERATELY READ-ONLY. It issues SELECT statements and nothing else --
no UPDATE, no DELETE, no DDL, no migration. Removing or redacting a row is
forbidden by the grant and is the human's decision, not this script's.

USAGE
    CLINICAL_TEST_DSN=postgresql://... python -m clinical.audit_payload_exposure_report
    python -m clinical.audit_payload_exposure_report --dsn postgresql://...
    python -m clinical.audit_payload_exposure_report --dsn ... --json

WHAT IT CANNOT TELL YOU. Run against a test database it reports that test
database's rows, which is a number about the test run and nothing else. The
production figure -- how many real users' backup codes are sitting in the
real audit table, and since when -- can only be obtained by running this
against the production audit_log with a read-only role. This file makes that
possible; it does not and cannot answer it from here.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List

# A resource_id that IS an identifier: a UUID, or one of the decorator's own
# markers. Anything else is a payload of some kind.
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_MARKERS = {"<none>", "<denied>", "<error>"}

# Actions whose resource_id is a legitimate non-UUID identifier.
_NON_UUID_IDENTIFIER_ACTIONS = {
    "retention_policy_set",  # (org, artefact_class) IS the policy's identity
}

CREDENTIAL_ACTIONS = {"totp_enrolled"}

SELECT_ROWS = """
    SELECT log_id, action, resource_type, resource_id, outcome, "timestamp", org_id
      FROM audit_log
     ORDER BY log_id
"""


def classify(action: str, resource_id: str | None) -> str:
    """What KIND of wrong value is in this row, if any."""
    value = resource_id if resource_id is not None else ""
    if value in _MARKERS:
        return "marker"
    if _UUID_RE.match(value):
        return "identifier"
    if action in _NON_UUID_IDENTIFIER_ACTIONS:
        return "identifier"
    if action in CREDENTIAL_ACTIONS and value.startswith("["):
        # enrol_totp's return value: the plaintext backup codes.
        return "credential"
    if value.startswith("[") or value.startswith("{"):
        return "result_set"
    if "(" in value and value.endswith(")"):
        return "object_repr"
    if value in ("True", "False"):
        return "verdict"
    return "other_non_identifier"


SEVERITY = {
    "credential": "CREDENTIAL EXPOSURE -- plaintext TOTP backup codes, permanently recorded",
    "result_set": "payload -- a stringified result set where an identifier belongs",
    "object_repr": "payload -- an object repr where an identifier belongs",
    "verdict": "not an identifier -- a boolean verdict",
    "other_non_identifier": "not an identifier -- a status string or free text",
    "marker": "no identifier recorded",
    "identifier": "correct",
}


def gather(conn) -> Dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(SELECT_ROWS)
        rows = cur.fetchall()

    by_kind: Dict[str, int] = {}
    by_action: Dict[str, Dict[str, Any]] = {}
    credential_rows: List[Dict[str, Any]] = []

    for log_id, action, resource_type, resource_id, outcome, ts, org_id in rows:
        kind = classify(action, resource_id)
        by_kind[kind] = by_kind.get(kind, 0) + 1
        entry = by_action.setdefault(
            action, {"action": action, "resource_type": resource_type, "kinds": {}, "total": 0}
        )
        entry["kinds"][kind] = entry["kinds"].get(kind, 0) + 1
        entry["total"] += 1
        if kind == "credential":
            # The VALUE is never printed. Only the fact, the shape and the
            # subject -- printing it would copy the exposure somewhere new.
            credential_rows.append(
                {
                    "log_id": log_id,
                    "action": action,
                    "org_id": str(org_id),
                    "timestamp": str(ts),
                    "outcome": outcome,
                    "value_length": len(resource_id or ""),
                    "approx_items": (resource_id or "").count(",") + 1,
                }
            )

    return {
        "total_rows": len(rows),
        "by_kind": by_kind,
        "by_action": sorted(by_action.values(), key=lambda e: -e["total"]),
        "credential_rows": credential_rows,
    }


def render(result: Dict[str, Any]) -> str:
    out: List[str] = []
    out.append("audit_log payload exposure -- READ-ONLY report")
    out.append("=" * 62)
    out.append(f"rows examined: {result['total_rows']}")
    out.append("")
    out.append("BY KIND")
    for kind, count in sorted(result["by_kind"].items(), key=lambda kv: -kv[1]):
        out.append(f"  {count:>8}  {kind:<22} {SEVERITY.get(kind, '')}")
    out.append("")
    out.append("AFFECTED ACTIONS (rows whose resource_id is not an identifier)")
    any_affected = False
    for entry in result["by_action"]:
        bad = {k: v for k, v in entry["kinds"].items() if k not in ("identifier", "marker")}
        if not bad:
            continue
        any_affected = True
        detail = ", ".join(f"{k}={v}" for k, v in sorted(bad.items()))
        out.append(
            f"  {sum(bad.values()):>8}  action={entry['action']:<40} resource_type={entry['resource_type']:<18} {detail}"
        )
    if not any_affected:
        out.append("  (none)")
    out.append("")
    creds = result["credential_rows"]
    out.append(f"CREDENTIAL ROWS (plaintext TOTP backup codes): {len(creds)}")
    if creds:
        out.append("  These cannot be deleted: audit_log is append-only by grant.")
        out.append("  Values are NOT printed -- printing them would copy the exposure.")
        for row in creds[:50]:
            out.append(
                f"    log_id={row['log_id']} org_id={row['org_id']} at={row['timestamp']} "
                f"shape=list of ~{row['approx_items']} items, {row['value_length']} chars"
            )
        if len(creds) > 50:
            out.append(f"    ... and {len(creds) - 50} more")
    out.append("")
    out.append("SCOPE OF THIS NUMBER. It is the count for the database this was run")
    out.append("against. Against a test database it says nothing about production.")
    out.append("The production figure requires running this against the production")
    out.append("audit_log with a read-only role.")
    return "\n".join(out)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only count of audit_log rows containing a payload.")
    parser.add_argument("--dsn", default=os.getenv("CLINICAL_TEST_DSN") or os.getenv("CLINICAL_DSN"))
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)

    if not args.dsn:
        parser.error("no DSN: pass --dsn or set CLINICAL_TEST_DSN / CLINICAL_DSN")

    import psycopg  # imported here so --help works without the driver

    # read_only=True is belt and braces on top of issuing only SELECT: the
    # transaction itself refuses to write.
    with psycopg.connect(args.dsn, autocommit=False, connect_timeout=10) as conn:
        conn.read_only = True
        result = gather(conn)
        conn.rollback()

    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
