"""
tests/test_phase_6_review_methods.py
────────────────────────────────────

Phase 6 commit 2: reviewer claim recording methods (spec 13.2's four actions).

Each method records ONE reviewer claim into reviewer_claims. This commit stores
claims independently: it does NOT enforce state transitions (draft -> under_review,
approved -> released) and does NOT enforce supersession uniqueness. Both belong to
commit 3, for the reason the schema records -- a row constraint cannot see the rest
of the table, so neither rule can be expressed here.

evidence_json is deliberately not written by any method in this commit; it stays
NULL pending the expert-branch resolution of what shape evidence takes.
"""

from __future__ import annotations

import datetime
from datetime import date, timezone
import json
import os
import uuid

import pytest

from clinical.data_access import DataAccess, BcryptHasher, SystemClock, NotFoundError

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


def _make_interpretation(dao, conn, session):
    """
    Build the full FK chain an interpretation needs:
    test -> patient -> consent -> order -> sample -> sequencing_run -> vcf -> interpretation.
    Returns the interpretation id.
    """
    org_id = session.org_id
    now = datetime.datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

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

    sample_id = uuid.uuid4()
    run_id = uuid.uuid4()
    vcf_id = uuid.uuid4()
    interp_id = uuid.uuid4()
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
            (org_id, vcf_id, run_id, "/tmp/x.vcf", "0" * 64, now, session.user_id),
        )
        cur.execute(
            "INSERT INTO interpretations "
            "(org_id, id, vcf_id, run_document, submission_key, created_at, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                org_id,
                interp_id,
                vcf_id,
                json.dumps({"variants": []}),
                "sub-" + str(interp_id),
                now,
                session.user_id,
            ),
        )
    conn.commit()
    return interp_id


@pytest.fixture
def interp_a(dao, conn, session_a):
    return _make_interpretation(dao, conn, session_a)


@pytest.fixture
def interp_b(dao, conn, session_b):
    return _make_interpretation(dao, conn, session_b)


# reviewer_claims.report_id is NOT NULL (R9, human ruling 2026-09-13): a claim
# is recorded AGAINST a report, so these tests need one to name. The draft
# report is all they need -- this file is about the four claim recorders, not
# about the report lifecycle, which test_phase_6_submit_for_review.py owns.
@pytest.fixture
def report_a(dao, session_a, interp_a):
    return dao.create_report(session_a, interp_a)


@pytest.fixture
def report_b(dao, session_b, interp_b):
    return dao.create_report(session_b, interp_b)


