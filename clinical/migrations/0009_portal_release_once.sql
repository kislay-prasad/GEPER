-- clinical/migrations/0009_portal_release_once.sql
-- ────────────────────────────────────────────────
-- A REPORT IS RELEASED TO THE PATIENT PORTAL AT MOST ONCE AT A TIME, AND THE
-- DATABASE IS WHAT SAYS SO.
--
-- THE GAP, MEASURED. `DataAccess.release_report` refuses a report that already
-- has a live `patient_portal` release event, but it does so with a SELECT
-- followed by an INSERT. Two requests arriving together both read "none" and
-- both insert. Run eight at once against PostgreSQL 16, that produced EIGHT
-- release_events rows and EIGHT 'report_released' success audit rows for one
-- release, on three runs out of three. A duplicate is not harmless: each row is
-- a recorded delivery, so the delivery count is wrong, and each success audit
-- row asserts a release that did not happen. The only control that holds under
-- concurrency is a unique index, evaluated under the insert's own lock.
--
-- THE SHAPE. A PARTIAL UNIQUE INDEX on (org_id, report_id)
--   WHERE consumer = 'patient_portal' AND tombstoned_at IS NULL.
--   * IT IS NARROW ON PURPOSE. The domain has always allowed one report to reach
--     many consumers, and the SAME consumer more than once: the retraction debt
--     is "one per distinct consumer" (uk_retraction_notification_consumer)
--     exactly because re-delivery happens. A unique key on (report, consumer)
--     for every consumer would forbid a thing the schema's own comments
--     describe as ordinary. Only the patient portal -- the one delivery path
--     that exists, and one that is a PUBLICATION rather than a transmission --
--     is limited to once.
--   * `tombstoned_at IS NULL`: a row retention has tombstoned no longer counts,
--     so it never blocks a later release.
--   * `org_id` first, so the key is tenant-scoped like every other key here.
--
-- COUPLING, STATED. The literal 'patient_portal' below is
-- `clinical.data_access.PORTAL_RELEASE_CONSUMER`. They are tied by a test
-- (test_w169), not by a convention; changing one without the other fails it.
--
-- EXISTING DATA IS NEVER ALTERED. If any report already has two or more live
-- `patient_portal` events, the migration REFUSES with the count and creates
-- nothing; the file is one transaction. Which duplicate to tombstone is a
-- retention/governance decision. (None can exist through the application
-- before this wave: nothing wrote the value 'patient_portal' until
-- `release_report`, and rows with every other consumer are untouched.)
--
-- ADDITIVE. One index; no column, constraint, grant or row is touched, so every
-- existing release record stays valid. It takes a short SHARE lock on
-- `release_events`; CONCURRENTLY is not used because it cannot run inside the
-- transaction this runner wraps every migration in.
--
-- MIGRATION-ASSERT: EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = 'release_events_portal_once' AND i.indrelid = to_regclass('release_events') AND i.indisunique AND i.indisvalid AND i.indpred IS NOT NULL AND strpos(pg_get_expr(i.indpred, i.indrelid), 'patient_portal') > 0 AND strpos(pg_get_expr(i.indpred, i.indrelid), 'tombstoned_at IS NULL') > 0 AND strpos(pg_get_indexdef(i.indexrelid), '(org_id, report_id)') > 0)

DO $$
DECLARE
    duplicated bigint;
BEGIN
    SELECT count(*) INTO duplicated
      FROM (
            SELECT org_id, report_id
              FROM release_events
             WHERE consumer = 'patient_portal' AND tombstoned_at IS NULL
             GROUP BY org_id, report_id
            HAVING count(*) > 1
           ) d;
    IF duplicated > 0 THEN
        RAISE EXCEPTION
            'migration 0009 refused: % report(s) already have more than one live patient_portal release event; nothing was changed',
            duplicated;
    END IF;
END
$$;

CREATE UNIQUE INDEX release_events_portal_once
    ON release_events (org_id, report_id)
    WHERE consumer = 'patient_portal' AND tombstoned_at IS NULL;
