# RNA-FM Migration: MultiMolecule → Official ml4bio/RNA-FM

**Date:** 2026-07-17
**Status:** Complete

## Why

`models/rna_fm.py` previously used the MultiMolecule reimplementation
(`from multimolecule import RnaFmModel, RnaTokenizer`, PyPI package
`multimolecule`). MultiMolecule's RNA-FM model is licensed
**AGPL-3.0-or-later** (confirmed directly on the model's own page,
`SPDX-License-Identifier: AGPL-3.0-or-later`) — a copyleft license
whose network-use clause requires releasing modified source (and,
under a strict reading, the served model) to any user interacting with
it over a network. For a commercial, hospital-deployed clinical
product, this is a material compliance risk that the project's other
model choices (DNABERT-2, ESM-2: permissive; CatBoost: Apache 2.0) had
already deliberately avoided (see prior licensing-audit history).

Official `ml4bio/RNA-FM` is **MIT licensed** — no copyleft, no
network-use disclosure obligation.

## What changed

- `models/rna_fm.py`: internals rewritten to use the official `rna-fm`
  PyPI package (`import fm`) instead of `multimolecule`. Public
  interface unchanged — `RNAFMModel.cache_key()`, `.is_available()`,
  `._load_impl()`, `.reported_max_length()`, and `._infer_impl()`'s
  return shape (`embedding_mean`, `embedding_dim`, `num_tokens`) are
  all identical to callers.
- `config.py`: `ModelConfig.RNA_FM` changed from the HF Hub id
  `"multimolecule/rnafm"` to the official pretrained-loader function
  name `"rna_fm_t12"` (called as `getattr(fm.pretrained, RNA_FM)()`).
- `requirements.txt`: `multimolecule>=0.2.0` → `rna-fm>=0.2.2`.
- Comment cross-references in `database/blast_client.py`,
  `models/evo2.py`, `verify_environment.py`, `utils/auto_install.py`,
  `README.md`, and `GEPER_Colab.ipynb` (which had a live
  `!pip install -q multimolecule` cell) updated accordingly.
- `DEPENDENCY_MIGRATION_REPORT.md` / `PHASE1_VERIFICATION_REPORT.md`
  are dated historical verification records and were deliberately left
  as-is — they document what was true at the time they were written;
  this file is the record of record for the RNA-FM change itself.

## Equivalence verification

Same architecture (12-layer RNA-FM, MultiMolecule's own model card
states the port was confirmed to reproduce the original's intermediate
representations), same embedding dimension (**640**, confirmed both
from MultiMolecule's model card and the official repo's published
architecture). Downstream code (`router.py`, `explainability_engine.py`,
etc.) reads `embedding_dim` dynamically off the tensor shape rather
than assuming a fixed constant, so nothing downstream needed to change.

Pooling behavior preserved: both implementations mean-pool over all
real (non-padding) tokens including BOS/EOS special tokens, via the
same shared `BaseGenomicModel.mean_pool` utility — the official API's
token stream (via `alphabet.get_batch_converter()`) has no HF-style
`attention_mask` output, so `_infer_impl` now constructs an
all-ones mask for the single-sequence (unpadded) batch case, which is
mathematically equivalent to the prior HF tokenizer's mask for the same
case.

## Known residual differences (disclosed, not hidden)

- **Empirical embedding equivalence was not verified in this sandbox**:
  running both `multimolecule` and `rna-fm` side-by-side needs both
  installed together, and this sandbox's disk/network couldn't
  accommodate that (PyPI's default `torch` package pulls in the full
  CUDA toolkit, several GB; the lean CPU-only wheels are hosted at
  `download.pytorch.org`, which this sandbox can't reach). Use
  `scripts/compare_rna_fm_embeddings.py` in an environment with both
  packages installed (e.g. Colab) to get the actual cosine-similarity
  numbers before relying on this migration for anything
  clinically load-bearing. Until that script has been run and passes,
  treat the equivalence claim below as based on the vendor's own
  published statement, not independently reproduced here.
- The official API has no HF-tokenizer-style `truncation=`/`max_length=`
  kwarg; sequence-length truncation is now done on the raw string
  before tokenization (character-level truncation, reserving room for
  BOS/EOS), functionally equivalent to the prior token-level truncation
  for this model's simple character-per-token RNA alphabet, but not
  byte-for-byte the same code path.
- Weight provenance: MultiMolecule states its checkpoint reproduces the
  original's representations; this project did not independently
  re-derive that claim from raw weights (out of scope for this
  migration) and instead relies on MultiMolecule's own published
  verification plus both implementations loading from the same
  original architecture family.
