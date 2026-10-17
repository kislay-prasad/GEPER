-- clinical/migrations/0007_order_test_same_org.sql
-- ────────────────────────────────────────────────
-- AN ORDER MAY NOT REFERENCE A TEST THAT BELONGS TO ANOTHER ORGANISATION, AND
-- THE DATABASE SAYS SO ITSELF.
--
-- THE GAP. `orders.test_id` has always been a single-column foreign key to
-- `tests (test_id)`. `tests` carries `org_id`, `orders` carries `org_id`, and
-- nothing tied the two together: a row in organisation A could name a test of
-- organisation B and PostgreSQL would accept it. `DataAccess.create_order`
-- looks the test up WITH `org_id = session.org_id`, so the application refuses
-- it -- but that is one code path, and the rule "tenant isolation is enforced
-- below the application" is exactly what `orders_patient_fk` and
-- `orders_consent_fk` already do for the patient and the consent (both are
-- composite on (id, org_id)). The test reference was the odd one out.
--
-- THE FIX, IN THE SHAPE THE SCHEMA ALREADY USES. A composite foreign key
-- (test_id, org_id) -> tests (test_id, org_id). A foreign key must reference a
-- unique key, and `tests` has only its primary key on `test_id`, so the target
-- is first given `UNIQUE (test_id, org_id)`. That is trivially satisfiable by
-- every existing row -- `test_id` alone is already unique -- so the constraint
-- cannot fail on any database that satisfies the primary key.
--
-- WHAT IS NOT DONE, ON PURPOSE. The old single-column `orders.test_id` foreign
-- key is KEPT. Dropping it would be a destructive statement (the first of its
-- kind since 0005), it is implied by the new one and therefore harmless, and
-- the task is to ADD an integrity guarantee, not to tidy. A later migration
-- can drop it if anyone wants the catalogue smaller.
--
-- EXISTING DATA IS NEVER ALTERED TO MAKE THIS PASS. If any order already points
-- at another organisation's test, the migration REFUSES with the count and
-- changes nothing: the whole file is one transaction, so the refusal rolls the
-- unique constraint back with it. Those rows would be a tenant-isolation
-- breach that has already happened, and a human has to look at them; silently
-- repairing or deleting them would destroy the evidence.
--
-- NO GRANT CHANGE. Both tables are already granted at table level and a
-- constraint is not a privilege.
--
-- ─── THE POST-CONDITIONS ─────────────────────────────────────────────────────
-- Read out of pg_catalog (never information_schema, which is privilege-filtered
-- and is evaluated by /readyz as the narrowed clinical_app). Each is FALSE on
-- every generation before this one:
--   * the UNIQUE target exists on tests over exactly (test_id, org_id);
--   * the composite foreign key exists on orders, references tests, spans two
--     columns on each side, and is validated.
--
-- MIGRATION-ASSERT: EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = to_regclass('tests') AND conname = 'tests_id_org_unique' AND contype = 'u' AND array_length(conkey, 1) = 2)
-- MIGRATION-ASSERT: EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = to_regclass('orders') AND conname = 'orders_test_org_fk' AND contype = 'f' AND confrelid = to_regclass('tests') AND array_length(conkey, 1) = 2 AND array_length(confkey, 1) = 2 AND convalidated)

DO $$
DECLARE
    crossing bigint;
BEGIN
    SELECT count(*) INTO crossing
      FROM orders o
      JOIN tests t ON t.test_id = o.test_id
     WHERE t.org_id <> o.org_id;
    IF crossing > 0 THEN
        RAISE EXCEPTION
            'migration 0007 refused: % order(s) reference a test belonging to another organisation; nothing was changed',
            crossing;
    END IF;
END
$$;

ALTER TABLE tests
    ADD CONSTRAINT tests_id_org_unique UNIQUE (test_id, org_id);

ALTER TABLE orders
    ADD CONSTRAINT orders_test_org_fk
    FOREIGN KEY (test_id, org_id) REFERENCES tests (test_id, org_id);
