"""
tests/test_fastq_scanner.py
───────────────────────────

The FASTQ scanner: a scheduled, READ-ONLY sweep of
`{GEPER_FASTQ_ROOT}/{org_id}/incoming/` that writes `fastq_sets` rows.

WHAT THE TESTS GO THROUGH. Every test below calls `FastqScanner.scan_org()`
and asserts on what it returns or on what the database then holds. None of them
reaches past the scanner to set up the state they are checking. That is the
Phase 5d trap stated as a habit rather than a warning: a test that calls
`db_session.execute()` INSTEAD OF the code under test verifies the database and
passes while the code is broken.

THE IDEMPOTENCY KEY IS THE CHECKSUM PAIR, NOT THE PATH (human ruling,
2026-09-08). This is why the table deliberately carries no
`UNIQUE (org_id, r1_path)`: a moved or renamed file with the same content is
THE SAME OBSERVATION, and a reused path with new content is a DIFFERENT one, so
a path-based constraint would have been wrong in both directions. Both
directions are tested -- `test_the_same_pair_under_a_different_name_is_not_a_new_row`
and `test_a_reused_path_with_new_content_is_a_new_row` -- because either alone
is consistent with keying on the wrong thing.

ORG SCOPING: ANOTHER ORG'S SAMPLE MUST BE **INDISTINGUISHABLE** FROM UNKNOWN,
not merely refused. A distinct outcome for "exists but not yours" tells a caller
which sample ids exist in another organisation. So the test asserts the two
outcomes are EQUAL TO EACH OTHER rather than equal to a literal string -- it
keeps holding if the wording changes, and it fails if the two ever diverge.

SIZE STABILITY EXISTS FOR ONE REASON, and it is not tidiness: A FILE STILL BEING
WRITTEN HAS A VALID NAME AND A READABLE PREFIX. Without the check the scanner
ingests half a sequencing run and everything downstream is confidently wrong
about complete data. So the first sighting of a set is never ingested -- it is
deferred until a later scan sees the same size.

READ-ONLY IS ASSERTED, NOT ASSUMED: the scanner never deletes and never moves.
`test_the_scanner_moves_and_deletes_nothing` snapshots the tree before and after
and compares names, sizes and contents.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import uuid
from datetime import date, datetime, timezone

import pytest

from clinical.data_access import BcryptHasher, DataAccess, SystemClock

DSN = os.getenv("CLINICAL_TEST_DSN")
SCHEMA_PATH = __import__("pathlib").Path(__file__).parent.parent / "schema.sql"

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def conn():
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    if not DSN:
        pytest.skip("CLINICAL_TEST_DSN not set")
    connection = psycopg.connect(DSN, autocommit=False, connect_timeout=10)
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        for role in ("clinical_app", "clinical_retention"):
            cur.execute(f"DO $$ BEGIN CREATE ROLE {role}; EXCEPTION WHEN duplicate_object THEN NULL; END $$;")
        cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture()
def dao(conn):
    return DataAccess(conn, clock=SystemClock(), password_hasher=BcryptHasher(rounds=4))


def _seed_org_with_sample(dao, conn, label: str):
    """One organisation carrying one sample. Fixture setup only: `samples` and
    its ancestors have their own tests, and rebuilding them through the scanner
    would test them a second time while saying nothing about the scanner."""
    org_id = dao.create_organisation(f"Scan Org {label}")
    admin_id = dao.create_user(org_id, f"admin@scan-{label}.test", "password")
    with conn.cursor() as cur:
        for role in ("Administrator", "Interpreter"):
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (admin_id, org_id, role, admin_id, NOW),
            )
    conn.commit()
    session = dao.login(f"admin@scan-{label}.test", org_id, "password")

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
    return {"a": _seed_org_with_sample(dao, conn, "A"), "b": _seed_org_with_sample(dao, conn, "B")}


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "fastq_root"


def _incoming(root, org_id):
    d = root / str(org_id) / "incoming"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_gz(path, payload: bytes):
    with gzip.open(path, "wb") as fh:
        fh.write(payload)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_pair(
    directory, sample_id, payload_r1=b"@r1\nACGT\n+\nIIII\n", payload_r2=b"@r2\nTGCA\n+\nIIII\n", tag="S1_L001"
):
    r1 = directory / f"{sample_id}_{tag}_R1_001.fastq.gz"
    r2 = directory / f"{sample_id}_{tag}_R2_001.fastq.gz"
    return r1, r2, _write_gz(r1, payload_r1), _write_gz(r2, payload_r2)


def _scanner(dao, root):
    from clinical.ingestion import FastqScanner

    return FastqScanner(dao, fastq_root=root, clock=SystemClock())


def _rows(conn, org_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fastq_set_id, sample_id, r1_path, r2_path, r1_checksum, r2_checksum, state "
            "FROM fastq_sets WHERE org_id = %s ORDER BY detected_at",
            (org_id,),
        )
        return cur.fetchall()


def _scan_twice(scanner, org_id):
    """A set is only ingested once its size has held across two scans, so the
    ordinary path is two calls. Returning both results keeps the deferral
    visible to tests rather than hidden inside a helper."""
    return scanner.scan_org(org_id), scanner.scan_org(org_id)


class TestASetIsIngestedOnceItsSizeHasHeld:
    def test_the_first_sighting_is_deferred_not_ingested(self, dao, conn, two_orgs, root):
        a = two_orgs["a"]
        _write_pair(_incoming(root, a["org_id"]), a["sample_id"])
        result = _scanner(dao, root).scan_org(a["org_id"])
        conn.commit()
        assert result.ingested == []
        assert len(result.deferred) == 1
        assert result.deferred[0].reason == "awaiting_size_stability"
        assert _rows(conn, a["org_id"]) == []

    def test_the_second_scan_ingests_it(self, dao, conn, two_orgs, root):
        a = two_orgs["a"]
        r1, r2, sum1, sum2 = _write_pair(_incoming(root, a["org_id"]), a["sample_id"])
        scanner = _scanner(dao, root)
        _, second = _scan_twice(scanner, a["org_id"])
        conn.commit()
        assert len(second.ingested) == 1
        rows = _rows(conn, a["org_id"])
        assert len(rows) == 1
        assert rows[0][1] == a["sample_id"]
        assert rows[0][2] == str(r1)
        assert rows[0][3] == str(r2)
        assert rows[0][4] == sum1
        assert rows[0][5] == sum2
        assert rows[0][6] == "detected"

    def test_a_file_that_grew_between_scans_is_deferred_again(self, dao, conn, two_orgs, root):
        """THE WHOLE POINT: a file still being written has a valid name and a
        readable prefix. Growing between scans is exactly what that looks like."""
        a = two_orgs["a"]
        directory = _incoming(root, a["org_id"])
        r1, r2, _, _ = _write_pair(directory, a["sample_id"])
        scanner = _scanner(dao, root)
        scanner.scan_org(a["org_id"])
        _write_gz(r1, b"@r1\nACGT\n+\nIIII\n" * 50)  # still being written
        second = scanner.scan_org(a["org_id"])
        conn.commit()
        assert second.ingested == []
        assert [d.reason for d in second.deferred] == ["awaiting_size_stability"]
        assert _rows(conn, a["org_id"]) == []


class TestTheChecksumPairIsTheIdempotencyKey:
    def test_a_rescan_does_not_create_a_second_row(self, dao, conn, two_orgs, root):
        a = two_orgs["a"]
        _write_pair(_incoming(root, a["org_id"]), a["sample_id"])
        scanner = _scanner(dao, root)
        _scan_twice(scanner, a["org_id"])
        conn.commit()
        third = scanner.scan_org(a["org_id"])
        conn.commit()
        assert third.ingested == []
        assert [s.reason for s in third.skipped] == ["already_recorded"]
        assert len(_rows(conn, a["org_id"])) == 1

    def test_the_same_pair_under_a_different_name_is_not_a_new_row(self, dao, conn, two_orgs, root):
        """A MOVED OR RENAMED FILE WITH THE SAME CONTENT IS THE SAME
        OBSERVATION. Keying on the path would create a second row here."""
        a = two_orgs["a"]
        directory = _incoming(root, a["org_id"])
        r1, r2, _, _ = _write_pair(directory, a["sample_id"], tag="S1_L001")
        scanner = _scanner(dao, root)
        _scan_twice(scanner, a["org_id"])
        conn.commit()
        r1.rename(directory / f"{a['sample_id']}_S9_L009_R1_001.fastq.gz")
        r2.rename(directory / f"{a['sample_id']}_S9_L009_R2_001.fastq.gz")
        _scan_twice(scanner, a["org_id"])
        conn.commit()
        assert len(_rows(conn, a["org_id"])) == 1

    def test_a_reused_path_with_new_content_is_a_new_row(self, dao, conn, two_orgs, root):
        """THE OTHER DIRECTION, and the one that makes the test above mean
        something: identical paths, different bytes, so it is a different
        observation and must be recorded as one."""
        a = two_orgs["a"]
        directory = _incoming(root, a["org_id"])
        r1, r2, _, _ = _write_pair(directory, a["sample_id"])
        scanner = _scanner(dao, root)
        _scan_twice(scanner, a["org_id"])
        conn.commit()
        _write_gz(r1, b"@r1\nGGGG\n+\nIIII\n")
        _write_gz(r2, b"@r2\nCCCC\n+\nIIII\n")
        _scan_twice(scanner, a["org_id"])
        conn.commit()
        rows = _rows(conn, a["org_id"])
        assert len(rows) == 2
        assert rows[0][4] != rows[1][4]


class TestAnotherOrgsSampleIsIndistinguishableFromUnknown:
    def test_the_two_outcomes_are_identical(self, dao, conn, two_orgs, root):
        """NOT 'refused' -- INDISTINGUISHABLE. A distinct outcome for "exists
        but not yours" tells the caller which sample ids exist elsewhere.
        Compared to EACH OTHER, not to a literal, so it survives a reworded
        message and fails the moment the two diverge."""
        a, b = two_orgs["a"], two_orgs["b"]
        directory = _incoming(root, a["org_id"])
        _write_pair(directory, b["sample_id"], tag="S1_L001")  # another org's sample
        unknown = uuid.uuid4()
        _write_pair(directory, unknown, tag="S2_L001")  # no such sample anywhere
        scanner = _scanner(dao, root)
        _, second = _scan_twice(scanner, a["org_id"])
        conn.commit()

        by_sample = {r.sample_id: r for r in second.rejected}
        assert str(b["sample_id"]) in by_sample, second.rejected
        assert str(unknown) in by_sample, second.rejected
        other_org = by_sample[str(b["sample_id"])]
        nonexistent = by_sample[str(unknown)]
        assert other_org.reason == nonexistent.reason
        assert other_org.detail == nonexistent.detail
        assert _rows(conn, a["org_id"]) == []

    def test_a_sample_in_this_org_does_resolve(self, dao, conn, two_orgs, root):
        """POSITIVE CONTROL. Without it, a scanner that rejected every sample
        would pass the test above."""
        a = two_orgs["a"]
        _write_pair(_incoming(root, a["org_id"]), a["sample_id"])
        _, second = _scan_twice(_scanner(dao, root), a["org_id"])
        conn.commit()
        assert len(second.ingested) == 1
        assert second.rejected == []


class TestTheFileChecks:
    def test_a_name_off_contract_is_rejected_by_name(self, dao, conn, two_orgs, root):
        a = two_orgs["a"]
        directory = _incoming(root, a["org_id"])
        (directory / "not-a-fastq-set.txt").write_bytes(b"hello")
        (directory / "missing_read_marker_001.fastq.gz").write_bytes(b"\x1f\x8b")
        _, second = _scan_twice(_scanner(dao, root), a["org_id"])
        conn.commit()
        names = sorted(r.detail for r in second.rejected)
        assert names == ["missing_read_marker_001.fastq.gz", "not-a-fastq-set.txt"], second.rejected
        assert {r.reason for r in second.rejected} == {"filename_off_contract"}

    def test_an_unpaired_r1_is_rejected(self, dao, conn, two_orgs, root):
        a = two_orgs["a"]
        directory = _incoming(root, a["org_id"])
        r1, r2, _, _ = _write_pair(directory, a["sample_id"])
        r2.unlink()
        _, second = _scan_twice(_scanner(dao, root), a["org_id"])
        conn.commit()
        assert [r.reason for r in second.rejected] == ["incomplete_pair"]
        assert _rows(conn, a["org_id"]) == []

    def test_a_corrupt_gzip_is_rejected(self, dao, conn, two_orgs, root):
        """Valid name, plausible size, unreadable content. The check exists
        because the first two are satisfied by a truncated transfer."""
        a = two_orgs["a"]
        directory = _incoming(root, a["org_id"])
        r1, r2, _, _ = _write_pair(directory, a["sample_id"])
        r2.write_bytes(b"\x1f\x8b\x08\x00" + b"garbage that is not a gzip stream")
        _, second = _scan_twice(_scanner(dao, root), a["org_id"])
        conn.commit()
        assert [r.reason for r in second.rejected] == ["gzip_unreadable"]
        assert _rows(conn, a["org_id"]) == []

    def test_the_recorded_checksums_are_of_the_files_on_disk(self, dao, conn, two_orgs, root):
        """A checksum column that is written but never verified is decoration.
        Computed here independently of the scanner."""
        a = two_orgs["a"]
        r1, r2, sum1, sum2 = _write_pair(_incoming(root, a["org_id"]), a["sample_id"])
        _scan_twice(_scanner(dao, root), a["org_id"])
        conn.commit()
        row = _rows(conn, a["org_id"])[0]
        assert row[4] == hashlib.sha256(r1.read_bytes()).hexdigest() == sum1
        assert row[5] == hashlib.sha256(r2.read_bytes()).hexdigest() == sum2


class TestTheScannerIsReadOnly:
    def test_the_scanner_moves_and_deletes_nothing(self, dao, conn, two_orgs, root):
        """READ-ONLY asserted rather than assumed: names, sizes and bytes
        before and after, including the files it rejects."""
        a = two_orgs["a"]
        directory = _incoming(root, a["org_id"])
        _write_pair(directory, a["sample_id"])
        _write_pair(directory, uuid.uuid4(), tag="S3_L001")  # will be rejected
        (directory / "junk.txt").write_bytes(b"junk")

        def snapshot():
            return {p.name: (p.stat().st_size, p.read_bytes()) for p in sorted(directory.iterdir())}

        before = snapshot()
        _scan_twice(_scanner(dao, root), a["org_id"])
        conn.commit()
        assert snapshot() == before


class TestTheScanIsAudited:
    def test_an_ingested_set_leaves_an_audit_row(self, dao, conn, two_orgs, root):
        """Reusing the existing audit trail rather than inventing a second
        one: the row must land in audit_log like every other write."""
        a = two_orgs["a"]
        _write_pair(_incoming(root, a["org_id"]), a["sample_id"])
        _scan_twice(_scanner(dao, root), a["org_id"])
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM audit_log WHERE org_id = %s AND action = 'fastq_set_detected'",
                (a["org_id"],),
            )
            assert cur.fetchone()[0] == 1


class TestOneOrgsScanCannotSeeAnothersDirectory:
    def test_scanning_org_a_ingests_nothing_for_org_b(self, dao, conn, two_orgs, root):
        a, b = two_orgs["a"], two_orgs["b"]
        _write_pair(_incoming(root, a["org_id"]), a["sample_id"])
        _write_pair(_incoming(root, b["org_id"]), b["sample_id"])
        _scan_twice(_scanner(dao, root), a["org_id"])
        conn.commit()
        assert len(_rows(conn, a["org_id"])) == 1
        # THE HALF THAT MAKES IT MEAN SOMETHING: B's files really are there and
        # really are ingestible, so A's scan returned one row because it was
        # scoped, not because B's directory was empty or unreadable.
        assert _rows(conn, b["org_id"]) == []
        _scan_twice(_scanner(dao, root), b["org_id"])
        conn.commit()
        assert len(_rows(conn, b["org_id"])) == 1