class TestRecordAccept:
    """_record_accept: spec 13.2's 'accept' action -- an independent concurrence."""

    def test_inserts_single_accept_claim(self, dao, conn, session_a, interp_a, report_a):
        claim_id = dao._record_accept(
            session_a,
            interpretation_id=interp_a,
            report_id=report_a,
            actor_id=session_a.user_id,
            reason="Independently re-derived the same classification from ClinVar and gnomAD.",
        )

        with conn.cursor() as cur:
            cur.execute(
                "SELECT org_id, interpretation_id, claim_type, actor_id, reason, "
                "       evidence_json, supersedes, variant_key, classification "
                "FROM reviewer_claims WHERE id = %s",
                (claim_id,),
            )
            row = cur.fetchone()

        assert row is not None, "accept claim must be inserted"
        assert row[0] == session_a.org_id
        assert row[1] == interp_a
        assert row[2] == "accept"
        assert row[3] == session_a.user_id
        assert row[4].startswith("Independently re-derived")
        assert row[5] is None, "evidence_json is not written in this commit"
        assert row[6] is None, "an original claim supersedes nothing"
        assert row[7] is None, "accept is interpretation-scoped and names no variant"
        assert row[8] is None, "accept asserts no classification of its own"

    def test_inserts_exactly_one_row(self, dao, conn, session_a, interp_a, report_a):
        dao._record_accept(
            session_a, interpretation_id=interp_a, report_id=report_a, actor_id=session_a.user_id, reason="Concur."
        )
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM reviewer_claims WHERE interpretation_id = %s", (interp_a,))
            assert cur.fetchone()[0] == 1

    def test_records_timezone_aware_timestamp(self, dao, conn, session_a, interp_a, report_a):
        claim_id = dao._record_accept(
            session_a, interpretation_id=interp_a, report_id=report_a, actor_id=session_a.user_id, reason="Concur."
        )
        with conn.cursor() as cur:
            cur.execute('SELECT "timestamp" FROM reviewer_claims WHERE id = %s', (claim_id,))
            ts = cur.fetchone()[0]
        assert ts is not None
        assert ts.tzinfo is not None, "timestamp must be timezone-aware"

    def test_empty_reason_rejected(self, dao, session_a, interp_a, report_a):
        with pytest.raises(ValueError, match="reason"):
            dao._record_accept(
                session_a, interpretation_id=interp_a, report_id=report_a, actor_id=session_a.user_id, reason=""
            )

    def test_whitespace_only_reason_rejected(self, dao, session_a, interp_a, report_a):
        with pytest.raises(ValueError, match="reason"):
            dao._record_accept(
                session_a, interpretation_id=interp_a, report_id=report_a, actor_id=session_a.user_id, reason="   "
            )

    def test_none_reason_rejected(self, dao, session_a, interp_a, report_a):
        with pytest.raises(ValueError, match="reason"):
            dao._record_accept(
                session_a, interpretation_id=interp_a, report_id=report_a, actor_id=session_a.user_id, reason=None
            )

    def test_unknown_actor_rejected(self, dao, session_a, interp_a, report_a):
        with pytest.raises(NotFoundError):
            dao._record_accept(
                session_a, interpretation_id=interp_a, report_id=report_a, actor_id=uuid.uuid4(), reason="Concur."
            )

    def test_actor_from_other_org_rejected(self, dao, session_a, session_b, interp_a, report_a):
        """An actor who exists, but in a different org, is not an actor here."""
        with pytest.raises(NotFoundError):
            dao._record_accept(
                session_a, interpretation_id=interp_a, report_id=report_a, actor_id=session_b.user_id, reason="Concur."
            )

    def test_unknown_interpretation_rejected(self, dao, session_a, report_a):
        with pytest.raises(NotFoundError):
            dao._record_accept(
                session_a,
                interpretation_id=uuid.uuid4(),
                report_id=report_a,
                actor_id=session_a.user_id,
                reason="Concur.",
            )

    def test_cross_org_interpretation_rejected(self, dao, session_a, interp_b, report_b):
        """Org A cannot record a claim against Org B's interpretation."""
        with pytest.raises(NotFoundError):
            dao._record_accept(
                session_a, interpretation_id=interp_b, report_id=report_b, actor_id=session_a.user_id, reason="Concur."
            )

    def test_cross_org_claims_stay_in_their_own_org(
        self, dao, conn, session_a, session_b, interp_a, interp_b, report_a, report_b
    ):
        """Claims recorded in one org never land under the other org's id."""
        dao._record_accept(
            session_a, interpretation_id=interp_a, report_id=report_a, actor_id=session_a.user_id, reason="A concurs."
        )
        dao._record_accept(
            session_b, interpretation_id=interp_b, report_id=report_b, actor_id=session_b.user_id, reason="B concurs."
        )

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM reviewer_claims WHERE org_id = %s", (session_a.org_id,))
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT count(*) FROM reviewer_claims WHERE org_id = %s", (session_b.org_id,))
            assert cur.fetchone()[0] == 1

    def test_writes_audit_entry(self, dao, conn, session_a, interp_a, report_a):
        dao._record_accept(
            session_a, interpretation_id=interp_a, report_id=report_a, actor_id=session_a.user_id, reason="Concur."
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM audit_log WHERE org_id = %s AND action = 'record_accept'",
                (session_a.org_id,),
            )
            assert cur.fetchone()[0] == 1, "accept must be audited"


def _claim_row(conn, claim_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT org_id, interpretation_id, variant_key, claim_type, classification, "
            "       actor_id, reason, evidence_json, supersedes "
            "FROM reviewer_claims WHERE id = %s",
            (claim_id,),
        )
        return cur.fetchone()


