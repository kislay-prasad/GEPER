"""
clinical/retention.py
──────────────────────

Phase 7 commit 4: retention (spec 22). This is the ONLY code path that ever
writes tombstoned_at/tombstoned_by on vcfs, interpretations, or reports.
DataAccess (clinical_app's privilege domain) never touches those two
columns -- it does not need to, and per human ruling D0(c) it must not be
ABLE to: "clinical_app's REVOKE stays ABSOLUTE -- the application still
cannot delete, which is what every claim rests on."

RetentionPrincipal holds its own connection, opened against a SEPARATE
Postgres login role (clinical_retention -- see schema.sql's GRANT/REVOKE
block for the exact grant, and its docstring for why it is UPDATE, not
DELETE: human ruling D4 chose tombstoning). That is the entire reason this
is its own class rather than more methods on DataAccess: DataAccess's
connection is bound to clinical_app's privileges and is not able to perform
the write this class exists to perform, by design, and mixing the two
privilege domains on one connection object would make that boundary a
convention again instead of a database-enforced fact.

RULINGS THIS MODULE IMPLEMENTS (phase7-c4-retention card, all human, quoted
in full in the commit that added this file -- summarised here for the
reader in front of the code):

  D3 -- the clock starts at report release (release_events, not creation,
  not last access), and re-analysis never resets or extends anything. Each
  artefact's own anchor is computed from the release_events row(s) tied to
  IT, never inherited from or propagated to a parent or child.

  D4 -- tombstone, not delete. tombstoned_at/tombstoned_by are set; nothing
  is ever removed from the table, and every other column is left exactly as
  it was written. (Content redaction beyond that is NOT implemented here --
  D4's ruling settles that this is not a real delete, not what, if
  anything, gets redacted inside a tombstoned row. That is a narrower,
  still-open question this commit does not answer, per the same discipline
  applied to D2/D5's counsel-pending numbers: build the ruled mechanism,
  do not invent the unruled part.)

  D6 -- block, don't cascade, don't null. An artefact with a live
  (non-tombstoned) descendant cannot expire, full stop, regardless of what
  its own date arithmetic says. Applied here to two lineage shapes that
  exist in this schema: interpretations.parent_interpretation_id (a
  re-analysis blocks its parent, c1/c3) and amendments.original_report_id
  (a live amendment blocks the report it amends, c2) -- same rule, same
  reasoning, applied to both FK shapes consistently.

  D7 -- the retention principal performs purges, and every purge is
  audited (one audit_log row per tombstoned artefact, actor_role='System',
  attributed to the org's Phase 5c system principal -- see
  DataAccess._create_system_session). audit_log itself is EXEMPT from
  retention and is never a purge target of this module.

  D2 -- periods are configuration (retention_policies, set via
  DataAccess.set_retention_policy), never hardcoded here, and a missing
  policy FAILS LOUDLY: purge_expired() raises rather than silently skipping
  or defaulting when no policy row exists for the (org, artefact_class)
  pair it was asked to purge.

NOT BUILT HERE, DELIBERATELY, AND NAMED SO THE GAP IS VISIBLE RATHER THAN
ASSUMED HANDLED (same discipline D1 required for excluding kim_pipeline):
no scheduler, no cron entry point. D7's spec quote ("retention is a policy
the platform enforces, not a manual process") is a requirement on the
eventual system, not a request to build the trigger in this commit --
purge_expired() and list_configured_policies() are the mechanism a
scheduled job would call; wiring an actual schedule is a deployment
concern, out of this commit's scope, and is not the same gap as D1's
(which excludes a whole tree) -- worth stating precisely rather than
folding the two together.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Optional, Sequence


class RetentionPolicyMissingError(ValueError):
    """Raised when purge_expired() is asked to purge a class with no configured policy.

    A ValueError subclass, not a bare ValueError, so callers can distinguish
    "you asked for an artefact_class that will never exist"
    (RETENTION_ARTEFACT_CLASSES ValueError, raised by
    DataAccess.set_retention_policy) from "you asked for a real class this
    org simply has not configured yet" -- the second is an expected,
    everyday condition for a scheduler iterating orgs, not a programming
    error.
    """


class NoSystemPrincipalError(ValueError):
    """Raised when an org has no Phase 5c system principal to attribute a purge to."""


@dataclass(frozen=True)
class PurgeResult:
    org_id: uuid.UUID
    artefact_class: str
    tombstoned_ids: List[uuid.UUID]


class RetentionPrincipal:
    """
    Wraps a connection opened as the clinical_retention role. See this
    module's docstring for why that must be a genuinely separate Postgres
    login, not merely a distinguished `users` row on clinical_app's own
    connection.
    """

    def __init__(self, connection: Any, clock: Optional[Any] = None) -> None:
        self.__connection = connection
        self._clock = clock

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock.now()
        return datetime.now(timezone.utc)

    def _query(self, sql: str, params: Sequence[Any] = ()) -> List[tuple]:
        cur = self.__connection.cursor()
        try:
            cur.execute(sql, tuple(params))
            return list(cur.fetchall())
        finally:
            cur.close()

    def _query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[tuple]:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        cur = self.__connection.cursor()
        try:
            cur.execute(sql, tuple(params))
        finally:
            cur.close()

    def _policy_days(self, org_id: uuid.UUID, artefact_class: str) -> int:
        row = self._query_one(
            "SELECT retention_days FROM retention_policies WHERE org_id = %s AND artefact_class = %s",
            (org_id, artefact_class),
        )
        if row is None:
            raise RetentionPolicyMissingError(
                f"No retention policy configured for org {org_id}, artefact_class {artefact_class!r}. "
                "Refusing to purge without a period someone actually set -- "
                "call DataAccess.set_retention_policy() first."
            )
        return row[0]

    def _system_principal(self, org_id: uuid.UUID) -> uuid.UUID:
        row = self._query_one(
            "SELECT user_id FROM users WHERE org_id = %s AND is_system_account = true LIMIT 1",
            (org_id,),
        )
        if row is None:
            raise NoSystemPrincipalError(
                f"Organisation {org_id} has no system principal to attribute a purge to. "
                "Call DataAccess._create_system_session(org_id) once before running retention for this org."
            )
        return row[0]

    def _write_purge_audit_entry(
        self, org_id: uuid.UUID, actor_id: uuid.UUID, resource_type: str, resource_id: uuid.UUID, artefact_class: str
    ) -> None:
        self._execute(
            'INSERT INTO audit_log (org_id, user_id, "timestamp", actor_role, action, resource_type, '
            "resource_id, outcome, details, ip_address) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                org_id,
                actor_id,
                self._now(),
                "System",
                "retention_purge",
                resource_type,
                str(resource_id),
                "success",
                json.dumps({"artefact_class": artefact_class}),
                None,
            ),
        )

    def list_configured_policies(self) -> List[tuple]:
        """All (org_id, artefact_class, retention_days) rows -- what a scheduler would iterate."""
        return self._query("SELECT org_id, artefact_class, retention_days FROM retention_policies")

    def purge_expired(self, org_id: uuid.UUID, artefact_class: str, now: Optional[datetime] = None) -> PurgeResult:
        """
        Tombstone every row of `artefact_class` in `org_id` whose own release
        anchor plus its configured retention_days has passed, and which has
        no live descendant blocking it (D6). Raises RetentionPolicyMissingError
        if no policy is configured for this (org, class) pair -- see D2 in
        this module's docstring.
        """
        retention_days = self._policy_days(org_id, artefact_class)
        when = now if now is not None else self._now()

        if artefact_class == "report":
            return self._purge_reports(org_id, retention_days, when)
        if artefact_class == "run_document":
            return self._purge_interpretations(org_id, retention_days, when)
        if artefact_class == "vcf":
            return self._purge_vcfs(org_id, retention_days, when)
        raise RetentionPolicyMissingError(
            f"artefact_class {artefact_class!r} has a policy row but no purge implementation -- "
            "this should be unreachable, since set_retention_policy refuses unknown classes."
        )

    def _purge_reports(self, org_id: uuid.UUID, retention_days: int, now: datetime) -> PurgeResult:
        """
        Anchor: the FIRST release of this report (MIN released_at) -- D3,
        release starts the clock, and re-delivery to a second consumer does
        not restart it.

        Descendant guard (D6): a live amendment -- one whose own report
        (amendments.amendment_report_id) is not yet tombstoned -- blocks the
        original it amends. Spec 15.2's traceability requirement is exactly
        why: an amendment names the original it replaces, and tombstoning
        the original out from under a live amendment would break that
        traceability.
        """
        rows = self._query(
            """
            SELECT r.id
            FROM reports r
            WHERE r.org_id = %s
              AND r.tombstoned_at IS NULL
              AND (
                  SELECT MIN(re.released_at) FROM release_events re
                  WHERE re.org_id = r.org_id AND re.report_id = r.id
              ) IS NOT NULL
              AND (
                  SELECT MIN(re.released_at) FROM release_events re
                  WHERE re.org_id = r.org_id AND re.report_id = r.id
              ) + (%s * INTERVAL '1 day') <= %s
              AND NOT EXISTS (
                  SELECT 1 FROM amendments a
                  JOIN reports r2 ON r2.org_id = a.org_id AND r2.id = a.amendment_report_id
                  WHERE a.org_id = r.org_id AND a.original_report_id = r.id
                    AND r2.tombstoned_at IS NULL
              )
            """,
            (org_id, retention_days, now),
        )
        ids = [row[0] for row in rows]
        actor_id = self._system_principal(org_id) if ids else None
        for report_id in ids:
            self._execute(
                "UPDATE reports SET tombstoned_at = %s, tombstoned_by = %s WHERE org_id = %s AND id = %s",
                (now, actor_id, org_id, report_id),
            )
            self._write_purge_audit_entry(org_id, actor_id, "report", report_id, "report")
        return PurgeResult(org_id=org_id, artefact_class="report", tombstoned_ids=ids)

    def _purge_interpretations(self, org_id: uuid.UUID, retention_days: int, now: datetime) -> PurgeResult:
        """
        Anchor: the LATEST release (MAX released_at) across every report
        tied to this interpretation (interpretation_id) -- an interpretation
        can carry more than one report over its life (an amendment shares
        its original's interpretation_id), so the anchor is conservative:
        the clock does not start until the most recent of them was released.

        Descendant guard (D6), two independent lineage shapes:
          (a) any report still tied to this interpretation that is not yet
              tombstoned (covers the original AND every amendment, since
              both carry this same interpretation_id);
          (b) any child interpretation (parent_interpretation_id = this id,
              c1/c3's re-analysis lineage) that is not yet tombstoned.
        Both are required in addition to the date check, not instead of it:
        the date check alone could not see an unreported child re-analysis
        (no release_events row exists for it yet, so it never appears in
        the anchor computation at all), which is exactly the gap the
        explicit guard exists to close.
        """
        rows = self._query(
            """
            SELECT i.id
            FROM interpretations i
            WHERE i.org_id = %s
              AND i.tombstoned_at IS NULL
              AND (
                  SELECT MAX(re.released_at) FROM reports r
                  JOIN release_events re ON re.org_id = r.org_id AND re.report_id = r.id
                  WHERE r.org_id = i.org_id AND r.interpretation_id = i.id
              ) IS NOT NULL
              AND (
                  SELECT MAX(re.released_at) FROM reports r
                  JOIN release_events re ON re.org_id = r.org_id AND re.report_id = r.id
                  WHERE r.org_id = i.org_id AND r.interpretation_id = i.id
              ) + (%s * INTERVAL '1 day') <= %s
              AND NOT EXISTS (
                  SELECT 1 FROM reports r
                  WHERE r.org_id = i.org_id AND r.interpretation_id = i.id AND r.tombstoned_at IS NULL
              )
              AND NOT EXISTS (
                  SELECT 1 FROM interpretations child
                  WHERE child.org_id = i.org_id AND child.parent_interpretation_id = i.id
                    AND child.tombstoned_at IS NULL
              )
            """,
            (org_id, retention_days, now),
        )
        ids = [row[0] for row in rows]
        actor_id = self._system_principal(org_id) if ids else None
        for interp_id in ids:
            self._execute(
                "UPDATE interpretations SET tombstoned_at = %s, tombstoned_by = %s WHERE org_id = %s AND id = %s",
                (now, actor_id, org_id, interp_id),
            )
            self._write_purge_audit_entry(org_id, actor_id, "interpretation", interp_id, "run_document")
        return PurgeResult(org_id=org_id, artefact_class="run_document", tombstoned_ids=ids)

    def _purge_vcfs(self, org_id: uuid.UUID, retention_days: int, now: datetime) -> PurgeResult:
        """
        Anchor: the LATEST release (MAX released_at) across every report of
        every interpretation of this VCF -- same conservative aggregation as
        interpretations, one join hop further out (a VCF can carry more than
        one interpretation: c3's re-analysis is explicitly "the same VCF").

        Descendant guard (D6): any interpretation of this VCF that is not
        yet tombstoned blocks it -- by construction, an interpretation
        cannot itself be tombstoned while any of ITS descendants (reports,
        child interpretations) are live, so this one check transitively
        covers the whole subtree below the VCF.
        """
        rows = self._query(
            """
            SELECT v.id
            FROM vcfs v
            WHERE v.org_id = %s
              AND v.tombstoned_at IS NULL
              AND (
                  SELECT MAX(re.released_at) FROM interpretations i
                  JOIN reports r ON r.org_id = i.org_id AND r.interpretation_id = i.id
                  JOIN release_events re ON re.org_id = r.org_id AND re.report_id = r.id
                  WHERE i.org_id = v.org_id AND i.vcf_id = v.id
              ) IS NOT NULL
              AND (
                  SELECT MAX(re.released_at) FROM interpretations i
                  JOIN reports r ON r.org_id = i.org_id AND r.interpretation_id = i.id
                  JOIN release_events re ON re.org_id = r.org_id AND re.report_id = r.id
                  WHERE i.org_id = v.org_id AND i.vcf_id = v.id
              ) + (%s * INTERVAL '1 day') <= %s
              AND NOT EXISTS (
                  SELECT 1 FROM interpretations i
                  WHERE i.org_id = v.org_id AND i.vcf_id = v.id AND i.tombstoned_at IS NULL
              )
            """,
            (org_id, retention_days, now),
        )
        ids = [row[0] for row in rows]
        actor_id = self._system_principal(org_id) if ids else None
        for vcf_id in ids:
            self._execute(
                "UPDATE vcfs SET tombstoned_at = %s, tombstoned_by = %s WHERE org_id = %s AND id = %s",
                (now, actor_id, org_id, vcf_id),
            )
            self._write_purge_audit_entry(org_id, actor_id, "vcf", vcf_id, "vcf")
        return PurgeResult(org_id=org_id, artefact_class="vcf", tombstoned_ids=ids)
