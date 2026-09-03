-- clinical/schema.sql
-- ────────────────────
-- Clinical platform, Phase 1: identity.
--
-- Organisations, users, role assignments, sessions, audit log. No endpoints,
-- no request handling -- this file and data_access.py are the whole of the
-- identity layer.
--
-- THE ISOLATION RULE THIS SCHEMA EXISTS TO SUPPORT: a query without an
-- organisation scope must be structurally impossible to construct, not a
-- WHERE clause someone remembers. SQL alone cannot deliver that -- any
-- SELECT can omit a predicate. What this schema contributes is the half that
-- IS structural: every org-scoped row carries org_id, and every foreign key
-- into users is COMPOSITE on (user_id, org_id), so a row cannot reference a
-- user in a different organisation even if the application tries. The other
-- half lives in data_access.py, whose session object carries the org and
-- which offers no raw-query path; see its module docstring, and
-- tests/test_identity.py::TestStructuralIsolation for the test that fails if
-- one is introduced.
--
-- Postgres. Requires the pgcrypto extension for gen_random_uuid().

CREATE EXTENSION IF NOT EXISTS pgcrypto;


-- ─── organisations ──────────────────────────────────────────────────────────

CREATE TABLE organisations (
    org_id      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL,
    -- Deactivation, not deletion: an organisation that stops using the
    -- platform must not be removed, because every audit_log row that names it
    -- would lose its referent and the audit chain is the thing we are least
    -- allowed to break.
    disabled    BOOLEAN     NOT NULL DEFAULT FALSE
);


-- ─── users ──────────────────────────────────────────────────────────────────

CREATE TABLE users (
    user_id                 UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id                  UUID        NOT NULL REFERENCES organisations (org_id),

    email                   TEXT        NOT NULL,
    -- bcrypt. The column holds the full modular-crypt string (algorithm,
    -- cost, salt, digest), so the cost factor can be raised later and old
    -- hashes still verify.
    password_hash           TEXT        NOT NULL,
    -- No expiry policy is implemented and none is intended (NIST SP 800-63B
    -- advises against routine rotation). This column exists because after a
    -- suspected compromise the question "which passwords predate the
    -- incident" has to be answerable, and it cannot be reconstructed later.
    password_changed_at     TIMESTAMPTZ NOT NULL,
    password_reset_required BOOLEAN     NOT NULL DEFAULT FALSE,

    -- TOTP shared secret, encrypted at rest by the application before it ever
    -- reaches this column. Encrypted rather than hashed, deliberately and
    -- unlike the backup codes below: TOTP verification needs the original
    -- secret to recompute the code, so a one-way hash would make the scheme
    -- unimplementable. NULL until an administrator enrols the user.
    totp_secret             TEXT,
    totp_enrolled_at        TIMESTAMPTZ,

    -- Reset to 0 only by a successful login BY THIS USER (data_access.py::
    -- login). Per-user by construction, so one account's successful login
    -- cannot clear another account's counter -- an attacker holding one valid
    -- credential cannot keep a second account permanently unlocked.
    failed_login_attempts   INTEGER     NOT NULL DEFAULT 0,
    locked_until            TIMESTAMPTZ,

    -- Offboarding without deletion, same reason as organisations.disabled: a
    -- deleted clinician would orphan every audit row and every role
    -- assignment that names them.
    disabled                BOOLEAN     NOT NULL DEFAULT FALSE,

    -- System account flag. System accounts:
    -- - Cannot log in via login() (authentication blocked)
    -- - Can only be used as submission principal via _create_system_session
    -- - Have role "System" with minimal privileges
    -- One system account per organisation for automatic submission actions.
    is_system_account       BOOLEAN     NOT NULL DEFAULT FALSE,

    created_at              TIMESTAMPTZ NOT NULL,
    updated_at              TIMESTAMPTZ NOT NULL,

    CONSTRAINT users_org_email_unique UNIQUE (org_id, email),

    -- DO NOT REMOVE THIS AS REDUNDANT. It is redundant as a uniqueness
    -- statement -- user_id is already the primary key, so (org_id, user_id)
    -- cannot repeat. It is NOT redundant structurally: Postgres will only
    -- accept a composite FOREIGN KEY that references a set of columns
    -- carrying a matching UNIQUE (or PRIMARY KEY) constraint. Every composite
    -- FK below -- role_assignments, sessions, totp_backup_codes -- references
    -- (user_id, org_id) and depends on this line existing. Drop it and those
    -- three foreign keys fail to create, taking the structural org isolation
    -- with them.
    CONSTRAINT users_org_user_unique  UNIQUE (org_id, user_id)
);

