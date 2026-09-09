"""
tests/test_phase_7_commit4_retention.py
─────────────────────────────────────────

Phase 7 commit 4: retention (spec 22), all nine decisions ruled on card
phase7-c4-retention, RE-RULED 2026-09-09 (human, D2): retention resolves
PER ARTEFACT from the patient's age at collection -- ten years for a minor,
five otherwise -- and every downstream artefact INHERITS the resolved value
rather than recomputing it. This file tests three things: the age-
conditional resolution rule itself (clinical.retention.resolve_vcf_retention_days,
a pure function), the per-org configuration surface (DataAccess
.set_retention_policy/get_retention_policy, now scoped to "vcf" only -- see
RETENTION_ARTEFACT_CLASSES's own comment), and the clinical_retention-
privileged purge mechanism (clinical.retention.RetentionPrincipal), which now
reads each row's own already-resolved retention_days instead of a class-wide
policy.

KNOWN TESTING LIMITATION, STATED RATHER THAN LEFT SILENT: like every other
test file in this codebase, RetentionPrincipal is exercised here over the
same superuser test connection everything else uses, not a real login as
the clinical_retention role -- no test file in clinical/tests/ actually
connects AS clinical_app either (grep confirms it; the role only needs to
exist for schema.sql's GRANT statements to apply without error). So this
file proves the PURGE LOGIC -- which rows get tombstoned, the D6 guard, the
age-conditional resolution and its propagation -- but does not prove the
GRANT/REVOKE block in schema.sql actually blocks a real clinical_retention
login from issuing DELETE, or blocks clinical_app from reaching
reviewer_claims/amendments. That would need a genuine second connection with
real clinical_retention credentials, which the existing fixture pattern does
not provide for any role. Naming the gap rather than assuming it is covered.
"""

from __future__ import annotations

import datetime
from datetime import date, timedelta, timezone
import os
import uuid

import pytest

from clinical.data_access import (
    AuthorizationError,
    BcryptHasher,
    DataAccess,
    SystemClock,
)
from clinical.retention import (
    DEFAULT_ADULT_VCF_RETENTION_DAYS,
    DEFAULT_MINOR_VCF_RETENTION_DAYS,
    MINOR_AGE_THRESHOLD_YEARS,
    NoSystemPrincipalError,
    RetentionPrincipal,
    is_minor_at,
    resolve_vcf_retention_days,
)

DSN = os.getenv("CLINICAL_TEST_DSN")
SCHEMA_PATH = __import__("pathlib").Path(__file__).parent.parent / "schema.sql"

_CREATE_ROLE = "DO $$ BEGIN CREATE ROLE clinical_app; EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
_CREATE_ROLE_RETENTION = (
    "DO $$ BEGIN CREATE ROLE clinical_retention; EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
)

NOW = datetime.datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
_COLLECTED_AT = datetime.datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
_ADULT_DOB = date(1990, 1, 1)
_MINOR_DOB = date(2020, 1, 1)  # ~6 years old at _COLLECTED_AT -- a minor


@pytest.fixture()
def conn():
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    if not DSN:
        pytest.skip("CLINICAL_TEST_DSN not set")
    connection = psycopg.connect(DSN, autocommit=False, connect_timeout=10)
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute(_CREATE_ROLE)
        cur.execute(_CREATE_ROLE_RETENTION)
        cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def dao(conn):
    return DataAccess(conn, clock=SystemClock(), password_hasher=BcryptHasher(rounds=4))


@pytest.fixture
def retention(conn):
    return RetentionPrincipal(conn)


@pytest.fixture
def org_a(dao):
    return dao.create_organisation("Org A")


def _fetchone(conn, sql, params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _admin_session(dao, conn, org_id, email):
    user_id = dao.create_user(org_id, email, "password")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
            "VALUES (%s, %s, 'Administrator', %s, %s)",
            (user_id, org_id, user_id, datetime.datetime(2026, 9, 1, tzinfo=timezone.utc)),
        )
    conn.commit()
    return dao.login(email, org_id, "password")


@pytest.fixture
def session_a(dao, org_a, conn):
    return _admin_session(dao, conn, org_a, "admin@org-a.test")


