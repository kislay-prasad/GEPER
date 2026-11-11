-- clinical/migrations/0010_call_sets.sql
-- ───────────────────────────────────────
-- CNV / STRUCTURAL-VARIANT CALL SETS: IMMUTABLE EVIDENCE, AN APPEND-ONLY LIFECYCLE, AND AN
-- APPEND-ONLY LOG OF WHICH CALLER VERSIONS THE LABORATORY HAS APPROVED.
--
-- PROMOTED FROM the reviewed design `docs/design/genome_callsets/0010_call_sets.proposed.sql` (v3). The
-- statement bodies are that file's, verbatim; what this file adds is the migration envelope: the preflight
-- refusals below and the post-conditions at the foot. Read
-- `docs/GENOME_ANALYSIS_CALLSET_IMPLEMENTATION_REVIEW.md` for the reasoning behind every control.
--
-- ORDER. Version 10, after 0009. It depends only on objects the baseline (0002) already created --
-- organisations, users(org_id, user_id), role_assignments, sequencing_runs(org_id, id), vcfs, audit_log --
-- and on the two login roles from `clinical/bootstrap.py`. Nothing in 0003..0009 is touched.
--
-- ADDITIVE. Three new tables, fifteen new functions, their triggers and grants, and one database-level
-- privilege change (TEMP, below). No existing table, column, constraint, row or grant is altered.
--
-- THE FOUR ACTS ARE FOUR RECORDS. Ingestion (call_sets + a 'submitted' event), validation ('validated' /
-- 'rejected'), approval of the CALLER VERSION (caller_approvals) and any clinical interpretation or approval
-- (NOT HERE). No column holds clinical meaning: 'detected'/'uncertain' inside parsed_document are the caller's
-- FILTER status only.
--
-- WHAT THE DATABASE ENFORCES, AND WHAT IT DOES NOT. It enforces SEPARATION OF RECORDED ACTORS: the actor ids
-- written into rows must hold the claimed roles, and a validator's recorded id may not equal the submitter's.
-- It does NOT authenticate a human: the application connects as one login and SUPPLIES every actor id. The
-- `clinical.actor_id` context check guards an application BUG (a forgotten or mismatched actor); a malicious
-- application can set it to anything and it is NOT a security boundary. The table owner, a superuser, and any
-- role able to disable triggers are outside every rule here.
--
-- OBJECT OWNERSHIP. Every object is owned by the role that runs this migration (the administrative DSN's role),
-- never by clinical_app or clinical_retention; the post-conditions below fail `migrate --verify` (which the deploy runs after `--migrate`) and `/readyz` if that is not so.
-- The one SECURITY DEFINER function (call_sets_require_redaction_audit) therefore runs with the
-- administrative role's rights, but: its search_path is pinned with pg_temp last, every name in it is
-- schema-qualified, it returns TRIGGER (so it cannot be called from SQL), EXECUTE is revoked from PUBLIC, and it
-- reads one table and writes nothing.
--
-- TEMP. The privilege to create temporary tables is revoked from PUBLIC (where the default grant lives) and from
-- both application roles, for THIS database. That is database-wide: it also removes TEMP from any other role in
-- this database that held it only through PUBLIC. The repository creates no temporary tables (grep-verified);
-- an operator who runs ad-hoc sessions that need TEMP must grant it back to the specific role. Together with the
-- pinned search_path it closes the pg_temp shadowing route (review finding B-1). The REVOKE needs the DATABASE
-- owner or a superuser; the migration refuses, and changes nothing, if it cannot be done.
--
-- FORWARD-ONLY, ONE TRANSACTION. A failure anywhere rolls the whole file back, leaving the database exactly as
-- it was (verified on a disposable database). There is no down-migration; to remove the feature a later numbered
-- migration must drop these objects.
--
-- WHAT THIS MIGRATION DOES NOT SETTLE (policy, not mechanism -- see the review document, section 16): the final
-- retention duration, the backup/WAL/replica erasure policy, the metadata allowed to survive redaction, and what
-- to do with call sets whose run has no VCF and no released report.
--
-- REVIEW-DRIVEN CHANGES TO THE v3 TEXT (final independent review, H-1/M-1/M-6; this file was never applied to a real
-- database, so it was edited rather than followed by an 0011): (1) one advisory lock per call set, taken by every
-- lifecycle event, by a replacement naming it, and by its tombstone/redaction; (2) the cross-run duplicate rule now refuses
-- any live earlier submission of the same bytes unless it was retracted with reason_code wrong_run_placement;
-- (3) callset_identity_guard() and FIVE triggers refuse repointing run/sample/order/test/consent identity under a call set;
-- (4) wave 3: resubmission_of lineage; the same-run duplicate rule moves from a unique index into the trigger (a fresh call set
-- over bytes the run has seen must be a resubmission of a rejected or withdrawn one); the guard also covers
-- orders.required_scope/consent_id and the meaning of a consent; a post-condition verifies the five triggers.
-- (5) wave 4: the database orders call sets itself (seq); duplicate protection ignores the evidence class; ONE active validated
-- call set per run, evidence class and assembly; a consent supersession must point at a newer consent; the prior-call-set
-- lock is taken before the content locks (one lock hierarchy shared with the DAO).
--
-- MIGRATION-ASSERT: (SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND NOT t.tgisinternal AND t.tgfoid = 'public.callset_identity_guard()'::regprocedure AND t.tgenabled = 'O' AND t.tgtype = 19 AND (c.relname::text, t.tgname::text, (SELECT array_agg(a.attname::text ORDER BY a.attname::text COLLATE "C") FROM unnest(t.tgattr::int2[]) AS k(attnum) JOIN pg_attribute a ON a.attrelid = t.tgrelid AND a.attnum = k.attnum)) IN (VALUES ('sequencing_runs', 'trg_callset_identity_runs', ARRAY['org_id', 'sample_id']), ('samples', 'trg_callset_identity_samples', ARRAY['order_id', 'org_id']), ('orders', 'trg_callset_identity_orders', ARRAY['consent_id', 'org_id', 'patient_id', 'required_scope', 'test_id']), ('tests', 'trg_callset_identity_tests', ARRAY['assembly', 'org_id']), ('consents', 'trg_callset_identity_consents', ARRAY['org_id', 'patient_id', 'recorded_at', 'scope', 'superseded_by', 'withdrawn_at']))) = 5 AND (SELECT count(*) FROM pg_trigger t WHERE NOT t.tgisinternal AND t.tgfoid = 'public.callset_identity_guard()'::regprocedure) = 5
-- MIGRATION-ASSERT: to_regclass('public.call_sets') IS NOT NULL AND to_regclass('public.call_set_events') IS NOT NULL AND to_regclass('public.caller_approvals') IS NOT NULL
-- MIGRATION-ASSERT: NOT has_database_privilege('clinical_app', current_database(), 'TEMP') AND NOT has_database_privilege('clinical_retention', current_database(), 'TEMP')
-- MIGRATION-ASSERT: NOT EXISTS (SELECT 1 FROM pg_database d, aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) a WHERE d.datname = current_database() AND a.grantee = 0 AND a.privilege_type = 'TEMPORARY')
-- MIGRATION-ASSERT: (SELECT count(*) FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace AND p.proname IN ('actor_holds_role', 'actor_context', 'caller_state', 'call_set_state', 'open_retraction_request', 'call_sets_before_insert', 'call_sets_require_submitted', 'call_sets_immutable', 'call_sets_require_redaction_audit', 'call_set_events_before_insert', 'call_set_events_after_insert', 'call_set_events_immutable', 'caller_approvals_before_insert', 'caller_approvals_no_update', 'callset_identity_guard') AND p.proconfig::text LIKE '%search_path=pg_catalog, public, pg_temp%') = 15
-- MIGRATION-ASSERT: NOT EXISTS (SELECT 1 FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace AND c.relname IN ('call_sets', 'call_set_events', 'caller_approvals') AND (pg_get_userbyid(c.relowner) IN ('clinical_app', 'clinical_retention') OR has_table_privilege('clinical_app', c.oid, 'UPDATE') OR has_table_privilege('clinical_app', c.oid, 'DELETE') OR has_table_privilege('clinical_retention', c.oid, 'DELETE')))
-- MIGRATION-ASSERT: NOT EXISTS (SELECT 1 FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace AND p.proname = 'call_sets_require_redaction_audit' AND (NOT p.prosecdef OR pg_get_userbyid(p.proowner) IN ('clinical_app', 'clinical_retention') OR has_function_privilege('clinical_app', p.oid, 'EXECUTE') OR has_function_privilege('clinical_retention', p.oid, 'EXECUTE')))