class TestRecordDisagreement:
    """_record_disagreement: spec 13.2's 'disagree' -- appended, never an edit."""

    def test_inserts_disagree_claim_with_variant_and_classification(self, dao, conn, session_a, interp_a, report_a):
        variant_key = "chr1-1000-A-T"
        claim_id = dao._record_disagreement(
            session_a,
            interpretation_id=interp_a,
            report_id=report_a,
            variant_key=variant_key,
            actor_id=session_a.user_id,
            new_classification="Likely Benign",
            reason="Population frequency in gnomAD SAS is too high for a pathogenic call.",
        )

        row = _claim_row(conn, claim_id)
        assert row is not None
        assert row[0] == session_a.org_id
        assert row[1] == interp_a
        assert row[2] == variant_key
        assert row[3] == "disagree"
        assert row[4] == "Likely Benign"
        assert row[5] == session_a.user_id
        assert row[6].startswith("Population frequency")
        assert row[7] is None, "evidence_json is not written in this commit"
        assert row[8] is None, "an original claim supersedes nothing"

    def test_missing_variant_id_rejected(self, dao, session_a, interp_a, report_a):
        with pytest.raises(ValueError, match="variant_key"):
            dao._record_disagreement(
                session_a,
                interpretation_id=interp_a,
                report_id=report_a,
                variant_key=None,
                actor_id=session_a.user_id,
                new_classification="Benign",
                reason="Reason.",
            )

    def test_missing_classification_rejected(self, dao, session_a, interp_a, report_a):
        with pytest.raises(ValueError, match="classification"):
            dao._record_disagreement(
                session_a,
                interpretation_id=interp_a,
                report_id=report_a,
                variant_key="chr1-1000-A-T",
                actor_id=session_a.user_id,
                new_classification="",
                reason="Reason.",
            )

    def test_missing_reason_rejected(self, dao, session_a, interp_a, report_a):
        with pytest.raises(ValueError, match="reason"):
            dao._record_disagreement(
                session_a,
                interpretation_id=interp_a,
                report_id=report_a,
                variant_key="chr1-1000-A-T",
                actor_id=session_a.user_id,
                new_classification="Benign",
                reason="   ",
            )

    def test_cross_org_interpretation_rejected(self, dao, session_a, interp_b, report_b):
        with pytest.raises(NotFoundError):
            dao._record_disagreement(
                session_a,
                interpretation_id=interp_b,
                report_id=report_b,
                variant_key="chr1-1000-A-T",
                actor_id=session_a.user_id,
                new_classification="Benign",
                reason="Reason.",
            )

    def test_does_not_alter_an_existing_accept(self, dao, conn, session_a, interp_a, report_a):
        """A disagreement is appended; it never edits the claim it disputes."""
        accept_id = dao._record_accept(
            session_a, interpretation_id=interp_a, report_id=report_a, actor_id=session_a.user_id, reason="Concur."
        )
        dao._record_disagreement(
            session_a,
            interpretation_id=interp_a,
            report_id=report_a,
            variant_key="chr1-1000-A-T",
            actor_id=session_a.user_id,
            new_classification="Benign",
            reason="On reflection, benign.",
        )

        accept = _claim_row(conn, accept_id)
        assert accept[3] == "accept", "the earlier claim keeps its type"
        assert accept[4] is None, "the earlier claim keeps its (absent) classification"
        assert accept[8] is None, "append-only: the earlier row is never touched"

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM reviewer_claims WHERE interpretation_id = %s", (interp_a,))
            assert cur.fetchone()[0] == 2, "both claims stand"


class TestAddVariantByReviewer:
    """_add_variant_by_reviewer: spec 13.2's 'variant_added'."""

    def test_inserts_variant_added_claim(self, dao, conn, session_a, interp_a, report_a):
        variant_key = "chr1-1000-A-T"
        claim_id = dao._add_variant_by_reviewer(
            session_a,
            interpretation_id=interp_a,
            report_id=report_a,
            variant_key=variant_key,
            actor_id=session_a.user_id,
            acmg_classification="Pathogenic",
            reason="Known founder variant absent from the pipeline's panel.",
        )

        row = _claim_row(conn, claim_id)
        assert row[2] == variant_key
        assert row[3] == "variant_added"
        assert row[4] == "Pathogenic"
        assert row[6].startswith("Known founder variant")

    def test_missing_variant_id_rejected(self, dao, session_a, interp_a, report_a):
        with pytest.raises(ValueError, match="variant_key"):
            dao._add_variant_by_reviewer(
                session_a,
                interpretation_id=interp_a,
                report_id=report_a,
                variant_key=None,
                actor_id=session_a.user_id,
                acmg_classification="Pathogenic",
                reason="Reason.",
            )

    def test_missing_classification_rejected(self, dao, session_a, interp_a, report_a):
        """An addition with no call says a variant matters without saying what it means."""
        with pytest.raises(ValueError, match="classification"):
            dao._add_variant_by_reviewer(
                session_a,
                interpretation_id=interp_a,
                report_id=report_a,
                variant_key="chr1-1000-A-T",
                actor_id=session_a.user_id,
                acmg_classification=None,
                reason="Reason.",
            )

    def test_actor_from_other_org_rejected(self, dao, session_a, session_b, interp_a, report_a):
        with pytest.raises(NotFoundError):
            dao._add_variant_by_reviewer(
                session_a,
                interpretation_id=interp_a,
                report_id=report_a,
                variant_key="chr1-1000-A-T",
                actor_id=session_b.user_id,
                acmg_classification="Pathogenic",
                reason="Reason.",
            )


