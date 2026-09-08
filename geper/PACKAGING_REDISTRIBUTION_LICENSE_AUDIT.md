# Packaging Part 2 -- Redistribution Licensing, Per Source, With Quoted Clauses

**Scope of this file, stated once so it is not re-derived per row:** `LICENSE_AUDIT.md`
and `DATA_SOURCE_LICENSE_AUDIT.md` audit whether GEPER may **use** a model or
data source at runtime -- load weights into a process, or query/cache an
external database's data as evidence. This file audits a different
permission: whether that same model's weights, or a snapshot of that same
database, may be **bundled inside a Docker image and shipped to a paying
customer**, per `PACKAGING-offline-sites-shipped-image-must-contain-model-weights-huggingface-acquisition-is-not-an-install-path`
(an on-premise site with no internet cannot acquire weights or data at
first run, so the shipped image must contain them). A source can be
perfectly usable and not redistributable; nothing in the two audits above
was ever asked to draw that line, so this file draws it fresh, per source,
against primary-source licence text.

**Standard licence texts (MIT, Apache-2.0, BSD-3-Clause, CC0 1.0, CC BY
4.0, CC BY-SA 4.0, GPL-3.0) were fetched directly from their canonical
publishers (opensource.org, apache.org, creativecommons.org, gnu.org) in
this pass** and are quoted below rather than recalled from memory or from
the earlier audits' summaries, per the standing instruction that a
redistribution verdict without a quoted clause is an opinion with a legal
shape. Repository- and dataset-specific facts (which exact licence
governs which exact model/database, which exact URL GEPER's code
downloads from) are carried over from `LICENSE_AUDIT.md` and
`DATA_SOURCE_LICENSE_AUDIT.md`, which already fetched those primary
sources directly; this file does not re-fetch them unless noted.

**`AM-04-attribution-condition-unresolved` is BLOCKED ON THE HUMAN** (does
CC BY 4.0 attribution trigger for a single extracted row in a report?).
That question is about single-row *use* in a report, not about
redistributing a bundled copy of a database inside an image. Bundling a
database snapshot is unambiguously "Sharing" under CC BY 4.0's own broad
definition (reproduction, distribution -- see the CC BY 4.0 quote below);
AM-04's narrower open question does not gate this file's verdicts, and no
verdict below depends on AM-04's answer. Stated explicitly so this is not
silently assumed either way.

**Out of scope:** the ground-truth validation datasets in
`GROUND_TRUTH_DATASET_AUDIT.md` (ClinVar expert-panel subset used as a
benchmark, GIAB, PharmGKB, HGMD, PharmVar, DECIPHER, Illumina Platinum
Genomes, GenomeAsia100K, GenomeIndia, IndiGen) are used by the
*validation study*, not by the *product a customer runs* -- they are not
loaded, queried, or bundled by `geper/` or `kim_pipeline/` at runtime.
Unless a future decision bundles them into a customer-facing offline
site (e.g. for on-site re-validation), they are not redistributed to a
customer today and are not scored here. If that changes, this file
needs a second pass.

---

## Standard licence texts, quoted, referenced by short name in every row below