-- ─── preflight: refuse, and change nothing, if what this file builds on is not there ─────────────────────────
DO $$
DECLARE
    missing TEXT;
BEGIN
    SELECT string_agg(r, ', ') INTO missing
      FROM unnest(ARRAY['clinical_app', 'clinical_retention']) AS r
     WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r);
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'migration 0010 refused: login role(s) % do not exist (run clinical.bootstrap first); nothing was changed', missing;
    END IF;

    SELECT string_agg(t, ', ') INTO missing
      FROM unnest(ARRAY['public.organisations', 'public.users', 'public.role_assignments', 'public.sequencing_runs', 'public.vcfs', 'public.audit_log']) AS t
     WHERE to_regclass(t) IS NULL;
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'migration 0010 refused: parent table(s) % do not exist; nothing was changed', missing;
    END IF;

    -- the composite parents the foreign keys below reference must be unique keys, or the FKs would not mean what they say
    IF NOT EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = to_regclass('public.sequencing_runs') AND i.indisunique
                    AND (SELECT array_agg(a.attname::text ORDER BY k.ord) FROM unnest(i.indkey) WITH ORDINALITY k(attnum, ord)
                           JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum) = ARRAY['org_id', 'id']) THEN
        RAISE EXCEPTION 'migration 0010 refused: sequencing_runs has no unique (org_id, id); nothing was changed';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = to_regclass('public.users') AND i.indisunique
                    AND (SELECT array_agg(a.attname::text ORDER BY k.ord) FROM unnest(i.indkey) WITH ORDINALITY k(attnum, ord)
                           JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum) = ARRAY['org_id', 'user_id']) THEN
        RAISE EXCEPTION 'migration 0010 refused: users has no unique (org_id, user_id); nothing was changed';
    END IF;

    -- the TEMP revoke below needs the database owner or a superuser; say so now, not half way through
    IF NOT (pg_catalog.pg_has_role(current_user, (SELECT datdba FROM pg_database WHERE datname = current_database()), 'USAGE')
            OR (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)) THEN
        RAISE EXCEPTION 'migration 0010 refused: % neither owns this database nor is a superuser, so cannot revoke TEMP; nothing was changed', current_user;
    END IF;

    IF to_regclass('public.call_sets') IS NOT NULL OR to_regclass('public.call_set_events') IS NOT NULL OR to_regclass('public.caller_approvals') IS NOT NULL THEN
        RAISE EXCEPTION 'migration 0010 refused: a call-set table already exists outside the migration mechanism; nothing was changed';
    END IF;
END
$$;

-- ─── helpers (all read-only; search_path pinned; names schema-qualified) ─────
CREATE FUNCTION public.actor_holds_role(p_org UUID, p_user UUID, p_role TEXT) RETURNS BOOLEAN
LANGUAGE sql STABLE
SET search_path = pg_catalog, public, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1 FROM public.role_assignments
         WHERE org_id = p_org AND user_id = p_user AND role = p_role AND revoked_at IS NULL)
$$;

-- The actor the APPLICATION says is acting in this transaction (SET LOCAL clinical.actor_id).
CREATE FUNCTION public.actor_context() RETURNS UUID
LANGUAGE sql STABLE
SET search_path = pg_catalog, public, pg_temp
AS $$
    SELECT nullif(pg_catalog.current_setting('clinical.actor_id', true), '')::uuid
$$;


-- ─── caller_approvals: append-only log; the CURRENT state of a (name, version, class) is its latest row ─
CREATE TABLE public.caller_approvals (
    org_id         UUID        NOT NULL,
    id             UUID        PRIMARY KEY NOT NULL DEFAULT gen_random_uuid(),
    seq            BIGINT      GENERATED ALWAYS AS IDENTITY UNIQUE,
    caller_name    TEXT        NOT NULL CHECK (caller_name    = btrim(caller_name)    AND length(caller_name)    BETWEEN 1 AND 100),
    caller_version TEXT        NOT NULL CHECK (caller_version = btrim(caller_version) AND length(caller_version) BETWEEN 1 AND 100),
    record_class   TEXT        NOT NULL CHECK (record_class IN ('cnv', 'sv')),
    event_type     TEXT        NOT NULL CHECK (event_type IN ('proposed', 'approved', 'retired')),
    actor_id       UUID        NOT NULL,
    actor_role     TEXT        NOT NULL,
    occurred_at    TIMESTAMPTZ NOT NULL,
    basis          TEXT        NOT NULL CHECK (length(btrim(basis)) > 0),
    CONSTRAINT fk_ca_actor FOREIGN KEY (org_id, actor_id) REFERENCES public.users (org_id, user_id),
    CONSTRAINT uk_ca_org_id UNIQUE (org_id, id),
    CONSTRAINT ca_role_for_event CHECK (
        (event_type = 'proposed' AND actor_role = 'Administrator')
        OR (event_type IN ('approved', 'retired') AND actor_role = 'Approver'))
);
CREATE INDEX caller_approvals_key ON public.caller_approvals (org_id, record_class, caller_name, caller_version, seq DESC);

CREATE FUNCTION public.caller_state(p_org UUID, p_name TEXT, p_version TEXT, p_class TEXT) RETURNS TEXT
LANGUAGE sql STABLE
SET search_path = pg_catalog, public, pg_temp
AS $$
    SELECT event_type FROM public.caller_approvals
     WHERE org_id = p_org AND caller_name = p_name AND caller_version = p_version AND record_class = p_class
     ORDER BY seq DESC LIMIT 1
$$;


