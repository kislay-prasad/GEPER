"""
tests/test_privilege_boundary.py
────────────────────────────────

Does schema.sql's GRANT/REVOKE block actually enforce what it says?

EVERY OTHER FILE IN clinical/tests/ RUNS OVER THE SUPERUSER CONNECTION, which
bypasses grants entirely, and creates the roles without LOGIN purely so
schema.sql's GRANT statements have something to name. Meredith wrote that
limitation into test_phase_7_commit4_retention.py's own docstring rather than
letting it be found later. This file closes it: it opens REAL LOGIN
CONNECTIONS as clinical_app and clinical_retention and asks the database.

WHY THIS BOUNDARY AND NOT ANOTHER. D0(c) was ruled specifically to make
deletion a DATABASE-ENFORCED fact rather than an application convention --
"clinical_app's REVOKE stays ABSOLUTE -- the application still cannot delete,
which is what every claim rests on." Several ISO claims rest on that sentence
being true of the database rather than true of the code that talks to it. Until
this file existed, a typo in the GRANT block, or a REVOKE that silently failed
to apply, would have passed all 340 tests.

HOW THIS FILE AVOIDS PASSING FOR THE WRONG REASON. A permission test that
passes because the connection was dead, the table name was misspelled, or the
assertion caught the wrong exception is indistinguishable from one that passes
because the privilege system works. Three guards, and they are the substance of
the file rather than decoration:

  1. THE EXACT EXCEPTION CLASS. Every refusal asserts
     psycopg.errors.InsufficientPrivilege. Catching bare Exception would pass on
     a typo'd table, a closed connection, or a syntax error -- it would assert
     only that SOMETHING went wrong, which is what a broken test also observes.

  2. THE CONNECTION IS PROVEN LIVE ON THE SAME TABLE it is refused on
     (TestTheConnectionsAreRealAndWorking). A role that cannot do ANYTHING
     passes every refusal test in this file. So each role first demonstrates a
     permission it does hold, against the same object, before its refusals mean
     anything.

  3. THE REFUSAL IS PROVEN CAPABLE OF FAILING
     (TestTheRefusalAssertionCanItselfFail). The grant is temporarily widened,
     the same statement is observed to SUCCEED, and the grant is put back and
     the refusal observed to return. A negative assertion that has only ever
     been seen passing is evidence of nothing; this makes the known-positive
     permanent and re-runnable instead of a one-off observation by whoever
     wrote it.

THIS FILE TESTS THE GRANT BLOCK. IT DOES NOT ADJUST IT. If an assertion here
fails, the finding is that the privilege boundary is not what the schema claims
-- which is a finding to report, not a test to soften.
"""

from __future__ import annotations

import datetime
from datetime import date, timezone
import os
import uuid

import pytest

from clinical.data_access import DataAccess, BcryptHasher, SystemClock

DSN = os.getenv("CLINICAL_TEST_DSN")
SCHEMA_PATH = __import__("pathlib").Path(__file__).parent.parent / "schema.sql"

# Local test fixture credential. Not a secret, not used anywhere but a throwaway
# container database, and deliberately self-describing so it cannot be mistaken
# for a real one if it is ever read out of context.
_LOCAL_TEST_PASSWORD = "not-a-secret-local-test-only"

_ROLES = ("clinical_app", "clinical_retention")

# The six append-only tables. clinical_app may SELECT and INSERT; UPDATE and
# DELETE are revoked from it and from PUBLIC.
APPEND_ONLY = (
    "audit_log",
    "reviewer_claims",
    "release_events",
    "amendments",
    "amendment_notifications",
    "notification_read_receipts",
)

# The three retention-managed tables. clinical_retention may SELECT and UPDATE
# (to write the tombstone); DELETE is revoked -- D4 ruled tombstone, not delete.
RETENTION_TABLES = ("vcfs", "interpretations", "reports")


def _login_dsn(role: str) -> str:
    return DSN


def _connect_as(role: str):
    """
    A real login as `role`. autocommit so that one refused statement does not
    abort a transaction and make every later statement fail with a DIFFERENT
    error -- which would make the rest of a test meaningless while still looking
    like refusals.
    """
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    return psycopg.connect(DSN, user=role, password=_LOCAL_TEST_PASSWORD, autocommit=True)


