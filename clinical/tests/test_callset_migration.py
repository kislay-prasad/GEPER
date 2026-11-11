"""
Migration 0010 (CNV/SV call sets) against the REAL migration mechanism and the REAL schema.

What is verified here, each against a disposable database built from the repository's own files:

  * ORDER: 0010 follows 0009; a database at version 9 upgrades to 10 through the runner; a database built from
    schema.sql carries the same stamp (the equivalence test checks the catalogs match).
  * DEPENDENCIES: the parents it names exist and are the unique composite keys it references.
  * OWNERSHIP / PRIVILEGES: every new object is owned by the migrating role, never an application role; the one
    SECURITY DEFINER function is not executable by either application role or PUBLIC; every function pins its
    search_path with pg_temp last.
  * TEMP: not held by PUBLIC, clinical_app or clinical_retention, and a session acting as either role cannot create
    a temporary table.
  * ROLLBACK: a refusal or a failure part-way leaves the database exactly as it was -- no table, no TEMP change, no
    history row -- and an existing row survives the upgrade.
"""

from __future__ import annotations

import pathlib

import pytest

from clinical import migrate
from clinical.tests.migration_db import connect, scratch_database

MIGRATIONS = pathlib.Path(migrate.__file__).resolve().parent / "migrations"
NEW_TABLES = ("call_sets", "call_set_events", "caller_approvals")
NEW_FUNCTIONS = (
    "actor_holds_role",
    "actor_context",
    "caller_state",
    "call_set_state",
    "open_retraction_request",
    "call_sets_before_insert",
    "call_sets_require_submitted",
    "call_sets_immutable",
    "call_sets_require_redaction_audit",
    "call_set_events_before_insert",
    "call_set_events_after_insert",
    "call_set_events_immutable",
    "caller_approvals_before_insert",
    "caller_approvals_no_update",
    "callset_identity_guard",
)


def _upto(version: int):
    return tuple(m for m in migrate.discover() if m.version <= version)


def _scalar(connection, query, params=()):
    with connection.cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
    connection.rollback()
    return row[0] if row else None


def _at_version_9(dsn):
    with connect(dsn) as c:
        migrate.migrate(c, _upto(9))
        c.commit()


def _public_has_temp(connection) -> bool:
    return bool(
        _scalar(
            connection,
            "SELECT EXISTS (SELECT 1 FROM pg_database d, aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) a "
            "WHERE d.datname = current_database() AND a.grantee = 0 AND a.privilege_type = 'TEMPORARY')",
        )
    )


class TestOrderAndDependencies:
    def test_0010_is_the_next_version_after_0009(self):
        versions = [m.version for m in migrate.discover()]
        assert versions == sorted(versions) and versions[-2:] == [9, 10]
        assert {m.version: m.name for m in migrate.discover()}[10] == "call_sets"

    def test_it_declares_its_own_post_conditions(self):
        mig = {m.version: m for m in migrate.discover()}[10]
        assert len(mig.assertions) >= 5, "the file must carry real catalog post-conditions, not a placeholder"

    def test_upgrade_from_9_to_10_applies_exactly_one_migration_and_keeps_existing_rows(self):
        with scratch_database("callset_up") as dsn:
            _at_version_9(dsn)
            with connect(dsn) as c:
                with c.cursor() as cur:
                    cur.execute("INSERT INTO organisations (name, created_at) VALUES ('survivor org', now())")
                c.commit()
                assert migrate.migrate(c) == (10,)
                c.commit()
                assert _scalar(c, "SELECT count(*) FROM organisations WHERE name = 'survivor org'") == 1
                for t in NEW_TABLES:
                    assert _scalar(c, "SELECT to_regclass(%s) IS NOT NULL", (f"public.{t}",)) is True
                assert _scalar(c, "SELECT max(version) FROM schema_migrations") == 10
                # running it again is a no-op, not an error
                assert migrate.migrate(c) == ()

    def test_the_foreign_keys_land_on_the_real_parent_keys(self):
        with scratch_database("callset_fk") as dsn:
            _at_version_9(dsn)
            with connect(dsn) as c:
                migrate.migrate(c)
                c.commit()
                with c.cursor() as cur:
                    cur.execute(
                        "SELECT conname, confrelid::regclass::text FROM pg_constraint "
                        "WHERE conrelid = 'public.call_sets'::regclass AND contype = 'f' ORDER BY conname"
                    )
                    got = dict(cur.fetchall())
                assert got["fk_callset_run"] == "sequencing_runs"
                assert got["fk_callset_creator"] == "users"
                assert got["fk_callset_supersedes"] == "call_sets"


