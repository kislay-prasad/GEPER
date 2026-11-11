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

    -- ─── signing identity (R10, human ruling 2026-09-13) ────────────────────
    --
    -- A clinical report shows the signing clinician's NAME, REGISTRATION
    -- NUMBER and HOSPITAL. Before these three columns the user record held an
    -- email address and nothing else, so all three were typed in by hand at
    -- sign-off time (geper/review/signoff.py::approve's --clinician-name /
    -- --reg-number / --hospital) with nothing tying the printed identity to
    -- the account that signed. A report could therefore show any identity at
    -- all, including a colleague's, and the record could not contradict it.
    --
    -- NULLABLE, DELIBERATELY. Not every user signs reports: system accounts
    -- (is_system_account) never do, and an Auditor or Administrator has no
    -- registration number to record. Making these NOT NULL would force a
    -- fabricated value onto every such account, which is the opposite of the
    -- guarantee. The gate is at the point of USE instead --
    -- data_access.py::get_signing_identity refuses an incomplete identity
    -- rather than the schema refusing an incomplete row.
    full_name               TEXT,
    -- NO FORMAT CONSTRAINT, and this is a decision rather than an omission.
    -- Indian registration numbers are issued per state medical council with
    -- no single national format (NMC/SMC numbers vary in length, prefix and
    -- punctuation), so any regex here would refuse valid registrations --
    -- refusing a real clinician's real number is a worse failure than storing
    -- one that is merely well-formed-looking. Non-empty and trimmed is
    -- enforced by data_access.py::set_clinician_identity; correctness against
    -- the issuing council is a verification step this platform does not
    -- perform and does not claim to.
    registration_number     TEXT,
    hospital                TEXT,

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

    -- w164 (migration 0006): THE REPLAY GUARD, and it is the only place
    -- one-time use of a TOTP code can live. A code is valid for its whole
    -- 30-second step -- three steps, with the one-step skew allowance
    -- data_access.py::TOTP_WINDOW_STEPS carries -- so refusing a code that has
    -- already been accepted means REMEMBERING which step was spent. RFC 6238
    -- 5.2: "the verifier MUST NOT accept the second attempt of the OTP
    -- generated for the same time-step".
    --
    -- THE STEP NUMBER, NOT THE CODE. Storing the last accepted code would
    -- store a credential and would be weaker besides: it refuses only the code
    -- it saw, so the PREVIOUS step's code -- still inside the window and still
    -- arithmetically valid -- would be admitted after it. A step counter is
    -- monotonic, so everything at or before the last accepted step is refused
    -- at once. BIGINT because a step is unix_seconds/30, which leaves INTEGER
    -- in 2038.
    --
    -- NULL means "no code has been accepted yet". enrol_totp sets it back to
    -- NULL, so a new authenticator does not inherit the old one's spent steps.
    totp_last_used_step     BIGINT,

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

-- One registration number, one clinician, within an organisation (R10). Two
-- accounts claiming the same registration number would make the identity a
-- report prints ambiguous about WHICH account signed, which is the whole
-- property these columns exist to establish. PARTIAL: NULL is the ordinary
-- state for every account that does not sign (see the column comments), and a
-- plain UNIQUE would be satisfied by unlimited NULLs anyway -- the WHERE makes
-- that explicit rather than incidental. Scoped to org_id, not global: the
-- platform is multi-tenant and one clinician legitimately holding accounts in
-- two organisations must not be blocked by the other tenant's row, which would
-- also leak the existence of that row across the isolation boundary.
CREATE UNIQUE INDEX users_org_registration_number_unique
    ON users (org_id, registration_number)
    WHERE registration_number IS NOT NULL;


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


-- ─── patient portal identity (w130) ─────────────────────────────────────────
--
-- A PATIENT IS A PRINCIPAL, AND IT IS NOT A `users` ROW. Not because of roles
-- -- a roleless `users` row is legal and is what create_user produces -- but
-- because of what a `users` row is STRUCTURALLY ELIGIBLE for. Counted at
-- ca183d3, BEFORE this table existed: `REFERENCES users` occurred 22 times in
-- this file (patient_logins_enroller_fk below is the twenty-third, and it is
-- correctly a staff reference -- the enroller IS staff). Nineteen of the
-- twenty-two are clinical-record actor columns, including reports.created_by and
-- reports.approver_id. A patient placed in `users` is a database-legal report
-- approver, and the only thing refusing it would be a Python check -- the
-- exact shape this schema records as a defect elsewhere (see the note on
-- users_org_user_unique above, which spends seven lines explaining that a
-- structural guarantee must not be downgraded to a remembered one).
--
-- A separate principal table makes "a patient cannot approve a report"
-- INEXPRESSIBLE rather than merely unwritten. That is the guarantee shape
-- this schema already uses for tenancy, applied to a second question.
--
-- NO ROLE IS ADDED. ROLES, role_assignments_role_known and _require_role are
-- untouched by these two tables; a patient holds no role assignment and
-- cannot, because role_assignments.user_id is a composite FK to `users`.

CREATE TABLE patient_logins (
    patient_login_id        UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id              UUID        NOT NULL,
    org_id                  UUID        NOT NULL,

    email                   TEXT        NOT NULL,
    -- bcrypt, the same modular-crypt string users.password_hash holds, written
    -- by the same hasher. No second credential format was introduced.
    password_hash           TEXT        NOT NULL,
    password_changed_at     TIMESTAMPTZ NOT NULL,
    password_reset_required BOOLEAN     NOT NULL DEFAULT FALSE,

    -- Same lockout policy as users: the counter and the ceiling are the same
    -- columns under the same names, read by the same constants
    -- (MAX_FAILED_LOGIN_ATTEMPTS, LOCKOUT_MINUTES).
    failed_login_attempts   INTEGER     NOT NULL DEFAULT 0,
    locked_until            TIMESTAMPTZ,

    disabled                BOOLEAN     NOT NULL DEFAULT FALSE,

    -- WHICH STAFF PRINCIPAL ISSUED THIS CREDENTIAL. Enrolment is a staff act
    -- (no self-registration), so the record of who performed it belongs on the
    -- row. Composite FK, so the enroller cannot be from another organisation.
    enrolled_by             UUID        NOT NULL,

    created_at              TIMESTAMPTZ NOT NULL,
    updated_at              TIMESTAMPTZ NOT NULL,

    -- The link to the clinical record, and the whole point of the table. The
    -- composite FK makes a credential for a patient in ANOTHER organisation
    -- inexpressible, exactly as role_assignments_user_fk does for staff.
    CONSTRAINT patient_logins_patient_fk
        FOREIGN KEY (patient_id, org_id) REFERENCES patients (patient_id, org_id),
    CONSTRAINT patient_logins_enroller_fk
        FOREIGN KEY (enrolled_by, org_id) REFERENCES users (user_id, org_id),

    CONSTRAINT patient_logins_org_email_unique UNIQUE (org_id, email),

    -- EXACTLY ONE PATIENT PER LOGIN, AND ONE LOGIN PER PATIENT. The second
    -- half is a DECISION, not a default: proxy and guardian access (a child, an
    -- adult without capacity) would relax it, and the schema already knows
    -- minors exist (retention_days_minor). Relaxing it is a ruling, and until
    -- one is made this constraint is what stops a second credential appearing
    -- for a patient without anybody deciding that it should.
    CONSTRAINT patient_logins_one_per_patient UNIQUE (org_id, patient_id),

    -- DO NOT REMOVE THIS AS REDUNDANT -- the same reasoning as
    -- users_org_user_unique above, and for the same reason. patient_sessions'
    -- composite FK references (patient_login_id, org_id) and Postgres will only
    -- accept a composite FOREIGN KEY against a matching UNIQUE constraint.
    -- Drop this line and that foreign key fails to create, taking the
    -- structural org isolation of every patient session with it.
    CONSTRAINT patient_logins_org_login_unique UNIQUE (org_id, patient_login_id)
);

CREATE INDEX patient_logins_org_patient ON patient_logins (org_id, patient_id);


-- Mirrors `sessions` column for column and policy for policy: the same 24-hour
-- absolute cap that activity never extends, the same 30-minute idle window
-- refreshed per validated request with the same inclusive boundary, the same
-- server-side termination, the same ip_address/user_agent.
--
-- A SEPARATE TABLE RATHER THAN A NULLABLE COLUMN ON `sessions`, deliberately.
-- The rejected alternative was patient_login_id nullable on sessions with an
-- XOR check: it needs ALTER TABLE ... DROP NOT NULL on a live table whose
-- composite FK is load-bearing, it makes Session.user_id optional in a layer
-- that writes session.user_id as created_by/recorded_by in roughly thirty
-- statements, and one table invites one session_valid() returning a union of
-- two principal kinds -- which is the dependency-widening that this whole
-- design exists to refuse.
--
-- THE TWO SESSION ID SPACES ARE DIFFERENT TABLES. A staff session id presented
-- to the portal is simply not found, and a patient session id presented to the
-- staff dependency is simply not found. That separation is structural, not
-- checked.

CREATE TABLE patient_sessions (
    session_id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_login_id     UUID        NOT NULL,
    org_id               UUID        NOT NULL,

    -- DENORMALISED ON PURPOSE, AND IT IS THE SECOND AXIS OF ISOLATION.
    -- patient_id is derivable by joining patient_logins, but it is carried
    -- here for the same reason sessions carries org_id: every patient-facing
    -- statement binds it FROM THE SESSION and never from a caller argument,
    -- and a value that has to be fetched before it can be bound is a value
    -- somebody will eventually pass in instead. The composite FK below plus
    -- patient_logins_one_per_patient keep it from disagreeing with the login.
    patient_id           UUID        NOT NULL,

    created_at           TIMESTAMPTZ NOT NULL,
    expires_at           TIMESTAMPTZ NOT NULL,
    last_activity_at     TIMESTAMPTZ NOT NULL,
    idle_timeout_minutes INTEGER     NOT NULL DEFAULT 30,

    terminated_at        TIMESTAMPTZ,
    terminated_by        UUID,

    ip_address           INET,
    user_agent           TEXT,

    CONSTRAINT patient_sessions_termination_complete CHECK (
        (terminated_at IS NULL) = (terminated_by IS NULL)
    ),
    CONSTRAINT patient_sessions_login_fk
        FOREIGN KEY (patient_login_id, org_id)
        REFERENCES patient_logins (patient_login_id, org_id),
    -- The carried patient_id is held to the patients table by the same
    -- composite FK every other patient reference in this schema uses, so a
    -- session cannot carry a patient from another organisation even if the
    -- application tried to write one.
    CONSTRAINT patient_sessions_patient_fk
        FOREIGN KEY (patient_id, org_id) REFERENCES patients (patient_id, org_id)
);

CREATE INDEX patient_sessions_live
    ON patient_sessions (org_id, patient_login_id)
    WHERE terminated_at IS NULL;



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
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Migration 0007. The target of orders_test_org_fk below: a foreign key
    -- must reference a unique key, and test_id alone is the primary key.
    CONSTRAINT tests_id_org_unique UNIQUE (test_id, org_id)
);

CREATE INDEX tests_org ON tests (org_id);

-- Migration 0008. Two ACTIVE tests in one organisation may not share a name
-- (case-insensitively); a retired test releases its name.
CREATE UNIQUE INDEX tests_org_active_name_unique
    ON tests (org_id, lower(name))
    WHERE status = 'active';


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
    -- Migration 0007. An order may not reference another organisation's test.
    CONSTRAINT orders_test_org_fk
        FOREIGN KEY (test_id, org_id) REFERENCES tests (test_id, org_id),
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


