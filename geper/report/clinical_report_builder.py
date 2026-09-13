"""
Phase 5 -- Clinical Report Upgrade.

Builds one structured, clinician-oriented "clinical report" per variant
from the canonical `InterpretationResult` dict (Phase 2), including the
confidence (Phase 3) and prioritization (Phase 4) data already attached
to it. Every section below reads a field `InterpretationResult` already
computed -- nothing here re-derives evidence from raw provider dicts a
second time (the one exception, `raw_evidence`, is itself a field
`InterpretationResult` already carries -- see interpretation_result.py),
and nothing is invented that GEPER doesn't actually have.

Single source of truth: both `report/json_builder.py` (adds this dict
as an additive `clinical_report` key) and `report/report_generator.py`
(renders this same dict as Markdown) call `build_clinical_report()` --
so the two output formats can never drift out of sync, per the Phase 5
requirement to "reuse InterpretationResult instead of rebuilding logic
repeatedly."
"""

import json
from typing import Any, Dict, List, Optional, Tuple, Union

from config import CONFIG
from pipeline.acmg_rules import NotEvaluatedReason, not_evaluated_breakdown
from pipeline.stage_schemas import StageStatus as _StageStatus
from pipeline.vcf_parser import VAF_NO_SAMPLE_COLUMNS_REASON
from utils.logger import get_logger
from utils.service_health import HEALTH, ServiceStatus
from utils.timezone_utils import format_ist_from_iso

logger = get_logger(__name__)

# Which evidence-combining system GEPER actually applies to turn
# triggered ACMG/AMP criteria into a final classification -- stated
# explicitly and identically everywhere the classification itself
# appears (report header, per-finding line, methodology/limitations),
# so a reviewing geneticist never has to guess. `pipeline/acmg_rules.py
# ::ACMGRuleEngine._combine` uses Tavtigian et al. 2018's Bayesian-
# calibrated point system (point weights 8/4/2/1 for very_strong/
# strong/moderate/supporting, thresholded on net pathogenic-minus-
# benign points) -- NOT Richards et al. 2015's original categorical
# combining table, which the unqualified phrase "ACMG/AMP
# Classification" would otherwise imply. The two systems do not always
# agree: PVS1 (very_strong) plus one Moderate criterion alone reaches
# Pathogenic under Tavtigian's net-points thresholds (8+2=10) but only
# Likely Pathogenic under Richards' categorical table (which requires
# a second Moderate, or a Moderate plus a Supporting, alongside a
# Very Strong to reach Pathogenic). See `_combine`'s own docstring
# comment for the full citation and reasoning.
ACMG_METHODOLOGY_STATEMENT = (
    "ACMG/AMP criteria are combined into a final classification using the Bayesian-calibrated point "
    'system of Tavtigian SV et al., "Modeling the ACMG/AMP variant classification guidelines as a '
    'Bayesian classification framework", Genet Med 2018 (PMID 29300386) -- not the original Richards '
    "et al. 2015 categorical combining table (PMID 25741868). The two can disagree on some evidence "
    "combinations (e.g. one top-tier-strength criterion such as PVS1 plus one Moderate criterion "
    "alone reaches Pathogenic under this point system's thresholds, but only Likely Pathogenic under "
    "the 2015 categorical table). GEPER also does not defer to another laboratory's own ClinVar "
    "classification as independent evidence: PP5 is never evaluated, and BP6, though computed and "
    "shown for transparency, is excluded from this point system's tally -- following ClinGen's SVI "
    "Working Group recommendation (Biesecker & Harrison 2018) that doing so is circular with respect "
    "to an independent ACMG/AMP evaluation."
)

# Display-only label for the net-points band a classification landed
# in -- report review round 10, so a reviewer can see why a variant got
# its classification without recomputing `ACMGRuleEngine._combine`'s
# thresholds by hand. Keyed off the classification string itself
# (already the authoritative output of `_combine`'s own threshold
# comparisons), never a second, independent re-implementation of those
# thresholds -- that would risk drifting from `_combine`'s real
# `>= 10` / `>= 6` / `<= -1` / `<= -7` comparisons (`pipeline/
# acmg_rules.py`) the way the discarded net ever could have. "Benign"
# has no band label since BA1's stand-alone short-circuit never runs
# the point tally at all (see `CombineResult`'s docstring) -- there is
# no band to name for that classification.
_ACMG_NET_POINTS_BAND_LABEL = {
    "Pathogenic": "≥10",  # >=10
    "Likely Pathogenic": "6–9",  # 6-9 (en dash)
    "Uncertain Significance": "0–5",  # 0-5
    "Likely Benign": "≤−1",  # <=-1
}


def acmg_net_points_band_label(classification: Optional[str]) -> Optional[str]:
    """The Tavtigian net-points band (e.g. "≥10") for `classification`,
    or `None` when there is no band to show (no classification yet, or
    Benign via BA1's stand-alone short-circuit -- see this module's
    `_ACMG_NET_POINTS_BAND_LABEL` comment)."""
    return _ACMG_NET_POINTS_BAND_LABEL.get(classification or "")


# Single source of truth for the clarifying caption shown next to every
# place `ConfidenceEngine.score()`'s output is displayed (full PDF,
# short PDF, Markdown) -- C2, report review round 2: this score
# measures evidence *completeness*, not classification *certainty*
# ("Pathogenic / Low (25%)" previously read as doubt about the call).
# Renamed from "Confidence" to "Evidence Completeness" everywhere the
# label itself appears; this exact sentence is the shared substring
# every renderer includes verbatim, so the caption can never drift out
# of sync the way independently-worded footnotes would.
EVIDENCE_COMPLETENESS_CAPTION = (
    "Evidence Completeness reflects how much evidence GEPER could gather for this variant, not how "
    "certain the classification is."
)

# What GEPER claims about itself, stated once for every renderer.
#
# GEPER is a variant prioritisation system that assists qualified
# clinicians and pathologists. It produces a draft classification
# requiring qualified human review and final sign-off before any
# clinical use, and does not independently provide final clinical
# interpretation. That is the project's settled position as of EJ-01
# (ratified 2026-08-22), asserted consistently elsewhere --
# `README.md`'s "Clinical disclaimer" section,
# `pipeline/explainability_engine.py`, `kim_pipeline/api/main.py`'s
# public API description, and `kim_pipeline/pipeline/reporting/
# stage.py` / `pdf_report.py`'s separately-worded copy.
#
# Until 2026-08-22 this comment and the constant below stated a
# narrower position instead -- "GEPER is a research pipeline, not a
# clinical diagnostic tool" -- partly on the strength of "the CC
# BY-NC-SA 4.0 non-commercial-research licence the project ships
# under." That licence claim was itself wrong and has been corrected,
# not just reworded: GEPER has no licence of its own anywhere in this
# repository (no LICENSE/LICENSE.md/COPYING file and no licence field
# in any build config, except one vendored third-party file under
# `geper/hyena-dna/`). The CC BY-NC-SA 4.0 references that old comment
# leaned on are inbound, on specific data sources and model weights
# (e.g. AlphaMissense, `LICENSE_AUDIT.md`'s model weights, PharmVar
# per `GROUND_TRUTH_DATASET_AUDIT.md`) -- not a licence GEPER itself
# ships under. See `VALIDATION_BASELINE_2026-08-21.md:263` for that
# earlier verdict, annotated superseded rather than rewritten: it was
# correct against the research-vs-clinical-use question it was
# actually answering at the time.
#
# Until 2026-08-21 the clinical PDF also carried a second, contradicting
# statement -- US CLIA/LDT template wording asserting the report was
# "intended for clinical use" -- so a single rendered PDF asserted both
# claims on different pages, and the Markdown report carried a third,
# separately-worded copy of this one. All three were replaced by this
# constant that day. Its positioning sentence was then updated again the
# following day per EJ-01, above; the mandatory-review clause salvaged
# from the retired CLIA/LDT text is now folded into that same sentence
# rather than kept as a separate clause.
#
# PLAIN PROSE, DELIBERATELY: no Markdown, no PDF markup, no leading
# label. Each renderer applies its own wrapper (`report_generator.py`
# a blockquote, `report/summary.py` a styled Paragraph). The previous
# Markdown copy existed because the PDF's version had its formatting
# baked in and therefore could not be imported -- the fork was the only
# option available, not carelessness. Keep this string presentation-free
# or the next renderer with different markup needs will fork it again.
RESEARCH_USE_DISCLAIMER = (
    "This report is generated by an automated research pipeline combining public database "
    "lookups, curated clinical resources, and machine-learning predictors. Bij AI is a variant "
    "prioritisation system that assists qualified clinicians and pathologists. It produces a "
    "draft classification requiring qualified human review and final sign-off before any "
    "clinical use. It does not independently provide final clinical interpretation. It is not "
    "a substitute for professional clinical genetic interpretation, diagnosis, or advice."
)

ISO_RESEARCH_ELEMENT = (
    "Bij AI is undertaken as part of a research or development programme for which no specific "
    "claims on measurement performance are available."
)
# This paragraph uses ISO 15189:2022 7.4.1.6 i)'s exact wording
# ("research or development programme" + "no specific claims on measurement performance are
# available") deliberately, not paraphrased. The standard recognises this category; inventing
# our own phrasing would require an assessor to evaluate our prose rather than recognise a
# defined slot. Do not paraphrase this text or harmonise it with other disclaimers -- the ISO
# language is the point.

# The same positioning claim as `RESEARCH_USE_DISCLAIMER` above, in the
# short form a document can lead with. NOT an independent statement --
# if the two ever disagree, this one is wrong: the disclaimer is the
# canonical text and this is its headline.
#
# It exists because prominence is not a font size. The Markdown report
# puts its status banner on line 3, above everything, so a reader learns
# what the document IS before they read what it FOUND. The PDF carried
# the same claim only in 6.5pt footer text below a grey rule, sharing
# the smallest type on the page with the AI-disclosure line -- present,
# and unread. `report/summary.py` renders this at the top of page 1,
# above the findings table, for that reason.
#
# DELIBERATELY SAYS NOTHING ABOUT REVIEW STATUS. Draft/reviewed/
# overridden is a per-run fact and belongs to the footer's own status
# logic (`summary.py::_icmr_ai_disclosure_footer_text`). This is a
# statement about what GEPER is, which is true of every run including
# a signed-off one.
DOCUMENT_POSITIONING_STATEMENT = (
    "Bij AI is a variant prioritisation system that assists qualified clinicians and pathologists. "
    "This document reports a draft classification requiring qualified human review and final sign-off "
    "before any clinical use. It does not independently provide final clinical interpretation."
)

