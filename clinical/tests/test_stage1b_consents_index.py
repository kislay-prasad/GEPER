"""
clinical/tests/test_stage1b_consents_index.py
─────────────────────────────────────────────
STAGE 1B: migration 0011, the index behind the consent rule's "latest consent" lookup.

One declarative specification (SPEC, below) is the only description of the agreed index in this file. The
permanent capability assertion in the migration header, the strict adoption checks in the migration body and
the fresh-install path are all pinned to it by BEHAVIOUR, never by extracting SQL from the migration:

  * the capability assertion is evaluated through the framework's own parsed `Migration.assertions`;
  * the adoption checks are exercised by building the index (and each way of getting it wrong) and running
    the real `migrate.migrate`;
  * the schema.sql path is checked by the same assertion and the same introspection, and
    test_w126m_schema_equivalence.py (unchanged) compares the two build paths' `indexdef` byte for byte.

RUNTIME vs ADOPTION. Runtime (the permanent assertion, re-evaluated on every /readyz): B-tree, valid/ready/live,
not partial, no expressions, no INCLUDE, five keys in order, `indoption` 0,0,0,3,3, default operator classes,
column-matching collations. Adoption only (the migration body): the agreed name, non-unique, no storage options.

The databases here are throwaway (`scratch_database`); nothing touches the shared test schema.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

import pytest

from clinical import migrate
from clinical.tests.migration_db import (
    CONSENTS_INDEX_COLUMNS,
    CONSENTS_INDEX_NAME,
    connect,
    prebuild_consents_index,
    require_psycopg,
    scratch_database,
)
from clinical.tests.test_w126m_schema_equivalence import catalog_of

REPO = pathlib.Path(__file__).resolve().parents[2]
MIGRATION = {m.version: m for m in migrate.discover()}[11]
ASSERTION = MIGRATION.assertions[0]

# ─── THE SPECIFICATION ───────────────────────────────────────────────────────
SPEC = {
    "method": "btree",
    "valid": True,
    "ready": True,
    "live": True,
    "unique": False,
    "partial": False,
    "expressions": False,
    "nkeyatts": 5,
    "natts": 5,
    "reloptions": None,
    "indoption": "0 0 0 3 3",  # ASC NULLS LAST x3, DESC NULLS FIRST x2
    "columns": ["org_id", "patient_id", "scope", "recorded_at", "consent_id"],
    "defaults_ok": True,  # every key: default B-tree operator class AND the column's own collation
}


def _upto(version: int):
    return tuple(m for m in migrate.discover() if m.version <= version)


def describe(connection, name: str = CONSENTS_INDEX_NAME):
    """The properties of the named index on consents, as the catalogue reports them; None if there is none."""
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT am.amname, i.indisvalid, i.indisready, i.indislive, i.indisunique,
                   i.indpred IS NOT NULL, i.indexprs IS NOT NULL, i.indnkeyatts, i.indnatts, c.reloptions,
                   i.indoption::text,
                   (SELECT array_agg(a.attname::text ORDER BY t.ord)
                      FROM unnest(i.indkey::int2[]) WITH ORDINALITY AS t(attnum, ord)
                      JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = t.attnum),
                   (SELECT bool_and(oc.opcdefault AND oc.opcmethod = c.relam AND oc.opcintype = a.atttypid
                                    AND t.coll = a.attcollation)
                      FROM unnest(i.indkey::int2[], i.indclass::oid[], i.indcollation::oid[]) AS t(attnum, opc, coll)
                      JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = t.attnum
                      JOIN pg_opclass oc ON oc.oid = t.opc)
              FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_am am ON am.oid = c.relam
             WHERE c.relname = %s AND i.indrelid = to_regclass('consents')
            """,
            (name,),
        )
        row = cur.fetchone()
    connection.rollback()
    if row is None:
        return None
    keys = (
        "method valid ready live unique partial expressions nkeyatts natts reloptions indoption columns defaults_ok"
    ).split()
    return dict(zip(keys, row))


def assertion_holds(connection) -> bool:
    result = migrate._evaluate(connection, ASSERTION)
    connection.rollback()
    return result


def _scalar(connection, query, params=()):
    with connection.cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
    connection.rollback()
    return row[0] if row else None


