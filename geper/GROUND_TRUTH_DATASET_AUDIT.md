# GEPER Ground-Truth Reference Dataset Audit

**Phase 2, item 4 (validation phase, definition work only).** An inventory of
externally obtainable ground-truth datasets GEPER's/kim_pipeline's output
*could* be validated against, following the same standard `LICENSE_AUDIT.md`
set for AI models: fetch the primary source's own terms directly, cite what
was actually read, and state a plain commercial-use verdict rather than a
column of adjectives. Nothing here is asserted from memory alone without a
primary or corroborating secondary source; every row says which it got.

**Why a third file, distinct from the other two audits:** `LICENSE_AUDIT.md`
covers AI model weights/code; `DATA_SOURCE_LICENSE_AUDIT.md` covers external
data sources GEPER queries *live at runtime* as evidence (ClinVar, gnomAD,
ClinGen, etc.). This file covers a different category: datasets whose purpose
is to be compared *against* GEPER's/kim_pipeline's own output to measure
whether that output is correct -- ground truth for a concordance study, not
evidence the pipeline consumes. A dataset can appear in both files (ClinVar
does) because it's used both ways.

**Standing frame, stated because the human was explicit about it:** two days
of prior work established that this pipeline no longer *misstates* what it
observed (the "model unavailable" mislabelling, the OMIM/IndiGenomes retirals,
the review-state and QC-provenance fixes). None of that established that what
it observes is *correct*. This document is an inventory of what ground truth
is obtainable to test that separate, harder question -- it does not itself
measure concordance, does not propose a rubric, and does not decide the
validation plan's owner. Per the phase rule: **definition work only.**

**Licensing is a go/no-go gate here, not a column.** OMIM was fully
integrated into kim_pipeline and then deleted entirely, after integration,
because its research-use-only terms conflicted with commercial deployment
(`DATA_SOURCE_LICENSE_AUDIT.md`, `51558c1`). Every dataset below was
licensing-checked *before* describing what it covers, not after, and a
"not usable" verdict is reported as a finding, not omitted.

**No dataset in this document has been downloaded. No acquisition code was
written. No concordance study was designed.** All of that is out of this
task's boundaries; the human is still deciding who owns the concordance
study itself.

---

## Bottom line

**Only two datasets are unconditionally usable at meaningful scale: the
ClinVar expert-panel/practice-guideline subset, and GIAB.** They answer
different questions -- ClinVar for classification concordance, GIAB for
variant-calling accuracy -- and neither substitutes for the other.

**The ICMR/Indian-population ground-truth question comes back functionally
empty for a commercial product, and this is stated plainly because it is
one of the most consequential facts in this phase.** Every India-specific
candidate found (IndiGen, GenomeIndia, GenomeAsia100K) is access-gated,
research-postured, and has no established commercial-use pathway today.
There is no population-matched benchmark equivalent to GIAB or ClinVar's
expert panels for the Indian population. See "The ICMR/India gap" below.

Two more (HGMD, PharmVar) are genuinely useful but commercially blocked
outright or only unblockable by purchasing a license. Two (PharmGKB,
Illumina Platinum Genomes) are usable only with a named condition attached.
One (DECIPHER) is a clean reject.

---

## Datasets confirmed obtainable and commercially usable

### ClinVar expert-panel / practice-guideline subset