CREATE INDEX users_org_disabled ON users (org_id, disabled);


-- ─── totp_backup_codes ──────────────────────────────────────────────────────
--
-- One row per code rather than an array column on users, because a code has
-- to be individually MARKED USED and never read back. An encrypted array
-- would be recoverable in full by anyone who can decrypt it; a bcrypt hash
-- per row is verified by comparison and reveals nothing, and used_at gives
-- the single-use property a place to live.

CREATE TABLE totp_backup_codes (
    code_id     UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID        NOT NULL,
    org_id      UUID        NOT NULL,
    -- bcrypt, exactly as password_hash. Never reversible, never displayed
    -- again after the one moment of issue.
    code_hash   TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,

    CONSTRAINT totp_backup_codes_user_fk
        FOREIGN KEY (user_id, org_id) REFERENCES users (user_id, org_id)
);

-- Partial index: verification only ever scans the unused codes for one user.
CREATE INDEX totp_backup_codes_unused
    ON totp_backup_codes (user_id, org_id)
    WHERE used_at IS NULL;


-- ─── role_assignments ───────────────────────────────────────────────────────

CREATE TABLE role_assignments (
    assignment_id UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID        NOT NULL,
    org_id        UUID        NOT NULL,
    role          TEXT        NOT NULL,

    assigned_by   UUID        NOT NULL,
    assigned_at   TIMESTAMPTZ NOT NULL,
    -- ISO 15189 7.4.1.5 c) requires that the identity of the person who
    -- reviewed and authorised a report be retrievable. An Approver whose
    -- authority rests on an unauthenticated string does not satisfy that, so
    -- the qualification basis on which the role was granted is recorded here
    -- and the CHECK below refuses an Approver assignment without one.
    basis         TEXT,

    revoked_at    TIMESTAMPTZ,
    revoked_by    UUID,

    CONSTRAINT role_assignments_role_known CHECK (
        role IN ('Orderer', 'Lab technician', 'Interpreter',
                 'Approver', 'Administrator', 'Auditor', 'System')
    ),
    CONSTRAINT role_assignments_approver_needs_basis CHECK (
        role <> 'Approver' OR basis IS NOT NULL
    ),
    CONSTRAINT role_assignments_revocation_complete CHECK (
        (revoked_at IS NULL) = (revoked_by IS NULL)
    ),

    -- The composite FK is the org isolation. A role assignment cannot name a
    -- user from a different organisation: (user_id, org_id) must exist as a
    -- pair on users. A plain FK on user_id alone would let an application bug
    -- grant an org_b user a role inside org_a.
    CONSTRAINT role_assignments_user_fk
        FOREIGN KEY (user_id, org_id) REFERENCES users (user_id, org_id)
);

-- Active assignments only: the read path always filters revoked_at IS NULL.
CREATE INDEX role_assignments_active
    ON role_assignments (org_id, user_id, role)
    WHERE revoked_at IS NULL;


-- ─── sessions ───────────────────────────────────────────────────────────────
--
-- Server-side, not a stateless token. A signed JWT cannot be revoked before
-- its own expiry, and an administrator must be able to end a session now --
-- so the row is the session and deleting the client's copy is not what ends
-- it.

CREATE TABLE sessions (
    session_id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id              UUID        NOT NULL,
    org_id               UUID        NOT NULL,

    created_at           TIMESTAMPTZ NOT NULL,
    -- Absolute ceiling, 24 hours from creation, never extended by activity.
    expires_at           TIMESTAMPTZ NOT NULL,
    -- Idle window, refreshed on each validated request. The realistic risk is
    -- a workstation left logged in overnight in a shared clinical area, which
    -- a 24-hour absolute cap alone does nothing about.
    -- Expiry at exactly the timeout: >= comparison.
    last_activity_at     TIMESTAMPTZ NOT NULL,
    idle_timeout_minutes INTEGER     NOT NULL DEFAULT 30,

    terminated_at        TIMESTAMPTZ,
    terminated_by        UUID,

    ip_address           INET,
    user_agent           TEXT,

    CONSTRAINT sessions_termination_complete CHECK (
        (terminated_at IS NULL) = (terminated_by IS NULL)
    ),
    CONSTRAINT sessions_user_fk
        FOREIGN KEY (user_id, org_id) REFERENCES users (user_id, org_id)
);

