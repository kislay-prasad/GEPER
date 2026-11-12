# GEPER Clinical service: deployment

This document covers the Docker image and Compose project for the GEPER Clinical
HTTP service (`clinical/`). It is **not** about the genomic pipeline image or
`docker-compose.yml`, which are unchanged and unrelated.

It states what was checked and what was not. Where something is described as
*verified*, it was run during the work that produced these files; where it is
described as *unverified*, it was not, and you should not rely on it.

## Not production-ready

**This stack is not production-ready, and bringing it up does not make a
deployment production-ready.** Specifically, none of the following is provided or
settled by the files in this repository:

| Missing | Consequence |
|---|---|
| **HTTPS.** Nothing terminates TLS. The service speaks plain HTTP. | The session cookie is `Secure`, so a browser will not use it over `http://` to a non-local host: remote use is impossible without a trusted TLS-terminating reverse proxy, and putting credentials on an unencrypted network is unsafe. |
| **Automated backups.** A manual `pg_dump` procedure is documented and was tested; nothing schedules, encrypts, ships or monitors backups. | A lost volume is lost data. |
| **Vulnerability scanning.** No image or dependency scan has been run (no scanner was available). | Known CVEs in the base image or packages are unknown. |
| **Hash-verified dependencies.** Versions are pinned, but not checked against digests. | A compromised package index could serve altered bytes for a pinned version. |
| **Retention.** The retention purge exists in code but nothing runs it. | Records are kept indefinitely. |
| **A tested multi-worker or high-load profile.** One worker; capacity is unmeasured. | See "Workers and the login throttle". |
| **Key escrow for the MFA key.** | See "Multi-factor authentication key". |

Section "Unresolved risks" collects these with what to do about them. Regulatory
and clinical-governance questions (data protection, retention periods, audit
review, who may read logs) are outside what a deployment file can answer.

## What is here

| File | Purpose |
|---|---|
| `clinical/Dockerfile.slim` | The service image: plain Python base pinned by digest, non-root, no test tools. Targets `runtime` and `test`. |
| `docker-compose.clinical.yml` | A separate Compose project: API, bundled PostgreSQL 16 (optional), init job, provisioning job, test profile. |
| `scripts/clinical-secrets.sh` | Creates and checks the secret files. Never overwrites, never prints. |
| `.env.clinical.example` | Non-secret configuration to copy to `.env.clinical`. |
| `clinical/docker-entrypoint.sh` | Reads `*_FILE` secrets into the environment for six listed variables. |
| `clinical/log_redaction.py`, `clinical/log_config.json` | Removes every query-parameter value from uvicorn's access log; the image starts uvicorn with this config. |
| `clinical/requirements.txt`, `requirements.lock` | Runtime dependencies: direct pins, and the full pinned set the image installs. |
| `clinical/requirements-dev.txt`, `requirements-dev.lock` | The same plus `pytest` and `httpx`, for CI and the `test` target. |

`clinical/Dockerfile` (built on the pipeline image) is unchanged and still
builds. The slim image is the one this document describes.

**Recommendation: supersede the `w125-clinical-service-up` branch.** That branch is
one commit that adds the clinical service to the *shared* `docker-compose.yml` and
`.env.example`, builds it from `clinical/Dockerfile` (the multi-gigabyte pipeline
image), and takes the database passwords as ordinary environment variables. This
document's stack is a separate Compose project with a clinical-only non-root image,
file-based secrets, an init job that verifies the migrations, and tests. The
branch was not modified, deleted or tested by this work; read-only inspection only.
Retire it once someone has confirmed that nothing on it is wanted that is missing
here. That decision, and the deletion, are the owner's.

## Quick start

For a local, single-machine stack with the bundled database. Requirements: Docker
with Compose v2.20 or later. Commands run from the repository root.

```bash
scripts/clinical-secrets.sh                      # creates ./secrets/* (git-ignored)
cp .env.clinical.example .env.clinical           # then edit; see "Secrets" for CLINICAL_UID
docker compose -f docker-compose.clinical.yml --env-file .env.clinical \
    --profile with-db up -d --build
```

This starts, in order: the database, the **init** job (roles, migrations,
verification), then the API on `127.0.0.1:8000`. Check it:

```bash
curl http://127.0.0.1:8000/readyz        # {"status":"ready","database":"ok","schema":"ok"}
docker compose -f docker-compose.clinical.yml --env-file .env.clinical ps
```

Then create the first administrator (next sections). To stop without losing data:
`down`. **`down -v` deletes the database volume.**

Use `-p NAME` to run a second copy under another project name; volumes and
networks are prefixed with the project name, and no container names are fixed.

## Secrets

Secrets are files, not environment variables, so they stay out of `docker
inspect`, the image and the Compose file. `scripts/clinical-secrets.sh` creates
seven files in `./secrets/`:

| File | Used by |
|---|---|
| `postgres_password` | the bundled database |
| `clinical_app_password`, `clinical_retention_password` | the init job sets the two login roles' passwords |
| `clinical_bootstrap_dsn` | the init job (database administrator connection) |
| `clinical_dsn` | the API and the provisioning job (the `clinical_app` role) |
| `clinical_totp_secret_keys` | the API (encrypts stored MFA secrets) |
| `clinical_first_admin_password` | the provisioning job; delete it after use |

The script's guarantees, each covered by a test:

* **It never overwrites.** An existing file is kept exactly as it is, including
  its mode. To rotate one, delete that file and run the script again.
