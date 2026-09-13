"""
tests/test_w118_report_revision_reader.py
─────────────────────────────────────────

w118: `DataAccess.get_report_revision` -- the read that makes the revision
banners REACHABLE.

The banner wording was signed off 2026-09-11 and the rendering merged with it
(geper d28da0a), but nothing ever read `amendments`, `interpretations.
parent_interpretation_id` or `release_events` into a document, so an amended
report still rendered as an original. These tests drive the reader against a
real database and assert on the FACTS it returns -- the block
`geper/report/clinical_report_builder.py::normalize_report_revision` consumes.

Human rulings of 2026-09-13, one class each:

  R2  "originally issued on" is the RELEASE date; approved-but-never-released
      reports report the approval date AND say so via the basis.
  R3  a DRAFT amendment does not supersede the original.
  R4  the amended report's date uses the same basis as R2.
  R5  several amendments: name the latest approved one; print its reason only
      when it is the amendment that superseded THIS document, else the count.
  R6  re-analysis: DIRECT children only, created AFTER this report.
  R7  a broken revision record raises; it is never a marker or an omission.
  R8  a report stays superseded after retention deletes its successor, and
      says the successor is no longer retained.

CROSS-ORG: every read here is org-scoped, and `test_cross_org_*` proves it by
asking org B about org A's report rather than by inspecting SQL.
"""

from __future__ import annotations

import datetime
from datetime import date, timezone
import json
import os
import uuid

import pytest

from clinical.data_access import (
    BcryptHasher,
    BrokenRevisionRecordError,
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

T0 = datetime.datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)


def _at(days: int = 0, hours: int = 0) -> datetime.datetime:
    return T0 + datetime.timedelta(days=days, hours=hours)


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


def _admin_session(dao, conn, org_id, email, name):
    user_id = dao.create_user(
        org_id,
        email,
        "password",
        full_name=name,
        registration_number="MCI-" + email.split("@")[0],
        hospital="Apollo Hospitals",
    )
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
            "VALUES (%s, %s, 'Administrator', %s, %s)",
            (user_id, org_id, user_id, T0),
        )
    conn.commit()
    return dao.login(email, org_id, "password")


@pytest.fixture
def org_a(dao):
    return dao.create_organisation("Org A")


@pytest.fixture
def org_b(dao):
    return dao.create_organisation("Org B")


@pytest.fixture
def session_a(dao, org_a, conn):
    return _admin_session(dao, conn, org_a, "dr-rao@org-a.test", "Dr A. Rao")


@pytest.fixture
def session_b(dao, org_b, conn):
    return _admin_session(dao, conn, org_b, "dr-bose@org-b.test", "Dr B. Bose")


def _make_interpretation(dao, conn, session, parent_interpretation_id=None, created_at=None, vcf_id=None):
    org_id = session.org_id
    created_at = created_at or T0
    if vcf_id is None:
        test_id = uuid.uuid4()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tests (test_id, org_id, name, assembly, status, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (test_id, org_id, "Panel", "GRCh38", "active", T0),
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
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO samples (sample_id, order_id, org_id, type, collected_at, collected_by, qc_status) "
                "VALUES (%s, %s, %s, 'blood', %s, %s, 'passed')",
                (sample_id, order_id, org_id, T0, session.user_id),
            )
            cur.execute(
                "INSERT INTO sequencing_runs (org_id, id, sample_id, created_at, created_by) "
                "VALUES (%s, %s, %s, %s, %s)",
                (org_id, run_id, sample_id, T0, session.user_id),
            )
            cur.execute(
                "INSERT INTO vcfs (org_id, id, sequencing_run_id, vcf_path, content_hash, created_at, created_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (org_id, vcf_id, run_id, "/tmp/x.vcf", "0" * 64, T0, session.user_id),
            )
        conn.commit()

    interp_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO interpretations "
            "(org_id, id, vcf_id, run_document, submission_key, parent_interpretation_id, created_at, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                org_id,
                interp_id,
                vcf_id,
                json.dumps({"variants": []}),
                "sub-" + str(interp_id),
                parent_interpretation_id,
                created_at,
                session.user_id,
            ),
        )
    conn.commit()
    return interp_id, vcf_id


@pytest.fixture
def interp_a(dao, conn, session_a):
    return _make_interpretation(dao, conn, session_a)


