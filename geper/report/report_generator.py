"""
Human-readable report generator.

Turns the unified JSON output into a clean Markdown report suitable
for a researcher or clinician to skim, with embeddings and other raw
numeric payloads deliberately summarized (not dumped) for readability.
"""

from typing import Any, Dict, List, Optional

from annotation.thousand_genomes_sas import DIASPORA_DISCLOSURE, SAMPLE_SIZE_DISCLOSURE
from report.clinical_report_builder import (
    candidate_interpretation_of,
    _QC_METRIC_LABELS,
    _QC_METRIC_ORDER,
    _QC_METRIC_UNITS,
    _parse_qc_metrics,
    _qc_status,
    _qc_threshold_pass_min,
    ACMG_METHODOLOGY_STATEMENT,
    EVIDENCE_COMPLETENESS_CAPTION,
    ISO_RESEARCH_ELEMENT,
    RESEARCH_USE_DISCLAIMER,
    _consent_value_label,
    _offline_sources_caveat_text,
    _variant_hgvs_or_locus,
    _variant_reviewer_flags,
    variant_allele_fraction_text,
)
from utils.logger import get_logger
from utils.timezone_utils import format_ist_from_iso
from pipeline.acmg_rules import mtdna_interpretation_disclaimer
from pipeline.hgvs_utils import is_mitochondrial_chrom
from pipeline.models.status import DISABLED, FAILED, SKIPPED, USED, render_status_table_lines
from pipeline.provenance import RETRIEVAL_MODE_LABELS
from pipeline.stage_schemas import StageStatus as _StageStatus

logger = get_logger(__name__)

# Shared with `report/summary.py`'s `_MODEL_STATUS_LABELS` so the
# Markdown and PDF "AI model checkpoints" sections never drift on
# wording -- same status vocabulary `pipeline/models/status.py` already
# defines for the per-variant AI Models table (F1a, report review
# round 4), reused here rather than a third vocabulary.
_MODEL_STATUS_LABELS = {
    USED: "ran this run",
    FAILED: "attempted, failed to load/run this run",
    DISABLED: "not available in this environment",
    SKIPPED: "not applicable to any variant this run",
}


def _render_1000_genomes_sas_markdown(ipf: Dict[str, Any]) -> List[str]:
    """
    Renders the 1000 Genomes SAS section -- the SOLE Indian/South-Asian
    cohort source as of 2026-08-08 (see `report/clinical_report_builder.py
    ::_indian_population_frequency`; IndiGenomes was retired from
    GEPER's active query path, see `DATA_SOURCE_LICENSE_AUDIT.md`).
    Called whenever `ipf["sas_shown"]` is True -- in real pipeline
    operation, always (the stage now runs unconditionally for every
    variant). Both mandatory disclosures (`SAMPLE_SIZE_DISCLOSURE`/
    `DIASPORA_DISCLOSURE`) are always rendered whenever this section has
    anything at all to show -- found, not found, or errored -- never
    only on the "found" path, since a reader seeing this source
    mentioned at all needs to know its limitations regardless of
    outcome.
    """
    lines = ["- **1000 Genomes (South Asian, SAS):**"]
    if ipf.get("sas_available"):
        pooled = ipf.get("sas_pooled") or {}
        if pooled.get("af") is not None:
            lines.append(f"    - Pooled SAS: AF={pooled.get('af')} (AC={pooled.get('ac')}, AN={pooled.get('an')})")
        labels = ipf.get("sas_population_labels") or {}
        sizes = ipf.get("sas_sample_sizes") or {}
        for code, sub in (ipf.get("sas_sub_populations") or {}).items():
            label = labels.get(code, code)
            n = sizes.get(code)
            lines.append(f"    - {code} ({label}, n={n}): AF={sub.get('af')} (AC={sub.get('ac')}, AN={sub.get('an')})")
    elif ipf.get("sas_error"):
        lines.append(f"    - _lookup failed (external service issue: {ipf['sas_error']})._")
    else:
        lines.append("    - Variant not found in this source.")
    lines.append(f"    - ⚠ {SAMPLE_SIZE_DISCLOSURE}")
    lines.append(f"    - ⚠ {DIASPORA_DISCLOSURE}")
    return lines


# Markdown presentation only -- the claim itself lives in
# Design ruling 2026-09-03: ISO statement is now a separate element.
# Each renderer composes the two pieces independently, eliminating per-renderer
# transformations that had no enforcement. This is the same fix as the shared-function
# ruling: one definition, consumers that cannot diverge.
_DISCLAIMER = f"> **Disclaimer:** {RESEARCH_USE_DISCLAIMER}\n>\n> {ISO_RESEARCH_ELEMENT}"

# Governance control (round 30 part 2): a one-line review-status banner,
# sourced from `geper_results.json`'s own `review_status` field (see
# `report/json_builder.py`) so Markdown never has to independently
# decide draft/reviewed/overridden -- it just reports whatever
# `review/signoff.py::approve()`/`override()` already wrote there.
#
# Deliberately NOT a second formal signature surface (that decision is
# explicit for this round): no name/registration-number/hospital
# fields, no visual sign-off block matching
# `report/summary.py::_build_signoff_block`'s PDF-only two-role table.
# The "reviewed"/"overridden" banners below say so explicitly ("see the
# signed PDF report...") precisely so this banner is never mistaken for
# carrying the same legal weight the PDF's footer/sign-off block does.
_REVIEW_STATUS_DRAFT_BANNER = "> **DRAFT — NOT FOR PATIENT USE — AWAITING CLINICAL REVIEW**"
_REVIEW_STATUS_REVIEWED_BANNER_TEMPLATE = (
    "> **REVIEWED** by {reviewed_by} on {reviewed_at}. This banner is informational only -- see the signed "
    "PDF report for the formal clinical sign-off."
)
_REVIEW_STATUS_OVERRIDDEN_BANNER = (
    "> **OVERRIDDEN — NOT FOR PATIENT USE — AWAITING CLINICAL REVIEW.** A clinician has applied a "
    "classification override to at least one finding in this run since it was last reviewed; a fresh "
    "sign-off is required before this run is ready for use. See the signed PDF report and the audit log "
    "for details."
)


def _render_review_status_banner(json_document: Dict[str, Any]) -> str:
    """
    One-line review-status banner text for `json_document`, sourced
    from its `review_status` field. Any value other than the two
    non-draft states this pipeline actually writes -- `"reviewed"` /
    `"overridden"` (see `review/signoff.py`) -- including `"draft"`
    itself, a missing key (any pre-round-30 file), `None`, or an
    unrecognized string, renders as the DRAFT banner: the same
    fail-toward-the-less-trusting-claim discipline
    `JSONResultBuilder.run_complete`'s own docstring already
    establishes for exactly this reason -- a report nobody has actually
    signed off on must never look reviewed/overridden just because a
    field is absent or unexpected.
    """
    status = json_document.get("review_status")
    if status == "reviewed":
        return _REVIEW_STATUS_REVIEWED_BANNER_TEMPLATE.format(
            reviewed_by=json_document.get("reviewed_by") or "an unrecorded reviewer",
            reviewed_at=json_document.get("reviewed_at") or "an unrecorded time",
        )
    if status == "overridden":
        return _REVIEW_STATUS_OVERRIDDEN_BANNER
    return _REVIEW_STATUS_DRAFT_BANNER


def _render_qc_metrics(json_document: Dict[str, Any]) -> List[str]:
    """
    Run-level sequencing/alignment QC (A7 fix).

    This section did not exist. QC only ever reached the PDF, as a
    `generate_pdf(qc_metrics=...)` argument, so this renderer had nothing
    to read and printed nothing -- not even an omission note. In a report
    where "no QC section" is indistinguishable from "this run had no QC",
    that silence is the defect: the full PDF showed a real coverage/Q30
    table for the same run this file rendered blank.

    Now QC lives on the document (`json_document["qc_metrics"]`), so both
    renderers describe the same run from the same source. The labels,
    units, thresholds and PASS/WARNING rule come from
    `report/clinical_report_builder.py` rather than being restated here,
    so the two tables cannot drift into disagreeing about what "PASS"
    means.

    The honest-absence contract is preserved exactly as the PDF states
    it: a run with no `--qc-metrics-json` renders every metric as "Not
    applicable" and prints the per-metric reasons, which say GEPER never
    observed an upstream sequencing step -- a positive true statement,
    not a gap to apologise for. `_parse_qc_metrics(None)` produces that
    default, so this function never has to decide what missing means.
    """
    qc_metrics = _parse_qc_metrics((json_document or {}).get("qc_metrics"))

    lines: List[str] = ["## Run Quality Control", ""]
    lines.append("| Metric | Value | Threshold | Status |")
    lines.append("| --- | --- | --- | --- |")

    not_run_entries: List[Any] = []
    error_entries: List[Any] = []

    for key in _QC_METRIC_ORDER:
        label = _QC_METRIC_LABELS[key]
        unit = _QC_METRIC_UNITS[key]
        threshold = _qc_threshold_pass_min(key)
        entry = qc_metrics.get(key) or {}
        status = entry.get("status")
        value = entry.get("value")

        if status == _StageStatus.FOUND.value and isinstance(value, (int, float)) and not isinstance(value, bool):
            pass_status = _qc_status(key, value)
            lines.append(f"| {label} | {value:g}{unit} | {threshold:g}{unit} | {pass_status} |")
        elif status == _StageStatus.ERROR.value:
            error_entries.append((label, entry))
            lines.append(f"| {label} | Measurement failed | {threshold:g}{unit} | ERROR |")
        else:
            # Same collapse the PDF makes: NOT_RUN and anything that failed
            # validation render identically, because `_parse_qc_metrics` has
            # already turned malformed input into a validated status.
            not_run_entries.append((label, entry))
            lines.append(f"| {label} | Not applicable | {threshold:g}{unit} | N/A |")

    lines.append("")

    # Reasons are grouped by the exact reason string rather than by a fixed
    # enum, for the reason the PDF states: callers pass their own reason
    # through verbatim, so the set is open-ended.
    for heading, entries in (("Not applicable this run", not_run_entries), ("Measurement failed", error_entries)):
        if not entries:
            continue
        grouped: Dict[str, List[str]] = {}
        for label, entry in entries:
            reason = entry.get("reason") or "No reason recorded."
            grouped.setdefault(reason, []).append(label)
        clauses = "; ".join(f"{', '.join(labels)} -- {reason}" for reason, labels in grouped.items())
        lines.append(f"*{heading}: {clauses}*")
        lines.append("")

    return lines


def _render_consent_line(json_document: Dict[str, Any]) -> Optional[str]:
    """
    One-line DPDP Act 2023 consent statement, or `None` when no consent
    object was supplied.

    Returns `None` rather than a "Not stated" line in that case, matching
    `report/summary.py::_consent_rows`' contract exactly: a run with no
    consent data at all must not manufacture the appearance of a
    compliance record. Within a consent object that WAS supplied, an
    individual unset permission still renders honestly as "Not stated"
    (via the shared `_consent_value_label`) -- absent-object and
    absent-field are different facts and stay different here.
    """
    consent = json_document.get("patient_consent")
    if not consent:
        return None
    parts = [
        f"Clinical reporting: {_consent_value_label(consent.get('clinical_reporting'))}",
        f"Research use: {_consent_value_label(consent.get('research'))}",
    ]
    timestamp = consent.get("timestamp")
    if timestamp:
        parts.append(f"Recorded: {timestamp}")
    return "**Data processing consent (DPDP Act 2023):** " + " · ".join(parts)