def _full_chain(dao, conn, session, suffix="1", dob=_ADULT_DOB):
    """
    patient -> order -> sample -> sequencing_run -> vcf -> interpretation ->
    report. Returns dict of ids.

    The vcf row is inserted directly (bypassing create_vcf, which needs a
    real file on disk to hash) -- but its retention_days is resolved via the
    SAME pure function create_vcf itself calls
    (clinical.retention.resolve_vcf_retention_days), against whatever this
    org's own 'vcf' policy row says (if any), exactly mirroring
    DataAccess._resolve_vcf_retention_days's own query shape. This is what
    lets a test configure a custom policy via _set_policy BEFORE calling
    this helper and have it actually take effect, the same as it would
    through create_vcf in production.
    """
    org_id = session.org_id
    now = _COLLECTED_AT

    test_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tests (test_id, org_id, name, assembly, status, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (test_id, org_id, f"Panel{suffix}", "GRCh38", "active", now),
        )
    conn.commit()

    patient_id = dao.create_patient(session, f"Test{suffix}", dob, "M")
    consent_id = dao.record_consent(session, patient_id, "testing")
    order_id = dao.create_order(
        session,
        patient_id=patient_id,
        test_id=test_id,
        consent_id=consent_id,
        required_scope="testing",
        priority="routine",
    )

    policy = _fetchone(
        conn,
        "SELECT retention_days, retention_days_minor FROM retention_policies WHERE org_id = %s AND artefact_class = 'vcf'",
        (org_id,),
    )
    policy_adult_days, policy_minor_days = policy if policy else (None, None)
    retention_days = resolve_vcf_retention_days(
        dob, now.date(), policy_adult_days=policy_adult_days, policy_minor_days=policy_minor_days
    )

    sample_id = uuid.uuid4()
    run_id = uuid.uuid4()
    vcf_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO samples (sample_id, order_id, org_id, type, collected_at, collected_by, qc_status) "
            "VALUES (%s, %s, %s, 'blood', %s, %s, 'passed')",
            (sample_id, order_id, org_id, now, session.user_id),
        )
        cur.execute(
            "INSERT INTO sequencing_runs (org_id, id, sample_id, created_at, created_by) VALUES (%s, %s, %s, %s, %s)",
            (org_id, run_id, sample_id, now, session.user_id),
        )
        cur.execute(
            "INSERT INTO vcfs (org_id, id, sequencing_run_id, vcf_path, content_hash, created_at, created_by, "
            "retention_days) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (org_id, vcf_id, run_id, f"/tmp/r_{vcf_id}.vcf", "0" * 64, now, session.user_id, retention_days),
        )
    conn.commit()

    interp_id = dao.create_interpretation(session, vcf_id, {"model_version": "v1"}, str(sample_id))
    report_id = dao.create_report(session, interp_id)
    return {"vcf_id": vcf_id, "interp_id": interp_id, "report_id": report_id, "retention_days": retention_days}


def _release(conn, session, report_id, released_at):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO release_events (org_id, id, report_id, consumer, released_at, released_by, content_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (session.org_id, uuid.uuid4(), report_id, "clinician", released_at, session.user_id, "1" * 64),
        )
    conn.commit()


def _set_vcf_policy(dao, session, retention_days, retention_days_minor=None):
    dao.set_retention_policy(session, "vcf", retention_days, retention_days_minor)