-- ─── Ingestion: FASTQ sets ──────────────────────────────────────────────────
--
-- The first unit of ingestion: a paired-end FASTQ set observed on disk for a
-- sample. One row per set; R1 and R2 travel together because they are one
-- observation, and a set with only one mate is a defect rather than a state.
--
-- ORG-SCOPED BY CONSTRUCTION, not by convention. The foreign key names
-- (org_id, sample_id) rather than sample_id alone, so a fastq set CANNOT
-- reference a sample belonging to another organisation -- the database refuses
-- the row. That is stronger than an application that declines to write one:
-- an unscoped reference is not expressible, rather than merely not written.
-- `samples_org_sample_unique` is what makes the composite reference possible
-- and is the reason it exists.
--
-- APPEND-ONLY, enforced by the GRANT/REVOKE block at the foot of this file
-- rather than by this comment. A FASTQ set is an observation of what was on
-- disk at a moment: rewriting it would rewrite the evidence of what arrived.
--
-- A CONSEQUENCE OF APPEND-ONLY THAT IS DELIBERATE AND WORTH READING BEFORE
-- ADDING A TRANSITION: `state` is set at INSERT and can never be updated,
-- because clinical_app holds no UPDATE on this table. So `state` records the
-- state AT DETECTION. Advancing a set through validation is therefore a
-- design question -- a new appended row, or a separate table -- and NOT a
-- missing grant. The scanner, validation and pipeline invocation are all out
-- of scope here; this table is the floor they will sit on.
CREATE TABLE fastq_sets (
    fastq_set_id    UUID        PRIMARY KEY NOT NULL DEFAULT gen_random_uuid(),
    org_id          UUID        NOT NULL,
    sample_id       UUID        NOT NULL,
    r1_path         TEXT        NOT NULL,
    r2_path         TEXT        NOT NULL,
    r1_checksum     TEXT        NOT NULL,
    r2_checksum     TEXT        NOT NULL,
    detected_at     TIMESTAMPTZ NOT NULL,
    state           TEXT        NOT NULL DEFAULT 'detected'
        CHECK (state IN ('detected', 'validated', 'rejected', 'consumed')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fastq_sets_sample_fk
        FOREIGN KEY (org_id, sample_id) REFERENCES samples (org_id, sample_id),
    CONSTRAINT fastq_sets_org_unique UNIQUE (org_id, fastq_set_id)
);


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

    -- Phase 7 commit 4 (spec 22, human ruling D4: tombstone not real
    -- delete). NULL means live. Written only by the clinical_retention
    -- principal (see the GRANT/REVOKE block below) -- clinical_app never
    -- writes these columns, on the same "no exclusion, no exception"
    -- discipline as the append-only tables: retention is a separate,
    -- auditable actor's job, not an ordinary application code path.
    tombstoned_at   TIMESTAMPTZ,
    tombstoned_by   UUID,

    -- Human ruling D2 (2026-09-09): retention resolves PER ARTEFACT from
    -- the patient's age at collection, not as one flat number per class
    -- (retention_policies, below, no longer determines a VCF's own
    -- period by itself -- see clinical/retention.py's docstring for the
    -- resolution rule and clinical/data_access.py::create_vcf for where
    -- this is computed once, at creation). NULLABLE, not NOT NULL: a
    -- handful of existing tests insert this table directly, bypassing
    -- create_vcf, and have no reason to resolve a real period -- NULL
    -- here means "no resolved period", which retention.py's purge
    -- queries treat as never-eligible (the safe direction: absence of a
    -- resolved period blocks a purge, it never permits one). Every VCF
    -- created through create_vcf() gets a real value; this is a
    -- deliberately permissive column, not a deliberately optional
    -- policy.
    retention_days  INTEGER     CHECK (retention_days IS NULL OR retention_days > 0),

    CONSTRAINT fk_vcf_sequ_run
        FOREIGN KEY (org_id, sequencing_run_id) REFERENCES sequencing_runs (org_id, id),
    CONSTRAINT fk_vcf_creator
        FOREIGN KEY (org_id, created_by) REFERENCES users (org_id, user_id),
    CONSTRAINT fk_vcf_tombstoner
        FOREIGN KEY (org_id, tombstoned_by) REFERENCES users (org_id, user_id),
    CONSTRAINT vcfs_tombstone_complete CHECK (
        (tombstoned_at IS NULL) = (tombstoned_by IS NULL)
    ),
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

    -- Phase 7 (spec 15.3). The lineage branch re-analysis needs and Phase
    -- 4a's chain does not have: NULL for a first interpretation of a VCF,
    -- populated with the interpretation it re-analyses when this one is a
    -- re-analysis. A composite self-FK, org-scoped like every other
    -- reference in this schema, so a re-analysis cannot point at another
    -- organisation's interpretation. Column only in this commit -- no
    -- method writes it yet; that is a later Phase 7 commit's job.
    parent_interpretation_id UUID,

    -- Phase 7 commit 4 (spec 22, human ruling D4). Same discipline as
    -- vcfs.tombstoned_at/tombstoned_by above.
    tombstoned_at   TIMESTAMPTZ,
    tombstoned_by   UUID,

    -- Human ruling D2 (2026-09-09): INHERITED from vcfs.retention_days at
    -- creation (create_interpretation()/create_reanalysis()), never
    -- independently configured and never recomputed -- see
    -- vcfs.retention_days's own comment above. Same NULLABLE-for-tests
    -- reasoning: a row inserted directly by a test that predates this
    -- column carries NULL, and NULL is never-eligible for purge.
    retention_days  INTEGER     CHECK (retention_days IS NULL OR retention_days > 0),

    CONSTRAINT fk_interp_vcf
        FOREIGN KEY (org_id, vcf_id) REFERENCES vcfs (org_id, id),
    CONSTRAINT fk_interp_creator
        FOREIGN KEY (org_id, created_by) REFERENCES users (org_id, user_id),
    CONSTRAINT fk_interp_parent
        FOREIGN KEY (org_id, parent_interpretation_id) REFERENCES interpretations (org_id, id),
    CONSTRAINT fk_interp_tombstoner
        FOREIGN KEY (org_id, tombstoned_by) REFERENCES users (org_id, user_id),
    CONSTRAINT interp_tombstone_complete CHECK (
        (tombstoned_at IS NULL) = (tombstoned_by IS NULL)
    ),
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

    -- Phase 6 (spec 13.1). The state machine is draft -> under_review ->
    -- approved -> released, with returned sending a report back to
    -- under_review. Enumerated here rather than in application code because a
    -- report in a state nobody defined is the failure mode this column exists
    -- to make unrepresentable. Transition legality is NOT a CHECK -- a row
    -- constraint cannot see the previous value -- and belongs to a later
    -- commit; what the CHECK guarantees is that whatever state a report is
    -- in is one of the five.
    state               TEXT        NOT NULL DEFAULT 'draft'
        CHECK (state IN ('draft', 'under_review', 'approved', 'released', 'returned')),

    -- Phase 6 (spec 13.3). The three approval facts: who, when, and exactly
    -- what. content_hash is the hash of the report content AT APPROVAL TIME,
    -- which is what makes 15.1 immutability checkable afterwards -- without
    -- it "this report was approved" is a claim about a document that may
    -- since have changed. VARCHAR(64) sizes a hex-encoded SHA-256.
    --
    -- approver_id is a composite FK, as every user reference in this schema
    -- is: an approver from another organisation must be structurally
    -- impossible, not merely unlikely.
    approver_id         UUID,
    approved_at         TIMESTAMPTZ,
    content_hash        VARCHAR(64),

    -- Phase 7 commit 4 (spec 22, human ruling D4). Same discipline as
    -- vcfs.tombstoned_at/tombstoned_by above. D3: the clock that decides
    -- WHEN a report becomes eligible runs from release_events, not from a
    -- column here -- these two columns record only the outcome once the
    -- retention principal has acted, same shape as approver_id/approved_at
    -- recording the outcome of approval rather than gating it.
    tombstoned_at       TIMESTAMPTZ,
    tombstoned_by       UUID,

    -- Human ruling D2 (2026-09-09): INHERITED from interpretations.retention_days
    -- at creation (create_report()/create_amendment()), never independently
    -- configured -- see vcfs.retention_days's comment above for the full
    -- rule and interpretations.retention_days for the same NULLABLE
    -- reasoning.
    retention_days      INTEGER     CHECK (retention_days IS NULL OR retention_days > 0),

    CONSTRAINT fk_report_interp
        FOREIGN KEY (org_id, interpretation_id) REFERENCES interpretations (org_id, id),
    CONSTRAINT fk_report_creator
        FOREIGN KEY (org_id, created_by) REFERENCES users (org_id, user_id),
    CONSTRAINT fk_report_approver
        FOREIGN KEY (org_id, approver_id) REFERENCES users (org_id, user_id),
    CONSTRAINT fk_report_tombstoner
        FOREIGN KEY (org_id, tombstoned_by) REFERENCES users (org_id, user_id),
    CONSTRAINT reports_tombstone_complete CHECK (
        (tombstoned_at IS NULL) = (tombstoned_by IS NULL)
    ),

    -- The three approval facts are one fact. A report carrying an approver
    -- but no hash records an approval of unknown content; a report carrying a
    -- hash but no approver records content nobody approved. Neither is a
    -- partial approval, both are corruption, and the row is refused.
    --
    -- ISO 15189 7.4.1.5 c) requires the identity of the person who reviewed
    -- and authorised a report to be RETRIEVABLE, and spec 15.1 requires the
    -- approved content to be VERIFIABLE afterwards against the hash recorded
    -- at approval. A row that satisfies one half and not the other satisfies
    -- neither requirement: an identity attached to unidentified content is
    -- not a retrievable authorisation of anything, and a hash nobody is
    -- named against is not a verifiable approval. The two requirements are
    -- only met together, so the constraint admits the row only together.
    CONSTRAINT reports_approval_complete CHECK (
        (approver_id IS NULL) = (approved_at IS NULL)
        AND (approver_id IS NULL) = (content_hash IS NULL)
    ),

    -- Spec 13.3: approval is what makes a report releasable, and nothing else
    -- does. So the two states downstream of approval cannot be reached by a
    -- row that records no approval. This is the fail-safe as a constraint
    -- rather than a convention: a path that sets state = 'released' without
    -- writing the approval facts fails its write instead of releasing an
    -- unapproved report.
    --
    -- ISO 15189 7.4.1.5 c) again: it is the approver's action that releases a
    -- report for clinical use, and the standard requires that approver's
    -- identity be retrievable for the report that went out. A released row
    -- with approver_id NULL is a report in clinical use whose authoriser
    -- cannot be retrieved, which is the exact condition the clause forbids.
    -- Same reasoning as role_assignments_approver_needs_basis above, one
    -- step later in the lifecycle.
    CONSTRAINT reports_released_states_need_approval CHECK (
        state NOT IN ('approved', 'released') OR approver_id IS NOT NULL
    ),

    -- Required for the composite FK from release_events; same reasoning as
    -- users_org_user_unique. Redundant as uniqueness, not redundant
    -- structurally.
    CONSTRAINT uk_report_org_id UNIQUE (org_id, id)
);

CREATE INDEX idx_reports_org_interp ON reports (org_id, interpretation_id);
-- The review worklist reads one organisation's reports by state.
CREATE INDEX idx_reports_org_state ON reports (org_id, state);


-- ─── Phase 6: reviewer_claims ─────────────────────────────────────────────
--
-- Spec 13.2: the reviewer's claims about an interpretation. The table is
-- shaped by one rule from that section -- the pipeline's classification is
-- never silently overwritten, and a disagreement is A SECOND CLAIM RECORDED
-- ALONGSIDE the first, not a correction of it. Hence a claim table rather
-- than mutable columns on the interpretation: every claim is its own row,
-- older claims are not edited, and the interpretation's own classification is
-- untouched by any of them.
--
-- NOTE ON THE ASYMMETRY WITH consents, WHICH IS DELIBERATE. consents records
-- supersession with superseded_by, a BACKWARD pointer on the row being
-- replaced; this table uses supersedes, a FORWARD pointer on the row doing
-- the replacing. The difference is not an oversight and the two must not be
-- unified. consents is an ordinary table the application may update, so it
-- can afford to reach back and annotate the old row. reviewer_claims is
-- evidentiary and append-only by structure rather than by convention, so
-- there is no UPDATE with which to annotate anything, and the pointer has to
-- travel forward. Anyone tempted to make these two consistent should change
-- consents, never this.
--
-- EXPERT-BRANCH DECISION POINTS (spec 13.2, ratify vs re-derive). Six points
-- are affected and NONE is decided in this commit; they are recorded here so
-- that a later commit resolves them deliberately rather than by accident:
--   1. Screen ordering -- whether the review screen leads with the derivation
--      surface (re-derive) or with the existing classification (ratify).
--   2. Accept semantics -- whether an 'accept' claim asserts independent
--      confirmation or agreement with a draft. Same enum value, different
--      evidentiary weight; see claim_type below.
--   3. Rendering -- how a claim and the classification it addresses are shown
--      together on report surfaces.
--   4. Report claims -- which claims reach the released report, and how they
--      are attributed there.
--      DECIDED, 2026-09-13 (human ruling, R9 defect (a)): A REPORT CARRIES
--      ITS OWN CLAIMS AND ONLY ITS OWN. An amended report does NOT carry the
--      cumulative set including the original's. Two reasons, both about the
--      reader: showing the original's claims presents reasoning about a
--      document the reader is not holding, and a cumulative set makes it
--      impossible to tell which claim applied to which version. The
--      attribution mechanism is report_id on this table (see that column);
--      the retrieval is get_reviewer_claims(session, report_id). The
--      original is still never withdrawn -- it keeps its own claims, on its
--      own report, and both reports stay queryable.
--      Points 1, 2, 3, 5 and 6 remain open and are untouched by this.
--   5. D1 applicability -- whether the D1 criterion applies to a re-derived
--      classification on the same terms as to a ratified one.
--   6. Evidence capture -- what evidence_json must hold per claim_type;
--      re-derivation implies capturing what the reviewer consulted, ratifying
--      does not. The column is nullable for exactly this reason and its
--      contents are unconstrained until the branch is decided.