def _rows(connection, query, params=()):
    with connection.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    connection.rollback()
    return rows


def _plant_consents(connection, count: int = 2):
    """A populated consents table: `count` consents of ONE organisation (so a unique build on org_id must fail)."""
    with connection.cursor() as cur:
        cur.execute("INSERT INTO organisations (name, created_at) VALUES ('Stage 1B org', now()) RETURNING org_id")
        org = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO patients (org_id, name, dob, sex, created_at) VALUES (%s, 'P', '1990-01-01', 'F', now()) "
            "RETURNING patient_id",
            (org,),
        )
        patient = cur.fetchone()[0]
        for _ in range(count):
            cur.execute(
                "INSERT INTO consents (patient_id, org_id, scope, recorded_by, recorded_at) "
                "VALUES (%s, %s, 'testing', gen_random_uuid(), now())",
                (patient, org),
            )
    connection.commit()


def _state(dsn: str) -> dict:
    """Everything a failed migration must leave exactly as it found it."""
    with connect(dsn) as c:
        return {
            "history": migrate.read_history(c),
            "consents_indexes": tuple(
                _rows(
                    c,
                    "SELECT c.oid::bigint, c.relname, i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                    "WHERE i.indrelid = 'consents'::regclass ORDER BY c.relname",
                )
            ),
            "relations": tuple(
                _rows(
                    c,
                    "SELECT oid::bigint, relname, relkind FROM pg_class WHERE relnamespace = 'public'::regnamespace "
                    "ORDER BY relname",
                )
            ),
            "catalog": catalog_of(c),
            "consents": tuple(
                _rows(c, "SELECT consent_id, patient_id, scope, recorded_at FROM consents ORDER BY consent_id")
            ),
        }


def _refused(dsn: str) -> str:
    """Run the full migration and return the database error's text; it MUST fail."""
    psycopg = require_psycopg()
    with connect(dsn) as c:
        with pytest.raises(psycopg.errors.Error) as info:
            migrate.migrate(c)
        c.rollback()
    return str(info.value)


# ─── 1. the files, structurally ──────────────────────────────────────────────


class TestTheMigrationFile:
    def test_it_is_migration_11_and_declares_exactly_one_post_condition(self):
        assert (MIGRATION.version, MIGRATION.name) == (11, "consents_patient_scope_latest")
        assert len(MIGRATION.assertions) == 1

    def test_the_permanent_assertion_is_not_bound_to_the_index_name(self):
        assert CONSENTS_INDEX_NAME not in ASSERTION

    def _body(self) -> str:
        return "\n".join(ln for ln in MIGRATION.sql.splitlines() if not ln.strip().startswith("--"))

    def test_it_never_builds_concurrently_or_removes_anything(self):
        body = self._body().upper()
        assert "CONCURRENTLY" not in body
        assert "DROP " not in body  # the fence test_0005_is_the_only_migration_that_drops_anything also holds

    def test_the_only_build_is_the_guarded_one_for_an_empty_table(self):
        body = self._body()
        assert body.upper().count("CREATE INDEX") == 1
        guard = body.index("EXISTS (SELECT 1 FROM consents)")
        assert body.index("CREATE INDEX consents_patient_scope_latest") > guard

    def test_there_is_one_definition_in_schema_sql_and_the_migration(self):
        schema = (REPO / "clinical" / "schema.sql").read_text(encoding="utf-8")
        in_schema = re.findall(r"CREATE INDEX consents_patient_scope_latest\s+ON consents \(([^)]*)\);", schema)
        in_migration = re.findall(
            r"'CREATE INDEX consents_patient_scope_latest ON consents \(([^)]*)\)'", MIGRATION.sql
        )
        assert len(in_schema) == 1 and len(in_migration) == 1

        def norm(s):
            return " ".join(s.split())

        assert norm(in_schema[0]) == norm(in_migration[0]) == norm(CONSENTS_INDEX_COLUMNS.strip("()"))

    def test_schema_sql_stamps_migration_11_with_the_files_checksum(self):
        schema = (REPO / "clinical" / "schema.sql").read_text(encoding="utf-8")
        assert f"(11, 'consents_patient_scope_latest', '{MIGRATION.checksum}')" in schema