class TestOwnershipAndPrivileges:
    @pytest.fixture()
    def migrated(self):
        with scratch_database("callset_own") as dsn:
            _at_version_9(dsn)
            with connect(dsn) as c:
                migrate.migrate(c)
                c.commit()
            yield dsn

    def test_no_new_object_is_owned_by_an_application_role(self, migrated):
        with connect(migrated) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT c.relname, pg_get_userbyid(c.relowner) FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace "
                    "AND c.relname = ANY(%s) UNION ALL "
                    "SELECT p.proname, pg_get_userbyid(p.proowner) FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace "
                    "AND p.proname = ANY(%s)",
                    (list(NEW_TABLES), list(NEW_FUNCTIONS)),
                )
                rows = cur.fetchall()
            assert len(rows) == len(NEW_TABLES) + len(NEW_FUNCTIONS)
            owners = {owner for _name, owner in rows}
            assert owners.isdisjoint({"clinical_app", "clinical_retention"}), rows
            migrating_role = _scalar(c, "SELECT current_user")
            assert owners == {migrating_role}, "every new object belongs to the role that ran the migration"

    def test_the_security_definer_function_is_the_only_one_and_is_not_callable_by_the_app(self, migrated):
        with connect(migrated) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT p.proname FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace AND p.prosecdef ORDER BY 1"
                )
                assert [r[0] for r in cur.fetchall()] == ["call_sets_require_redaction_audit"]
            for role in ("clinical_app", "clinical_retention"):
                assert (
                    _scalar(
                        c,
                        "SELECT has_function_privilege(%s, 'public.call_sets_require_redaction_audit()', 'EXECUTE')",
                        (role,),
                    )
                    is False
                )
            assert (
                _scalar(
                    c,
                    "SELECT has_function_privilege('public', 'public.call_sets_require_redaction_audit()', 'EXECUTE')",
                )
                is False
            )

    def test_every_new_function_pins_search_path_with_pg_temp_last(self, migrated):
        with connect(migrated) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT p.proname, p.proconfig FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace AND p.proname = ANY(%s)",
                    (list(NEW_FUNCTIONS),),
                )
                rows = cur.fetchall()
            assert len(rows) == len(NEW_FUNCTIONS)
            for name, config in rows:
                assert config == ["search_path=pg_catalog, public, pg_temp"], (name, config)

    def test_the_application_roles_hold_exactly_the_documented_table_grants(self, migrated):
        with connect(migrated) as c:
            for t in NEW_TABLES:
                # call_sets is granted INSERT column by column (every column but the generated `seq`)
                assert _scalar(c, "SELECT has_table_privilege('clinical_app', %s, 'INSERT')", (f"public.{t}",)) is (
                    t != "call_sets"
                )
                assert _scalar(c, "SELECT has_table_privilege('clinical_app', %s, 'SELECT')", (f"public.{t}",)) is True
                for priv in ("UPDATE", "DELETE", "TRUNCATE"):
                    assert (
                        _scalar(c, "SELECT has_table_privilege('clinical_app', %s, %s)", (f"public.{t}", priv)) is False
                    ), (t, priv)
                for priv in ("INSERT", "DELETE", "TRUNCATE"):
                    assert (
                        _scalar(c, "SELECT has_table_privilege('clinical_retention', %s, %s)", (f"public.{t}", priv))
                        is False
                    ), (t, priv)
            retention_columns = {
                "call_sets": {
                    "tombstoned_at",
                    "tombstoned_by",
                    "source_vcf",
                    "parsed_document",
                    "content_redacted_at",
                    "content_redacted_by",
                },
                "call_set_events": {"tombstoned_at", "tombstoned_by"},
                "caller_approvals": set(),
            }
            for table, allowed in retention_columns.items():
                with c.cursor() as cur:
                    cur.execute(
                        "SELECT a.attname FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped "
                        "AND has_column_privilege('clinical_retention', a.attrelid, a.attnum, 'UPDATE')",
                        (f"public.{table}",),
                    )
                    assert {r[0] for r in cur.fetchall()} == allowed, table
                c.rollback()