CREATE INDEX sessions_live
    ON sessions (org_id, user_id)
    WHERE terminated_at IS NULL;


-- ─── audit_log ──────────────────────────────────────────────────────────────

CREATE TABLE audit_log (
    log_id     BIGSERIAL   PRIMARY KEY,

    -- NULLABLE, and NOT foreign-keyed, on purpose. A failed authentication
    -- against an organisation identifier that does not exist is precisely the
    -- event most worth recording, and an FK would reject the insert and lose
    -- it -- the audit would fall silent exactly when someone is probing. Same
    -- reasoning for user_id: a login attempt against an unknown address must
    -- still leave a trace. What was submitted goes in `details`.
    org_id     UUID,
    user_id    UUID,

    "timestamp" TIMESTAMPTZ NOT NULL,
    actor_role TEXT,

    action          TEXT NOT NULL,
    resource_type   TEXT,
    resource_id     TEXT,
    outcome         TEXT,

    details    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    ip_address INET
);

CREATE INDEX audit_org_user           ON audit_log (org_id, user_id, "timestamp");
CREATE INDEX audit_action_timestamp   ON audit_log (action, "timestamp");
CREATE INDEX audit_outcome_timestamp  ON audit_log (outcome, "timestamp");


-- ─── Phase 3: Clinical domain (patients, consents, orders, samples) ──────────

CREATE TABLE patients (
    patient_id      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID        NOT NULL REFERENCES organisations (org_id),
    name            TEXT        NOT NULL,
    dob             DATE        NOT NULL,
    sex             TEXT        NOT NULL CHECK (sex IN ('M', 'F', 'O', 'U')),
    created_at      TIMESTAMPTZ NOT NULL,
    disabled        BOOLEAN     NOT NULL DEFAULT FALSE,

    CONSTRAINT patients_org_patient_unique UNIQUE (org_id, patient_id)
);

CREATE INDEX patients_org ON patients (org_id);


CREATE TABLE external_identifiers (
    identifier_id   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID        NOT NULL,
    org_id          UUID        NOT NULL,
    id_type         TEXT        NOT NULL,
    id_value        TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,

    CONSTRAINT external_identifiers_patient_fk
        FOREIGN KEY (patient_id, org_id) REFERENCES patients (patient_id, org_id)
);

CREATE UNIQUE INDEX external_identifiers_unique
    ON external_identifiers (org_id, id_type, id_value);


CREATE TABLE consents (
    consent_id      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID        NOT NULL,
    org_id          UUID        NOT NULL,
    scope           TEXT        NOT NULL,
    recorded_by     UUID        NOT NULL,
    recorded_at     TIMESTAMPTZ NOT NULL,
    withdrawn_at    TIMESTAMPTZ,
    withdrawn_by    UUID,
    superseded_by   UUID,

    CONSTRAINT consents_withdrawal_complete CHECK (
        (withdrawn_at IS NULL) = (withdrawn_by IS NULL)
    ),
    CONSTRAINT consents_patient_fk
        FOREIGN KEY (patient_id, org_id) REFERENCES patients (patient_id, org_id),
    CONSTRAINT consents_superseded_fk
        FOREIGN KEY (superseded_by, org_id) REFERENCES consents (consent_id, org_id),
    CONSTRAINT consents_org_unique UNIQUE (org_id, consent_id)
);

CREATE INDEX consents_patient_active
    ON consents (org_id, patient_id)
    WHERE withdrawn_at IS NULL AND superseded_by IS NULL;


