# Phase 1 Verification Report — Project Hardening / Dependency Management

Date of this pass: 2026-07-06. This report is written from inside the
sandbox that did the work: no GPU, no network access to PyPI/Hugging
Face/Ensembl/gnomAD (see "What could not be verified live" below). Every
claim below is either (a) something this sandbox actually executed and
you can re-run yourself (`verify_environment.py`, `pytest tests/`,
`benchmark_gnomad.py`), or (b) explicitly marked as unverified here.

## 1. Dependency audit

Audited: `torch`, `torchvision`, `torchaudio`, `transformers`,
`accelerate`, `tensorflow`, `multimolecule`, `mmsplice`, `triton`,
`flash-attn`, `cyvcf2`, `pysam`, `pyfaidx`, `biopython`.

**Finding: `requirements.txt` was already clean.** A duplicate/obsolete
scan (`grep -v '^#' requirements.txt | sed 's/[><=;].*//' | sort | uniq -c`)
found zero duplicate package entries and zero pins that needed removal.
This codebase's own `DEPENDENCY_MIGRATION_REPORT.md` documents a prior
hardening pass that already did this work; this audit re-confirms its
current state rather than repeating it from scratch.

**Finding: four of the audited packages are intentionally *not*
dependencies of this codebase, and should stay that way:**

| Package | Status | Why |
|---|---|---|
| `torchaudio` | Not used | Nothing in `models/`/`pipeline/` imports it. Not adding it — an unused pin is exactly the kind of dependency-surface bloat this hardening pass is supposed to remove, not add. |
| `cyvcf2` | Not used | `pipeline/vcf_parser.py`'s own module docstring documents choosing a plain-text VCF parser specifically to avoid a native-extension (htslib-backed) dependency. gnomAD's local-index provider (Phase 2) follows the same policy: it shells out to the `tabix` CLI rather than linking `cyvcf2`. |
| `pysam` | Not used | Same reasoning as `cyvcf2`. |
| `pyfaidx` | Not used | Reference sequence comes from the Ensembl REST API (`pipeline/sequence_context.py`), not a local FASTA, so there's no local index to read with `pyfaidx`. |
| `triton` | Not a direct dependency | Only relevant transitively via `flash-attn` (Evo2's dependency, itself not pip-installed — see below). |

Reintroducing any of these speculatively would work against this
project's own stated dependency-minimization strategy; they're
recorded here as an explicit "audited, deliberately excluded" decision
so a future contributor doesn't add them back without re-reading this
reasoning.

**Finding: `flash-attn` is correctly *not* in `requirements.txt`.**
It's Evo2-only, requires a matching CUDA/torch ABI to build, and this
codebase's own `EVO2_T4_HARDWARE_FINDINGS.md` documents why Evo2 (and
therefore flash-attn) is disabled below compute capability 8.0. Per
this task's explicit instruction, Evo2 was not touched in this pass.

**No blindly-upgraded packages.** Every version referenced in
`verify_environment.py::EXPECTED` matches the pins already present in
`requirements.txt` / `DEPENDENCY_MIGRATION_REPORT.md` prior to this
pass — this hardening effort adds *verification* of those pins, it
does not change what they are.

## 2. Stable requirements

`requirements.txt` was reviewed and left as-is (already fully pinned,
Colab/Ubuntu 22.04/Ubuntu 24.04 compatible per the existing
`DEPENDENCY_MIGRATION_REPORT.md`). No `pyproject.toml` exists in this
project; introducing one was out of scope for this pass since it would
be a packaging-format change orthogonal to dependency correctness, not
requested independently of the existing `requirements.txt` workflow.

## 3–6. Startup validation / environment checker / error messages / repair suggestions

New file: **`verify_environment.py`** (root). Run it directly
(`python verify_environment.py`) or import its `run_all()` in your own
startup path. It performs 16 independent checks (Python, torch,
torchvision, transformers, accelerate, tensorflow, MMSplice
prerequisites, CUDA/GPU + compute capability, cross-package
compatibility, internet connectivity, BLAST, tabix, RAM, disk, model
module imports, database client module imports), each producing a
PASS/WARN/FAIL line with a human-readable explanation and, where
applicable, a concrete `pip install ...` fix line — never a bare
traceback. `--json` for machine-readable output, `--strict` to exit 1
on any failure (for use in a CI/startup gate).

Example of the "better error message" requirement in practice (this is
real output from this sandbox, not a mockup):

```
❌ [FAIL] Model module imports
        OK: none. Failed to import: DNABERT-2 (No module named 'torch'); HyenaDNA (No module named 'torch'); ...
```