| Short name | Governing clause, quoted from the canonical source | Redistribution permitted? | Conditions on redistribution |
|---|---|---|---|
| **MIT** | "Permission is hereby granted, free of charge, to any person obtaining a copy of this software... to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, **distribute**, sublicense, and/or sell copies of the Software..." "The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software." (opensource.org/license/mit) | **Yes** | Preserve the copyright notice + licence text in every copy shipped. |
| **Apache-2.0** | Section 4: "You must give any other recipients of the Work or Derivative Works a copy of this License"; "You must cause any modified files to carry prominent notices stating that You changed the files"; "You must retain... all copyright, patent, trademark, and attribution notices"; "If the Work includes a 'NOTICE' text file..., then any Derivative Works that You distribute must include a readable copy of the attribution notices" (apache.org/licenses/LICENSE-2.0) | **Yes** | Include a copy of the licence; preserve NOTICE file contents; mark any changed files. |
| **BSD-3-Clause** | "Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer." "Redistributions in binary form must reproduce the above copyright notice... in the documentation and/or other materials provided with the distribution." Plus a no-endorsement clause. (opensource.org/license/bsd-3-clause) | **Yes** | Preserve copyright notice + disclaimer (source or binary form); no use of the author's name to endorse derived products. |
| **CC0 1.0** | "Affirmer hereby overtly, fully, permanently, irrevocably and unconditionally waives, abandons, and surrenders all of Affirmer's Copyright and Related Rights... for any purpose whatsoever, including without limitation commercial, advertising or promotional purposes." (creativecommons.org/publicdomain/zero/1.0/legalcode) | **Yes** | None -- full public-domain dedication. |
| **CC BY 4.0** | Section 2(a)(1): licensor grants a "worldwide, royalty-free, non-sublicensable, non-exclusive, irrevocable license to... reproduce and Share the Licensed Material, in whole or in part." Section 3(a)(1): the licensee must "retain... identification of the creator(s)... a copyright notice... a notice that refers to this Public License... a URI or hyperlink to the Licensed Material," and "indicate if You modified the Licensed Material." Section 4: for database rights, the license "grants You the right to extract, reuse, reproduce, and Share all or a substantial portion of the contents of the database." (creativecommons.org/licenses/by/4.0/legalcode) | **Yes**, explicitly including bulk/substantial-portion database redistribution | Attribution notice (creator, copyright notice, licence reference/link, modification flag) retained wherever the material is shared. |
| **CC BY-SA 4.0** | Section 3(b): "The Adapter's License You apply must be a Creative Commons license with the same License Elements, this version or later, or a BY-SA Compatible License." (creativecommons.org/licenses/by-sa/4.0/legalcode) | **Yes, for the redistributed item itself** | ShareAlike applies to *Adapted* (modified) Material -- redistributing the item unmodified requires only CC BY 4.0's own attribution terms plus keeping the CC BY-SA notice on that item; it does not require relicensing the whole product under CC BY-SA. |
| **GPL-3.0** | Section 5: "You must license the entire work, as a whole, under this License to anyone who comes into possession of a copy." Section 4 requires the same terms pass through on verbatim copies. (gnu.org/licenses/gpl-3.0.en.html) | **Effectively no**, for a proprietary commercial codebase | Would require the entire combined work (not just the GPL component) to ship under GPL-3.0. |
| **CC BY-NC 4.0** | Not independently re-fetched in this pass (already established as a hard commercial blocker in `LICENSE_AUDIT.md`'s SpliceAI row -- "NC" bars commercial use outright, which is the entire product's positioning, so the redistribution question does not even arise). | **No** | N/A |

---

## Models -- redistribution verdict, per source

Model-to-licence mapping (which exact repo/URL, which exact licence) is
carried over from `LICENSE_AUDIT.md`, already fetched against primary
sources there; this table applies the redistribution clauses above to
each.