CREATE TABLE reviewer_claims (
    org_id              UUID        NOT NULL,
    id                  UUID        PRIMARY KEY NOT NULL DEFAULT gen_random_uuid(),
    interpretation_id   UUID        NOT NULL,

    -- THE REPORT THIS CLAIM BELONGS TO (R9 defects (a) and (b), human ruling
    -- 2026-09-13). Added because scoping claims to the INTERPRETATION alone
    -- made two different reports on one interpretation indistinguishable.
    --
    -- An amendment (see the amendments table below) is a SECOND reports row
    -- on the SAME interpretation. With claims scoped only to the
    -- interpretation, two things followed, both wrong:
    --
    --   (b) THE FALSE TAMPER ALARM. _compute_report_content_hash() hashed
    --       every claim on the interpretation, so the FIRST claim recorded
    --       for an amendment changed the recomputed hash of the ORIGINAL
    --       report, and verify_report_integrity() reported the original as
    --       tampered. Nothing had been tampered with. A false alarm on an
    --       integrity check teaches people to ignore integrity checks.
    --
    --   (a) THE AMENDED REPORT WITH THE ORIGINAL'S BODY. An amendment could
    --       not carry reviewer claims of its own, because every claim on the
    --       interpretation was equally every report's claim. There was no
    --       query that could answer "this report's claims" at all.
    --
    -- THE ALTERNATIVE THAT WAS REJECTED, recorded because it is cheaper and
    -- will be proposed again: bound the hash by reports.approved_at instead,
    -- hashing only claims recorded at or before approval. No schema change,
    -- and it silences the false alarm. It was rejected because it trades a
    -- false alarm for a real blind spot: a claim INJECTED after approval is
    -- precisely what an integrity check exists to catch, and a timestamp
    -- cutoff cannot tell that injection apart from an amendment's first
    -- legitimate claim. The check was never wrong to care that a claim
    -- appeared; it was wrong about which claims belong to which report.
    -- This column is what makes the data model match what is being verified.
    --
    -- NOT NULL, no "unassigned" state: a claim that names no report is the
    -- ambiguity this column exists to remove, and admitting one would put
    -- the old bug back one row at a time. A claim is therefore recorded
    -- AGAINST an existing report -- create_report() first, then the claims,
    -- then submit_for_review().
    --
    -- The claim's report must be a report OF this claim's interpretation.
    -- That is not a CHECK here (a row constraint cannot read the reports
    -- row) and is enforced by _validate_claim_inputs(), alongside variant
    -- membership and supersession uniqueness, which are unenforceable here
    -- for the same reason.
    --
    -- BACKFILL, for any database predating this column: every existing claim
    -- belongs to its interpretation's ORIGINAL report -- the earliest report
    -- on that interpretation that is not itself an amendment, the same
    -- derivation find_interpretation_by_submission_key() uses. Amendments
    -- could not have had claims of their own before this column existed, so
    -- there is no case this misattributes.
    --
    --   ALTER TABLE reviewer_claims ADD COLUMN report_id UUID;
    --   UPDATE reviewer_claims c SET report_id = (
    --       SELECT r.id FROM reports r
    --        WHERE r.org_id = c.org_id AND r.interpretation_id = c.interpretation_id
    --          AND NOT EXISTS (SELECT 1 FROM amendments a
    --                           WHERE a.org_id = r.org_id AND a.amendment_report_id = r.id)
    --        ORDER BY r.created_at, r.id LIMIT 1);
    --   ALTER TABLE reviewer_claims ALTER COLUMN report_id SET NOT NULL;
    --   ALTER TABLE reviewer_claims ADD CONSTRAINT fk_claim_report
    --       FOREIGN KEY (org_id, report_id) REFERENCES reports (org_id, id);
    report_id           UUID        NOT NULL,

    -- The variant this claim is about, identified within
    -- interpretations.run_document. NOT a foreign key, and cannot be one:
    -- variants are not rows anywhere in this schema, they live inside that
    -- JSONB document, so there is no table for this column to reference. The
    -- integrity that matters -- that the variant is one this interpretation
    -- actually contains -- is therefore the method's to check, like transition
    -- legality and supersession uniqueness below, and for the same reason.
    --
    -- NULLABLE, and the asymmetry is the point rather than an omission: an
    -- 'accept' is scoped to the whole interpretation and names no variant,
    -- while the other three are each about one specific variant. Added in
    -- amendment 2 because the claim_type CHECK above already commits to
    -- variant-scoped claims existing -- 'variant_added' means "the pipeline
    -- did not surface THIS variant" -- while the original table gave them no
    -- column to name the variant by. The gap was inside this table, and it
    -- surfaced the moment anything first had to write those rows.

    -- Variants are not rows in this platform; they live inside
    -- interpretations.run_document JSONB, identified by the engine's key
    -- (chrom-pos-ref-alt as produced by parse_variant_key). Reviewer claims
    -- reference them by that key rather than a foreign key. The system does
    -- not maintain a variant table; this column holds the key the engine
    -- assigned. NULLABLE: 'accept' and 'sole_signatory' are
    -- interpretation-scoped and name no variant; the other three claim types
    -- are variant-scoped.
    variant_key         TEXT,

    -- Spec 13.2's four reviewer actions, closed. 'disagree' does not remove
    -- the classification it disagrees with; 'variant_added' names a variant
    -- the pipeline did not surface; 'variant_not_relevant' scopes a variant
    -- out against the indication without deleting it.
    --
    -- 'sole_signatory' IS A FIFTH VALUE AND NOT ONE OF SPEC 13.2's FOUR
    -- (human ruling, 2026-09-13, w117). The product signs off with ONE
    -- person today. That person's concurrence had nowhere truthful to go:
    -- 'accept' is documented above and in _record_accept as an INDEPENDENT
    -- CONCURRENCE BY A SECOND CLINICIAN, so a same-person accept would write
    -- a row the schema itself defines as false -- the record would assert
    -- two-clinician review where one person acted.
    --
    -- A DISTINCT KIND MAKES THE RECORD TRUE AT THE LEVEL THINGS QUERY IT.
    -- Two consequences the ruling named, neither obtainable from prose in a
    -- reason field:
    --   - a later audit asking WHICH REPORTS HAD INDEPENDENT CONCURRENCE gets
    --     a correct answer by claim_type alone, without reading free text;
    --   - a site that adopts two-person sign-off can see WHICH HISTORICAL
    --     REPORTS WERE SINGLE-SIGNED, and decide what to do about them.
    --
    -- It is interpretation-scoped like 'accept' (variant_key and
    -- classification both NULL) and it satisfies submit_for_review's
    -- at-least-one-claim precondition like any other claim: review happened,
    -- and the row says exactly how much of it happened.
    --
    -- WHAT THIS VALUE DOES NOT DO: it does not change enforcement anywhere.
    -- The two-person path is untouched -- _approve_report still requires the
    -- Approver role on both the session and the recorded approver, and
    -- nothing here makes a second signature optional that was not already
    -- optional. Adopting two-person sign-off is a separate, carded decision.
    claim_type          TEXT        NOT NULL
        CHECK (claim_type IN ('accept', 'disagree', 'variant_added',
                              'variant_not_relevant', 'sole_signatory')),

    -- The classification the REVIEWER asserts: the replacement on a
    -- 'disagree', the proposed call on a 'variant_added'. NULL on 'accept'
    -- and 'sole_signatory' (both of which assert no new classification, only
    -- concurrence with the one already there) and on 'variant_not_relevant'
    -- (which scopes a variant out against the indication rather than
    -- reclassifying it).
    --
    -- Deliberately unconstrained for now. A CHECK against the ACMG five-tier
    -- vocabulary is the obvious next move and is NOT made here: the
    -- vocabulary this platform will accept is not yet settled across the
    -- pipeline, and fixing it in a table constraint before that decision
    -- would put the narrower list in the harder place to change.
    classification      TEXT,

    actor_id            UUID        NOT NULL,
    "timestamp"         TIMESTAMPTZ NOT NULL,

    -- The reasoning. Spec 13.2 requires it recorded for a disagreement and is
    -- silent on the other three; the column is NOT NULL for ALL FOUR, and the
    -- reason is the deferred branch rather than symmetry for its own sake.
    --
    -- Under re-derive, an 'accept' is not agreement with a draft -- it is an
    -- INDEPENDENT CONCURRENCE, a second clinician arriving at the same
    -- classification on their own. That is evidence. Evidence without stated
    -- grounds is not evidence, so a re-derive reading needs the grounds
    -- recorded for an accept exactly as much as for a disagreement. Making
    -- the column nullable now would foreclose that reading before the expert
    -- branch is decided (point 2 above); making it NOT NULL keeps both
    -- readings available, at the cost of requiring text on an accept.
    reason              TEXT        NOT NULL,

    -- Nullable pending expert-branch point 6 above. No shape is imposed yet.
    evidence_json       JSONB,

    -- The claim this claim supersedes, i.e. the earlier claim it corrects or
    -- replaces. NULL when this is an original claim. Written at INSERT, never
    -- afterwards -- which is the whole reason the pointer runs in this
    -- direction.
    --
    -- FORWARD, NOT BACKWARD, AND THE DIRECTION IS THE POINT. The obvious
    -- shape is the one consents uses: superseded_by on the OLD row, naming
    -- its successor. It cannot be used here. The superseding claim does not
    -- exist at the moment the old row is inserted, so a backward pointer can
    -- only ever be filled in by an UPDATE of that old row -- and this table
    -- is append-only, so there is no UPDATE to give. The available fix was a
    -- column-level UPDATE grant on that one column, and it was rejected
    -- deliberately: append-only-except-one-column is a rule with an
    -- exception, and an exception in an evidentiary table is the thing
    -- someone later widens. The new row naming what it replaces needs no
    -- exception at all. Append-only stays absolute: the old row is never
    -- touched.
    --
    -- The cost, stated plainly because it lands on every reader of this
    -- table: "is this claim superseded" is no longer a column read, it is a
    -- query.
    --     EXISTS (SELECT 1 FROM reviewer_claims
    --              WHERE supersedes = X.id AND org_id = X.org_id)
    -- And nothing here stops two claims superseding the same one; that a
    -- superseded claim cannot be superseded again is enforced by the method,
    -- like transition legality on reports, for the same reason -- a row
    -- constraint cannot see the rest of the table.
    --
    -- A superseded claim is still a claim that was made, and the read path
    -- must not quietly drop it. GEPER's own rule for amended reports (spec
    -- 15.2, implementing ISO 15189 7.4.1.8 b)/d)) -- the original is never
    -- modified and never withdrawn from the record -- is the same rule one
    -- level down.
    supersedes          UUID,

    -- Human ruling, tombstone-cascade (2026-09-09, following D2): a
    -- reviewer claim about a variant in a PURGED interpretation has no
    -- meaning -- it is an artefact of a specific clinical action on a
    -- specific VCF, not a record with standing of its own. Stamped by
    -- clinical_retention IN THE SAME TRANSACTION as its parent
    -- interpretation's own tombstoned_at (RetentionPrincipal
    -- ::_purge_interpretations), never independently. Same
    -- discipline as vcfs.tombstoned_at/tombstoned_by: NULL means live,
    -- nothing is ever removed, every other column is left exactly as
    -- written.
    tombstoned_at       TIMESTAMPTZ,
    tombstoned_by       UUID,

    CONSTRAINT fk_claim_interp
        FOREIGN KEY (org_id, interpretation_id) REFERENCES interpretations (org_id, id),
    CONSTRAINT fk_claim_report
        FOREIGN KEY (org_id, report_id) REFERENCES reports (org_id, id),
    CONSTRAINT fk_claim_actor
        FOREIGN KEY (org_id, actor_id) REFERENCES users (org_id, user_id),
    CONSTRAINT fk_claim_supersedes
        FOREIGN KEY (org_id, supersedes) REFERENCES reviewer_claims (org_id, id),
    CONSTRAINT fk_claim_tombstoner
        FOREIGN KEY (org_id, tombstoned_by) REFERENCES users (org_id, user_id),
    CONSTRAINT claim_tombstone_complete CHECK (
        (tombstoned_at IS NULL) = (tombstoned_by IS NULL)
    ),
    CONSTRAINT uk_claim_org_id UNIQUE (org_id, id)
);

-- The review screen reads every claim on one interpretation, oldest first.
CREATE INDEX idx_claims_org_interp
    ON reviewer_claims (org_id, interpretation_id, "timestamp");

-- "This report's claims", oldest first: the read behind both
-- _compute_report_content_hash() and get_reviewer_claims(). Ordered by
-- ("timestamp", id) at the query, so the index leads with "timestamp";
-- id can never tie, which is what makes that order total.
CREATE INDEX idx_claims_org_report
    ON reviewer_claims (org_id, report_id, "timestamp");


