# GEPER LIMS Export -- Field Mapping

`report/export_lims.py` exports an existing `geper_results.json` (from a
prior GEPER run -- this never re-runs the pipeline) into a generic,
standards-adjacent format meant for a downstream LIMS/lab-management
system to ingest. This document is the field-by-field mapping that code
implements, kept in sync with it deliberately so that when a real
hospital/lab's actual LIMS spec eventually shows up, adapting this is a
mapping exercise against the table below, not a rewrite.

## Why this format, not HL7 FHIR

Two options were considered (per the task this module was built for): the
HL7 FHIR Genomics Reporting Implementation Guide, or a plain structured
export. Researched live (not assumed) before choosing:

- The IG is at **STU3** (v3.0.0, published 2024-12-12) -- Standard for
  Trial Use, **not** Normative. v4.0.0 is already an active ci-build,
  meaning the spec itself is still moving under implementers' feet.
- The IG's own home page states plainly: *"The Clinical Genomics Working
  Group understands that this guide is not complete, and implementers
  might identify additional concepts and data elements."*
- Adoption found: eMERGE and CSER (large, federally-funded US research
  consortia), the Sync for Genes pilot program, and Molit (one small
  German non-profit, ~2 years in production). No evidence of adoption by
  commercial/community diagnostic labs generally, and none found for
  India specifically -- GEPER's actual target market (see
  `report/summary.py`'s own DPDP Act framing).
- Implementing it properly needs LOINC codes per genomic Observation
  type, HGVS-validated `Coding` elements, SNOMED CT terms,
  ClinVar/ClinGen `CodeableConcept`s, and nested
  `Observation.component` hierarchies across Variant/Genotype/
  DiagnosticImplication/MolecularConsequence profiles -- real
  engineering investment spent validating against a spec with no real
  target implementer to confirm it against, since (per this task's own
  framing) there is no pilot customer's actual format to build to yet.

**Decision:** a plain, exhaustively-documented export (this table +
`report/export_lims.py`) -- universally ingestable (every LIMS can parse
JSON/CSV; not every LIMS has FHIR tooling), and using standards-adjacent
values wherever GEPER already produces them for free (ISO 8601
timestamps, real HGVS nomenclature, verbatim ACMG/AMP 2015 classification
terms, real external accessions) so a future FHIR crosswalk is a mapping
exercise against this table, not a rewrite.

## Governance gate: review/sign-off required before export

Round 30 part 2: `export_lims_json`/`export_lims_csv` (and the pure
`build_lims_export` they both call) refuse to run at all unless the
source `geper_results.json`'s `review_status` field is exactly
`"reviewed"` -- raising `utils.exceptions.LIMSExportBlockedError` and
writing an `action: "export_blocked"` entry to a `geper_signoff_audit.log`
alongside the requested export path (see `report/export_lims.py
::_require_reviewed`). A LIMS is an automated downstream consumer that
never opens the PDF a human clinician would see the DRAFT/OVERRIDDEN
status on, so this gate exists precisely so an unreviewed -- or
subsequently clinician-overridden -- run's classification can never
reach a hospital's LIMS silently.

`review_status` starts `"draft"` (set by `report/json_builder.py` at
generation time) and only ever changes via `review/signoff.py`:
`approve()` sets it to `"reviewed"` (plus `reviewed_by`/`reviewed_at`);
`override()` sets it to `"overridden"` -- deliberately re-blocking
export even on a previously-approved run, since the prior sign-off no
longer covers content a clinician has since changed. A fresh
`approve()` call after an `override()` is what makes a run
export-eligible again. `review_status`/`reviewed_by`/`reviewed_at` are
NOT themselves part of the `LIMSExport` schema below -- they gate
whether an export happens at all, rather than being data a LIMS needs
mapped.

## Format

- **JSON** (`export_lims_json`): one file per run, nested, matches the
  `LIMSExport` schema below exactly (`report/export_lims.py`'s Pydantic
  models -- validated at construction, the same validation-at-a-boundary
  convention `pipeline/stage_schemas.py` already uses elsewhere in this
  codebase).
- **CSV** (`export_lims_csv`): flattened, exactly one row per variant
  finding, for LIMS/lab-management systems that ingest tabular data
  rather than nested JSON (common for smaller diagnostic labs without an
  HL7/FHIR interface). List-valued fields are semicolon-joined into a
  single cell. Column order is a fixed, documented contract
  (`_CSV_COLUMNS` in `export_lims.py`) -- matches this table's row order.