| Source | Licence (code / weights) | Verdict | Concrete obligation if conditional |
|---|---|---|---|
| HyenaDNA | Apache-2.0 (code) / BSD-3-Clause (weights) | **May redistribute** | Ship a NOTICE/attribution file: Apache-2.0's NOTICE-preservation clause for the code, BSD-3-Clause's copyright-notice-in-distribution clause for the weights. |
| Evo 2 | Apache-2.0 composite (Apache-2.0 + bundled BSD-3-Clause + MIT upstream code) / Apache-2.0 (weights) | **May redistribute** | Same NOTICE-preservation obligation as HyenaDNA. Moot in practice today -- Evo 2 has no CPU inference path and GEPER already skips it gracefully, so it is unlikely to be baked into a CPU-only offline image at all; if it ever is, the obligation applies. |
| RNA-FM | MIT (code) / Apache-2.0 (weights, `cuhkaih/rnafm`) | **May redistribute** | Preserve MIT copyright notice for the code; NOTICE-preservation for the weights if the HF repo ships a NOTICE file (not separately re-checked in this pass). |
| ESM-2 (geper's copy) | MIT (code) / MIT (weights, `facebook/esm2_t33_650M_UR50D`) | **May redistribute** | Preserve copyright notice in the shipped copy. |
| AlphaMissense catalogue | Apache-2.0 (code, unused) / CC BY 4.0 (predictions catalogue) | **May redistribute**, including the full ~71M-row catalogue file GEPER already downloads whole -- CC BY 4.0's own database-rights clause explicitly names "all or a substantial portion of the contents" as within the grant | Attribution notice (DeepMind copyright, CC BY 4.0 reference/link) retained wherever the catalogue file is shipped. GEPER's report layer already carries this citation per `AM-05`; the *file itself* inside the image also needs the notice, which is a packaging step, not a report step. |
| MMSplice | MIT (code) / MIT (weights) | **May redistribute** | Preserve copyright notice. |
| TensorFlow | Apache-2.0 (mandatory MMSplice dependency) | **May redistribute** | NOTICE preservation, ordinarily satisfied automatically by shipping the unmodified pip wheel with its own bundled licence metadata intact. |
| Enformer | MIT (code) / CC BY 4.0 (weights, `EleutherAI/enformer-official-rough`) | **May redistribute** | Attribution notice for the weights (DeepMind, CC BY 4.0 reference). |
| Borzoi | Apache-2.0 (code) / CC BY 4.0 (weights, `johahi/borzoi-replicate-0`) | **May redistribute** | NOTICE preservation (code) + attribution notice (weights). The existing code-level guard restricting weight loads to the `johahi/` namespace is a separate, unaffected control. |
| SpliceFormer | MIT (code) / MIT (weights) | **May redistribute** | Preserve copyright notice. |
| SpliceBERT | BSD-3-Clause (code) / CC BY 4.0 (weights, Zenodo record 7995778) | **May redistribute** | BSD-3-Clause copyright notice (code) + CC BY 4.0 attribution notice (weights). Currently disabled by default (`ENABLE_SPLICEBERT=false`, a runtime-stability gate, unrelated to this licensing verdict) -- if baked into an offline image while disabled, the obligation still applies to the bytes shipped, whether or not the flag is on. |
| SPiP | MIT (code) / public-domain-equivalent reference data (RefSeq transcript annotation, genome sequence -- same category as data every other GEPER stage reads, not a third party's trained weights) | **May redistribute** | Preserve MIT copyright notice for the code; no separate condition for the reference data. |
| DNABERT-2 (`kim_pipeline/pipeline/ai/engine.py::DnaBertEngine`, `zhihan1996/DNABERT-2-117M`) | **UNVERIFIED** -- `LICENSE_AUDIT.md` already flagged this as an open, unreviewed licensing gap (the 2026-08-20 security review fetched the model's *code* to assess `trust_remote_code=True` RCE risk, not its licence metadata); re-attempted a direct fetch of the HF model card in this pass and the licence field was **not visible in the extracted content** -- could not establish the clause. | **UNVERIFIED -- cannot state a redistribution verdict** | See "Would force a product change," below. This is the one model row where "ships or doesn't ship" genuinely cannot be answered from what is on record. |
| ESM-2 (kim_pipeline's copy, same model as geper's row) | MIT / MIT | **May redistribute** | Same as geper's ESM-2 row -- listed separately only because it is a second load site in a second subproject, not a second licence question. |

**Evaluated and explicitly not integrated (not currently loaded by either
subproject; listed for completeness per `LICENSE_AUDIT.md`'s own
precedent, verdict is hypothetical/N/A unless reconsidered):**

| Source | Licence | Verdict if it were ever shipped |
|---|---|---|
| OpenSpliceAI | GPL-3.0 (code and bundled pretrained models) | **May not** redistribute inside GEPER's proprietary image -- GPL-3.0 §5 would require licensing the *entire combined work* under GPL-3.0. Already rejected on this basis; not revisited here. |
| SpliceAI (Illumina) | GPL-3.0 (code) / CC BY-NC 4.0 (weights, Dec 2023 relicense) | **May not** -- GPL-3.0 blocks redistribution the same way as OpenSpliceAI, and CC BY-NC 4.0 independently blocks it on commercial-use grounds alone, before redistribution is even reached. Two independent blockers, not one. |

---

## Databases -- redistribution verdict, per source

`geper/` currently reaches most of these live at runtime (an API call or a
per-query lookup), not as a bundled snapshot. For an air-gapped site, a
live call becomes impossible regardless of licence -- that is `PACKAGING-P3`'s
question (sizes, cadence, what a stale cache breaks), not this file's.
This file answers only: **if a snapshot of this source were bundled into
the image, would that be legally permitted, and on what terms?** A "may
redistribute" verdict below does not mean the bundling work is already
done -- several of these (AlphaFold DB, BLAST, Ensembl/1000G SAS) are
live-query-only today and would need a local/bulk copy built, which is a
technical change independent of the legal answer.

| Source | Licence / terms, quoted | Verdict | Concrete obligation if conditional |
|---|---|---|---|
| ClinVar (NCBI) | "Information that is created by or for the US government on this site is within the public domain... may be freely distributed and copied." | **May redistribute** | None. |
| dbSNP (NCBI) | Same NCBI site-wide policy as ClinVar. | **May redistribute** | None. |
| MANE (NCBI/EBI) | NCBI side: same public-domain policy. EBI side: no MANE-specific restrictive licence found (per `DATA_SOURCE_LICENSE_AUDIT.md`). | **May redistribute** | None found. |
| NCBI BLAST reference database | Currently a **live remote-service call** (GEPER submits queries, redistributes nothing today). If an offline site instead needs a **local BLAST database** built from NCBI sequence data, that data falls under the same NCBI public-domain policy quoted above. | **May redistribute** (conditional on this being NCBI-sourced sequence data, not a third-party BLAST database) | Confirm the specific reference set chosen for a local build is itself NCBI-originated before bundling; this is a `PACKAGING-P1`/`P3` build question, not a licence blocker. |
| gnomAD (Broad Institute) | "The primary data from the gnomAD exomes and genomes are available free of restrictions under the Creative Commons Zero Public Domain Dedication... There are absolutely no restrictions or embargoes on the publication of results." | **May redistribute** | None required; a courtesy acknowledgment is requested, not required, per gnomAD's own terms (already quoted in `DATA_SOURCE_LICENSE_AUDIT.md`). |
| ClinGen (gene validity, dosage sensitivity, ERepo) | "All curated content published by ClinGen is available free of restriction under the CC0 1.0 Universal... Public Domain Dedication." | **May redistribute** | None. |
| UniProt | CC BY 4.0 -- "for any purpose, even commercially," with attribution. | **May redistribute** | Attribution notice, as above. |
| InterPro / Pfam | "All of the InterPro, Pfam, PRINTS and SFLD downloadable data... is freely available under CC0 1.0 Universal... You do not need a special license for commercial use but please cite the resource." | **May redistribute** | None required; citation requested. |
| AlphaFold DB (EMBL-EBI / Google DeepMind) | "Data is available for academic and commercial use, under a CC-BY-4.0 license." Currently accessed via **live per-query lookup**, not bulk download. | **May redistribute** | Attribution notice if a bulk local copy is bundled. Switching from live lookup to a bundled local copy is a technical/product change (`PACKAGING-P1`/`P3`), not a licence blocker -- CC BY 4.0 explicitly covers "all or a substantial portion" per its database-rights clause quoted above. |
| Ensembl (GTF/CDS bootstrap, REST API) | **Re-fetched directly in this pass** (the original `DATA_SOURCE_LICENSE_AUDIT.md` entry was bot-blocked and sourced via a search index; direct fetch succeeded here): "Ensembl imposes no restrictions on access to, or use of, the data provided and the software used to analyse and present it," with Ensembl's own code (not the data GEPER uses) under Apache-2.0. Source: `jun2026.archive.ensembl.org/info/about/legal/disclaimer.html` (redirected from `ensembl.org`'s own disclaimer URL). | **May redistribute** | None found on the data side; the page notes users remain responsible for any third-party constraints on data Ensembl itself incorporates from elsewhere -- not independently traced further in this pass. **Confidence upgraded from the prior audit**: now a direct primary fetch, not a search-index citation. |
| 1000 Genomes SAS (via Ensembl) | Accessed exclusively through Ensembl's REST API, so Ensembl's no-restriction stance (row above) governs the access path GEPER uses. IGSR's own site states: "IGSR provides open data to support the community's research efforts," directing to its own data disclaimer for details (checked in this pass; the disclaimer sub-page itself was not separately traced). | **May redistribute** (via the Ensembl access path GEPER actually uses) | Not independently re-verified against IGSR's own primary terms page directly (same caveat the original audit flagged) -- if a bulk local 1000G SAS copy is ever built independent of Ensembl's API, that should be re-checked directly against IGSR's own terms rather than assumed to inherit Ensembl's. |
| HPO (Human Phenotype Ontology) | CC BY 4.0, per the original audit's moderate-confidence finding (the specific licence sub-page 404'd; sourced via corroborating secondary confirmation, not one direct fetch). Not re-fetched in this pass. | **May redistribute**, moderate confidence | Attribution notice. **Sourcing strength carried over unchanged and flagged**: this is the weakest-sourced "may" verdict in this table; worth a direct re-fetch before this is relied on for a shipped bundle, not just a live evidence-source citation. |
| Orphanet / Orphadata | "Users are free to copy, distribute, display and make commercial use of this data in all legislations, provided they cite the provenance." Explicitly CC BY 4.0. Sourced via search-engine index in the original audit, corroborating the repo's own prior `DATA_PROVENANCE.md` finding; not re-fetched directly in this pass. | **May redistribute** | Attribution notice (provenance citation), which the text itself makes an explicit condition. |
| MaveDB | **Mixed, per-score-set, not platform-wide.** Active/permissive: CC0, CC BY 4.0, CC BY-SA 4.0. Inactive/restrictive: CC BY-NC-SA 4.0, "Other - See Data Usage Guidelines" (unspecified terms). | **Conditional -- and this is the one row where "use" and "redistribution" genuinely diverge, which is exactly the distinction this whole directive is about.** | `DATA_SOURCE_LICENSE_AUDIT.md`'s existing fix (`_index_one_score_set`) filters by license **at query time**, so CC BY-NC-SA / "Other" records never become *evidence in a report* -- that fix answers the **use** question and was correctly scoped to it. It does not answer whether those same excluded records, or any MaveDB records at all, may be **bundled as a snapshot inside an offline image** -- a different action nobody has designed yet. If MaveDB is ever bundled for offline use, the same per-record license filter must gate what goes *into the bundle*, not just what the pipeline reads back out of it; CC BY-SA 4.0 records may be included but must carry their own attribution + CC BY-SA notice (verbatim inclusion does not trigger relicensing the whole product, per the CC BY-SA clause quoted above -- only *adapting* a CC BY-SA record would). |
| IndiGenomes | N/A -- retired from active use 2026-08-08 (commercial-use conflict, see `DATA_SOURCE_LICENSE_AUDIT.md`); not queried, not bundled. | **N/A, not shipped** | If reinstated with a commercial licence from CSIR-IGIB (not currently pursued), this file would need a redistribution-specific pass on whatever that licence's terms are -- "commercial use permitted" is not automatically "redistribution permitted," the exact distinction this whole directive exists to check. |
| OMIM | N/A -- code fully removed from `kim_pipeline` 2026-08-20 (research-use-only conflict). | **N/A, not shipped** | None -- the integration would need to be rebuilt from scratch if OMIM is ever reconsidered, at which point this applies fresh. |

---

## Which sources, if any, would force a product change

**One, clearly: DNABERT-2's licence is unverified, and `kim_pipeline` --
which the shipped Dockerfile does bundle (confirmed by grep: `Dockerfile`
copies `kim_pipeline/requirements.txt` and invokes `kim_pipeline/main.py`
as a subprocess) -- loads it via `from_pretrained`, i.e. from the
network, with no baked weights confirmed anywhere in the packaging work
done so far.** This is not a "may not redistribute" finding -- it is a
**"cannot yet say" finding**, which is a different and in one sense more
urgent problem: every other row in this file has an answer a build
engineer can act on today; this one does not. Until DNABERT-2's actual
licence is established (fetch the HF repo's licence metadata directly,
not the model card body, which is where this pass's attempt came up
empty), the offline image cannot honestly claim DNABERT-2 is baked in
*or* that it is safe to bake in. If it turns out non-permissive, the
product change is not hypothetical: `kim_pipeline`'s FASTQ-to-VCF engine
would either lose its DNABERT-2 DNA-sequence scoring at offline sites, or
require a licensed/relicensed alternative -- HyenaDNA already fills the
equivalent role in `geper/`'s pipeline per `LICENSE_AUDIT.md`'s "removed
from geper entirely" note, which is the closest existing precedent for
what that substitution would look like.

**Everything else in this file is a "may redistribute" or "may
redistribute, conditionally," and every condition found is a tractable
packaging obligation** (a bundled attribution/NOTICE file; a per-record
license filter applied at bundle-build time instead of only at
query-read time for MaveDB) **-- none of them requires relicensing GEPER
itself, none of them is a hard blocker, and none of them changes which
models or databases the product can offer offline.** The two GPL-3.0
sources that would be hard blockers (OpenSpliceAI, SpliceAI) are already
not integrated and were rejected for licensing reasons before this
question was even asked.

**Two rows are weaker-sourced than the rest and are worth a direct
re-fetch before being relied on for an actual shipped bundle, not just
flagged and moved past**: HPO (moderate confidence, original 404 on the
licence sub-page, corroborated only secondarily) and Orphanet (sourced
via search-index in the original audit, not independently re-fetched in
this pass either). Neither is disputed by any conflicting source, unlike
AlphaMissense's now-resolved CC BY vs CC BY-NC-SA history -- but
"undisputed" and "directly verified" are not the same claim, and this
file does not conflate them.

## Re-verify this audit if

Any new model or database source is integrated into either `geper/` or
`kim_pipeline/`; DNABERT-2's licence is ever established (this file's one
open item); any source currently accessed live (AlphaFold DB, BLAST,
Ensembl/1000G SAS) is converted to a bundled local copy for offline
packaging, at which point its *scale* of redistribution changes even if
its licence does not; or as part of general periodic license-compliance
review before a commercial release -- same cadence as `LICENSE_AUDIT.md`
and `DATA_SOURCE_LICENSE_AUDIT.md`.
