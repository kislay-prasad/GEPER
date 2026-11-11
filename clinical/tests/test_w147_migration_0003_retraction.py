"""
clinical/tests/test_w147_migration_0003_retraction.py
──────────────────────────────────────────────────────
MIGRATION 0003 -- THE FIRST REAL THIRD MIGRATION, AND THE FIRST REAL TEST OF
THE GENERATION-INVARIANT LIMITATION w146 WROTE DOWN.

`test_w146_generation_survives_a_third_migration.py` demonstrated six-state
discrimination with a THROWAWAY three-migration set in `tmp_path`, and said
plainly what it could not see: `/readyz` calls `migrate.discover()` itself and
cannot be handed a pinned set, so a throwaway set never reaches the probe.
`clinical/migrations/0003_report_retraction.sql` is the first set member that
is really on disk, so the rows here are the ones that run through the real
`discover()` -- the real file, the real checksum, the real stamp in
`clinical/schema.sql`, and the real `(3, "report_retraction")` key in
`_GENERATION_INVARIANTS`.

WHAT THIS FILE DOES NOT TEST, AND WHY THAT IS NOT AN OMISSION. 0003 lands
SCHEMA. The `retract_report` verb, the report builder's retraction banners and
the patient-facing `is_retracted` flag are NOT in this repository at this
commit, so nothing here writes a retraction, reads one back, or asserts
anything about what a patient is shown. Two rows in
`test_w135_patient_report_access.py` and `test_w136_trends_retraction.py` go
RED the moment these tables appear; that is their designed behaviour and they
were left standing rather than weakened -- see this wave's report.

A THIRD ROW GOES RED AND IT IS NOT A TRIPWIRE, IT IS A BLOCKER THIS WAVE MAY
NOT CLEAR. `test_identity.py::test_org_scoped_table_coverage_is_derived_from_
the_schema` derives the org-owned table set FROM `clinical/schema.sql` and
checks it against `clinical/data_access.py::ORG_SCOPED_TABLES`. Both new
tables carry `org_id`, so neither may be exempted -- the interlock in
`test_only_a_table_without_an_org_id_column_may_be_exempt` permits an
exemption only where the column genuinely does not exist, which is the route
`schema_migrations` legitimately took in w126m and the route these two cannot.
The fix is two string literals in `ORG_SCOPED_TABLES`, and
`clinical/data_access.py` is an APPLICATION FILE, which this wave is forbidden
to touch. Measured, so the size of the blocker is on the record rather than
guessed: `ORG_SCOPED_TABLES` has no runtime consumer anywhere in `clinical/`
or `geper/` -- its only readers are `test_identity.py` and
`test_patient_identity.py` -- so adding the two names changes no behaviour.
The wave that brings `retract_report` across owns that file and should do it
there. The row is left RED, naming the two tables, deliberately.

THE WRONG VERSIONS THIS FILE WAS BUILT AGAINST. Each row below names the one
it catches AND the one it is vacuous against; these are the five the brief
enumerates:

  A  0003 adds the objects but declares NO generation invariant
  B  the invariants are written against `information_schema`, not `pg_catalog`
  C  an "upgrade" test that passes because the fixture rebuilt the database
  D  0003 applied before 0002
  E  a privilege statement omitted, so `clinical_app` cannot read the new table

THESE TESTS CREATE THEIR OWN DATABASES (`clinical/tests/migration_db.py`) and
never touch the package's drop-and-recreate fixtures, for the reason that
module's docstring gives at length: a fixture that rebuilds from the current
`schema.sql` destroys the one property an upgrade test is about.
"""

from __future__ import annotations

import pathlib
import threading

import pytest

from clinical import migrate
from clinical.tests.migration_db import (
    SENTINEL_NOTE,
    SENTINEL_ORG,
    connect,
    plant_sentinel,
    read_sentinel,
    scratch_database,
)
from clinical.tests.test_w126m_schema_equivalence import catalog_of, describe_difference

SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / "schema.sql"
PRE_SECURITY_PATH = pathlib.Path(__file__).resolve().parent / "fixtures" / "pre_security_schema_da97f847.sql"

RETRACTION_VERSION = 3
RETRACTION_NAME = "report_retraction"

# The object set 0003 is FOR, written out here rather than derived from the
# migration file: a test that reads its expectations out of the file it is
# testing agrees with that file by construction. Same reasoning as
# `test_privilege_boundary.py`'s explicit literals.
RETRACTION_TABLES = ("report_retractions", "retraction_notifications")
RETRACTION_INDEXES = ("idx_retraction_org_report", "idx_retraction_notification_org_retraction")


# ─── helpers ────────────────────────────────────────────────────────────────


def _apply(connection, sql: str) -> None:
    with connection.cursor() as cur:
        cur.execute(sql)
    connection.commit()


def _build_fresh_from_schema_sql(connection) -> None:
    """The production fresh-database path. It stamps its own history."""
    _apply(connection, SCHEMA_PATH.read_text(encoding="utf-8"))


