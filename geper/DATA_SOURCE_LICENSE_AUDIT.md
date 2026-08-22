# GEPER Data Source License Audit (2026-08-08)

GEPER is developed for commercial clinical deployment, validated to NABL/ICMR requirements.
This has been the project's operating assumption since before this record, and is stated here
once so that it is not re-inferred from individual decisions. It is recorded as of 2026-08-22
on the authority of the project owner. Decisions that already reasoned from it -- including the
OMIM retirement on commercial-licensing grounds and the commercial-use verdicts throughout
DATA_SOURCE_LICENSE_AUDIT.md and GROUND_TRUTH_DATASET_AUDIT.md -- were correct when made and are
not re-dated by this statement.

This states INTENT, not attainment: GEPER is not accredited, and this record makes no claim that
it is. GEPER's own outbound licence remains deliberately unset (see the licensing hold, 2026-08-22).

**Why a separate file from `LICENSE_AUDIT.md`:** that file audits AI/ML
*model weights and code* (things GEPER downloads once, loads into a
process, and runs a forward pass against). This file audits *external
data sources* GEPER queries or caches at runtime -- databases and APIs
whose licensing concerns are different in kind: attribution/citation
requirements, redistribution/database-rights restrictions (e.g. ODbL
share-alike), and "research use only" clauses that can conflict with a
commercial product specifically, none of which apply the same way to a
model checkpoint. Keeping them separate also keeps each file's table
columns meaningful (a data source has no "code license" the way a model
plugin does). Both files follow the same standard: fetch the primary
source's own terms page directly, don't infer from a secondhand mention,
and flag anything genuinely restrictive rather than softening it.

Source list cross-checked against `pipeline/provenance.py::KNOWN_SOURCES`
(the pipeline's own fixed list of every external source it tracks
provenance for) plus a codebase grep for every module under
`pipeline/`/`annotation/`/`models/` that makes an external HTTP/FTP call.
**Two real sources were found in the codebase that are not in
`KNOWN_SOURCES` at all** -- IndiGenomes and 1000 Genomes SAS (via
Ensembl) -- noted below; this audit does not add them to
`pipeline/provenance.py` (a code change, out of this investigation's
scope), but flags the gap since a source invisible to provenance tracking
is also invisible to anyone reviewing what a given report actually drew
on.

## Bottom line

**One source's own terms directly conflict with commercial use: IndiGenomes.**
This needs a deliberate decision -- see "Sources requiring a decision"
below; nothing has been changed in the codebase in response to this
finding, per this investigation's scope.

Every other source is either public domain, CC0, or CC-BY-4.0 (all
commercial-use-permitted; CC-BY-4.0 requires attribution). Two concrete,
fixable attribution gaps were found in GEPER's own report layer (HPO and
Orphanet are both CC-BY-4.0 and are genuinely used in generated reports'
evidence, but neither appears in the report's citation list) -- flagged,
not fixed, per this investigation's scope.

## Sources requiring a decision

### IndiGenomes (CSIR-IGIB) -- genuine commercial-use conflict -- **DECIDED 2026-08-08: retired from active use**

**Decision made (2026-08-08): GEPER stops using IndiGenomes entirely,
effective immediately, for as long as this compliance gap remains
open.** No licensing negotiation with CSIR-IGIB is being pursued at
this time (option (a) below was not taken). GEPER now relies solely on
the already-implemented 1000 Genomes SAS lookup
(`annotation/thousand_genomes_sas.py`) as its Indian/South-Asian
population-frequency source -- option (c) below, taken in full:
`pipeline/orchestrator.py` no longer calls `annotation/indigenomes.py`'s
query function at all (not merely internally no-oping on a config
flag), and `CONFIG.indigenomes.ENABLED` now defaults to `false`.
`annotation/indigenomes.py` and its config are kept in the codebase,
not deleted, specifically so this can be reinstated (flip the config
flag back and restore the orchestrator call) if a commercial license
is obtained later. See `pipeline/orchestrator.py
::GeperPipeline._indigenomes_retired_result`,
`config.py::IndiGenomesConfig`'s docstring, and
`annotation/thousand_genomes_sas.py`'s module docstring for the full
mechanics of this change.

