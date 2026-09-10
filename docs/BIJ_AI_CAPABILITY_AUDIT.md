# Bij AI Capability Audit — factual, from the code

Author: meredith-msydpy6m. Scope ruled by god 2026-09-05, applying the
human's existing 2026-08-22 ratification (board.md:2268): "Bij AI" names
the **combined system** — geper/ + kim_pipeline/ + bridge/ — not either
component alone. This is one audit, not two. Every capability entry below
states the **configuration it requires**, since the combined system's
capability surface is deployment-dependent: a geper/-only deployment
cannot do everything a geper/+kim_pipeline/+bridge/ deployment can.

Method: AST/body reads, not grep-and-infer (credited to Ryan, retained as
prescribed). Every claim cites `file:line` and a function/method name.
Where nothing in the repo measures a thing: **NOT BENCHMARKED**. Where the
repo cannot answer at all: **UNKNOWN — REQUIRES BENCHMARK/TESTING**. No
invented numbers, no marketing language. All paths below are relative to
`C:\Users\kisla\geper-wt\meredith-msydpy6m\` unless a `C:\Users\kisla\GEPER\`
top-level path is given explicitly (bridge/, README_INTEGRATION.md,
CROSS_TREE_DIVERGENCES.md).

---

## 0. The naming/boundary finding (documentation, not a scope decision)

geper/README.md:1 self-identifies alone as "Bij AI — Genetic Evaluation &
Prediction Engine." kim_pipeline/docs/INSTALL.md:1 and kim_pipeline/main.py's
own module docstring **also** self-identify alone as "Bij AI v8," with
main.py describing its `analyze` subcommand (full FASTQ → Report) as "the
GEPER primary workflow." `C:\Users\kisla\GEPER\README_INTEGRATION.md`
frames kim_pipeline as a separate product, "Kim," bridged to "Current Bij
AI" (geper/) — but hive/board.md:2268 ratifies "Bij AI" as the name for the
**combined system**, and the branding pass that followed only touched
document *titles*, explicitly leaving "component names" (i.e., each tree's
internal self-identification) untouched. Two component trees each
independently call themselves the whole product, under a ratified combined
name applied to nothing structural underneath it. **This is a real,
verified documentation defect, carded separately by god and sequenced with
the existing geper→bij-ai package rename. Reported here, not fixed here.**

It does not change what the system can do. Capability, not naming, is this
audit's subject.

---

## 1. Inputs, genome builds, outputs

### geper/ (VCF-first path — works with or without kim_pipeline)
- **Input**: `--vcf` is the only required argument (main.py:45,
  `build_arg_parser`), `.vcf` or `.vcf.gz` (`VCFParser._open`,
  pipeline/vcf_parser.py:296-311). Optional: `--blast-reference-fasta`,
  `--patient-meta` (JSON), `--qc-metrics-json` (JSON), `--hpo-terms`/
  `--phenotype-file`, `--blast-db`, `--assembly`, `--species`,
  `--max-variants`.
- **FASTQ/BAM/CRAM are NOT accepted by geper/ alone.**
  pipeline/raw_input_validator.py's own docstring (lines 1-20) states this
  module is "intentionally NOT wired into `GeperPipeline.run()`" — no code
  path in geper/ alone consumes these file types. This is what makes the
  combined-system framing necessary: the capability exists, but not here.
- **API**: geper/api/main.py exposes `GET /structures/{accession}`
  (AlphaFold viewer, :376-395) and `POST /interpretations` (:398-443,
  JSON body with `vcf_path` as a path string, not an upload), which queues
  a submission rather than interpreting inline. **UNKNOWN** whether the
  async worker (api/submission_worker.py) calls the same
  `GeperPipeline.run()` the CLI does — not traced.
- **Genome build**: `detect_vcf_assembly` (pipeline/assembly_validator.py:
  54-79) scans VCF header markers, falls back to contig-length
  fingerprinting (GRCh37 249,250,621 vs GRCh38 248,956,422).
  `validate_assembly`: undetermined AND no `--assembly` → raises
  `AssemblyMismatchError`, halting the run (changed 2026-09-10; it
  previously warned and proceeded, which let Ensembl's default build
  silently become the answer). Undetermined but `--assembly` given →
  warns and proceeds, because the caller has established the build.
  Detected build vs. `--assembly` disagreement → raises
  `AssemblyMismatchError`, halts before any variant is processed.
  Cited by function name rather than line number: the previous version
  of this entry carried `:92-131` and `:119-129`, both of which had
  moved.
- **Outputs**, all confirmed as actual file-write calls in
  `GeperPipeline.run` (pipeline/orchestrator.py): `geper_results.json`
  (:883,953), `geper_report.md` (:954), `geper_report_full.pdf` (:963-971,
  non-fatal on render failure), `geper_report_short.pdf` (:980-990, same),
  `geper_benchmark.json`/`.md` (:1014-1032, only if profiling enabled —
  default on). LIMS export (report/export_lims.py) is **not** part of the
  default run — only reachable via review/signoff.py's separate CLI.
- **Limits**: `EVO2_MAX_SAFE_TOKENS=8192` (config.py:227), enforced in
  `models/evo2.py::_infer_impl` (222-256) as a defensive truncate+warn
  (`truncated=True` flag), not a hard failure. `--max-variants`
  (main.py:123-131) is an opt-in debug cap via `itertools.islice`
  (orchestrator.py:709-720) — **no default cap on variant count found**.
  No file-size limit found anywhere in the ingestion path (ruled out by
  reading main.py/orchestrator.py/vcf_parser.py directly).

### kim_pipeline/ — requires kim_pipeline deployed
- **Input**: CLI `analyze` subcommand (main.py:649-714): `--r1` required
  FASTQ, `--r2` optional paired FASTQ, `--ref` required FASTA, `--mode
  {full,vcf_only}`, `--stop-after {variant_calling}`. No content/format
  validation in the argparser — existence-only check
  (`_print_startup_validation`, main.py:118-125).
- **REST API note**: `POST /api/v1/pipeline/start` requires
  `fastq_r1_path`+`reference_fasta_path` (api/main.py:310-329), but
  `_run_pipeline_sync` (:393-400) calls `runner.run()` **without** passing
  `mode=`/`stop_after=` — **`vcf_only` mode is reachable only via the CLI,
  never via the REST API**, which always runs `mode="full"`.
- **Genome build**: `SUPPORTED_BUILD = "GRCh38"`
  (pipeline/utils/genome_build.py:36). `detect_genome_build` (:54-119):
  header → contig-length lookup → weak chr-prefix guess; never raises.
  `warn_if_unsupported_build` (:122-155): **non-blocking**, logs only,
  never halts. `runner.py:601-614` runs this post-variant-calling,
  non-fatally. `config_validator.py` has **no** genome-build validation
  section at all.
- **Outputs, vcf_only mode**: `qc_report.json`/`.html`, `aligned.markdup.bam`
  +`.bai`+`alignment_metrics.json`, `variants.vcf` (raw FreeBayes),
  `variants.norm.vcf` (if bcftools available), `filtered_variants.vcf`
  (the vcf_only terminal output — confirmed by the early-return at
  `runner.py:624-633`), `checkpoint.json`, `pipeline.log`.
- **Outputs, full mode**: everything above, plus `vep_annotation/`,
  `annotation/annotation.json`, ACMG/evidence results, `ancestry/`,
  `reporting/report.json`+`report.html`+`report.pdf` (see §2).
  **[CORRECTED 2026-09-10 (angela-mszpsmyw), dated: this bullet originally
  also listed `pgx/` as a full-mode output directory. `kim_pipeline/pipeline/pgx/`
  was deleted 2026-09-10 (merge `51320e1`, the human's ruling to remove
  pharmacogenomics from the clinical report) -- no `pgx/` output exists any
  longer. Removed rather than struck through, since the claim is no longer
  even conditionally true.]**
- **Limits**: API upload size `_MAX_UPLOAD_MB` env, default 2048MB,
  enforced by streaming chunk check (api/main.py:640-658), HTTP 413 on
  exceed. QC memory guards (not pipeline-halting): `max_dup_sample=100_000`,
  `max_perbase_reads=50_000` (qc/stage.py:297-298,634). `bcftools norm`
  subprocess timeout 300s. **No file-size, read-count, or wall-clock limit**
  anywhere in the CLI path or `PipelineRunner.run()` itself.
- **QC gate halts the pipeline by default**: `qc.stop_on_failure` defaults
  `True` (qc/stage.py:635; config/default.yaml:133). On failure,
  `QCThresholdError` propagates through `runner.py:489-491` and aborts the
  entire run — alignment and variant calling never execute. Thresholds
  checked (qc/stage.py:69-78,718-763): mean Phred quality ≥20.0, N-base
  fraction ≤0.1, GC fraction 0.2-0.8, adapter contamination ≤0.5, estimated
  duplicate fraction ≤0.8, total read count ≥100, mean read length ≥25bp.

### bridge/ — requires kim_pipeline + geper/ + bridge/ deployed together
`run_combined.py`'s CLI (argparse :44-85): required `--r1`, `--ref`,
`--sample-id`, `--kim-output-dir`, `--geper-output-dir`; optional `--r2`,
`--kim-python`/`--geper-python` (separate interpreters), `--blast-mode`,
`--assembly` (auto-filled from Kim's detected build if the caller doesn't
supply one — `combined_pipeline.py:253-277,536-538` — real conditional
logic, not blind passthrough), `--max-variants`, `--hpo-terms`. **This is
the fact that settles the boundary question for capability purposes**: a
user can type one command today and get FASTQ → clinical report — a
working entry point that a geper/-only audit would have omitted.

---

## 2. The real processing chain, end to end

### geper/ VCF → report (requires only geper/)
1. `main.py:206 main()` → args, HPO phenotype build, constructs
   `GeperPipeline`.
2. `GeperPipeline.run()` (orchestrator.py:686): parse VCF, assembly
   preflight (:733), Ensembl batch prefetch (:747), BLAST batch prefetch
   (:771).
3. Per variant, `_process_variant()` (:1437): normalize → sequence context
   → model routing + DNA-model inference → ClinGen+transcript resolution →
   RNA-FM → ESM2 → (mtDNA-gated) AlphaMissense/MMSplice/Enformer-Borzoi/
   SpliceFormer-SpliceBERT → BLAST → dbSNP → ClinVar → gnomAD (mtDNA-gated)
   → conservation/HPO/Orphanet/functional evidence → UniProt → InterPro →
   AlphaFold DB (gated on gene biotype) → `InterpretationEngine.interpret()`
   (:1615) → internally ACMGRuleEngine → ConfidenceEngine →
   ConflictResolutionEngine → ExplainabilityEngine.
4. Serialize to the four output files (§1).

**Five README-claimed pretrained models — all real, non-stubbed inference**
(models/base_model.py contract, models/__init__.py:92-98 registry):
- **HyenaDNA** (models/hyenadna.py, `_infer_impl`:392) — real forward pass.
  **The router's default**: `pipeline/router.py:44-55` falls back to
  HyenaDNA whenever no other rule fires — confirms the "universal default"
  claim.
- **Evo2** (models/evo2.py, `_infer_impl`:222) — real forward pass. Requires
  CUDA compute capability ≥8.0 (Ampere+); `is_available()` (:120) is
  `False` on CPU-only or Turing (e.g. T4) GPUs; **no CPU fallback**
  (docstring :32-42).
- **RNA-FM** (models/rna_fm.py, `_infer_impl`:512) — real forward pass,
  3-tier weight-loading fallback.
- **ESM2** (models/esm2.py, `_infer_impl`:43) — real forward pass.
- **AlphaMissense** (models/alphamissense.py) — **not a loaded neural
  net**: DeepMind never released trained weights (docstring 1-56). This is
  a tabix-indexed lookup against DeepMind's ~71M-row precomputed catalogue
  (`_infer_impl`:628, shells to `tabix`) — a disclosed, deliberate design
  choice matching how the official Ensembl VEP plugin integrates it, not a
  stub; still returns a real per-variant score.

**Database integrations** — live+cached hybrid for ClinVar, dbSNP, BLAST+,
gnomAD, ClinGen, UniProt, InterPro, AlphaFold DB, ClinGen ERepo, MaveDB
(each has a `provider.py`/`client.py` live-network call + a `cache.py`
disk cache). **Ensembl is different**: a local, self-bootstrapped
JSON-lines index, deliberately cache-only (pipeline/ensembl/provider.py
docstring :30-35) — live REST fallback lives in the *callers*
(pipeline/pvs1/lookup.py, pipeline/clingen/utils.py), not this module.

**ACMG criteria** (pipeline/acmg_rules.py, `ACMGRuleEngine.evaluate()`:782):
**19 implemented** with real per-criterion logic (PVS1, PS1, PM1, PM2, PM4,
PM5, PP3/BP4, BA1/BS1, BP7, PP1, BS4, PS3, BS3, BP1, BP3, BP6, PP4).
**9 never evaluated**, each with a stated reason
(`_NEVER_INTEGRATED_ACMG_CODES`:291, `_not_evaluated`:933-971): PS2, PM3,
PM6, PP2, PP5, BS2, BP2, BP5, PS4. PP5's reason (:953-958): ClinGen SVI
Working Group (Biesecker & Harrison 2018) circularity. PS4 has real code
(`_ps4`:1481) but **always** returns `not_evaluated` by design (:1508) —
case-frequency data is structurally unintegrated. 7 more are
conditionally `not_evaluated` for mitochondrial variants specifically
(:134-179).

**Confidence/Conflict/Explainability engines** — all real computation,
none stubbed, wired via pipeline/interpretation.py::InterpretationEngine
(:55-61,358). `ConflictResolutionEngine` explicitly does **not** alter the
ACMG classification (docstring :5-12). `ExplainabilityEngine` explicitly
"computes nothing new" (:7) — pure reorganization of already-produced
fields.

**Mandatory human review/signoff — narrower in code than the prose claims.**
review/signoff.py's `require_reviewed()` (:205-296) is real, enforced code
— but its own docstring (:242-247) states it is a gate for **automated
consumption only**. Today it has exactly **one** real caller path:
report/export_lims.py's LIMS-export wrapper. **The PDF/Markdown/JSON a
clinician opens directly is not code-gated** — it carries a "DRAFT" vs.
"reviewed" banner for a human to read (:100-108,244-247); nothing in code
stops acting on an unreviewed draft. The trust boundary is stated as
deliberate (:119-136): SHA-256 manifest is write-once and never
re-verified, `--clinician-name`/`--reg-number` are unvalidated free text,
no identity verification on who calls approve/override, no database. So:
`README_INTEGRATION.md`'s "no output is clinically actionable before
mandatory review/sign-off" is enforced in code **only** for the automated
LIMS path — for a human reading the report, the control is a banner.

### kim_pipeline/ FASTQ → VCF (requires kim_pipeline)
`PipelineRunner.run()` (pipeline/orchestration/runner.py): config
validation → reference resolve → FASTQ validation → **QC** (halts by
default on failure, §1) → **Alignment** (`AlignmentStage.run()`:128;
`_resolve_aligner()`:98-126 — **both bwa and minimap2 are real, working
subprocess-invoking aligners**, neither a stub; "auto" prefers bwa if both
installed; no cross-aligner fallback) → **Variant Calling**
(`VariantCallingStage.run()`:77-229 — **FreeBayes is the only caller, no
substitute**, raises if missing) → `bcftools norm` (best-effort) →
`apply_pass_filter()` → `filtered_variants.vcf`. Early-return at
`runner.py:624-633` if `stop_after=="variant_calling"` — confirmed exactly
matching README_INTEGRATION.md's vcf_only claim.

### kim_pipeline/ own annotation/AI/ACMG/ancestry/report stack (requires kim_pipeline, `full` mode)
This is a **separate, independently-implemented** stack from geper/'s —
not the same code, not shared.

> **DATED AMENDMENT, 2026-09-10 (angela-mszpsmyw).** This section's heading
> originally read "...annotation/AI/ACMG/PGx/ancestry/report stack", and the
> bullet immediately below described PGx as a real, working stage,
> verbatim: *"**PGx** (pipeline/pgx/) and **Ancestry** (pipeline/ancestry/)
> — real, working stages with **no equivalent in geper/ at all**:
> star-allele calling for 10 pharmacogenes with CPIC drug implications,
> and population-ancestry estimation from a 101-marker AIM panel. Both
> disclose real limitations in their own code (PGx: no independent
> ground-truth validation, PharmVar licensing; ancestry: presence/absence
> marker matching, not true genotype dosage)."* That was accurate when
> written. **`kim_pipeline/pipeline/pgx/` was deleted 2026-09-10 (merge
> `51320e1`), the human's ruling to remove pharmacogenomics from the
> clinical report and delete the computation.** The heading and bullet
> below are corrected to describe only what remains (Ancestry); this is
> a documentation-currency fix, not a re-litigation of the deletion
> itself, which is the human's ruling and stands.

- **AI models**: only **two** — DNABERT-2 and ESM-2
  (pipeline/ai/engine.py:173,310) — both real inference (thinner than
  geper/'s five-model stack, but neither a stub).
- **ACMG classifier** (pipeline/acmg/classifier.py) implements **26 of 28**
  criteria with real logic; PS4 and BS2 are permanently
  `STATUS_NOT_EVALUATED` (automated-mode data unavailability, :457-470,
  993-1004).
- **Ancestry** (pipeline/ancestry/) — a real, working stage with **no
  equivalent in geper/ at all**: population-ancestry estimation from a
  101-marker AIM panel, disclosing a real limitation in its own code
  (presence/absence marker matching, not true genotype dosage).
- **Evidence aggregation** (pipeline/evidence/aggregator.py) is explicitly
  disclosed as exploratory-only and double-counting relative to the ACMG
  score (:14-38,146-161) — never a second independent clinical score.
- **Reporting**: one `report.json`/`.html`/`.pdf` triple per sample
  (ReportLab → WeasyPrint → wkhtmltopdf fallback chain) — a **different
  output shape** from geper/'s four files (no split short/full PDF, no
  Markdown, but includes an ancestry section geper/ has no equivalent
  for). kim_pipeline's own report disclaimer text independently
  requires "qualified human review and final sign-off" — **not verified
  whether this is unified with or fully independent from
  geper/review/signoff.py's gate**; flagged as unconfirmed.

### bridge/ (requires kim_pipeline + geper/ + bridge/)
`combined_pipeline.py::run_combined()` (:472-566) runs the two projects as
**two subprocess calls with real coupling logic between them** — not the
"zero in-process work" README_INTEGRATION.md's prose might suggest:
- Runs Kim (`analyze --mode vcf_only`), checks exit code, reads Kim's
  `checkpoint.json`, and **validates the resulting VCF actually exists on
  disk** (:352-358) before proceeding.
- Translates Kim's QC-metrics schema into GEPER's sidecar shape
  (`qc_metrics_from_kim_checkpoint`:138-225 — real data transformation,
  non-fatal on translation error).
- Auto-fills geper/'s `--assembly` from Kim's detected build **only** if
  the caller didn't supply one explicitly (:253-277,536-538).
- Runs geper/ (`main.py --vcf ...`), checks exit code, returns result paths
  only if they exist on disk.
- If Kim's stage raises, geper/'s stage is **structurally never invoked**
  (same try block). No retry logic anywhere.
- **Test-evidence caveat**: bridge/tests/test_bridge.py (594 lines,
  ~20+ test methods) replaces **both** Kim's and geper/'s real `main.py`
  with fake stand-in scripts (docstring :4-18, explicit about this). It
  proves the bridge's *wiring* — argument passing, failure propagation,
  path-resolution correctness, QC-schema translation, assembly forwarding
  — but **no real biology runs in this test suite**.
  `docs/BWA_INDEXING.md:235-236`'s "7/7 passing" citation is stale relative
  to the file's current ~20+ methods (a documentation-currency finding).
  **See the 2026-09-05 dated amendment at the end of this section for a
  correction to this entry's original claim about real end-to-end runs.**

> **DATED AMENDMENT, 2026-09-05.** This section originally stated: "no
> document anywhere in the repo claims a real (non-mocked) end-to-end
> bridge run has ever happened." **That sentence is too strong and is
> corrected here, not silently edited**, per this document's own
> discipline of not stating a number/claim it cannot point to.
>
> Ryan found `test_data/README_TEST_DATASET.md`, section "Self-validation
> performed" (lines 60-98), while scoping a real bridge run for unrelated
> work. It **claims** a full Kim run on unmodified production code, in a
> sandboxed Linux environment with real bwa 0.7.17, samtools/bcftools
> 1.19, and freebayes 1.3.6 installed — 1825 records, 100% of reads
> mapped in 0.18s, a real `filtered_variants.vcf` with one PASS record
> (m.3243A>G at MT:3243, DP=17, QUAL=593.383 — a real, clinically known
> variant, not a synthetic marker) — and that the bridge CLI ran this
> through to Kim's subprocess completing and handing the correct VCF path
> to geper/'s subprocess stage. The same document, lines 99-112, states
> **geper/'s half was not executed** — it needs torch, the model stack, and
> live network access to Ensembl/ClinVar/gnomAD, and the validation
> sandbox could reach PyPI but not the genomic databases; geper/'s
> pipeline was observed to *start* and never to *finish*.
>
> **The honest form, replacing the original sentence**: the bridge has
> never been exercised through to a report, and its geper/ half has never
> run at all. One half (Kim's, real internals) has a documented claimed
> run; the other half (geper/'s) has never started.
>
> **This is a self-reported claim in a test-data README, not a verified
> run and not code.** Nobody involved in this amendment — Ryan or I — ran
> it, watched it run, or verified the artifacts it describes still exist
> or reproduce. It is evidence that someone once claimed this happened,
> cited to its exact location, no stronger than that.
>
> The **NEEDS BENCHMARKS TO KNOW** entry on whether the bridge functions
> against real, non-mocked internals end to end stands unchanged and is,
> if anything, more precisely UNKNOWN now: one half is claimed (unverified
> by this audit), the other has never started.
>
> **Scope-of-claim note, for the general case**: the original sentence was
> a claim of the form "no document anywhere says X" — an assertion about
> an entire corpus, stronger than a code-scoped method can support. This
> audit was scoped to establishing capability from the code; Ryan's find
> was in a test-data README, reached by a different question (what a real
> run would require), not a gap in this audit's method. Worth naming as
> the general lesson: a "nowhere" claim needs a search scoped as wide as
> the claim itself, or it should be phrased as "not found in the code
> paths this audit read," not "nowhere."

---

## 3. The cross-tree ACMG divergence — established as a finding, not left as background

`C:\Users\kisla\GEPER\CROSS_TREE_DIVERGENCES.md` documents that geper/'s
and kim_pipeline/'s ACMG stacks disagree on two criteria. **Under the
ratified combined-system name, this is one product producing two different
classifications for the same variant depending on which entry point ran
it** — a first-class finding, not background. I read both engines directly
to establish which side is better-grounded, per instruction ("establish
which, do not assume either").

**PP5/BP6** (ClinVar's own classification as supporting evidence):
- geper/ (pipeline/acmg_rules.py:953-958): PP5 **never evaluated**, citing
  ClinGen SVI Working Group (Biesecker & Harrison 2018) by name, on
  circularity grounds — a source's own classification cannot independently
  support the classification derived from it. BP6 is evaluated (can be
  flagged "triggered") but explicitly excluded from the point-combining
  loop, so it contributes zero regardless.
- kim_pipeline/ (pipeline/acmg/classifier.py:872-933,1230-1269): PP5 and
  BP6 both **ON by default** (`disable_pp5_bp6=False`). The classifier's
  own docstring (:887-889) **names the same SVI recommendation and
  declines to follow it** — stated reason: "to preserve existing behavior
  and test expectations."
- **My finding**: this is a genuine, deliberate divergence on both sides —
  neither is an accidental bug, both are documented, working-as-designed
  code paths. But the two sides are **not equally grounded**. geper/'s
  choice is justified by a specific clinical-methodology citation. Kim's
  choice for PP5/BP6 is justified by implementation continuity
  (preserving existing behavior/tests), while its own docstring
  acknowledges the clinical recommendation against it. That is a weaker
  basis for a clinical-classification default than geper/'s, by the
  standard kim_pipeline's own code states. **I am not overriding this or
  calling it a bug — that is a design decision belonging to whoever owns
  kim_pipeline's ACMG defaults — but the audit should not present the two
  sides as equally justified when kim's own comment says otherwise.**

**PP3/BP4** (in-silico predictor combining rule):
- geper/ applies "any dissent blocks the claim" — if computational
  evidence disagrees (some predictors damaging, some benign), **neither**
  PP3 nor BP4 triggers. Docstring states this was a considered choice,
  citing ACMG/AMP 2015 and Pejaver et al. 2022, and explicitly rejecting
  majority voting.
- kim_pipeline/ (classifier.py:796-840,1160-1197) does **strict-majority
  voting** instead (`n_dam >= max(1, len(votes)//2+1)`).
- **Additional finding, not in CROSS_TREE_DIVERGENCES.md**: kim_pipeline's
  in-silico ensemble for this vote is effectively **three** predictors in
  current deployment (CADD, REVEL, AlphaMissense), not four — SpliceAI was
  removed 2026-08-22 for CC BY-NC 4.0 licensing (pipeline/vep/stage.py
  docstring:13-25), but the PP3/BP4 vote-counting code still references it
  as a source; it simply always contributes `None` now (each append
  guarded `is not None`, `classifier.py:814-815,1171-1172` — the removal
  degrades silently rather than raising). **This is not a cosmetic
  change: the majority threshold at `classifier.py:831`
  (`met = n_dam >= max(1, len(votes)//2+1)`, symmetric BP4 site :1188)
  fell from 3-of-4 damaging calls required to 2-of-3 — a licensing
  decision silently lowered the evidence bar for a pathogenicity
  criterion.** See §1 (CANNOT DO TODAY, the expanded SpliceAI entry) for
  the full consequence and status: this is already known (Kelly's cards
  `kelly-classifier-denominator-fix-pp3-bp4` and
  `kelly-pp3-bp4-invariance-vs-single-predictor-incompatibility`, both
  waiting) and referred out to the unfilled clinical-expert role, not
  overlooked or newly discovered here.
- **My finding**: CROSS_TREE_DIVERGENCES.md itself already states this one
  is "unresolved by design, referred to clinical expert review" on both
  sides — I found nothing in either engine's code that changes that
  characterization; both sides cite defensible methodological positions,
  and I am treating this as a genuine open clinical question rather than
  ranking one design over the other, unlike PP5/BP6 above where one side's
  own comment concedes the weaker ground.

---

## 4. Capacity, concurrency, measured runtimes, bottlenecks

### geper/
- **No hard variant-count cap.** Sequential per-variant loop
  (orchestrator.py:871); `--max-variants` is opt-in debug only.
- **Model inference is not parallelized or batched** — sequential loop,
  each model call takes exactly one sequence (evo2: `unsqueeze(0)` = batch
  size 1). **No GPU batching anywhere.**
- **Concurrency exists only for I/O-bound external lookups**: BLAST
  (`ThreadPoolExecutor`, default 3 remote/`os.cpu_count()` local),
  ClinGen/InterPro/HPO/UniProt/conservation providers (`ThreadPoolExecutor`,
  default 8), gnomAD (both `ThreadPoolExecutor` and a real `asyncio`
  path).
- **Measured runtimes actually recorded in-repo** (both explicitly
  sandbox-scale, not production hardware, both self-disclaimed as such):
  - BLAST (PERFORMANCE_REPORT.md:116-127): 12-variant synthetic VCF,
    remote BLAST *simulated* at 0.5s/submission (not real NCBI latency).
    Serial 4.51s → concurrent+cache 1.56s → cached rerun 0.01s.
  - gnomAD (PHASE2_GNOMAD_SUMMARY.md:125-139): 300-variant synthetic
    fixture, **real** local tabix subprocess. Sequential cold 700.8ms
    (428.1 var/s) → threaded batch 451.0ms (665.1 var/s) → warm-cache
    0.4ms (671,058.4 var/s). Document's own caution: "not... a production-
    hardware capacity-planning number."
  - **NOT BENCHMARKED anywhere in-repo**: benchmark_clingen.py,
    benchmark_local_blast.py (remote side is a `time.sleep()` stand-in on
    NCBI's *published* range, not measured), benchmark_spliceformer.py
    (own docstring: no GPU/network route, any number "would be a
    fabricated guess"). No end-to-end full-pipeline wall-clock number
    exists anywhere.
- **Bottleneck, per the repo's own evidence**: remote BLAST —
  "commonly 30s to several minutes" per submission, "10-100x the combined
  cost of every other stage" on any VCF with more than a handful of
  variants (PERFORMANCE_REPORT.md:19-24). Evo2 has a **hard architectural
  floor**: no Turing GPU support (compute capability ≥8.0 required), no
  CPU fallback, independently confirmed against upstream FlashAttention-2
  and Arc Institute docs (EVO2_T4_HARDWARE_FINDINGS.md:39-61). The gnomAD
  benchmark's own evidence: caching (1567.6×) is a far larger lever than
  concurrency (1.6×) for the one measured workload.
- **Deployment fact, measured on both sides: first-run model acquisition
  vs. a baked-image load.** This is not a performance note — it is the
  evidence for the standing on-premise packaging requirement (a
  deployment with no internet access must ship the model weights baked
  into the image; a deployment that lets each model download on first use
  cannot function without network access at all, let alone quickly):

  | Model    | First run (acquisition-dominated) | Baked image (offline) | Ratio |
  |----------|-----------------------------------:|-----------------------:|------:|
  | RNA-FM   | 1147.2s                            | 8.7s                   | 132×  |
  | ESM2     | 1601.4s                            | 12.3s                  | 130×  |
  | HyenaDNA | 157.6s                             | 14.7s                  | 11×   |
  | MMSplice | 0.7s                               | 0.5s                   | ~1×   |

  **Total, baked and offline, all four: ~36 seconds.** The first-run
  total is ~47 minutes and requires live network access throughout.

  Provenance, kept explicit because it matters for what these numbers
  can and can't support: the first-run figures are `Loaded 'X' ... in
  Ns` loader log lines (geper/logs/geper.log) — success-conditional by
  construction (a failed load emits no such line), **not** rows from the
  per-stage profiler table this audit deliberately does not cite
  elsewhere in this section (that table's `finally`-based recording
  cannot distinguish a stage that succeeded from one that raised, so no
  number from it is used here or anywhere else in this document). The
  baked-image figures are `_load_impl()` runs of each model inside
  `geper:bridge-ready`, quoted by digest —
  `sha256:a8a5fe67749e38206cbd188487cc34b95bd7609cc99f3e234030c707c5f6af94`,
  not the mutable `bridge-ready` tag — under `--network none`, each
  producing an actually-constructed model object confirmed in the run
  log (ESM2: 651.0M params; MMSplice: five named submodels loaded from
  the vendored package path; HyenaDNA and RNA-FM: `_verify_materialized():
  OK`), not an import or a listing.

  **What this does and does not establish, stated plainly:** the
  first-run figures are acquisition time (network fetch dominated), not
  load time on an already-warm cache — conflating the two is the exact
  mistake that led to two of these four models almost going unmeasured
  on the estimate that testing them would cost ~21 minutes; measuring
  them for real cost 23 seconds. Both sides are **one machine, one run
  each** — no variance, no second sample, no statistical claim implied.
  The baked-image figures are from **one specific image**, identified by
  digest, not a general claim about any `bridge-ready`-tagged image at
  any point in time. And this measures **startup cost only** — it says
  nothing about per-variant annotation throughput once models are
  already loaded, which remains NOT BENCHMARKED (see §5's NEEDS
  BENCHMARKS TO KNOW).

### kim_pipeline/
- **No read/file-size/variant-count limit** anywhere in
  runner.py/bwa_runner.py/minimap2_runner.py/freebayes_runner.py.
- **Concurrency is real**: bwa and minimap2 both receive `-t N` (default 4)
  — genuine OS-level multithreading delegated to the binary. **FreeBayes is
  single-threaded by design**; real parallel calling needs
  `freebayes-parallel` + `fasta_generate_regions.py` both on `PATH`, and
  **silently falls back to single-process with a logged warning** if
  either is missing (:116-121) — and defaults to `threads=1` regardless
  (stage.py:104), unlike bwa/minimap2's default of 4. gnomAD lookups use a
  real `ThreadPoolExecutor`.
- **Measured runtimes**: only docs/BWA_INDEXING.md has any recorded
  number, and it is explicit that it used a **150MB synthetic FASTA** on
  "this sandbox: 1 vCPU, no GPU" — a real 900MB human-scale genome
  "couldn't be safely built in this sandbox" (:136). Persistence (caching
  the built index) is the entire measured lever: 168.6s → 6.8s on reuse.
  **NOT BENCHMARKED anywhere**: actual alignment runtime, FreeBayes
  runtime, gnomAD lookup latency, annotation runtime, or any end-to-end
  FASTQ→report time on a real (non-synthetic) genome.
- **Bottleneck per repo's own evidence**: BWT/suffix-array construction is
  the dominant indexing cost; the reported user pain point was re-indexing
  every session (no persistence), not the per-run cost itself.

---

## 5. Four lists

### CAN DO TODAY

*geper/-only deployment (VCF-first):*
- Accept a VCF (.vcf/.vcf.gz), auto-detect or validate its genome build
  (GRCh37/GRCh38, halting on a real mismatch), and score every variant
  through five real pretrained models (HyenaDNA default, Evo2 on
  supported GPUs, RNA-FM, ESM2, AlphaMissense as an indexed lookup).
  [main.py:45; models/*.py; pipeline/assembly_validator.py]
- Query 9 live+cached external databases per variant (ClinVar, dbSNP,
  BLAST+, gnomAD, ClinGen, UniProt, InterPro, AlphaFold DB, ClinGen
  ERepo/MaveDB) plus a local Ensembl transcript index.
  [database/*.py; pipeline/{clingen,gnomad,ensembl,...}/provider.py]
- Evaluate 19 of 28 ACMG criteria with real per-variant logic; compute a
  confidence score, detect evidence conflicts, and generate a plain-
  language explainability trace. [pipeline/acmg_rules.py:782;
  pipeline/{confidence_engine,conflict_resolution_engine,
  explainability_engine}.py]
- Produce a machine-readable JSON result, a Markdown report, and two PDF
  reports (full and short) per run. [pipeline/orchestrator.py:883-990]
- Truncate (not fail) an oversized Evo2 input at an 8,192-token safety
  ceiling. [config.py:227; models/evo2.py:222-256]
- Block a LIMS export of an unreviewed report via a real code gate.
  [review/signoff.py:205-296]

*Requires kim_pipeline deployed:*
- Accept paired or single-end FASTQ + a FASTA reference via the CLI, run
  real QC (halting by default on documented threshold failures), align
  with a real bwa **or** minimap2, call variants with FreeBayes (the only
  caller), normalize/filter with bcftools, and produce a
  `filtered_variants.vcf`. [main.py:649-714; pipeline/qc/stage.py:635-763;
  pipeline/alignment/stage.py:98-128; pipeline/variant_calling/stage.py:
  77-229]
- In `full` mode (not the vcf_only path), additionally: annotate with a
  real gene/transcript/HGVS pipeline, score variants with two real AI
  models (DNABERT-2, ESM-2), classify 26 of 28 ACMG criteria (with
  PP5/BP6 on by default and PP3/BP4 via strict-majority voting — see §3
  for how these differ from geper/'s defaults), estimate ancestry from a
  101-marker panel, and produce its own JSON/HTML/PDF report.
  [pipeline/{annotation,ai,acmg,ancestry,reporting}/*.py]
  **[CORRECTED 2026-09-10 (angela-mszpsmyw): originally also listed
  "call pharmacogenomic star-alleles/phenotypes for 10 genes" and
  included `pgx` in the module-path list. `pipeline/pgx/` was deleted
  2026-09-10 (merge `51320e1`) -- see §2's dated amendment.]**

*Requires kim_pipeline + geper/ + bridge/ deployed together:*
- Run one command (`bridge/run_combined.py`) and get FASTQ → clinical
  report end to end: kim_pipeline produces the VCF (subprocess), the
  bridge validates and translates the handoff (QC metrics, genome build),
  and geper/ produces the interpretation (subprocess). This is a real,
  working entry point today. [bridge/run_combined.py:44-85;
  bridge/combined_pipeline.py:283-566]
  **This bullet is silent on what running that one command actually
  requires of the host — deliberately not duplicated here.** See
  [DOCKER.md](../DOCKER.md): "Before you build" (:22-79, the three
  pre-fetched torch wheels a fresh clone does not contain — the same
  files this repository's `.gitignore:124-126` excludes and
  `Dockerfile:237-239` `COPY`s by exact name), "What's baked into the
  image vs. what's still a first-run cost" (:215-228, the invoked
  system binaries and the volume-vs-baked-in model weights), "One
  shared venv, not the `--kim-python`/`--geper-python` split" (:594-613,
  the bridge's two-interpreter flags currently point at one merged
  environment), and "Verified vs. not yet verified" (:726-734, **this
  combined image has never actually been built or run** — only
  statically reviewed, per that section's own words).

### CANNOT DO TODAY
- geper/ alone cannot ingest FASTQ/BAM/CRAM — confirmed by the repo's own
  code comment stating the relevant validator is deliberately unwired.
  [pipeline/raw_input_validator.py:1-20]
- Neither engine evaluates PS4 (case/control cohort frequency) or BS2/
  (kim's naming) healthy-adult-observation data — both explicitly and
  permanently `not_evaluated` in automated mode on both sides.
  [geper: acmg_rules.py:291; kim: classifier.py:457-470,993-1004]
- **CORRECTED 2026-09-10** (this row previously said the SpliceAI score
  "is always `None`", citing only a code comment as evidence -- it is not
  always `None`, and the corrected claim below cites the code that proves
  it). kim_pipeline's VEP-CSQ path for SpliceAI was deliberately fixed to
  permanently return `None` (2026-08-22, licence) -- but that fix is what
  makes a SEPARATE, un-fixed fallback guard (`if var.spliceai_score is
  None:`) unconditionally true, and that fallback reads `SpliceAI=` /
  `DS_AG` / `DS_AL` / `DS_DG` / `DS_DL` straight out of the input VCF's
  own INFO field and takes the max.
  [kim_pipeline/pipeline/annotation/stage.py:733-743, :1145-1157 --
  CODE, not the vep/stage.py:13-25 module comment this entry previously
  cited, which asserts the same "always `None`" mistake]. Scope:
  kim_pipeline only (not geper/, which never integrated SpliceAI at all).
  On any input VCF the caller has already run through Illumina's SpliceAI
  tool upstream (a standard splicing-analysis step, independent of VEP),
  this fallback fires and produces a real, non-`None` `spliceai_score`
  that reaches PP3/BP4 exactly like any other predictor. Evidence, not
  just a reading of the code: `kim_pipeline/tests/
  test_undetermined_aa_guard.py:388-389` (added 2026-08-28) documents and
  exercises exactly this path, and currently PASSES.
  **Consequence, not just absence**: PP3/BP4's majority-vote threshold
  (`classifier.py:831`, symmetric BP4 site `classifier.py:1188`) is
  `met = n_dam >= max(1, len(votes) // 2 + 1)` — a threshold computed over
  however many voters are actually present (each vote append is guarded
  `is not None`, so a missing predictor degrades silently rather than
  raising, `classifier.py:814-815,1171-1172`). With SpliceAI present that
  was 3-of-4 damaging calls required; with it absent (the VEP-CSQ path,
  unannotated input), it is 2-of-3. **Given the 2026-09-10 correction
  above, "absent" is not the universal case**: on input the caller has
  pre-annotated with SpliceAI upstream, the fallback restores the vote
  and the threshold reverts to 3-of-4 for that variant. So the actual
  consequence is an evidence bar that silently VARIES per-input (2-of-3
  or 3-of-4, depending on what the caller's VCF happens to carry) rather
  than a single, permanently-lowered 2-of-3 — an inconsistency, not just
  a reduction. **A licensing decision silently changed the evidence bar
  for a pathogenicity criterion** — nobody chose that as a clinical
  position, it is a side effect of removing one voter from a
  majority-of-present formula.
  **This is known and unresolved, not a fresh finding of this audit**:
  Kelly found it first, and found the deeper version — cards
  `kelly-classifier-denominator-fix-pp3-bp4` and
  `kelly-pp3-bp4-invariance-vs-single-predictor-incompatibility` (both
  waiting). Her finding: the obvious "count only present votes" fix is a
  no-op (votes never contains `None` to begin with — the guard already
  does that), because the actual problem is structural — a
  majority-of-present-voters formula cannot simultaneously guarantee (a)
  single-predictor sufficiency (pinned by five existing tests) and (b)
  invariance under predictor removal (pinned by four of Pam's tests); the
  two requirements are incompatible with each other, not just with
  SpliceAI's removal specifically. **Status: referred out, not overlooked.**
  The human declined to rule and referred it to the clinical-expert role,
  since overriding geper/'s own written PP3/BP4 rejection of majority
  voting (§3 above) is a literature judgement, not an implementation
  choice this floor can make. Blocked on that role being filled, not on
  engineering.
- MMSplice (geper/) refuses to score multi-nucleotide variants by default
  — SNV/insertion/deletion only. [config.py:457-467]
- Neither tree's mandatory-review gate blocks a clinician from acting on
  an unreviewed report read directly (PDF/MD/HTML/JSON) — both are
  banners for a human to read, not code blocks; only the automated
  LIMS-export path is actually gated in geper/. [geper: review/signoff.py:
  242-247; kim: pipeline/reporting/stage.py:713-719 — not verified whether
  these two gates are unified]
- The kim_pipeline REST API cannot run vcf_only mode — it always runs
  `full`; only the CLI exposes the mode switch. [api/main.py:393-400]
- The bridge has never been exercised through to a report, and its geper/
  half has never run at all. Its own test suite mocks both projects'
  entry points entirely [bridge/tests/test_bridge.py:4-18]. Separately,
  `test_data/README_TEST_DATASET.md:60-98` **claims** (self-reported, not
  a verified run, not code) a real end-to-end Kim-side run with real
  bwa/samtools/bcftools/freebayes producing a real filtered VCF, handed
  correctly to geper/'s subprocess stage — but the same document,
  `:99-112`, states geper/'s half was never executed (missing torch/model
  stack/live database access in that sandbox). See the dated amendment in
  §2's bridge/ subsection for the full correction and citation.

### COULD DO AFTER ENGINEERING
- Age-conditional retention logic aside (a separate, already-carded
  finding from the counsel-shells work, not part of this audit), nothing
  here requires new engineering to reach an already-latent capability —
  most gaps found are either deliberate design boundaries (with stated
  reasons) or missing input data classes (PS4/BS2's cohort data), not
  half-built code.
- Reconciling geper/'s and kim_pipeline's ACMG defaults (PP5/BP6, PP3/BP4)
  into one behavior for the combined system, if desired, is an
  engineering + clinical-policy decision — the code for both sides
  already exists and works; what's missing is a decision about which
  default the combined product should present, and that is exactly the
  kind of call this audit is not positioned to make on its own.
- Wiring kim_pipeline's REST API to accept `mode`/`stop_after` (parity with
  its own CLI) is a small, scoped, mechanical change if the vcf_only path
  needs to be reachable over HTTP.

### NEEDS BENCHMARKS TO KNOW
- **Everything about real-world capacity and runtime.** No number in this
  repo is measured on production hardware, a real human genome, or a
  clinically realistic variant count, on either side of the bridge:
  - geper/: real Evo2 forward-pass time on a supported GPU, real remote-
    BLAST production latency, real gnomAD/ClinGen live-network latency,
    end-to-end wall-clock for a full clinical VCF. NOT BENCHMARKED.
  - kim_pipeline/: real alignment runtime (as opposed to indexing), real
    FreeBayes calling runtime, real annotation-stage runtime, indexing
    time on an actual 900MB human reference (the repo's own doc says this
    couldn't be safely attempted in its sandbox). NOT BENCHMARKED.
  - The combined bridge/ path end to end, on any real dataset. NOT
    BENCHMARKED by this audit — one half (Kim's, real internals) has a
    self-reported, unverified claim (`test_data/README_TEST_DATASET.md:
    60-98`); the other half (geper/'s) is documented in the same source
    (`:99-112`) as never having run at all. See §2's bridge/ subsection,
    dated amendment.
  - Variant-capacity ceiling for either engine under realistic
    memory/wall-clock constraints — no code-enforced cap exists on either
    side, so the practical ceiling is a hardware question this repo does
    not answer. UNKNOWN — REQUIRES BENCHMARK/TESTING.
  - Whether ancestry estimates are accurate against independent ground
    truth — the stage discloses in its own code that this has not been
    established. UNKNOWN — REQUIRES BENCHMARK/TESTING.
    **[CORRECTED 2026-09-10 (angela-mszpsmyw): originally also asked this
    of "PGx diplotype calls". `pipeline/pgx/` was deleted 2026-09-10
    (merge `51320e1`) -- see §2's dated amendment. The question no longer
    applies to a stage that does not exist; removed rather than left
    open against nothing.]**
  - Whether the bridge actually functions against the real (non-mocked)
    Kim and geper/ binaries/models end to end, producing a report — no
    verified run exists; one half has an unverified self-reported claim,
    the other half has never been attempted. UNKNOWN — REQUIRES
    BENCHMARK/TESTING, more precisely stated now than before the
    2026-09-05 amendment: it is not a symmetric unknown across both
    halves.
