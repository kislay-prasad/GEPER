# Kim → GEPER Validation Dataset

A small, fast, **biologically real** FASTQ/reference pair that passes Kim's
production QC thresholds unmodified and exercises the complete
`FASTQ → QC → Alignment → Variant Calling → filtered_variants.vcf → GEPER`
workflow with a real, clinically known variant.

## Files

| File | Description |
|---|---|
| `reference.fasta` | 16,569 bp — derived from the real revised Cambridge Reference Sequence (rCRS, NCBI `NC_012920.1`), the human mitochondrial genome used as contig `MT` in GRCh38. |
| `reads_1.fastq` | 1,825 single-end 150 bp reads, ~16.7x average coverage, tiled across the whole reference. |
| `generate_test_dataset.py` | The generator script (deterministic, seeded) — re-run it to regenerate the identical dataset from `rcrs_raw.fasta`. |
| `rcrs_raw.fasta` | The raw NCBI rCRS record the generator starts from. |

No `reads_2.fastq` is included — this is a single-end dataset (paired-end was not necessary to clear any QC/alignment/variant-calling threshold, and single-end keeps the dataset smaller and simpler for a first Colab run). Kim's `--r2` flag is optional and the workflow runs identically without it.

## Why this design

**Reference — real mitochondrial genome, not arbitrary random sequence.**
Kim's variant-calling output is consumed by GEPER's annotation stage, which
fetches sequence context and clinical evidence *live from Ensembl/ClinVar/
gnomAD by genomic coordinate* — it does not re-use Kim's reference FASTA for
annotation. That means an arbitrary synthetic reference at arbitrary
coordinates would produce a VCF that is real and valid, but meaningless to
GEPER's downstream lookups. Using the real rCRS at its real coordinates means
GEPER's downstream stages are querying real, resolvable genomic positions.
mtDNA was chosen over a nuclear region because it's a single 16.5 kb contig —
large enough to be realistic, small enough to align in well under a second in
Colab.

**Variant — a real, well-characterized pathogenic ClinVar variant, not an
arbitrary substitution.** Reads are generated from a copy of the reference
carrying a single homozygous substitution at 1-based position 3243: **A>G**.
This is the real **m.3243A>G** variant in *MT-TL1* — one of the most
well-characterized pathogenic mitochondrial variants known, associated with
MELAS and MIDD, with existing ClinVar/literature entries. This gives the
downstream GEPER annotation/ACMG stages a real chance of finding actual
clinical evidence rather than exercising the workflow against a coordinate
with no database entries.

