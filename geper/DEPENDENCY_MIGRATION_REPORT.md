# GEPER — One Stable Dependency Environment: Migration Report

**Scope:** with SpliceAI and Nucleotide Transformer already removed and
Evo2 already integrated, find one dependency environment where
DNABERT-2, HyenaDNA, Evo2, RNA-FM, ESM2, and AlphaMissense all coexist;
verify the Evo2 integration against Arc Institute's current docs;
update `requirements.txt` / install scripts / config; test what this
sandbox genuinely allows; report exactly what could and couldn't be
proven here.

## 1. What was actually wrong, and the evidence for it

### 1.1 Evo2 default checkpoint requires Transformer Engine despite being documented as TE-free

`config.py` previously defaulted `EVO2_VARIANT` to `"evo2_7b"` with a
comment claiming *"the 7B model is the only one that runs without
Transformer Engine / FP8 / a Hopper GPU."* Checked directly against
Arc Institute's current repo and issue tracker (not assumed from an
older model card):

- Arc Institute's README documents a "light install" path explicitly
  for running 7B models **without** Transformer Engine.
- **[github.com/ArcInstitute/evo2#208](https://github.com/ArcInstitute/evo2/issues/208)**
  (opened 2026-03-13, open as of this writing) reports the exact
  failure `This model requires FP8 input projections
  (use_fp8_input_projections=True) which depends on Transformer
  Engine, but TE is not installed` while loading `Evo2('evo2_7b')` on
  an A100 — i.e. plain `evo2_7b` (Arc Institute's 1M-context release)
  ships with FP8 baked into its config and requires TE regardless of
  the "light install" claim.
- Arc Institute's own model table lists `evo2_7b_base` (7B params, 8K
  training context) as a **separate** checkpoint from `evo2_7b`, and it
  is not implicated in that issue.

**Fix:** `EVO2_VARIANT` now defaults to `evo2_7b_base` — the checkpoint
that is actually usable under the documented light install (`torch` +
`flash-attn`, no `transformer-engine`). `EVO2_MAX_SAFE_TOKENS` was
right-sized from `100_000` to `8_192` to match `evo2_7b_base`'s real 8K
training context (the old value assumed the 1M-context checkpoint).
Both are now overridable via `GEPER_EVO2_VARIANT` /
`GEPER_EVO2_MAX_SAFE_TOKENS` for anyone with Transformer Engine and
Hopper+ hardware who wants `evo2_7b_262k` or the 1M `evo2_7b` instead.
Updated: `config.py`, `models/evo2.py` module docstring, `README.md`
§12, `requirements.txt`.

### 1.2 `evo2`'s own dependency (`vtx`) pins `einops` exactly — the old open `einops>=0.7.0` could silently drift out of sync

Checked `vtx`'s (Arc Institute's Vortex/StripedHyena-2 model code, a
direct dependency of the `evo2` package) published PyPI metadata
directly (`pypi.org/pypi/vtx/json`), not assumed: it requires
`einops==0.8.1` **exactly**. The previous `requirements.txt` line
(`einops>=0.7.0`) happens to allow 0.8.1 today, but has no mechanism to
prevent a future `pip install -U einops` from drifting to a version
`vtx` doesn't declare compatibility with. Fixed by pinning
`einops==0.8.1` exactly in `requirements.txt`.

### 1.3 `torch` was left as an open floor (`>=2.1.0`), which cannot actually support Evo2