# Stable, well-known public-resource references for whichever sources
# actually contributed evidence to this variant (via `evidence_sources`,
# already computed by Phase 2 -- not re-derived here). These are fixed
# citations to the resource itself, not fabricated per-variant claims.
#
# "HPO" is included here (not as a separate unconditional citation the
# way Orphanet is below) because PP4 genuinely tags `evidence_sources
# =["HPO"]` whenever it evaluates at all (`pipeline/acmg_rules.py
# ::ACMGRuleEngine._pp4`, both the "triggered" and "not_triggered"
# branches) -- so this dict's existing conditional-on-evidence_sources
# mechanism already does the right thing for HPO with no other change
# needed. Citation text follows HPO's own citation guidance
# (obophenotype.github.io/human-phenotype-ontology/community/cite/,
# confirmed live 2026-08-08: "we recommend citing the most recent
# article" -- currently Gargano et al. 2024, NAR).
_REFERENCES = {
    "ClinVar": "ClinVar -- https://www.ncbi.nlm.nih.gov/clinvar/",
    "dbSNP": "dbSNP -- https://www.ncbi.nlm.nih.gov/snp/",
    "gnomAD": "gnomAD -- https://gnomad.broadinstitute.org/",
    "ClinGen": "ClinGen -- https://clinicalgenome.org/",
    "UniProt": "UniProt -- https://www.uniprot.org/",
    "InterPro": "InterPro/Pfam -- https://www.ebi.ac.uk/interpro/",
    # Brought up to `_ALPHAFOLD_REFERENCE`'s form below (citation +
    # licensor + licence + link) -- AlphaMissense and AlphaFold DB are
    # the same licensor under the same licence (CC-BY-4.0, primary-
    # source-confirmed in LICENSE_AUDIT.md's AlphaMissense entry), so an
    # entry that carries neither is indefensible next to one that
    # carries both. This is NOT a resolution of AM-04 (whether CC-BY-4.0
    # attribution is legally owed for a single extracted row) -- that
    # stays human-gated; this change is made purely because the
    # inconsistency between the two entries, on its own terms, has no
    # defense.
    #
    # The copyright notice below is VERIFIED, not copied by analogy from
    # `_ALPHAFOLD_REFERENCE`'s form: the GCS bucket's own README.pdf, at
    # the exact URL config.py:342-349 downloads from (`timeCreated`
    # 2024-03-13, the day of DeepMind's public relicense -- same source
    # models/alphamissense.py's module docstring and
    # config.py::AlphaMissenseConfig's docstring now cite), states
    # verbatim: "Copyright (2023) DeepMind Technologies Limited. All
    # materials are licensed under the Creative Commons Attribution 4.0
    # International License (CC-BY)." Confirming CC-BY-4.0 governs, and
    # now this copyright notice, still does NOT resolve whether CC-BY-4.0's
    # attribution condition is actually triggered by extracting one row
    # from a ~71M-row catalogue into a report -- that stays the separate,
    # human-gated AM-04 question.
    "AlphaMissense": (
        "AlphaMissense -- Cheng et al. 2023, Science. Predictions data Copyright (2023) DeepMind "
        "Technologies Limited, available under CC-BY-4.0 "
        "(https://storage.googleapis.com/dm_alphamissense/README.pdf, "
        "https://creativecommons.org/licenses/by/4.0/)"
    ),
    "MMSplice": "MMSplice -- Cheng et al. 2019, Genome Biology",
    "BLAST": "NCBI BLAST -- https://blast.ncbi.nlm.nih.gov/",
    "HPO": "Human Phenotype Ontology (HPO) -- Gargano et al. 2024, Nucleic Acids Research",
    "protein_translator": "GEPER internal reference/alternate protein translation (standard genetic code).",
}

# Orphanet/Orphadata (CC-BY-4.0) is a genuine exception to the
# conditional-on-evidence_sources mechanism above: GEPER's pipeline
# fetches and caches Orphanet's gene-disorder association file every
# run (`pipeline/orphanet/bootstrap.py`, live-verified working in
# `DATA_PROVENANCE.md`), but as of 2026-08-08 that data is not wired
# into any ACMG criterion or otherwise surfaced in the report body --
# it never becomes an `evidence_sources` entry, so it could never earn
# a place in `_REFERENCES` above no matter how many reports were
# generated. Since CC-BY-4.0 requires attribution wherever GEPER's
# overall product incorporates the dataset -- not only in the specific
# runs where a criterion happened to consult it -- this is cited
# unconditionally instead, in every report, rather than silently never
# citing a source the pipeline genuinely integrates. (See
# `DATA_SOURCE_LICENSE_AUDIT.md`'s HPO/Orphanet entries for the
# original finding and this decision.) Citation text follows Orphanet's
# own recommended format for its Orphadata products
# (orphadata.com/legal-notice/, confirmed live 2026-08-08): "Orphadata
# Products: Data and services from Orphanet. (c) INSERM 1999."
_ORPHANET_REFERENCE = (
    "Orphanet/Orphadata -- https://www.orphadata.com/ "
    "(Orphadata Products: Data and services from Orphanet, (c) INSERM 1999)"
)

# AlphaFold DB (CC-BY-4.0) is integrated into GEPER's pipeline
# (`pipeline/alphafold/` — summary API queries, per-residue pLDDT confidence,
# structure-file downloads) and surfaced in variant reports (confidence bands,
# model version, 3D-viewer URLs when available). Like Orphanet, AlphaFold
# doesn't contribute to ACMG criterion evidence, so it never appears in the
# `evidence_sources` field that gates conditional citation in `_REFERENCES`
# above -- it would be present-but-never-cited without unconditional
# attribution. CC-BY-4.0 requires citation wherever the licensed material
# is presented to end users as part of the product, not only in runs where
# it happens to be consulted. Citation text follows AlphaFold's own
# recommended attribution (https://alphafold.ebi.ac.uk/assets/License-Disclaimer.pdf,
# verified live 2026-08-22): includes the methods paper citation, identifies
# the source as DeepMind Technologies Limited / AlphaFold DB, and links the
# CC-BY-4.0 license.
_ALPHAFOLD_REFERENCE = (
    "AlphaFold Protein Structure Database -- Jumper et al. 2021, Nature. "
    "Structure data (c) 2021 DeepMind Technologies Limited, available under CC-BY-4.0 "
    "(https://alphafold.ebi.ac.uk/, https://creativecommons.org/licenses/by/4.0/)"
)

_TOOL_LIMITATIONS = (
    # Same sentence as the closing disclaimer block every renderer
    # prints, by construction rather than by coincidence -- this used to
    # be an independently-worded near-duplicate, which is exactly how it
    # and the PDF's own closing text drifted into contradicting each
    # other. Appearing both per finding and once at the end is
    # placement, not duplication.
    # ISO statement split to separate element per design ruling 2026-09-03.
    RESEARCH_USE_DISCLAIMER,
    ISO_RESEARCH_ELEMENT,
    ACMG_METHODOLOGY_STATEMENT,
    "Protein-residue positions used for domain (InterPro/Pfam) and structural (AlphaFold DB) "
    "overlap checks are transcript-verified (computed from the variant's real, strand- and "
    "splice-aware coding-sequence mapping, not a flat unspliced translation window) but only "
    "when the resolved transcript is that gene's MANE Select or Ensembl-canonical isoform -- "
    "InterPro/UniProt/AlphaFold DB coordinates are only meaningful against the canonical "
    "isoform. When the resolved transcript is a different one, or no transcript could be "
    "resolved at all, no residue position is reported and the corresponding domain/structure "
    "criteria are marked not evaluated rather than shown against a mismatched coordinate.",
    "PS3/BS3 functional-evidence lookups query the ClinGen Evidence Repository (ERepo) first; "
    "MaveDB is consulted only as a fallback, and only for a variant ERepo has no record for. "
    "The two sources are never queried together and never cross-checked against each other for "
    "the same variant -- a PS3/BS3 call always comes from a single source, not a combination.",
)


def candidate_interpretation_of(variant_result):
    """
    The per-variant candidate-interpretation object, accepting either
    key during the `clinical_report` deprecation window.

    READ THROUGH THIS, NEVER `.get("candidate_interpretation")` DIRECTLY,
    until the alias is removed (target 2026-11-22). The two keys are the
    same object when `json_builder` builds the document -- but
    `review/signoff.py` reloads `geper_results.json` from disk, and a
    JSON round-trip turns one shared object into two independent copies.
    A writer that mutates one then leaves the other stale, and every
    reader in this codebase uses `.get(...) or {}`, so the stale copy is
    read without complaint rather than raising.

    Preferring the new key and falling back to the old makes a reader
    correct whichever key the last writer touched. It does not, on its
    own, fix the writer side -- an external consumer reading
    `clinical_report` straight out of the JSON would still see a stale
    copy after an override. That gap was real and is now closed at the
    writer: `review/signoff.py` re-points BOTH keys at the same mutated
    object before it dumps. Reader and writer are what make the alias
    safe together; neither does it alone.

    Resolution is by KEY PRESENCE, not truthiness. An empty
    `candidate_interpretation` resolves to that empty object and does
    NOT fall through to the deprecated key -- "the interpretation is
    empty" and "there is no interpretation here" are different facts and
    only the second should reach the old key.
    """
    if not isinstance(variant_result, dict):
        return None
    # PRESENCE, not truthiness. `get(new) or get(old)` fell through to
    # the deprecated key whenever the new one held an empty object, so a
    # document carrying an empty candidate interpretation beside a stale
    # `clinical_report` resolved to the stale copy -- rendering old
    # clinical content as current. Testing membership keeps "the
    # interpretation is empty" distinct from "there is no interpretation
    # under this key", which is the distinction the whole alias exists to
    # protect.
    if "candidate_interpretation" in variant_result:
        return variant_result["candidate_interpretation"]
    return variant_result.get("clinical_report")