CREATE TABLE tests (
    test_id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID        NOT NULL REFERENCES organisations (org_id),
    name            TEXT        NOT NULL,
    cdsco_class     TEXT,
    assembly        TEXT        NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
    approved_at     TIMESTAMPTZ,
    approved_by     UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX tests_org ON tests (org_id);


CREATE TABLE test_genes (
    gene_id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    test_id         UUID        NOT NULL REFERENCES tests (test_id),
    gene            TEXT        NOT NULL,
    added_at        TIMESTAMPTZ NOT NULL,
    added_by        UUID        NOT NULL
);

CREATE INDEX test_genes_test ON test_genes (test_id);


CREATE TABLE orders (
    order_id        UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID        NOT NULL,
    org_id          UUID        NOT NULL,
    test_id         UUID        NOT NULL REFERENCES tests (test_id),
    required_scope  TEXT        NOT NULL,
    clinical_indication TEXT,
    ordered_by      UUID        NOT NULL,
    priority        TEXT        NOT NULL DEFAULT 'routine' CHECK (priority IN ('routine', 'urgent')),
    consent_id      UUID        NOT NULL,
    state           TEXT        NOT NULL DEFAULT 'draft'
        CHECK (state IN ('draft', 'placed', 'sample_awaited', 'in_progress', 'reported', 'closed', 'cancelled')),
    placed_at       TIMESTAMPTZ,
    cancelled_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT orders_patient_fk
        FOREIGN KEY (patient_id, org_id) REFERENCES patients (patient_id, org_id),
    CONSTRAINT orders_consent_fk
        FOREIGN KEY (consent_id, org_id) REFERENCES consents (consent_id, org_id),
    CONSTRAINT orders_org_unique UNIQUE (org_id, order_id)
);

CREATE INDEX orders_patient ON orders (org_id, patient_id);
CREATE INDEX orders_state ON orders (org_id, state);


CREATE TABLE samples (
    sample_id       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id        UUID        NOT NULL,
    org_id          UUID        NOT NULL,
    type            TEXT        NOT NULL CHECK (type IN ('blood', 'saliva', 'tissue', 'dna')),
    collected_at    TIMESTAMPTZ NOT NULL,
    collected_by    UUID        NOT NULL,
    received_at     TIMESTAMPTZ,
    received_by     UUID,
    condition_on_receipt TEXT,
    qc_status       TEXT        NOT NULL DEFAULT 'pending'
        CHECK (qc_status IN ('pending', 'passed', 'failed')),
    qc_reason       TEXT,
    qc_recorded_by  UUID,
    qc_recorded_at  TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT samples_order_fk
        FOREIGN KEY (order_id, org_id) REFERENCES orders (order_id, org_id),
    CONSTRAINT samples_org_sample_unique UNIQUE (org_id, sample_id)
);

CREATE INDEX samples_order ON samples (org_id, order_id);


-- ─── Phase 4a: Lineage foundation (sequencing, VCF, interpretation, reports) ─

CREATE TABLE sequencing_runs (
    org_id      UUID        NOT NULL,
    id          UUID        PRIMARY KEY NOT NULL DEFAULT gen_random_uuid(),
    sample_id   UUID        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL,
    created_by  UUID        NOT NULL,

    CONSTRAINT fk_sequ_run_sample
        FOREIGN KEY (org_id, sample_id) REFERENCES samples (org_id, sample_id),
    CONSTRAINT fk_sequ_run_creator
        FOREIGN KEY (org_id, created_by) REFERENCES users (org_id, user_id),
    CONSTRAINT uk_sequ_run_org_id UNIQUE (org_id, id)
);

CREATE INDEX idx_sequ_runs_org_sample ON sequencing_runs (org_id, sample_id);


CREATE TABLE vcfs (
    org_id          UUID        NOT NULL,
    id              UUID        PRIMARY KEY NOT NULL DEFAULT gen_random_uuid(),
    sequencing_run_id UUID      NOT NULL,
    vcf_path        TEXT        NOT NULL,
    content_hash    CHAR(64)    NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    created_by      UUID        NOT NULL,

    CONSTRAINT fk_vcf_sequ_run
        FOREIGN KEY (org_id, sequencing_run_id) REFERENCES sequencing_runs (org_id, id),
    CONSTRAINT fk_vcf_creator
        FOREIGN KEY (org_id, created_by) REFERENCES users (org_id, user_id),
    CONSTRAINT uk_vcf_org_id UNIQUE (org_id, id)
);

CREATE INDEX idx_vcfs_org_sequ_run ON vcfs (org_id, sequencing_run_id);


CREATE TABLE interpretations (
    org_id          UUID        NOT NULL,
    id              UUID        PRIMARY KEY NOT NULL DEFAULT gen_random_uuid(),
    vcf_id          UUID        NOT NULL,
    run_document    JSONB       NOT NULL,
    submission_key  TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    created_by      UUID        NOT NULL,

    CONSTRAINT fk_interp_vcf
        FOREIGN KEY (org_id, vcf_id) REFERENCES vcfs (org_id, id),
    CONSTRAINT fk_interp_creator
        FOREIGN KEY (org_id, created_by) REFERENCES users (org_id, user_id),
    CONSTRAINT uk_interp_submission UNIQUE (org_id, submission_key),
    CONSTRAINT uk_interp_org_id UNIQUE (org_id, id)
);

CREATE INDEX idx_interp_org_vcf ON interpretations (org_id, vcf_id);
CREATE INDEX idx_interp_submission ON interpretations (org_id, submission_key);


CREATE TABLE reports (
    org_id              UUID        NOT NULL,
    id                  UUID        PRIMARY KEY NOT NULL DEFAULT gen_random_uuid(),
    interpretation_id   UUID        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL,
    created_by          UUID        NOT NULL,

    CONSTRAINT fk_report_interp
        FOREIGN KEY (org_id, interpretation_id) REFERENCES interpretations (org_id, id),
    CONSTRAINT fk_report_creator
        FOREIGN KEY (org_id, created_by) REFERENCES users (org_id, user_id)
);

CREATE INDEX idx_reports_org_interp ON reports (org_id, interpretation_id);


-- ─── Phase 5d: Exception workflow (order exceptions and resolution tracking) ─

CREATE TABLE exceptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organisations (org_id),
    order_id UUID NOT NULL,
    category TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    error_message TEXT,
    status TEXT NOT NULL CHECK (status IN ('open', 'escalated', 'resolved')),
    owner TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    last_resolved_by TEXT,
    last_resolved_at TIMESTAMPTZ,
    resolution_action TEXT,
    resolution_note TEXT,

    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMPTZ,
    next_retry_at TIMESTAMPTZ,

    CONSTRAINT exceptions_order_fk
        FOREIGN KEY (org_id, order_id) REFERENCES orders (org_id, order_id),
    CONSTRAINT exceptions_org_exception_unique UNIQUE (org_id, id)
);