class ReportGenerator:
    """Builds a Markdown report from a GEPER JSON result document."""

    def generate(self, json_document: Dict[str, Any]) -> str:
        lines: List[str] = []
        lines.append("# Bij AI Variant Analysis Report")
        lines.append("")
        lines.append(_render_review_status_banner(json_document))
        lines.append("")
        # Displayed in IST (report is for Indian hospitals); the
        # stored `generated_at` itself stays UTC (see
        # report/json_builder.py) -- only this human-facing line
        # converts it. See utils/timezone_utils.py.
        lines.append(f"**Generated:** {format_ist_from_iso(json_document.get('generated_at'))}")
        lines.append(f"**Input VCF:** `{json_document.get('input_vcf')}`")
        lines.append(f"**Variants analyzed:** {json_document.get('variant_count')}")
        consent_line = _render_consent_line(json_document)
        if consent_line:
            lines.append(consent_line)
        lines.append("")
        lines.append(_DISCLAIMER)
        lines.append("")
        # Run-level caveats live together, immediately below the
        # disclaimer that is itself one of them -- a reader who needs to
        # know what qualifies this whole run finds all of it in one
        # place, before any finding. The per-variant sections below
        # carry their own caveats and are unaffected.
        lines.append(f"*{ACMG_METHODOLOGY_STATEMENT}*")
        lines.append("")
        lines.append("---")
        lines.append("")

        lines.extend(self._render_reviewer_attention(json_document))
        lines.append("---")
        lines.append("")

        lines.extend(self._render_provenance(json_document))
        lines.append("---")
        lines.append("")

        lines.extend(self._render_data_freshness_warnings(json_document))
        lines.append("---")
        lines.append("")

        # Run-level, like provenance above: describes the whole run rather
        # than one finding, so it belongs before the per-variant sections.
        lines.extend(_render_qc_metrics(json_document))
        lines.append("---")
        lines.append("")

        case_ranking = self._render_case_phenotype_ranking(json_document)
        if case_ranking:
            lines.extend(case_ranking)
            lines.append("---")
            lines.append("")

        for idx, variant_result in enumerate(json_document.get("variants", []), start=1):
            lines.extend(self._render_variant_section(idx, variant_result))
            lines.append("---")
            lines.append("")

        return "\n".join(lines)

    def write(self, json_document: Dict[str, Any], output_path: str) -> str:
        content = self.generate(json_document)
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        logger.info(f"Wrote Markdown report to '{output_path}'.")
        return output_path

    @staticmethod
    def _render_reviewer_attention(json_document: Dict[str, Any]) -> List[str]:
        """
        Run-level triage: the short list of things a reviewer should look
        at before sign-off, mirroring the full PDF's own "Reviewer
        Attention" section (`report/summary.py`) so the two reports flag
        the same findings for the same reasons.

        Two sources, both shared rather than reimplemented here:
        per-finding flags from `_variant_reviewer_flags` (evidence
        conflicts, ambiguous gene resolution, findings with no clinical
        interpretation at all), and the run-level offline-sources caveat
        from `_offline_sources_caveat_text` -- which matters most of all,
        because a source that was never queried must never be mistaken
        for one queried and found empty.

        Always renders, including when nothing is flagged: "nothing to
        review" is a finding a reviewer needs stated, not an absence they
        should have to infer from a missing section.

        Narrower than the PDF's version in one respect: that one also
        lists data sources whose version could not be pinned down
        (`report/summary.py::_provenance_gap_sources`). That helper and
        the constant it reads were left in the PDF renderer as out of
        scope for this change, so they are not reflected here yet.
        """
        lines = ["## Reviewer Attention", ""]
        attention: List[str] = []
        for idx, variant_result in enumerate(json_document.get("variants", []), start=1):
            flags = _variant_reviewer_flags(variant_result, candidate_interpretation_of(variant_result))
            if flags:
                v = variant_result.get("variant") or {}
                locus = f"{v.get('chrom')}:{v.get('pos')} {v.get('ref')}>{v.get('alt')}"
                attention.append(f"Finding {idx} ({locus}): {'; '.join(flags)}")
        offline_caveat = _offline_sources_caveat_text(json_document)
        if offline_caveat:
            attention.append(offline_caveat)

        if attention:
            lines.extend(f"- **!** {item}" for item in attention)
        else:
            lines.append("*No conflicts, ambiguous gene resolution, or unreachable data sources flagged for this run.*")
        lines.append("")
        return lines

    @staticmethod
    def _render_provenance(json_document: Dict[str, Any]) -> List[str]:
        """
        Data-source version pinning / run provenance (pipeline/provenance.py)
        -- the section that makes this report reproducible later: GEPER's
        own code version, the AI model checkpoints this run used, and
        every external data source's version/hash/timestamp. Run-level
        (once per report, not per-variant) -- placed before the per-
        variant sections since it describes the whole run, not one
        finding.

        `status` labels are rendered honestly, not smoothed over: a
        source this run never consulted says exactly that (NOT the same
        as "no version available", which gets its own distinct label --
        see `pipeline/provenance.py::VersionStatus`'s own docstring for
        why collapsing the two would repeat the raw_evidence bug class).
        """
        lines = ["## Data Source Provenance", ""]
        lines.append(
            "*Recorded for reproducibility: if this report needs to be reproduced later, "
            "the exact data-source versions and code version below are what to match.*"
        )
        lines.append("")
        lines.append(f"- **Bij AI code version:** `{json_document.get('code_version') or 'unknown'}`")
        lines.append("")

        checkpoints = json_document.get("model_checkpoints") or {}
        lines.append("**AI model checkpoints:**")
        lines.append("")
        # Round 17: `run_complete` is `False` (its honest default, and
        # what an absent key -- any pre-round-17 file -- also reads as
        # via `bool(...)`, never a bare `.get(..., True)`) for a
        # geper_results.json left behind by a run that died mid-loop.
        # Every checkpoint below is then still the bare config
        # identifier `pipeline/provenance.py::get_model_checkpoint_
        # identifiers` set at startup -- correct data, but rendering it
        # with no caveat reads as "this model's status is unremarkable",
        # not "unknown", which is the actual honest claim
        # (ROUND_CANDIDATES.md, round 12).
        if not bool(json_document.get("run_complete")):
            lines.append(
                "_This run did not complete (no post-loop status enrichment was recorded) -- the "
                "identifiers below are configuration only. Whether each model actually ran, failed to "
                "load, or was skipped this run is not yet known._"
            )
            lines.append("")
        if checkpoints:
            for name, value in sorted(checkpoints.items()):
                # `value` is either the enriched
                # {"identifier", "status", "reason"} shape
                # (`pipeline/provenance.py::finalize_model_checkpoint_provenance`,
                # F1a) once a run-level status was tracked for this
                # model, or a plain identifier string when it wasn't
                # (no per-variant status exists for that checkpoint
                # key, e.g. a run with zero variants) -- both render,
                # rather than assuming the enriched shape unconditionally.
                if isinstance(value, dict):
                    status_label = _MODEL_STATUS_LABELS.get(value.get("status"), value.get("status") or "unknown")
                    lines.append(f"- **{name}:** `{value.get('identifier')}` -- {status_label}")
                    if value.get("reason"):
                        lines.append(f"  - {value['reason']}")
                else:
                    lines.append(f"- **{name}:** `{value}`")
        else:
            lines.append("*No AI model checkpoint identifiers recorded for this run.*")
        lines.append("")

        provenance = json_document.get("provenance") or []
        lines.append("**External data sources:**")
        lines.append("")
        if not provenance:
            lines.append("*No data-source provenance was recorded for this run.*")
            lines.append("")
            return lines

        status_labels = {
            "not_consulted": "Not consulted this run",
            "unknown": "Consulted -- no version/hash could be determined",
            "timestamp_only": "Consulted -- no release version published; query time recorded",
            "hash_only": "Consulted -- content hash recorded, no release version published",
            "version_known": "Version known",
        }
        for record in provenance:
            status = record.get("status", "unknown")
            label = status_labels.get(status, status)
            lines.append(f"- **{record.get('source')}:** {label}")
            # A SECOND, independent axis: where this run's data actually
            # came from. Absent (None) for every source whose retrieval
            # mode is not tracked -- rendered as nothing at all rather
            # than as an implied "live", since claiming a source was
            # contacted when nobody checked is the failure this line
            # exists to fix.
            retrieval = record.get("retrieval")
            if retrieval:
                lines.append(f"  - Retrieval: {RETRIEVAL_MODE_LABELS.get(retrieval, retrieval)}")
            if record.get("version"):
                lines.append(f"  - Version: {record['version']}")
            if record.get("release_date"):
                lines.append(f"  - Release date: {record['release_date']}")
            if record.get("content_hash"):
                lines.append(
                    f"  - Content hash ({record.get('hash_algorithm') or 'unknown algorithm'}): `{record['content_hash']}`"
                )
            if record.get("query_timestamp"):
                lines.append(f"  - Query/download time (UTC): {record['query_timestamp']}")
            if record.get("endpoint"):
                lines.append(f"  - Endpoint: {record['endpoint']}")
            if record.get("notes"):
                lines.append(f"  - _{record['notes']}_")
        lines.append("")
        return lines

    @staticmethod
    def _render_data_freshness_warnings(json_document: Dict[str, Any]) -> List[str]:
        """
        Packaging Part 3 mechanism: surfaces every stale-cache fallback
        this run hit (`pipeline/provenance.py::record_stale_fallback`,
        called from the seven bootstrap modules' "Using stale cached
        ... after a failed refresh" branches). Run-level, next to
        `_render_provenance` above, for the same reason: it describes
        the whole run, not one finding.

        The standing rule this section exists to satisfy: a warning
        nobody sees is not a warning, and for THIS finding that bar is
        higher than a log level -- the reader needs to know an
        interpretation was built on a snapshot from a DATE, not just
        that a stale-cache event happened. `recorded_at` is therefore
        always rendered, never dropped for brevity.
        """
        lines = ["## Data Freshness Warnings", ""]
        warnings = json_document.get("data_freshness_warnings") or []
        if not warnings:
            lines.append(
                "*No data-source cache fell back to a stale copy this run -- every "
                "auto-fetched dataset this run consulted was either fresh or not consulted at all.*"
            )
            lines.append("")
            return lines

        lines.append(
            "*The following data source(s) could not be refreshed this run and this report "
            "was built using an existing, possibly-outdated local copy instead. This matters most for "
            "sources that can REVISE a past conclusion (e.g. ClinVar reclassification) -- a stale copy "
            "of those can make an interpretation actively wrong, not just incomplete.*"
        )
        lines.append("")
        for warning in warnings:
            lines.append(f"- **{warning.get('source')}:** stale cache used ({warning.get('reason')})")
            lines.append(f"  - Recorded stale at (UTC): {warning.get('recorded_at')}")
            if warning.get("path"):
                lines.append(f"  - Cache file: `{warning['path']}`")
        lines.append("")
        return lines

    @staticmethod
    def _render_case_phenotype_ranking(json_document: Dict[str, Any]) -> List[str]:
        """
        Case-level, HPO-phenotype-driven variant ranking (see
        `pipeline/case_prioritization.py`) -- run-level, like
        Provenance above, so it's placed immediately after it and
        before any per-variant section. Empty list (renders nothing)
        whenever the patient supplied no HPO terms this run, or ranking
        genuinely failed -- i.e. whenever no variant carries a
        `case_prioritization` key at all -- so a report from a run
        without `--hpo-terms`/`--phenotype-file` looks exactly as it
        did before this feature existed.

        Deliberately NOT titled/framed as ACMG or clinical evidence:
        this table is a reviewer-triage aid only (which variant to
        read first), never a classification signal -- the explicit
        callout below says so, and `## Candidate Interpretation`
        (per-variant, further down) is completely unaffected by
        anything here.
        """
        variants = json_document.get("variants", [])
        ranked = [
            (idx, vr) for idx, vr in enumerate(variants, start=1) if isinstance(vr.get("case_prioritization"), dict)
        ]
        if not ranked:
            return []

        ranked.sort(
            key=lambda pair: (
                pair[1]["case_prioritization"].get("case_rank") is None,
                pair[1]["case_prioritization"].get("case_rank") or 0,
            )
        )

        lines = ["## Case-Level Phenotype-Driven Variant Ranking", ""]
        lines.append(
            "*Reviewer triage aid only -- ranks variants by how well their gene's HPO-curated "
            "phenotype profile matches the patient's observed symptoms, combined with each "
            "variant's existing priority score. This is a SEPARATE, additive signal: it never "
            "influences ACMG classification, PP4, confidence, or priority score below -- see "
            'each variant\'s own "Candidate Interpretation" section for those.*'
        )
        lines.append("")
        lines.append("| Case Rank | Finding | Variant / Gene | Case Score | Phenotype Match | Why |")
        lines.append("|---|---|---|---|---|---|")
        for idx, vr in ranked:
            cp = vr["case_prioritization"]
            variant = vr.get("variant", {})
            locus = f"{variant.get('chrom')}:{variant.get('pos')} {variant.get('ref')}>{variant.get('alt')}"
            gene = (vr.get("interpretation_result") or {}).get("gene_symbol")
            locus_gene = f"{locus} ({gene})" if gene else locus

            case_rank = cp.get("case_rank")
            rank_text = str(case_rank) if case_rank is not None else "unranked"
            case_score = cp.get("case_rank_score")
            score_text = f"{case_score:.1f}" if isinstance(case_score, (int, float)) else "n/a"

            pm = cp.get("phenotype_match") or {}
            pm_score = pm.get("score")
            if pm_score is not None:
                pm_text = f"{pm_score:.1f}/100"
            else:
                pm_text = "no HPO data for gene"

            top_matches = [m for m in (pm.get("term_matches") or []) if m.get("similarity", 0) > 0]
            top_matches.sort(key=lambda m: -m.get("similarity", 0))
            if top_matches:
                why = "; ".join(
                    f"{m['patient_term_id']} -> {m.get('matched_gene_term_name') or m.get('matched_gene_term_id')} "
                    f"({m['match_type']}, {m['similarity']:.2f})"
                    for m in top_matches[:3]
                )
            else:
                why = cp.get("reason") or "n/a"

            lines.append(f"| {rank_text} | {idx} | {locus_gene} | {score_text} | {pm_text} | {why} |")
        lines.append("")
        return lines

    def _render_variant_section(self, idx: int, result: Dict[str, Any]) -> List[str]:
        variant = result.get("variant", {})
        lines: List[str] = []

        # Card T3-F4: the coordinate string is retained (it is the
        # precise identifier) and HGVS is added alongside it (the
        # identifier clinicians actually use, and the one the short PDF
        # heading is built from -- see `summary_short.py::
        # _build_variant_block`) via the same shared fallback helper
        # both PDFs and the short PDF now call, so all three degrade the
        # same way when neither hgvs_c nor hgvs_g is available (the
        # helper falls back to the locus itself in that case, so the
        # parenthetical below will then literally repeat the coordinate
        # string just shown -- an accepted, intentional consequence of
        # one shared fallback rather than a fourth, renderer-specific
        # special case).
        header = (
            f"## Variant {idx}: {variant.get('chrom')}:{variant.get('pos')} {variant.get('ref')}>{variant.get('alt')}"
            f" ({_variant_hgvs_or_locus(result)})"
        )
        lines.append(header)
        lines.append("")
        lines.append(f"- **Type:** {variant.get('variant_type')}")
        lines.append(f"- **VCF ID:** {variant.get('id') or 'n/a'}")
        lines.append(f"- **Filter status:** {variant.get('filter') or 'n/a'}")
        # Within-sample read support (NABL 112A s.7.8.5(b)(iii)) -- NOT the
        # gnomAD population allele frequency shown under Population
        # Evidence, which is a different quantity that shares the name.
        # Rendered through the shared helper (not the `or 'n/a'` idiom the
        # three lines above use) because this value is a float: `0.0 or
        # 'n/a'` would print a genuine 0% allele fraction as "n/a", and an
        # absence and a real zero are exactly what must stay apart here.
        lines.append(f"- **Allele fraction (this sample):** {variant_allele_fraction_text(result)}")
        lines.append("")

        # Round 14, B2: per-finding, not report-level -- a reviewer reads
        # findings, not preambles, and a chrM finding with a
        # classification looks complete to anyone who skipped the header.
        # Placed first, before any criteria/classification content.
        if is_mitochondrial_chrom(variant.get("chrom")):
            # Round 16: `not_evaluated_rules` (this finding's real
            # per-criterion breakdown) is passed so the disclaimer's
            # counts derive from the same data as the executive-summary
            # accounting / Limitations text below -- see
            # `mtdna_interpretation_disclaimer`'s own docstring for why
            # the two surfaces used to be able to contradict each other.
            not_evaluated_rules = (result.get("interpretation_result") or {}).get("not_evaluated_rules", [])
            lines.append(f"> **{mtdna_interpretation_disclaimer(result.get('transcript'), not_evaluated_rules)}**")
            lines.append("")

        out_of_scope = result.get("out_of_scope")
        if out_of_scope:
            # Generic "never assessed at all" rendering: rendering the
            # normal Annotation Detail / audit-trail sections below for a
            # variant this run genuinely never attempted would show a wall
            # of "not found"/"skipped" boilerplate that reads as "checked,
            # nothing there" when the honest statement is "never checked,
            # by design". This is a distinct status, not a Variant of
            # Uncertain Significance and not a stage failure. No current
            # producer as of round 14, B2 (which replaced the
            # mitochondrial compartment's whole-variant rejection with
            # per-criterion gating -- see `pipeline/acmg_rules.py`) --
            # kept as reusable infrastructure for a future genuinely-
            # unassessable variant class.
            lines.append(f"**Status:** Out of scope ({out_of_scope.get('scope', 'unspecified')})")
            lines.append("")
            lines.append(out_of_scope.get("reason") or "This variant is out of scope for this Bij AI build.")
            lines.append("")
            lines.append(
                "*No ACMG/AMP criteria were evaluated for this variant. This is not a Variant of "
                "Uncertain Significance -- it was never assessed.*"
            )
            lines.append("")
            return lines

        lines.extend(self._render_ai_model_status(result.get("ai_model_status", {})))

        lines.extend(
            self._render_clinical_report(candidate_interpretation_of(result), result.get("interpretation", {}))
        )

        lines.append("### Annotation Detail (Audit Trail)")
        lines.append("")
        lines.append(
            "*The sections below show each pipeline stage's raw output, preserved for full "
            "traceability of every statement made in the clinical report above.*"
        )
        lines.append("")

        lines.extend(self._render_interpretation(result.get("interpretation", {})))
        lines.extend(self._render_dbsnp(result.get("dbsnp", {})))
        lines.extend(self._render_clinvar(result.get("clinvar", {})))
        lines.extend(self._render_dna_models(result.get("dna_model_results", {})))
        lines.extend(self._render_rna(result.get("rna_analysis", {})))
        lines.extend(self._render_protein(result.get("protein_analysis", {})))
        lines.extend(self._render_alphamissense(result.get("alphamissense", {})))
        lines.extend(self._render_mmsplice(result.get("mmsplice", {})))
        lines.extend(self._render_ai_splicing_ensemble(result.get("ai_splicing_ensemble", {})))
        lines.extend(self._render_gnomad(result.get("gnomad", {})))
        lines.extend(self._render_clingen(result.get("clingen", {})))
        lines.extend(self._render_uniprot(result.get("uniprot", {})))
        lines.extend(self._render_interpro(result.get("interpro", {})))
        lines.extend(self._render_alphafold(result.get("alphafold", {})))
        lines.extend(self._render_blast(result.get("blast", {})))

        errors = result.get("errors", [])
        if errors:
            lines.append("### ⚠ Stage Warnings / Errors")
            lines.append("")
            for err in errors:
                lines.append(f"- {err}")
            lines.append("")

        return lines

    @staticmethod
    def _render_clinical_report(clinical_report: Dict[str, Any], legacy_interpretation: Dict[str, Any]) -> List[str]:
        """
        Renders the Phase 5 clinician-facing report (17 sections) from
        the shared `build_clinical_report()` dict -- the same dict
        `report/json_builder.py` emits under the `clinical_report` JSON
        key, so Markdown and JSON can never disagree about what GEPER
        found. If the clinical report couldn't be built (interpretation
        aggregation failed for this variant), falls back to a plain
        note rather than fabricating sections with no data behind them.
        """
        if not clinical_report:
            return [
                "## Candidate Interpretation (requires clinician review)",
                "",
                "*Candidate interpretation unavailable for this variant (interpretation aggregation did not "
                "complete). See Annotation Detail below for whatever raw stage output is available.*",
                "",
            ]

        lines = ["## Candidate Interpretation (requires clinician review)", ""]

        lines.append("### 1. Executive Summary")
        lines.append("")
        lines.append(clinical_report["executive_summary"])
        lines.append("")

        acmg = clinical_report["acmg_classification"]
        lines.append("### 2. Final ACMG Classification")
        lines.append("")
        lines.append(f"**Classification:** {acmg.get('classification') or 'Not classified'}")
        lines.append("")
        # Report review round 10: the real Tavtigian point total next
        # to the classification it actually decided, with the
        # threshold band it landed in -- see `pipeline/acmg_rules.py::
        # CombineResult`'s docstring for why this was previously
        # discarded. `None` only for BA1's stand-alone-benign
        # short-circuit (no point tally ran) or an unclassified
        # variant -- neither fabricates a number.
        net_points = acmg.get("net_points")
        if net_points is not None:
            band = acmg.get("net_points_band")
            band_text = f" -- threshold band: {band}" if band else ""
            lines.append(
                f"**Net points (Tavtigian 2018):** {net_points:g} "
                f"(pathogenic {acmg.get('pathogenic_points', 0):g} − benign {acmg.get('benign_points', 0):g})"
                f"{band_text}"
            )
            lines.append("")
        # Clinician override (geper/review/signoff.py's "override" command)
        # -- layered on top of, never substituting for, GEPER's own
        # classification above: both are shown, explicitly labelled,
        # so a reader can never mistake one for the other. Absent for
        # every variant no clinician has overridden (the common case).
        override = acmg.get("clinician_override")
        if override:
            lines.append(
                f"> **Clinician override:** GEPER classification: {override.get('original_classification') or 'Not classified'}; "
                f"Clinician override: **{override.get('new_classification')}** -- {override.get('reason')} "
                f"(by {override.get('clinician_id')}, {override.get('timestamp')})"
            )
            lines.append("")
        if acmg.get("triggered_criteria"):
            lines.append("| Criterion | Strength | Direction | Rationale |")
            lines.append("|---|---|---|---|")
            for c in acmg["triggered_criteria"]:
                lines.append(f"| {c['code']} | {c['strength']} | {c['direction']} | {c['rationale']} |")
            lines.append("")
        # PVS1 decision-tree path (C5, report review round 2): PVS1's
        # own rationale text refers to "the decision tree" reaching its
        # strength -- render that tree's actual path rather than only
        # citing it (see `pipeline/pvs1/decision_tree.py::
        # PVS1DecisionTree.evaluate`'s `decision_path`).
        pvs1_entry = next(
            (
                c
                for c in (acmg.get("triggered_criteria") or []) + (acmg.get("not_triggered_criteria") or [])
                if c.get("code") == "PVS1" and c.get("details")
            ),
            None,
        )
        if pvs1_entry:
            decision_path = pvs1_entry["details"].get("decision_path") or []
            if decision_path:
                lines.append("<details><summary>PVS1 decision-tree path</summary>")
                lines.append("")
                for i, step in enumerate(decision_path, 1):
                    lines.append(f"{i}. {step}")
                lines.append("")
                lines.append("</details>")
                lines.append("")
        if acmg.get("combining_rule_trace"):
            lines.append("<details><summary>Combining-rule trace</summary>")
            lines.append("")
            for t in acmg["combining_rule_trace"]:
                lines.append(f"- {t}")
            lines.append("")
            lines.append("</details>")
            lines.append("")
        if acmg.get("not_evaluated_count"):
            lines.append(
                f"*{acmg['not_evaluated_count']} additional ACMG criteria could not be evaluated (see Limitations).*"
            )
            lines.append("")

        conf = clinical_report["confidence"]
        lines.append("### 3. Evidence Completeness")
        lines.append("")
        lines.append(
            f"*{EVIDENCE_COMPLETENESS_CAPTION} A variant with the strongest possible single line of "
            "evidence can still score Low here if unrelated categories don't apply to it (see the "
            "breakdown below).*"
        )
        lines.append("")
        if conf.get("pending"):
            lines.append("*Evidence completeness scoring did not complete for this variant.*")
        else:
            score = conf.get("score")
            lines.append(
                f"**{score:.1f}% -- {conf.get('label')}**"
                if isinstance(score, (int, float))
                else f"**{score}% -- {conf.get('label')}**"
            )
            breakdown = (conf.get("breakdown") or {}).get("category_breakdown", [])
            if breakdown:
                lines.append("")
                lines.append("| Evidence Category | Weight | Presence | Quality | Contribution |")
                lines.append("|---|---|---|---|---|")
                for cat in breakdown:
                    lines.append(
                        f"| {cat['category']} | {cat['weight']} | {cat['presence']} | {cat['quality']} | {cat['contribution']} |"
                    )
        lines.append("")

        pri = clinical_report["priority"]
        lines.append("### 4. Priority Score")
        lines.append("")
        if pri.get("pending"):
            lines.append("*Priority scoring did not complete for this variant.*")
        else:
            rank_text = f" (rank {pri['rank']} in this run)" if pri.get("rank") is not None else ""
            score = pri.get("score")
            score_text = f"{score:.1f}" if isinstance(score, (int, float)) else str(score)
            lines.append(f"**{pri.get('category')} -- score {score_text}{rank_text}**")
            if pri.get("explanation"):
                lines.append("")
                for reason in pri["explanation"]:
                    lines.append(f"- {reason}")
        lines.append("")

        lines.append("### 5. Supporting Evidence")
        lines.append("")
        supporting = clinical_report.get("supporting_evidence") or []
        if supporting:
            for item in supporting:
                text = item.get("text") if isinstance(item, dict) else item
                sources = ", ".join(item.get("sources", [])) if isinstance(item, dict) else None
                lines.append(f"- {text}" + (f" *({sources})*" if sources else ""))
        else:
            lines.append("*No supporting evidence recorded.*")
        lines.append("")

        lines.append("### 6. Conflicting Evidence")
        lines.append("")
        conflicting = clinical_report.get("conflicting_evidence") or []
        if conflicting:
            for item in conflicting:
                text = item.get("text") if isinstance(item, dict) else item
                sources = ", ".join(item.get("sources", [])) if isinstance(item, dict) else None
                lines.append(f"- {text}" + (f" *({sources})*" if sources else ""))
            lines.append("")

        # Phase 6: structured, resolved conflict detail. Rendered
        # additively below the existing free-text list above (which
        # Phase 5 already shipped) -- same underlying data, richer view.
        conflict_res = clinical_report.get("conflict_resolution") or {}
        lines.append(
            f"**Overall assessment:** {conflict_res.get('summary') or 'No significant conflicting evidence detected.'}"
        )
        lines.append("")
        real_conflicts = [
            c for c in (conflict_res.get("conflicts") or []) if c.get("severity") in ("Minor", "Moderate", "Major")
        ]
        if real_conflicts:
            lines.append(f"**Conflict score:** {conflict_res.get('score')} ({conflict_res.get('severity')})")
            lines.append("")
            lines.append("| Category | Conflict Type | Evidence A | Evidence B | Severity |")
            lines.append("|---|---|---|---|---|")
            for c in real_conflicts:
                ea = f"{c['evidence_a']['source']}: {c['evidence_a']['statement']}"
                eb = f"{c['evidence_b']['source']}: {c['evidence_b']['statement']}"
                lines.append(f"| {c['category']} | {c['conflict_type']} | {ea} | {eb} | {c['severity']} |")
            lines.append("")
            for c in real_conflicts:
                lines.append(f"<details><summary>{c['conflict_type']} -- resolution</summary>")
                lines.append("")
                lines.append(f"- **Resolution:** {c['resolution']}")
                lines.append(f"- **Rationale:** {c['resolution_rationale']}")
                lines.append(f"- **Confidence impact:** {c['confidence_impact']}")
                lines.append(f"- **Priority impact:** {c['priority_impact']}")
                lines.append("")
                lines.append("</details>")
                lines.append("")
            if conflict_res.get("resolution"):
                lines.append(f"*{conflict_res['resolution']}*")
                lines.append("")
        if not conflicting and not real_conflicts:
            lines.append("*No conflicting evidence detected across integrated sources.*")
            lines.append("")

        ai = clinical_report["ai_consensus"]
        lines.append("### 7. AI Consensus")
        lines.append("")
        votes = ai.get("classifying_models") or []
        model_errors = ai.get("model_errors") or []
        if votes:
            for v in votes:
                source = v.get("source")
                line = f"- **{source}:** {v.get('prediction')} (score={v.get('score')})"
                # Same caveat wording `acmg_rules.py::_pp3_bp4` established,
                # appended to this vote's own bullet rather than a separate
                # line -- each bullet here is a self-contained per-model
                # claim (MMSplice's bullet needs no AlphaMissense caveat),
                # so the caveat has to travel with the specific bullet it
                # qualifies, not sit once at the section level.
                if source == "AlphaMissense":
                    line += (
                        " -- not clinically validated; not approved for clinical use. This is a raw model "
                        "score, not a validated clinical pathogenicity measure."
                    )
                lines.append(line)
        elif not model_errors:
            lines.append("*No classifying AI model (AlphaMissense/MMSplice) produced a result.*")
        # `model_errors` is reported alongside `votes`, not only in its
        # `elif` -- one model can crash while the other still produces
        # a real verdict, and a crash must never be silently absent
        # just because the other model's result is present.
        for err in model_errors:
            lines.append(
                f"- **{err.get('source')}:** _lookup failed ({err.get('error')}) -- not evidence of no effect, see Annotation Detail below._"
            )
        context = ai.get("context_models_used") or []
        lines.append("")
        lines.append(
            f"*Sequence-context models used (routing/embedding only, no direct pathogenicity verdict): "
            f"{', '.join(context) if context else 'none'}.*"
        )
        lines.append("")

        prot = clinical_report["protein_knowledge"]
        lines.append("### 8. Protein Knowledge")
        lines.append("")
        if prot.get("uniprot_available"):
            u = prot["uniprot"]
            lines.append(
                f"- **UniProt:** {u.get('protein_name') or 'n/a'} ({u.get('accession') or 'n/a'}, {'reviewed' if u.get('reviewed') else 'unreviewed'})"
            )
        elif prot.get("uniprot_error"):
            lines.append(
                f"- **UniProt:** _lookup failed (external service issue: {prot['uniprot_error']}) -- not evidence of a missing entry, see Annotation Detail below._"
            )
        elif prot.get("uniprot_reason"):
            lines.append(f"- **UniProt:** {prot['uniprot_reason']}")
        else:
            lines.append("- **UniProt:** no entry resolved.")
        if prot.get("interpro_available"):
            domains = prot["interpro"].get("affected_domains")
            position = prot["interpro"].get("protein_position")
            if domains:
                names = ", ".join(d.get("name") or d.get("member_accession") or "unnamed" for d in domains)
                lines.append(
                    f"- **InterPro/Pfam:** residue {position} (transcript-verified) overlaps {len(domains)} domain(s): {names}"
                )
            elif domains is None:
                lines.append(
                    "- **InterPro/Pfam:** annotation available, but this variant's residue position could not be determined "
                    "from the transcript structure -- domain overlap was not checked."
                )
            else:
                lines.append(
                    f"- **InterPro/Pfam:** annotation available; no domain overlap at residue {position} (transcript-verified)."
                )
        elif prot.get("interpro_error"):
            lines.append(
                f"- **InterPro/Pfam:** _lookup failed (external service issue: {prot['interpro_error']}) -- not evidence of an absent domain, see Annotation Detail below._"
            )
        elif prot.get("interpro_reason"):
            lines.append(f"- **InterPro/Pfam:** {prot['interpro_reason']}")
        else:
            lines.append("- **InterPro/Pfam:** no annotation available.")
        lines.append("")

        struct = clinical_report["structural_knowledge"]
        lines.append("### 9. Structural Knowledge")
        lines.append("")
        if struct.get("available"):
            band_label = (
                f"at residue {struct.get('protein_position')} (transcript-verified)"
                if struct.get("confidence_band_is_residue_specific")
                # `mapping_unavailable_reason` is the mapping gate's own
                # specific verdict (mapping_gate.py names six distinct
                # reasons) -- "variant residue position unknown" is only
                # accurate for one of them, so it is now a fallback for
                # the reason being absent, not the default explanation.
                else f"whole-protein mean ({struct.get('mapping_unavailable_reason') or 'variant residue position unknown'})"
            )
            lines.append(
                f"- **AlphaFold DB:** confidence band '{struct.get('confidence_band') or 'n/a'}' {band_label} (model {struct.get('model_version') or 'n/a'})"
            )
            if struct.get("pdb_url"):
                lines.append(f"  - Structure: {struct['pdb_url']}")
        # POSITIVE-POLARITY TRUTHINESS (2026-08-31, sweep Batch 4):
        # `elif struct.get("error"):` missed an empty-but-present
        # error, falling through to the "reason"/generic-negative
        # branches below -- a confirmed negative, same class as this
        # module's other provider-status branches. `struct["error"]` is
        # `clinical_report_builder.py::_structural_knowledge`'s direct
        # passthrough of `alphafold_result["error"]`, confirmed by
        # reading that function -- the same field Batch 1 already fixed
        # at the producer (orchestrator.py's AlphaFold stage /
        # alphafold/provider.py). Safe today for that reason; fixed
        # here anyway so the class cannot reopen if that producer
        # regresses.
        elif struct.get("error") is not None:
            lines.append(
                f"*AlphaFold DB lookup failed (external service issue: {struct['error']}) -- not evidence of an unresolved structure, see Annotation Detail below.*"
            )
        elif struct.get("reason"):
            lines.append(f"*{struct['reason']}*")
        else:
            lines.append("*No AlphaFold DB structure resolved for this protein.*")
        lines.append("")

        pop = clinical_report["population_evidence"]
        lines.append("### 10. Population Evidence")
        lines.append("")
        g = pop["gnomad"]
        if g["queried"]:
            lines.append(
                f"- **gnomAD:** {'found, AF=' + str(g['global_af']) if g['found'] else 'variant not found (absent from gnomAD)'}"
            )
        elif g.get("skip_reason"):
            lines.append(f"- **gnomAD:** {g['skip_reason']}")
        else:
            lines.append("- **gnomAD:** lookup unavailable for this variant.")
        d = pop["dbsnp"]
        lines.append(f"- **dbSNP:** {'catalogued as ' + d['rsid'] if d['found'] else 'not found'}")
        lines.append("")

        ipf = clinical_report.get("indian_population_frequency") or {}
        lines.append("### 11. Indian Population Frequency")
        lines.append("")
        gnomad_sas_af = ipf.get("gnomad_af_sas")
        if ipf.get("gnomad_sas_queried"):
            lines.append(
                f"- **gnomAD (South Asian, SAS):** {'AF=' + str(gnomad_sas_af) if gnomad_sas_af is not None else 'no South Asian subpopulation data for this variant'}"
            )
        else:
            lines.append("- **gnomAD (South Asian, SAS):** lookup unavailable for this variant.")
        if ipf.get("sas_shown"):
            lines.extend(_render_1000_genomes_sas_markdown(ipf))
        threshold_pct = f"{ipf.get('common_af_threshold', 0.01):.0%}"
        if ipf.get("common_in_indian_population"):
            lines.append(f"- **⚠ Common in Indian populations** (at or above the {threshold_pct} threshold).")
        lines.append("")

        clin = clinical_report["clinical_evidence"]
        lines.append("### 12. Clinical Evidence")
        lines.append("")
        if clin.get("clinvar_available"):
            cv = clin["clinvar"]
            lines.append(
                f"- **ClinVar:** {cv.get('clinical_significance') or 'n/a'} ({cv.get('review_status') or 'n/a'})"
            )
        elif clin.get("clinvar_error"):
            lines.append(
                f"- **ClinVar:** _lookup failed (external service issue: {clin['clinvar_error']}) -- not evidence of an absent record, see Annotation Detail below._"
            )
        elif clin.get("clinvar_co_located_count"):
            lines.append(
                f"- **ClinVar:** no record found for this exact variant "
                f"({clin['clinvar_co_located_count']} other variant(s) catalogued at this genomic "
                "position, but none match this allele -- see Annotation Detail below)."
            )
        else:
            lines.append("- **ClinVar:** no record found.")
        if clin.get("clingen_available"):
            cg = clin["clingen"]
            lines.append(
                f"- **ClinGen:** {cg.get('gene_symbol') or 'n/a'} -- gene-disease validity: {cg.get('clinical_validity_summary') or 'n/a'}"
            )
        elif clin.get("clingen_error"):
            lines.append(
                f"- **ClinGen:** _lookup failed (external service issue: {clin['clingen_error']}) -- not evidence of an absent curation, see Annotation Detail below._"
            )
        else:
            lines.append("- **ClinGen:** no curation found for this gene.")
        lines.append("")

        seq = clinical_report["sequence_context"]
        lines.append("### 13. Sequence Context")
        lines.append("")
        lines.append(f"- **Context models used:** {', '.join(seq.get('context_models_used') or []) or 'none'}")
        blast_error = seq["blast"].get("error")
        if blast_error is not None:
            lines.append(
                f"- **BLAST:** _lookup failed ({blast_error}) -- not evidence of no homology, see Annotation Detail below._"
            )
        else:
            lines.append(f"- **BLAST:** {seq['blast'].get('hit_count', 0)} homology hit(s)")
        lines.append(f"- *{seq.get('ensembl_note')}*")
        lines.append("")

        lines.append("### 14. Recommendations")
        lines.append("")
        recs = clinical_report.get("recommendations") or []
        if recs:
            for r in recs:
                lines.append(f"- {r}")
        else:
            lines.append("*No specific recommendations generated.*")
        lines.append("")

        lines.append("### 15. Limitations")
        lines.append("")
        for lim in clinical_report.get("limitations") or []:
            lines.append(f"- {lim}")
        lines.append("")

        lines.append("### 16. References")
        lines.append("")
        refs = clinical_report.get("references") or []
        if refs:
            for r in refs:
                lines.append(f"- {r}")
        else:
            lines.append("*No evidence sources contributed to this variant's interpretation.*")
        lines.append("")

        lines.append("### 17. Evidence Sources")
        lines.append("")
        sources = clinical_report.get("evidence_sources") or []
        lines.append(", ".join(sources) if sources else "*none*")
        lines.append("")

        lines.extend(ReportGenerator._render_explainability(clinical_report.get("explainability")))

        return lines

    @staticmethod
    def _render_explainability(explainability: Dict[str, Any]) -> List[str]:
        """
        Phase 7: renders the full explainability trace. Additive
        section (17) after the 16 required by Phase 5 -- everything
        here is copied or lightly formatted from `explainability`
        (itself built purely from fields the earlier sections already
        show), so nothing here can disagree with the rest of the
        report.
        """
        if not explainability:
            return [
                "### 17. Explainability",
                "",
                "*Explainability trace unavailable for this variant.*",
                "",
            ]
        lines = ["### 17. Explainability", ""]

        lines.append("**Decision summary:** " + explainability["decision_summary"])
        lines.append("")

        lines.append("**Reasoning chain:**")
        for step in explainability.get("reasoning_chain", []):
            lines.append(f"- {step}")
        lines.append("")

        contributed = explainability.get("evidence_contributed") or []
        lines.append(f"**Evidence sources that contributed:** {', '.join(contributed) if contributed else 'none'}")
        lines.append("")
        not_contributed = explainability.get("evidence_not_contributed") or []
        if not_contributed:
            lines.append("**Evidence sources that did not contribute:**")
            for e in not_contributed:
                lines.append(f"- {e['source']}: {e['reason']}")
            lines.append("")

        influential = explainability.get("ai_models_influential") or []
        lines.append(
            "**AI models that influenced the decision:** "
            + (", ".join(f"{v.get('source')} ({v.get('prediction')})" for v in influential) if influential else "none")
        )
        contextual = explainability.get("ai_models_contextual_only") or []
        lines.append(
            f"**AI models used for context only (no verdict):** {', '.join(contextual) if contextual else 'none'}"
        )
        lines.append("")

        hw = explainability.get("highest_weight_evidence") or {}
        if hw.get("confidence"):
            lines.append(
                f"**Highest-weight evidence for confidence:** {hw['confidence']['category']} (contribution={hw['confidence']['contribution']})"
            )
        if hw.get("priority"):
            lines.append(
                f"**Highest-weight evidence for priority:** {hw['priority']['factor']} (contribution={hw['priority']['contribution']})"
            )
        lines.append("")

        conflicts = explainability.get("conflicts_detected") or []
        lines.append(f"**Conflicts detected:** {len(conflicts)}")
        if conflicts:
            for c in conflicts:
                lines.append(f"- {c['conflict_type']} ({c['severity']})")
        lines.append("")
        lines.append(
            "**How conflicts were resolved:** " + explainability.get("conflicts_resolved", "No conflicts to resolve.")
        )
        lines.append("")

        uncertainties = explainability.get("remaining_uncertainties") or []
        lines.append("**Remaining uncertainties:**")
        if uncertainties:
            for u in uncertainties:
                lines.append(f"- {u}")
        else:
            lines.append("- None identified.")
        lines.append("")

        lines.append("**Limitations:**")
        for lim in explainability.get("limitations", []):
            lines.append(f"- {lim}")
        lines.append("")

        lines.append("**Confidence rationale:** " + explainability.get("confidence_rationale", ""))
        lines.append("")
        lines.append("**Priority rationale:** " + explainability.get("priority_rationale", ""))
        lines.append("")

        trace = explainability.get("evidence_trace") or []
        if trace:
            lines.append(f"<details><summary>Full evidence trace (all {len(trace)} ACMG criteria)</summary>")
            lines.append("")
            lines.append("| Code | Status | Rationale |")
            lines.append("|---|---|---|")
            for t in trace:
                lines.append(f"| {t['code']} | {t['status']} | {t['rationale']} |")
            lines.append("")
            lines.append("</details>")
            lines.append("")

        return lines

    @staticmethod
    def _render_interpretation(interpretation: Dict[str, Any]) -> List[str]:
        if not interpretation:
            return []
        lines = ["### Unified Interpretation", ""]
        lines.append(f"**Summary:** {interpretation.get('summary', 'n/a')}")
        lines.append("")
        confidence = interpretation.get("confidence")
        if confidence is not None:
            lines.append(f"**Confidence (legacy):** {confidence}")
            lines.append("")

        # Phase 3: independent confidence score, rendered alongside (not
        # instead of) the legacy confidence line above. Pulled from
        # `interpretation_result`, which Phase 2 already threads through
        # this same `interpretation` dict -- nothing re-derived here.
        ir = interpretation.get("interpretation_result") or {}
        if ir.get("acmg_classification"):
            lines.append(f"**ACMG Classification:** {ir['acmg_classification']}")
            lines.append("")
        if not ir.get("confidence_pending", True):
            lines.append(f"**Evidence Completeness:** {ir.get('confidence_score')}% ({ir.get('confidence_label')})")
            breakdown = (ir.get("confidence_breakdown") or {}).get("category_breakdown", [])
            if breakdown:
                lines.append("")
                lines.append("| Evidence Category | Weight | Presence | Quality | Contribution |")
                lines.append("|---|---|---|---|---|")
                for cat in breakdown:
                    lines.append(
                        f"| {cat['category']} | {cat['weight']} | {cat['presence']} | "
                        f"{cat['quality']} | {cat['contribution']} |"
                    )
                lines.append("")
                conflict_note = (ir.get("confidence_breakdown") or {}).get("conflict_explanation")
                if conflict_note:
                    lines.append(f"*Conflict adjustment: {conflict_note}*")
                    lines.append("")
        else:
            lines.append("**Evidence Completeness:** pending (Phase 3 confidence engine did not run for this variant)")
            lines.append("")

        # Phase 4: priority score/category, rendered the same way as
        # Phase 3's confidence block above -- additive, sourced entirely
        # from `interpretation_result`. Sibling to the confidence block,
        # not nested in it -- a variant can have priority available even
        # if confidence scoring failed for some reason, or vice versa.
        if not ir.get("priority_pending", True):
            rank = ir.get("priority_rank")
            rank_text = f" (rank {rank} in this run)" if rank is not None else ""
            lines.append(f"**Priority:** {ir.get('priority_category')} -- score {ir.get('priority_score')}{rank_text}")
            lines.append("")
            explanation = ir.get("priority_explanation") or []
            if explanation:
                lines.append("**Priority reasons:**")
                for reason in explanation:
                    lines.append(f"- {reason}")
                lines.append("")
        else:
            lines.append("**Priority:** pending (Phase 4 prioritization engine did not run for this variant)")
            lines.append("")

        evidence = interpretation.get("supporting_evidence", [])
        if evidence:
            lines.append("**Supporting evidence:**")
            for item in evidence:
                lines.append(f"- {item}")
            lines.append("")
        return lines

    @staticmethod
    def _render_dbsnp(dbsnp: Dict[str, Any]) -> List[str]:
        # `found` means an allele-matched rsID was resolved (see
        # `database/dbsnp_client.py`'s module docstring), not merely
        # "some rsID exists at this position" -- `match_status ==
        # "position_only"` gets its own honest line rather than falling
        # into "no dbSNP record found".
        lines = ["### dbSNP", ""]
        if dbsnp and dbsnp.get("match_status") == "position_only":
            lines.append(
                f"_Not catalogued in dbSNP under this exact allele ({dbsnp.get('record_count')} other "
                "rsID(s) exist at this genomic position, but do not match this allele)._"
            )
            lines.append("")
            return lines
        if not dbsnp or not dbsnp.get("found"):
            lines.append("_No dbSNP record found._")
            lines.append("")
            return lines
        lines.append(f"- **rsID:** {dbsnp.get('rsid')}")
        detail = dbsnp.get("detail") or {}
        if detail.get("genes"):
            lines.append(f"- **Gene(s):** {', '.join(detail['genes'])}")
        lines.append("")
        return lines

    @staticmethod
    def _render_clinvar(clinvar: Dict[str, Any]) -> List[str]:
        # `found` now means "a record matching this exact variant's
        # allele was found" (see `database/clinvar_client.py`'s module
        # docstring), not merely "ClinVar returned something at this
        # genomic position" -- so `match_status == "position_only"`
        # gets its own branch rather than falling into "no record
        # found", and every listed record is labelled with whether it
        # is this variant or a different, co-located one, so this raw
        # audit-trail listing can never again be read as "here are up
        # to 3 records about this variant" when some of them aren't.
        lines = ["### ClinVar", ""]
        # POSITIVE-POLARITY TRUTHINESS (2026-08-31, sweep Batch 4):
        # `if clinvar and clinvar.get("error"):` missed an empty-but-
        # present error, falling through to whatever "no record found"
        # text follows -- a confirmed negative. Safe today because
        # orchestrator.py's ClinVar stage was already fixed pre-session
        # (8df8b9f) to never emit an empty string; fixed here anyway so
        # the class cannot reopen if that producer regresses.
        if clinvar and clinvar.get("error") is not None:
            lines.append(
                f"_Failed: {clinvar['error']} -- not evidence ClinVar has no record, see the AI Model Status/Stage Warnings above._"
            )
            lines.append("")
            return lines
        if not clinvar or clinvar.get("match_status") != "position_only" and clinvar.get("match_status") != "matched":
            # Covers both a genuine `not_found` and any other
            # unrecognized/missing shape -- never silently renders an
            # empty section, matching this function's pre-fix fallback.
            lines.append("_No ClinVar record found for this variant._")
            lines.append("")
            return lines
        if clinvar.get("match_status") == "position_only":
            lines.append(
                "_No ClinVar record matches this exact variant's allele. The following variant(s) are "
                "catalogued at this same genomic position but are NOT this variant:_"
            )
            lines.append("")
        for record in clinvar.get("records", [])[:3]:
            match = record.get("variant_match")
            label = (
                "**this variant**"
                if match
                else ("_different variant at this position_" if match is False else "_match unconfirmed_")
            )
            lines.append(
                f"- {label} — **{record.get('clinical_significance', 'Unknown significance')}** "
                f"({record.get('review_status', 'n/a')}) — "
                f"{', '.join(record.get('condition') or []) or 'condition not specified'}"
            )
        lines.append("")
        return lines

    @staticmethod
    def _render_dna_models(dna_results: Dict[str, Any]) -> List[str]:
        if not dna_results:
            return []
        lines = ["### DNA Foundation Model Analysis", ""]
        for model_name, result in dna_results.items():
            meta = result.get("meta", {}) if isinstance(result, dict) else {}
            lines.append(
                f"- **{model_name}**: embedding dim "
                f"{result.get('embedding_dim', 'n/a')}, "
                f"{meta.get('inference_seconds', 'n/a')}s on {meta.get('device', 'n/a')}"
            )
        lines.append("")
        return lines

    @staticmethod
    def _render_rna(rna_result: Dict[str, Any]) -> List[str]:
        if not rna_result:
            return []
        lines = ["### RNA-FM Analysis", ""]
        # POSITIVE-POLARITY TRUTHINESS (2026-08-31, sweep Batch 4): `if
        # rna_result.get("error"):` missed an empty-but-present error,
        # falling through to a "skipped" reading -- a confirmed
        # negative. Safe today because _run_rna_stage's producer was
        # already fixed in Batch 2 to never emit an empty string; fixed
        # here anyway so the class cannot reopen if that producer
        # regresses.
        if rna_result.get("error") is not None:
            # A genuine crash, not a normal "not transcript-relevant"
            # skip -- distinguished via the `error` key
            # `pipeline/orchestrator.py::_run_rna_stage` now sets only
            # on its exception path (see that method's comment).
            lines.append(
                f"_Failed: {rna_result['error']} -- not evidence RNA-FM was inapplicable, see the AI Model Status table above._"
            )
        elif rna_result.get("skipped"):
            lines.append(f"_Skipped: {rna_result.get('reason', 'not applicable')}._")
        else:
            lines.append(
                f"- Embedding dim: {rna_result.get('embedding_dim', 'n/a')}, "
                f"tokens: {rna_result.get('num_tokens', 'n/a')}"
            )
        lines.append("")
        return lines

    @staticmethod
    def _render_protein(protein_result: Dict[str, Any]) -> List[str]:
        if not protein_result:
            return []
        lines = ["### Protein / ESM-2 Analysis", ""]
        # POSITIVE-POLARITY TRUTHINESS (2026-08-31, sweep Batch 4): same
        # defect and same fix as `_render_rna` above -- safe today
        # because _run_protein_stage's producer was already fixed in
        # Batch 2, fixed here anyway for the same reason.
        if protein_result.get("error") is not None:
            # Same distinction as `_render_rna` -- see that method's comment.
            lines.append(
                f"_Failed: {protein_result['error']} -- not evidence ESM-2 was inapplicable, see the AI Model Status table above._"
            )
        elif protein_result.get("skipped"):
            lines.append(f"_Skipped: {protein_result.get('reason', 'not applicable')}._")
        else:
            translation = protein_result.get("translation", {})
            lines.append(f"- **Reference protein (local window):** `{translation.get('ref_protein') or 'n/a'}`")
            lines.append(f"- **Alternate protein (local window):** `{translation.get('alt_protein') or 'n/a'}`")
            lines.append(
                "  - _Translated from the first start codon found in a short flanking DNA window "
                "(no exon/splicing awareness); this is the raw sequence handed to ESM-2, not a "
                "clinical call. It is NOT the transcript-verified consequence -- see the ACMG "
                "criteria (BP7/BP1/PS1/PM5/PVS1) in the Clinical Report above for that._"
            )
            esm = protein_result.get("esm2", {})
            if esm:
                lines.append(f"- Embedding dim: {esm.get('embedding_dim', 'n/a')}")
        lines.append("")
        return lines

    @staticmethod
    def _render_alphamissense(am_result: Dict[str, Any]) -> List[str]:
        if not am_result:
            return []
        lines = ["### AlphaMissense", ""]
        # POSITIVE-POLARITY TRUTHINESS (2026-08-31, sweep Batch 4): same
        # defect and same fix as `_render_rna` above -- safe today
        # because _run_alphamissense_stage's producer was already fixed
        # in Batch 2, fixed here anyway for the same reason.
        if am_result.get("error") is not None:
            # Same distinction as `_render_rna` -- see that method's comment.
            lines.append(
                f"_Failed: {am_result['error']} -- not evidence this variant lacks a catalogue entry, see the AI Model Status table above._"
            )
            lines.append("")
            return lines
        if am_result.get("skipped"):
            lines.append(f"_Skipped: {am_result.get('reason', 'not applicable')}._")
            lines.append("")
            return lines
        if not am_result.get("found"):
            lines.append(
                f"_No AlphaMissense catalogue entry found for this variant "
                f"(genome build: {am_result.get('genome', 'n/a')})._"
            )
            lines.append("")
            return lines
        pathogenicity = am_result.get("am_pathogenicity")
        pathogenicity_str = f"{pathogenicity:.4f}" if isinstance(pathogenicity, (int, float)) else "n/a"
        lines.append(f"- **am_pathogenicity:** {pathogenicity_str}")
        lines.append(f"- **am_class:** {am_result.get('am_class', 'n/a')}")
        lines.append(f"- **Protein variant:** {am_result.get('protein_variant', 'n/a')}")
        lines.append(f"- **Transcript:** {am_result.get('transcript_id', 'n/a')}")
        lines.append(f"- **UniProt ID:** {am_result.get('uniprot_id', 'n/a')}")
        lines.append(f"- **Genome build:** {am_result.get('genome', 'n/a')}")
        lines.append("")
        # Same caveat wording `acmg_rules.py::_pp3_bp4` established, as a
        # trailing italic note rather than a bullet -- this block renders
        # the raw stage result directly (not an ACMG evidence sentence),
        # so there is no existing sentence to fold the caveat into; a
        # standalone note matches this method's own convention for
        # meta-commentary (see the Failed/Skipped/No-entry branches above).
        lines.append(
            "_AlphaMissense is not clinically validated and not approved for clinical use. This is a raw "
            "model score, not a validated clinical pathogenicity measure._"
        )
        lines.append("")
        return lines

    @staticmethod
    def _render_mmsplice(mmsplice_result: Dict[str, Any]) -> List[str]:
        if not mmsplice_result:
            return []
        lines = ["### MMSplice (Splice Effect Prediction)", ""]
        # POSITIVE-POLARITY TRUTHINESS (2026-08-31, sweep Batch 4): this
        # is the site that closes Phase 1's "MMSplice untraced" open
        # item -- `if mmsplice_result.get("error"):` missed an empty-
        # but-present error, falling through to MMSplice's "not scored"
        # text, a confirmed negative same class as BLAST's own already-
        # fixed defect. Safe today because _run_mmsplice_stage's
        # producer was already fixed in Batch 2, fixed here anyway so
        # the class cannot reopen if that producer regresses.
        if mmsplice_result.get("error") is not None:
            # Same distinction as `_render_rna` -- see that method's
            # comment. Checked first: MMSplice's own "not scored"/
            # "skipped" branches below both key off `supported`/
            # `predicted`, which a genuine crash also leaves False.
            lines.append(
                f"_Failed: {mmsplice_result['error']} -- not evidence of no splice effect, see the AI Model Status table above._"
            )
            lines.append("")
            return lines
        if not mmsplice_result.get("supported", False):
            lines.append(
                f"_Not scored: {mmsplice_result.get('skip_reason') or mmsplice_result.get('interpretation') or 'unsupported'}._"
            )
            lines.append("")
            return lines
        if not mmsplice_result.get("predicted", False):
            lines.append(f"_Skipped: {mmsplice_result.get('skip_reason', 'not predicted')}._")
            lines.append("")
            return lines

        delta_logit_psi = mmsplice_result.get("delta_logit_psi")
        delta_str = f"{delta_logit_psi:.4f}" if isinstance(delta_logit_psi, (int, float)) else "n/a"
        lines.append(f"- **Interpretation:** {mmsplice_result.get('interpretation', 'n/a')}")
        lines.append(f"- **Confidence:** {mmsplice_result.get('confidence', 'n/a')}")
        lines.append(f"- **delta_logit_psi:** {delta_str}")
        lines.append(f"- **Donor score (delta):** {mmsplice_result.get('donor_score', 'n/a')}")
        lines.append(f"- **Acceptor score (delta):** {mmsplice_result.get('acceptor_score', 'n/a')}")
        lines.append(f"- **Exon skipping score:** {mmsplice_result.get('exon_skipping', 'n/a')}")
        lines.append(f"- **Intron retention score:** {mmsplice_result.get('intron_retention', 'n/a')}")
        lines.append(f"- **Alt donor (cryptic site) score:** {mmsplice_result.get('alt_donor', 'n/a')}")
        lines.append(f"- **Alt acceptor (cryptic site) score:** {mmsplice_result.get('alt_acceptor', 'n/a')}")
        lines.append(f"- **Runtime:** {mmsplice_result.get('runtime_ms', 'n/a')} ms")
        lines.append(f"- **Model version:** {mmsplice_result.get('model_version', 'n/a')}")
        lines.append("")
        return lines

    @staticmethod
    def _render_ai_model_status(ai_model_status: Dict[str, Dict[str, str]]) -> List[str]:
        """
        Objective 6: the "AI Models" status table. Unlike every other
        `_render_*` method in this file, this one is NEVER allowed to
        return an empty list -- every report must show this table,
        even if `ai_model_status` itself is empty/missing (a legacy
        result dict from before this feature existed), because the
        entire point is that a reader can always see, for every model
        GEPER knows about, whether it ran, was skipped, was disabled,
        or failed -- never silence.

        Delegates the actual bucketing/formatting to
        `pipeline.models.status.render_status_table_lines`, which is
        independently unit-tested against `DISPLAY_ORDER` covering
        every known model -- this method only adds the section header.
        """
        lines = ["### AI Models", ""]
        status = ai_model_status or {}
        if not status:
            # Defensive fallback for a legacy/malformed result dict --
            # still say something explicit rather than rendering
            # nothing at all.
            lines.append("- No AI model status was recorded for this variant.")
            lines.append("")
            return lines
        lines.extend(render_status_table_lines(status))
        lines.append("")
        return lines

    @staticmethod
    def _render_ai_splicing_ensemble(ensemble_result: Dict[str, Any]) -> List[str]:
        """
        Renders the Enformer + Borzoi ensemble ("AI Splicing Analysis")
        section, following the same structure as `_render_mmsplice`
        immediately above: a header, a short data table, then a few
        labeled summary lines. `ensemble_result` is always the clean,
        already-sanitized dict `pipeline.models.ensemble.
        EnsembleManager.evaluate` returns (see that module) -- never a
        raw exception or traceback, so nothing here needs its own
        try/except: there is no technical error text this function
        could accidentally leak into a clinical report.

        The section is hidden entirely (returns an empty list, adding
        no heading at all) whenever no ensemble evidence exists for
        this variant -- either because `ensemble_result` itself is
        empty/missing, or because it exists but no model actually ran
        (`models_used` is empty, e.g. both Enformer and Borzoi were
        disabled/unavailable). This mirrors exactly what
        `report/json_builder.py::build_variant_result` does: the
        `ai_splicing_ensemble` key is only present in the JSON output
        under that same condition, so JSON and Markdown agree on when
        this evidence "exists."
        """
        if not ensemble_result or not ensemble_result.get("models_used"):
            return []

        lines = ["### AI Splicing Analysis (Enformer + Borzoi Ensemble)", ""]

        lines.append("| Model | Version | Score | Classification | Confidence |")
        lines.append("|---|---|---|---|---|")
        for name, model_result in (ensemble_result.get("individual_scores") or {}).items():
            version = (model_result.get("meta") or {}).get("version", "n/a")
            score = model_result.get("score")
            score_str = f"{score:.3f}" if isinstance(score, (int, float)) else "n/a"
            classification = model_result.get("classification", "n/a")
            confidence = model_result.get("confidence")
            confidence_str = f"{confidence:.2f}" if isinstance(confidence, (int, float)) else "n/a"
            lines.append(f"| {name} | {version} | {score_str} | {classification} | {confidence_str} |")
        lines.append("")

        consensus_score = ensemble_result.get("consensus_score")
        consensus_str = f"{consensus_score:.3f}" if isinstance(consensus_score, (int, float)) else "n/a"
        classification = ensemble_result.get("classification", "n/a")
        confidence = ensemble_result.get("confidence")
        confidence_str = f"{confidence:.2f}" if isinstance(confidence, (int, float)) else "n/a"
        agreement = ensemble_result.get("agreement_percentage")
        agreement_str = (
            f"{agreement}%" if agreement is not None else "n/a (single model -- agreement is not applicable)"
        )
        basis_label = {
            "single_model": "single-model result",
            "two_model_consensus": "two-model consensus",
        }.get(ensemble_result.get("basis"), "n/a")

        lines.append(f"- **Consensus score:** {consensus_str} ({basis_label})")
        lines.append(f"- **Confidence:** {confidence_str}")
        lines.append(f"- **Agreement:** {agreement_str}")
        lines.append(f"- **Interpretation:** {classification}")
        reasoning = ensemble_result.get("reasoning")
        if reasoning:
            lines.append(f"- **Summary:** {reasoning}")
        lines.append("")
        return lines

    @staticmethod
    def _render_gnomad(gnomad_result: Dict[str, Any]) -> List[str]:
        """
        Dedicated gnomAD evidence panel (requirement #9): global/
        highest-population/genome/exome AF, a per-population frequency
        table, evidence source, and genome build -- separate from the
        ACMG contribution itself (which lives in the "Unified
        Interpretation" section's supporting-evidence list above,
        since that's where every other evidence source's ACMG
        contribution already surfaces).
        """
        if not gnomad_result:
            return []
        lines = ["### gnomAD (Population Frequency)", ""]

        if gnomad_result.get("skipped"):
            lines.append(f"_Skipped: {gnomad_result.get('reason', 'gnomAD integration disabled')}._")
            lines.append("")
            return lines

        lines.append(f"- **Source:** {gnomad_result.get('source', 'n/a')}")
        lines.append(f"- **Genome build:** {gnomad_result.get('build', 'n/a')}")

        if gnomad_result.get("error") is not None:
            lines.append(f"- **Status:** query failed ({gnomad_result['error']})")
            lines.append("")
            return lines

        if not gnomad_result.get("found"):
            lines.append("- **Status:** Not found in gnomAD (consistent with PM2 -- absent from population database).")
            lines.append("")
            return lines

        def _fmt_af(value) -> str:
            return f"{value:.6%}" if isinstance(value, (int, float)) else "n/a"

        lines.append(f"- **Global AF:** {_fmt_af(gnomad_result.get('global_af'))}")
        lines.append(f"- **Genome AF:** {_fmt_af(gnomad_result.get('genome_af'))}")
        lines.append(f"- **Exome AF:** {_fmt_af(gnomad_result.get('exome_af'))}")
        lines.append(
            f"- **Allele count / number:** {gnomad_result.get('ac', 'n/a')} / {gnomad_result.get('an', 'n/a')}"
        )
        lines.append(f"- **Homozygotes:** {gnomad_result.get('hom', 'n/a')}")
        if gnomad_result.get("hemi") is not None:
            lines.append(f"- **Hemizygotes:** {gnomad_result.get('hemi')}")
        highest_pop = gnomad_result.get("highest_population")
        if highest_pop:
            lines.append(f"- **Highest population:** {highest_pop}")
        lines.append("")

        breakdown = gnomad_result.get("population_breakdown") or {}
        if breakdown:
            lines.append("| Population | AC | AN | AF | Hom | Hemi |")
            lines.append("|---|---|---|---|---|---|")
            for pop_key, freq in breakdown.items():
                lines.append(
                    f"| {freq.get('label', pop_key)} | {freq.get('ac', 'n/a')} | {freq.get('an', 'n/a')} | "
                    f"{_fmt_af(freq.get('af'))} | {freq.get('hom', 'n/a')} | {freq.get('hemi', 'n/a')} |"
                )
            lines.append("")

        return lines

    @staticmethod
    def _render_clingen(clingen_result: Dict[str, Any]) -> List[str]:
        """
        Dedicated ClinGen evidence panel: gene-disease clinical
        validity, dosage sensitivity, expert panel, and actionability
        -- separate from the ACMG contribution itself (which lives in
        the "Unified Interpretation" section's supporting-evidence
        list above), matching `_render_gnomad`'s split.
        """
        if not clingen_result:
            return []
        lines = ["### ClinGen (Clinical Evidence)", ""]

        if clingen_result.get("skipped"):
            lines.append(f"_Skipped: {clingen_result.get('reason', 'ClinGen integration disabled')}._")
            lines.append("")
            return lines

        gene_symbol = clingen_result.get("gene_symbol")
        lines.append(f"- **Gene:** {gene_symbol or 'n/a'}")
        lines.append(f"- **Source:** {clingen_result.get('source', 'n/a')}")

        # POSITIVE-POLARITY TRUTHINESS (2026-08-31, sweep Batch 4): one
        # of Phase 1's original four confirmed-negative sites. Safe
        # today because clingen/provider.py's producer was already
        # fixed in Batch 1, fixed here anyway so the class cannot
        # reopen if that producer regresses.
        if clingen_result.get("error") is not None:
            lines.append(f"- **Status:** query failed ({clingen_result['error']})")
            lines.append("")
            return lines

        if not clingen_result.get("found"):
            reason = clingen_result.get("reason", "No ClinGen curation found for this gene.")
            lines.append(f"- **Status:** {reason}")
            lines.append("")
            return lines

        validities = clingen_result.get("gene_disease_validity") or []
        if validities:
            lines.append("")
            lines.append("| Disease | Classification | MOI | Expert Panel | Classified |")
            lines.append("|---|---|---|---|---|")
            for v in validities:
                lines.append(
                    f"| {v.get('disease_label', 'n/a')} | {v.get('classification', 'n/a')} | "
                    f"{v.get('moi', 'n/a')} | {v.get('gcep', 'n/a')} | {v.get('classification_date', 'n/a')} |"
                )
            lines.append("")

        dosage = clingen_result.get("dosage_sensitivity")
        if dosage:
            lines.append(f"- **Haploinsufficiency:** {dosage.get('haploinsufficiency_label', 'n/a')}")
            lines.append(f"- **Triplosensitivity:** {dosage.get('triplosensitivity_label', 'n/a')}")

        actionability = clingen_result.get("actionability") or []
        if actionability:
            lines.append("- **Actionability:**")
            for a in actionability:
                lines.append(
                    f"  - {a.get('disease_label', 'n/a')}: adult={a.get('adult_actionability_score', 'n/a')}, "
                    f"pediatric={a.get('pediatric_actionability_score', 'n/a')}"
                )
        lines.append("")

        return lines

    @staticmethod
    def _render_uniprot(uniprot_result: Dict[str, Any]) -> List[str]:
        """
        UniProt reviewed-protein-annotation panel: function, disease
        relevance, and sequence features -- mirrors `_render_clingen` /
        `_render_gnomad`'s skipped/error/not-found short-circuit shape.
        """
        if not uniprot_result:
            return []
        lines = ["### UniProt (Protein Annotation)", ""]

        if uniprot_result.get("skipped"):
            lines.append(f"_Skipped: {uniprot_result.get('reason', 'UniProt integration disabled')}._")
            lines.append("")
            return lines

        gene_symbol = uniprot_result.get("gene_symbol")
        lines.append(f"- **Gene:** {gene_symbol or 'n/a'}")
        lines.append(f"- **Source:** {uniprot_result.get('source', 'n/a')}")

        # POSITIVE-POLARITY TRUTHINESS (2026-08-31, sweep Batch 4): one
        # of Phase 1's original four confirmed-negative sites. Safe
        # today because uniprot/provider.py's producer was already
        # fixed in Batch 1, fixed here anyway so the class cannot
        # reopen if that producer regresses.
        if uniprot_result.get("error") is not None:
            lines.append(f"- **Status:** query failed ({uniprot_result['error']})")
            lines.append("")
            return lines

        if not uniprot_result.get("found"):
            reason = uniprot_result.get("reason", "No reviewed UniProt entry found for this gene.")
            lines.append(f"- **Status:** {reason}")
            lines.append("")
            return lines

        lines.append(f"- **Accession:** {uniprot_result.get('accession', 'n/a')}")
        lines.append(f"- **Protein name:** {uniprot_result.get('protein_name', 'n/a')}")
        lines.append(f"- **Reviewed (Swiss-Prot):** {'Yes' if uniprot_result.get('reviewed') else 'No'}")
        lines.append(f"- **Organism:** {uniprot_result.get('organism', 'n/a')}")
        lines.append(f"- **Sequence length:** {uniprot_result.get('sequence_length', 'n/a')} aa")

        if uniprot_result.get("function"):
            lines.append(f"- **Function:** {uniprot_result['function']}")

        disease_comments = uniprot_result.get("disease_comments") or []
        if disease_comments:
            lines.append("- **Disease relevance:**")
            for comment in disease_comments[:5]:
                lines.append(f"  - {comment}")

        features = uniprot_result.get("features") or []
        if features:
            lines.append("")
            lines.append("| Feature type | Description | Span |")
            lines.append("|---|---|---|")
            for feat in features[:15]:
                span = f"{feat.get('begin', 'n/a')}-{feat.get('end', 'n/a')}"
                lines.append(f"| {feat.get('feature_type', 'n/a')} | {feat.get('description') or 'n/a'} | {span} |")
        lines.append("")

        return lines

    @staticmethod
    def _render_interpro(interpro_result: Dict[str, Any]) -> List[str]:
        """InterPro/Pfam conserved-domain panel, including which domains (if any) the variant's transcript-verified residue affects."""
        if not interpro_result:
            return []
        lines = ["### InterPro / Pfam (Conserved Domains)", ""]

        if interpro_result.get("skipped"):
            lines.append(f"_Skipped: {interpro_result.get('reason', 'InterPro integration disabled')}._")
            lines.append("")
            return lines

        lines.append(f"- **UniProt accession:** {interpro_result.get('accession', 'n/a')}")
        lines.append(f"- **Source:** {interpro_result.get('source', 'n/a')}")

        # POSITIVE-POLARITY TRUTHINESS (2026-08-31, sweep Batch 4): one
        # of Phase 1's original four confirmed-negative sites. Safe
        # today because interpro/provider.py's producer was already
        # fixed in Batch 1, fixed here anyway so the class cannot
        # reopen if that producer regresses.
        if interpro_result.get("error") is not None:
            lines.append(f"- **Status:** query failed ({interpro_result['error']})")
            lines.append("")
            return lines

        if not interpro_result.get("found"):
            reason = interpro_result.get("reason", "No InterPro/Pfam domain matches found for this protein.")
            lines.append(f"- **Status:** {reason}")
            lines.append("")
            return lines

        domains = interpro_result.get("domains") or []
        if domains:
            lines.append("")
            lines.append("| InterPro | Name | Type | Member DB | Member accession | Span |")
            lines.append("|---|---|---|---|---|---|")
            for d in domains[:20]:
                span = f"{d.get('start', 'n/a')}-{d.get('end', 'n/a')}"
                lines.append(
                    f"| {d.get('interpro_accession') or 'n/a'} | {d.get('name') or 'n/a'} | "
                    f"{d.get('type') or 'n/a'} | {d.get('member_database') or 'n/a'} | "
                    f"{d.get('member_accession') or 'n/a'} | {span} |"
                )
            lines.append("")

        affected = interpro_result.get("affected_domains")
        position = interpro_result.get("protein_position")
        if affected:
            lines.append(
                f"- **Affected domain(s) at residue {position}** "
                f"(_{interpro_result.get('protein_position_basis', 'n/a')}_):"
            )
            for d in affected:
                lines.append(f"  - {d.get('name') or d.get('member_accession') or 'unnamed domain'}")
        elif affected is None:
            lines.append(
                "- **Affected domain(s):** not checked -- this variant's residue position could not be "
                "determined from the transcript structure."
            )
        lines.append("")

        return lines

    @staticmethod
    def _render_alphafold(alphafold_result: Dict[str, Any]) -> List[str]:
        """AlphaFold DB structural-reference panel: model URL/version, overall confidence, and residue-level confidence when estimable."""
        if not alphafold_result:
            return []
        lines = ["### AlphaFold DB (Structural Reference)", ""]

        if alphafold_result.get("skipped"):
            lines.append(f"_Skipped: {alphafold_result.get('reason', 'AlphaFold integration disabled')}._")
            lines.append("")
            return lines

        lines.append(f"- **UniProt accession:** {alphafold_result.get('accession', 'n/a')}")
        lines.append(f"- **Source:** {alphafold_result.get('source', 'n/a')}")

        if alphafold_result.get("error") is not None:
            lines.append(f"- **Status:** query failed ({alphafold_result['error']})")
            lines.append("")
            return lines

        if not alphafold_result.get("found"):
            reason = alphafold_result.get("reason", "No AlphaFold DB structure prediction found for this protein.")
            lines.append(f"- **Status:** {reason}")
            lines.append("")
            return lines

        lines.append(f"- **Model version:** {alphafold_result.get('model_version', 'n/a')}")
        lines.append(f"- **Model (PDB):** {alphafold_result.get('pdb_url', 'n/a')}")
        lines.append(
            f"- **UniProt coverage:** {alphafold_result.get('uniprot_start', 'n/a')}-{alphafold_result.get('uniprot_end', 'n/a')}"
        )

        mean_plddt = alphafold_result.get("mean_plddt")
        if mean_plddt is not None:
            lines.append(f"- **Mean pLDDT:** {mean_plddt:.1f} ({alphafold_result.get('mean_plddt_band', 'n/a')})")
        else:
            lines.append("- **Mean pLDDT:** n/a (structure file not fetched)")

        if alphafold_result.get("affected_residue_plddt") is not None:
            lines.append(
                f"- **pLDDT at residue {alphafold_result.get('protein_position')}:** "
                f"{alphafold_result['affected_residue_plddt']:.1f} "
                f"({alphafold_result.get('affected_residue_band', 'n/a')}) "
                f"(_{alphafold_result.get('protein_position_basis', 'n/a')}_)"
            )
        elif alphafold_result.get("mean_plddt") is not None:
            # The old hardcoded text here claimed the residue position
            # couldn't be determined from the transcript -- true only for
            # one of mapping_gate.py's six failure reasons, and actively
            # false for the other five (e.g. a known position that's
            # simply outside the AlphaFold entry's span, or not modelled).
            reason = alphafold_result.get("mapping_unavailable_reason") or (
                "this variant's residue position could not be determined from the transcript structure"
            )
            lines.append(f"- **pLDDT at variant residue:** not checked -- {reason}.")
        lines.append("")

        return lines

    @staticmethod
    def _render_blast(blast_result: Dict[str, Any]) -> List[str]:
        if not blast_result:
            return []
        lines = ["### BLAST Results", ""]
        # POSITIVE-POLARITY TRUTHINESS (2026-08-31, sweep Batch 4): one
        # of Phase 1's original four confirmed-negative sites -- the
        # sharpest instance found this sweep, since the comment just
        # below already names the exact defect this truthiness bug
        # reopened for any empty-message crash. Safe today because
        # orchestrator.py's BLAST stage producer was already fixed in
        # Batch 1, fixed here anyway so the class cannot reopen if that
        # producer regresses.
        if blast_result.get("error") is not None:
            # A genuine crash, not "genuinely no hits" -- see
            # `pipeline/orchestrator.py::_run_blast_stage`'s comment.
            # Previously this branch didn't exist at all: an empty
            # `hits` list from either cause rendered identically as
            # "No significant BLAST hits."
            lines.append(f"_Failed: {blast_result['error']} -- not evidence of no homology._")
            lines.append("")
            return lines
        hits = blast_result.get("hits", [])
        if not hits:
            lines.append("_No significant BLAST hits._")
        else:
            for hit in hits[:5]:
                lines.append(
                    f"- {hit.get('title', hit.get('hit_id', 'unknown'))} (E-value: {hit.get('e_value', 'n/a')})"
                )
        lines.append("")
        return lines
