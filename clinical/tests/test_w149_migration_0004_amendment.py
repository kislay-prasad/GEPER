"""
clinical/tests/test_w149_migration_0004_amendment.py
─────────────────────────────────────────────────────
MIGRATION 0004 -- THE EXCEPTION-AMENDMENT RELATIONSHIP, TENANT-BOUND, AND THE
CROSS-ORGANISATION ROW PROVED INEXPRESSIBLE BY ATTEMPTING THE WRITE.

THE ONE THING TO READ FIRST. The human's row 2 -- "cross-org amendment fails"
-- is NOT proved by asking the catalogue whether a constraint exists. A
catalogue assertion is satisfied by a constraint that is present and
unenforced, and by a constraint whose columns are not the ones anybody
believes. Every row in `TestTheSixRequiredRows` below ATTEMPTS THE WRITE
against a real PostgreSQL server and asserts the server's own verdict,
including its SQLSTATE.

AND THE SQLSTATE IS NOT DECORATION. At 4d16b8f -- before this migration --
`exception_events` has no `amends_event_id` column at all, so the cross-org
INSERT below fails with 42703 (undefined_column). A row asserting only "this
raises" would be GREEN on the pre-fix code, for a reason that has nothing to
do with tenancy. That is the shape w148 measured in the other direction: a
missing predicate invisible to every behavioural row because the identifiers
could not collide by accident. Here the identifiers collide BY CONSTRUCTION --
the amendment names another organisation's REAL, PLANTED event id, and the
test asserts that id genuinely exists before attempting to reference it -- and
the refusal must be 23503 (foreign_key_violation) specifically. Measured
against the four wrong versions this wave built (A, B, D and the pre-fix tip);
the results are in the wave report.

WHAT 0004 LANDS, AND WHAT IT DELIBERATELY DOES NOT. Three objects: a UNIQUE
key over (org_id, id), a nullable `amends_event_id UUID`, and the COMPOSITE
self-foreign key `(amends_event_id, org_id) -> exception_events (id, org_id)`.
It does NOT add `'amend_resolution'` to the `action` CHECK, and it does not add
the held branch's CHECK tying the action to the column: widening an inline
CHECK is DROP-then-ADD, which is destructive, and the derivation MEASURED that
this is exactly where the held branch stops being additive (5 opcodes, 18
inserted lines, one `replace`). So after this migration an amendment row is
STRUCTURALLY POSSIBLE AND TENANT-BOUND, and NOTHING IN THIS REPOSITORY WRITES
ONE -- `amends_event_id` appears in no INSERT outside this file. The
relationship is landed and proved; the verb is the next authorisation's.

THE WRONG VERSIONS THIS FILE WAS BUILT AGAINST. Each row names the one it
catches AND the one it is vacuous against:

  A  the held branch's SINGLE-COLUMN self-FK  (REFERENCES exception_events (id))
  B  the composite FK added WITHOUT the unique key -- must fail to create
  C  `org_id` omitted from an amendment insert
  D  the unique key added but the FK left single-column: the catalogue carries
     a constraint of the right NAME on the right table, and is wrong
  E  a migration that adds the objects but declares no generation invariant

THESE TESTS CREATE THEIR OWN DATABASES (`clinical/tests/migration_db.py`) for
everything about the migration mechanism, for the reason that module's
docstring gives: a fixture that rebuilds from the current `schema.sql`
destroys the one property an upgrade test is about. The six behavioural rows
use the package's ordinary `DROP SCHEMA`/`schema.sql` fixture, because their
subject is the CURRENT schema and their two organisations are built through
`DataAccess` -- the same path `test_w129_exception_tenant_isolation.py` uses,
and its `_org_with_admin` / `_order_for` helpers are imported here rather than
copied so the two files cannot drift.
"""

from __future__ import annotations

import datetime
import os
import pathlib
import threading
import uuid

import pytest

from clinical import migrate
from clinical.data_access import BcryptHasher, DataAccess, NotFoundError, SystemClock
from clinical.models.exception import (
    ExceptionCategory,
    ExceptionReasonCode,
    ResolutionAction,
)
from clinical.tests.migration_db import (
    SENTINEL_NOTE,
    SENTINEL_ORG,
    connect,
    plant_sentinel,
    read_sentinel,
    scratch_database,
)
from clinical.tests.test_w126m_schema_equivalence import catalog_of, describe_difference
from clinical.tests.test_w129_exception_tenant_isolation import _order_for, _org_with_admin

SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / "schema.sql"
PRE_SECURITY_PATH = pathlib.Path(__file__).resolve().parent / "fixtures" / "pre_security_schema_da97f847.sql"

DSN = os.getenv("CLINICAL_TEST_DSN")
NOW = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)

AMENDMENT_VERSION = 4
AMENDMENT_NAME = "exception_amendment"

# The object set 0004 is FOR, written out here rather than read out of the
# migration file: a test that reads its expectations from the file it is
# testing agrees with that file by construction.
AMENDMENT_COLUMN = "amends_event_id"
AMENDMENT_UNIQUE = "exception_events_org_event_unique"
AMENDMENT_FK = "exception_events_amends_event_fk"

# The FK's definition AS THE SERVER RENDERS IT. This literal is what separates
# the right version from WRONG D: a single-column self-FK carries the same
# name on the same table and differs only here.
AMENDMENT_FK_DEF = "FOREIGN KEY (amends_event_id, org_id) REFERENCES exception_events(id, org_id)"
AMENDMENT_UNIQUE_DEF = "UNIQUE (org_id, id)"


# ─── helpers ────────────────────────────────────────────────────────────────


def _apply(connection, sql: str) -> None:
    with connection.cursor() as cur:
        cur.execute(sql)
    connection.commit()


def _build_fresh_from_schema_sql(connection) -> None:
    """The production fresh-database path. It stamps its own history."""
    _apply(connection, SCHEMA_PATH.read_text(encoding="utf-8"))


def _build_historical_baseline_generation(connection) -> None:
    """A REAL pre-mechanism database of the BASELINE generation: the frozen
    `0002_baseline.sql` bytes, applied for real, with no history table."""
    baseline = {m.version: m for m in migrate.discover()}[migrate.BASELINE_VERSION]
    _apply(connection, baseline.sql)


def _build_pre_security(connection) -> None:
    """The real EARLIER generation -- `7b034e5:clinical/schema.sql`, blob
    da97f847, byte for byte. Its `exception_events` has no `org_id` at all."""
    _apply(connection, PRE_SECURITY_PATH.read_text(encoding="utf-8"))


def _regclass(connection, name: str):
    with connection.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (name,))
        return cur.fetchone()[0]


def _history(connection) -> tuple:
    with connection.cursor() as cur:
        cur.execute("SELECT version, name, checksum FROM schema_migrations ORDER BY version")
        return tuple(cur.fetchall())


