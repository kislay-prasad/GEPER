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
    -- assigned. NULLABLE: 'accept' is interpretation-scoped and names no
    -- variant; the other three claim types are variant-scoped.
    variant_key         TEXT,

    -- Spec 13.2's four reviewer actions, closed. 'disagree' does not remove
    -- the classification it disagrees with; 'variant_added' names a variant
    -- the pipeline did not surface; 'variant_not_relevant' scopes a variant
    -- out against the indication without deleting it.
    claim_type          TEXT        NOT NULL
        CHECK (claim_type IN ('accept', 'disagree', 'variant_added',
                              'variant_not_relevant')),

    -- The classification the REVIEWER asserts: the replacement on a
    -- 'disagree', the proposed call on a 'variant_added'. NULL on 'accept'
    -- (which asserts no new classification, only concurrence with the one
    -- already there) and on 'variant_not_relevant' (which scopes a variant
    -- out against the indication rather than reclassifying it).
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
    -- must not quietly drop it. ISO 15189 7.4.1.8's rule for amended reports
    -- -- the original is never modified and never withdrawn from the record
    -- -- is the same rule one level down.
    supersedes          UUID,

    CONSTRAINT fk_claim_interp
        FOREIGN KEY (org_id, interpretation_id) REFERENCES interpretations (org_id, id),
    CONSTRAINT fk_claim_actor
        FOREIGN KEY (org_id, actor_id) REFERENCES users (org_id, user_id),
    CONSTRAINT fk_claim_supersedes
        FOREIGN KEY (org_id, supersedes) REFERENCES reviewer_claims (org_id, id),
    CONSTRAINT uk_claim_org_id UNIQUE (org_id, id)
);

-- The review screen reads every claim on one interpretation, oldest first.
CREATE INDEX idx_claims_org_interp
    ON reviewer_claims (org_id, interpretation_id, "timestamp");


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

    CONSTRAINT fk_release_report
        FOREIGN KEY (org_id, report_id) REFERENCES reports (org_id, id),
    CONSTRAINT fk_release_releaser
        FOREIGN KEY (org_id, released_by) REFERENCES users (org_id, user_id),
    CONSTRAINT uk_release_org_id UNIQUE (org_id, id)
);

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

    CONSTRAINT fk_amendment_original
        FOREIGN KEY (org_id, original_report_id) REFERENCES reports (org_id, id),
    CONSTRAINT fk_amendment_report
        FOREIGN KEY (org_id, amendment_report_id) REFERENCES reports (org_id, id),
    CONSTRAINT fk_amendment_creator
        FOREIGN KEY (org_id, created_by) REFERENCES users (org_id, user_id),
    CONSTRAINT fk_amendment_supersedes
        FOREIGN KEY (org_id, supersedes_amendment_id) REFERENCES amendments (org_id, id),
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

    CONSTRAINT fk_notification_amendment
        FOREIGN KEY (org_id, amendment_report_id) REFERENCES reports (org_id, id),
    CONSTRAINT fk_notification_creator
        FOREIGN KEY (org_id, created_by) REFERENCES users (org_id, user_id),
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

    CONSTRAINT fk_receipt_notification
        FOREIGN KEY (org_id, notification_id) REFERENCES amendment_notifications (org_id, id),
    CONSTRAINT fk_receipt_reader
        FOREIGN KEY (org_id, read_by) REFERENCES users (org_id, user_id),
    CONSTRAINT uk_receipt_org_id UNIQUE (org_id, id)
);

-- Read receipts are immutable (append-only): the same rule as amendments,
-- reviewer_claims, and release_events. No row update, only INSERT.
CREATE INDEX idx_receipt_org_notification
    ON notification_read_receipts (org_id, notification_id);


-- ─── Phase 7 commit 4: Retention policy configuration (spec 22) ────────────
--
-- Human ruling D2: retention periods are CONFIGURABLE PER ARTEFACT CLASS,
-- never hardcoded. This table is that configuration surface and nothing
-- more -- it holds no logic and enforces no retention by itself. Deliberately
-- empty: no row is seeded by this schema for any artefact_class, because
-- NABL 112A's >=5y figure (spec line 826) is a FLOOR, not a value, and every
-- other period is a counsel question (D2, D5) not yet answered. A class with
-- no row here is not "retained forever by default" -- it is unconfigured,
-- and clinical/retention.py's RetentionPrincipal fails loudly rather than
-- silently skipping or defaulting when it is asked to purge a class with no
-- policy row, per the same ruling: "make an unset period FAIL LOUDLY rather
-- than defaulting -- a silent default here would be a retention policy
-- nobody chose."
--
-- artefact_class values wired to an actual purge path in this commit: 'vcf',
-- 'run_document' (the interpretations table -- named this way because spec
-- 22.1's artefact list says "run documents", and interpretations.run_document
-- is the column that IS one), 'report'. Ordinary clinical_app privileges
-- (below) -- this is policy configuration, not evidentiary data, and an
-- Administrator sets it the same way any other configuration is set.
CREATE TABLE retention_policies (
    org_id          UUID        NOT NULL,
    artefact_class  TEXT        NOT NULL,
    retention_days  INTEGER     NOT NULL CHECK (retention_days > 0),
    created_at      TIMESTAMPTZ NOT NULL,
    created_by      UUID        NOT NULL,

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

-- reviewer_claims and release_events are evidentiary records, and they are
-- held to audit_log's rule rather than the ordinary tables' rule for the same
-- reason audit_log is: both are exactly what someone would want to alter after
-- the fact. A reviewer's disagreement quietly edited into an agreement, or a
-- release event's consumer or content_hash rewritten after a report went out,
-- would leave no trace that anything had changed -- and ISO 15189 7.4.1.8's
-- requirement that the original is never modified and never withdrawn from the
-- record cannot be met by a table the application is free to rewrite.
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
       exceptions, exception_events, retention_policies
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
-- retention_policies (to read configured periods), release_events and
-- amendments (read-only, to compute a release anchor and a live-descendant
-- check), users (read-only, to attribute a purge to the org's existing
-- Phase 5c system principal -- see clinical/retention.py), and INSERT-only
-- on audit_log, matching clinical_app's own audit_log privilege exactly:
-- the retention principal can account for what it did and can rewrite
-- nothing, including its own account of itself.
--
-- reviewer_claims and amendment_notifications are deliberately untouched:
-- no policy exists to purge them (human ruling D2: Phase 6/7 tables absent
-- from spec 22.1's list "inherit nothing by default; each needs a period
-- named"), so clinical_retention has no grant on them and no code path
-- writes to them. Extending retention to those tables is future work, not
-- an omission -- if it lands, it needs its own commit and its own grant,
-- exactly as this commit needed one.

-- CREATE ROLE clinical_retention LOGIN PASSWORD '...';   -- deployment, not here

GRANT  USAGE                       ON SCHEMA public TO clinical_retention;
GRANT  SELECT                      ON retention_policies, release_events, amendments, users TO clinical_retention;
GRANT  SELECT, UPDATE              ON vcfs, interpretations, reports TO clinical_retention;
REVOKE DELETE                      ON vcfs, interpretations, reports FROM clinical_retention;
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