* **It never prints a secret value.**
* **It creates the directory mode 700 and the files mode 600**, whatever the
  caller's `umask`, and refuses to create anything where git would track it.
* `scripts/clinical-secrets.sh --check` verifies the files without changing them:
  presence, non-empty, single line, modes, git status, and that each connection
  string agrees with its password file.

`.gitignore` ignores `secrets/` and `.env.clinical`. Never commit either.

**File ownership (unverified on native Linux).** Docker Compose ignores `uid`,
`gid` and `mode` on file secrets (verified on Compose 5.4.0; it prints a warning),
so a secret file is bind-mounted with the host's ownership and mode. On Docker
Desktop ownership is flattened and the default container user (10001) can read
the files. On a native Linux host a mode-600 file owned by you is **not** readable
by uid 10001: set `CLINICAL_UID` and `CLINICAL_GID` in `.env.clinical` to the
owner's ids (`id -u`, `id -g`; `--check` prints them). The image works with any
uid. This arrangement was reasoned from the Compose behaviour and has not been run
on a native Linux host.

**Using an externally managed PostgreSQL.** Omit `--profile with-db`, replace
`clinical_dsn` and `clinical_bootstrap_dsn` with the real connection strings
before the first `up`, and ignore `postgres_password`. The bootstrap step needs
`CREATEROLE`; managed services that withhold it need the two roles created by
their administrators first (unverified). The database network is `internal` in
the bundled setup; for an external database the API and init job are also on the
`app` network so they can route out.

## Database initialisation and migrations

The **init** job runs `python -m clinical.bootstrap && python -m clinical.migrate
--migrate && python -m clinical.migrate --verify`, and the API does not start unless
it exits 0.

* On an **empty database** it creates the two login roles and applies every migration
  in order (0001 to 0011 at the time of writing). *Verified, historical*: this was
  observed when the newest migration was 0009 (it applied 1 to 9 and the verifier
  reported OK at version 9); that stack run was not repeated for 0010 or 0011, which
  are covered by the migration test suites instead. The result was catalog-identical (602 columns, constraints, indexes,
  triggers and privilege facts) to a database built directly from `clinical/schema.sql`.
* On a **current database** it is a no-op. *Verified*: running the deploy command a
  second time updated the two roles' passwords, applied nothing, verified OK, left
  the catalog, the migration history (including `applied_at`) and the table count
  unchanged, and did not recreate the API container.
* On a database that **has the schema but no migration history** it refuses and
  the API does not start. That needs a deliberate `python -m clinical.migrate
  --adopt-existing` by an operator, never automatically.
* Migrations are forward-only. There is no down-migration; rollback means restoring
  a backup. Migration 0007 and 0009 refuse, with a count, if existing data would
  violate the new constraint, and change nothing.

`/readyz` re-checks the migration state on every call, so a database that drifts
or is behind is reported not ready (503) with the reason named.

### Migration 0010 revokes TEMPORARY on the database

Migration 0010 (call sets, Genome Analysis Phase 2) runs three statements against the
database it is applied to, naming that database (`current_database()`):

```sql
REVOKE TEMPORARY ON DATABASE <this database> FROM PUBLIC;
REVOKE TEMPORARY ON DATABASE <this database> FROM clinical_app;
REVOKE TEMPORARY ON DATABASE <this database> FROM clinical_retention;
```

A database built directly from `clinical/schema.sql` ends in the same state. The reason is
defence in depth beside the pinned `search_path` (it closes the `pg_temp` shadowing route);
the repository itself creates no temporary tables (checked by search when the migration was
written).

What this means in operation:

* **It is database-wide, not limited to the two application roles.** The default grant of
  `TEMPORARY` lives on `PUBLIC`, so every role in that database that held it only through
  `PUBLIC` loses it. Other databases in the same cluster are not affected.
* **Anything that creates temporary tables in this database now fails** with *permission
  denied to create temporary table*: ad-hoc sessions, reporting tools, scripts. Nothing in
  this repository does; tools outside it were not examined.
* **Who can apply it.** The database owner or a superuser. The migration checks first and
  refuses, changing nothing, if the administrative role is neither
  (`migration 0010 refused: … neither owns this database nor is a superuser, so cannot
  revoke TEMP`). On a managed database whose administrative role is neither, the migration
  will not apply until that is resolved.
* **Nothing in the stack undoes it, and there is no down-migration.** Restoring the
  privilege is an explicit action by the database owner or a superuser, for a specific role:
  `GRANT TEMPORARY ON DATABASE <database> TO <role>;`.
* **Do not restore it to `PUBLIC` or to the application roles.** The migration's own
  post-conditions, which `/readyz` and `python -m clinical.migrate --verify` re-evaluate,
  require that neither `clinical_app` nor `clinical_retention` has `TEMPORARY` on the database
  (including through a role they are members of) and that `PUBLIC` does not. Granting it to
  `PUBLIC`, to either application role, or to a role they inherit from makes the service
  report `schema_drift`. Granting it to a separate login that those roles are not members of
  does not.
* **After a restore, run `python -m clinical.migrate --verify`** (the restore steps below
  already do). Whether a `pg_dump`/`pg_restore` round trip carries this database-level
  privilege was not examined; if it does not, `--verify` reports `schema_drift` and the owner
  must apply the revokes again.

These statements come from the migration text and its tests; the `GRANT` and `--verify`
commands above were not run against this stack.

### Upgrading to migration 0011 (the consents index)