class TestMarkVariantNotRelevant:
    """_mark_variant_not_relevant: scoped out against the indication, not deleted."""

    def test_inserts_variant_not_relevant_claim(self, dao, conn, session_a, interp_a, report_a):
        variant_key = "chr1-1000-A-T"
        claim_id = dao._mark_variant_not_relevant(
            session_a,
            interpretation_id=interp_a,
            report_id=report_a,
            variant_key=variant_key,
            actor_id=session_a.user_id,
            reason="Cardiac gene; this indication is hereditary breast cancer.",
        )

        row = _claim_row(conn, claim_id)
        assert row[2] == variant_key
        assert row[3] == "variant_not_relevant"
        assert row[6].startswith("Cardiac gene")

    def test_asserts_no_classification(self, dao, conn, session_a, interp_a, report_a):
        """Scoping out is not a statement about what the variant means."""
        claim_id = dao._mark_variant_not_relevant(
            session_a,
            interpretation_id=interp_a,
            report_id=report_a,
            variant_key="chr1-1000-A-T",
            actor_id=session_a.user_id,
            reason="Out of scope for this indication.",
        )
        assert _claim_row(conn, claim_id)[4] is None

    def test_missing_variant_id_rejected(self, dao, session_a, interp_a, report_a):
        with pytest.raises(ValueError, match="variant_key"):
            dao._mark_variant_not_relevant(
                session_a,
                interpretation_id=interp_a,
                report_id=report_a,
                variant_key=None,
                actor_id=session_a.user_id,
                reason="Reason.",
            )

    def test_missing_reason_rejected(self, dao, session_a, interp_a, report_a):
        with pytest.raises(ValueError, match="reason"):
            dao._mark_variant_not_relevant(
                session_a,
                interpretation_id=interp_a,
                report_id=report_a,
                variant_key="chr1-1000-A-T",
                actor_id=session_a.user_id,
                reason=None,
            )


class TestAllFourClaimTypes:
    """Cross-cutting guarantees that must hold for every claim type."""

    def _record_all_four(self, dao, session, interp, report):
        dao._record_accept(
            session, interpretation_id=interp, report_id=report, actor_id=session.user_id, reason="Concur."
        )
        dao._record_disagreement(
            session,
            interpretation_id=interp,
            report_id=report,
            variant_key="chr1-1000-A-T",
            actor_id=session.user_id,
            new_classification="Benign",
            reason="Disagree.",
        )
        dao._add_variant_by_reviewer(
            session,
            interpretation_id=interp,
            report_id=report,
            variant_key="chr1-1000-A-T",
            actor_id=session.user_id,
            acmg_classification="Pathogenic",
            reason="Missed.",
        )
        dao._mark_variant_not_relevant(
            session,
            interpretation_id=interp,
            report_id=report,
            variant_key="chr1-1000-A-T",
            actor_id=session.user_id,
            reason="Out of scope.",
        )

    def test_all_four_recorded_against_one_interpretation(self, dao, conn, session_a, interp_a, report_a):
        self._record_all_four(dao, session_a, interp_a, report_a)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT claim_type FROM reviewer_claims WHERE interpretation_id = %s",
                (interp_a,),
            )
            types = {r[0] for r in cur.fetchall()}

        assert types == {
            "accept",
            "disagree",
            "variant_added",
            "variant_not_relevant",
        }, "all four of spec 13.2's actions must be recordable"

    def test_no_claim_writes_evidence_json(self, dao, conn, session_a, interp_a, report_a):
        """evidence_json stays NULL across every claim type in this commit."""
        self._record_all_four(dao, session_a, interp_a, report_a)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM reviewer_claims WHERE interpretation_id = %s AND evidence_json IS NOT NULL",
                (interp_a,),
            )
            assert cur.fetchone()[0] == 0

    def test_no_claim_writes_supersedes(self, dao, conn, session_a, interp_a, report_a):
        """Supersession is commit 3's; nothing here writes the pointer."""
        self._record_all_four(dao, session_a, interp_a, report_a)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM reviewer_claims WHERE interpretation_id = %s AND supersedes IS NOT NULL",
                (interp_a,),
            )
            assert cur.fetchone()[0] == 0

    def test_every_claim_type_is_audited(self, dao, conn, session_a, interp_a, report_a):
        self._record_all_four(dao, session_a, interp_a, report_a)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT action FROM audit_log WHERE org_id = %s AND resource_type = 'reviewer_claim'",
                (session_a.org_id,),
            )
            actions = {r[0] for r in cur.fetchall()}

        assert actions == {
            "record_accept",
            "record_disagreement",
            "add_variant_by_reviewer",
            "mark_variant_not_relevant",
        }, "every reviewer action must leave its own audit trail"

    def test_all_four_are_org_scoped(self, dao, conn, session_a, session_b, interp_a, interp_b, report_a, report_b):
        """Every claim type lands under its own org and no other."""
        self._record_all_four(dao, session_a, interp_a, report_a)
        self._record_all_four(dao, session_b, interp_b, report_b)

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM reviewer_claims WHERE org_id = %s", (session_a.org_id,))
            assert cur.fetchone()[0] == 4
            cur.execute("SELECT count(*) FROM reviewer_claims WHERE org_id = %s", (session_b.org_id,))
            assert cur.fetchone()[0] == 4