def _build_historical_baseline_generation(connection) -> None:
    """
    A REAL pre-mechanism database of the BASELINE generation: the frozen
    `0002_baseline.sql` bytes, applied for real, with no history table. This
    is the state `--adopt-existing` exists for and the only historical state
    that can legitimately reach 0003.

    NOT a database rebuilt from the current `schema.sql` with something
    removed: that file now CONTAINS the retraction objects, so building from
    it would hand the upgrade its own answer.
    """
    baseline = {m.version: m for m in migrate.discover()}[migrate.BASELINE_VERSION]
    _apply(connection, baseline.sql)


def _build_pre_security(connection) -> None:
    """The real EARLIER generation -- `7b034e5:clinical/schema.sql`, blob
    da97f847, byte for byte."""
    _apply(connection, PRE_SECURITY_PATH.read_text(encoding="utf-8"))


def _regclass(connection, name: str):
    with connection.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (name,))
        return cur.fetchone()[0]


def _history(connection) -> tuple:
    with connection.cursor() as cur:
        cur.execute("SELECT version, name, checksum FROM schema_migrations ORDER BY version")
        return tuple(cur.fetchall())


def _table_census(cur) -> tuple:
    """How many public tables the CURRENT role can see through each view.
    `information_schema` is privilege-filtered; `pg_class` is not."""
    cur.execute("SELECT count(DISTINCT table_name) FROM information_schema.columns WHERE table_schema = 'public'")
    via_information_schema = cur.fetchone()[0]
    cur.execute(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r'"
    )
    return via_information_schema, cur.fetchone()[0]


def _the_migration() -> migrate.Migration:
    by_version = {m.version: m for m in migrate.discover()}
    assert RETRACTION_VERSION in by_version, "migration 0003 is not on disk"
    return by_version[RETRACTION_VERSION]


def _on_disk_versions() -> list:
    """
    The migration set, DERIVED (w149).

    This file was written when 0003 was the last migration and wrote `[1, 2,
    RETRACTION_VERSION]` in nine places. That is the exact defect w146 swept
    the suite for and w147 re-introduced in its own file: a hard-coded set that
    every later wave must re-author. 0004 is the wave that had to. Every such
    literal below now derives from `discover()`, which is STRICTLY STRONGER --
    it asserts the history matches the files on disk rather than matching a
    number somebody typed -- and the facts that are genuinely ABOUT 0003
    (that it is on disk, that it is version 3, that it is named
    `report_retraction`) are asserted directly and are unchanged.
    """
    return [m.version for m in migrate.discover()]


