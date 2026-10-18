"""
tests/test_approval_hash_timezone.py
────────────────────────────────────

The approval content hash must not depend on the PostgreSQL SESSION time zone.

THE DEFECT. `_compute_report_content_hash` serialised each reviewer claim's timestamp with `datetime.isoformat()`.
psycopg returns a `timestamptz` as an aware datetime in the zone of the CONNECTION, and nothing in the clinical service
pins that zone, so the same stored instant printed as `...+00:00`, `...+05:30`, `...+01:00` or `...-07:00` depending on
which connection did the computing. A hash stored by an approval was then reproduced only by a session in the same zone.
(Observed on a throwaway cluster, run tag r1a2: 13 of 13 approvals recomputed under UTC, 0 of 13 under each of three
other zones. docs/SIGNOFF_RECOVERY_DESIGN.md, section 13, fact 4.)

THE FIX under test: each claim timestamp is converted to UTC before it enters the hash. For a session that was already
UTC the output is byte-identical to the previous algorithm, so every hash computed under UTC stays verifiable.

WHAT THESE TESTS PROVE
  * the golden vector: a fixed dataset hashes to a literal value produced by the PREVIOUS algorithm, under every zone;
  * computation and verification agree across all pairs of {UTC, Asia/Kolkata, Europe/London, America/Los_Angeles},
    including a report APPROVED in one zone and VERIFIED in another;
  * tampering is still detected in every zone (normalisation does not mask it);
  * a LEGACY hash (computed by the previous algorithm in a non-UTC session) is not reported as tampering: it is
    recognised as a distinguishable outcome, and a tampered legacy report is still `no_match`.

Tests that need PostgreSQL use CLINICAL_TEST_DSN like their neighbours; the pure tests need nothing.
"""

from __future__ import annotations

import datetime
from datetime import timezone
import hashlib
import json
import os
import uuid
import zoneinfo

import pytest

from clinical.data_access import BcryptHasher, DataAccess, SystemClock
from clinical.tests.test_phase_7_commit1_hash_locking import (
    _admin_session,
    _approved_report,
    _expected_hash,
    _make_interpretation,
    _role_session,
)

DSN = os.getenv("CLINICAL_TEST_DSN")
SCHEMA_PATH = __import__("pathlib").Path(__file__).parent.parent / "schema.sql"
_CREATE_ROLE = "DO $$ BEGIN CREATE ROLE clinical_app; EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
_CREATE_ROLE_RETENTION = (
    "DO $$ BEGIN CREATE ROLE clinical_retention; EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
)

ZONES = ["UTC", "Asia/Kolkata", "Europe/London", "America/Los_Angeles"]

# ── the golden dataset (fixed values only) ────────────────────────────────────────────────────────────────────────
GOLDEN_ACTOR = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
GOLDEN_RUN_DOCUMENT = {
    "variants": [{"variant": {"chrom": "17", "pos": 43106534, "ref": "C", "alt": "A"}, "note": "café", "score": 0.1}],
    "run_complete": True,
}
# (claim_type, variant_key, classification, reason, timestamp). The second timestamp has zero microseconds on purpose:
# isoformat() omits the fraction then, and the fix must keep omitting it.
GOLDEN_CLAIMS = [
    (
        "sole_signatory",
        None,
        None,
        "Golden vector concurrence",
        datetime.datetime(2026, 9, 3, 12, 0, 0, 123456, tzinfo=timezone.utc),
    ),
    (
        "disagree",
        "17-43106534-C-A",
        "Likely pathogenic",
        "Golden second claim",
        datetime.datetime(2026, 9, 3, 12, 0, 5, tzinfo=timezone.utc),
    ),
]
# Produced by an independent implementation of the PREVIOUS algorithm (isoformat of a UTC-session timestamp) over the
# dataset above. It must never be regenerated from the code under test.
GOLDEN_HASH = "4603f82a6a6db2ce5b514d295e58ce41ff5e0c8ff5b23072f248513dcb10a31b"


def _previous_algorithm_hash(run_document, claim_tuples, render_timestamp):
    """The PREVIOUS algorithm, reimplemented here as an independent reference. `render_timestamp` is what the previous
    code did implicitly: print the timestamp in whatever zone the connection used."""
    claims = [
        {
            "claim_type": ctype,
            "variant_key": vkey,
            "classification": cls,
            "reason": reason,
            "actor_id": str(GOLDEN_ACTOR),
            "timestamp": render_timestamp(ts),
            "evidence_json": None,
            "supersedes": None,
        }
        for (ctype, vkey, cls, reason, ts) in claim_tuples
    ]
    payload = {"run_document": run_document, "reviewer_claims": claims}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