Migration 0011 adds `consents_patient_scope_latest`, the index the consent rule's
"latest consent" lookup needs. It changes **performance only**, never an answer: without
it each lookup scans the whole `consents` table (about 20 ms per lookup at 300,000
consents on a synthetic database, against about 0.02 ms with it).

**A new database needs nothing.** `consents` is empty, so the migration builds the index
itself, instantly. A database built from `schema.sql` already has it.

**A database that already holds consents does not get the index built by the migration**,
because a plain build blocks writes to `consents` for its whole duration and
`CREATE INDEX CONCURRENTLY` cannot run inside the migration's transaction. **The operator
builds it first, then the migration adopts it.** If it is absent, invalid or different in
any respect, the migration refuses, records nothing and changes nothing. *Tested*: the
migration command (`python -m clinical.migrate --migrate`) exits non-zero in that case and
the database is left exactly as it was. *Not yet verified*: that the Docker Compose stack
then keeps the API from starting. The API service declares `condition: service_completed_successfully` on the init job
(`docker-compose.clinical.yml`), so it should not start, but a failed upgrade was never run
against the stack. The
migration never repairs, rebuilds or removes an index.

The sequence (run it against the target database *before* `up` starts the init job; the
init job runs migrations automatically and nothing in this stack pauses it):

1. Back up the database and `secrets/` (above).
2. Check the context, then that no invalid index and no relation of that name exists. Run
   these read-only queries in `psql` against the target database:

   ```sql
   -- context: the database you mean, the role you are, and that `consents` resolves
   SELECT current_database(), current_user, current_schemas(false), to_regclass('consents');
   -- expected: your database name, the administrative role, {public}, consents

   -- any relation already using the agreed name (an index, table, view...)
   SELECT c.relname, c.relkind FROM pg_class c
    WHERE c.relnamespace = 'public'::regnamespace AND c.relname = 'consents_patient_scope_latest';
   -- expected: zero rows (otherwise stop; see step 6)

   -- leftover invalid indexes on consents
   SELECT indexrelid::regclass FROM pg_index
    WHERE indrelid = 'consents'::regclass AND NOT indisvalid;
   -- expected: zero rows
   ```
3. Build it with `CREATE INDEX CONCURRENTLY consents_patient_scope_latest ON consents
   (org_id, patient_id, scope, recorded_at DESC, consent_id DESC)`, in **autocommit**
   (not inside a transaction), as the table's owner (the administrative role), from a
   session that stays open for the whole build, with a `lock_timeout` set. It scans the
   table twice and waits for older transactions; watch `pg_stat_progress_create_index`.
   For the bundled database, `docker compose … exec clinical-db psql -U postgres -d clinical`
   is the analogue of the `pg_dump` command below (`clinical` is the bundled default
   database name; use yours); the runtime image carries no `psql`.
4. Verify it. Repeat the context query from step 2, then:

   ```sql
   SELECT indexdef FROM pg_indexes
    WHERE schemaname = 'public' AND tablename = 'consents' AND indexname = 'consents_patient_scope_latest';
   -- expected, exactly one row, exactly this text:
   -- CREATE INDEX consents_patient_scope_latest ON public.consents USING btree (org_id, patient_id, scope, recorded_at DESC, consent_id DESC)

   SELECT i.indisvalid, i.indisready, i.indislive, i.indisunique,
          i.indpred IS NULL AS no_where, i.indexprs IS NULL AS no_expressions,
          i.indnkeyatts, i.indnatts, c.reloptions
     FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
    WHERE c.relnamespace = 'public'::regnamespace AND c.relname = 'consents_patient_scope_latest';
   -- expected, exactly one row: t, t, t, f, t, t, 5, 5, and reloptions NULL
   ```

   The definition text pins the method, columns, order, directions, absence of `WHERE` and
   `INCLUDE`. The migration additionally checks operator classes and collations (a default
   build on these columns passes), and refuses anything else. *Verified*: the queries
   above return the stated values on a database built from `schema.sql` (PostgreSQL 16).
5. Only then `up -d` (or run `python -m clinical.migrate --migrate`).
6. If step 3 is interrupted or fails, PostgreSQL leaves an **invalid** index of that name.
   Remove it by hand and build it again; the migration will refuse it until you do.

**Not verified:** these commands were not run against this stack; `docker compose … run
--rm clinical-init python -m clinical.migrate …` for a separate migration step is inferred
from Compose, not tested; and what happens to an already-running API when init fails during
an upgrade was not observed. Rehearse on a disposable stack first.

Once applied, `/readyz` checks on every call that **some** valid index with that shape
exists (not that one particular name does), so removing the index makes the service report
`schema_drift`. Replace it by adding the new index first and removing the old one in a
later migration.

### Release note: staff genome reads are consent-gated

Applies to the four read-only staff routes of Genome Analysis (`GET /clinic/patients/{patient_id}/genome/chromosomes`,
`.../chromosomes/{name}`, `.../map` and `.../map/{name}`). No migration, no setting and no restart procedure; it is
code only.

* **What changed.** Every analysis these routes would return is now checked against the call-set consent rule
  (Policy C): the analysis's order must carry an **active** consent for its scope, **and** the patient's **latest**
  consent for that scope must not be withdrawn. A newer consent does not revive a withdrawn order consent.
* **Fail closed, whole read.** If **any** analysis of the patient fails the check, the **entire** response is refused
  with HTTP 403 and the message *Consent for this patient's analysis is withdrawn or not active; the analysis is
  withheld.* The routes never return a shorter list or an empty one, because that would read as "no findings".
  A patient who has no analysis at all still gets an empty list (there is nothing to withhold).
