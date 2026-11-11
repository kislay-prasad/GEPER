# VCF storage for the interpretation API and worker

`POST /interpretations` takes a `vcf_path`: a path on the **server's** filesystem. Which paths are accepted is decided by
`geper/api/vcf_paths.py`, at the API and again in the worker immediately before it reads the file.

## Configuration

| Setting | Meaning |
|---|---|
| `GEPER_VCF_ROOT` | The one approved root. Required. An existing directory; unset, blank or missing = **every submission refused** (API: HTTP 503; worker: row failed with `vcf_root_not_configured`). |

Set it in the environment of **both** the API process and every worker process (see `.env.example`). They must see the same
storage: a worker that cannot see the file the API accepted will report it as missing. No default path is built in.

Related settings (not new): `GEPER_SUBMISSION_STORE_PATH` (the SQLite store; default `geper/.submissions.db`),
`GEPER_SUBMISSION_OUTPUT_ROOT`, `BIJ_AI_CLI_PATH`, and `CLINICAL_DSN` for the worker.

## Layout

```
$GEPER_VCF_ROOT/<organisation UUID>/<file>.vcf
$GEPER_VCF_ROOT/<organisation UUID>/<any subdirectories>/<file>.vcf.gz
```

Each organisation owns the directory named by its UUID (the organisation of the API key, never a field of the request). A
submitted path is accepted only if, **after every symlink is resolved**, it lies inside that directory. Relative paths are
read relative to it. Absolute paths are accepted if they resolve inside it.

## What is accepted and refused

- Accepted: a `.vcf` or `.vcf.gz` file (any letter case) inside the organisation's directory. A file that does not exist
  yet is accepted; the worker records it as `vcf_missing` for an operator.
- Refused, with the exception reason code an operator will see on the order:

| Reason | Cause |
|---|---|
| `vcf_root_not_configured` | `GEPER_VCF_ROOT` unset, blank or not a directory (deployment fault) |
| `vcf_path_invalid` | not text, blank, contains NUL, longer than 4096 characters (or, on Windows, a `:` after the drive) |
| `vcf_path_traversal` | any `..` component |
| `vcf_path_outside_organisation_directory` | resolves outside the directory: another organisation's, the root, elsewhere, or a link out |
| `vcf_path_not_a_vcf` | not `.vcf` / `.vcf.gz` |
| `vcf_path_not_a_file` | exists but is not a regular file |

At the API a refusal is HTTP 400 and nothing is stored; the answer never repeats the path. A row that reaches the worker
with a refused path (stored before this policy, or changed since) fails and opens a `validation_failure` exception owned
by `lab_operator` on its order. It is never retried automatically; repeat attempts reuse the same exception.

## Filesystem permissions

- Only the platform may **write** under `GEPER_VCF_ROOT` (the process that places VCFs for organisations). API keys cannot
  create files, but anyone who can write there can choose what the engine reads.
- The API and worker accounts need **read** and directory-traverse access. The API only resolves paths; the worker and
  the engine read the files.
- Hard links are **not** detected: a hard link inside an organisation's directory to another file looks like any file.
  Keep the root on storage that untrusted users cannot write to.
- After the engine runs, the worker hashes the file again and fails the row (`vcf_changed_during_run`) if it differs from
  the hash taken before the run. This detects a change that is still in place; it cannot prove which bytes the engine read.

## Single-host limitation

The processing claim (`SubmissionStore.claim`) is an atomic update in a **SQLite** file. It prevents duplicate execution
only for workers on **one host** using that one file. Do not put the store on a network filesystem or run workers on
several hosts against copies of it. (The database's unique key on VCF hash plus sample still stops a second report.)

## Auditing the queue before deploying this policy

Rows queued under the old rules may hold paths outside this layout; the worker will fail them. Find them first, without
changing anything:

1. **Work on a copy** of the SQLite store whenever you can (copy while the worker is idle, with any `-wal`/`-shm` files).
2. Set `GEPER_VCF_ROOT` as it will be in production, then run:

   ```
   python scripts/audit_submission_paths.py /path/to/copy-of/.submissions.db
   ```

   It opens the file **read-only** (SQLite `mode=ro`), looks at rows in `queued`, `running` and `interrupted`, and judges
   each with the same `resolve_vcf_path` the API and worker use. It prints the submission id, status, organisation and
   the refusal code per offending row, plus a summary, and exits 1 if any row would be refused (2 if it could not run).
   It never writes, retries, deletes or migrates anything, and prints no VCF path or file content.
3. For each row it lists, an operator decides: place the file under the organisation's directory and resubmit, or let it
   fail and handle it from the worklist. Changing rows is a separate, deliberate step outside this tool.

Run it only on the store you intend to inspect.