> **[2026-08-22] This captured output is no longer reproducible against current code — retained, not corrected.**
> The block above was real output when this verification ran, and the "not a mockup" claim was true at the time.
> It has since been falsified by changes elsewhere: `verify_environment.py`'s model map no longer contains
> **DNABERT-2** (zero occurrences in that file today) and now contains **Enformer** and **Borzoi**, which the
> block never mentions — 7 entries where the capture shows 6. **No current run can produce this text.**
> It is kept rather than rewritten because this is a verification record: replacing captured output with a
> hand-written approximation would re-commit the defect the "not a mockup" claim exists to rule out.
> Read it as evidence of what was observed then, never as current output.
> Found by Andy's doc-quoted-program-strings inventory; annotated by god. See also `verify_environment.py:573`,
> which still hardcodes "All 6 model wrapper modules" against that same 7-entry map — a source defect, not
> documentation, and therefore left for the human.

instead of a bare `ModuleNotFoundError` traceback surfacing from deep
inside `models/__init__.py` the first time any model is touched.

## 7. Documentation

`README.md` updated:
- "8. External data sources" — added gnomAD.
- "10. Environment variables" — added all 14 new `GEPER_GNOMAD_*` variables.
- New "16. gnomAD" section — full configuration/architecture writeup.
- New "17. Environment verification & troubleshooting" section —
  `verify_environment.py` usage, the compatibility matrix, a
  symptom→cause→fix troubleshooting table, and supported OS/GPU
  guidance (Ubuntu 22.04/24.04, Colab; compute capability ≥7.0
  general / ≥8.0 for Evo2 specifically).

## 8. Preserve existing functionality