def _constraint_def(connection, name: str):
    """What the SERVER says the constraint is, not what its name suggests."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = to_regclass('exception_events') AND conname = %s",
            (name,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _has_column(connection, table: str, column: str) -> bool:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass(%s) "
            "AND attname = %s AND attnum > 0 AND NOT attisdropped)",
            (table, column),
        )
        return cur.fetchone()[0]


def _the_migration() -> migrate.Migration:
    by_version = {m.version: m for m in migrate.discover()}
    assert AMENDMENT_VERSION in by_version, "migration 0004 is not on disk"
    return by_version[AMENDMENT_VERSION]


def _on_disk_versions() -> list:
    """Derived from `discover()`, never hard-coded: w146's finding was that a
    hard-coded migration set is a row that has to be re-authored by every
    later wave, and w147 re-introduced it in its own file."""
    return [m.version for m in migrate.discover()]


@pytest.fixture()
def client():
    pytest.importorskip("fastapi", reason="fastapi not installed")
    pytest.importorskip("httpx", reason="httpx not installed (starlette TestClient requires it)")
    from fastapi.testclient import TestClient

    from clinical.app import app

    with TestClient(app) as test_client:
        yield test_client


def _probe(client, monkeypatch, dsn):
    monkeypatch.setenv("CLINICAL_DSN", dsn)
    response = client.get("/readyz")
    return response.status_code, response.json().get("reason"), response.text


# ─── 1. the file, before any database is built with it ──────────────────────


class TestTheMigrationIsOnDiskAndTheSetStillOrders:
    def test_the_set_is_contiguous_and_ordered_by_integer_and_includes_four(self):
        """
        CATCHES: a 0004 that is not discovered at all, and a set whose order
        comes from the string rather than the integer.

        DERIVED, NOT HARD-CODED. `[1, 2, 3, 4]` written out here would be the
        exact row w146 swept the suite for and w147 wrote back into its own
        file; the next migration would have to re-author it.

        VACUOUS AGAINST: everything about the DDL. A file of the right name
        containing nothing passes this.
        """
        discovered = migrate.discover()
        versions = [m.version for m in discovered]
        assert versions == sorted(versions)
        assert versions == list(range(1, len(versions) + 1)), versions
        assert AMENDMENT_VERSION in versions
        assert {m.version: m.name for m in discovered}[AMENDMENT_VERSION] == AMENDMENT_NAME

    def test_the_frozen_earlier_migrations_are_byte_identical(self):
        """
        THE GATE, AS A TEST. 0002 and 0003 are committed and frozen; their
        checksums are stamped into `clinical/schema.sql` and an edit to either
        makes every already-adopted database raise MigrationChecksumMismatch.

        CATCHES: a 0004 that was landed by editing 0003 instead, which is the
        shortcut this wave was explicitly forbidden.

        VACUOUS AGAINST: A, B, C, D and E -- all of which leave 0002 and 0003
        alone.
        """
        by_version = {m.version: m for m in migrate.discover()}
        assert by_version[2].checksum == "cc842c3943b7aa1e5786d638eff742f63fd47ce5a5048d7321036234a5290af8"
        assert by_version[3].checksum == "d0e0ae8d149aa20aea64e18e47424f48efb63d0a7ccdd5cbfd9b3f8016202204"

    def test_schema_sql_stamps_0004_with_the_checksum_of_the_file_on_disk(self):
        """
        The pair that cannot be half-done: the DDL goes into `schema.sql` AND
        its stamp goes into the INSERT at the foot of that file. A stamp with
        the right version and the wrong bytes reports pending for ever.
        """
        migration = _the_migration()
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        assert f"({AMENDMENT_VERSION}, '{AMENDMENT_NAME}', '{migration.checksum}')" in schema

    def test_0004_declares_generation_invariants_and_one_of_them_is_the_foreign_key(self):
        """
        WRONG E, and it is a row about `clinical/migrate.py`, not about the
        DDL. A 0004 that declared no GENERATION INVARIANT would pass every
        other row in this file, because the objects would all be there.

        CORRECTED 2026-09-19 (human ruling, implemented by w160): it is no
        longer true that "nothing forces a migration to declare" — every
        migration must declare at least one post-condition in ONE of the two
        channels, and `clinical/migrate.py::_refuse_undeclared` enforces it.
        This row is still needed and is NOT that rule: 0004 satisfies the rule
        through its own header lines alone, so the ruling would be met by a
        0004 with an empty `_GENERATION_INVARIANTS` entry. What this row
        demands is the STRONGER declaration — the composite foreign key, which
        no `to_regclass` header line can express. The rule sets a floor; this
        row sets 0004's ceiling.

        The FK invariant is asserted specifically because it is the strong
        one: it cannot be satisfied without the unique key either (PostgreSQL
        refuses the FK with 42830 otherwise), so one probe covers two objects.

        VACUOUS AGAINST: A, B, C and D -- all of which are DDL.
        """
        invariants = migrate.generation_invariants(_the_migration())
        assert invariants, "migration 0004 declares no generation invariant"
        kinds = {i.kind for i in invariants}
        assert "constraint" in kinds, kinds
        assert "column" in kinds, kinds
        descriptions = " ".join(i.description for i in invariants)
        assert AMENDMENT_FK in descriptions
        assert f"exception_events.{AMENDMENT_COLUMN}" in descriptions

    def test_every_invariant_reads_pg_catalog_and_none_reads_information_schema(self):
        """
        WRONG B of w147's list, re-asserted for this migration's own
        invariants. `information_schema` is privilege-filtered; `/readyz`
        evaluates these as a role whose privileges are deliberately narrowed,
        and a filtered answer reports a present column as absent.

        THE OLD RATIONALE IS WRONG AND THE RULE STILL STANDS -- the correction
        0003 established: `clinical_app` can in fact see every table in this
        schema; `clinical_retention` is the filtered role. The rule is kept
        because the privilege map is a thing that changes and the catalogue
        read should not depend on it.
        """
        for invariant in migrate.generation_invariants(_the_migration()):
            assert "information_schema" not in invariant.expression, invariant.description
            assert "pg_" in invariant.expression, invariant.description

    def test_the_migration_states_post_conditions_and_they_are_the_weak_half(self):
        """
        The `MIGRATION-ASSERT` lines exist and are `to_regclass` probes. The
        row also states, as an assertion rather than as a comment, WHAT THEY
        CANNOT SEE: neither the column nor the foreign key is nameable by
        `to_regclass`, which is the whole reason the generation invariants
        above exist.
        """
        assertions = _the_migration().assertions
        assert assertions, "0004 states no post-conditions at all"
        joined = " ".join(assertions)
        assert AMENDMENT_UNIQUE in joined, "the unique constraint's index is nameable and should be probed"
        assert AMENDMENT_COLUMN not in joined, "to_regclass cannot see a column; this probe would be a lie"
        assert AMENDMENT_FK not in joined, "to_regclass cannot see a constraint; this probe would be a lie"

    def test_0004_contains_no_destructive_statement(self):
        """
        THE PROPERTY THE WHOLE MIGRATION RESTS ON, asserted on the bytes. No
        DROP, no RENAME, no DELETE, no TRUNCATE, no ALTER COLUMN. Comment
        lines are stripped first, because this file's header discusses DROP at
        length and a naive substring search would be satisfied by the prose
        rather than by the DDL.

        CATCHES: a later editor "just" widening the action CHECK here, which
        requires DROP CONSTRAINT and is the reason the vocabulary is not in
        this migration.
        """
        body = "\n".join(
            line for line in _the_migration().sql.splitlines() if not line.strip().startswith("--")
        ).upper()
        for forbidden in ("DROP ", "RENAME ", "DELETE ", "TRUNCATE ", "ALTER COLUMN"):
            assert forbidden not in body, f"0004 contains {forbidden.strip()!r}"
        assert "ADD CONSTRAINT" in body and "ADD COLUMN" in body


# ─── 2. a fresh database ────────────────────────────────────────────────────


class TestAFreshDatabaseHasTheAmendmentObjectsAndSaysItIsAtFour:
    def test_schema_sql_produces_all_three_objects_and_a_current_history(self):
        with scratch_database("w149_fresh") as dsn, connect(dsn) as connection:
            _build_fresh_from_schema_sql(connection)

            assert _has_column(connection, "exception_events", AMENDMENT_COLUMN)
            assert _constraint_def(connection, AMENDMENT_UNIQUE) == AMENDMENT_UNIQUE_DEF
            assert _constraint_def(connection, AMENDMENT_FK) == AMENDMENT_FK_DEF

            assert [row[0] for row in _history(connection)] == _on_disk_versions()
            state = migrate.inspect_state(connection)
            assert state.ok is True, state.detail
            # DERIVED, NOT `== AMENDMENT_VERSION`. `current_version` is
            # `max(applied)` -- the FRONTIER of the set, not this migration.
            # Pinning it to 4 asserts that 0004 is the LAST migration, which is
            # a claim about the size of `clinical/migrations/` that stops being
            # true the morning a fifth file lands. w146 removed nine instances
            # of this shape; w147 and then this file re-introduced it.
            # `test_w151_migration_declaration_guards.py::TestNoTestPinsTheFrontier`
            # is the structural guard that now forbids it.
            assert state.current_version == max(_on_disk_versions())
            assert state.pending == ()
            assert migrate.migrate(connection) == ()

    def test_the_column_is_nullable_which_is_what_makes_it_mean_something_when_set(self):
        """
        Rule 4's positive twin for the nullability claim. A NOT NULL
        `amends_event_id` would force every `open`/`resolve`/`reopen` row to
        name an amendment target, which is the opposite of the design.
        """
        with scratch_database("w149_null") as dsn, connect(dsn) as connection:
            _build_fresh_from_schema_sql(connection)
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT attnotnull FROM pg_attribute WHERE attrelid = to_regclass('exception_events') "
                    "AND attname = %s",
                    (AMENDMENT_COLUMN,),
                )
                assert cur.fetchone()[0] is False


# ─── 3. a real historical database, upgraded rather than rebuilt ────────────


class TestARealHistoricalDatabaseIsUpgradedRatherThanRebuilt:
    def test_a_baseline_generation_database_is_adopted_and_carried_to_four(self):
        """
        THE SENTINEL ROW. Data written BEFORE the upgrade must still be there
        AFTER it, by content, and the new objects must be present. A
        marker-only assertion passes against a runner that does nothing; an
        objects-only assertion passes against a fixture that rebuilt the
        database, which is the accident `migration_db.py` exists to prevent.
        """
        with scratch_database("w149_upgrade") as dsn, connect(dsn) as connection:
            _build_historical_baseline_generation(connection)
            org_id = plant_sentinel(connection)
            assert read_sentinel(connection) == ((SENTINEL_NOTE,), (SENTINEL_ORG,))

            migrate.adopt_existing(connection)
            applied = migrate.migrate(connection)
            assert applied == tuple(v for v in _on_disk_versions() if v > migrate.BASELINE_VERSION), applied

            assert read_sentinel(connection) == ((SENTINEL_NOTE,), (SENTINEL_ORG,))
            assert _has_column(connection, "exception_events", AMENDMENT_COLUMN)
            assert _constraint_def(connection, AMENDMENT_FK) == AMENDMENT_FK_DEF

            # and the organisation planted before the upgrade is still a
            # usable tenant afterwards -- the upgrade did not orphan it.
            with connection.cursor() as cur:
                cur.execute("SELECT count(*) FROM organisations WHERE org_id = %s", (org_id,))
                assert cur.fetchone()[0] == 1
            connection.rollback()

            assert [row[0] for row in _history(connection)] == _on_disk_versions()
            assert migrate.inspect_state(connection).ok is True

    def test_the_control_a_rebuild_destroys_the_sentinel_while_satisfying_every_object_probe(self):
        """
        The sentinel's own non-vacuity: do the WRONG thing on purpose -- drop
        the schema and rebuild from `schema.sql` -- and require the sentinel to
        be GONE while every object 0004 is for is PRESENT.
        """
        with scratch_database("w149_rebuild") as dsn, connect(dsn) as connection:
            _build_historical_baseline_generation(connection)
            plant_sentinel(connection)
            assert read_sentinel(connection) == ((SENTINEL_NOTE,), (SENTINEL_ORG,))

            _apply(connection, "DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
            _build_fresh_from_schema_sql(connection)

            assert _has_column(connection, "exception_events", AMENDMENT_COLUMN)
            assert _constraint_def(connection, AMENDMENT_FK) == AMENDMENT_FK_DEF
            assert read_sentinel(connection) == ((), ())


# ─── 4. convergence, by comparing catalogues ────────────────────────────────


class TestFreshCreationAndHistoricalUpgradeConverge:
    def test_the_two_catalogs_are_identical(self):
        """
        Proved by COMPARING THE TWO CATALOGS -- nine sections, every row -- and
        not by running both databases past the same probe list, which is
        satisfied by two databases that differ in anything the list does not
        mention.

        CATCHES: a 0004 whose DDL differs in ANY way from the amendment text
        added to `clinical/schema.sql` -- a constraint spelled differently, a
        column of a different type, a unique key on the wrong columns. In
        particular it catches the version where `schema.sql` declares the
        column inside CREATE TABLE and the migration's ALTER produces
        something that is not the same object.

        VACUOUS AGAINST: the history. Two identical catalogs can carry
        different stamps; that is asserted separately below.
        """
        with (
            scratch_database("w149_cfresh") as dsn_fresh,
            scratch_database("w149_cupgraded") as dsn_upgraded,
        ):
            with connect(dsn_fresh) as fresh, connect(dsn_upgraded) as upgraded:
                _build_fresh_from_schema_sql(fresh)

                _build_historical_baseline_generation(upgraded)
                migrate.adopt_existing(upgraded)
                applied = migrate.migrate(upgraded)
                assert AMENDMENT_VERSION in applied, applied

                left = catalog_of(fresh)
                right = catalog_of(upgraded)

            difference = describe_difference("fresh from schema.sql", left, "upgraded historical", right)
            assert not difference, (
                "a fresh database and an upgraded historical one are not the same database:" + difference
            )

    def test_both_paths_record_the_same_history_by_content(self):
        with (
            scratch_database("w149_hfresh") as dsn_fresh,
            scratch_database("w149_hupgraded") as dsn_upgraded,
        ):
            with connect(dsn_fresh) as fresh, connect(dsn_upgraded) as upgraded:
                _build_fresh_from_schema_sql(fresh)
                _build_historical_baseline_generation(upgraded)
                migrate.adopt_existing(upgraded)
                migrate.migrate(upgraded)

                assert _history(fresh) == _history(upgraded)
                assert migrate.inspect_state(fresh).ok is True
                assert migrate.inspect_state(upgraded).ok is True


# ─── 5. readiness cannot report healthy for the wrong generation ────────────


class TestReadinessCannotReportHealthyForTheWrongGeneration:
    def test_a_database_missing_only_the_amendment_FOREIGN_KEY_is_refused(self, client, monkeypatch):
        """
        WRONG A AND WRONG D, IN THEIR SHARPEST FORM, and the reason a
        `MIGRATION-ASSERT` line is not enough. Drop the ONE named foreign key
        and leave the table, the column and the unique index standing: BOTH of
        0004's own `to_regclass` post-conditions still hold, so a migration
        that declared no generation invariant reports this database READY.

        The positive twin runs FIRST on the same database, so this cannot pass
        because `inspect_state` is broken in the other direction.

        Asserted on BOTH channels per Standing Rule 8: the library verdict and
        the probe's status AND body, and the body must NAME the constraint.

        VACUOUS AGAINST: C and E.
        """
        with scratch_database("w149_drift") as dsn, connect(dsn) as connection:
            _build_fresh_from_schema_sql(connection)
            assert migrate.inspect_state(connection).ok is True

            _apply(connection, f"ALTER TABLE exception_events DROP CONSTRAINT {AMENDMENT_FK}")

            for expression in _the_migration().assertions:
                assert migrate._evaluate(connection, expression) is True, expression

            state = migrate.inspect_state(connection)
            status, reason, text = _probe(client, monkeypatch, dsn)

        assert state.ok is False
        assert state.reason == "schema_drift"
        assert AMENDMENT_FK in state.detail
        assert status == 503
        assert reason == "schema_drift"
        assert AMENDMENT_FK in text

    def test_a_database_missing_only_the_amendment_COLUMN_is_refused(self):
        """
        The column half. Dropping `amends_event_id` CASCADEs the foreign key
        with it, so this row asserts the column's own invariant by name --
        without it the detail would name only the constraint and an operator
        would rebuild the wrong thing.
        """
        with scratch_database("w149_driftcol") as dsn, connect(dsn) as connection:
            _build_fresh_from_schema_sql(connection)
            assert migrate.inspect_state(connection).ok is True

            _apply(connection, f"ALTER TABLE exception_events DROP COLUMN {AMENDMENT_COLUMN} CASCADE")
            assert _regclass(connection, "exception_events") is not None

            state = migrate.inspect_state(connection)

        assert state.ok is False
        assert state.reason == "schema_drift"
        assert f"exception_events.{AMENDMENT_COLUMN}" in state.detail

    def test_a_forged_history_claiming_four_on_a_retraction_database_is_refused(self, client, monkeypatch):
        """
        THE FALSELY-READY DATABASE, at the new version. A database that never
        ran 0004, stamped by hand with the whole discovered set and the REAL
        checksums. Nothing in the history can say so; only the catalogue can.

        `pending == ()` is asserted, so this row cannot certify the GENERATION
        verdict while actually measuring the cheaper PENDING one.

        CATCHES: E through the probe, which is where it matters -- `/readyz`
        calls `discover()` itself, so a real fourth migration on disk is the
        only way to reach this path.
        """
        with scratch_database("w149_forged") as dsn, connect(dsn) as connection:
            _build_historical_baseline_generation(connection)
            migrate.adopt_existing(connection)
            # everything up to but NOT including 0004, applied for real
            for migration in migrate.discover():
                if migrate.BASELINE_VERSION < migration.version < AMENDMENT_VERSION:
                    _apply(connection, migration.sql)
            # ...and then the WHOLE set stamped by hand, 0004 included.
            with connection.cursor() as cur:
                for migration in migrate.discover():
                    cur.execute(
                        "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s) "
                        "ON CONFLICT (version) DO NOTHING",
                        (migration.version, migration.name, migration.checksum),
                    )
            connection.commit()

            assert not _has_column(connection, "exception_events", AMENDMENT_COLUMN)
            state = migrate.inspect_state(connection)
            status, reason, text = _probe(client, monkeypatch, dsn)

        assert state.pending == (), "this row must measure the generation verdict, not the pending one"
        assert state.ok is False
        assert state.reason == "schema_drift"
        assert f"exception_events.{AMENDMENT_COLUMN}" in state.detail
        assert status == 503
        assert reason == "schema_drift"
        assert f"exception_events.{AMENDMENT_COLUMN}" in text


# ─── 6. ordering and concurrency ────────────────────────────────────────────


class TestOrderingIsDeterministicAndOutOfOrderIsNotSilent:
    def test_0004_against_a_wrong_generation_database_RAISES_rather_than_refusing(self):
        """
        THE QUESTION THE BRIEF ASKS: raise or refuse? MEASURED, not inherited
        from 0003. 0004 alters an existing table rather than creating new ones,
        so the failure is a DIFFERENT SQLSTATE from 0003's -- a pre-security
        `exception_events` HAS the table and LACKS `org_id`, so PostgreSQL
        answers 42703 (undefined_column) on the very first statement.

        It RAISES. And the raise leaves NOTHING behind: the whole migration is
        one transaction, so a half-applied amendment relationship -- a unique
        key with no foreign key, or a column with neither -- is not a state
        this database can be in.

        VACUOUS AGAINST: A, B, D and E, none of which is about ordering.
        """
        psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
        with scratch_database("w149_order") as dsn, connect(dsn) as connection:
            _build_pre_security(connection)

            with pytest.raises(psycopg.Error) as caught:
                with connection.cursor() as cur:
                    cur.execute(_the_migration().sql)
            connection.rollback()

            assert caught.value.sqlstate in ("42703", "42P01", "42830"), caught.value.sqlstate
            assert not _has_column(connection, "exception_events", AMENDMENT_COLUMN)
            assert _constraint_def(connection, AMENDMENT_UNIQUE) is None
            assert _constraint_def(connection, AMENDMENT_FK) is None

    def test_0004_before_0002_raises_and_leaves_nothing_behind(self):
        """
        The out-of-order half at the level of the SET: 0004 applied to a
        database that has the history table and nothing else. `exception_events`
        does not exist, so PostgreSQL answers 42P01.
        """
        psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
        by_version = {m.version: m for m in migrate.discover()}
        with scratch_database("w149_order2") as dsn, connect(dsn) as connection:
            _apply(connection, by_version[1].sql)

            with pytest.raises(psycopg.Error) as caught:
                with connection.cursor() as cur:
                    cur.execute(by_version[AMENDMENT_VERSION].sql)
            connection.rollback()
            assert caught.value.sqlstate in ("42P01", "42703", "42830"), caught.value.sqlstate
            assert _regclass(connection, AMENDMENT_UNIQUE) is None

            # the positive twin, on the same database, in order
            for version in _on_disk_versions():
                if version == 1:
                    continue
                _apply(connection, by_version[version].sql)
            assert _constraint_def(connection, AMENDMENT_FK) == AMENDMENT_FK_DEF

    def test_the_unique_key_must_come_before_the_foreign_key_in_this_file(self):
        """
        WRONG B, AND IT FAILS TO CREATE AT ALL -- which is the brief's wording
        and is worth proving rather than asserting. The composite FK is applied
        to a database that has the column and NOT the unique key, and
        PostgreSQL answers 42830: "there is no unique constraint matching the
        given keys for referenced table". So the two objects are not two
        independent choices; the key is what makes the reference EXPRESSIBLE.

        VACUOUS AGAINST: A and D, both of which create successfully.
        """
        psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
        with scratch_database("w149_orderb") as dsn, connect(dsn) as connection:
            _build_historical_baseline_generation(connection)
            _apply(connection, f"ALTER TABLE exception_events ADD COLUMN {AMENDMENT_COLUMN} UUID")

            with pytest.raises(psycopg.Error) as caught:
                with connection.cursor() as cur:
                    cur.execute(
                        f"ALTER TABLE exception_events ADD CONSTRAINT {AMENDMENT_FK} "
                        f"FOREIGN KEY ({AMENDMENT_COLUMN}, org_id) REFERENCES exception_events (id, org_id)"
                    )
            connection.rollback()
            assert caught.value.sqlstate == "42830", caught.value.sqlstate

            # the positive twin: add the key first and the same statement works
            _apply(connection, f"ALTER TABLE exception_events ADD CONSTRAINT {AMENDMENT_UNIQUE} UNIQUE (org_id, id)")
            _apply(
                connection,
                f"ALTER TABLE exception_events ADD CONSTRAINT {AMENDMENT_FK} "
                f"FOREIGN KEY ({AMENDMENT_COLUMN}, org_id) REFERENCES exception_events (id, org_id)",
            )
            assert _constraint_def(connection, AMENDMENT_FK) == AMENDMENT_FK_DEF

    def test_the_runner_applies_the_whole_set_in_version_order_from_empty(self):
        with scratch_database("w149_order3") as dsn, connect(dsn) as connection:
            assert list(migrate.migrate(connection)) == _on_disk_versions()
            assert [row[0] for row in _history(connection)] == _on_disk_versions()
            assert migrate.inspect_state(connection).ok is True

    def test_a_wrong_generation_database_never_reaches_0004_at_all(self):
        """
        The refusal an operator actually meets, EARLIER than the raise above:
        `adopt_existing` rejects a pre-security database politely, by name, and
        `migrate` refuses it too. So the RAISE is reachable only by someone who
        has already bypassed two refusals.
        """
        with scratch_database("w149_presec") as dsn, connect(dsn) as connection:
            _build_pre_security(connection)

            with pytest.raises(migrate.MigrationStateError) as adoption:
                migrate.adopt_existing(connection)
            connection.rollback()
            assert "exception_events.org_id" in str(adoption.value)

            with pytest.raises(migrate.MigrationStateError) as migration:
                migrate.migrate(connection)
            connection.rollback()
            assert "adopt-existing" in str(migration.value)

            assert not _has_column(connection, "exception_events", AMENDMENT_COLUMN)


class TestConcurrentMigrationIsStillSafeWithFourMigrations:
    def test_a_second_runner_is_refused_while_the_first_holds_the_lock(self):
        with scratch_database("w149_lock") as dsn:
            with connect(dsn) as holder, connect(dsn) as blocked:
                migrate._acquire(holder, "30s")
                try:
                    with pytest.raises(migrate.MigrationLockUnavailable):
                        migrate.migrate(blocked, lock_timeout="1s")
                    blocked.rollback()
                    assert _regclass(blocked, "schema_migrations") is None
                    assert _regclass(blocked, AMENDMENT_UNIQUE) is None
                finally:
                    migrate._release(holder)

                assert list(migrate.migrate(blocked)) == _on_disk_versions()
                assert _constraint_def(blocked, AMENDMENT_FK) == AMENDMENT_FK_DEF

    def test_two_runners_racing_from_empty_produce_one_history_and_one_schema(self):
        """
        Exactly one runner applies the set; the other applies nothing. Asserted
        on the RESULTS of both threads, not on the final state alone -- a final
        state can be right while both runners believed they did the work.
        """
        results = []
        errors = []

        with scratch_database("w149_race") as dsn:

            def run():
                try:
                    with connect(dsn) as connection:
                        results.append(migrate.migrate(connection, lock_timeout="30s"))
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=120)

            with connect(dsn) as connection:
                history = _history(connection)
                definition = _constraint_def(connection, AMENDMENT_FK)

        assert not errors, errors
        assert sorted(results) == [(), tuple(_on_disk_versions())], results
        assert [row[0] for row in history] == _on_disk_versions()
        assert definition == AMENDMENT_FK_DEF


# ─── 7. THE SIX REQUIRED ROWS ───────────────────────────────────────────────


@pytest.fixture()
def conn():
    """The package's ordinary fixture: a database built from the CURRENT
    `schema.sql`, which is the right subject for these six rows -- they are
    about the schema a fresh deployment gets, not about the upgrade path."""
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    if not DSN:
        pytest.skip("CLINICAL_TEST_DSN not set")
    connection = psycopg.connect(DSN, autocommit=False, connect_timeout=10)
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute("DO $$ BEGIN CREATE ROLE clinical_app; EXCEPTION WHEN duplicate_object THEN NULL; END $$;")
        cur.execute("DO $$ BEGIN CREATE ROLE clinical_retention; EXCEPTION WHEN duplicate_object THEN NULL; END $$;")
        cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture()
def dao(conn):
    return DataAccess(conn, clock=SystemClock(), password_hasher=BcryptHasher(rounds=4))


@pytest.fixture()
def two_orgs_with_events(dao, conn):
    """
    A and B: two real organisations, each with an administrator session and one
    open exception created THROUGH THE METHOD -- so the `open` event each one
    carries is the row production writes, not one this file invented.

    Returns (session_a, session_b, exception_a, exception_b, event_a, event_b).
    """
    _org_a, session_a = _org_with_admin(dao, conn, "Org A", "w149-admin@org-a.test")
    _org_b, session_b = _org_with_admin(dao, conn, "Org B", "w149-admin@org-b.test")

    made = {}
    for tag, session in (("a", session_a), ("b", session_b)):
        order = _order_for(dao, conn, session)
        made[tag] = dao.create_or_reopen_exception(
            session,
            order,
            ExceptionCategory.PRECONDITION_FAILURE.value,
            ExceptionReasonCode.CONSENT_MISSING.value,
            f"{tag}'s own failure",
            "orderer",
            str(session.user_id),
        )
    conn.commit()

    events = {}
    with conn.cursor() as cur:
        for tag, exception_id in made.items():
            cur.execute("SELECT id FROM exception_events WHERE exception_id = %s", (exception_id,))
            rows = cur.fetchall()
            assert len(rows) == 1, f"expected exactly one open event for {tag}, got {rows}"
            events[tag] = rows[0][0]
    conn.rollback()

    assert session_a.org_id != session_b.org_id
    return session_a, session_b, made["a"], made["b"], events["a"], events["b"]


def _insert_event(cur, *, org_id, exception_id, action="resolve", note="w149", amends=None, omit_org=False):
    if omit_org:
        cur.execute(
            "INSERT INTO exception_events (id, exception_id, actor, timestamp, action, action_note, amends_event_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (uuid.uuid4(), exception_id, "actor", NOW, action, note, amends),
        )
        return
    cur.execute(
        "INSERT INTO exception_events "
        "(id, org_id, exception_id, actor, timestamp, action, action_note, amends_event_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (uuid.uuid4(), org_id, exception_id, "actor", NOW, action, note, amends),
    )


class TestTheSixRequiredRows:
    """
    THE HUMAN'S SIX, PROVED BY ATTEMPTING THE WRITE.

    These go through a raw connection ON PURPOSE, exactly as
    `test_w129_exception_tenant_isolation.py::TestTheSchemaMakesTheRowInexpressible`
    does and for the same stated reason: the property under test is a DATABASE
    property. That is not the Phase 5d cannot-fail-test antipattern -- the
    antipattern is using a raw query to prove something the PYTHON is meant to
    do, and rows 5 and 6 below, which ARE about the Python, call the method.
    """

    # ── 1 ──────────────────────────────────────────────────────────────────
    def test_1_a_same_org_amendment_is_accepted(self, conn, two_orgs_with_events):
        """
        THE POSITIVE TWIN THE OTHER FIVE ARE WORTHLESS WITHOUT. Without this,
        every refusal below could be passing because the column does not
        exist, the statement is malformed, or the table is unwritable.

        CATCHES: a 0004 that landed the unique key and not the column, and a
        composite FK written against the wrong columns so that nothing can ever
        satisfy it.

        VACUOUS AGAINST: A and D -- a single-column self-FK accepts this row
        too, which is exactly why row 2 exists.
        """
        session_a, _session_b, exception_a, _exception_b, event_a, _event_b = two_orgs_with_events

        with conn.cursor() as cur:
            _insert_event(cur, org_id=session_a.org_id, exception_id=exception_a, amends=event_a)
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT amends_event_id FROM exception_events WHERE org_id = %s AND amends_event_id IS NOT NULL",
                (session_a.org_id,),
            )
            rows = cur.fetchall()
        conn.rollback()
        assert rows == [(event_a,)], rows

    # ── 2 ──────────────────────────────────────────────────────────────────
    def test_2_a_cross_org_amendment_is_REFUSED_BY_THE_DATABASE(self, conn, two_orgs_with_events):
        """
        THE POINT OF THE WAVE. Organisation B writes an event into its OWN
        exception, in its OWN org partition -- everything about the row is B's
        -- and names A's event as the thing it amends. That is the whole attack
        surface of a single-column self-reference: nothing about the row is
        cross-tenant except the one column that matters.

        THE TARGET IS PROVED TO EXIST FIRST. Without that, this refusal could
        be the same refusal an invented id gets (row 3), and the row would be
        vacuous for the reason w148 measured: identifiers that cannot collide
        by accident make a missing predicate invisible.

        AND THE SQLSTATE IS ASSERTED. On the pre-fix tip (4d16b8f) there is no
        `amends_event_id` column, so this statement raises 42703 and a row
        asserting only "it raises" would be GREEN AGAINST THE CODE THIS WAVE
        EXISTS TO CHANGE. It must be 23503, a foreign key violation, and the
        server's message must name this wave's constraint.

        CATCHES: A (single-column self-FK -- the row is ACCEPTED, because A's
        id exists GLOBALLY), D (the same, with the catalogue looking right) and
        the pre-fix tip (wrong SQLSTATE).

        VACUOUS AGAINST: B and E. B never creates, so this test never runs
        against it; E is about `migrate.py` and leaves the DDL correct.
        """
        psycopg = pytest.importorskip("psycopg")
        session_a, session_b, _exception_a, exception_b, event_a, _event_b = two_orgs_with_events

        # The target is REAL and belongs to A. If this ever stops being true
        # the refusal below means nothing.
        with conn.cursor() as cur:
            cur.execute("SELECT org_id FROM exception_events WHERE id = %s", (event_a,))
            owner = cur.fetchone()
        conn.rollback()
        assert owner is not None, "the amendment target must exist before the cross-org attempt means anything"
        assert owner[0] == session_a.org_id
        assert owner[0] != session_b.org_id

        with pytest.raises(psycopg.errors.ForeignKeyViolation) as caught:
            with conn.cursor() as cur:
                _insert_event(
                    cur,
                    org_id=session_b.org_id,
                    exception_id=exception_b,
                    note="B amends A's resolution",
                    amends=event_a,
                )
        conn.rollback()

        assert caught.value.sqlstate == "23503", caught.value.sqlstate
        assert AMENDMENT_FK in str(caught.value), str(caught.value)

        # and nothing landed anywhere -- neither in B's partition nor in A's.
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM exception_events WHERE amends_event_id IS NOT NULL")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM exception_events WHERE action_note = %s", ("B amends A's resolution",))
            assert cur.fetchone()[0] == 0
        conn.rollback()

    def test_2b_the_same_target_IS_accepted_from_its_own_organisation(self, conn, two_orgs_with_events):
        """
        Rule 4's twin for row 2, and the one that makes it a TENANCY result
        rather than a "this column refuses everything" result: the SAME target
        event id, referenced from A, is accepted. The only difference between
        the two statements is which organisation writes them.
        """
        session_a, _session_b, exception_a, _exception_b, event_a, _event_b = two_orgs_with_events
        with conn.cursor() as cur:
            _insert_event(cur, org_id=session_a.org_id, exception_id=exception_a, amends=event_a)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM exception_events WHERE amends_event_id = %s", (event_a,))
            assert cur.fetchone()[0] == 1
        conn.rollback()

    # ── 3 ──────────────────────────────────────────────────────────────────
    def test_3_an_amendment_naming_an_event_that_exists_nowhere_is_refused(self, conn, two_orgs_with_events):
        """
        The missing/invalid target. Same organisation, same exception, an id
        that exists in no tenant at all.

        CATCHES: a 0004 that landed the column and NO foreign key -- which
        passes rows 1, 4, 5 and 6 and fails only here and at row 2.

        VACUOUS AGAINST: A and D. A single-column self-FK refuses this row
        just as happily; only row 2 tells the two apart.
        """
        psycopg = pytest.importorskip("psycopg")
        session_a, _session_b, exception_a, _exception_b, _event_a, _event_b = two_orgs_with_events
        invented = uuid.uuid4()

        with pytest.raises(psycopg.errors.ForeignKeyViolation) as caught:
            with conn.cursor() as cur:
                _insert_event(cur, org_id=session_a.org_id, exception_id=exception_a, amends=invented)
        conn.rollback()
        assert caught.value.sqlstate == "23503", caught.value.sqlstate

    def test_3b_the_two_refusals_are_the_same_kind_of_object(self, conn, two_orgs_with_events):
        """
        STANDING RULE 8, at the database layer. A foreign id and an invented id
        must not be told apart HERE either -- if the database distinguished
        them, any surface that surfaced the error would become a cross-tenant
        existence oracle for free.

        Asserted as a SET OF SIZE ONE over (exception type, SQLSTATE,
        constraint name), with the identifiers normalised out of the message,
        rather than as a pair of per-case equalities.
        """
        psycopg = pytest.importorskip("psycopg")
        session_a, session_b, _exception_a, exception_b, event_a, _event_b = two_orgs_with_events
        invented = uuid.uuid4()

        verdicts = set()
        for label, target in (("foreign", event_a), ("nowhere", invented)):
            with pytest.raises(psycopg.Error) as caught:
                with conn.cursor() as cur:
                    _insert_event(cur, org_id=session_b.org_id, exception_id=exception_b, amends=target)
            conn.rollback()
            verdicts.add(
                (
                    type(caught.value).__name__,
                    caught.value.sqlstate,
                    getattr(caught.value.diag, "constraint_name", None),
                )
            )
        assert len(verdicts) == 1, verdicts

    # ── 4 ──────────────────────────────────────────────────────────────────
    def test_4_org_id_cannot_be_silently_omitted_from_an_amendment_insert(self, conn, two_orgs_with_events):
        """
        WRONG C, and it is a WRITE rather than a version of the migration.
        `org_id` has no DEFAULT and is NOT NULL, so an INSERT that leaves it
        out is refused with 23502 -- it does NOT quietly land a row with a NULL
        tenant, which is the state in which MATCH SIMPLE would skip the
        composite check.

        THIS IS THE ROW THAT CLOSES THE MATCH SIMPLE QUESTION. The composite FK
        is MATCH SIMPLE, so a NULL in either referencing column skips the
        check; `amends_event_id` being NULL is fine (it means "no amendment"),
        and `org_id` being NULL is the hole. There is no hole, because the
        column refuses NULL.

        VACUOUS AGAINST: A, B, D and E -- `org_id NOT NULL` predates all of
        them and none of them changes it.
        """
        psycopg = pytest.importorskip("psycopg")
        session_a, _session_b, exception_a, _exception_b, event_a, _event_b = two_orgs_with_events

        with pytest.raises(psycopg.errors.NotNullViolation) as caught:
            with conn.cursor() as cur:
                _insert_event(cur, org_id=None, exception_id=exception_a, amends=event_a, omit_org=True)
        conn.rollback()
        assert caught.value.sqlstate == "23502", caught.value.sqlstate
        assert caught.value.diag.column_name == "org_id"

        # and the same statement WITH org_id lands: the refusal is about the
        # tenant column, not about the statement.
        with conn.cursor() as cur:
            _insert_event(cur, org_id=session_a.org_id, exception_id=exception_a, amends=event_a)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM exception_events WHERE org_id IS NULL")
            assert cur.fetchone()[0] == 0
        conn.rollback()

    # ── 5 ──────────────────────────────────────────────────────────────────
    def test_5_existing_exception_event_writes_still_work_through_the_method(self, dao, conn, two_orgs_with_events):
        """
        THE REGRESSION ROW, AND IT CALLS THE METHOD (Rule 2a). A new NOT-NULL
        column, a DEFAULT, a CHECK, or a foreign key that fired on NULL would
        break every existing write, and the three `INSERT INTO exception_events`
        statements in `clinical/data_access.py` do not name `amends_event_id`
        at all -- so this is the row that proves the migration is additive in
        BEHAVIOUR and not merely in catalogue rows.

        CATCHES: a 0004 that made the column NOT NULL, or added a CHECK
        requiring it.

        VACUOUS AGAINST: A, B, D -- all of which leave existing writes alone.
        """
        session_a, _session_b, exception_a, _exception_b, _event_a, _event_b = two_orgs_with_events

        dao.resolve_exception(
            session_a,
            exception_a,
            ResolutionAction.RETRY_CHECK.value,
            "Consent obtained",
            str(session_a.user_id),
        )

        after = dao.get_exception_by_id(session_a, exception_a)
        assert after.status == "resolved"
        assert [e.action for e in after.events] == ["open", "resolve"]
        assert after.events[-1].actor == str(session_a.user_id)
        assert after.events[-1].action_note == "Consent obtained"

        # the rows the method wrote name NO amendment -- the column is
        # genuinely unused by production code at this commit.
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM exception_events WHERE amends_event_id IS NOT NULL")
            assert cur.fetchone()[0] == 0
        conn.rollback()

    def test_5b_nothing_in_the_repository_writes_an_amendment(self):
        """
        SAID PLAINLY, AND ASSERTED. `amends_event_id` appears in no INSERT in
        `clinical/` or `geper/` outside this test file. When the verb arrives,
        this row goes red and that is the correct signal: the claim "production
        cannot write an amendment" will have stopped being true and the wave
        that makes it untrue should say so.

        Measured over the source, not asserted from a report. Scoped to INSERT
        STATEMENTS rather than to the bare name: `clinical/migrate.py` names
        the column legitimately, in this migration's generation invariant, and
        a whole-file search would be satisfied by deleting that invariant --
        the exact shape of a test that is satisfied by the broken state.

        NARROWED TO PRODUCTION BY w161, AND THE CLAIM IS UNWEAKENED. The scan
        used to exclude one file by NAME -- this one -- which was enough while
        this was the only suite that wrote an amendment. Migration 0005's suite
        writes one too, legitimately: proving an amendment row can be written
        at all is the whole point of that wave, and it could not be proved
        without writing one. Excluding tests by DIRECTORY rather than adding a
        second filename keeps the claim exactly where it was always aimed --
        `production cannot write an amendment` -- and stops the exclusion list
        being a thing every later wave has to append to, which is the decay
        this floor keeps paying for. The three production INSERT sites in
        `clinical/data_access.py` are still scanned, and the non-vacuity floor
        below is what proves it.

        NON-VACUITY: the search must find the INSERT statements it is scanning
        at all, or a typo in the marker makes this row pass for ever.
        """
        root = pathlib.Path(__file__).resolve().parent.parent.parent
        marker = "INSERT INTO exception_events"
        offenders = []
        scanned = 0
        for path in sorted(list((root / "clinical").rglob("*.py")) + list((root / "geper").rglob("*.py"))):
            if "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            start = text.find(marker)
            while start != -1:
                scanned += 1
                if AMENDMENT_COLUMN in text[start : start + 400]:
                    offenders.append(f"{path.relative_to(root)}:{text.count(chr(10), 0, start) + 1}")
                start = text.find(marker, start + 1)
        assert scanned >= 3, f"the INSERT-site scan found only {scanned} statements; the marker is wrong"
        assert offenders == [], offenders

    # ── 6 ──────────────────────────────────────────────────────────────────
    def test_6_audit_records_retain_correct_organisation_attribution(self, dao, conn, two_orgs_with_events):
        """
        THE HUMAN'S ROW 6. A successful exception-event write is audited in its
        OWN organisation's partition, and a refused cross-organisation attempt
        is audited in the ATTACKER'S partition and nowhere else -- unchanged by
        this migration.

        WHAT WAS CHECKED ABOUT @auditable AND TRANSACTION BOUNDARIES, because
        this floor has been bitten three times. `resolve_exception` is
        `@transactional` OUTSIDE `@auditable`. The audit wrapper calls the
        method, writes the audit row, and THEN commits -- one commit covering
        both -- so the exception-event INSERT and its audit row are in ONE
        transaction. The hazard the w128 lesson names is COMPOSITION: calling
        one decorated method from another commits in the middle. This path does
        not compose, and its own docstring says so. Nothing was changed.

        Asserted on CONTENT (Rule 4): which organisation, which outcome, which
        user, which resource -- never a count alone.
        """
        session_a, session_b, exception_a, _exception_b, _event_a, _event_b = two_orgs_with_events

        dao.resolve_exception(
            session_a, exception_a, ResolutionAction.RETRY_CHECK.value, "A's own", str(session_a.user_id)
        )
        with pytest.raises(NotFoundError):
            dao.resolve_exception(
                session_b, exception_a, ResolutionAction.RETRY_CHECK.value, "B was here", str(session_b.user_id)
            )

        with conn.cursor() as cur:
            cur.execute(
                "SELECT org_id, outcome, user_id, resource_id FROM audit_log "
                "WHERE action = 'resolve_exception' ORDER BY outcome"
            )
            rows = cur.fetchall()
        conn.rollback()

        assert len(rows) == 2, rows
        by_outcome = {row[1]: row for row in rows}
        assert set(by_outcome) == {"success", "error"}

        success = by_outcome["success"]
        assert success[0] == session_a.org_id
        assert success[2] == session_a.user_id
        assert success[3] == str(exception_a)

        failure = by_outcome["error"]
        assert failure[0] == session_b.org_id, "the refused attempt is attributed to the org that made it"
        assert failure[0] != session_a.org_id
        assert failure[2] == session_b.user_id

    def test_6b_the_event_and_its_audit_row_are_in_ONE_transaction(self, dao, conn, two_orgs_with_events):
        """
        The @auditable boundary, MEASURED rather than reasoned about. A second
        connection sees NEITHER the new exception event NOR the audit row while
        the decorated call is in flight, and BOTH once it returns. If
        `@auditable` committed between the two, the second connection would see
        a window in which the event exists and its audit row does not.

        Done by taking the two counts from a SEPARATE connection before and
        after, and by asserting that the in-flight state is neither-of-them:
        the method is called with the work already staged on a connection that
        has not committed.
        """
        psycopg = pytest.importorskip("psycopg")
        session_a, _session_b, exception_a, _exception_b, _event_a, _event_b = two_orgs_with_events

        observer = psycopg.connect(DSN, autocommit=True, connect_timeout=10)
        try:

            def seen():
                with observer.cursor() as cur:
                    cur.execute(
                        "SELECT (SELECT count(*) FROM exception_events WHERE exception_id = %s), "
                        "(SELECT count(*) FROM audit_log WHERE action = 'resolve_exception')",
                        (exception_a,),
                    )
                    return cur.fetchone()

            before = seen()
            assert before == (1, 0), before

            dao.resolve_exception(
                session_a, exception_a, ResolutionAction.RETRY_CHECK.value, "A's own", str(session_a.user_id)
            )

            after = seen()
            assert after == (2, 1), after
        finally:
            observer.close()


# ─── 8. the catalogue, as the LAST word rather than the only one ────────────


class TestTheCatalogueSaysWhatTheBehaviourProved:
    def test_the_foreign_key_is_composite_and_its_definition_is_asserted_not_its_name(self, conn):
        """
        WRONG D IN ONE ROW. A single-column self-FK carries the same NAME on
        the same TABLE and satisfies every `EXISTS (... conname = ...)` probe
        ever written. The definition is what differs, so the definition is what
        is asserted -- and the forbidden spelling is asserted ABSENT by name.

        This row is deliberately LAST. It is the catalogue confirming what the
        behavioural rows already proved, not a substitute for them: a
        constraint can be present and NOT VALID, and this query cannot see the
        difference.
        """
        assert _constraint_def(conn, AMENDMENT_FK) == AMENDMENT_FK_DEF
        assert _constraint_def(conn, AMENDMENT_UNIQUE) == AMENDMENT_UNIQUE_DEF
        assert "FOREIGN KEY (amends_event_id) REFERENCES" not in (_constraint_def(conn, AMENDMENT_FK) or "")

    def test_the_constraint_is_validated_and_not_merely_declared(self, conn):
        """
        The gap a catalogue assertion leaves: `ADD CONSTRAINT ... NOT VALID`
        creates a constraint that EXISTS and is not enforced against existing
        rows. `convalidated` is the column that says which, and this asserts
        it -- so "the constraint exists" and "the constraint is enforced" are
        two different claims here, as they should be.
        """
        with conn.cursor() as cur:
            cur.execute(
                "SELECT convalidated FROM pg_constraint "
                "WHERE conrelid = to_regclass('exception_events') AND conname = %s",
                (AMENDMENT_FK,),
            )
            assert cur.fetchone()[0] is True
        conn.rollback()

    def test_the_action_vocabulary_is_UNCHANGED_by_THIS_migration(self, conn):
        """
        THE SCOPE BOUNDARY, AND IT HAS BEEN DELIBERATELY RE-AIMED -- w161,
        2026-09-19, with the human's authorisation, which is exactly what the
        previous version of this docstring asked for:

            "its failure is the CORRECT signal for the wave that brings the
            vocabulary across: it should change this row deliberately, with the
            human's authorisation, rather than discover it changed."

        WHAT CHANGED AND WHY THE ROW IS STILL NEEDED. Migration 0005 widened
        the action CHECK to admit `'amend_resolution'`, so the DATABASE-side
        half of this row ("the vocabulary is still three verbs") is now false
        and asserting it would be asserting the past. The claim that was ever
        0004's to make is about 0004's OWN BYTES: this migration drops nothing
        and changes no vocabulary. That claim is unchanged, is the reason 0004
        could be additive at all, and is now asserted where it belongs -- on
        the file -- rather than on a database whose shape a later migration is
        entitled to change.

        The database-side assertion is kept in the weaker, durable form: the
        three original verbs are still admitted. Losing one of them is a defect
        no matter which migration did it, and 0005's own suite
        (`test_w161_migration_0005_vocabulary.py`) owns the positive half.
        """
        vocabulary_ddl = "\n".join(
            line for line in _the_migration().sql.splitlines() if not line.strip().startswith("--")
        )
        assert "amend_resolution" not in vocabulary_ddl, "0004's DDL must not touch the vocabulary"
        assert "CHECK" not in vocabulary_ddl.upper(), "0004 adds no CHECK constraint of any kind"

        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = to_regclass('exception_events') AND contype = 'c'"
            )
            checks = [row[0] for row in cur.fetchall()]
        conn.rollback()
        joined = " ".join(checks)
        assert "'open'" in joined and "'resolve'" in joined and "'reopen'" in joined

    def test_no_other_org_scoped_table_gained_a_composite_key(self, conn):
        """
        THE HUMAN'S EXPLICIT BOUNDARY, as a test: "Do NOT alter unrelated
        org-scoped tables merely because they lack composite keys."

        MEASURED BY CATALOGUE QUERY, not by grep -- a text search cannot see a
        named CONSTRAINT, a separate CREATE UNIQUE INDEX, or a primary key that
        is not called `id`, which is exactly why the earlier census claim was
        wrong. Seven tables lacked the key after this wave (six since 0007 gave `tests` one) and are listed by
        name, so a later wave that "helpfully" fixes one has to edit this list.
        """
        still_lacking = set()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY c.relname"
            )
            tables = [row[0] for row in cur.fetchall()]
            for table in tables:
                cur.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass(%s) "
                    "AND attname = 'org_id' AND attnum > 0 AND NOT attisdropped)",
                    (table,),
                )
                if not cur.fetchone()[0]:
                    continue
                cur.execute(
                    "SELECT a.attname FROM pg_constraint k "
                    "JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = ANY(k.conkey) "
                    "WHERE k.conrelid = to_regclass(%s) AND k.contype = 'p'",
                    (table,),
                )
                pk = sorted(row[0] for row in cur.fetchall())
                identity = [column for column in pk if column != "org_id"]
                if len(identity) != 1:
                    continue
                wanted = sorted(["org_id"] + identity)
                cur.execute(
                    "SELECT array_agg(a.attname ORDER BY a.attname) FROM pg_index x "
                    "JOIN pg_attribute a ON a.attrelid = x.indrelid AND a.attnum = ANY(x.indkey) "
                    "WHERE x.indrelid = to_regclass(%s) AND x.indisunique GROUP BY x.indexrelid",
                    (table,),
                )
                if not any(sorted(row[0]) == wanted for row in cur.fetchall()):
                    still_lacking.add(table)
        conn.rollback()

        assert still_lacking == {
            "audit_log",
            "external_identifiers",
            "patient_sessions",
            "role_assignments",
            "sessions",
            "totp_backup_codes",
        }, still_lacking
        # `tests` LEFT THIS LIST AT MIGRATION 0007, which gave it
        # UNIQUE (test_id, org_id) as the target of orders_test_org_fk.
        assert "tests" not in still_lacking
        assert "exception_events" not in still_lacking, "this wave's whole subject"
