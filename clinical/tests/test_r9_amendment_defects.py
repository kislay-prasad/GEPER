"""
tests/test_r9_amendment_defects.py
──────────────────────────────────

R9: the two clinical amendment defects, and the one root cause under both.

An amendment is a SECOND reports row on the SAME interpretation
(create_amendment). Reviewer claims used to be scoped to the interpretation
alone, so every claim on that interpretation was equally every report's
claim. Two defects followed:

  (b) THE FALSE TAMPER ALARM. _compute_report_content_hash() hashed every
      claim on the interpretation, so the FIRST claim recorded for an
      amendment changed the ORIGINAL report's recomputed hash and
      verify_report_integrity(original) returned False. Nothing had been
      tampered with. A false alarm on an integrity check teaches people to
      ignore integrity checks.

  (a) THE AMENDED REPORT WITH THE ORIGINAL'S BODY. An amendment could not
      carry reviewer claims of its own: no query could answer "this
      report's claims" at all.

Fixed by reviewer_claims.report_id (human ruling 2026-09-13), which also
resolves expert-branch decision point 4 in that table's schema comment: a
report carries its OWN claims and only its own, never the cumulative set.

Both classes below were RED against b1ceaf1 -- (b) behaviourally, with the
false alarm observed; (a) because get_reviewer_claims and the column it
reads did not exist. The controls (a genuine tamper still detected, an
unamended report unchanged) were green before and must stay green.

NOT COVERED HERE, deliberately: there is no clinical report-BODY renderer
in this codebase, and this change does not build one. These tests stop at
the data model -- "this report's claims" being answerable and correct,
which is what a renderer will need.
"""

from __future__ import annotations

import datetime
from datetime import date, timezone
import json
import os
import uuid

import pytest

from clinical.data_access import BcryptHasher, DataAccess, SystemClock

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


def _role_session(dao, admin_session, org_id, email, role):
    user_id = dao.create_user(org_id, email, "password")
    dao.assign_role(admin_session, user_id, role, basis="Test fixture role grant")
    return dao.login(email, org_id, "password")


@pytest.fixture
def interpreter_a(dao, session_a, org_a):
    return _role_session(dao, session_a, org_a, "interpreter@org-a.test", "Interpreter")


@pytest.fixture
def approver_a(dao, session_a, org_a):
    return _role_session(dao, session_a, org_a, "approver@org-a.test", "Approver")