class TestTempPrivilege:
    def test_temp_is_revoked_from_public_and_both_application_roles(self):
        with scratch_database("callset_temp") as dsn:
            _at_version_9(dsn)
            with connect(dsn) as c:
                # BEFORE: the default grant to PUBLIC is what makes a revoke from clinical_app alone meaningless
                assert _public_has_temp(c) is True
                assert _scalar(c, "SELECT has_database_privilege('clinical_app', current_database(), 'TEMP')") is True
                migrate.migrate(c)
                c.commit()
                assert _public_has_temp(c) is False
                for role in ("clinical_app", "clinical_retention"):
                    assert _scalar(c, "SELECT has_database_privilege(%s, current_database(), 'TEMP')", (role,)) is False

    @pytest.mark.parametrize("role", ["clinical_app", "clinical_retention"])
    def test_an_application_role_cannot_create_a_temporary_table(self, role):
        psycopg = pytest.importorskip("psycopg")
        with scratch_database("callset_temp2") as dsn:
            _at_version_9(dsn)
            with connect(dsn) as c:
                migrate.migrate(c)
                c.commit()
                with c.cursor() as cur:
                    cur.execute(f"SET ROLE {role}")
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        cur.execute("CREATE TEMP TABLE call_sets (x int)")
                c.rollback()


class TestRollbackAndRefusal:
    def _unchanged(self, c):
        assert _scalar(c, "SELECT max(version) FROM schema_migrations") == 9
        for t in NEW_TABLES:
            assert _scalar(c, "SELECT to_regclass(%s) IS NULL", (f"public.{t}",)) is True
        assert _public_has_temp(c) is True, (
            "the TEMP revoke is the LAST statement and must have rolled back with the rest"
        )

    def test_a_failure_in_the_middle_of_the_file_leaves_nothing_behind(self):
        psycopg = pytest.importorskip("psycopg")
        with scratch_database("callset_rb") as dsn:
            _at_version_9(dsn)
            with connect(dsn) as c:
                # a function of a name the migration creates, defined outside the mechanism: the file fails part-way
                with c.cursor() as cur:
                    cur.execute(
                        "CREATE FUNCTION public.call_set_state(uuid, uuid) RETURNS text LANGUAGE sql AS $$ SELECT 'x'::text $$"
                    )
                c.commit()
                with pytest.raises((psycopg.Error, migrate.MigrationError)):
                    migrate.migrate(c)
                c.rollback()
                self._unchanged(c)
                assert _scalar(c, "SELECT to_regclass('public.caller_approvals') IS NULL") is True

    def test_preexisting_call_sets_table_is_refused_before_anything_is_created(self):
        psycopg = pytest.importorskip("psycopg")
        with scratch_database("callset_pre") as dsn:
            _at_version_9(dsn)
            with connect(dsn) as c:
                with c.cursor() as cur:
                    cur.execute("CREATE TABLE public.call_set_events (x int)")
                c.commit()
                with pytest.raises((psycopg.Error, migrate.MigrationError)) as err:
                    migrate.migrate(c)
                c.rollback()
                assert "refused" in str(err.value) or "already exists" in str(err.value)
                assert _scalar(c, "SELECT max(version) FROM schema_migrations") == 9
                assert _scalar(c, "SELECT to_regclass('public.call_sets') IS NULL") is True
                assert _public_has_temp(c) is True

    def test_a_role_that_neither_owns_the_database_nor_is_superuser_is_refused(self):
        psycopg = pytest.importorskip("psycopg")
        with scratch_database("callset_own2") as dsn:
            _at_version_9(dsn)
            sql = (MIGRATIONS / "0010_call_sets.sql").read_text(encoding="utf-8")
            with connect(dsn) as c:
                with c.cursor() as cur:
                    cur.execute(
                        "DO $$ BEGIN CREATE ROLE callset_nobody; EXCEPTION WHEN duplicate_object THEN NULL; END $$"
                    )
                    cur.execute("GRANT USAGE ON SCHEMA public TO callset_nobody")
                c.commit()
                with c.cursor() as cur:
                    cur.execute("SET ROLE callset_nobody")
                    with pytest.raises(psycopg.errors.RaiseException) as err:
                        cur.execute(sql)
                c.rollback()
                assert "cannot revoke TEMP" in str(err.value)
                assert _scalar(c, "SELECT to_regclass('public.call_sets') IS NULL") is True
                assert _public_has_temp(c) is True


