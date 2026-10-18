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

from clinical.data_access import BcryptHasher, DataAccess, SystemClock, _claim_timestamp_utc, _INTEGRITY_AUDIT
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


def _assert_zone_matches_the_sessions_offsets(connection, report_id, zone_name):
    """For every claim of the report, the offset the original SESSION printed equals the offset `zone_name` gives."""
    with connection.cursor() as cur:
        cur.execute(
            'SELECT "timestamp" FROM reviewer_claims WHERE report_id = %s ORDER BY "timestamp", id', (report_id,)
        )
        instants = [row[0] for row in cur.fetchall()]
    connection.rollback()
    assert instants, "the report has no claims to compare"
    zone = zoneinfo.ZoneInfo(zone_name)
    for ts in instants:
        assert ts.astimezone(zone).utcoffset() == ts.utcoffset(), (zone_name, ts.isoformat())


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
        # The returned zone must be one that renders the claim instants the way the ORIGINAL session did. The expected
        # offsets come from PostgreSQL/psycopg's rendering in that session, not from the code under test, and not from
        # today's date: they are read back from the stored claims.
        _assert_zone_matches_the_sessions_offsets(legacy_conn, report_id, zone)
        if legacy_zone == "Asia/Kolkata":
            probe = datetime.datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc).astimezone(zoneinfo.ZoneInfo(zone))
            assert probe.utcoffset() == datetime.timedelta(hours=5, minutes=30)
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


# ══ edge cases and audit visibility (follow-up to the first commit) ═══════════════════════════════════════════════


@pytest.fixture
def require_tzdata():
    """The DST tests need the IANA rules for these zones on the host. Fail loudly (not skip) when they are absent."""
    for name in ("Europe/London", "America/Los_Angeles", "Asia/Kolkata"):
        try:
            zoneinfo.ZoneInfo(name)
        except zoneinfo.ZoneInfoNotFoundError:  # pragma: no cover - environment problem
            pytest.fail(f"tzdata for {name} is not available on this host; install the 'tzdata' package")


def _utc(*args, **kwargs):
    return datetime.datetime(*args, tzinfo=timezone.utc, **kwargs)


def _pure_rows(instants):
    """Claim rows in the shape `_load_report_hash_inputs` returns, for the DB-free tests."""
    return [("accept", None, None, f"claim {i}", GOLDEN_ACTOR, ts, None, None) for i, ts in enumerate(instants)]


def _expected_hash_from_literal_strings(instants, literal_strings):
    """The PREVIOUS algorithm's hash for these instants, given the exact `isoformat()` text the original session printed
    for each. The text is written out by hand below (not computed by zoneinfo), so it is independent of the code under test."""
    by_instant = dict(zip(instants, literal_strings))
    claims = [("accept", None, None, f"claim {i}", ts) for i, ts in enumerate(instants)]
    return _previous_algorithm_hash(GOLDEN_RUN_DOCUMENT, claims, lambda ts: by_instant[ts])


def _bare_dao():
    return DataAccess.__new__(DataAccess)  # no connection: the pure helpers do not use one


# (zone the ORIGINAL session used, instants, literal text it printed for each, expected offset in hours for each)
DST_CASES = {
    "london_spring_forward_at_the_gap": (
        "Europe/London",
        [_utc(2026, 3, 29, 0, 59, 59), _utc(2026, 3, 29, 1, 0, 0), _utc(2026, 3, 29, 12, 0, 0, 999999)],
        ["2026-03-29T00:59:59+00:00", "2026-03-29T02:00:00+01:00", "2026-03-29T13:00:00.999999+01:00"],
        [0, 1, 1],
    ),
    "london_autumn_overlap_ambiguous_local_time": (
        "Europe/London",
        [_utc(2026, 10, 25, 0, 30, 0), _utc(2026, 10, 25, 1, 30, 0)],
        ["2026-10-25T01:30:00+01:00", "2026-10-25T01:30:00+00:00"],
        [1, 0],
    ),
    "los_angeles_spring_forward": (
        "America/Los_Angeles",
        [_utc(2026, 3, 8, 9, 59, 59), _utc(2026, 3, 8, 10, 0, 0)],
        ["2026-03-08T01:59:59-08:00", "2026-03-08T03:00:00-07:00"],
        [-8, -7],
    ),
}


