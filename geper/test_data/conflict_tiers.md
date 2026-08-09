# conflict_tiers.vcf — targeted test data for A3 Critical, A6 BP1 (VHL), C7 window clustering, A1 PP3/BP4 gate

**Purpose.** Across seven rounds of fixes, A3's Critical conflict-severity
tier (GEPER's own classification disagreeing with a `MATCHED` ClinVar
record reviewed by expert panel or endorsed as a practice guideline) has
never fired on live data — `nuclear_test.vcf`'s one GEPER-vs-ClinVar
disagreement (PRNP P102L) is a 2-star record, which correctly routes to
the Moderate tier instead. This file targets the untested paths directly.

**Do not run the pipeline against this file locally** (8GB RAM machine, no
GPU, model loads are off-limits here) — this is fixture + documentation
only, for the next Colab run. Parsing (not pipeline execution) was
verified with GEPER's own `pipeline.vcf_parser.VCFParser` — all 5 records
parse cleanly with the expected `chrom`/`pos`/`ref`/`alt`/INFO fields.

**Every coordinate, REF base, VCV accession, review status, and
classification below was looked up live** (NCBI E-utilities
`esearch`/`esummary` against the `clinvar` database, and Ensembl REST
`sequence/region` for GRCh38 REF-base confirmation) during this session —
none were constructed or guessed. Each variant's exact queries/URLs are
inherently reproducible from the accession given (`esummary.fcgi?db=clinvar&id=<uid>`);
UIDs are noted alongside each VCV accession below for exactly that reason.

**Predictions, not verified results.** Every "Expected GEPER behaviour"
below is this session's best-reasoned prediction of what GEPER's
*automated* evidence should conclude, based on reading the relevant rule
code (not by running it — no pipeline execution was possible here). The
whole point of building this fixture is to check these predictions
against a real run; a mismatch is a finding to investigate, not
necessarily a bug in this fixture. Where a prediction could plausibly go
either way, that uncertainty is stated explicitly rather than hidden.

---

## Variants

### 1. `3:37008903 C>T` — MLH1, the A3 Critical target
- **HGVS:** `NM_000249.4:c.543C>T` (`p.Gly181=`, synonymous)
- **ClinVar record:** VCV000633499 (uid 633499), dbSNP rs1481129490
- **Review status / star rating:** "reviewed by expert panel" — **3 stars** (InSiGHT Hereditary Colorectal Cancer/Polyposis VCEP)
- **ClinVar classification:** **Pathogenic**, last evaluated 2018-10-18, condition Lynch syndrome
- **REF base confirmed:** Ensembl GRCh38 `3:37008903` = `C` (matches)
- **Why this is the A3 candidate:** this is a synonymous change — no amino acid substitution. InSiGHT classified it Pathogenic based on a demonstrated splicing defect (an exonic variant that disrupts splicing despite not changing the encoded residue), which requires either a functional/splicing assay or a sensitive splice predictor to catch. GEPER's automated criteria that most directly drive a Pathogenic call for a coding SNV — PM1 (mutational hotspot/domain, weakened without an AA change), PP3/AlphaMissense (missense pathogenicity prediction — does not apply to a synonymous change), PVS1 (null variant — does not apply, this is not nonsense/frameshift/canonical splice site) — are all either inapplicable or weak for a synonymous variant. GEPER's splice-effect models (MMSplice/SpliceFormer/SpliceBERT) are the one path that *could* independently catch this, and might.
- **Expected GEPER behaviour (prediction):** GEPER's own classification plausibly lands on **VUS** (or, less likely, Likely Benign) rather than Pathogenic, purely from the mismatch between "no AA change" and "known pathogenic via splicing." ClinVar's record is `MATCHED` (same allele, not just co-located) and 3-star ("reviewed by expert panel") — if GEPER's classification is anything other than Pathogenic/Likely Pathogenic, `conflict_resolution_engine.py::_expert_panel_disagreement_conflict` should fire, producing **conflict_severity = Critical**. If GEPER's splice models *do* independently flag this as damaging and its own classification comes out Pathogenic/Likely Pathogenic anyway, A3 correctly will NOT fire — that outcome would mean GEPER's splice-effect evidence is more sensitive than this fixture assumed, which is worth noting either way.