def _approve(conn, report_id, approver_id, at=None):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE reports SET state = 'approved', approver_id = %s, approved_at = %s, content_hash = %s "
            "WHERE id = %s",
            (approver_id, at or _at(days=1), "0" * 64, report_id),
        )
    conn.commit()


def _release(conn, org_id, report_id, actor_id, at=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO release_events (org_id, id, report_id, consumer, released_at, released_by, content_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (org_id, uuid.uuid4(), report_id, "ordering_clinician", at or _at(days=2), actor_id, "0" * 64),
        )
        cur.execute("UPDATE reports SET state = 'released' WHERE id = %s", (report_id,))
    conn.commit()


def _tombstone_report(conn, report_id, actor_id):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE reports SET tombstoned_at = %s, tombstoned_by = %s WHERE id = %s",
            (_at(days=400), actor_id, report_id),
        )
    conn.commit()


@pytest.fixture
def released_original(dao, conn, session_a, interp_a):
    """An original report, approved on day 1 and released on day 2."""
    interp_id, _vcf_id = interp_a
    report_id = dao.create_report(session_a, interp_id)
    _approve(conn, report_id, session_a.user_id)
    _release(conn, session_a.org_id, report_id, session_a.user_id)
    return report_id


class TestNoRevisionState:
    def test_a_plain_report_reports_no_state_at_all(self, dao, conn, session_a, interp_a):
        interp_id, _ = interp_a
        report_id = dao.create_report(session_a, interp_id)
        assert dao.get_report_revision(session_a, report_id) == {}

    def test_unknown_report_is_not_found(self, dao, session_a):
        with pytest.raises(NotFoundError):
            dao.get_report_revision(session_a, uuid.uuid4())

    def test_cross_org_report_is_not_found(self, dao, session_b, released_original):
        with pytest.raises(NotFoundError):
            dao.get_report_revision(session_b, released_original)


class TestR2OriginalIssueDateIsTheReleaseDate:
    def test_released_original_uses_the_release_date(self, dao, conn, session_a, released_original):
        amendment_report_id, _ = dao.create_amendment(session_a, released_original, "ClinVar reclassified BRCA1")
        block = dao.get_report_revision(session_a, amendment_report_id)["amends"]
        assert block["original_issued_basis"] == "released"
        assert block["original_issued_at"] == _at(days=2)
        # Not the approval date, which is a different day in this fixture.
        assert block["original_issued_at"] != _at(days=1)

    def test_approved_but_never_released_original_uses_the_approval_date_and_says_so(
        self, dao, conn, session_a, interp_a
    ):
        interp_id, _ = interp_a
        original = dao.create_report(session_a, interp_id)
        _approve(conn, original, session_a.user_id)
        amendment_report_id, _ = dao.create_amendment(session_a, original, "ClinVar reclassified BRCA1")
        block = dao.get_report_revision(session_a, amendment_report_id)["amends"]
        assert block["original_issued_at"] == _at(days=1)
        # The whole ruling: the DATE alone is ambiguous, the BASIS is what
        # stops the banner claiming a release that never happened.
        assert block["original_issued_basis"] == "approved"

    def test_the_earliest_release_is_the_one_reported(self, dao, conn, session_a, released_original):
        # A second consumer asking for a copy on day 5 does not move the day
        # the report was originally issued.
        _release(conn, session_a.org_id, released_original, session_a.user_id, at=_at(days=5))
        amendment_report_id, _ = dao.create_amendment(session_a, released_original, "ClinVar reclassified BRCA1")
        block = dao.get_report_revision(session_a, amendment_report_id)["amends"]
        assert block["original_issued_at"] == _at(days=2)

    def test_amended_by_is_the_three_identity_fields(self, dao, conn, session_a, released_original):
        amendment_report_id, _ = dao.create_amendment(session_a, released_original, "ClinVar reclassified BRCA1")
        block = dao.get_report_revision(session_a, amendment_report_id)["amends"]
        assert block["amended_by"] == "Dr A. Rao (MCI-dr-rao), Apollo Hospitals"


