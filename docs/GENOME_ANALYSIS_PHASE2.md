# Genome Analysis — Phase 2: CNV and structural-variant support

Status: implemented as a backend model, ingestion interface, map builder, clinician
API and a standalone explorer page. **Storage for CNV/SV call sets is implemented in
migration 0010 and the data-access layer, in the working tree only: not committed, not
deployed, not applied to any real clinical database** (see
`GENOME_ANALYSIS_CALLSET_IMPLEMENTATION_REVIEW.md` §17). **Nothing in GEPER detects
CNVs or structural variants.**

## What exists, honestly

| Capability | State |
|---|---|
| FASTQ → QC → alignment → variant calling (`kim_pipeline`) | Small variants only (FreeBayes). No CNV/SV caller. |
| `geper/pipeline/vcf_parser.py` | Skips `<…>` symbolic ALTs (counted in a log line, **not** recorded in `run_document`). Bracket breakends such as `G]2:5000]` are **not** filtered by that check — see "Known pre-existing issue". |
| CNV / SV representation, validation, ingestion | **New:** `clinical/genome_analysis/structural.py`, `structural_vcf.py` |
| Map layout data | **New:** `visualization.py` |
| Clinician API | **New:** `GET /clinic/patients/{id}/genome/map[/{name}?start=&end=]` |
| Patient exposure of CNV/SV | **None.** Patient routes remain off by default and carry no CNV/SV. |
| Frontend | **New:** `frontend/chromosome-explorer/` (the repo had only a static marketing site). |
| Persistent storage of call sets | **Implemented, uncommitted:** `clinical/migrations/0010_call_sets.sql`, `DataAccess` call-set verbs, `RetentionPrincipal._purge_call_sets`. Not applied to any real database. |

## Supported external input

A separately validated caller's VCF, supplied with an **operator declaration** of
`assembly` (GRCh37/GRCh38) and `record_class` (`cnv` or `sv`).

* The header is checked against the declaration: a `##contig` length that is the
  wrong build's, or a `##reference` naming the other build, rejects the whole file.
  A header with no such evidence is accepted and recorded as `unverified`.
* **CNV class:** `<DEL> <DUP> <CNV> <CNn>` with `END` or `SVLEN`; optional `CN`
  (INFO, ALT or a single sample's FORMAT), `LOG2RATIO`/`LOG2`, `CIPOS`, `CIEND`,
  `IMPRECISE`.
* **SV class:** `<DEL> <DUP> <INV> <INS> <BND> <TRA>` (and `:subtype` forms), VCF
  bracket breends, single breakends, literal insertions with `SVTYPE=INS`.
* Every record yields exactly one outcome: `detected`, `uncertain`, `unsupported`
  (valid but out of scope, with a reason) or `invalid` (malformed or inconsistent,
  with a reason). Original record text is preserved in `evidence.raw_record`.
* Not supported: liftover, contigs outside 1–22/X/Y/MT (a BND *partner* on one is
  kept, unplaced), gVCF, compressed input, multi-allelic structural ALTs,
  interpretation of genotype fields.

## Scientific rules enforced in code

* Only `FILTER=PASS` is `detected`; `.` or any other filter is `uncertain`. Never
  promoted.
* Positional imprecision (`CIPOS`/`CIEND`/`IMPRECISE`) is drawn as a range and is
  separate from confidence in the call.
* A missing copy number is `null`, never 2. CNV direction is not inferred from CN.
* "No call on a chromosome" is `no_calls_reported`, never "normal"; no call set at
  all is `not_assessed`. There is no "normal"/"diploid" value in any response.
* Karyotype is always `not_assessed`. No sex is inferred.
* A pair of breakends is not labelled a translocation; only a source `SVTYPE=TRA`
  is carried through as such.
* Call sets are drawn only on a map of their own assembly.
* Records are never merged; overlapping CNVs stack on lanes and are listed in
  `overlaps`.

## Known pre-existing issue (not changed here)

`vcf_parser` skips only `<…>` symbolic ALTs. A bracket-notation BND ALT in a
small-variant VCF is not covered by that check and flows toward sequence-context
code expecting plain bases. This is existing pipeline behaviour and is reported,
not altered, per "do not change existing pipeline behaviour".

## Migration (implemented as 0010; uncommitted)

**Superseded by `GENOME_ANALYSIS_CALLSET_STORAGE_DESIGN.md`, then built as migration 0010.** The one-table sketch that stood
here lacked a lifecycle, the original VCF bytes, supersession, duplicate and consent/assembly
guards, separation of duties and a retention rule; the design document specifies the
corrected model; §17 of the implementation review records what was built and what was not decided.
