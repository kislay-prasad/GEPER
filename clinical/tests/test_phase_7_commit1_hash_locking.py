"""
tests/test_phase_7_commit1_hash_locking.py
────────────────────────────────────────────

Phase 7 commit 1: hash scope corrected, verify_report_integrity, lineage FK.

This corrects a Phase 6 decision. _approve_report's content_hash originally
covered run_document only; reviewer_claims were excluded on the reasoning that
spec 13.2 leaves WHICH CLAIMS A RELEASED REPORT DISPLAYS as an undecided
expert-branch question, and hashing them felt like fixing undecided rendering
into an immutability check early.

That conflated two questions. Which claims are DISPLAYED is rendering, and the
expert branch still governs that, undisturbed by this commit. Which claims
EXISTED AT APPROVAL is a fact about what was approved, fixed the moment an
Approver signs. The hash now covers the second question: run_document AND the
full set of reviewer_claims against it, as stored -- not as any surface later
renders them.

TestVerifyReportIntegrityDetectsTampering is the test the commit exists to
prove: a reviewer_claims row edited after approval, via a direct database
write that bypasses the application layer entirely (reviewer_claims is
append-only against clinical_app itself -- see schema.sql's REVOKE UPDATE --
so this is exactly the class of tampering a content hash exists to catch,
not a channel the application would ever open on its own). Before this
commit, nothing detected it: the old hash never covered reviewer_claims at
all, so a mutated claim and an unmutated one hashed identically upstream of
that gap. This test's purpose is to fail against the OLD hash scope and pass
against the new one.
"""

from __future__ import annotations

import datetime
from datetime import date, timezone
import hashlib
import json
import os
import uuid

import pytest

from clinical.data_access import (
    BcryptHasher,
    DataAccess,
    NotFoundError,
    SystemClock,
)

DSN = os.getenv("CLINICAL_TEST_DSN")
SCHEMA_PATH = __import__("pathlib").Path(__file__).parent.parent / "schema.sql"

_CREATE_ROLE = "DO $$ BEGIN CREATE ROLE clinical_app; EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
_CREATE_ROLE_RETENTION = (
    "DO $$ BEGIN CREATE ROLE clinical_retention; EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
)


def _foreign_key_violation():
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    return psycopg.errors.ForeignKeyViolation


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


def _role_session(dao, admin_session, org_id, email, role):
    user_id = dao.create_user(org_id, email, "password")
    dao.assign_role(admin_session, user_id, role, basis="Test fixture role grant")
    return dao.login(email, org_id, "password")


@pytest.fixture
def interpreter_a(dao, session_a, org_a):
    return _role_session(dao, session_a, org_a, "interpreter@org-a.test", "Interpreter")


@pytest.fixture
def interpreter_b(dao, session_b, org_b):
    return _role_session(dao, session_b, org_b, "interpreter@org-b.test", "Interpreter")


@pytest.fixture
def approver_a(dao, session_a, org_a):
    return _role_session(dao, session_a, org_a, "approver@org-a.test", "Approver")


@pytest.fixture
def approver_b(dao, session_b, org_b):
    return _role_session(dao, session_b, org_b, "approver@org-b.test", "Approver")


