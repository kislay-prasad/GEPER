# Contributing

## Pre-commit hooks

This repo uses [pre-commit](https://pre-commit.com/) to run a few checks
before each commit:

- **ruff** -- lint (`ruff check --fix`) and format (`ruff format`), repo-wide
  on whatever files are in the commit (config: [ruff.toml](ruff.toml)).
- **mypy** -- type-checks a deliberately narrow scope: the ACMG
  interpretation path (`geper/pipeline/acmg_rules.py`,
  `interpretation.py`, `interpretation_result.py`, `stage_schemas.py`,
  `pvs1/utils.py`, `interpro/lookup.py`). This is the exact code path
  the PM1 bug (a `None` flowing where a residue number was expected)
  lived in -- the goal is catching that class of bug at commit time, not
  blanket type coverage across a codebase that leans on dynamic
  provider dicts (ClinVar/gnomAD/ClinGen API responses) by design
  elsewhere. See [mypy.ini](mypy.ini) for the full scope and reasoning.
  Only runs when a commit actually touches one of those files.
- **gitleaks** -- scans staged changes for hardcoded credentials/API
  keys before they're committed.

### Setup

```bash
pip install pre-commit
pre-commit install
```

That's a one-time setup per clone. After it, every `git commit` runs the
hooks above automatically against the files you're committing; a
failing hook blocks the commit until fixed (ruff auto-fixes what it
can, ruff-format rewrites the file in place, and both simply need to be
re-staged and re-committed).

To run all hooks against the whole repo on demand (not just staged
files) -- useful right after first installing, or before a large PR:

```bash
pre-commit run --all-files
```

To run one hook by id (e.g. just mypy, while iterating on the
interpretation path):

```bash
pre-commit run mypy --all-files
```

### Extending mypy's scope

Add the new file's path to **both** `mypy.ini`'s `files =` list and the
`files:` regex in `.pre-commit-config.yaml`'s mypy hook -- they're
independent settings that both need to agree on the file set, or a
commit that only touches the new file won't trigger the check at all
even though `mypy.ini` itself would catch it if invoked directly.