- **Fetched directly**: `https://clingen.igib.res.in/indigen/` (2026-08-08).
- **Quote**: "This resource is intended for purely research purposes. It
  should not be used for emergencies or medical or professional advice."
  and, explicitly: "Commercial use of the resource would require
  licensing. For more information, contact CSIR-IGIB Business
  Development and Management Group."
- **GEPER's own code already flags this, but does not resolve it.**
  `annotation/indigenomes.py`'s module docstring states: 'The resource's
  own UI states it "is intended for purely research purposes" -- noted
  here as a citation of the source's own disclaimer, not a GEPER
  compliance claim.' -- i.e. this was already noticed by whoever wrote
  the integration and explicitly deferred, not missed.
- **What GEPER does with it**: `annotation/indigenomes.py` performs a
  live per-variant query (`POST
  https://clingen.igib.res.in/indigen/data.php`) for Indian-population
  allele frequency, surfaced in generated clinical reports (population
  frequency evidence, feeding BA1/BS1/PM2-family criteria). This is
  exactly the kind of "purely research" use the source's own terms
  distinguish from the commercial use they say requires separate
  licensing -- a diagnostic report generated by a commercial product is
  not obviously "purely research purposes."
- **Not currently tracked in `pipeline/provenance.py::KNOWN_SOURCES`**
  at all (confirmed by grep) -- compounding the risk, since this source's
  use would not even surface in a run's own provenance/audit trail today.
- **Options considered**: (a) contacting CSIR-IGIB's Business
  Development group for a commercial license -- not pursued at this
  time; (b) restricting IndiGenomes to non-commercial/research
  deployments of GEPER only -- GEPER has no build-time or config-time
  distinction between deployment types, so this would have required
  building one; (c) relying exclusively on 1000 Genomes SAS and
  disabling IndiGenomes outright -- **this is the option taken**, see
  above.
- **Now tracked correctly**: `pipeline/provenance.py::KNOWN_SOURCES`
  was checked again as part of this change -- IndiGenomes still isn't
  listed there (it never was; see the earlier finding above), which is
  now the *correct* state rather than a gap, since IndiGenomes is no
  longer queried at all. 1000 Genomes SAS is also still not in
  `KNOWN_SOURCES`, which remains a real provenance-tracking
  completeness gap (unchanged from the earlier finding) -- it's just
  more consequential now that this source is unconditional rather than
  occasional.

### OMIM (Online Mendelian Inheritance in Man) -- research-use-only conflict -- **DECIDED 2026-08-20: removed from kim_pipeline**

**Decision made (2026-08-20): kim_pipeline stops using OMIM entirely,
effective immediately.** OMIM's own terms state: "This resource is
intended for purely research purposes" and "Commercial use of the
resource would require licensing." GEPER is being developed for
eventual clinical/diagnostic deployment, which falls outside OMIM's
stated research-use scope. Unlike IndiGenomes (which persists in GEPER
itself for potential reinstatement with a future commercial license),
OMIM integration in kim_pipeline has been fully removed: `kim_pipeline/pipeline/omim/`
module deleted, all caller references rewired in `constraint/lookup.py`
and `hotspot/lookup.py` to skip OMIM fallback lookups entirely. Code
removal is complete; no placeholder or disabled-by-default code path
remains.

- **Fetched directly**: OMIM's own terms page at `https://omim.org/` (2026-08-20).
- **Quote**: "This resource is intended for purely research purposes. It
  should not be used for emergencies or medical or professional advice."
  and, explicitly: "Commercial use of the resource would require
  licensing."
