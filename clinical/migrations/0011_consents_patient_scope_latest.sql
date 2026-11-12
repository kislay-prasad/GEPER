-- clinical/migrations/0011_consents_patient_scope_latest.sql
-- ──────────────────────────────────────────────────────────
-- STAGE 1B: THE INDEX THE CONSENT RULE'S "LATEST CONSENT" LOOKUP NEEDS.
--
-- WHAT IT IS FOR, AND WHAT IT IS NOT. The consent rule (Policy C, clinical/data_access.py
-- `_CALLSET_LATEST_CONSENT_OK_SQL`) asks, per order, for the patient's LATEST consent for the
-- order's scope:  WHERE org_id = .. AND patient_id = .. AND scope = ..  ORDER BY recorded_at DESC,
-- consent_id DESC LIMIT 1.  No existing index can answer that (`consents_patient_active` is
-- partial: it omits withdrawn and superseded rows, which the rule must see), so every probe
-- scans the table. MEASURED on a synthetic database of 300,000 consents: about 20 ms per probe
-- without this index and about 0.02 ms with it; a patient with 1,001 orders took about 15 s
-- without it. THE INDEX CHANGES THE PLAN AND NOTHING ELSE: the rule, its tie-break and every
-- answer are unchanged (the answers were compared with and without it). It is a performance
-- object, not a correctness one.
--
-- THE DEFINITION (one authoritative shape; `clinical/schema.sql` carries the same statement and
-- test_w126m_schema_equivalence.py compares the two byte for byte):
--     CREATE INDEX consents_patient_scope_latest
--         ON consents (org_id, patient_id, scope, recorded_at DESC, consent_id DESC);
--   * a plain (non-unique, non-partial, expression-free, no INCLUDE) B-tree;
--   * default operator classes and the columns' own collations;
--   * keys 1-3 ascending (`indoption` 0 = ASC, NULLS LAST), keys 4-5 descending
--     (`indoption` 3 = DESC | NULLS FIRST). PostgreSQL 16 defines INDOPTION_DESC = 0x0001 and
--     INDOPTION_NULLS_FIRST = 0x0002 (src/include/catalog/pg_index.h); DESC defaults to
--     NULLS FIRST. All five columns are NOT NULL, so null placement cannot change which rows
--     match; it is pinned exactly anyway so the planner can use the index for the ORDER BY.
--
-- THIS MIGRATION DOES NOT BUILD THE INDEX ON A TABLE THAT HOLDS ROWS (the agreed "Option C").
-- A plain CREATE INDEX takes a SHARE lock on `consents` for the whole build and would block
-- consent writes; CREATE INDEX CONCURRENTLY cannot run inside the transaction this runner wraps
-- every migration in (clinical/migrate.py `_apply`), and this change adds no non-transactional
-- mechanism. So:
--   * `consents` HOLDS NO ROWS (a new database): the build is instantaneous, the migration
--     builds the index itself, then validates it.
--   * `consents` HOLDS ROWS: the OPERATOR must already have built the index, CONCURRENTLY, under
--     the agreed name (docs/CLINICAL_DEPLOYMENT.md, "Upgrading to migration 0011"). The
--     migration then ADOPTS it after validating it strictly. If it is absent, invalid or
--     different in any respect, the migration RAISES: the file is one transaction, so nothing is
--     recorded and nothing is changed, and the init job exits non-zero so the API does not
--     start. IT NEVER REPAIRS, REBUILDS OR REMOVES A LEFTOVER: an interrupted concurrent build
--     leaves an INVALID index of the agreed name, and clearing that is the operator's act.
-- The error text names the condition, never a row.
--
-- WHY THE STRICT CHECKS ARE IN THE BODY AND NOT ONLY IN THE HEADER. `migrate()` evaluates
-- header assertions only AFTER a migration has committed (`_apply` records the history row and
-- commits; `inspect_state` runs on /readyz and on the CLI's closing verdict). A RAISE inside
-- the migration is the only thing that rolls the migration back before it is recorded.
--
-- THE PERMANENT POST-CONDITION BELOW IS A CAPABILITY, NOT A NAME. Assertions of an applied
-- migration are re-evaluated for ever (every /readyz) and the file is checksum-frozen, so an
-- assertion naming this index would turn a later, legitimate replacement into permanent
-- `schema_drift`. It therefore asks only whether SOME valid, ready, live B-tree index on
-- `consents` has the five key columns in this order with these directions, default operator
-- classes and column-matching collations, no predicate, no expressions and no INCLUDE columns.
-- The exact name, non-uniqueness and absence of storage options are ADOPTION-ONLY properties,
-- checked once by the body below and pinned afterwards for every installation by the schema
-- equivalence test. Replacement strategy: add the new index first (the capability stays true),
-- remove the old one in a LATER migration.
--
-- Read from pg_catalog (never information_schema, which is privilege-filtered and which /readyz
-- evaluates as the narrowed clinical_app). `indkey`, `indclass`, `indcollation` and `indoption`
-- are vectors; they are cast to arrays and walked together WITH ORDINALITY, and `indoption` is
-- compared per key rather than as an array (an int2vector's subscripts start at 0, an array's
-- at 1, and array equality compares those bounds).
--
-- NO DATA, GRANT OR CONSTRAINT IS TOUCHED. One index, or none.
--
-- MIGRATION-ASSERT: EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_am am ON am.oid = c.relam WHERE i.indrelid = to_regclass('consents') AND am.amname = 'btree' AND i.indisvalid AND i.indisready AND i.indislive AND i.indpred IS NULL AND i.indexprs IS NULL AND i.indnkeyatts = 5 AND i.indnatts = 5 AND (SELECT count(*) FROM unnest(i.indkey::int2[], i.indclass::oid[], i.indcollation::oid[], i.indoption::int2[]) WITH ORDINALITY AS t(attnum, opc, coll, opt, ord) JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = t.attnum JOIN pg_opclass oc ON oc.oid = t.opc WHERE a.attname = (ARRAY['org_id','patient_id','scope','recorded_at','consent_id'])[t.ord::int] AND t.opt = (ARRAY[0,0,0,3,3])[t.ord::int] AND oc.opcdefault AND oc.opcmethod = c.relam AND oc.opcintype = a.atttypid AND t.coll = a.attcollation) = 5)

