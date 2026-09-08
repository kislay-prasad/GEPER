# Contributing

## Commit attribution -- read this before trusting `git log`

**Commits in this repository dated before 2026-09-08 are all authored
`kislay-prasad <kislay30@gmail.com>`, regardless of who or what actually wrote
them.** Much of that work was done by automated agents committing through the
repository owner's git identity. The log is accurate about *what changed* and
about *when*; before that date it is **not** evidence of *who* performed the
work.

**This is not retroactive and will not be made so.** Existing history is not
rewritten here, so the ambiguity above is permanent. Do not read a uniform
author column in the early history as a claim that one person wrote it all.

### How attribution works from 2026-09-08 onward

Each working tree sets its own **author** identity, while the **committer**
stays the repository owner:

```
git config --worktree agent.author "your-name <your-name@agents.hive>"
git ci -m "your message"          # instead of: git commit -m "your message"
```

- `git ci` is an alias defined in this repository's `.git/config`. It is a
  normal commit -- hooks run, nothing is bypassed -- with `--author` supplied
  from the current worktree's `agent.author`.
- If `agent.author` is not set in the worktree, **`git ci` refuses and prints
  the one-line fix**. That refusal is deliberate: a silent fall back to a plain
  commit would look exactly like attribution working while quietly not
  happening.
- Requires `extensions.worktreeConfig=true` (already set here). Per-worktree
  `user.name` is *not* the mechanism -- it sets author **and** committer, which
  would lose the committer of record.
- The alias reads `git config --worktree agent.author`, **scoped deliberately**.
  An unscoped read would walk worktree -> local -> global, so a repo-level or
  global `agent.author` would make an unconfigured worktree commit silently
  under someone else's name -- a false record of who performed the work, which
  is worse than an absent one. Scoping the read keeps identity per-worktree by
  construction, and keeps the refusal above working.
- `git commit` still works and is untouched. It produces the old, unattributed
  form.

### `git log` now has three eras, not two

Do not read "authored `kislay-prasad`" as "the human wrote it". After the
cutover there are **three** kinds of commit, and two of them look identical:

1. **Before 2026-09-08** -- authored `kislay-prasad`, whoever wrote it.
2. **After, from a worktree with an identity set** -- authored by that agent.
3. **After, from a worktree with no identity, or via plain `git commit`** --
   authored `kislay-prasad` again.

**(1) and (3) are indistinguishable by author alone.** Attribution being "live"
does not make the whole log readable; it makes *some* commits readable and
leaves the rest exactly as ambiguous as they were. A reader who knows only that
attribution was enabled will over-read the log precisely here.

### Checking who wrote something

`git log` shows the author by default, so a plain log conceals the split. To
see both roles:

```
git log --format='%h %an | %cn | %s'
```

Author is the agent (after the cutover); committer is the repository owner
throughout.

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

### Your test run does not bind to the committed tree -- re-verify after a hook rewrites a file

**A hook that fails on its first pass and rewrites the file means whatever
you tested a moment ago is not what you are about to commit.** `ruff
check --fix` and `ruff format` run *after* you've already run tests and
decided the change is correct, but *before* the commit is final -- and
their edits are never re-tested automatically. "I ran the tests, they
passed, then I committed" does not mean the tree that actually reached
`origin/master` still passes; it means the tree you tested did, and
that tree may not be the one git recorded.

This has bitten this project more than once with the same shape:
lint autofix silently stripping a deliberate re-export (flagged as an
unused import, `F401`, when it wasn't), and formatting rewrites large
enough that a reviewer skimming the diff could plausibly miss a
semantic change hiding among hundreds of mechanical ones. Two commits
in this history (`cfba75e`, `751b910`) hit the "hook rewrites the whole
file, not just the lines you touched" version of this directly -- ruff
reformatted files wholesale on their first commit under the hook,
because nothing had normalized them before.

**The fix is not to skip the hooks (`--no-verify` defeats the point of
having them) -- it's to re-verify after they run, not just before:**

1. Stage your change, run your tests, confirm they pass -- as normal.
2. `git commit`. If a hook rewrites a file, the commit is blocked (as
   documented above) and the working tree now differs from what you
   just tested.
3. **Before re-staging and re-committing, read the diff the hook
   produced** (`git diff`) and confirm it's mechanical (formatting,
   an import genuinely unused) rather than a change that could alter
   behavior. If it's non-trivial, re-run the tests against the
   post-hook tree before committing again -- don't assume a passing
   run from step 1 still applies.
4. Re-stage, commit again. It should now succeed with no further
   hook-reported changes, since the tree is already in the shape the
   hooks want.

The one thing this doesn't solve: there is no CI in this repository
(no `.github/workflows`) to catch a case where step 3 was skipped, so
this discipline is presently enforced by habit, not by tooling. A
`pre-commit run --files <changed files>` invoked manually *before* your
first `git commit` attempt (rather than discovering the mutation via a
blocked commit) surfaces the same rewrite earlier and is worth doing
for any change you're not prepared to re-verify twice.

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