def _make_interpretation(dao, conn, session, run_document=None, parent_interpretation_id=None):
    """
    Build the full FK chain an interpretation needs:
    test -> patient -> consent -> order -> sample -> sequencing_run -> vcf -> interpretation.
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
    document = run_document if run_document is not None else {"variants": []}
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
            "(org_id, id, vcf_id, run_document, submission_key, parent_interpretation_id, created_at, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                org_id,
                interp_id,
                vcf_id,
                json.dumps(document),
                "sub-" + str(interp_id),
                parent_interpretation_id,
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


def _add_claim(dao, session, interp_id, actor_id, reason="Independently re-derived the same classification."):
    return dao._record_accept(session, interpretation_id=interp_id, actor_id=actor_id, reason=reason)


def _approved_report(dao, conn, session, interpreter, approver, interp_id):
    """Full workflow: create -> submit_for_review (needs >=1 claim) -> approve. Returns report_id."""
    _add_claim(dao, session, interp_id, interpreter.user_id)
    report_id = dao.create_report(session, interp_id)
    dao.submit_for_review(interpreter, report_id)
    dao._approve_report(approver, report_id, actor_id=approver.user_id)
    return report_id


def _expected_hash(conn, interp_id):
    """Recompute the expected hash independently of DataAccess, from raw rows, for a cross-check."""
    with conn.cursor() as cur:
        cur.execute("SELECT run_document FROM interpretations WHERE id = %s", (interp_id,))
        run_document = cur.fetchone()[0]
        if isinstance(run_document, str):
            run_document = json.loads(run_document)
        cur.execute(
            'SELECT claim_type, variant_key, classification, reason, actor_id, "timestamp", '
            "evidence_json, supersedes "
            'FROM reviewer_claims WHERE interpretation_id = %s ORDER BY "timestamp", id',
            (interp_id,),
        )
        claims = []
        for row in cur.fetchall():
            evidence_json = row[6]
            if isinstance(evidence_json, str):
                evidence_json = json.loads(evidence_json)
            claims.append(
                {
                    "claim_type": row[0],
                    "variant_key": row[1],
                    "classification": row[2],
                    "reason": row[3],
                    "actor_id": str(row[4]),
                    "timestamp": row[5].isoformat(),
                    "evidence_json": evidence_json,
                    "supersedes": str(row[7]) if row[7] is not None else None,
                }
            )
    payload = {"run_document": run_document, "reviewer_claims": claims}
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


class TestApprovalHashCoversReviewerClaims:
    """The hash scope correction: content_hash must reflect run_document AND reviewer_claims."""

    def test_hash_matches_independent_recomputation(self, dao, conn, session_a, interpreter_a, approver_a, interp_a):
        report_id = _approved_report(dao, conn, session_a, interpreter_a, approver_a, interp_a)

        with conn.cursor() as cur:
            cur.execute("SELECT content_hash FROM reports WHERE id = %s", (report_id,))
            stored = cur.fetchone()[0]

        assert stored == _expected_hash(conn, interp_a)

    def test_hash_differs_when_claim_content_differs(self, dao, conn, session_a, interpreter_a, approver_a):
        """
        Two interpretations with an IDENTICAL run_document but different
        claim reasons must approve to different hashes -- proof the claim
        content is actually inside the hash, not merely present as a row
        the hash ignores.
        """
        interp_1 = _make_interpretation(dao, conn, session_a, run_document={"variants": []})
        interp_2 = _make_interpretation(dao, conn, session_a, run_document={"variants": []})

        _add_claim(dao, session_a, interp_1, interpreter_a.user_id, reason="Reason A.")
        report_1 = dao.create_report(session_a, interp_1)
        dao.submit_for_review(interpreter_a, report_1)
        dao._approve_report(approver_a, report_1, actor_id=approver_a.user_id)

        _add_claim(dao, session_a, interp_2, interpreter_a.user_id, reason="Reason B, completely different.")
        report_2 = dao.create_report(session_a, interp_2)
        dao.submit_for_review(interpreter_a, report_2)
        dao._approve_report(approver_a, report_2, actor_id=approver_a.user_id)

        with conn.cursor() as cur:
            cur.execute("SELECT content_hash FROM reports WHERE id = %s", (report_1,))
            hash_1 = cur.fetchone()[0]
            cur.execute("SELECT content_hash FROM reports WHERE id = %s", (report_2,))
            hash_2 = cur.fetchone()[0]

        assert hash_1 != hash_2

    def test_hash_unaffected_by_claim_added_after_approval(
        self, dao, conn, session_a, interpreter_a, approver_a, interp_a
    ):
        """content_hash is fixed at approval time; a claim added later must not retroactively change it."""
        report_id = _approved_report(dao, conn, session_a, interpreter_a, approver_a, interp_a)
        with conn.cursor() as cur:
            cur.execute("SELECT content_hash FROM reports WHERE id = %s", (report_id,))
            hash_before = cur.fetchone()[0]

        _add_claim(dao, session_a, interp_a, interpreter_a.user_id, reason="A later, second claim.")

        with conn.cursor() as cur:
            cur.execute("SELECT content_hash FROM reports WHERE id = %s", (report_id,))
            hash_after = cur.fetchone()[0]

        assert hash_before == hash_after


class TestVerifyReportIntegrityDetectsTampering:
    """
    verify_report_integrity recomputes from CURRENT data and compares to the
    hash stored at approval. This class is the point of the commit.
    """

    def test_verify_passes_immediately_after_approval(self, dao, conn, session_a, interpreter_a, approver_a, interp_a):
        report_id = _approved_report(dao, conn, session_a, interpreter_a, approver_a, interp_a)
        assert dao.verify_report_integrity(approver_a, report_id) is True

    def test_detects_reviewer_claim_tampered_after_approval(
        self, dao, conn, session_a, interpreter_a, approver_a, interp_a
    ):
        """
        THE CORE TEST. reviewer_claims is append-only against clinical_app
        (schema.sql REVOKE UPDATE) -- this UPDATE goes through the raw
        superuser test connection specifically to simulate a write that
        bypasses the application role entirely, which is exactly the threat
        a content hash exists to catch. Against the OLD hash scope (which
        never covered reviewer_claims), this tamper was invisible:
        verify_report_integrity did not exist, and the stored hash could
        not have detected it even conceptually, because reviewer_claims was
        never part of what was hashed.
        """
        report_id = _approved_report(dao, conn, session_a, interpreter_a, approver_a, interp_a)
        assert dao.verify_report_integrity(approver_a, report_id) is True

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reviewer_claims SET reason = %s WHERE interpretation_id = %s",
                ("TAMPERED: this was not the reviewer's original reasoning.", interp_a),
            )
        conn.commit()

        assert dao.verify_report_integrity(approver_a, report_id) is False

    # test_detects_run_document_tampered_after_approval -- RETIRED (D0b half two).
    #
    # This test simulated a post-approval tamper of interpretations.run_document
    # via a raw UPDATE, then asserted verify_report_integrity() caught it
    # (DETECTION). The content-immutability trigger (clinical/schema.sql,
    # enforce_content_immutability()) now makes interpretations.run_document
    # immutable outright: no UPDATE to that column succeeds for any principal
    # except through the privilege-gated tombstone path, which does not touch
    # run_document. A STRONGER GUARANTEE REPLACED THE ONE THIS TEST EXERCISED --
    # prevention superseded detection on this path -- and the attack the test
    # relied on to demonstrate detection can no longer be constructed at all.
    #
    # Ruled out explicitly, not chosen by default, two alternatives that looked
    # cheaper:
    #   - Disabling the trigger for the test (as TestRefusalCanItselfFail does,
    #     legitimately, to prove the refusal itself is real) would leave the
    #     codebase holding a working, committed recipe for turning the
    #     guarantee off -- exercising a world the running system no longer has.
    #   - Rewriting the test against a still-mutable column would change what
    #     is tested while keeping the old name and calling it the same test.
    #
    # What this means for what the system can DEMONSTRATE: detection remains
    # implemented in verify_report_integrity() for run_document, but is now
    # UNTESTED on this specific path, because prevention deletes the executable
    # evidence that detection still works here. See spec 15.1 for the same
    # distinction stated for the design as a whole. Detection against
    # reviewer_claims and evidence_json (still mutable-by-attack, not covered
    # by this trigger) remains both implemented and tested, unaffected --
    # see test_detects_supersedes_tampered_after_approval and
    # test_detects_evidence_json_tampered_after_approval below.

    def test_detects_claim_added_after_approval(self, dao, conn, session_a, interpreter_a, approver_a, interp_a):
        """A new claim, not just an edited one, is also a change the hash must catch."""
        report_id = _approved_report(dao, conn, session_a, interpreter_a, approver_a, interp_a)
        assert dao.verify_report_integrity(approver_a, report_id) is True

        _add_claim(dao, session_a, interp_a, interpreter_a.user_id, reason="Added after the fact.")

        assert dao.verify_report_integrity(approver_a, report_id) is False

    def test_detects_evidence_json_tampered_after_approval(
        self, dao, conn, session_a, interpreter_a, approver_a, interp_a
    ):
        """
        No method writes evidence_json yet (it is NULL pending the expert
        branch on what shape evidence takes), but the human's ruling is
        that the hash covers every stored column with no exclusion list --
        precisely so that the day something DOES write this column, no
        revisit of this function is needed for it to be covered. Simulated
        here via a raw UPDATE, exactly like the reviewer_claims tamper test
        above, for the same reason: this column is meant to be covered
        starting now, before anything populates it in the ordinary course.
        """
        report_id = _approved_report(dao, conn, session_a, interpreter_a, approver_a, interp_a)
        assert dao.verify_report_integrity(approver_a, report_id) is True

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reviewer_claims SET evidence_json = %s WHERE interpretation_id = %s",
                (json.dumps({"consulted": "ClinVar, gnomAD"}), interp_a),
            )
        conn.commit()

        assert dao.verify_report_integrity(approver_a, report_id) is False

    def test_detects_supersedes_tampered_after_approval(
        self, dao, conn, session_a, interpreter_a, approver_a, interp_a
    ):
        """
        supersedes is a pointer a claim carries about what it replaces.
        Append-only against clinical_app makes altering an existing claim's
        supersedes value unlikely, not impossible -- and per the ruling, a
        hash with an exception for "columns append-only makes unlikely" has
        an exception to explain later. Two claims are needed so the second
        can legitimately point at the first before the tamper.
        """
        first_claim = _add_claim(dao, session_a, interp_a, interpreter_a.user_id, reason="Original claim.")
        second_claim = dao._record_accept(
            session_a, interpretation_id=interp_a, actor_id=interpreter_a.user_id, reason="A second, unrelated claim."
        )
        report_id = dao.create_report(session_a, interp_a)
        dao.submit_for_review(interpreter_a, report_id)
        dao._approve_report(approver_a, report_id, actor_id=approver_a.user_id)
        assert dao.verify_report_integrity(approver_a, report_id) is True

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reviewer_claims SET supersedes = %s WHERE id = %s",
                (first_claim, second_claim),
            )
        conn.commit()

        assert dao.verify_report_integrity(approver_a, report_id) is False

    def test_unapproved_report_rejected(self, dao, session_a, interpreter_a, approver_a, interp_a):
        report_id = dao.create_report(session_a, interp_a)  # still 'draft', never approved
        with pytest.raises(ValueError, match="not been approved"):
            dao.verify_report_integrity(approver_a, report_id)

    def test_unknown_report_rejected(self, dao, approver_a):
        with pytest.raises(NotFoundError):
            dao.verify_report_integrity(approver_a, uuid.uuid4())

    def test_cross_org_report_rejected(
        self, dao, conn, session_a, session_b, interpreter_b, approver_a, approver_b, interp_b
    ):
        report_id = _approved_report(dao, conn, session_b, interpreter_b, approver_b, interp_b)
        with pytest.raises(NotFoundError):
            dao.verify_report_integrity(approver_a, report_id)


class TestLineageColumn:
    """interpretations.parent_interpretation_id: column only, no methods in this commit.

    This is the class that actually exercises `fk_interp_parent`, the
    composite FK on this column (confirmed 2026-09-11 by dropping the
    constraint and re-running: `test_rejects_a_cross_org_parent`/
    `test_rejects_an_unknown_parent` below both went red instantly,
    since raw SQL here bypasses `create_reanalysis()` entirely). See
    `test_phase_7_commit3_reanalysis.py::
    TestCreateReanalysisRejectsAnInvalidParentViaItsOwnPrecheck` for
    the sibling tests that go through `create_reanalysis()` itself --
    those two test the DAO's own pre-check, not this constraint; the
    same mutation left them green.
    """

    def test_defaults_to_null(self, conn, interp_a):
        with conn.cursor() as cur:
            cur.execute("SELECT parent_interpretation_id FROM interpretations WHERE id = %s", (interp_a,))
            assert cur.fetchone()[0] is None

    def test_accepts_a_same_org_parent(self, dao, conn, session_a):
        """
        Re-expressed at INSERT (not a post-hoc UPDATE): interpretations has no
        legitimate UPDATE path left under the content-immutability trigger
        (clinical/schema.sql), and parent_interpretation_id is no exception --
        it is set once, at creation, same as every other lineage fact. The
        property under test is the composite FK constraint itself, not the
        UPDATE mechanism the original test happened to use to exercise it.
        """
        parent_id = _make_interpretation(dao, conn, session_a)
        child_id = _make_interpretation(dao, conn, session_a, parent_interpretation_id=parent_id)
        with conn.cursor() as cur:
            cur.execute("SELECT parent_interpretation_id FROM interpretations WHERE id = %s", (child_id,))
            assert cur.fetchone()[0] == parent_id

    def test_rejects_a_cross_org_parent(self, dao, conn, session_a, session_b):
        """
        Exception narrowed to ForeignKeyViolation by name (was bare Exception):
        under the content-immutability trigger, an UPDATE-based version of this
        test would also raise an Exception subclass on a same-org parent
        (ImmutabilityViolationError, via RAISE EXCEPTION P0001) -- a bare
        `pytest.raises(Exception)` cannot distinguish "the FK rejected this" from
        "the trigger rejected this for an unrelated reason," so it would report
        green even with the composite FK constraint dropped entirely. See
        clinical/tests/test_privilege_boundary.py, which asserts
        InsufficientPrivilege by name for the same reason.
        """
        parent_id = _make_interpretation(dao, conn, session_b)  # org B
        with pytest.raises(_foreign_key_violation()):
            _make_interpretation(dao, conn, session_a, parent_interpretation_id=parent_id)  # org A
        conn.rollback()

    def test_rejects_an_unknown_parent(self, dao, conn, session_a):
        """Exception narrowed to ForeignKeyViolation by name; see test_rejects_a_cross_org_parent."""
        with pytest.raises(_foreign_key_violation()):
            _make_interpretation(dao, conn, session_a, parent_interpretation_id=uuid.uuid4())
        conn.rollback()