def _versions_after_baseline() -> tuple:
    return tuple(v for v in _on_disk_versions() if v > migrate.BASELINE_VERSION)


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
    def test_the_set_is_one_two_three_in_that_order_and_by_integer(self):
        """
        CATCHES: D at the level of the SET -- a runner that hands the caller
        0003 before 0002, or a file named so that it sorts wrong.
        `discover` parses the integer out of the name; this row asserts the
        order it returns, not the order of a directory listing.

        VACUOUS AGAINST: A, B, C and E -- it reads filenames and never opens a
        database. The row that proves ordering has teeth is
        `test_0003_before_0002_raises_rather_than_producing_a_wrong_schema`.
        """
        discovered = migrate.discover()
        versions = [m.version for m in discovered]
        assert versions == sorted(versions)
        assert versions == list(range(1, len(versions) + 1)), versions
        names = {m.version: m.name for m in discovered}
        assert names[1] == "schema_migrations"
        assert names[migrate.BASELINE_VERSION] == "baseline"
        assert names[RETRACTION_VERSION] == RETRACTION_NAME

    def test_the_frozen_baseline_and_its_stamp_are_untouched(self):
        """
        CATCHES: a third migration landed by editing the baseline instead of
        adding a file -- which would change `0002_baseline.sql`'s checksum,
        break the stamp `schema.sql` carries, and make every already-adopted
        database raise MigrationChecksumMismatch on its next run.

        Asserted by CONTENT against the value `schema.sql` records, read out of
        the file, so it cannot pass because both sides were changed together in
        the same way an automatic derivation would.

        VACUOUS AGAINST: everything 0003 actually does.
        """
        by_version = {m.version: m for m in migrate.discover()}
        baseline = by_version[migrate.BASELINE_VERSION]
        assert baseline.checksum == "cc842c3943b7aa1e5786d638eff742f63fd47ce5a5048d7321036234a5290af8"
        assert by_version[1].checksum == "00d8c90bb0eaba561d843cde7a45eca5af4d594aca6a3e230938aef27286f559"

        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        assert f"(2, 'baseline',          '{baseline.checksum}')" in schema
        assert f"(3, '{RETRACTION_NAME}', '{by_version[RETRACTION_VERSION].checksum}')" in schema

    def test_0003_declares_generation_invariants_and_they_are_not_all_table_probes(self):
        """
        CATCHES: WRONG A, at the level of the declaration -- a third migration
        that adds objects and declares nothing in the MAP. Keyed by
        `(version, name)`, so this row also fails if the file is renamed
        without the key moving with it.

        THE LIMITATION w146 WROTE DOWN IS GONE, and the quotation that used to
        stand here ("the list is hand-maintained and nothing forces a new
        migration to declare one") is no longer true of this tree. The human
        ruled on 2026-09-19 that every migration must declare at least one
        checkable post-condition, and `clinical/migrate.py::_refuse_undeclared`
        now enforces it in `migrate`, `adopt_existing` and `inspect_state`.
        This row survives the ruling because it is stricter than it: the
        ruling is satisfied by 0003's four `MIGRATION-ASSERT` header lines
        alone, and those are the bare `to_regclass` vocabulary w143 measured to
        be blind across generations. What this row demands is the column and
        constraint probes, which only the map can carry.

        NON-VACUITY: a set of table probes alone would be exactly the
        vocabulary w143 measured to be blind, so the column and constraint
        kinds are asserted present and the table probes asserted outnumbered.

        VACUOUS AGAINST: B -- an `information_schema` invariant of the right
        KIND passes this row. That is the next one.
        """
        invariants = migrate.generation_invariants(_the_migration())
        assert invariants, "migration 0003 declares no generation invariants"
        assert invariants == migrate.RETRACTION_GENERATION_INVARIANTS

        kinds = {i.kind for i in invariants}
        assert "column" in kinds
        assert "constraint" in kinds
        assert sum(1 for i in invariants if i.kind == "table") < sum(
            1 for i in invariants if i.kind in ("column", "constraint")
        )

    def test_the_migrations_ddl_is_the_same_text_schema_sql_carries(self):
        """
        ADDITIVITY AND AGREEMENT, MADE EXECUTABLE RATHER THAN REPORTED. The
        DDL in 0003 and the retraction text in `clinical/schema.sql` came from
        the same four blocks lifted from the held branch, so every line of the
        migration below its header must appear in `schema.sql` IN THE SAME
        ORDER. (Not contiguously: `schema.sql` interleaves them with the
        surrounding table and grant blocks, which is the whole reason the
        derivation placed four blocks and not one.)

        CATCHES: an edit to one side and not the other -- the drift the
        equivalence proof catches only once a database has been built twice,
        caught here from the files alone and named line by line.

        NON-VACUITY: the body is asserted non-trivial (it contains both CREATE
        TABLE statements), so a header-parsing bug that left it empty fails
        here instead of passing.

        VACUOUS AGAINST: everything semantic. Two files can agree perfectly and
        both be wrong; that is what sections 2-8 are for.
        """
        migration_lines = _the_migration().sql.split("\n")
        start = max(i for i, line in enumerate(migration_lines) if line.startswith("-- MIGRATION-ASSERT:")) + 1
        body = [line for line in migration_lines[start:] if line.strip()]
        assert sum(1 for line in body if line.startswith("CREATE TABLE ")) == 2, body[:5]

        schema_lines = [line for line in SCHEMA_PATH.read_text(encoding="utf-8").split("\n") if line.strip()]
        cursor = 0
        for line in body:
            while cursor < len(schema_lines) and schema_lines[cursor] != line:
                cursor += 1
            assert cursor < len(schema_lines), (
                "clinical/migrations/0003_report_retraction.sql and clinical/schema.sql have "
                f"drifted: this line of the migration is not in schema.sql, in order: {line!r}"
            )
            cursor += 1

    def test_no_invariant_of_0003_reads_information_schema(self):
        """
        CATCHES: WRONG B as a TEXT property, which is the half that holds even
        on a database where the two views happen to agree.

        VACUOUS AGAINST: an invariant that reads pg_catalog and is simply
        WRONG. That is what the catalog rows below are for.
        """
        for invariant in migrate.generation_invariants(_the_migration()):
            assert "information_schema" not in invariant.expression
            assert "pg_" in invariant.expression


# ─── 2. fresh creation ──────────────────────────────────────────────────────


class TestAFreshDatabaseHasTheRetractionObjectsAndSaysItIsAtThree:
    def test_schema_sql_creates_the_objects_and_stamps_version_three(self, client, monkeypatch):
        """
        FRESH CREATION, the production path. `clinical/bootstrap.py
        --apply-schema` sends this whole file as one statement in one
        transaction, and the stamp lands with the tables or neither lands.

        CATCHES: the DDL added to `schema.sql` without its stamp (the database
        would report "3 pending" for ever) and the stamp added without the DDL
        (readiness would answer schema_drift). Both halves asserted, plus the
        probe, because a library verdict is not what a deploy reads.

        VACUOUS AGAINST: C and D -- nothing is upgraded and nothing is ordered
        here.
        """
        with scratch_database("w147_fresh") as dsn, connect(dsn) as connection:
            _build_fresh_from_schema_sql(connection)

            for name in RETRACTION_TABLES + RETRACTION_INDEXES:
                assert _regclass(connection, name) is not None, name

            assert [row[0] for row in _history(connection)] == _on_disk_versions()
            state = migrate.inspect_state(connection)
            assert state.ok is True, state.detail
            assert state.pending == ()
            assert state.current_version == max(_on_disk_versions())

            assert migrate.migrate(connection) == ()

            status, reason, text = _probe(client, monkeypatch, dsn)

        assert status == 200, text
        assert reason is None


