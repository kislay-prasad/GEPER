"""
clinical/tests/test_w167_catalogue_integrity_migrations.py
──────────────────────────────────────────────────────────
Migrations 0007 and 0008: the two integrity guarantees the catalogue and the
order stage were missing, enforced by PostgreSQL ITSELF.

  0007  orders (test_id, org_id) -> tests (test_id, org_id)
        An order cannot reference another organisation's test.
  0008  UNIQUE (org_id, lower(name)) WHERE status = 'active' on tests
        Two active tests in one organisation cannot share a name, however the
        two inserts race. A retired test releases its name.

EVERY ROW HERE TALKS TO THE DATABASE WITH RAW SQL ON PURPOSE. The application
already refuses both cases (w166 proves it over HTTP). These rows prove the
refusal survives the application being wrong, which is the only reason the
constraints exist. Each refusal asserts the SQLSTATE and the CONSTRAINT NAME,
so a different error (a typo, a missing column) cannot pass for the guarantee.

Each test builds its own scratch database (clinical/tests/migration_db.py), so
none touches the database the rest of the suite uses.
"""

from __future__ import annotations

import threading
import uuid

import pytest

from clinical import migrate
from clinical.tests.migration_db import connect, require_psycopg, scratch_database

T = "2026-01-01T00:00:00+00:00"


# ─── helpers ─────────────────────────────────────────────────────────────────


def _upto(version):
    return tuple(m for m in migrate.discover() if m.version <= version)


def _plant_org(cur, name):
    org = uuid.uuid4()
    cur.execute("INSERT INTO organisations (org_id, name, created_at) VALUES (%s, %s, %s)", (org, name, T))
    patient, consent = uuid.uuid4(), uuid.uuid4()
    cur.execute(
        "INSERT INTO patients (patient_id, org_id, name, dob, sex, created_at) VALUES (%s, %s, 'P', '1990-01-01', 'F', %s)",
        (patient, org, T),
    )
    cur.execute(
        "INSERT INTO consents (consent_id, patient_id, org_id, scope, recorded_by, recorded_at) "
        "VALUES (%s, %s, %s, 'testing', %s, %s)",
        (consent, patient, org, uuid.uuid4(), T),
    )
    return org, patient, consent


def _plant_test(cur, org, name, status="active"):
    test = uuid.uuid4()
    cur.execute(
        "INSERT INTO tests (test_id, org_id, name, assembly, status, created_at) VALUES (%s, %s, %s, 'GRCh38', %s, %s)",
        (test, org, name, status, T),
    )
    return test


def _plant_order(cur, org, patient, consent, test):
    order = uuid.uuid4()
    cur.execute(
        "INSERT INTO orders (order_id, patient_id, org_id, test_id, required_scope, ordered_by, consent_id, created_at) "
        "VALUES (%s, %s, %s, %s, 'testing', %s, %s, %s)",
        (order, patient, org, test, uuid.uuid4(), consent, T),
    )
    return order


def _violation(connection, statement_runner):
    """Run a statement expected to fail; return (sqlstate, constraint_name) and roll back."""
    psycopg = require_psycopg()
    with pytest.raises(psycopg.errors.Error) as info:
        statement_runner()
    connection.rollback()
    return info.value.sqlstate, info.value.diag.constraint_name


@pytest.fixture()
def fresh():
    """A database built from clinical/schema.sql, the way bootstrap does it."""
    from clinical.bootstrap import apply_schema

    with scratch_database("w167") as dsn:
        with connect(dsn) as connection:
            apply_schema(connection)
            connection.commit()
        yield dsn


# ─── the migrations themselves ───────────────────────────────────────────────


