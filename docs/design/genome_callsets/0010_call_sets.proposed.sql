-- =============================================================================
-- PROPOSED migration 0010 -- CNV/SV call sets (v3).  *** HISTORICAL: promoted to clinical/migrations/0010_call_sets.sql, with the
-- review-driven deltas listed in that file's header (never applied to a real database). Kept unchanged as the reviewed v3 text. ***
--
-- Lives under docs/ for design review only. Executed against a throwaway PostgreSQL 16
-- database built from the REAL clinical/schema.sql and exercised by test_lifecycle.py in
-- this directory as the REAL clinical_app / clinical_retention login roles.
--
-- v3 CHANGES (approved decisions):
--   B-1  every function pins `search_path = pg_catalog, public, pg_temp` (pg_temp LAST) and
--        every reference to a table or to one of our functions is schema-qualified;
--        TEMP is revoked from PUBLIC and from the application roles.
--   B-2  Option B: the retention role (and only it) may REDACT both the original VCF bytes and
--        the parsed document, completely, atomically, only on a tombstoned record, only with a
--        matching audit row in the same transaction; a redacted record is final.
--   H-1  an optional actor-context guard (SET LOCAL clinical.actor_id). DEFENCE IN DEPTH
--        AGAINST ACCIDENTAL MISSING CONTEXT ONLY -- NOT a security boundary (see below).
--   H-3  retention_days is NOT NULL and inherits the run's VCF value when one exists.
--
-- WHAT THE DATABASE DOES AND DOES NOT ENFORCE (stated precisely):
--   * It enforces SEPARATION OF RECORDED ACTORS: the actor ids written into rows must hold the
--     claimed roles, and a validator's recorded id may not equal the submitter's recorded id.
--   * It does NOT authenticate a human. The application connects as ONE database login
--     (clinical_app) and SUPPLIES every actor id; a malicious or compromised application role
--     can record any actor id (and can already INSERT/UPDATE role_assignments by design).
--     The actor-context guard below stops an application BUG that forgets or mismatches the
--     actor; a malicious application can simply set the context to whatever it likes.
--   * Whoever owns the tables, a superuser, or a role able to disable triggers is outside
--     every rule in this file.
--
-- Requires (all exist in the real schema): organisations, users(org_id, user_id),
-- role_assignments, sequencing_runs(org_id, id), vcfs, audit_log, and the login roles
-- clinical_app and clinical_retention.
--
-- THE FOUR ACTS ARE FOUR RECORDS. Ingestion (call_sets + 'submitted'), validation
-- ('validated'/'rejected'), approval of the CALLER (caller_approvals) and any clinical
-- interpretation/approval (NOT HERE). No column holds clinical meaning; 'detected'/'uncertain'
-- inside parsed_document are the CALLER's FILTER status only.
-- =============================================================================

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
CREATE UNIQUE INDEX callsets_no_duplicate  ON public.call_sets (org_id, sequencing_run_id, record_class, source_content_hash);
CREATE INDEX        callsets_hash          ON public.call_sets (org_id, source_content_hash);
CREATE UNIQUE INDEX callsets_one_successor ON public.call_sets (org_id, supersedes_id) WHERE supersedes_id IS NOT NULL;
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
        OR (event_type <> 'validated' AND checks = '{}'::jsonb)),
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
        IF public.call_set_state(NEW.org_id, NEW.supersedes_id) IS DISTINCT FROM 'validated'
           OR EXISTS (SELECT 1 FROM public.call_sets p WHERE p.org_id = NEW.org_id AND p.id = NEW.supersedes_id AND p.tombstoned_at IS NOT NULL) THEN
            RAISE EXCEPTION 'call_sets: a replacement may only name a live call set that is currently validated';
        END IF;
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.org_id::text || NEW.source_content_hash, 0));
    IF EXISTS (SELECT 1 FROM public.call_sets c
                WHERE c.org_id = NEW.org_id AND c.source_content_hash = NEW.source_content_hash
                  AND c.sequencing_run_id <> NEW.sequencing_run_id
                  AND public.call_set_state(c.org_id, c.id) IN ('pending_validation', 'validated')) THEN
        RAISE EXCEPTION 'call_sets: the same file is already live on another sequencing run';
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

    SELECT created_by, tombstoned_at, caller_name, caller_version, record_class
      INTO cs_created_by, cs_tomb, cs_name, cs_version, cs_class
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


-- ─── GRANTS (same pattern as vcfs / reviewer_claims / report_retractions) ─────
GRANT  SELECT, INSERT ON public.call_sets, public.call_set_events, public.caller_approvals TO clinical_app;
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

-- MIGRATION-ASSERT (when promoted; the repository requires >= 1 per migration):
--   to_regclass('public.call_sets') IS NOT NULL AND to_regclass('public.call_set_events') IS NOT NULL
--   NOT has_database_privilege('clinical_app', current_database(), 'TEMP')
--   NOT has_database_privilege('clinical_retention', current_database(), 'TEMP')
