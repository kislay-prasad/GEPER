# Validation Baseline Record — `validation-baseline-2026-08-21` (470a993)

This document records what is in the frozen baseline tag
`validation-baseline-2026-08-21` (`470a993`), pushed by god ahead of this
record. It exists because the human's instruction for phase 2 was explicit:
*"Record what is in it: every commit from the remediation window with what
it changed and how it was verified. Anything measured from here on is
measured against this tag, not against a moving branch."*

**Measure against the tag, never against a moving `master`.**

## What this baseline does and does not establish

This is the same distinction the tag message states, restated here so this
document doesn't contradict it:

**ESTABLISHED — reporting integrity.** The pipeline no longer misstates what
it observed. A gnomAD lookup failure no longer returns the same value as a
confirmed absence. A local `OSError` (disk full, paging-file exhaustion) is
no longer relabelled as a network outage in five plugins. QC metrics reach
every renderer and a signed re-render can no longer silently drop them.
Consent and review-status each have exactly one source of truth instead of
two that could disagree. Run-level caveats reach all five consumers (both
PDFs, Markdown, JSON, LIMS export). A mis-keyed ClinVar config announces
itself instead of silently falling through to the live API.

**NOT ESTABLISHED — analytical or clinical validation.** No concordance
study against ground truth. No inter-run reproducibility measurement. No
external QA panel. This is **absent, not partial** — nothing in the window
below measures whether GEPER's classifications are *correct*, only whether
the system honestly represents what it did.

"Reporting integrity" is *the system says what it did*. "Analytical and
clinical validation" is *what it did is correct, and here is the evidence
measured against a reference*. This tag closes the first and opens the
second.

## Window boundary — evidence-based, not chosen

**Window: `51558c1^..470a993`, 49 commits, inclusive of `51558c1`.**