class TestTheMigrationsApplyCleanlyAndAreDeclared:
    def test_the_two_files_exist_and_declare_post_conditions(self):
        by_version = {m.version: m for m in migrate.discover()}
        assert by_version[7].name == "order_test_same_org"
        assert by_version[8].name == "active_test_name_unique"

    def test_a_clean_database_migrated_from_nothing_reaches_the_current_version(self):
        with scratch_database("w167") as dsn, connect(dsn) as connection:
            applied = migrate.migrate(connection)
            assert applied[-4:] == (7, 8, 9, 10)
            state = migrate.inspect_state(connection)
            assert state.ok, (state.reason, state.detail)
            assert max(state.applied) == 10

    def test_a_database_built_from_schema_sql_is_already_at_the_current_version(self, fresh):
        with connect(fresh) as connection:
            state = migrate.inspect_state(connection)
            assert state.ok, (state.reason, state.detail)
            assert {7, 8, 9} <= set(state.applied)
            assert migrate.migrate(connection) == ()  # nothing pending

    def test_an_existing_generation_6_database_upgrades_and_keeps_its_data(self):
        with scratch_database("w167") as dsn, connect(dsn) as connection:
            migrate.migrate(connection, _upto(6))
            with connection.cursor() as cur:
                org, patient, consent = _plant_org(cur, "Org A")
                test = _plant_test(cur, org, "Panel")
                order = _plant_order(cur, org, patient, consent, test)
            connection.commit()

            assert migrate.migrate(connection) == (7, 8, 9, 10)
            state = migrate.inspect_state(connection)
            assert state.ok, (state.reason, state.detail)

            with connection.cursor() as cur:
                cur.execute("SELECT test_id, org_id FROM orders WHERE order_id = %s", (order,))
                assert cur.fetchone() == (test, org)
                cur.execute("SELECT name, status FROM tests WHERE test_id = %s", (test,))
                assert cur.fetchone() == ("Panel", "active")

    def test_the_catalogue_of_a_migrated_database_has_both_objects(self):
        with scratch_database("w167") as dsn, connect(dsn) as connection:
            migrate.migrate(connection)
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT conname, contype FROM pg_constraint WHERE conname IN ('tests_id_org_unique','orders_test_org_fk') "
                    "ORDER BY conname"
                )
                assert cur.fetchall() == [("orders_test_org_fk", "f"), ("tests_id_org_unique", "u")]
                cur.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'tests_org_active_name_unique'")
                (definition,) = cur.fetchone()
                assert "UNIQUE" in definition and "lower(name)" in definition and "'active'" in definition


class TestExistingViolationsAreReportedNotRepaired:
    def test_0007_refuses_when_an_order_already_crosses_organisations_and_changes_nothing(self):
        with scratch_database("w167") as dsn:
            with connect(dsn) as connection:
                migrate.migrate(connection, _upto(6))
                with connection.cursor() as cur:
                    org_a, patient_a, consent_a = _plant_org(cur, "Org A")
                    org_b, _, _ = _plant_org(cur, "Org B")
                    foreign_test = _plant_test(cur, org_b, "B panel")
                    # Accepted by generation 6 -- this is the gap itself.
                    order = _plant_order(cur, org_a, patient_a, consent_a, foreign_test)
                connection.commit()

            with connect(dsn) as connection:
                with pytest.raises(Exception) as info:
                    migrate.migrate(connection)
                chain = " | ".join(str(e) for e in (info.value, info.value.__cause__, info.value.__context__) if e)
                assert "migration 0007 refused" in chain and "1 order" in chain
                connection.rollback()

            with connect(dsn) as connection:
                state = migrate.inspect_state(connection)
                assert max(state.applied) == 6
                with connection.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM pg_constraint WHERE conname IN ('tests_id_org_unique','orders_test_org_fk')"
                    )
                    assert cur.fetchone()[0] == 0
                    cur.execute("SELECT test_id, org_id FROM orders WHERE order_id = %s", (order,))
                    assert cur.fetchone() == (foreign_test, org_a)  # untouched

    def test_0008_refuses_when_two_active_tests_already_share_a_name_and_changes_nothing(self):
        with scratch_database("w167") as dsn:
            with connect(dsn) as connection:
                migrate.migrate(connection, _upto(7))
                with connection.cursor() as cur:
                    org, _, _ = _plant_org(cur, "Org A")
                    first = _plant_test(cur, org, "BRCA panel")
                    second = _plant_test(cur, org, "brca PANEL")
                connection.commit()

            with connect(dsn) as connection:
                with pytest.raises(Exception) as info:
                    migrate.migrate(connection)
                chain = " | ".join(str(e) for e in (info.value, info.value.__cause__, info.value.__context__) if e)
                assert "migration 0008 refused" in chain
                connection.rollback()

            with connect(dsn) as connection:
                assert max(migrate.inspect_state(connection).applied) == 7
                with connection.cursor() as cur:
                    cur.execute("SELECT to_regclass('tests_org_active_name_unique')")
                    assert cur.fetchone()[0] is None
                    cur.execute("SELECT count(*), count(*) FILTER (WHERE status = 'active') FROM tests")
                    assert cur.fetchone() == (2, 2)  # both rows, both still active
        assert first != second