**One deliberate dataset-generation fix worth noting:** the real rCRS
contains exactly one ambiguous base, `N`, at position 3107 — a well-known
historical numbering-preservation artifact (not a real ambiguous base in any
individual's mtDNA). Emitting that literal `N` into `reference.fasta` would
make FreeBayes call a non-standard `REF=N` VCF record purely as an artifact
of the reference file, which risks breaking strict downstream REF/ALT
handling and has nothing to do with the actual variant we're validating.
The generator resolves it once, deterministically, to a fixed real base —
documented in `generate_test_dataset.py` and in the `reference.fasta` header
line itself.

**Reads.** 150 bp single-end, tiled at a 9 bp step (~16.7x average coverage
— well above Kim's `variant_calling.filter_min_depth=10` and
`qc.min_total_reads=100` thresholds), alternating strand per read, with a
realistic 0.1% per-base sequencing error rate and Phred 40 quality
(Illumina 1.8+ `I` encoding) elsewhere. GC content ~44.4%, comfortably
inside the QC stage's 20–80% acceptance range.

## Self-validation performed

All of the following were actually executed in a sandboxed Linux
environment with `bwa` 0.7.17, `samtools`/`bcftools` 1.19, and `freebayes`
1.3.6 installed (the same toolchain Kim's `install_dependencies.sh` sets
up), not just structurally inspected:

1. **FASTQ/FASTA structural validation** — line counts divisible by 4,
   uniform 150 bp read/quality-string lengths, zero `N` bases in reads,
   valid reference FASTA with the documented single artifact resolved.
2. **Full Kim run**, unmodified production code, `mode=vcf_only`:
   ```
   ✓ FASTQ validation:  1825 records, 150–150 bp — PASS
   ✓ QC stage:          1825 reads (>= 100 required), mean quality 39.98
                         (>= 20 required), GC 44.4% (20–80% required),
                         Q20 fraction 1.0 — overall PASS
   ✓ Alignment (bwa):   100.0% of 1825 reads mapped, 0.18s
   ✓ Variant calling:   1/1 PASS (1 SNV, 0 indels), 0.08s
   ```
3. **Resulting `filtered_variants.vcf`** — exactly one PASS record:
   ```
   MT  3243  .  A  G  593.383  PASS  ...DP=17;AF=1;...  GT:DP:AD...  1/1:17:0,17:...
   ```
   `DP=17` (depth), `AF=1` / `GT=1/1` (clean homozygous call), `QUAL=593.383`
   (well above the `filter_min_qual=20.0` threshold) — an unambiguous,
   high-confidence call of the intended m.3243A>G variant, with zero
   spurious records.
4. **GEPER's own `VCFParser`** (pure Python, no heavy deps) parses this VCF
   with **zero skipped records**: `Variant(chrom='MT', pos=3243,
   variant_id='.', ref='A', alt='G', qual='593.383', filter_status='PASS',
   ...)`, correctly attributed to sample `MT_TEST01`.
5. **Full bridge CLI (`bridge/run_combined.py`)** run end-to-end: Kim's
   subprocess stage completed exactly as above and hands off the correct
   `filtered_variants.vcf` path to GEPER's subprocess stage, which starts
   correctly (loads config, begins importing GEPER's pipeline) — confirming
   the bridge's argument-passing, checkpoint-reading, and subprocess
   handoff are all correct.

## What was *not* re-validated here, and why

GEPER's own annotation stage (the second half of the combined workflow)
needs `torch` + the rest of GEPER's model stack, **and live network access
to Ensembl/ClinVar/gnomAD** to fetch sequence context and clinical evidence.
This validation was performed in a network-restricted sandbox that can
reach package registries (PyPI, npm, GitHub) but not genomic-database APIs
— so the GEPER half of the workflow could not be executed here regardless
of installed dependencies. This is a limitation of the validation
environment, not of the dataset or the bridge: the wiring between the two
stages (item 5 above) is confirmed correct, and everything Kim produces is
confirmed to be exactly what GEPER's parser expects.

**Expected behavior in your Colab environment** (where you've already
confirmed GEPER's own dependencies are installed and reachable):
- Ensembl sequence-context lookup for `MT:3243` will succeed (a real,
  resolvable coordinate).
- ClinVar lookup for `MT:3243 A>G` has a real chance of returning existing
  pathogenic evidence, since m.3243A>G is a well-documented variant.
- **[CORRECTED 2026-09-10 (angela-mszpsmyw): this bullet originally read "GEPER's PGx
  star-allele module targets nuclear pharmacogenes — expect it to report 'not applicable'
  for this mitochondrial variant, which is correct behavior, not an error." `kim_pipeline`'s
  PGx module (`pipeline/pgx/`) -- the only PGx code that ever existed in this codebase; `geper/`
  never had one, per `docs/BIJ_AI_CAPABILITY_AUDIT.md`'s own capability inventory -- was deleted
  2026-09-10 (merge `51320e1`). There is no PGx behavior, "not applicable" or otherwise, left to
  expect for this variant. Removed rather than left describing a module that does not exist;
  the pre-existing "GEPER's" attribution was itself imprecise (PGx was always `kim_pipeline`'s,
  not `geper/`'s) and is now moot either way.]**
- ACMG automation and clinical report generation should complete normally
  and produce `geper_results.json` / `geper_report.md`.

## Reproducing / re-running

```bash
# Regenerate the dataset (deterministic, seeded — byte-identical output)
python3 generate_test_dataset.py

# Run the combined workflow
python bridge/run_combined.py \
    --r1 dataset/reads_1.fastq \
    --ref dataset/reference.fasta \
    --sample-id MT_TEST01 \
    --kim-output-dir ./work/kim --geper-output-dir ./work/geper \
    --no-resume
```