-- ─── call_sets: immutable evidence record; content removable ONLY by complete redaction ─
CREATE TABLE public.call_sets (
    org_id               UUID        NOT NULL,
    id                   UUID        PRIMARY KEY NOT NULL DEFAULT gen_random_uuid(),
    sequencing_run_id    UUID        NOT NULL,
    record_class         TEXT        NOT NULL CHECK (record_class IN ('cnv', 'sv')),
    assembly             TEXT        NOT NULL CHECK (assembly IN ('GRCh37', 'GRCh38')),
    caller_name          TEXT        NOT NULL CHECK (caller_name = btrim(caller_name) AND length(caller_name) BETWEEN 1 AND 100),
    caller_version       TEXT        CHECK (caller_version IS NULL OR (caller_version = btrim(caller_version) AND length(caller_version) BETWEEN 1 AND 100)),
    caller_in_header     TEXT,
    caller_approved_at_submission BOOLEAN NOT NULL,              -- SET BY TRIGGER; informational snapshot only
    vcf_sample_name      TEXT        NOT NULL CHECK (length(btrim(vcf_sample_name)) > 0),
    source_filename      TEXT        NOT NULL CHECK (length(source_filename) BETWEEN 1 AND 255 AND source_filename !~ '[/\\]'),
    source_vcf           BYTEA       CHECK (source_vcf IS NULL OR octet_length(source_vcf) BETWEEN 1 AND 67108864),   -- NULL ONLY after redaction
    source_content_hash  CHAR(64)    NOT NULL CHECK (source_vcf IS NULL OR source_content_hash = encode(sha256(source_vcf), 'hex')),
    parser_version       TEXT        NOT NULL CHECK (length(btrim(parser_version)) > 0),
    parsed_document      JSONB,                                                                                  -- NULL ONLY after redaction
    supersedes_id        UUID,
    resubmission_of      UUID,
    seq                  BIGINT      GENERATED ALWAYS AS IDENTITY UNIQUE,
    created_at           TIMESTAMPTZ NOT NULL,
    created_by           UUID        NOT NULL,
    retention_days       INTEGER     NOT NULL CHECK (retention_days > 0),                                        -- snapshot at creation (H-3)
    tombstoned_at        TIMESTAMPTZ,
    tombstoned_by        UUID,
    content_redacted_at  TIMESTAMPTZ,
    content_redacted_by  UUID,

    CONSTRAINT fk_callset_run      FOREIGN KEY (org_id, sequencing_run_id) REFERENCES public.sequencing_runs (org_id, id),
    CONSTRAINT fk_callset_creator  FOREIGN KEY (org_id, created_by)        REFERENCES public.users (org_id, user_id),
    CONSTRAINT fk_callset_tomb     FOREIGN KEY (org_id, tombstoned_by)     REFERENCES public.users (org_id, user_id),
    CONSTRAINT fk_callset_redactor FOREIGN KEY (org_id, content_redacted_by) REFERENCES public.users (org_id, user_id),
    CONSTRAINT callset_tombstone_complete CHECK ((tombstoned_at IS NULL) = (tombstoned_by IS NULL)),
    CONSTRAINT callset_redaction_complete CHECK ((content_redacted_at IS NULL) = (content_redacted_by IS NULL)),
    -- content is present XOR (redacted AND both content columns are NULL AND the record is tombstoned)
    CONSTRAINT callset_content_state CHECK (
        (content_redacted_at IS NULL     AND source_vcf IS NOT NULL AND parsed_document IS NOT NULL)
        OR (content_redacted_at IS NOT NULL AND source_vcf IS NULL     AND parsed_document IS NULL AND tombstoned_at IS NOT NULL)),
    CONSTRAINT uk_callset_org_id   UNIQUE (org_id, id),
    CONSTRAINT uk_callset_lineage  UNIQUE (org_id, id, sequencing_run_id, record_class),
    CONSTRAINT fk_callset_supersedes FOREIGN KEY (org_id, supersedes_id, sequencing_run_id, record_class)
        REFERENCES public.call_sets (org_id, id, sequencing_run_id, record_class),
    CONSTRAINT callset_not_self_superseding CHECK (supersedes_id IS NULL OR supersedes_id <> id),
    CONSTRAINT fk_callset_resubmission FOREIGN KEY (org_id, resubmission_of) REFERENCES public.call_sets (org_id, id),
    CONSTRAINT callset_not_self_resubmission CHECK (resubmission_of IS NULL OR resubmission_of <> id),
    CONSTRAINT callset_doc_envelope CHECK (
        parsed_document IS NULL
        OR (jsonb_typeof(parsed_document) = 'object'
            AND parsed_document->>'schema_version' = '1'
            AND parsed_document->>'assembly'       = assembly
            AND parsed_document->>'record_class'   = record_class
            AND jsonb_typeof(parsed_document->'records') = 'array'
            AND (parsed_document->'counts'->>'records')::int = jsonb_array_length(parsed_document->'records')
            AND octet_length(parsed_document::text) <= 134217728))
);
CREATE INDEX        callsets_content       ON public.call_sets (org_id, sequencing_run_id, record_class, source_content_hash);
CREATE INDEX        callsets_hash          ON public.call_sets (org_id, source_content_hash);
CREATE INDEX        callsets_successor     ON public.call_sets (org_id, supersedes_id) WHERE supersedes_id IS NOT NULL;
CREATE INDEX        callsets_run           ON public.call_sets (org_id, sequencing_run_id);