# ─── 0007: PostgreSQL refuses a cross-organisation order ─────────────────────


class TestPostgresRefusesACrossOrganisationOrder:
    def test_a_same_organisation_order_is_accepted(self, fresh):
        with connect(fresh) as connection, connection.cursor() as cur:
            org, patient, consent = _plant_org(cur, "A")
            test = _plant_test(cur, org, "Panel")
            _plant_order(cur, org, patient, consent, test)  # the positive twin
            connection.commit()

    def test_an_order_naming_another_organisations_test_is_refused_by_the_composite_key(self, fresh):
        with connect(fresh) as connection:
            with connection.cursor() as cur:
                org_a, patient_a, consent_a = _plant_org(cur, "A")
                org_b, _, _ = _plant_org(cur, "B")
                foreign = _plant_test(cur, org_b, "B panel")
            connection.commit()

            def attempt():
                with connection.cursor() as cur:
                    _plant_order(cur, org_a, patient_a, consent_a, foreign)

            assert _violation(connection, attempt) == ("23503", "orders_test_org_fk")

    def test_the_old_single_column_key_alone_would_have_accepted_it(self, fresh):
        """NON-VACUITY. Drop the new constraint in a scratch copy and the same
        insert succeeds -- so the refusal above is the new constraint's, not
        something else's."""
        with connect(fresh) as connection:
            with connection.cursor() as cur:
                org_a, patient_a, consent_a = _plant_org(cur, "A")
                org_b, _, _ = _plant_org(cur, "B")
                foreign = _plant_test(cur, org_b, "B panel")
                cur.execute("ALTER TABLE orders DROP CONSTRAINT orders_test_org_fk")
                _plant_order(cur, org_a, patient_a, consent_a, foreign)  # accepted: the gap
            connection.rollback()

    def test_a_test_cannot_be_moved_to_another_organisation_under_an_existing_order(self, fresh):
        with connect(fresh) as connection:
            with connection.cursor() as cur:
                org_a, patient_a, consent_a = _plant_org(cur, "A")
                org_b, _, _ = _plant_org(cur, "B")
                test = _plant_test(cur, org_a, "Panel")
                _plant_order(cur, org_a, patient_a, consent_a, test)
            connection.commit()

            def attempt():
                with connection.cursor() as cur:
                    cur.execute("UPDATE tests SET org_id = %s WHERE test_id = %s", (org_b, test))

            sqlstate, constraint = _violation(connection, attempt)
            assert sqlstate == "23503"  # an order still references (test_id, org_a)


# ─── 0008: PostgreSQL refuses a duplicate active name ────────────────────────