class TestResolveVcfRetentionDaysPureFunction:
    """
    The age-conditional rule itself (human ruling D2, 2026-09-09), tested
    independent of any database -- resolve_vcf_retention_days is a pure
    function precisely so this is possible.
    """

    def test_adult_gets_the_product_default(self):
        assert resolve_vcf_retention_days(_ADULT_DOB, _COLLECTED_AT.date()) == DEFAULT_ADULT_VCF_RETENTION_DAYS

    def test_minor_gets_the_product_default(self):
        assert resolve_vcf_retention_days(_MINOR_DOB, _COLLECTED_AT.date()) == DEFAULT_MINOR_VCF_RETENTION_DAYS

    def test_org_adult_override_applies_only_to_adults(self):
        result = resolve_vcf_retention_days(_ADULT_DOB, _COLLECTED_AT.date(), policy_adult_days=100)
        assert result == 100

    def test_org_adult_override_does_not_leak_into_the_minor_branch(self):
        """Setting only the adult override must not implicitly change the minor number."""
        result = resolve_vcf_retention_days(_MINOR_DOB, _COLLECTED_AT.date(), policy_adult_days=100)
        assert result == DEFAULT_MINOR_VCF_RETENTION_DAYS

    def test_org_minor_override_applies_only_to_minors(self):
        result = resolve_vcf_retention_days(_MINOR_DOB, _COLLECTED_AT.date(), policy_minor_days=9999)
        assert result == 9999

    def test_org_minor_override_does_not_leak_into_the_adult_branch(self):
        result = resolve_vcf_retention_days(_ADULT_DOB, _COLLECTED_AT.date(), policy_minor_days=9999)
        assert result == DEFAULT_ADULT_VCF_RETENTION_DAYS

    def test_both_overrides_independently_applied(self):
        assert (
            resolve_vcf_retention_days(_ADULT_DOB, _COLLECTED_AT.date(), policy_adult_days=111, policy_minor_days=222)
            == 111
        )
        assert (
            resolve_vcf_retention_days(_MINOR_DOB, _COLLECTED_AT.date(), policy_adult_days=111, policy_minor_days=222)
            == 222
        )

    def test_exactly_at_the_majority_boundary_is_not_a_minor(self):
        """A patient whose MINOR_AGE_THRESHOLD_YEARS-th birthday is exactly the
        collection date has already reached majority -- not still a minor."""
        dob = date(2008, 8, 1)
        collected_at = date(dob.year + MINOR_AGE_THRESHOLD_YEARS, dob.month, dob.day)
        assert is_minor_at(dob, collected_at) is False
        assert resolve_vcf_retention_days(dob, collected_at) == DEFAULT_ADULT_VCF_RETENTION_DAYS

    def test_one_day_before_the_majority_boundary_is_still_a_minor(self):
        dob = date(2008, 8, 1)
        collected_at = date(dob.year + MINOR_AGE_THRESHOLD_YEARS, dob.month, dob.day) - timedelta(days=1)
        assert is_minor_at(dob, collected_at) is True
        assert resolve_vcf_retention_days(dob, collected_at) == DEFAULT_MINOR_VCF_RETENTION_DAYS


class TestRetentionPolicyConfiguration:
    def test_administrator_can_set_and_get(self, dao, session_a):
        dao.set_retention_policy(session_a, "vcf", 30)
        assert dao.get_retention_policy(session_a, "vcf") == 30

    def test_unset_class_returns_none(self, dao, session_a):
        assert dao.get_retention_policy(session_a, "vcf") is None

    def test_minor_override_unset_by_default(self, dao, session_a):
        dao.set_retention_policy(session_a, "vcf", 30)
        assert dao.get_retention_policy_minor(session_a, "vcf") is None

    def test_minor_override_can_be_set_independently_of_adult(self, dao, session_a):
        dao.set_retention_policy(session_a, "vcf", 30, retention_days_minor=60)
        assert dao.get_retention_policy(session_a, "vcf") == 30
        assert dao.get_retention_policy_minor(session_a, "vcf") == 60

    def test_report_is_no_longer_a_configurable_class(self, dao, session_a):
        """Human ruling D2, re-ruled 2026-09-09: report/run_document inherit
        from the vcf and are no longer independently configurable -- see
        RETENTION_ARTEFACT_CLASSES's own comment (an engineering decision)."""
        with pytest.raises(ValueError, match="Unknown retention artefact_class"):
            dao.set_retention_policy(session_a, "report", 30)

    def test_run_document_is_no_longer_a_configurable_class(self, dao, session_a):
        with pytest.raises(ValueError, match="Unknown retention artefact_class"):
            dao.set_retention_policy(session_a, "run_document", 30)

    def test_unknown_artefact_class_rejected(self, dao, session_a):
        with pytest.raises(ValueError, match="Unknown retention artefact_class"):
            dao.set_retention_policy(session_a, "raw_reads", 30)

    def test_non_positive_days_rejected(self, dao, session_a):
        with pytest.raises(ValueError, match="positive"):
            dao.set_retention_policy(session_a, "vcf", 0)

    def test_non_positive_minor_days_rejected(self, dao, session_a):
        with pytest.raises(ValueError, match="positive"):
            dao.set_retention_policy(session_a, "vcf", 30, retention_days_minor=0)

    def test_setting_again_updates_not_duplicates(self, dao, session_a, conn):
        dao.set_retention_policy(session_a, "vcf", 30)
        dao.set_retention_policy(session_a, "vcf", 90)
        assert dao.get_retention_policy(session_a, "vcf") == 90

        count = _fetchone(
            conn,
            "SELECT COUNT(*) FROM retention_policies WHERE org_id = %s AND artefact_class = 'vcf'",
            (session_a.org_id,),
        )[0]
        assert count == 1

    def test_non_administrator_rejected(self, dao, conn, session_a):
        interpreter_id = dao.create_user(session_a.org_id, "interp@org-a.test", "password")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                "VALUES (%s, %s, 'Interpreter', %s, %s)",
                (interpreter_id, session_a.org_id, session_a.user_id, NOW),
            )
        conn.commit()
        interp_session = dao.login("interp@org-a.test", session_a.org_id, "password")

        with pytest.raises(AuthorizationError):
            dao.set_retention_policy(interp_session, "vcf", 30)