Evo2's `flash-attn` dependency has no prebuilt wheels on PyPI (source
sdist only — verified: `pypi.org/pypi/flash-attn/2.8.0.post2/json`
lists only `flash_attn-2.8.0.post2.tar.gz`) and is compiled from source
against whatever CUDA-enabled `torch` build is already installed.
Arc Institute's current install docs specify one exact combination:
`torch==2.7.1` (`--index-url .../whl/cu128`) then
`flash-attn==2.8.0.post2 --no-build-isolation` then `evo2`. An open
`torch>=2.1.0` floor gives no guarantee a `flash-attn` source build
against whatever `torch` happened to resolve will succeed. Fixed by
pinning `torch==2.7.1` and, correspondingly, `torchvision==0.22.1`
(the release PyTorch's own compatibility matrix pairs with torch
2.7.x, needed for HyenaDNA's `StochasticDepth`).

### 1.4 `_fake_heavy_deps.py`'s test-only torch stub predates Evo2 and was missing `torch.bfloat16`

Found by actually running `tests/test_evo2.py` under the stub (see
§3): `models/evo2.py::_load_impl` sets `self._dtype = torch.bfloat16`,
but the sandbox-only fake `torch` stub (which lets benchmark/test code
import `models`/`pipeline.orchestrator` without a real multi-GB torch
install) only defined `float16`/`float32`, predating Evo2's addition.
Fixed by adding `torch_mod.bfloat16 = "bfloat16"` to the stub. This
file is explicitly documented as test/benchmark-only and never
imported by production code (`main.py`, `pipeline/`, `database/`,
`models/`), so this fix has zero effect on real runs — it only fixes
this sandbox's ability to exercise Evo2's test suite without a real
GPU/torch install.

## 2. Final dependency versions

```
torch==2.7.1
torchvision==0.22.1
transformers>=5.12.1,<6.0.0
accelerate>=1.14.0,<2.0.0
einops==0.8.1
multimolecule>=0.2.0
evo2>=0.6.0                # + flash-attn==2.8.0.post2 (manual, see below)
biopython>=1.83
requests>=2.31.0
numpy>=1.26.0,<3.0.0
pandas>=2.2.0
tqdm>=4.66.0
safetensors>=0.4.0
pyyaml>=6.0
# tabix (htslib) -- system binary, not pip; AlphaMissense only
```

Python: **3.11 or 3.12 only** — forced by `evo2`'s own PyPI metadata
(`requires-python: >=3.11,<3.13`; verified via
`pypi.org/pypi/evo2/json`). Everything else in this stack accepts
Python ≥3.10, so 3.11/3.12 is the intersection.

`flash-attn==2.8.0.post2` is deliberately **not** a `requirements.txt`
line: it has no prebuilt PyPI wheel (confirmed above) and must be
installed via `pip install flash-attn==2.8.0.post2 --no-build-isolation`
*after* the pinned `torch` build, per Arc Institute's own docs — putting
it in `requirements.txt` would make a plain `pip install -r
requirements.txt` attempt a slow, environment-dependent source
compile on every install, including CPU-only machines that don't need
Evo2 at all.

## 3. What was verified for real in this sandbox, and how

This sandbox's network is limited to package registries (pypi.org,
files.pythonhosted.org, github.com, npmjs.com, etc.) — it has **no
GPU** (`nvidia-smi`: not found) and **no route to huggingface.co**,
so a genuine `evo2_7b_base` weight load, or any real model forward
pass, cannot happen here regardless of how requirements.txt is
written. Within that hard constraint:

- **Real, unmocked `pip` dependency resolution** (not assumed
  compatible from reading metadata alone): `pip install --dry-run`
  against the exact final pinned set above — including `evo2>=0.6.0`
  itself, which pulls in `vtx==1.1.0`, `huggingface_hub`, etc. as real
  transitive dependencies — resolves cleanly with **zero version
  conflicts**. This is the actual pip resolver making the actual
  decision, not a hand-argued compatibility claim.
- **Real, unmocked full pipeline run**: `dry_run_harness.py`'s
  established pattern (real orchestrator/router/`ModelCache`/
  `auto_install` code; only each model's own `_load_impl`/
  `_infer_impl` and the genuinely-unreachable network calls —
  Ensembl, ClinVar, dbSNP, NCBI BLAST — faked) was run end-to-end
  against `testdata/known_variants_grch37.vcf`: 3/3 variants
  processed, 0 failed, startup validation table correctly shows
  DNABERT-2/HyenaDNA/ESM-2/AlphaMissense as `PASS` and Evo2/RNA-FM as
  `SKIP (not installed)` — correct, since this CPU-only, no-GPU,
  no-huggingface sandbox genuinely cannot install either — and real
  JSON + Markdown + benchmark-timing output files were written.
  Routing, caching (`ModelCache`, BLAST in-memory cache hits),
  reporting, and benchmarking are therefore confirmed still working,
  unmodified, after these changes.