-- ─── Phase 6: release_events ───────────────────────────────────────────
--
-- Release is an EVENT, not a flag. A boolean "released" on reports answers
-- whether a report left the platform but not when, to whom, or carrying what
-- content -- and it cannot represent the ordinary case of one approved report
-- going to a clinician and then to a LIMS. One row per delivery.
--
-- content_hash is recorded again here rather than read back from reports at
-- display time, and the duplication is the point: it states what was actually
-- handed to THIS consumer. If it ever differs from reports.content_hash, that
-- difference is the finding (spec 15.1), and a design that read the hash back
-- from reports could not produce it.

CREATE TABLE release_events (
    org_id          UUID        NOT NULL,
    id              UUID        PRIMARY KEY NOT NULL DEFAULT gen_random_uuid(),
    report_id       UUID        NOT NULL,

    -- The downstream recipient (spec 13.4: a clinician, a patient, a LIMS,
    -- any consumer). Free text pending the delivery paths of a later commit;
    -- enumerating it now would fix a vocabulary before the paths exist.
    consumer        TEXT        NOT NULL,

    released_at     TIMESTAMPTZ NOT NULL,
    released_by     UUID        NOT NULL,
    content_hash    VARCHAR(64) NOT NULL,

    -- Human ruling, tombstone-cascade (2026-09-09, following D2): a
    -- delivery record for a PURGED report has no meaning of its own.
    -- Stamped by clinical_retention IN THE SAME TRANSACTION as its
    -- parent report's own tombstoned_at (RetentionPrincipal
    -- ::_purge_reports), never independently.
    tombstoned_at   TIMESTAMPTZ,
    tombstoned_by   UUID,

    CONSTRAINT fk_release_report
        FOREIGN KEY (org_id, report_id) REFERENCES reports (org_id, id),
    CONSTRAINT fk_release_releaser
        FOREIGN KEY (org_id, released_by) REFERENCES users (org_id, user_id),
    CONSTRAINT fk_release_tombstoner
        FOREIGN KEY (org_id, tombstoned_by) REFERENCES users (org_id, user_id),
    CONSTRAINT release_tombstone_complete CHECK (
        (tombstoned_at IS NULL) = (tombstoned_by IS NULL)
    ),
    CONSTRAINT uk_release_org_id UNIQUE (org_id, id)
);

-- Migration 0009. A report is released to the patient portal at most once at a
-- time; every other consumer, and re-delivery to the same one, is unconstrained.
-- The literal is clinical.data_access.PORTAL_RELEASE_CONSUMER (tied by a test).
CREATE UNIQUE INDEX release_events_portal_once
    ON release_events (org_id, report_id)
    WHERE consumer = 'patient_portal' AND tombstoned_at IS NULL;

-- "What left the platform for this report, and when" -- the question asked of
-- this table both by the audit path and by amendment (spec 15.2), which has
-- to know who received the original.
CREATE INDEX idx_release_org_report
    ON release_events (org_id, report_id, released_at);


-- ─── Phase 7 commit 2: Amendments (ISO 15189 7.4.1.8) ──────────────────────
--
-- Amendment records an existing approved report was revised: a new report is
-- created, the original is never modified. Both original and amendment stay
-- live and queryable. Which amendment is "current" is derivable, not stored
-- (the one with supersedes_amendment_id naming it -- see the query below).
--
-- Spec 15.2 requires the original report to be never withdrawn from the record
-- and traceable alongside the amendment. This table makes both constraints
-- structural: original_report_id is the immutable reference to what was
-- revised, and append-only prevents an amendment from being hidden or reverted.
--
-- Amendments are append-only: no UPDATE, only INSERT. A new amendment INSERTs
-- one row naming, in supersedes_amendment_id, the amendment it replaces (NULL
-- if it is the first amendment of this original). The row it replaces is never
-- touched. Pattern, identical in shape to reviewer_claims.supersedes:
--
--   A(supersedes=NULL) <- B(supersedes=A) <- C(supersedes=B, current)
--
-- "Current" is the row nothing else supersedes -- a query, not a column, for
-- the same reason reviewer_claims.supersedes is a query and not a column:
-- the superseding row does not exist yet at the moment the row it will
-- eventually supersede is inserted, so there is no backward pointer to fill
-- in without an UPDATE, and this table has none to give.

