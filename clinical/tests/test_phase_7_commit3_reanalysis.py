"""
tests/test_phase_7_commit3_reanalysis.py
──────────────────────────────────────────

Phase 7 commit 3: re-analysis (spec 15.3). "Re-analysis produces a new
interpretation, a new report, and a new lineage branch from the same VCF.
It does not update the old one."

Three design decisions were scope-silent and ruled by the human (not
re-derived here):

  1. WHO MAY TRIGGER: Interpreter -- same act as create_interpretation,
     with a different trigger. create_interpretation has no explicit role
     gate today, so create_reanalysis does not add one either.
  2. PREDECESSOR STATE: not gated on the parent's report state. 15.3
     re-interprets the VCF, not the report.
  3. BRANCHING: parent_interpretation_id is an explicit caller argument,
     never derived as "the current tip". A->B and A->C are both valid, and
     so is re-branching from B after C exists. The method validates the
     named parent exists, is in this org, and belongs to the named vcf_id.
"""

from __future__ import annotations

import datetime
from datetime import date, timezone
import hashlib
import os
import uuid

import pytest

from clinical.data_access import (
    DataAccess,
    NotFoundError,
    BcryptHasher,
    SystemClock,
)

DSN = os.getenv("CLINICAL_TEST_DSN")
SCHEMA_PATH = __import__("pathlib").Path(__file__).parent.parent / "schema.sql"

_CREATE_ROLE = "DO $$ BEGIN CREATE ROLE clinical_app; EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
_CREATE_ROLE_RETENTION = (
    "DO $$ BEGIN CREATE ROLE clinical_retention; EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
)


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
def org_a(dao):
    return dao.create_organisation("Org A")


@pytest.fixture
def org_b(dao):
    return dao.create_organisation("Org B")


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


@pytest.fixture
def session_b(dao, org_b, conn):
    return _admin_session(dao, conn, org_b, "admin@org-b.test")


def _make_vcf(dao, conn, session, patient_suffix="1", tmp_path=None):
    """Build a full chain: patient -> order -> sample -> sequencing_run -> vcf. Returns (vcf_id, sample_id)."""
    org_id = session.org_id
    now = datetime.datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

    test_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tests (test_id, org_id, name, assembly, status, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (test_id, org_id, f"Panel{patient_suffix}", "GRCh38", "active", now),
        )
    conn.commit()

    patient_id = dao.create_patient(session, f"Test{patient_suffix}", date(1990, 1, 1), "M")
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
            (
                org_id,
                vcf_id,
                run_id,
                f"/tmp/reanalysis_{vcf_id}.vcf",
                hashlib.sha256(str(vcf_id).encode()).hexdigest(),
                now,
                session.user_id,
            ),
        )
    conn.commit()
    return vcf_id, str(sample_id)


def _make_interpretation(dao, session, vcf_id, sample_id, model_version="v1"):
    return dao.create_interpretation(session, vcf_id, {"model_version": model_version}, sample_id)