# ─── 2. the PostgreSQL behaviour the assertion relies on (run-time calibration) ──


class TestIndoptionCalibration:
    def test_indoption_encodes_asc_desc_and_nulls_placement_as_the_assertion_assumes(self):
        with scratch_database("stage1bcal") as dsn, connect(dsn) as c:
            with c.cursor() as cur:
                cur.execute("CREATE TABLE cal (a int, b int)")
                for name, ddl in (
                    ("cal_asc", "(a)"),
                    ("cal_desc", "(a DESC)"),
                    ("cal_asc_nulls_first", "(a NULLS FIRST)"),
                    ("cal_desc_nulls_last", "(a DESC NULLS LAST)"),
                    ("cal_two", "(a, b DESC)"),
                ):
                    cur.execute(f"CREATE INDEX {name} ON cal {ddl}")
            c.commit()
            got = dict(
                _rows(
                    c,
                    "SELECT c.relname, i.indoption::text FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                    "WHERE i.indrelid = 'cal'::regclass",
                )
            )
            assert got["cal_asc"] == "0"  # ASC, NULLS LAST (the default)
            assert got["cal_desc"] == "3"  # DESC | NULLS FIRST (DESC's default)
            assert got["cal_asc_nulls_first"] == "2"
            assert got["cal_desc_nulls_last"] == "1"
            assert got["cal_two"] == "0 3"

    def test_the_vectors_are_walked_by_ordinality_not_by_subscript(self):
        """int2vector subscripts start at 0, int2[] at 1; the assertion walks WITH ORDINALITY (1-based) so neither
        matters. This pins that the walk yields the keys in index order with the right option bits."""
        with scratch_database("stage1bcal") as dsn, connect(dsn) as c:
            with c.cursor() as cur:
                cur.execute("CREATE TABLE cal (a int, b int)")
                cur.execute("CREATE INDEX cal_two ON cal (a, b DESC)")
            c.commit()
            walked = _rows(
                c,
                "SELECT t.ord, t.opt FROM pg_index i, unnest(i.indoption::int2[]) WITH ORDINALITY AS t(opt, ord) "
                "WHERE i.indexrelid = 'cal_two'::regclass ORDER BY t.ord",
            )
            assert walked == [(1, 0), (2, 3)]


# ─── 3. both build paths satisfy the same specification ──────────────────────


@pytest.fixture()
def fresh():
    from clinical.bootstrap import apply_schema

    with scratch_database("stage1b") as dsn:
        with connect(dsn) as c:
            apply_schema(c)
            c.commit()
        yield dsn


@pytest.fixture()
def migrated_from_empty():
    with scratch_database("stage1b") as dsn:
        with connect(dsn) as c:
            assert migrate.migrate(c)[-1] == 11
        yield dsn


class TestBothInstallPathsSatisfyOneSpecification:
    def test_a_fresh_schema_sql_install(self, fresh):
        with connect(fresh) as c:
            assert describe(c) == SPEC
            assert assertion_holds(c) is True
            state = migrate.inspect_state(c)
            assert state.ok, (state.reason, state.detail)
            assert max(state.applied) == 11
            assert migrate.migrate(c) == ()

    def test_a_database_migrated_from_empty(self, migrated_from_empty):
        with connect(migrated_from_empty) as c:
            assert describe(c) == SPEC
            assert assertion_holds(c) is True
            state = migrate.inspect_state(c)
            assert state.ok, (state.reason, state.detail)

    def test_the_two_paths_report_the_identical_index_definition(self, fresh, migrated_from_empty):
        query = "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE indexname = %s"
        with connect(fresh) as a, connect(migrated_from_empty) as b:
            left, right = _rows(a, query, (CONSENTS_INDEX_NAME,)), _rows(b, query, (CONSENTS_INDEX_NAME,))
        assert left == right and len(left) == 1
        definition = left[0][2]
        assert (
            "USING btree" in definition
            and "(org_id, patient_id, scope, recorded_at DESC, consent_id DESC)" in definition
        )
        assert "UNIQUE" not in definition and "WHERE" not in definition and "INCLUDE" not in definition

    def test_an_empty_consents_table_has_the_index_built_by_the_migration_itself(self):
        with scratch_database("stage1b") as dsn:
            with connect(dsn) as c:
                migrate.migrate(c, _upto(10))
                c.commit()
                assert describe(c) is None
                assert migrate.migrate(c) == (11,)
                assert describe(c) == SPEC