@pytest.fixture()
def conn():
    """Superuser connection: builds the schema and owns the roles."""
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    if not DSN:
        pytest.skip("CLINICAL_TEST_DSN not set")
    connection = psycopg.connect(DSN, autocommit=False, connect_timeout=10)
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        # WITH LOGIN AND A PASSWORD -- the whole point of this file. Every other
        # fixture creates these roles without login, which is why no existing
        # test can reach the privilege system at all. ALTER as well as CREATE,
        # because the role survives at cluster level between runs and may
        # already exist from an earlier fixture that made it login-less.
        for role in _ROLES:
            cur.execute(f"DO $$ BEGIN CREATE ROLE {role}; EXCEPTION WHEN duplicate_object THEN NULL; END $$;")
            # Inlined, not bound: ALTER ROLE is DDL and takes no placeholder.
            # Safe to interpolate because the constant is a fixed literal
            # defined above with no quote characters in it.
            cur.execute(f"ALTER ROLE {role} LOGIN PASSWORD '{_LOCAL_TEST_PASSWORD}'")
        cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture()
def app_conn(conn):
    c = _connect_as("clinical_app")
    yield c
    c.close()


@pytest.fixture()
def retention_conn(conn):
    c = _connect_as("clinical_retention")
    yield c
    c.close()


@pytest.fixture()
def dao(conn):
    return DataAccess(conn, clock=SystemClock(), password_hasher=BcryptHasher(rounds=4))


@pytest.fixture()
def seeded(dao, conn):
    """
    A real row in each of vcfs, interpretations and reports, plus the session
    that owns them.

    Rows are needed for the PERMITTED direction and only there: an UPDATE that
    matches nothing raises no error and reports zero rows, so "clinical_retention
    may update" would pass against an empty table while proving nothing about
    whether the write can actually land. The refusal tests do not need rows --
    permission is checked before any row is examined.
    """
    org_id = dao.create_organisation("Priv Org")
    user_id = dao.create_user(org_id, "priv@org.test", "password")
    now = datetime.datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
            "VALUES (%s, %s, 'Administrator', %s, %s)",
            (user_id, org_id, user_id, now),
        )
    conn.commit()
    session = dao.login("priv@org.test", org_id, "password")

    test_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tests (test_id, org_id, name, assembly, status, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (test_id, org_id, "Panel", "GRCh38", "active", now),
        )
    conn.commit()

    patient_id = dao.create_patient(session, "Test", date(1990, 1, 1), "M")
    consent_id = dao.record_consent(session, patient_id, "testing")
    order_id = dao.create_order(
        session,
        patient_id=patient_id,
        test_id=test_id,
        consent_id=consent_id,
        required_scope="testing",
        priority="routine",
    )

    sample_id, run_id, vcf_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
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
            (org_id, vcf_id, run_id, f"/tmp/{vcf_id}.vcf", "0" * 64, now, session.user_id),
        )
    conn.commit()

    interp_id = dao.create_interpretation(session, vcf_id, {"model_version": "v1"}, str(sample_id))
    report_id = dao.create_report(session, interp_id)
    return {
        "org_id": org_id,
        "user_id": session.user_id,
        "vcfs": vcf_id,
        "interpretations": interp_id,
        "reports": report_id,
    }


def _insufficient_privilege():
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    return psycopg.errors.InsufficientPrivilege


# ─── guard 2: the connections are real and can actually do something ─────────


class TestTheConnectionsAreRealAndWorking:
    """
    A role that cannot reach the database at all passes every refusal test in
    this file. So before any refusal is believed, each connection demonstrates a
    privilege it DOES hold -- on the same tables it is about to be refused on.
    """

    def test_app_connection_is_really_clinical_app(self, app_conn):
        assert app_conn.execute("SELECT current_user").fetchone()[0] == "clinical_app"

    def test_retention_connection_is_really_clinical_retention(self, retention_conn):
        assert retention_conn.execute("SELECT current_user").fetchone()[0] == "clinical_retention"

    @pytest.mark.parametrize("table", APPEND_ONLY)
    def test_app_may_select_the_tables_it_may_not_rewrite(self, app_conn, table):
        # Proves the refusals below are about UPDATE/DELETE specifically, and
        # not about clinical_app being unable to see the table at all.
        app_conn.execute(f"SELECT count(*) FROM {table}").fetchone()

    @pytest.mark.parametrize("table", RETENTION_TABLES)
    def test_retention_may_select_the_tables_it_may_not_delete_from(self, retention_conn, table):
        retention_conn.execute(f"SELECT count(*) FROM {table}").fetchone()

    def test_app_may_still_insert_into_an_append_only_table(self, app_conn):
        # Append-only means exactly this: the row goes in and never changes.
        # If INSERT were also refused the tables would be unusable, and every
        # refusal test here would pass for the wrong reason.
        app_conn.execute(
            'INSERT INTO audit_log (org_id, user_id, "timestamp", action, details) '
            "VALUES (NULL, NULL, now(), 'privilege_boundary_probe', '{}'::jsonb)"
        )
        assert (
            app_conn.execute("SELECT count(*) FROM audit_log WHERE action = 'privilege_boundary_probe'").fetchone()[0]
            == 1
        )