# ═══════════════════════════════════════════════════════════════════════════
# THE ACCEPTANCE TEST, human ruling D2 (2026-09-09), red-first evidence sent
# to god alongside the diff: "a minor's lineage -- create the chain, confirm
# EVERY artefact resolves to ten; then an adult's, confirm five." Declared
# BEFORE running, against the CURRENT (unmodified) schema/code at commit
# 737c0bc: EXPECTED RESULT was FAILURE for both directions, same failure
# mode (psycopg.errors.UndefinedColumn: column "retention_days" does not
# exist), because neither vcfs, interpretations, nor reports had the column
# at all -- confirmed failing before any production code changed. These are
# now the GREEN assertions against the actual resolved values, not merely
# "a column exists" -- the red run only had to prove the column's absence;
# this is the real claim.
# ═══════════════════════════════════════════════════════════════════════════


class TestAgeConditionalLineagePropagation:
    def test_minor_lineage_every_artefact_resolves_to_ten_years(self, dao, conn, session_a, retention):
        dao._create_system_session(session_a.org_id)
        chain = _full_chain(dao, conn, session_a, dob=_MINOR_DOB)

        assert chain["retention_days"] == DEFAULT_MINOR_VCF_RETENTION_DAYS
        for table, id_column, row_id in (
            ("vcfs", "id", chain["vcf_id"]),
            ("interpretations", "id", chain["interp_id"]),
            ("reports", "id", chain["report_id"]),
        ):
            row = _fetchone(conn, f"SELECT retention_days FROM {table} WHERE {id_column} = %s", (row_id,))
            assert row[0] == DEFAULT_MINOR_VCF_RETENTION_DAYS, f"{table} did not inherit the minor period"

    def test_adult_lineage_every_artefact_resolves_to_five_years(self, dao, conn, session_a, retention):
        dao._create_system_session(session_a.org_id)
        chain = _full_chain(dao, conn, session_a, dob=_ADULT_DOB)

        assert chain["retention_days"] == DEFAULT_ADULT_VCF_RETENTION_DAYS
        for table, id_column, row_id in (
            ("vcfs", "id", chain["vcf_id"]),
            ("interpretations", "id", chain["interp_id"]),
            ("reports", "id", chain["report_id"]),
        ):
            row = _fetchone(conn, f"SELECT retention_days FROM {table} WHERE {id_column} = %s", (row_id,))
            assert row[0] == DEFAULT_ADULT_VCF_RETENTION_DAYS, f"{table} did not inherit the adult period"

    def test_org_override_propagates_down_the_lineage_too(self, dao, conn, session_a):
        """Not just the product default -- an org's own configured override
        must reach every downstream artefact exactly the same way."""
        _set_vcf_policy(dao, session_a, retention_days=111, retention_days_minor=222)
        adult_chain = _full_chain(dao, conn, session_a, suffix="adult", dob=_ADULT_DOB)
        minor_chain = _full_chain(dao, conn, session_a, suffix="minor", dob=_MINOR_DOB)

        assert adult_chain["retention_days"] == 111
        assert minor_chain["retention_days"] == 222
        for chain, expected in ((adult_chain, 111), (minor_chain, 222)):
            for table, row_id in (
                ("vcfs", chain["vcf_id"]),
                ("interpretations", chain["interp_id"]),
                ("reports", chain["report_id"]),
            ):
                row = _fetchone(conn, f"SELECT retention_days FROM {table} WHERE id = %s", (row_id,))
                assert row[0] == expected

    def test_reanalysis_inherits_the_same_vcf_retention_days_not_the_current_default(self, dao, conn, session_a):
        """A re-analysis is a NEW interpretation of the SAME vcf (spec 15.3)
        -- it must inherit that vcf's already-resolved value even if the
        org's policy changes in between, since the vcf's own period was
        fixed at ITS creation, not recomputed per descendant."""
        chain = _full_chain(dao, conn, session_a, dob=_ADULT_DOB)
        assert chain["retention_days"] == DEFAULT_ADULT_VCF_RETENTION_DAYS

        # Policy changes AFTER the vcf was created -- must not retroactively
        # change what a new interpretation of the SAME vcf inherits.
        _set_vcf_policy(dao, session_a, retention_days=999)

        reanalysis_id = dao.create_reanalysis(session_a, chain["vcf_id"], chain["interp_id"], {"model_version": "v2"})
        row = _fetchone(conn, "SELECT retention_days FROM interpretations WHERE id = %s", (reanalysis_id,))
        assert row[0] == DEFAULT_ADULT_VCF_RETENTION_DAYS

    def test_amendment_report_inherits_the_original_reports_retention_days(self, dao, conn, session_a):
        chain = _full_chain(dao, conn, session_a, dob=_MINOR_DOB)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reports SET state = 'approved', approver_id = %s, approved_at = %s, content_hash = %s "
                "WHERE id = %s",
                (session_a.user_id, NOW, "a" * 64, chain["report_id"]),
            )
        conn.commit()

        amendment_report_id, _ = dao.create_amendment(session_a, chain["report_id"], "revised findings")

        row = _fetchone(conn, "SELECT retention_days FROM reports WHERE id = %s", (amendment_report_id,))
        assert row[0] == DEFAULT_MINOR_VCF_RETENTION_DAYS


