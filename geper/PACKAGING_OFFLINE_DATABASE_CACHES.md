# Packaging Part 3 -- Offline Database Caches: Sizes, Cadence, and How an Update Reaches an Air-Gapped Site

**Scope of this file.** `PACKAGING_REDISTRIBUTION_LICENSE_AUDIT.md` (Part 2)
already answered whether each of these sources may be legally bundled into
a customer-shipped image -- that question is **not re-opened here**; every
row below cites its P2 verdict rather than re-deriving it. This file
answers the separate, logistics question the human framed as: *sizes,
update cadence, and what a site with no internet actually needs --
including how an update reaches it.* This is a **scope-and-report**
deliverable. Nothing was built, downloaded, or committed beyond this
document. No size is stated as measured unless the command or source page
that produced it is cited; where a size could not be established without
downloading the source, it is marked **UNKNOWN** rather than estimated.

**Enumeration method.** Derived from `pipeline/provenance.py::KNOWN_SOURCES`
(18 entries, the pipeline's own fixed list of every external source it
tracks provenance for) cross-checked against each source's own `config.py`
dataclass and bootstrap module -- the consuming code is the evidence, not
a README's list of data sources. `IndiGenomes` and `OMIM` are excluded:
both are retired/removed per `DATA_SOURCE_LICENSE_AUDIT.md` and queried by
nothing today. `kim_pipeline`'s separate bulk ClinVar path is included
because it is the one genuine offline-capable database path in the
repository and this file cannot answer "what does a disconnected site
need" without it.

---

## Part A -- the reclassify-shaped sources (stale = actively wrong)

A source in this section does not merely fall behind when its cache goes
stale -- an existing answer can flip from correct to wrong without any
code change, because the source itself revises past conclusions (a
variant reclassified pathogenic-to-benign, a gene-disease validity
downgrade, a functional-evidence call withdrawn). This is the axis the
human's framing named as the real risk; the sources below are the entire
risk surface.

