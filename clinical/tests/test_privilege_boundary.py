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

# The seven append-only tables. clinical_app may SELECT and INSERT; UPDATE and
# DELETE are revoked from it and from PUBLIC.
#
# fastq_sets joins them here and NOT in DELETE_REVOKED below, deliberately: the
# two lists are disjoint and mean different things. DELETE_REVOKED is "tables
# clinical_app may still SELECT, INSERT and UPDATE, on which D0b revoked
# DELETE", and UPDATE_STILL_PERMITTED is derived from it by subtraction -- so a
# table added there without also being excluded would be asserted UPDATABLE,
# which for an append-only table is the opposite of what its grant block says.
# This list already covers everything the other one would: SELECT permitted,
# UPDATE refused, DELETE refused.
APPEND_ONLY = (
    "audit_log",
    "reviewer_claims",
    "release_events",
    "amendments",
    "amendment_notifications",
    "notification_read_receipts",
    "fastq_sets",
)

# The three retention-managed tables. clinical_retention may SELECT and UPDATE
# (to write the tombstone); DELETE is revoked -- D4 ruled tombstone, not delete.
RETENTION_TABLES = ("vcfs", "interpretations", "reports")

# D0b, the free win (human ruling, 2026-09-04). These two are INSERT-ONLY for
# clinical_app: it may SELECT and INSERT, and UPDATE is revoked from it and from
# PUBLIC. Named as an explicit exception list rather than left implicit, because
# the guard below subtracts it from DELETE_REVOKED and a silent change to either
# end of that subtraction is exactly what this file exists to catch.
INSERT_ONLY_FOR_APP = ("interpretations", "vcfs")

# The nineteen tables clinical_app may still SELECT, INSERT and UPDATE, and on
# which DELETE was revoked by D0b half one (human ruling, 2026-09-04). Kept as
# an explicit literal rather than derived from schema.sql at runtime: a test
# that reads its expectations out of the file it is testing agrees with that
# file by construction and would not notice a table silently leaving the block.
DELETE_REVOKED = (
    "organisations",
    "users",
    "totp_backup_codes",
    "role_assignments",
    "sessions",
    "patients",
    "external_identifiers",
    "consents",
    "tests",
    "test_genes",
    "orders",
    "samples",
    "sequencing_runs",
    "vcfs",
    "interpretations",
    "reports",
    "exceptions",
    "exception_events",
    "retention_policies",
)

# The seventeen on which clinical_app still holds UPDATE: the nineteen above,
# less the two that D0b's free win made INSERT-only. Derived by subtraction and
# then CHECKED FOR ARITY, so that a table entering or leaving either list
# without the other being considered fails here rather than silently widening
# or narrowing the permitted-direction guard.
# D0b half two. reports leaves the blanket guard for a DIFFERENT reason than the
# two above: not because clinical_app may never update it, but because its UPDATE
# is now COLUMN-SCOPED to state/approver_id/approved_at/content_hash. A blanket
# `SET col = col` probe therefore hits whichever column happens to be first and
# says nothing useful. Its permitted directions are asserted per transition
# instead, in TestAnApprovedReportAdmitsOnlyReleaseAndTombstone -- which is
# STRONGER than the probe it replaces, not a relaxation of it. Kept as its own
# named list rather than folded into INSERT_ONLY_FOR_APP because the two
# exclusions mean different things and a reader must not have to guess which.
COLUMN_SCOPED_FOR_APP = ("reports",)

UPDATE_STILL_PERMITTED = tuple(t for t in DELETE_REVOKED if t not in INSERT_ONLY_FOR_APP + COLUMN_SCOPED_FOR_APP)
assert len(UPDATE_STILL_PERMITTED) == 16, UPDATE_STILL_PERMITTED
assert len(DELETE_REVOKED) == 19, DELETE_REVOKED
assert set(INSERT_ONLY_FOR_APP) <= set(DELETE_REVOKED), INSERT_ONLY_FOR_APP
assert set(COLUMN_SCOPED_FOR_APP) <= set(DELETE_REVOKED), COLUMN_SCOPED_FOR_APP


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


# ─── D0b half one: nothing deletes ───────────────────────────────────────────