-- ─── call_set_events: append-only lifecycle ───────────────────────────────────
CREATE TABLE public.call_set_events (
    org_id              UUID        NOT NULL,
    id                  UUID        PRIMARY KEY NOT NULL DEFAULT gen_random_uuid(),
    seq                 BIGINT      GENERATED ALWAYS AS IDENTITY UNIQUE,
    call_set_id         UUID        NOT NULL,
    event_type          TEXT        NOT NULL CHECK (event_type IN (
        'submitted', 'validated', 'rejected', 'withdrawn', 'superseded',
        'retraction_requested', 'retraction_declined', 'retracted')),
    actor_id            UUID        NOT NULL,
    actor_role          TEXT        NOT NULL,
    occurred_at         TIMESTAMPTZ NOT NULL,
    reason              TEXT,
    checks              JSONB       NOT NULL DEFAULT '{}'::jsonb,
    related_call_set_id UUID,
    responds_to         UUID,
    caller_approval_id  UUID,
    tombstoned_at       TIMESTAMPTZ,
    tombstoned_by       UUID,

    CONSTRAINT fk_cse_set     FOREIGN KEY (org_id, call_set_id)         REFERENCES public.call_sets (org_id, id),
    CONSTRAINT fk_cse_actor   FOREIGN KEY (org_id, actor_id)            REFERENCES public.users (org_id, user_id),
    CONSTRAINT fk_cse_related FOREIGN KEY (org_id, related_call_set_id) REFERENCES public.call_sets (org_id, id),
    CONSTRAINT fk_cse_tomb    FOREIGN KEY (org_id, tombstoned_by)       REFERENCES public.users (org_id, user_id),
    CONSTRAINT uk_cse_org_id  UNIQUE (org_id, id),
    CONSTRAINT fk_cse_responds FOREIGN KEY (org_id, responds_to)        REFERENCES public.call_set_events (org_id, id),
    CONSTRAINT fk_cse_approval FOREIGN KEY (org_id, caller_approval_id) REFERENCES public.caller_approvals (org_id, id),
    CONSTRAINT cse_tombstone_complete CHECK ((tombstoned_at IS NULL) = (tombstoned_by IS NULL)),
    CONSTRAINT cse_role_for_event CHECK (
        (event_type IN ('submitted', 'withdrawn') AND actor_role = 'Lab technician')
        OR (event_type IN ('validated', 'rejected', 'retraction_requested', 'superseded') AND actor_role = 'Interpreter')
        OR (event_type IN ('retracted', 'retraction_declined') AND actor_role = 'Approver')),
    CONSTRAINT cse_reason_required CHECK (
        event_type IN ('submitted', 'validated', 'superseded')
        OR (reason IS NOT NULL AND length(btrim(reason)) > 0)),
    CONSTRAINT cse_checks_shape CHECK (
        (event_type = 'validated'
            AND checks = '{"sample_identity": true, "assembly": true, "caller": true}'::jsonb)
        OR (event_type = 'retracted'
            AND checks IN ('{}'::jsonb, '{"reason_code": "wrong_run_placement"}'::jsonb))
        OR (event_type NOT IN ('validated', 'retracted') AND checks = '{}'::jsonb)),
    CONSTRAINT cse_related_iff_superseded CHECK ((event_type = 'superseded') = (related_call_set_id IS NOT NULL)),
    CONSTRAINT cse_approval_iff_validated CHECK ((event_type = 'validated') = (caller_approval_id IS NOT NULL)),
    CONSTRAINT cse_responds_shape CHECK (
        (event_type = 'retraction_declined' AND responds_to IS NOT NULL)
        OR (event_type = 'retracted')
        OR (event_type NOT IN ('retraction_declined', 'retracted') AND responds_to IS NULL))
);
CREATE UNIQUE INDEX cse_one_submitted   ON public.call_set_events (org_id, call_set_id) WHERE event_type = 'submitted';
CREATE UNIQUE INDEX cse_one_decision    ON public.call_set_events (org_id, call_set_id) WHERE event_type IN ('validated', 'rejected', 'withdrawn');
CREATE UNIQUE INDEX cse_one_superseded  ON public.call_set_events (org_id, call_set_id) WHERE event_type = 'superseded';
CREATE UNIQUE INDEX cse_one_retraction  ON public.call_set_events (org_id, call_set_id) WHERE event_type = 'retracted';
CREATE UNIQUE INDEX cse_one_answer      ON public.call_set_events (org_id, responds_to) WHERE event_type = 'retraction_declined';
CREATE INDEX        cse_set             ON public.call_set_events (org_id, call_set_id, seq);

-- ─── derived state ────────────────────────────────────────────────────────────
CREATE FUNCTION public.call_set_state(p_org UUID, p_id UUID) RETURNS TEXT
LANGUAGE sql STABLE
SET search_path = pg_catalog, public, pg_temp
AS $$
    SELECT CASE e.event_type WHEN 'submitted' THEN 'pending_validation' ELSE e.event_type END
      FROM public.call_set_events e
     WHERE e.org_id = p_org AND e.call_set_id = p_id
       AND e.event_type IN ('submitted', 'validated', 'rejected', 'withdrawn', 'superseded', 'retracted')
     ORDER BY e.seq DESC LIMIT 1
$$;

CREATE FUNCTION public.open_retraction_request(p_org UUID, p_id UUID) RETURNS UUID
LANGUAGE sql STABLE
SET search_path = pg_catalog, public, pg_temp
AS $$
    SELECT r.id FROM public.call_set_events r
     WHERE r.org_id = p_org AND r.call_set_id = p_id AND r.event_type = 'retraction_requested'
       AND NOT EXISTS (SELECT 1 FROM public.call_set_events d
                        WHERE d.org_id = r.org_id AND d.call_set_id = r.call_set_id
                          AND d.event_type = 'retraction_declined' AND d.responds_to = r.id)
     ORDER BY r.seq DESC LIMIT 1
$$;


-- ─── TRIGGER: call_sets BEFORE INSERT ─────────────────────────────────────────
CREATE FUNCTION public.call_sets_before_insert() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE vmax INTEGER;
BEGIN
    IF NEW.tombstoned_at IS NOT NULL OR NEW.content_redacted_at IS NOT NULL THEN
        RAISE EXCEPTION 'call_sets: a call set cannot be created tombstoned or redacted';
    END IF;
    IF public.actor_context() IS DISTINCT FROM NEW.created_by THEN
        RAISE EXCEPTION 'call_sets: actor context is missing or does not match the creator (defence in depth; not a security boundary)';
    END IF;
    IF NOT public.actor_holds_role(NEW.org_id, NEW.created_by, 'Lab technician') THEN
        RAISE EXCEPTION 'call_sets: only a Lab technician may submit a call set';
    END IF;

    NEW.caller_approved_at_submission := COALESCE(
        NEW.caller_version IS NOT NULL
        AND public.caller_state(NEW.org_id, NEW.caller_name, NEW.caller_version, NEW.record_class) = 'approved', false);

    -- retention snapshot: inherit the run's VCF value when there is one (D2); otherwise the DAO resolved it
    SELECT max(v.retention_days) INTO vmax FROM public.vcfs v
     WHERE v.org_id = NEW.org_id AND v.sequencing_run_id = NEW.sequencing_run_id
       AND v.tombstoned_at IS NULL AND v.retention_days IS NOT NULL;
    IF vmax IS NOT NULL AND NEW.retention_days <> vmax THEN
        RAISE EXCEPTION 'call_sets: retention_days must inherit the run''s VCF value (%)', vmax;
    END IF;

    IF NEW.supersedes_id IS NOT NULL THEN
        -- the SAME lock every lifecycle act and the retention purge take on that call set (key = its id, seed 1)
        PERFORM pg_advisory_xact_lock(hashtextextended(NEW.supersedes_id::text, 1));
        IF public.call_set_state(NEW.org_id, NEW.supersedes_id) IS DISTINCT FROM 'validated'
           OR EXISTS (SELECT 1 FROM public.call_sets p WHERE p.org_id = NEW.org_id AND p.id = NEW.supersedes_id AND p.tombstoned_at IS NOT NULL) THEN
            RAISE EXCEPTION 'call_sets: a replacement may only name a live call set that is currently validated';
        END IF;
        -- ONE live successor (replaces the unique index): a rejected or withdrawn earlier replacement does not count
        IF EXISTS (SELECT 1 FROM public.call_sets x
                    WHERE x.org_id = NEW.org_id AND x.supersedes_id = NEW.supersedes_id AND x.tombstoned_at IS NULL
                      AND public.call_set_state(x.org_id, x.id) NOT IN ('rejected', 'withdrawn')) THEN
            RAISE EXCEPTION 'call_sets: this call set already has a replacement in progress or in force';
        END IF;
    END IF;

    -- LOCK ORDER (one hierarchy, shared with the DAO): call-set locks (key = id, seed 1) first, then content locks (seeds 0, 3, 4)
    IF NEW.resubmission_of IS NOT NULL THEN
        PERFORM pg_advisory_xact_lock(hashtextextended(NEW.resubmission_of::text, 1));
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.org_id::text || NEW.source_content_hash, 0));
    -- SAME RUN (replaces the unique index, under the same lock): the same bytes may be live once, and bytes this run has
    -- already seen may be submitted again ONLY as an explicit resubmission of a rejected or withdrawn call set.
    IF EXISTS (SELECT 1 FROM public.call_sets c
                WHERE c.org_id = NEW.org_id AND c.sequencing_run_id = NEW.sequencing_run_id
                  AND c.source_content_hash = NEW.source_content_hash
                  AND c.tombstoned_at IS NULL
                  AND public.call_set_state(c.org_id, c.id) IN ('pending_validation', 'validated')) THEN
        RAISE EXCEPTION 'call_sets: this file is already live on this sequencing run';
    END IF;
    IF NEW.resubmission_of IS NULL THEN
        IF EXISTS (SELECT 1 FROM public.call_sets c
                    WHERE c.org_id = NEW.org_id AND c.sequencing_run_id = NEW.sequencing_run_id
                      AND c.source_content_hash = NEW.source_content_hash) THEN
            RAISE EXCEPTION 'call_sets: this file was already submitted on this sequencing run (under any evidence class); submit it again only as a resubmission of a rejected or withdrawn call set';
        END IF;
    ELSE
        IF NOT EXISTS (SELECT 1 FROM public.call_sets p
                        WHERE p.org_id = NEW.org_id AND p.id = NEW.resubmission_of
                          AND p.sequencing_run_id = NEW.sequencing_run_id AND p.record_class = NEW.record_class
                          AND p.source_content_hash = NEW.source_content_hash AND p.tombstoned_at IS NULL
                          AND public.call_set_state(p.org_id, p.id) IN ('rejected', 'withdrawn')) THEN
            RAISE EXCEPTION 'call_sets: a resubmission must name a rejected or withdrawn, live call set of this run with the same file';
        END IF;
    END IF;
    IF EXISTS (SELECT 1 FROM public.call_sets c
                WHERE c.org_id = NEW.org_id AND c.source_content_hash = NEW.source_content_hash
                  AND c.sequencing_run_id <> NEW.sequencing_run_id
                  AND c.tombstoned_at IS NULL
                  AND NOT EXISTS (SELECT 1 FROM public.call_sets later
                                   WHERE later.org_id = c.org_id AND later.sequencing_run_id = c.sequencing_run_id
                                     AND later.source_content_hash = c.source_content_hash AND later.seq > c.seq)
                  AND NOT (public.call_set_state(c.org_id, c.id) = 'retracted'
                           AND EXISTS (SELECT 1 FROM public.call_set_events e
                                        WHERE e.org_id = c.org_id AND e.call_set_id = c.id AND e.event_type = 'retracted'
                                          AND e.checks = '{"reason_code": "wrong_run_placement"}'::jsonb))) THEN
        RAISE EXCEPTION 'call_sets: the same file was already submitted on another sequencing run and was not retracted for wrong-run placement';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_call_sets_before_insert BEFORE INSERT ON public.call_sets
    FOR EACH ROW EXECUTE FUNCTION public.call_sets_before_insert();