CREATE INDEX idx_exceptions_org_order ON exceptions (org_id, order_id);
CREATE INDEX idx_exceptions_org_owner_status ON exceptions (org_id, owner, status)
    WHERE status = 'open';
CREATE INDEX idx_exceptions_reason_code ON exceptions (org_id, reason_code);
CREATE INDEX idx_exceptions_retry_scheduler ON exceptions (org_id, next_retry_at)
    WHERE status = 'open';


CREATE TABLE exception_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    exception_id UUID NOT NULL REFERENCES exceptions (id),
    actor TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('open', 'resolve', 'reopen')),
    action_note TEXT
);

CREATE INDEX idx_exception_events_exception ON exception_events (exception_id, timestamp);


-- ─── append-only enforcement ────────────────────────────────────────────────
--
-- A comment saying "append-only" is not a control. These statements are.
-- Run them as the table owner, after creating the application's role; the
-- application connects as clinical_app and from that point CANNOT rewrite or
-- erase history, only add to it. Nothing in data_access.py is trusted to
-- respect this -- the privilege is simply absent.
--
-- Note the deliberate asymmetry: the app keeps INSERT and SELECT. An auditor
-- must be able to read the log; nobody, including the application, may edit it.
--
-- Correcting a mistaken entry is done by appending a correcting entry, which
-- is what an append-only log means. If UPDATE is ever granted back, this
-- table stops being evidence.

-- CREATE ROLE clinical_app LOGIN PASSWORD '...';   -- deployment, not here

-- Explicit grant required for hardened deployments. Stock Postgres grants
-- USAGE on the default 'public' schema to PUBLIC, so this omission is
-- invisible on a standard install and fails on a recreated or non-default
-- schema. The comment preserves the line against future removal.
GRANT USAGE ON SCHEMA public TO clinical_app;

GRANT  SELECT, INSERT         ON audit_log TO clinical_app;
REVOKE UPDATE, DELETE         ON audit_log FROM clinical_app;
REVOKE UPDATE, DELETE         ON audit_log FROM PUBLIC;
GRANT  USAGE, SELECT          ON SEQUENCE audit_log_log_id_seq TO clinical_app;

GRANT SELECT, INSERT, UPDATE, DELETE
    ON organisations, users, totp_backup_codes, role_assignments, sessions,
       patients, external_identifiers, consents, tests, test_genes, orders, samples,
       sequencing_runs, vcfs, interpretations, reports,
       exceptions, exception_events
    TO clinical_app;