def _self_assignable_column(conn, table: str) -> str:
    """
    Any column of `table`, for a no-op `SET col = col`.

    Read from information_schema rather than hard-coded per table: the point of
    the UPDATE probe below is only that the privilege is present, and a
    hand-written column list would need editing every time a column is renamed
    and would fail as a spurious privilege result when it was really a typo.
    Self-assignment cannot violate a constraint, because the value is unchanged.
    """
    row = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position LIMIT 1",
        (table,),
    ).fetchone()
    assert row is not None, f"{table} has no columns -- does the table exist?"
    return row[0]


class TestNothingDeletes:
    """
    D0b half one. The nineteen tables that carried full DELETE now do not.

    THE RULING THIS ENFORCES: spec 15.1 says an approved report is immutable
    while the grant block let the application delete the report outright, so the
    claim rested on application code declining to do something the database
    permitted -- the same shape as an unverified sign-off identity, a guarantee
    asserted at a layer that cannot enforce it.

    THE REVOKE HAD TO BE SURGICAL, which is why the permitted-direction tests
    here matter as much as the refusals: DELETE goes, UPDATE stays. A blanket
    REVOKE would also have taken UPDATE and broken the report state machine
    (draft -> under_review -> approved -> released) outright, and every refusal
    test in this class would still have passed while it did so.
    """

    @pytest.mark.parametrize("table", DELETE_REVOKED)
    def test_delete_is_refused(self, app_conn, table):
        with pytest.raises(_insufficient_privilege()):
            app_conn.execute(f"DELETE FROM {table}")

    @pytest.mark.parametrize("table", UPDATE_STILL_PERMITTED)
    def test_update_is_still_permitted(self, conn, app_conn, table):
        # THE GUARD THAT MAKES THE REFUSAL ABOVE MEAN SOMETHING, and the one
        # that catches an over-broad revoke. If UPDATE had gone with DELETE,
        # test_delete_is_refused would pass just as happily.
        col = _self_assignable_column(conn, table)
        app_conn.execute(f"UPDATE {table} SET {col} = {col}")

    @pytest.mark.parametrize("table", DELETE_REVOKED)
    def test_select_is_still_permitted(self, app_conn, table):
        app_conn.execute(f"SELECT count(*) FROM {table}").fetchone()


# ─── D0b, the free win: interpretations and vcfs are INSERT-only ─────────────