Every field below is either a real value traced to a real
`geper_results.json` key, or explicit `null`/empty. **Never** a fabricated
default -- an export field a downstream LIMS could misread as a real
result is worse than an honest `null`.

## Run-level fields

| Export field (JSON path) | CSV column | GEPER source | Notes |
|---|---|---|---|
| `export_format` | -- | constant `"geper_lims_export"` | Format identifier, for a downstream system (or future migration script) to detect which mapping this file follows. |
| `export_format_version` | -- | constant, currently `"1.0.0"` | Bump on any breaking change to this table; a downstream LIMS should key its parser off this, not just the file extension. |
| `run.sample_id` | `sample_id` | `document["vcf_samples"]` (joined), else the input VCF's filename stem | GEPER has no first-class Sample ID concept (see `report/summary.py::_derive_sample_id`, reused directly here -- not re-derived). A real deployment should supply its own authoritative sample ID. |
| `run.run_id` | `run_id` | caller-supplied override, else derived from `document["generated_at"]` | Reuses `report/summary.py::_derive_run_id` directly, so a JSON export and a PDF generated from the same `geper_results.json` (and no explicit `run_id`) get the *same* run ID. |
| `run.input_vcf` | -- | `document["input_vcf"]` | Verbatim. |
| `run.reference_assembly` | -- | `document["assembly"]` | e.g. `"GRCh38"`; `null` if the assembly preflight never resolved one. |
| `run.variant_count` | -- | `document["variant_count"]` | |
| `run.geper_code_version` | -- | `document["code_version"]` | GEPER's own code version (`pipeline/provenance.py::get_geper_code_version`) -- for reproducibility, not a clinical field. |
| `run.generated_at` | -- | `document["generated_at"]` | ISO 8601 UTC, verbatim -- GEPER already stores it that way. |

## Per-finding fields

`findings[]` -- one entry per `document["variants"][i]`, in the same
order. `finding_number` (1-based) is stable across export runs against
the same file and matches the "Finding N" numbering GEPER's own PDF/
Markdown reports already use for the same variant, so a reviewer can
cross-reference this export against those reports directly.

### Variant identity

| Export field | CSV column | GEPER source | Notes |
|---|---|---|---|
| `variant.chromosome` | `chromosome` | `variants[i]["variant"]["chrom"]` | |
| `variant.position` | `position` | `variants[i]["variant"]["pos"]` | 1-based, VCF convention. |
| `variant.reference_allele` | `reference_allele` | `variants[i]["variant"]["ref"]` | |
| `variant.alternate_allele` | `alternate_allele` | `variants[i]["variant"]["alt"]` | |
| `variant.variant_type` | `variant_type` | `variants[i]["variant"]["variant_type"]` | `"SNV"` \| `"insertion"` \| `"deletion"` \| `"MNV"`. |
| `variant.vcf_id` | -- | `variants[i]["variant"]["id"]` | The VCF record's own `ID` column. |
| `variant.vcf_filter_status` | -- | `variants[i]["variant"]["filter"]` | |
| `variant.genomic_hgvs` | `genomic_hgvs` | `variants[i]["normalization"]["hgvs_g"]` | HGVS.g -- always computed when variant normalization succeeded (`pipeline/hgvs_utils.py::to_hgvs_g`). `null` if normalization was disabled/failed. |
| `variant.coding_hgvs` | `coding_hgvs` | `variants[i]["normalization"]["hgvs_c"]` | HGVS.c -- only when a transcript structure resolved for this variant (`pipeline/hgvs_utils.py::to_hgvs_c`). `null` for intergenic variants or when transcript resolution failed. |
| `variant.protein_hgvs` | `protein_hgvs` | `variants[i]["alphamissense"]["protein_variant"]`, only when `alphamissense["found"]` | **Honest gap, stated plainly:** GEPER's `pipeline/hgvs_utils.py` supports computing HGVS.p (`to_hgvs_p`/`to_hgvs_p_for_substitution`), but no orchestrator stage currently calls it for every variant -- confirmed by grepping `pipeline/orchestrator.py` for both function names (zero call sites as of this writing). The one place a real protein change already flows into `geper_results.json` is AlphaMissense's own precomputed catalogue match, available only for missense variants AlphaMissense actually scored. `null` for every other variant -- never independently computed or guessed here. |
| `variant.protein_hgvs_source` | `protein_hgvs_source` | derived | `"alphamissense_catalogue"` when `protein_hgvs` is populated; `null` otherwise. Exists so a downstream consumer never mistakes an AlphaMissense-sourced protein change for a GEPER-computed one. |