| | |
|---|---|
| **What it covers** | A graded-confidence subset of ClinVar's own aggregate variant classifications: **22,402 unique variation records reviewed by an expert panel (3 stars)** and **663 with a practice guideline classification (4 stars)**, out of 6,946,727 total submitted records (live-fetched count, `ncbi.nlm.nih.gov/clinvar/docs/statistics/`, dated 2026-08-16 on the page itself -- **~0.33% of all ClinVar records combined**). Contributing expert panels include ClinGen's disease-specific Variant Curation Expert Panels and specialty consortia (e.g. ENIGMA for *BRCA1/2*, InSiGHT for Lynch-syndrome genes) -- concentrated in well-studied, medically actionable genes, not a genome-wide representative sample. Predominantly SNVs/small indels with an assigned pathogenicity classification (Pathogenic/Likely pathogenic/VUS/Likely benign/Benign), exactly the output class GEPER's own ACMG engine produces. |
| **Provenance / how obtained** | Same channel GEPER already queries live: `ClinVar` via NCBI's public API/FTP (`geper/database/clinvar_client.py`). Filtering to `review_status` of `reviewed_by_expert_panel` / `practice_guideline` is a query-time filter on data already flowing through the existing client -- **no new integration, credential, or endpoint needed** to obtain this subset. |
| **License, verbatim** | "Information that is created by or for the US government on this site is within the public domain... may be freely distributed and copied" (`ncbi.nlm.nih.gov/home/about/policies/`, already the basis for ClinVar's "usable" verdict in `DATA_SOURCE_LICENSE_AUDIT.md`). Independently re-confirmed for this specific use (validation ground truth, not runtime evidence) via ClinVar's own submission-policy page: **"Once the data are in ClinVar, they are available for unrestricted distribution"** (`ncbi.nlm.nih.gov/clinvar/docs/submit/`, "Do I need consent to submit?" section) -- this directly answers the question this audit needed that the prior one didn't: whether individual *submitters* (clinical labs, expert panels) retain any redistribution/commercial restriction on their own contributed classifications. They do not; once submitted, ClinVar's public-domain terms govern regardless of submitter. |
| **Primary source fetched** | `ncbi.nlm.nih.gov/clinvar/docs/statistics/` (star-rating counts), `ncbi.nlm.nih.gov/clinvar/docs/review_status/` (star-rating definitions), `ncbi.nlm.nih.gov/clinvar/docs/submit/` (submitter redistribution terms) -- all fetched directly this session. |
| **Commercial-use verdict** | **Usable.** No restriction found, none expected given NCBI's site-wide policy already governs the rest of ClinVar. |
| **Methodological flag, not a licensing issue** | GEPER's own ACMG engine already consumes ClinVar's `clinical_significance`/`review_status` fields as live evidence for PP5/BP6 (direct-match) and PS1/PM5 (same-residue) criteria (`geper/database/clinvar_client.py:344-345`, `pipeline/ps1_pm5/`). For any variant where GEPER's classification was itself informed by that variant's own ClinVar entry, comparing GEPER's output back against ClinVar's classification is not a fully independent concordance check -- it partly measures whether GEPER correctly echoed what it was told, not whether it reached the right answer independently. This is a fact about study design, not about the dataset's licensing or availability; flagging it for whoever designs the concordance study, not resolving it here. |

### GIAB (Genome in a Bottle, NIST)

| | |
|---|---|
| **What it covers** | High-confidence small-variant (SNV/indel) truth-set VCFs + confident-region BEDs for seven reference genomes: **HG001/NA12878** (original pilot, HapMap), **HG002/HG003/HG004** (Ashkenazi Jewish trio -- son/father/mother, Personal Genome Project), **HG005/HG006/HG007** (Han Chinese trio, Personal Genome Project). This validates variant-*calling* accuracy (precision/recall/F1 against a known-correct call set within defined confident regions) -- a different question from ClinVar's classification ground truth, and the more directly relevant one for kim_pipeline's FASTQ-to-VCF stages rather than geper's ACMG interpretation layer. No South/East-Asian-subcontinent ancestry represented among the seven genomes. |
| **Provenance / how obtained** | NCBI FTP (`ftp-trace.ncbi.nlm.nih.gov/ReferenceSamples/giab/release/`) or the `genome-in-a-bottle/giab_data_indexes` GitHub repo's index files; standard `hap.py`-style benchmarking against these is the field's normal practice (same tool GIAB's own truthset documentation points to). |
| **License** | Two independent, corroborating findings, neither a single dedicated GIAB license file (none was found despite checking): (1) NIST's own site-wide policy, fetched directly (`nist.gov/open/license`): **"works of NIST employees are not subject to copyright protection in the United States."** GIAB's truth-set VCF/BED files are NIST's own scientific data product (Zook/Wagner et al., NIST Genome in a Bottle Consortium), not a third party's dataset merely hosted by NIST. (2) The GIAB program page itself (`nist.gov/programs-projects/genome-bottle`, fetched directly) states the reference samples are **"consented for commercial redistribution"** -- addressing the separate question of the underlying human subjects' consent, distinct from the copyright question NIST's policy answers. |
| **Primary source fetched** | `nist.gov/programs-projects/genome-bottle`, `nist.gov/open/license`, `ftp-trace.ncbi.nlm.nih.gov/ReferenceSamples/giab/` (directory listing only -- no single consolidated GIAB-specific LICENSE/terms file was locatable in the time available; this is a weaker citation than the ClinVar row above and is flagged as such, unlike asserting it with full confidence). |
| **Commercial-use verdict** | **Usable**, on the strength of NIST's general public-domain policy plus the program's own explicit commercial-redistribution consent language -- but re-verify with a direct GIAB-specific terms document if one surfaces, since no single authoritative GIAB-only license page was found to close this out with the same certainty as the ClinVar row. |

---

## The ICMR/India gap

**This is the single most consequential negative finding in this document,
stated as plainly as the human asked for it to be.** No population-matched
ground-truth dataset for the Indian population is freely obtainable for a
commercial product today. Every candidate found is gated, not merely
inconvenient:

### IndiGen (CSIR-IGIB)

Already documented in `DATA_SOURCE_LICENSE_AUDIT.md` and already the subject
of a decision: retired from GEPER's own evidence pipeline for exactly this
conflict. Its own terms page (`clingen.igib.res.in/indigen/`, previously
fetched): "This resource is intended for purely research purposes... Commercial
use of the resource would require licensing." No new finding here -- cited for
completeness of this inventory, not re-investigated.

### GenomeIndia (DBT / Indian Biological Data Centre)

| | |
|---|---|
| **What it covers** | Whole-genome sequences of 10,000 individuals from diverse Indian population groups (Phase 1, DBT-funded), the closest thing to a large-scale Indian-population genomic resource that exists. |
| **Access model** | Archived at the Indian Biological Data Centre (IBDC), accessed via the "FeED" protocol under BIOTECH-PRIDE guidelines -- a Data Access Committee model, not open download. Per government/press sourcing (PIB press releases, science-policy coverage; direct fetch of `genomeindia.org` and the specific PIB press-release URL both failed -- connection refused and 403 respectively -- so this section rests on secondary corroboration via search rather than a direct primary fetch, weaker sourcing than the rows above and flagged as such): data is "shared with researchers from renowned institutes" under guidelines "balancing the benefits of open data with the necessity of confidentiality and security." |
| **Commercial-use verdict** | **Not currently usable, and no pathway exists yet, not just an unmet condition.** The same sourcing explicitly frames commercial use as an *unsolved policy gap*: "a national framework for human-genomics data, covering consent, access, commercial use, benefit-sharing... would fill a current gap," and that commercial sectors "should be able to license or use Genome India data under appropriate safeguards, with a clear pathway" -- future-tense, aspirational language, not a statement of an existing mechanism. There is nothing to apply for yet. |

### GenomeAsia100K

| | |
|---|---|
| **What it covers** | Pan-Asian whole-genome sequences (the GenomeAsia100K Consortium); includes South Asian representation but is not India-specific. |
| **Access model** | Hosted at EGA (European Genome-Phenome Archive) under accession `EGAS00001002921`, individual-level VCFs gated behind a Data Access Committee (the GenomeAsia100K Consortium itself acts as its own DAC, `ega-archive.org/dacs/EGAC00001001439`). Access requires a completed data-access agreement and a research-proposal review by the DAC (`dataaccess@genomeasia100k.org`) -- confirmed directly from EGA's own DAC page and the consortium's stated process. |
| **Commercial-use verdict** | **Undetermined, not "conditional" -- the actual Data Access Agreement text was not reachable** (the DAC contact page gives only the request process, not the agreement's substantive terms; the "Data Use Conditions" / Data Use Ontology page referenced was not independently fetched). Given the pattern of every other DAC-gated resource checked in this document (IndiGen, GenomeIndia, DECIPHER) being research-postured, a commercial grant is plausible but not assumed; this would need an actual DAC application to resolve, which is out of this task's scope (no acquisition, no correspondence with data custodians). |

**Net finding:** three candidates checked, zero yield a dataset usable in a
commercial product today without either (a) a licensing negotiation with no
established process (GenomeIndia), (b) a DAC application whose terms are
unknown until applied (GenomeAsia100K), or (c) a resource GEPER already
evaluated and declined for this exact reason (IndiGen). If population-matched
validation ground truth is required for ICMR/NABL readiness, it is not
obtainable off-the-shelf; that gap itself may need to be raised as its own
decision rather than assumed solvable by more searching.

---

## Datasets evaluated and rejected on licensing

### HGMD (Human Gene Mutation Database, QIAGEN)

| | |
|---|---|
| **What it covers** | The most comprehensive curated catalogue of published disease-causing/disease-associated human gene mutations in the field -- broader curated-mutation coverage than ClinVar in several respects, widely used as a comparison point in clinical variant interpretation literature. |
| **License** | Two-tier, confirmed via HGMD's own site and corroborating sources: the free **Public** version is "freely available only to registered users from academic institutions/non-profit organisations" and is explicitly the *less up-to-date* version -- not usable for a commercial product even setting aside currency concerns. The **Professional** version (current, complete) requires **"all commercial users are required to purchase a license from QIAGEN"** (`hgmd.cf.ac.uk`, corroborated by QIAGEN's own product page). |
| **Commercial-use verdict** | **Not usable free; conditional on purchasing a paid QIAGEN Professional license.** Named explicitly per the LICENSE_AUDIT.md convention of naming the condition rather than a bare "no." Cost was not obtainable without contacting QIAGEN directly (out of this task's scope). If budget exists for it, this would be the single highest-value paid addition to this inventory -- broader mutation coverage than the free alternatives above. |

### PharmVar (Pharmacogene Variation Consortium)

| | |
|---|---|
| **What it covers** | The authoritative star-allele nomenclature database for pharmacogenes (CYP2D6, CYP2C19, etc.) -- relevant specifically to kim_pipeline's PGx stage as potential ground truth for star-allele/diplotype calling, not to geper's ACMG classification. |
| **License** | **Creative Commons Attribution-NonCommercial-NoDerivs.** Explicitly non-commercial; distribution permitted only with attribution, unmodified, and not for commercial use. |
| **Commercial-use verdict** | **Not usable.** No paid tier or licensing contact found (unlike HGMD, this is not a "buy your way in" case) -- a clean reject for a commercial product's ground truth, same shape as the OMIM/PharmVar-style research-only terms already seen twice in this codebase's history. |

### DECIPHER (Sanger Institute / Wellcome)

| | |
|---|---|
| **What it covers** | Chromosomal-imbalance and rare-disease phenotype-linked variant data, used in the field for CNV/structural-variant interpretation support -- a possible ground-truth candidate if GEPER/kim_pipeline ever validates CNV calling (not currently in scope per the codebase reviewed). |
| **License** | Registered-access Data Access Agreement, confirmed via the DAA's own stated terms (corroborated by `reusabledata.org`'s independent classification, itself a secondary source but consistent with the DAA text found): **"accepted users can only use data for research"** -- explicit, unqualified research-only restriction, plus data-purging/QA obligations that reusabledata.org's own assessment says make "downstream reuse... problematic" even within the research carve-out. |
| **Commercial-use verdict** | **Not usable.** No commercial licensing path found or referenced anywhere in the sourcing checked. |

---

## Datasets requiring a named condition before use

### PharmGKB / ClinPGx

| | |
|---|---|
| **What it covers** | Curated gene-drug interaction and phenotype-outcome annotations -- relevant to kim_pipeline's PGx stage as a possible evidentiary/ground-truth reference for drug-response phenotype prediction, distinct from PharmVar's star-allele nomenclature role above. |
| **License, verbatim** | Fetched directly (`blog.clinpgx.org/changes-to-pharmgkb-data-licensing/`, the platform's own licensing-change announcement): **"All of PharmGKB, including these files, are now licensed under the Creative Commons Attribution-ShareAlike 4.0 International License."** Immediately followed by an added restriction beyond the CC-BY-SA baseline: **"Under no circumstances can PharmGKB data be sold for other's private or commercial use."** ShareAlike also obligates: "if you alter, amend, reuse or otherwise change PharmGKB data... all recipients shall first agree to be subject to this license" if the changed product is distributed. |
| **Commercial-use verdict** | **Conditional -- usable, with the condition named**: base CC-BY-SA 4.0 permits commercial use of the data/derivatives, but PharmGKB's own added term blocks *reselling* the data itself. Same shape as MaveDB's CC-BY-SA score sets already handled elsewhere in this codebase (`LICENSE_AUDIT.md`'s MaveDB section) -- reading PharmGKB as an evidence reference (never redistributing its underlying database as a standalone product) avoids the resale restriction; any redistributed *derivative* would need to carry the same CC-BY-SA license forward, per ShareAlike. |

### Illumina Platinum Genomes (CEPH 1463 pedigree)

| | |
|---|---|
| **What it covers** | Truth-set variant calls for a well-studied 17-member three-generation pedigree (CEPH/Utah family 1463), validated via haplotype-inheritance logic across the whole family rather than orthogonal-technology confirmation the way GIAB is -- a complementary, not redundant, variant-calling validation resource to GIAB, of a single European-ancestry family rather than GIAB's broader ancestry spread. |
| **License** | **No explicit license file or terms-of-use statement was found** in the `Illumina/PlatinumGenomes` GitHub repository (README describes download/citation only) or on the public AWS S3 bucket (`platinum-genomes`, openly downloadable with `--no-sign-request`, i.e. no access agreement gate at all). Some related samples are noted elsewhere as available only via controlled access at dbGaP -- the truth-set files themselves, per what was checked, are not behind that gate. |
| **Commercial-use verdict** | **Undetermined -- do not treat as verified-usable.** Freely downloadable with no access agreement is a strong practical signal, but the *absence* of an explicit license is not the same as an affirmative commercial grant, and this audit's own standard (verify, don't infer) means this cannot be marked "usable" on absence of a restriction alone. Flagging as needing either a direct statement from Illumina or independent legal sign-off before relying on it, same posture `LICENSE_AUDIT.md` already takes toward independent legal review generally. |

---

## Covering note

**Datasets I consider genuinely usable today, without further negotiation or
purchase:** the ClinVar expert-panel/practice-guideline subset (23,065
records; already flowing through GEPER's existing ClinVar client, so no new
integration cost) and GIAB (seven benchmark genomes, NIST public domain).
These answer two different validation questions -- classification concordance
and variant-calling accuracy respectively -- and a validation plan needs both,
not either.

**The single dataset whose loss would break the validation plan:** the
ClinVar expert-panel/practice-guideline subset. It is the only dataset found,
at any scale, that offers *graded* ACMG-relevant classification ground truth
obtainable today without a purchase or a DAC application. GIAB validates a
different, narrower question (did the caller get the right genotype) and has
no bearing on whether a classification is correct. If ClinVar's terms
changed, or if the methodological circularity flagged above (GEPER already
consumes ClinVar as live evidence for some of the same variants) turns out to
disqualify it as independent ground truth for those variants, there is
currently no alternative of comparable scale to fall back on -- that is a
real single point of failure in the whole validation plan, not a hypothetical
one.

**The most consequential negative finding is the ICMR/India gap above.** If
population-matched ground truth is a hard requirement for ICMR/NABL
readiness, it does not exist off-the-shelf today; every candidate checked is
either already declined (IndiGen) or gated behind a process with no
established commercial terms (GenomeIndia, GenomeAsia100K). This should reach
whoever owns the ICMR/NABL readiness question directly, not stay implicit in
an inventory document.

---

## Outstanding items

1. **Independent legal review**, same standing recommendation as both prior
   audits -- this was compiled by fetching primary sources directly, not by
   outside counsel.
2. **GenomeAsia100K's actual Data Access Agreement text** was not reached;
   resolving its commercial-use verdict from "undetermined" to a real answer
   needs a DAC application, out of this task's scope.
3. **GenomeIndia's sourcing is weaker than every other row** in this
   document -- both direct-fetch attempts (`genomeindia.org`, the specific
   PIB press release) failed at the network level, so that section rests on
   secondary corroboration rather than one direct primary fetch. Worth a
   follow-up attempt with a different access path if this dataset's status
   becomes decision-relevant.
4. **GIAB's licensing citation is weaker than ClinVar's** -- no single
   GIAB-specific terms document was found; the verdict rests on NIST's
   general policy plus the program page's consent language, not a dedicated
   GIAB license file. Flagged in that row, not treated as fully closed.
5. **HGMD Professional's actual price** was not obtained (requires direct
   QIAGEN contact) -- relevant only if a paid-license path is pursued later.

## Re-verify this audit if

Any of these datasets' own terms change, a new ground-truth candidate is
proposed, the concordance study's design is finalized (which may sharpen or
remove the ClinVar circularity flag above), or as part of general periodic
license-compliance review before a commercial release -- same cadence as the
other two audits.