class TestPostgresRefusesADuplicateActiveName:
    def test_the_same_name_in_one_organisation_is_refused_case_insensitively(self, fresh):
        with connect(fresh) as connection:
            with connection.cursor() as cur:
                org, _, _ = _plant_org(cur, "A")
                _plant_test(cur, org, "BRCA panel")
            connection.commit()

            def attempt():
                with connection.cursor() as cur:
                    _plant_test(cur, org, "brca PANEL")

            assert _violation(connection, attempt) == ("23505", "tests_org_active_name_unique")

    def test_two_organisations_may_each_have_the_name(self, fresh):
        with connect(fresh) as connection, connection.cursor() as cur:
            org_a, _, _ = _plant_org(cur, "A")
            org_b, _, _ = _plant_org(cur, "B")
            _plant_test(cur, org_a, "BRCA panel")
            _plant_test(cur, org_b, "BRCA panel")
            connection.commit()

    def test_a_retired_test_releases_its_name_and_many_retired_may_share_it(self, fresh):
        with connect(fresh) as connection, connection.cursor() as cur:
            org, _, _ = _plant_org(cur, "A")
            _plant_test(cur, org, "BRCA panel", status="retired")
            _plant_test(cur, org, "BRCA panel", status="retired")
            _plant_test(cur, org, "BRCA panel")  # the replacement
            connection.commit()

    def test_retiring_the_active_one_frees_the_name_and_unretiring_a_duplicate_is_refused(self, fresh):
        with connect(fresh) as connection:
            with connection.cursor() as cur:
                org, _, _ = _plant_org(cur, "A")
                old = _plant_test(cur, org, "BRCA panel")
                cur.execute("UPDATE tests SET status = 'retired' WHERE test_id = %s", (old,))
                _plant_test(cur, org, "BRCA panel")
            connection.commit()

            def attempt():
                with connection.cursor() as cur:
                    cur.execute("UPDATE tests SET status = 'active' WHERE test_id = %s", (old,))

            assert _violation(connection, attempt) == ("23505", "tests_org_active_name_unique")

    def test_simultaneous_raw_inserts_of_one_name_let_exactly_one_through(self, fresh):
        psycopg = require_psycopg()
        with connect(fresh) as setup:
            with setup.cursor() as cur:
                org, _, _ = _plant_org(cur, "A")
            setup.commit()

        threads = 8
        barrier = threading.Barrier(threads)
        outcomes = []

        def worker():
            with connect(fresh) as connection:
                barrier.wait()
                try:
                    with connection.cursor() as cur:
                        _plant_test(cur, org, "Race panel")
                    connection.commit()
                    outcomes.append("ok")
                except psycopg.errors.UniqueViolation as exc:
                    connection.rollback()
                    outcomes.append(exc.diag.constraint_name)

        pool = [threading.Thread(target=worker) for _ in range(threads)]
        [t.start() for t in pool]
        [t.join(30) for t in pool]

        assert sorted(outcomes) == ["ok"] + ["tests_org_active_name_unique"] * (threads - 1), outcomes


# ─── the application over the index ──────────────────────────────────────────


@pytest.fixture()
def administrator(fresh):
    """An Administrator Session in a fresh database, and the org it belongs to."""
    from clinical.data_access import BcryptHasher, DataAccess

    with connect(fresh) as connection:
        dao = DataAccess(connection, password_hasher=BcryptHasher(rounds=4))
        org = dao.create_organisation("Org A")
        user = dao.create_user(org, "admin@a.example", "a-long-enough-password")
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                "VALUES (%s, %s, 'Administrator', %s, %s)",
                (user, org, user, T),
            )
        connection.commit()
        session = dao.login("admin@a.example", org, "a-long-enough-password")
    return fresh, session