# ─── 4. a populated table: adopt a valid concurrent pre-build, refuse everything else ──


class TestAdoptionOfAPrebuiltIndex:
    def test_a_valid_concurrent_prebuild_is_adopted_not_rebuilt(self):
        with scratch_database("stage1b") as dsn:
            with connect(dsn) as c:
                migrate.migrate(c, _upto(10))
                c.commit()
                _plant_consents(c)
            prebuild_consents_index(dsn)
            with connect(dsn) as c:
                before = _rows(
                    c,
                    "SELECT oid::bigint, relfilenode::bigint FROM pg_class WHERE relname = %s",
                    (CONSENTS_INDEX_NAME,),
                )
                assert describe(c) == SPEC
                assert migrate.migrate(c) == (11,)
                after = _rows(
                    c,
                    "SELECT oid::bigint, relfilenode::bigint FROM pg_class WHERE relname = %s",
                    (CONSENTS_INDEX_NAME,),
                )
                assert before == after and len(after) == 1, "the pre-built index must be adopted in place, not rebuilt"
                assert assertion_holds(c) is True
                state = migrate.inspect_state(c)
                assert state.ok, (state.reason, state.detail)
                assert migrate.migrate(c) == ()

    def test_a_prebuild_is_valid_ready_and_live_before_adoption(self):
        with scratch_database("stage1b") as dsn:
            with connect(dsn) as c:
                migrate.migrate(c, _upto(10))
                c.commit()
                _plant_consents(c)
            prebuild_consents_index(dsn)
            with connect(dsn) as c:
                found = describe(c)
        assert found["valid"] and found["ready"] and found["live"]


# (label, statements that set the situation up, statements that clear it, does the PERMANENT assertion hold?,
#  fragment of the migration's refusal)
ABSENT = "table consents holds rows and index consents_patient_scope_latest has not been built"
NOT_VALID = "not valid, ready and live"
MISMATCH = "does not match the agreed definition"
AGREED = "consents_patient_scope_latest"
OTHER = "consents_stage1b_other"


