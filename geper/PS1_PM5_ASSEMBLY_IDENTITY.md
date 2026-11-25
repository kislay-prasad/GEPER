# PS1/PM5: "is this ClinVar record the variant itself?" across genome assemblies (ClinVar audit C3)

## What was wrong

PS1 and PM5 compare the variant with other ClinVar records at the same codon, and must never use the variant's
**own** ClinVar record as its own anchor. The own record was recognised by comparing positions, but the two
positions were in different assemblies:

* a codon match's `pos` is its NCBI canonical-SPDI position, which is **always GRCh38**;
* the query variant's `pos` is in the **input VCF's assembly**.

On a GRCh37 run the two never agreed (the offset between builds is hundreds of thousands to millions of bases; none
of 999 sampled live ClinVar missense records had equal GRCh37/GRCh38 positions), so the own record was *not*
excluded. It could be cited as the "different nucleotide change" supporting PS1 (+4 points), or appear among the
rejected anchors. GRCh38 runs were never affected, PM5 was not affected in any reproduced case, and indel queries
never reach the comparison (PS1/PM5 apply to substitutions only).

## How identity is decided now

Implemented in `pipeline/ps1_pm5/decision.py::identify_record`, **per query variant, at comparison time** (matches are
cached per transcript/codon and shared by every variant in the codon, so no per-variant verdict is ever stored in a
match or in the cache).

1. The run's assembly is canonicalised (`pipeline/ps1_pm5/utils.py::canonical_build`): `GRCh37`, `GRCH37`, `hg19`,
   `b37` -> GRCh37; `GRCh38`, `hg38`, `b38` -> GRCh38. Anything else (`None`, `""`, `37`, `38`, `GRCh38.p13`,
   `GRCh38.p14`, `GRCh37.p13`, `GRCh-37`, `grch_37`, `GRCh36`, ...) is **unresolved** and is never treated as
   GRCh38. A missing assembly (`None`) is reported as "no genome assembly was provided", a different unsupported
   value as "not a supported build"; neither runs the ClinVar lookup, and PS1/PM5 are `not_evaluated`. (Matching is
   case-insensitive and ignores surrounding whitespace, as in the original C3 implementation; whether to narrow that
   to the literal approved list is an open policy decision. The mitochondrial-only exemption is a pipeline-level
   concept recorded in the run's `assembly_note`; PS1/PM5 do not special-case it and treat its missing assembly like
   any other.)
2. A codon match carries ClinVar's own per-build placement, `pos_by_build` (from the record's `variation_loc`),
   next to the unchanged `pos` (still the GRCh38/SPDI position).
3. Decision order for each record that could be an anchor for the rule being evaluated:
   * its ClinVar UID is one the **variant-level ClinVar lookup already confirmed as an exact match** -> the
     variant's own record (checked first, so it holds even if coordinates cannot be compared);
   * unresolved assembly, missing query coordinates, a GRCh37 query against a match with no `pos_by_build` (older
     shape) or without a position in that build -> **identity cannot be verified**;
   * position (in the run's build) and ALT equal: REF equal -> own record; REF **different** -> `ref_mismatch`;
   * anything else (another position, or another ALT at the same position) -> a genuinely different variant, which
     stays a valid anchor. Neither rsID nor position proximity is ever used.
4. Own records, unverifiable records and REF mismatches are **not anchors**. None of them is in the accepted or the
   rejected anchor lists.

## Where the reasons appear (nothing is dropped silently)

Every excluded record is listed in `excluded_records` of the PS1/PM5 criterion's `details` (uid, accession, title,
`verdict`, `reason_code`, `reason`; JSON/geper_results.json). In the reviewer-facing text:

| Case | Where it shows |
|---|---|
| Own record excluded | `caveats_checked`: "Self-exclusion: ClinVar record VCV… is this variant's own record and was not used as evidence for PS1." |
| Identity could not be verified | `unchecked_caveats` **and** `conflicting_evidence` (so Markdown, PDF, JSON and LIMS JSON `conflicting_evidence` show it): "N ClinVar record(s) at this codon could not be confirmed as different from this variant (reason) and were not used as PS1 evidence: VCV…". The criterion rationale adds "N record(s) were not used because their identity could not be verified or needs review". |
| REF mismatch | same two places: "…has the same position and alternate allele as this variant but a different reference allele; it was not used as PS1 evidence and the mismatch needs review." |
| Assembly unresolved for the whole run | the ClinVar codon lookup is **not run**; PS1 and PM5 are `not_evaluated` with "the ClinVar codon lookup was not run because the genome assembly of this run (…) could not be resolved to GRCh37 or GRCh38". |