class TestPurgeExpiredReport:
    def test_uses_the_product_default_when_the_org_has_no_override(self, dao, conn, session_a, retention):
        """No policy configured -- purge must use DEFAULT_ADULT_VCF_RETENTION_DAYS,
        not refuse. Released well within that window: not yet purgeable."""
        dao._create_system_session(session_a.org_id)
        chain = _full_chain(dao, conn, session_a)
        _release(conn, session_a, chain["report_id"], NOW - timedelta(days=100))

        result = retention.purge_expired(session_a.org_id, "report", now=NOW)

        assert chain["report_id"] not in result.tombstoned_ids

    def test_tombstones_a_report_past_its_retention_window(self, dao, conn, session_a, retention):
        dao._create_system_session(session_a.org_id)
        _set_vcf_policy(dao, session_a, 30)
        chain = _full_chain(dao, conn, session_a)
        _release(conn, session_a, chain["report_id"], NOW - timedelta(days=31))

        result = retention.purge_expired(session_a.org_id, "report", now=NOW)

        assert chain["report_id"] in result.tombstoned_ids
        row = _fetchone(conn, "SELECT tombstoned_at, tombstoned_by FROM reports WHERE id = %s", (chain["report_id"],))
        assert row[0] is not None
        assert row[1] is not None

    def test_leaves_a_report_not_yet_past_its_window(self, dao, conn, session_a, retention):
        _set_vcf_policy(dao, session_a, 30)
        chain = _full_chain(dao, conn, session_a)
        _release(conn, session_a, chain["report_id"], NOW - timedelta(days=5))

        result = retention.purge_expired(session_a.org_id, "report", now=NOW)

        assert chain["report_id"] not in result.tombstoned_ids
        row = _fetchone(conn, "SELECT tombstoned_at FROM reports WHERE id = %s", (chain["report_id"],))
        assert row[0] is None

    def test_leaves_an_unreleased_report_alone(self, dao, conn, session_a, retention):
        _set_vcf_policy(dao, session_a, 30)
        chain = _full_chain(dao, conn, session_a)
        # no _release() call -- no release_events row exists

        result = retention.purge_expired(session_a.org_id, "report", now=NOW)

        assert chain["report_id"] not in result.tombstoned_ids

    def test_writes_an_audit_entry_per_purge(self, dao, conn, session_a, retention):
        dao._create_system_session(session_a.org_id)
        _set_vcf_policy(dao, session_a, 30)
        chain = _full_chain(dao, conn, session_a)
        _release(conn, session_a, chain["report_id"], NOW - timedelta(days=31))

        retention.purge_expired(session_a.org_id, "report", now=NOW)

        row = _fetchone(
            conn,
            "SELECT action, actor_role, resource_id, outcome FROM audit_log "
            "WHERE org_id = %s AND action = 'retention_purge' AND resource_id = %s",
            (session_a.org_id, str(chain["report_id"])),
        )
        assert row is not None
        assert row[1] == "System"
        assert row[3] == "success"

    def test_already_tombstoned_report_not_purged_again(self, dao, conn, session_a, retention):
        dao._create_system_session(session_a.org_id)
        _set_vcf_policy(dao, session_a, 30)
        chain = _full_chain(dao, conn, session_a)
        _release(conn, session_a, chain["report_id"], NOW - timedelta(days=31))

        first = retention.purge_expired(session_a.org_id, "report", now=NOW)
        second = retention.purge_expired(session_a.org_id, "report", now=NOW)

        assert chain["report_id"] in first.tombstoned_ids
        assert chain["report_id"] not in second.tombstoned_ids

    def test_no_system_principal_raises(self, dao, conn, session_a, retention):
        _set_vcf_policy(dao, session_a, 30)
        chain = _full_chain(dao, conn, session_a)
        _release(conn, session_a, chain["report_id"], NOW - timedelta(days=31))
        # no dao._create_system_session(org_id) call -- org has no system principal

        with pytest.raises(NoSystemPrincipalError):
            retention.purge_expired(session_a.org_id, "report", now=NOW)