class TestLegacySearchAcrossDaylightSavingPure:
    """DB-free. Claims whose instants straddle a DST transition: the original session printed DIFFERENT offsets for them."""

    @pytest.mark.parametrize("case", sorted(DST_CASES))
    def test_a_hash_made_across_a_transition_is_recognised_with_the_right_offsets(self, require_tzdata, case):
        _, instants, literals, offsets = DST_CASES[case]
        expected_stored_hash = _expected_hash_from_literal_strings(instants, literals)
        rows = _pure_rows(instants)

        # the canonical (UTC) form must NOT reproduce it, otherwise the search would not be exercised
        assert DataAccess._report_hash_digest(GOLDEN_RUN_DOCUMENT, rows, _claim_timestamp_utc) != expected_stored_hash

        zone = _bare_dao()._legacy_zone_reproducing(GOLDEN_RUN_DOCUMENT, rows, expected_stored_hash)
        assert zone is not None, "the search must recognise a hash whose offsets change within the claim set"
        found = [ts.astimezone(zoneinfo.ZoneInfo(zone)).utcoffset() for ts in instants]
        assert found == [datetime.timedelta(hours=h) for h in offsets]

    def test_a_zone_with_a_constant_offset_cannot_reproduce_a_hash_that_straddles_a_transition(self, require_tzdata):
        """Guards against a search that matches too loosely: Asia/Kolkata never prints +00:00 then +01:00."""
        _, instants, literals, _ = DST_CASES["london_spring_forward_at_the_gap"]
        zone = _bare_dao()._legacy_zone_reproducing(
            GOLDEN_RUN_DOCUMENT, _pure_rows(instants), _expected_hash_from_literal_strings(instants, literals)
        )
        assert zone != "Asia/Kolkata"

    def test_a_hash_that_no_zone_reproduces_is_not_recognised(self, require_tzdata):
        _, instants, literals, _ = DST_CASES["london_spring_forward_at_the_gap"]
        altered = list(literals)
        altered[0] = "2026-03-29T00:59:59+05:30"  # a text no single zone prints for these instants
        assert (
            _bare_dao()._legacy_zone_reproducing(
                GOLDEN_RUN_DOCUMENT, _pure_rows(instants), _expected_hash_from_literal_strings(instants, altered)
            )
            is None
        )