* **Who sees the message.** Only Orderer, Interpreter and Approver in the patient's own organisation, after the
  role and organisation checks. Any other role gets the generic 403, and another organisation's patient gets the same
  404 as an invented id. The refusal is audited as a denied `genome_read` with no details.
* **Map routes behave differently from before.** For a patient who has a small-variant analysis and a withdrawn or
  inactive consent, the map routes now answer 403 instead of drawing the map with a "held back" count for the call
  sets. A patient with call sets but no small-variant analysis is unchanged: the map still reports call sets held back
  for consent.
* **Clinicians will notice** that one withdrawn consent can hide **all** of a patient's analyses until the per-analysis
  notice of the planned Stage 1A exists. That is deliberate.
* **Not verified:** behaviour against a deployed stack; it is covered by the data-layer and HTTP tests only.

## First administrator

A hospital is onboarded by an operator, not through the API: there is no
registration route.

```bash
docker compose -f docker-compose.clinical.yml --env-file .env.clinical \
    run --rm clinical-provision --org-name "St Example Hospital" \
    --admin-email admin@example.org \
    --operating-context "Onboarding, ticket OPS-123, run by <your name>" --yes
```

It prints the new `org_id`, which login requires, and nothing secret. The
administrator's password is the content of `secrets/clinical_first_admin_password`;
**read it once, store it in a password manager, and delete the file.** Each run
creates a new organisation; it cannot add an administrator to an existing one.

*Verified*: one organisation, one administrator whose password is stored as a bcrypt
hash, an `organisation_provisioned` audit row with no actor, and the password absent
from the job output and from the audit details. Login with the right password
returned 204 and a cookie with `HttpOnly; Secure; SameSite=strict`.

**Accountability limit.** The audit row records the *operating context* you type
plus the operating-system user and host of the process. Inside a container that is
the generic `clinical` user and a container id, so the typed `--operating-context`
is the only real record of who ran it: put your name and the reason in it.

Failed-login behaviour, *verified*: a wrong password, an unknown e-mail and an
unknown organisation all return the identical `401 {"detail":"Authentication
failed."}`. After five wrong passwords the account is locked for 30 minutes and
even the correct password returns the same 401. Separately, a source address is
limited to 10 login attempts (successful or not) per 60 seconds; the 11th is
refused with `429` before any credential is examined.

## HTTPS and reverse proxy

**The service speaks plain HTTP and must not be exposed to a public interface.**
Compose publishes the API on `127.0.0.1` only and publishes no database port.

For any use beyond one machine, a trusted TLS-terminating reverse proxy must sit in
front, and none is provided here. The proxy must:

* terminate TLS and redirect plain HTTP, and set HSTS and the usual security
  headers (the service sets none, and sets no CORS policy);
* restrict the accepted `Host` header (the service does not check it);
* overwrite, not append to, `X-Forwarded-For` and `X-Forwarded-Proto`;
* be the only thing that can reach the API: give the API and the proxy a dedicated
  network with a fixed subnet, and do not publish port 8000.

**Trusting forwarded headers.** The service deliberately reads only the connection's
socket address, for the login throttle and for the address recorded in sessions and
audit rows. Behind a proxy that address is the proxy's, so the throttle becomes one
global cap (any caller can exhaust it for everyone) and audit rows lose the real
client. uvicorn can rewrite the client address, but only for peers on an allow-list.
Set `CLINICAL_FORWARDED_ALLOW_IPS` to the proxy's **explicit address or subnet**,
and only then. **Never `*`**: that lets any caller forge its own address.

*Verified* through a real disposable nginx 1.27 in front of the real stack (a
throwaway Compose project on a network with a fixed subnet; the proxy had a fixed
address, and `CLINICAL_FORWARDED_ALLOW_IPS` named exactly that one address; nginx
was configured to *append* to `X-Forwarded-For`, the harder case). Recorded in
`sessions.ip_address` and `audit_log.ip_address`:

| Request | Address recorded |
|---|---|
| Straight to the API from an untrusted container, with a forged `X-Forwarded-For: 203.0.113.99` | the container's real address; the header was ignored |
| Through the proxy, no header | the real client's address |
| Through the proxy with forged entries `203.0.113.77, 198.51.100.5` | the real client's address; the forged entries were discarded |
| 12 failed logins through the proxy from one client, a different forged address each time | attempts 1-10 answered 401, 11 and 12 answered 429: rotating the header did not evade the throttle |
| A second client through the same proxy while the first was throttled | answered 401, not 429: the throttle is keyed per client address, so one client does not lock out the others |
| Proxy address **not** on the list (the default `127.0.0.1`), with a forged header | every request was recorded as the proxy's own address; the header was ignored |

That last row is the safe default and also the reason to set the variable: without
it the throttle is one global cap and audit rows name the proxy. The semantics are
also pinned by tests that need no Docker (`test_w172_log_redaction_and_proxy.py`):
a direct untrusted peer cannot forge its address, a trusted proxy's right-most
untrusted entry is used, and a neighbouring address is not trusted. No wildcard was
used anywhere, and a test fails if the Compose default contains one. Although the
service tolerates an appending proxy, still configure the proxy to **overwrite**
the header. (Incidentally, the failed logins also locked the test administrator account after
five attempts, which is existing behaviour.)

## Multi-factor authentication key