class TestPurgeExpiredReportDescendantGuard:
    """Human ruling D6: an artefact with a live descendant cannot expire."""

    def test_a_report_with_a_live_amendment_is_blocked(self, dao, conn, session_a, retention):
        dao._create_system_session(session_a.org_id)
        _set_vcf_policy(dao, session_a, 30)
        chain = _full_chain(dao, conn, session_a)
        _release(conn, session_a, chain["report_id"], NOW - timedelta(days=31))

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reports SET state = 'approved', approver_id = %s, approved_at = %s, content_hash = %s WHERE id = %s",
                (session_a.user_id, NOW - timedelta(days=40), "a" * 64, chain["report_id"]),
            )
        conn.commit()
        amendment_report_id, _ = dao.create_amendment(session_a, chain["report_id"], "revised findings")

        result = retention.purge_expired(session_a.org_id, "report", now=NOW)

        assert chain["report_id"] not in result.tombstoned_ids
        row = _fetchone(conn, "SELECT tombstoned_at FROM reports WHERE id = %s", (chain["report_id"],))
        assert row[0] is None

    def test_once_the_amendment_report_is_tombstoned_the_original_is_purgeable(self, dao, conn, session_a, retention):
        dao._create_system_session(session_a.org_id)
        _set_vcf_policy(dao, session_a, 30)
        chain = _full_chain(dao, conn, session_a)
        _release(conn, session_a, chain["report_id"], NOW - timedelta(days=31))

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reports SET state = 'approved', approver_id = %s, approved_at = %s, content_hash = %s WHERE id = %s",
                (session_a.user_id, NOW - timedelta(days=40), "a" * 64, chain["report_id"]),
            )
        conn.commit()
        amendment_report_id, _ = dao.create_amendment(session_a, chain["report_id"], "revised findings")

        # Manually tombstone the amendment's own report to simulate it having cleared its own window.
        system_id = _fetchone(
            conn, "SELECT user_id FROM users WHERE org_id = %s AND is_system_account = true", (session_a.org_id,)
        )[0]
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reports SET tombstoned_at = %s, tombstoned_by = %s WHERE id = %s",
                (NOW, system_id, amendment_report_id),
            )
        conn.commit()

        result = retention.purge_expired(session_a.org_id, "report", now=NOW)

        assert chain["report_id"] in result.tombstoned_ids