### Gene

| Export field | CSV column | GEPER source | Notes |
|---|---|---|---|
| `gene.symbol` | `gene_symbol` | `variants[i]["interpretation_result"]["gene_symbol"]` | Resolved from ClinGen's or UniProt's own gene-symbol lookup (see `pipeline/interpretation_result.py::build_interpretation_result`) -- `null` if neither resolved one for this variant. |

### ACMG classification

**Explicitly separate from, and never influenced by, `case_prioritization` below** -- see that section's own note.

| Export field | CSV column | GEPER source | Notes |
|---|---|---|---|
| `classification.acmg_classification` | `acmg_classification` | `variants[i]["clinical_report"]["acmg_classification"]["classification"]` | Verbatim ACMG/AMP 2015 term: `"Pathogenic"` \| `"Likely Pathogenic"` \| `"Uncertain Significance"` \| `"Likely Benign"` \| `"Benign"` \| `null`. Never remapped to a different vocabulary here. |
| `classification.triggered_criteria[].code` etc. | `triggered_acmg_criteria` (semicolon-joined codes) | `...["acmg_classification"]["triggered_criteria"]` | Each entry: ACMG criterion code (e.g. `"PS3"`), strength, direction. |
| `classification.not_evaluated_criteria_count` | `not_evaluated_criteria_count` | `...["acmg_classification"]["not_evaluated_count"]` | How many of the 28 ACMG/AMP criteria couldn't be evaluated for this variant due to missing evidence (e.g. no functional-evidence source available) -- see `pipeline/acmg_rules.py` for per-criterion reasons (not carried into this export; see `geper_results.json` itself for the full detail this summarizes). |

### Confidence (Phase 3) and Priority (Phase 4)

| Export field | CSV column | GEPER source | Notes |
|---|---|---|---|
| `confidence.pending` / `.score` / `.label` | `confidence_pending` / `confidence_score` / `confidence_label` | `variants[i]["clinical_report"]["confidence"]` | `pending=true` means confidence scoring did not complete for this variant -- `score`/`label` stay `null` in that case, never a fabricated default. |
| `priority.pending` / `.score` / `.category` / `.rank` | `priority_pending` / `priority_score` / `priority_category` / `priority_rank` | `variants[i]["clinical_report"]["priority"]` | `rank` is 1-indexed, batch-relative (`pipeline/prioritization_engine.py::rank_batch`) -- `null` until every variant in the run has been scored. |

### Case-level phenotype-driven ranking (`pipeline/case_prioritization.py`)