| Source | Access pattern (code-cited) | On-disk size | Upstream cadence (cited) | What a disconnected site needs |
|---|---|---|---|---|
| **ClinVar** (`geper/database/clinvar_client.py`) | **Live NCBI E-utilities only.** `base_url = CONFIG.api.NCBI_EUTILS_BASE` (`clinvar_client.py:135`); `HEALTH.is_offline("ClinVar")` (`clinvar_client.py:545`) only *skips the request and raises an error* when offline -- it is a startup health gate, not a local dataset. **No local-file code path exists in `geper/` at all.** | N/A in `geper/` (nothing cached). The bulk file this pipeline would need, if it adopted one, is measured below under kim_pipeline's copy. | NCBI's own FAQ (`ncbi.nlm.nih.gov/clinvar/docs/faq/`, fetched directly): *"The website is updated weekly, while the VCF file is created monthly."* | Cannot function offline today. `geper/`'s clinical interpretation pipeline has **zero** offline path for the single most clinically consequential database it reads. |
| **ClinVar, kim_pipeline's copy** (`kim_pipeline/pipeline/clinvar/lookup.py`) | **LOCAL backend exists and works**: parses NCBI's own `variant_summary.txt.gz` into a dict-backed lookup (module docstring, `lookup.py:6-8`). This is the only working offline ClinVar path anywhere in the repository -- established fact, not re-derived here. | **422 MB measured directly**: `ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited/variant_summary.txt.gz` = 442,645,117 bytes, last-modified 2026-09-06 (fetched from the NCBI FTP directory listing itself, not estimated). | Same NCBI FAQ citation as above: VCF-equivalent bulk files are on a **monthly** cadence; the file's own last-modified date (2026-09-06) is consistent with that. | A disconnected site needs this exact file, refreshed monthly, physically delivered -- there is no in-code mechanism that does this (see Part C). |
| **ClinGen gene-disease validity** (`pipeline/clingen/`, `ClinGenConfig`, `config.py:894`) | **Local-first, auto-fetched.** Bulk TSV/CSV from `search.clinicalgenome.org/kb/gene-validity` (config docstring, `config.py:903-907`), `AUTO_FETCH_TTL_HOURS=24` (`config.py:1040`). Live API fallback only when no local file / TTL expired and reachable. | Not measured this pass (would require the download `PACKAGING-P1`/`P3` boundaries exclude). | ClinGen's own file is versioned/dated at source; GEPER's own re-check interval is 24h (code-configured, not the publisher's own stated release cadence -- distinguishing the two, since GEPER checking every 24h does not mean ClinGen republishes every 24h). | Needs the flat file, refreshed on ClinGen's own schedule (not independently verified against ClinGen's publication cadence in this pass -- flagged below). |
| **ClinGen dosage sensitivity** | Same file/config/TTL mechanism as gene-disease validity, `.../kb/dosage/download` (`config.py:906`). | Not measured. | Same caveat as above. | Same as above. |
| **ClinGen Evidence Repository (ERepo)** -- PS3/BS3 functional evidence | **Live API only.** `FunctionalEvidenceConfig`'s own docstring states plainly: *"both are live APIs; there is no bulk-download tier for either today"* (`config.py:1474-1476`). CC0 licensed (per P2). | N/A -- nothing to cache; no bulk download exists to measure. | Not established -- no bulk artifact exists to have a cadence. | **Cannot function offline at all, and unlike ClinVar there is no precedent anywhere in the repository for how it would.** This is a gap ClinVar at least has a (partial, kim_pipeline-only) answer for; ERepo has none. |
| **MaveDB** -- secondary functional evidence | Same `FunctionalEvidenceConfig`, same "no bulk-download tier... today" statement. Per-record license already handled at query time (P2 finding). | N/A. | Not established. | Same as ERepo: no offline path exists or has ever existed in this codebase. |

---

## Part B -- the add-only / reference sources (stale = incomplete, not wrong)

These sources mostly grow or slowly revise rather than reverse a prior
clinical conclusion. Staleness here means a disconnected site is missing
*newer* information, not acting on *withdrawn* information -- a real but
lower-severity failure mode.

| Source | Access pattern (code-cited) | On-disk size | Upstream cadence | Notes |
|---|---|---|---|---|
| **dbSNP** (`geper`) | Live E-utilities only (`DBSNP_DB = "snp"`, `config.py:1983` -- same E-utils client shape as ClinVar). No local-file code path found. | N/A in `geper/`. If a bulk VCF were ever adopted: `ftp.ncbi.nlm.nih.gov/snp/latest_release/VCF/GCF_000001405.40.gz` = measured directly at **~28 GB** (NCBI FTP listing, dated 2025-01-15) -- a whole-build VCF, far larger than anything else in this table, and GEPER's per-rsID query pattern would need a materially different (indexed subset) approach to use a local copy at all. | Not established from a single authoritative cadence page in this pass. | Same "cannot function offline" gap as `geper/`'s own ClinVar client, and no equivalent to kim_pipeline's ClinVar workaround exists for dbSNP anywhere in the repo. |
| **gnomAD** (`GnomadConfig`, `config.py:547`) | **Local-first, user-provisioned.** GEPER reads a local tabix-indexed sites VCF if configured (`GRCH38_LOCAL_VCF`/`GRCH37_LOCAL_VCF`), falling back to the public GraphQL API otherwise, unless `OFFLINE_MODE` is set (`config.py:570-585`). GEPER never downloads this itself -- the docstring says so explicitly ("point these at a copy you've already provisioned, e.g. via `gsutil -m cp`"). | **UNKNOWN, unmeasured -- stated as such rather than estimated.** A full sites VCF is a bulk Broad Institute release; establishing its exact size requires either downloading it or reading Broad's own download page, which did not render usable content when fetched in this pass (client-rendered SPA). Flagged below as not established. | **Real release history, fetched directly from gnomAD's own news page** (`gnomad.broadinstitute.org/news`): v4.0 (2023-11-01), v4.1 (2024-04-19), v4.1.1 (2026-03-30, a minor gene-constraint/flag update) -- major releases roughly annual, with occasional smaller patch releases between. | Query-time cache TTL is 6h (`CACHE_TTL_SECS`, `config.py:608`) -- that governs how often a *configured* local file's query results are re-read, not how gnomAD itself publishes. |
| **HPO** (`HPOConfig`, `pipeline/hpo/bootstrap.py`) | Local-first, **auto-fetched** bulk TSV (`genes_to_phenotype.txt`), `AUTO_FETCH_TTL_HOURS=24` (`config.py:1335`). | Not measured. | Not independently verified against HPO's own publication schedule in this pass. | On a failed refresh, logs `"Using stale cached HPO dataset ... after a failed refresh"` (`bootstrap.py:122`) and **continues serving the stale file** -- see Part C, this is the general pattern. |
| **Orphanet** (`OrphanetConfig`, `pipeline/orphanet/bootstrap.py`) | Local-first, auto-fetched bulk XML. | Not measured. | **Bi-annual (July/December), per the integration's own config docstring**, which itself cites having verified this live against `orphadata.com`'s own pages before the integration was added (`config.py:1387-1391`) -- the strongest-sourced cadence claim in this table after ClinVar's. | Same fail-open pattern on refresh failure (`bootstrap.py:135`). |
| **UniProt** (`UniProtConfig`, `pipeline/uniprot/bootstrap.py`) | Local-first, auto-fetched, `AUTO_FETCH_TTL_HOURS=168` (7 days, `config.py:1806`). | Not measured. | **Code comment states "UniProt releases roughly every 8 weeks"** (`uniprot/bootstrap.py:386`) -- this is the codebase's own stated assumption, not independently re-verified against UniProt's own release-notes page in this pass; flagged accordingly. | Same fail-open pattern (`bootstrap.py:390`). |
| **Ensembl (GTF+CDS gene/transcript cache)** (`EnsemblConfig`, `pipeline/ensembl/bootstrap.py`) | Local-first, auto-fetched, `AUTO_FETCH_TTL_HOURS=168` (`config.py:1239`). | Not measured. | **Code comment: "Ensembl releases roughly every 2-3 months"** (`ensembl/bootstrap.py:468`) -- same caveat as UniProt, codebase's own assumption, not independently re-verified here. | Same fail-open pattern (`bootstrap.py:472`). Distinct from the live "Ensembl" REST-API-release-number provenance entry (`provenance.py:846-856`), which is a per-run version capture, not a cache. |
| **MANE Select (NCBI/EBI)** (`MANEConfig`, `pipeline/mane/bootstrap.py`) | Local-first, auto-fetched, `AUTO_FETCH_TTL_HOURS=168` (7 days, `config.py:1122`). Lowest-criticality source in this table by the code's own account -- used only as a tie-break when two genes' coordinates genuinely overlap (`config.py:1051-1058`). | Not measured. | **Code comment: "MANE releases roughly every few months"** (`mane/bootstrap.py:187`) -- codebase's own assumption, not independently re-verified against NCBI/EBI's own MANE release page in this pass. | Same fail-open pattern (`bootstrap.py:191`). |
| **InterPro / Pfam** (`InterProConfig`, `config.py:1844`) | Local file **supported but never auto-fetched by GEPER** -- the class docstring states directly: "no separate local-dataset layer is needed the way gnomAD/ClinGen have one for their much larger flat-file downloads" (`config.py:1859-1861`) -- i.e. no `AUTO_FETCH_DIR_URL` exists for this source. User must provision `LOCAL_DATASET_FILE` manually; otherwise live API only. | N/A unless user-provisioned. | Not established -- there is no in-code update mechanism to have a cadence. | Manual-only: if a site runs offline with a local InterPro file, refreshing it is entirely the deployer's own process, with no code support at all -- a smaller version of Part C's general gap. |
| **AlphaFold DB** (`AlphaFoldConfig`, `config.py:1899`) | Local file supported (`LOCAL_DATASET_FILE`) but the primary pattern is **live per-query structure-file download** for per-residue pLDDT (`config.py:1919` area) -- not a bulk catalogue the way AlphaMissense is. | N/A -- no bulk catalogue exists for GEPER to cache; per-protein files would need to be fetched individually. | N/A. | The redistribution question (P2) is answered for what's fetched; the logistics question here is closer to "there is no bulk mode to make offline" than "here is a stale-cache risk." |
| **Conservation -- PhyloP/PhastCons (UCSC)** (`ConservationConfig`, `config.py:772`) | Local bigWig file, **user-provisioned** (GEPER never downloads it -- docstring states so explicitly), or live UCSC REST API fallback. Query-result cache TTL 6h (`config.py:865`). | N/A -- user-provisioned, not GEPER-managed. | Static reference tracks (100-way alignments); not expected to be revised on any regular cadence. | Effectively immutable once provisioned; the "staleness" concern that dominates Part A does not apply here in practice. |
| **Conservation -- GERP++ (MyVariant.info)** | **Live API only** -- confirmed no UCSC or Ensembl endpoint exists for this score (config docstring, `config.py:789-796`); MyVariant.info re-publishes dbNSFP's precomputed score. No local-file option found. | N/A. | Not established (re-published, not GEPER's own source). | No offline path exists in code for this specific sub-source, unlike its PhyloP/PhastCons siblings. |
| **BLAST** | `LOCAL_DB_PATH` supported (user-provisioned, `config.py:2042`), or NCBI remote BLAST service fallback (per `DATA_SOURCE_LICENSE_AUDIT.md`). | N/A -- user-provisioned. | N/A for a static reference sequence database. | Same "static once provisioned" character as the conservation bigWig tracks. |
| **AlphaMissense catalogue** | Bulk tabix-indexed TSV downloaded directly by `models/alphamissense.py` (already covered as a P2 model-audit row, not re-litigated here). | **Measured directly from the GCS bucket GEPER's own code downloads from** (`storage.googleapis.com/dm_alphamissense/`): `AlphaMissense_hg19.tsv.gz` = 622,293,310 bytes, `AlphaMissense_hg38.tsv.gz` = 642,961,469 bytes -- **~1.21 GB combined**, both last-modified **2023-08-03**. | **No update since initial release** (both files carry the same 2023-08-03 timestamp as of this fetch) -- DeepMind has not republished this catalogue since. Effectively static, not on any recurring cadence. | Lowest-risk source in this table on the reclassify axis: nothing has changed to go stale against since 2023. |

---

## Part C -- the update-path question, as its own section

**What physically moves, and how big it is:** answered per-source above
where established; the pattern that generalizes is a single file per
source (a TSV/CSV/XML flat file, tens to hundreds of MB), not a
database-engine-level transfer.

**How often:** the six sources with an auto-fetch mechanism
(ClinGen x2, HPO, Orphanet, UniProt, Ensembl, MANE) each carry a
GEPER-side TTL (24h for the reclassify-shaped ones, 168h/24h for the
rest) that governs how often the *running process* attempts a re-fetch
**when it has network reach**. This is not the same fact as how often the
*publisher* actually republishes -- conflating the two would be exactly
the kind of measured-the-wrong-thing error this floor has been correcting
tonight. Publisher cadence is independently sourced above where possible
(ClinVar: NCBI's own FAQ, primary source; Orphanet: the integration's own
verified docstring; gnomAD: dated release history) and flagged as the
codebase's own unverified comment where that's what exists (MANE,
Ensembl, UniProt).

**Who does it, for an air-gapped site: nobody, today -- there is no
mechanism in code.** This is the central finding of this file. Every
auto-fetch source's own bootstrap module (`clingen/`, `hpo/`, `mane/`,
`ensembl/`, `uniprot/`, `orphanet/bootstrap.py` -- checked in all six,
not just one and generalized) shares one behaviour on a failed refresh:
log a warning (*"Using stale cached `<source>` ... after a failed
refresh"*) and **continue serving the existing file, silently, with no
report-level surface.** Checked directly: neither
`report/clinical_report_builder.py` nor `pipeline/provenance.py` contains
any reference to cache staleness -- a grep for "stale" in the report
layer turns up an unrelated finding (a stale-document race condition in
`clinical_report_builder.py`, nothing to do with database freshness).
**A disconnected site cannot tell, from the running product, that any of
these caches has gone stale.** ClinVar (via kim_pipeline's bulk file) is
the sharpest instance because it is also the reclassify-shaped source
that matters most clinically, but the mechanism -- or rather, the absence
of one -- is identical across every auto-fetch source in the table.

**What an update physically requires today, for every source in this
table:** a person with network access fetches the current file from its
own publisher URL (already cited per source above) and places it at the
path the deployment's config points `*_LOCAL_FILE`/`*_LOCAL_VCF`/
`tsv_gz_path` to. There is no manifest, no version marker written
anywhere GEPER itself checks, no checksum-on-import, and no signed
release -- confirmed by grep: no reference to a manifest format, an
import-time hash check, or a signature-verification step exists in any
of the six bootstrap modules or `kim_pipeline/pipeline/clinvar/lookup.py`.
An air-gapped update today is a human copying a file over a path, with
nothing in the code confirming afterward that it is newer, valid, or
even from the claimed source.

---

## What I could not establish

- **gnomAD's actual local-VCF file size** -- Broad's own downloads page
  did not render usable content via WebFetch (client-rendered SPA); not
  independently corroborated via a second source in this pass. Marked
  UNKNOWN rather than estimated from public knowledge of gnomAD's
  approximate scale.
- **ClinGen's own gene-validity/dosage-sensitivity file sizes and its own
  stated (not GEPER's TTL) republication cadence** -- not fetched
  directly from `search.clinicalgenome.org` in this pass; GEPER's 24h
  auto-fetch TTL is a lower bound on staleness tolerance, not evidence of
  ClinGen's actual update frequency.
- **HPO's, MANE's, Ensembl's, and UniProt's file sizes**, and **HPO's**
  publication cadence specifically -- for MANE/Ensembl/UniProt, the
  codebase's own bootstrap-module comments state a cadence ("roughly
  every few months" / "2-3 months" / "8 weeks" respectively); these are
  the codebase's own unverified assumptions, not independently re-checked
  against each publisher's own release-notes page in this pass, and are
  flagged as such in Part B rather than presented as equally solid as
  ClinVar's or Orphanet's directly-sourced cadences.
- **Whether InterPro, AlphaFold DB, or the GERP++ conservation source have
  ever actually been exercised in local-file mode in a real deployment**
  -- the code paths exist; whether they have been used is not established
  here (no download performed, per boundaries).
- Not a gap, corrected before sending rather than left in: an earlier
  draft of this section flagged "what a caller does with the
  `ExternalAPIError`" as unestablished. Re-reading `clinvar_client.py:
  545-557` directly, the code's own comment already answers it:
  the raised message is *"folded verbatim into `clinvar_error`... and
  rendered into every report unconditionally by
  `report/report_generator.py`, plus embedded verbatim in
  `geper_results.json`'s `clinvar` key"* -- i.e. the offline-skip is
  surfaced to every report and export, not silently swallowed. The
  same comment names four other sources handled identically
  (SpliceBERT/MMSplice/UniProt/InterPro/ClinGen/AlphaFold DB, "rounds
  20-24"), not independently re-verified per-source in this pass.

## Re-verify this report if

Any `*_LOCAL_*`/`*_AUTO_FETCH*` config default changes; a new external
data source is integrated; ClinGen, HPO, MANE, Ensembl, UniProt, or
Orphanet's own publication pages are fetched directly (several rows above
rest on the codebase's own comments rather than a direct publisher fetch,
and are flagged as such); or as part of general periodic review before a
commercial release -- same cadence as `LICENSE_AUDIT.md`,
`DATA_SOURCE_LICENSE_AUDIT.md`, and `PACKAGING_REDISTRIBUTION_LICENSE_AUDIT.md`.
