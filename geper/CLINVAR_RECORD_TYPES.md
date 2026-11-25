# ClinVar record types and GEPER's evidence policy

ClinVar stores more than single variants. GEPER used to treat any ClinVar record whose component list
contained the queried allele as that variant's own record, so a haplotype's "Likely pathogenic" or a
diplotype's "drug response (practice guideline)" was attached to each component variant (ClinVar audit
finding C2). This file records the approved policy, the ClinVar definitions it rests on, and what to do
about results produced before the policy.

## 1. Evidence policy (`clinvar-single-allele-v1`)

Implemented in `utils/clinvar_records.py` and applied in `database/clinvar_client.py`.

1. **Eligible individual-variant evidence** is a record that is (a) an explicitly recognised
   single-allele type, (b) made of exactly **one** component, (c) allele-matched to the queried variant
   and (d) carries an individual-variant classification. Only eligible records fill `matched_records`
   and `primary_record`, and only they reach ACMG/BP6, PS1/PM5, confidence, priority, the legacy summary
   and pathogenicity-conflict scoring.
2. **Context only**: haplotype, single-variant haplotype / star allele, diplotype, genotype, compound
   heterozygote, phase-unknown and any other multi-variant record. They are kept in `context_records`
   with their record type, shown to reviewers (Markdown, PDFs, JSON, LIMS JSON), and never scored.
3. **Unknown or missing record types are context only**, so a new NCBI type can never silently become
   evidence.
4. **No automatic pathogenicity flag** is raised from the classification of a context record.
5. **"no classification for the single variant" is not a classification.** It is never read as Benign,
   Pathogenic or Likely pathogenic; such a record is context only and `primary_record` stays `null`.
6. **Several eligible records with different classifications** are all kept in `matched_records`;
   `individual_records_conflict` is `true`, `primary_record` is `null`, and nothing is scored from
   ClinVar. Review status and evaluation date order the display only; they never pick a winner.
7. **`primary_record` is `null` when no eligible record exists.** `match_status` keeps its three
   values: `matched` (an eligible record exists), `position_only` (records exist but none is eligible),
   `not_found`.

## 2. ClinVar definitions and their official sources

All quotations below were read directly from NCBI's FTP site on 2026-11-24 (file hashes recorded so the
reading can be repeated):

* **ClinVar VCV XML schema, version 2.6 (February 26, 2026):**
  `https://ftp.ncbi.nlm.nih.gov/pub/clinvar/xsd_public/ClinVar_VCV_2.6.xsd`
  (sha256 `de01424a0c495babd69d08f4bed1c1271aaeed5e4f441b59d0b1e3e16793d658`)
* **ClinVar FTP README (last updated January 13, 2026):**
  `https://ftp.ncbi.nlm.nih.gov/pub/clinvar/README.txt`
  (sha256 `9d6808a11d4fff31a73a4aae066dc60b455f0a0923923c2a6aec275ae6b3fbdb`)