**Deliberately a separate object, never merged into `classification` above:** this is a continuous, HPO-phenotype-driven reviewer-triage signal, not an ACMG criterion -- it must never be misread by a downstream LIMS as affecting clinical classification. `null` for the whole object when the patient supplied no HPO terms for this run at all (the feature simply didn't run) -- not a forced/fabricated ranking.

| Export field | CSV column | GEPER source | Notes |
|---|---|---|---|
| `case_prioritization.case_rank` | `case_rank` | `variants[i]["case_prioritization"]["case_rank"]` | 1 = best phenotype/evidence match in this batch. `null` if this variant's priority score was itself unavailable (`tier="not_scored"`). |
| `case_prioritization.case_rank_score` | `case_rank_score` | `...["case_rank_score"]` | 0-100 combined score (see `pipeline/case_prioritization.py`'s own docstring for the weighting rationale). `null` for the `no_gene_hpo_data`/`not_scored` tiers -- never backfilled with a fake 0. |
| `case_prioritization.phenotype_match_score` | `phenotype_match_score` | `...["phenotype_match"]["score"]` | 0-100; `null` specifically means "this gene has no HPO-curated phenotype data at all" -- distinct from a genuine `0.0` (checked, no overlap with the patient's observed terms). |
| `case_prioritization.tier` | `case_prioritization_tier` | `...["tier"]` | `"phenotype_matched"` \| `"no_gene_hpo_data"` \| `"not_scored"` -- see `pipeline/case_prioritization.py::rank_case`'s docstring for the full tier semantics, including why `no_gene_hpo_data` variants rank after every `phenotype_matched` one regardless of raw priority score. |
| `case_prioritization.reason` | -- | `...["reason"]` | Human-readable explanation, always present. |

### Population frequency

| Export field | CSV column | GEPER source | Notes |
|---|---|---|---|
| `population_frequency.gnomad_queried` | *(JSON only -- not a CSV column)* | derived from `variants[i]["gnomad"]` | `true` unless gnomAD was skipped/errored for this variant. |
| `population_frequency.gnomad_found` | `gnomad_found` | `variants[i]["gnomad"]["found"]` | |
| `population_frequency.gnomad_global_af` | `gnomad_global_af` | `variants[i]["gnomad"]["global_af"]` | `null` if gnomAD wasn't queried, found nothing, or reported no frequency. |
| `population_frequency.dbsnp_rsid` | `dbsnp_rsid` | `variants[i]["dbsnp"]["rsid"]`, only when `dbsnp["found"]` | Allele-matched rsID only -- never a co-located-but-different-allele rsID (see `database/dbsnp_client.py`'s module docstring for why that distinction matters). |

### Clinical databases

| Export field | CSV column | GEPER source | Notes |
|---|---|---|---|
| `clinical_database.clinvar_significance` | `clinvar_significance` | `variants[i]["clinvar"]["primary_record"]["clinical_significance"]`, only when `match_status == "matched"` | Allele-matched ClinVar record only -- never a co-located, non-matching variant's classification (see `database/clinvar_client.py`'s module docstring for the real bug this distinction fixes). |
| `clinical_database.clinvar_review_status` | `clinvar_review_status` | `...["primary_record"]["review_status"]` | ClinVar's own review-status string (e.g. "criteria provided, multiple submitters"). |
| `clinical_database.clingen_gene_validity` | `clingen_gene_validity` | `variants[i]["clingen"]["clinical_validity_summary"]`, only when `clingen["found"]` | Gene-level (not variant-level) clinical validity classification (e.g. "Definitive"). |

### Evidence, recommendations, and quality flags

| Export field | CSV column | GEPER source | Notes |
|---|---|---|---|
| `supporting_evidence[]` | -- | `variants[i]["clinical_report"]["supporting_evidence"]` | Free-text evidence statements, already deduplicated by Phase 2 (`pipeline/interpretation_result.py`). |
| `conflicting_evidence[]` | -- | `...["conflicting_evidence"]` | |
| `recommendations[]` | -- | `...["recommendations"]` | |
| `evidence_sources[]` | `evidence_sources` (semicolon-joined) | `...["evidence_sources"]` | Which external sources actually contributed evidence to this variant's classification (e.g. `["ClinVar", "dbSNP", "ClinGen"]`). |
| `stage_errors[]` | `stage_errors` (semicolon-joined) | `variants[i]["errors"]` | Non-fatal, per-stage failures recorded during this variant's processing (e.g. a single BLAST timeout) -- present even when `interpretation_available` is `true`, since GEPER's own "best-effort partial evidence is still a success" policy means a variant can be fully classified despite one stage failing. |
| `interpretation_available` | `interpretation_available` | derived | `false` when `variants[i]["interpretation_result"]` is missing or itself an `{"error": ...}` record (the ACMG aggregation engine failed for this variant) -- when `false`, `classification`/`confidence`/`priority` all stay at their honest empty defaults (never fabricated), while `variant`/`gene` identity and `case_prioritization` (a genuinely independent signal -- see above) are still populated when available, so a reviewer knows *which* variant failed and *why*, not just that one did. |

## Regenerating this export

```bash
python -m report.export_lims path/to/geper_results.json --json out.json --csv out.csv
```

Runs entirely against an already-written `geper_results.json` -- no
pipeline re-run, no model weights, no network access.

## Extending this mapping

When a real hospital/lab's LIMS spec is known:

1. Add new columns/fields to `report/export_lims.py`'s Pydantic models
   and `_CSV_COLUMNS`, each still tracing to a real `geper_results.json`
   key (or explicit `null`) -- never a fabricated placeholder.
2. Update this table to match.
3. Bump `EXPORT_FORMAT_VERSION` in `export_lims.py` if the change is
   breaking (a field renamed/removed, not just added).
4. If the target turns out to genuinely require HL7 FHIR, this table is
   the crosswalk source: each row's "GEPER source" column is already the
   traceability audit a FHIR `Provenance`/`DiagnosticReport` mapping
   would need anyway.