CREATE TABLE amendments (
    org_id              UUID        NOT NULL,
    id                  UUID        PRIMARY KEY NOT NULL DEFAULT gen_random_uuid(),
    original_report_id  UUID        NOT NULL,

    -- The new report this amendment IS: create_amendment() creates a fresh
    -- reports row (draft state, same interpretation as the original) and
    -- records its id here. Without this column an amendments row cannot
    -- name its own report, which create_amendment() needs to return and
    -- amendment_notifications needs to reference.
    amendment_report_id UUID        NOT NULL,

    -- Reason for change (NOT NULL). ISO 15189 7.4.1.8 requires the reason
    -- a report was amended to be retrievable.
    reason              TEXT        NOT NULL,

    -- This amendment supersedes the one named here (NULL for the first amendment).
    -- Append-only: written at INSERT, never updated. The new amendment row
    -- declares what it replaces, just like reviewer_claims.supersedes.
    -- Current amendment: the one that no other amendment supersedes.
    supersedes_amendment_id UUID,

    created_at          TIMESTAMPTZ NOT NULL,
    created_by          UUID        NOT NULL,

    -- Human ruling, tombstone-cascade (2026-09-09, following D2): "an
    -- amendment to an interpretation of data that no longer exists is
    -- incoherent." Stamped by clinical_retention IN THE SAME TRANSACTION
    -- as ITS OWN original_report_id's tombstoned_at
    -- (RetentionPrincipal::_purge_reports) -- NOT the amendment_report_id
    -- side. By the time original_report_id purges, D6's existing guard
    -- has already required amendment_report_id's own report to be
    -- tombstoned first (see that guard's own comment), so this is always
    -- the LAST of the two reports named here to become eligible; keying
    -- the cascade off original_report_id gives one deterministic trigger
    -- per amendments row rather than two competing ones.
    tombstoned_at       TIMESTAMPTZ,
    tombstoned_by       UUID,

    CONSTRAINT fk_amendment_original
        FOREIGN KEY (org_id, original_report_id) REFERENCES reports (org_id, id),
    CONSTRAINT fk_amendment_report
        FOREIGN KEY (org_id, amendment_report_id) REFERENCES reports (org_id, id),
    CONSTRAINT fk_amendment_creator
        FOREIGN KEY (org_id, created_by) REFERENCES users (org_id, user_id),
    CONSTRAINT fk_amendment_supersedes
        FOREIGN KEY (org_id, supersedes_amendment_id) REFERENCES amendments (org_id, id),
    CONSTRAINT fk_amendment_tombstoner
        FOREIGN KEY (org_id, tombstoned_by) REFERENCES users (org_id, user_id),
    CONSTRAINT amendment_tombstone_complete CHECK (
        (tombstoned_at IS NULL) = (tombstoned_by IS NULL)
    ),
    CONSTRAINT uk_amendment_org_id UNIQUE (org_id, id)
);

-- Amendments are immutable (append-only): the same rule as audit_log,
-- reviewer_claims, and release_events. No row update, only INSERT.
CREATE INDEX idx_amendment_org_original
    ON amendments (org_id, original_report_id);

-- Current amendment for one original (not superseded by any other amendment
-- of the same original):
--   SELECT * FROM amendments
--   WHERE org_id = ? AND original_report_id = ?
--     AND id NOT IN (
--       SELECT supersedes_amendment_id FROM amendments
--       WHERE org_id = ? AND original_report_id = ? AND supersedes_amendment_id IS NOT NULL
--     )


-- ─── Phase 7 commit 2: Amendment notifications ──────────────────────────────
--
-- Records that an ordering clinician was notified of an amendment (ISO 15189
-- 7.4.1.8 requires notification). The notification is recorded AS SENT at the
-- moment of amendment creation. Delivery mechanism (email, SMS, etc.) is
-- deferred to a later phase; this table records the event itself, not the
-- transport. Read state is DERIVED from notification_read_receipts table:
-- a receipt row is inserted when the clinician opens the notification, carrying
-- who and when (not a mutable flag). Current: a notification is read if a
-- receipt exists for it.

CREATE TABLE amendment_notifications (
    org_id                  UUID        NOT NULL,
    id                      UUID        PRIMARY KEY NOT NULL DEFAULT gen_random_uuid(),
    amendment_report_id     UUID        NOT NULL,

    -- Reason for the amendment (denormalized from amendments.reason for
    -- queryability without a join). The notification includes this so the
    -- ordering clinician can understand why the report was revised.
    reason_for_change       TEXT        NOT NULL,

    -- The role the notification was delivered to (e.g., 'ordering_clinician').
    -- Free text pending the role vocabulary of a later commit; enumerating it
    -- now would fix the vocabulary before the platform's delivery paths exist.
    delivered_to_role       TEXT        NOT NULL,

    created_at              TIMESTAMPTZ NOT NULL,
    created_by              UUID        NOT NULL,

    -- Human ruling, tombstone-cascade (2026-09-09, following D2): a
    -- notification about an amendment that no longer exists has no
    -- meaning. Stamped by clinical_retention alongside its amendments
    -- row, IN THE SAME TRANSACTION as that row's own tombstoned_at and
    -- the original report's (RetentionPrincipal::_purge_reports) --
    -- looked up by this row's own amendment_report_id, which is the
    -- same value the amendments row it belongs to carries.
    tombstoned_at           TIMESTAMPTZ,
    tombstoned_by           UUID,

    CONSTRAINT fk_notification_amendment
        FOREIGN KEY (org_id, amendment_report_id) REFERENCES reports (org_id, id),
    CONSTRAINT fk_notification_creator
        FOREIGN KEY (org_id, created_by) REFERENCES users (org_id, user_id),
    CONSTRAINT fk_notification_tombstoner
        FOREIGN KEY (org_id, tombstoned_by) REFERENCES users (org_id, user_id),
    CONSTRAINT notification_tombstone_complete CHECK (
        (tombstoned_at IS NULL) = (tombstoned_by IS NULL)
    ),
    CONSTRAINT uk_notification_org_id UNIQUE (org_id, id)
);

CREATE INDEX idx_notification_org_amendment
    ON amendment_notifications (org_id, amendment_report_id);


-- ─── Phase 7 commit 2b: Notification read receipts (append-only) ───────────
--
-- Records when a clinician opened/read a notification. An append-only record
-- carries WHO read it and WHEN, not a mutable flag. A notification is read
-- (derived) if at least one receipt row exists for it. Multiple receipts may
-- exist for the same notification if multiple clinicians with the same role
-- open it, or if the same clinician opens it multiple times.

CREATE TABLE notification_read_receipts (
    org_id                  UUID        NOT NULL,
    id                      UUID        PRIMARY KEY NOT NULL DEFAULT gen_random_uuid(),
    notification_id         UUID        NOT NULL,

    -- When the notification was read (opened by the clinician).
    read_at                 TIMESTAMPTZ NOT NULL,

    -- Who read it: the user_id of the clinician who opened the notification.
    read_by                 UUID        NOT NULL,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Human ruling, notification_read_receipts cascade (2026-09-09/10,
    -- following the tombstone-cascade dispatch's own D2 fallout): a read
    -- receipt for a notification that no longer exists has no meaning.
    -- Stamped by clinical_retention alongside the amendment_notifications
    -- row it belongs to, IN THE SAME TRANSACTION -- looked up by this
    -- row's own notification_id.
    --
    -- NOTE ON THE CHECK CONSTRAINT BELOW: the originating brief specified
    -- CHECK (tombstoned_at IS NOT NULL OR read_at IS NOT NULL), which
    -- would reject a live, unread receipt (a freshly-inserted row has
    -- both tombstoned_at NULL and, since read_at is itself NOT NULL,
    -- would need read_at populated at INSERT time regardless -- but the
    -- clause as written would ALSO forbid a row that is simply not yet
    -- tombstoned, since tombstoned_at IS NOT NULL is false for every live
    -- row and the OR would then hinge entirely on read_at, which is
    -- unrelated to tombstoning). This was flagged by god as wrong before
    -- implementation and NOT implemented as written; see this dispatch's
    -- report for the full argument. What is implemented instead is the
    -- same complete-pair check already used on the other five tombstoned
    -- tables (reviewer_claims, release_events, amendments,
    -- amendment_notifications, and reports/interpretations/vcfs before
    -- them): tombstoned_at and tombstoned_by are either both NULL or
    -- both set, independent of read_at entirely.
    tombstoned_at           TIMESTAMPTZ,
    tombstoned_by           UUID,

    CONSTRAINT fk_receipt_notification
        FOREIGN KEY (org_id, notification_id) REFERENCES amendment_notifications (org_id, id),
    CONSTRAINT fk_receipt_reader
        FOREIGN KEY (org_id, read_by) REFERENCES users (org_id, user_id),
    CONSTRAINT fk_receipt_tombstoner
        FOREIGN KEY (org_id, tombstoned_by) REFERENCES users (org_id, user_id),
    CONSTRAINT receipt_tombstone_complete CHECK (
        (tombstoned_at IS NULL) = (tombstoned_by IS NULL)
    ),
    CONSTRAINT uk_receipt_org_id UNIQUE (org_id, id)
);

-- Read receipts are immutable (append-only): the same rule as amendments,
-- reviewer_claims, and release_events. No row update, only INSERT.
CREATE INDEX idx_receipt_org_notification
    ON notification_read_receipts (org_id, notification_id);


-- ─── w122: Report retraction (model 3, human-approved 2026-09-13) ──────────
--
-- A retraction says "DO NOT ACT ON THIS DOCUMENT". It is NOT a state, NOT a
-- sixth value of reports.state, and NOT a column on reports. The approved
-- `reports` row stays BYTE-IDENTICAL: the report WAS approved on the day it
-- was approved, and a row rewritten to say otherwise would destroy THE
-- REPORT'S OWN record of that approval. Not the last trace of it -- w124
-- checked before letting "the only record" stand: _approve_report is
-- @auditable, so an audit_log 'report_approved' entry keeps the actor and the
-- moment. But no reader of this platform asks audit_log whether a report is
-- approved, and reports_approval_complete and
-- reports_released_states_need_approval force the three approval facts to
-- travel with the state, so "un-approving" cannot be done truthfully.
--
-- THE TRIGGER IS NOT BEING WORKED AROUND, IT IS BEING SERVED. The walk-back
-- clause in enforce_content_immutability() below exists because an approved
-- report could be "effectively WITHDRAWN FROM THE RECORD without touching a
-- single content column" -- the harm it names is INVISIBILITY, not
-- withdrawal. A row carrying an actor, a timestamp, a reason and a composite
-- FK to the report is the maximally visible withdrawal. No clause of that
-- trigger changes, no CHECK on reports.state changes, and the
-- GRANT UPDATE (state, approver_id, approved_at, content_hash) ON reports
-- line at the bottom of this file is untouched.
--
-- Append-only, exactly as reviewer_claims/release_events/amendments are, and
-- for the same reason: a retraction quietly edited or deleted after the fact
-- would leave no trace. There is no UPDATE grant for clinical_app and there
-- should never be one -- see the "WITH NO EXCEPTION" note in the grant block
-- below. E5 (human ruling): retraction is NOT reversible in this wave and no
-- reversal is built; if it is ever ruled in, its shape is already settled by
-- the rest of this schema -- a second row superseding the first, never an
-- edit -- and adding a self-FK to an append-only table is additive.

CREATE TABLE report_retractions (
    org_id              UUID        NOT NULL,
    id                  UUID        PRIMARY KEY NOT NULL DEFAULT gen_random_uuid(),

    -- The report being retracted. Composite FK: a retraction of another
    -- organisation's report is structurally impossible, not merely unlikely
    -- (the same reasoning as reports.approver_id).
    report_id           UUID        NOT NULL,

    retracted_at        TIMESTAMPTZ NOT NULL,
    retracted_by        UUID        NOT NULL,

    -- Reason (NOT NULL), following amendments.reason and
    -- reviewer_claims.reason: the reason a document was withdrawn must be
    -- retrievable, and a retraction nobody can account for is the invisible
    -- withdrawal this table exists to replace.
    reason              TEXT        NOT NULL,

    -- HUMAN RULING E1: a retraction may be recorded against a report that is
    -- 'approved' OR 'released', AND THE ROW RECORDS WHICH. Stored rather than
    -- inferred from reports.state, for the same reason the banners carry an
    -- explicit *_basis (ruling R2): reports.state advances after this row is
    -- written -- an approved report retracted today can be released
    -- tomorrow -- so reading it back later would silently change what this
    -- row claims. A report that was approved but never issued must never be
    -- described to a clinician as having been issued.
    retracted_from_state TEXT       NOT NULL
        CHECK (retracted_from_state IN ('approved', 'released')),

    -- HUMAN RULING E4: a replacement is OPTIONAL. A report issued against the
    -- wrong patient's sample has nothing to replace it with, and requiring a
    -- replacement would force a false row into existence to satisfy a
    -- constraint -- the exact shape reviewer_claims' sole_signatory ruling
    -- was created to avoid. NULL means "no replacement report has been
    -- issued", which the banner states rather than glossing over.
    replacement_report_id UUID,

    -- HUMAN RULING E8: no change to the retention clock. A retraction is an
    -- artefact of a specific clinical action on a specific report, not a
    -- record with standing of its own, so it cascade-tombstones WITH the
    -- report it retracts, in the same transaction, exactly like
    -- release_events and amendments (RetentionPrincipal
    -- ::_cascade_tombstone_report_dependents).
    tombstoned_at       TIMESTAMPTZ,
    tombstoned_by       UUID,

    CONSTRAINT fk_retraction_report
        FOREIGN KEY (org_id, report_id) REFERENCES reports (org_id, id),
    CONSTRAINT fk_retraction_retractor
        FOREIGN KEY (org_id, retracted_by) REFERENCES users (org_id, user_id),
    CONSTRAINT fk_retraction_replacement
        FOREIGN KEY (org_id, replacement_report_id) REFERENCES reports (org_id, id),
    CONSTRAINT fk_retraction_tombstoner
        FOREIGN KEY (org_id, tombstoned_by) REFERENCES users (org_id, user_id),
    CONSTRAINT retraction_tombstone_complete CHECK (
        (tombstoned_at IS NULL) = (tombstoned_by IS NULL)
    ),
    -- A report cannot be its own replacement: that would render a banner
    -- instructing the reader to obtain the document they are already holding.
    CONSTRAINT retraction_replacement_is_not_itself CHECK (
        replacement_report_id IS NULL OR replacement_report_id <> report_id
    ),
    CONSTRAINT uk_retraction_org_id UNIQUE (org_id, id)
);

CREATE INDEX idx_retraction_org_report
    ON report_retractions (org_id, report_id, retracted_at);


-- ─── w122: Retraction notification obligations (human ruling E6) ───────────
--
-- E6: notification of a retraction to the parties who already hold the report
-- is OWED, is RECORDED, and does NOT BLOCK the retraction. A lab that has
-- discovered a dangerous report must never be prevented from recording that
-- fact because an email did not send -- and no email can send, because THERE
-- IS NO DELIVERY TRANSPORT ANYWHERE IN THIS PLATFORM.
--
-- PER CONSUMER, NOT PER ROLE (human ruling E6, stated in those words). This
-- is the difference the RULING names, not the only structural difference from
-- amendment_notifications -- w124 counted them: that table has no `status`
-- column at all (see the paragraph below) and no uniqueness on its recipient,
-- so this one carries two columns and one constraint it has no counterpart
-- for. What E6 settled is the recipient column: amendment_notifications
-- hardcodes delivered_to_role = 'ordering_clinician'. release_events.consumer
-- is free text and names "a clinician, a patient, a LIMS, any consumer"
-- (spec 13.4): a release event's consumer may be a LIMS, and a role is an
-- abstraction over people that a LIMS is not. One obligation per DISTINCT
-- consumer that received this report, deduplicated by the unique constraint
-- below -- re-delivery to the same consumer is one debt, not two, the same
-- way D3 rules that re-delivery does not restart the retention clock.
--
-- WHAT `status` DOES NOT SAY. amendment_notifications' docstring says
-- notifications are "Recorded as sent" while the sentence before it says the
-- transport is deferred; only one of those can be true and it is the first.
-- A row here asserts a DEBT, never a transmission. The vocabulary therefore
-- has exactly one value today: there is no 'sent' and no 'discharged',
-- because nothing in this platform can send or discharge one. A discharge
-- value is added by the commit that builds the transport, which is the
-- commit that can honestly write it.
CREATE TABLE retraction_notifications (
    org_id              UUID        NOT NULL,
    id                  UUID        PRIMARY KEY NOT NULL DEFAULT gen_random_uuid(),
    retraction_id       UUID        NOT NULL,

    -- The downstream recipient, copied from release_events.consumer -- the
    -- party that actually received the retracted report.
    consumer            TEXT        NOT NULL,

    -- Denormalized from report_retractions.reason, for the same reason
    -- amendment_notifications denormalizes amendments.reason: the recipient
    -- has to be able to understand why without a join.
    reason_for_retraction TEXT      NOT NULL,

    status              TEXT        NOT NULL DEFAULT 'notification_owed'
        CHECK (status = 'notification_owed'),

    created_at          TIMESTAMPTZ NOT NULL,
    created_by          UUID        NOT NULL,

    tombstoned_at       TIMESTAMPTZ,
    tombstoned_by       UUID,

    CONSTRAINT fk_retraction_notification_retraction
        FOREIGN KEY (org_id, retraction_id) REFERENCES report_retractions (org_id, id),
    CONSTRAINT fk_retraction_notification_creator
        FOREIGN KEY (org_id, created_by) REFERENCES users (org_id, user_id),
    CONSTRAINT fk_retraction_notification_tombstoner
        FOREIGN KEY (org_id, tombstoned_by) REFERENCES users (org_id, user_id),
    CONSTRAINT retraction_notification_tombstone_complete CHECK (
        (tombstoned_at IS NULL) = (tombstoned_by IS NULL)
    ),
    -- One outstanding debt per consumer per retraction. Enforced here rather
    -- than by the writer's SELECT DISTINCT alone, so a second delivery path
    -- cannot double-count the same recipient.
    CONSTRAINT uk_retraction_notification_consumer
        UNIQUE (org_id, retraction_id, consumer),
    CONSTRAINT uk_retraction_notification_org_id UNIQUE (org_id, id)
);

CREATE INDEX idx_retraction_notification_org_retraction
    ON retraction_notifications (org_id, retraction_id);


-- ─── Phase 7 commit 4: Retention policy configuration (spec 22) ────────────
--
-- Human ruling D2 (2026-09-04): retention periods are CONFIGURABLE, never
-- hardcoded. RE-RULED 2026-09-09: "everything downstream of a VCF inherits
-- the VCF period -- five years minimum, ten for minors ... configurable per
-- organisation: a site with a stricter obligation sets its own; a site with
-- none inherits ours." THESE TWO NUMBERS ARE A FLOOR THE PRODUCT CHOSE,
-- WITHIN NABL 112A 7.8.5(b)(iv)'s stated range (>=5y, "at least 5-10 years"
-- for a minor) -- NOT a legal finding; see clinical/retention.py's
-- DEFAULT_ADULT_VCF_RETENTION_DAYS/DEFAULT_MINOR_VCF_RETENTION_DAYS for
-- where that same caveat travels with the numbers in code.
--
-- ONLY 'vcf' is configured here as of the 2026-09-09 ruling -- run_document
-- and report are no longer independently configurable: their period is
-- INHERITED from the vcf at creation (interpretations.retention_days,
-- reports.retention_days), never looked up here. A row that could be set
-- but silently ignored by every purge path is worse than no row at all, so
-- the surface was narrowed rather than left present-but-inert (an
-- engineering decision, not part of the ruling -- see
-- clinical/data_access.py::RETENTION_ARTEFACT_CLASSES).
--
-- retention_days is the ADULT override; retention_days_minor is the MINOR
-- override, independently nullable -- an organisation may set either, both,
-- or neither. Either column, when NULL, falls back to this codebase's own
-- product default (see clinical/retention.py) rather than to the other
-- column: an org that overrides only the adult number has not implicitly
-- chosen a minor number too.
CREATE TABLE retention_policies (
    org_id              UUID        NOT NULL,
    artefact_class      TEXT        NOT NULL,
    retention_days      INTEGER     NOT NULL CHECK (retention_days > 0),
    retention_days_minor INTEGER    CHECK (retention_days_minor IS NULL OR retention_days_minor > 0),
    created_at          TIMESTAMPTZ NOT NULL,
    created_by          UUID        NOT NULL,

    -- Org-scoped, like every reference in this schema (see fk_interp_parent's
    -- comment on the same point) -- one organisation's retention policy is
    -- not another's, and nothing here should make cross-org policy leakage
    -- structurally possible.
    PRIMARY KEY (org_id, artefact_class),
    CONSTRAINT fk_retention_policy_creator
        FOREIGN KEY (org_id, created_by) REFERENCES users (org_id, user_id)
);


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


-- w129 GAP-1 -- THE ONE TABLE IN THIS SCHEMA THAT WAS NOT ORG-SCOPED, AND THE
-- ONLY PLACE A CROSS-TENANT WRITE WAS REACHABLE OVER AUTHENTICATED HTTP.
--
-- This table used to carry no org_id and to name `REFERENCES exceptions (id)`
-- alone. The consequence was measured, not reasoned (w129 Phase A): an
-- authenticated session in organisation B, issuing
-- PATCH /exceptions/{id} against an exception id belonging to organisation A,
-- left A's exception untouched -- the UPDATE is org-scoped and matched zero
-- rows, silently, because _execute never inspects rowcount -- and then
-- appended a 'resolve' row to A's APPEND-ONLY event history, carrying B's user
-- id as actor and B's free text as action_note. A could see the row (its
-- readers resolve the exception org-scoped first and then read events by the
-- already-validated id) and could not explain it: the only audit record of who
-- wrote it sat in B's audit partition, which A's Auditor cannot read.
--
-- The single-column FK was satisfied because the row existed GLOBALLY. That is
-- the whole defect in one clause: a foreign key that does not carry the tenant
-- asks "does this id exist?" when the question is "does this id exist HERE?".
--
-- The composite target it needed already existed, twelve lines above:
-- exceptions_org_exception_unique UNIQUE (org_id, id). It was simply not used.
-- With the FK composite, the contaminating row is not refused at runtime by
-- code anyone has to remember to write -- it is INEXPRESSIBLE, which is the
-- guarantee every other child table in this schema already had (see
-- users_org_user_unique's comment for why that unique key is not redundant).
--
-- org_id is NOT NULL and references organisations directly as well, so a row
-- cannot exist without naming its tenant even in the seconds before the
-- composite FK is checked.
CREATE TABLE exception_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organisations (org_id),
    exception_id UUID NOT NULL,
    actor TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    -- w161 (migration 0005): THE AMENDMENT ACTION VOCABULARY. Four verbs, not
    -- three. `'amend_resolution'` is APPENDED rather than inserted, and the
    -- order matters: `pg_get_constraintdef` renders the list in the order it
    -- was written, and migration 0005 re-creates this constraint with the same
    -- four tokens in this same order so that a fresh database and an upgraded
    -- one carry one identical definition. Change the order here and the drift
    -- detector in test_w126m_schema_equivalence.py will report the two
    -- databases as different, correctly.
    --
    -- THE CONSTRAINT IS UNNAMED HERE AND NAMED IN 0005, ON PURPOSE. PostgreSQL
    -- names this `exception_events_action_check` -- it is the only unnamed
    -- CHECK on this table -- and 0005 DROPs and re-ADDs that exact name, so
    -- neither path invents an identifier the other does not have.
    --
    -- THE TOKEN CAME FROM THE HELD BRANCH w126-reresolve-amendment (95a33be),
    -- which names `'amend_resolution'` in its own schema.sql and in
    -- ExceptionEventAction.AMEND_RESOLUTION. Nothing else about that branch is
    -- imported. See 0005's header for the full derivation.
    --
    -- WHAT IS STILL NOT HERE: the held branch's
    -- `exception_events_amendment_names_its_target CHECK ((action =
    -- 'amend_resolution') = (amends_event_id IS NOT NULL))`. That is a
    -- SEMANTIC rule about what an amendment must carry, not a vocabulary, and
    -- it cannot be settled without ruling on the amendment verb -- which this
    -- wave is not authorised to do. It is carded for the verb's wave, where it
    -- is still additive: every row this schema can hold today satisfies the
    -- biconditional already.
    --
    -- AND STILL NOTHING WRITES ONE. `'amend_resolution'` is now a value the
    -- database will accept; no code in this repository produces it. The verb
    -- is the next authorisation's, exactly as `amends_event_id` was left by
    -- migration 0004.
    action TEXT NOT NULL CHECK (action IN ('open', 'resolve', 'reopen', 'amend_resolution')),
    action_note TEXT,

    -- w149 (migration 0004): THE AMENDMENT RELATIONSHIP, AND IT CARRIES THE
    -- TENANT. The event this row amends -- always another row of this same
    -- table. NULLABLE: an `open`, `resolve` or `reopen` amends nothing, and
    -- being NULL on every one of them is what makes it mean something when set.
    --
    -- THE HELD BRANCH w126 WROTE `amends_event_id UUID REFERENCES
    -- exception_events (id)` AND THAT SHAPE IS FORBIDDEN HERE. It is the exact
    -- defect the comment above this table describes: a single-column foreign
    -- key is satisfied because the row exists GLOBALLY, so an amendment could
    -- name an event belonging to another organisation and the database would
    -- accept it. The composite form below asks "does this id exist HERE?".
    --
    -- MATCH SIMPLE (the default) is correct and is a real difference from
    -- exception_events_org_exception_fk, whose two referencing columns are both
    -- NOT NULL. Here amends_event_id is nullable, so a NULL skips the check --
    -- which is the intent. When it is SET, org_id is NOT NULL, so both
    -- referencing columns are non-null and the check runs in full. The only way
    -- to evade MATCH SIMPLE is to null a referencing column, and nulling
    -- amends_event_id is nulling the amendment itself.
    --
    -- THE VOCABULARY ARRIVED IN 0005, THE PAIRING CHECK HAS NOT. This comment
    -- used to read "no action value denotes an amendment"; that stopped being
    -- true when migration 0005 (w161) widened the action CHECK above to admit
    -- `'amend_resolution'`, which is why it is rewritten here rather than left
    -- to rot. What the held branch ALSO carries and this schema still does not
    -- is `exception_events_amendment_names_its_target CHECK ((action =
    -- 'amend_resolution') = (amends_event_id IS NOT NULL))` -- a semantic rule
    -- about what an amendment must name, carded for the wave that rules the
    -- amendment verb. So an amendment event is now EXPRESSIBLE and still
    -- UNWRITTEN: `amends_event_id` appears in no INSERT in this repository
    -- outside the migration tests.
    amends_event_id UUID,

    CONSTRAINT exception_events_org_exception_fk
        FOREIGN KEY (exception_id, org_id) REFERENCES exceptions (id, org_id),

    -- The FOREIGN KEY TARGET for the self-reference below, derived from
    -- exceptions_org_exception_unique. Not redundant with the primary key: the
    -- primary key already enforces uniqueness, and this exists so that a
    -- foreign key can name (id, org_id) at all -- without it PostgreSQL answers
    -- 42830, "there is no unique constraint matching the given keys".
    CONSTRAINT exception_events_org_event_unique UNIQUE (org_id, id),

    CONSTRAINT exception_events_amends_event_fk
        FOREIGN KEY (amends_event_id, org_id) REFERENCES exception_events (id, org_id)
);