Not modified: `pipeline/vcf_parser.py`'s parsing logic, any model's
`_load_impl`/`_infer_impl`, `pipeline/interpretation.py`'s existing
ClinVar/dbSNP/protein/AlphaMissense/MMSplice evidence blocks (only new
code was *appended* — see the diff in `pipeline/interpretation.py`),
`report/json_builder.py`'s existing keys (`gnomad` is a new,
additive key), the REST/API-facing JSON shape for every field that
existed before this pass, model routing, or Evo2 (untouched, per this
task's explicit instruction).

`tests/test_gnomad_integration.py::TestJsonBuilderGnomadBackwardCompatibility`
verifies this directly: a `build_variant_result(...)` call using the
exact positional/keyword shape that existed before Phase 2 (no
`gnomad_result` kwarg) still produces a complete, valid record.

## 9. Verify: do DNABERT-2/HyenaDNA/RNA-FM/ESM2/AlphaMissense/MMSplice still load?

**This is the one requirement this sandbox cannot fully confirm, and
it's important to be precise about why, rather than claim success it
didn't actually observe:**

- **What was verified (real, executed, in `verify_environment.py`'s
  "Model module imports" check and this repo's own test suite):**
  every one of the 6 models' wrapper *modules* still imports without
  error, and the surrounding pipeline code
  (`pipeline/orchestrator.py`, `pipeline/interpretation.py`,
  `report/json_builder.py`) that routes to them still imports and
  runs correctly — confirmed via `pytest tests/` (110/110 passing,
  using this repo's own `_fake_heavy_deps.py` harness where a real
  torch/tensorflow install isn't available) and via direct import
  checks with the fake-deps stub installed.
- **What was NOT verified, and why:** this sandbox has no GPU and no
  network access to Hugging Face / the model checkpoint hosts (see
  `verify_environment.py`'s own "Internet connectivity" check output,
  which is included below for this exact run), so it cannot download
  the real DNABERT-2/HyenaDNA/RNA-FM/ESM2 checkpoints, the real
  AlphaMissense catalogue, or the real MMSplice `.h5` weights, and
  therefore cannot execute a real forward pass through any of them.
  "Imports cleanly" is a necessary but explicitly *not* sufficient
  condition for "loads and predicts correctly" — treat this section as
  confirming the former only.

Actual output from this sandbox (torch/tensorflow not installed here —
this is the expected, honestly-reported state of *this* environment,
not a defect introduced by this pass):

```
❌ [FAIL] Model module imports
        OK: none. Failed to import: DNABERT-2 (No module named 'torch'); HyenaDNA (No module named 'torch'); RNA-FM (No module named 'torch'); ESM2 (No module named 'torch'); AlphaMissense (No module named 'torch'); MMSplice (No module named 'torch')
✅ [PASS] Database client imports
        All import cleanly: ClinVar, dbSNP, BLAST, gnomAD.
```

> **[2026-08-22] This captured output is no longer reproducible against current code — retained, not corrected.**
> The block above was real output when this verification ran, and the "not a mockup" claim was true at the time.
> It has since been falsified by changes elsewhere: `verify_environment.py`'s model map no longer contains
> **DNABERT-2** (zero occurrences in that file today) and now contains **Enformer** and **Borzoi**, which the
> block never mentions — 7 entries where the capture shows 6. **No current run can produce this text.**
> It is kept rather than rewritten because this is a verification record: replacing captured output with a
> hand-written approximation would re-commit the defect the "not a mockup" claim exists to rule out.
> Read it as evidence of what was observed then, never as current output.
> Found by Andy's doc-quoted-program-strings inventory; annotated by god. See also `verify_environment.py:573`,
> which still hardcodes "All 6 model wrapper modules" against that same 7-entry map — a source defect, not
> documentation, and therefore left for the human.

**To actually confirm requirement #9 in a real deployment:** install
the pinned versions from `requirements.txt`, run
`python verify_environment.py --strict`, and confirm it exits 0 with
"Model module imports" showing PASS — then run a real
`python main.py --vcf <small VCF>` end to end on a machine with GPU
access (or CPU, slower) and internet access to Hugging Face, and
confirm each model's section appears populated (not skipped) in
`geper_results.json`.

## Known limitations of this Phase 1 pass

- No live confirmation of the 6 models' actual inference correctness
  (see "9. Verify" above) — this sandbox's lack of GPU/network is the
  binding constraint, not a gap in the validation logic itself.
- No `pyproject.toml` was introduced (see "2. Stable requirements").
- `verify_environment.py`'s internet-connectivity check only confirms
  TCP reachability to three specific hosts (huggingface.co,
  rest.ensembl.org, eutils.ncbi.nlm.nih.gov); a host being reachable
  doesn't guarantee an actual API call to it will succeed (auth,
  rate-limiting, etc. are separate failure modes it doesn't check).

## Addendum — torchaudio pin (post-review fix)

**Reported issue:** `requirements.txt` pinned `torch==2.7.1` and
`torchvision==0.22.1` but left `torchaudio` unpinned. On Colab
specifically, this meant `pip install -r requirements.txt` never
touched Colab's own pre-installed `torchaudio` build (tied to
whatever torch version Colab's base image shipped, not this project's
pinned 2.7.1) — and when something in the import chain (a transitive
dependency of multimolecule/evo2, or Colab's own environment) later
imported that mismatched `torchaudio`, it failed with `OSError:
undefined symbol: torch_library_impl`. Because that failure isn't
scoped to whichever module triggered the import, it surfaced while
importing ESM2, not torchaudio itself — a genuinely confusing failure
mode to debug from the traceback alone.

**Fix applied:**
- `requirements.txt` now pins `torchaudio==2.7.1` explicitly, paired
  with `torch==2.7.1` exactly the same way `torchvision==0.22.1`
  already was, with an inline comment explaining why (see the file).
- `verify_environment.py` gained a dedicated `check_torchaudio()`
  check (PASS/WARN/FAIL, with a `pip install torchaudio==2.7.1` fix
  line) and `check_dependency_compatibility()` was extended to flag a
  torch/torchaudio version mismatch specifically, calling out the
  `undefined symbol: torch_library_impl` failure mode and that it can
  appear to originate from an unrelated model import.
- `README.md`'s compatibility matrix and troubleshooting table (section
  17) both updated with a torchaudio row.
- New regression test, `tests/test_torchaudio_pin_regression.py` (5
  tests, all passing): confirms `requirements.txt` pins `torchaudio`
  to the exact same version as `torch`, and — using fake `torch`/
  `torchaudio` modules reproducing the exact reported scenario
  (torch 2.7.1 + torchaudio 2.11.0+cu128) — confirms
  `verify_environment.py` flags the mismatch via both checks, with the
  right version numbers and the `torch_library_impl` symbol name in
  the diagnostic text, and confirms the validator script itself has no
  top-level import of `models`/`pipeline` (i.e. it can genuinely run,
  and catch this, before any model import is attempted).

**Verified for real, in this sandbox** (`pytest
tests/test_torchaudio_pin_regression.py -q` → 5 passed; full suite →
115/115 passed). **Not verified against a real Colab environment** —
this sandbox cannot reproduce Colab's actual pre-installed package set
or a real `pip install -r requirements.txt` run against it; the fix
addresses the mechanism as described (unpinned dependency + transitive
import), confirmed via the simulated-module test above, not via a live
Colab re-run.
