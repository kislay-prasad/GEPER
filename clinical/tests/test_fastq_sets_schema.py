"""
tests/test_fastq_sets_schema.py
───────────────────────────────

`fastq_sets`: the first unit of ingestion. One table, org-scoped by
construction, composite FK to `samples`, append-only, holding R1/R2 paths and
checksums, a detected timestamp and a state.

WHAT THIS FILE IS TESTING, STATED PLAINLY BECAUSE IT DECIDES WHETHER THE TESTS
MEAN ANYTHING. There is no scanner, no validation and no pipeline invocation in
this commit -- so the artefact under test IS THE SCHEMA, and the questions are
answered by asking the database to do something it must refuse. That is not the
Phase 5d trap in disguise: the 5d incident was tests that called
`db_session.execute()` INSTEAD OF the methods under test, so they verified the
database while the methods were broken. Here there are no methods yet. When
ingestion code lands, its own tests must go through it, and these stay as the
structural floor underneath.

THE ORG-SCOPING TRAP, WHICH MATTERS MORE THAN THE TABLE (Phase 5d, carded):
A PASSING ORG-SCOPING TEST IS CONSISTENT WITH THE SCOPING WORKING AND WITH THERE
BEING NO SCOPING AT ALL. A query with no org filter passes every single-org test
ever written. Only the CROSS-ORG case separates the two, so the org-scoping
tests here are all cross-org: org A's sample must be unreachable from org B, and
the assertion must fail if the mechanism is removed.

"ORG-SCOPED BY CONSTRUCTION" IS STRONGER THAN "ORG-SCOPED", and the composite FK
is what makes it structural rather than conventional: `fastq_sets` references
`samples (org_id, sample_id)`, not `samples (sample_id)`. An unscoped reference
is therefore NOT EXPRESSIBLE -- it is rejected by the database, not merely
absent from the code that was written. `TestAnUnscopedReferenceIsNotExpressible`
is the test of that claim, and `test_the_same_insert_succeeds_within_one_org` is
its positive control: a refusal test whose INSERT was malformed for some other
reason would pass while proving nothing.

APPEND-ONLY IS TESTED AS A PRIVILEGE, NOT AS A CONVENTION. A comment saying
"append-only" is not a control, and neither is application code that declines to
UPDATE. This file opens a REAL LOGIN CONNECTION as clinical_app -- the way
test_privilege_boundary.py does, and for the reason given there: every other
test file runs as superuser, which bypasses grants entirely -- and asserts the
exact exception class, proves the connection is live on the same table first,
and proves the refusal is capable of failing.
"""

from __future__ import annotations

import datetime
import os
import uuid
from datetime import date, timezone

import pytest

from clinical.data_access import BcryptHasher, DataAccess, SystemClock

DSN = os.getenv("CLINICAL_TEST_DSN")
SCHEMA_PATH = __import__("pathlib").Path(__file__).parent.parent / "schema.sql"

# Same throwaway container credential as test_privilege_boundary.py, and
# self-describing for the same reason: it must not be mistakable for a real one.
_LOCAL_TEST_PASSWORD = "not-a-secret-local-test-only"
_ROLES = ("clinical_app", "clinical_retention")

NOW = datetime.datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


def _connect_as(role: str):
    """A real login as `role`. autocommit, so one refused statement does not
    abort the transaction and make every later statement fail with a DIFFERENT
    error -- which would look like refusals while meaning nothing."""
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
        for role in _ROLES:
            cur.execute(f"DO $$ BEGIN CREATE ROLE {role}; EXCEPTION WHEN duplicate_object THEN NULL; END $$;")
            cur.execute(f"ALTER ROLE {role} LOGIN PASSWORD '{_LOCAL_TEST_PASSWORD}'")
        cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture()
def dao(conn):
    return DataAccess(conn, clock=SystemClock(), password_hasher=BcryptHasher(rounds=4))