# ── pure tests (no database) ─────────────────────────────────────────────────────────────────────────────────────


class TestGoldenVectorPure:
    def test_reference_reproduces_the_golden_literal_under_utc(self):
        """The literal is the previous algorithm's output for a UTC session. This is the byte-for-byte anchor."""
        assert _previous_algorithm_hash(GOLDEN_RUN_DOCUMENT, GOLDEN_CLAIMS, lambda ts: ts.isoformat()) == GOLDEN_HASH

    @pytest.mark.parametrize("zone", ["Asia/Kolkata", "Europe/London", "America/Los_Angeles"])
    def test_the_previous_algorithm_really_depends_on_the_zone(self, zone):
        """Documents the defect: the same instants hashed in another zone give another value."""
        zi = zoneinfo.ZoneInfo(zone)
        other = _previous_algorithm_hash(GOLDEN_RUN_DOCUMENT, GOLDEN_CLAIMS, lambda ts: ts.astimezone(zi).isoformat())
        assert other != GOLDEN_HASH

    @pytest.mark.parametrize("zone", ZONES)
    def test_utc_normalisation_gives_the_golden_value_whatever_zone_the_timestamp_arrived_in(self, zone):
        zi = zoneinfo.ZoneInfo(zone)
        normalised = _previous_algorithm_hash(
            GOLDEN_RUN_DOCUMENT, GOLDEN_CLAIMS, lambda ts: ts.astimezone(zi).astimezone(timezone.utc).isoformat()
        )
        assert normalised == GOLDEN_HASH


# ── database fixtures ────────────────────────────────────────────────────────────────────────────────────────────


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
def session_a(dao, org_a, conn):
    return _admin_session(dao, conn, org_a, "admin@org-a.test")


@pytest.fixture
def interpreter_a(dao, session_a, org_a):
    return _role_session(dao, session_a, org_a, "interpreter@org-a.test", "Interpreter")


@pytest.fixture
def approver_a(dao, session_a, org_a):
    return _role_session(dao, session_a, org_a, "approver@org-a.test", "Approver")


@pytest.fixture
def zone_connections():
    """One connection per zone, each with that PostgreSQL SESSION TimeZone. Closed at teardown."""
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    if not DSN:
        pytest.skip("CLINICAL_TEST_DSN not set")
    opened = {}
    for zone in ZONES:
        c = psycopg.connect(DSN, autocommit=False, connect_timeout=10, options=f"-c TimeZone={zone}")
        with c.cursor() as cur:
            cur.execute("SHOW TimeZone")
            assert cur.fetchone()[0] == zone, "the session did not take the requested time zone"
        c.rollback()
        opened[zone] = c
    yield opened
    for c in opened.values():
        c.close()


def _dao_on(connection):
    return DataAccess(connection, clock=SystemClock(), password_hasher=BcryptHasher(rounds=4))


def _stored_hash(conn, report_id):
    with conn.cursor() as cur:
        cur.execute("SELECT content_hash FROM reports WHERE id = %s", (report_id,))
        value = cur.fetchone()[0]
    conn.rollback()
    return value


# ── golden vector through the real database path ─────────────────────────────────────────────────────────────────