# ─── 3. historical upgrade, with a sentinel a rebuild destroys ──────────────


class TestARealHistoricalDatabaseIsUpgradedRatherThanRebuilt:
    def test_a_pre_mechanism_baseline_database_is_adopted_then_reaches_three(self):
        """
        THE UPGRADE, and the row WRONG C is aimed at. The starting database is
        built from the frozen `0002_baseline.sql` bytes -- a real
        pre-mechanism database -- and then carries DATA that a drop-and-recreate
        cannot bring across.

        CATCHES: C. A "migration" that rebuilds from the current `schema.sql`
        produces every object this row asks for and LOSES the sentinel, which
        is asserted BY CONTENT (the exact organisation name and the exact note
        string), never by count.

        Paired with the positive half in the same row, because a sentinel-only
        assertion passes against a runner that does nothing at all: the two
        retraction tables and both indexes must be present afterwards, and the
        history must say 3.

        VACUOUS AGAINST: A and B -- the generation verdict is not exercised by
        a database that is genuinely at the right generation. Those are
        section 5.
        """
        with scratch_database("w147_hist") as dsn, connect(dsn) as connection:
            _build_historical_baseline_generation(connection)
            org_id = plant_sentinel(connection)
            assert org_id

            for name in RETRACTION_TABLES:
                assert _regclass(connection, name) is None, f"{name} exists BEFORE the upgrade"

            assert migrate.adopt_existing(connection) == (1, migrate.BASELINE_VERSION)
            assert migrate.migrate(connection) == _versions_after_baseline()

            # the sentinel, by content -- a rebuild loses both halves
            assert read_sentinel(connection) == ((SENTINEL_NOTE,), (SENTINEL_ORG,))

            # ... and the positive twin, because surviving a no-op is easy
            for name in RETRACTION_TABLES + RETRACTION_INDEXES:
                assert _regclass(connection, name) is not None, name

            assert [row[0] for row in _history(connection)] == _on_disk_versions()
            state = migrate.inspect_state(connection)
            assert state.ok is True, state.detail
            assert state.current_version == max(_on_disk_versions())

    def test_the_sentinel_really_does_die_in_a_rebuild(self):
        """
        THE NON-VACUITY OF WRONG C ITSELF. The row above is only evidence if a
        rebuild would actually have failed it. So: plant the sentinel, then do
        the wrong thing on purpose -- drop the schema and rebuild from
        `schema.sql` -- and require the sentinel to be GONE while every object
        the upgrade row checks is PRESENT.

        That combination is the whole point: the wrong version satisfies every
        object assertion and fails on the data.
        """
        with scratch_database("w147_rebuild") as dsn, connect(dsn) as connection:
            _build_historical_baseline_generation(connection)
            plant_sentinel(connection)
            assert read_sentinel(connection) == ((SENTINEL_NOTE,), (SENTINEL_ORG,))

            _apply(connection, "DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
            _build_fresh_from_schema_sql(connection)

            for name in RETRACTION_TABLES + RETRACTION_INDEXES:
                assert _regclass(connection, name) is not None, name
            assert read_sentinel(connection) == ((), ())


# ─── 4. convergence: the two paths, compared as CATALOGS ────────────────────


class TestFreshCreationAndHistoricalUpgradeConverge:
    def test_the_two_catalogs_are_identical(self):
        """
        THE HUMAN'S REQUIREMENT: "Ensure fresh database creation and historical
        upgrade converge to the same intended schema." Proved by COMPARING THE
        TWO CATALOGS -- nine sections, every row -- and not by running both
        databases past the same probe list, which is satisfied by two databases
        that differ in anything the list does not mention.

        This is a DIFFERENT pair from
        `test_w126m_schema_equivalence.py::test_schema_sql_and_the_migration_set_produce_the_same_catalog`,
        which compares schema.sql against the migrations run from EMPTY. This
        one's right-hand side is a database that existed before the mechanism,
        was adopted, and was then upgraded -- the path a deployed database
        actually takes, and the only one on which `adopt_existing`'s stamp of
        the baseline (rather than its execution) is in play.

        CATCHES: a 0003 whose DDL differs in any way from the retraction text
        added to `schema.sql` -- a privilege left out of one side (which is
        WRONG E, arriving through the catalog rather than through a role), a
        constraint spelled differently, an index on different columns.

        VACUOUS AGAINST: the history. Two identical catalogs can carry
        different stamps; that is asserted separately below.
        """
        with (
            scratch_database("w147_fresh") as dsn_fresh,
            scratch_database("w147_upgraded") as dsn_upgraded,
        ):
            with connect(dsn_fresh) as fresh, connect(dsn_upgraded) as upgraded:
                _build_fresh_from_schema_sql(fresh)

                _build_historical_baseline_generation(upgraded)
                migrate.adopt_existing(upgraded)
                assert migrate.migrate(upgraded) == _versions_after_baseline()

                left = catalog_of(fresh)
                right = catalog_of(upgraded)

                # NO SENTINEL IN THIS ROW, DELIBERATELY. The sentinel is a
                # TABLE and a ROW that the fresh database cannot have, so
                # planting it here would make nine catalog sections differ for
                # a reason that is not drift and force a filter that would
                # quietly hide real differences too. The sentinel's job -- the
                # proof that the upgrade did not rebuild -- is done by
                # `TestARealHistoricalDatabaseIsUpgradedRatherThanRebuilt`,
                # whose starting state this row builds identically and whose
                # `migrate` return value is asserted here too: the upgraded
                # side really did run 0003 rather than already having it.

            difference = describe_difference("fresh from schema.sql", left, "upgraded historical", right)
            assert not difference, (
                "a fresh database and an upgraded historical one are not the same database:" + difference
            )

    def test_both_paths_record_the_same_history_by_content(self):
        """
        The other half: catalogs can match while the HISTORIES do not. A fresh
        database stamped at the wrong version reports pending for ever.
        Compared by version, name AND checksum, so a stamp with the right
        number and the wrong bytes fails here.
        """
        with (
            scratch_database("w147_fresh") as dsn_fresh,
            scratch_database("w147_upgraded") as dsn_upgraded,
        ):
            with connect(dsn_fresh) as fresh, connect(dsn_upgraded) as upgraded:
                _build_fresh_from_schema_sql(fresh)
                _build_historical_baseline_generation(upgraded)
                migrate.adopt_existing(upgraded)
                migrate.migrate(upgraded)

                assert _history(fresh) == _history(upgraded)
                assert migrate.inspect_state(fresh).ok is True
                assert migrate.inspect_state(upgraded).ok is True


# ─── 5. the generation verdict, and readiness ───────────────────────────────


class TestReadinessCannotReportHealthyForTheWrongGeneration:
    def test_a_database_missing_only_the_retraction_CONSTRAINT_is_refused(self, client, monkeypatch):
        """
        WRONG A, IN ITS SHARPEST FORM, and the reason a `MIGRATION-ASSERT`
        line is not enough on its own. Drop ONE named foreign key and leave
        both tables, both indexes and every column standing: all four of
        0003's own `to_regclass` post-conditions still hold, so a migration
        that declared no generation invariant reports this database READY.

        The positive twin runs FIRST on the same database, so this cannot pass
        because `inspect_state` is broken in the other direction.

        Asserted on BOTH channels per Standing Rule 8: the library verdict and
        the probe's status AND body, and the body must NAME the constraint --
        a generic 503 would be satisfied by a probe that is always 503.

        VACUOUS AGAINST: C, D and E.
        """
        with scratch_database("w147_drift") as dsn, connect(dsn) as connection:
            _build_fresh_from_schema_sql(connection)
            assert migrate.inspect_state(connection).ok is True

            _apply(connection, "ALTER TABLE report_retractions DROP CONSTRAINT fk_retraction_report")

            # every to_regclass post-condition 0003 states still holds
            for expression in _the_migration().assertions:
                assert migrate._evaluate(connection, expression) is True, expression

            state = migrate.inspect_state(connection)
            status, reason, text = _probe(client, monkeypatch, dsn)

        assert state.ok is False
        assert state.reason == "schema_drift"
        assert "fk_retraction_report" in state.detail
        assert status == 503
        assert reason == "schema_drift"
        assert "fk_retraction_report" in text

    def test_a_database_missing_only_the_retraction_COLUMN_is_refused(self):
        """
        The column half of the same vocabulary argument. `retracted_from_state`
        is the human's ruling E1 made structural -- the row records which state
        the report was retracted FROM -- and a table rebuilt by hand without it
        is a different generation wearing the right name.

        VACUOUS AGAINST: everything about privileges and ordering.
        """
        with scratch_database("w147_drift_col") as dsn, connect(dsn) as connection:
            _build_fresh_from_schema_sql(connection)
            assert migrate.inspect_state(connection).ok is True

            _apply(connection, "ALTER TABLE report_retractions DROP COLUMN retracted_from_state CASCADE")
            assert _regclass(connection, "report_retractions") is not None

            state = migrate.inspect_state(connection)

        assert state.ok is False
        assert state.reason == "schema_drift"
        assert "report_retractions.retracted_from_state" in state.detail

    def test_a_forged_history_claiming_three_on_a_baseline_database_is_refused(self, client, monkeypatch):
        """
        THE FALSELY-READY DATABASE, at the new version. History rows for 1, 2
        AND 3 with the REAL checksums, written by hand against a database that
        never ran 0003. Nothing in the history can say so.

        The forgery stamps the WHOLE discovered set on purpose -- w146's
        finding: a history that stops at 2 is claiming to be BEHIND, which
        `inspect_state` catches at its cheaper second question and returns
        before it ever reads the catalog. `pending == ()` is asserted, so this
        row cannot certify the generation verdict while actually measuring the
        pending one.

        CATCHES: A through the probe, which is where it matters -- `/readyz`
        calls `discover()` itself and this is the first test in the repository
        where that call returns a real third migration.
        """
        on_disk = migrate.discover()
        with scratch_database("w147_forged") as dsn, connect(dsn) as connection:
            _build_historical_baseline_generation(connection)
            _apply(connection, {m.version: m for m in on_disk}[1].sql)
            with connection.cursor() as cur:
                for migration in on_disk:
                    cur.execute(
                        "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                        (migration.version, migration.name, migration.checksum),
                    )
            connection.commit()

            state = migrate.inspect_state(connection)
            status, reason, text = _probe(client, monkeypatch, dsn)

            with connection.cursor() as cur:
                cur.execute("SELECT count(*) FROM schema_migrations")
                assert cur.fetchone()[0] == len(on_disk)
            connection.rollback()

        assert state.pending == ()
        assert state.ok is False
        assert state.reason == "schema_drift"
        assert "report_retractions" in state.detail
        assert status == 503
        assert reason == "schema_drift"
        assert "report_retractions" in text

    def test_every_invariant_of_0003_holds_for_the_role_readyz_connects_as(self):
        """
        WRONG B, MEASURED RATHER THAN ARGUED. `/readyz` evaluates these
        expressions as `clinical_app`. Each of 0003's invariants is evaluated
        under `SET ROLE clinical_app` and must be TRUE.

        AND A MEASUREMENT THAT NARROWS THE STANDING CLAIM, recorded here
        rather than left as prose. `clinical/migrate.py` says an
        `information_schema` invariant "would report a present object as
        absent and fail readiness for the wrong reason", and
        `test_w143_adoption_proves_the_generation.py` asserts that with
        `via_information_schema <= via_pg_catalog` -- a comparison that also
        passes when the two are EQUAL, which is what they are. Measured on a
        database built from this `schema.sql` (2026-09-18): `clinical_app`
        holds at least one privilege on ALL 34 tables in this schema, so
        `information_schema` hides NOTHING from it and an information_schema
        invariant would not fail readiness today. `clinical_retention` is the
        role that is really filtered -- it sees 16 tables of 34
        (13 of 31 when this was measured, before migration 0010 added three tables it can read).

        So this row asserts three separate things and says which is which:
          1. the LOAD-BEARING half -- every invariant is TRUE as `clinical_app`;
          2. the census, STRICTLY -- `clinical_app` sees every table through
             `information_schema` too, so the pg_catalog rule is
             defence-in-depth here and not a live fix. If a future REVOKE
             changes that, this row goes red and the claim gets revisited
             instead of quietly becoming true again;
          3. the filtering PROVED LIVE on the role where it happens, so
             "information_schema is privilege-filtered" is a measurement in
             this suite rather than a citation.
        """
        invariants = migrate.generation_invariants(_the_migration())
        with scratch_database("w147_role") as dsn, connect(dsn) as connection:
            with connection.cursor() as cur:
                cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
            connection.commit()

            with connection.cursor() as cur:
                cur.execute("SET ROLE clinical_app")
                for invariant in invariants:
                    cur.execute(f"SELECT ({invariant.expression})")
                    assert cur.fetchone()[0] is True, invariant.description

                app_via_is, app_via_catalog = _table_census(cur)
                cur.execute("RESET ROLE")

                cur.execute("SET ROLE clinical_retention")
                retention_via_is, retention_via_catalog = _table_census(cur)
                cur.execute("RESET ROLE")
            connection.rollback()

        assert app_via_catalog == 34, app_via_catalog
        assert app_via_is == app_via_catalog, (
            "information_schema now hides tables from clinical_app. That makes the pg_catalog "
            "rule LOAD-BEARING rather than defence-in-depth, which is a change worth noticing: "
            "re-read the measurement in this docstring before adjusting anything."
        )
        assert retention_via_catalog == app_via_catalog
        assert retention_via_is < retention_via_catalog, (
            "information_schema is no longer privilege-filtered for any role in this schema -- "
            "the premise the invariant vocabulary rests on has changed"
        )


# ─── 6. ordering ────────────────────────────────────────────────────────────


class TestOrderingIsDeterministicAndOutOfOrderIsNotSilent:
    def test_0003_before_0002_raises_rather_than_producing_a_wrong_schema(self):
        """
        WRONG D. 0003 applied to a database that has the history table and
        nothing else. Its four foreign keys reference `reports (org_id, id)`
        and `users (org_id, user_id)`, so Postgres refuses -- and the refusal
        is asserted to leave NOTHING behind, because a half-applied migration
        is the state the one-transaction rule exists to prevent.

        THIS IS THE ANSWER TO THE QUESTION w146 LEFT: a migration whose DDL
        assumes an object a wrong-generation database lacks RAISES; it does not
        refuse politely. The runner's own refusal is a different and EARLIER
        one -- `adopt_existing` rejects a wrong-generation database before any
        of this is reached -- and both are asserted in this file so the
        distinction is on the record rather than in a report.

        VACUOUS AGAINST: A, B and E.
        """
        psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
        by_version = {m.version: m for m in migrate.discover()}
        with scratch_database("w147_order") as dsn, connect(dsn) as connection:
            _apply(connection, by_version[1].sql)

            with pytest.raises(psycopg.Error) as caught:
                with connection.cursor() as cur:
                    cur.execute(by_version[RETRACTION_VERSION].sql)
            connection.rollback()

            assert caught.value.sqlstate in ("42P01", "42830"), caught.value.sqlstate
            for name in RETRACTION_TABLES:
                assert _regclass(connection, name) is None, f"{name} survived a failed out-of-order apply"

            # and in order, on the same database, it works -- the positive
            # twin, without which this row is satisfied by a migration that can
            # never run at all.
            _apply(connection, by_version[migrate.BASELINE_VERSION].sql)
            _apply(connection, by_version[RETRACTION_VERSION].sql)
            for name in RETRACTION_TABLES:
                assert _regclass(connection, name) is not None, name

    def test_the_runner_applies_them_in_version_order_from_empty(self):
        """
        The ordering the RUNNER produces, asserted from the history it wrote
        rather than from the file list: `migrate` on an empty database returns
        the versions it applied, in order, and the history agrees.
        """
        with scratch_database("w147_order2") as dsn, connect(dsn) as connection:
            assert list(migrate.migrate(connection)) == _on_disk_versions()
            assert [row[0] for row in _history(connection)] == _on_disk_versions()
            assert migrate.inspect_state(connection).ok is True

    def test_a_wrong_generation_database_never_reaches_0003_at_all(self):
        """
        THE OTHER HALF OF THE ANSWER ABOVE, and the one an operator actually
        meets. A real pre-security database (`7b034e5:clinical/schema.sql`,
        blob da97f847) is REFUSED by `adopt_existing` -- politely, by the
        runner, naming the column -- and `migrate` refuses it too rather than
        running 0003 against it. So the RAISE measured above is reachable only
        by someone who has already bypassed two refusals.

        CATCHES: a runner that would let a wrong-generation database limp
        forward into a migration whose DDL cannot apply to it.
        """
        with scratch_database("w147_presec") as dsn, connect(dsn) as connection:
            _build_pre_security(connection)

            with pytest.raises(migrate.MigrationStateError) as adoption:
                migrate.adopt_existing(connection)
            connection.rollback()
            assert "exception_events.org_id" in str(adoption.value)

            with pytest.raises(migrate.MigrationStateError) as migration:
                migrate.migrate(connection)
            connection.rollback()
            assert "adopt-existing" in str(migration.value)

            for name in RETRACTION_TABLES:
                assert _regclass(connection, name) is None, name


# ─── 7. concurrency ─────────────────────────────────────────────────────────


class TestConcurrentMigrationIsStillSafeWithThreeMigrations:
    def test_a_second_runner_is_refused_while_the_first_holds_the_lock(self):
        """
        The advisory lock, re-asserted with a real third migration on disk --
        the case where a second runner has something to do rather than nothing.

        CATCHES: a lock scoped to a transaction rather than a session, which
        would release between 0002 and 0003 and let a second runner in
        mid-upgrade.

        The refusal is asserted to have applied NOTHING: the blocked runner's
        database is checked for the retraction tables afterwards.
        """
        with scratch_database("w147_lock") as dsn:
            with connect(dsn) as holder, connect(dsn) as blocked:
                migrate._acquire(holder, "30s")
                try:
                    with pytest.raises(migrate.MigrationLockUnavailable):
                        migrate.migrate(blocked, lock_timeout="1s")
                    blocked.rollback()
                    assert _regclass(blocked, "schema_migrations") is None
                    for name in RETRACTION_TABLES:
                        assert _regclass(blocked, name) is None, name
                finally:
                    migrate._release(holder)

                # and once the lock is free the same connection completes
                assert list(migrate.migrate(blocked)) == _on_disk_versions()

    def test_two_runners_racing_apply_each_migration_exactly_once(self):
        """
        THE REAL RACE, not a simulated one: two threads, two connections, one
        empty database, both calling `migrate`. Exactly one set of history rows
        must exist afterwards and each version must appear ONCE -- asserted
        from the database, by content.

        CATCHES: a runner that serialises the lock but not the history, which
        would leave duplicate rows or a half-applied 0003.

        A lock timeout long enough for the loser to WAIT rather than refuse:
        this row is about what happens when both eventually run, which is the
        case a deploy with `--workers` actually produces.
        """
        results: list = []

        def run(dsn):
            try:
                with connect(dsn) as connection:
                    results.append(migrate.migrate(connection, lock_timeout="30s"))
            except Exception as exc:  # noqa: BLE001 -- recorded, asserted below
                results.append(exc)

        with scratch_database("w147_race") as dsn:
            threads = [threading.Thread(target=run, args=(dsn,)) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=120)

            with connect(dsn) as connection:
                history = _history(connection)
                state = migrate.inspect_state(connection)

        assert len(results) == 2, results
        for outcome in results:
            assert not isinstance(outcome, Exception), outcome
        assert sorted(results) == [(), tuple(_on_disk_versions())], results
        assert [row[0] for row in history] == _on_disk_versions()
        assert state.ok is True, state.detail


# ─── 8. the six privilege statements ────────────────────────────────────────


class TestTheNewTablesCarryExactlyTheGrantsTheDdlStates:
    """
    WRONG E: a privilege statement omitted, so `clinical_app` cannot read the
    new table. The convergence row in section 4 catches an asymmetry between
    the two build paths; this section catches the case where BOTH paths are
    equally wrong, which a catalog comparison cannot see.

    Asked through `has_table_privilege`/`has_column_privilege` rather than by
    connecting: `clinical_app` and `clinical_retention` are NOLOGIN in this
    repository's schema and every other privilege test here asks the same way.
    """

    @pytest.fixture()
    def built(self):
        with scratch_database("w147_grants") as dsn, connect(dsn) as connection:
            _build_fresh_from_schema_sql(connection)
            yield connection

    def _table(self, connection, role, table, privilege) -> bool:
        with connection.cursor() as cur:
            cur.execute("SELECT has_table_privilege(%s, %s, %s)", (role, table, privilege))
            return cur.fetchone()[0]

    def _column(self, connection, role, table, column, privilege) -> bool:
        with connection.cursor() as cur:
            cur.execute("SELECT has_column_privilege(%s, %s, %s, %s)", (role, table, column, privilege))
            return cur.fetchone()[0]

    @pytest.mark.parametrize("table", RETRACTION_TABLES)
    def test_clinical_app_may_select_and_insert(self, built, table):
        """CATCHES E directly: the GRANT line omitted. Without it the
        application cannot read a retraction it just wrote."""
        assert self._table(built, "clinical_app", table, "SELECT") is True
        assert self._table(built, "clinical_app", table, "INSERT") is True

    @pytest.mark.parametrize("table", RETRACTION_TABLES)
    def test_clinical_app_may_never_rewrite_or_delete(self, built, table):
        """The other direction, and the one the append-only ruling is about: a
        retraction quietly edited or deleted after the fact leaves no trace.
        Without this half, E's fix (`GRANT ALL`) passes the row above."""
        assert self._table(built, "clinical_app", table, "UPDATE") is False
        assert self._table(built, "clinical_app", table, "DELETE") is False

    @pytest.mark.parametrize("table", RETRACTION_TABLES)
    def test_public_holds_nothing(self, built, table):
        assert self._table(built, "public", table, "UPDATE") is False
        assert self._table(built, "public", table, "DELETE") is False

    @pytest.mark.parametrize("table", RETRACTION_TABLES)
    def test_retention_may_tombstone_those_two_columns_and_no_others(self, built, table):
        """
        Human ruling E8: these cascade-tombstone with the report they are
        about, so `clinical_retention` needs a COLUMN-SCOPED update and no
        more. Both halves: the two tombstone columns permitted, a third column
        refused, DELETE refused.
        """
        assert self._table(built, "clinical_retention", table, "SELECT") is True
        assert self._column(built, "clinical_retention", table, "tombstoned_at", "UPDATE") is True
        assert self._column(built, "clinical_retention", table, "tombstoned_by", "UPDATE") is True
        assert self._column(built, "clinical_retention", table, "org_id", "UPDATE") is False
        assert self._table(built, "clinical_retention", table, "DELETE") is False

    def test_this_privilege_probe_can_actually_fail(self, built):
        """
        NON-VACUITY. `has_table_privilege` answering False for everything would
        satisfy half the rows above and nothing would ever notice. Grant, then
        revoke, and require the answer to move both ways.
        """
        assert self._table(built, "clinical_app", "report_retractions", "UPDATE") is False
        _apply(built, "GRANT UPDATE ON report_retractions TO clinical_app")
        assert self._table(built, "clinical_app", "report_retractions", "UPDATE") is True
        _apply(built, "REVOKE UPDATE ON report_retractions FROM clinical_app")
        assert self._table(built, "clinical_app", "report_retractions", "UPDATE") is False