def build_clinical_report(
    interpretation_result: Optional[Dict[str, Any]],
    variant_dict: Dict[str, Any] = None,
    raw_evidence: Optional[Dict[str, Any]] = None,
    indigenomes_result: Optional[Dict[str, Any]] = None,
    thousand_genomes_sas_result: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Returns the 17-section clinical report dict, or `None` if
    `interpretation_result` is missing/errored (e.g. the aggregation
    engine failed for this variant) -- callers must not fabricate a
    report when the underlying data isn't there.

    `raw_evidence`: the raw per-stage provider dicts (uniprot, interpro,
    alphafold, gnomad, dbsnp, clinvar, clingen, blast, ...) this
    variant's evidence-dependent sections (protein/structural/
    population/clinical knowledge, sequence context) are built from.

    Pass this explicitly whenever the caller has the live provider
    dicts in hand (as `report/json_builder.py::build_variant_result`
    now does -- see that module) rather than relying on
    `interpretation_result["raw_evidence"]`: `InterpretationResult
    .to_dict()` (`pipeline/interpretation_result.py`) deliberately
    OMITS `raw_evidence` from its serialized output (to avoid
    duplicating data already emitted under the top-level `uniprot`/
    `interpro`/`gnomad`/etc. keys), so `interpretation_result` here --
    which is that *serialized* dict, not the live dataclass instance --
    never actually carries it. Before this parameter existed, every
    caller silently fell through to `ir.get("raw_evidence") or {}`,
    which was *always* an empty dict, so every one of this function's
    raw-evidence-derived sections (protein/structural/population/
    clinical knowledge, sequence context) unconditionally reported
    "not found"/"no annotation available" regardless of what the
    corresponding stage had actually returned -- a real bug (found via
    a live Colab run where the report claimed "UniProt: no entry
    resolved" in this section while the same report's own Annotation
    Detail trail, built from the actual stage output, showed a
    resolved accession two sections later). Kept optional and
    falling back to the old (broken-if-empty) behavior only so this
    isn't a breaking API change for any other future caller.

    `indigenomes_result`: VESTIGIAL as of 2026-08-08 -- IndiGenomes was
    retired from GEPER's active query path entirely (its own terms
    restrict commercial use, which GEPER has not licensed; see
    `config.py::IndiGenomesConfig`'s docstring and
    `DATA_SOURCE_LICENSE_AUDIT.md`). Still accepted here, purely so
    every existing caller (`report/json_builder.py`, this module's own
    tests) keeps working without a signature-breaking change, but it is
    no longer read or passed to `_indian_population_frequency` below --
    there is no longer an "IndiGenomes vs. fallback" branch to feed. If
    IndiGenomes is ever reinstated, restore the argument's use here
    alongside `pipeline/orchestrator.py::_run_indigenomes_stage`.

    `thousand_genomes_sas_result`: the raw 1000 Genomes SAS provider
    dict (see `annotation/thousand_genomes_sas.py
    ::ThousandGenomesSASLookup.query_variant`) -- the SOLE source for
    the `indian_population_frequency` section below as of 2026-08-08
    (previously an IndiGenomes fallback, shown only when IndiGenomes
    was confirmed offline; now shown unconditionally). Passed as its
    own explicit parameter, NOT folded into `raw_evidence` -- that
    dict's validated shape (`pipeline/stage_schemas.py::RawEvidenceBundle`)
    has a fixed 11 fields and this is not one of them (an India-
    deployment-only source, unlike gnomAD/ClinVar/etc.); adding an
    unlisted key there would raise a `TypeError` at the schema boundary
    rather than degrade gracefully.
    """
    if not interpretation_result or "error" in interpretation_result:
        return None
    ir = interpretation_result
    raw = raw_evidence if raw_evidence is not None else (ir.get("raw_evidence") or {})

    return {
        "executive_summary": _executive_summary(ir, variant_dict),
        "acmg_classification": _acmg_section(ir),
        # Human ruling, 2026-09-09/10: carried ALONGSIDE
        # acmg_classification above, never replacing it. See
        # `pipeline/interpretation_outcome.py`.
        "interpretation_outcome": ir.get("interpretation_outcome"),
        # Round 2 (RULED, 2026-09-09/10): always a list, even when empty
        # -- see `pipeline/interpretation_outcome.py::determine_review_required_reasons`.
        "review_required_reasons": ir.get("review_required_reasons") or [],
        "confidence": _confidence_section(ir),
        "priority": _priority_section(ir),
        "supporting_evidence": ir.get("supporting_evidence", []),
        "conflicting_evidence": _conflicting_evidence(ir),
        "conflict_resolution": _conflict_resolution_section(ir),
        "explainability": ir.get("explainability"),
        "ai_consensus": _ai_consensus_section(ir),
        "protein_knowledge": _protein_knowledge(raw),
        "structural_knowledge": _structural_knowledge(raw),
        "population_evidence": _population_evidence(raw),
        "indian_population_frequency": _indian_population_frequency(raw, thousand_genomes_sas_result),
        "clinical_evidence": _clinical_evidence(raw),
        "sequence_context": _sequence_context(ir, raw),
        "recommendations": ir.get("recommendations", []),
        "limitations": _limitations(ir),
        # Ruling (A), 2026-09-11: `references` is ONLY what this run used;
        # the unconditional licence notices are their own block. See
        # `DATA_ATTRIBUTION` / `reference_blocks`.
        "references": _references(ir),
        "data_attribution": list(DATA_ATTRIBUTION),
        "evidence_sources": ir.get("evidence_sources", []),
    }


# ----------------------------------------------------------------------
# Section builders. Each reads only fields InterpretationResult already
# computed (Phase 1-4) -- no new scoring, classification, or evidence
# derivation happens here.
# ----------------------------------------------------------------------


_NOT_EVALUATED_REASON_LABELS = {
    NotEvaluatedReason.NOT_INTEGRATED.value: "no data source GEPER integrates for any variant",
    NotEvaluatedReason.COMPARTMENT_INAPPLICABLE.value: (
        "structurally inapplicable to this variant's genomic compartment (see the compartment notice, on a "
        "mitochondrial finding)"
    ),
    NotEvaluatedReason.GENE_CLASS_INAPPLICABLE.value: "inapplicable to this gene's biotype/class",
    NotEvaluatedReason.CONSEQUENCE_INAPPLICABLE.value: (
        "inapplicable given this variant's own already-determined protein consequence (e.g. a frameshift, "
        "nonsense, or canonical splice-site change, for which computational predictors are moot)"
    ),
    NotEvaluatedReason.DATA_UNAVAILABLE.value: "a missing/unavailable evidence source for this specific variant",
}


def _not_evaluated_reason_clause(not_evaluated_rules: List[Dict[str, Any]]) -> str:
    """
    Round 16: replaces the old single, blanket "due to missing data
    sources" phrase -- true for `DATA_UNAVAILABLE` criteria, flatly
    false for a criterion this pipeline structurally never applies to
    this variant's compartment, this gene's biotype, or (round 29) this
    variant's own already-determined protein consequence (none of those
    is "missing", they were never applicable). Reads
    `pipeline.acmg_rules.not_evaluated_breakdown` -- the same function
    `mtdna_interpretation_disclaimer` now reads for its own counts, so
    this sentence and the disclaimer can never describe the same
    `not_evaluated_rules` list two contradictory ways again.

    Returns a clause starting with ": " (or ", " isn't used, to read as
    a colon-led explanation), omitting any category with zero criteria
    rather than printing "0 for X" noise. Returns "" when there's
    nothing to explain (list empty).
    """
    if not not_evaluated_rules:
        return ""
    breakdown = not_evaluated_breakdown(not_evaluated_rules)
    parts = [
        f"{len(codes)} {_NOT_EVALUATED_REASON_LABELS[category]}"
        for category, codes in breakdown.items()
        if codes and category in _NOT_EVALUATED_REASON_LABELS
    ]
    return f" ({'; '.join(parts)})" if parts else ""


def _executive_summary(ir: Dict[str, Any], variant_dict: Dict[str, Any] = None) -> str:
    variant_dict = variant_dict or ir.get("variant") or {}
    locus = f"{variant_dict.get('chrom')}:{variant_dict.get('pos')} {variant_dict.get('ref')}>{variant_dict.get('alt')}"
    gene = ir.get("gene_symbol")
    gene_clause = f" in {gene}" if gene else ""

    classification = ir.get("acmg_classification") or "not classified"
    conf_label = ir.get("confidence_label")
    conf_clause = (
        f" (evidence completeness: {conf_label})"
        if conf_label and not ir.get("confidence_pending", True)
        else " (evidence completeness not yet scored)"
    )

    priority_cat = ir.get("priority_category")
    priority_clause = (
        f" Assigned {priority_cat} review priority." if priority_cat and not ir.get("priority_pending", True) else ""
    )

    # Full accounting, not just two of the three buckets (C3, report
    # review round 2): a reader who saw only "N triggered" (here) and
    # "M could not be evaluated" (in Limitations) naturally added the
    # two expecting a total -- but that omits `not_triggered_rules`
    # (checked, and did not trigger), so the two numbers alone never
    # summed to the real 28-criterion total. Every one of the 28
    # ACMG/AMP criteria `pipeline/acmg_rules.py::ACMGRuleEngine.evaluate`
    # evaluates lands in exactly one of these three buckets -- this is
    # a structural partition of a fixed 28-entry set (`_STRENGTH`), not
    # something that can silently lose or double-count a criterion --
    # so the three numbers below always sum to the fourth.
    n_triggered = len(ir.get("triggered_rules", []))
    n_not_triggered = len(ir.get("not_triggered_rules", []))
    not_evaluated_rules = ir.get("not_evaluated_rules", [])
    n_not_evaluated = len(not_evaluated_rules)
    n_total = n_triggered + n_not_triggered + n_not_evaluated
    evidence_clause = (
        f" Of {n_total} ACMG/AMP criteria evaluated: {n_triggered} triggered, {n_not_triggered} checked but "
        f"not triggered, {n_not_evaluated} could not be evaluated{_not_evaluated_reason_clause(not_evaluated_rules)}."
    )

    return (
        f"Variant {locus}{gene_clause} was classified as **{classification}**{conf_clause}."
        f"{priority_clause}{evidence_clause}"
    )


def _acmg_section(ir: Dict[str, Any]) -> Dict[str, Any]:
    # `triggered_rules` and `not_triggered_rules` are rendered here in
    # the same shape so every one of the 28 ACMG/AMP criteria this
    # engine evaluates is accounted for somewhere in the report -- a
    # criterion that was checked and came back negative (e.g. BP1 not
    # triggered because an opposing missense predictor fired) used to
    # be silently absent from every report section (not in the
    # triggered table, not in `_limitations`'s not-evaluated-codes
    # footnote either, since that only lists `not_evaluated_rules`),
    # which read identically to that criterion never having been
    # checked at all.
    return {
        "classification": ir.get("acmg_classification"),
        "triggered_criteria": [
            {
                "code": c.get("code"),
                "strength": c.get("strength"),
                "direction": c.get("direction"),
                "rationale": c.get("rationale"),
                # PVS1's decision-tree audit trail (decision_path/
                # caveats_checked/unchecked_caveats) -- carried through
                # here so it can actually be rendered (C5, report
                # review round 2): the rationale text already refers a
                # reader to "the decision tree" by name, but until now
                # that tree's own path was computed and then dropped at
                # this exact mapping, never reaching any report format.
                "details": c.get("details"),
            }
            for c in ir.get("triggered_rules", [])
        ],
        "not_triggered_criteria": [
            {
                "code": c.get("code"),
                "strength": c.get("strength"),
                "direction": c.get("direction"),
                "rationale": c.get("rationale"),
                "details": c.get("details"),
            }
            for c in ir.get("not_triggered_rules", [])
        ],
        "combining_rule_trace": ir.get("combining_rule_trace", []),
        "not_evaluated_count": len(ir.get("not_evaluated_rules", [])),
        # Report review round 10: the real Tavtigian point totals next
        # to the classification they actually decided -- see
        # `InterpretationResult.acmg_net_points`'s docstring and
        # `acmg_net_points_band_label` above. `None` on all three only
        # when BA1's stand-alone-benign short-circuit fired (no point
        # tally was run), never a fabricated `0`.
        "net_points": ir.get("acmg_net_points"),
        "pathogenic_points": ir.get("acmg_pathogenic_points"),
        "benign_points": ir.get("acmg_benign_points"),
        "net_points_band": acmg_net_points_band_label(ir.get("acmg_classification")),
    }


def _confidence_section(ir: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "pending": ir.get("confidence_pending", True),
        "score": ir.get("confidence_score"),
        "label": ir.get("confidence_label"),
        "breakdown": ir.get("confidence_breakdown"),
    }


def _priority_section(ir: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "pending": ir.get("priority_pending", True),
        "score": ir.get("priority_score"),
        "category": ir.get("priority_category"),
        "rank": ir.get("priority_rank"),
        "explanation": ir.get("priority_explanation", []),
        "breakdown": ir.get("priority_breakdown"),
    }


def _conflicting_evidence(ir: Dict[str, Any]) -> List[str]:
    conflicts = ir.get("conflicting_evidence", [])
    return conflicts  # already a deduped, sourced list from Phase 2; empty list if none -- never fabricated


def _conflict_resolution_section(ir: Dict[str, Any]) -> Dict[str, Any]:
    # Phase 6, added as a new top-level section rather than changing the
    # shape of the existing `conflicting_evidence` key above (which
    # Phase 5 already shipped as a plain list) -- keeps that key
    # backward compatible while still surfacing the richer, resolved
    # conflict data from the Conflict Resolution Engine.
    return {
        "summary": ir.get("conflict_summary"),
        "score": ir.get("conflict_score"),
        "severity": ir.get("conflict_severity"),
        "resolution": ir.get("conflict_resolution"),
        "conflicts": ir.get("conflict_list", []),
    }


def _ai_consensus_section(ir: Dict[str, Any]) -> Dict[str, Any]:
    """
    `model_errors` distinguishes "this model genuinely crashed" from
    "this model was never applicable/available" -- an absent entry in
    `classifying_models` alone is ambiguous between the two (a
    synonymous variant never routed to AlphaMissense looks identical
    to an AlphaMissense that crashed, from `classifying_models` alone).
    Same distinction `_protein_knowledge`/`_structural_knowledge`/
    `_clinical_evidence`'s `*_error` fields already make for UniProt/
    InterPro/AlphaFold/ClinVar/ClinGen -- see
    `pipeline/interpretation_result.py::InterpretationResult
    .ai_model_errors`'s docstring for where this is computed.
    """
    return {
        "classifying_models": ir.get("ai_consensus", []),  # AlphaMissense / MMSplice verdicts only
        "context_models_used": ir.get("ai_context_models", []),  # HyenaDNA/Evo2/RNA-FM/ESM2, no verdict
        "model_errors": ir.get("ai_model_errors", []),
    }


def _protein_knowledge(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    `uniprot_error`/`interpro_error` are surfaced as their own field,
    distinct from `*_available` -- a failed external lookup (e.g. the
    EBI InterPro API returning an error after retries) is not the same
    statement as "this protein genuinely has no annotation", and
    conflating the two would misreport an external-service outage as a
    negative biological finding. Matches the distinction
    `report/report_generator.py::_render_uniprot`/`_render_interpro`
    (the Annotation Detail audit trail) already make for the same
    provider dicts -- this brings the Clinical Interpretation Report's
    summary section into agreement with it.
    """
    uniprot = raw.get("uniprot") or {}
    interpro = raw.get("interpro") or {}
    out: Dict[str, Any] = {
        "uniprot_available": False,
        "uniprot_error": uniprot.get("error"),
        # Round 16: distinct from `uniprot_error` -- a `reason` here means
        # this gene was never queried (or was queried and genuinely has
        # nothing) for an honest, stated cause (e.g. Ensembl biotype
        # confirms no protein-coding transcript exists), not a lookup
        # failure. `None` when no such reason was computed, in which case
        # the renderer's own generic "no entry resolved" fallback applies
        # -- see `pipeline/acmg_rules.py::non_protein_coding_gene_reason`
        # and `pipeline/orchestrator.py`'s use of it.
        "uniprot_reason": uniprot.get("reason") if not uniprot.get("found") else None,
        "interpro_available": False,
        "interpro_error": interpro.get("error"),
        "interpro_reason": interpro.get("reason") if not interpro.get("found") else None,
    }
    if uniprot.get("found"):
        out["uniprot_available"] = True
        out["uniprot"] = {
            "accession": uniprot.get("accession"),
            "protein_name": uniprot.get("protein_name"),
            "reviewed": uniprot.get("reviewed"),
        }
    if interpro.get("found"):
        out["interpro_available"] = True
        out["interpro"] = {
            "protein_position": interpro.get("protein_position"),
            # `affected_domains` stays three-valued here too (None =
            # position unknown, domain overlap never checked; [] =
            # checked, no overlap; non-empty = overlap found) -- see
            # `pipeline/interpro/lookup.py::query_variant`'s
            # docstring. `.get(..., [])`'s default only applies when
            # the key is absent, so an explicit `None` value passes
            # through unchanged rather than being silently coerced.
            "affected_domains": interpro.get("affected_domains", []),
            "domain_overlap": bool(interpro.get("affected_domains")),
        }
    return out


