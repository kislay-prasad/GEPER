# nuclear_test.vcf — ground-truth test dataset for PP4 / PS3 / BS3 / PM1

**Purpose.** Every smoke test to date used MT-RNR2 (mitochondrial), which has
zero HPO entries, zero ClinGen ERepo/MaveDB functional evidence, and no
InterPro domain annotations. PP4, PS3, BS3, and PM1 have therefore never
fired through the real orchestrator end-to-end — only via unit tests and
isolated live-API checks. This VCF exercises all four against real, cited,
GRCh38-correct nuclear-gene variants.

All coordinates were verified live against the actual GRCh38 reference
(Ensembl `sequence/region`) on 2026-07-31 — every REF base below is
confirmed to match the genome, so `sequence_context.py`'s
REF-mismatch warning should not fire.

**Do not run the pipeline against this file locally** (8GB RAM machine,
model loads are off-limits here) — this is fixture + documentation only,
for the Colab run.

---

## Variants

### 1. `17:43106534 C>A` — BRCA1 PS3 positive control
- **HGVS:** `NM_007294.4:c.135-1G>T` (splice acceptor, intron 2)
- **Source:** ClinGen Evidence Repository, `erepo.clinicalgenome.org/evrepo/api/classifications?gene=BRCA1`, queried live 2026-07-31
- **Curation:** ENIGMA BRCA1 and BRCA2 VCEP — **PS3: Met**, overall classification **Pathogenic**, condition "BRCA1-related cancer predisposition"
- **Expected GEPER result:** `PS3` → `triggered`, Strength Strong (ERepo default), via `pipeline/functional_evidence/erepo_provider.py`. This is the exact variant already cited in that provider's own module docstring as the original live-verification case — using it again here confirms the wiring still works, not just the fetch.
- **Note:** this is a splice-acceptor variant, not missense — it is not a PM1 candidate (PM1 requires a mapped protein residue) and will correctly report PM1 `not_evaluated`/`not_triggered` for unrelated reasons (no residue to place in a domain), which is expected and fine.

### 2. `17:43094298 A>C` — BRCA1 BS3 positive control
- **HGVS:** `NM_007294.4:c.1233T>G` (`p.Asp411Glu`)
- **Source:** ClinGen ERepo, same BRCA1 query as above
- **Curation:** ENIGMA BRCA1 and BRCA2 VCEP — **BS3: Met**, overall classification **Benign**
- **Expected GEPER result:** `BS3` → `triggered`, Strength Strong.
- Residue 411 sits between BRCA1's RING domain (24–65) and BRCT repeats (1642–1855, per live InterPro query on UniProt P38398) — a real, unstructured linker region, consistent with a benign call.

### 3. `17:7674221 G>A` — TP53 PS3 positive control
- **HGVS:** `NM_000546.6:c.742C>T` (`p.Arg248Trp`)
- **Source:** ClinGen ERepo, `gene=TP53`, queried live 2026-07-31 (note: this endpoint returned an HTTP 500 with a truncated-but-valid-looking JSON body on the first attempt and a clean HTTP 200 with 25 entries on a retry a few seconds later — a real, observed transient-flakiness pattern for this specific gene's larger payload, not a request-shape bug; GEPER's own provider already retries with backoff, `CONFIG.functional_evidence.MAX_RETRIES`, which should absorb this)
- **Curation:** TP53 VCEP — **PS3: Met**, overall classification **Pathogenic**
- R248W is one of the best-known TP53 DNA-binding-domain "hotspot" mutations in the literature (direct DNA-contact residue).
- **Expected GEPER result:** `PS3` → `triggered`, Strength Strong.

### 4. `17:7676152 C>T` — TP53 BS3 positive control
- **HGVS:** `NM_000546.6:c.217G>A` (`p.Val73Met`)
- **Source:** ClinGen ERepo, same TP53 query
- **Curation:** TP53 VCEP — **BS3: Met**, overall classification **Benign**
- **Expected GEPER result:** `BS3` → `triggered`, Strength Strong.