CREATE FUNCTION public.call_sets_require_submitted() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM public.call_set_events e
                    WHERE e.org_id = NEW.org_id AND e.call_set_id = NEW.id
                      AND e.event_type = 'submitted' AND e.actor_id = NEW.created_by) THEN
        RAISE EXCEPTION 'call_sets: call set % has no submitted event by its creator', NEW.id;
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_call_sets_require_submitted AFTER INSERT ON public.call_sets
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.call_sets_require_submitted();

-- ─── TRIGGER: immutability. Only the tombstone pair and a COMPLETE redaction may ever change. ─
CREATE FUNCTION public.call_sets_immutable() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    n public.call_sets%ROWTYPE := NEW;
    o public.call_sets%ROWTYPE := OLD;
BEGIN
    -- tombstoning/redaction serialises with every lifecycle act on this call set (same lock as the event trigger)
    IF NEW.tombstoned_at IS DISTINCT FROM OLD.tombstoned_at OR NEW.content_redacted_at IS DISTINCT FROM OLD.content_redacted_at THEN
        PERFORM pg_advisory_xact_lock(hashtextextended(OLD.id::text, 1));
    END IF;
    -- a redacted record is FINAL in every column: nothing, by anyone, can restore or alter it
    IF OLD.content_redacted_at IS NOT NULL AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'call_sets: a redacted call set is final and cannot be changed (call set %)', OLD.id;
    END IF;

    -- every column outside the content/tombstone set is immutable (the big columns are blanked on both sides first)
    n.tombstoned_at := NULL; n.tombstoned_by := NULL; n.source_vcf := NULL; n.parsed_document := NULL;
    n.content_redacted_at := NULL; n.content_redacted_by := NULL;
    o.tombstoned_at := NULL; o.tombstoned_by := NULL; o.source_vcf := NULL; o.parsed_document := NULL;
    o.content_redacted_at := NULL; o.content_redacted_by := NULL;
    IF n IS DISTINCT FROM o THEN
        RAISE EXCEPTION 'call_sets: content-independent columns are immutable (call set %)', OLD.id;
    END IF;

    -- a tombstone is one-way
    IF OLD.tombstoned_at IS NOT NULL
       AND (NEW.tombstoned_at IS DISTINCT FROM OLD.tombstoned_at OR NEW.tombstoned_by IS DISTINCT FROM OLD.tombstoned_by) THEN
        RAISE EXCEPTION 'call_sets: a tombstone is final (call set %)', OLD.id;
    END IF;
    IF (NEW.tombstoned_at IS DISTINCT FROM OLD.tombstoned_at OR NEW.tombstoned_by IS DISTINCT FROM OLD.tombstoned_by)
       AND NOT has_column_privilege(current_user, 'public.call_sets', 'tombstoned_by', 'UPDATE') THEN
        RAISE EXCEPTION 'call_sets: tombstoning requires the retention principal''s privilege (call set %)', OLD.id;
    END IF;

    -- content may leave only as a COMPLETE redaction of a tombstoned record, by the privileged principal
    IF NEW.source_vcf IS DISTINCT FROM OLD.source_vcf OR NEW.parsed_document IS DISTINCT FROM OLD.parsed_document
       OR NEW.content_redacted_at IS DISTINCT FROM OLD.content_redacted_at OR NEW.content_redacted_by IS DISTINCT FROM OLD.content_redacted_by THEN
        IF NOT (NEW.source_vcf IS NULL AND NEW.parsed_document IS NULL AND OLD.source_vcf IS NOT NULL AND OLD.parsed_document IS NOT NULL
                AND NEW.content_redacted_at IS NOT NULL AND NEW.content_redacted_by IS NOT NULL AND NEW.tombstoned_at IS NOT NULL) THEN
            RAISE EXCEPTION 'call_sets: content may only be removed, completely (bytes and parsed document together), from a tombstoned record';
        END IF;
        IF NOT has_column_privilege(current_user, 'public.call_sets', 'content_redacted_by', 'UPDATE') THEN
            RAISE EXCEPTION 'call_sets: redaction requires the retention principal''s privilege';
        END IF;
        IF public.actor_context() IS DISTINCT FROM NEW.content_redacted_by THEN
            RAISE EXCEPTION 'call_sets: actor context is missing or does not match the redactor (defence in depth; not a security boundary)';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_call_sets_immutable BEFORE UPDATE ON public.call_sets
    FOR EACH ROW EXECUTE FUNCTION public.call_sets_immutable();

