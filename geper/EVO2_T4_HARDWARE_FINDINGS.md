# Evo 2 on Tesla T4: Findings and Fix

## 1. The report

On a real deployment reaching Evo 2 initialization (DNABERT-2, HyenaDNA
loading; CUDA/Tesla T4 detected; pipeline starting), the remaining
failure was:

```
Failed to load model 'evo2': No module named 'flash_attn_2_cuda'
```

## 2. What was checked, and against what (not assumed)

- **Dao-AILab/flash-attention's own current README/PyPI page**
  (`pypi.org/project/flash-attn/`, `github.com/Dao-AILab/flash-attention`):
  states its CUDA backend supports "Ampere, Ada, or Hopper GPUs" and
  that Turing GPUs (explicitly naming the T4 and RTX 2080) are covered
  only by a separate, third-party project
  ([flash-attention-turing](https://github.com/ssiu/flash-attention-turing))
  offering "a core subset of FlashAttention features," not the
  official package.
- **Arc Institute's own `evo2` README/PyPI page**: both the
  Transformer-Engine and "light" (no-TE) install paths install
  `flash-attn==2.8.0.post2` directly and explicitly defer to "the
  Flash Attention GitHub for system requirements and troubleshooting"
  if that install has problems -- i.e. Arc Institute does not
  document, support, or provide any Turing-specific fallback for
  `evo2` itself.
- **GPU architecture facts**: the Tesla T4 is a Turing-generation GPU,
  compute capability 7.5. FlashAttention-2's official floor is compute
  capability 8.0 (Ampere) and above.
- **Multiple independent, current (2026) secondary sources**
  (DataCamp's FlashAttention guide, a GitHub inquiry thread on Turing
  support for FlashAttention-2, Hugging Face forum reports of
  `flash_attention_2` failing on T4 deployments) corroborate the same
  floor from different angles.

## 3. Conclusion

**This is a hardware/kernel-support floor, not a dependency-version
bug.** No combination of `torch`/`flash-attn`/`evo2` pins makes
FlashAttention-2's official CUDA backend run on a T4, because the
T4's architecture (Turing, SM 7.5) is one generation below what that
backend's compiled kernels target (SM 8.0+). On a T4, `pip install
flash-attn==2.8.0.post2 --no-build-isolation` either fails outright to
build its CUDA extension, or -- in an environment where some other
process left a stale/mismatched build artifact behind -- leaves the
pure-Python `flash_attn` package importable while the compiled
`flash_attn_2_cuda` extension it needs is missing, which is exactly
the reported failure. This is unrelated to, and not fixed by, the
`torch==2.7.1` / `flash-attn==2.8.0.post2` / `einops==0.8.1` pins
already established in `DEPENDENCY_MIGRATION_REPORT.md` -- those pins
are still correct for hardware that *does* meet the floor.

There is no officially-supported way to run Arc Institute's `evo2`
package on a Tesla T4. The only paths that exist are: (a) run Evo 2 on
compute-capability-8.0+ hardware (A10, A100, L4, L40S, H100, etc.), or
(b) adopt the unofficial, partial-feature `flash-attention-turing`
project entirely outside Arc Institute's support surface (not done
here -- see \u00a75).

## 4. What was changed instead: a graceful, detection-based fallback

Rather than guessing at a workaround, GEPER's existing
"unavailable model -> skip and fall back" architecture (already used
for HyenaDNA-without-git, AlphaMissense-without-tabix, and Evo2-
without-a-GPU-at-all) was extended to cover this exact case:

- **`utils/device_utils.py`**: added `get_cuda_compute_capability()`,
  a small, deliberately uncached helper returning the `(major, minor)`
  compute capability of GPU 0 (or `None` if no CUDA GPU is present).
- **`models/evo2.py`**:
  - `is_available()` now checks compute capability *before* attempting
    the `evo2` package import/install, returning `False` immediately
    on Turing (or any GPU below 8.0) -- so the orchestrator's existing
    per-model skip logic engages up front, the same way it already
    does for a genuinely-missing package, instead of reaching
    `from evo2 import Evo2` and surfacing a raw `ImportError` for
    `flash_attn_2_cuda` three layers deep.
  - Added `unavailability_reason()`, naming the actual GPU and the
    specific compute-capability shortfall (e.g. `"GPU present (Tesla
    T4, compute capability 7.5) but below what FlashAttention-2
    requires ... (compute capability >= 8.0, i.e. Ampere/Ada/Hopper)"`)
    instead of a generic "not installed."
  - `_load_impl()` raises the same specific, actionable
    `ModelLoadError` if ever called directly (bypassing
    `is_available()`), rather than letting the underlying
    `flash_attn_2_cuda` `ModuleNotFoundError` propagate raw.
- **`models/base_model.py`**: added `unavailability_reason()` as an
  optional override point (defaults to `"not installed"`, the only
  reason that existed before this change), so any model -- not just
  Evo 2 -- can report *why* it's unavailable, not just *that* it is.
- **`pipeline/orchestrator.py`**: the startup warning, the startup
  validation table's `Status` column, and the end-of-run summary all
  now render each unavailable model's specific
  `unavailability_reason()` instead of a hardcoded `"not installed"` /
  `"missing / not installed"` string. Example, on a T4:

  ```
  Model      Device  Precision  Max Length  Status
  ---------------------------------------------------------------------------------------------
  Evo2       n/a     n/a        n/a         SKIP (GPU present (Tesla T4, compute capability 7.5)
                                              but below what FlashAttention-2 requires for its CUDA
                                              backend (compute capability >= 8.0, i.e. Ampere/Ada/
                                              Hopper). Evo 2 depends on flash-attn directly and has
                                              no officially supported path on Turing GPUs (T4, RTX
                                              2080) ...)
  ```

- **The commercial architecture is fully preserved.** No model class
  was removed, no routing rule changed, and no other model's behavior
  changed. On a T4: DNABERT-2, HyenaDNA, RNA-FM, ESM-2, and
  AlphaMissense are entirely unaffected. Variants the router flags as
  structurally/evolutionarily complex (the only ones ever routed to
  Evo 2 -- see `config.RoutingConfig.COMPLEX_CONTEXT_VARIANT_TYPES`)
  fall back to DNABERT-2/HyenaDNA via the exact same
  `_filter_available_models` mechanism already used whenever any model
  is unavailable for any reason. A run on a T4 completes with 0 failed
  variants, not a crash.
- **`README.md` \u00a712** and **`config.py`**'s `EVO2_VARIANT` comment
  block were corrected: an earlier note incorrectly described
  `evo2_7b_base` on a T4 as merely "a tight fit at best" (implying a
  VRAM-sizing problem); it is now described accurately as an
  architecture-level incompatibility with a link back to this
  document's evidence trail.
- **`GEPER_Colab.ipynb`**: the notebook's default GPU hint was changed
  from T4 to A100, and its runtime-setup markdown cell now explains
  the compute-capability requirement explicitly (previously it implied
  "T4 or better" was sufficient for everything, including Evo 2).

## 5. What was deliberately NOT done, and why

- **No vendoring of the unofficial `flash-attention-turing` project.**
  It supports only "a core subset" of FlashAttention's features (per
  its own PyPI description), is not published, tested, or endorsed by
  Arc Institute for use with `evo2`, and swapping it in underneath
  `evo2`'s own `vtx`/StripedHyena-2 code (which expects the official
  `flash_attn` API surface) is not something that can be verified
  correct without a real T4 and a real forward pass to compare
  numerically against known-good output -- exactly the kind of "do not
  guess" situation this task called out. If commercial deployment
  specifically requires Evo 2 embeddings on T4-class hardware, that is
  a deliberate build-vs-buy decision (evaluate
  `flash-attention-turing` end-to-end on real hardware, or provision
  Ampere+/Hopper+ instances) that belongs to Kim, not something to
  silently bake into a "fallback" path.