### 2. `3:10142111 G>T` — VHL missense, the BP1-gate-on-VHL target
- **HGVS:** `NM_000551.4:c.264G>T` (`p.Trp88Cys`)
- **ClinVar record:** VCV000223171 (uid 223171)
- **Review status / star rating:** "reviewed by expert panel" — **3 stars** (VHL VCEP)
- **ClinVar classification:** **Likely pathogenic**, condition Von Hippel-Lindau syndrome
- **REF base confirmed:** Ensembl GRCh38 `3:10142111` = `G` (matches)
- **Why this is the BP1 candidate — corrected after reading `pipeline/acmg_rules.py::_bp1`/`_bp1_opposing_missense_evidence` directly (do not trust the simpler framing below without that check):** VHL's ClinGen dosage-sensitivity score is **3, "Sufficient evidence for dosage pathogenicity"** — the same `LOF_ESTABLISHED` state BRCA1 has (confirmed against the locally-cached `geper/model_cache/clingen/dosage_sensitivity.tsv`, see variant #5 below). BP1's *first* gate (gene-level LOF mechanism) therefore does **not** block VHL any differently than it doesn't block BRCA1 — a naive "VHL has a missense mechanism so BP1's gene gate should decline" framing would be wrong. The actual, second gate is what matters here: `_bp1_opposing_missense_evidence` — added specifically with "VHL type 2 disease is substantially missense-driven despite VHL's overall LOF/haploinsufficiency curation" named in its own docstring — blocks BP1 outright when AlphaMissense's `am_class == "likely_pathogenic"` or PM1 independently triggers for the residue. This exact code path has apparently never been exercised end-to-end against a real VHL variant; this is the real, previously-untested target, not a gene-level mechanism question.
- **Expected GEPER behaviour (prediction):** BP1 should evaluate **not_triggered**, via the opposing-evidence path specifically — the rationale should cite an AlphaMissense `likely_pathogenic` call and/or an independently-triggered PM1, not a bare "not a missense" or "gene mechanism doesn't apply" reason. This session did not query AlphaMissense's precomputed score for p.Trp88Cys directly, so whether the opposing-evidence gate actually fires (as opposed to BP1 firing unblocked, which would be the interesting negative result) is a genuine open question this run should answer, not something already known here.

### 3. `3:10149906 C>T` — VHL nonsense, C7 window-cluster member A
- **HGVS:** `NM_000551.4:c.583C>T` (`p.Gln195Ter`)
- **ClinVar record:** VCV000428794 (uid 428794)
- **Review status / star rating:** "reviewed by expert panel" — **3 stars** (VHL VCEP)
- **ClinVar classification:** **Pathogenic**, condition Von Hippel-Lindau syndrome
- **REF base confirmed:** Ensembl GRCh38 `3:10149906` = `C` (matches)