CREATE INDEX idx_exception_events_exception ON exception_events (org_id, exception_id, timestamp);


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

-- clinical_app is CREATED BY clinical/bootstrap.py, which must run against an
-- empty database BEFORE this file is applied. The grants below are real
-- statements and fail outright if the role does not exist; this comment used
-- to read "deployment, not here" and name no "there", so applying this schema
-- to a fresh database was impossible. See tests/test_bootstrap_empty_database.py,
-- which deliberately does not use the fixture that hid it.
--   CREATE ROLE clinical_app LOGIN PASSWORD '...';   -- done by clinical/bootstrap.py

-- Explicit grant required for hardened deployments. Stock Postgres grants
-- USAGE on the default 'public' schema to PUBLIC, so this omission is
-- invisible on a standard install and fails on a recreated or non-default
-- schema. The comment preserves the line against future removal.
GRANT USAGE ON SCHEMA public TO clinical_app;

GRANT  SELECT, INSERT         ON audit_log TO clinical_app;
REVOKE UPDATE, DELETE         ON audit_log FROM clinical_app;
REVOKE UPDATE, DELETE         ON audit_log FROM PUBLIC;
GRANT  USAGE, SELECT          ON SEQUENCE audit_log_log_id_seq TO clinical_app;

-- reviewer_claims and release_events are evidentiary records, and they are
-- held to audit_log's rule rather than the ordinary tables' rule for the same
-- reason audit_log is: both are exactly what someone would want to alter after
-- the fact. A reviewer's disagreement quietly edited into an agreement, or a
-- release event's consumer or content_hash rewritten after a report went out,
-- would leave no trace that anything had changed -- and GEPER's own rule
-- (spec 15.2, implementing ISO 15189 7.4.1.8 b)/d)) that the original is
-- never modified and never withdrawn from the record cannot be met by a
-- table the application is free to rewrite.
--
-- A wrong claim is corrected by appending a correcting claim that names the
-- one it replaces, which is what reviewer_claims.supersedes is for and what
-- append-only means, per the audit_log note above.
--
-- WITH NO EXCEPTION. There is no column-level UPDATE grant here and there
-- should never be one: the supersedes pointer is written at INSERT by the new
-- row, so correcting the record never requires touching an existing one.
-- These tables have exactly the privileges audit_log has, and a future
-- change that needs UPDATE on any of them is a design error rather than a
-- missing grant. amendments, amendment_notifications, and notification_read_receipts
-- are append-only by the same reasoning: an amendment, its notification, and read
-- receipts are recorded at creation and never touched again. The forward pointer
-- and read receipt are immutable once written.
-- fastq_sets is append-only for the same reason the tables above are: it
-- records what was observed on disk at a point in time, and a FASTQ set whose
-- paths or checksums could be rewritten afterwards is not evidence of what
-- arrived. There is no UPDATE grant and there should not be one -- see the
-- note on `state` at the table definition, which is the one place someone will
-- be tempted to ask for it.
GRANT  SELECT, INSERT ON fastq_sets TO clinical_app;
REVOKE UPDATE, DELETE ON fastq_sets FROM clinical_app;
REVOKE UPDATE, DELETE ON fastq_sets FROM PUBLIC;

GRANT  SELECT, INSERT ON reviewer_claims, release_events, amendments, amendment_notifications, notification_read_receipts TO clinical_app;
REVOKE UPDATE, DELETE ON reviewer_claims, release_events, amendments, amendment_notifications, notification_read_receipts FROM clinical_app;
REVOKE UPDATE, DELETE ON reviewer_claims, release_events, amendments, amendment_notifications, notification_read_receipts FROM PUBLIC;

-- w122: report_retractions and retraction_notifications join the append-only
-- block above, under the same "WITH NO EXCEPTION" rule and for the sharpest
-- version of the same reason: a retraction is a deliberate safety act, and a
-- retraction that could be edited or deleted after the fact would let a
-- withdrawal be un-said with no trace -- which is the invisible withdrawal
-- the immutability trigger exists to forbid, arriving by the other door.
--
-- A SEPARATE GRANT LINE, NOT AN ADDITION TO THE ONE ABOVE, so this wave's
-- diff cannot be misread as having touched the five tables that were already
-- there.
GRANT  SELECT, INSERT ON report_retractions, retraction_notifications TO clinical_app;
REVOKE UPDATE, DELETE ON report_retractions, retraction_notifications FROM clinical_app;
REVOKE UPDATE, DELETE ON report_retractions, retraction_notifications FROM PUBLIC;

-- D0b half one (human ruling, 2026-09-04): NOTHING DELETES. DELETE is revoked
-- on all nineteen of these, from clinical_app and from PUBLIC, in the same
-- belt-and-braces shape the append-only tables above use.
--
-- WHY IT COSTS NOTHING: there is not one DELETE statement in clinical/'s
-- production code -- measured, twice, at two different commits, not assumed.
-- The privilege being removed is one the application has never once exercised.
-- What it removes is the GAP between spec 15.1's "an approved report is
-- immutable" and a grant block that let the application delete the report
-- outright: the claim was resting on application code declining to do
-- something the database permitted, which is the same shape as an unverified
-- sign-off identity -- a guarantee asserted at a layer that cannot enforce it.
--
-- UPDATE DELIBERATELY STAYS. Deletion is unconditional and so a plain REVOKE
-- settles it; mutation is not, because reports are legitimately updated through
-- draft -> under_review -> approved -> released and a blanket REVOKE UPDATE
-- would break the state machine outright. State-conditional protection cannot
-- be expressed by a grant at all -- grants are per table, not per row -- so
-- that half is a separate design and a separate commit.
--
-- IF A DELETE IS EVER GENUINELY NEEDED, it is a design question and not a
-- missing grant: retention already answered the one real case by TOMBSTONING
-- rather than deleting (D4), which is why clinical_retention holds UPDATE and
-- is refused DELETE below.
GRANT  SELECT, INSERT, UPDATE
    ON organisations, users, totp_backup_codes, role_assignments, sessions,
       patients, external_identifiers, consents, tests, test_genes, orders, samples,
       sequencing_runs, reports,
       exceptions, exception_events, retention_policies,
       -- w130. EXACTLY the privileges users and sessions hold, and no more:
       -- both are mutated in place (the lockout counter, the idle window, the
       -- terminated_at stamp), so UPDATE is required and is not a widening.
       -- DELETE is absent here as it is for every table above, under the same
       -- D0b ruling: nothing deletes.
       patient_logins, patient_sessions
    TO clinical_app;