ACMG thresholds, evidence strengths, point weights and classification rules are unchanged. The LIMS CSV header is
unchanged.

## Lookup and cache

`ClinVarCodonLookup.query_codon` canonicalises the assembly for the Entrez field (`chrpos37`/`chrpos38`), puts the
canonical build and a match-schema version in the cache key, records `assembly` and `match_schema` in its result, and
serves a cached entry only if it was written by the current code for the requested build
(`lookup._cached_result_is_usable`): the stored `match_schema` must be the current one, the stored `assembly` must be
exactly the query's canonical build, and `matches` must be a list of records that each carry a `pos_by_build` mapping.
Anything else is a **miss**: the lookup is repeated, the stale record never becomes an anchor, and if the repeat fails
the result carries the error (PS1/PM5 `not_evaluated`), never the stale data and never a GRCh38 fallback. An
unresolved assembly does not read the cache at all.

Cache upgrade behaviour: entries written before C3 (old key format, no `match_schema`/`assembly`, no `pos_by_build`)
sit under keys the new code never asks for, so they are never read; the first query of each transcript/codon/build
after the upgrade therefore performs **one** ClinVar refetch (the same number of E-utilities calls as a cold cache
for that codon), and the fresh entry is appended. On-disk cache files (`GEPER_PS1_PM5_CACHE_DISK_PATH`, default off)
are append-only JSON lines: nothing is deleted or rewritten, old lines stay in the file unread, and on a restart a
fresh entry (last write wins) is served from disk. Disk entries are loaded without a TTL, which is why the content
check above, not age, is what keeps a stale entry out. Operators who want the old lines gone can move the file aside;
no code does it.

## Compatibility

* Stored results: additive only (`pos_by_build` in `clinvar_codon_matches.matches[]`, `assembly` / `match_schema` in
  the codon result, `excluded_records` in the PS1/PM5 `details`). `pos` keeps its meaning.
* The PS1/PM5 evidence trail (`triggered_criteria`, `supporting_evidence`, `conflicting_evidence`) is part of the
  content hash (versions 1 and 2), so a document produced after this change hashes differently from one produced
  before it **only if its PS1/PM5 evidence differs**. Existing documents, hashes and sign-offs are never rewritten.
* GRCh38 runs: PS1/PM5 output for 108 real queries is identical to the previous code
  (`tests/fixtures/c3_ps1_pm5_grch38_golden.json`).
* Spellings such as `GRCh38.p14` or `37`, which an earlier version silently searched as GRCh38, are now unresolved and
  make PS1/PM5 `not_evaluated` with an explicit reason. `hg19`/`b37` are now searched in the correct build, and `b38`
  is accepted as GRCh38.

## Historical GRCh37 runs (controlled review; nothing is reprocessed automatically)

A GRCh37 run made before this change may have cited a variant's own ClinVar record as PS1 evidence. Candidates can be
found read-only in `geper_results.json`: the document's `assembly` is GRCh37 (or an alias) and, for a variant,
`interpretation_result.triggered_rules[code=PS1].details.matched_anchors[].accession` intersects
`clinvar.matched_records[].accession` / `clinvar.primary_record.accession`.

Whether any report is amended, re-issued or retracted is a clinical-owner decision. If one authorises it, the run
must be repeated as **fresh processing with `--no-resume` into a new output directory**: a resumed run copies the
earlier per-variant results verbatim, and the earlier sign-off belongs to the earlier document and does not carry to
the new one. (Independently of this change, default resume currently fails with a `TypeError` on a re-run into an
existing output directory; that is a separate, pre-existing issue.)

## Known reporting limitation (documented; for a separate auditability task)

Identity-exclusion notes are covered by the content hash (versions 1 and 2) whenever they can affect or accompany a
PS1/PM5 conclusion: an own record removed from the anchor list changes the hashed triggered criteria and supporting
evidence, and an unverifiable or REF-mismatched record is listed in the hashed conflicting evidence
(`tests/test_ps1_pm5_assembly_identity.py::TestDecisionRelevantExclusionsChangeTheContentHash`).

What the hash reads, precisely (`report/models.py::_extract_clinical_content`, versions 1 and 2): the triggered
criteria (code, strength, direction), the not-evaluated count, the report's supporting and conflicting evidence text
and (version 2) the ClinVar attestation. It does **not** read a criterion's `details` (`excluded_records`,
`caveats_checked`, `unchecked_caveats`, the structured anchor lists) or the raw `clinvar_codon_matches`. So the
decision-relevant consequence of an exclusion is sealed, but the structured exclusion metadata alone is not:

| Change | In the hash? |
|---|---|
| a PS1/PM5 supporting anchor added, removed, or its accession / classification / review status changed | yes (supporting evidence) |
| a record turns conflicting or is rejected by the confidence filter, or their number changes | yes (conflicting evidence count / supporting evidence) |
| the PS1/PM5 criterion triggered or not, its code, strength or direction | yes (triggered criteria) |
| an unverifiable record's accession; unverified -> ref_mismatch | yes (conflicting evidence text) |
| own record excluded vs counted as an anchor | yes (supporting evidence / triggered criteria) |
| own-record exclusion reason code alone (by UID vs by coordinates; same record, same decision, same evidence text) | no |
| `excluded_records` / `caveats_checked` edited without any change to the evidence text | **no** |
| one rejected record swapped for another of the same count | **no** (the titles live in `caveats_checked`) |
| a not-evaluated criterion's exclusion note | **no** (below) |

Proposed, **not implemented** (needs explicit approval): a content-hash **version 3** that additionally hashes, for
every PS1/PM5 criterion including not-evaluated ones, a canonical list of `(uid, accession, verdict, reason_code)` of
the excluded records and of the records rejected by the confidence filter. It is backward compatible by construction:
the version is declared per document (`content_hash_version`), versions 1 and 2 keep their exact definitions and
digests (pinned by `tests/fixtures/c3_signed_documents_ps1_pm5.json` and the C2 legacy fixture), already signed
reports keep verifying under the version they declare, and only a **new** approval would be sealed under version 3.
Consequences to weigh before approving: a report re-approved after the change gets a different digest from its earlier
seal for identical clinical content (an explicit new seal, as for the C2 re-approval case); exporters, the manifest and
`signoff_status` need to accept version 3; and a version-3 document must not be verifiable by a build that only knows
versions 1 and 2 (it fails closed, as an unknown version does today).

One case is **not** covered. When the ClinVar codon lookup failed or was incomplete, PS1/PM5 end `not_evaluated`
(`ACMGRuleEngine._ps1_pm5_lookup_gap`), and that criterion's exclusion note (`excluded_records`, `unchecked_caveats`,
`conflicting_evidence` of the criterion) is kept only in `geper_results.json`:

* it is not in the content hash, because the document-level `conflicting_evidence` is aggregated from triggered and
  not-triggered criteria only (`pipeline/interpretation_result.py`, pre-existing for every not-evaluated criterion), and
  the hash records only the not-evaluated *count*;
* it is not shown in the Markdown, PDF or LIMS outputs.

No decision is hidden: the criterion is "not evaluated" with or without the exclusion, and the failed-lookup reason is
shown. Changing this would alter existing hashes or require a new hash version, so it was deliberately **not** done here
(no hash version, historical hash or sign-off is changed). A test pins the current behaviour
(`test_known_limitation_a_not_evaluated_criterions_exclusion_note_is_not_in_the_hash`); the separate task must update
that test together with its hash-version decision.

## GRCh37 full-engine validation: still open

The tests use real ClinVar records (frozen esummary entries with ClinVar's own GRCh37 and GRCh38 placements) and the real
`ClinVarClient`, `InterpretationEngine`, report and hash chain, but the engine's query-context step
(`ACMGRuleEngine._ps1_pm5_query_context`) is stubbed on GRCh37 because the repository holds **no GRCh37 transcript
structure**: the transcript fixtures (`ps1_pm5_transcript.json`, `pvs1_transcripts.json`, the Ensembl cache record) are
GRCh38, the local Ensembl cache is GRCh38-only, and `testdata/known_variants_grch37.vcf` carries three variants but no
transcript or protein consequence. A full GRCh37 run needs a frozen, provenance-recorded GRCh37 Ensembl transcript
(exon/CDS coordinates and CDS sequence for a gene with ClinVar records in the fixtures, e.g. TP53 NM_000546) plus the
GRCh37 reference alleles at the query coordinates; they cannot be derived by liftover. Until that fixture exists this
remains an explicit unresolved validation item, and nothing here claims full-engine GRCh37 validation.

## Not part of this change

The decision layer still does not check that an anchor lies in the query's codon/gene (it relies on position-based
retrieval), the transcript stage (PVS1) still recognises only a `GRCh37` prefix, and the rest of the pipeline
normalises assembly strings on its own. These are separate findings.