- **No CPU fallback for Evo 2.** As already documented in
  `models/evo2.py` prior to this change, `evo2` has no practical CPU
  inference path at all (flash-attn has no real CPU build), so there
  is nothing to fall back *to* for Evo 2 specifically -- unlike every
  other model in GEPER's stack, which all have a real (if slower) CPU
  path.

## 6. What is explicitly NOT verified here, and why

This sandbox has no GPU at all (`torch.cuda.is_available()` is
`False` throughout), so the Turing/T4-specific code path
(`is_available()` returning `False` *because of* a 7.5 compute
capability, as opposed to because of "no GPU at all") was verified by:

- **14/14 real unit tests** in `tests/test_evo2.py` (run against a
  genuinely-installed `torch==2.12.1+cu130` in this sandbox, not a
  fake stub -- see \u00a77), including four new tests that mock
  `torch.cuda.is_available()=True` and
  `get_cuda_compute_capability()=(7, 5)` together to simulate a T4,
  and assert both `is_available()` returns `False` *without* calling
  `ensure_pip_package_available` (i.e. it never reaches the point
  where the real failure occurred) and that `unavailability_reason()`
  / `_load_impl()`'s raised error both name the GPU and the specific
  compute-capability shortfall.
- **Direct interactive exercise** of `Evo2Model.is_available()`,
  `.unavailability_reason()`, and `._load_impl()` under the same T4
  simulation, confirming the exact messages a real T4 run would emit.
