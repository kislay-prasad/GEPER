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

  D2 -- RE-RULED 2026-09-09, superseding this module's original per-class
  reading. "Everything downstream of a VCF inherits the VCF period -- five
  years minimum, ten for minors ... configurable per organisation." This
  is now an AGE-CONDITIONAL, PER-ARTEFACT resolution, not one flat integer
  per class:

    * A VCF's own retention_days is resolved ONCE, at creation
      (DataAccess.create_vcf), from the patient's age AT COLLECTION
      (samples.collected_at vs patients.dob) -- ten years if the patient
      was a minor at collection, five otherwise, UNLESS the organisation
      has configured its own adult and/or minor override
      (retention_policies.retention_days / retention_days_minor for the
      'vcf' class; see DataAccess.set_retention_policy). These two numbers
      are a FLOOR THE PRODUCT CHOSE, WITHIN NABL 112A 7.8.5(b)(iv)'s
      stated range -- NOT a legal finding; see
      DEFAULT_ADULT_VCF_RETENTION_DAYS/DEFAULT_MINOR_VCF_RETENTION_DAYS
      below for where that caveat is pinned to the numbers themselves.
    * Every downstream artefact -- an interpretation of that VCF, a
      report of that interpretation, an amendment (itself a report row) --
      INHERITS the VCF's already-resolved retention_days AT ITS OWN
      CREATION and never recomputes it. This is what keeps a report from
      outliving or predeceasing the VCF it rests on: splitting the period
      across the lineage independently is the D6 problem in a different
      form (the human's own words on the ruling card) -- each class could
      look individually correct while the chain silently loses its base.
    * purge_expired() therefore no longer looks up a class-wide period at
      all. It reads each candidate row's OWN retention_days column. A row
      with no resolved value (NULL -- only possible for a row inserted
      outside DataAccess, e.g. directly by a test) is never purge-eligible:
      absence of a resolved period blocks a purge, it can never permit
      one. RetentionPolicyMissingError, from the pre-2026-09-09 per-class
      design, is retired along with that design -- there is no longer a
      "class has no configured policy" state to fail loudly about, because
      a VCF's period is now always resolvable (it falls back to the
      product default, never to nothing) and downstream artefacts never
      had their own policy to be missing in the first place.
    * RETENTION_ARTEFACT_CLASSES (DataAccess) is narrowed to ("vcf",) for
      the same reason: leaving "run_document"/"report" independently
      settable while no purge path any longer consults their setting would
      be a control that looks live and does nothing -- worse than no
      control. THIS NARROWING IS MY OWN ENGINEERING DECISION, not part of
      the human's ruling, which spoke only to what inherits from what.

  MINOR-AGE THRESHOLD -- ALSO MY OWN DECISION, NOT RULED: NABL 112A's
  clause says "testing of minor" without defining the age itself. This
  module uses MINOR_AGE_THRESHOLD_YEARS = 18 (the Indian Majority Act,
  1875's default age of majority) as the ordinary, unremarkable reading of
  "minor" in an Indian clinical-lab context. Nobody has ruled this
  specifically; if a different threshold is ever wanted, this is the one
  constant to change.

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

ALSO NOT BUILT HERE, AND OUT OF THIS DISPATCH'S BOUNDARY RATHER THAN
FORGOTTEN: reviewer_claims, release_events, amendments and
amendment_notifications are named in the 2026-09-09 D2 ruling as
inheriting the VCF period, but NONE of the four carries a tombstoned_at/
tombstoned_by column or any purge path today -- an amendment's OWN report
row (amendments.amendment_report_id) does inherit and purge correctly,
since it IS a `reports` row, but the amendments row itself, and the other
three tables, have no retention mechanism to extend at all. Building one
for four more tables (new columns, new D6 guards, new grants) is a new
mechanism, not the age-conditional floor and its propagation this dispatch
scoped -- named here so the gap is visible rather than silently assumed
closed by this commit.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, List, Optional, Sequence

# NABL 112A 7.8.5(b)(iv) also sets a >=5 year floor for raw sequencing data
# (fastq/SAM/BAM), but this codebase has no artefact_class, table, or column
# for it -- D1 (human ruling: clinical/ only) excludes kim_pipeline, which is
# where raw reads live, from this tree entirely. Not a gap this shell leaves
# open; an already-ruled exclusion, re-confirmed here rather than silently
# assumed when the two NABL floors (VCF, raw reads) were read side by side.

# Human ruling D2, 2026-09-09: "everything downstream of a VCF inherits the
# VCF period -- five years minimum, ten for minors." *** THESE ARE A FLOOR
# THE PRODUCT CHOSE WITHIN NABL 112A 7.8.5(b)(iv)'s STATED RANGE (>=5y,
# "at least 5-10 years" for a minor) -- NOT A LEGAL FINDING. *** Nobody may
# cite these two constants as a legal conclusion; they are defaults,
# configurable per organisation via DataAccess.set_retention_policy("vcf",
# retention_days=..., retention_days_minor=...). An org that sets neither
# override inherits exactly these two numbers.
DEFAULT_ADULT_VCF_RETENTION_DAYS = 5 * 365  # 1825
DEFAULT_MINOR_VCF_RETENTION_DAYS = 10 * 365  # 3650

# MY OWN ENGINEERING DECISION, NOT PART OF ANY RULING: NABL's clause names
# "testing of minor" without defining the age. 18 is the Indian Majority
# Act, 1875's default age of majority -- the ordinary reading absent a
# clinical-specific definition. If counsel or the human later specify a
# different threshold, this is the one constant to change.
MINOR_AGE_THRESHOLD_YEARS = 18


def is_minor_at(dob: date, at: date) -> bool:
    """Whether a patient born `dob` had not yet reached MINOR_AGE_THRESHOLD_YEARS
    as of date `at` (e.g. a sample's collection date). Pure and DB-free so the
    age-conditional branch itself is unit-testable without a connection."""
    threshold_birthday = date(dob.year + MINOR_AGE_THRESHOLD_YEARS, dob.month, dob.day)
    return at < threshold_birthday


def resolve_vcf_retention_days(
    dob: date,
    collected_at: date,
    *,
    policy_adult_days: Optional[int] = None,
    policy_minor_days: Optional[int] = None,
) -> int:
    """
    THE age-conditional resolution rule (human ruling D2, 2026-09-09),
    isolated as a pure function so it is provable independent of any
    database: ten years for a minor at collection, five otherwise, unless
    the organisation configured its own override for that branch.

    `policy_adult_days`/`policy_minor_days` are this org's own
    retention_policies row for the 'vcf' class, if any (None when
    unconfigured, or when the org configured one side and not the other --
    each falls back to the product default INDEPENDENTLY; an org that
    overrides the adult number has not implicitly chosen a minor one too).

    Called ONCE, by DataAccess.create_vcf, at VCF creation -- the result is
    persisted on vcfs.retention_days and never recomputed; every downstream
    artefact inherits that persisted value rather than calling this again.
    """
    if is_minor_at(dob, collected_at):
        return policy_minor_days if policy_minor_days is not None else DEFAULT_MINOR_VCF_RETENTION_DAYS
    return policy_adult_days if policy_adult_days is not None else DEFAULT_ADULT_VCF_RETENTION_DAYS


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
        anchor plus its OWN resolved retention_days has passed, and which has
        no live descendant blocking it (D6).

        No longer looks up a class-wide period (see D2 in this module's
        docstring, re-ruled 2026-09-09): each row already carries its own
        resolved retention_days, set once at creation and inherited down the
        lineage. A row whose retention_days is NULL -- possible only for a
        row inserted outside DataAccess, e.g. directly by a test -- is never
        eligible; the queries below all filter on retention_days IS NOT NULL.
        """
        when = now if now is not None else self._now()

        if artefact_class == "report":
            return self._purge_reports(org_id, when)
        if artefact_class == "run_document":
            return self._purge_interpretations(org_id, when)
        if artefact_class == "vcf":
            return self._purge_vcfs(org_id, when)
        raise ValueError(f"artefact_class {artefact_class!r} has no purge implementation.")

    def _purge_reports(self, org_id: uuid.UUID, now: datetime) -> PurgeResult:
        """
        Anchor: the FIRST release of this report (MIN released_at) -- D3,
        release starts the clock, and re-delivery to a second consumer does
        not restart it. Window: the report's OWN retention_days, inherited
        from its interpretation at creation (D2, 2026-09-09).

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
              AND r.retention_days IS NOT NULL
              AND (
                  SELECT MIN(re.released_at) FROM release_events re
                  WHERE re.org_id = r.org_id AND re.report_id = r.id
              ) IS NOT NULL
              AND (
                  SELECT MIN(re.released_at) FROM release_events re
                  WHERE re.org_id = r.org_id AND re.report_id = r.id
              ) + (r.retention_days * INTERVAL '1 day') <= %s
              AND NOT EXISTS (
                  SELECT 1 FROM amendments a
                  JOIN reports r2 ON r2.org_id = a.org_id AND r2.id = a.amendment_report_id
                  WHERE a.org_id = r.org_id AND a.original_report_id = r.id
                    AND r2.tombstoned_at IS NULL
              )
            """,
            (org_id, now),
        )
        ids = [row[0] for row in rows]
        actor_id = self._system_principal(org_id) if ids else None
        for report_id in ids:
            self._execute(
                "UPDATE reports SET tombstoned_at = %s, tombstoned_by = %s WHERE org_id = %s AND id = %s",
                (now, actor_id, org_id, report_id),
            )
            self._write_purge_audit_entry(org_id, actor_id, "report", report_id, "report")
            self._cascade_tombstone_report_dependents(org_id, report_id, now, actor_id)
        return PurgeResult(org_id=org_id, artefact_class="report", tombstoned_ids=ids)

    def _cascade_tombstone_report_dependents(
        self, org_id: uuid.UUID, report_id: uuid.UUID, now: datetime, actor_id: uuid.UUID
    ) -> None:
        """
        Human ruling, tombstone-cascade (2026-09-09, following D2): "an
        amendment to an interpretation of data that no longer exists is
        incoherent; a reviewer claim about a purged variant has no
        meaning." When a report is tombstoned, IN THE SAME TRANSACTION
        (no commit happens anywhere in this class -- see this module's
        docstring), also tombstone everything hanging off it that has no
        standing of its own:

          1. release_events keyed directly on this report_id.
          2. amendments where THIS report is the ORIGINAL
             (original_report_id) -- not the amendment side
             (amendment_report_id). By the time a report purges, D6's own
             guard (see _purge_reports's docstring) has already required
             any amendment OF it to have its own report tombstoned first,
             so original_report_id is always the later, deterministic
             trigger for a given amendments row -- see that column's own
             schema.sql comment for the full argument.
          3. For each amendments row just stamped, the amendment_notifications
             row that names the SAME amendment_report_id -- the
             notification about that specific amendment event.
          4. For each amendment_notifications row just stamped, every
             notification_read_receipts row keyed on it (human ruling,
             2026-09-09/10, closing the residual gap named when the first
             three of these four were wired: a receipt for a notification
             that no longer exists has no meaning either). Same trigger
             point as step 3's own loop body -- a receipt is stamped at
             the exact moment its notification is, never as a separate
             pass.

        NO separate audit_log entry per cascaded row -- MY OWN DECISION,
        not something the ruling settled: these four tables are not
        independently-purged artefact classes, they are fallout of the
        one parent report purge that already carries its own D7 audit
        entry. Cascading the AUDIT of a purge would misrepresent four
        (or more) rows as four separate purge decisions when there was
        only ever one.
        """
        release_event_rows = self._query(
            "SELECT id FROM release_events WHERE org_id = %s AND report_id = %s AND tombstoned_at IS NULL",
            (org_id, report_id),
        )
        for (release_event_id,) in release_event_rows:
            self._execute(
                "UPDATE release_events SET tombstoned_at = %s, tombstoned_by = %s WHERE org_id = %s AND id = %s",
                (now, actor_id, org_id, release_event_id),
            )

        amendment_rows = self._query(
            "SELECT id, amendment_report_id FROM amendments "
            "WHERE org_id = %s AND original_report_id = %s AND tombstoned_at IS NULL",
            (org_id, report_id),
        )
        for amendment_id, amendment_report_id in amendment_rows:
            self._execute(
                "UPDATE amendments SET tombstoned_at = %s, tombstoned_by = %s WHERE org_id = %s AND id = %s",
                (now, actor_id, org_id, amendment_id),
            )
            notification_rows = self._query(
                "SELECT id FROM amendment_notifications "
                "WHERE org_id = %s AND amendment_report_id = %s AND tombstoned_at IS NULL",
                (org_id, amendment_report_id),
            )
            for (notification_id,) in notification_rows:
                self._execute(
                    "UPDATE amendment_notifications SET tombstoned_at = %s, tombstoned_by = %s "
                    "WHERE org_id = %s AND id = %s",
                    (now, actor_id, org_id, notification_id),
                )
                receipt_rows = self._query(
                    "SELECT id FROM notification_read_receipts "
                    "WHERE org_id = %s AND notification_id = %s AND tombstoned_at IS NULL",
                    (org_id, notification_id),
                )
                for (receipt_id,) in receipt_rows:
                    self._execute(
                        "UPDATE notification_read_receipts SET tombstoned_at = %s, tombstoned_by = %s "
                        "WHERE org_id = %s AND id = %s",
                        (now, actor_id, org_id, receipt_id),
                    )

    def _purge_interpretations(self, org_id: uuid.UUID, now: datetime) -> PurgeResult:
        """
        Anchor: the LATEST release (MAX released_at) across every report
        tied to this interpretation (interpretation_id) -- an interpretation
        can carry more than one report over its life (an amendment shares
        its original's interpretation_id), so the anchor is conservative:
        the clock does not start until the most recent of them was released.
        Window: the interpretation's OWN retention_days, inherited from its
        vcf at creation (D2, 2026-09-09).

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
              AND i.retention_days IS NOT NULL
              AND (
                  SELECT MAX(re.released_at) FROM reports r
                  JOIN release_events re ON re.org_id = r.org_id AND re.report_id = r.id
                  WHERE r.org_id = i.org_id AND r.interpretation_id = i.id
              ) IS NOT NULL
              AND (
                  SELECT MAX(re.released_at) FROM reports r
                  JOIN release_events re ON re.org_id = r.org_id AND re.report_id = r.id
                  WHERE r.org_id = i.org_id AND r.interpretation_id = i.id
              ) + (i.retention_days * INTERVAL '1 day') <= %s
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
            (org_id, now),
        )
        ids = [row[0] for row in rows]
        actor_id = self._system_principal(org_id) if ids else None
        for interp_id in ids:
            self._execute(
                "UPDATE interpretations SET tombstoned_at = %s, tombstoned_by = %s WHERE org_id = %s AND id = %s",
                (now, actor_id, org_id, interp_id),
            )
            self._write_purge_audit_entry(org_id, actor_id, "interpretation", interp_id, "run_document")
            self._cascade_tombstone_interpretation_dependents(org_id, interp_id, now, actor_id)
        return PurgeResult(org_id=org_id, artefact_class="run_document", tombstoned_ids=ids)

    def _cascade_tombstone_interpretation_dependents(
        self, org_id: uuid.UUID, interpretation_id: uuid.UUID, now: datetime, actor_id: uuid.UUID
    ) -> None:
        """
        Human ruling, tombstone-cascade (2026-09-09, following D2): "a
        reviewer claim about a purged variant has no meaning." When an
        interpretation is tombstoned, IN THE SAME TRANSACTION, also
        tombstone every reviewer_claims row keyed on it. No separate
        audit_log entry per claim -- same reasoning as
        _cascade_tombstone_report_dependents's own docstring.
        """
        claim_rows = self._query(
            "SELECT id FROM reviewer_claims WHERE org_id = %s AND interpretation_id = %s AND tombstoned_at IS NULL",
            (org_id, interpretation_id),
        )
        for (claim_id,) in claim_rows:
            self._execute(
                "UPDATE reviewer_claims SET tombstoned_at = %s, tombstoned_by = %s WHERE org_id = %s AND id = %s",
                (now, actor_id, org_id, claim_id),
            )

    def _purge_vcfs(self, org_id: uuid.UUID, now: datetime) -> PurgeResult:
        """
        Anchor: the LATEST release (MAX released_at) across every report of
        every interpretation of this VCF -- same conservative aggregation as
        interpretations, one join hop further out (a VCF can carry more than
        one interpretation: c3's re-analysis is explicitly "the same VCF").
        Window: the VCF's OWN retention_days -- the value resolved once, at
        creation, from the patient's age at collection (D2, 2026-09-09);
        every other class's window (above) is this same value, inherited.

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
              AND v.retention_days IS NOT NULL
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
              ) + (v.retention_days * INTERVAL '1 day') <= %s
              AND NOT EXISTS (
                  SELECT 1 FROM interpretations i
                  WHERE i.org_id = v.org_id AND i.vcf_id = v.id AND i.tombstoned_at IS NULL
              )
            """,
            (org_id, now),
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