class TestR3DraftAmendmentDoesNotSupersede:
    def test_draft_amendment_leaves_the_original_not_superseded(self, dao, conn, session_a, released_original):
        dao.create_amendment(session_a, released_original, "ClinVar reclassified BRCA1")
        # The amendment report is created in draft state and stays there.
        assert "superseded_by" not in dao.get_report_revision(session_a, released_original)

    def test_approving_the_amendment_supersedes_the_original(self, dao, conn, session_a, released_original):
        amendment_report_id, _ = dao.create_amendment(session_a, released_original, "ClinVar reclassified BRCA1")
        _approve(conn, amendment_report_id, session_a.user_id, at=_at(days=3))
        assert "superseded_by" in dao.get_report_revision(session_a, released_original)

    def test_under_review_amendment_does_not_supersede(self, dao, conn, session_a, released_original):
        amendment_report_id, _ = dao.create_amendment(session_a, released_original, "ClinVar reclassified BRCA1")
        with conn.cursor() as cur:
            cur.execute("UPDATE reports SET state = 'under_review' WHERE id = %s", (amendment_report_id,))
        conn.commit()
        assert "superseded_by" not in dao.get_report_revision(session_a, released_original)


class TestR4AmendedReportDateUsesTheSameBasis:
    def test_released_amendment_reports_its_release_date(self, dao, conn, session_a, released_original):
        amendment_report_id, _ = dao.create_amendment(session_a, released_original, "ClinVar reclassified BRCA1")
        _approve(conn, amendment_report_id, session_a.user_id, at=_at(days=3))
        _release(conn, session_a.org_id, amendment_report_id, session_a.user_id, at=_at(days=4))
        block = dao.get_report_revision(session_a, released_original)["superseded_by"]
        assert block["amended_at"] == _at(days=4)
        assert block["amended_at_basis"] == "released"

    def test_approved_only_amendment_reports_the_approval_date_and_says_so(
        self, dao, conn, session_a, released_original
    ):
        amendment_report_id, _ = dao.create_amendment(session_a, released_original, "ClinVar reclassified BRCA1")
        _approve(conn, amendment_report_id, session_a.user_id, at=_at(days=3))
        block = dao.get_report_revision(session_a, released_original)["superseded_by"]
        assert block["amended_at"] == _at(days=3)
        assert block["amended_at_basis"] == "approved"

    def test_the_two_banners_agree_on_the_basis_for_the_same_report(self, dao, conn, session_a, released_original):
        # The original is RELEASED; the amendment is only APPROVED. Read from
        # both ends, the same report must be described the same way.
        amendment_report_id, _ = dao.create_amendment(session_a, released_original, "ClinVar reclassified BRCA1")
        _approve(conn, amendment_report_id, session_a.user_id, at=_at(days=3))
        from_original = dao.get_report_revision(session_a, released_original)["superseded_by"]
        from_amendment = dao.get_report_revision(session_a, amendment_report_id)["amends"]
        assert from_amendment["original_issued_basis"] == "released"
        assert from_original["amended_at_basis"] == "approved"


class TestR5SeveralAmendments:
    def _chain(self, dao, conn, session, original, n):
        ids = []
        for i in range(n):
            amendment_report_id, _ = dao.create_amendment(session, original, f"reason {i + 1}")
            _approve(conn, amendment_report_id, session.user_id, at=_at(days=3 + i))
            ids.append(amendment_report_id)
        return ids

    def test_one_amendment_carries_its_reason(self, dao, conn, session_a, released_original):
        self._chain(dao, conn, session_a, released_original, 1)
        block = dao.get_report_revision(session_a, released_original)["superseded_by"]
        assert block["reason"] == "reason 1"
        assert "amendment_count" not in block

    def test_three_amendments_name_the_latest_and_give_the_count_not_a_reason(
        self, dao, conn, session_a, released_original
    ):
        ids = self._chain(dao, conn, session_a, released_original, 3)
        block = dao.get_report_revision(session_a, released_original)["superseded_by"]
        assert block["amendment_report_id"] == ids[-1]
        assert block["amendment_count"] == 3
        # The ruling's actual point: the third amendment's reason explains a
        # change from the second, which this reader has never seen.
        assert "reason" not in block

    def test_only_the_approved_ones_count(self, dao, conn, session_a, released_original):
        ids = self._chain(dao, conn, session_a, released_original, 2)
        third, _ = dao.create_amendment(session_a, released_original, "reason 3")  # left in draft
        block = dao.get_report_revision(session_a, released_original)["superseded_by"]
        assert block["amendment_report_id"] == ids[-1]
        assert block["amendment_report_id"] != third
        assert block["amendment_count"] == 2

    def test_an_approved_amendment_behind_a_draft_one_still_reports_its_chain_depth(
        self, dao, conn, session_a, released_original
    ):
        # First amendment left in draft, second approved. The second's reason
        # explains a change from the first -- which nobody can see, draft or
        # not -- so the count form is used, and it is 2, not 1.
        first, _ = dao.create_amendment(session_a, released_original, "reason 1")
        second, _ = dao.create_amendment(session_a, released_original, "reason 2")
        _approve(conn, second, session_a.user_id, at=_at(days=4))
        block = dao.get_report_revision(session_a, released_original)["superseded_by"]
        assert block["amendment_report_id"] == second
        assert block["amendment_count"] == 2
        assert "reason" not in block


