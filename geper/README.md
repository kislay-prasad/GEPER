# Bij AI — Genetic Evaluation & Prediction Engine

Bij AI integrates five pretrained genomic/proteomic foundation models,
AlphaMissense missense-pathogenicity scoring, MMSplice splice-effect
prediction, NCBI BLAST+, ClinVar, and dbSNP into one production-ready
pipeline that takes a VCF file and produces a unified JSON result and
a human-readable report per variant.

**Scope note:** Bij AI integrates existing pretrained models as-is. No
model is retrained or fine-tuned — HyenaDNA, RNA-FM, and ESM-2 are used
exactly as published by their respective authors, and Evo 2 is loaded
via Arc Institute's own official `evo2` package rather than a custom
re-implementation (see "1. Architecture" for why it isn't a
`transformers.AutoModel` checkpoint like the others). DNABERT-2 was
Bij AI's original DNA-model default and has since been removed from
this pipeline entirely — HyenaDNA is now the universal default (see
"7. Routing logic" and `LICENSE_AUDIT.md`).
AlphaMissense is integrated as an indexed lookup against DeepMind's
precomputed prediction catalogue rather than a loaded model (see "11.
AlphaMissense" for why, and read its licensing notes before any
commercial deployment).

---

## 1. Architecture

```
geper/
├── main.py                       # CLI entry point
├── config.py                     # All model IDs, API endpoints, routing thresholds
├── config.yaml.example            # Optional YAML config overlay (copy to config.yaml to use)
├── requirements.txt
├── GEPER_Colab.ipynb              # End-to-end Colab notebook
│
├── models/                       # One class per pretrained model, all independent
│   ├── base_model.py              # Abstract base: device detection, caching, timing
│   ├── hyenadna.py                 # HyenaDNA (universal default DNA context, up to 450kb)
│   ├── evo2.py                     # Evo 2 7B, StripedHyena-2 (complex/multi-species context)
│   ├── rna_fm.py                    # RNA-FM (RNA sequence embedding)
│   ├── esm2.py                      # ESM-2 650M (protein sequence embedding)
│   ├── alphamissense.py             # AlphaMissense (missense pathogenicity, catalogue lookup)
│   └── __init__.py                 # MODEL_REGISTRY — add new models here
│
├── pipeline/
│   ├── models/
│   │   └── mmsplice/                # MMSplice (splice-effect prediction) -- see "12. MMSplice"
│   │       ├── models.py             #   typed dataclasses (ExonAnnotation, ModularScores, ...)
│   │       ├── utils.py              #   pure math: encoding, SeqSplitter, delta_logit_psi, eligibility
│   │       ├── cache.py              #   prediction-result LRU cache
│   │       ├── loader.py             #   MMSpliceModel(BaseGenomicModel): loads the 5 Keras submodels once
│   │       ├── predictor.py          #   ref/alt scoring, delta computation, cryptic-site scan
│   │       └── service.py            #   exon lookup, eligibility, windows, caching, interpretation
│   ├── vcf_parser.py               # Dependency-free VCF parsing (no pysam/cyvcf2)
│   ├── sequence_context.py         # Fetches reference context (Ensembl REST), builds ref/alt windows
│   ├── router.py                   # Chooses HyenaDNA / Evo 2 / AlphaMissense eligibility
│   ├── rna_generator.py            # DNA -> RNA transcription
│   ├── protein_translator.py       # RNA -> protein translation (standard codon table)
│   ├── interpretation.py           # Merges all evidence into one candidate interpretation summary
│   └── orchestrator.py             # GeperPipeline — runs every stage in order
│
├── database/
│   ├── blast_client.py             # NCBI BLAST+ (remote qblast or local blastn)
│   ├── clinvar_client.py           # ClinVar via NCBI E-utilities
│   └── dbsnp_client.py             # dbSNP via NCBI E-utilities + Variation Services
│
├── report/
│   ├── json_builder.py             # Assembles the unified JSON document
│   └── report_generator.py         # Renders a Markdown report from the JSON
│
└── tests/
    └── __init__.py
```

## 2. Pipeline flow

```
VCF file
  -> VCFParser                          (pipeline/vcf_parser.py)
  -> per variant: chrom, pos, REF, ALT
  -> SequenceContextGenerator            (pipeline/sequence_context.py)
  -> BLASTClient.search()                 (cached/batched; see "8. External data sources")
  -> SequenceRouter.route()              (pipeline/router.py)
       -> HyenaDNA     (universal default: standard AND long context, >= 10kb)
       -> Evo 2         (complex/structural/multi-species context)
  -> RNAGenerator (if transcript-relevant) -> RNA-FM
  -> ProteinTranslator (if coding)         -> ESM-2
  -> SequenceRouter.is_missense_eligible() -> AlphaMissense (missense SNVs only)
  -> MMSpliceService.predict()            -> MMSplice (splice-window-eligible variants only)
  -> ClinVarClient.query_variant()
  -> DbSNPClient.lookup_variant()
  -> InterpretationEngine.interpret()     (merges everything into a candidate summary for review)
  -> JSONResultBuilder -> geper_results.json
  -> ReportGenerator   -> geper_report.md
```

Orchestrated end-to-end by `pipeline/orchestrator.py::GeperPipeline.run()`.

## 3. Design principles enforced in this codebase

- **OOP, one class per model.** Every model in `models/` extends
  `BaseGenomicModel` and only knows about itself — no model class
  imports another model class.
- **Load once, cache always.** `utils/model_cache.py` is a
  process-wide, thread-safe singleton registry. `BaseGenomicModel.load()`
  routes every load through it, so a model's weights are read from disk
  exactly once per process regardless of how many variants are processed.
- **GPU auto-detection with CPU fallback.** `utils/device_utils.py`
  resolves CUDA -> Apple MPS -> CPU once per process and every model
  uses the same resolved device.
- **Graceful degradation.** A failed BLAST/ClinVar/dbSNP call or a
  failed model inference for one variant is caught, logged, and
  recorded in that variant's `errors` list — it never aborts the rest
  of the run. Only a fatally malformed input VCF stops the pipeline
  entirely.
- **No duplicated logic.** Shared behavior (retry/backoff for external
  APIs, mean-pooling for embeddings, logging setup) lives in one place
  and is reused by every model/client.
- **Additive extension.** Adding model #6 requires: (1) a new file in
  `models/` extending `BaseGenomicModel`, (2) one line in
  `models/__init__.py::MODEL_REGISTRY`. No other file changes. This
  holds even when "model" means something other than a loaded neural
  network with GPU weights — AlphaMissense (see "12. AlphaMissense")
  is a `BaseGenomicModel` subclass backed by an indexed lookup table,
  added the same way, participating in the same load/cache/startup-
  validation/graceful-degradation machinery with zero changes to that
  machinery itself.

## 4. Adding a new pretrained model (example)

```python
# models/my_new_model.py
from models.base_model import BaseGenomicModel

class MyNewModel(BaseGenomicModel):
    def cache_key(self) -> str:
        return "my_new_model"

    def _load_impl(self):
        # load self.tokenizer / self.model, move to self.device
        ...

    def _infer_impl(self, sequence: str, **kwargs) -> dict:
        # run inference, return a result dict
        ...
```

```python
# models/__init__.py
from models.my_new_model import MyNewModel
MODEL_REGISTRY["my_new_model"] = MyNewModel
```

To route variants to it, add a rule in `pipeline/router.py::SequenceRouter.route()`.

## 5. Installation

```bash
pip install -r requirements.txt

# HyenaDNA is not distributed on PyPI; install from source:
git clone https://github.com/HazyResearch/hyena-dna.git
export PYTHONPATH="$PYTHONPATH:$(pwd)/hyena-dna"
# then download the checkpoint you need (see that repo's README) into ./checkpoints

# MMSplice: tensorflow is installed above via requirements.txt; the
# mmsplice package itself (its bundled .h5 weight files only -- see
# "12. MMSplice") is auto-installed with --no-deps the first time it's
# needed. To pre-provision it instead (e.g. in a Docker build step):
pip install "mmsplice==2.4.0" --no-deps

# Optional: enables config.yaml as an alternative to environment
# variables (see "10. Environment variables" and "14. config.yaml").
pip install pyyaml
```

For Colab, use `GEPER_Colab.ipynb`, which handles all of the above.

**Setup gotcha: a bare `pip install -r requirements.txt` is not
sufficient to bring geper/ up end-to-end.** Three manual steps are
required beyond that single command, and skipping any of them fails
the pipeline at *startup* — a `ModuleNotFoundError` raised before any
model-specific code runs, not a failure isolated to whichever model
needed the missing package — because `pipeline/orchestrator.py`
unconditionally imports `pipeline/uniprot/bootstrap.py` at module load
time, which itself imports `Bio` (biopython):

1. **torch/torchvision/torchaudio must be installed as the exact pinned
   trio, not individually.** `torch==2.7.1` is pinned specifically
   because `torchvision==0.22.1` and `torchaudio==2.7.1` (both pinned
   further down in `requirements.txt`) are only guaranteed compatible
   with that exact torch release per PyTorch's own compatibility
   matrix. Picking up a different torch build separately (e.g. an
   unpinned `pip install torch` pulling in a newer CPU wheel) can leave
   torchvision/torchaudio silently mismatched, or skipped outright. See
   "18. Environment verification & troubleshooting" for the symptom
   this produces (`undefined symbol` at import time) if missed.
2. **Evo 2 needs a manual, CUDA-toolkit-matched install** — see the
   comment block directly above the `evo2>=0.6.0` line in
   `requirements.txt` for the exact commands (Arc Institute's own
   documented torch+CUDA+flash-attn sequence). It cannot be safely
   pip-installed via the bare requirements file in a fresh or CPU-only
   environment.
3. **tensorflow must be installed before `mmsplice==2.4.0 --no-deps`**,
   not interchangeably — see the comment block above the `tensorflow`
   line in `requirements.txt` for why the ordering matters.