DO $$
DECLARE
    tbl     regclass := to_regclass('consents');
    idx     regclass := to_regclass('consents_patient_scope_latest');
    matched bigint;
    shape   boolean;
BEGIN
    IF tbl IS NULL THEN
        RAISE EXCEPTION 'migration 0011 refused: table consents does not exist; nothing was changed';
    END IF;

    IF idx IS NULL THEN
        IF EXISTS (SELECT 1 FROM consents) THEN
            RAISE EXCEPTION
                'migration 0011 refused: table consents holds rows and index consents_patient_scope_latest has not been built; build it first, without blocking writers, as docs/CLINICAL_DEPLOYMENT.md describes, then run the migration again; nothing was changed';
        END IF;
        -- An empty table: the build is instantaneous and blocks nothing.
        EXECUTE 'CREATE INDEX consents_patient_scope_latest ON consents (org_id, patient_id, scope, recorded_at DESC, consent_id DESC)';
        idx := to_regclass('consents_patient_scope_latest');
    END IF;

    -- It must be an index, and an index ON consents.
    IF NOT EXISTS (
        SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
         WHERE i.indexrelid = idx AND c.relkind = 'i' AND i.indrelid = tbl
    ) THEN
        RAISE EXCEPTION
            'migration 0011 refused: a relation named consents_patient_scope_latest exists but is not an index on consents; nothing was changed';
    END IF;

    -- An interrupted concurrent build leaves an index that is not valid, ready and live. It is
    -- reported, never repaired: clearing it is the operator's act.
    IF NOT EXISTS (
        SELECT 1 FROM pg_index i
         WHERE i.indexrelid = idx AND i.indisvalid AND i.indisready AND i.indislive
    ) THEN
        RAISE EXCEPTION
            'migration 0011 refused: index consents_patient_scope_latest exists but is not valid, ready and live (an interrupted concurrent build leaves such an index); this migration does not repair it; remove it by hand, build it again as docs/CLINICAL_DEPLOYMENT.md describes, then run the migration again; nothing was changed';
    END IF;

    -- Everything else about the agreed definition.
    SELECT count(*) INTO matched
      FROM pg_index i
      JOIN pg_class c ON c.oid = i.indexrelid
      JOIN pg_am am ON am.oid = c.relam
      CROSS JOIN LATERAL unnest(i.indkey::int2[], i.indclass::oid[], i.indcollation::oid[], i.indoption::int2[])
                   WITH ORDINALITY AS t(attnum, opc, coll, opt, ord)
      JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = t.attnum
      JOIN pg_opclass oc ON oc.oid = t.opc
     WHERE i.indexrelid = idx
       AND a.attname = (ARRAY['org_id','patient_id','scope','recorded_at','consent_id'])[t.ord::int]
       AND t.opt = (ARRAY[0,0,0,3,3])[t.ord::int]
       AND oc.opcdefault AND oc.opcmethod = c.relam AND oc.opcintype = a.atttypid
       AND t.coll = a.attcollation;

    SELECT (am.amname = 'btree'
            AND NOT i.indisunique AND NOT i.indisprimary AND NOT i.indisexclusion
            AND i.indpred IS NULL AND i.indexprs IS NULL
            AND i.indnkeyatts = 5 AND i.indnatts = 5
            AND c.reloptions IS NULL)
      INTO shape
      FROM pg_index i
      JOIN pg_class c ON c.oid = i.indexrelid
      JOIN pg_am am ON am.oid = c.relam
     WHERE i.indexrelid = idx;

    IF matched IS DISTINCT FROM 5 OR shape IS NOT TRUE THEN
        RAISE EXCEPTION
            'migration 0011 refused: index consents_patient_scope_latest does not match the agreed definition (non-unique B-tree on consents (org_id, patient_id, scope, recorded_at DESC, consent_id DESC), default operator classes and collations, no predicate, no expression, no INCLUDE column, no storage options); nothing was changed';
    END IF;
END
$$;