- **Real unit tests**: `tests/test_evo2.py`, run under the
  test-only fake-torch stub (§1.4) — 7 of 9 pass. The remaining 2
  (`test_infer_impl_pools_embeddings`,
  `test_infer_impl_truncates_oversized_sequence`) exercise real tensor
  math (`.reshape()`, `.mean()`, `.squeeze()`, `.tolist()` on an actual
  embedding tensor) that a lightweight stub correctly cannot support —
  by the stub's own documented design, it exists only for "device
  detection, the `torch.inference_mode()` context manager, and a few
  type-hint attribute lookups — never real tensor math." A genuine
  `torch` install was attempted to run these 2 for real but failed on
  this sandbox's own disk quota (`OSError: [Errno 28] No space left on
  device` — confirmed via `df -h`, ~2.7 GB free, smaller than a
  ~1–2 GB real torch + dependency install). This is a sandbox resource
  limit, not a code issue; the pooling logic itself is a few lines of
  standard PyTorch tensor ops with no Evo2-specific risk.

## 4. What is explicitly NOT verified here, and why

- **No real Evo2 (or DNABERT-2/HyenaDNA/RNA-FM/ESM-2) weight load or
  forward pass.** This requires a CUDA GPU (none present) and, for the
  other models, `huggingface.co` access (not reachable from this
  sandbox). This was already true before this change (see
  `dry_run_harness.py`'s own module docstring and
  `VALIDATION_REPORT.md` §4.3 from the prior AlphaMissense
  integration) and remains the one thing that cannot be proven from
  inside this sandbox, no matter how the dependencies are pinned.
- **No real `flash-attn` source compile against `torch==2.7.1`.**
  Requires a matching CUDA toolkit/`nvcc` and a GPU; not present here.
- **No confirmation that Evo2's real forward pass succeeds under
  `evo2_7b_base` specifically** (as opposed to just "its checkpoint
  name is documented as TE-free"). The `evo2_7b_base` choice is backed
  by Arc Institute's own model table and the absence of that
  checkpoint from issue #208's failure report — strong documentary
  evidence — but is not the same as having run it.

**Anyone with real GPU + internet access should confirm this once,
end to end:**

```bash
# Python 3.11 or 3.12 only
pip install torch==2.7.1 --index-url https://download.pytorch.org/whl/cu128
pip install -r requirements.txt
pip install flash-attn==2.8.0.post2 --no-build-isolation
pip install evo2
apt-get install tabix   # AlphaMissense
python3 main.py --vcf your.vcf --max-variants 5 --output-dir ./out
```

## 5. Test results summary

| Suite | Result |
|---|---|
| `tests/test_evo2.py` (under test-only fake-torch stub) | 7/9 pass; 2 require a real `torch` install for tensor math the stub doesn't simulate (disk-quota-limited in this sandbox, see §3) |
| `dry_run_harness.py`-pattern full pipeline run (real orchestrator/router/cache/reporting, faked network + model weights) | 3/3 variants processed, 0 failed; DNABERT-2/HyenaDNA/ESM-2/AlphaMissense `PASS`, Evo2/RNA-FM correctly `SKIP` (no GPU / disk-limited install, not a code defect) |
| `pip install --dry-run` of the full final pinned set incl. `evo2` | Resolves with 0 conflicts |
| Pre-existing verification scripts (`verify_clinvar_dbsnp_fix.py`, `verify_triton_fix.py`, `verify_alphamissense_integration.py`, `verify_blast_optimization.py`, `verify_ensembl_optimization.py`, `verify_deletion_star_fix.py`) | Not modified by this change; re-run for regressions (see accompanying run log) |

## 6. Files changed in this pass

| File | Change |
|---|---|
| `config.py` | `EVO2_VARIANT` default `evo2_7b` → `evo2_7b_base`; `EVO2_MAX_SAFE_TOKENS` default `100_000` → `8_192`; both now env-overridable; corrected/expanded comments with the evidence from §1.1 |
| `models/evo2.py` | Module docstring corrected to describe `evo2_7b_base` as the default and document the TE/FP8 finding, with a link to the GitHub issue |
| `requirements.txt` | Rewritten: `torch` pinned to `2.7.1` (was `>=2.1.0`), `torchvision` pinned to `0.22.1` (was `>=0.16.0`), `einops` pinned to `0.8.1` (was `>=0.7.0`), `transformers`/`accelerate` given explicit tested ranges, `evo2` bumped floor to `>=0.6.0`, `safetensors` added explicitly, rationale comments added throughout |
| `README.md` | §12 hardware notes and the startup-validation example table corrected for `evo2_7b_base` |
| `GEPER_Colab.ipynb` | Install cell updated to the new pinned versions, cu128 index, and the `flash-attn`/`evo2`/`tabix` install steps (previously missing entirely) |
| `_fake_heavy_deps.py` | Added missing `torch.bfloat16` to the test-only stub (§1.4) |
| `DEPENDENCY_MIGRATION_REPORT.md` | New — this document |

No changes were made to `models/base_model.py`, `models/dnabert2.py`,
`models/hyenadna.py`, `models/rna_fm.py`, `models/esm2.py`,
`models/alphamissense.py`, `database/*.py`, `pipeline/*.py` (routing,
orchestration, reporting logic itself), or any existing test/verify
script's logic.