class TestR6ReanalysisIsDirectAndSince:
    def test_a_reanalysis_report_names_its_parent_interpretation(self, dao, conn, session_a, interp_a):
        parent_interp, vcf_id = interp_a
        child_interp = dao.create_reanalysis(session_a, vcf_id, parent_interp, {"variants": []})
        report_id = dao.create_report(session_a, child_interp)
        block = dao.get_report_revision(session_a, report_id)["reanalysis_of"]
        assert block["parent_interpretation_id"] == parent_interp
        assert block["parent_interpreted_at"] == T0

    def test_a_later_reanalysis_is_reported_as_since(self, dao, conn, session_a, interp_a, released_original):
        parent_interp, _vcf = interp_a
        _make_interpretation(dao, conn, session_a, parent_interpretation_id=parent_interp, created_at=_at(days=9))
        since = dao.get_report_revision(session_a, released_original)["reanalysed_since"]
        assert len(since) == 1

    def test_a_reanalysis_created_before_this_report_was_issued_is_excluded(
        self, dao, conn, session_a, interp_a, released_original
    ):
        # Released on day 2. A branch created on day 1 is not "since".
        parent_interp, _vcf = interp_a
        _make_interpretation(dao, conn, session_a, parent_interpretation_id=parent_interp, created_at=_at(days=1))
        assert "reanalysed_since" not in dao.get_report_revision(session_a, released_original)

    def test_earlier_and_later_branches_are_separated_not_merged(
        self, dao, conn, session_a, interp_a, released_original
    ):
        # Both exist; only the later one may be counted. A reader that simply
        # returned all children would pass the "later" test above on its own.
        parent_interp, _vcf = interp_a
        _make_interpretation(dao, conn, session_a, parent_interpretation_id=parent_interp, created_at=_at(days=1))
        later = _make_interpretation(
            dao, conn, session_a, parent_interpretation_id=parent_interp, created_at=_at(days=9)
        )[0]
        since = dao.get_report_revision(session_a, released_original)["reanalysed_since"]
        assert [row["interpretation_id"] for row in since] == [later]

    def test_a_grandchild_reanalysis_is_not_reported_as_since(self, dao, conn, session_a, interp_a, released_original):
        # DIRECT only: a re-analysis of a re-analysis branched from a document
        # this report's holder never had.
        parent_interp, _vcf = interp_a
        child = _make_interpretation(
            dao, conn, session_a, parent_interpretation_id=parent_interp, created_at=_at(days=9)
        )[0]
        _make_interpretation(dao, conn, session_a, parent_interpretation_id=child, created_at=_at(days=10))
        since = dao.get_report_revision(session_a, released_original)["reanalysed_since"]
        assert [row["interpretation_id"] for row in since] == [child]


def _orphan_reanalysis_report(dao, conn, session, vcf_id):
    """
    A report whose interpretation names a parent that does not exist.

    Built by INSERT with fk_interp_parent dropped, NOT by UPDATE: the
    interpretations immutability trigger refuses any rewrite of
    parent_interpretation_id, so an UPDATE here would fail on the trigger and
    the test would never reach the reader it is meant to exercise.
    """
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE interpretations DROP CONSTRAINT IF EXISTS fk_interp_parent")
    conn.commit()
    interp_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO interpretations "
            "(org_id, id, vcf_id, run_document, submission_key, parent_interpretation_id, created_at, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                session.org_id,
                interp_id,
                vcf_id,
                json.dumps({"variants": []}),
                "sub-" + str(interp_id),
                uuid.uuid4(),  # a parent that was never written
                T0,
                session.user_id,
            ),
        )
    conn.commit()
    return dao.create_report(session, interp_id)