# ─── clinical_app: UPDATE and DELETE refused on the five append-only tables ──


class TestClinicalAppCannotRewriteAppendOnlyTables:
    """
    The append-only guarantee, asked of the database rather than of the code.
    ISO 15189 7.4.1.8 requires that the original is never modified and never
    withdrawn from the record; a table the application may rewrite cannot meet
    that however carefully the application is written.
    """

    @pytest.mark.parametrize("table", APPEND_ONLY)
    def test_update_is_refused(self, app_conn, table):
        with pytest.raises(_insufficient_privilege()):
            app_conn.execute(f"UPDATE {table} SET org_id = org_id")

    @pytest.mark.parametrize("table", APPEND_ONLY)
    def test_delete_is_refused(self, app_conn, table):
        with pytest.raises(_insufficient_privilege()):
            app_conn.execute(f"DELETE FROM {table}")


# ─── clinical_retention: UPDATE permitted, DELETE refused ────────────────────


class TestClinicalRetentionMayTombstoneButNeverDelete:
    """
    D4 ruled tombstone, not delete. So the retention principal needs UPDATE and
    must not have DELETE -- and both halves have to be true for the ruling to
    mean anything. A role with neither would pass the DELETE test alone.
    """

    @pytest.mark.parametrize("table", RETENTION_TABLES)
    def test_delete_is_refused(self, retention_conn, seeded, table):
        with pytest.raises(_insufficient_privilege()):
            retention_conn.execute(f"DELETE FROM {table}")

    @pytest.mark.parametrize("table", RETENTION_TABLES)
    def test_update_succeeds_and_the_write_actually_lands(self, retention_conn, seeded, table):
        # NOT merely "raises nothing". An UPDATE matching no rows also raises
        # nothing, so the row count and a read-back are what make this a
        # measurement of the grant rather than of an empty table.
        now = datetime.datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
        cur = retention_conn.execute(
            f"UPDATE {table} SET tombstoned_at = %s, tombstoned_by = %s WHERE id = %s",
            (now, seeded["user_id"], seeded[table]),
        )
        assert cur.rowcount == 1, f"clinical_retention's UPDATE on {table} matched no row"
        landed = retention_conn.execute(
            f"SELECT tombstoned_at FROM {table} WHERE id = %s", (seeded[table],)
        ).fetchone()[0]
        assert landed == now, f"the tombstone did not persist on {table}"

    def test_retention_cannot_rewrite_the_append_only_tables_either(self, retention_conn):
        # clinical_retention is narrowly privileged: its UPDATE is scoped to the
        # three retention tables and must not extend to the evidence record.
        with pytest.raises(_insufficient_privilege()):
            retention_conn.execute("UPDATE release_events SET consumer = 'x'")


# ─── guard 3: the refusal assertion is proven capable of failing ─────────────


class TestTheRefusalAssertionCanItselfFail:
    """
    THE KNOWN-POSITIVE, MADE PERMANENT.

    Every assertion above is a negative: something did not happen. A negative
    that has only ever been observed passing is compatible with the test being
    inert -- wrong table, dead connection, exception class that can never be
    raised. This test widens the grant, observes the SAME statement succeed,
    then puts the grant back and observes the refusal return. If the refusal
    tests were inert, the middle step here would not change anything and this
    test would fail.

    It restores the grant through the superuser connection before asserting, so
    a failure in the middle cannot leave the database more permissive than the
    schema says.
    """

    def test_granting_update_makes_the_refusal_go_away_and_revoking_brings_it_back(self, conn, app_conn):
        table = "reviewer_claims"

        # 1. baseline: refused, as every test above claims
        with pytest.raises(_insufficient_privilege()):
            app_conn.execute(f"UPDATE {table} SET org_id = org_id")

        # 2. widen the grant -- the statement must now be ALLOWED. This is the
        #    step that proves step 1 was measuring the privilege system.
        with conn.cursor() as cur:
            cur.execute(f"GRANT UPDATE ON {table} TO clinical_app")
        conn.commit()
        try:
            app_conn.execute(f"UPDATE {table} SET org_id = org_id")
        except Exception as exc:  # pragma: no cover - this failing is the finding
            pytest.fail(
                "Granting UPDATE did not make the statement succeed, so the refusal "
                f"assertions above are not measuring the grant: {exc!r}"
            )
        finally:
            # 3. restore before asserting anything else
            with conn.cursor() as cur:
                cur.execute(f"REVOKE UPDATE ON {table} FROM clinical_app")
            conn.commit()

        # 4. and the refusal returns
        with pytest.raises(_insufficient_privilege()):
            app_conn.execute(f"UPDATE {table} SET org_id = org_id")
