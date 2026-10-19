# Running the processing services (staging)

The processing hand-off is three long-running processes. They are deployed together from one image and share one
SQLite submission store on one host. This is a **controlled staging** setup: it has not been run against real patient
data, the real engine is not exercised by CI, and clinical validity has not been established.

| Service | Command (working directory `/app/geper`) | What it does |
|---|---|---|
| `processing-api` | `python -m uvicorn api.main:app --host 0.0.0.0 --port 8000` | `POST /interpretations` (API key bound to one organisation), `GET /interpretations/{id}`; `/healthz`, `/readyz` |
| `processing-worker` | `python -m api.submission_worker` | Claims queued submissions, runs the engine (`geper/main.py`), records the draft report |
| `processing-retry-worker` | `python -m api.exception_retry_worker` | Retries transient failures, escalates after 6 attempts, re-queues failures an operator resolved with `retry_check` |

`docker-compose.processing.yml` defines all three from `geper:latest` (built by the root `Dockerfile`; see
`docker-compose.yml`). It does not start the clinical database or the clinical service (`docker-compose.clinical.yml`).

## Configuration

Copy `.env.processing.example` to `.env.processing` (untracked) and fill it in. Nothing secret is stored in source control.

| Variable | Used by | Meaning |
|---|---|---|
| `CLINICAL_DSN` | worker, retry worker | Clinical database DSN (an application role, not an administrative one). Unset = refuses to start |
| `GEPER_API_KEYS` | API | `key:organisation-uuid`, comma separated; one key to one organisation. Malformed = refuses to start |
| `GEPER_CORS_ORIGINS` | API | Allowed browser origins. Unset = refuses to start (unless `GEPER_DEV_INSECURE=1`, never in staging) |
| `GEPER_VCF_HOST_DIR` | compose | Host directory mounted read-only at `/data/vcf` (= `GEPER_VCF_ROOT`) |
| `GEPER_VCF_ROOT` | API, worker | Set by the compose file. Layout and policy: `docs/VCF_STORAGE.md` |
| `GEPER_SUBMISSION_STORE_PATH` | all three | The SQLite store, on the shared `geper_state` volume |
| `GEPER_SUBMISSION_OUTPUT_ROOT` | worker | Per-submission engine output, on the `geper_output` volume |
| `BIJ_AI_CLI_PATH` | worker | The engine entry point; default `/app/geper/main.py` |
| `EXCEPTION_RETRY_POLL_INTERVAL` | retry worker | Seconds between passes (default 30) |
| `GEPER_WORKER_POLL_INTERVAL` | worker | Seconds between queue polls (default 5) |
| `GEPER_NCBI_EMAIL`, `GEPER_NCBI_API_KEY`, `GEPER_BLAST_DATABASE`, `GEPER_ALPHAMISSENSE_*` | worker | Engine settings, as in `.env.example` |

**VCF storage.** Create `<GEPER_VCF_HOST_DIR>/<organisation UUID>/` for each organisation. The platform writes files there
from outside; the services mount it read-only. The directory must be readable by the container user (root in the image)
and **not world-writable** — a world-writable root is refused at startup.

**External dependencies.** The engine calls third-party services (Ensembl, NCBI, ClinVar and others) and downloads model
weights on first use into the `geper_model_cache` and `geper_hf_cache` volumes; allow the worker outbound access, or
pre-populate those volumes. The API and the retry worker need only the clinical database.

## Start and stop

```
docker compose -f docker-compose.yml build                     # builds geper:latest (large)
docker compose -f docker-compose.processing.yml --env-file .env.processing config   # checks the settings
docker compose -f docker-compose.processing.yml --env-file .env.processing up -d
docker compose -f docker-compose.processing.yml --env-file .env.processing ps       # health status
docker compose -f docker-compose.processing.yml --env-file .env.processing stop     # graceful
```

## Startup, health and shutdown behaviour

- **Startup validation** (`api/service_health.py`, also `python -m api.service_health check-config <role>`): a service
  prints every configuration problem and exits 1 **before** taking work. Checks: `GEPER_VCF_ROOT` set, an existing
  directory, readable, not world-writable (API and worker); the store directory writable (all); `CLINICAL_DSN` set
  (workers); the engine file exists and the output root is writable (worker). The workers also exit 1 if the clinical
  database cannot be reached. The API checks at startup and reports problem *codes* on `/readyz`.
  There is no switch that disables the VCF-root check.
- **Health.** API: `/healthz` (alive) and `/readyz` (200 only with a valid configuration, otherwise 503 and codes).
  Workers: each rewrites a heartbeat file on every pass of its loop; the container health check fails when it is stale
  (retry worker 300 s; submission worker 1800 s engine timeout + 300 s, because it does not loop during an engine run).
  A crashed worker is restarted by `restart: unless-stopped`.