-- D0b, the free win (human ruling, 2026-09-04): interpretations and vcfs are
-- INSERT-ONLY for clinical_app. They are absent from the UPDATE grant above and
-- the privilege is revoked explicitly below, from clinical_app and from PUBLIC.
--
-- WHY IT COSTS NOTHING: clinical_app's legitimate UPDATE count on both tables is
-- ZERO -- enumerated by parsing every SQL-executing call site and constant-folding
-- its statement, not by searching for a name. Every write either role makes to
-- these two tables is one of five:
--     data_access.py:1649  create_vcf()               INSERT
--     data_access.py:1723  create_interpretation()    INSERT
--     data_access.py:1844  create_reanalysis()        INSERT   (spec 15.3: re-analysis
--                              creates a NEW interpretation, it never updates the old one)
--     retention.py:310     _purge_interpretations()   UPDATE   -- clinical_RETENTION
--     retention.py:358     _purge_vcfs()              UPDATE   -- clinical_RETENTION
-- The two UPDATEs belong to a different role, which keeps its grant below. So this
-- removes a privilege the application has never once exercised, exactly as the
-- DELETE revoke above did.
--
-- WHAT IT BUYS, AND IT IS NOT SMALL: interpretations.run_document is the evidence a
-- report's content hash is computed over. It is written at INSERT and never updated.
-- While clinical_app held UPDATE, a rewritten run_document would make
-- verify_report_integrity pass against tampered content -- the hash would still match,
-- because the thing it is a hash OF had moved. Detection could not see it. This closes
-- that by PREVENTION rather than detection, and needs no trigger, no column-level
-- grant, no new role and no state-conditional logic, because the intended surface here
-- is unconditional: never updated, by anyone, in any state.
--
-- Contrast reports, immediately above, where UPDATE deliberately stays: that table IS
-- legitimately updated through its state machine, so protecting it is state-conditional
-- and a grant cannot express it. That is the trigger's job, not this commit's.
GRANT  SELECT, INSERT ON interpretations, vcfs TO clinical_app;
REVOKE UPDATE         ON interpretations, vcfs FROM clinical_app;
REVOKE UPDATE         ON interpretations, vcfs FROM PUBLIC;
REVOKE DELETE
    ON organisations, users, totp_backup_codes, role_assignments, sessions,
       patients, external_identifiers, consents, tests, test_genes, orders, samples,
       sequencing_runs, vcfs, interpretations, reports,
       exceptions, exception_events, retention_policies
    FROM clinical_app;
REVOKE DELETE
    ON organisations, users, totp_backup_codes, role_assignments, sessions,
       patients, external_identifiers, consents, tests, test_genes, orders, samples,
       sequencing_runs, vcfs, interpretations, reports,
       exceptions, exception_events, retention_policies
    FROM PUBLIC;


