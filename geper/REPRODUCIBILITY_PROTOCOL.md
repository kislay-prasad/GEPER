# GEPER Inter-Run Reproducibility Protocol

Status: **DEFINITION ONLY. UNCOMMITTED. NO MEASUREMENT RUNS PERFORMED.**
Author: Andy (DevOps & Infrastructure lane), dispatched by god, human-approved 2026-08-21.
Baseline: measure against tag `validation-baseline-2026-08-21` (470a993), never against a moving `master`.

## 0. Is reproducibility claimable at all, given mid-run interpreter mutation?

This question is prior to everything below it. It is answered narrowly here, not designed
around -- S3e and S4b (further down) already design around the mutation once this section
establishes what can honestly be designed around at all.

**Short answer: reproducibility, as this protocol can measure it, is claimable only for a
bounded scope -- a pre-provisioned, isolated execution -- and is NOT claimable for GEPER's
own default/documented mode of operation. This is a real narrowing of what "the GEPER
reproducibility protocol" can honestly promise, not a footnote on an otherwise-general
claim.**

**[DEMONSTRATED — `auto_install.py`'s own docstring; this session's own forensics]**
`utils/auto_install.py`'s `ensure_pip_package_available` exists, in its own words, because
"`pip install -r requirements.txt` was never run, or a runtime restart dropped a previous
session's installs" -- this is not an edge case bolted on, it is GEPER's documented
mechanism for tolerating an incomplete starting environment. `pipeline/models/mmsplice/
loader.py`'s `_ensure_mmsplice_package_files_available` does the identical thing for
`mmsplice`. Both can `pip install` into the running interpreter the first time a package is
actually needed, mid-run.

**[ANALOGY — extending the repeatability/reproducibility framing already used in S4b]**
Reproducibility, in the sense an assessor will read the word, presupposes that "the
conditions" held constant across repeats are a single, well-defined value for the duration
of the thing being measured. A run whose own package set changes between its start and its
end does not have one environment to hold constant -- it has at least two: whatever was
present for the code paths that executed before the mutation, and a different set for
whatever executed after. "Did this run reproduce" is not a well-posed question until "under
what environment" has a single answer, not two.