-- ─── TRIGGER: a redaction is only valid with its purge audit row IN THE SAME TRANSACTION ─
-- SECURITY DEFINER, deliberately and narrowly: clinical_retention may INSERT into audit_log but, by existing
-- design, may not READ it. This trigger function is the ONLY definer function here: search_path pinned, every name
-- qualified, returns TRIGGER (so it cannot be called from SQL), reads one table, returns nothing to the caller.
-- It checks audit rows by the writing transaction's xid; purge code must therefore not wrap the audit INSERT
-- in a savepoint (a refusal, not data loss, would result).
CREATE FUNCTION public.call_sets_require_redaction_audit() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    IF NEW.content_redacted_at IS NOT NULL AND OLD.content_redacted_at IS NULL THEN
        IF NOT EXISTS (SELECT 1 FROM public.audit_log a
                        WHERE a.org_id = NEW.org_id AND a.action = 'retention_purge' AND a.resource_type = 'call_set'
                          AND a.resource_id = NEW.id::text AND a.outcome = 'success'
                          AND a.xmin = pg_current_xact_id()::xid) THEN
            RAISE EXCEPTION 'call_sets: redaction of % has no retention_purge audit row in the same transaction', NEW.id;
        END IF;
    END IF;
    RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION public.call_sets_require_redaction_audit() FROM PUBLIC;
CREATE CONSTRAINT TRIGGER trg_call_sets_redaction_audit AFTER UPDATE ON public.call_sets
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.call_sets_require_redaction_audit();


-- ─── TRIGGER: call_set_events BEFORE INSERT -- THE LIFECYCLE RULES ────────────
CREATE FUNCTION public.call_set_events_before_insert() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    cs_created_by UUID; cs_tomb TIMESTAMPTZ; cs_name TEXT; cs_version TEXT; cs_class TEXT;
    cs_run UUID; cs_assembly TEXT; cs_supersedes UUID;
    st          TEXT;
    open_req    UUID;
    ca          public.caller_approvals%ROWTYPE;
    succ_supers UUID;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.call_set_id::text, 1));
    NEW.occurred_at := clock_timestamp();

    IF public.actor_context() IS DISTINCT FROM NEW.actor_id THEN
        RAISE EXCEPTION 'call_set_events: actor context is missing or does not match the actor (defence in depth; not a security boundary)';
    END IF;
    IF NOT public.actor_holds_role(NEW.org_id, NEW.actor_id, NEW.actor_role) THEN
        RAISE EXCEPTION 'call_set_events: actor does not currently hold the % role', NEW.actor_role;
    END IF;

    SELECT created_by, tombstoned_at, caller_name, caller_version, record_class, sequencing_run_id, assembly, supersedes_id
      INTO cs_created_by, cs_tomb, cs_name, cs_version, cs_class, cs_run, cs_assembly, cs_supersedes
      FROM public.call_sets WHERE org_id = NEW.org_id AND id = NEW.call_set_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'call_set_events: no such call set'; END IF;
    IF cs_tomb IS NOT NULL THEN RAISE EXCEPTION 'call_set_events: a tombstoned call set takes no further events'; END IF;
    st := public.call_set_state(NEW.org_id, NEW.call_set_id);

    CASE NEW.event_type
    WHEN 'submitted' THEN
        IF st IS NOT NULL THEN RAISE EXCEPTION 'call_set_events: a call set is submitted exactly once'; END IF;
        IF NEW.actor_id <> cs_created_by THEN RAISE EXCEPTION 'call_set_events: only the creator may submit'; END IF;

    WHEN 'validated', 'rejected' THEN
        IF st IS DISTINCT FROM 'pending_validation' THEN
            RAISE EXCEPTION 'call_set_events: % requires a pending call set (it is %)', NEW.event_type, COALESCE(st, 'unsubmitted');
        END IF;
        IF NEW.actor_id = cs_created_by THEN
            RAISE EXCEPTION 'call_set_events: the submitter may not record a % decision on their own call set', NEW.event_type;
        END IF;
        IF NEW.event_type = 'validated' THEN
            SELECT * INTO ca FROM public.caller_approvals WHERE org_id = NEW.org_id AND id = NEW.caller_approval_id;
            IF NOT FOUND
               OR ca.event_type <> 'approved'
               OR (ca.caller_name, ca.caller_version, ca.record_class) IS DISTINCT FROM (cs_name, cs_version, cs_class)
               OR ca.seq <> (SELECT max(m.seq) FROM public.caller_approvals m
                              WHERE m.org_id = ca.org_id AND m.caller_name = ca.caller_name
                                AND m.caller_version = ca.caller_version AND m.record_class = ca.record_class) THEN
                RAISE EXCEPTION 'call_set_events: the caller % % is not currently approved for %', cs_name, cs_version, cs_class;
            END IF;
            -- ONE active validated call set per run, evidence class and assembly. A second one must replace the first
            -- (supersedes_id), whose supersession this validation records atomically.
            PERFORM pg_advisory_xact_lock(hashtextextended(NEW.org_id::text || '|' || cs_run::text || '|' || cs_class || '|' || cs_assembly, 4));
            IF EXISTS (SELECT 1 FROM public.call_sets o
                        WHERE o.org_id = NEW.org_id AND o.sequencing_run_id = cs_run AND o.record_class = cs_class
                          AND o.assembly = cs_assembly AND o.id <> NEW.call_set_id AND o.tombstoned_at IS NULL
                          AND o.id IS DISTINCT FROM cs_supersedes
                          AND public.call_set_state(o.org_id, o.id) = 'validated') THEN
                RAISE EXCEPTION 'call_set_events: another call set is already validated for this run, evidence class and assembly; replace it explicitly';
            END IF;
        END IF;

    WHEN 'withdrawn' THEN
        IF st IS DISTINCT FROM 'pending_validation' THEN
            RAISE EXCEPTION 'call_set_events: only a pending call set can be withdrawn (it is %)', COALESCE(st, 'unsubmitted');
        END IF;
        IF NEW.actor_id <> cs_created_by THEN RAISE EXCEPTION 'call_set_events: only the submitter may withdraw'; END IF;

    WHEN 'superseded' THEN
        IF st IS DISTINCT FROM 'validated' THEN
            RAISE EXCEPTION 'call_set_events: only a validated call set can be superseded (it is %)', COALESCE(st, 'unsubmitted');
        END IF;
        SELECT supersedes_id INTO succ_supers FROM public.call_sets WHERE org_id = NEW.org_id AND id = NEW.related_call_set_id;
        IF NOT FOUND OR succ_supers IS DISTINCT FROM NEW.call_set_id
           OR public.call_set_state(NEW.org_id, NEW.related_call_set_id) IS DISTINCT FROM 'validated' THEN
            RAISE EXCEPTION 'call_set_events: superseded must point at a validated successor that names this call set';
        END IF;

    WHEN 'retraction_requested' THEN
        IF st IS DISTINCT FROM 'validated' THEN
            RAISE EXCEPTION 'call_set_events: a retraction can only be requested of a validated call set (it is %)', COALESCE(st, 'unsubmitted');
        END IF;
        IF public.open_retraction_request(NEW.org_id, NEW.call_set_id) IS NOT NULL THEN
            RAISE EXCEPTION 'call_set_events: a retraction request is already open';
        END IF;

    WHEN 'retraction_declined' THEN
        IF st IS DISTINCT FROM 'validated' THEN
            RAISE EXCEPTION 'call_set_events: only a validated call set carries a retraction request (it is %)', COALESCE(st, 'unsubmitted');
        END IF;
        open_req := public.open_retraction_request(NEW.org_id, NEW.call_set_id);
        IF open_req IS NULL OR open_req IS DISTINCT FROM NEW.responds_to THEN
            RAISE EXCEPTION 'call_set_events: a decline must answer the open retraction request';
        END IF;

    WHEN 'retracted' THEN
        IF st IS DISTINCT FROM 'validated' THEN
            RAISE EXCEPTION 'call_set_events: only a validated call set can be retracted (it is %)', COALESCE(st, 'unsubmitted');
        END IF;
        IF NEW.responds_to IS NOT NULL AND NEW.responds_to IS DISTINCT FROM public.open_retraction_request(NEW.org_id, NEW.call_set_id) THEN
            RAISE EXCEPTION 'call_set_events: responds_to must be the open retraction request';
        END IF;
    END CASE;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_cse_before_insert BEFORE INSERT ON public.call_set_events
    FOR EACH ROW EXECUTE FUNCTION public.call_set_events_before_insert();