class TestInterpretationsAndVcfsAreInsertOnlyForTheApplication:
    """
    D0b's free win (human ruling, 2026-09-04). clinical_app's legitimate UPDATE
    count on these two tables is ZERO -- enumerated by parsing every
    SQL-executing call site and constant-folding its statement, not by searching
    for a name. So UPDATE is revoked outright, with no trigger, no column-level
    grant and no new role: the intended surface here is UNCONDITIONAL, unlike
    reports, where UPDATE stays because the state machine legitimately needs it.

    WHAT THIS ACTUALLY PROTECTS: interpretations.run_document is the evidence a
    report's content hash is computed over. While clinical_app held UPDATE, a
    rewritten run_document would make verify_report_integrity PASS against
    tampered content -- the hash still matching because the thing it is a hash
    OF had moved underneath it. That is the one tampering shape integrity
    verification cannot see, and this closes it by prevention rather than
    detection.

    THE PERMITTED DIRECTIONS ARE HALF THE POINT, exactly as in TestNothingDeletes:
    INSERT and SELECT must survive, and clinical_retention must still be able to
    tombstone. A blanket revoke would break creation and purging outright while
    every refusal below carried on passing.
    """

    @pytest.mark.parametrize("table", INSERT_ONLY_FOR_APP)
    def test_update_is_refused(self, app_conn, table):
        with pytest.raises(_insufficient_privilege()):
            app_conn.execute(f"UPDATE {table} SET tombstoned_at = now()")

    def test_the_run_document_column_specifically_cannot_be_rewritten(self, app_conn):
        # Named on its own rather than left to the parametrised case above,
        # because this column is the reason the ruling was taken: it is the one
        # whose mutation defeats hash verification instead of being caught by it.
        with pytest.raises(_insufficient_privilege()):
            app_conn.execute("UPDATE interpretations SET run_document = '{}'::jsonb")

    @pytest.mark.parametrize("table", INSERT_ONLY_FOR_APP)
    def test_insert_is_still_permitted(self, app_conn, table):
        # A DELIBERATELY INVALID INSERT. PostgreSQL checks privilege before it
        # checks constraints, so a row that cannot satisfy NOT NULL still proves
        # which of the two walls it hit: InsufficientPrivilege means the grant is
        # gone, any other error means the grant is present and the row was simply
        # bad. Building a valid row here would need the whole FK chain and would
        # measure the fixture rather than the privilege.
        psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
        with pytest.raises(psycopg.Error) as caught:
            app_conn.execute(f"INSERT INTO {table} DEFAULT VALUES")
        assert not isinstance(caught.value, _insufficient_privilege()), (
            f"clinical_app has lost INSERT on {table} -- the revoke was over-broad"
        )

    @pytest.mark.parametrize("table", INSERT_ONLY_FOR_APP)
    def test_select_is_still_permitted(self, app_conn, table):
        app_conn.execute(f"SELECT count(*) FROM {table}").fetchone()

    @pytest.mark.parametrize("table", INSERT_ONLY_FOR_APP)
    def test_retention_can_still_tombstone_them(self, retention_conn, seeded, table):
        # THE RULING SAID SO EXPLICITLY: the retention principal must keep its
        # tombstone UPDATE on both tables, and that is to be VERIFIED rather than
        # assumed to survive a revoke aimed at a different role. Row count and
        # read-back, not "raises nothing" -- an UPDATE matching no row also
        # raises nothing.
        now = datetime.datetime(2026, 9, 4, 12, 30, 0, tzinfo=timezone.utc)
        cur = retention_conn.execute(
            f"UPDATE {table} SET tombstoned_at = %s, tombstoned_by = %s WHERE id = %s",
            (now, seeded["user_id"], seeded[table]),
        )
        assert cur.rowcount == 1, f"clinical_retention's tombstone on {table} matched no row"
        landed = retention_conn.execute(
            f"SELECT tombstoned_at FROM {table} WHERE id = %s", (seeded[table],)
        ).fetchone()[0]
        assert landed == now, f"the tombstone did not persist on {table}"

    def test_this_refusal_can_itself_fail(self, conn, app_conn):
        # KNOWN-POSITIVE, and separate from the DELETE and append-only ones
        # already in this file: this refusal comes from a different grant on a
        # different table set, so neither of those proving live says anything
        # about this one. Grant UPDATE back and the refusal must disappear;
        # revoke it and the refusal must return.
        with pytest.raises(_insufficient_privilege()):
            app_conn.execute("UPDATE interpretations SET run_document = run_document")

        with conn.cursor() as cur:
            cur.execute("GRANT UPDATE ON interpretations TO clinical_app")
        conn.commit()
        app_conn.execute("UPDATE interpretations SET run_document = run_document")

        with conn.cursor() as cur:
            cur.execute("REVOKE UPDATE ON interpretations FROM clinical_app")
        conn.commit()
        with pytest.raises(_insufficient_privilege()):
            app_conn.execute("UPDATE interpretations SET run_document = run_document")


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

    def test_granting_delete_makes_the_refusal_go_away_and_revoking_brings_it_back(self, conn, app_conn):
        """
        The same known-positive for the DELETE class, not assumed to follow from
        the UPDATE one. The two refusals are produced by DIFFERENT grants on a
        DIFFERENT set of tables -- the append-only six for UPDATE, the nineteen
        for DELETE -- so one proving live says nothing about the other. Run
        against `reports`, which is one of the nineteen and is empty here, so a
        permitted DELETE removes nothing.
        """
        table = "reports"

        with pytest.raises(_insufficient_privilege()):
            app_conn.execute(f"DELETE FROM {table}")

        with conn.cursor() as cur:
            cur.execute(f"GRANT DELETE ON {table} TO clinical_app")
        conn.commit()
        try:
            app_conn.execute(f"DELETE FROM {table}")
        except Exception as exc:  # pragma: no cover - this failing is the finding
            pytest.fail(
                "Granting DELETE did not make the statement succeed, so the DELETE "
                f"refusals are not measuring the grant: {exc!r}"
            )
        finally:
            with conn.cursor() as cur:
                cur.execute(f"REVOKE DELETE ON {table} FROM clinical_app")
            conn.commit()

        with pytest.raises(_insufficient_privilege()):
            app_conn.execute(f"DELETE FROM {table}")


