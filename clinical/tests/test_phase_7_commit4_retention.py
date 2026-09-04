"""
tests/test_phase_7_commit4_retention.py
─────────────────────────────────────────

Phase 7 commit 4: retention (spec 22), all nine decisions ruled on card
phase7-c4-retention. This file tests both halves: the ordinary
clinical_app-privileged configuration surface (DataAccess.set_retention_policy
/ get_retention_policy) and the separate clinical_retention-privileged purge
mechanism (clinical.retention.RetentionPrincipal).

KNOWN TESTING LIMITATION, STATED RATHER THAN LEFT SILENT: like every other
test file in this codebase, RetentionPrincipal is exercised here over the
same superuser test connection everything else uses, not a real login as
the clinical_retention role -- no test file in clinical/tests/ actually
connects AS clinical_app either (grep confirms it; the role only needs to
exist for schema.sql's GRANT statements to apply without error). So this
file proves the PURGE LOGIC -- which rows get tombstoned, the D6 guard, the
fail-loud missing-policy behaviour -- but does not prove the GRANT/REVOKE
block in schema.sql actually blocks a real clinical_retention login from
issuing DELETE, or blocks clinical_app from reaching reviewer_claims/
amendments. That would need a genuine second connection with real
clinical_retention credentials, which the existing fixture pattern does not
provide for any role. Naming the gap rather than assuming it is covered.
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
    NoSystemPrincipalError,
    RetentionPolicyMissingError,
    RetentionPrincipal,
    seed_nabl_default_retention_policies,
)

DSN = os.getenv("CLINICAL_TEST_DSN")
SCHEMA_PATH = __import__("pathlib").Path(__file__).parent.parent / "schema.sql"

_CREATE_ROLE = "DO $$ BEGIN CREATE ROLE clinical_app; EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
_CREATE_ROLE_RETENTION = (
    "DO $$ BEGIN CREATE ROLE clinical_retention; EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
)

NOW = datetime.datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)


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


def _full_chain(dao, conn, session, suffix="1"):
    """patient -> order -> sample -> sequencing_run -> vcf -> interpretation -> report. Returns dict of ids."""
    org_id = session.org_id
    now = datetime.datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)

    test_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tests (test_id, org_id, name, assembly, status, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (test_id, org_id, f"Panel{suffix}", "GRCh38", "active", now),
        )
    conn.commit()

    patient_id = dao.create_patient(session, f"Test{suffix}", date(1990, 1, 1), "M")
    consent_id = dao.record_consent(session, patient_id, "testing")
    order_id = dao.create_order(
        session,
        patient_id=patient_id,
        test_id=test_id,
        consent_id=consent_id,
        required_scope="testing",
        priority="routine",
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
            "INSERT INTO vcfs (org_id, id, sequencing_run_id, vcf_path, content_hash, created_at, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (org_id, vcf_id, run_id, f"/tmp/r_{vcf_id}.vcf", "0" * 64, now, session.user_id),
        )
    conn.commit()

    interp_id = dao.create_interpretation(session, vcf_id, {"model_version": "v1"}, str(sample_id))
    report_id = dao.create_report(session, interp_id)
    return {"vcf_id": vcf_id, "interp_id": interp_id, "report_id": report_id}


def _release(conn, session, report_id, released_at):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO release_events (org_id, id, report_id, consumer, released_at, released_by, content_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (session.org_id, uuid.uuid4(), report_id, "clinician", released_at, session.user_id, "1" * 64),
        )
    conn.commit()


def _set_policy(dao, session, artefact_class, days):
    dao.set_retention_policy(session, artefact_class, days)


class TestRetentionPolicyConfiguration:
    def test_administrator_can_set_and_get(self, dao, session_a):
        dao.set_retention_policy(session_a, "report", 30)
        assert dao.get_retention_policy(session_a, "report") == 30

    def test_unset_class_returns_none(self, dao, session_a):
        assert dao.get_retention_policy(session_a, "vcf") is None

    def test_unknown_artefact_class_rejected(self, dao, session_a):
        with pytest.raises(ValueError, match="Unknown retention artefact_class"):
            dao.set_retention_policy(session_a, "raw_reads", 30)

    def test_non_positive_days_rejected(self, dao, session_a):
        with pytest.raises(ValueError, match="positive"):
            dao.set_retention_policy(session_a, "report", 0)

    def test_setting_again_updates_not_duplicates(self, dao, session_a, conn):
        dao.set_retention_policy(session_a, "report", 30)
        dao.set_retention_policy(session_a, "report", 90)
        assert dao.get_retention_policy(session_a, "report") == 90

        count = _fetchone(
            conn,
            "SELECT COUNT(*) FROM retention_policies WHERE org_id = %s AND artefact_class = 'report'",
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
            dao.set_retention_policy(interp_session, "report", 30)


# Not a real-world number, deliberately: proves the mechanism (set_retention_policy
# + purge_expired work end to end on a real, configured artefact_class)
# without asserting anything that could be mistaken for a production period.
# NABL_VCF_RETENTION_DAYS itself is NOT used as a configured value anywhere
# in this class -- human ruling 2026-09-05: entering it as a production
# default would answer a question (how long do we actually keep an adult
# VCF) that counsel, not this codebase, must answer. See
# seed_nabl_default_retention_policies's own docstring for the full argument.
_TEST_ONLY_VCF_DAYS = 30


class TestSeedNablDefaultRetentionPolicies:
    """
    COUNSEL-d2's engineering half. As of the human's 2026-09-05 ruling, the
    PRODUCTION call (no vcf_retention_days argument) configures NOTHING --
    "vcf" gets the same treatment as "run_document" and "report", all three
    awaiting counsel. The function still accepts an explicit
    vcf_retention_days so the underlying mechanism stays provable against a
    real, configured class; tests here drive it with _TEST_ONLY_VCF_DAYS, not
    the NABL constant. The refusal path is what matters either way -- purging
    an unconfigured class must fail loudly with the specific error, not a
    base class, same discipline as everywhere else the raises(Exception)
    rule is carded.
    """

    def test_production_call_configures_nothing(self, dao, session_a):
        """The default call, as a production caller would make it."""
        seed_nabl_default_retention_policies(dao, session_a)
        assert dao.get_retention_policy(session_a, "vcf") is None
        assert dao.get_retention_policy(session_a, "run_document") is None
        assert dao.get_retention_policy(session_a, "report") is None

    def test_explicit_test_only_value_configures_vcf(self, dao, session_a):
        seed_nabl_default_retention_policies(dao, session_a, vcf_retention_days=_TEST_ONLY_VCF_DAYS)
        assert dao.get_retention_policy(session_a, "vcf") == _TEST_ONLY_VCF_DAYS

    def test_explicit_test_only_value_still_leaves_run_document_and_report_unconfigured(self, dao, session_a):
        seed_nabl_default_retention_policies(dao, session_a, vcf_retention_days=_TEST_ONLY_VCF_DAYS)
        assert dao.get_retention_policy(session_a, "run_document") is None
        assert dao.get_retention_policy(session_a, "report") is None

    def test_seeded_vcf_policy_is_past_its_window_but_blocked_by_its_live_interpretation(
        self, dao, conn, session_a, retention
    ):
        """
        NOT a "purge succeeds" test -- it deliberately cannot be one under
        this config. D6 blocks a vcf purge behind any not-yet-tombstoned
        interpretation of it (TestPurgeExpiredVcf below proves the same
        guard directly), and interpretations only tombstone via the
        "run_document" class, which stays unconfigured here on purpose
        (awaiting counsel). So a vcf whose date window has genuinely passed
        is STILL correctly refused -- not because the date check failed,
        but because nothing can retire its interpretation yet. Worth
        stating plainly: configuring "vcf" alone is safe (nothing purges
        prematurely) but not yet SUFFICIENT on its own to ever tombstone
        anything, until "run_document" also has a policy -- true whether
        "vcf" is configured via a real counsel-supplied period or, as here,
        a test-only one.
        """
        dao._create_system_session(session_a.org_id)
        seed_nabl_default_retention_policies(dao, session_a, vcf_retention_days=_TEST_ONLY_VCF_DAYS)
        chain = _full_chain(dao, conn, session_a)
        _release(conn, session_a, chain["report_id"], NOW - timedelta(days=_TEST_ONLY_VCF_DAYS + 1))

        result = retention.purge_expired(session_a.org_id, "vcf", now=NOW)
        assert result.tombstoned_ids == []

    def test_purge_against_unconfigured_run_document_fails_loudly(self, dao, conn, session_a, retention):
        seed_nabl_default_retention_policies(dao, session_a, vcf_retention_days=_TEST_ONLY_VCF_DAYS)
        _full_chain(dao, conn, session_a)

        with pytest.raises(RetentionPolicyMissingError):
            retention.purge_expired(session_a.org_id, "run_document", now=NOW)

    def test_purge_against_unconfigured_report_fails_loudly(self, dao, conn, session_a, retention):
        seed_nabl_default_retention_policies(dao, session_a, vcf_retention_days=_TEST_ONLY_VCF_DAYS)
        _full_chain(dao, conn, session_a)

        with pytest.raises(RetentionPolicyMissingError):
            retention.purge_expired(session_a.org_id, "report", now=NOW)


class TestPurgeExpiredReport:
    def test_fails_loudly_without_a_configured_policy(self, dao, conn, session_a, retention):
        chain = _full_chain(dao, conn, session_a)
        _release(conn, session_a, chain["report_id"], NOW - timedelta(days=100))

        with pytest.raises(RetentionPolicyMissingError):
            retention.purge_expired(session_a.org_id, "report", now=NOW)

    def test_tombstones_a_report_past_its_retention_window(self, dao, conn, session_a, retention):
        dao._create_system_session(session_a.org_id)
        _set_policy(dao, session_a, "report", 30)
        chain = _full_chain(dao, conn, session_a)
        _release(conn, session_a, chain["report_id"], NOW - timedelta(days=31))

        result = retention.purge_expired(session_a.org_id, "report", now=NOW)

        assert chain["report_id"] in result.tombstoned_ids
        row = _fetchone(conn, "SELECT tombstoned_at, tombstoned_by FROM reports WHERE id = %s", (chain["report_id"],))
        assert row[0] is not None
        assert row[1] is not None

    def test_leaves_a_report_not_yet_past_its_window(self, dao, conn, session_a, retention):
        _set_policy(dao, session_a, "report", 30)
        chain = _full_chain(dao, conn, session_a)
        _release(conn, session_a, chain["report_id"], NOW - timedelta(days=5))

        result = retention.purge_expired(session_a.org_id, "report", now=NOW)

        assert chain["report_id"] not in result.tombstoned_ids
        row = _fetchone(conn, "SELECT tombstoned_at FROM reports WHERE id = %s", (chain["report_id"],))
        assert row[0] is None

    def test_leaves_an_unreleased_report_alone(self, dao, conn, session_a, retention):
        _set_policy(dao, session_a, "report", 30)
        chain = _full_chain(dao, conn, session_a)
        # no _release() call -- no release_events row exists

        result = retention.purge_expired(session_a.org_id, "report", now=NOW)

        assert chain["report_id"] not in result.tombstoned_ids

    def test_writes_an_audit_entry_per_purge(self, dao, conn, session_a, retention):
        dao._create_system_session(session_a.org_id)
        _set_policy(dao, session_a, "report", 30)
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
        _set_policy(dao, session_a, "report", 30)
        chain = _full_chain(dao, conn, session_a)
        _release(conn, session_a, chain["report_id"], NOW - timedelta(days=31))

        first = retention.purge_expired(session_a.org_id, "report", now=NOW)
        second = retention.purge_expired(session_a.org_id, "report", now=NOW)

        assert chain["report_id"] in first.tombstoned_ids
        assert chain["report_id"] not in second.tombstoned_ids

    def test_no_system_principal_raises(self, dao, conn, session_a, retention):
        _set_policy(dao, session_a, "report", 30)
        chain = _full_chain(dao, conn, session_a)
        _release(conn, session_a, chain["report_id"], NOW - timedelta(days=31))
        # no dao._create_system_session(org_id) call -- org has no system principal

        with pytest.raises(NoSystemPrincipalError):
            retention.purge_expired(session_a.org_id, "report", now=NOW)


class TestPurgeExpiredReportDescendantGuard:
    """Human ruling D6: an artefact with a live descendant cannot expire."""

    def test_a_report_with_a_live_amendment_is_blocked(self, dao, conn, session_a, retention):
        dao._create_system_session(session_a.org_id)
        _set_policy(dao, session_a, "report", 30)
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
        _set_policy(dao, session_a, "report", 30)
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
        _set_policy(dao, session_a, "run_document", 30)
        chain = _full_chain(dao, conn, session_a)
        _release(conn, session_a, chain["report_id"], NOW - timedelta(days=31))
        # report itself never tombstoned

        result = retention.purge_expired(session_a.org_id, "run_document", now=NOW)

        assert chain["interp_id"] not in result.tombstoned_ids

    def test_blocked_by_a_live_child_reanalysis(self, dao, conn, session_a, retention):
        dao._create_system_session(session_a.org_id)
        _set_policy(dao, session_a, "run_document", 30)
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
        _set_policy(dao, session_a, "run_document", 30)
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
        _set_policy(dao, session_a, "vcf", 30)
        chain = _full_chain(dao, conn, session_a)
        _release(conn, session_a, chain["report_id"], NOW - timedelta(days=31))
        # interpretation itself never tombstoned

        result = retention.purge_expired(session_a.org_id, "vcf", now=NOW)

        assert chain["vcf_id"] not in result.tombstoned_ids

    def test_purged_once_its_interpretation_is_tombstoned(self, dao, conn, session_a, retention):
        dao._create_system_session(session_a.org_id)
        _set_policy(dao, session_a, "vcf", 30)
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