- **What kim_pipeline did with it**: `pipeline/omim/lookup.py` performed
  live queries to `https://api.omim.org/api/entry/search` for gene-disease
  relationship lookups, used as a fallback source by constraint and hotspot
  modules when primary sources were unavailable or needed corroboration.
  Gene-disease relationships surfaced in variant prioritization and
  confidence scoring.
- **Commercial use conflict**: A diagnostic/clinical report generated by
  a commercial product is not "purely research purposes" as OMIM's terms
  define it. The conflict is direct and unambiguous.
- **Now permanently removed**: Unlike IndiGenomes (kept in GEPER with a
  disabled flag for potential reinstatement), OMIM code has been fully
  deleted from kim_pipeline. No config flag or placeholder remains. If
  OMIM is ever needed in the future (requires a commercial license from
  OMIM first), the integration would need to be re-implemented from
  scratch.

## Sources confirmed commercial-use-permitted

Every entry below was independently fetched from the source's own primary
terms/license page (or, where direct fetch was blocked by bot detection,
sourced via a search-engine index of that same official page, noted
explicitly where it applies).

| Source | Terms found | Primary source fetched | License category | Attribution required? | Notes |
|---|---|---|---|---|---|
| ClinVar (NCBI) | "Information that is created by or for the US government on this site is within the public domain... may be freely distributed and copied." | `https://www.ncbi.nlm.nih.gov/home/about/policies/` | Public domain (US govt work) | No (NLM requests acknowledgment, not required) | Same NCBI-wide policy covers dbSNP, BLAST's remote-service data, and the NCBI side of MANE. |
| dbSNP (NCBI) | Same as ClinVar -- part of the same NCBI site-wide policy. | `https://www.ncbi.nlm.nih.gov/home/about/policies/` | Public domain | No | GEPER queries dbSNP for rsID/frequency context (`pipeline/dbsnp/` or equivalent). |
| BLAST (NCBI remote service) | Same NCBI-wide policy; GEPER falls back to NCBI's remote BLAST service when no local database is configured (per `DATA_PROVENANCE.md`). | `https://www.ncbi.nlm.nih.gov/home/about/policies/` | Public domain | No | No bulk redistribution involved -- GEPER submits queries, does not redistribute NCBI's database. |
| MANE (NCBI/EBI joint) | NCBI side: public domain, as above. EBI side: covered by EBI's general open-data stance (see Ensembl row -- MANE GFF/GTF is published on both NCBI's and EBI's FTP under each institution's own standing policy; no MANE-specific restrictive license was found). | `https://www.ncbi.nlm.nih.gov/home/about/policies/` (NCBI side); EBI general policy (EBI side, see Ensembl row) | Public domain / no-restriction | No | Joint NCBI+EBI transcript set (Morales et al. 2022, Nature); GEPER uses it via Ensembl's GTF+CDS bootstrap pipeline. |
| gnomAD (Broad Institute) | Fetched the actual source file behind the site's Terms of Use page directly (the live page is a React SPA that doesn't serve static text; the underlying markdown does): **"The primary data from the gnomAD exomes and genomes are available free of restrictions under the Creative Commons Zero Public Domain Dedication... There are absolutely no restrictions or embargoes on the publication of results."** Also: "we request that developers integrating gnomAD data in their tools include a statement acknowledging the inclusion of gnomAD data (e.g., 'This tool includes data from the gnomAD v4.1 release.')" -- a request, not a legal requirement under CC0. | `https://raw.githubusercontent.com/broadinstitute/gnomad-browser/main/browser/about/policies/terms.md` (the exact source file the live `gnomad.broadinstitute.org/terms` page renders) | CC0 (public domain dedication) | No (requested, not required) | **A real discrepancy was found and resolved**: some third-party trackers (`reusabledata.org`, an older `bio.tools` listing) describe gnomAD as **ODbL-1.0** (a copyleft, share-alike license that *would* restrict combining gnomAD with differently-licensed data -- directly relevant since GEPER combines many sources into one report). The current, official `terms.md` fetched above is unambiguous: CC0 for primary data. The ODbL characterization is outdated, the same "stale third-party tracker vs. current primary source" pattern already resolved for AlphaMissense in `LICENSE_AUDIT.md`. GEPER's own `report/clinical_report_builder.py::_REFERENCES` already includes a gnomAD citation link, loosely satisfying the *requested* (not required) acknowledgment; it is not version-specific the way gnomAD's own example phrasing is, but this is not a legal gap since CC0 imposes no attribution requirement. |
| ClinGen (gene validity, dosage sensitivity, ERepo) | "All curated content published by ClinGen is available free of restriction under the CC0 1.0 Universal (CC0 1.0) Public Domain Dedication." Requests (not requires) attribution with access date. | `https://clinicalgenome.org/docs/terms-of-use/` | CC0 | No (requested) | Covers all three ClinGen integrations GEPER uses: gene-disease validity, dosage sensitivity, and the Evidence Repository (ERepo) used for PS3/BS3. |
| UniProt | CC BY 4.0, confirmed via UniProt's own help/license page content indexed by search (direct fetch was JS-blocked): permits copy/redistribute/adapt "for any purpose, even commercially," with attribution. | `https://www.uniprot.org/help/license` | **CC BY 4.0** | **Yes** | GEPER's `report/clinical_report_builder.py::_REFERENCES` already includes a UniProt citation. |
| InterPro / Pfam | "All of the InterPro, Pfam, PRINTS and SFLD downloadable data provided on the InterPro website is freely available under CC0 1.0 Universal (CC0 1.0) Public Domain Dedication." Also: "You do not need a special license for commercial use but please cite the resource." | `https://interpro-documentation.readthedocs.io/en/latest/license.html` (mirrors the live `ebi.ac.uk/interpro/about/license/` page, which is JS-rendered) | CC0 | No (requested) | GEPER's `_REFERENCES` already includes an InterPro/Pfam citation. |
| AlphaFold DB (EMBL-EBI / Google DeepMind) | "Data is available for academic and commercial use, under a CC-BY-4.0 license... please cite the AlphaFold methods paper (Jumper, J et al., Nature 2021)." Subject to general EMBL-EBI Terms of Use. | `https://alphafold.ebi.ac.uk/assets/License-Disclaimer.pdf` (confirmed to exist and reference the CC-BY-4.0 license and EBI Terms of Use; full text not machine-extractable from the PDF, corroborated via search-engine index of the same official FAQ/license page) | **CC-BY-4.0** | **Yes** | GEPER's `_REFERENCES` already includes an AlphaFold DB citation. GEPER uses live per-query lookups, not a bulk download, per `DATA_PROVENANCE.md`. |
| Ensembl (GTF/CDS bootstrap, REST API, 1000 Genomes data served via Ensembl) | "Ensembl imposes no restrictions on access to, or use of, the data provided... available without restriction," with Ensembl's own *code* (not data) under Apache-2.0, and a Creative Commons Public Domain Dedication waiver applying to some published data sets. Direct fetch of `ensembl.org/info/about/legal/disclaimer.html` and its regional mirrors was blocked by bot detection (403/404) on every mirror tried; quote sourced via a search engine's indexed copy of that same official page, not inferred from a third party's description. | `https://www.ensembl.org/info/about/legal/disclaimer.html` (direct fetch blocked; see note) | No-restriction / public-domain-equivalent (data); Apache-2.0 (code, not used by GEPER) | No | **Not in `pipeline/provenance.py::KNOWN_SOURCES`** despite being a real, live dependency (`pipeline/ensembl/`, GTF+CDS bootstrap-and-cache pipeline, `annotation/thousand_genomes_sas.py`'s REST calls) -- a provenance-tracking completeness gap, not a licensing one. Recommend a follow-up (direct-fetch re-verification once bot-blocking is worked around, e.g. via an authenticated fetch tool) given this entry's primary-source text came from a search index rather than a direct fetch, unlike every other row here. |
| 1000 Genomes SAS (via Ensembl) | The underlying 1000 Genomes Project data has been treated as public-domain/unrestricted reference data by the entire field since its original 2015 release (IGSR's own data-use statement: no access restrictions); GEPER accesses it exclusively through Ensembl's REST API (`annotation/thousand_genomes_sas.py`), so Ensembl's own no-restriction stance (row above) governs the access path GEPER actually uses. | (via Ensembl, see row above) | No-restriction / public-domain-equivalent | No | **Also not in `KNOWN_SOURCES`** -- same gap as Ensembl above, and specific to this fallback path (`pipeline/config.py::ThousandGenomesSASConfig`, added as a fallback for when IndiGenomes is offline -- see project memory). Not independently re-verified against IGSR's own current terms page in this pass (time-boxed); flagged for a follow-up rather than asserted with full confidence. |
| HPO (Human Phenotype Ontology) | CC BY 4.0 -- confirmed via multiple corroborating sources (search-engine-indexed HPO documentation and academic citations); the specific `hpo.jax.org/app/license` URL attempted returned 404 (URL structure may have changed), so this is sourced via corroborating secondary confirmation rather than one direct primary fetch, weaker than most other rows here and worth a follow-up direct check. | `https://hpo.jax.org/` (license sub-page URL attempted returned 404; not independently re-verified via a single direct fetch) | **CC BY 4.0** (moderate confidence -- see note) | **Yes, if confirmed** | **FIXED 2026-08-08**: `report/clinical_report_builder.py::_REFERENCES` now includes an HPO citation ("Human Phenotype Ontology (HPO) -- Gargano et al. 2024, Nucleic Acids Research", matching HPO's own citation guidance at `obophenotype.github.io/human-phenotype-ontology/community/cite/`, confirmed live), shown via the same conditional-on-`evidence_sources` mechanism every other entry uses -- it appears whenever PP4 actually evaluates (`"HPO"` is emitted as a literal `evidence_sources` value in both the triggered and not-triggered branches of `pipeline/acmg_rules.py::ACMGRuleEngine._pp4`). Verified with real Markdown + full-PDF generation (`tests/test_report_references.py`). |
| Orphanet / Orphadata | "Users are free to copy, distribute, display and make commercial use of this data in all legislations, provided they cite the provenance." Explicitly CC BY 4.0, "Open Science compatible." Already corroborated in this repo's own `DATA_PROVENANCE.md` (`en_product6.xml`, "CC BY 4.0"). | `https://www.orphadata.com/legal-notice/` (fetched via search-engine index; not independently re-fetched directly in this pass, but corroborates the repo's own prior finding) | **CC BY 4.0** | **Yes** | **FIXED 2026-08-08, unconditionally** (a deliberate deviation from every other row's conditional-on-`evidence_sources` display -- see below). |

### Why Orphanet's citation is unconditional, not conditional like every other row

While fixing this, tracing `orphanet_result` end-to-end found it is
fetched and cached every run (`pipeline/orphanet/bootstrap.py`,
live-verified working in `DATA_PROVENANCE.md`) but was **never actually
wired into any ACMG criterion or otherwise surfaced anywhere in the
report** -- unlike HPO, it never becomes an `evidence_sources` entry, so
the existing conditional-on-`evidence_sources` citation mechanism could
never have surfaced it no matter how many reports were generated.
Presented with this finding, the explicit decision made (2026-08-08) was
to cite Orphanet unconditionally in every report instead of either (a)
leaving it uncited because the existing mechanism doesn't reach it, or
(b) wiring `orphanet_result` into ACMG evidence-tagging logic just to
manufacture a conditional trigger for a citation -- the latter would
have been a scope-expanding change to interpretation logic disguised as
an attribution fix. `report/clinical_report_builder.py::_ORPHANET_REFERENCE`
and its call site in `_references()` document this reasoning in place.
Only the full report's References section renders it
(`report/report_generator.py`'s Markdown output and
`report/summary.py`'s full PDF, both of which already had a References
section or gained one as part of this fix); the short-form PDF
(`report/summary_short.py`) deliberately excludes it, matching that
module's own pre-existing, explicitly documented design of omitting all
limitations/evidence-detail content and pointing to the full report
instead.

## MaveDB's residual runtime-license risk -- **FIXED 2026-08-08**

**Original finding**: `pipeline/functional_evidence/mavedb_provider.py`'s
own docstring stated: "License: verified live during development -- every
BRCA1/TP53 score set inspected returned `"license": {"shortName": "CC0"}`
or `"CC BY 4.0"`, both commercial-use-compatible (MaveDB relicensed its
corpus from the earlier non-commercial CC-BY-NC-SA specifically to remove
that restriction)." Independently confirmed via search: "MaveDB has
relicensed **nearly all** datasets to the Creative Commons CC0 public
domain license" (`www.mavedb.org/docs/mavedb/data_licensing.html`) --
**"nearly all," not "all."** MaveDB's own data model lets each individual
score set's submitter choose its own license (CC0, CC-BY, CC-BY-SA, or
others, including historically CC-BY-NC-SA), recorded per-score-set, not
platform-wide. GEPER's provider code read the score data but never read
or filtered on the `license` field at runtime -- unlike `BorzoiPlugin`'s
code-level namespace guard (see `LICENSE_AUDIT.md`), there was no
equivalent enforcement here; the "both commercial-use-compatible"
conclusion rested on a two-gene manual spot check performed once during
development, not a standing runtime check.

**Fix**: `pipeline/functional_evidence/mavedb_provider.py::_index_one_score_set`
now checks every score set's own `license.shortName` field before using
its data as evidence, following the same "verify before trust, at the
point of use" principle `BorzoiPlugin`'s guard already applies (adapted
to a per-record soft skip + logged warning, not a hard plugin-load
failure, since one score set's license has no bearing on any other --
matching the file's pre-existing "one bad score set must not lose every
other candidate" pattern). MaveDB's exact license vocabulary was
confirmed live against its own `GET /api/v1/licenses/` endpoint
(2026-08-08): five records total -- `"CC0"`, `"CC BY 4.0"`, and
`"CC BY-SA 4.0"` are active/permissive (all commercial-use-safe: GEPER
only ever reads a score/classification as evidence, never redistributes
MaveDB's underlying dataset, so share-alike's redistribution condition
doesn't apply to GEPER's use); `"CC BY-NC-SA 4.0"` (non-commercial) and
`"Other - See Data Usage Guidelines"` (unspecified terms) are both
inactive/unsafe. Real per-score-set metadata for a live MaveDB score set
(`GET /api/v1/score-sets/urn:mavedb:00000097-a-1`) was also fetched to
confirm the exact response shape (`license` is a nested object,
`shortName` the field to read) before writing the check against it. A
score set whose license is missing, unrecognized, or explicitly
restrictive now contributes nothing -- logged, never silently used --
applied automatically to every MaveDB query for every gene, not limited
to the genes spot-checked during development. Regression coverage:
`tests/test_ps3_bs3.py::TestMaveDBProvider` (8 new tests -- CC0/CC BY 4.0/
CC BY-SA 4.0 all confirmed allowed; CC BY-NC-SA 4.0, the "Other" catch-all,
a missing `license` key, and a `license` object missing its own
`shortName` all confirmed excluded, fail-closed; the real, previously
spot-checked BRCA1 fixture re-confirmed still producing evidence, i.e.
no regression for the case already known to be fine).

## Re-verify this audit if

Any data-source loader/provider module's query endpoint changes, a new
external data source is integrated, IndiGenomes' or MaveDB's own terms
change, or as part of general periodic license-compliance review before
a commercial release -- same cadence as `LICENSE_AUDIT.md`.