`clinical_totp_secret_keys` holds the Fernet key list that encrypts each user's
stored second-factor secret. Without it, nobody can enrol a second factor, and an
account that has one cannot log in (fail-closed in both directions). The script
generates one; to make one by hand:
`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.

* **Storage.** The file is mode 600 on the host. Back it up **separately from the
  database backups**: a database backup together with the key lets whoever holds both
  decrypt every stored secret.
* **Rotation.** The list is comma-separated: the **first** key encrypts new secrets
  and **every** key decrypts. To rotate, put the new key first and keep the old one:
  `NEW,OLD`. Restart the API afterwards (`restart clinical-api`); the entrypoint reads
  the file when the container starts. Existing secrets stay readable under the old key.
* **Verified end to end** on the running stack (a second factor enrolled with a real
  one-time code, stored as a Fernet token and not as the plaintext secret; replay of a
  used code refused): after rotating to `NEW,OLD` and restarting, a login with a fresh
  code **still succeeded**, so the secret stored under the old key stays readable;
  with the old key **dropped** (`NEW` only) a valid, unused code was **refused**; with
  `NEW,OLD` restored, **the same code was accepted**. The failure was the missing key,
  not the code.
* **A missing old key is invisible to health checks.** In that state `/readyz` still
  returns 200 and the container is healthy, because the service cannot know which
  users hold secrets under which key; only the affected users find out, at login.
  Before removing an old key from the list, be sure no stored secret still depends
  on it.
* **An empty or unreadable key file stops the container** (the entrypoint exits 66 with
  a message naming the variable) instead of starting with MFA silently off.
* **There is no tool that re-encrypts existing secrets.** They stay under the key that
  encrypted them until that user re-enrols, so **the old key must be kept
  indefinitely** (or until every user has re-enrolled).
* **Losing the only key** locks every enrolled user out of their second factor. There
  is no recovery of the secrets; they fall back to their backup codes and then
  re-enrol (an administrator can reset enrolment). There is no escrow.
* MFA is available, not mandatory: nothing requires administrators to enrol. That is a
  policy decision, not a deployment one.

## External PostgreSQL

Omit the `with-db` profile and point `clinical_dsn` (the `clinical_app` role) and
`clinical_bootstrap_dsn` (the administrative role) at the database. The design was
reviewed against the code and exercised on a disposable PostgreSQL 16 with the
privileges a managed service typically gives. **Verified:**

* **A non-superuser is enough to initialise.** A role with `LOGIN CREATEROLE
  NOSUPERUSER` that *owns the empty database* ran `bootstrap`, applied migrations
  1-9 and passed `migrate --verify`. The one extension (`pgcrypto`) installs for a
  database owner on PostgreSQL 13 and later.
* **Roles that already exist are the trap.** If `clinical_app` and `clinical_retention`
  were created by someone else, bootstrap fails with *permission denied to alter role*
  (PostgreSQL 16 requires the `ADMIN` option on a role to alter it). After
  `GRANT clinical_app, clinical_retention TO <admin role> WITH ADMIN OPTION` the same
  run succeeded. Either let bootstrap create the roles or make that grant.
* **The DSN options are honoured**: `sslmode=require` made a connection to a server
  without TLS fail with *server does not support SSL*, so a DSN with `sslmode` is
  passed to the driver. That proves the option is read, not that TLS works.
* The API, built from the image, reached the database over a user-defined network and
  answered `/readyz` 200 with a `clinical_app` login.

**Design facts that matter, from the code and the Compose file (not load-tested):**

* **One new database connection per request** (no pool). Each request pays a TCP and
  possibly TLS handshake to a remote host, and concurrent requests each hold a
  connection. One worker serves one request at a time, so the connection count stays
  small here, but it is a cost over a network and a limit to check with the provider.
* **Network path.** The bundled database sits on an `internal` network. The API and
  init job are *also* on the `app` network, so they can route out to an external host;
  nothing was run to prove that from a restricted network. Decide egress rules
  deliberately (allow only the database host and port).
* **Three credentials**: `clinical_app` (API), `clinical_retention` (nothing runs it
  yet), and the administrative role, which only the init job reads.
* **Migrations run as the administrative role** (`CLINICAL_BOOTSTRAP_DSN`), so use the
  same role for every later migration; table ownership was not inspected here.
* The clinical suite was not run against a managed service.

**Not verified:** TLS to the database (certificate verification, a CA file mounted into
the container -- the Compose file mounts none, so `sslmode=verify-full` needs an
addition), a real managed service (RDS, Cloud SQL, Azure), connection limits and
failover, provider-specific restrictions on `ALTER ROLE` or extensions, backups
and point-in-time recovery on the provider's side, and latency over a WAN. The
`pg_dump` procedure below was tried against the bundled database only.

## Backup and restore

Back up the database **and** the secrets directory, separately and off the machine.
A database dump does not contain the login roles (cluster-level objects), the
secrets, or the MFA key.

```bash
# backup: custom format, from the running database
docker compose -f docker-compose.clinical.yml --env-file .env.clinical \
    exec -T clinical-db pg_dump -U postgres -Fc clinical > clinical-$(date +%F).dump