class TestTheApplicationAnswersTheRaceAsAnOrdinaryRefusal:
    def test_concurrent_create_test_yields_one_success_and_the_rest_valueerror(self, administrator):
        from clinical.data_access import BcryptHasher, DataAccess

        dsn, session = administrator
        threads = 6
        barrier = threading.Barrier(threads)
        outcomes = []

        def worker():
            with connect(dsn) as connection:
                dao = DataAccess(connection, password_hasher=BcryptHasher(rounds=4))
                barrier.wait()
                try:
                    dao.create_test(session, "Race panel", "hg38", ["BRCA1"])
                    outcomes.append("ok")
                except ValueError:
                    outcomes.append("refused")
                except Exception as exc:  # noqa: BLE001 -- anything else is the bug this test hunts
                    outcomes.append(f"UNEXPECTED {type(exc).__name__}")

        pool = [threading.Thread(target=worker) for _ in range(threads)]
        [t.start() for t in pool]
        [t.join(60) for t in pool]

        assert sorted(outcomes) == ["ok"] + ["refused"] * (threads - 1), outcomes

    def test_the_index_alone_turns_into_the_same_valueerror_when_the_courtesy_check_is_blind(self, administrator):
        """Deterministic version of the race: blind the application's own
        pre-check so ONLY the index can refuse, and require the same message."""
        from clinical.data_access import BcryptHasher, DataAccess

        dsn, session = administrator
        with connect(dsn) as connection:
            dao = DataAccess(connection, password_hasher=BcryptHasher(rounds=4))
            dao.create_test(session, "BRCA panel", "hg38", ["BRCA1"])

            real = dao._query_one
            dao._query_one = lambda sql, params=(): None if "lower(name) = lower(%s)" in sql else real(sql, params)
            with pytest.raises(ValueError, match="already exists"):
                dao.create_test(session, "brca PANEL", "GRCh38", ["TP53"])
            dao._query_one = real

            listed = dao.list_tests(session)
            assert [t["name"] for t in listed] == ["BRCA panel"]  # no orphan, no half-written panel
            with connection.cursor() as cur:
                cur.execute("SELECT count(*) FROM test_genes")
                assert cur.fetchone()[0] == 1  # the refused attempt left no genes behind

    def test_retire_then_recreate_the_same_name_works_through_the_application(self, administrator):
        from clinical.data_access import BcryptHasher, DataAccess

        dsn, session = administrator
        with connect(dsn) as connection:
            dao = DataAccess(connection, password_hasher=BcryptHasher(rounds=4))
            first = dao.create_test(session, "BRCA panel", "hg38", ["BRCA1"])
            dao.retire_test(session, first)
            second = dao.create_test(session, "BRCA panel", "hg38", ["BRCA1", "BRCA2"])
            assert second != first
            names = [(t["name"], t["status"]) for t in dao.list_tests(session, include_retired=True)]
            assert sorted(names) == [("BRCA panel", "active"), ("BRCA panel", "retired")]


# ─── migration 0009: one live patient_portal release per report ──────────────


def _plant_report_chain(cur, org, patient, consent):
    """order -> sample -> sequencing run -> VCF -> interpretation -> report, by
    raw SQL (the generation under test predates what the domain methods expect).
    Returns (report_id, a user id that exists in the organisation)."""
    user = uuid.uuid4()
    cur.execute(
        "INSERT INTO users (user_id, org_id, email, password_hash, password_changed_at, created_at, updated_at) "
        "VALUES (%s, %s, %s, 'x', %s, %s, %s)",
        (user, org, f"u{user}@x.test", T, T, T),
    )
    test = _plant_test(cur, org, f"Panel {uuid.uuid4().hex[:6]}")
    order = _plant_order(cur, org, patient, consent, test)
    sample, run, vcf, interpretation, report = (uuid.uuid4() for _ in range(5))
    cur.execute(
        "INSERT INTO samples (sample_id, org_id, order_id, type, collected_at, collected_by, qc_status, created_at) "
        "VALUES (%s, %s, %s, 'blood', %s, %s, 'passed', %s)",
        (sample, org, order, T, user, T),
    )
    cur.execute(
        "INSERT INTO sequencing_runs (org_id, id, sample_id, created_at, created_by) VALUES (%s, %s, %s, %s, %s)",
        (org, run, sample, T, user),
    )
    cur.execute(
        "INSERT INTO vcfs (org_id, id, sequencing_run_id, vcf_path, content_hash, created_at, created_by) "
        "VALUES (%s, %s, %s, '/tmp/x.vcf', %s, %s, %s)",
        (org, vcf, run, "a" * 64, T, user),
    )
    cur.execute(
        "INSERT INTO interpretations (org_id, id, vcf_id, run_document, submission_key, created_at, created_by) "
        "VALUES (%s, %s, %s, '{}', %s, %s, %s)",
        (org, interpretation, vcf, f"k-{interpretation}", T, user),
    )
    cur.execute(
        "INSERT INTO reports (org_id, id, interpretation_id, created_at, created_by) VALUES (%s, %s, %s, %s, %s)",
        (org, report, interpretation, T, user),
    )
    return report, user