# ─── D0b half two: what an approved report still admits ─────────────────────


def _raise_exception():
    """
    The trigger's refusal, from a RAW ROLE LOGIN.

    NOT ImmutabilityViolationError. That is what DataAccess._execute() translates
    a P0001 into, and every test in this file deliberately bypasses DataAccess to
    speak to the database as a real role -- so what arrives here is the driver's
    own error. psycopg has no dedicated subclass for P0001 the way it has
    InsufficientPrivilege for 42501, so RaiseException plus the SQLSTATE is the
    precise assertion, and the sqlstate check below is what keeps it precise.
    """
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    return psycopg.errors.RaiseException


@pytest.fixture()
def other_user(dao, seeded):
    """
    A SECOND real user in the same org. Needed so that "reassign the authoriser"
    can be attempted with a VALID value: setting approver_id to NULL or to a
    nonexistent id would be stopped by a CHECK or an FK, and a test that cannot
    tell a constraint from the mechanism it is measuring proves nothing.
    """
    return dao.create_user(seeded["org_id"], "other@org.test", "password")


@pytest.fixture()
def approved_report(conn, seeded):
    """
    A freshly approved report, created rather than mutated into place.

    THE ROW CANNOT BE RESET, which is worth stating because it is the mechanism
    proving itself: once approver_id is set, the trigger refuses to move it back
    -- to the SUPERUSER as well -- so a fixture that un-approved a row between
    tests could not exist. Each test that needs an approved report gets a new one.
    """
    import uuid as _uuid

    new_id = _uuid.uuid4()
    now = datetime.datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO reports (org_id, id, interpretation_id, created_at, created_by) "
            "SELECT org_id, %s, interpretation_id, created_at, created_by FROM reports WHERE id = %s",
            (new_id, seeded["reports"]),
        )
        cur.execute(
            "UPDATE reports SET state = 'approved', approver_id = %s, approved_at = %s, "
            "content_hash = %s WHERE id = %s",
            (seeded["user_id"], now, "a" * 64, new_id),
        )
    conn.commit()
    return new_id


@pytest.fixture()
def draft_report(conn, seeded):
    import uuid as _uuid

    new_id = _uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO reports (org_id, id, interpretation_id, created_at, created_by) "
            "SELECT org_id, %s, interpretation_id, created_at, created_by FROM reports WHERE id = %s",
            (new_id, seeded["reports"]),
        )
    conn.commit()
    return new_id