The line was not picked for convenience: `51558c1` ("Retire OMIM
integration from kim_pipeline") lands **2026-08-20 15:21:52 +0530**. The
commit immediately before it, `c1e0949`, lands **2026-08-17 15:51** — a
three-day gap with nothing in between. That gap is what defines the start
of the remediation window; everything from `51558c1` through `470a993`
(2026-08-21 16:57:59 +0530) is inside it.

## How to read the "How verified" column

Four tiers, used consistently below. Where a commit's message does not
state one and no board record supplies it, this document says **UNCLEAR**
rather than inferring a tier the evidence doesn't support — an honest
unknown is worth more here than a confident guess, because every future
measurement is anchored to this record.

- **PERSISTED TEST** — a named test (file, and count where stated) that
  would go red without the change, and is re-runnable from the repo today.
- **MANUAL VERIFICATION** — a real check performed once (a real run, a
  rendered/extracted artefact, a re-run tool invocation), not re-runnable
  from the repo as an automated check. This is a real and acceptable
  category on its own terms — it is just not the same guarantee as a
  persisted test, and this document does not blur the two.
- **DOCUMENTATION ONLY** — no functional/behavioural change; verified (where
  stated) by confirming the change is comment/docstring-only (`ast.parse`,
  or "every changed line is a comment").
- **UNCLEAR** — no verification tier is statable from the commit message,
  the diff, or the board record for this commit.

A recurring shape in this window is worth naming once, up front, rather
than repeating 7 times below: several commits shipped on **MANUAL
VERIFICATION at commit time**, with the **PERSISTED TEST landing 1–2
commits later** once someone (usually Pam) wrote the regression coverage.
Where that happened, this table states both halves and cross-references the
commit that added the test, rather than crediting the original commit with
protection it did not yet have when it shipped.

## Provenance caveat: this document does not use git's authorship fields, and neither should a reader auditing it

This is a limit on the document itself, not a footnote. The tables below
name agents throughout — Andy reproduced this, Pam added that test, Kelly
found this defect. A reader will reasonably assume git's own authorship
metadata backs those attributions. **It does not, and cannot, on this
floor**, and this applies to the whole project, not to this window
specifically.

**The fact, checked directly rather than assumed:** every one of the 49
commits in this window has `kislay-prasad <kislay30@gmail.com>` as *both*
its git `author` and `committer` field, with zero exceptions
(`git log --format="%an|%cn" 51558c1^..470a993 | sort -u` returns exactly
one line). Git's own authorship mechanism carries no per-agent signal at
all here, because god runs `git commit` for other agents' work as standing
practice on this floor. Authorship of a change and authorship of its
commit is a real and recurring distinction here — git records the second,
and reasoning from the first, using git, is not possible.

**Where this document's own attributions actually come from:** commit
*message content* — narrative prose ("Pam extended...", "Andy reproduced
and fixed...") and `Co-Authored-By:` trailers — plus, in a handful of
places, `hive/tasks.json` board-card notes cross-checked against the
message. **None of the agent names anywhere in the 49 tables below rest on
a git authorship field.** Stating this plainly rather than leaving it
implicit, per this document's own standing rule from the Findings section:
an absent caveat and a checked-and-clean caveat look identical on the page
and mean different things.

That said, message content is not immune to the same underlying problem in
principle — a `Co-Authored-By` trailer or a narrative sentence reflects
what the person who wrote the message chose to record, not an
independently verified fact. This document's attributions are only as
reliable as the message or board record they were read from, which is a
real limit, just a different and generally more trustworthy one than "who
ran the command."

**That is not a hypothetical caution — it is demonstrable in this exact
window.** `Co-Authored-By:` trailers do carry real per-agent signal (Andy,
Angela, Kelly, Pam, Ryan and Michael all appear), but the identity
namespace behind them is **not normalised**, checked directly
(`git log --format="%b" 51558c1^..470a993 | grep -i "Co-Authored-By" |
sort -u`):

- Kelly appears as both `Kelly <kelly-msydnnmj@hive>` and
  `Kelly <kelly@GEPER>`.
- Pam appears as both `Pam <pam-msydeubk@hive>` and `Pam <pam@GEPER>`.
- Ryan appears as both `Ryan <ryan-msydr4vx@hive>` and
  `Ryan (Documentation & Product Readiness) <ryan@hive.local>`.
- Michael appears once, as `Michael (god) <god@hive.local>`.
- Three model identities also appear as trailers in their own right —
  Claude Opus 5, Claude Sonnet 5, Claude Haiku 4.5 — a separate axis from
  agent identity (which model produced a commit, not which agent directed
  it), not elaborated on further here.
- **Meredith appears in zero commits in this window**, trailer or
  otherwise (checked: zero case-insensitive hits for "meredith" in any of
  the 49 commit bodies) — and this is not the abstract case it might look
  like. She is the author of `871f23d` (the ClinGen Dosage Sensitivity
  wiring in Group 1 above, and the same commit this document's Findings
  section flags as the one with zero verification anywhere in the
  window). Board card `clingen-dosage-wiring` is assigned to her, closed
  `done`; her own outbox record (`clingen-wiring-done.json`, 2026-08-20
  12:39, and `clingen-resume-mechanism-check.json`, 14:41) describes the
  work in full — the license re-verification, the design decision not to
  import geper/'s own ClinGen client (a package-name collision between
  the two subprojects' `pipeline.*` trees), the smoke-test validation
  performed, and her own closing line, **"Ready for Pam to add tests."**
  `871f23d` landed at 15:24, 45 minutes after her second message. She
  does not appear anywhere in it — not as a trailer, not in prose. The
  one commit in this window that most needed a recoverable owner is the
  one commit with no owner recoverable from git at all.

**A reader must not mechanically count commits-per-agent from these
trailers.** Doing so would double-count Kelly, Pam and Ryan under two
identities each, and report zero for Meredith. This is not a caution about
what could theoretically happen — the duplicate identities and the
absence are already present and greppable, in this window, today.

**What a reader should use instead, if provenance genuinely matters for a
decision:** `hive/tasks.json` — it records who did the work per card,
independent of who happened to run `git commit` for it. This is not a
theoretical recommendation: the board has already been the better
provenance source than git history three times in the course of producing
this document, and the three instances are not all the same kind of
better. First, `e2e-retry-ensembl-recovered`'s card supplied a real,
page-and-verbatim-quote end-to-end verification for `fd75fe3` and
`01a2e2d` that neither commit's own message contains (§4 above) — the
board filled a gap git left empty. Second, this exact authorship confusion
was caught when Kelly corrected four commits god had inferred as "Kelly's"
from the code alone — two of which Kelly never ran `git commit` for, and
the other two of which were Pam's, a fact god's own notes already had
recorded correctly before the mis-inference happened — the board
corrected a wrong inference git's silence had invited. **Third, and
different in kind from the first two: `clingen-dosage-wiring` recovered
`871f23d`'s real author (Meredith, above) *and, in the same record,
confirmed that the verification this document already marked UNCLEAR
still does not exist* — her own closing note is "Ready for Pam to add
tests," a code-review-level check and a licence re-confirmation, not a
test and not a described behavioural verification.** A provenance source
that can name who did the work *and* independently confirm that nobody
checked it is doing more than filling a gap — it is corroborating a
finding this document already made on other grounds. This does not move
`871f23d`'s tier; it is additional evidence for the tier already assigned,
not new evidence toward a different one.

---

## 1. OMIM retirement and its consequences (4 commits)

| Commit | What changed | How verified |
|---|---|---|
| `51558c1` | Removes `pipeline/omim/` entirely from kim_pipeline (module, config block, config-validator rule, `omim_fallback` params/wiring), updates INSTALL.md/LICENSE_AUDIT.md. Licensing reason: OMIM's genemap2 API is research-use-only, conflicting with commercial deployment. | **PERSISTED TEST** (pre-existing suite, not new coverage) — message states "5 OMIM-touched test files still pass (85/85)". |
| `d500f32` | `pipeline/hotspot/lookup.py::is_in_hotspot()` returns `Optional[bool]` (`None`, not `False`) when no hotspot/domain source is loaded; `acmg/classifier.py::_pm1()` returns `STATUS_NOT_EVALUATED` on `None` rather than silently evaluating "not in hotspot". Closes a silent false-negative PM1 opened by OMIM's removal. | **PERSISTED TEST** — `test_phase2_data_sources.py::test_is_in_hotspot_none_when_no_data` (renamed from a `_false_` variant), `test_lookup_cache.py::test_is_in_hotspot_uses_no_external_call` tightened from `assertFalse` to `assertIsNone`. |
| `871f23d` | New module `kim_pipeline/pipeline/clingen/` (`ClinGenDosageLookup`, 324 lines) wired as a fallback for `is_lof_intolerant` (PVS1-adjacent evidence) once OMIM's fallback was removed. License: ClinGen CC0 1.0, independently re-verified for kim_pipeline's separate commercial deployment. | **UNCLEAR — genuinely unverified in this window.** No test file was added (diff touches only `lookup.py` + `__init__.py`); the commit message states no manual check; the board card (`clingen-dosage-wiring`) closes with **"Ready for Pam to add tests"**, and no later commit in this 49-commit window adds ClinGen test coverage (checked via full-text search of every commit message in the window — zero further "clingen" hits after this commit). **See "Findings" below — flagged, not folded into the table.** |
| `bd86103` | Repairs `test_fixes_6_to_11.py` + `test_phase1_symbolic_acmg_reporting.py`, which still imported `pipeline.omim.lookup` after `51558c1` deleted it — the committed kim_pipeline suite had been failing to *import* since `51558c1` landed, until this commit. | **PERSISTED TEST** (the repaired test files themselves) — the commit's own necessity (import-time `ModuleNotFoundError` on `origin/master` before it) is direct proof of the defect it closes; no explicit fresh pass count is stated in the message itself. Also unblocked by `42624db` immediately prior (see Group 6) — this was "the previously-blocked commit" that `42624db`'s board card refers to as going from 38 lint errors to 2 and passing on the first attempt. |

## 2. Security (1 commit)

| Commit | What changed | How verified |
|---|---|---|
| `1fa8c56` | Pins DNABERT-2's `from_pretrained()` revision to a reviewed commit hash (`zhihan1996/DNABERT-2-117M@7bce263...`); `trust_remote_code=True` is kept deliberately (needed to load custom classes — the revision pin is the actual RCE mitigation, per HuggingFace's own documented practice). | **PERSISTED TEST** — new `test_dnabert2_pin_regression.py`, asserting the pin + `trust_remote_code` behaviour. Message also states "full suite still passes (878p/17f/83s/37e)". |

## 3. Documentation accuracy pass, post-OMIM audit (1 commit)

| Commit | What changed | How verified |
|---|---|---|
| `a1d31e5` | Corrects 9+ stale DNABERT-2 references in `geper/README.md`, adds a kim_pipeline scope note + a missing TensorFlow row to `LICENSE_AUDIT.md`, removes false "Disabled by default" claims from 5 plugin docstrings, removes a false BP7/SpliceFormer integration-gap claim from `acmg_rules.py`. | **DOCUMENTATION ONLY** — message states "docstrings/documentation only. No production logic modified. Code verified to parse cleanly (`ast.parse`)." |

## 4. BP7 disclosure and the PDF Conflicting-Evidence defect (4 commits)

| Commit | What changed | How verified |
|---|---|---|
| `fd75fe3` | Adds explicit disclosure (model name, calibration status, clinical-validation status) to every BP7 evidence block that SpliceFormer/SpliceBERT contribute to, in both Markdown and PDF. Policy: these models are enabled but uncalibrated, and a signing pathologist needs to see that, not have it hidden. | **PERSISTED TEST + MANUAL VERIFICATION.** The 3 tests covering this shipped *untracked* (see `35a9c80` below — they existed but weren't in the repo until the next commit). Separately, real end-to-end confirmation: Andy's authorised single-variant run (venv Python 3.12.10, HEAD `3062a87`, `main.py --vcf test_data/conflict_tiers.vcf --max-variants 1`, 2026-08-21 ~21:45, board card `e2e-retry-ensembl-recovered`) extracted the actual rendered PDF text with `pypdf` and confirmed the exact disclosure sentence — **"uncalibrated; not clinically validated"** — renders verbatim on page 4 of a real generated PDF, not just in source. |
| `01a2e2d` | Adds the Conflicting Evidence section to the full PDF renderer (`summary.py`), which had been absent since project inception (2026-07-30) — present in Markdown/JSON the whole time, never called from the PDF path. Blast radius: every PDF generated before this commit omitted conflicting evidence for all criteria, not just BP7. | **MANUAL VERIFICATION only — no persisted test found in this window** (diff touches only `summary.py`, 20 insertions, no test file). Verified by the same real E2E run named above: the extracted PDF's page 4, under "Conflicting Evidence:", shows the real MMSplice/SpliceFormer conflict text for the fixture's one eligible variant — confirmed rendering correctly on an independent fixture run, per the board record, not merely present in source. |
| `35a9c80` | Tracks the 3 tests covering `fd75fe3`'s disclosure work into the repo — they had been written but never `git add`ed, so the disclosure shipped one commit earlier with zero persisted coverage. | **PERSISTED TEST** (this commit *is* the tracking of that coverage) — "No change to production code — this only tracks the existing test file." This is the "test landed 1 commit later" pattern named above. |
| `0d3abd6` | Flips `CONFIG.splicing.ENABLE_SPLICEBERT` default `true` → `false`: SpliceBERT reliably times out loading against `transformers` 5.13.1, so shipping it enabled ships a stage that reliably fails (silent absence, not silent wrong-evidence — a distinct risk from `fd75fe3`'s disclosure fix). | **PERSISTED TEST** — new regression test in `test_splicebert_plugin.py` asserting the default, deliberately not mocking `CONFIG` so it exercises the real object graph rather than passing tautologically. |

## 5. Build/setup documentation (5 commits)

| Commit | What changed | How verified |
|---|---|---|
| `1e525ed` | README §5 documents 3 manual install steps a bare `pip install -r requirements.txt` misses (torch/torchvision/torchaudio pairing, evo2's CUDA-matched install, tensorflow-before-mmsplice ordering). `DATA_PROVENANCE.md` separates a correction (local was always test/dev; every real E2E run happened on Colab) from a genuine gap (requirements.txt never proven to build on any machine). `LICENSE_AUDIT.md`'s SpliceBERT row notes the `0d3abd6` default-off as a stability gate, not a licensing concern. | **DOCUMENTATION ONLY** — message states "Documentation only. No production logic modified." |
| `48841cf` | Narrows the requirements.txt gap claim: walking every import site found exactly 2 hard blockers at module-load (a corrupted local `transformers`, `biopython`) and zero packages needed-but-undeclared — the file's own declarations were accurate everywhere checked. Reframes the gap from "deficient file" to "declared-but-never-proven-to-build". | **DOCUMENTATION ONLY** — precision correction to the previous commit's claim, no functional change. |
| `e6aa0ce` | Fixes `UnicodeDecodeError` in `test_torchaudio_pin_regression.py`'s own two `open()` calls (reading `requirements.txt`/`verify_environment.py`), which relied on the platform default encoding — `cp1252` on Windows, which chokes on non-ASCII content in those files. | **PERSISTED TEST**, and independently re-confirmed by this agent today (2026-08-21, unrelated to this record's writing): `pytest geper/tests/test_torchaudio_pin_regression.py` → **5 passed** on the current tree. |
| `c8c3120` | Tracks `diagnostics/profile_esm2_init.py`, a hand-written profiling script from 2026-08-17 that had sat untracked. | **DOCUMENTATION ONLY / administrative** — no behaviour to verify; adds a file to version control, changes nothing at runtime. |
| `c753a77` | Records that a clean isolated venv (Python 3.12.10, 82 packages) built successfully from `requirements.txt` for the first time, resolving with zero conflicts and a clean `GeperPipeline` import. Updates `DATA_PROVENANCE.md`'s point 2 from "declared-but-never-proven" to "proven to build (minus evo2)", explicitly *not* claiming end-to-end correctness. Adds `geper/VENV_BUILD_RECIPE.md` with the full recipe and package-freeze list. | **MANUAL VERIFICATION** — the commit itself is documentation, but it records a real, one-off, non-repeatable-from-the-repo build event (Andy's venv build), not a persisted/automated check. The document is explicit that "proven to build" is not "proven to run end-to-end" — that distinction is preserved here, not blurred. |

## 6. Tooling & test-infrastructure fixes (2 commits)

| Commit | What changed | How verified |
|---|---|---|
| `42624db` | Adds the missing `[tool.ruff.lint] select = [E4,E7,E9,F]` to `kim_pipeline/pyproject.toml`, mirroring root `ruff.toml`. Without it, ruff resolved kim_pipeline/'s *nearest* config (its own, with no `select`) and linted the whole subtree against a far broader, unintended rule set, hard-blocking commits on unfixable pre-existing violations. | **MANUAL VERIFICATION** — board card `precommit-ruff-double-commit` states the effect was "verified immediately: the previously-blocked commit went from 38 errors to 2, and after those the hook PASSED ON THE FIRST ATTEMPT" (that previously-blocked commit is `bd86103`, Group 1 above). Not a pytest-level check; a real hook re-run with a stated before/after count. |
| `5da05d5` | `test_fixes_6_to_11.py`'s autouse fixture created `Path("/tmp/geper_uploads")` with `mkdir(exist_ok=True)`, no `parents=True`. On Windows a leading slash means current-drive-root, so the real target was `C:\tmp\geper_uploads`, and `C:\tmp` itself doesn't exist on a clean box — every test in the file errored (37 setup errors) before running. | **PERSISTED TEST effect**, with explicit before/after counts in the message: "Before: 37 setup errors. After: 22 passed, 15 failed" (the 15 are a single unrelated, pre-existing cause — `fastapi` absent from this environment — named and cross-referenced to a separate tracking card, not this fix). Message also states the fix was "Verified from a genuinely clean state" and that the original error was re-triggered once first, to confirm the reproduction was real before fixing it. |

## 7. Enformer/Borzoi `transformers`-pin self-corruption and its fix (6 commits)

| Commit | What changed | How verified |
|---|---|---|
| `47748b6` | `ensure_pip_package_available` ran a bare `pip install <pkg>` with no `--constraint`; installing enformer-pytorch (pins `transformers==4.56.2` exactly) or borzoi-pytorch (`<5.0.0`) silently downgraded an already-installed `transformers` 5.x mid-process, which — because `transformers` loads submodules lazily — broke HyenaDNA and ESM2 *later in the same run*, swallowed by graceful degradation. Fix: pass pip its own `--constraint requirements.txt`, so pip refuses the conflicting install outright instead of resolving it by moving a core pin. Also corrects requirements.txt's stale "both default off" header comment (code has always defaulted them on). | **PERSISTED TEST** — new `tests/test_auto_install.py` covers all three paths offline (constraint always present; a conflicting package returns `False` after exactly one pip call; an already-importable package never invokes pip). Message also names a **known, deliberately-not-papered-over consequence**: `test_enformer_plugin.py::test_available_when_flag_on_and_package_installed` now correctly fails on any box without the package pre-installed (see `8fe4682` immediately below). |
| `8fe4682` | Both `test_available_when_flag_on_and_package_installed` tests (enformer + borzoi) called the real, unmocked `is_available()`, which — before `47748b6` — reached the real auto-install and could corrupt the environment running the test suite (confirmed: one agent's `transformers` went 5.15.1 → 4.57.6 from a test run alone). Both now mock `ensure_pip_package_available`, matching the convention already used elsewhere in both files. | **PERSISTED TEST** — message states "48 tests pass across both files." |
| `8d3c7d9` | Corrects the Borzoi/Enformer `all_tied_weights_keys` shim comments: `c1e0949`'s description was accurate *for the borzoi-pytorch version installed on 2026-08-17* (≤0.4.4, never calls `post_init()`) but stale by the time of this commit (0.5.0+, released 2026-06-10, does call it) — the shim is a no-op on current installs but stays load-bearing because the installer never upgrades an already-satisfied import. Enformer's shim is unaffected — it never calls `post_init()` at all, by design upstream. | **DOCUMENTATION ONLY** — message states "Comments only — verified every changed line in both plugins is a comment, and both files still parse. Behaviour is untouched." |
| `3062a87` | Hardens both shims: `all_tied_weights_keys` is copied onto the *instance* after model construction rather than left only on the class, closing a latent shared-mutable-state risk if either model ever ties weights (neither does today, so nothing was actually broken). | **PERSISTED TEST** — "48 passed across `test_enformer_plugin.py` and `test_borzoi_plugin.py` in the py3.12 environment." Message explicitly names and explains an environment difference rather than hiding it: the same run in the shared 3.14 environment shows "38 passed and 10 pre-existing errors, all `ModuleNotFoundError: enformer_pytorch`... an environment difference, not a regression." |
| `ee39c09` | *(This agent's commit.)* Documents, in both plugin files, that enformer-pytorch/borzoi-pytorch cannot be auto-installed in an environment holding GEPER's own correct `transformers` core pin — **by design**, since `47748b6`'s constraint refuses on a verified-real metadata conflict (`enformer_pytorch 0.8.12 → transformers[torch]==4.56.2`; `borzoi_pytorch 0.5.1 → transformers<5.0.0,>=4.57.6`, each confirmed independently against installed wheel `METADATA`, not assumed symmetric). States the manual command (`pip install --no-deps <pkg>`) and corrects the two `RuntimeError` message strings, which previously told an operator to run a plain `pip install` that would silently reintroduce the exact downgrade `47748b6` closes. | **DOCUMENTATION ONLY** — comment/message-string changes only; the two `transformers` pin facts were verified by this agent by reading the installed wheels' `METADATA` files directly (not by pip, not assumed), see the commit message for both exact `Requires-Dist` lines. |
| `3c638a7` | `test_default_manager_uses_real_registry_and_never_crashes` deliberately builds a real `EnsembleManager` (no mocking, by design, per the test's own comment) — which reaches real `is_available()` calls and shells out to real `pip install` against whatever interpreter runs it. On a shared multi-agent interpreter with a demonstrated file-lock collision history, that's a live hazard, not the test's fault. Fix: register a `real_pip` marker, deselect it by default (`pytest -m real_pip` to opt in). States plainly, in the test's own docstring, what coverage the default suite gives up by doing this. | **MANUAL VERIFICATION** (of the marker/deselection mechanism, via a reproducible command, not a persisted assertion) — message states exact `pytest --collect-only` output confirming the test is deselected by default and selectable via the marker, and that the full-suite collection count is otherwise unchanged (`1950/1951, 1 deselected, no errors`). |

## 8. Health probe: retry-before-latch and observability (4 commits)

| Commit | What changed | How verified |
|---|---|---|
| `5094620` | A single failed startup probe could latch an external service offline for an entire run, which also switches off each client's own retry logic (`note_skip()` is called *instead of* retrying once `is_offline()` is true) — so one false negative, not a real outage, silently degraded every downstream lookup for the rest of the run. Fix: 3 probe attempts with backoff; the final, latch-deciding attempt gets a 30s timeout (derived from clients' own `REQUEST_TIMEOUT_SECS`, not guessed — an earlier 8.0s proposal was considered and withdrawn as the same kind of guess). Real motivating case: a genuine, successful Ensembl probe took 21,931 ms on this machine; the old 4.0s default would have false-latched that run. | **PERSISTED TEST**, explicitly red-then-green verified: "`tests/test_service_health.py` 31 passed; regression sweep across the 8 suites touching `service_health` 204 passed / 23 subtests; full suite 1848 passed / 1 pre-existing unrelated failure... The new tests were confirmed to FAIL against the unfixed code first, so the green result is meaningful." |
| `5702f31` | Adds README §26 documenting the probe/retry/latch mechanism, which had zero prior README coverage — leads with "the latch was not weakened," records the 30s derivation and the withdrawn 8.0s proposal, and documents the full `GEPER_HEALTH_CHECK_*` env-var family (2 pre-existing vars had also never been documented). | **DOCUMENTATION ONLY.** |
| `6a52384` | Pure instrumentation: records a failed probe's latency (previously discarded — only the `HEALTHY` branch reported it), and adds `measure_latched_services()` (called from `main.py`'s `finally`), which re-probes each still-latched service once at run end and *logs* whether it was still down, changing nothing. Exists to produce the evidence a future bounded-reprobe feature would need to size itself — that feature is explicitly not built here. | **MANUAL VERIFICATION at commit time** — message describes verifying the observation-only contract by comparing every observable field before/after across three paths (still-down, recovery, probe-raises), but this was **not yet a persisted test** — the very next commit (`0fd2ab3`) says so explicitly: "that guarantee was carried by a docstring, with no test behind it." |
| `0fd2ab3` | Adds the persisted test `0fd2ab3` describes as missing from `6a52384`: `TestMeasureLatchedServicesIsObservationOnly` (2 tests), verified to actually discriminate by running them against a deliberately-buggy version that writes state back on recovery — the recovery-path test correctly fails against it. | **PERSISTED TEST** — "33 passed in this file," discrimination explicitly demonstrated rather than assumed. Closes the gap `6a52384` shipped with. |

## 9. Report governance and test hygiene (2 commits)

| Commit | What changed | How verified |
|---|---|---|
| `0fdf874` | Both PDF renderers decided "has this been reviewed?" from the free-text `patient["physician"]` field rather than the actual sign-off record — so any caller supplying a physician string suppressed the DRAFT warning and printed "has been reviewed by X" with no sign-off ever performed. Now derives review state from `document["review_status"]` alone (same source Markdown/`export_lims` already used), with three outcomes (reviewed / overridden / everything-else-renders-DRAFT as the fail-safe). Also resolves a previously-documented "known, deliberately unresolved tension": an approve-then-override run used to show "reviewed by X" on PDF while JSON/Markdown said "overridden". | **PERSISTED TEST** — "`test_review_status_governance` + `test_signoff` + `test_icmr_footer`: 68 passed, 0 failed," eight assertions rewritten because they had encoded the old (defective) behaviour as a requirement. The `review/signoff.py` half of this change is docstring-only, separately verified "by comparing docstring-stripped ASTs." |
| `89dc9c8` | Two tests asserted "no model plugins available" — but only because the local HuggingFace cache happened to be corrupt (each snapshot file held a path string instead of blob content). Once the cache was repaired, the assertions went red — not a regression, the environment stopped being broken. Rewrites both to pin what the plugin manager actually *guarantees* (list is a subset of known keys, `basis` agrees with emptiness, never crashes) rather than a specific observed plugin combination. | **PERSISTED TEST** — "`test_ensemble_manager` + `test_new_plugins_integration`: 19 passed. Full suite: 1899 passed, 6 skipped, 403 subtests, 1 failed" (the 1 failure is the pre-existing, unrelated SpliceFormer finding, later closed in Group 13's `21f35e1`). Message also states the design was validated against real machine variance: plugin availability moved through 3 distinct states in one night and both rewritten tests held across all of them. |

## 10. A4 clinical-standing unification and run-level caveat parity (6 commits)

| Commit | What changed | How verified |
|---|---|---|
| `62e2b2b` | The full PDF's closing page asserted "intended for clinical use" while every other renderer (per-variant findings, Markdown, the API description) asserted "research pipeline" — the same document made opposite claims. Decision: research-pipeline wording is correct (supported by README §15, `clinical_report_builder.py`, the API description, and the project's own CC BY-NC-SA 4.0 "non-commercial research use only" license); the clinical-use text was a lone orphan, possibly borrowed US LDT/CLIA template language, inconsistent with GEPER's actual ICMR/DPDP Act 2023 regulatory context. Promotes a single `RESEARCH_USE_DISCLAIMER` constant that every renderer now imports rather than forking its own copy. | **PERSISTED TEST** — "10 test cases covering consistency across renderers, presence in all surfaces, and single-source property (would fail if someone re-forked). Verified pre-fix RED and post-fix GREEN against 1909-test suite baseline" (Pam). Also flags, as a separately-tracked finding rather than folding it in: the zero-variant JSON asymmetry addressed next. |
| `428a77d` | Adds a document-level `caveats` field to `geper_results.json`, carrying `RESEARCH_USE_DISCLAIMER`, built unconditionally so a zero-variant run still states what qualifies it. Closes the asymmetry `62e2b2b` flagged (JSON had no run-level disclaimer mechanism at all; only PDF/Markdown did). | **PERSISTED TEST**, landing in the very next commit — see `8d8299d`. Not yet tested at the point this commit itself lands (scope stated as "`json_builder.py` only"). |
| `8d8299d` | Adds the 4 tests covering `428a77d`: `TestJsonRunLevelCaveatsBlock` in `test_disclaimer_consistency.py`. Confirms the field appears for both a typical run and a genuinely zero-variant run, carries the exact shared constant object (identity check, not just equality), and that per-variant `clinical_report["limitations"]` is untouched. | **PERSISTED TEST** — pre-fix RED explicitly verified (production file stashed, all 3 regression tests correctly failed, the scope-boundary test correctly passed in both states), restored byte-exact, re-verified GREEN. Full suite: "1913 passed, 6 skipped, 403 subtests, 1 failed" (pre-existing, unrelated). |
| `f519f72` | Closes the remaining caveat-parity gaps: Markdown gains the ACMG methodology statement and a "Reviewer Attention" section (per-finding flags + offline-data-sources caveat, always rendered even when empty, plus a DPDP Act 2023 consent line omitted when no consent object exists); the full PDF gains a per-finding Recommendations block it previously lacked while Markdown had it. Three shared helpers relocate from `summary.py` to `clinical_report_builder.py` so neither renderer is the de facto shared library for content every renderer needs — the same class of problem `RESEARCH_USE_DISCLAIMER`'s consolidation fixed, in function form. Explicitly documents two things left out on purpose (evidence-completeness caption — already in Markdown; QC not-applicable reason — no home in Markdown, a structural difference not a parity defect) and one known narrowing (Markdown's Reviewer Attention omits unpinned-version data sources, left as a follow-up). | **PERSISTED TEST** — message states tests "pin observable output rather than implementation: that Markdown carries each caveat, that the PDF carries recommendations and omits the block when there are none, that renderers read one source instead of holding copies, and that the skipped items stay skipped." No specific file name or pass count is given in the message itself — the tier is clear (a dedicated persisted test exists) but the exact coverage accounting is thinner here than in most of this window's other commits. Noted rather than smoothed over. |
| `457dd5b` | The LIMS export never read the new `caveats` block — an integration built against `LIMSExport` rather than the raw JSON was blind to every run-level caveat. Adds `caveats` to `LIMSRun` (JSON) and a `run_caveats` column (CSV, semicolon-joined, repeated per row, appended last so no existing column shifts). Defaults to `[]` for a document written before the field existed. | **MANUAL VERIFICATION at commit time** — message states "Verified by building one document through the real builders and reading both written files back," a real one-off check, not a persisted test at the point this commit lands. |
| `d18f959` | Adds the persisted coverage `457dd5b` shipped without: asserts the JSON export carries `run.caveats`, the CSV column is present and identical on every row, and a document written before the field existed still exports cleanly rather than raising. Message states plainly: "The feature shipped without its tests tracked, which left a gap of the same kind that work exists to close." | **PERSISTED TEST** — closes the gap named in `457dd5b`. |

## 11. QC and consent: single source of truth (3 commits)

| Commit | What changed | How verified |
|---|---|---|
| `2b19ca0` | QC metrics reached the PDF renderers only as a caller-supplied argument, never stored on the document itself — so anything re-rendering later from the stored `geper_results.json` (notably `review/signoff.py`'s `approve()`/`override()`, run in a separate process, potentially days later) had no way to know the original run's QC. Now the orchestrator parses QC once and stores it on the document; this commit changes no rendering behaviour on its own — it is the prerequisite for `ae0d9f7`. | **PERSISTED TEST**, added two commits later — `test_qc_metrics_durability.py`, per `2cc57a6`'s message: "Carries... her QC durability tests covering the two commits before this one" (i.e., this commit and `ae0d9f7`). Not yet tested at the point `2b19ca0` itself lands. |
| `ae0d9f7` | `approve()`/`override()` re-render both PDFs from the stored document but were never passed `qc_metrics` — and until `2b19ca0`, the document didn't carry it either — so both fell into the "no QC" branch, which asserts (not merely omits) that no run-level QC exists. For any run that *did* supply QC, the signed PDF — the artefact that actually reaches a clinician — stated the pipeline never observed sequencing at all. The sign-off step itself introduced the false claim. Now reads QC back off the document. | **MANUAL VERIFICATION at commit time** — message states "Verified end to end: a real `approve()` against a document carrying QC produces a signed PDF showing the values... Control checked too... a run that genuinely had no QC still prints the no-QC text honestly." Persisted coverage lands with `2b19ca0` in `2cc57a6`, as above. |
| `2cc57a6` | Both PDF renderers took DPDP Act 2023 consent from whatever `--patient-meta` they were handed, while Markdown read `document["patient_consent"]` — two independent inputs answering one question, caught disagreeing by a render-and-diff of a single run. The document now wins (the unified shape every other field already flows through). States the real-world consequence plainly: a `--patient-meta` consent object no longer reaches a PDF on its own; a real pipeline run is unaffected since `orchestrator.py` already populates the document field from the same source. | **PERSISTED TEST** — carries "Pam's consent fixture fixes, and her QC durability tests covering the two commits before this one" (`2b19ca0`, `ae0d9f7`) into this commit. No standalone pass count is given for the consent-specific assertions in this message; the QC-durability coverage for the two prior commits is explicit. |

## 12. LIMS export design boundaries (1 commit)

| Commit | What changed | How verified |
|---|---|---|
| `13c458e` | *(This agent's commit.)* Documents, in `geper/LIMS_EXPORT_MAPPING.md`, that QC metrics, patient consent, and per-variant `clinical_report` limitations are intentionally out of scope for the LIMS export — a human-ruled design boundary given the export's deliberate minimalism against an unknown target spec, not a defect. States the boundary as "out of scope by design, pending a real target spec," not "considered and rejected." Preserves two nuances: per-variant limitations is the mildest gap, since the qualification it would restate already reaches LIMS at run level via `run.caveats`; patient consent carries a distinct DPDP Act 2023 regulatory caveat. Also documents the `run.caveats`/`run_caveats` field (shipped in `457dd5b`, never previously documented). | **DOCUMENTATION ONLY** — no schema/model change; a one-line pointer added to `report/export_lims.py`'s module docstring rather than duplicating the rationale there. |

## 13. Evidence state-collapse fixes (9 commits)

*One recurring failure shape across this whole group: a lookup failure, a local resource error, or a mis-keyed config option produced a value indistinguishable from a different, real, confirmed state — "not evaluated" reading as "confirmed absent," a local disk error reading as "network down," a typo'd config key reading as "using the intended source." Each fix below makes the failure state observably distinct again.*

| Commit | What changed | How verified |
|---|---|---|
| `41935a2` | A bare `OSError` in `_NETWORK_ERROR_TYPES` caught both genuine network failures *and* local resource failures (paging-file exhaustion, disk-full, permission-denied) across 5 plugins (enformer, borzoi, spliceformer, splicebert, spip) — a local failure silently relabelled as a connectivity problem in results/provenance. Andy reproduced Windows error 1455 (paging-file exhaustion) loading ESM2 to confirm the mislabelling for real. Narrows each plugin's caught-exception tuple to the specific network exception types its own HTTP layer actually raises; promotes 4 sites from `debug` to `warning`. | **MANUAL VERIFICATION at commit time, self-graded down by its own author.** Message states "All existing tests pass; zero regressions" — no *new* dedicated test. `470a993` (below) records this explicitly, quoting Angela's own readiness-audit grading: **"evidenced-by-manual-verification, not evidenced-by-persisted-test."** Persisted coverage lands 9 commits later in `470a993`. |
| `2331f44` | `gnomad_lkp.lookup()` exceptions previously set `gnomad_af_absent = False` — the same value used for "confirmed present in gnomAD." Three distinct real states (checked-and-present, service-unavailable, lookup-raised) collapsed into one the ACMG classifier couldn't tell apart. Both the exception path and the non-exception `UNAVAILABLE` path now return `None`, matching the pattern `d500f32` established for PM1/hotspot data. | **MANUAL VERIFICATION at commit time** — message states "All tests pass; zero regressions in evidence layer output," no new dedicated test named. Persisted coverage lands in `027b151` below. |
| `9f23882` | After QC became document-level (`2b19ca0`, `ae0d9f7`), individual renderers *still* took `qc_metrics` as a parameter — two sources of truth, and Markdown had zero reads of the document-level field at all, so Markdown never rendered QC even for runs that supplied it (the "A7" defect). Moves the QC constants/parsing into `clinical_report_builder.py`; all renderers and both sign-off re-render paths now read QC from the document. | **MANUAL VERIFICATION at commit time** — message describes rendered-output checks in both directions (with/without `qc_metrics`, extracted from actual output not emit sites) and states "322 passed, 12 subtests, 0 failed in report/review suites" (pre-existing suite, not a new dedicated A7 regression test). The dedicated persisted test lands in `21f35e1` below. |
| `027b151` | Adds the persisted coverage `2331f44` shipped without: `TestGnomadAbsentSentinelIsNoneNotFalseOnFailure` (4 tests, both exception and non-exception paths, all 4 confirmed to fail pre-fix) + a warning-level logging test. Also repairs `_simulate_gnomad_branch`, a hand-copied duplicate of the real dispatch logic in `test_fixes_6_to_11.py` that would have silently drifted from `shared.py` on any future production change — rewritten to drive the real `run_acmg_evidence_batch()`. | **PERSISTED TEST** — "9 tests pass post-fix," pre-fix RED explicitly verified via path-scoped stash. Kim_pipeline suite: 919 passed / 22 failed, all 22 pre-existing/environmental (the same 4-cause set this agent independently re-diagnosed earlier this session — see this agent's own memory for that investigation). |
| `21f35e1` | Adds the persisted coverage `9f23882` shipped without: `TestA7MarkdownRendersQcMetrics` (2 tests, both confirmed to fail pre-fix). Also fixes the standing "1 failed" SpliceFormer test — it had mocked enformer/borzoi's availability but left SpliceFormer's own `einops` check unmocked, so its "all three return None" assertion was only ever true by machine coincidence; rewritten to assert the manager's real guarantee. Also repairs a collateral regression in `test_indian_population_frequency.py`'s `_patch_threshold()` fixture, exposed by A7's QC-everywhere rendering reaching a bare `MagicMock`. | **PERSISTED TEST** — "geper suite: 1939 passed, 0 failed (first zero-failure run this session)." |
| `1007b99` | Ruff's F401 autofix stripped `_QC_METRICS_NOT_APPLICABLE_REASON`'s re-export during a commit hook (it's referenced only in comments, not code, in `summary.py`), breaking 3 tests on the pushed tree. Restores it with an explicit `noqa`. | **PERSISTED TEST** — "3 tests now pass; 31 passed / 6 subtests / 0 failed in the file" (`test_qc_metrics_rendering.py`), i.e. the breakage itself was caught by pre-existing persisted tests, and this commit's fix is verified by those same tests going green again. |
| `b2e819c` | `ClinVarLookup` reads `clinvar.tsv_gz_path`; a config supplying `tsv_path` instead (a real, legitimate key name — just for a *different* section, `gnomad_constraint`) had its setting silently discarded with no warning, falling through to the live NCBI API. Adds a warning naming the mismatch and the correct section, carefully scoped so it does not fire on `gnomad_constraint.tsv_path`, which is legitimate there. | **MANUAL VERIFICATION at commit time, explicitly self-described as such by the next commit.** Message lists 5 verified scenarios (no section / empty / `None` / all-canonical-keys / the cross-section guard) but names no test file. `9f3391a` (below) states outright: "`b2e819c` shipped on manual verification alone — the five silent-control cases were checked by hand and were correct, but none of it was re-runnable from the repo." |
| `9f3391a` | Adds the persisted coverage `b2e819c` shipped without: 12 tests, including the cross-section guard (confirmed to actually discriminate by running it against a deliberately over-broad throwaway validator) and `_CLINVAR_KNOWN_KEYS` pinned against what `lookup.py` actually reads via `inspect.getsource`. | **PERSISTED TEST** — "12 passed," discrimination independently verified two ways (via `git show b2e819c~1` confirming the validator didn't exist before, and via the deliberately-wrong-validator check). |
| `470a993` | Adds the persisted coverage `41935a2` shipped without, 9 commits later. `TestNoBareOSErrorInNetworkTuples` (10, parametrized ×5) is the actual regression pin, independently confirmed to have gone red against `41935a2~1` (all 5 tuples were literally `(OSError, ConnectionError, TimeoutError)` pre-fix). `TestOSErrorPropagationSurvivesRelabeling` (2) is a real but separate invariant that — Pam states explicitly, unprompted — *cannot* go red against the pre-fix tree, since the fake plugin it exercises has no `except` clause at all and behaves identically before and after. A proxy-pin using the real plugins (which would have routed through real `pip install` against the shared interpreter) was considered and explicitly refused at design time as "asserting on machine state, not the fix." | **PERSISTED TEST** — 12 passed, with an unusually precise accounting of what is and is not actually covered (10 regression / 2 invariant, stated as such rather than left as "12 green tests"). Message also states "12 passed in 0.44s — the sub-second runtime is itself evidence no pip shell-out occurred," i.e., a verification of the test's own hygiene, not just the fix. |

## 14. Process and observability (1 commit)

| Commit | What changed | How verified |
|---|---|---|
| `7fe3ee5` | `main()` now logs PID, parent PID, and start time as its first statement (before argument parsing), using `logger.info` rather than `print` specifically because `StreamHandler.emit()` flushes unconditionally and survives a hard kill where buffered `print` output would not. Local time, to share a clock domain with Windows Event Log. Both PID and parent PID are captured because a venv-launched `python.exe` on this box does not always stay one process. Exists to make a harness-killed background task's OS process attributable after the fact, which was previously impossible even with full harness-log access. | **MANUAL VERIFICATION, no persisted test in this window.** Message states it was "Verified against a real run rather than only in source" — `python main.py --help` was run for real and its logged line captured verbatim (`PID=56816 PARENT_PID=52640 START=...`), which also empirically confirmed the launcher/worker process split is real on this box, not merely theoretical. No test file is named, and no later commit in this window adds one. |

---

## Findings — where stated or expected verification does not fully hold

Per the dispatch's instruction, these are called out prominently rather than folded quietly into table cells:

1. **`871f23d` (ClinGen Dosage Sensitivity wiring) has no verification evidence anywhere in this 49-commit window.** A new 324-line production module was wired into a real ACMG evidence path (`is_lof_intolerant`, PVS1-adjacent) with zero test coverage added, no manual-verification statement in its own commit message, and the board's own closing note for that work explicitly reads "Ready for Pam to add tests" — work that a full-text search of every subsequent commit message in this window confirms never landed. This is not a case of a wrong claim; nothing in the commit claims verification it doesn't have. It is an honest gap this record should not paper over: this is the one production code path in the baseline whose correctness rests entirely on code review, not on any demonstrated or persisted check.

2. **The "manual-verify-now, persisted-test-later" pattern recurs at least 7 times in this window** (`fd75fe3`→`35a9c80`; `428a77d`→`8d8299d`; `457dd5b`→`d18f959`; `2b19ca0`/`ae0d9f7`→`2cc57a6`; `41935a2`→`470a993`; `9f23882`→`21f35e1`; `2331f44`→`027b151`; `b2e819c`→`9f3391a`). This is not a defect in any individual commit — each is honest about its own tier at the time it shipped, and in most cases the gap between manual verification and persisted-test landing is one to two commits (minutes to hours, not days). It is recorded here because it means **a commit's true verification tier is sometimes only established by reading 1–2 commits ahead of it**, not from that commit in isolation — exactly the kind of thing this record exists to make explicit rather than leave implicit in a chain of commit messages.

3. **No mismatch was found** between a commit's *stated* verification tier and what its diff actually does. Several commits (`3062a87`, `3c638a7`) go out of their way to name environment-dependent differences in their own test results rather than presenting a single misleadingly-clean number, which is the opposite failure mode from what this finding category is watching for.

## What this record is, and isn't

This is a record of **verification tier**, not a re-grading of correctness. A commit graded MANUAL VERIFICATION here was not necessarily wrong to ship that way, and several of the most careful, most-cross-checked verification work in this window (the real end-to-end PDF text-extraction runs behind `fd75fe3`/`01a2e2d`, the venv build behind `c753a77`) is manual precisely because it required a real GPU/network/environment that a persisted unit test cannot reproduce. The distinction this document exists to preserve is only ever: *is this claim re-runnable from the repo today, or does someone have to trust that it was checked once and take that on record.*

Both are legitimate. Only one is repeatable.