def _plant_release(cur, org, report, user, consumer, tombstoned=False):
    cur.execute(
        "INSERT INTO release_events (org_id, id, report_id, consumer, released_at, released_by, content_hash, "
        "tombstoned_at, tombstoned_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            org,
            uuid.uuid4(),
            report,
            consumer,
            T,
            user,
            "b" * 64,
            T if tombstoned else None,
            user if tombstoned else None,
        ),
    )


def _release_rows(connection):
    with connection.cursor() as cur:
        cur.execute(
            "SELECT id, report_id, consumer, released_at, released_by, content_hash, tombstoned_at, tombstoned_by "
            "FROM release_events ORDER BY id"
        )
        return cur.fetchall()


@pytest.fixture()
def fresh_release_world():
    """A factory: a scratch database migrated to generation `upto`, holding
    release data of every shape the domain allows. Torn down after the test."""
    import contextlib

    stack = contextlib.ExitStack()

    def build(upto, duplicate_live_portal=False):
        dsn = stack.enter_context(scratch_database("w167"))
        with connect(dsn) as connection:
            migrate.migrate(connection, _upto(upto))
            with connection.cursor() as cur:
                org, patient, consent = _plant_org(cur, "Org A")
                report, user = _plant_report_chain(cur, org, patient, consent)
                _plant_release(cur, org, report, user, "patient_portal")  # live
                _plant_release(cur, org, report, user, "Clinic A")
                _plant_release(cur, org, report, user, "Clinic A")  # the same external consumer twice
                _plant_release(cur, org, report, user, "LIMS")
                _plant_release(cur, org, report, user, "patient_portal", tombstoned=True)  # beside a live one
                if duplicate_live_portal:
                    _plant_release(cur, org, report, user, "patient_portal")
            connection.commit()
        return dsn, {"org": org, "report": report}

    try:
        yield build
    finally:
        stack.close()


class TestMigration0009:
    def test_it_is_declared_and_named(self):
        assert {m.version: m.name for m in migrate.discover()}[9] == "portal_release_once"

    def test_the_index_is_narrow_partial_and_unique(self):
        with scratch_database("w167") as dsn, connect(dsn) as connection:
            migrate.migrate(connection)
            with connection.cursor() as cur:
                cur.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'release_events_portal_once'")
                (definition,) = cur.fetchone()
        assert "UNIQUE" in definition and "(org_id, report_id)" in definition
        assert "'patient_portal'" in definition and "tombstoned_at IS NULL" in definition

    def test_existing_release_records_of_every_shape_survive_the_upgrade(self, fresh_release_world):
        """Generation 8 -> 9 over release data of every shape the domain allows:
        a live portal row, the same external consumer twice, other consumers,
        and a tombstoned portal row beside the live one. All valid, all kept."""
        dsn, _ = fresh_release_world(upto=8)
        with connect(dsn) as connection:
            before = _release_rows(connection)
            assert len(before) == 5
            # 0009 is what is under test; 0010 (call sets) is additive and follows it in the same upgrade
            assert migrate.migrate(connection) == (9, 10)
            assert migrate.inspect_state(connection).ok
            assert _release_rows(connection) == before  # not one row altered, added or removed

    def test_it_refuses_when_a_report_already_has_two_live_portal_events_and_changes_nothing(self, fresh_release_world):
        dsn, _ = fresh_release_world(upto=8, duplicate_live_portal=True)
        with connect(dsn) as connection:
            before = _release_rows(connection)
            with pytest.raises(Exception) as info:
                migrate.migrate(connection)
            chain = " | ".join(str(e) for e in (info.value, info.value.__cause__, info.value.__context__) if e)
            assert "migration 0009 refused" in chain and "1 report" in chain
            connection.rollback()
        with connect(dsn) as connection:
            assert max(migrate.inspect_state(connection).applied) == 8
            with connection.cursor() as cur:
                cur.execute("SELECT to_regclass('release_events_portal_once')")
                assert cur.fetchone()[0] is None
            assert _release_rows(connection) == before