def _structural_knowledge(raw: Dict[str, Any]) -> Dict[str, Any]:
    """`error` distinguishes a failed AlphaFold DB lookup from a
    genuine "no structure resolved" result -- see `_protein_knowledge`'s
    docstring for the same distinction and why it matters."""
    alphafold = raw.get("alphafold") or {}
    if not alphafold.get("found"):
        # `reason` (round 16): as `_protein_knowledge`'s `uniprot_reason`
        # -- an honest, stated cause (e.g. no protein-coding transcript
        # for this gene) distinct from `error` (a failed lookup) and from
        # the generic "no structure resolved" default a renderer falls
        # back to when neither is present.
        return {"available": False, "error": alphafold.get("error"), "reason": alphafold.get("reason")}
    # `confidence_band`/`confidence_band_is_residue_specific` distinguish
    # "pLDDT at this variant's own residue" from a silent fallback to
    # the whole-protein mean when the residue position is unknown --
    # both are legitimate numbers, but reporting one under a label that
    # doesn't say which would let a reader assume residue-specificity
    # that isn't there.
    residue_band = alphafold.get("affected_residue_band")
    return {
        "available": True,
        "confidence_band": residue_band or alphafold.get("mean_plddt_band"),
        "confidence_band_is_residue_specific": residue_band is not None,
        # The mapping gate's own specific verdict for why (mapping_gate.py
        # has six distinct reasons; "position unknown" is only one of
        # them) -- carried through so a renderer never has to fall back
        # to a hardcoded, sometimes-inaccurate guess at the reason.
        "mapping_unavailable_reason": alphafold.get("mapping_unavailable_reason"),
        "protein_position": alphafold.get("protein_position"),
        "mean_plddt": alphafold.get("mean_plddt"),
        "affected_residue_plddt": alphafold.get("affected_residue_plddt"),
        "model_version": alphafold.get("model_version"),
        "pdb_url": alphafold.get("pdb_url"),
        "cif_url": alphafold.get("cif_url"),
        "protein_position_basis": alphafold.get("protein_position_basis"),
    }


def _population_evidence(raw: Dict[str, Any]) -> Dict[str, Any]:
    gnomad = raw.get("gnomad") or {}
    dbsnp = raw.get("dbsnp") or {}
    return {
        "gnomad": {
            "queried": not (gnomad.get("skipped") or gnomad.get("error") is not None),
            "found": bool(gnomad.get("found")),
            "global_af": gnomad.get("global_af"),
            # Round 16: when gnomAD was deliberately never queried (e.g.
            # `pipeline/acmg_rules.py::mtdna_gnomad_skip_result` for a
            # mitochondrial variant -- gnomAD's mitochondrial callset is a
            # separate resource this pipeline doesn't query), `reason`
            # carries the real, stated cause instead of the generic
            # "lookup unavailable" a renderer previously always printed
            # regardless of whether this was a deliberate skip or a
            # genuine outage.
            "skip_reason": gnomad.get("reason") if gnomad.get("skipped") else None,
            # `error` distinguishes a failed lookup from either of the
            # above -- see `_protein_knowledge`'s docstring for the same
            # distinction. Before this field existed, `queried=False` with
            # no `skip_reason` (an error is not a skip, so `skip_reason`
            # stays `None` for it too) was indistinguishable from a
            # deliberate skip whose `reason` happened to be falsy, and the
            # renderer rendered both as generic "lookup unavailable" --
            # collapsing "gnomAD broke" into "gnomAD was never asked",
            # inside the clinician-facing summary section specifically
            # (Section 10), while the Annotation Detail audit trail
            # (`_render_gnomad`) already got this right.
            "error": gnomad.get("error"),
        },
        "dbsnp": {
            "queried": not (dbsnp.get("skipped") or dbsnp.get("error") is not None),
            "found": bool(dbsnp.get("found")),
            "rsid": dbsnp.get("rsid") if dbsnp.get("found") else None,
            # Same shape as gnomAD's `skip_reason` three lines above --
            # dbSNP's own stages never set `skipped`/`reason` today (no
            # deliberate-skip path exists for it the way the mtDNA
            # compartment gate skips gnomAD), so this is currently
            # always `None`; carried for symmetry and so a future
            # deliberate-skip path has somewhere to put its reason
            # without a second silent-drop defect.
            "skip_reason": dbsnp.get("reason") if dbsnp.get("skipped") else None,
            # Same reasoning and same fix as gnomAD's `error` field above.
            "error": dbsnp.get("error"),
        },
    }