```

Restore into a **new, empty** database (same or a new cluster):

1. Start an empty database (`up -d clinical-db` on a fresh volume).
2. Recreate the roles with the same passwords: run `python -m clinical.bootstrap`
   from the init job's image and environment, without `--apply-schema`.
3. `pg_restore -U postgres -d clinical --exit-on-error < clinical-DATE.dump`.
4. **If the dump was taken before migration 0011 and `consents` holds rows**, build the
   consents index now, exactly as in "Upgrading to migration 0011" above (its steps 2 to
   4: check, `CREATE INDEX CONCURRENTLY`, verify), *before* any migration step. A dump
   taken at 0011 or later already contains the index; skip this step. Without it, applying
   0011 refuses (and `--verify` cannot say OK) because the restored table is populated.
5. Apply any pending migrations (`python -m clinical.migrate --migrate`; 0010 and 0011 for
   a pre-0011 dump), then run `python -m clinical.migrate --verify`; it must say OK.
6. Start the API.

The order is: restore, then index (pre-0011 populated dumps only), then migrate, then
verify, then start. The *Verified* restores below pre-date migration 0011 and were not
repeated for it, so the 0011 path of this procedure is untested end to end.

*Verified* with `docker exec` equivalents of these commands, twice: restoring into a
second database on the same cluster, and into a brand-new empty cluster whose roles
were created by the bootstrap step. Both restored copies had the identical catalog,
the same row counts (organisations, users, patients, tests, 45 audit rows) and the
same migration history, passed `--verify`, kept `clinical_app` unable to update or
delete the audit log, and accepted the application's own `clinical_app` login.

## Recovery limitations

* **Backups are not automatic, encrypted, monitored or tested on a schedule.** The
  dump contains patient records: treat it as such.
* **No point-in-time recovery.** Recovery returns to the time of the last dump; anything
  recorded since is lost, including audit rows.
* **Same major version only.** The restore was exercised on PostgreSQL 16 into
  PostgreSQL 16. Cross-version restores were not tried.
* **Secrets are not in the database.** Lose the secrets directory and the database
  passwords can be regenerated (run the script, then the init job, which resets the
  role passwords), but **the MFA key cannot** (see above).
* **Sessions are in the database.** After a restore, users logged in at the time of the
  dump have sessions that may or may not still be valid; the lockout and throttle state
  also follows the database (the throttle itself is in memory and resets on restart).
* **The `down -v` and volume-removal commands are irreversible.**

## Logs and privacy

*Verified* by driving real requests (login, catalogue, patient registration, consent,
a worklist query, unauthenticated and forbidden calls) and searching the entire logs
of the API, init and database containers for the values sent.

**What the access log is.** uvicorn's line: client address and port, the request
line (method, path, query string), and the status. Example shapes:
`POST /auth/login ... 204`, `POST /clinic/patients/<uuid>/consents ... 201`,
`GET /exceptions/worklist?owner=<redacted> ... 200`.

**Redaction (implemented).** uvicorn's default line carried the *value* of every
query parameter, for example an e-mail address in `?owner=`. The image now starts
uvicorn with `--log-config clinical/log_config.json`, which attaches a filter
(`clinical/log_redaction.py`) to the access handler:

* **every query-parameter value is replaced by `<redacted>`**, whatever the parameter
  is called (no list of names to maintain), including repeated parameters, encoded
  values, empty values and segments with no `=`;
* a parameter **name** is kept only if it is a plain token (letters, digits, `_ . - [ ]`,
  at most 64 characters); anything else is replaced too. Names are the API's
  vocabulary and are kept for diagnosis; a client that puts data in a *name* has put
  it in the URL it chose, and that is not hidden;
* method, path, HTTP version, status and the client address are **kept**;
* **fail closed**: a record with an unexpected shape, or a path containing a control
  character (log injection), is logged as `<request line redacted: unrecognised log
  record>` and never as the original text.

*Verified* by tests against a real uvicorn process started with the shipped config
(a distinctive value sent in the query, an `Authorization` header and a cookie never
appears in the output), through uvicorn's own formatter, and in the built image
(`GET /exceptions/worklist?owner=<redacted>&owner=<redacted>`). The reverse proxy's
own access log is **not** covered: configure it the same way.

**What is not in it.** No password, session id, cookie value, bearer token, MFA key,
request body (so no patient name, date of birth or e-mail typed into a form), and no
organisation id. None of those strings appeared anywhere in the container logs.

**What is in it, and why it matters:**

| Item | Observation | Concern |
|---|---|---|
| Record UUIDs in paths (`/clinic/patients/<id>/consents`, `/admin/users/<id>/roles`, `/review/reports/<id>/approval`) | Present | Pseudonymous identifiers. Not names, but linkable to a person by anyone who can read the database; whether they count as personal data is a regulatory question. |
| Query strings | Values are redacted (above). Before this change `?owner=<free text>` logged an e-mail address | Resolved for this log; the reverse proxy's log still needs the same treatment. |
| Client address | Present | Personal data in some regimes. Behind a proxy it is the proxy's address unless forwarded headers are trusted. |

**Record UUIDs in paths are still logged, deliberately.** Operators need them to
correlate a request with the `audit_log`. They identify patient records, so they
remain **sensitive identifiers** and the log must be treated as sensitive: restrict
who can read it, ship it only to somewhere with the same controls, and give it a
retention period.

**Recommendations** (the rotation cap and query redaction are implemented; the rest are not):

1. **Rotation is in place.** Each service keeps five files of 10 MB, so logs cannot fill
   the disk. That is a cap, not a retention policy.
2. **Decide who may read `docker logs`** and whether logs are shipped elsewhere; apply
   the same policy to the reverse proxy's access log, which records the same paths.
3. **Prefer not to put free text in query strings at all.** They are redacted here,
   but they also reach browser history and the proxy's log. The worklist's `owner`
   parameter is the case found; changing the route is an application change.
4. **If record UUIDs must not appear in logs**, start uvicorn with `--no-access-log`
   (state-changing actions are already recorded, with actor, action, resource and
   address, in the `audit_log` table) or extend the filter to mask UUID path
   segments. Not done: it needs a decision, because it removes the correlation key.
5. The audit trail is the database's `audit_log`, not these logs. Note that failed
   logins appear there with the action name `login_succeeded` and outcome `error`
   alongside `login_failed` rows; the naming is confusing but existing behaviour.

## Session identifiers and the audit log

**The finding.** The session cookie's value *is* `sessions.session_id`: it is the whole
of authentication. Until this change, `login_succeeded` and `patient_login_succeeded`
wrote that id into `audit_log.details` (and, after the `@auditable` opt-in default,
into `resource_id`). `audit_log` is append-only by grant, so the id could never be
removed, and anyone able to read the table (an Auditor through `/admin/audit`, a
database reader, a backup, a log export) could present it as a cookie for as long as
that session lived. By reading the code (not by exploiting a system), an Auditor could therefore have acted as an Administrator.

**What changed.** No audit field carries a session id any more. Staff login, patient
login, both logouts and an administrator ending a session record a **reference**
instead (`sref_` plus 32 hex characters, in `resource_id` and in `details` as
`session_ref` / `target_session_ref`). The reference is a SHA-256 digest of the session
id with a version prefix. The input is a server-generated random UUID (122 bits), so
the digest cannot be walked back by guessing, and no key is needed. The login row and
the logout row of one session carry the same reference, so a session can still be
followed from start to end. Both audit writers also rewrite a `session_id` key if a
future change names one, and the audit route shows a reference in place of the id in
rows written before the change. *Verified* over HTTP against the real database with the
real cookie value as the search string (`test_w173_session_ids_not_in_audit.py`).

**Session validation is not bound to a client.** *Verified*, by reading
`DataAccess.session_valid` and by a request with a different user agent and a different
`X-Forwarded-For`: `sessions` stores `ip_address` and `user_agent` at login, but the
lookup uses the session id alone, so the same cookie works from any address with any
user agent. This was not changed here (it is a separate hardening decision, with real
cost for users on mobile networks), and a test pins today's behaviour so a change is
deliberate. It is what makes a leaked session id directly usable.

**Existing deployments and historical rows.** Rows already written cannot be changed
(append-only by grant), and nothing here modified any. Do the following when this change
is deployed to a system that has real users:

1. **Treat existing `audit_log` rows, dumps and exports as holding credentials** until
   the sessions they name have ended. A session ends at logout, administrative
   termination, its idle timeout (30 minutes since last activity) or its absolute cap.
2. **After deploying, end every session that predates the deployment** so that no
   historical id is still usable. This signs everyone out once. It is an operator
   decision and was **not done or tested against any real data**; the statement is
   `UPDATE sessions SET terminated_at = now() WHERE terminated_at IS NULL` (and the same
   for `patient_sessions`), run as a database administrator.
3. Until then, the audit route no longer returns a session id in any row, old or new.
   A database reader still sees the old rows.
4. Keep backups taken before the change under the same controls as the database.

**Still open.** A random opaque cookie value that is *not* `sessions.session_id` would
remove this class of problem; so would binding sessions to a client. Neither is done.

## Workers and the login throttle

The image runs **one** uvicorn worker. Two facts from the code and a measurement bear
on raising it:

* All route handlers (42 operations at this writing) are `async def` and call blocking database and bcrypt code
  directly, so a worker serves one request at a time; a login (bcrypt) blocks it. The
  number of workers is therefore the concurrency. Capacity has **not** been measured.
* The login throttle (10 attempts per source per 60 seconds, counting successes) is
  **per process and resets on restart**. N workers multiply every limit by N, and the
  limit does not hold across replicas. The 10-per-60-seconds limit was *verified*: with
  two attempts already in the window, the 9th of a burst from one source was refused
  with `429`.

Each request opens its own database connection (no pool), so connections are at most
the worker count plus the jobs.

## Tools a person runs: the sign-off API

`approve`, `override`, `withdraw`, report-revision refresh and the LIMS export's status lookup are run by people. They
hold **no database credential** and have no `CLINICAL_DSN` fallback: they log in to this service over HTTPS
(`--clinical-url` / `GEPER_CLINICAL_URL`, `https://` only) and call the routes below. Design and invariants:
[SIGNOFF_API_DESIGN.md](SIGNOFF_API_DESIGN.md).