def _make_interpretation(dao, conn, session, run_document=None):
    """
    Build the full FK chain an interpretation needs:
    test -> patient -> consent -> order -> sample -> sequencing_run -> vcf -> interpretation.
    (Same shape as test_phase_7_commit1_hash_locking.py's helper.)
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
    document = run_document if run_document is not None else {"variants": [{"key": "1-100-A-T"}]}
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
            (org_id, interp_id, vcf_id, json.dumps(document), "sub-" + str(interp_id), now, session.user_id),
        )
    conn.commit()
    return interp_id


@pytest.fixture
def interp_a(dao, conn, session_a):
    return _make_interpretation(dao, conn, session_a)


def _approved_original(dao, session, interpreter, approver, interp_id):
    report_id = dao.create_report(session, interp_id)
    dao._record_accept(
        session,
        interpretation_id=interp_id,
        report_id=report_id,
        actor_id=interpreter.user_id,
        reason="Independently re-derived the same classification.",
    )
    dao.submit_for_review(interpreter, report_id)
    dao._approve_report(approver, report_id, actor_id=approver.user_id)
    return report_id


class TestAmendmentDoesNotFalselyTamperTheOriginal:
    """
    R9 (b). Creating an amendment and recording the reviewer claim that
    justifies it must not make the ORIGINAL report read as tampered.
    """

    def test_original_still_verifies_after_amendment_is_claimed(
        self, dao, conn, session_a, interpreter_a, approver_a, interp_a
    ):
        original = _approved_original(dao, session_a, interpreter_a, approver_a, interp_a)
        assert dao.verify_report_integrity(approver_a, original) is True

        amendment, _ = dao.create_amendment(session_a, original, "ClinVar reclassified the variant")

        # The amendment's own reviewer claim: the clinical reason it exists.
        dao._record_disagreement(
            session_a,
            interpretation_id=interp_a,
            report_id=amendment,
            variant_key="1-100-A-T",
            actor_id=interpreter_a.user_id,
            new_classification="Pathogenic",
            reason="Reclassified on the new ClinVar submission.",
        )

        assert dao.verify_report_integrity(approver_a, original) is True

    # ── Controls: these must be green before AND after the fix ──────────────

    def test_genuine_tamper_of_an_approved_claim_still_fails(
        self, dao, conn, session_a, interpreter_a, approver_a, interp_a
    ):
        """
        The claim that WAS part of the approved content is edited in place by
        a write that bypasses clinical_app entirely. Detection must survive
        any cutoff: this row is inside every reasonable definition of "what
        was approved".
        """
        original = _approved_original(dao, session_a, interpreter_a, approver_a, interp_a)
        assert dao.verify_report_integrity(approver_a, original) is True

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reviewer_claims SET reason = %s WHERE interpretation_id = %s",
                ("TAMPERED: not the reviewer's original reasoning.", interp_a),
            )
        conn.commit()

        assert dao.verify_report_integrity(approver_a, original) is False

    def test_unamended_report_is_unchanged(self, dao, conn, session_a, interpreter_a, approver_a, interp_a):
        """No amendment, no new claim: verification is unaffected by the fix."""
        original = _approved_original(dao, session_a, interpreter_a, approver_a, interp_a)
        assert dao.verify_report_integrity(approver_a, original) is True
        assert dao.verify_report_integrity(approver_a, original) is True

    def test_amendment_claim_does_not_change_the_originals_stored_hash(
        self, dao, conn, session_a, interpreter_a, approver_a, interp_a
    ):
        """
        The stored hash is written once at approval and nothing rewrites it.
        Pinned separately from verify_report_integrity so a future bug that
        made them agree by rewriting the stored value -- rather than by
        computing the right thing -- cannot pass as a fix.
        """
        original = _approved_original(dao, session_a, interpreter_a, approver_a, interp_a)
        with conn.cursor() as cur:
            cur.execute("SELECT content_hash FROM reports WHERE id = %s", (original,))
            before = cur.fetchone()[0]

        amendment, _ = dao.create_amendment(session_a, original, "ClinVar reclassified the variant")
        dao._record_accept(
            session_a,
            interpretation_id=interp_a,
            report_id=amendment,
            actor_id=interpreter_a.user_id,
            reason="Re-reviewed against the new submission.",
        )

        with conn.cursor() as cur:
            cur.execute("SELECT content_hash FROM reports WHERE id = %s", (original,))
            after = cur.fetchone()[0]

        assert before == after


class TestAReportCarriesItsOwnClaimsAndOnlyItsOwn:
    """
    R9 (a), the data-model half. Human ruling 2026-09-13, resolving
    expert-branch decision point 4 (clinical/schema.sql, reviewer_claims):
    an amended report carries its OWN reviewer claims, never the cumulative
    set including the original's.

    NOT tested here, because it does not exist and this change deliberately
    does not build it: there is no clinical report-BODY renderer. These tests
    pin that "this report's claims" is answerable and correct, which is what
    a renderer will need. See the commit message.
    """

    def test_original_and_amendment_claims_are_separately_retrievable(
        self, dao, conn, session_a, interpreter_a, approver_a, interp_a
    ):
        original = _approved_original(dao, session_a, interpreter_a, approver_a, interp_a)
        amendment, _ = dao.create_amendment(session_a, original, "ClinVar reclassified the variant")
        dao._record_disagreement(
            session_a,
            interpretation_id=interp_a,
            report_id=amendment,
            variant_key="1-100-A-T",
            actor_id=interpreter_a.user_id,
            new_classification="Pathogenic",
            reason="Reclassified on the new ClinVar submission.",
        )

        original_claims = dao.get_reviewer_claims(session_a, original)
        amendment_claims = dao.get_reviewer_claims(session_a, amendment)

        assert [c["claim_type"] for c in original_claims] == ["accept"]
        assert [c["claim_type"] for c in amendment_claims] == ["disagree"]

        # The bodies differ where they must: the amendment carries its own
        # reviewer reasoning, the original still carries only what was
        # approved with it.
        assert original_claims != amendment_claims
        assert amendment_claims[0]["reason"] == "Reclassified on the new ClinVar submission."
        assert original_claims[0]["reason"] == "Independently re-derived the same classification."

    def test_claims_do_not_leak_between_original_and_amendment(
        self, dao, conn, session_a, interpreter_a, approver_a, interp_a
    ):
        """
        Not the cumulative set, in either direction: the amendment does not
        inherit the original's claims, and the original does not acquire the
        amendment's. Both live on one interpretation, which is exactly the
        case the old interpretation-scoped model could not represent.
        """
        original = _approved_original(dao, session_a, interpreter_a, approver_a, interp_a)
        amendment, _ = dao.create_amendment(session_a, original, "ClinVar reclassified the variant")
        dao._record_accept(
            session_a,
            interpretation_id=interp_a,
            report_id=amendment,
            actor_id=interpreter_a.user_id,
            reason="Re-reviewed against the new submission.",
        )

        original_ids = {c["id"] for c in dao.get_reviewer_claims(session_a, original)}
        amendment_ids = {c["id"] for c in dao.get_reviewer_claims(session_a, amendment)}

        assert len(original_ids) == 1
        assert len(amendment_ids) == 1
        assert original_ids.isdisjoint(amendment_ids)

    def test_an_amendment_cannot_be_submitted_on_the_originals_review(
        self, dao, conn, session_a, interpreter_a, approver_a, interp_a
    ):
        """
        submit_for_review requires a claim OF THIS REPORT. An amendment that
        has had no review of its own must refuse, rather than riding on the
        original's reviewer -- who never read it.
        """
        original = _approved_original(dao, session_a, interpreter_a, approver_a, interp_a)
        amendment, _ = dao.create_amendment(session_a, original, "ClinVar reclassified the variant")

        with pytest.raises(ValueError, match="no reviewer claim of its own"):
            dao.submit_for_review(interpreter_a, amendment)

    def test_a_claim_cannot_name_a_report_of_another_interpretation(
        self, dao, conn, session_a, interpreter_a, approver_a, interp_a
    ):
        """
        report_id is not a free-text association: the report must be a report
        OF the interpretation the claim is about.
        """
        other_interp = _make_interpretation(dao, conn, session_a)
        other_report = dao.create_report(session_a, other_interp)

        with pytest.raises(ValueError, match="not a report of interpretation"):
            dao._record_accept(
                session_a,
                interpretation_id=interp_a,
                report_id=other_report,
                actor_id=interpreter_a.user_id,
                reason="Concur.",
            )

    def test_unknown_report_rejected(self, dao, session_a, interpreter_a):
        from clinical.data_access import NotFoundError

        with pytest.raises(NotFoundError):
            dao.get_reviewer_claims(session_a, uuid.uuid4())