CREATE FUNCTION public.call_set_events_after_insert() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE pred UUID;
BEGIN
    IF NEW.event_type = 'validated' THEN
        SELECT supersedes_id INTO pred FROM public.call_sets WHERE org_id = NEW.org_id AND id = NEW.call_set_id;
        IF pred IS NOT NULL
           AND public.call_set_state(NEW.org_id, pred) = 'validated'
           AND NOT EXISTS (SELECT 1 FROM public.call_sets p WHERE p.org_id = NEW.org_id AND p.id = pred AND p.tombstoned_at IS NOT NULL) THEN
            INSERT INTO public.call_set_events (org_id, call_set_id, event_type, actor_id, actor_role, occurred_at, related_call_set_id)
            VALUES (NEW.org_id, pred, 'superseded', NEW.actor_id, 'Interpreter', clock_timestamp(), NEW.call_set_id);
        END IF;
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER trg_cse_after_insert AFTER INSERT ON public.call_set_events
    FOR EACH ROW EXECUTE FUNCTION public.call_set_events_after_insert();

CREATE FUNCTION public.call_set_events_immutable() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    n public.call_set_events%ROWTYPE := NEW;
    o public.call_set_events%ROWTYPE := OLD;
BEGIN
    n.tombstoned_at := NULL; n.tombstoned_by := NULL; o.tombstoned_at := NULL; o.tombstoned_by := NULL;
    IF n IS DISTINCT FROM o THEN
        RAISE EXCEPTION 'call_set_events: events are immutable (event %)', OLD.id;
    END IF;
    IF OLD.tombstoned_at IS NOT NULL
       AND (NEW.tombstoned_at IS DISTINCT FROM OLD.tombstoned_at OR NEW.tombstoned_by IS DISTINCT FROM OLD.tombstoned_by) THEN
        RAISE EXCEPTION 'call_set_events: a tombstone is final';
    END IF;
    IF (NEW.tombstoned_at IS DISTINCT FROM OLD.tombstoned_at OR NEW.tombstoned_by IS DISTINCT FROM OLD.tombstoned_by)
       AND NOT has_column_privilege(current_user, 'public.call_set_events', 'tombstoned_by', 'UPDATE') THEN
        RAISE EXCEPTION 'call_set_events: tombstoning requires the retention principal''s privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_cse_immutable BEFORE UPDATE ON public.call_set_events
    FOR EACH ROW EXECUTE FUNCTION public.call_set_events_immutable();


-- ─── TRIGGER: caller_approvals -- who may propose, approve, retire ────────────
CREATE FUNCTION public.caller_approvals_before_insert() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    st TEXT;
    proposer UUID;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        NEW.org_id::text || '|' || NEW.record_class || '|' || NEW.caller_name || '|' || NEW.caller_version, 2));
    NEW.occurred_at := clock_timestamp();
    IF public.actor_context() IS DISTINCT FROM NEW.actor_id THEN
        RAISE EXCEPTION 'caller_approvals: actor context is missing or does not match the actor (defence in depth; not a security boundary)';
    END IF;
    IF NOT public.actor_holds_role(NEW.org_id, NEW.actor_id, NEW.actor_role) THEN
        RAISE EXCEPTION 'caller_approvals: actor does not currently hold the % role', NEW.actor_role;
    END IF;
    st := public.caller_state(NEW.org_id, NEW.caller_name, NEW.caller_version, NEW.record_class);
    CASE NEW.event_type
    WHEN 'proposed' THEN
        IF st IS NOT NULL AND st <> 'retired' THEN
            RAISE EXCEPTION 'caller_approvals: this caller version is already %', st;
        END IF;
    WHEN 'approved' THEN
        IF st IS DISTINCT FROM 'proposed' THEN
            RAISE EXCEPTION 'caller_approvals: only a proposed caller version can be approved (it is %)', COALESCE(st, 'unknown');
        END IF;
        SELECT actor_id INTO proposer FROM public.caller_approvals
         WHERE org_id = NEW.org_id AND caller_name = NEW.caller_name AND caller_version = NEW.caller_version
           AND record_class = NEW.record_class AND event_type = 'proposed' ORDER BY seq DESC LIMIT 1;
        IF proposer = NEW.actor_id THEN
            RAISE EXCEPTION 'caller_approvals: the proposer may not approve their own proposal';
        END IF;
    WHEN 'retired' THEN
        IF st IS DISTINCT FROM 'approved' THEN
            RAISE EXCEPTION 'caller_approvals: only an approved caller version can be retired (it is %)', COALESCE(st, 'unknown');
        END IF;
    END CASE;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_ca_before_insert BEFORE INSERT ON public.caller_approvals
    FOR EACH ROW EXECUTE FUNCTION public.caller_approvals_before_insert();

CREATE FUNCTION public.caller_approvals_no_update() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    RAISE EXCEPTION 'caller_approvals: an approval record is never changed; append a new one';
END $$;
CREATE TRIGGER trg_ca_no_update BEFORE UPDATE ON public.caller_approvals
    FOR EACH ROW EXECUTE FUNCTION public.caller_approvals_no_update();