MUTANTS = [
    ("absent", [], [], False, ABSENT),
    (
        "renamed_equivalent",
        [f"CREATE INDEX {OTHER} ON consents (org_id, patient_id, scope, recorded_at DESC, consent_id DESC)"],
        [f"DROP INDEX IF EXISTS {OTHER}"],
        True,
        ABSENT,
    ),
    (
        "columns_in_the_wrong_order",
        [
            "CREATE INDEX consents_patient_scope_latest ON consents (patient_id, org_id, scope, recorded_at DESC, consent_id DESC)"
        ],
        ["DROP INDEX IF EXISTS consents_patient_scope_latest"],
        False,
        MISMATCH,
    ),
    (
        "recorded_at_ascending",
        [
            "CREATE INDEX consents_patient_scope_latest ON consents (org_id, patient_id, scope, recorded_at, consent_id DESC)"
        ],
        ["DROP INDEX IF EXISTS consents_patient_scope_latest"],
        False,
        MISMATCH,
    ),
    (
        "consent_id_ascending",
        [
            "CREATE INDEX consents_patient_scope_latest ON consents (org_id, patient_id, scope, recorded_at DESC, consent_id)"
        ],
        ["DROP INDEX IF EXISTS consents_patient_scope_latest"],
        False,
        MISMATCH,
    ),
    (
        "descending_nulls_last",
        [
            "CREATE INDEX consents_patient_scope_latest ON consents (org_id, patient_id, scope, recorded_at DESC NULLS LAST, consent_id DESC)"
        ],
        ["DROP INDEX IF EXISTS consents_patient_scope_latest"],
        False,
        MISMATCH,
    ),
    (
        "ascending_nulls_first",
        [
            "CREATE INDEX consents_patient_scope_latest ON consents (org_id, patient_id, scope NULLS FIRST, recorded_at DESC, consent_id DESC)"
        ],
        ["DROP INDEX IF EXISTS consents_patient_scope_latest"],
        False,
        MISMATCH,
    ),
    (
        "partial",
        [
            "CREATE INDEX consents_patient_scope_latest ON consents (org_id, patient_id, scope, recorded_at DESC, consent_id DESC) WHERE withdrawn_at IS NULL"
        ],
        ["DROP INDEX IF EXISTS consents_patient_scope_latest"],
        False,
        MISMATCH,
    ),
    (
        "expression",
        [
            "CREATE INDEX consents_patient_scope_latest ON consents (org_id, patient_id, lower(scope), recorded_at DESC, consent_id DESC)"
        ],
        ["DROP INDEX IF EXISTS consents_patient_scope_latest"],
        False,
        MISMATCH,
    ),
    (
        "include_column",
        [
            "CREATE INDEX consents_patient_scope_latest ON consents (org_id, patient_id, scope, recorded_at DESC, consent_id DESC) INCLUDE (withdrawn_at)"
        ],
        ["DROP INDEX IF EXISTS consents_patient_scope_latest"],
        False,
        MISMATCH,
    ),
    (
        "too_few_keys",
        ["CREATE INDEX consents_patient_scope_latest ON consents (org_id, patient_id, scope, recorded_at DESC)"],
        ["DROP INDEX IF EXISTS consents_patient_scope_latest"],
        False,
        MISMATCH,
    ),
    (
        "not_a_btree",
        ["CREATE INDEX consents_patient_scope_latest ON consents USING hash (org_id)"],
        ["DROP INDEX IF EXISTS consents_patient_scope_latest"],
        False,
        MISMATCH,
    ),
    (
        "non_default_operator_class",
        [
            "CREATE INDEX consents_patient_scope_latest ON consents (org_id, patient_id, scope text_pattern_ops, recorded_at DESC, consent_id DESC)"
        ],
        ["DROP INDEX IF EXISTS consents_patient_scope_latest"],
        False,
        MISMATCH,
    ),
    (
        "differing_collation",
        [
            'CREATE INDEX consents_patient_scope_latest ON consents (org_id, patient_id, scope COLLATE "C", recorded_at DESC, consent_id DESC)'
        ],
        ["DROP INDEX IF EXISTS consents_patient_scope_latest"],
        False,
        MISMATCH,
    ),
    # runtime requirements do not include uniqueness or storage options; adoption does
    (
        "unique_equivalent",
        [
            "CREATE UNIQUE INDEX consents_patient_scope_latest ON consents (org_id, patient_id, scope, recorded_at DESC, consent_id DESC)"
        ],
        ["DROP INDEX IF EXISTS consents_patient_scope_latest"],
        True,
        MISMATCH,
    ),
    (
        "storage_option",
        [
            "CREATE INDEX consents_patient_scope_latest ON consents (org_id, patient_id, scope, recorded_at DESC, consent_id DESC) WITH (fillfactor = 70)"
        ],
        ["DROP INDEX IF EXISTS consents_patient_scope_latest"],
        True,
        MISMATCH,
    ),
    (
        "flagged_invalid",
        [
            "CREATE INDEX consents_patient_scope_latest ON consents (org_id, patient_id, scope, recorded_at DESC, consent_id DESC)",
            "UPDATE pg_index SET indisvalid = false WHERE indexrelid = 'consents_patient_scope_latest'::regclass",
        ],
        ["DROP INDEX IF EXISTS consents_patient_scope_latest"],
        False,
        NOT_VALID,
    ),
    (
        "a_relation_that_is_not_an_index",
        ["CREATE TABLE consents_patient_scope_latest (x int)"],
        ["DROP TABLE IF EXISTS consents_patient_scope_latest"],
        False,
        "is not an index on consents",
    ),
]


@pytest.fixture(scope="module")
def populated_at_10():
    with scratch_database("stage1b") as dsn:
        with connect(dsn) as c:
            migrate.migrate(c, _upto(10))
            c.commit()
            _plant_consents(c, 2)
        yield dsn