| Route | Roles | Answers |
|---|---|---|
| `GET /auth/me` | any staff session | the caller's own `user_id` and `org_id` |
| `GET /auth/me/signing-identity` | any staff session | the caller's OWN printed name, registration number, hospital (409 `incomplete_clinician_identity` if any is missing). No route takes another user's id. |
| `GET /review/reports/{id}` | Interpreter, Approver, Auditor, Administrator | state, interpretation id, `is_retracted`, creation time |
| `GET /review/reports/{id}/revision` | same | the authoritative revision block; `{}` only when the lookup succeeded and found nothing |
| `POST /review/reports/{id}/concurrence` | Interpreter | records the sole-signatory claim (201 `{claim_id}`) |
| `POST /review/reports/{id}/disagreement` | Interpreter | records a disagreement claim for one variant (201 `{claim_id}`) |
| `POST /review/reports/{id}/submission` | Interpreter | draft to under_review (204) |
| `POST /review/reports/{id}/approval` | Approver | under_review to approved (204) |

The four POST routes pre-date the tools using them; the tools only call them now. A write whose outcome is unknown (timeout,
5xx) is never retried by the client and writes no file; the operator is told to check the report's state first.

Organisation isolation is the session's `org_id` (a report of another organisation is the same 404 as
an absent one). An unreadable revision record is a 500 carrying `code: revision_record_unreadable`;
an authorization refusal is 403; neither is ever returned as an empty revision. The client
(`geper/review/clinical_http.py`) speaks `https://` only, follows no redirects, and offers only the four sign-off writes
above (no release, amendment or retraction). **Not done:** the background workers
(`api/submission_worker.py`, `api/exception_retry_worker.py`) still hold the application's database credentials, and
the three-call approve is not atomic.