def _indian_population_frequency(
    raw: Dict[str, Any],
    thousand_genomes_sas_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    India-deployment feature: gnomAD's South Asian (SAS) subpopulation
    allele frequency alongside the 1000 Genomes Project's SAS
    sub-population frequency (see `annotation/thousand_genomes_sas.py`'s
    module docstring), plus a single "common in Indian populations"
    flag when either exceeds `CONFIG.indigenomes.COMMON_AF_THRESHOLD`
    (default 1%).

    AS OF 2026-08-08, 1000 Genomes SAS is the SOLE Indian/South-Asian
    cohort source this section shows, run unconditionally for every
    variant -- see `annotation/thousand_genomes_sas.py`'s module
    docstring and `config.py::ThousandGenomesSASConfig`'s docstring for
    why: IndiGenomes (the source this originally sat alongside, and
    which this was built as an offline-fallback for) was retired from
    GEPER's active query path entirely, because its own terms restrict
    commercial use and GEPER has not obtained a license (see
    `DATA_SOURCE_LICENSE_AUDIT.md`). There is therefore no longer an
    "IndiGenomes vs. 1000 Genomes SAS fallback" branch here -- unlike
    the prior version of this function, `sas_*` below is simply THE
    result whenever this section has anything to show, the same way any
    other single-source report field is rendered, not something gated
    on another source's confirmed-offline state.

    The two mandatory disclosures (`SAMPLE_SIZE_DISCLOSURE`/
    `DIASPORA_DISCLOSURE`, see `annotation/thousand_genomes_sas.py`)
    matter more now than when this was an occasional fallback -- they
    must appear every time this section shows anything from this
    source, not conditionally. This function doesn't render them
    itself (that's `report/report_generator.py`'s and
    `report/summary.py`'s job, both importing the same shared text so
    Markdown and PDF never drift on wording), but `sas_shown` below is
    unconditionally true whenever there's a gnomAD-SAS or 1000-Genomes-
    SAS figure to report, so the renderer always has a reason to
    include them.

    `gnomad_af_sas` is read from `raw["gnomad"]["population_breakdown"]`
    (already computed by the gnomAD stage -- see
    `pipeline/gnomad/models.py::POPULATIONS`), not re-queried here.
    `sas_population_labels`/`sas_sample_sizes`/`sas_total_sample_size`
    are carried through explicitly (not left for the renderer to
    hardcode or, worse, omit) so every render of this source --
    Markdown and both PDF paths -- states both mandatory disclosures
    the same way, from the same source of truth. Deliberately NOT
    folded into `common_in_indian_population` below: `sas_pooled`'s AF
    is a small, diaspora-sourced figure (see
    `annotation/thousand_genomes_sas.py`'s disclosures) and mixing it
    into the same threshold check as gnomAD's much larger SAS reference
    population would let a genuinely low-confidence number quietly
    influence a clinical flag on equal footing with a better-powered
    one -- so the flag is driven by gnomAD SAS alone now (previously
    gnomAD SAS + IndiGenomes; IndiGenomes is gone, and 1000 Genomes SAS
    was never eligible for this flag for the same reason).
    """
    gnomad = raw.get("gnomad") or {}
    gnomad_sas = (gnomad.get("population_breakdown") or {}).get("sas") or {}
    gnomad_sas_af = gnomad_sas.get("af")

    threshold = CONFIG.indigenomes.COMMON_AF_THRESHOLD
    common_in_indian_population = gnomad_sas_af is not None and gnomad_sas_af >= threshold

    tgs = thousand_genomes_sas_result or {}
    sas_available = bool(tgs.get("found"))
    # True whenever the 1000 Genomes SAS stage actually ran this
    # variant (`pipeline/orchestrator.py::_run_thousand_genomes_sas_stage`,
    # unconditional as of 2026-08-08) -- i.e. whenever a caller passed a
    # real result dict at all, found/not-found/errored alike. Only
    # False in a test/caller that passes `None` outright (the stage
    # genuinely wasn't invoked), mirroring how `fallback_shown` used to
    # gate on "was IndiGenomes confirmed offline" -- now it gates on
    # "did this stage run", which in real pipeline operation is always.
    sas_shown = thousand_genomes_sas_result is not None

    return {
        "gnomad_af_sas": gnomad_sas_af,
        "gnomad_sas_queried": not (gnomad.get("skipped") or gnomad.get("error") is not None),
        # Same fix, same reasoning as `_population_evidence`'s gnomad
        # `error` field: this is the same underlying gnomAD stage result,
        # re-read here for its South Asian subpopulation figure, and it
        # had the identical collapse -- a failed lookup and a lookup
        # never attempted both left `gnomad_sas_queried=False` with no
        # way for the renderer to tell them apart, so both used to
        # print the generic "lookup unavailable for this variant."
        "gnomad_sas_error": gnomad.get("error"),
        "common_af_threshold": threshold,
        "common_in_indian_population": common_in_indian_population,
        "sas_shown": sas_shown,
        "sas_available": sas_available,
        "sas_error": tgs.get("error") if sas_shown else None,
        "sas_rsid": tgs.get("rsid") if sas_shown else None,
        "sas_pooled": tgs.get("sas_pooled") if sas_available else None,
        "sas_sub_populations": tgs.get("sub_populations") if sas_available else None,
        "sas_population_labels": tgs.get("population_labels") if sas_available else None,
        "sas_sample_sizes": tgs.get("sample_sizes") if sas_available else None,
        "sas_total_sample_size": tgs.get("total_sample_size") if sas_available else None,
    }


def _clinical_evidence(raw: Dict[str, Any]) -> Dict[str, Any]:
    """`clinvar_error`/`clingen_error` distinguish a failed lookup from
    a genuine "no record"/"no curation" result -- see `_protein_knowledge`'s
    docstring for the same distinction and why it matters."""
    clinvar = raw.get("clinvar") or {}
    clingen = raw.get("clingen") or {}
    out: Dict[str, Any] = {
        "clinvar_available": False,
        "clinvar_error": clinvar.get("error"),
        "clingen_available": False,
        "clingen_error": clingen.get("error"),
    }
    # `primary_record`, never `records[0]` -- ClinVar's positional
    # search can return several distinct co-located variants at one
    # locus, and a bare first-record read used to attribute whichever
    # one sorted first to this variant regardless of whether it was
    # actually the same allele (see `database/clinvar_client.py`'s
    # module docstring for the real case this fixes). `match_status ==
    # "position_only"` means other, non-matching variants exist at
    # this position -- surfaced as count-only context, distinct from
    # "no record found" (`not_found`).
    if clinvar.get("match_status") == "matched" and clinvar.get("primary_record"):
        primary = clinvar["primary_record"]
        out["clinvar_available"] = True
        out["clinvar"] = {
            "clinical_significance": primary.get("clinical_significance"),
            "review_status": primary.get("review_status"),
        }
    elif clinvar.get("match_status") == "position_only":
        out["clinvar_co_located_count"] = len(clinvar.get("records") or [])
    if clingen.get("found"):
        out["clingen_available"] = True
        out["clingen"] = {
            "gene_symbol": clingen.get("gene_symbol"),
            "clinical_validity_summary": clingen.get("clinical_validity_summary"),
            "dosage_sensitivity": clingen.get("dosage_sensitivity"),
        }
    return out


def _sequence_context(ir: Dict[str, Any], raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    `blast["error"]` distinguishes a failed BLAST search from a
    genuine "no homology hits" result -- see `_protein_knowledge`'s
    docstring for the same distinction elsewhere. Unlike UniProt/
    InterPro/AlphaFold/ClinVar/ClinGen, BLAST previously had NO
    mechanism anywhere in this report to make that distinction (see
    `pipeline/orchestrator.py::_run_blast_stage`).
    """
    blast = raw.get("blast") or {}
    return {
        "context_models_used": ir.get("ai_context_models", []),
        "blast": {
            # No default: a count that was never recorded stays None, and the
            # renderers say so instead of printing a 0 nobody measured
            # (ruling #18, 2026-09-11: print the number only when recorded).
            "hit_count": blast.get("hit_count"),
            "error": blast.get("error"),
            # Carried because every not-run path (no sequence context,
            # BLAST disabled, no usable backend) reports `hit_count: 0`
            # WITH `skipped: True` -- dropping `skipped` is what turned a
            # search that never ran into "0 homology hit(s)".
            "skipped": bool(blast.get("skipped")),
            "reason": blast.get("reason"),
        },
        # Ensembl is used internally for sequence-context retrieval
        # (transcript/region lookups feeding the DNA/RNA models above)
        # rather than surfaced as its own per-variant evidence dict, so
        # it is noted here descriptively rather than scored -- same
        # treatment as in the Phase 3/4 engines.
        "ensembl_note": (
            "Ensembl contributes indirectly via sequence-context retrieval (transcript/region "
            "lookups feeding the sequence-context models above); it is not surfaced as a separate "
            "per-variant evidence source in this pipeline."
        ),
    }


def _limitations(ir: Dict[str, Any]) -> List[str]:
    limitations = list(_TOOL_LIMITATIONS)
    # Built per-finding from `ai_context_models` (what this variant's
    # DNA-model router actually ran, per `pipeline/orchestrator.py`'s
    # `dna_models_used`, plus RNA-FM/ESM2 when applicable -- see
    # `interpretation_result.py`), never a fixed claim that all four of
    # HyenaDNA/Evo2/RNA-FM/ESM2 ran for every variant, which previously
    # contradicted findings whose own evidence line named only one.
    context_models = ir.get("ai_context_models") or []
    if context_models:
        limitations.append(
            f"Sequence-context model(s) actually used for this variant: {', '.join(context_models)}. "
            "These contribute routing and embedding context in this pipeline; they do not themselves "
            "output a per-variant pathogenic/benign verdict, and are reported separately from "
            "AlphaMissense/MMSplice for that reason."
        )
    else:
        limitations.append(
            "No sequence-context model (HyenaDNA, Evo2, RNA-FM, ESM2) produced a result for this variant."
        )
    not_evaluated = ir.get("not_evaluated_rules", [])
    if not_evaluated:
        codes = ", ".join(c.get("code", "?") for c in not_evaluated)
        n_total = len(ir.get("triggered_rules", [])) + len(ir.get("not_triggered_rules", [])) + len(not_evaluated)
        # Round 16: was one blanket "due to missing evidence sources" for
        # every not_evaluated criterion -- true for some, false for a
        # criterion this pipeline structurally never applies to this
        # variant's compartment or gene-class (that's not "missing", it
        # was never applicable). `_not_evaluated_reason_clause` reads
        # `pipeline.acmg_rules.not_evaluated_breakdown`, the same
        # function `mtdna_interpretation_disclaimer` reads for its own
        # counts -- one source, so this sentence and that disclaimer
        # cannot describe the same list two contradictory ways again.
        # 2026-09-10: this sentence used to send the reader to "the ACMG
        # classification section... for the specific reason each
        # not-evaluated one was skipped." That pointer was false on two
        # independent grounds, not one: (1) not-evaluated is a third
        # bucket, disjoint from the triggered/not-triggered criteria
        # those tables are built from, so per-criterion not-evaluated
        # detail cannot appear in either table regardless of format; and
        # (2) the "checked-but-not-triggered" table it named is rendered
        # only in the PDF (summary.py) -- report_generator.py's Markdown
        # output never builds one at all, so the claim was additionally
        # false, on that surface, independent of (1). Neither defect is
        # fixed by inventing a destination; this report does not render
        # each not-evaluated criterion's individual reason anywhere, on
        # any surface, and says so rather than pointing at a place that
        # cannot hold it.
        limitations.append(
            f"{len(not_evaluated)} of {n_total} ACMG criteria could not be evaluated for this variant"
            f"{_not_evaluated_reason_clause(not_evaluated)} ({codes}); the specific reason each individual "
            f"one was skipped is not rendered elsewhere in this report."
        )
    if ir.get("confidence_pending", True):
        limitations.append("Confidence scoring did not complete for this variant.")
    if ir.get("priority_pending", True):
        limitations.append("Priority scoring did not complete for this variant.")
    return limitations


# RULING (A), human via god 2026-09-11 (card HUMAN-DECISION-THE-REFERENCES-
# LIST-CONFLATES-...): the sources a run USED and the licence notices the
# product attributes UNCONDITIONALLY are two different claims and are rendered
# as two labelled blocks on every surface. Until then `_references()` appended
# Orphanet and AlphaFold to the gated list, so a run that used only HPO and
# ClinVar listed four "References", two of which it never consulted -- "a
# fabricated citation in the exact place a reader goes to check everything
# else." The headings are the ruled wording; the notices' own text is
# unchanged.
EVIDENCE_SOURCES_USED_HEADING = "Evidence sources used in this run"
DATA_ATTRIBUTION_HEADING = "Data attribution"
NO_EVIDENCE_SOURCES_TEXT = "No evidence sources contributed to this variant's interpretation."

# Fixed licence notices, attributed in every report -- see `_ORPHANET_REFERENCE`
# and `_ALPHAFOLD_REFERENCE` above for why each is unconditional. Order kept
# from the old single list (Orphanet, then AlphaFold).
DATA_ATTRIBUTION = (_ORPHANET_REFERENCE, _ALPHAFOLD_REFERENCE)


def _references(ir: Dict[str, Any]) -> List[str]:
    """Only the sources this variant's evidence actually came from (gated on
    `evidence_sources`). The unconditional licence notices are NOT in here any
    more -- they are `DATA_ATTRIBUTION`, carried as the clinical report's
    separate `data_attribution` key."""
    sources = ir.get("evidence_sources", [])
    return [_REFERENCES[s] for s in sources if s in _REFERENCES]


def reference_blocks(clinical: Optional[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    """
    `(evidence_sources_used, data_attribution)` for one variant's clinical
    report -- the ONE place every renderer (Markdown, full PDF, short PDF)
    gets its two blocks from, so they cannot drift.

    The attribution notices are filtered out of `references` rather than
    trusted to be absent: a `geper_results.json` written before the split
    carries them mixed into that list, and `review/signoff.py` re-renders
    stored documents. Without the filter a re-rendered legacy document would
    list Orphanet/AlphaFold as sources the run used -- the exact claim the
    ruling removes. Attribution itself is the fixed tuple, not read from the
    document: it is unconditional by design, so there is nothing per-run to
    read.
    """
    references = (clinical or {}).get("references") or []
    evidence = [r for r in references if r not in DATA_ATTRIBUTION]
    return evidence, list(DATA_ATTRIBUTION)


# ---------------------------------------------------------------------------
# Revision traceability (amendment / supersession / re-analysis)
#
# Card HUMAN-CLINICAL-report-revision-traceability-...: RULED 2026-09-10
# "SURFACE IT" ("Amended and reanalysed reports must say so ON THE PAGE, with
# the reason retrievable"), 2026-09-11 "KEEP THE REASON ON THE PAGE ... APPLY
# TO TODAY'S GEPER RENDERERS", wording signed off 2026-09-11 (banners 1, 2, 4
# approved; 3 revised). THE FOUR TEMPLATES BELOW ARE THAT SIGNED-OFF WORDING,
# VERBATIM. Do not reword them; tests pin them as literals.
#
# WHERE THE FACTS COME FROM. The clinical platform (clinical/schema.sql) is
# the system of record: `amendments` (original_report_id, amendment_report_id,
# reason NOT NULL, created_at, created_by, supersedes_amendment_id) and
# `interpretations.parent_interpretation_id` (re-analysis lineage, spec 15.3).
# This renderer layer has no database access, so a caller holding those rows
# states them in `document["report_revision"]` (see
# `normalize_report_revision` for the shape) -- via `JSONResultBuilder(
# report_revision=...)` or `apply_report_revision()` for a stored document --
# and every surface renders from that one block.
#
# BANNER 3 HAS NO REASON SLOT, DELIBERATELY. `interpretations` has no
# re-analysis-reason column (`create_reanalysis` takes no reason), so the
# ruled wording states that structurally ("The system does not record reasons
# for re-analysis.") rather than as a finding about this report. If a reason
# column is ever added, the ruling is: render the reason when present and
# OMIT that sentence when absent -- that is a wording change for this block,
# not something to bolt on silently.
# ---------------------------------------------------------------------------

REPORT_REVISION_AMENDED_BANNER = (
    "THIS IS AN AMENDED REPORT. It amends a report originally issued on {date}. "
    "Reason for amendment: {reason}. Amended by {who} on {when}."
)
REPORT_REVISION_SUPERSEDED_BANNER = (
    "*** THIS REPORT HAS BEEN SUPERSEDED. An amended report was issued on {when} for the reason below; "
    "do not act on this document without first obtaining the amended report ({id}). "
    "Reason for the amendment: {reason}. ***"
)
REPORT_REVISION_REANALYSIS_BANNER = (
    "THIS REPORT IS BASED ON A RE-ANALYSIS of the VCF data underlying interpretation {id}, "
    "originally interpreted on {date}. The system does not record reasons for re-analysis."
)
REPORT_REVISION_REANALYSED_SINCE_BANNER = (
    "One or more re-analyses of the underlying VCF data exist since this report was issued. This report "
    "reflects the original analysis only; it has not been retracted or superseded by the re-analysis "
    "(see spec 15.3 -- re-analysis creates a new branch, it does not replace this one)."
)

_REVISION_REQUIRED_FIELDS = {
    "amends": ("original_report_id", "original_issued_at", "reason", "amended_by", "amended_at"),
    "superseded_by": ("amendment_report_id", "amended_at", "reason"),
    "reanalysis_of": ("parent_interpretation_id", "parent_interpreted_at"),
}


def _revision_value(value: Any) -> Any:
    """JSON-safe copy of one field: datetimes/dates/UUIDs become strings."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _revision_block(kind: str, block: Any) -> Optional[Dict[str, Any]]:
    if not block:
        return None
    if not isinstance(block, dict):
        raise ValueError(f"report_revision.{kind} must be an object, got {type(block).__name__}")
    out = {k: _revision_value(v) for k, v in block.items()}
    # FAIL CLOSED. Every one of these is NOT NULL in the clinical schema, so
    # a block missing one is corrupted input. Rendering it anyway would print
    # an amendment with a blank reason, or -- worse -- let a caller's partial
    # dict fall through to "no banner", which is an amended report reading as
    # an original: the exact failure this block exists to prevent.
    missing = [f for f in _REVISION_REQUIRED_FIELDS[kind] if not str(out.get(f) or "").strip()]
    if missing:
        raise ValueError(f"report_revision.{kind} is missing required field(s) {missing}; refusing to render it")
    return out


def _revision_date(value: str) -> str:
    return format_ist_from_iso(value, "%Y-%m-%d")


def _revision_when(value: str) -> str:
    return format_ist_from_iso(value)


def _revision_reason(value: str) -> str:
    # The template supplies the sentence's closing period; strip one the
    # author typed so it never renders as "..variant..". Wording untouched.
    reason = str(value).strip()
    return reason[:-1].rstrip() if reason.endswith(".") else reason


def normalize_report_revision(raw: Any) -> Optional[Dict[str, Any]]:
    """
    Validate a caller's revision facts and return the block written to
    `document["report_revision"]`, or `None` when none were supplied.

    Input (every key optional; an absent/empty key means "not in that
    state"):
      amends:          {original_report_id, original_issued_at, reason,
                        amended_by, amended_at}      -- banner 1
      superseded_by:   {amendment_report_id, amended_at, reason}  -- banner 2
      reanalysis_of:   {parent_interpretation_id, parent_interpreted_at}
                                                       -- banner 3
      reanalysed_since: list of {interpretation_id, created_at}, non-empty
                        when any re-analysis branches from this report's
                        interpretation                -- banner 4

    Output adds the machine-readable flags (`is_amendment`, `is_superseded`,
    `is_reanalysis`, `has_been_reanalysed`) and `banners`, the rendered text
    in display order. Superseded comes FIRST: "do not act on this document"
    outranks every other statement on the page.

    `None` stays `None` -- NOT an all-false block. A pipeline run cannot know
    whether the clinical platform will later store it as a re-analysis, so an
    unsupplied state must not read as "not a re-analysis". An explicitly
    supplied block with no state (`{}`) is all-false with no banners.

    Raises `ValueError` on a state block missing a required field (see
    `_revision_block`).
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"report_revision must be an object, got {type(raw).__name__}")

    amends = _revision_block("amends", raw.get("amends"))
    superseded_by = _revision_block("superseded_by", raw.get("superseded_by"))
    reanalysis_of = _revision_block("reanalysis_of", raw.get("reanalysis_of"))
    reanalysed_since_raw = raw.get("reanalysed_since") or []
    if not isinstance(reanalysed_since_raw, list):
        raise ValueError("report_revision.reanalysed_since must be a list")
    reanalysed_since = [
        {k: _revision_value(v) for k, v in item.items()} if isinstance(item, dict) else _revision_value(item)
        for item in reanalysed_since_raw
    ]

    banners: List[str] = []
    if superseded_by:
        banners.append(
            REPORT_REVISION_SUPERSEDED_BANNER.format(
                when=_revision_when(superseded_by["amended_at"]),
                id=superseded_by["amendment_report_id"],
                reason=_revision_reason(superseded_by["reason"]),
            )
        )
    if amends:
        banners.append(
            REPORT_REVISION_AMENDED_BANNER.format(
                date=_revision_date(amends["original_issued_at"]),
                reason=_revision_reason(amends["reason"]),
                who=amends["amended_by"],
                when=_revision_when(amends["amended_at"]),
            )
        )
    if reanalysis_of:
        banners.append(
            REPORT_REVISION_REANALYSIS_BANNER.format(
                id=reanalysis_of["parent_interpretation_id"],
                date=_revision_date(reanalysis_of["parent_interpreted_at"]),
            )
        )
    if reanalysed_since:
        banners.append(REPORT_REVISION_REANALYSED_SINCE_BANNER)

    return {
        "is_amendment": amends is not None,
        "is_superseded": superseded_by is not None,
        "is_reanalysis": reanalysis_of is not None,
        "has_been_reanalysed": bool(reanalysed_since),
        "amends": amends,
        "superseded_by": superseded_by,
        "reanalysis_of": reanalysis_of,
        "reanalysed_since": reanalysed_since,
        "banners": banners,
    }


def apply_report_revision(document: Dict[str, Any], revision: Any) -> Dict[str, Any]:
    """Stamp validated revision facts onto an already-built (e.g. stored and
    reloaded) document before re-rendering it. Returns the same dict."""
    document["report_revision"] = normalize_report_revision(revision)
    return document


def report_revision_banners(document: Optional[Dict[str, Any]]) -> List[str]:
    """
    The banner texts every renderer prints at the top of the document, in
    display order; `[]` when the document carries no revision state (absent
    key, `null`, or a block in no state) -- the negative case renders nothing.

    Re-derived from the block's FACTS on every render rather than trusting a
    stored `banners` list, so a hand-edited or stale document cannot print
    wording that differs from the signed-off templates above.
    """
    normalized = normalize_report_revision((document or {}).get("report_revision"))
    return normalized["banners"] if normalized else []


# ---------------------------------------------------------------------------
# Shared run-level caveat helpers
#
# These three lived in `report/summary.py` until 2026-08-21. They are read
# by every renderer -- both PDFs, the Markdown report, and (for the offline
# sources caveat) the JSON run-level block -- so leaving them in one
# renderer would have made the full-PDF module the de facto shared library
# for the whole report layer, and forced `report/report_generator.py` to
# import from a PDF renderer to reach them.
#
# That is the same failure the `RESEARCH_USE_DISCLAIMER` consolidation
# fixed, in function form rather than constant form: content every renderer
# needs, living inside one of them, is what produced two independently
# worded disclaimers that drifted into contradicting each other. Anything
# a second renderer needs belongs here, where all of them already import
# from.
#
# They stay `_`-prefixed rather than being renamed on the move: the point
# of this change is the location, and renaming would have churned every
# call site and several test modules for no behavioural gain.
# ---------------------------------------------------------------------------


def _offline_sources_caveat_text(document: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """
    Run-level, not per-finding: which external evidence sources (see
    `utils/service_health.py`) did not fully answer during this run, so
    that "not evaluated" or "not found" text elsewhere in the report is
    never mistaken for a completed, negative search. Returns `None`
    only when every source was reachable at startup AND none failed
    during the run.

    TWO KINDS OF UNAVAILABILITY, REPORTED SEPARATELY BECAUSE THEY MEAN
    DIFFERENT THINGS:

      offline at startup -- never queried for any variant this run.
      failed during the run -- queried, failed, retried. The client
          may have recovered on a later attempt or fallen back to a
          graceful skip, so evidence from it may be present, partial,
          or absent, and the report cannot tell the reader which.

    The second kind used to be reported nowhere. `offline_services()`
    only knows about services LATCHED OFFLINE AT STARTUP, so a source
    that was healthy at startup and then failed mid-run produced no
    caveat at all -- while its handled failures never reached any
    variant's `errors` list either (that list collects stage
    exceptions, and a retried failure raises none). Observed 2026-08-29:
    Ensembl probed Online (1155 ms) at startup, failed 3/3 attempts on a
    sequence region and 3/3 on an exon annotation mid-run, and the run
    wrote `errors: []`, `run_complete: true` and four output artefacts
    that said nothing about it, while the console printed
    `Ensembl ... Intermittent (2 failures)` and exited. The one visible
    consequence was an exon-annotation lookup rendered as the
    biological claim "no exon annotation found near this variant" -- for
    a canonical splice-acceptor variant.

    PREFER THE DOCUMENT, FALL BACK TO THE LIVE REGISTRY. When
    `document` carries a `service_health` list (written by
    `report/json_builder.py` from `ServiceHealthRegistry.snapshot()`),
    this reads that. Otherwise it reads the live `HEALTH` singleton, as
    it always did, so a legacy document produced before that field
    existed still renders the startup-offline caveat.

    Reading the document matters for re-renders. `review/signoff.py`
    loads a stored `geper_results.json` back in a FRESH PROCESS and
    re-invokes both PDF renderers and the Markdown generator against
    it. In that process `HEALTH` has never run a check, so the live
    registry is empty and this function returned `None` unconditionally
    -- meaning approving a run in which a source was offline produced a
    SIGNED, clinician-facing PDF with the caveat silently dropped. Same
    failure, and the same fix, as `qc_metrics` on
    `report/json_builder.py::JSONResultBuilder`: a caveat in the
    document survives, an argument passed once does not.

    IndiGenomes no longer appears here as of 2026-08-08: it's retired
    from GEPER's active query path (see `config.py::IndiGenomesConfig`'s
    docstring and `DATA_SOURCE_LICENSE_AUDIT.md`), which also disables
    its `utils/service_health.py` startup probe by default -- a source
    that's never checked can never show up as "confirmed offline"
    here, so this caveat mechanism naturally stops mentioning it
    without needing a special case.
    """
    snapshot = (document or {}).get("service_health") or []
    if snapshot:
        offline = [s.get("service") for s in snapshot if s.get("startup_status") == ServiceStatus.OFFLINE.value]
        # In-process components are partitioned out here rather than
        # described as retried data sources: nothing retried them, and
        # the consequence for the reader is different (a withheld score,
        # not evidence that may be complete/partial/absent).
        components = [s for s in snapshot if s.get("failure_count") and s.get("is_local_component")]
        degraded = [
            s
            for s in snapshot
            if s.get("failure_count") and s.get("service") not in offline and not s.get("is_local_component")
        ]
    else:
        # Legacy document (no `service_health` key) or a caller that
        # passed nothing: fall back to exactly the previous behaviour.
        offline = HEALTH.offline_services()
        degraded = []
        components = []

    if not offline and not degraded and not components:
        return None

    parts: List[str] = []
    if offline:
        parts.append(
            "The following data source(s) were unreachable during this analysis run and were not queried for "
            "any variant in this report: " + ", ".join(offline) + '. Any finding reported as "not evaluated" '
            "or lacking data from these sources reflects a data-collection gap for this run, not a confirmed "
            "absence -- it should not be treated as a negative result."
        )
    if degraded:
        detail = ", ".join(f"{s.get('service')} ({s.get('failure_count')} failure(s))" for s in degraded)
        parts.append(
            "The following data source(s) failed at least once during this run and were retried: "
            + detail
            + ". A retry may have succeeded, so evidence from these sources may be complete, partial, or "
            "absent, and this report cannot distinguish which. Any finding that depends on them and is "
            "reported as absent, not evaluated, or not found should be read as possibly reflecting that "
            "failure rather than a confirmed negative."
        )
    if components:
        detail = ", ".join(f"{s.get('service')} ({s.get('failure_count')} failure(s))" for s in components)
        parts.append(
            "The following internal analysis component(s) failed during this run: "
            + detail
            + ". Where this affected a finding, the score that component feeds is reported as "
            '"Pending" rather than computed, because a component that did not run produces no '
            "evidence of conflict and must not be read as having found none. Any finding showing a "
            "pending confidence or review-priority value should be scored manually before sign-off."
        )
    return " ".join(parts)


def _variant_locus(variant_result: Dict[str, Any]) -> str:
    """Genomic-coordinate identifier -- chrom:pos ref>alt. Always
    available whenever `variant_result["variant"]` is present; the
    fallback of last resort for `_variant_hgvs_or_locus` below."""
    variant = variant_result.get("variant") or {}
    return f"{variant.get('chrom')}:{variant.get('pos')} {variant.get('ref')}>{variant.get('alt')}"


def _variant_hgvs_or_locus(variant_result: Dict[str, Any]) -> str:
    """
    Best available variant identifier: transcript-level HGVS.c when the
    normalization stage resolved one, otherwise genomic HGVS.g,
    otherwise the plain chrom:pos ref>alt locus. Never synthesizes
    notation of its own -- see `pipeline/orchestrator.py::
    _run_normalization_stage` / `_attach_hgvs_c` for how `normalization`
    is populated.

    Card T3-F4: single source of truth for all three report renderers
    (full Markdown, full PDF, short PDF) so they degrade the same way
    when a field is missing, rather than each carrying its own
    independently-maintained copy of this fallback order that could
    silently drift out of sync -- the same failure mode commit b2a6083
    ("HIGH 3: replace three by-hand-synced duplications with shared
    functions, and delete one divergent line") removed elsewhere in this
    codebase. Previously this logic existed only in
    `report/summary_short.py` (as `_variant_hgvs`); the full report
    headings carried no HGVS at all.
    """
    normalization = variant_result.get("normalization") or {}
    return normalization.get("hgvs_c") or normalization.get("hgvs_g") or _variant_locus(variant_result)


VAF_FIELD_NOT_CAPTURED_REASON = (
    "This report was rendered from a geper_results.json written before per-sample allele fractions "
    "were recorded, so none is available for this variant. The field was never captured for this run "
    "-- which is not the same as this VCF having carried no per-sample data."
)


def _variant_allele_fraction_states(variant_result: Dict[str, Any]) -> Optional[Dict[str, Dict[str, Any]]]:
    """
    The `{sample_name: {status, value, ...}}` mapping `pipeline/vcf_parser.py
    ::Variant.to_dict` emits.

    Three return values, not two, for the same reason the states inside it
    are three: `None` means the document predates this field entirely (a
    re-render of an older `geper_results.json`), while `{}` means this run
    DID look and the VCF carried no genotype columns. Collapsing them
    would let a stale document assert something about a VCF it never
    examined -- the same "absence of a record is not a record of absence"
    error this whole field exists to avoid, displaced onto the document.
    """
    variant = variant_result.get("variant") or {}
    if "variant_allele_fractions" not in variant:
        return None
    return variant.get("variant_allele_fractions") or {}


def _variant_allele_fraction_found(states: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Only the samples carrying a REAL number: status FOUND *and* a usable
    numeric value. `bool` is excluded explicitly because it is an `int`
    subclass, so a stray `True` can never be read as a fraction of 1.0 --
    the same guard `_parse_one_qc_metric` applies to QC values, for the
    same reason.
    """
    return {
        name: state
        for name, state in states.items()
        if state.get("status") == _StageStatus.FOUND.value
        and isinstance(state.get("value"), (int, float))
        and not isinstance(state.get("value"), bool)
    }


def _one_allele_fraction_text(state: Dict[str, Any]) -> str:
    """One FOUND sample's fraction, with its denominator when there is one to show."""
    value = state.get("value")
    alt_depth, depth = state.get("alt_depth"), state.get("depth")
    if isinstance(alt_depth, int) and isinstance(depth, int):
        return f"{value:.3g} ({alt_depth}/{depth} reads, FORMAT/AD)"
    # FORMAT/AF: a caller-supplied fraction with no denominator to audit
    # it against. Said out loud rather than left to look like an AD-backed
    # number with the read counts merely omitted.
    return f"{value:.3g} (FORMAT/AF; no read depths reported)"


def variant_allele_fraction_text(variant_result: Dict[str, Any]) -> str:
    """
    The one rendering of a variant's per-sample allele fraction, shared by
    the full Markdown report (`report/report_generator.py`) and the full
    clinical PDF (`report/summary.py`) so the two cannot drift -- the same
    single-source-of-truth reason `_variant_hgvs_or_locus` above exists,
    and the reason this lives here rather than in either renderer.

    Deliberately NOT a section of `build_clinical_report`'s returned dict:
    that function returns `None` outright when interpretation is missing
    or errored (see its guard above), and an allele fraction is a property
    of the VCF record -- known the instant the line is parsed, and wholly
    independent of whether interpretation succeeded. Routed through the
    clinical report, a failed interpretation and a VCF carrying no
    per-sample data would render identically: the exact collapse this
    field exists to prevent, one layer up.

    Resolution order -- the tri-state resolves FIRST, the sample count
    SECOND. Getting that order backwards is what makes "multiple samples;
    see JSON" a lie on a VCF that has several samples and no allele
    depths at all: it points a reader at a JSON holding nothing but
    `not_run` entries, asserting data exists where none does.
    """
    states = _variant_allele_fraction_states(variant_result)
    if states is None:
        return VAF_FIELD_NOT_CAPTURED_REASON
    if not states:
        return VAF_NO_SAMPLE_COLUMNS_REASON

    found = _variant_allele_fraction_found(states)
    if found:
        if len(found) > 1:
            return (
                f"{len(found)} samples carry an allele fraction for this variant; per-sample values are "
                f"in geper_results.json (variants[].variant.variant_allele_fractions)."
            )
        # Exactly one real number. The sample name always travels with it
        # -- an unlabelled fraction on a multi-sample run reads as though
        # it stood for the whole record.
        name, state = next(iter(found.items()))
        text = f"{_one_allele_fraction_text(state)}, sample {name}"
        others = len(states) - 1
        if others > 0:
            # Never silently drop the other samples: without this, one
            # found value on a trio would hide two failed or unmeasured
            # ones behind a single confident-looking number.
            return f"{text}; {others} further sample(s) carry no usable fraction -- see geper_results.json."
        return text

    # No sample carries a number. The count is not what a reader needs
    # here -- that there is no fraction anywhere is -- so it is not
    # mentioned, and an "all not_run" run must not be dressed up as a
    # failure (or the reverse).
    errored = {name: s for name, s in states.items() if s.get("status") == _StageStatus.ERROR.value}
    if errored:
        reasons = sorted({(s.get("reason") or "No reason recorded.") for s in errored.values()})
        return f"Could not be determined for any sample -- {' '.join(reasons)}"
    reasons = sorted({(s.get("reason") or VAF_NO_SAMPLE_COLUMNS_REASON) for s in states.values()})
    return " ".join(reasons)


def _variant_reviewer_flags(variant_result: Dict[str, Any], clinical: Optional[Dict[str, Any]]) -> List[str]:
    """
    Short, plain-language reasons this one variant's finding may need a
    reviewer's attention before sign-off -- deliberately narrow (not every
    caveat the full report carries, e.g. routine "not yet scored" pending
    states are left to the detailed sections) so this stays a genuine
    signal on a 30-second skim, not noise.

      - No clinical interpretation could be built at all (a data gap, not
        a benign finding -- see `_build_variant_section`'s identical
        framing for the full-detail section).
      - A real (Minor/Moderate/Major/Critical) evidence conflict was
        detected -- `clinical_report["conflict_resolution"]["severity"]`,
        the same field the Conflict Resolution Engine (Phase 6) computes.
        "Critical" means GEPER's own classification disagrees with an
        expert-panel/practice-guideline ClinVar record for this exact
        variant -- see `pipeline/conflict_resolution_engine.py::
        _expert_panel_disagreement_conflict`.
      - Gene resolution came back genuinely ambiguous (multiple candidate
        genes overlap this position and could not be disambiguated) --
        see `pipeline/orchestrator.py::GeperPipeline._with_gene_resolution_context`
        and `pipeline/clingen/utils.py::GeneResolutionStatus`. Checked
        against the ClinGen and transcript-structure stages, the two
        stages that surface this status onto their own result dict.
    """
    if not clinical:
        return ["No clinical interpretation available"]

    flags: List[str] = []

    severity = (clinical.get("conflict_resolution") or {}).get("severity")
    if severity in ("Minor", "Moderate", "Major", "Critical"):
        flags.append(f"Conflicting evidence ({severity})")

    for stage_key in ("clingen", "transcript"):
        if (variant_result.get(stage_key) or {}).get("gene_resolution_status") == "ambiguous":
            flags.append("Ambiguous gene resolution")
            break

    return flags


def _consent_value_label(value: Optional[bool]) -> str:
    """ "Yes"/"No"/"Not stated" -- matches the "Not provided" convention `_build_patient_header_table` already uses for a missing DOB/Gender/Physician, applied to a tri-state (True/False/None) field instead of a missing string."""
    if value is None:
        return "Not stated"
    return "Yes" if value else "No"


# ---------------------------------------------------------------------------
# Run-level QC metrics -- shared vocabulary, parsing and thresholds
#
# Relocated here from `report/summary.py` (A7 fix). None of this is
# reportlab-specific: it is the labels, units, PASS/WARNING thresholds and
# the validated `{status, value, reason}` shape that EVERY renderer needs in
# order to describe the same run the same way. While it lived in the PDF
# module the Markdown report could not reach it without importing a renderer,
# so it rendered no QC at all -- the A7 gap. Content routed through this
# module reaches every renderer; content each renderer implements for itself
# drifts, which is the rule the caveat-parity work established.
#
# Each renderer still applies its own presentation on top (reportlab tables in
# summary.py, a Markdown table in report_generator.py) -- what is shared is
# plain data and plain prose, never a pre-wrapped fragment.
# ---------------------------------------------------------------------------

# Display units for each QC metric -- not a "mock" value (renamed from
# `_MOCK_QC_METRICS`, report review round 7: the old name predated the
# honest not-supplied/not-applicable rendering and had stopped
# describing what this dict is actually used for, which is only ever
# the unit suffix below).
_QC_METRIC_UNITS: Dict[str, str] = {
    "mean_coverage_depth": "x",
    "bases_at_20x": "%",
    "q30_score": "%",
}

_QC_METRIC_LABELS = {
    "mean_coverage_depth": "Mean Coverage Depth",
    "bases_at_20x": "Bases at >20x Coverage",
    "q30_score": "Q30 Score",
}

_QC_METRIC_ORDER = ("mean_coverage_depth", "bases_at_20x", "q30_score")


def _qc_threshold_pass_min(metric_key: str) -> float:
    """PASS/WARNING threshold for one QC metric -- see QCReportConfig's docstring (config.py) for why these are placeholders, configurable via GEPER_QC_* env vars."""
    return {
        "mean_coverage_depth": CONFIG.qc_report.MEAN_COVERAGE_DEPTH_PASS_MIN,
        "bases_at_20x": CONFIG.qc_report.BASES_AT_20X_PASS_MIN,
        "q30_score": CONFIG.qc_report.Q30_SCORE_PASS_MIN,
    }[metric_key]


def _qc_status(metric_key: str, value: float) -> str:
    return "PASS" if value >= _qc_threshold_pass_min(metric_key) else "WARNING"


# ---------------------------------------------------------------------------
# QC metrics parsing (report review round 7)
#
# Three states, not two: kim_pipeline genuinely measured a value
# (FOUND), GEPER was invoked in VCF-only mode so no run-level QC could
# ever exist (NOT_RUN, "not applicable" -- not a gap), or an upstream
# step was attempted and failed / a tool to produce this metric simply
# doesn't exist yet (also NOT_RUN or ERROR depending on which, each
# with its own `reason`). Reuses `pipeline.stage_schemas.StageStatus`
# rather than inventing a fourth status vocabulary in this codebase --
# NOT_FOUND is deliberately unused here (a QC metric either was
# measured or wasn't; there's no "checked, confirmed absent" reading
# for a number the way there is for a database lookup).
# ---------------------------------------------------------------------------

_QC_METRICS_NOT_APPLICABLE_REASON = (
    "This report was generated directly from a VCF (no --qc-metrics-json was supplied), so GEPER "
    "never ran or observed any upstream sequencing/alignment step for this sample -- there is no "
    "run-level QC to show here, not merely an unreported one."
)

_VALID_QC_STATUSES = {_StageStatus.NOT_RUN.value, _StageStatus.ERROR.value, _StageStatus.FOUND.value}


def _parse_one_qc_metric(key: str, entry: Any, unrecognized_keys: tuple = ()) -> Dict[str, Any]:
    """
    Validates one `qc_metrics[key]` entry against the required
    `{"status", "value", "reason"}` shape. This is the single choke
    point a raw number must pass through before `_qc_status`'s `>=`
    comparison can ever see it -- anything that doesn't parse into
    exactly this shape (a bare float where the dict belongs, a missing
    or unrecognized `status`, a `status: "found"` with no usable
    numeric `value`) fails THIS metric closed to ERROR rather than
    being coerced into a number to compare against a threshold. This
    is what makes the B-series fabricated-PASS-on-placeholder-numbers
    regression structurally impossible here, not merely avoided by
    convention: there is no code path from an untrusted input to
    `_qc_status` that skips this validation.

    `unrecognized_keys`: keys the caller's raw `qc_metrics` dict carried
    that are none of `_QC_METRIC_ORDER` (2026-09-11 ruling,
    PHASE8-bij-interpret, option B). When THIS key is absent (`entry is
    None`) and the caller's submission was non-empty but used a name
    GEPER doesn't recognize -- e.g. an external submitter's
    `{"depth": 50}` -- "not reported by the upstream pipeline" would be
    a FALSE SENTENCE: something WAS reported, just not under a
    recognized name. Say so honestly instead. Safety net beneath option
    A's boundary validation (geper/api/submission_worker.py::
    _qc_metrics_validated), which rejects an unrecognized key outright
    for any caller routed through that boundary -- this covers any
    OTHER caller that reaches this function directly.
    """
    if entry is None:
        if unrecognized_keys:
            return {
                "status": _StageStatus.NOT_RUN.value,
                "value": None,
                "reason": (
                    f"qc_metrics was submitted but '{key}' was reported but not recognised -- "
                    f"the submission used unrecognized key(s) {list(unrecognized_keys)!r} instead "
                    f"of one of {list(_QC_METRIC_ORDER)!r}."
                ),
            }
        return {
            "status": _StageStatus.NOT_RUN.value,
            "value": None,
            "reason": "Not reported by the upstream sequencing/alignment pipeline for this run.",
        }
    if not isinstance(entry, dict):
        logger.warning(
            f"qc_metrics['{key}'] was not an object ({entry!r}); a bare number is never accepted here -- "
            "a real value must be wrapped in {'status': 'found', 'value': ..., 'reason': ...}. Rendering "
            "as a failed measurement rather than trusting an unvalidated number."
        )
        return {
            "status": _StageStatus.ERROR.value,
            "value": None,
            "reason": f"Malformed qc_metrics entry for '{key}': expected an object, got {type(entry).__name__}.",
        }

    status = entry.get("status")
    if status not in _VALID_QC_STATUSES:
        logger.warning(f"qc_metrics['{key}']['status']={status!r} is not a recognized status; treating as ERROR.")
        return {
            "status": _StageStatus.ERROR.value,
            "value": None,
            "reason": entry.get("reason") or f"Unrecognized status {status!r} reported for '{key}'.",
        }

    reason = entry.get("reason")
    if status == _StageStatus.FOUND.value:
        value = entry.get("value")
        # bool is an int subclass -- excluded explicitly so a stray
        # `"value": true` can never be silently read as `1.0`.
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            logger.warning(
                f"qc_metrics['{key}'] status was 'found' but 'value' was not a real number ({value!r}); "
                "treating as ERROR rather than coercing it."
            )
            return {
                "status": _StageStatus.ERROR.value,
                "value": None,
                "reason": f"'{key}' was reported found but carried no usable numeric value.",
            }
        return {"status": _StageStatus.FOUND.value, "value": float(value), "reason": reason}

    # NOT_RUN / ERROR: `value` is deliberately discarded even if present
    # -- only a FOUND status may ever hand a number to `_qc_status`.
    return {"status": status, "value": None, "reason": reason}


def _parse_qc_metrics(qc_metrics: Optional[Union[Dict[str, Any], str]]) -> Dict[str, Dict[str, Any]]:
    """
    Accepts a dict (already-parsed, e.g. from an in-process caller), a
    path to a JSON sidecar file (see `bridge/combined_pipeline.py`'s
    translation of kim_pipeline's `checkpoint.json` into this shape),
    or `None`. Always returns exactly `_QC_METRIC_ORDER`'s three keys,
    each mapped to a validated `{"status", "value", "reason"}` dict --
    never a partial result, so `_build_qc_flowables` never has to guess
    whether a missing key means "not applicable" or "forgot to check".

    `None` (no `--qc-metrics-json` was ever passed to `generate_pdf` --
    the default for `main.py --vcf` today, and for every fixture/Colab
    run so far) is NOT "missing data to apologize for". A hospital
    handing GEPER a bare VCF has no run-level QC GEPER could ever have
    computed -- GEPER never touched their FASTQ or BAM. Every metric is
    therefore explicitly NOT_RUN with a reason saying exactly that,
    distinct from a kim_pipeline-combined run where a metric was
    genuinely attempted and failed (ERROR), or where the tool to
    produce it has never been wired at all (also NOT_RUN, but with a
    per-metric reason naming the missing step -- see
    `bridge/combined_pipeline.py`'s fixed reason for `bases_at_20x`,
    which has no computation anywhere in kim_pipeline as of this round).

    A missing file, corrupt JSON, or non-object body all fall back to
    the same all-NOT_RUN default as `None` (logged for operators, never
    raised) -- a malformed sidecar must never look like a real
    measurement any more than an absent one would.
    """
    not_applicable = {
        key: {"status": _StageStatus.NOT_RUN.value, "value": None, "reason": _QC_METRICS_NOT_APPLICABLE_REASON}
        for key in _QC_METRIC_ORDER
    }
    if not qc_metrics:
        return not_applicable

    try:
        if isinstance(qc_metrics, dict):
            raw = qc_metrics
        else:
            with open(qc_metrics, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        if not isinstance(raw, dict):
            raise ValueError("qc_metrics JSON must be an object")
    except (OSError, ValueError, TypeError) as exc:
        logger.warning(f"Could not parse qc_metrics ({exc}); rendering the not-applicable default instead.")
        return not_applicable

    unrecognized_keys = tuple(k for k in raw if k not in _QC_METRIC_ORDER)
    return {
        key: _parse_one_qc_metric(key, raw.get(key), unrecognized_keys=unrecognized_keys) for key in _QC_METRIC_ORDER
    }