### 4. `3:10149933 G>T` — VHL nonsense, C7 window-cluster member B
- **HGVS:** `NM_000551.4:c.610G>T` (`p.Glu204Ter`)
- **ClinVar record:** VCV000526673 (uid 526673)
- **Review status / star rating:** "reviewed by expert panel" — **3 stars** (VHL VCEP)
- **ClinVar classification:** **Uncertain significance**, condition Von Hippel-Lindau syndrome
- **REF base confirmed:** Ensembl GRCh38 `3:10149933` = `G` (matches)
- **Why #3 and #4 together are the C7 window-cluster candidates:** 27 bp apart, same gene, same chromosome — real ClinVar-curated variants, not constructed to be close together for convenience (they were found already 27 bp apart in the live search results). `report/summary.py::_genomic_window_clusters` groups same-chromosome findings within `CONFIG.variant_clustering.WINDOW_BP` (default 1000 bp) of their nearest neighbor; 27 bp is comfortably inside that. This exercises the genomic-window half of C7, which — unlike the gene-grouping half (`_gene_clusters`, which only needs >1 finding sharing a gene symbol, already exercised) — has never fired on live data.
- **Expected GEPER behaviour (prediction):** with this VCF's row order (see the full listing below), these are findings **#3 and #4**. The report's multi-finding-observation section should include a line reading approximately: *"Findings #3, #4 sit within 1,000 bp of each other on chromosome 3 (positions 10149906-10149933)."* Separately, **all four VHL findings** (#2, #3, #4, #5) should also produce a gene-grouping line (*"4 findings in VHL: #2, #3, #4, #5."*) — that line is not new information (gene-grouping already works), but its co-occurrence alongside the window-cluster line is worth checking: the two are independent computations and should not be conflated in the rendered text.
- **Caveat:** the window-cluster computation only looks at `chrom`/`pos`, not gene — verified by reading `_genomic_window_clusters` that this VCF's other two VHL variants (10142111 and 10146594) are each >3 kb from their nearest neighbor and will *not* join this cluster, so the window-cluster line should name exactly #3 and #4, not all four VHL findings.

### 5. `3:10146594 AA>AAA` — VHL frameshift, the A1 PP3/BP4-gate-on-non-BRCA1 target
- **HGVS:** `NM_000551.4:c.422dup` (`p.Asn141fs`)
- **ClinVar record:** VCV000411979 (uid 411979)
- **Review status / star rating:** "reviewed by expert panel" — **3 stars** (VHL VCEP)
- **ClinVar classification:** **Pathogenic**, condition Von Hippel-Lindau syndrome
- **REF bases confirmed:** Ensembl GRCh38 `3:10146594-10146595` = `AA` (matches; VCF REF/ALT `AA`/`AAA` derived directly from ClinVar's own canonical SPDI `NC_000003.12:10146593:AA:AAA`, converted from SPDI's 0-based interbase convention to VCF's 1-based convention)
- **ClinGen dosage curation confirmed locally:** `geper/model_cache/clingen/dosage_sensitivity.tsv` (already-cached real ClinGen data, not re-fetched) lists VHL with haploinsufficiency score **3, "Sufficient evidence for dosage pathogenicity"** — the same curation tier BRCA1 has, on a different gene.
- **Why this is the A1 PP3/BP4-gate candidate:** a frameshift is a null variant — PVS1 should apply (LOF is an established mechanism per the dosage curation above), and PP3/BP4 (computational missense-effect evidence) should be gated off as inapplicable rather than independently evaluated, since there is no missense/splice-prediction-shaped question left to ask once a frameshift has been identified. This exact gate was previously only exercised on BRCA1; this puts it on VHL instead.
- **Expected GEPER behaviour (prediction):** PVS1 triggered (Very Strong or Strong depending on NMD/last-exon caveats — not predicted precisely here, that arithmetic depends on transcript structure this session did not re-derive by hand). PP3/BP4 should both report status **not_evaluated** (via `pipeline/acmg_rules.py::_pp3_bp4_inapplicability_reason`, confirmed by reading that gate directly: it detects a frameshift via transcript-CDS-frame protein flags and REF/ALT length, and returns a rationale naming it "a frameshift ... variant with a transcript-CDS-frame-determined null consequence") rather than being evaluated against a fabricated missense-style prediction. GEPER's overall classification should plausibly reach Pathogenic or Likely Pathogenic, **matching** ClinVar's expert-panel Pathogenic call — so this variant, unlike #1, is **not** expected to trigger A3 (or any conflict tier); it exists to exercise the PP3/BP4 gate, not the conflict engine.

---

## Full variant listing (VCF row order = finding number in a GEPER run)

| # | Position | Gene | HGVSp | ClinVar (VCV) | Star rating | ClinVar call | Primary target |
|---|---|---|---|---|---|---|---|
| 1 | 3:37008903 C>T | MLH1 | p.Gly181= | VCV000633499 | 3 (expert panel) | Pathogenic | **A3 Critical** |
| 2 | 3:10142111 G>T | VHL | p.Trp88Cys | VCV000223171 | 3 (expert panel) | Likely pathogenic | **BP1 gate (VHL)** |
| 3 | 3:10149906 C>T | VHL | p.Gln195Ter | VCV000428794 | 3 (expert panel) | Pathogenic | **C7 window cluster** (with #4) |
| 4 | 3:10149933 G>T | VHL | p.Glu204Ter | VCV000526673 | 3 (expert panel) | Uncertain significance | **C7 window cluster** (with #3) |
| 5 | 3:10146594 AA>AAA | VHL | p.Asn141fs | VCV000411979 | 3 (expert panel) | Pathogenic | **A1 PP3/BP4 gate** (non-BRCA1) |

## Target-case coverage

All four target cases from the task are covered by this 5-variant file:

1. **A3 Critical tier** — variant #1 (MLH1 synonymous vs. expert-panel Pathogenic). ✅ Covered.
2. **VHL missense (BP1 gate)** — variant #2. ✅ Covered.
3. **Two variants in a short genomic window, same gene (C7)** — variants #3/#4, 27 bp apart. ✅ Covered.
4. **Frameshift/null in a ClinGen-haploinsufficiency gene, non-BRCA1 (A1 PP3/BP4 gate)** — variant #5 (VHL, dosage score 3). ✅ Covered (optional target).

## What this run should be diffed against

When this file is next run through the real pipeline (Colab, T4 or better), check specifically:

- Finding #1's `conflict_resolution.severity` — predicted **Critical**. If it's anything else (None/Minor/Moderate), read the actual GEPER classification and evidence GEPER used for c.543C>T before concluding A3 is broken — it's equally possible GEPER's splice models correctly caught this and A3 correctly didn't fire.
- Finding #2's BP1 criterion status — predicted **not_triggered**, via the `_bp1_opposing_missense_evidence` path specifically (citing AlphaMissense and/or PM1), not via a gene-level "VHL isn't LOF-driven" reason, which would be factually wrong (VHL's ClinGen score is the same `LOF_ESTABLISHED` state as BRCA1's).
- The multi-finding observation section — predicted one gene-grouping line naming all of #2-#5, and one *separate* genomic-window line naming only #3 and #4.
- Finding #5's PP3 and BP4 criterion status — predicted **not_evaluated**, with a rationale naming the frameshift/null consequence specifically (gated off by `_pp3_bp4_inapplicability_reason`), not evaluated against a real or fabricated missense score.

None of the above has been verified against a real run — these are this session's predictions from reading the rule code, to be checked, not results.