**A verified, working install recipe following exactly this sequence**
(Python 3.12.10, step-by-step install order, and the full ground-truth
`pip freeze`) is recorded in `VENV_BUILD_RECIPE.md` — the first time
this project has had a from-scratch build proven to resolve cleanly
and import successfully (`evo2` excluded by design; see that file for
what is and isn't proven by it).

## 6. Usage

```bash
# Quick sanity check against a huge VCF before committing to a full run
python main.py --vcf sample.vcf.gz --max-variants 20

# Full run
python main.py --vcf sample.vcf --output-dir ./geper_output

# Full run with patient-observed phenotypes, so ACMG's PP4 rule can
# actually evaluate (see "--hpo-terms" / "--phenotype-file" below) --
# real Marfan-syndrome-associated HPO terms (Arachnodactyly, Tall
# stature) supplied directly on the command line:
python main.py --vcf sample.vcf --output-dir ./geper_output \
    --hpo-terms "HP:0001166,HP:0000098"

# Same, but from a file (more practical when a clinician has several
# observed phenotypes to enter):
python main.py --vcf sample.vcf --output-dir ./geper_output \
    --phenotype-file patient_phenotypes.txt
```

Options:

| Flag | Description | Default |
|---|---|---|
| `--vcf` | Path to input VCF (`.vcf` or `.vcf.gz`) | required |
| `--output-dir` | Where to write `geper_results.json` / `geper_report.md` | `./geper_output` |
| `--blast-mode` | `auto` (prefer local blastn+DB, fall back to remote, skip gracefully if neither available), `remote` (NCBI-hosted), or `local` (blastn binary + DB) | `auto` (or `GEPER_BLAST_MODE`) |
| `--blast-db` | Path/prefix of a local BLAST database -- the default production backend (enables the `auto`/`local` fast path) | — (or `GEPER_BLAST_DATABASE`) |
| `--blast-reference-fasta` | FASTA reference to auto-build a local BLAST database from via `makeblastdb` (skipped if a database already exists at `--blast-db`) | — (or `GEPER_BLAST_REFERENCE_FASTA`) |
| `--ai-only` | Disable BLAST entirely; rely only on the DNA/RNA/protein models + ClinVar/dbSNP/AlphaMissense | off |
| `--no-blast-cache` | Disable the persistent, cross-run on-disk BLAST result cache | cache enabled |
| `--no-profiling` | Disable per-stage wall-clock profiling / skip `geper_benchmark.json`,`.md` | profiling enabled |
| `--species` | Ensembl species name for reference sequence lookup | `human` |
| `--assembly` | Genome assembly / coord system version, e.g. `GRCh38`. If omitted, Bij AI auto-detects the build from the VCF header; if supplied *and* it definitely disagrees with what the header declares, the run stops with an explanatory error rather than silently fetching reference sequence from the wrong build. | Ensembl default / auto-detected |
| `--max-variants` | Stop after processing this many variant records. Uses the streaming parser, so the rest of the file is never even read. Intended for debugging against large VCFs. | unlimited |
| `--no-resume` | By default, if `geper_results.json` already exists in `--output-dir`, Bij AI skips variants already recorded there and continues where a previous run (e.g. before a Colab disconnect) left off. Pass this to force a from-scratch run instead. | resume enabled |
| `--hpo-terms` | Comma-separated patient-observed HPO phenotype term IDs, e.g. `"HP:0001166,HP:0002011"`, evaluated against each variant's gene via the HPO gene-to-phenotype dataset for ACMG's PP4 rule (see "21. HPO" below). Malformed IDs (anything not matching `HP:#######`) are logged as a warning and skipped, never fatal. Combines with `--phenotype-file` if both are given. | — (PP4 stays `not_evaluated`) |
| `--phenotype-file` | Path to a file with patient-observed HPO terms for PP4: either a plain text file with one `HP:#######` ID per line, or a JSON file containing a list of HPO ID strings. Alternative/addition to `--hpo-terms` for real clinical use where several observed phenotypes need to be entered at once. A missing/unreadable/malformed file is logged as a warning, never fatal. | — (PP4 stays `not_evaluated`) |

Every run ends with a summary in the log:

```
===== GEPER Run Summary =====

Loaded:
  ✓ RNA-FM
  ✓ ESM2
  ✗ HyenaDNA (missing / not installed)
  – Evo2 (not invoked -- no variant routed to it)

Processed variants: 20
Successful predictions: 18
Skipped: 2
Failed: 0
==============================
```

Programmatic usage:

```python
from pipeline.orchestrator import GeperPipeline

pipeline = GeperPipeline(blast_mode="auto", output_dir="./out")
result = pipeline.run("sample.vcf", max_variants=20, resume=True)
```

No single model failing -- a missing optional dependency (HyenaDNA), an oversized
sequence, a CUDA error, a network hiccup against Ensembl/ClinVar/dbSNP -- ever
terminates the run. Each variant's `errors` list in the JSON output records exactly
what happened for that variant; the run itself always finishes and writes a report.

## 7. Routing logic

| Condition | Model selected |
|---|---|
| Sequence context length ≥ 10,000 bp | HyenaDNA |
| Variant flagged structural (`SVTYPE`), tagged repeat/splice-region, or a long MNV (≥20bp) | Evo 2 |
| Otherwise (typical SNV/indel, short window) | HyenaDNA (universal default) |
| A clean, single-residue missense SNV (see below) | AlphaMissense (in addition to the above) |

A variant can be routed to more than one model when multiple
conditions apply; the orchestrator runs each selected model and
merges their embeddings into `dna_model_results` in the JSON output.
Thresholds live in `config.py::RoutingConfig` and can be tuned without
touching any pipeline logic.

**AlphaMissense eligibility** (`SequenceRouter.is_missense_eligible`,
evaluated after the ClinGen + transcript-structure stages,
independently of the DNA-model routing table above) requires *all* of:
- the VCF record is a single-base SNV (not an insertion/deletion/MNV),
- it isn't a symbolic (`<DEL>`, `*`) or `SVTYPE`-flagged ALT allele,
- the transcript-CDS-frame consequence classifier
  (`pipeline/pvs1/utils.py::protein_effect_flags` — the same one
  BP1/BP7 use) determines the variant's consequence at all (needs a
  fetched transcript structure with CDS sequence covering this
  position; otherwise the variant is treated as
  non-coding/intronic/undeterminable for this purpose, and
  AlphaMissense is skipped rather than guessed at), and
- that classification is exactly missense (excludes synonymous,
  nonsense/stop-loss, and frameshift/in-frame-indel changes).