class TestPurgeExpiredInterpretation:
    def test_blocked_while_its_own_report_is_live(self, dao, conn, session_a, retention):
        dao._create_system_session(session_a.org_id)
        _set_vcf_policy(dao, session_a, 30)
        chain = _full_chain(dao, conn, session_a)
        _release(conn, session_a, chain["report_id"], NOW - timedelta(days=31))
        # report itself never tombstoned

        result = retention.purge_expired(session_a.org_id, "run_document", now=NOW)

        assert chain["interp_id"] not in result.tombstoned_ids

    def test_blocked_by_a_live_child_reanalysis(self, dao, conn, session_a, retention):
        dao._create_system_session(session_a.org_id)
        _set_vcf_policy(dao, session_a, 30)
        chain = _full_chain(dao, conn, session_a)
        _release(conn, session_a, chain["report_id"], NOW - timedelta(days=31))
        system_id = _fetchone(
            conn, "SELECT user_id FROM users WHERE org_id = %s AND is_system_account = true", (session_a.org_id,)
        )[0]
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reports SET tombstoned_at = %s, tombstoned_by = %s WHERE id = %s",
                (NOW, system_id, chain["report_id"]),
            )
        conn.commit()

        # child re-analysis, no report of its own -- still blocks the parent
        dao.create_reanalysis(session_a, chain["vcf_id"], chain["interp_id"], {"model_version": "v2"})

        result = retention.purge_expired(session_a.org_id, "run_document", now=NOW)

        assert chain["interp_id"] not in result.tombstoned_ids

    def test_purged_once_report_tombstoned_and_no_live_child(self, dao, conn, session_a, retention):
        dao._create_system_session(session_a.org_id)
        _set_vcf_policy(dao, session_a, 30)
        chain = _full_chain(dao, conn, session_a)
        _release(conn, session_a, chain["report_id"], NOW - timedelta(days=31))
        system_id = _fetchone(
            conn, "SELECT user_id FROM users WHERE org_id = %s AND is_system_account = true", (session_a.org_id,)
        )[0]
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reports SET tombstoned_at = %s, tombstoned_by = %s WHERE id = %s",
                (NOW, system_id, chain["report_id"]),
            )
        conn.commit()

        result = retention.purge_expired(session_a.org_id, "run_document", now=NOW)

        assert chain["interp_id"] in result.tombstoned_ids


class TestPurgeExpiredVcf:
    def test_blocked_by_a_live_interpretation(self, dao, conn, session_a, retention):
        dao._create_system_session(session_a.org_id)
        _set_vcf_policy(dao, session_a, 30)
        chain = _full_chain(dao, conn, session_a)
        _release(conn, session_a, chain["report_id"], NOW - timedelta(days=31))
        # interpretation itself never tombstoned

        result = retention.purge_expired(session_a.org_id, "vcf", now=NOW)

        assert chain["vcf_id"] not in result.tombstoned_ids

    def test_purged_once_its_interpretation_is_tombstoned(self, dao, conn, session_a, retention):
        dao._create_system_session(session_a.org_id)
        _set_vcf_policy(dao, session_a, 30)
        chain = _full_chain(dao, conn, session_a)
        _release(conn, session_a, chain["report_id"], NOW - timedelta(days=31))
        system_id = _fetchone(
            conn, "SELECT user_id FROM users WHERE org_id = %s AND is_system_account = true", (session_a.org_id,)
        )[0]
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reports SET tombstoned_at = %s, tombstoned_by = %s WHERE id = %s",
                (NOW, system_id, chain["report_id"]),
            )
            cur.execute(
                "UPDATE interpretations SET tombstoned_at = %s, tombstoned_by = %s WHERE id = %s",
                (NOW, system_id, chain["interp_id"]),
            )
        conn.commit()

        result = retention.purge_expired(session_a.org_id, "vcf", now=NOW)

        assert chain["vcf_id"] in result.tombstoned_ids


class TestPurgeSkipsRowsWithNoResolvedRetentionDays:
    """A row inserted outside DataAccess (e.g. by an older schema, or a raw
    test fixture) can carry retention_days = NULL. Never purge-eligible --
    absence of a resolved period blocks a purge, it can never permit one."""

    def test_a_report_with_null_retention_days_is_never_purged(self, dao, conn, session_a, retention):
        dao._create_system_session(session_a.org_id)
        _set_vcf_policy(dao, session_a, 30)
        chain = _full_chain(dao, conn, session_a)
        _release(conn, session_a, chain["report_id"], NOW - timedelta(days=3650))

        with conn.cursor() as cur:
            cur.execute("UPDATE reports SET retention_days = NULL WHERE id = %s", (chain["report_id"],))
        conn.commit()

        result = retention.purge_expired(session_a.org_id, "report", now=NOW)

        assert chain["report_id"] not in result.tombstoned_ids