## Running the tests

The suite resets its schema and changes database roles, so it must never touch the
real database. The `test` profile gives it a throwaway one (in memory, on an internal
network, no password), mounts the checkout read-only, and treats any skip as a failure:

```bash
docker compose -f docker-compose.clinical.yml --env-file .env.clinical \
    --profile test run --rm clinical-test
```

`run --rm` removes the test runner but **leaves the throwaway test database running**
(it is a dependency). Stop it when you are done, with the same profile:

```bash
docker compose -f docker-compose.clinical.yml --env-file .env.clinical --profile test down
```

Several tests read files outside `clinical/` (the repository's `geper/` tree,
`.env.example`, the workflow file), which is why the checkout is mounted rather than
copied into the image. The GEPER-side tests that exercise the clinical code
(`geper/api/tests` and three files in `geper/tests`) need the pipeline's Python
dependencies and are run on a machine that has them, against a disposable PostgreSQL
and with `CLINICAL_TEST_DSN` and `CLINICAL_TEST_DB_DISPOSABLE=1` set; they do not run
in the slim image.

Deployment-file tests (`clinical/tests/test_w170_deployment_files.py`,
`test_w171_compose_and_secrets.py`) need neither Docker nor a database.

## Verification

Run on Docker Desktop (Windows) with Docker 29.7.2 and Compose 5.4.0, PostgreSQL 16,
Python 3.12.14, in a throwaway Compose project with secrets generated outside the
repository:

* image build, contents (byte-identical to the source files), non-root, read-only
  filesystem, dropped capabilities, no tests or test tools; secrets read from files and
  absent from `docker inspect` and logs; the two refusals (both variable and file set,
  missing file);
* fresh initialisation, second initialisation, readiness, provisioning, login, lockout,
  throttle (above);
* second-factor enrolment, login with a one-time code, replay refusal, and the key
  rotation drill (above);
* access-log content before redaction, and after it in a real process and in the
  built image (above);
* client-address attribution through a real nginx, trusted and untrusted, with forged
  headers (above);
* initialisation as a non-superuser with `CREATEROLE`, and the pre-existing-role
  failure and its fix (above);
* backup and restore, same cluster and new cluster (above).

**Not verified:** a native Linux host (file ownership, `CLINICAL_UID`); any other Docker
Desktop platform; HTTPS (the proxy test was plain HTTP between disposable containers); more than one worker;
behaviour under load; TLS to, and a real managed, external PostgreSQL (only the
privilege model was exercised, on a plain PostgreSQL 16); the reverse proxy's own
access log; restoring across PostgreSQL versions; the image under a vulnerability scanner.

## Unresolved risks

| Risk | What to do |
|---|---|
| No HTTPS | Put a trusted TLS-terminating reverse proxy in front before any remote use. Do not publish port 8000. |
| No automated, encrypted, monitored backups; no point-in-time recovery | Schedule `pg_dump`, encrypt and ship the dumps, test restores, and back up `secrets/` separately. |
| No vulnerability scanning | Scan the image and the locked packages (for example with Trivy or Docker Scout) and act on the results; none was available here. |
| Dependencies pinned but not hash-verified; base images pinned by digest so OS updates are manual | Generate hashed lock files; move the Python and PostgreSQL digests deliberately and rerun the clinical suite. |
| MFA key has no escrow and no re-encryption tool | Keep the key and every older key in a separate, backed-up secret store. |
| Retention purge is not operable (`clinical_retention` has a role and a password; nothing runs it) | Define the retention policy and authorisation, then provide an explicit, audited operation. |
| Capacity unknown; handlers block the event loop; throttle is per process | Measure before raising workers; consider making handlers non-blocking (an application change). |
| A session id is the cookie and is not bound to an address or user agent | A leaked cookie is usable until the session ends. Keep sessions short, terminate sessions after any suspected exposure, and decide whether to bind them to a client. |
| Rows written before the session-reference change still hold session ids | See "Session identifiers and the audit log": end all pre-change sessions at deployment and treat old dumps as sensitive. |
| Logs contain record UUIDs and client addresses (query values are now redacted) | Treat the log as sensitive; decide who reads it, where it goes and how long it is kept. Redact the proxy's log too. |
| An external database was exercised only for privileges, not TLS or a managed service | Test the real target, including certificate verification and egress rules. |
| MFA is optional for administrators | Decide the policy. |
| Operator bootstrap needs a database-administrator connection string | It is mounted only into the init job, never the API; restrict who can read `secrets/`. |
| The first administrator has no second factor until they enrol | Enrol administrators immediately after provisioning. |
| `/readyz` reports database and schema state but the Docker health check runs it every 30 seconds with a real connection | Acceptable at this scale; revisit if probes become a load. |
| Native Linux secret-file ownership and Compose behaviour are unverified | Test on the target host: `scripts/clinical-secrets.sh --check`, then `up`. |

A deployment that has addressed all of the above, and the regulatory questions this
document cannot answer, can reasonably call itself production-ready. This one has
not.