**[DEMONSTRATED — this session's own enformer_pytorch investigation]** This is not a
hypothetical concern for this floor specifically: this session directly investigated a real
disappearance of an installed package from the shared global interpreter mid-day, and
separately confirmed live that GEPER's own `--constraint`-guarded auto-install can still
interact with whatever else happens to be installed at the moment it fires. On a **shared**
interpreter -- the shared global Python 3.14 environment multiple agents on this floor use
is a demonstrated example, not a theoretical one -- a run's starting package set is not even
a function of that run's own input; it is a function of whatever every other process that
has touched that interpreter did before it. That is a second, independent way the
"same conditions" precondition can fail, beyond a given run's own auto-install behavior.

**Therefore, the scope this protocol can actually deliver.** A reproducibility claim is
available, and well-posed, ONLY for a run satisfying both:

- **(a) Pre-provisioned environment**: every package any code path in the run could need is
  already installed before the run starts, so the cheap `is_pip_package_installed` check
  (present in both `auto_install.py` and `mmsplice/loader.py`) finds everything already
  present and the install code path is never *entered* -- not invoked-and-succeeded, never
  invoked. A precondition to arrange before measurement, not a variable the measurement
  observes.
- **(b) Isolated execution**: the interpreter is not shared, concurrently or serially within
  the measurement window, with any other process capable of installing or removing packages
  in it -- a dedicated environment for the measurement, not the shared global environment
  this floor has directly observed being mutated by unrelated activity.

(a) is independently **verifiable after the fact**, exactly the way S3e already specifies:
capture the resolved-package freeze before and after the run; if they differ, condition (a)
failed for that run, full stop, and that run is **out of scope for any reproducibility
claim** -- not noisier, not counted with a caveat, excluded by construction. (b) is **not**
verifiable from inside a single run's own output at all -- it is a procedural guarantee
about how the measurement was set up (a dedicated venv, network access removed or
restricted to only the specific external sources S2f already requires, no concurrent
agent/process activity against that interpreter during the window), and has to be true by
arrangement; the protocol cannot detect after the fact that it wasn't.

**What this explicitly does not cover, stated so an assessor cannot read more into the
title than is meant:** GEPER's own default, documented deployment mode -- start with
whatever happens to already be installed, let `auto_install.py`/`mmsplice/loader.py` fill
gaps on demand -- is, by design, allowed to resolve differently on different invocations
(different network conditions, different package-registry state at the moment pip
resolves, different starting state on a shared or reused interpreter). This protocol makes
**no** reproducibility claim about that mode, and must not be read as implying one exists.
"GEPER is reproducible" is not a sentence this protocol can support. "GEPER is reproducible
when run pre-provisioned and isolated, and the S4 Tier 1/Tier 2 measurement is only
meaningful under that precondition" is the sentence it can support.

**[INVENTED]** The two-part necessary-condition statement above and the conclusion that the
default deployment mode is out of scope have no external or in-repo precedent -- they are
this session's own reasoning from the demonstrated facts cited above, not drawn from a
citation. Flagged so a reviewer knows exactly where to push back.

## 0b. What this document is, and is not

This document defines **how to measure** whether GEPER produces the same output from the
same input, and how to characterize the variance when it does not. It does **not** define
what level of reproducibility is acceptable. Every place a number would be needed to say
"this run passed," this document leaves a named, visibly-unfilled placeholder instead of a
number, and states what kind of judgement (always clinical-genomics / validation-lane, never
infrastructure) has to fill it.

This split is not mine to soften. Two agents own two different questions:

- **This document (infrastructure):** if you ran the same input twice, would you get the
  same answer, and if not, how different, measured how?
- **The validation/study-design lane (clinical judgement, separate agent):** is that
  difference small enough to trust the result clinically?

A number that quietly answers the second question while dressed as the first is exactly
the failure mode the human is guarding against with this split. None appear below.

### Tiering legend (used on every substantive claim below)

- **[PUBLISHED]** — drawn from a named, external, published source. Where I'm citing from
  training knowledge rather than something fetched/read this session, that's flagged
  explicitly — treat those as "named and plausible" rather than "verified live."
- **[ANALOGY]** — inferred by analogy to GEPER's own existing infrastructure, or to
  adjacent software-reproducibility practice, without a specific named external source.
  Per the human's own framing: this is where a plausible-looking wrong answer hides. Read
  these more skeptically than [PUBLISHED] ones.
- **[INVENTED]** — no source, no analogy; a design choice I made because the protocol
  needed one. Flagged so a reviewer knows exactly where to push back hardest.

---

## 1. The comparable surface: what "same output" means

Byte-identical `geper_results.json` is the wrong bar. GEPER's own output format already
contains fields that are *supposed* to differ between two runs of the identical input
against the identical code — comparing those would manufacture false "non-reproducibility"
findings. The protocol's first job is separating those from the fields that must actually
match.

### 1a. Fields expected to vary — excluded from equality comparison **[ANALOGY]**

| Field | Why it legitimately varies |
|---|---|
| `generated_at` (`report/json_builder.py:128`) | Wall-clock timestamp of the write, by construction. |
| Derived `run_id` (`report/summary.py:455` `_derive_run_id`) | Itself derived from `generated_at` when not explicitly overridden — inherits the same variability. |
| Per-stage wall-clock durations (`geper_benchmark.json`, profiling) | Timing, not a claim about the variant. |
| `PID=... PARENT_PID=... START=...` log line (`main.py`, this session's own addition) | Process identity, informational only — never enters `geper_results.json`. |
| Any field whose value legitimately depends on an external data source's *current* state (see §2f) | Not code non-determinism — the input to that field changed, not the code that reads it. |

### 1b. Fields that must be identical for two runs to be called "reproducible" **[ANALOGY]**

- `input_vcf`, `assembly`, `vcf_samples`, `variant_count` — trivially, since these are
  restatements of the input, not computed results. A mismatch here means the two runs
  didn't actually share an input, not a reproducibility finding.
- `code_version` (`pipeline/provenance.py::get_geper_code_version`) — must be the identical
  commit SHA **and** must not carry the `+dirty` suffix on either run. A dirty-tree run is,
  by that function's own documented reasoning, "not reproducible from the commit alone" —
  so reproducibility runs are only meaningful on a clean tree in the first place. This is a
  **precondition** on running the protocol, not a comparable field with expected variance.
- `model_checkpoints[*].identifier` (model name/revision strings, e.g. the DNABERT-2 pin
  pattern from `1fa8c56`) — must match. A revision drifting between two "identical" runs
  means the environments weren't actually identical (see §3).
- Per-variant **evidence content**: ACMG criteria applied, BLAST hit identity/coordinates
  (when BLAST mode and DB are held fixed), model raw outputs, interpretation text.
- Per-variant **final classification** (Pathogenic/Likely Pathogenic/VUS/Likely
  Benign/Benign) — treated as its own comparable field, not merely re-derived from
  comparing raw scores. See §1d for why that distinction matters.

### 1c. Fields that must match "as of the provenance record" — the data-drift carve-out

`pipeline/provenance.py`'s `RunProvenanceCollector` already exists to answer exactly this
question for external sources (ClinVar, gnomAD, dbSNP, InterPro, UniProt, Ensembl, ClinGen,
MANE, AlphaMissense, BLAST tool versions — see that module's own audit table). The protocol
reuses it rather than re-inventing a parallel mechanism **[ANALOGY — reuse of existing
GEPER infrastructure, not a new invention]**:

> Two runs are compared **only** after first diffing their provenance records against each
> other. Any per-variant output difference that traces to a data source whose captured
> version/hash/timestamp *also* differs between the two runs is **data drift, not a
> reproducibility finding**, and must be excluded from the variance tally before any other
> comparison happens. Any output difference where the provenance records match exactly *is*
> a genuine reproducibility finding and belongs in the tally.

Without this step, a rerun of the identical VCF weeks apart would show real annotation
differences purely because ClinVar updated weekly (documented in `provenance.py`'s own
module docstring) or gnomAD/MANE released a new version — and a naive protocol would
misreport that as pipeline non-determinism.

Sources `provenance.py` itself already reports as `UNKNOWN` (ClinVar E-utilities has no
database-wide version; ClinGen ERepo/MaveDB and MyVariant.info likewise) cannot be
data-drift-excluded this way — there's no version signal to diff. For these, the protocol
can only recommend holding **wall-clock proximity** between repeat runs as a substitute
control (see §4c) and flagging any output difference touching those sources as
*unattributable* rather than confidently "code" or "data."

### 1d. Numeric comparison vs. categorical comparison — two different failure modes

Raw model scores are floating point and, per §2a/§2d, are not guaranteed bit-identical even
under a genuinely fixed environment (CPU BLAS reduction-order effects). ACMG classification
is a **thresholded** function of those scores. This means two categorically distinct
failure modes exist and must be reported separately, not collapsed into one "did it match"
boolean **[INVENTED — this distinction doesn't exist anywhere in GEPER today; I added it
because §2a's threading finding makes it a real risk, not a hypothetical one]**:

- **Numeric jitter**: a score differs by a small amount, classification unchanged.
- **Classification flip**: a score's jitter crossed a decision boundary, changing the
  reported ACMG tier. Far more clinically consequential than its numeric magnitude
  suggests — a flip driven by 1e-6 of floating-point noise looks, to a report reader,
  identical to a flip driven by a real defect. The protocol (§4d) requires flips to be
  counted and reported as their own category, always, regardless of how small the
  triggering numeric delta was.

The magnitude of numeric jitter that counts as "acceptable noise" vs. "investigate this" is
a threshold — **left blank, see §5**.

---

## 2. Sources of non-determinism (enumerated, not assumed absent)

### 2a. Model inference — CPU kernel non-determinism **[PUBLISHED, from training knowledge —
not fetched/verified live this session: PyTorch's own "Reproducibility" documentation notes
that even with all seeds fixed, multi-threaded CPU operations (and CUDA operations) can
produce run-to-run floating-point differences because parallel reduction order is not
guaranteed, and that `torch.use_deterministic_algorithms(True)` only *narrows*, never fully
eliminates, this on CPU.]**

Confirmed relevant to this box specifically: the proven build recipe (`c753a77`,
`VENV_BUILD_RECIPE.md`) resolves `torch==2.7.1+cpu` — this pipeline runs CPU inference, not
GPU, in every environment verified so far. I grepped GEPER's own inference code for any
thread-count pin (`torch.set_num_threads`, `OMP_NUM_THREADS`) and found none — the only
`ThreadPoolExecutor` usage in `pipeline/` is for concurrent *network* I/O (gnomAD, ClinGen,
HPO, InterPro, UniProt, conservation providers), not model inference. Torch's own default
CPU thread count is environment-dependent (core count), which is itself a second, compounding
source of the same underlying reduction-order effect across machines with different core
counts.

### 2b. Random seeds — **demonstrated absence, not inferred [DEMONSTRATED — grep, this
session]**

`grep -rniE "manual_seed|random\.seed|np\.random\.seed|torch\.seed|PYTHONHASHSEED|
use_deterministic|cudnn\.deterministic|set_seed"` across the GEPER inference tree returns
zero hits inside GEPER's own pipeline/models code. Every hit is inside the vendored
`hyena-dna/` *training* repo (evals and dataloaders), which GEPER's inference path does not
execute. **GEPER's own inference pipeline sets no random seed anywhere.** Whether any model
GEPER actually calls at inference time has a stochastic component sensitive to this (most
inference-mode/eval-mode transformer forward passes do not sample, but this has not been
verified per-model without running code, which is out of scope here) is an **open question**
— see §6.

### 2c. Dict/set ordering **[ANALOGY]**

Python dict insertion order is guaranteed since 3.7; not a risk by itself. Set iteration
order is not guaranteed and is process-randomized for strings unless
`PYTHONHASHSEED` is fixed (also not set anywhere in GEPER's own launch path, per the same
grep as §2b). Whether any GEPER output path iterates a `set` directly into JSON/report
ordering (as opposed to a `dict` or a sorted list) was not exhaustively audited under this
document-only mandate — flagged as an **open question**, not a confirmed source, in §6.

### 2d. Parallelism / concurrent I/O ordering **[ANALOGY]**

`ThreadPoolExecutor` is used for concurrent provider lookups (gnomAD `provider.py:414`,
ClinGen `provider.py:497`, HPO `provider.py:354`, InterPro `provider.py:224`, UniProt
`provider.py:295`, conservation `provider.py:546`, plus SpliceBERT's single-worker load
executor and `service_health.py`'s startup probes). Completion order of concurrent network
calls is not guaranteed. Whether any of these has a "first response wins, rest discarded"
race (as opposed to "wait for all, aggregate by key" — the safer pattern) was not audited
line-by-line under this mandate — flagged as an **open question** in §6, since a race of
that shape would be a genuine non-determinism source independent of everything else here.

### 2e. Runtime environment mutation mid-run — **demonstrated, not hypothetical
[DEMONSTRATED — this session's own forensics]**

`utils/auto_install.py` (`ensure_pip_package_available`, ~line 68 onward) and
`pipeline/models/mmsplice/loader.py` (`_ensure_mmsplice_package_files_available`, ~line 85)
both pip-install packages into the running interpreter's own environment **during a run**,
the first time a package is actually needed. This is confirmed live behavior, not a
theoretical risk — this session directly investigated a real, unexplained
`enformer_pytorch` disappearance from the shared environment mid-day, and separately
confirmed (via `--constraint requirements.txt`, commit 47748b6, this floor's own fix) that
`enformer-pytorch`'s pinned `transformers==4.56.2` can silently downgrade an
already-installed, correctly-pinned `transformers` if the constraint isn't applied — i.e.
GEPER's own auto-install machinery is a real, demonstrated vector for one run to leave the
*environment* in a different state than it found it, which the *next* run then inherits.

Consequence for this protocol: **"pin the environment once, then diff outputs" is not
sufficient.** The environment must be captured (or re-captured) **after** a run completes,
not only before it starts, because the run itself may have changed what's installed. Two
runs launched against an identically-pinned starting environment are not guaranteed to
still share an environment by the time either one finishes. See §3e.

### 2f. External data-source drift — **not code non-determinism, a distinct threat model
[DEMONSTRATED — `pipeline/provenance.py` module docstring, already-shipped GEPER
infrastructure built for this exact reason]**

ClinVar (weekly), gnomAD, dbSNP, HPO, ClinGen, MANE, InterPro, UniProt, AlphaMissense, and
Ensembl are all live, independently-versioned external services. `provenance.py`'s own
module docstring states this plainly: "a report that says 'ClinVar reports Pathogenic' with
no record of WHICH ClinVar snapshot cannot be reproduced later." A rerun of the identical
VCF weeks apart can legitimately return different annotations for reasons that have nothing
to do with GEPER's code. This is why §1c requires diffing provenance records before
attributing any output difference to the pipeline itself.

### 2g. Caching and network mode configuration **[ANALOGY]**

`--blast-mode` (auto/remote/local), `--no-blast-cache`, and the auto-fetch TTLs (raised
earlier this session as a run-configuration mitigation, per this floor's own history) all
affect whether a given run hits a live network call or a cached prior answer. A cache that
expired between two "repeat" runs is itself a live-vs-cached-answer non-determinism source,
distinct from both §2a-e (code) and §2f (data drift) — it's a *configuration* variable that
must be pinned identically across repeats for the comparison to mean anything (see §3).

### 2h. Hardware **[ANALOGY]**

Every environment verified so far on this floor is CPU-only (`torch==2.7.1+cpu`). A future
GPU deployment would add cuDNN/CUDA-kernel non-determinism, which is extensively documented
upstream **[PUBLISHED, training knowledge, not fetched this session]** as a materially
larger effect than CPU reduction-order noise. Out of scope for measurement today (no GPU
environment exists to measure), but the protocol should not silently assume CPU-only
forever — flagged for §3 as a field to capture even though it isn't yet a variable, so a
future GPU run is visibly different in the record rather than assumed equivalent.

---

## 3. Environment pinning: what must be captured for a run to be re-creatable

GEPER already has real, working infrastructure for several of these — this section cites
and reuses it rather than re-specifying a parallel scheme, per the same principle as §1c.

### 3a. Interpreter **[ANALOGY — reuse of `VENV_BUILD_RECIPE.md` precedent]**
Exact Python version (proven: 3.12.10, `c753a77`) and interpreter path. The `evo2`
exclusion and the 3.11/3.12 constraint documented in `VENV_BUILD_RECIPE.md` are cited, not
repeated here.

### 3b. Packages, **as actually resolved**, not as declared **[ANALOGY — reuse of
`VENV_BUILD_RECIPE.md` precedent]**
`requirements.txt` declares ranges (`transformers>=5.12.1,<6.0.0`); the proven build
resolved to `transformers==5.15.1`. A reproducibility record must capture the full
`pip freeze`-equivalent resolved set (the 82-package freeze in `VENV_BUILD_RECIPE.md` is
the existing artifact shape to reuse), not the requirements.txt ranges — two environments
can satisfy the same `requirements.txt` and still differ.

### 3c. Model weights / revisions **[ANALOGY — reuse of DNABERT-2 pin precedent, `1fa8c56`]**
`pipeline/provenance.py::finalize_model_checkpoint_provenance` already enriches
`model_checkpoints` with each model's real per-run USED/FAILED/DISABLED/SKIPPED status —
this is the existing mechanism, cited not re-invented. `1fa8c56`'s DNABERT-2 revision pin
(`zhihan1996/DNABERT-2-117M@7bce263...`) is the existing precedent for "pin to a specific
reviewed commit hash, not a moving `main`/`latest` ref" — the protocol's requirement is that
**every** model consulted in a reproducibility run have a captured revision identifier of
this shape, not just DNABERT-2, so that "same model" is a checkable claim rather than an
assumption.

### 3d. Reference / external data versions **[ANALOGY — reuse of `pipeline/provenance.py`,
already shipped]**
`RunProvenanceCollector` is the existing, working mechanism (see §1c and §2f) — cited, not
re-specified. Its honest `UNKNOWN` reporting for sources with no source-wide version
(ClinVar E-utilities, ClinGen ERepo/MaveDB, MyVariant.info) is itself part of what this
protocol relies on: it tells you *which* comparisons can be provenance-verified and which
can only be time-proximity-controlled (§4c).

### 3e. Code version, captured **after** the run, not only before **[INVENTED — the
"after, not just before" requirement, reasoning drawn directly from §2e's demonstrated
finding]**
`get_geper_code_version()` (`pipeline/provenance.py:347`) captures the exact commit SHA and
a `+dirty` flag if the tree has uncommitted changes. Existing mechanism, cited. The
protocol's addition: because §2e demonstrates the *installed package set* can mutate
mid-run even when the *git tree* does not, environment pinning for this protocol means
capturing the resolved-package freeze (§3b) **both before and after** each run, and treating
a before/after mismatch itself as a first-class finding — "this run changed its own
environment" is exactly the kind of fact a naive single before-run snapshot would hide.

### 3f. OS / hardware / thread configuration — **a real gap, not currently captured
anywhere [DEMONSTRATED — grep, this session; the gap itself, not a citation]**
Nothing in `geper_results.json`, `pipeline/provenance.py`, or anywhere else in the pipeline
records OS version, CPU model/core count, total RAM, or the effective thread count torch
actually used. §2a and §2h identify these as real non-determinism variables. This is a
genuine hole in GEPER's current reproducibility-relevant capture, not merely
under-specified by this protocol — flagged plainly as an implementation gap for whoever
picks up §6/§7, since capturing it is out of scope for a document-only mandate.

---

## 4. Run-to-run variance measurement procedure

### 4a. Repeat count **[INVENTED, with reasoning — not a citation]**
- **Minimum viable (detects that non-determinism exists at all): 3 repeats.** Two repeats
  cannot distinguish "these two runs happened to match" from "this pipeline is
  deterministic" — a third breaks that ambiguity cheaply.
- **Recommended for characterizing the *distribution* of variance (not just its
  existence): ≥10 repeats**, per input, per tier (§4b). This is a measurement-granularity
  choice, not an acceptance threshold — it says how precisely you can describe the
  variance, not what variance is tolerable. If the validation lane later needs a specific
  statistical power/confidence level, that would refine this number; nothing here requires
  waiting on that to start measuring.

### 4b. Two-tier design — same-environment repeats vs. rebuilt-environment repeats
**[INVENTED — the split itself; loosely informed by the general metrology distinction
between "repeatability" (same conditions) and "reproducibility" (varied conditions), a
distinction I recognize from general measurement-science convention but have not verified
against a specific named clinical-lab standard this session — treat the *terminology* as
[ANALOGY] even though the *need* for the split is [DEMONSTRATED] by §2e/§3e]**

The enumerated sources in §2 split cleanly into two categories that must not be measured
together, because conflating them hides which one is actually responsible for any observed
variance:

- **Tier 1 — same pinned environment, repeated execution.** Same interpreter, same resolved
  package set (verified via §3b's before/after freeze), same machine, run N times against
  the same input. Isolates §2a-d (inference/threading/ordering non-determinism) from
  everything else, since the environment itself is held maximally constant.
- **Tier 2 — environment rebuilt from the pinned manifest (§3), then repeated.** Follows
  the `VENV_BUILD_RECIPE.md`-style rebuild process, once per repeat, then runs the same
  input. Isolates whether the *pinning itself* (§3) is sufficient to reproduce a matching
  environment at all — a Tier 2 mismatch that Tier 1 doesn't show means the manifest is
  under-specifying something (most likely candidate, given §3f: OS/hardware/thread count).

A variance finding that appears in Tier 1 is a code/model-inference finding. A variance
finding that appears only in Tier 2 is an environment-pinning-completeness finding. Reporting
these as one undifferentiated "reproducibility score" would erase exactly the information
someone would need to go fix anything.

### 4c. Input selection **[ANALOGY — reuse of this session's own E2E verification
precedent]**
Representative inputs, not just one trivial case: at minimum, a single-variant run (fastest
signal, per this session's own earlier "decisive negative" experiment pattern), a
multi-variant VCF, and inputs that exercise each optional plugin path (Enformer, Borzoi,
SpliceFormer, SpliceBERT, SPiP) and each graceful-degradation branch, since §2e's
environment-mutation risk is plugin-load-triggered. `conflict_tiers.vcf` and
`nuclear_test.vcf` (already used for this floor's BP7/Conflicting-Evidence E2E verification)
are candidate fixtures, cited as existing precedent rather than newly proposed. Repeats for
a given tier should be run in close wall-clock proximity to each other (same session/day)
specifically to hold §2f's uncontrollable-by-provenance sources (ClinVar E-utilities,
ClinGen ERepo, MyVariant.info — the ones `provenance.py` itself reports as `UNKNOWN`) as
close to constant as practically achievable, since those can't be verified equal after the
fact the way versioned sources can.

### 4d. What gets compared, and how it's reported
For each repeat pair, against the comparable surface defined in §1:

1. **Provenance diff first** (§1c) — establishes which output differences, if any, are
   pre-explained by data-source drift and excluded before anything else is measured.
2. **Categorical fields** (§1b: classification, ACMG criteria applied) — exact-match only.
   Any mismatch is reported as a **classification flip**, always, regardless of what numeric
   delta (if any) produced it (§1d).
3. **Numeric fields** (raw model scores) — reported as a distribution of deltas (mean,
   max, per-model breakdown), not a single pass/fail number. What magnitude of delta is
   "noise" vs. "investigate" is the placeholder in §5, not a call this step makes.
4. **Structural fields** (§1b: which variants got processed, which models ran/were
   skipped/disabled/failed per `model_checkpoints`) — exact-match; a difference here (e.g.
   a plugin loaded in one repeat and was `DISABLED` in another) is itself evidence of §2e/§3f
   at work and should be flagged as an environment-parity failure, not folded into the
   numeric-noise report.

Output: a structured report per input × tier — not a single aggregate reproducibility
score. Aggregating away the per-field, per-tier breakdown would re-introduce exactly the
ambiguity §4b exists to avoid.

---

## 5. Placeholders — explicitly not filled here

Each of the following requires clinical-genomics / validation-lane judgement, not
infrastructure judgement, per the boundary stated in §0. Filling any of these is out of
scope for this document and for the agent that wrote it.

> **[ACCEPTANCE THRESHOLD -- NOT SET. Numeric-noise tolerance (§1d, §4d-3): what magnitude
> of score delta between two runs of a "reproducible" pipeline counts as acceptable noise
> vs. a defect worth investigating. Requires: validation-lane / clinical-genomics
> judgement, informed by how close typical scores sit to classification thresholds in
> practice.]**

> **[ACCEPTANCE THRESHOLD -- NOT SET. Classification-flip tolerance (§1d, §4d-2): whether
> ANY flip is disqualifying, or whether a flip only between adjacent tiers (e.g. Likely
> Pathogenic <-> VUS) at a boundary is treated differently from a flip spanning multiple
> tiers. Requires: validation-lane / clinical-genomics judgement.]**

> **[ACCEPTANCE THRESHOLD -- NOT SET. Overall pass/fail criterion for a Tier 1 or Tier 2
> reproducibility run (§4b): e.g. "X% of variants must show zero classification flips
> across N repeats." This document defines how the flips/deltas are counted and reported;
> it does not say how many is too many. Requires: validation-lane / clinical-genomics
> judgement.]**

> **[ACCEPTANCE THRESHOLD -- NOT SET. Statistical confidence level for the repeat count in
> §4a: if the validation lane needs a specific power/confidence guarantee ("95% confidence
> the true flip rate is below X%"), that would set a *specific* repeat count in place of
> this document's measurement-granularity default of >=10. Requires: validation-lane /
> statistical judgement.]**

---

## 6. Open questions — require code execution or deeper review, not answered here

Per this dispatch's explicit boundary ("no measurement runs... if a question can only be
answered by executing something, write it down as an open question rather than executing
it"), the following are flagged rather than resolved:

1. **§2b** — does any model GEPER actually calls at inference time have a
   stochastic/sampling component sensitive to the absent seed, or are all inference calls
   deterministic eval-mode forward passes regardless? Not verified without running code.
2. **§2c** — does any GEPER output-serialization path iterate a raw `set` (as opposed to a
   `dict` or sorted list) into JSON/report ordering? Flagged from general Python-language
   risk, not confirmed present in this codebase; would need a full read of every
   report-building code path to rule in or out.
3. **§2d** — does any concurrent provider lookup use a "first response wins" race pattern,
   or do all of them wait-for-all-and-aggregate-by-key (the safer pattern)? Needs a
   line-by-line read of each `ThreadPoolExecutor` call site listed in §2d; not done here to
   stay within a document-only mandate reading depth appropriate to the dispatch.
4. **§3f** — the OS/hardware/thread-count capture gap: is this something the
   infrastructure lane should be tasked with adding to the run manifest (a `main.py`/
   `provenance.py`-level change, analogous to this session's own PID-capture addition), or
   is it out of scope until Tier 2 measurement is actually scheduled? Not mine to decide
   unilaterally — a genuine "does this need doing now" question for god.

---

## Covering note

**What made this harder than the dispatch's framing suggested:** the dispatch anticipated
"runtime environment mutation" as one enumerated source among several (§2e). Investigating
it for this document surfaced that it's not just *one* source alongside the others — it
breaks the naive "pin environment once, then diff outputs" framing the rest of the protocol
would otherwise use, because the environment isn't stable *within* a single run, let alone
across two. That's why §3e requires an environment capture after each run, not only before,
and why §4b's Tier 1/Tier 2 split exists at all rather than one flat repeat-and-diff loop.

**Where I invented rather than cited:** §1d (numeric-vs-categorical comparison as separate
failure modes), §3e (capture-after-not-just-before), and §4b (the Tier 1/Tier 2 split
itself) have no external or in-repo precedent — they're design choices built from this
session's own §2 findings, flagged [INVENTED] at each occurrence rather than dressed as
established practice. §4a's repeat counts are a reasoned default, also [INVENTED], explicitly
separated from anything resembling a pass threshold.

**Genuine gap found, not merely under-specified:** §3f — GEPER currently captures code
version, model revisions, and external-data versions in real, working, already-shipped
mechanisms (§3c, §3d, §3e), but captures nothing about OS/hardware/thread configuration
anywhere. The data and code provenance story is solid; the hardware side of "environment"
has no home yet. Left as an open question (§6-4) rather than a fix, per this dispatch's
document-only boundary.