Anything else (synonymous, intronic, splice-only, frameshift,
structural, or a consequence the transcript data couldn't determine)
is never sent to AlphaMissense. This gate used to read
`pipeline/protein_translator.py`'s frame-unaware local-window
translation (ref/alt protein strings); that window has no splicing and
no reverse-complement step, so it silently misclassified real missense
variants (e.g. it read as "no change" for real BRCA1/TP53/PRNP
missense substitutions verified live on 2026-07-31) — never routing
them to AlphaMissense at all. The transcript-CDS-frame classifier
fixes this.

## 8. External data sources

- **Reference sequence:** Ensembl REST API (`/sequence/region`) — no
  local reference FASTA required.
- **BLAST:** local `blastn` + a local database is the default,
  preferred production backend (`auto`/`local` mode, see "20. Local
  BLAST+ deployment" below); NCBI-hosted `qblast` via Biopython
  (`remote` mode) is the automatic fallback when no local database is
  configured, and BLAST is skipped gracefully (not a hard failure) if
  neither backend is usable.
- **ClinVar / dbSNP:** NCBI E-utilities (`esearch`/`esummary`) plus the
  NCBI Variation Services REST API for richer dbSNP detail. Set
  `GEPER_NCBI_EMAIL` and optionally `GEPER_NCBI_API_KEY` as environment
  variables for polite usage / higher rate limits.
- **AlphaMissense:** Google DeepMind's precomputed prediction
  catalogue, hosted on Google Cloud Storage, queried by genomic
  coordinate via `tabix`. See "11. AlphaMissense" below — this is
  fundamentally different from the other three (no API calls, no
  weight downloads; an indexed-file lookup) and has its own section
  because of that, plus a licensing caveat that matters if you plan to
  deploy commercially.
- **gnomAD:** population allele-frequency evidence, from either a
  locally-provisioned, tabix-indexed gnomAD sites VCF (fast, no
  network) or gnomAD's public GraphQL API as an automatic fallback.
  See "16. gnomAD" below.
- **ClinGen:** gene-level clinical evidence (gene-disease clinical
  validity, dosage sensitivity, actionability), from either
  locally-provisioned curated-download files (fast, no network) or
  ClinGen's live API as an automatic fallback. See "17. ClinGen"
  below.
- **UniProt:** reviewed (Swiss-Prot) protein function, disease
  relevance, and sequence features, from either a locally-provisioned
  JSON-lines dataset or UniProt's public REST API as an automatic
  fallback. See "19. Biological Evidence Layer" below.
- **InterPro/Pfam:** conserved protein domain/family/motif
  annotation, keyed by the UniProt accession the UniProt stage
  resolves, from InterPro's public REST API (which now also serves
  Pfam signature matches). See "19. Biological Evidence Layer" below.
- **AlphaFold DB:** predicted structural reference (model URL/version,
  per-residue pLDDT confidence), keyed by UniProt accession, from
  AlphaFold DB's public prediction API plus its structure-file
  download. See "19. Biological Evidence Layer" below.

All seven network clients (BLAST/ClinVar/dbSNP/gnomAD's GraphQL
fallback/ClinGen's API fallback/UniProt/InterPro/AlphaFold DB) retry
transient failures with exponential backoff and raise a typed error on
exhaustion, which the orchestrator catches per-stage (none of these
ever let that propagate past their respective `Lookup` facade -- see
"16. gnomAD", "17. ClinGen", and "19. Biological Evidence Layer").

## 9. Output format

`geper_results.json`:

```json
{
  "geper_version": "1.0.0",
  "generated_at": "...",
  "input_vcf": "sample.vcf",
  "variant_count": 1,
  "variants": [
    {
      "variant": { "chrom": "...", "pos": ..., "ref": "...", "alt": "...", "variant_type": "SNV" },
      "sequence_context": { "chrom": "...", "window_start": ..., "window_end": ..., "length": ... },
      "dna_model_results": { "dnabert2": { "embedding_mean": [...], "meta": {...} } },
      "rna_analysis": { "embedding_mean": [...], "meta": {...} },
      "protein_analysis": { "translation": {...}, "esm2": {...} },
      "alphamissense": {
        "found": true,
        "genome": "hg38",
        "am_pathogenicity": 0.9871,
        "am_class": "likely_pathogenic",
        "uniprot_id": "P12259",
        "transcript_id": "ENST00000367797",
        "protein_variant": "p.Arg506Gln",
        "skipped": false
      },
      "blast": { "hits": [...], "hit_count": ... },
      "mmsplice": {
        "supported": true,
        "predicted": true,
        "delta_logit_psi": -6.42,
        "donor_score": -5.1,
        "acceptor_score": -0.2,
        "exon_skipping": 0.0,
        "intron_retention": 0.0,
        "alt_donor": 1.3,
        "alt_acceptor": 0.4,
        "confidence": "high",
        "interpretation": "Strong donor site loss.",
        "runtime_ms": 41.2,
        "model_version": "mmsplice-2.4.0 (official pretrained weights)"
      },
      "clinvar": { "found": true, "records": [...] },
      "dbsnp": { "rsid": "rs...", "found": true, "detail": {...} },
      "interpretation": { "summary": "...", "confidence": "moderate", "supporting_evidence": [...] },
      "errors": []
    }
  ]
}
```

For a variant AlphaMissense was never routed to (synonymous, intronic,
frameshift, etc.), `"alphamissense"` is `{"skipped": true, "reason": "..."}`
instead.

`geper_report.md` renders the same content as a readable Markdown
document with a clinical-use disclaimer at the top, including an
"### AlphaMissense" subsection per variant when applicable.

## 10. Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `GEPER_OUTPUT_DIR` | Default output directory | `./geper_output` |
| `GEPER_CACHE_DIR` | Shared root for bootstrapped-dataset caches (ClinGen, HPO, Orphanet, UniProt, Ensembl, MANE, AlphaMissense) and, when set, the fallback root for HyenaDNA/plugin model weights and RNA-FM's `TORCH_HOME` below (F2, report review round 4) -- not HF-specific despite the name; ESM2 also uses it directly via `cache_dir=` | `./model_cache` |
| `GEPER_HYENADNA_CKPT_DIR` | HyenaDNA checkpoint directory. Overrides everything below when set | `./checkpoints`, or `<GEPER_CACHE_DIR>/hyenadna` if `GEPER_CACHE_DIR` is set |
| `GEPER_PLUGIN_CACHE_DIR` | Weight cache for the plugin model family (Enformer, Borzoi, SpliceFormer, SpliceBERT). Overrides everything below when set | `./plugin_model_cache`, or `<GEPER_CACHE_DIR>/plugin_model_cache` if `GEPER_CACHE_DIR` is set |
| `TORCH_HOME` | RNA-FM's checkpoint cache (via `torch.hub`) -- a PyTorch-standard env var, only set automatically by GEPER when `GEPER_CACHE_DIR` is set and `TORCH_HOME` isn't already in the environment | PyTorch's own default (`~/.cache/torch`), or `<GEPER_CACHE_DIR>/torch` if `GEPER_CACHE_DIR` is set and `TORCH_HOME` is unset |
| `GEPER_NCBI_EMAIL` | Email sent to NCBI E-utilities (politeness policy) | placeholder |
| `GEPER_NCBI_API_KEY` | NCBI API key for higher rate limits | unset |
| `GEPER_BLAST_MODE` | BLAST backend priority: `auto` (local first, remote fallback, graceful skip if neither available), `local`, or `remote` | `auto` |
| `GEPER_BLAST_DATABASE` | Path/prefix of a local BLAST database (default production backend). `GEPER_BLAST_LOCAL_DB` and the standard `BLASTDB` variable are still honored for backward compatibility | unset |
| `GEPER_BLAST_REFERENCE_FASTA` | FASTA reference to auto-build a local BLAST database from via `makeblastdb` (only if no database already exists at `GEPER_BLAST_DATABASE`) | unset |
| `GEPER_BLAST_DISK_CACHE` | Enable/disable the persistent, cross-run on-disk BLAST result cache | `true` |
| `GEPER_BLAST_ENABLE_PREFETCH` | Enable/disable up-front concurrent BLAST submission for distinct sequences | `true` |
| `GEPER_BLAST_MAX_CONCURRENT_REMOTE` / `_LOCAL` | Max concurrent distinct-sequence BLAST submissions | `3` / CPU count |
| `GEPER_LOG_DIR` | Log file directory | `./logs` |
| `GEPER_LOG_LEVEL` | Logging level | `INFO` |
| `GEPER_CHECKPOINT_INTERVAL` | Variants between mid-run `geper_results.json` flushes (used for resume) | `25` |
| `GEPER_ENABLE_ALPHAMISSENSE` | Enable/disable the AlphaMissense stage entirely | `true` |
| `GEPER_ALPHAMISSENSE_HG38_URL` | Remote AlphaMissense hg38 catalogue URL | Google Cloud Storage `dm_alphamissense` bucket |
| `GEPER_ALPHAMISSENSE_HG19_URL` | Remote AlphaMissense hg19 catalogue URL | Google Cloud Storage `dm_alphamissense` bucket |
| `GEPER_ALPHAMISSENSE_HG38_LOCAL` | Local pre-downloaded hg38 catalogue path (`.tsv.gz`, needs a sibling `.tbi`) | unset (uses the remote URL) |
| `GEPER_ALPHAMISSENSE_HG19_LOCAL` | Local pre-downloaded hg19 catalogue path | unset (uses the remote URL) |
| `GEPER_TABIX_BINARY` | Path to the `tabix` executable | `tabix` (resolved via `PATH`) |
| `GEPER_ALPHAMISSENSE_TIMEOUT` | Per-query `tabix` timeout, in seconds | `30` |
| `GEPER_ENABLE_MMSPLICE` | Enable/disable the MMSplice stage entirely | `true` |
| `GEPER_MMSPLICE_DEVICE` | `auto` / `cpu` / `cuda` for the 5 Keras submodels | `auto` |
| `GEPER_MMSPLICE_MODEL_DIR` | Local directory with `layers.py` + `models/*.h5` (air-gapped deployments) | unset (auto-detects installed `mmsplice` package) |
| `GEPER_MMSPLICE_VARIANT_TYPES` | Comma-separated variant types MMSplice will score | `SNV,insertion,deletion` |
| `GEPER_MMSPLICE_INTRON_WINDOW` | Max bp into the intron from an exon boundary still considered eligible | `100` |
| `GEPER_MMSPLICE_EXON_WINDOW` | Max bp into the exon from a boundary still considered "near splice" | `50` |
| `GEPER_MMSPLICE_CRYPTIC_SCAN_RANGE` | bp scanned each side for the alt_donor/alt_acceptor cryptic-site scan | `20` |
| `GEPER_MMSPLICE_MODERATE_THRESHOLD` / `_STRONG_THRESHOLD` | `\|delta_logit_psi\|` interpretation thresholds | `2.0` / `5.0` |
| `GEPER_MMSPLICE_SITE_LOSS_THRESHOLD` | Per-site delta magnitude called "site loss" | `2.5` |
| `GEPER_MMSPLICE_EXON_SKIPPING_THRESHOLD` / `_INTRON_RETENTION_THRESHOLD` | Derived-score thresholds | `2.0` / `2.0` |
| `GEPER_MMSPLICE_ACMG_WEIGHT` | MMSplice's weight in the aggregate significance score (`0` excludes it) | `1.0` |
| `GEPER_MMSPLICE_CACHE_ENABLED` / `_CACHE_MAX_SIZE` | Prediction-result LRU cache | `true` / `5000` |
| `GEPER_MMSPLICE_BATCH_SIZE` | Keras batch size for `score_modular_batch` | `16` |
| `GEPER_ENABLE_GNOMAD` | Enable/disable the gnomAD stage entirely | `true` |
| `GEPER_GNOMAD_GRCH38_LOCAL_VCF` / `_GRCH37_LOCAL_VCF` | Local tabix-indexed gnomAD sites VCF path per build | unset (falls through to GraphQL) |
| `GEPER_GNOMAD_TABIX_BINARY` | Path to the `tabix` executable used for the local index | `tabix` |
| `GEPER_GNOMAD_GRAPHQL_ENDPOINT` | gnomAD GraphQL API URL | `https://gnomad.broadinstitute.org/api` |
| `GEPER_GNOMAD_ENABLE_GRAPHQL_FALLBACK` | Allow falling through to the GraphQL API when no local index answers | `true` |
| `GEPER_GNOMAD_OFFLINE` | Never attempt the GraphQL fallback at all (air-gapped deployments) | `false` |
| `GEPER_GNOMAD_TIMEOUT` / `_MAX_RETRIES` / `_RETRY_BACKOFF` | GraphQL request timeout (s) / retry count / backoff multiplier (s) | `30` / `3` / `1.5` |
| `GEPER_GNOMAD_CACHE_ENABLED` / `_CACHE_MAX_SIZE` / `_CACHE_TTL_HOURS` | Lookup-result LRU cache | `true` / `20000` / `6` |
| `GEPER_GNOMAD_CACHE_DISK_PATH` | Optional on-disk cache persistence path (survives process restart) | unset (in-memory only) |
| `GEPER_GNOMAD_MAX_CONCURRENT` | Max concurrent batch/async gnomAD lookups | `8` |
| `GEPER_GNOMAD_BA1_AF` / `_BS1_AF` / `_PM2_AF` | ACMG BA1/BS1/PM2 allele-frequency thresholds | `0.05` / `0.01` / `0.0001` |
| `GEPER_GNOMAD_USE_POPMAX` | Use highest single-population AF (rather than pooled global AF) for BA1/BS1 | `true` |
| `GEPER_ENABLE_CLINGEN` | Enable/disable the ClinGen stage entirely | `true` |
| `GEPER_CLINGEN_GENE_VALIDITY_FILE` / `_DOSAGE_FILE` | Local ClinGen gene-validity / dosage-sensitivity download paths | unset (falls through to the live API) |
| `GEPER_CLINGEN_API_ENDPOINT` | ClinGen live API base URL | `https://search.clinicalgenome.org/api` |
| `GEPER_CLINGEN_API_ENABLED` | Allow falling through to the live API when no local file answers | `true` |
| `GEPER_CLINGEN_OFFLINE` | Never attempt the live API at all (air-gapped deployments) | `false` |
| `GEPER_CLINGEN_TIMEOUT` / `_MAX_RETRIES` / `_RETRY_BACKOFF` | Live API request timeout (s) / retry count / backoff multiplier (s) | `30` / `3` / `1.5` |
| `GEPER_CLINGEN_CACHE_ENABLED` / `_CACHE_MAX_SIZE` / `_CACHE_TTL_HOURS` | Gene-evidence LRU cache | `true` / `5000` / `24` |
| `GEPER_CLINGEN_CACHE_DISK_PATH` | Optional on-disk cache persistence path (survives process restart) | unset (in-memory only) |
| `GEPER_CLINGEN_MAX_CONCURRENT` | Max concurrent batch ClinGen lookups | `8` |
| `GEPER_HEALTH_CHECK_ENABLED` | Enable/disable the startup external-service health probe entirely (see "26. External service health checks") | `true` |
| `GEPER_HEALTH_CHECK_TIMEOUT` | Per-attempt timeout for every probe attempt except the final, latch-deciding one | `4.0` |
| `GEPER_HEALTH_CHECK_ATTEMPTS` | Consecutive failed attempts required before latching a service offline for the run; only network-level failures are retried | `3` |
| `GEPER_HEALTH_CHECK_BACKOFF` | Pause between probe attempts | `0.5` |
| `GEPER_HEALTH_CHECK_CONFIRM_TIMEOUT` | Timeout for the final, latch-deciding attempt only — deliberately matches clients' own request timeout | `30.0` |
| `GEPER_CONFIG_FILE` | Path to an optional `config.yaml` overlay (see "14. config.yaml") | `./config.yaml` |

## 11. AlphaMissense

**Why this is a lookup, not a loaded model.** Google DeepMind has not
released trained AlphaMissense model weights ("What we don't provide:
The trained AlphaMissense model weights" —
github.com/google-deepmind/alphamissense). What they publish instead
is a precomputed catalogue of pathogenicity scores for essentially
every possible human missense substitution (~71M rows, hg19 and
hg38), as a bgzip'd, tabix-indexed TSV — the same file the official
Ensembl VEP AlphaMissense plugin queries. `models/alphamissense.py`
integrates against that catalogue via the `tabix` CLI, matched by
genomic coordinate (`chrom`, `pos`, `ref`, `alt`), rather than running
a forward pass. It still participates in every piece of Bij AI's model
lifecycle (`BaseGenomicModel`, `ModelCache`, startup validation,
`is_available()`-gated graceful degradation, the run summary) exactly
like the five embedding models — that machinery only assumes
`cache_key()` / `_load_impl()` / `_infer_impl()`, not "has GPU
weights".

**Setup.** Install `tabix` (htslib) — a common bioinformatics CLI, not
a pip package:

```bash
apt-get install tabix
# or
conda install -c bioconda htslib
```

**This is the only setup step.** Bij AI does **not** need, use, or
import Google DeepMind's `alphamissense` PyPI/GitHub package (the one
installed via `pip install -e .` from
github.com/google-deepmind/alphamissense) — that package is DeepMind's
own *inference-pipeline reference implementation*, for a model whose
weights were never released, so it can't actually be used to make new
predictions anyway. `models/alphamissense.py` never imports a package
named `alphamissense`; if you see a `ModuleNotFoundError` mentioning
that name, it isn't coming from this file, and installing DeepMind's
package will not fix it — check that the `geper/` project directory
itself is on `sys.path` (e.g. `sys.path.append('/content/geper')` as
the Colab notebook's setup cell does) and that you're running scripts
from within it.

Nothing else is required by default, but note what "default" actually
does: Google's GCS bucket has no `.tbi` sidecar index next to the
catalogue file, so indexed HTTP range queries against the remote file
aren't possible. `_load_impl()` resolves the public GCS catalogue URLs
(`GEPER_ALPHAMISSENSE_HG38_URL` / `_HG19_URL`, both pre-set to the
official bucket) and, the first time each genome build is needed,
**streams the full ~9GB `AlphaMissense_<build>.tsv.gz` down once**,
caching it under `GEPER_ALPHAMISSENSE_CACHE_SUBDIR` inside GEPER's
cache directory, then builds a local `tabix` index over that cached
copy. Every subsequent run reuses the cached file + index without
re-downloading, but the first run for a given genome build does pay a
one-time multi-GB download and index-build cost — plan disk space and
first-run time accordingly. Set `GEPER_ALPHAMISSENSE_AUTO_DOWNLOAD=false`
to disable this and require a local file instead (fails fast rather
than downloading if none is configured). For air-gapped deployments,
very high-throughput runs, or to avoid the first-run download
entirely, pre-download `AlphaMissense_hg38.tsv.gz` / `_hg19.tsv.gz`
yourself, build (or fetch) a `.tbi` index for each, and point
`GEPER_ALPHAMISSENSE_HG38_LOCAL` / `_HG19_LOCAL` at them; GEPER then
never touches the network for AlphaMissense at all.

**Enable/disable:** set `GEPER_ENABLE_ALPHAMISSENSE=false` to turn the
stage off entirely (it's then reported the same way a missing optional
dependency like HyenaDNA-without-git is: one clear startup warning,
never a crash, and every variant's `"alphamissense"` field is a
`{"skipped": true, ...}` record).

**Routing:** see "7. Routing logic" above for the exact eligibility
rule — only clean, single-residue missense SNVs are routed;
synonymous, nonsense, frameshift, intronic/splice, and structural
variants never are.

**Startup validation table:** AlphaMissense gets its own row, same as
every embedding model, but with values that reflect what's actually
happening — there's no tensor computation, so `Device` is always `cpu`
(no GPU is ever used, regardless of what's available) and `Precision`
reads `n/a (lookup table, no weights)` rather than a fabricated torch
dtype:

```
Model                   Device    Precision       Max Length  Status
--------------------------------------------------------------------
HyenaDNA                 cuda      torch.float32   450000      PASS
Evo2                      cuda      torch.bfloat16 (evo2_7b_base, StripedHyena-2)  8192  PASS
RNA-FM                   cuda      torch.float32   1024        PASS
ESM2                     cuda      torch.float32   1024        PASS
AlphaMissense             cpu      n/a (lookup table, no weights)  n/a  PASS
```

**Licensing — read before commercial deployment.** Sources disagree.
The current official repository (github.com/google-deepmind/alphamissense,
archived 2025-05-16) states the predictions are CC BY 4.0 (attribution
only, commercial use permitted). However, the Google Cloud Storage
download page, the Ensembl VEP plugin docs, the EBI announcement, and
the HuggingFace dataset mirror all instead describe the predictions as
CC BY-NC-SA 4.0 — non-commercial research use only — and note that use
of the GCS-hosted files is additionally subject to the Google Cloud
Platform Terms of Service. Bij AI does not know which currently governs
the specific file your deployment downloads; this is a genuine,
unresolved discrepancy between DeepMind's own sources, not a Bij AI
judgment call. **Confirm the license that applies to the exact file
you download directly with Google DeepMind (alphamissense@google.com)
or counsel before relying on AlphaMissense output in a commercial
product.** (Full detail: `config.py::AlphaMissenseConfig` docstring.)

**Testing.** `verify_alphamissense_integration.py` exercises the real,
unmodified `AlphaMissenseModel` code — real `tabix` subprocess calls
against a real local bgzip+tabix-indexed catalogue file (built with
the exact real AlphaMissense schema) — end to end, alongside a full
orchestrator run using the same known ClinVar missense variants as
`verify_clinvar_dbsnp_fix.py`. See that script's module docstring for
exactly what is and isn't proved without real network access to the
HyenaDNA/etc. weights and the live GCS catalogue.

## 12. MMSplice

**What it adds.** Splice-effect prediction (`pipeline/models/mmsplice/`):
for eligible SNVs/insertions/deletions near an exon boundary, scores
donor/acceptor site strength, exon inclusion, and intron retention
signal for the reference vs. alt allele, and derives a
`delta_logit_psi` effect size plus a plain-English interpretation
("Strong donor site loss.", "Likely exon skipping.", etc.).

**Why this isn't `pip install mmsplice`.** The official `mmsplice`
PyPI package pins `cyvcf2<=0.30.15`, which cannot be built on Python
3.12+ (no prebuilt wheel for that pin, and the pinned version's C
extension uses a CPython-internal struct field removed in 3.12) — this
was confirmed directly against this project's own Python 3.12
toolchain; `pip install mmsplice` fails outright. Since `mmsplice`'s
own `__init__.py` unconditionally imports that whole
kipoiseq/kipoi/pyranges/cyvcf2 chain (even though Bij AI never uses the
VCF-dataloader functionality those packages exist for), this
integration instead:

1. Installs `mmsplice` with `--no-deps` (skipping the unbuildable pin
   entirely), purely to obtain the package's bundled **official**
   pretrained Keras `.h5` weight files.
2. Loads `mmsplice/layers.py` directly by file path
   (`importlib.util.spec_from_file_location`), bypassing
   `mmsplice/__init__.py` entirely so the kipoiseq/cyvcf2 chain is
   never touched. `layers.py` itself only imports
   tensorflow/scipy/numpy.
3. Loads the five official `.h5` files with
   `tensorflow.keras.models.load_model`.
4. Reimplements (ported faithfully, with attribution) the small amount
   of pure-numpy score-combination math (`delta_logit_psi`'s published
   linear model, sequence splitting into the five modular windows)
   directly in `pipeline/models/mmsplice/utils.py`, so nothing in this
   integration ever imports the `mmsplice` package's own Python code.

Every prediction still uses the real, official, unmodified MMSplice
network weights (Cheng et al. 2019, *Genome Biology*, "MMSplice:
modular modeling improves the predictions of genetic variant effects
on splicing") — no retraining, no approximated weights, no mock
outputs anywhere in this path. Full rationale, verified line-by-line
against mmsplice 2.4.0's published source: see the module docstrings
in `pipeline/models/mmsplice/loader.py` and `utils.py`.

**Module layout:**

```
pipeline/models/mmsplice/
├── models.py       # Typed dataclasses: ExonAnnotation, SpliceWindow, ModularScores, etc.
├── utils.py        # One-hot encoding, SeqSplitter, delta_logit_psi math, eligibility/region
│                    #   classification, interpretation text generation -- all pure functions
├── cache.py         # Bounded LRU cache of finished prediction results (per chrom/pos/ref/alt/transcript)
├── loader.py        # MMSpliceModel(BaseGenomicModel): loads/caches the 5 Keras submodels once
├── predictor.py      # MMSplicePredictor: ref/alt scoring, delta computation, cryptic-site scan
└── service.py        # MMSpliceService: exon lookup (Ensembl REST), eligibility, window
                       #   construction, caching, interpretation -- the orchestrator's entry point
```

`MMSpliceModel` is also registered into `pipeline/orchestrator.py`'s
own extended `MODEL_REGISTRY` (not `models/__init__.py`'s — see the
long comment there for why: importing `pipeline.models.mmsplice.loader`
from inside the `models` package's own `__init__.py` creates a real
import cycle, since that loader needs `models.base_model`). MMSplice
therefore still participates in the same load-once/cache/startup-
validation/graceful-degradation machinery as every other model, with
zero changes to that shared machinery itself.

**Eligibility (requirement-driven, fully configurable).** A variant is
scored only if both of these hold:

- its `variant_type` (SNV / insertion / deletion by default; MNVs
  excluded — override via `GEPER_MMSPLICE_VARIANT_TYPES`) is supported, and
- it falls within `GEPER_MMSPLICE_INTRON_WINDOW` bp of an exon boundary
  if intronic, or within `GEPER_MMSPLICE_EXON_WINDOW` bp of a boundary
  (or inside a short exon) if exonic.

Anything else — deep intronic, deep exonic, unsupported type, no
overlapping exon annotation found, an Ensembl fetch failure — is
skipped gracefully with a recorded `skip_reason`; the pipeline
continues (`"mmsplice": {"supported": false, "predicted": false,
"skip_reason": "..."}`), never a hard failure.

**Result schema** (`result["mmsplice"]` in the unified JSON output; see
"9. Output format"):

```json
{
  "supported": true,
  "predicted": true,
  "delta_logit_psi": -6.42,
  "donor_score": -5.1,
  "acceptor_score": -0.2,
  "exon_skipping": 0.0,
  "intron_retention": 0.0,
  "alt_donor": 1.3,
  "alt_acceptor": 0.4,
  "confidence": "high",
  "interpretation": "Strong donor site loss.",
  "interpretation_category": "strong_donor_loss",
  "runtime_ms": 41.2,
  "model_version": "mmsplice-2.4.0 (official pretrained weights)",
  "skip_reason": null
}
```

**ACMG/evidence integration.** `pipeline/interpretation.py` appends one
additional evidence line and adds to the aggregate `significance_score`
when MMSplice produces a prediction — additive only; it never
overwrites or removes anything ClinVar/dbSNP/protein/AlphaMissense
already contributed. Weighted by
`config.py::MMSpliceConfig.ACMG_EVIDENCE_WEIGHT` (default `1.0`; set to
`0` via `GEPER_MMSPLICE_ACMG_WEIGHT=0` to keep MMSplice visible in
reports/API output while excluding it from the scored aggregate).

**Enable/disable:** `GEPER_ENABLE_MMSPLICE=false` turns the stage off
entirely (same graceful, one-warning-then-silent pattern as every
other optional model).

**Caching & batching.** A bounded LRU (`GEPER_MMSPLICE_CACHE_MAX_SIZE`,
default 5000) avoids rescoring the same (variant, transcript) pair
twice in one run. `MMSpliceService.predict_batch()` /
`MMSplicePredictor.predict_batch()` /
`MMSpliceModel.score_modular_batch()` batch the expensive part (the
five Keras forward passes) across many variants in two calls per
module instead of two calls per variant.

**Device:** `GEPER_MMSPLICE_DEVICE` = `auto` (default, prefers GPU if
TensorFlow reports one visible) / `cpu` / `cuda`.

**Testing:** `tests/test_mmsplice_utils.py` (pure math/eligibility/
interpretation logic, no mocking), `tests/test_mmsplice_predictor.py`
(mocked `MMSpliceModel`), `tests/test_mmsplice_service.py` (mocked
Ensembl + predictor), `tests/test_mmsplice_integration.py`
(non-mocked `pipeline/interpretation.py` + `report/json_builder.py`
wiring, backward-compatibility, additive-evidence checks).

## 13. Hardware notes

- **GPU required for Evo 2, strongly recommended overall.** ESM-2
  (650M params) is large; on CPU it will run but slowly. Evo 2 (7B
  params, via the official `evo2` package) has no practical CPU path
  at all — it depends on `flash-attn`, which has no real CPU build —
  so it is skipped entirely (with one clear warning, not a per-variant
  failure) whenever no CUDA GPU is detected. `models/evo2.py` loads it
  in bfloat16, matching Arc Institute's own recommendation for the 7B
  checkpoint.
  **Which 7B checkpoint, and why it matters:** Bij AI defaults to
  `evo2_7b_base` (`GEPER_EVO2_VARIANT`), not the plain `evo2_7b`
  checkpoint some older guides reference. Verified directly against
  Arc Institute's own issue tracker
  ([#208](https://github.com/ArcInstitute/evo2/issues/208), open as of
  2026-03-13): plain `evo2_7b` (their 1M-token long-context release)
  ships with `use_fp8_input_projections=True` baked into its config and
  requires Transformer Engine + FP8-capable (Hopper+) hardware even
  under the documented "light install" path — despite that path being
  described as TE-free. `evo2_7b_base` (8K training context) is the
  checkpoint that's actually usable with just `torch` + `flash-attn`
  and no `transformer-engine`/FP8/Hopper requirement. If you have TE
  installed and Hopper+ hardware and want the longer context instead,
  set `GEPER_EVO2_VARIANT=evo2_7b_262k` or `evo2_7b` (1M) and raise
  `GEPER_EVO2_MAX_SAFE_TOKENS` to match (GEPER does not infer the
  correct ceiling from the variant name automatically).
- **Colab free tier / Tesla T4 (16GB VRAM): Evo 2 does not run at
  all, and this is a hardware ceiling, not a memory-sizing issue.**
  An earlier version of this note said `evo2_7b_base` was merely "a
  tight fit" on a T4 — that was wrong. Corrected against Dao-AILab's
  current `flash-attention` README/PyPI page and Arc Institute's own
  `evo2` install docs (not assumed): `evo2` depends directly on
  FlashAttention-2 (via `vtx`'s StripedHyena-2 attention kernels), and
  FlashAttention-2's official CUDA backend only supports
  **Ampere, Ada, or Hopper GPUs (compute capability >= 8.0)**. The
  Tesla T4 is Turing (compute capability 7.5) — one architecture
  generation below that floor. Turing is covered only by a separate,
  third-party, partial-feature project
  ([flash-attention-turing](https://github.com/ssiu/flash-attention-turing))
  that Arc Institute does not integrate with or support for `evo2`.
  This is why `flash-attn==2.8.0.post2` either fails to build its CUDA
  extension on a T4 at all, or — if an environment somehow already has
  a stray/cached build — leaves the pure-Python `evo2` package
  importable while its actual attention kernel
  (`flash_attn_2_cuda`) is missing, producing exactly the
  `No module named 'flash_attn_2_cuda'` failure some users have hit
  when forcing evo2 onto a T4. No `requirements.txt` pin changes this;
  it is a kernel-support floor, not a version-compatibility bug.
  **What Bij AI does about it:** `models/evo2.py` now checks the GPU's
  compute capability (not just "is a GPU present") before ever
  attempting to load `evo2`, and skips it automatically — with one
  clear, specific log message naming the actual GPU and the compute
  capability shortfall — exactly like it already does when no GPU is
  present at all. HyenaDNA, RNA-FM, ESM-2, and AlphaMissense are
  entirely unaffected and continue to run on a T4; only the subset
  of variants the router flags as structurally/evolutionarily complex
  (which would otherwise route to Evo 2) fall back to HyenaDNA instead,
  via the same graceful-degradation path already used when any other
  model is unavailable. **To actually run Evo 2,**
  use a GPU with compute capability >= 8.0 — e.g. A10, A100, L4, L40S,
  or H100 (Google Colab's paid tiers offer A100/L4; most major cloud
  providers offer A10/A100/L4 on-demand). Sequences longer than
  Bij AI's configured safety ceiling (8,192 tokens by default, matching
  `evo2_7b_base`'s real 8K training context) are truncated with a
  warning rather than crashing, on any GPU where Evo 2 does run.
- **HyenaDNA** is Bij AI's universal default DNA model — invoked both
  for long-context variants (≥10kb) and as the fallback for the
  typical short SNV/indel window, so its checkpoint should be
  downloaded for any real run. If it isn't installed, Bij AI detects
  that once at startup, logs a single warning, and the variant
  proceeds without DNA-model-level embedding evidence rather than
  failing the run.
- **AlphaMissense** runs on CPU unconditionally (it's a file lookup,
  not a GPU computation) and adds negligible overhead — one `tabix`
  subprocess call per eligible missense variant.

## 14. config.yaml

Every Bij AI setting can be set via an environment variable (see "10.
Environment variables"). `config.yaml` (copy `config.yaml.example` to
`config.yaml`) is an optional convenience layer over that same
mechanism — not a second, divergent configuration system: every key in
`config.yaml.example` maps to the exact same `GEPER_*` variable the
corresponding setting already reads. An explicit environment variable
always takes priority over `config.yaml` if both are set. Requires
`pip install pyyaml`; if PyYAML isn't installed, or `config.yaml` is
missing/malformed, Bij AI logs one warning and proceeds with defaults/
environment variables only — it never fails to start because of it.

```bash
cp config.yaml.example config.yaml
# edit config.yaml, e.g.:
#   alphamissense:
#     enabled: true
python main.py --vcf sample.vcf
```

Set `GEPER_CONFIG_FILE=/path/to/other.yaml` to use a different path.

## 15. Clinical disclaimer

Bij AI is a variant prioritisation system built on pretrained machine
learning models and public database lookups. It assists qualified
clinicians and pathologists by producing a draft classification;
qualified human review and final sign-off are required before any
clinical use, and Bij AI does not independently provide final clinical
interpretation. It is **not** a substitute for professional clinical
genetic interpretation, diagnosis, or medical advice.

## 16. gnomAD

Population allele-frequency evidence (Phase 2 of this project's
hardening effort), implemented in `pipeline/gnomad/`. Same
"AI-only-never-blocks-on-this" philosophy as ClinVar/dbSNP/BLAST: a
gnomAD lookup failure is logged and reported per-variant, never a
fatal error for the run.

**Two independent, configurable query sources:**

- **Local indexed database** (`GnomadLookup` -> `LocalIndexedGnomadProvider`):
  queries a locally-provisioned, `tabix`-indexed gnomAD "sites" VCF —
  exactly the file format Broad already publishes (bgzip + `.tbi`
  sidecar), so Bij AI never builds this index itself. Point
  `GEPER_GNOMAD_GRCH38_LOCAL_VCF` / `GEPER_GNOMAD_GRCH37_LOCAL_VCF` at
  a copy you've downloaded (e.g. via `gsutil -m cp` from gnomAD's
  public GCS bucket, or `wget` from https://gnomad.broadinstitute.org/downloads).
  This is the fast, no-network path and is tried first.
- **GraphQL fallback** (`GraphQLGnomadProvider`): automatically used
  when no local index is configured for the variant's build (or the
  local lookup itself errors), querying gnomAD's public GraphQL API
  (`https://gnomad.broadinstitute.org/api`) with the same
  retry/backoff/timeout policy as ClinVar/dbSNP. Set
  `GEPER_GNOMAD_OFFLINE=true` to disable this entirely for air-gapped
  deployments.

**Build handling:** GRCh38 is the default; GRCh37 is used automatically
whenever the pipeline's own resolved assembly (CLI flag or VCF-header
auto-detection, `pipeline/assembly_validator.py`) says so — the same
mechanism AlphaMissense's `resolve_genome_label` already uses, so all
build-aware stages agree.

**Evidence retrieved:** genome AF, exome AF, allele count/number,
homozygote count, hemizygote count (X/Y non-PAR only), and a
per-population breakdown across gnomAD's 7 continental populations
(AFR/AMR/ASJ/EAS/FIN/NFE/SAS) plus a "remaining" bucket.

**ACMG contribution** (`pipeline/interpretation.py::InterpretationEngine._gnomad_acmg_evidence`),
additive only — never overwrites evidence any other stage already
contributed:

| Criterion | Meaning | Default threshold | Env var |
|---|---|---|---|
| BA1 | Stand-alone benign (too common) | AF ≥ 5% | `GEPER_GNOMAD_BA1_AF` |
| BS1 | Strong benign (more common than expected) | AF ≥ 1% | `GEPER_GNOMAD_BS1_AF` |
| PM2 | Moderate pathogenic (absent / extremely rare) | AF ≤ 0.01%, or absent from gnomAD entirely | `GEPER_GNOMAD_PM2_AF` |

BA1/BS1 use the single highest population frequency ("popmax") by
default (`GEPER_GNOMAD_USE_POPMAX=true`, ACMG/AMP's own recommended
approach), not the pooled global AF — a variant common in one
population but rare overall should still trigger BA1/BS1. PM2 always
uses the pooled global AF (its criterion is about overall rarity, not
population-specific).

**Performance:** results are cached (in-memory LRU with a TTL, plus an
optional on-disk JSON-lines persistence layer via
`GEPER_GNOMAD_CACHE_DISK_PATH` for surviving a Colab restart); batch
lookups run thread-pooled (`GnomadLookup.query_variants_batch`) or via
`asyncio` (`GnomadLookup.async_query_variants_batch`) for callers
already inside an event loop. See `benchmark_gnomad.py` for a real,
reproducible measurement of the local-index + cache + concurrency code
paths (this sandbox cannot reach gnomAD's live GraphQL API to
benchmark that path — run it yourself against a live network for that
number).

**Report/API:** every variant's Markdown report gets a dedicated
"### gnomAD (Population Frequency)" panel (global/genome/exome AF,
highest population, a per-population table); the JSON output gets a
new, additive `"gnomad"` key per variant (see "9. Output format") —
omitting it entirely from a `build_variant_result` call (as any
pre-Phase-2 caller would) still produces a complete, valid record.

## 17. ClinGen

Gene-level clinical evidence — gene-disease clinical validity, dosage
sensitivity (haploinsufficiency/triplosensitivity), and clinical
actionability — implemented in `pipeline/clingen/`. Same
never-blocks-the-pipeline philosophy as every other evidence source
here: a ClinGen lookup failure is logged and reported per-variant,
never fatal.

**Why gene-level, not variant-level:** unlike ClinVar/dbSNP/gnomAD,
ClinGen's curation (https://clinicalgenome.org) is fundamentally about
a *gene* (or a gene-disease pair), not a specific variant. So before
any ClinGen evidence can be fetched, a variant's overlapping gene
symbol is first resolved via an Ensembl `overlap/region` (`feature=gene`)
lookup — the same REST endpoint/library MMSplice's exon annotation
already uses (`pipeline/models/mmsplice/service.py`), just with
`feature=gene` instead of `feature=exon`. That resolution is memoized
per genomic position for the life of a run.

**Two independent, configurable query sources**, mirroring gnomAD's
local-first/API-fallback shape:

- **Local curated-dataset files** (`ClinGenLookup` ->
  `LocalDatasetClinGenProvider`): ClinGen's own published, versioned
  Gene-Disease Validity and Dosage Sensitivity flat-file downloads
  (https://search.clinicalgenome.org/kb/gene-validity and
  ftp.clinicalgenome.org's dosage TSV). By default Bij AI **auto-fetches
  both files for you** (`pipeline/clingen/bootstrap.py`, same
  auto-bootstrap pattern as HPO/Orphanet below) — gated by
  `GEPER_CLINGEN_AUTO_FETCH` (default `true`), cached under
  `GEPER_CLINGEN_AUTO_FETCH_DIR`, and refreshed on a
  `GEPER_CLINGEN_AUTO_FETCH_TTL_HOURS`-hour TTL (default 24h; a failed
  refresh falls back to the last good cached copy rather than nothing).
  This is the primary, most reliable source — gene curation changes on
  the order of weeks/months, so a periodically-refreshed local copy is
  both faster and more robust than a live call for routine annotation.
  `GEPER_CLINGEN_GENE_VALIDITY_FILE` / `GEPER_CLINGEN_DOSAGE_FILE`
  remain available as a separate, higher-priority manual override —
  set either to point at your own file and auto-fetch is skipped for
  that file — for air-gapped deployments or a pinned/vetted snapshot.
- **Live API fallback** (`LiveAPIClinGenProvider`): used automatically
  for a gene not present in the local dataset (or when no local
  dataset is configured), querying `GEPER_CLINGEN_API_ENDPOINT`
  (default `https://search.clinicalgenome.org/api`) with the same
  retry/backoff/timeout policy as every other external client here.
  Set `GEPER_CLINGEN_OFFLINE=true` to disable this entirely for
  air-gapped deployments, or `GEPER_CLINGEN_API_ENABLED=false` to
  disable just the live fallback while keeping the local dataset.
  **Known limitation:** this sandbox has no network route to
  `clinicalgenome.org` (confirmed directly — a request returns HTTP
  403 with `x-deny-reason: host_not_allowed` from the egress proxy, the
  same situation `benchmark_gnomad.py` documents for gnomAD's live
  GraphQL API), so this fallback's exact endpoint path and JSON
  response schema could not be exercised against a live call during
  development. `pipeline/clingen/provider.py`'s module docstring and
  `_evidence_from_api_payload`'s docstring both flag this; re-verify
  against ClinGen's current API documentation before relying on this
  fallback in production, or use the local-dataset path (which *is*
  exercised end-to-end by `tests/test_clingen_provider.py` against
  `testdata/clingen_fixture/`) exclusively until then.

**Evidence retrieved:** one or more gene-disease clinical validity
curations per gene (disease, MONDO ID, classification, mode of
inheritance, curating GCEP, classification date), dosage sensitivity
(haploinsufficiency and triplosensitivity scores + labels), and
clinical actionability (adult/pediatric scores) when the live API
returns them.

**ACMG contribution** (`pipeline/interpretation.py::InterpretationEngine._clingen_acmg_evidence`),
additive only — never overwrites evidence any other stage already
contributed:

| Signal | Meaning | Contribution |
|---|---|---|
| Sufficient haploinsufficiency evidence + predicted LOF variant | Gene-level prerequisite ACMG/AMP's PVS1 rule requires | PVS1-supporting evidence, positive weight |
| "Dosage sensitivity unlikely" + predicted LOF variant | Caution against a naive PVS1 application | Cautionary note, small negative weight (never reverses the underlying LOF evidence to benign) |
| "Gene associated with autosomal recessive phenotype" + predicted LOF variant | ClinGen's curation does not establish that one damaged allele is sufficient, so there is no PVS1 gene-level support — but the gene *is* curated, which is not the same as having no curation | Disclosure only, weight `0.0` (never changes the classification; LOF may still be the mechanism biallelically) |
| Definitive/Strong gene-disease clinical validity | Established disease mechanism for this gene | PP5/BP6-style supporting evidence |
| Disputed/Refuted gene-disease clinical validity | Contrary evidence for this gene's disease association | Argues against a causal role, negative weight |

The haploinsufficiency scores that count as sufficient evidence are
**not** configurable, and deliberately so. Every other ACMG threshold in
this codebase (CADD, REVEL, pLI, LOEUF, oe_mis) is a *continuous* score,
where a deployer-tunable cut-point is meaningful. ClinGen's
haploinsufficiency Score column is not: `0`-`3` are graded evidence
levels, while `30` ("gene associated with autosomal recessive
phenotype") and `40` ("dosage sensitivity unlikely") are *special
codes*. On a scale like that a cut-point can only be wrong — set it to
`30` and you admit the recessive code, set it to `40` and you admit
both, and there is no value a deployer could correctly choose that
`{3}` does not already express. Bij AI therefore matches on an explicit
set of qualifying scores (`CONFIG.clingen.DOSAGE_SUFFICIENT_EVIDENCE_SCORES`)
rather than comparing against a threshold.

These *were* environment-overridable, justified here as "matching every
other ACMG threshold in this codebase" — an analogy that fails exactly
where the defect lived, and one that licensed a `>=` comparison which
read scores `30` and `40` as *stronger* evidence for haploinsufficiency
than `3`.

**Performance:** gene-evidence results are cached (in-memory LRU with a
24-hour default TTL — much longer than gnomAD's, since gene curation
changes far less often than population frequencies — plus an optional
on-disk JSON-lines persistence layer via `GEPER_CLINGEN_CACHE_DISK_PATH`);
batch lookups (`ClinGenLookup.query_variants_batch`) resolve each
*distinct* gene once and fan the shared result back out to every
variant that mapped to it, so a VCF with many variants in the same
gene pays the ClinGen lookup once, not once per variant.

**Report/API:** every variant's Markdown report gets a dedicated
"### ClinGen (Clinical Evidence)" panel (gene, clinical validity table,
dosage sensitivity, actionability); the JSON output gets a new,
additive `"clingen"` key per variant (see "9. Output format") —
omitting it entirely from a `build_variant_result` call still produces
a complete, valid record.

## 18. Environment verification & troubleshooting

Run this before `python main.py ...` (or at the top of a Colab cell):

```bash
python verify_environment.py          # human-readable report
python verify_environment.py --json   # machine-readable
python verify_environment.py --strict # exit 1 if anything required is missing
```

It checks, independently and without ever installing/upgrading
anything itself: Python version, torch/torchvision/transformers/
accelerate/tensorflow versions (against the pinned compatibility
matrix below), CUDA/GPU compute capability, cross-package version
compatibility (the #1 cause of a bare "undefined symbol" import
crash), internet connectivity, `blastn`/`tabix` availability, RAM,
disk space, and that every model wrapper and database client module
actually imports cleanly in this process. Every check degrades to its
own PASS/WARN/FAIL line rather than raising — one broken check never
hides the rest of the report.

**Compatibility matrix** (mirrors `requirements.txt` and
`verify_environment.py::EXPECTED` — if you change one, update all
three):

| Package | Pinned version | Notes |
|---|---|---|
| Python | 3.11.x or 3.12.x | Evo2's own PyPI metadata (`>=3.11,<3.13`) is the binding constraint |
| torch | 2.7.1 | flash-attn (Evo2 only) is compiled against this exact ABI |
| torchvision | 0.22.1 | Must match the torch pin exactly, or import fails with "undefined symbol" |
| torchaudio | 2.7.1 | Not imported by Bij AI itself, but pinned defensively — see the troubleshooting row below |
| transformers | ≥5.12.1,<6.0.0 | |
| accelerate | ≥1.14.0,<2.0.0 | |
| tensorflow | ≥2.16.0,<3.0.0 | MMSplice only |

**Common failure -> fix:**

| Symptom | Cause | Fix |
|---|---|---|
| `undefined symbol: ...torch...` at import time | torch/torchvision version mismatch | `pip install torch==2.7.1 torchvision==0.22.1 --force-reinstall` |
| `OSError: undefined symbol: torch_library_impl` while importing an unrelated model (e.g. ESM2) | torchaudio version mismatch — common on Colab, which ships its own pre-installed torchaudio tied to Colab's default torch, not this project's pinned one; a transitive dependency (rna-fm/evo2) can trigger the actual import | `pip install torchaudio==2.7.1 --force-reinstall` (now pinned explicitly in `requirements.txt` for exactly this reason; `verify_environment.py` catches this mismatch before any model import is attempted) |
| Evo2 silently unavailable | GPU compute capability < 8.0 (e.g. Tesla T4) | Expected and by design — see `EVO2_T4_HARDWARE_FINDINGS.md`. Every other model still works. |
| MMSplice stage skipped | tensorflow not installed, or `mmsplice` package's `.h5` weights not yet provisioned | `pip install 'tensorflow>=2.16.0,<3.0.0'`; the `mmsplice` package itself auto-installs `--no-deps` on first use |
| gnomAD stage always falls to GraphQL | No local index configured for this build | Set `GEPER_GNOMAD_GRCH38_LOCAL_VCF` (or `_GRCH37_LOCAL_VCF`) to a provisioned tabix-indexed sites VCF |
| AlphaMissense/gnomAD local lookups skipped | `tabix` not on PATH | `apt-get install tabix` (or `conda install -c bioconda htslib`) |
| BLAST falls back to slow remote NCBI queue | No local `blastn` on PATH | `apt-get install ncbi-blast+`, or set `blast_mode="local"` with a database path |

**Supported operating systems:** Ubuntu 22.04 LTS, Ubuntu 24.04 LTS,
and Google Colab's standard runtime (Ubuntu-based). Other Debian-family
Linux distributions with a Python 3.11/3.12 environment are expected
to work but are not part of this project's tested matrix.

**Recommended GPU:** any NVIDIA GPU with compute capability ≥ 7.0 for
HyenaDNA/RNA-FM/ESM2/AlphaMissense/MMSplice (CPU fallback
also works, slower); compute capability ≥ 8.0 additionally required for
Evo2 specifically (see "13. Hardware notes" and
`EVO2_T4_HARDWARE_FINDINGS.md` — Evo2 is intentionally disabled below
this, not crashing).

## 19. Biological Evidence Layer (UniProt / InterPro-Pfam / AlphaFold DB)

Three additional, gene/protein-level evidence sources, run in this
order right after the ClinGen stage (`pipeline/uniprot/`,
`pipeline/interpro/`, `pipeline/alphafold/`):

1. **UniProt** — reviewed (Swiss-Prot) protein function, disease
   relevance ("DISEASE"/"INVOLVEMENT_IN_DISEASE" comments), and
   sequence features (domains, regions, active/binding sites), for the
   gene ClinGen's stage already resolved for this variant (no second
   Ensembl lookup — see `UniProtLookup.query_variant`'s
   `gene_symbol_hint`).
2. **InterPro/Pfam** — conserved domain/family/motif matches for the
   protein UniProt resolved, keyed by its UniProt accession. InterPro's
   API now serves both InterPro-integrated entries and not-yet-integrated
   member-database (Pfam, SMART, PROSITE, ...) signature matches in one
   call.
3. **AlphaFold DB** — structural reference (model URL, version), mean
   pLDDT, and (when a residue position can be estimated) pLDDT at that
   specific residue.

**Licensing (verified before integration, per this project's "check
the license before integrating" policy):**

| Source | License | Commercial use |
|---|---|---|
| UniProt | CC-BY-4.0 (https://www.uniprot.org/help/license) | Yes, with attribution |
| InterPro / Pfam | CC0 1.0 Universal (public domain; https://interpro-documentation.readthedocs.io/en/latest/license.html) | Yes, no restriction |
| AlphaFold DB (structures + confidence metrics) | CC-BY-4.0, explicitly for "academic and commercial use" (https://alphafold.ebi.ac.uk/faq) | Yes, with attribution |

Note: this is the AlphaFold **database** license (predictions and
confidence metrics Bij AI downloads). It is distinct from the AlphaFold
**model parameters'** license (CC-BY-NC-4.0, non-commercial) — Bij AI
never downloads or runs the AlphaFold model itself, only its
already-computed, separately-licensed database entries.

**Protein-position caveat (read this before trusting a residue-level
result):** Bij AI's protein translation
(`pipeline/protein_translator.py`) works from a short local flanking
window, not a transcript-verified CDS, so it has no canonical HGVS.p
coordinate to offer (the same limitation earlier audits of this
codebase already flagged for HGVS notation generally). Where InterPro
and AlphaFold report a residue- or domain-level result, it is against
`pipeline/orchestrator.py::Orchestrator._estimate_protein_position`'s
best-effort, *local-window-relative* index — surfaced everywhere as
`"protein_position_basis": "local_translation_window_estimate"`, never
presented as an authoritative transcript-numbered position. Treat any
"affected domain" / "residue pLDDT" field as an approximate,
reviewer-facing hint, not a validated coordinate.

**Graceful fallback:** exactly like gnomAD/ClinGen, each of the three
`Lookup` facades (`UniProtLookup`, `InterProLookup`, `AlphaFoldLookup`)
never raises. A disabled integration
(`GEPER_ENABLE_UNIPROT`/`GEPER_ENABLE_INTERPRO`/`GEPER_ENABLE_ALPHAFOLD=false`),
an unreachable API, or a gene/accession with no data all produce a
clean `{"found": false, ...}` (or `{"skipped": true, ...}`) result, and
the pipeline continues to completion.

**Caching:** all three cache (in-memory LRU + optional on-disk
JSON-lines persistence via
`GEPER_UNIPROT_CACHE_DISK_PATH`/`GEPER_INTERPRO_CACHE_DISK_PATH`/`GEPER_ALPHAFOLD_CACHE_DISK_PATH`),
keyed by gene symbol (UniProt) or UniProt accession (InterPro,
AlphaFold). AlphaFold's per-residue pLDDT array is the one exception:
it's the expensive part (a structure-file download), so it's cached
separately, in-memory only, inside `LiveAPIAlphaFoldProvider` — never
written to the smaller on-disk summary cache, and never reused across
two different variants' residue positions without being recomputed
from that same cached array (see `AlphaFoldLookup`'s docstring for why).

**Offline / local-dataset mode:** like gnomAD/ClinGen, each source
supports a local, deployer-provisioned JSON-lines file as an
alternative to (or fallback ahead of) its live API —
`GEPER_UNIPROT_LOCAL_FILE`, `GEPER_INTERPRO_LOCAL_FILE`,
`GEPER_ALPHAFOLD_LOCAL_FILE` — plus a hard offline switch
(`GEPER_UNIPROT_OFFLINE`/`GEPER_INTERPRO_OFFLINE`/`GEPER_ALPHAFOLD_OFFLINE=true`)
that refuses to call the network at all.

**Startup validation:** `verify_environment.py`'s
`check_biological_evidence_layer` constructs each enabled integration's
`Lookup` facade (config parses, cache initializes, provider wiring is
intact) with no network call, exactly like `check_installed_databases`
does for the database clients — run as part of "18. Environment
verification & troubleshooting" above.

**Report/API:** every variant's Markdown report gets three new panels
("### UniProt (Protein Annotation)", "### InterPro / Pfam (Conserved
Domains)", "### AlphaFold DB (Structural Reference)"); the JSON output
gets three new, additive `"uniprot"`/`"interpro"`/`"alphafold"` keys
per variant (see "9. Output format") — omitting them entirely from a
`build_variant_result` call still produces a complete, valid record.
Unlike gnomAD/ClinGen's ACMG-criterion contributions, these three are
surfaced in the interpretation summary as purely descriptive,
zero-weight context (`InterpretationEngine._biological_context_evidence`)
— none of them map onto a specific ACMG/AMP criterion the way
population frequency or gene curation do, and the position they're
keyed to is only an estimate, not a validated coordinate.

**Key environment variables** (full list in `config.py`; mirrors the
`GEPER_GNOMAD_*`/`GEPER_CLINGEN_*` shape):

| Variable | Default | Meaning |
|---|---|---|
| `GEPER_ENABLE_UNIPROT` / `_INTERPRO` / `_ALPHAFOLD` | `true` | Master on/off switch per integration |
| `GEPER_UNIPROT_OFFLINE` / `_INTERPRO_OFFLINE` / `_ALPHAFOLD_OFFLINE` | `false` | Refuse all network calls for this integration |
| `GEPER_UNIPROT_LOCAL_FILE` / `_INTERPRO_LOCAL_FILE` / `_ALPHAFOLD_LOCAL_FILE` | unset | Local JSON-lines dataset path (offline/deterministic use) |
| `GEPER_ALPHAFOLD_FETCH_STRUCTURE` | `true` | Download the AlphaFold structure file for per-residue pLDDT (off = summary/version only, no residue confidence) |

## 20. Local BLAST+ deployment (hospital installations)

Local BLAST+ (`blastn`/`makeblastdb`/`blastdbcmd`) is the default,
preferred production BLAST backend: once a database is provisioned, a
search is a local subprocess call (tens of milliseconds) instead of a
round trip through NCBI's shared, public hosted queue (commonly
30s-several minutes per submission). Remote NCBI BLAST remains the
automatic fallback for any deployment that hasn't provisioned a local
database yet, and BLAST is skipped gracefully (not a hard pipeline
failure) if neither backend is usable.

### 20.1 Install NCBI BLAST+

```bash
# Debian/Ubuntu (what verify_environment.py's fix hint recommends)
apt-get install ncbi-blast+

# Alternative: conda
conda install -c bioconda blast
```

This installs `blastn`, `makeblastdb`, and `blastdbcmd`. All three are
checked automatically at startup (see 20.4 below).

### 20.2 Provision a database

Either bring a prebuilt NCBI BLAST database (e.g. an existing `nt` or
a curated in-house reference), or let Bij AI build one for you from a
FASTA reference the first time it's needed:

```bash
# Option A: point at an existing, prebuilt database
export GEPER_BLAST_DATABASE=/data/blastdb/GRCh38

# Option B: point at a FASTA reference; Bij AI runs makeblastdb for you
# once, then reuses the built database on every subsequent run (it is
# never rebuilt once the .n*/.ndb files exist at GEPER_BLAST_DATABASE)
export GEPER_BLAST_REFERENCE_FASTA=/data/reference/GRCh38.fasta
export GEPER_BLAST_DATABASE=/data/blastdb/GRCh38   # where to build/look
```

Equivalent CLI flags: `--blast-db` / `--blast-reference-fasta`.

### 20.3 Select the backend priority

```bash
export GEPER_BLAST_MODE=auto     # default: local first, remote fallback, graceful skip
# GEPER_BLAST_MODE=local          # require local BLAST+; fails fast if no database configured
# GEPER_BLAST_MODE=remote         # always use NCBI-hosted BLAST (e.g. no local disk budget for a DB)
```

`auto` (the default, matching a bare `BLASTClient()`/`GeperPipeline()`
with no explicit `blast_mode`) resolves in this priority order:

1. **Local BLAST+** -- used whenever `blastn` is on `PATH` and a
   database is found at `GEPER_BLAST_DATABASE` (or built there from
   `GEPER_BLAST_REFERENCE_FASTA` per 20.2).
2. **Remote NCBI BLAST** -- used when local isn't available but
   Biopython is importable/installable (Bij AI auto-installs it via pip
   the first time it's needed, same as its other optional
   dependencies).
3. **Graceful skip** -- if neither is usable (e.g. an air-gapped
   deployment with no local database and no PyPI access for
   Biopython), BLAST is skipped for that pipeline instance rather than
   raising: every variant's BLAST evidence is recorded as
   `{"skipped": true, "reason": "..."}`, and interpretation continues
   on the remaining evidence sources.

### 20.4 Startup validation

`python verify_environment.py` checks and reports installed versions of
all three local BLAST+ tools, plus whether a usable local database is
currently detected at the configured path:

```
✅ [PASS] BLAST+ (blastn/makeblastdb/blastdbcmd)
        All local BLAST+ command line tools found -- blastn: blastn: 2.12.0+; makeblastdb: makeblastdb: 2.12.0+; blastdbcmd: blastdbcmd: 2.12.0+.

✅ [PASS] BLAST database / mode
        GEPER_BLAST_MODE='auto'; local database detected at '/data/blastdb/GRCh38' (auto-resolution -> 'local').
```

Missing binaries/database are reported as `WARN` (with a `-> Fix`
hint), never `FAIL` -- Bij AI falls back to remote BLAST, or skips
BLAST gracefully, rather than refusing to start.

### 20.5 Caching and performance

The existing in-memory + on-disk BLAST result cache (`GEPER_BLAST_DISK_CACHE`,
default on) and `search_many()` concurrent batch submission
(`GEPER_BLAST_ENABLE_PREFETCH`, default on) apply identically to local
and remote BLAST -- a sequence already searched (this run or a
previous one) is never searched twice, regardless of backend. Local
BLAST additionally defaults its concurrency ceiling
(`GEPER_BLAST_MAX_CONCURRENT_LOCAL`) to the machine's CPU count rather
than remote's deliberately-polite default of 3
(`GEPER_BLAST_MAX_CONCURRENT_REMOTE`), since local BLAST has no shared
external queue to be considerate of.

None of this changes what a BLAST result *is* -- see
`verify_blast_optimization.py` (PARTs 1-6 for the caching/batching
correctness proof that predates this feature, PART 7 for the
makeblastdb auto-build proof, PART 8 for the graceful-skip proof) and
`benchmark_local_blast.py` for a real local-vs-remote timing
comparison using the actual `blastn`/`makeblastdb` binaries.

### 20.6 Backward compatibility

- `GEPER_BLAST_LOCAL_DB` and the standard NCBI `BLASTDB` environment
  variable are still honored as fallbacks for `GEPER_BLAST_DATABASE`,
  in that priority order -- existing deployments/scripts that already
  set either of those continue to work unchanged.
- `BLASTClient`'s and `GeperPipeline`'s public constructor signatures
  are unchanged except for new, optional, backward-compatible
  parameters (`reference_fasta` / `blast_reference_fasta`); every
  existing call site (including direct `BLASTClient(mode="remote", ...)`
  construction) behaves exactly as before.
- Report format, JSON schema, and every other evidence source
  (ClinVar, gnomAD, ClinGen, UniProt, InterPro, AlphaFold, ACMG,
  Confidence, Priority, Conflict, Explainability) are untouched by this
  feature -- BLAST results are consumed by the interpretation engine
  the same way regardless of which backend produced them.
| `GEPER_UNIPROT_CACHE_TTL_HOURS` / `_INTERPRO_..._TTL_HOURS` / `_ALPHAFOLD_..._TTL_HOURS` | `24` | Cache TTL |

## 21. HPO (phenotype evidence)

Gene-phenotype evidence from the Human Phenotype Ontology
(https://hpo.jax.org), implemented in `pipeline/hpo/`. Same
never-blocks-the-pipeline philosophy as every other evidence source
here.

**Bootstrap:** by default Bij AI auto-fetches HPO's official
`genes_to_phenotype.txt` annotation file
(`purl.obolibrary.org/obo/hp/hpoa/genes_to_phenotype.txt`) the first
time it's needed — gated by `GEPER_HPO_AUTO_FETCH` (default `true`),
cached under `GEPER_HPO_AUTO_FETCH_DIR`, refreshed on a
`GEPER_HPO_AUTO_FETCH_TTL_HOURS`-hour TTL (default 24h), with a live
API fallback and stale-cache fallback on a failed refresh — the same
pattern as ClinGen (see "17. ClinGen") and Orphanet (see "22.
Orphanet") below.

**Wiring:** `_run_hpo_stage()` (`pipeline/orchestrator.py`) reuses the
gene symbol ClinGen's stage already resolved for the variant and calls
`hpo_client.query_variant(gene_symbol)`, catching all exceptions
defensively so an HPO lookup failure never fails the variant.

**ACMG contribution — PP4.** The evidence this stage retrieves feeds
ACMG's PP4 rule (`pipeline/acmg_rules.py::ACMGRuleEngine._pp4`): it
compares the patient's observed HPO terms (supplied via `--hpo-terms`
/ `--phenotype-file`, see "6. Usage" above) against this variant's
gene's own HPO-curated phenotype set, using overlap ratio
(`GEPER_HPO_PP4_OVERLAP_THRESHOLD`, default `0.5`) and distinct-disease
count (`GEPER_HPO_PP4_MAX_DISTINCT_DISEASES`, default `3`, a heuristic
proxy for "single genetic etiology") as its two triggering conditions.
**If no patient phenotype terms are supplied for a run (the default),
PP4 reports `not_evaluated` exactly as before** — there's simply
nothing to compare the gene's phenotype set against. Supplying
`--hpo-terms`/`--phenotype-file` is what turns PP4 from structurally
unable to fire into an active, evaluated criterion; see
`tests/test_hpo.py` (rule logic against real FBN1/CFTR data) and
`tests/test_phenotype_input.py` (the CLI input path itself) for full
coverage of both states.

## 22. Orphanet (rare-disease context)

Gene-disorder associations from Orphanet (https://www.orphadata.com),
implemented in `pipeline/orphanet/`. Same graceful-degradation
philosophy as every other evidence source here.

**Bootstrap:** by default Bij AI auto-fetches Orphanet's
`en_product6.xml` gene-disorder association file (CC BY 4.0,
`orphadata.com/data/xml/en_product6.xml`) — gated by
`GEPER_ORPHANET_AUTO_FETCH` (default `true`), cached under
`GEPER_ORPHANET_AUTO_FETCH_DIR`, refreshed on a
`GEPER_ORPHANET_AUTO_FETCH_TTL_HOURS`-hour TTL (default 24h). Unlike
HPO/ClinGen, Orphanet has **no live-API fallback** (Orphanet's REST
API requires a paid Data Transfer Agreement), so a failed fetch simply
reports Orphanet evidence as unavailable for that run rather than
retrying live.

**Wiring:** `_run_orphanet_stage()` (`pipeline/orchestrator.py`) reuses
the gene symbol ClinGen's stage already resolved.

**Role:** annotation/report context only — Orphanet's disorder
associations are surfaced in the report alongside the other evidence
sources, but (unlike ClinGen's graded gene-disease validity scale used
by PVS1/PP5/BP6-style evidence — see "17. ClinGen") they are not
themselves mapped to an ACMG criterion, so Orphanet does not
independently move a variant's classification.

## 23. SPiP (splicing prediction, standalone)

The official, MIT-licensed SPiP R implementation
(https://github.com/LBGC-CFB/SPiP), vendored and run via an `Rscript`
subprocess (`pipeline/models/spip_plugin.py`,
`pipeline/models/spip/loader.py` + `vendor/`) against a one-variant VCF.

**Bootstrap:** reference data (`model.RData`, a `dataRefSeq<genome>.RData`
file, and a large `transcriptome_<genome>.RData` file) auto-bootstraps
at load time from the upstream GitHub repository plus a SourceForge
mirror, gated by `CONFIG.splicing.ENABLE_SPIP` and by whether `Rscript`
is available on `PATH`.

**Not currently wired into the pipeline or ACMG evidence.** Unlike
SpliceFormer/SpliceBERT (`pipeline/orchestrator.py`'s
`_run_standalone_splice_plugin_stage`, which *are* invoked per-variant
and feed BP7 — see `ACMGRuleEngine._bp7`), SPiP is deliberately
excluded from `pipeline/models/ensemble.py`'s consensus, is never
passed to `InterpretationEngine` (no PP3/PP4/BP7 contribution), does
not appear in the AI Models startup/status table, and is not called
from any per-variant stage in `orchestrator.py`. It's reachable today
only via direct/programmatic use (`ModelManager`, `pending_plugins.py`)
and its own tests — wiring it into a per-variant orchestrator stage
and an ACMG rule is future work, not something a current run already
does.

## 24. PS3/BS3 (functional evidence)

Published functional-assay evidence — saturation genome editing, deep
mutational scans, MAVE reporter assays — for the ACMG/AMP PS3
("well-established functional studies show a damaging effect") and BS3
("...show no damaging effect") criteria, implemented in
`pipeline/functional_evidence/`. Unlike ClinGen/HPO/Orphanet above,
this is **variant-level**, not gene-level: it answers "does this exact
substitution behave abnormally in a published assay?", so it needs
this variant's own genomic (g.) and coding (c.) HGVS notation
(generated via `pipeline/hgvs_utils.py`, reusing the gene symbol
ClinGen's stage already resolved and the transcript structure the
transcript stage already fetched — no separate lookup).

**Two sources, primary/secondary** (not local/API-fallback — both are
live APIs; there is no bulk-download tier for either today), following
the ClinGen SVI Working Group's PS3/BS3 recommendation (Brnich et al.
2019, *Genome Medicine*, PMID 31892348) for strength-tier logic:

- **ClinGen Evidence Repository** (primary,
  `pipeline/functional_evidence/erepo_provider.py`,
  `erepo.clinicalgenome.org`): expert-panel (VCEP)-curated PS3/BS3
  "Met"/"Not Met" calls, already assigned a strength (honoring a VCEP's
  own explicit override, e.g. an evidence code literally labeled
  `PS3_Moderate`, over the default `strong`) and traceable to that
  panel's published specification. CC0-licensed, same as every other
  ClinGen curated resource Bij AI already integrates. **Verified live**
  during development — unlike `ClinGenConfig.API_ENDPOINT` (see "17.
  ClinGen"), this environment *can* reach `erepo.clinicalgenome.org`:
  a real `GET .../classifications?gene=BRCA1` request returned a real
  ENIGMA BRCA1/BRCA2 VCEP curation of `NM_007294.4:c.135-1G>T`
  (`NC_000017.11:g.43106534C>A`) with evidence code `PS3: Met`,
  classified Pathogenic. The same query for `gene=CFTR` returned zero
  results — a genuine coverage gap, not a request-shape bug.
- **MaveDB** (secondary, `pipeline/functional_evidence/mavedb_provider.py`,
  `api.mavedb.org`), consulted only for a variant ERepo has no
  curation for: a raw multiplexed-assay score, bucketed into
  functional/intermediate/non-functional using that score set's own
  investigator-provided `scoreCalibrations` thresholds — Bij AI never
  invents a threshold here. Verified live: every BRCA1/TP53 score set
  checked during development was `CC0` or `CC BY 4.0` licensed (both
  commercial-use-compatible — MaveDB relicensed its whole corpus from
  the earlier non-commercial CC-BY-NC-SA specifically to remove that
  restriction), and a real BRCA1 saturation-genome-editing row
  (`NM_007294.3:c.5565A>T`, score `-0.0153`) was confirmed to bucket
  into MaveDB's own "normal" (BS3-relevant) range. CFTR returned zero
  score sets — the same gap ERepo has.

**Strength/confidence defaults** (`CONFIG.functional_evidence.*`, all
overridden by a source's own explicit strength when it gives one): an
ERepo call is used at **Strong/High confidence** (an expert panel has
already completed the SVI framework's four steps); a MaveDB call is
**Moderate confidence** by default, or **Supporting/Low confidence**
when the score set's own calibration is flagged research-use-only
(`scoreCalibrations[].researchUseOnly`) — one step below an
already-adjudicated VCEP call in the SVI framework's validation
hierarchy, since Bij AI performed the bucketing itself rather than
consuming an expert panel's own conclusion.

**Performance:** both sources are queried once per *gene*, not per
variant — each provider's `fetch_gene_index()` call returns every
curated/assayed variant for that gene in one pass (MaveDB additionally
sorts a gene's score sets by variant count and caps the number fetched
via `GEPER_FUNCTIONAL_EVIDENCE_MAVEDB_MAX_SCORE_SETS`, default 10,
since a gene like BRCA1 can have 60+ score sets), cached
(`GEPER_FUNCTIONAL_EVIDENCE_CACHE_TTL_HOURS`, default 24h) so every
subsequent variant in an already-seen gene costs zero additional
network calls for the rest of the run.

**Honest gap reporting:** when neither source has anything for a
gene/variant (confirmed live for CFTR during development), both PS3
and BS3 report `not_evaluated` with a clear rationale — the same
pattern already used for PS4/BS4/PP4-before-patient-input, never a
fabricated call.

**Testing:** `tests/test_ps3_bs3.py` (41 tests) uses frozen, real
ClinGen ERepo/MaveDB fixture data (`tests/fixtures/ps3_bs3_*`) —
including the exact BRCA1/TP53/CFTR examples above — for deterministic,
network-free coverage; `verify_ps3_bs3_integration.py` is the live
counterpart, exercising the real, unmocked provider code against
`erepo.clinicalgenome.org`/`api.mavedb.org` directly (15/15 checks
passing as of this integration).

## 25. Clinician Review Workflow

A minimal, filesystem-only mechanism (`geper/review/`) that lets a
clinician move a completed run from **DRAFT** to **REVIEWED**, and
layer a classification override on top of Bij AI's own ACMG result —
without a database, an IP-address/hash audit trail, or an erasure/
withdrawal workflow. Those are real DPDP Act 2023 obligations, but
belong to a much larger patient-intake/storage-lifecycle system that
doesn't exist in Bij AI yet; this module deliberately does not bolt
them on speculatively (see `geper/review/signoff.py`'s module
docstring, and `report/summary.py::_parse_consent`'s docstring for the
same discipline applied to consent metadata — see
`patient_metadata.example.json`).

**Design note — reuses the existing draft/reviewed mechanism, doesn't
invent a second one.** "DRAFT" vs "REVIEWED" is not a file-location
choice anywhere in Bij AI — every clinical PDF (`geper_report_full.pdf`,
`geper_report_short.pdf`) is stamped with a mandatory ICMR-style AI-
disclosure footer on every page, decided purely by whether the
`patient_meta` passed to `generate_pdf`/`generate_short_pdf` has a
non-empty `physician` field (see
`report/summary.py::_icmr_ai_disclosure_footer_text`): absent →
`"DRAFT -- NOT FOR PATIENT USE. Awaiting clinical review."`; present →
`"...reviewed by {physician}. This is not a standalone diagnosis."`
`physician` is deliberately independent of `patient_name` (see
`_parse_patient_meta`'s docstring) so a de-identified run — no patient
name is required by default — can still be reviewed and signed off
by a named clinician without a patient name ever being attached.
`approve` below works entirely by writing that one field and
**re-invoking `generate_pdf`/`generate_short_pdf`** — the same
functions `pipeline/orchestrator.py::run` already calls — never a new
rendering path, PDF-editing library, or watermark-removal code.

### Commands

Run from inside the `geper/` directory, matching `main.py`'s own
invocation convention (there is no installed `geper` console script,
and this codebase uses plain `argparse` throughout, not Click):

```bash
# Approve: regenerates both PDFs with the clinician's identity in the
# footer, writes a SHA-256-checksummed manifest.
python review/cli.py approve \
  --output-dir ./geper_output \
  --clinician-name "Dr. Rajesh Sharma" \
  --reg-number "MCI-12345" \
  --hospital "AIIMS Delhi"

# Override: layers a clinician's classification on top of one variant's
# Bij AI-derived result -- never replaces it -- and regenerates all
# three report formats.
python review/cli.py override \
  --output-dir ./geper_output \
  --variant "17:43106534:C>A" \
  --new-classification "Likely Pathogenic" \
  --reason "Additional family history of early-onset breast cancer" \
  --clinician-id "rajesh.sharma@aiims.edu"

# List every run under a directory tree and its DRAFT/REVIEWED status
# (DRAFT only by default; --all to also show REVIEWED).
python review/cli.py list-pending --search-root ./geper_output_root --all
```

Both `--output-dir` arguments above must already contain a
`geper_results.json` from a completed Bij AI run
(`python main.py --vcf ...`) — `approve`/`override` error clearly,
without doing anything, if it's missing.

### What `approve` does

1. Confirms `geper_results.json` exists in `--output-dir`.
2. Writes/updates `geper_patient_meta.json` in that same directory,
   folding `--clinician-name`/`--reg-number`/`--hospital` into the
   existing `physician` field (e.g. `"Dr. Rajesh Sharma, Reg. No.
   MCI-12345, AIIMS Delhi"`) — merging into, never clobbering, any
   `patient_name`/`dob`/`gender`/`consent` a lab already stored there.
3. Re-invokes `generate_pdf`/`generate_short_pdf` against the
   unchanged `geper_results.json`, overwriting `geper_report_full.pdf`/
   `geper_report_short.pdf` in place — this is what actually flips the
   footer to "reviewed by ...".
4. Computes SHA-256 of both regenerated PDFs plus `geper_results.json`.
5. Writes `geper_signoff_manifest.json`: the three checksums,
   clinician identity, `approved_at` (ISO 8601), and one
   `{variant_id, gene, final_classification, confidence}` entry per
   variant (`final_classification` reflects an active clinician
   override when one exists — see below — otherwise Bij AI's own call).
6. Appends one JSON-Lines record to `geper_signoff_audit.log`.

### What `override` does

1. Parses `--variant` (`chrom:pos:ref>alt`) and finds the matching
   variant in `geper_results.json` by position **and** allele — never
   position alone.
2. **Never overwrites Bij AI's own classification.** Appends
   `{variant_id, original_classification, new_classification, reason,
   clinician_id, timestamp}` to that variant's `"overrides"` list (full
   history, most recent last), and sets
   `clinical_report["acmg_classification"]["clinician_override"]` to
   that same record — a convenience pointer the report renderers read.
3. Regenerates `geper_report.md`, `geper_report_full.pdf`, and
   `geper_report_short.pdf` from the updated document (same
   `ReportGenerator`/`generate_pdf`/`generate_short_pdf` any run
   already uses). Every renderer shows both classifications explicitly
   — e.g. *"GEPER classification: Uncertain significance; Clinician
   override: Likely Pathogenic — Additional family history..."* —
   never one silently standing in for the other.
4. Does **not** touch `geper_patient_meta.json`. A not-yet-approved
   run stays DRAFT after an override; an already-approved run stays
   REVIEWED (the footer mechanism's state is untouched either way).
5. Appends one JSON-Lines record to `geper_signoff_audit.log`.

### What `list-pending` does

Recursively finds every `geper_results.json` under `--search-root` and
prints `output_dir | report_date | num_variants |
has_conflicting_evidence | status`. Status is REVIEWED if a
`geper_signoff_manifest.json` sits alongside that run's results (i.e.
a successful `approve` has run), DRAFT otherwise.
`has_conflicting_evidence` reads the existing Conflict Resolution
Engine output (`clinical_report["conflict_resolution"]["severity"]`,
Phase 6) already computed for each variant — never recalculated here.
A corrupt `geper_results.json` under the search root is skipped with a
warning, not a fatal error.

**Testing:** `tests/test_signoff.py` (36 tests) uses synthetic
3-variant fixtures (Pathogenic / VUS-with-a-conflict / Likely Benign)
built via the real `report/clinical_report_builder.py
::build_clinical_report()` — not a hand-rolled partial dict — so they
carry every key the real report renderers expect. No model weights, no
real pipeline run. Covers: the reviewed footer appearing on every page
after `approve` (pypdf text extraction), manifest/file SHA-256
agreement, `override` preserving the original classification while
regenerating all three formats, `list-pending` distinguishing DRAFT
from REVIEWED across multiple runs, and the CLI's argument parsing and
exit codes.

## 26. External service health checks (startup probe, retry, and the offline latch)

Before the first variant is processed, Bij AI probes every external
service it depends on (Ensembl, ClinVar/dbSNP, ClinGen, MaveDB,
IndiGenomes) once each, in parallel, and prints a status table. This
is separate from — and sits on top of — the retry logic each client
already has in its own request loop (`pipeline/sequence_context.py`,
`pipeline/clingen/utils.py`, `pipeline/functional_evidence/
erepo_provider.py`, etc.): the startup probe's verdict, once a service
is marked offline, is treated as **confirmed** for the rest of the
run, and every client checks `HEALTH.is_offline(...)` at the top of
its own retry loop to skip straight past its own retries rather than
burn a multi-attempt budget on a service already known dead.

**Why the probe now retries before it latches.** That "confirmed"
verdict used to come from a single request. Because a client skips its
own retries once a service is marked offline, one unlucky slow request
didn't just mislabel that service — it switched off the exact retry
logic that would otherwise have ridden out the jitter. Nothing crashed
and nothing errored (this is by design — see "3. Design principles"),
so a run degraded this way looked clean: it finished, produced a
report, and gave no indication anything was missing. This happened for
real: a run lost sequence context for every variant and SpliceFormer
went unavailable across the board, because one 4-second timeout on an
Ensembl probe latched it offline for the whole run.

**The latch itself was not weakened** — it still exists specifically
to stop a run from hanging on a genuinely dead service, and a service
that fails every attempt still ends up latched offline exactly as
before. What changed is how much evidence is required before that
latch fires:

- `CONFIG.health_check.PROBE_ATTEMPTS` (default `3`) — a service is
  only latched offline after this many consecutive failed attempts,
  with `PROBE_BACKOFF_SECS` (default `0.5s`) between them. Only a
  network-level failure (timeout, connection error) is retried; a 5xx
  response is a real answer from a host that is up and routing, so
  HEALTHY and DEGRADED verdicts still return on the first attempt,
  exactly as before.
- The per-attempt timeout is `CONFIG.health_check.TIMEOUT_SECS`
  (default `4.0s`, unchanged) for every attempt **except the last**.
  The final attempt — the one that actually decides whether to
  latch — uses `CONFIG.health_check.LATCH_CONFIRM_TIMEOUT_SECS`
  (default `30.0s`) instead.

**Why 30 seconds, specifically, and not a new guess.** It isn't picked
to feel generous — it's the same `REQUEST_TIMEOUT_SECS` a client's own
retry loop already waits on before giving up, the very number the
probe's short 4.0s timeout was originally justified against ("this
only needs to tell up from down/slow, much shorter than a client's own
budget"). That reasoning is sound for a status *report*, but this
probe doesn't only report — it *latches*, and revoking a service from
every client after 4 seconds when those same clients would have
happily waited 30 is not defensible. Using the clients' own number
instead of inventing a new one collapses what had been a 7.5x mismatch
between the probe's patience and the clients' own patience down to
exactly 1x. An earlier proposal to simply raise the probe's default
timeout to 8.0s was considered and withdrawn for the same reason: it
would still have been a guess at the same kind of number, just a
larger one, with no principled reason to stop at 8 rather than 10 or
15.

**The real case that motivated this**, from this project's own logs: a
legitimate, successful Ensembl startup probe took **21,931 ms**
alongside ClinVar answering in 1,207 ms on the same run — a single
public API can be that much slower than its neighbors on an ordinary
day, with nothing wrong on either end. The old 4.0s default would have
declared that Ensembl call dead. Under this change, the same sequence
of attempts fails on attempt 1 and attempt 2 (each still bounded at
4.0s) and succeeds on attempt 3, which is allowed the full 30s. No
fixed single-shot timeout — 4s, 8s, or even 20s — would have survived
that 22-second response; only giving the deciding attempt real
patience does.

**Cost.** A healthy service pays nothing: a first-attempt success
still returns within the original 4.0s budget, unchanged. A
genuinely dead service now costs roughly 4 + 0.5 + 4 + 0.5 + 30 ≈ 39
seconds instead of failing after 4 — but that cost is paid **once per
run**, in parallel across every probed service (probes don't queue
behind each other), against the roughly 90 seconds **per variant**
that a false latch used to cost by disabling a client's own retries
for the rest of the run. On any run with more than one variant, the
false-latch cost this change removes dominates the confirmation cost
it adds.

**A gotcha worth knowing before you rely on it**:
`GEPER_HEALTH_CHECK_ATTEMPTS=1` does **not** fully restore the old,
single-shot behavior. With only one attempt configured, that one
attempt *is* the deciding attempt, so it runs with the long 30s
confirm timeout rather than the short 4.0s fast-path timeout. This is
intentional — the timeout budget scales with how consequential the
decision is, not with how many attempts happen to precede it — but it
means "set attempts back to 1" is not a way to get the exact old
timing back.

**What a reader sees when a service does end up latched offline**:
today, a caveat naming the offline service(s) is included in the
**PDF** report's Limitations section (`report/summary.py`); the
Markdown report and the JSON/LIMS export do not yet carry the
equivalent caveat. This asymmetry is tracked separately, not part of
this fix, and is the same "one renderer got the caveat, the others
didn't" pattern already fixed for BP7's conflicting-evidence PDF gap
elsewhere in this project's history.

**Open decision, deliberately left unresolved here — not an
oversight**: should a latch ever be lifted mid-run, if a service that
failed all three attempts at startup comes back later? The answer is
no *for this change* — a re-probe/un-latch mechanism was designed but
not implemented, on the reasoning that (a) it would extend the runtime
contract across every one of the many call sites (`is_offline`/
`note_skip`) that currently only ever read a latch, not clear one, and
(b) its parameters (how often to re-check, how many re-probes to
allow) should be chosen against evidence of how often a false latch
still occurs now that retry-before-latch is live — evidence that
doesn't exist yet. Revisit this once that evidence accumulates, rather
than guessing at the parameters today the same way the original 4.0s
timeout was guessed at.

**Environment variables** for this mechanism (all five, existing and
new, listed together — see "10. Environment variables" for the full
table):

| Variable | Purpose | Default |
|---|---|---|
| `GEPER_HEALTH_CHECK_ENABLED` | Enable/disable the startup probe entirely | `true` |
| `GEPER_HEALTH_CHECK_TIMEOUT` | Per-attempt timeout for every attempt except the final, latch-deciding one | `4.0` |
| `GEPER_HEALTH_CHECK_ATTEMPTS` | Consecutive failed attempts required before latching a service offline (only network-level failures are retried) | `3` |
| `GEPER_HEALTH_CHECK_BACKOFF` | Pause between probe attempts | `0.5` |
| `GEPER_HEALTH_CHECK_CONFIRM_TIMEOUT` | Timeout for the final, latch-deciding attempt only — matches clients' own request timeout rather than a new guess | `30.0` |