class TestSchemaSqlAgrees:
    def test_a_database_built_from_schema_sql_is_stamped_at_10_with_the_files_checksum(self):
        from clinical.bootstrap import apply_schema

        with scratch_database("callset_fresh") as dsn:
            with connect(dsn) as c:
                apply_schema(c)
                c.commit()
                assert (
                    _scalar(c, "SELECT checksum FROM schema_migrations WHERE version = 10")
                    == {m.version: m.checksum for m in migrate.discover()}[10]
                )
                assert _public_has_temp(c) is False
                assert (
                    _scalar(c, "SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND prosecdef")
                    >= 1
                )


class TestDeviationFromTheReviewedDesign:
    @staticmethod
    def _norm(text: str) -> str:
        return text.replace("\r\n", "\n")

    # The ONLY differences between the reviewed v3 text and the promoted body, each from the final independent review.
    # Anything else that differs fails the test below, so an unreviewed edit cannot ride along.
    REVIEW_DELTAS = (
        (
            "    IF NEW.supersedes_id IS NOT NULL THEN\n        IF public.call_set_state(",
            "    IF NEW.supersedes_id IS NOT NULL THEN\n"
            "        -- the SAME lock every lifecycle act and the retention purge take on that call set (key = its id, seed 1)\n"
            "        PERFORM pg_advisory_xact_lock(hashtextextended(NEW.supersedes_id::text, 1));\n"
            "        IF public.call_set_state(",
        ),
        (
            "                  AND public.call_set_state(c.org_id, c.id) IN ('pending_validation', 'validated')) THEN\n"
            "        RAISE EXCEPTION 'call_sets: the same file is already live on another sequencing run';",
            "                  AND c.tombstoned_at IS NULL\n"
            "                  AND NOT EXISTS (SELECT 1 FROM public.call_sets later\n"
            "                                   WHERE later.org_id = c.org_id AND later.sequencing_run_id = c.sequencing_run_id\n"
            "                                     AND later.record_class = c.record_class AND later.source_content_hash = c.source_content_hash\n"
            "                                     AND (later.created_at, later.id) > (c.created_at, c.id))\n"
            "                  AND NOT (public.call_set_state(c.org_id, c.id) = 'retracted'\n"
            "                           AND EXISTS (SELECT 1 FROM public.call_set_events e\n"
            "                                        WHERE e.org_id = c.org_id AND e.call_set_id = c.id AND e.event_type = 'retracted'\n"
            '                                          AND e.checks = \'{"reason_code": "wrong_run_placement"}\'::jsonb))) THEN\n'
            "        RAISE EXCEPTION 'call_sets: the same file was already submitted on another sequencing run and was not retracted for wrong-run placement';",
        ),
        (
            "        OR (event_type <> 'validated' AND checks = '{}'::jsonb)),",
            "        OR (event_type = 'retracted'\n"
            "            AND checks IN ('{}'::jsonb, '{\"reason_code\": \"wrong_run_placement\"}'::jsonb))\n"
            "        OR (event_type NOT IN ('validated', 'retracted') AND checks = '{}'::jsonb)),",
        ),
        (
            "    o public.call_sets%ROWTYPE := OLD;\nBEGIN\n",
            "    o public.call_sets%ROWTYPE := OLD;\nBEGIN\n"
            "    -- tombstoning/redaction serialises with every lifecycle act on this call set (same lock as the event trigger)\n"
            "    IF NEW.tombstoned_at IS DISTINCT FROM OLD.tombstoned_at OR NEW.content_redacted_at IS DISTINCT FROM OLD.content_redacted_at THEN\n"
            "        PERFORM pg_advisory_xact_lock(hashtextextended(OLD.id::text, 1));\n"
            "    END IF;\n",
        ),
        # ── wave 3 (second independent review): resubmission lineage; the same-run duplicate rule moves from a unique
        # index into the trigger so a fresh call set over seen bytes must be an explicit resubmission ──
        (
            "    supersedes_id        UUID,\n",
            "    supersedes_id        UUID,\n    resubmission_of      UUID,\n",
        ),
        (
            "    CONSTRAINT callset_not_self_superseding CHECK (supersedes_id IS NULL OR supersedes_id <> id),\n",
            "    CONSTRAINT callset_not_self_superseding CHECK (supersedes_id IS NULL OR supersedes_id <> id),\n"
            "    CONSTRAINT fk_callset_resubmission FOREIGN KEY (org_id, resubmission_of) REFERENCES public.call_sets (org_id, id),\n"
            "    CONSTRAINT callset_not_self_resubmission CHECK (resubmission_of IS NULL OR resubmission_of <> id),\n",
        ),
        (
            "CREATE UNIQUE INDEX callsets_no_duplicate  ON public.call_sets (org_id, sequencing_run_id, record_class, source_content_hash);",
            "CREATE INDEX        callsets_content       ON public.call_sets (org_id, sequencing_run_id, record_class, source_content_hash);",
        ),
        (
            "    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.org_id::text || NEW.source_content_hash, 0));\n",
            "    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.org_id::text || NEW.source_content_hash, 0));\n"
            "    -- SAME RUN (replaces the unique index, under the same lock): the same bytes may be live once, and bytes this run has\n"
            "    -- already seen may be submitted again ONLY as an explicit resubmission of a rejected or withdrawn call set.\n"
            "    IF EXISTS (SELECT 1 FROM public.call_sets c\n"
            "                WHERE c.org_id = NEW.org_id AND c.sequencing_run_id = NEW.sequencing_run_id\n"
            "                  AND c.record_class = NEW.record_class AND c.source_content_hash = NEW.source_content_hash\n"
            "                  AND c.tombstoned_at IS NULL\n"
            "                  AND public.call_set_state(c.org_id, c.id) IN ('pending_validation', 'validated')) THEN\n"
            "        RAISE EXCEPTION 'call_sets: this file is already live on this sequencing run';\n"
            "    END IF;\n"
            "    IF NEW.resubmission_of IS NULL THEN\n"
            "        IF EXISTS (SELECT 1 FROM public.call_sets c\n"
            "                    WHERE c.org_id = NEW.org_id AND c.sequencing_run_id = NEW.sequencing_run_id\n"
            "                      AND c.record_class = NEW.record_class AND c.source_content_hash = NEW.source_content_hash) THEN\n"
            "            RAISE EXCEPTION 'call_sets: this file was already submitted on this sequencing run; submit it again only as a resubmission of a rejected or withdrawn call set';\n"
            "        END IF;\n"
            "    ELSE\n"
            "        PERFORM pg_advisory_xact_lock(hashtextextended(NEW.resubmission_of::text, 1));\n"
            "        IF NOT EXISTS (SELECT 1 FROM public.call_sets p\n"
            "                        WHERE p.org_id = NEW.org_id AND p.id = NEW.resubmission_of\n"
            "                          AND p.sequencing_run_id = NEW.sequencing_run_id AND p.record_class = NEW.record_class\n"
            "                          AND p.source_content_hash = NEW.source_content_hash AND p.tombstoned_at IS NULL\n"
            "                          AND public.call_set_state(p.org_id, p.id) IN ('rejected', 'withdrawn')) THEN\n"
            "            RAISE EXCEPTION 'call_sets: a resubmission must name a rejected or withdrawn, live call set of this run with the same file';\n"
            "        END IF;\n"
            "    END IF;\n",
        ),
        # ── wave 4 (third independent review) ──
        (
            "    resubmission_of      UUID,\n",
            "    resubmission_of      UUID,\n    seq                  BIGINT      GENERATED ALWAYS AS IDENTITY UNIQUE,\n",
        ),
        (
            "                WHERE c.org_id = NEW.org_id AND c.sequencing_run_id = NEW.sequencing_run_id\n                  AND c.record_class = NEW.record_class AND c.source_content_hash = NEW.source_content_hash\n                  AND c.tombstoned_at IS NULL\n                  AND public.call_set_state(c.org_id, c.id) IN ('pending_validation', 'validated')) THEN",
            "                WHERE c.org_id = NEW.org_id AND c.sequencing_run_id = NEW.sequencing_run_id\n                  AND c.source_content_hash = NEW.source_content_hash\n                  AND c.tombstoned_at IS NULL\n                  AND public.call_set_state(c.org_id, c.id) IN ('pending_validation', 'validated')) THEN",
        ),
        (
            "                    WHERE c.org_id = NEW.org_id AND c.sequencing_run_id = NEW.sequencing_run_id\n                      AND c.record_class = NEW.record_class AND c.source_content_hash = NEW.source_content_hash) THEN\n            RAISE EXCEPTION 'call_sets: this file was already submitted on this sequencing run; submit it again only as a resubmission of a rejected or withdrawn call set';",
            "                    WHERE c.org_id = NEW.org_id AND c.sequencing_run_id = NEW.sequencing_run_id\n                      AND c.source_content_hash = NEW.source_content_hash) THEN\n            RAISE EXCEPTION 'call_sets: this file was already submitted on this sequencing run (under any evidence class); submit it again only as a resubmission of a rejected or withdrawn call set';",
        ),
        (
            "        PERFORM pg_advisory_xact_lock(hashtextextended(NEW.resubmission_of::text, 1));\n        IF NOT EXISTS (SELECT 1 FROM public.call_sets p",
            "        IF NOT EXISTS (SELECT 1 FROM public.call_sets p",
        ),
        (
            "    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.org_id::text || NEW.source_content_hash, 0));\n    -- SAME RUN",
            "    -- LOCK ORDER (one hierarchy, shared with the DAO): call-set locks (key = id, seed 1) first, then content locks (seeds 0, 3, 4)\n    IF NEW.resubmission_of IS NOT NULL THEN\n        PERFORM pg_advisory_xact_lock(hashtextextended(NEW.resubmission_of::text, 1));\n    END IF;\n    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.org_id::text || NEW.source_content_hash, 0));\n    -- SAME RUN",
        ),
        (
            "                                   WHERE later.org_id = c.org_id AND later.sequencing_run_id = c.sequencing_run_id\n                                     AND later.record_class = c.record_class AND later.source_content_hash = c.source_content_hash\n                                     AND (later.created_at, later.id) > (c.created_at, c.id))",
            "                                   WHERE later.org_id = c.org_id AND later.sequencing_run_id = c.sequencing_run_id\n                                     AND later.source_content_hash = c.source_content_hash AND later.seq > c.seq)",
        ),
        (
            "    cs_created_by UUID; cs_tomb TIMESTAMPTZ; cs_name TEXT; cs_version TEXT; cs_class TEXT;",
            "    cs_created_by UUID; cs_tomb TIMESTAMPTZ; cs_name TEXT; cs_version TEXT; cs_class TEXT;\n    cs_run UUID; cs_assembly TEXT; cs_supersedes UUID;",
        ),
        (
            "    SELECT created_by, tombstoned_at, caller_name, caller_version, record_class\n      INTO cs_created_by, cs_tomb, cs_name, cs_version, cs_class\n",
            "    SELECT created_by, tombstoned_at, caller_name, caller_version, record_class, sequencing_run_id, assembly, supersedes_id\n      INTO cs_created_by, cs_tomb, cs_name, cs_version, cs_class, cs_run, cs_assembly, cs_supersedes\n",
        ),
        (
            "                RAISE EXCEPTION 'call_set_events: the caller % % is not currently approved for %', cs_name, cs_version, cs_class;\n            END IF;\n",
            "                RAISE EXCEPTION 'call_set_events: the caller % % is not currently approved for %', cs_name, cs_version, cs_class;\n            END IF;\n            -- ONE active validated call set per run, evidence class and assembly. A second one must replace the first\n            -- (supersedes_id), whose supersession this validation records atomically.\n            PERFORM pg_advisory_xact_lock(hashtextextended(NEW.org_id::text || '|' || cs_run::text || '|' || cs_class || '|' || cs_assembly, 4));\n            IF EXISTS (SELECT 1 FROM public.call_sets o\n                        WHERE o.org_id = NEW.org_id AND o.sequencing_run_id = cs_run AND o.record_class = cs_class\n                          AND o.assembly = cs_assembly AND o.id <> NEW.call_set_id AND o.tombstoned_at IS NULL\n                          AND o.id IS DISTINCT FROM cs_supersedes\n                          AND public.call_set_state(o.org_id, o.id) = 'validated') THEN\n                RAISE EXCEPTION 'call_set_events: another call set is already validated for this run, evidence class and assembly; replace it explicitly';\n            END IF;\n",
        ),
        (
            "CREATE UNIQUE INDEX callsets_one_successor ON public.call_sets (org_id, supersedes_id) WHERE supersedes_id IS NOT NULL;",
            "CREATE INDEX        callsets_successor     ON public.call_sets (org_id, supersedes_id) WHERE supersedes_id IS NOT NULL;",
        ),
        (
            "            RAISE EXCEPTION 'call_sets: a replacement may only name a live call set that is currently validated';\n        END IF;\n",
            "            RAISE EXCEPTION 'call_sets: a replacement may only name a live call set that is currently validated';\n        END IF;\n        -- ONE live successor (replaces the unique index): a rejected or withdrawn earlier replacement does not count\n        IF EXISTS (SELECT 1 FROM public.call_sets x\n                    WHERE x.org_id = NEW.org_id AND x.supersedes_id = NEW.supersedes_id AND x.tombstoned_at IS NULL\n                      AND public.call_set_state(x.org_id, x.id) NOT IN ('rejected', 'withdrawn')) THEN\n            RAISE EXCEPTION 'call_sets: this call set already has a replacement in progress or in force';\n        END IF;\n",
        ),
        # ── wave 5 (fourth independent review): the application role cannot choose the ordering column ──
        (
            "GRANT  SELECT, INSERT ON public.call_sets, public.call_set_events, public.caller_approvals TO clinical_app;\n",
            "GRANT  SELECT, INSERT ON public.call_set_events, public.caller_approvals TO clinical_app;\nGRANT  SELECT ON public.call_sets TO clinical_app;\n-- INSERT on call_sets is granted COLUMN BY COLUMN, every column but `seq`: the ordering column is generated by the\n-- database and the application role cannot supply it (INSERT ... OVERRIDING SYSTEM VALUE is refused).\nDO $$\nDECLARE cols TEXT;\nBEGIN\n    SELECT string_agg(quote_ident(attname), ', ' ORDER BY attnum) INTO cols\n      FROM pg_attribute\n     WHERE attrelid = 'public.call_sets'::regclass AND attnum > 0 AND NOT attisdropped AND attname <> 'seq';\n    EXECUTE 'GRANT INSERT (' || cols || ') ON public.call_sets TO clinical_app';\nEND $$;\n",
        ),
    )

    def test_the_migration_body_is_the_reviewed_v3_sql_plus_only_the_declared_review_deltas(self):
        design = MIGRATIONS.parent.parent / "docs" / "design" / "genome_callsets" / "0010_call_sets.proposed.sql"
        if not design.exists():
            pytest.skip("design document not present in this checkout")
        text = self._norm(design.read_text(encoding="utf-8"))
        body = text[
            text.index("-- ─── helpers (all read-only") : text.index("-- MIGRATION-ASSERT (when promoted")
        ].rstrip("\n")
        for old, new in self.REVIEW_DELTAS:
            assert body.count(old) == 1, old[:60]
            body = body.replace(old, new, 1)
        promoted = self._norm((MIGRATIONS / "0010_call_sets.sql").read_text(encoding="utf-8"))
        guard_start = promoted.index("-- ─── IDENTITY: nothing a call set")
        guard_end = promoted.index("-- ─── GRANTS (same pattern")
        assert body in promoted[:guard_start] + promoted[guard_end:]
        assert "CREATE FUNCTION public.callset_identity_guard()" in promoted[guard_start:guard_end]

    def test_schema_sql_carries_the_same_statements(self):
        schema = self._norm((MIGRATIONS.parent / "schema.sql").read_text(encoding="utf-8"))
        promoted = self._norm((MIGRATIONS / "0010_call_sets.sql").read_text(encoding="utf-8"))
        body = promoted[promoted.index("-- ─── helpers (all read-only") :].rstrip("\n")
        assert body in schema