- **The full `dry_run_harness.py` integration path**, re-run after
  this change: 3/3 variants processed, 0 failed, and the run summary
  now shows the specific reason string (in this CPU-only sandbox:
  `"Evo2 (no CUDA GPU detected (Evo 2 has no practical CPU path))"`)
  flowing correctly through the startup warning, the validation table,
  and the final summary -- confirming the plumbing change didn't
  break any other model or the overall run.

**Not verified, and not verifiable from inside this sandbox:** an
actual `pip install flash-attn==2.8.0.post2 --no-build-isolation`
failing on a real T4 with a real CUDA toolkit (this sandbox has no
GPU and no `nvcc`), and an actual Evo 2 forward pass on Ampere+/Hopper
hardware succeeding. The compute-capability numbers and package
behavior above are drawn directly from Dao-AILab's and Arc Institute's
own current published documentation, not inferred.

## 7. Test results summary

| Suite | Result |
|---|---|
| `tests/test_evo2.py`, run under a **real** `torch==2.12.1+cu130` install (this sandbox has 10GB free vs. the earlier 2.7GB shortfall) | **14/14 pass** (previously 7/9 under the test-only fake-torch stub; the 2 tensor-math tests that stub couldn't support now pass for real) |
| `dry_run_harness.py`-pattern full pipeline run (real orchestrator/router/cache/reporting, faked network + model weights, corrected VCF fixture path) | 3/3 variants processed, 0 failed |
| Pre-existing verification scripts (`verify_alphamissense_integration.py`, `verify_blast_optimization.py`, `verify_clinvar_dbsnp_fix.py`, `verify_deletion_star_fix.py`, `verify_ensembl_optimization.py`, `verify_triton_fix.py`) | All PASSED, re-run after this change; no regressions |

## 8. Files changed in this pass

| File | Change |
|---|---|
| `utils/device_utils.py` | Added `get_cuda_compute_capability()` |
| `models/evo2.py` | Added the compute-capability gate to `is_available()`/`_load_impl()`; added `unavailability_reason()`; expanded module docstring with the FlashAttention-2/Turing evidence trail |
| `models/base_model.py` | Added `unavailability_reason()` optional-override hook (default: `"not installed"`) |
| `pipeline/orchestrator.py` | Startup warning, startup validation table, and run summary now show each model's specific `unavailability_reason()` instead of a hardcoded string |
| `tests/test_evo2.py` | Updated existing tests to mock the new capability check; added 6 new tests covering the Turing/T4 gate specifically |
| `dry_run_harness.py` | Fixed a stale hardcoded VCF fixture path left over from a prior sandbox session (unrelated to Evo 2, found while re-running this harness) |
| `README.md` | \u00a712 hardware notes corrected: T4 + Evo 2 is a hard architectural incompatibility, not a VRAM-sizing note |
| `config.py` | `EVO2_VARIANT` comment block expanded with the same hardware-floor explanation |
| `GEPER_Colab.ipynb` | Default GPU hint changed T4 -> A100; runtime-setup cell explains the compute-capability requirement |
| `EVO2_T4_HARDWARE_FINDINGS.md` | New -- this document |

No changes were made to `models/dnabert2.py` (which already has its
own, independent, pre-existing Turing/Ampere gate for its optional
Triton kernel -- unrelated to this fix and left untouched),
`models/hyenadna.py`, `models/rna_fm.py`, `models/esm2.py`,
`models/alphamissense.py`, `database/*.py`, `pipeline/router.py`,
`pipeline/vcf_parser.py`, or any other pipeline/report logic.