| Term | What the official source says |
|---|---|
| `SimpleAllele` | XSD: "Describes a single sequence change relative to a contiguous region of a chromosome or the mitochondrion." |
| `Haplotype` | XSD: "Describes multiple sequence changes on one of the chromosomes of a homologous pair or on the mitochondrion." `typeHaplotype`: "This is a record of one or more simple alleles on the same chromosome molecule." |
| `Genotype` | XSD: "Describes the combination of sequence changes on each chromosome of a homologous pair". `typeGenotype`: "Used to report genotypes, be they simple or complex diplotypes." |
| Haplotype-level types | XSD `typeHaplotypeVariationType` lists exactly: `Haplotype`, `Haplotype, single variant`, `Variation`, `Phase unknown`, `Haplotype defined by a single variant`. |
| Genotype-level types | XSD `typeVariationType` lists exactly: `Diplotype`, `CompoundHeterozygote`, `Distinct chromosomes`. |
| Classified vs included | XSD, `ClassifiedRecord`: "This element is restricted to variation records for which an explicit classification was submitted." `IncludedRecord`: "This element is used for alleles that were not directly part of a submission but were part of a complex submission. They have no direct submitted classification, but are being reported for a complete representation of all alleles in ClinVar." |
| Interpreted | README, `variation_allele.txt`: "Interpreted: _yes_ indicates an interpretation was submitted about the VariationID specifically, _no_ indicates that information about the VariationID was submitted as a component of a different record." |
| Types of VariationID | README: "Types of VariationID include Variant (simple variant), Haplotype, CompoundHeterozygote, Complex, Phase unknown, Distinct chromosomes". |
| Included variants | README, `ClinSigSimple` = -1: "no values for clinical significance at all for this variant or set of variants; used for the "included" variants that are only in ClinVar because they are included in a haplotype or genotype with an interpretation". |
| `no classification for the single variant` | Appears in the XSD as an enumeration value of the aggregate germline / somatic review-status types, with no annotation. The `ClassifiedRecord` / `IncludedRecord` and `ClinSigSimple = -1` wording above is the official description of such component alleles; the sentence tying the phrase to them is GEPER's reading, not an NCBI statement. |
| `drug response` | README, `submission_summary.txt`: ExplanationOfInterpretation is "the submitter's preferred term for the interpretation when ClinicalSignificance is submitted as 'other' or 'drug response'." No definition beyond that was found. |

