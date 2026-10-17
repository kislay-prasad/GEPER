-- clinical/migrations/0008_active_test_name_unique.sql
-- ────────────────────────────────────────────────────
-- TWO ACTIVE TESTS IN ONE ORGANISATION MAY NOT SHARE A NAME, AND THE DATABASE
-- IS WHAT SAYS SO.
--
-- THE GAP. `DataAccess.create_test` checks for an active test of the same
-- name before inserting. A check followed by an insert is a race: two
-- administrators submitting the same name at the same instant both pass the
-- check and both insert. Only a unique index closes that, because the index is
-- evaluated under the insert's own lock.
--
-- THE SHAPE. A PARTIAL UNIQUE INDEX on (org_id, lower(name)) WHERE status =
-- 'active'.
--   * `org_id` first: names are unique PER organisation; two organisations may
--     each have "BRCA panel".
--   * `lower(name)`: the application already compares case-insensitively
--     ("BRCA panel" and "brca PANEL" are the same test), so the index must be
--     at least as strict or it would admit what the application refuses.
--   * `WHERE status = 'active'`: a RETIRED test releases its name. Tests are
--     retired, never edited, so replacing a panel means retiring the old entry
--     and creating a new one of the same name; that has to keep working, and
--     it is the business rule the application has always applied.
-- It is an index, not a table constraint, because PostgreSQL cannot attach a
-- predicate or an expression to UNIQUE (...) in a constraint.
--
-- EXISTING DATA IS NEVER ALTERED. If any organisation already has two active
-- tests whose names match case-insensitively, the migration REFUSES with the
-- count and creates nothing; the file is one transaction. Choosing which of
-- the duplicates to retire is a clinical-governance decision, not this file's.
-- (No such rows can exist through the application: until Stage 2 nothing wrote
-- `tests` at all.)
--
-- ADDITIVE. One index; no column, constraint, grant or row is touched.
-- `CREATE INDEX` here takes a short SHARE lock on `tests`, which is a small
-- catalogue table; CONCURRENTLY is not used because it cannot run inside the
-- transaction this runner wraps every migration in.
--
-- MIGRATION-ASSERT: EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = 'tests_org_active_name_unique' AND i.indrelid = to_regclass('tests') AND i.indisunique AND i.indisvalid AND i.indpred IS NOT NULL AND strpos(pg_get_expr(i.indpred, i.indrelid), 'active') > 0 AND strpos(pg_get_indexdef(i.indexrelid), 'lower(name)') > 0)

DO $$
DECLARE
    duplicates bigint;
BEGIN
    SELECT count(*) INTO duplicates
      FROM (
            SELECT org_id, lower(name)
              FROM tests
             WHERE status = 'active'
             GROUP BY org_id, lower(name)
            HAVING count(*) > 1
           ) d;
    IF duplicates > 0 THEN
        RAISE EXCEPTION
            'migration 0008 refused: % group(s) of active tests share a name within one organisation; nothing was changed',
            duplicates;
    END IF;
END
$$;

CREATE UNIQUE INDEX tests_org_active_name_unique
    ON tests (org_id, lower(name))
    WHERE status = 'active';
