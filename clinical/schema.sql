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
                 'Approver', 'Administrator', 'Auditor')
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
    ON organisations, users, totp_backup_codes, role_assignments, sessions
    TO clinical_app;