- **Liveness vs readiness (API).** `/healthz` only says the process answers. `/readyz` says the API can take a submission
  safely: it fails (503, problem codes only) when `GEPER_VCF_ROOT` is missing, unreadable or world-writable, when the store
  directory is not writable, or when a store file exists but cannot be opened as a SQLite database
  (`store_unreadable`). `/readyz` does **not** check PostgreSQL: the API itself reads and writes only the SQLite store and the
  VCF root; the clinical database is the workers' dependency. The compose health check uses `/readyz`.
- **Clinical database loss (workers).** Each pass the worker first checks the database connection. While it is unreachable
  the worker claims nothing and stops beating its heartbeat; after 3 failed checks in a row it exits 1 and the supervisor
  restarts it (a lost connection does not recover in place). Start-up connection attempts time out after 10 s. A job that
  is **already running** when the database disappears is not protected by this: it fails with an unexpected-error message
  and, because the exception cannot be written either, has no worklist entry. It must be re-queued by hand once the database
  is back (`SubmissionStore.requeue`), so check the worker log after any database outage.
- **Shutdown.** On SIGTERM/SIGINT a worker finishes the job it is on and claims no more. The submission worker's
  grace period is 120 s; a job still running then is killed. Its row stays `running`, becomes `interrupted` at the next
  start, and is completed only if its clinical record had already been written. Partial output is never published: the
  clinical record is written in one transaction after a validated, hash-verified run.
- **Logs.** json-file, rotated (20 MB x 5). Worker logs include failure messages and engine stderr, which can contain
  file paths: treat them as sensitive and restrict who can read them. This change did not audit them for patient data.

## Local smoke test on Windows (Docker Desktop)

**Status: the steps below have NOT been run.** The override file has been checked with `docker compose config` only and has
not been validated against a running Docker engine; the image has not been built here. Treat it as the intended
procedure, and record what actually happens the first time.

- **Two separate Compose projects.** `docker-compose.clinical.yml` is the project `geper-clinical` (database, one-shot
  `clinical-init`, clinical API). `docker-compose.processing.yml` is a second project: run it with `-p geper-processing`.
  They share no network, and neither file refers to the other.
- **A local-only network override is needed to use the bundled clinical database.** The bundled database sits on the
  clinical project's `db` network, which is `internal: true` and publishes no port; the processing project's default network
  cannot reach it. Join the two workers (not the API, which does not use the database) to that network from a file kept
  **outside the repository** (for example `C:\geper-local\processing.local.override.yml`):

  ```yaml
  services:
    processing-worker:
      networks: [default, clinical_db]
    processing-retry-worker:
      networks: [default, clinical_db]
  networks:
    clinical_db:
      external: true
      name: geper-clinical_db      # confirm with `docker network ls` once the clinical project exists
  ```

  Pass it as a second `-f`. The connection string written by `scripts/clinical-secrets.sh` already uses the host
  `clinical-db`, which resolves on that network. The workers read `CLINICAL_DSN` (not a `_FILE` secret), so copy the value
  from `secrets/clinical_dsn` into `.env.processing` without printing it.
- **Use a named volume for the VCF root in the first Docker Desktop test.** The services refuse a `GEPER_VCF_ROOT` that is
  world-writable, and a bind mount of a Windows folder is expected to be presented to the container with open permissions
  (not yet confirmed here). In the same override file, mount a Docker named volume over `/data/vcf` instead (compose merges
  by container path, so it replaces the bind mount), and copy the synthetic VCFs into `<org-uuid>/` in it with a one-off
  container. `GEPER_VCF_HOST_DIR` must still be set to any value, because the base file requires it.

  ```yaml
  services:
    processing-api:         { volumes: ["geper_vcf:/data/vcf:ro"] }
    processing-worker:      { volumes: ["geper_vcf:/data/vcf:ro"] }
    processing-retry-worker: { volumes: ["geper_vcf:/data/vcf:ro"] }
  volumes:
    geper_vcf:
  ```
- **Keep secrets out of the build context.** Generate them outside the repository (`CLINICAL_SECRETS_DIR`, set in
  `.env.clinical`). `.dockerignore` excludes `secrets/`, nested `.env*` files, keys, SQLite databases and the local
  `data/`, `output/` and submission-state folders, because the root image is built with `COPY . /app`.
- **First engine: the stand-in.** To exercise the plumbing without model downloads, set
  `BIJ_AI_CLI_PATH=/app/geper/api/tests/standin_engine.py` in `.env.processing` (the image is expected to contain the test
  files; unverified). The real engine needs the models and outbound network access.

## Limitations

- **Single host.** The processing claim is an atomic update in a SQLite file. Run one `processing-worker` per store and
  keep all three services on the same machine and volume. Not safe across hosts or on a network filesystem.
- The compose file was validated with `docker compose config` only; the multi-gigabyte image was not built and the
  containers were not started as part of this change.
- The engine has not been run by CI, FASTQ input is not connected to this workflow (it starts from a VCF), and no
  clinical validation has been done.
- Containers run as the image's default user (root) because the engine writes caches under `/app` and `/root`; `cap_drop`
  and `no-new-privileges` are applied. A non-root, read-only-root-filesystem variant is future hardening.