def _fetchone(conn, sql, params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _fetchall(conn, sql, params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


class TestCreateReanalysisBasics:
    def test_creates_new_interpretation(self, dao, conn, session_a):
        vcf_id, sample_id = _make_vcf(dao, conn, session_a)
        parent_id = _make_interpretation(dao, session_a, vcf_id, sample_id)

        child_id = dao.create_reanalysis(session_a, vcf_id, parent_id, {"model_version": "v2"})

        assert child_id is not None
        assert child_id != parent_id

        row = _fetchone(
            conn,
            "SELECT vcf_id, parent_interpretation_id FROM interpretations WHERE id = %s",
            (child_id,),
        )
        assert row[0] == vcf_id
        assert row[1] == parent_id

    def test_original_interpretation_unchanged(self, dao, conn, session_a):
        vcf_id, sample_id = _make_vcf(dao, conn, session_a)
        parent_id = _make_interpretation(dao, session_a, vcf_id, sample_id)

        before = _fetchone(
            conn,
            "SELECT run_document, parent_interpretation_id FROM interpretations WHERE id = %s",
            (parent_id,),
        )

        dao.create_reanalysis(session_a, vcf_id, parent_id, {"model_version": "v2"})

        after = _fetchone(
            conn,
            "SELECT run_document, parent_interpretation_id FROM interpretations WHERE id = %s",
            (parent_id,),
        )
        assert before == after
        assert after[1] is None  # parent's own parent is still NULL -- it was never a re-analysis itself

    def test_can_create_report_from_reanalysis_same_as_fresh_interpretation(self, dao, conn, session_a):
        vcf_id, sample_id = _make_vcf(dao, conn, session_a)
        parent_id = _make_interpretation(dao, session_a, vcf_id, sample_id)
        child_id = dao.create_reanalysis(session_a, vcf_id, parent_id, {"model_version": "v2"})

        report_id = dao.create_report(session_a, child_id)
        assert report_id is not None

    def test_unknown_vcf_rejected(self, dao, conn, session_a):
        vcf_id, sample_id = _make_vcf(dao, conn, session_a)
        parent_id = _make_interpretation(dao, session_a, vcf_id, sample_id)

        with pytest.raises(NotFoundError):
            dao.create_reanalysis(session_a, uuid.uuid4(), parent_id, {"model_version": "v2"})

    def test_unknown_parent_rejected(self, dao, conn, session_a):
        vcf_id, sample_id = _make_vcf(dao, conn, session_a)

        with pytest.raises(NotFoundError):
            dao.create_reanalysis(session_a, vcf_id, uuid.uuid4(), {"model_version": "v2"})

    def test_cross_org_parent_rejected(self, dao, conn, session_a, session_b):
        vcf_a, sample_a = _make_vcf(dao, conn, session_a)
        vcf_b, sample_b = _make_vcf(dao, conn, session_b, patient_suffix="2")
        parent_b = _make_interpretation(dao, session_b, vcf_b, sample_b)

        with pytest.raises(NotFoundError):
            dao.create_reanalysis(session_a, vcf_a, parent_b, {"model_version": "v2"})

    def test_parent_from_a_different_vcf_rejected(self, dao, conn, session_a):
        vcf_1, sample_1 = _make_vcf(dao, conn, session_a, patient_suffix="1")
        vcf_2, sample_2 = _make_vcf(dao, conn, session_a, patient_suffix="2")
        parent_on_vcf_1 = _make_interpretation(dao, session_a, vcf_1, sample_1)

        # parent belongs to vcf_1, but caller claims vcf_2 -- must be refused,
        # not silently accepted (would otherwise mislabel a re-analysis's VCF).
        with pytest.raises(ValueError):
            dao.create_reanalysis(session_a, vcf_2, parent_on_vcf_1, {"model_version": "v2"})


class TestPredecessorStateNotGating:
    """Human ruling 2: not gated on the parent's report state."""

    def test_reanalysis_allowed_while_parent_report_is_draft(self, dao, conn, session_a):
        vcf_id, sample_id = _make_vcf(dao, conn, session_a)
        parent_id = _make_interpretation(dao, session_a, vcf_id, sample_id)
        dao.create_report(session_a, parent_id)  # left in draft state, deliberately

        child_id = dao.create_reanalysis(session_a, vcf_id, parent_id, {"model_version": "v2"})
        assert child_id is not None

    def test_reanalysis_allowed_when_parent_has_no_report_at_all(self, dao, conn, session_a):
        vcf_id, sample_id = _make_vcf(dao, conn, session_a)
        parent_id = _make_interpretation(dao, session_a, vcf_id, sample_id)
        # no create_report call at all

        child_id = dao.create_reanalysis(session_a, vcf_id, parent_id, {"model_version": "v2"})
        assert child_id is not None


class TestBranching:
    """Human ruling 3: explicit parent, any same-VCF interpretation. A->B and
    A->C both valid; re-branching from B after C exists is valid too."""

    def test_two_siblings_of_the_same_parent_both_succeed(self, dao, conn, session_a):
        vcf_id, sample_id = _make_vcf(dao, conn, session_a)
        a = _make_interpretation(dao, session_a, vcf_id, sample_id)

        b = dao.create_reanalysis(session_a, vcf_id, a, {"model_version": "v2"})
        c = dao.create_reanalysis(session_a, vcf_id, a, {"model_version": "v3"})

        assert b != c

        rows = _fetchall(
            conn,
            "SELECT id FROM interpretations WHERE parent_interpretation_id = %s ORDER BY id",
            (a,),
        )
        assert {r[0] for r in rows} == {b, c}

    def test_rebranching_from_b_after_c_exists(self, dao, conn, session_a):
        vcf_id, sample_id = _make_vcf(dao, conn, session_a)
        a = _make_interpretation(dao, session_a, vcf_id, sample_id)
        b = dao.create_reanalysis(session_a, vcf_id, a, {"model_version": "v2"})
        dao.create_reanalysis(session_a, vcf_id, a, {"model_version": "v3"})  # c, sibling of b

        d = dao.create_reanalysis(session_a, vcf_id, b, {"model_version": "v4"})

        row = _fetchone(conn, "SELECT parent_interpretation_id FROM interpretations WHERE id = %s", (d,))
        assert row[0] == b


class TestFindReanalyses:
    def test_returns_empty_for_a_leaf_interpretation(self, dao, conn, session_a):
        vcf_id, sample_id = _make_vcf(dao, conn, session_a)
        leaf = _make_interpretation(dao, session_a, vcf_id, sample_id)

        assert dao.find_reanalyses(session_a, leaf) == []

    def test_returns_direct_children_only(self, dao, conn, session_a):
        vcf_id, sample_id = _make_vcf(dao, conn, session_a)
        a = _make_interpretation(dao, session_a, vcf_id, sample_id)
        b = dao.create_reanalysis(session_a, vcf_id, a, {"model_version": "v2"})
        c = dao.create_reanalysis(session_a, vcf_id, a, {"model_version": "v3"})
        dao.create_reanalysis(session_a, vcf_id, b, {"model_version": "v4"})  # d, child of b not a

        results = dao.find_reanalyses(session_a, a)
        ids = {r["interpretation_id"] for r in results}
        assert ids == {b, c}

    def test_cross_org_reanalyses_not_returned(self, dao, conn, session_a, session_b):
        vcf_a, sample_a = _make_vcf(dao, conn, session_a)
        a = _make_interpretation(dao, session_a, vcf_a, sample_a)
        dao.create_reanalysis(session_a, vcf_a, a, {"model_version": "v2"})

        # session_b querying org_a's parent id finds nothing -- org isolation.
        assert dao.find_reanalyses(session_b, a) == []