-- ─── IDENTITY: nothing a call set's meaning depends on may be repointed while a call set stands on it ───────
-- run -> sample -> order -> patient/test(assembly). The application role holds UPDATE on all four tables and no shipped
-- code changes these columns; this makes the database refuse the change instead of trusting that. A call set records the
-- sample it was checked against (vcf_sample_name) and its assembly, and the map re-checks both; this closes the write side.
CREATE FUNCTION public.callset_identity_guard() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE in_use BOOLEAN := false;
BEGIN
    IF TG_TABLE_NAME = 'sequencing_runs' THEN
        IF NEW.sample_id IS DISTINCT FROM OLD.sample_id OR NEW.org_id IS DISTINCT FROM OLD.org_id THEN
            in_use := EXISTS (SELECT 1 FROM public.call_sets c WHERE c.org_id = OLD.org_id AND c.sequencing_run_id = OLD.id);
        END IF;
    ELSIF TG_TABLE_NAME = 'samples' THEN
        IF NEW.order_id IS DISTINCT FROM OLD.order_id OR NEW.org_id IS DISTINCT FROM OLD.org_id THEN
            in_use := EXISTS (SELECT 1 FROM public.call_sets c
                                JOIN public.sequencing_runs r ON r.org_id = c.org_id AND r.id = c.sequencing_run_id
                               WHERE r.org_id = OLD.org_id AND r.sample_id = OLD.sample_id);
        END IF;
    ELSIF TG_TABLE_NAME = 'orders' THEN
        IF NEW.patient_id IS DISTINCT FROM OLD.patient_id OR NEW.test_id IS DISTINCT FROM OLD.test_id
           OR NEW.required_scope IS DISTINCT FROM OLD.required_scope OR NEW.consent_id IS DISTINCT FROM OLD.consent_id
           OR NEW.org_id IS DISTINCT FROM OLD.org_id THEN
            in_use := EXISTS (SELECT 1 FROM public.call_sets c
                                JOIN public.sequencing_runs r ON r.org_id = c.org_id AND r.id = c.sequencing_run_id
                                JOIN public.samples s ON s.org_id = r.org_id AND s.sample_id = r.sample_id
                               WHERE s.org_id = OLD.org_id AND s.order_id = OLD.order_id);
        END IF;
    ELSIF TG_TABLE_NAME = 'consents' THEN
        -- what a consent MEANS (whose, for what, when) never changes under a call set; a withdrawal and a supersession,
        -- once recorded, are never undone or re-pointed. Recording them (NULL -> value) stays allowed.
        IF NEW.scope IS DISTINCT FROM OLD.scope OR NEW.patient_id IS DISTINCT FROM OLD.patient_id
           OR NEW.org_id IS DISTINCT FROM OLD.org_id OR NEW.recorded_at IS DISTINCT FROM OLD.recorded_at
           OR (OLD.withdrawn_at IS NOT NULL AND NEW.withdrawn_at IS DISTINCT FROM OLD.withdrawn_at)
           OR (OLD.superseded_by IS NOT NULL AND NEW.superseded_by IS DISTINCT FROM OLD.superseded_by)
           OR (OLD.superseded_by IS NULL AND NEW.superseded_by IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM public.consents t
                                WHERE t.org_id = OLD.org_id AND t.consent_id = NEW.superseded_by
                                  AND t.patient_id = OLD.patient_id AND t.scope = OLD.scope AND t.recorded_at > OLD.recorded_at)) THEN
            in_use := EXISTS (SELECT 1 FROM public.call_sets c
                                JOIN public.sequencing_runs r ON r.org_id = c.org_id AND r.id = c.sequencing_run_id
                                JOIN public.samples s ON s.org_id = r.org_id AND s.sample_id = r.sample_id
                                JOIN public.orders o ON o.org_id = s.org_id AND o.order_id = s.order_id
                               WHERE o.org_id = OLD.org_id AND o.patient_id = OLD.patient_id);
        END IF;
    ELSIF TG_TABLE_NAME = 'tests' THEN
        IF NEW.assembly IS DISTINCT FROM OLD.assembly OR NEW.org_id IS DISTINCT FROM OLD.org_id THEN
            in_use := EXISTS (SELECT 1 FROM public.call_sets c
                                JOIN public.sequencing_runs r ON r.org_id = c.org_id AND r.id = c.sequencing_run_id
                                JOIN public.samples s ON s.org_id = r.org_id AND s.sample_id = r.sample_id
                                JOIN public.orders o ON o.org_id = s.org_id AND o.order_id = s.order_id
                               WHERE o.org_id = OLD.org_id AND o.test_id = OLD.test_id);
        END IF;
    END IF;
    IF in_use THEN
        RAISE EXCEPTION 'callset_identity: % identity cannot change while a call set stands on it', TG_TABLE_NAME;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_callset_identity_runs    BEFORE UPDATE OF sample_id, org_id        ON public.sequencing_runs
    FOR EACH ROW EXECUTE FUNCTION public.callset_identity_guard();
CREATE TRIGGER trg_callset_identity_samples BEFORE UPDATE OF order_id, org_id         ON public.samples
    FOR EACH ROW EXECUTE FUNCTION public.callset_identity_guard();
CREATE TRIGGER trg_callset_identity_orders  BEFORE UPDATE OF patient_id, test_id, required_scope, consent_id, org_id ON public.orders
    FOR EACH ROW EXECUTE FUNCTION public.callset_identity_guard();
CREATE TRIGGER trg_callset_identity_tests   BEFORE UPDATE OF assembly, org_id         ON public.tests
    FOR EACH ROW EXECUTE FUNCTION public.callset_identity_guard();
CREATE TRIGGER trg_callset_identity_consents BEFORE UPDATE OF scope, patient_id, org_id, recorded_at, withdrawn_at, superseded_by ON public.consents
    FOR EACH ROW EXECUTE FUNCTION public.callset_identity_guard();


-- ─── GRANTS (same pattern as vcfs / reviewer_claims / report_retractions) ─────
GRANT  SELECT, INSERT ON public.call_set_events, public.caller_approvals TO clinical_app;
GRANT  SELECT ON public.call_sets TO clinical_app;
-- INSERT on call_sets is granted COLUMN BY COLUMN, every column but `seq`: the ordering column is generated by the
-- database and the application role cannot supply it (INSERT ... OVERRIDING SYSTEM VALUE is refused).
DO $$
DECLARE cols TEXT;
BEGIN
    SELECT string_agg(quote_ident(attname), ', ' ORDER BY attnum) INTO cols
      FROM pg_attribute
     WHERE attrelid = 'public.call_sets'::regclass AND attnum > 0 AND NOT attisdropped AND attname <> 'seq';
    EXECUTE 'GRANT INSERT (' || cols || ') ON public.call_sets TO clinical_app';
END $$;
REVOKE UPDATE, DELETE ON public.call_sets, public.call_set_events, public.caller_approvals FROM clinical_app;
REVOKE UPDATE, DELETE ON public.call_sets, public.call_set_events, public.caller_approvals FROM PUBLIC;

GRANT  SELECT ON public.call_sets, public.call_set_events, public.caller_approvals TO clinical_retention;
-- the retention principal may tombstone AND redact; it may not change anything else, and cannot DELETE
GRANT  UPDATE (tombstoned_at, tombstoned_by, source_vcf, parsed_document, content_redacted_at, content_redacted_by)
    ON public.call_sets TO clinical_retention;
GRANT  UPDATE (tombstoned_at, tombstoned_by) ON public.call_set_events TO clinical_retention;
REVOKE DELETE ON public.call_sets, public.call_set_events, public.caller_approvals FROM clinical_retention;
-- caller_approvals is organisation CONFIGURATION, not patient data: it is not tombstoned.

-- ─── TEMP: revoked from PUBLIC (where the default grant lives) AND from the application roles ─────────────
-- A revoke from clinical_app alone would do nothing: the privilege is inherited from PUBLIC. The repository
-- uses no temporary tables (grep-verified), so nothing legitimate is lost. Defence in depth beside the pinned search_path.
DO $$
BEGIN
    EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
    EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM clinical_app', current_database());
    EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM clinical_retention', current_database());
END $$;