class TestAnApprovedReportAdmitsOnlyReleaseAndTombstone:
    """
    D0b half two, from the role-login side. Meredith's own trigger tests exercise
    the mechanism through DataAccess; this class asks the different question this
    file exists to ask -- what can a REAL LOGIN as each principal actually do to
    an approved report.

    THE POST-APPROVAL WRITE SET IS CLOSED AT EXACTLY TWO, by AST enumeration of
    every write to `reports`:
        _release_report()  UPDATE reports SET state = 'released'
        _purge_reports()   UPDATE reports SET tombstoned_at, tombstoned_by
    Disjoint and narrow. Both are asserted to SUCCEED below; everything else is
    asserted to be refused.

    WHY PER TRANSITION AND NOT PER COLUMN: `state` must stay writable -- draft ->
    under_review -> approved -> released -- so no column list can express
    "writable while draft, frozen once approved". That property is a function of
    the ROW'S STATE, which a grant cannot see and a BEFORE UPDATE trigger can.

    TWO DIFFERENT WALLS, AND THE TESTS NAME WHICH ONE THEY EXPECT. The column
    grant (defence in depth) refuses anything outside
    state/approver_id/approved_at/content_hash with InsufficientPrivilege before
    the trigger ever runs; the trigger refuses the approval facts themselves with
    P0001 once they are set. Asserting the wrong wall would pass while proving
    the other one absent, so each is named.
    """

    # ── the permitted two ────────────────────────────────────────────────────

    def test_an_approved_report_may_be_released(self, app_conn, approved_report):
        cur = app_conn.execute("UPDATE reports SET state = 'released' WHERE id = %s", (approved_report,))
        assert cur.rowcount == 1
        state = app_conn.execute("SELECT state FROM reports WHERE id = %s", (approved_report,)).fetchone()[0]
        assert state == "released", "the release did not persist"

    def test_retention_may_tombstone_an_approved_report(self, retention_conn, seeded, approved_report):
        now = datetime.datetime(2026, 9, 4, 13, 0, 0, tzinfo=timezone.utc)
        cur = retention_conn.execute(
            "UPDATE reports SET tombstoned_at = %s, tombstoned_by = %s WHERE id = %s",
            (now, seeded["user_id"], approved_report),
        )
        assert cur.rowcount == 1
        landed = retention_conn.execute(
            "SELECT tombstoned_at FROM reports WHERE id = %s", (approved_report,)
        ).fetchone()[0]
        assert landed == now, "the tombstone did not persist"

    # ── the approval facts: frozen by the trigger once set ───────────────────

    @pytest.mark.parametrize("column", ["content_hash", "approver_id", "approved_at"])
    def test_the_approval_facts_are_frozen_once_set(self, app_conn, approved_report, other_user, column):
        # EVERY VALUE HERE IS VALID -- a different real hash, a different real
        # user in the same org, a different real timestamp. So the only thing that
        # can refuse the write is the mechanism under test. NULLs would have been
        # stopped by the reports_approval_complete CHECK, and would have made a
        # passing test out of a constraint.
        value = {
            "content_hash": "b" * 64,
            "approver_id": other_user,  # ISO 15189 7.4.1.5 c): the authoriser
            "approved_at": datetime.datetime(  # identity must stay retrievable
                2027, 1, 1, tzinfo=timezone.utc
            ),
        }[column]
        with pytest.raises(_raise_exception()) as caught:
            app_conn.execute(f"UPDATE reports SET {column} = %s WHERE id = %s", (value, approved_report))
        assert caught.value.sqlstate == "P0001", (
            "refused, but not by the trigger -- a different wall stopped this, and "
            "the trigger's own coverage is therefore unproven"
        )

    def test_rewriting_the_hash_is_what_this_exists_to_stop(self, app_conn, approved_report):
        # Named on its own rather than left to the parametrised case: an attacker
        # who can rewrite content_hash makes verify_report_integrity agree with
        # tampered content, which is the one shape verification cannot detect.
        with pytest.raises(_raise_exception()):
            app_conn.execute("UPDATE reports SET content_hash = %s WHERE id = %s", ("f" * 64, approved_report))
        held = app_conn.execute("SELECT content_hash FROM reports WHERE id = %s", (approved_report,)).fetchone()[0]
        assert held == "a" * 64, "the hash moved despite the refusal"

    # ── identity and provenance: refused by the column grant, before the trigger

    @pytest.mark.parametrize("column", ["created_by", "interpretation_id", "created_at", "org_id"])
    def test_identity_and_provenance_are_not_even_grantable(self, app_conn, approved_report, column):
        # DEFENCE IN DEPTH, and it lands FIRST: these columns are outside
        # clinical_app's column grant, so the statement is refused before the
        # trigger runs. The trigger covers them too -- that is what binds the
        # superuser -- but the application never reaches it.
        # SELF-ASSIGNMENT, deliberately: `SET col = col` cannot violate NOT NULL or
        # an FK, so InsufficientPrivilege is the only outcome available and a
        # constraint cannot masquerade as a privilege result. It is also invisible
        # to the trigger (IS DISTINCT FROM is false), which is what makes this a
        # measurement of the GRANT specifically.
        with pytest.raises(_insufficient_privilege()):
            app_conn.execute(f"UPDATE reports SET {column} = {column} WHERE id = %s", (approved_report,))

    def test_the_application_cannot_tombstone_a_report(self, app_conn, seeded, approved_report):
        # Tombstoning is the retention principal's act alone. Without this, an
        # application-level compromise could withdraw a report from the record --
        # which ISO 15189 7.4.1.8 forbids as squarely as altering one.
        with pytest.raises(_insufficient_privilege()):
            app_conn.execute(
                "UPDATE reports SET tombstoned_at = now(), tombstoned_by = %s WHERE id = %s",
                (seeded["user_id"], approved_report),
            )

    # ── the paired permitted directions ──────────────────────────────────────
    # WITHOUT THESE THE WHOLE CLASS IS SATISFIED BY A MECHANISM THAT REFUSES
    # EVERYTHING -- the same over-broad failure the DELETE work already taught.

    def test_a_draft_report_can_still_be_submitted_for_review(self, app_conn, draft_report):
        cur = app_conn.execute("UPDATE reports SET state = 'under_review' WHERE id = %s", (draft_report,))
        assert cur.rowcount == 1

    def test_approval_can_still_write_the_hash_on_an_unapproved_report(self, app_conn, seeded, draft_report):
        # The write-once direction must survive: NULL -> value is legal exactly
        # once. If this failed, reports could never be approved at all and every
        # refusal above would still pass.
        app_conn.execute("UPDATE reports SET state = 'under_review' WHERE id = %s", (draft_report,))
        cur = app_conn.execute(
            "UPDATE reports SET state = 'approved', approver_id = %s, approved_at = now(), "
            "content_hash = %s WHERE id = %s",
            (seeded["user_id"], "e" * 64, draft_report),
        )
        assert cur.rowcount == 1

    # ── the ruling that no privilege design could have satisfied ─────────────

    def test_the_superuser_is_bound_too(self, conn, approved_report):
        """
        BIND THE SUPERUSER was ruled explicitly, and this is the only assertion
        in this file that can prove it. The superuser bypasses every GRANT and
        every RLS policy, so under any privilege-based design this test is
        impossible to write. A BEFORE UPDATE trigger binds the owner and the
        superuser alike. If this passes, the mechanism is genuinely a trigger and
        not a grant wearing one's name.
        """
        psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.RaiseException) as caught:
                cur.execute("UPDATE reports SET content_hash = %s WHERE id = %s", ("d" * 64, approved_report))
        conn.rollback()
        assert caught.value.sqlstate == "P0001"

    def test_the_superuser_cannot_rewrite_a_run_document_either(self, conn, seeded):
        # The other half of the same ruling, on the table whose mutation defeats
        # hash verification rather than being caught by it. This is what closes
        # run_document for EVERY principal: the INSERT-only revoke stops
        # clinical_app, and the trigger stops everyone the revoke cannot reach --
        # including the retention principal, which does hold UPDATE here.
        psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.RaiseException) as caught:
                cur.execute(
                    "UPDATE interpretations SET run_document = %s WHERE id = %s",
                    ('{"tampered": true}', seeded["interpretations"]),
                )
        conn.rollback()
        assert caught.value.sqlstate == "P0001"

    def test_retention_cannot_rewrite_a_run_document(self, retention_conn, seeded):
        # clinical_retention holds table-level UPDATE on interpretations, so the
        # grant does NOT stop it here and only the trigger does. This is the case
        # that would have been open under a privilege-only design.
        with pytest.raises(_raise_exception()):
            retention_conn.execute(
                "UPDATE interpretations SET run_document = %s WHERE id = %s",
                ('{"tampered": true}', seeded["interpretations"]),
            )

    # ── the refusal is proven capable of failing ─────────────────────────────

    def test_this_refusal_can_itself_fail(self, conn, app_conn, approved_report):
        """
        KNOWN-POSITIVE, and a THIRD distinct one in this file: the DELETE and
        INSERT-only known-positives both prove a GRANT is live, which says
        nothing about a TRIGGER. Drop the trigger and the write must succeed;
        recreate it and the refusal must return. Without this, the class passes
        just as happily on a database where the trigger silently failed to
        install -- which is the whole failure mode a schema-level mechanism has.
        """
        with pytest.raises(_raise_exception()):
            app_conn.execute("UPDATE reports SET content_hash = %s WHERE id = %s", ("b" * 64, approved_report))

        with conn.cursor() as cur:
            cur.execute("DROP TRIGGER trg_reports_content_immutability ON reports")
        conn.commit()
        app_conn.execute("UPDATE reports SET content_hash = %s WHERE id = %s", ("b" * 64, approved_report))

        with conn.cursor() as cur:
            cur.execute(
                "CREATE TRIGGER trg_reports_content_immutability BEFORE UPDATE ON reports "
                "FOR EACH ROW EXECUTE FUNCTION enforce_content_immutability()"
            )
        conn.commit()
        with pytest.raises(_raise_exception()):
            app_conn.execute("UPDATE reports SET content_hash = %s WHERE id = %s", ("c" * 64, approved_report))
