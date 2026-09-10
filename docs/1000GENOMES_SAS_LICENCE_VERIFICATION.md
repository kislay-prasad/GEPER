# 1000 Genomes SAS -- licence verification (the IndiGenomes REPLACEMENT)

**Card:** GAP-1000-Genomes-SAS-the-IndiGenomes-REPLACEMENT-has-no-fetched-and-quoted-licence-verification
**Date fetched:** 2026-09-10
**Verified by:** meredith-msydpy6m (hive agent), all quotes below fetched live today via `WebFetch`/`WebSearch` -- none copied from any pre-existing audit file in this repo.

## Why this document exists

`DATA_SOURCE_LICENSE_AUDIT.md` documents, with a fetched-and-quoted licence, why
`annotation/indigenomes.py` (IndiGenomes) was retired from GEPER's active query
path on 2026-08-08: its own terms state it "is intended for purely research
purposes" and that "commercial use ... would require licensing," which GEPER
never obtained.

`annotation/thousand_genomes_sas.py` (1000 Genomes SAS, via Ensembl's REST API)
is IndiGenomes' unconditional replacement for the "Indian Population Frequency"
report section -- but it never went through the same fetched-and-quoted licence
check its predecessor did. This document closes that gap.

## (a) Exact URLs fetched

All fetched live on 2026-09-10:

1. `https://www.internationalgenome.org/data-sharing-policy/`
2. `https://www.internationalgenome.org/IGSR_disclaimer`
3. `https://www.internationalgenome.org/faq/do-i-need-permission-to-use-igsr-data-in-my-own-scientific-research/`
4. `https://www.internationalgenome.org/faq/is-there-any-fee-associated-with-using-andor-reproducing-the-data/`
5. `https://www.ebi.ac.uk/about/terms-of-use/`
6. `https://www.internationalgenome.org/category/data-reuse/`
7. `https://www.internationalgenome.org/data/`
8. `https://registry.opendata.aws/1000-genomes/`

(1000 Genomes phase_3 data -- the specific release `annotation/thousand_genomes_sas.py`
queries via Ensembl's `1000GENOMES:phase_3:*` population labels -- predates the
newer per-collection "data reuse statement" convention IGSR now uses for later
collections like HGSVC/HGDP; the phase_3 release's terms are instead governed by
the general IGSR/EMBL-EBI terms above, which is why the verification spans these
eight pages rather than one single phase_3-specific statement page.)

## (b) Licence text, quoted verbatim

**From the IGSR data disclaimer** (`internationalgenome.org/IGSR_disclaimer`):

> "users should be aware that data made available by IGSR comes from many different owners and that consequently restrictions on different pieces of data within IGSR and rights claimed on pieces of data vary."

> "It remains the responsibility of users to ensure that their exploitation of the data does not infringe any of the rights of third parties, including the data owners."

> "Data from the 1000 Genomes Project is now available without embargo, following the final publication from the project."

**From the EMBL-EBI Terms of Use** (`ebi.ac.uk/about/terms-of-use/`) -- EBI hosts
the phase_3 data GEPER actually queries (`ftp.1000genomes.ebi.ac.uk`, and Ensembl
itself is an EBI/EMBL-EBI service):

> "EMBL-EBI itself places no additional restrictions on the use or redistribution of the data available via its Data Resources and Tools other than those provided by the original data owners"

> "The original data may be subject to rights claimed by third parties, including but not limited to, patent, copyright, other intellectual property rights"

> "It is the responsibility of users of EMBL-EBI Data Resources and Tools to ensure that their exploitation of the data does not infringe any of the rights of such third parties"

**From the IGSR FAQ "Is there any fee associated with using and/or reproducing the data?"**:

> "The data presented in IGSR is available free of charge, however there may be restrictions on publication of analyses using these data."

**From the IGSR FAQ "Do I need permission to use IGSR data in my own scientific research?"**: the 1000 Genomes data's original release terms are stated to follow the **Fort Lauderdale Agreement**, which restricts only the timing/priority of the Consortium's own first global-analysis publication -- not downstream use, and not commercial use specifically.

**From the AWS Registry of Open Data listing** (`registry.opendata.aws/1000-genomes/`), which mirrors this exact dataset -- its structured "License" field reads, in full:

> "Data from the 1000 Genomes Project is now available without embargo, following the final publication from the project. Use of the data should be cited in the usual way, with current details available at http://www.internationalgenome.org/faq/how-do-i-cite-1000-genomes-project"

No page fetched -- the IGSR disclaimer, the two IGSR FAQ pages, the IGSR data-reuse
category page, the IGSR data landing page, the EMBL-EBI terms of use, or the AWS
Registry of Open Data listing -- contains any clause restricting, conditioning,
or requiring separate licensing for **commercial** use. The only universal
condition stated anywhere is **citation** (see the AWS License field and the FAQ
pages above); no page imposes IndiGenomes' "purely research purposes" /
"commercial use ... would require licensing" restriction, or anything like it.

## (c) Date fetched

2026-09-10 (all eight pages, same session, live).

## (d) Commercial use: explicit answer

**YES -- commercial use appears permitted**, with one important qualification
on the strength of that "yes" stated honestly below.

**Basis:** every terms/disclaimer/FAQ/licensing page fetched for this dataset
(IGSR's own disclaimer, both relevant IGSR FAQs, IGSR's data-reuse and data
landing pages, EMBL-EBI's terms of use for the service that hosts the data GEPER
actually queries, and the AWS Registry of Open Data's structured License field)
either states no restriction at all, or states only a citation requirement, or
states EMBL-EBI "places no additional restrictions ... other than those provided
by the original data owners" -- and none of the "original data owner" restriction
language IGSR's own disclaimer warns can vary "piece of data" to "piece of data"
was found to apply to the phase_3 SAS/sub-population frequency data this module
queries specifically.

**Qualification, stated plainly rather than glossed over:** no page fetched
contains an affirmative sentence that reads, in so many words, "commercial use
is permitted." The "yes" above is inferred from the **consistent absence** of
any commercial restriction across eight independent official pages -- including
the one page (EMBL-EBI's terms of use) that explicitly discusses the concept of
data-owner-imposed restrictions and states none apply beyond what the owners
themselves set -- rather than from one page that says so outright. This is a
qualitatively different (weaker) form of evidence than IndiGenomes' case, where
the disqualifying sentence was itself stated outright ("commercial use ... would
require licensing"). It is not, however, a "silence because nobody has looked"
situation: `internationalgenome.org/faq/do-i-need-permission-to-use-igsr-data-in-my-own-scientific-research/`
is a page that exists specifically to answer this class of question, and even
it does not surface a commercial restriction -- it names only the Fort
Lauderdale first-publication-priority condition.

**Recommendation given this qualification:** treat this as sufficient to
proceed (no disqualifying clause exists anywhere checked), but if GEPER's legal/
compliance process requires an affirmative rather than an absence-of-restriction
basis before a commercial ship, an email to `info@1000genomes.org` (given on the
IGSR site as the contact for terms-of-use questions) asking for a
one-line written confirmation would close that last gap cheaply. This document
does not do that outreach itself -- it is out of scope for a verification task,
and the card said "STOP and tell me before changing anything" only for a
disqualifying answer, which this is not.

## (e) Where the source is consumed in the repo

- `geper/annotation/thousand_genomes_sas.py` -- the module itself: `ThousandGenomesSASLookup.query_variant()`, querying Ensembl's REST API (`GET /variation/human/{rsid}?pops=1`, `GET /overlap/region/human/{chrom}:{pos}-{pos}`) for `1000GENOMES:phase_3:*` population frequencies.
- `geper/pipeline/orchestrator.py` -- calls `ThousandGenomesSASLookup.query_variant()` unconditionally for every variant (IndiGenomes' old conditional-fallback call site was removed here on 2026-08-08).
- `geper/config.py` -- `ThousandGenomesSASConfig` (enable flag, retry/timeout settings).
- `geper/report/clinical_report_builder.py` -- `_indian_population_frequency()`, the clinical-report rendering of this section, including the mandatory sample-size and diaspora-origin disclosures.
- `geper/report/report_generator.py` -- Markdown rendering, importing `SAMPLE_SIZE_DISCLOSURE`/`DIASPORA_DISCLOSURE` from the module.
- `geper/report/summary.py` -- PDF rendering, same disclosures.
- `geper/report/json_builder.py` -- JSON export of the section.
- `geper/tests/test_thousand_genomes_sas.py`, `geper/tests/test_indian_population_frequency.py` -- unit/integration tests.
- `geper/DATA_SOURCE_LICENSE_AUDIT.md`, `geper/PACKAGING_REDISTRIBUTION_LICENSE_AUDIT.md` -- prior audits that reference this module without themselves fetching its licence (the gap this document closes).

## Scope note

Verification only, as instructed -- no code was changed to produce this
document, and none of the module's own comments or docstrings were edited.