### 5. `20:4699525 C>T` — PRNP PM1 candidate (real biology positive; GEPER-mechanism likely negative — see caveat)
- **HGVS:** `NM_000311.5:c.305C>T` (`p.Pro102Leu`)
- **Source:** ClinVar Variation ID 3690, `esummary` queried live 2026-07-31 — **Pathogenic**, review status "criteria provided, multiple submitters, no conflicts"; associated with Gerstmann-Sträussler-Scheinker syndrome, inherited Creutzfeldt-Jakob disease, Huntington disease-like 1 (OMIM 137440/123400/603218)
- **Domain evidence:** live InterPro query on UniProt P04156 (PRNP) confirms residue 102 falls inside `IPR036924` ("Prion/Doppel beta-ribbon domain superfamily", residues 90–231, InterPro type `homologous_superfamily` — **not** filtered out by GEPER's own `_NON_DOMAIN_ENTRY_TYPES = {"family"}` exclusion in `pipeline/interpro/lookup.py`). This is real, true, canonical-numbering domain overlap — P102L is THE textbook GSS mutation and is inside a genuine structural domain, not a `family`-type whole-protein span.
- **Why PRNP and not BRCA1/TP53 for PM1** — see "Major finding" below.

---

## PP4 — recommended `--hpo-terms`

PP4's gene-side reference set (`hpo_result['distinct_disease_ids']`) comes
from GEPER's own HPO integration (`pipeline/hpo/`), not from anything this
VCF or `--hpo-terms` controls. I pulled the real, current HPO
`genes_to_phenotype.txt` release (20.7 MB, streamed and filtered, not
loaded as a whole) and also cross-checked live against
`ontology.jax.org/api` — both agree:

| Gene  | Distinct HPO-curated disease IDs | `PP4_MAX_DISTINCT_DISEASES` default |
|-------|-----------------------------------|--------------------------------------|
| BRCA1 | 8  (HBOC + Fanconi anemia complementation group S, a genuinely different recessive phenotype) | 3 |
| TP53  | 20 (Li-Fraumeni + many sporadic-tumor associations) | 3 |
| PRNP  | 11 (CJD, GSS, FFI, Huntington-disease-like 1, kuru susceptibility — genuinely distinct prion phenotypes) | 3 |

**None of these three genes can reach PP4 `triggered` with the default
threshold**, and that is the biologically correct outcome, not a bug: PP4
specifically requires "a disease with a **single** genetic etiology," and
all three chosen genes are real, well-documented pleiotropic genes. This
mirrors the FBN1 finding from the original HPO integration session (17
disease entries, only a disease-filtered subset triggers) — no CLI
mechanism exists (nor should one, arguably) for the user to filter the
*gene's own* reference disease set via `--hpo-terms`, which only controls
the *patient's* observed terms.

**Recommended terms** (chosen for 100% overlap against each gene's real
curated phenotype set, so `specific_enough` passes cleanly and the
`not_triggered` rationale shows the disease-count reason in isolation,
not conflated with a poor-overlap reason):

```bash
# BRCA1 record (breast/ovarian carcinoma — real curated BRCA1 phenotypes)
--hpo-terms HP:0003002,HP:0025318

# TP53 record (Li-Fraumeni triad — real curated TP53 phenotypes)
--hpo-terms HP:0003002,HP:0006744,HP:0030448

# PRNP record (classic CJD/GSS triad — real curated PRNP phenotypes)
--hpo-terms HP:0000726,HP:0001251,HP:0100785
```

Expected result for all three: PP4 `not_triggered`, with rationale text
reading approximately *"...phenotype overlap (100%) is [above] the 50%
threshold; {gene} has N distinct HPO-curated disease entries (above the 3
threshold for 'single etiology')"* — i.e. evidence was genuinely evaluated
(not `not_evaluated`), and only the multi-etiology gate is what stops it
from `triggered`.

**If you specifically want to see PP4 reach `triggered`** (e.g. to confirm
the triggered code path itself, accepting this is no longer testing
default/realistic behavior), override the threshold for the run:
`GEPER_HPO_PP4_MAX_DISTINCT_DISEASES=8` (BRCA1) / `=20` (TP53) / `=11`
(PRNP), combined with the same `--hpo-terms` above.

---

## Major finding: PM1's protein-position estimate is unreliable for essentially all realistic variants

This surfaced while trying to pick a BRCA1/TP53 missense variant that would
double as a PM1 domain-overlap positive control (there are excellent
candidates by true biology — see "Rejected candidates" below) — I verified
it empirically against GEPER's actual code (three lightweight, pure-Python
modules plus real Ensembl sequence fetches; no model weights loaded) rather
than assert it from reading the source.