class TestNaiveTimestampsAreRefused:
    def test_a_naive_datetime_is_rejected_not_read_as_local_time(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            _claim_timestamp_utc(datetime.datetime(2026, 9, 3, 12, 0, 0))

    def test_aware_values_are_converted_to_utc_text(self, require_tzdata):
        assert _claim_timestamp_utc(_utc(2026, 9, 3, 12, 0, 0)) == "2026-09-03T12:00:00+00:00"
        kolkata = datetime.datetime(2026, 9, 3, 17, 30, 0, tzinfo=zoneinfo.ZoneInfo("Asia/Kolkata"))
        assert _claim_timestamp_utc(kolkata) == "2026-09-03T12:00:00+00:00"
        assert _claim_timestamp_utc(_utc(2026, 9, 3, 12, 0, 0, 123456)) == "2026-09-03T12:00:00.123456+00:00"

    def test_the_hash_path_refuses_a_naive_claim_timestamp(self):
        rows = [("accept", None, None, "r", GOLDEN_ACTOR, datetime.datetime(2026, 9, 3, 12, 0, 0), None, None)]
        with pytest.raises(ValueError):
            DataAccess._report_hash_digest(GOLDEN_RUN_DOCUMENT, rows, _claim_timestamp_utc)


class TestLegacySearchFailsClosedWithoutTimeZoneData:
    """If the host cannot enumerate or build zones, legacy recognition is simply unavailable: the answer is None
    (which the classifier reports as `no_match`), never a guess and never an exception."""

    def _fixture(self):
        _, instants, literals, _ = DST_CASES["london_spring_forward_at_the_gap"]
        return _pure_rows(instants), _expected_hash_from_literal_strings(instants, literals)

    def test_enumeration_raising(self, require_tzdata, monkeypatch):
        rows, stored = self._fixture()
        monkeypatch.setattr(
            zoneinfo, "available_timezones", lambda: (_ for _ in ()).throw(RuntimeError("no tz database"))
        )
        assert _bare_dao()._legacy_zone_reproducing(GOLDEN_RUN_DOCUMENT, rows, stored) is None

    def test_no_zones_available(self, require_tzdata, monkeypatch):
        rows, stored = self._fixture()
        monkeypatch.setattr(zoneinfo, "available_timezones", lambda: set())
        assert _bare_dao()._legacy_zone_reproducing(GOLDEN_RUN_DOCUMENT, rows, stored) is None

    def test_every_zone_failing_to_load(self, require_tzdata, monkeypatch):
        rows, stored = self._fixture()

        def missing(key):
            raise zoneinfo.ZoneInfoNotFoundError(key)

        monkeypatch.setattr(zoneinfo, "ZoneInfo", missing)
        assert _bare_dao()._legacy_zone_reproducing(GOLDEN_RUN_DOCUMENT, rows, stored) is None

    def test_the_zoneinfo_module_being_unimportable(self, require_tzdata, monkeypatch):
        import sys

        rows, stored = self._fixture()
        monkeypatch.setitem(sys.modules, "zoneinfo", None)  # `import zoneinfo` now raises ImportError
        assert _bare_dao()._legacy_zone_reproducing(GOLDEN_RUN_DOCUMENT, rows, stored) is None


# ── database-level: DST straddle, an unresolvable session zone, no tzdata, and the audit trail ─────────────────────


def _report_with_claims_at(dao, conn, session_a, interpreter_a, instants):
    """A report in review whose claims carry FIXED instants (written beneath the recorders)."""
    interp = _make_interpretation(dao, conn, session_a)
    report_id = dao.create_report(session_a, interp)
    with conn.cursor() as cur:
        for i, ts in enumerate(instants):
            cur.execute(
                "INSERT INTO reviewer_claims (org_id, id, interpretation_id, report_id, claim_type, actor_id, "
                '"timestamp", reason) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                (session_a.org_id, uuid.uuid4(), interp, report_id, "accept", interpreter_a.user_id, ts, f"claim {i}"),
            )
    conn.commit()
    dao.submit_for_review(interpreter_a, report_id)
    return interp, report_id


def _audit_rows(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT outcome, resource_id, details FROM audit_log WHERE action = 'report_integrity_verified' ORDER BY log_id"
        )
        rows = cur.fetchall()
    conn.rollback()
    return rows


class TestLegacyHashAcrossDaylightSavingThroughTheDatabase:
    @pytest.mark.parametrize("case", ["london_spring_forward_at_the_gap", "los_angeles_spring_forward"])
    def test_recognised_with_the_offsets_the_original_session_printed(
        self, dao, conn, session_a, interpreter_a, approver_a, zone_connections, require_tzdata, case
    ):
        zone_name, instants, literals, offsets = DST_CASES[case]
        legacy_conn = zone_connections[zone_name]
        interp, report_id = _report_with_claims_at(dao, conn, session_a, interpreter_a, instants)

        # guard: the DATABASE really prints the hand-written text in that session (the premise of the test)
        with legacy_conn.cursor() as cur:
            cur.execute(
                'SELECT "timestamp" FROM reviewer_claims WHERE report_id = %s ORDER BY "timestamp"', (report_id,)
            )
            printed = [row[0].isoformat() for row in cur.fetchall()]
        legacy_conn.rollback()
        assert printed == literals

        _legacy_dao_on(legacy_conn)._approve_report(approver_a, report_id, actor_id=approver_a.user_id)
        outcome, zone = dao._classify_report_integrity(approver_a, report_id)
        assert outcome == "match_legacy_zone"
        found = [ts.astimezone(zoneinfo.ZoneInfo(zone)).utcoffset() for ts in instants]
        assert found == [datetime.timedelta(hours=h) for h in offsets]
        assert dao.verify_report_integrity(approver_a, report_id) is True


class TestUnresolvableSessionTimeZone:
    UNRESOLVABLE = "ABC-5:30"  # a POSIX time zone: PostgreSQL accepts it, Python's zoneinfo has no such key

    def test_hashes_made_in_such_a_session_are_the_canonical_form_because_psycopg_falls_back_to_utc(
        self, dao, conn, session_a, interpreter_a, approver_a
    ):
        psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
        odd = psycopg.connect(DSN, autocommit=False, connect_timeout=10, options=f"-c TimeZone={self.UNRESOLVABLE}")
        try:
            with odd.cursor() as cur:
                cur.execute("SHOW TimeZone")
                assert cur.fetchone()[0] == self.UNRESOLVABLE
                cur.execute("SELECT '2026-09-03 12:00:00+00'::timestamptz::text")
                assert cur.fetchone()[0].endswith("+05:30")  # PostgreSQL itself prints this zone's offset...
            odd.rollback()
            with pytest.raises((KeyError, ValueError, OSError)):
                zoneinfo.ZoneInfo(self.UNRESOLVABLE)  # ...which Python cannot resolve

            odd_dao = _legacy_dao_on(odd)  # the PREVIOUS algorithm on that session
            interp = _make_interpretation(odd_dao, odd, session_a)
            report_id = _approved_report(odd_dao, odd, session_a, interpreter_a, approver_a, interp)
            # psycopg could not resolve the zone, so it handed back UTC datetimes and the old algorithm printed +00:00:
            # that hash IS the canonical form and verifies without any zone search.
            assert dao._classify_report_integrity(approver_a, report_id) == ("match", None)
        finally:
            odd.close()


class TestNoTimeZoneDataFailsClosedThroughTheDatabase:
    def test_a_legacy_hash_becomes_no_match_not_tampering_and_not_an_error(
        self, dao, conn, session_a, interpreter_a, approver_a, zone_connections, monkeypatch
    ):
        legacy_conn = zone_connections["Asia/Kolkata"]
        legacy_dao = _legacy_dao_on(legacy_conn)
        interp = _make_interpretation(legacy_dao, legacy_conn, session_a)
        report_id = _approved_report(legacy_dao, legacy_conn, session_a, interpreter_a, approver_a, interp)
        monkeypatch.setattr(zoneinfo, "available_timezones", lambda: set())
        assert dao._classify_report_integrity(approver_a, report_id) == ("no_match", None)
        assert dao.verify_report_integrity(approver_a, report_id) is False
        outcome, _, details = _audit_rows(conn)[-1]
        assert outcome == "success"  # the verification ran; it just could not verify
        assert details["outcome"] == "no_match" and details["matched"] is False


class TestTheIntegrityOutcomeIsInTheAuditTrail:
    """verify_report_integrity still returns a plain bool; the audit row says WHICH kind of match (or non-match) it was."""

    def test_canonical_match(self, dao, conn, session_a, interpreter_a, approver_a, interp_a_factory):
        report_id = _approved_report(dao, conn, session_a, interpreter_a, approver_a, interp_a_factory())
        result = dao.verify_report_integrity(approver_a, report_id)
        assert type(result) is bool and result is True
        outcome, resource_id, details = _audit_rows(conn)[-1]
        assert (outcome, resource_id) == ("success", str(report_id))
        assert details == {"report_id": str(report_id), "matched": True, "outcome": "match"}

    @pytest.mark.parametrize("legacy_zone", ["Asia/Kolkata", "Europe/London", "America/Los_Angeles"])
    def test_legacy_zone_match_names_the_zone(
        self, dao, conn, session_a, interpreter_a, approver_a, zone_connections, legacy_zone
    ):
        legacy_conn = zone_connections[legacy_zone]
        legacy_dao = _legacy_dao_on(legacy_conn)
        interp = _make_interpretation(legacy_dao, legacy_conn, session_a)
        report_id = _approved_report(legacy_dao, legacy_conn, session_a, interpreter_a, approver_a, interp)

        result = dao.verify_report_integrity(approver_a, report_id)
        assert type(result) is bool and result is True  # the compatibility contract is unchanged
        _, _, details = _audit_rows(conn)[-1]
        assert set(details) == {"report_id", "matched", "outcome", "legacy_zone"}
        assert details["outcome"] == "match_legacy_zone" and details["matched"] is True
        _assert_zone_matches_the_sessions_offsets(legacy_conn, report_id, details["legacy_zone"])

    def test_no_match_is_recorded_as_unverified_not_as_tampering(
        self, dao, conn, session_a, interpreter_a, approver_a, interp_a_factory
    ):
        interp = interp_a_factory()
        report_id = _approved_report(dao, conn, session_a, interpreter_a, approver_a, interp)
        with conn.cursor() as cur:
            cur.execute("UPDATE reviewer_claims SET reason = %s WHERE interpretation_id = %s", ("CHANGED", interp))
        conn.commit()
        result = dao.verify_report_integrity(approver_a, report_id)
        assert type(result) is bool and result is False
        _, _, details = _audit_rows(conn)[-1]
        assert details == {"report_id": str(report_id), "matched": False, "outcome": "no_match"}
        assert "tamper" not in json.dumps(details).lower()  # the record never asserts a cause

    def test_a_failed_call_records_no_outcome_and_one_calls_outcome_never_leaks_into_the_next(
        self, dao, conn, session_a, interpreter_a, approver_a, zone_connections, interp_a_factory
    ):
        legacy_conn = zone_connections["Asia/Kolkata"]
        legacy_dao = _legacy_dao_on(legacy_conn)
        legacy_interp = _make_interpretation(legacy_dao, legacy_conn, session_a)
        legacy_report = _approved_report(legacy_dao, legacy_conn, session_a, interpreter_a, approver_a, legacy_interp)
        assert dao.verify_report_integrity(approver_a, legacy_report) is True  # its outcome goes to ITS audit row...
        unapproved = dao.create_report(session_a, interp_a_factory())
        with pytest.raises(ValueError):
            dao.verify_report_integrity(session_a, unapproved)  # ...and a later failing call records none
        outcome, _, details = _audit_rows(conn)[-1]
        assert outcome == "error"
        assert details == {"report_id": str(unapproved), "matched": None}

    def test_the_audit_details_carry_no_report_content(
        self, dao, conn, session_a, interpreter_a, approver_a, interp_a_factory
    ):
        report_id = _approved_report(dao, conn, session_a, interpreter_a, approver_a, interp_a_factory())
        dao.verify_report_integrity(approver_a, report_id)
        _, _, details = _audit_rows(conn)[-1]
        assert set(details) <= {"report_id", "matched", "outcome", "legacy_zone"}


class TestAStaleIntegrityDetailCannotReachAnotherAuditRecord:
    """
    The verify audit row takes its `outcome` from a ContextVar that `verify_report_integrity` sets just before it
    returns and `_integrity_audit_details` consumes. The invariant these tests pin: whatever value is lying in that
    variable when a call starts, the audit row written for THAT call reflects only that call.

    Control flow relied on (clinical/data_access.py): `@auditable` wraps `@transactional` wraps the body. A call that
    completes runs the body (clear, classify, set), then the transaction commit, then the builder with the result. A call
    that raises goes to `@auditable`'s except branches, which call the builder with result None. The stale value is a
    distinctive, impossible one ("Stale/Zone") so any inheritance is unmistakable, and it is always cleared afterwards.
    """

    STALE = ("match_legacy_zone", "Stale/Zone")

    @pytest.fixture
    def seed_stale_detail(self):
        def seed():
            _INTEGRITY_AUDIT.set(self.STALE)

        try:
            yield seed
        finally:
            _INTEGRITY_AUDIT.set(None)  # reset even when the test body fails

    @staticmethod
    def _assert_not_inherited(details):
        assert "Stale/Zone" not in json.dumps(details)
        assert "legacy_zone" not in details

    def test_a_call_that_fails_in_the_classifier_records_no_outcome(
        self, dao, conn, session_a, interp_a_factory, seed_stale_detail
    ):
        unapproved = dao.create_report(
            session_a, interp_a_factory()
        )  # no stored hash: the classifier raises ValueError
        seed_stale_detail()
        with pytest.raises(ValueError):
            dao.verify_report_integrity(session_a, unapproved)
        outcome, _, details = _audit_rows(conn)[-1]
        assert outcome == "error"
        assert details == {"report_id": str(unapproved), "matched": None}
        self._assert_not_inherited(details)
        assert _INTEGRITY_AUDIT.get() is None  # consumed by the failing call's audit row, not left for the next one

    def test_a_call_for_an_unknown_report_records_no_outcome(self, dao, conn, session_a, seed_stale_detail):
        unknown = uuid.uuid4()
        seed_stale_detail()
        with pytest.raises(Exception):  # NotFoundError: the exact type is not the invariant
            dao.verify_report_integrity(session_a, unknown)
        outcome, _, details = _audit_rows(conn)[-1]
        assert outcome == "error"
        assert details == {"report_id": str(unknown), "matched": None}
        self._assert_not_inherited(details)

    def test_a_successful_match_reports_its_own_outcome_not_the_stale_one(
        self, dao, conn, session_a, interpreter_a, approver_a, interp_a_factory, seed_stale_detail
    ):
        report_id = _approved_report(dao, conn, session_a, interpreter_a, approver_a, interp_a_factory())
        seed_stale_detail()
        assert dao.verify_report_integrity(approver_a, report_id) is True
        _, _, details = _audit_rows(conn)[-1]
        assert details == {"report_id": str(report_id), "matched": True, "outcome": "match"}
        self._assert_not_inherited(details)

    def test_a_successful_no_match_reports_its_own_outcome_not_the_stale_one(
        self, dao, conn, session_a, interpreter_a, approver_a, interp_a_factory, seed_stale_detail
    ):
        interp = interp_a_factory()
        report_id = _approved_report(dao, conn, session_a, interpreter_a, approver_a, interp)
        with conn.cursor() as cur:
            cur.execute("UPDATE reviewer_claims SET reason = %s WHERE interpretation_id = %s", ("CHANGED", interp))
        conn.commit()
        seed_stale_detail()
        assert dao.verify_report_integrity(approver_a, report_id) is False
        _, _, details = _audit_rows(conn)[-1]
        assert details == {"report_id": str(report_id), "matched": False, "outcome": "no_match"}
        self._assert_not_inherited(details)

    def test_the_stale_value_does_not_survive_into_the_following_call(
        self, dao, conn, session_a, interpreter_a, approver_a, interp_a_factory, seed_stale_detail
    ):
        """One failing call consumes it; the next successful call must then report only its own outcome."""
        unapproved = dao.create_report(session_a, interp_a_factory())
        good = _approved_report(dao, conn, session_a, interpreter_a, approver_a, interp_a_factory())
        seed_stale_detail()
        with pytest.raises(ValueError):
            dao.verify_report_integrity(session_a, unapproved)
        assert dao.verify_report_integrity(approver_a, good) is True
        rows = _audit_rows(conn)
        assert rows[-2][2] == {"report_id": str(unapproved), "matched": None}
        assert rows[-1][2] == {"report_id": str(good), "matched": True, "outcome": "match"}