def _build_golden_report(dao, conn, session_a, org_a):
    """A report whose hashed content is FIXED: fixed run_document, fixed claims, fixed actor id, fixed timestamps.
    Rows are written beneath the recorders (superuser connection) so that every hashed field is deterministic."""
    interp = _make_interpretation(dao, conn, session_a, run_document=GOLDEN_RUN_DOCUMENT)
    report_id = dao.create_report(session_a, interp)
    now = datetime.datetime(2026, 9, 1, tzinfo=timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (user_id, org_id, email, password_hash, password_changed_at, is_system_account, "
            "disabled, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, false, false, %s, %s)",
            (GOLDEN_ACTOR, org_a, "golden-actor@org-a.test", "x", now, now, now),
        )
        for ctype, vkey, cls, reason, ts in GOLDEN_CLAIMS:
            cur.execute(
                "INSERT INTO reviewer_claims (org_id, id, interpretation_id, report_id, variant_key, claim_type, "
                'classification, actor_id, "timestamp", reason) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
                (org_a, uuid.uuid4(), interp, report_id, vkey, ctype, cls, GOLDEN_ACTOR, ts, reason),
            )
    conn.commit()
    return interp, report_id


class TestGoldenVectorThroughTheDatabase:
    @pytest.mark.parametrize("zone", ZONES)
    def test_hash_of_the_golden_dataset_is_the_golden_value_in_every_session_zone(
        self, dao, conn, session_a, org_a, zone_connections, zone
    ):
        _, report_id = _build_golden_report(dao, conn, session_a, org_a)
        zone_dao = _dao_on(zone_connections[zone])
        assert zone_dao._compute_report_content_hash(session_a, report_id) == GOLDEN_HASH

    @pytest.mark.parametrize(
        "zone, expected_offset_hours",
        [("UTC", 0.0), ("Asia/Kolkata", 5.5), ("Europe/London", 1.0), ("America/Los_Angeles", -7.0)],
    )
    def test_the_database_really_hands_back_the_claim_time_in_the_session_zone(
        self, dao, conn, session_a, org_a, zone_connections, zone, expected_offset_hours
    ):
        """Guards the test itself: if psycopg returned UTC whatever the session said, the zone tests would prove nothing."""
        _, report_id = _build_golden_report(dao, conn, session_a, org_a)
        c = zone_connections[zone]
        with c.cursor() as cur:
            cur.execute(
                'SELECT "timestamp" FROM reviewer_claims WHERE report_id = %s ORDER BY "timestamp" LIMIT 1',
                (report_id,),
            )
            ts = cur.fetchone()[0]
        c.rollback()
        assert ts.utcoffset().total_seconds() / 3600 == expected_offset_hours
        assert ts == GOLDEN_CLAIMS[0][4]  # same instant
        if zone != "UTC":
            assert ts.isoformat() != GOLDEN_CLAIMS[0][4].isoformat()  # but printed differently: the old defect


# ── computation and verification across zones ─────────────────────────────────────────────────────────────────────


class TestComputationAndVerificationAcrossZones:
    @pytest.mark.parametrize("approve_zone", ZONES)
    def test_approved_in_one_zone_verifies_and_recomputes_identically_in_every_zone(
        self, dao, conn, session_a, interpreter_a, approver_a, zone_connections, approve_zone
    ):
        approving_conn = zone_connections[approve_zone]
        approving_dao = _dao_on(approving_conn)
        interp = _make_interpretation(approving_dao, approving_conn, session_a)
        report_id = _approved_report(approving_dao, approving_conn, session_a, interpreter_a, approver_a, interp)
        stored = _stored_hash(conn, report_id)

        for verify_zone in ZONES:
            verifying_conn = zone_connections[verify_zone]
            verifying_dao = _dao_on(verifying_conn)
            assert verifying_dao._compute_report_content_hash(approver_a, report_id) == stored, (
                approve_zone,
                verify_zone,
            )
            assert verifying_dao.verify_report_integrity(approver_a, report_id) is True, (approve_zone, verify_zone)
            assert _expected_hash(verifying_conn, interp, report_id) == stored, (approve_zone, verify_zone)

    @pytest.mark.parametrize("verify_zone", ZONES)
    def test_tampering_is_still_detected_in_every_zone(
        self, dao, conn, session_a, interpreter_a, approver_a, interp_a_factory, zone_connections, verify_zone
    ):
        interp = interp_a_factory()
        report_id = _approved_report(dao, conn, session_a, interpreter_a, approver_a, interp)
        verifying_dao = _dao_on(zone_connections[verify_zone])
        assert verifying_dao.verify_report_integrity(approver_a, report_id) is True
        with conn.cursor() as cur:
            cur.execute("UPDATE reviewer_claims SET reason = %s WHERE interpretation_id = %s", ("TAMPERED", interp))
        conn.commit()
        assert verifying_dao.verify_report_integrity(approver_a, report_id) is False
        assert verifying_dao._classify_report_integrity(approver_a, report_id) == ("no_match", None)

    def test_an_unapproved_report_still_has_no_hash_to_verify(self, dao, conn, session_a, org_a, zone_connections):
        interp = _make_interpretation(dao, conn, session_a)
        report_id = dao.create_report(session_a, interp)
        for zone in ZONES:
            with pytest.raises(ValueError):
                _dao_on(zone_connections[zone]).verify_report_integrity(session_a, report_id)


@pytest.fixture
def interp_a_factory(dao, conn, session_a):
    return lambda: _make_interpretation(dao, conn, session_a)


# ── legacy hashes: computed by the PREVIOUS algorithm in a non-UTC session ─────────────────────────────────────────


def _previous_algorithm_from_the_database(connection, report_id):
    """The PREVIOUS `_compute_report_content_hash`, verbatim in effect: raw `isoformat()` of whatever the connection's zone
    gave. Used only to manufacture a legacy approval."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT i.run_document FROM interpretations i JOIN reports r ON r.org_id = i.org_id AND r.interpretation_id = i.id "
            "WHERE r.id = %s",
            (report_id,),
        )
        run_document = cur.fetchone()[0]
        if isinstance(run_document, str):
            run_document = json.loads(run_document)
        cur.execute(
            'SELECT claim_type, variant_key, classification, reason, actor_id, "timestamp", evidence_json, supersedes '
            'FROM reviewer_claims WHERE report_id = %s ORDER BY "timestamp", id',
            (report_id,),
        )
        claims = [
            {
                "claim_type": r[0],
                "variant_key": r[1],
                "classification": r[2],
                "reason": r[3],
                "actor_id": str(r[4]),
                "timestamp": r[5].isoformat(),
                "evidence_json": r[6],
                "supersedes": str(r[7]) if r[7] is not None else None,
            }
            for r in cur.fetchall()
        ]
    connection.rollback()
    payload = {"run_document": run_document, "reviewer_claims": claims}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _legacy_dao_on(connection):
    class LegacyDataAccess(DataAccess):
        def _compute_report_content_hash(self, session, report_id):
            return _previous_algorithm_from_the_database(connection, report_id)

    return LegacyDataAccess(connection, clock=SystemClock(), password_hasher=BcryptHasher(rounds=4))


class TestLegacyHashesAreNotReportedAsTampering:
    @pytest.mark.parametrize("legacy_zone", ["Asia/Kolkata", "Europe/London", "America/Los_Angeles"])
    def test_a_legacy_hash_from_a_non_utc_session_is_recognised_and_distinguishable(
        self, dao, conn, session_a, interpreter_a, approver_a, zone_connections, legacy_zone
    ):
        legacy_conn = zone_connections[legacy_zone]
        legacy_dao = _legacy_dao_on(legacy_conn)
        interp = _make_interpretation(legacy_dao, legacy_conn, session_a)
        report_id = _approved_report(legacy_dao, legacy_conn, session_a, interpreter_a, approver_a, interp)
        stored = _stored_hash(conn, report_id)

        # It is NOT the canonical (UTC) form, so the canonical recomputation differs...
        assert dao._compute_report_content_hash(approver_a, report_id) != stored
        # ...but it is recognised as the previous algorithm run in a named zone, never as tampering.
        outcome, zone = dao._classify_report_integrity(approver_a, report_id)
        assert outcome == "match_legacy_zone"
        assert zone in zoneinfo.available_timezones()
        assert dao.verify_report_integrity(approver_a, report_id) is True

    def test_a_tampered_legacy_report_is_no_match_and_is_not_labelled_tampered(
        self, dao, conn, session_a, interpreter_a, approver_a, zone_connections
    ):
        legacy_conn = zone_connections["Asia/Kolkata"]
        legacy_dao = _legacy_dao_on(legacy_conn)
        interp = _make_interpretation(legacy_dao, legacy_conn, session_a)
        report_id = _approved_report(legacy_dao, legacy_conn, session_a, interpreter_a, approver_a, interp)
        with conn.cursor() as cur:
            cur.execute("UPDATE reviewer_claims SET reason = %s WHERE interpretation_id = %s", ("TAMPERED", interp))
        conn.commit()
        assert dao._classify_report_integrity(approver_a, report_id) == ("no_match", None)
        assert dao.verify_report_integrity(approver_a, report_id) is False

    def test_a_canonical_hash_classifies_as_match_without_any_zone_search(
        self, dao, conn, session_a, interpreter_a, approver_a, interp_a_factory
    ):
        report_id = _approved_report(dao, conn, session_a, interpreter_a, approver_a, interp_a_factory())
        assert dao._classify_report_integrity(approver_a, report_id) == ("match", None)

    def test_stored_hashes_and_approvals_are_never_rewritten_by_verification(
        self, dao, conn, session_a, interpreter_a, approver_a, zone_connections
    ):
        legacy_conn = zone_connections["Europe/London"]
        legacy_dao = _legacy_dao_on(legacy_conn)
        interp = _make_interpretation(legacy_dao, legacy_conn, session_a)
        report_id = _approved_report(legacy_dao, legacy_conn, session_a, interpreter_a, approver_a, interp)
        with conn.cursor() as cur:
            cur.execute("SELECT state, approver_id, approved_at, content_hash FROM reports WHERE id = %s", (report_id,))
            before = cur.fetchone()
        conn.rollback()
        dao.verify_report_integrity(approver_a, report_id)
        dao._classify_report_integrity(approver_a, report_id)
        with conn.cursor() as cur:
            cur.execute("SELECT state, approver_id, approved_at, content_hash FROM reports WHERE id = %s", (report_id,))
            after = cur.fetchone()
        conn.rollback()
        assert after == before