**Mechanism** (`pipeline/sequence_context.py` → `pipeline/rna_generator.py`
→ `pipeline/protein_translator.py` → `orchestrator._estimate_protein_position`):
1. `SequenceContextGenerator.build_context` fetches a flat **±500bp genomic
   window** (`CONFIG.routing.DEFAULT_FLANK_SIZE`) centered on the variant —
   plain Ensembl `sequence/region`, **the literal plus-strand genome text,
   never reverse-complemented**.
2. `RNAGenerator` just does DNA T→U (no splicing — this is explicitly
   documented in the module's own docstring: *"GEPER does not perform full
   splicing simulation... it transcribes the coding-strand window
   as-is"*).
3. `ProteinTranslator._translate_orf` calls `rna_sequence.find("AUG")` —
   the **first** AUG substring anywhere in that 1000bp window, translated
   until a stop codon.
4. `_estimate_protein_position` reports the first differing ref/alt residue
   **index within that local peptide** — not a transcript-verified
   canonical position. This part is already disclosed in the code's own
   docstring as an estimate.

**What I found empirically, not just inferred:** for every real variant I
tested — BRCA1 `p.His41Asn` (RING domain, residue 41), BRCA1
`p.Cys1697Tyr` (BRCT domain, residue 1697), LDLR `p.Asp227Glu` (a real,
expert-panel-reviewed ClinVar Pathogenic FH variant, GRCh38 `19:11105587
C>G`, inside the real LA5 domain 196–233 per InterPro), and PRNP
`p.Pro102Leu` (this VCF's PM1 record) — the locally-translated "protein"
did **not** match the real UniProt sequence at all (verified by substring
search against the live UniProt FASTA). In every case either:
- an earlier, spurious "AUG" substring in the intronic/intergenic sequence
  upstream of the true CDS start codon hijacked translation (BRCA1, PRNP —
  both confirmed via direct simulation), or
- the window crossed an intron entirely uncorrected, since GEPER's window
  is flat genomic sequence with no splicing (LDLR — window found *an*
  in-frame AUG-to-stop ORF with no premature stop, but it does not occur
  anywhere in the real 860-aa LDLR protein).
- BRCA1 and TP53 carry an *additional*, independent problem on top of
  this: both are **minus-strand genes** (confirmed live via Ensembl
  `lookup/symbol`, `strand: -1`), and nothing in this translation path
  reverse-complements — so even a window that stayed within one exon would
  still be translating the wrong strand.

**Practical consequence for this dataset:** `_estimate_protein_position`
returned `None` for both tested BRCA1 candidates and for this VCF's PRNP
P102L record (simulated locally, not run through the full orchestrator).
When that happens, `InterProLookup.query_variant`'s `protein_position is
not None` check is false, so it takes the `else` branch and sets
`affected_domains = []` unconditionally — **not** `not_evaluated`. Since
`uniprot_result`/`interpro_result.found` is independently `True` (PRNP has
real InterPro domain data regardless of position), `_pm1` will very likely
report **`not_triggered`** ("does not overlap any annotated... domain
region") for the PRNP record above — a **false negative** relative to true
biology (residue 102 genuinely is inside `IPR036924`), not a data gap and
not `not_evaluated`.

I'm including the PRNP record anyway because it's still the scientifically
correct ground-truth case (true canonical-numbering domain overlap, cited,
real pathogenic variant) — worth having in the dataset specifically
*because* comparing GEPER's actual Colab-run output against this expected
answer is exactly how this limitation gets tracked down and eventually
fixed, rather than staying invisible. If the Colab run instead shows PM1
`triggered` for this record, that would mean the local ORF's first AUG
happened to coincide with the true start codon for this specific window —
worth noting either way.

This is a real, previously-undocumented limitation in the residue-position
pipeline (affects PM1, and — since it's the same `protein_result` — any
other stage keyed off `_estimate_protein_position`, e.g. AlphaFold pLDDT
lookup), not something introduced by or specific to this test dataset. I
have not attempted to fix it — out of scope for "build the fixture,
don't run the pipeline."

---

## Rejected candidates

- **CFTR** — excluded per prior PS3/BS3 feasibility research: ClinGen
  ERepo and MaveDB both return zero coverage for CFTR (confirmed again
  not to have changed as of this session's context). Not usable as a PS3/BS3
  positive control.
- **BRCA1 `p.Cys64Tyr` / `p.His41Asn` / `p.His41Leu`** (`c.191G>A`,
  `c.121C>A`, `c.122A>T`) — all three are real ERepo **PS3: Met**
  (Pathogenic/Likely Pathogenic) missense calls, and residue 41/64 is
  genuinely inside BRCA1's RING domain (`PF00097`/`IPR001841`, 24–64/65 per
  live InterPro) — by true biology, excellent combined PS3+PM1 candidates.
  Rejected as the PM1 pick specifically because of the discovered
  minus-strand + no-splicing limitation above, empirically confirmed to
  produce a garbage local translation for this exact gene. Still valid
  PS3 evidence on their own merits; not included only to avoid the dataset
  implying a PM1 result I can't actually back with a verified simulation.
- **BRCA1 `p.Cys1697Tyr`** (`c.5090G>A`) — real ERepo PS3: Met, Likely
  Pathogenic, residue 1697 genuinely inside the first BRCT repeat
  (`PF00533`, 1645–1723). Same rejection reason as above (empirically
  confirmed garbage translation this far into a multi-exon CDS).
- **LDLR `p.Asp227Glu`** (`c.681C>G`, aliases "D206E"/"FH Afrikaner-1"/"FH
  Maine", GRCh38 `19:11105587 C>G`) — real ClinVar Variation ID 3690,
  expert-panel-reviewed **Pathogenic**, familial hypercholesterolemia
  (OMIM:143890), genuinely inside LDLR's 5th LDL-A repeat (`PF00057`,
  196–233 per InterPro). This is a **plus-strand** gene (unlike
  BRCA1/TP53), so I used it specifically to isolate whether the
  minus-strand issue was the whole story — it wasn't: the local window
  still produced a 175-residue peptide that does not appear anywhere in
  the real 860-aa UniProt P01130 sequence, because the window crosses
  intron 1 (LDLR's CDS-containing first exon is only ~67nt; the variant is
  ~660nt into the CDS, in a different exon entirely). Kept out of the
  final VCF only because it isn't BRCA1/TP53/CFTR and doesn't add a new
  finding beyond what PRNP already demonstrates; documented here since it
  was real, useful, and disproves "it's just a strand problem."
- **PRNP is single-exon** (confirmed via Ensembl `lookup/id` with
  `expand=1;utr=1`: the entire 253-aa ORF sits inside one 2378bp exon) —
  chosen over LDLR *because* it eliminates the splicing variable entirely,
  isolating the remaining "first spurious AUG upstream of true start"
  failure mode as the sole cause for the `None` result documented above.

---

## What was verified live vs. what is a documented estimate

Verified live against real external services during this session
(2026-07-31), all lightweight JSON/text calls, no model weights loaded:
ClinGen ERepo (`erepo.clinicalgenome.org`), NCBI ClinVar E-utilities
(`eutils.ncbi.nlm.nih.gov`), Ensembl REST (`rest.ensembl.org` — sequence,
gene/transcript/exon lookup), EBI InterPro REST
(`www.ebi.ac.uk/interpro/api`), UniProt REST (`rest.uniprot.org`), HPO's
official `genes_to_phenotype.txt` release, and `ontology.jax.org/api`.

Not run: the actual GEPER orchestrator/pipeline (would load SpliceFormer/
SpliceBERT/ESM-2/RNA-FM model weights — explicitly out of scope on this
machine). Every "expected GEPER result" above is a citation-backed
prediction to check the Colab output against, not a confirmed pipeline
output — except where explicitly marked "simulated locally" (the
protein-translation limitation section), which used only the three
pure-Python modules directly (`sequence_context.py`, `rna_generator.py`,
`protein_translator.py`) plus real Ensembl sequence fetches, never the
full orchestrator or any model.
