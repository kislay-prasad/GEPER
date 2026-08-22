# Bij AI Integrated Pipeline — Kim (FASTQ Engine) + Current Bij AI (VCF Interpretation Engine)

This is **not a repository merge**. It is a bridge between two independent,
unmodified-in-purpose projects:

```
FASTQ
  │
  ▼
Kim Pipeline                         (kim_pipeline/)
  QC → Alignment → Variant Calling
  │
  ▼
filtered_variants.vcf
  │
  ▼
Current Bij AI                        (geper/)
  Annotation → AI Models → Databases → Interpretation Engine → Clinical Report
```

"Clinical Report" here denotes a draft classification: no output is clinically
actionable before mandatory qualified human review and final sign-off (see
`geper/review/signoff.py`).

## Directory layout

```
geper_integrated/
├── geper/            Project A — current Bij AI. UNCHANGED. Remains the sole,
│                     authoritative implementation for Annotation, AI Models
│                     (DNABERT-2, HyenaDNA, RNA-FM, ESM2, AlphaMissense, Evo 2,
│                     MMSplice), ClinVar, dbSNP, gnomAD, ClinGen, UniProt,
│                     InterPro, Pfam, AlphaFold DB, BLAST+, ACMG automation,
│                     confidence engine, variant prioritization, conflict
│                     resolution, explainability, and clinical report generation.
│
├── kim_pipeline/      Project B — Kim pipeline. Responsible ONLY for FASTQ
│                     validation, QC, alignment, and variant calling in the
│                     combined workflow. Its own annotation/ACMG/AI/reporting/
│                     ClinVar/gnomAD/VEP/evidence-aggregation modules are
│                     completely untouched and still run normally when Kim is
│                     executed standalone (the default `mode="full"`).
│                     ONE new capability was added (Stage 1 of this
│                     integration): `analyze --mode vcf_only` /
│                     `--stop-after variant_calling`, which stops the existing
│                     `PipelineRunner.run()` immediately after Variant Calling
│                     instead of continuing into Kim's own annotation/report
│                     stages. Default behavior (no flag) is 100% unchanged.
│
└── bridge/            NEW. The only new code written for this integration.
    ├── combined_pipeline.py   Core orchestration: runs Kim's own main.py in
    │                          one subprocess, reads the resulting VCF path
    │                          from Kim's own checkpoint.json, then runs
    │                          Bij AI's own main.py --vcf in a second subprocess.
    ├── run_combined.py        CLI entry point for the full FASTQ → Report workflow.
    └── tests/test_bridge.py   Regression tests for the bridge itself.
```

## Why subprocess isolation (and not in-process imports)?

Both projects define top-level modules with the same names (`pipeline`, etc.),
and Bij AI requires a separate, heavier dependency environment (PyTorch,
Evo 2, HyenaDNA, etc. — pinned to torch 2.7.1 in Kislay's setup) from Kim's
alignment/variant-calling toolchain (bwa, samtools, freebayes, bcftools).
Importing both into one Python process would require renaming or merging
modules — exactly what this task explicitly prohibits. Running each
project's own, unmodified CLI entry point in its own subprocess:

- keeps every file of both projects byte-for-byte independent-of-each-other,
- requires zero renaming, shimming, or module surgery,
- lets each project keep its own Python environment (`--kim-python`,
  `--geper-python` flags select interpreters independently),
- and means "run Kim standalone" / "run Bij AI standalone" are the *same*
  commands a user already runs — the bridge doesn't add a special-case path
  for either project, it just calls them.

## Usage

Running this natively requires installing both projects' dependencies
by hand (see each project's own install docs). For a pre-built
environment with every system tool (bwa/samtools/bcftools/freebayes/
tabix/etc.) and Python dependency already in place, see
[DOCKER.md](DOCKER.md).

### 1. Run Kim standalone (unchanged, full mode)
```bash
cd kim_pipeline
python main.py analyze --r1 R1.fastq.gz --r2 R2.fastq.gz --ref GRCh38.fasta \
    --output-dir ./work --sample-id sample01
```

### 2. Run Kim standalone in vcf_only mode (new; still Kim-only)
```bash
cd kim_pipeline
python main.py analyze --r1 R1.fastq.gz --ref GRCh38.fasta \
    --output-dir ./work --sample-id sample01 --mode vcf_only
# -> ./work/sample01/filtered_variants.vcf, Kim's own annotation/report skipped
```

### 3. Run Bij AI directly against an existing VCF (unchanged)
```bash
cd geper
python main.py --vcf existing.vcf --output-dir ./geper_output
```

### 4. Run the full combined workflow (new)
```bash
python bridge/run_combined.py \
    --r1 R1.fastq.gz --r2 R2.fastq.gz --ref GRCh38.fasta \
    --sample-id sample01 \
    --kim-output-dir ./work/kim --geper-output-dir ./work/geper \
    --kim-python /path/to/kim-venv/bin/python \
    --geper-python /path/to/geper-venv/bin/python \
    --blast-mode auto --species human --assembly GRCh38
```
Output:
- `./work/kim/sample01/filtered_variants.vcf` (Kim)
- `./work/geper/geper_results.json`, `./work/geper/geper_report.md` (Bij AI)

If `--kim-python` / `--geper-python` are omitted, the interpreter currently
running the bridge is used for both.

## Validation dataset

`test_data/` contains a ready-to-use, self-validated FASTQ/reference pair
(real human mitochondrial genome, real m.3243A>G MELAS variant) that clears
Kim's production QC thresholds unmodified and has been run end-to-end
through Kim's real `analyze --mode vcf_only` (bwa/samtools/bcftools/freebayes)
producing exactly one PASS variant. See `test_data/README_TEST_DATASET.md`
for full details, or just run:

```bash
python bridge/run_combined.py \
    --r1 test_data/reads_1.fastq --ref test_data/reference.fasta \
    --sample-id MT_TEST01 \
    --kim-output-dir ./work/kim --geper-output-dir ./work/geper \
    --no-resume
```

## What changed vs. what didn't

| Area | Status |
|---|---|
| Bij AI annotation / AI models / databases / ACMG / report generation | **Unchanged** |
| Kim FASTQ validation / QC / alignment / variant calling | **Unchanged** |
| Kim's own annotation / ACMG / AI / PGx / ancestry / reporting stages | **Unchanged**, simply not invoked in the combined workflow |
| Kim `PipelineRunner.run()` | **+2 optional params**: `mode` (default `"full"`), `stop_after` (default `None`) |
| Kim `main.py analyze` / `run_pipeline.py` CLI | **+2 optional flags**: `--mode`, `--stop-after` |
| New `bridge/` package | **New**, zero duplication of either project's logic |

No duplicate interpretation ever occurs: Kim's annotation/ACMG/AI/reporting
code paths are structurally unreachable once `mode="vcf_only"` triggers the
early return in `PipelineRunner.run()`, verified by
`kim_pipeline/tests/test_vcf_only_mode.py` (asserts VEP/Annotation/Reporting
mocks are never called).