class TestR7FailClosed:
    def test_amendment_naming_a_missing_original_raises(self, dao, conn, session_a, released_original):
        amendment_report_id, _ = dao.create_amendment(session_a, released_original, "ClinVar reclassified BRCA1")
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE amendments DROP CONSTRAINT IF EXISTS fk_amendment_original")
            cur.execute(
                "UPDATE amendments SET original_report_id = %s WHERE amendment_report_id = %s",
                (uuid.uuid4(), amendment_report_id),
            )
        conn.commit()
        with pytest.raises(BrokenRevisionRecordError):
            dao.get_report_revision(session_a, amendment_report_id)

    def test_interpretation_naming_a_missing_parent_raises(self, dao, conn, session_a, interp_a):
        _interp_id, vcf_id = interp_a
        report_id = _orphan_reanalysis_report(dao, conn, session_a, vcf_id)
        with pytest.raises(BrokenRevisionRecordError):
            dao.get_report_revision(session_a, report_id)

    def test_a_broken_record_does_not_return_a_clean_block(self, dao, conn, session_a, interp_a):
        # No marker text, no silent omission: the specific failure is a report
        # that is a re-analysis of something unreadable rendering as an
        # original. Nothing is returned at all.
        _interp_id, vcf_id = interp_a
        report_id = _orphan_reanalysis_report(dao, conn, session_a, vcf_id)
        try:
            result = dao.get_report_revision(session_a, report_id)
        except BrokenRevisionRecordError:
            return
        pytest.fail(f"returned a renderable block instead of failing closed: {result}")

    def test_an_amendment_with_a_blanked_reason_raises(self, dao, conn, session_a, released_original):
        amendment_report_id, _ = dao.create_amendment(session_a, released_original, "ClinVar reclassified BRCA1")
        _approve(conn, amendment_report_id, session_a.user_id, at=_at(days=3))
        with conn.cursor() as cur:
            cur.execute("UPDATE amendments SET reason = '   ' WHERE amendment_report_id = %s", (amendment_report_id,))
        conn.commit()
        with pytest.raises(BrokenRevisionRecordError):
            dao.get_report_revision(session_a, released_original)


class TestR8SupersededAfterRetentionDeletedTheSuccessor:
    def test_still_superseded_but_flagged_not_retained(self, dao, conn, session_a, released_original):
        amendment_report_id, _ = dao.create_amendment(session_a, released_original, "ClinVar reclassified BRCA1")
        _approve(conn, amendment_report_id, session_a.user_id, at=_at(days=3))
        _tombstone_report(conn, amendment_report_id, session_a.user_id)
        block = dao.get_report_revision(session_a, released_original)["superseded_by"]
        assert block["retained"] is False
        assert block["amendment_report_id"] == amendment_report_id

    def test_a_live_successor_is_retained(self, dao, conn, session_a, released_original):
        amendment_report_id, _ = dao.create_amendment(session_a, released_original, "ClinVar reclassified BRCA1")
        _approve(conn, amendment_report_id, session_a.user_id, at=_at(days=3))
        block = dao.get_report_revision(session_a, released_original)["superseded_by"]
        assert block["retained"] is True


class TestBannersActuallyRender:
    """
    The point of the whole commit: these facts reach the signed-off wording.
    Imported here rather than in geper's suite because only this suite has the
    database the facts come from.
    """

    def test_a_superseded_original_renders_the_superseded_banner(self, dao, conn, session_a, released_original):
        # sys.path, not importorskip: conftest turns every skip in this package
        # into a failure under CLINICAL_REQUIRE_DB, and rightly -- a banner
        # test that quietly declines to run is the coverage hole this whole
        # card is about.
        import sys
        from pathlib import Path

        geper_dir = str(Path(__file__).resolve().parents[2] / "geper")
        if geper_dir not in sys.path:
            sys.path.insert(0, geper_dir)
        from report.clinical_report_builder import report_revision_banners

        amendment_report_id, _ = dao.create_amendment(session_a, released_original, "ClinVar reclassified BRCA1")
        _approve(conn, amendment_report_id, session_a.user_id, at=_at(days=3))
        revision = dao.get_report_revision(session_a, released_original)
        banners = report_revision_banners({"report_revision": revision})
        assert len(banners) == 1
        assert "THIS REPORT HAS BEEN SUPERSEDED" in banners[0]
        assert "An amended report was approved on" in banners[0]
        assert "ClinVar reclassified BRCA1" in banners[0]