def _seed_org_with_sample(dao, conn, label: str):
    """One organisation carrying one sample, built through the DAO where the DAO
    owns the invariant and by direct INSERT where it does not.

    The direct INSERTs are FIXTURE SETUP, not the thing under test: `samples`
    and its ancestors already have their own tests, and reproducing their
    creation paths here would test them a second time while telling us nothing
    about `fastq_sets`.
    """
    org_id = dao.create_organisation(f"FASTQ Org {label}")
    admin_id = dao.create_user(org_id, f"admin@fastq-{label}.test", "password")
    with conn.cursor() as cur:
        for role in ("Administrator", "Interpreter"):
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (admin_id, org_id, role, admin_id, NOW),
            )
    conn.commit()
    session = dao.login(f"admin@fastq-{label}.test", org_id, "password")

    test_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tests (test_id, org_id, name, assembly, status, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (test_id, org_id, "Panel", "GRCh38", "active", NOW),
        )
    conn.commit()

    patient_id = dao.create_patient(session, f"Patient {label}", date(1990, 1, 1), "M")
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
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO samples (sample_id, order_id, org_id, type, collected_at, collected_by, qc_status) "
            "VALUES (%s, %s, %s, 'blood', %s, %s, 'passed')",
            (sample_id, order_id, org_id, NOW, admin_id),
        )
    conn.commit()
    return {"org_id": org_id, "sample_id": sample_id, "user_id": admin_id}


@pytest.fixture()
def two_orgs(dao, conn):
    """TWO organisations, each with its own sample. Two is the minimum that can
    tell org-scoping apart from no scoping at all; every test below that claims
    an org boundary uses both."""
    return {"a": _seed_org_with_sample(dao, conn, "A"), "b": _seed_org_with_sample(dao, conn, "B")}


def _insert_fastq_set(cur, *, org_id, sample_id, fastq_set_id=None, state="detected", suffix="1"):
    fastq_set_id = fastq_set_id or uuid.uuid4()
    cur.execute(
        "INSERT INTO fastq_sets "
        "(fastq_set_id, org_id, sample_id, r1_path, r2_path, r1_checksum, r2_checksum, detected_at, state) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            fastq_set_id,
            org_id,
            sample_id,
            f"/data/incoming/{suffix}_R1_001.fastq.gz",
            f"/data/incoming/{suffix}_R2_001.fastq.gz",
            "a" * 64,
            "b" * 64,
            NOW,
            state,
        ),
    )
    return fastq_set_id


class TestTheTableExistsWithTheColumnsTheSpecificationNames:
    """R1 AND R2 paths and checksums, a detected timestamp, and a state -- named
    one at a time, so a missing column names itself instead of surfacing later
    as an opaque INSERT failure."""

    def test_every_specified_column_is_present(self, conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name = 'fastq_sets'"
            )
            columns = {row[0]: row[1] for row in cur.fetchall()}
        for required in (
            "fastq_set_id",
            "org_id",
            "sample_id",
            "r1_path",
            "r2_path",
            "r1_checksum",
            "r2_checksum",
            "detected_at",
            "state",
        ):
            assert required in columns, f"fastq_sets is missing {required}: {sorted(columns)}"
            assert columns[required] == "NO", f"{required} must be NOT NULL"

    def test_a_row_can_be_inserted_and_read_back_unchanged(self, conn, two_orgs):
        a = two_orgs["a"]
        with conn.cursor() as cur:
            fastq_set_id = _insert_fastq_set(cur, org_id=a["org_id"], sample_id=a["sample_id"])
            cur.execute(
                "SELECT org_id, sample_id, r1_path, r2_path, r1_checksum, r2_checksum, detected_at, state "
                "FROM fastq_sets WHERE fastq_set_id = %s",
                (fastq_set_id,),
            )
            row = cur.fetchone()
        conn.commit()
        assert row[0] == a["org_id"]
        assert row[1] == a["sample_id"]
        assert row[2].endswith("_R1_001.fastq.gz")
        assert row[3].endswith("_R2_001.fastq.gz")
        assert row[4] == "a" * 64
        assert row[5] == "b" * 64
        assert row[6] == NOW
        assert row[7] == "detected"