**Not documented in any page that could be read:** the E-utilities `esummary` field `obj_type` and its
values (GEPER's whitelist is taken from the XSD names above plus the values observed live), and the
meaning of `Complex`. The ClinVar help pages on `www.ncbi.nlm.nih.gov` (including the NCBI Bookshelf
ClinVar help) answered automated requests with a bot-check page and could not be read; a person with
browser access should confirm the above there.

## 3. How `obj_type` maps to GEPER roles

| `obj_type` (esummary) | Components | Role | Used as evidence? |
|---|---|---|---|
| `single nucleotide variant`, `Deletion`, `Duplication`, `Insertion`, `Indel`, `Microsatellite`, `Inversion`, `Tandem duplication` | 1 | `variant_record` (with a classification) / `variant_record_unclassified` | only the first |
| `Haplotype, single variant`, `Haplotype defined by a single variant` | 1 | `star_allele_record` | no (context) |
| `Haplotype`, `Diplotype`, `Genotype`, `CompoundHeterozygote`, `Phase unknown`, `Distinct chromosomes` | any | `multi_variant_record` | no (context) |
| anything else, including `Variation`, `Complex`, copy-number types and a missing value | any | `unrecognized_record_type` | no (context) |

A `Diplotype` can list a single component (a homozygous genotype such as CYP2C19\*23/\*23), so the
component count alone never identifies a single-variant record.

## 4. Result fields (all additive)

`clinvar` result: `evidence_policy`, `context_records`, `context_record_count`,
`individual_records_conflict`, `multiple_eligible_records`, `record_type_warnings`. Each record:
`record_type`, `component_count`, `record_role`, `record_role_label`, `eligible_for_variant_classification`
(and `record_type_malformed: true` only when NCBI sent an `obj_type` that is not text).
`variant_match` keeps its meaning ("this record contains the allele"). `matched_records` and
`matched_record_count` now mean eligible individual-variant records, and `primary_record` may be `null`.

Interpretation result and clinical report: `evidence_sources` lists ClinVar only when an individual-variant
record (or several conflicting ones) was actually used. Records that merely contain the allele, or sit at
the same position, are listed in the new `context_data_sources` ("data retrieved, not evidence about this
variant"). A result stored before the policy keeps the old rule (ClinVar is listed whenever it holds a
record).

Clinical evidence (`candidate_interpretation.clinical_evidence`): `clinvar_other_variants_count` (non-matching
records next to a matched one), `clinvar_reviewer_attention` (conflicting eligible records),
`clinvar_record_type_warnings`.

LIMS JSON (`clinical_database`): `clinvar_evidence_policy`, `clinvar_individual_record_count`,
`clinvar_individual_records_conflict`, `clinvar_context_record_count`, `clinvar_context_records[]`
(at most 10), `clinvar_reviewer_attention`, `clinvar_non_matching_record_count`,
`clinvar_record_type_warnings[]`; finding level: `context_data_sources`. The LIMS CSV header is unchanged.

### Unknown or missing record types

An unknown, missing, non-text or inconsistent (wrong component count) `obj_type` on a record that contains
the allele stays **context only** and never fails the pipeline. A structured warning
(`record_type_warnings[]`: `accession`, `uid`, `record_type`, `component_count`, `reason`, `treated_as`) is
stored, logged at WARNING level, shown with the variant's stage warnings in the reports and carried in the
LIMS JSON, so a new NCBI record type is noticed instead of silently demoting evidence.

### Reviewer attention

Conflicting eligible records raise an explicit reviewer-attention entry: the shared per-finding flags
(Markdown "Reviewer Attention", full PDF, short PDF), a Section 12 line, `clinvar_reviewer_attention` in
the JSON and in the LIMS JSON. It is display only: no ACMG criterion, point, threshold, conflict score,
confidence or priority input is derived from it.

## 4a. Sign-off content hash versions

The content hash stored at approval (`content_hash`, 64 hex characters) now travels with
`content_hash_version` in the results document (the sign-off manifest's key set is unchanged; it binds the
document, version included, through `results_json_sha256`):

* **version 1** -- the legacy content, unchanged byte for byte. A document with no `content_hash_version`
  (everything approved before versioning) is verified as version 1, so earlier approvals keep verifying;
  nothing is rewritten.
* **version 2** -- written by every new approval: version 1 plus a per-variant ClinVar attestation
  (evidence-policy version, match status, failed-query flag, eligible records, conflict flag, context-only
  records, non-matching records, record-type warnings) and the context-only data sources. Context records
  still never affect ACMG scoring; they are attested so reviewer-visible ClinVar information cannot change
  after approval without invalidating it. A version-2 digest can never equal a version-1 digest.
* A declared version this code does not know never verifies. Re-approving an old report is an explicit new
  seal under version 2; the old seal and its manifest are not edited.

## 5. Results produced before this policy

* A stored result without `evidence_policy` predates the policy. GEPER renders it exactly as it did
  before (no reinterpretation, no rewriting); historical reports are **not** rewritten automatically.
* Candidates for re-checking: `clinvar.matched_record_count > 1`; a `primary_record` whose title looks
  like a haplotype or diplotype (`c.[...;...]`, `*N/*M`, "Multiple alleles"); a `primary_record` with
  `clinical_significance` "drug response"; or a missing `evidence_policy` on a variant in a pharmacogene.
* **Revalidate with `--no-resume`, into a fresh output directory.** Resume is on by default and copies
  every prior variant result into the new document, skipping any variant whose coordinates are already
  in it. This policy does not change coordinates, so a resumed run would silently keep the OLD ClinVar
  evidence for every variant. (Separately, and unrelated to this policy: on `origin/master` the default
  resume path currently raises a `TypeError` because `_variant_key_from_dict` lacks `@staticmethod`, so a
  re-run into a directory that already holds `geper_results.json` fails until that pre-existing defect is
  fixed on its own; `--no-resume` avoids it. It is tracked separately and is not part of the ClinVar work.)
* Keep the historical reports and their sign-offs as they are. A re-run changes the document content
  hash, so earlier reviewer sign-offs are stale for the new document; do not overwrite historical
  clinical records.

## 6. Not covered here

ClinVar classification labels such as "Pathogenic/Likely pathogenic", review-status scoring and the
`Conflicting classifications` wording (audit findings C4-C6), the dbSNP/GRCh37 and rsID issues
(C8, C9) and everything else in the audit remain as they were.
