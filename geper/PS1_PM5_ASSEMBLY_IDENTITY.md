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
   `b37` -> GRCh37; `GRCh38`, `hg38` -> GRCh38. Anything else (`None`, `37`, `GRCh38.p14`, `GRCh36`, ...) is
   **unresolved** and is never treated as GRCh38.
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
treats a cached entry without the current schema marker or for another build as a miss. Entries written before this
change (old key format, or old match shape) are therefore never read as if they had per-assembly positions.

## Compatibility

* Stored results: additive only (`pos_by_build` in `clinvar_codon_matches.matches[]`, `assembly` / `match_schema` in
  the codon result, `excluded_records` in the PS1/PM5 `details`). `pos` keeps its meaning.
* The PS1/PM5 evidence trail (`triggered_criteria`, `supporting_evidence`, `conflicting_evidence`) is part of the
  content hash (versions 1 and 2), so a document produced after this change hashes differently from one produced
  before it **only if its PS1/PM5 evidence differs**. Existing documents, hashes and sign-offs are never rewritten.
* GRCh38 runs: PS1/PM5 output for 108 real queries is identical to the previous code
  (`tests/fixtures/c3_ps1_pm5_grch38_golden.json`).
* Spellings such as `GRCh38.p14` or `37`, which an earlier version silently searched as GRCh38, are now unresolved and
  make PS1/PM5 `not_evaluated` with an explicit reason. `hg19`/`b37` are now searched in the correct build.

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

## Not part of this change

The decision layer still does not check that an anchor lies in the query's codon/gene (it relies on position-based
retrieval), the transcript stage (PVS1) still recognises only a `GRCh37` prefix, and the rest of the pipeline
normalises assembly strings on its own. These are separate findings.