class TestAnUnscopedReferenceIsNotExpressible:
    """ORG-SCOPED BY CONSTRUCTION. The composite FK means a fastq set cannot
    name a sample belonging to another organisation -- the database refuses it,
    so no amount of application code, present or future, can produce the row."""

    def test_a_fastq_set_cannot_reference_another_orgs_sample(self, conn, two_orgs):
        psycopg = pytest.importorskip("psycopg")
        a, b = two_orgs["a"], two_orgs["b"]
        with conn.cursor() as cur:
            # EXACT exception class. A bare Exception would also be raised by a
            # typo'd column or a dead connection, which is what a broken test
            # looks like too.
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                _insert_fastq_set(cur, org_id=a["org_id"], sample_id=b["sample_id"])
        conn.rollback()

    def test_the_same_insert_succeeds_within_one_org(self, conn, two_orgs):
        """POSITIVE CONTROL for the test above. If the cross-org INSERT were
        malformed for some unrelated reason it would raise too, and the refusal
        test would pass while proving nothing. Same statement, same columns,
        only the org/sample pairing differs."""
        a = two_orgs["a"]
        with conn.cursor() as cur:
            _insert_fastq_set(cur, org_id=a["org_id"], sample_id=a["sample_id"])
        conn.commit()

    def test_the_foreign_key_names_both_columns(self, conn):
        """The mechanism itself, not just its effect. A single-column FK to
        sample_id would pass neither of the tests above today -- but it would
        start passing the moment someone 'simplified' the constraint, and this
        test is what fails then."""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT a.attname FROM pg_constraint c "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
                "WHERE c.conrelid = 'fastq_sets'::regclass AND c.contype = 'f' "
                "AND c.confrelid = 'samples'::regclass"
            )
            cols = {row[0] for row in cur.fetchall()}
        assert cols == {"org_id", "sample_id"}, f"FK to samples must be composite, got {cols}"


class TestOneOrgCannotSeeAnothersRows:
    """The read side of the same claim. Stated as a cross-org query because a
    single-org query returns the right answer whether the filter exists or not."""

    def test_a_query_scoped_to_org_a_returns_none_of_org_bs_rows(self, conn, two_orgs):
        a, b = two_orgs["a"], two_orgs["b"]
        with conn.cursor() as cur:
            _insert_fastq_set(cur, org_id=a["org_id"], sample_id=a["sample_id"], suffix="A")
            _insert_fastq_set(cur, org_id=b["org_id"], sample_id=b["sample_id"], suffix="B")
            conn.commit()
            cur.execute("SELECT org_id FROM fastq_sets WHERE org_id = %s", (a["org_id"],))
            scoped = [row[0] for row in cur.fetchall()]
            cur.execute("SELECT org_id FROM fastq_sets")
            unscoped = [row[0] for row in cur.fetchall()]
        assert scoped == [a["org_id"]]
        # THE HALF THAT MAKES THE HALF ABOVE MEAN SOMETHING: both rows really do
        # exist, so the scoped query returned one because it filtered, not
        # because there was only ever one row to return.
        assert sorted(unscoped) == sorted([a["org_id"], b["org_id"]])
        assert len(unscoped) == 2