class TestEveryWayOfGettingTheIndexWrong:
    @pytest.mark.parametrize("label,setup,cleanup,capability,fragment", MUTANTS, ids=[m[0] for m in MUTANTS])
    def test_the_assertion_and_the_refusal(self, populated_at_10, label, setup, cleanup, capability, fragment):
        dsn = populated_at_10
        try:
            with connect(dsn, autocommit=True) as c:
                for statement in setup:
                    c.execute(statement)
            with connect(dsn) as c:
                assert assertion_holds(c) is capability, f"{label}: the permanent capability assertion"
            before = _state(dsn)
            message = _refused(dsn)
            assert "migration 0011 refused" in message and fragment in message, (label, message)
            after = _state(dsn)
            # the three outcomes, and the rest of the catalogue
            assert after["history"] == before["history"], "a history row was written"
            assert max(v.version for v in after["history"]) == 10
            assert after["consents_indexes"] == before["consents_indexes"], "an index appeared, vanished or changed"
            assert after["relations"] == before["relations"], "a relation was created or removed"
            assert after["catalog"] == before["catalog"], "the schema changed"
            assert after["consents"] == before["consents"], "consent rows changed"
        finally:
            with connect(dsn, autocommit=True) as c:
                for statement in cleanup:
                    c.execute(statement)

    def test_an_interrupted_concurrent_build_leaves_an_invalid_index_that_is_rejected_not_repaired(
        self, populated_at_10
    ):
        """The natural way to get an invalid leftover: a concurrent build that fails part-way (here, a unique build
        over duplicate organisation ids). PostgreSQL leaves the INVALID index; the migration reports it and leaves it."""
        psycopg = require_psycopg()
        dsn = populated_at_10
        try:
            with connect(dsn, autocommit=True) as c:
                with pytest.raises(psycopg.errors.UniqueViolation):
                    c.execute(f"CREATE UNIQUE INDEX CONCURRENTLY {AGREED} ON consents (org_id)")
            with connect(dsn) as c:
                leftover = _rows(c, "SELECT indisvalid FROM pg_index WHERE indexrelid = %s::regclass", (AGREED,))
                assert leftover == [(False,)], "the premise: the failed concurrent build left an invalid index"
                assert assertion_holds(c) is False
            before = _state(dsn)
            message = _refused(dsn)
            assert "migration 0011 refused" in message and NOT_VALID in message
            assert _state(dsn) == before, "the migration repaired, rebuilt or removed the leftover"
            with connect(dsn) as c:
                assert _rows(c, "SELECT indisvalid FROM pg_index WHERE indexrelid = %s::regclass", (AGREED,)) == [
                    (False,)
                ]
        finally:
            with connect(dsn, autocommit=True) as c:
                c.execute(f"DROP INDEX IF EXISTS {AGREED}")

    def test_the_cli_exits_non_zero_and_records_nothing(self, populated_at_10):
        dsn = populated_at_10
        env = dict(os.environ, CLINICAL_BOOTSTRAP_DSN=dsn, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(REPO))
        before = _state(dsn)
        done = subprocess.run(
            [sys.executable, "-m", "clinical.migrate", "--migrate"],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert done.returncode != 0, (done.stdout, done.stderr)
        assert "migration 0011 refused" in (done.stdout + done.stderr)
        assert _state(dsn) == before


# ─── 5. readiness ────────────────────────────────────────────────────────────


class TestReadiness:
    def test_dropping_the_capability_is_drift_and_recreating_it_recovers(self, migrated_from_empty):
        dsn = migrated_from_empty
        with connect(dsn) as c:
            assert migrate.inspect_state(c).ok
        with connect(dsn, autocommit=True) as c:
            c.execute(f"DROP INDEX {AGREED}")
        with connect(dsn) as c:
            state = migrate.inspect_state(c)
            assert state.ok is False and state.reason == "schema_drift", (state.reason, state.detail)
            assert "migration 11 (consents_patient_scope_latest)" in (state.detail or "")
        with connect(dsn, autocommit=True) as c:
            c.execute(f"CREATE INDEX {AGREED} ON consents {CONSENTS_INDEX_COLUMNS}")
        with connect(dsn) as c:
            state = migrate.inspect_state(c)
            assert state.ok, (state.reason, state.detail)

    def test_an_equivalent_index_under_another_name_keeps_readiness(self, migrated_from_empty):
        dsn = migrated_from_empty
        with connect(dsn, autocommit=True) as c:
            c.execute(f"ALTER INDEX {AGREED} RENAME TO {OTHER}")
        with connect(dsn) as c:
            assert assertion_holds(c) is True
            state = migrate.inspect_state(c)
            assert state.ok, (state.reason, state.detail)

    def test_an_invalid_index_is_drift(self, migrated_from_empty):
        dsn = migrated_from_empty
        with connect(dsn, autocommit=True) as c:
            c.execute(f"UPDATE pg_index SET indisvalid = false WHERE indexrelid = '{AGREED}'::regclass")
        with connect(dsn) as c:
            state = migrate.inspect_state(c)
            assert state.ok is False and state.reason == "schema_drift"
        with connect(dsn, autocommit=True) as c:
            c.execute(f"UPDATE pg_index SET indisvalid = true WHERE indexrelid = '{AGREED}'::regclass")
        with connect(dsn) as c:
            assert migrate.inspect_state(c).ok

    def test_a_wrongly_ordered_replacement_is_drift(self, migrated_from_empty):
        dsn = migrated_from_empty
        with connect(dsn, autocommit=True) as c:
            c.execute(f"DROP INDEX {AGREED}")
            c.execute(f"CREATE INDEX {AGREED} ON consents (org_id, patient_id, scope, recorded_at, consent_id)")
        with connect(dsn) as c:
            state = migrate.inspect_state(c)
            assert state.ok is False and state.reason == "schema_drift"

    # N2: /readyz does not run as the administrator. The assertion and the whole readiness verdict must hold for the
    # roles the service actually connects as (catalogue reads only; neither role needs any privilege on `consents`).
    @pytest.mark.parametrize("role", ["clinical_app", "clinical_retention"])
    def test_readiness_holds_under_the_service_roles(self, migrated_from_empty, role):
        # autocommit: SET ROLE is then session state that survives inspect_state's own transaction handling,
        # and the current_user check below proves the verdict was really computed under the role.
        with connect(migrated_from_empty, autocommit=True) as c:
            try:
                c.execute(f"SET ROLE {role}")
                assert c.execute(f"SELECT ({ASSERTION})").fetchone()[0] is True
                if (
                    role == "clinical_app"
                ):  # the identity /readyz uses; clinical_retention has no grant on schema_migrations
                    state = migrate.inspect_state(c)
                    assert c.execute("SELECT current_user").fetchone()[0] == role
                    assert state.ok, (state.reason, state.detail)
                    assert max(state.applied) == 11
            finally:
                c.execute("RESET ROLE")
            assert c.execute("SELECT current_user = session_user").fetchone()[0] is True


# ─── 6. the production consent query and the index stay coupled ──────────────
#
# What this section proves, and what it does not. It proves that the index's key order is the one the production
# query filters and sorts by, and that the query's answer (including the tie-break on equal timestamps) is the
# same when the planner is FORCED to read the index as when no index exists. Forcing the planner on a handful
# of rows says nothing about production speed or about what the planner would choose at production volume; the
# only performance evidence is measured elsewhere (docs/CLINICAL_DEPLOYMENT.md, "Upgrading to migration 0011").

from clinical.data_access import DataAccess  # noqa: E402

LOW_ID = "00000000-0000-0000-0000-00000000000a"
HIGH_ID = "00000000-0000-0000-0000-00000000000b"


class TestTheConsentQueryAndTheIndexStayCoupled:
    def test_the_latest_consent_lookup_filters_and_orders_by_exactly_the_index_keys(self):
        sql = DataAccess._CALLSET_LATEST_CONSENT_OK_SQL
        equalities = re.findall(r"\bk\.(\w+) = o\.\w+", sql)
        order = re.search(r"ORDER BY ((?:k\.\w+ (?:DESC|ASC)(?:, )?)+) LIMIT 1", sql)
        assert order, sql
        ordering = [tuple(part.split(".")[1].split(" ")) for part in order.group(1).split(", ")]
        assert equalities == SPEC["columns"][:3], "equality filters must be the index's leading three keys, in order"
        assert ordering == [("recorded_at", "DESC"), ("consent_id", "DESC")]
        assert [c for c, _ in ordering] == SPEC["columns"][3:]
        # and the index declares those same directions
        assert CONSENTS_INDEX_COLUMNS.replace(
            " ", ""
        ) == "(org_id,patient_id,scope,recorded_at DESC,consent_id DESC)".replace(" ", "")

    def _seed(self, dsn):
        with connect(dsn, autocommit=True) as c:
            c.execute("SET session_replication_role = replica")  # superuser-only; throwaway database
            c.execute("INSERT INTO organisations(org_id,name,created_at) VALUES (md5('org')::uuid,'o',now())")
            c.execute(
                "INSERT INTO tests(test_id,org_id,name,assembly,status) "
                "VALUES (md5('t')::uuid, md5('org')::uuid,'t','GRCh38','active')"
            )
            c.execute(
                "INSERT INTO patients(patient_id,org_id,name,dob,sex,created_at) "
                "VALUES (md5('p')::uuid, md5('org')::uuid,'P',date '1980-01-01','F',now())"
            )

    def _scenario(self, c, low_withdrawn, high_withdrawn):
        """Two consents with an IDENTICAL recorded_at; the order was placed under HIGH when possible. Returns the expected verdict."""
        c.execute("DELETE FROM orders")
        c.execute("DELETE FROM consents")
        for cid, withdrawn in ((LOW_ID, low_withdrawn), (HIGH_ID, high_withdrawn)):
            c.execute(
                "INSERT INTO consents(consent_id,patient_id,org_id,scope,recorded_by,recorded_at,withdrawn_at,withdrawn_by) "
                "VALUES (%s, md5('p')::uuid, md5('org')::uuid,'testing', md5('u')::uuid, timestamptz '2025-03-03 10:00:00+00', %s, %s)",
                (
                    cid,
                    "2025-06-01" if withdrawn else None,
                    "00000000-0000-0000-0000-0000000000ff" if withdrawn else None,
                ),
            )
        own = LOW_ID if high_withdrawn else HIGH_ID
        c.execute(
            "INSERT INTO orders(order_id,patient_id,org_id,test_id,required_scope,ordered_by,consent_id,state,placed_at,created_at) "
            "VALUES (md5('ord')::uuid, md5('p')::uuid, md5('org')::uuid, md5('t')::uuid,'testing', md5('u')::uuid, %s,'closed',now(),now())",
            (own,),
        )
        own_active = not (low_withdrawn if own == LOW_ID else high_withdrawn)
        # the higher consent_id decides a tie on recorded_at, so the LATEST consent is withdrawn iff HIGH is
        return own_active and not high_withdrawn

    CASES = [(False, True), (True, False), (False, False), (True, True)]

    def test_a_timestamp_tie_is_decided_by_consent_id_with_and_without_the_index_scan(self):
        verdict = f"SELECT {DataAccess._CALLSET_CONSENT_OK_SQL} FROM orders o WHERE o.order_id = md5('ord')::uuid"
        with scratch_database("stage1b_tie") as dsn:
            with connect(dsn) as c:
                migrate.migrate(c)
            self._seed(dsn)
            expected, forced, plans = [], [], []
            with connect(dsn, autocommit=True) as c:
                for low_wd, high_wd in self.CASES:
                    expected.append(self._scenario(c, low_wd, high_wd))
                    c.execute("SET enable_seqscan = off")
                    c.execute("SET enable_bitmapscan = off")
                    c.execute("SET enable_sort = off")
                    plans.append("\n".join(r[0] for r in c.execute("EXPLAIN " + verdict).fetchall()))
                    forced.append(c.execute(verdict).fetchone()[0])
                    c.execute("RESET enable_seqscan")
                    c.execute("RESET enable_bitmapscan")
                    c.execute("RESET enable_sort")
                # same four scenarios, index removed (throwaway database): the answer must not depend on the index
                c.execute(f"DROP INDEX {AGREED}")
                without = []
                for low_wd, high_wd in self.CASES:
                    self._scenario(c, low_wd, high_wd)
                    without.append(c.execute(verdict).fetchone()[0])
        assert all(CONSENTS_INDEX_NAME in p for p in plans), (
            "the forced plan must really read the new index:\n" + plans[0]
        )
        assert expected == [False, True, True, False]
        assert forced == expected
        assert without == expected