-- ─── Phase 7 commit 4: the retention principal (spec 22, human ruling D0c) ──
--
-- "clinical_app's REVOKE stays ABSOLUTE -- the application still cannot
-- delete, which is what every claim rests on." clinical_app's own grants
-- above are UNCHANGED by this commit: no new privilege, on any table, is
-- given to clinical_app here. Retention is performed by a second, distinct,
-- narrowly-privileged login role instead -- the composition god's dispatch
-- named: D0 was framed as "who holds the DELETE grant" but D4 ruled
-- tombstone, not delete, so what this role actually needs is UPDATE (to
-- write tombstoned_at/tombstoned_by) and enough SELECT to decide what is
-- eligible, plus INSERT on audit_log so a purge is itself an audited act
-- (D7). It needs no DELETE anywhere -- tombstoning never removes a row.
--
-- Scope of what clinical_retention can touch is deliberately narrow and
-- named explicitly rather than inherited: the three artefact classes wired
-- to an actual purge path this commit (vcfs, interpretations, reports),
-- retention_policies, release_events and amendments (read-only, to compute
-- a release anchor and a live-descendant check), users (read-only, to
-- attribute a purge to the org's existing Phase 5c system principal --
-- see clinical/retention.py), and INSERT-only on audit_log, matching
-- clinical_app's own audit_log privilege exactly: the retention principal
-- can account for what it did and can rewrite nothing, including its own
-- account of itself.
--
-- retention_policies's SELECT grant here PREDATES human ruling D2's
-- 2026-09-09 age-conditional re-ruling and is UNUSED by
-- RetentionPrincipal as of that change: each of vcfs/interpretations/
-- reports now carries its own already-resolved retention_days (set once,
-- at creation, by clinical_app -- see clinical/data_access.py::create_vcf),
-- and purge_expired() reads that column directly rather than re-consulting
-- retention_policies per purge. Left in place rather than revoked: an
-- unused SELECT grant is not a hazard the way an unused UPDATE/DELETE
-- grant would be, and this commit's boundary is the age-conditional floor
-- and its propagation, not clinical_retention's privilege surface (see
-- the open FINDING-clinical-retention-holds-TABLE-level-UPDATE card,
-- deliberately untouched here for the same reason).
--
-- reviewer_claims, release_events, amendments and amendment_notifications:
-- EXTENDED here (human ruling, tombstone-cascade, 2026-09-09, following
-- D2) -- these four are no longer untouched. Each gains a
-- tombstoned_at/tombstoned_by pair (see each table's own comment above)
-- that clinical_retention stamps IN THE SAME TRANSACTION as the parent
-- artefact it hangs off (an interpretation for reviewer_claims; a report
-- for release_events and, via original_report_id, amendments and
-- amendment_notifications) -- never independently audited, never
-- independently purged, per the human's ruling that these four "are
-- artefacts of a specific clinical action on a specific VCF, not records
-- with standing of their own." Column-level UPDATE grants, not table-
-- level -- a deliberate choice for these four NEW grants (unlike
-- vcfs/interpretations/reports above, which predate this decision and
-- are Ryan's own open finding, untouched here per the dispatch's
-- boundary) precisely so this commit does not introduce a fresh instance
-- of the same over-broad-grant shape into the schema.
--
-- notification_read_receipts: EXTENDED here too (human ruling,
-- 2026-09-09/10, in direct answer to the residual gap named above --
-- this comment previously said it was NOT extended; that was true only
-- through the prior dispatch). A receipt for a since-tombstoned
-- notification is now cascade-tombstoned alongside it, IN THE SAME
-- TRANSACTION, looked up by the receipt's own notification_id -- one
-- more hop past amendment_notifications, same column-level-grant shape
-- as the other four, same "no independent audit entry, no independent
-- purge" reasoning.
GRANT  USAGE                       ON SCHEMA public TO clinical_retention;
GRANT  SELECT                      ON retention_policies, release_events, amendments, users TO clinical_retention;
GRANT  SELECT                      ON reviewer_claims, amendment_notifications, notification_read_receipts TO clinical_retention;
GRANT  SELECT, UPDATE              ON vcfs, interpretations, reports TO clinical_retention;
GRANT  UPDATE (tombstoned_at, tombstoned_by)
    ON reviewer_claims, release_events, amendments, amendment_notifications, notification_read_receipts
    TO clinical_retention;
-- w122, human ruling E8: a retraction and its notification obligations
-- cascade-tombstone WITH the report they are about -- same category as the
-- five above ("artefacts of a specific clinical action ... not records with
-- standing of their own"), same COLUMN-LEVEL grant shape rather than the
-- table-level one, so this wave introduces no fresh instance of the
-- over-broad-grant shape. E8 also settles what does NOT change: the purge
-- eligibility query in RetentionPrincipal::_purge_reports is untouched -- a
-- retraction neither shortens the window nor blocks the purge the way a live
-- amendment does (D6).
GRANT  SELECT                      ON report_retractions, retraction_notifications TO clinical_retention;
GRANT  UPDATE (tombstoned_at, tombstoned_by)
    ON report_retractions, retraction_notifications
    TO clinical_retention;
REVOKE DELETE
    ON vcfs, interpretations, reports, reviewer_claims, release_events, amendments, amendment_notifications, notification_read_receipts
    FROM clinical_retention;
REVOKE DELETE ON report_retractions, retraction_notifications FROM clinical_retention;
GRANT  INSERT                      ON audit_log TO clinical_retention;
GRANT  USAGE, SELECT               ON SEQUENCE audit_log_log_id_seq TO clinical_retention;


-- ─── D0b half two: content immutability (BEFORE UPDATE triggers) ───────────
--
-- Measured, not assumed (card phase7-d0b-approved-report-privilege-layer-
-- protection): a CHECK constraint cannot reference OLD ("missing FROM-clause
-- entry for table \"old\""). Row Level Security cannot compare OLD to NEW --
-- USING sees the old row, WITH CHECK sees the new row, nothing sees both --
-- and it fails SILENTLY (rowcount=0, no error), which would have broken
-- release: approved -> released is a legitimate UPDATE of an
-- already-approved row (data_access.py's _release_report), and
-- release_events is inserted FIRST, so a silently-refused state write would
-- release a report that still said "approved". Worse than no protection.
-- A BEFORE UPDATE TRIGGER is the only mechanism of the three that can see
-- both old and new, raise loudly, and still let a legitimate state advance
-- through.
--
-- THE RULE, human ruling: "state may ADVANCE, content may NOT CHANGE."
-- Not "an approved report may not be updated" -- that is the RLS finding
-- restated, and it is wrong for the same reason.
--
-- WHAT EACH TABLE'S PROTECTION BUYS (human ruling, stated per table so it
-- survives into the schema rather than living only in chat):
--   reports.content_hash: DETECTION. verify_report_integrity() recomputes
--     the hash and compares it against the value STORED on this row
--     (data_access.py, `return current_hash == stored_hash`). Protecting
--     the stored value does not stop an attacker altering run_document or
--     reviewer_claims -- it stops them making the ALTERED content verify,
--     by denying them the one thing that would let a lie pass the check.
--   interpretations.run_document: PREVENTION. There is no verification
--     step for this column the way there is for reports -- protecting it
--     is the only thing standing between the row and a silent rewrite.
--   vcfs (vcf_path, content_hash): PREVENTION, and it also protects the
--     MEANING of interpretations.submission_key (Phase 4a), which is
--     derived from this row's content_hash at submission time and would
--     silently stop matching what it was computed from if this row could
--     be altered afterward.
--
-- EXEMPTION, KEYED ON PRIVILEGE NOT ON current_user (human ruling 4, verbatim:
-- "Role names are strings that change; a privilege check is what the
-- guarantee actually rests on"). tombstoned_at/tombstoned_by are the one
-- pair of columns a non-application principal (clinical_retention) may
-- write. The trigger below checks has_column_privilege(current_user,
-- TG_TABLE_NAME, 'tombstoned_by', 'UPDATE') rather than current_user =
-- 'clinical_retention' -- a role-name check would silently stop working
-- the moment the role is renamed, and would not follow a grant given to
-- some future second retention-capable role. The privilege check follows
-- the grant, wherever it goes. This is why clinical_app's own UPDATE on
-- reports/interpretations/vcfs is rebuilt at COLUMN level, excluding
-- tombstoned_at/tombstoned_by, near the end of this file: the privilege the
-- trigger keys on must actually distinguish the two roles, and clinical_app's
-- original blanket table-level UPDATE (from the big GRANT above) would
-- otherwise make the check pass for it too -- see that section's own comment
-- for why a column-level REVOKE alone does not achieve this.
--
-- BINDS THE SUPERUSER TOO (human ruling 3): a plain trigger fires for every
-- role including superuser -- there is no automatic bypass the way Row
-- Level Security bypasses the table owner and superuser by default. Nothing
-- in the function below checks for or exempts a superuser role; the
-- content-immutability checks are pure OLD/NEW value comparisons with no
-- privilege escape at all, so no role, however privileged, can pass them.
-- A migration that genuinely needs to touch protected content is a
-- deliberate act that disables the trigger explicitly
-- (ALTER TABLE ... DISABLE TRIGGER ...) and re-enables it afterward; silent
-- bypass is what this trigger exists to remove.
--
-- LOUD REFUSAL (human ruling 2): RAISE EXCEPTION aborts the statement (and,
-- inside a transaction, the transaction) rather than silently doing
-- nothing, which is the RLS finding's exact failure mode. This means
-- data_access.py's write paths to these three tables can now fail in a way
-- they structurally could not before -- see ImmutabilityViolationError in
-- data_access.py, and _execute()'s translation of SQLSTATE P0001 into it.
--
-- NOT COVERED HERE: DELETE. That REVOKE landed separately (Ryan, D0b half
-- one, phase7-d0b-approved-report-privilege-layer-protection). This trigger
-- adds UPDATE protection on top of it; it does not touch DELETE grants.

CREATE OR REPLACE FUNCTION enforce_content_immutability() RETURNS TRIGGER AS $$
BEGIN
    IF TG_TABLE_NAME = 'reports' THEN
        -- state: mostly mutable, EXCEPT an approved or released report may
        -- not be walked backwards. This check was added after the original
        -- design here treated state as "always mutable, transition legality
        -- is the application's job" -- defensible when state was assumed to
        -- be an application-controlled workflow column. An audit (Ryan,
        -- test_report_state_is_a_control.py) established that premise was
        -- false: require_release() -- "the one gate every delivery path
        -- calls", data_access.py:4385 -- and create_amendment(),
        -- data_access.py:4449, both refuse unless state is approved or
        -- released. state GATES DELIVERY, so it is a control, and a control
        -- must not be freely rewritable by the principal it constrains.
        -- `UPDATE reports SET state = 'draft'` on an approved report was
        -- measured PERMITTED for clinical_app and for the superuser before
        -- this check existed -- an approved report could be walked back and
        -- effectively withdrawn from the record without touching a single
        -- content column, which no grant can see and only a trigger (it
        -- alone holds OLD and NEW together) can refuse.
        --
        -- The one forward transition past approval stays legal: approved ->
        -- released is exactly what _release_report performs. Everything
        -- else that moves state away from approved or released -- including
        -- released -> anything, since release is the platform's exit and
        -- nothing legitimately reopens it -- is refused. draft ->
        -- under_review -> approved and returned -> under_review are
        -- untouched by this check (OLD.state is neither approved nor
        -- released), so the application's own workflow preconditions still
        -- govern every transition below approval, same as before.
        IF OLD.state IN ('approved', 'released')
            AND NEW.state IS DISTINCT FROM OLD.state
            AND NOT (OLD.state = 'approved' AND NEW.state = 'released')
        THEN
            RAISE EXCEPTION 'reports: an approved or released report cannot have its state walked back (report %, % -> %)', OLD.id, OLD.state, NEW.state;
        END IF;

        -- Everything else about state: unchecked by this trigger. Spec
        -- 13.1's state machine below approval (draft -> under_review ->
        -- approved, returned -> under_review) is enforced by application
        -- preconditions, not here -- this trigger asks "is this transition
        -- legal" only for the one boundary a control's own state can be
        -- walked back across (approved/released), and otherwise still only
        -- ever asks "did content change".
        --
        -- approver_id/approved_at/content_hash: write-once together (the
        -- reports_approval_complete CHECK already requires all three NULL
        -- or all three set). Legal exactly once, at approval, moving from
        -- NULL to a value; frozen forever after. _approve_report only ever
        -- reaches this UPDATE when state = 'under_review', which the
        -- reports_released_states_need_approval CHECK guarantees means
        -- approver_id IS NULL, so this never fires against a genuine
        -- approval -- it exists for what a bug or a raw-SQL bypass would
        -- attempt.
        IF OLD.approver_id IS NOT NULL AND (
            NEW.approver_id IS DISTINCT FROM OLD.approver_id OR
            NEW.approved_at IS DISTINCT FROM OLD.approved_at OR
            NEW.content_hash IS DISTINCT FROM OLD.content_hash
        ) THEN
            RAISE EXCEPTION 'reports: approval facts are immutable once set (report %)', OLD.id;
        END IF;

        -- identity and provenance: never legitimately change after INSERT.
        IF NEW.org_id IS DISTINCT FROM OLD.org_id
            OR NEW.interpretation_id IS DISTINCT FROM OLD.interpretation_id
            OR NEW.created_at IS DISTINCT FROM OLD.created_at
            OR NEW.created_by IS DISTINCT FROM OLD.created_by
        THEN
            RAISE EXCEPTION 'reports: identity and provenance columns are immutable (report %)', OLD.id;
        END IF;

        -- tombstoned_at/tombstoned_by: privilege-gated, see comment above.
        IF (NEW.tombstoned_at IS DISTINCT FROM OLD.tombstoned_at
            OR NEW.tombstoned_by IS DISTINCT FROM OLD.tombstoned_by)
            AND NOT has_column_privilege(current_user, TG_TABLE_NAME, 'tombstoned_by', 'UPDATE')
        THEN
            RAISE EXCEPTION 'reports: tombstoning requires the retention principal''s privilege (report %)', OLD.id;
        END IF;

    ELSIF TG_TABLE_NAME = 'interpretations' THEN
        -- No column here is ever legitimately rewritten after INSERT --
        -- unlike reports, interpretations has no state machine and no
        -- write-once-then-frozen fact pattern. Full immutability except the
        -- privilege-gated tombstone pair.
        IF NEW.org_id IS DISTINCT FROM OLD.org_id
            OR NEW.vcf_id IS DISTINCT FROM OLD.vcf_id
            OR NEW.run_document IS DISTINCT FROM OLD.run_document
            OR NEW.submission_key IS DISTINCT FROM OLD.submission_key
            OR NEW.parent_interpretation_id IS DISTINCT FROM OLD.parent_interpretation_id
            OR NEW.created_at IS DISTINCT FROM OLD.created_at
            OR NEW.created_by IS DISTINCT FROM OLD.created_by
        THEN
            RAISE EXCEPTION 'interpretations: content and lineage columns are immutable (interpretation %)', OLD.id;
        END IF;

        IF (NEW.tombstoned_at IS DISTINCT FROM OLD.tombstoned_at
            OR NEW.tombstoned_by IS DISTINCT FROM OLD.tombstoned_by)
            AND NOT has_column_privilege(current_user, TG_TABLE_NAME, 'tombstoned_by', 'UPDATE')
        THEN
            RAISE EXCEPTION 'interpretations: tombstoning requires the retention principal''s privilege (interpretation %)', OLD.id;
        END IF;

    ELSIF TG_TABLE_NAME = 'vcfs' THEN
        -- Same reasoning as interpretations: nothing here is legitimately
        -- rewritten after INSERT except the tombstone pair.
        IF NEW.org_id IS DISTINCT FROM OLD.org_id
            OR NEW.sequencing_run_id IS DISTINCT FROM OLD.sequencing_run_id
            OR NEW.vcf_path IS DISTINCT FROM OLD.vcf_path
            OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
            OR NEW.created_at IS DISTINCT FROM OLD.created_at
            OR NEW.created_by IS DISTINCT FROM OLD.created_by
        THEN
            RAISE EXCEPTION 'vcfs: content columns are immutable (vcf %)', OLD.id;
        END IF;

        IF (NEW.tombstoned_at IS DISTINCT FROM OLD.tombstoned_at
            OR NEW.tombstoned_by IS DISTINCT FROM OLD.tombstoned_by)
            AND NOT has_column_privilege(current_user, TG_TABLE_NAME, 'tombstoned_by', 'UPDATE')
        THEN
            RAISE EXCEPTION 'vcfs: tombstoning requires the retention principal''s privilege (vcf %)', OLD.id;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_reports_content_immutability
    BEFORE UPDATE ON reports
    FOR EACH ROW EXECUTE FUNCTION enforce_content_immutability();

CREATE TRIGGER trg_interpretations_content_immutability
    BEFORE UPDATE ON interpretations
    FOR EACH ROW EXECUTE FUNCTION enforce_content_immutability();

CREATE TRIGGER trg_vcfs_content_immutability
    BEFORE UPDATE ON vcfs
    FOR EACH ROW EXECUTE FUNCTION enforce_content_immutability();

-- MEASURED, NOT ASSUMED: a column-level REVOKE cannot narrow a broader
-- TABLE-LEVEL GRANT that already covers that column -- has_column_privilege()
-- checks both and returns true if EITHER authorises it, so a plain
-- REVOKE UPDATE (tombstoned_at, tombstoned_by) ON ... FROM clinical_app
-- does nothing while the blanket GRANT SELECT, INSERT, UPDATE ... TO
-- clinical_app above still stands. The table-level grant has to be revoked
-- and rebuilt at column level for exactly what clinical_app's own code
-- writes.
--
-- reports: clinical_app writes state (submit_for_review, _release_report)
-- and approver_id/approved_at/content_hash together, once, at approval
-- (_approve_report). tombstoned_at/tombstoned_by are excluded from the
-- re-grant on purpose -- that exclusion IS the distinguishing privilege
-- has_column_privilege() checks for in the trigger above.
--
-- interpretations and vcfs: clinical_app has NO legitimate UPDATE path to
-- either table at all -- grep confirms zero UPDATE statements touching
-- either anywhere in data_access.py; every column on both tables is written
-- once at INSERT and never again except by clinical_retention's tombstone
-- write. So no column-level UPDATE grant follows for clinical_app on
-- either table -- not even a narrowed one.
REVOKE UPDATE ON reports, interpretations, vcfs FROM clinical_app;
GRANT UPDATE (state, approver_id, approved_at, content_hash) ON reports TO clinical_app;


-- ─── Migration 0010. CNV / structural-variant call sets ─────────────────────────────────────────────
--
-- The DDL of `clinical/migrations/0010_call_sets.sql`, minus its preflight (a new database has none of the
-- rows or tables that preflight guards). Kept equal to the migration by test_w126m_schema_equivalence.py and
-- test_callset_migration.py. Read that file for the design.

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

-- ─── schema_migrations ───────────────────────────────────────────────────────
--
-- THE ONE OBJECT THIS FILE GAINS FOR THE MIGRATION MECHANISM (w126m), and the
-- only schema change in that wave. Its DDL is not written out here: it is
-- `clinical/migrations/0001_schema_migrations.sql`, reproduced below so that
-- this file stays what it has always been -- the complete, self-contained
-- description of a NEW database -- while the migration file stays what a
-- migration has to be, the operation that reaches this state from the one
-- before it. The two are kept in step by
-- `clinical/tests/test_w126m_schema_equivalence.py`, which builds a database
-- from THIS file and another from the migrations and fails on any catalog
-- difference. That test, not a convention, is what stops them drifting.
--
-- Read `clinical/migrations/0001_schema_migrations.sql` for why the columns
-- are what they are.
CREATE TABLE schema_migrations (
    version     INTEGER     PRIMARY KEY,
    name        TEXT        NOT NULL,
    checksum    TEXT        NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_by  TEXT        NOT NULL DEFAULT current_user
);

REVOKE ALL                    ON schema_migrations FROM PUBLIC;
GRANT  SELECT                 ON schema_migrations TO clinical_app;
REVOKE INSERT, UPDATE, DELETE ON schema_migrations FROM clinical_app;

-- A FRESH DATABASE IS ALREADY AT THE LATEST VERSION, AND SAYS SO.
--
-- This file creates the CURRENT state directly -- it does not replay the
-- migrations -- so a database built from it has never run migration 1 or 2
-- and yet holds exactly what they produce. Without these two rows it would
-- report "2 pending" for ever, and the next deploy would try to run the
-- baseline against a database that already has it.
--
-- They are INSERTed here rather than by the application, or by a later step,
-- because `clinical/bootstrap.py --apply-schema` sends this whole file as one
-- statement inside one transaction: the stamps land in the same transaction
-- as the tables they describe, or neither lands. A stamp written afterwards
-- could be lost while the schema survived, which is the falsely-ready
-- database this mechanism exists to prevent.
--
-- THE CHECKSUMS ARE THE MIGRATION FILES' BYTES, SHA-256 OVER LF-NORMALISED
-- CONTENT (clinical/migrate.py::checksum_of; the normalisation is why a
-- Windows CRLF checkout and the Linux CI container agree). Adding a migration
-- means adding its DDL above AND its stamp here;
-- test_w126m_migration_mechanism.py::test_schema_sql_stamps_exactly_the_migration_set
-- reads these rows back OUT OF A DATABASE and fails if they do not match the
-- files on disk, so the pair cannot be half-done.
INSERT INTO schema_migrations (version, name, checksum) VALUES
    (1, 'schema_migrations', '00d8c90bb0eaba561d843cde7a45eca5af4d594aca6a3e230938aef27286f559'),
    (2, 'baseline',          'cc842c3943b7aa1e5786d638eff742f63fd47ce5a5048d7321036234a5290af8'),
    (3, 'report_retraction', 'd0e0ae8d149aa20aea64e18e47424f48efb63d0a7ccdd5cbfd9b3f8016202204'),
    (4, 'exception_amendment', 'a379abc3cf0a81da3af438f4bc092648ab196d1c53e5039cef106a02200743a0'),
    (5, 'exception_amendment_vocabulary', 'b2569f6f9927c82e8dd3c66142b053cd25efa8100abeeec607fc625fcc626de0'),
    (6, 'totp_replay_guard', '3db752c93973912b85076708d08f120b7062dcb6e8d949720fee43ff03fead94'),
    (7, 'order_test_same_org', '848b0c0cb9a4e2c19aecd28aeea694cbdbc8eb83a3f397a2bd5502ecf3fd7c02'),
    (8, 'active_test_name_unique', '0b8f520d74afc140175dfcd6218f8c441a1b5b80c35fe5c03cc292acf33546a1'),
    (9, 'portal_release_once', '4110a7fe900eeac745be879280387fd6e74b96b41221d70258740e651a2aae15'),
    (10, 'call_sets', '99ea97ec440ea04ffa254f172f2be1dc6aef3b223fc4b3d7d8b2ba6bce07fc71');