class TestAppendOnlyIsAPrivilegeAndNotAConvention:
    """As clinical_app, over a real login connection. Superuser bypasses grants,
    so every other test file in this directory would pass these while the
    privilege was absent."""

    @pytest.fixture()
    def app_conn(self, conn):
        c = _connect_as("clinical_app")
        yield c
        c.close()

    @pytest.fixture()
    def seeded_row(self, conn, two_orgs):
        a = two_orgs["a"]
        with conn.cursor() as cur:
            fastq_set_id = _insert_fastq_set(cur, org_id=a["org_id"], sample_id=a["sample_id"], suffix="seed")
        conn.commit()
        return {"fastq_set_id": fastq_set_id, **a}

    def test_the_connection_is_real_and_can_do_something(self, app_conn, seeded_row):
        """GUARD: a role that can do NOTHING passes every refusal test below. So
        it must first be seen exercising a permission it does hold, on the same
        table it will be refused on."""
        with app_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM fastq_sets")
            assert cur.fetchone()[0] >= 1

    def test_the_app_may_insert(self, app_conn, two_orgs):
        a = two_orgs["a"]
        with app_conn.cursor() as cur:
            _insert_fastq_set(cur, org_id=a["org_id"], sample_id=a["sample_id"], suffix="app")

    def test_the_app_may_not_update(self, app_conn, seeded_row):
        psycopg = pytest.importorskip("psycopg")
        with app_conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "UPDATE fastq_sets SET state = 'validated' WHERE fastq_set_id = %s", (seeded_row["fastq_set_id"],)
                )

    def test_the_app_may_not_delete(self, app_conn, seeded_row):
        psycopg = pytest.importorskip("psycopg")
        with app_conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("DELETE FROM fastq_sets WHERE fastq_set_id = %s", (seeded_row["fastq_set_id"],))

    def test_public_holds_neither_update_nor_delete(self, conn):
        """Belt and braces, the same shape schema.sql uses: a REVOKE from
        clinical_app alone leaves the privilege reachable through PUBLIC."""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT has_table_privilege('public', 'fastq_sets', 'UPDATE'), "
                "       has_table_privilege('public', 'fastq_sets', 'DELETE')"
            )
            update_ok, delete_ok = cur.fetchone()
        assert update_ok is False
        assert delete_ok is False


class TestTheRefusalAssertionCanItselfFail:
    """A negative assertion that has only ever been seen passing is evidence of
    nothing. The grant is widened, the same UPDATE observed to SUCCEED, then the
    grant is put back and the refusal observed to return -- so the known-positive
    is permanent and re-runnable rather than a one-off by whoever wrote this."""

    def test_update_succeeds_when_the_grant_is_widened_and_is_refused_again_after(self, conn, two_orgs):
        psycopg = pytest.importorskip("psycopg")
        a = two_orgs["a"]
        with conn.cursor() as cur:
            fastq_set_id = _insert_fastq_set(cur, org_id=a["org_id"], sample_id=a["sample_id"], suffix="ctl")
        conn.commit()

        app = _connect_as("clinical_app")
        try:
            with app.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("UPDATE fastq_sets SET state = 'validated' WHERE fastq_set_id = %s", (fastq_set_id,))

            with conn.cursor() as cur:
                cur.execute("GRANT UPDATE ON fastq_sets TO clinical_app")
            conn.commit()

            with app.cursor() as cur:
                cur.execute("UPDATE fastq_sets SET state = 'validated' WHERE fastq_set_id = %s", (fastq_set_id,))
                assert cur.rowcount == 1  # the statement really landed

            with conn.cursor() as cur:
                cur.execute("REVOKE UPDATE ON fastq_sets FROM clinical_app")
            conn.commit()

            with app.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("UPDATE fastq_sets SET state = 'detected' WHERE fastq_set_id = %s", (fastq_set_id,))
        finally:
            app.close()


class TestTheStateColumnIsConstrained:
    """A state column whose values are not constrained is a TEXT column with a
    hopeful name."""

    def test_an_unknown_state_is_rejected(self, conn, two_orgs):
        psycopg = pytest.importorskip("psycopg")
        a = two_orgs["a"]
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.CheckViolation):
                _insert_fastq_set(cur, org_id=a["org_id"], sample_id=a["sample_id"], state="banana")
        conn.rollback()

    def test_each_declared_state_is_accepted(self, conn, two_orgs):
        """POSITIVE CONTROL for the CHECK: a constraint that rejected everything
        would pass the test above."""
        a = two_orgs["a"]
        for index, state in enumerate(("detected", "validated", "rejected", "consumed")):
            with conn.cursor() as cur:
                _insert_fastq_set(cur, org_id=a["org_id"], sample_id=a["sample_id"], state=state, suffix=f"s{index}")
        conn.commit()
