"""
GEPER report comparison -- diffs two completed runs' `geper_results.json`
output directories and renders a readable Markdown diff.

Usage:
    python compare_reports.py <run_a_dir> <run_b_dir> [--output diff.md]

Example:
    python compare_reports.py ./geper_output_before ./geper_output_after

What this reports (all derived from the two already-computed JSON
documents -- nothing here re-runs the pipeline or re-derives evidence):

  1. Variants whose ACMG/AMP classification changed between the two runs
     (matched by chrom/pos/ref/alt identity), with each side's
     classification and confidence label for context.
  1b. Variants whose numeric confidence *score* changed, independently of
      whether the classification or confidence label did (Finding 8: a
      label like "Moderate" can span a wide score range, so a real shift
      such as 55.6 -> 64.8 previously had nowhere to appear at all).
  2. Per-variant evidence-source changes: which sources newly contributed
     evidence in run B that didn't in run A, and vice versa
     (`clinical_report["evidence_sources"]`, already computed by
     `report/clinical_report_builder.py` -- see that module).
  3. Data-source provenance differences (`pipeline/provenance.py`):
     every known source (ClinVar, ClinGen, gnomAD, ...) whose recorded
     status/version/release date/content hash differs between the two
     runs.
  4. "Unexplained" classification changes -- a variant whose
     classification changed between the two runs, but for which NONE of
     the evidence sources that variant actually cites shows a matching
     provenance version difference, AND GEPER's own code version
     (`pipeline/provenance.py::get_geper_code_version`) is identical
     between the two runs. A classification cannot legitimately move for
     no reason: if neither the underlying data nor the code that
     interprets it changed, something is wrong (nondeterminism, an
     uncaptured environment difference, a bug) -- this is flagged in its
     own section, distinctly and first, as the case most worth a
     reviewer's attention. A change explained by either a real
     data-source version bump OR a code-version change is still listed
     under (1) but is NOT flagged here.
  5. Variants present in only one run (added/removed), listed as their
     own honest category rather than silently absorbed into "changed".
"""

from __future__ import annotations

from report.clinical_report_builder import candidate_interpretation_of

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

from pipeline.provenance import EVIDENCE_SOURCE_TO_PROVENANCE_PREFIX

VariantKey = Tuple[str, Any, str, str]

_PROVENANCE_COMPARE_KEYS = ("status", "version", "release_date", "content_hash")


# ---------------------------------------------------------------------------
# Loading + basic accessors
# ---------------------------------------------------------------------------


def load_document(output_dir: str) -> Dict[str, Any]:
    """Reads `<output_dir>/geper_results.json`. Raises `FileNotFoundError`/
    `ValueError` with a clear message rather than a bare traceback -- this
    is a CLI tool, so a malformed/missing input should read as a usage
    error, not a crash."""
    path = os.path.join(output_dir, "geper_results.json")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"No geper_results.json found in '{output_dir}' (looked for '{path}').")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read/parse '{path}': {exc}") from exc


def _variant_key(variant_dict: Dict[str, Any]) -> VariantKey:
    return (
        str(variant_dict.get("chrom")),
        variant_dict.get("pos"),
        str(variant_dict.get("ref")),
        str(variant_dict.get("alt")),
    )


def _variant_locus(variant_dict: Dict[str, Any]) -> str:
    return f"{variant_dict.get('chrom')}:{variant_dict.get('pos')} {variant_dict.get('ref')}>{variant_dict.get('alt')}"


def _index_variants(document: Dict[str, Any]) -> Dict[VariantKey, Dict[str, Any]]:
    return {_variant_key(v.get("variant") or {}): v for v in document.get("variants") or []}


def _classification(variant_result: Dict[str, Any]) -> Optional[str]:
    clinical = candidate_interpretation_of(variant_result) or {}
    return (clinical.get("acmg_classification") or {}).get("classification")


def _confidence_label(variant_result: Dict[str, Any]) -> Optional[str]:
    clinical = candidate_interpretation_of(variant_result) or {}
    confidence = clinical.get("confidence") or {}
    if confidence.get("pending", True):
        return None
    return confidence.get("label")


def _confidence_score(variant_result: Dict[str, Any]) -> Optional[float]:
    """The numeric confidence score `_confidence_label` deliberately
    doesn't surface (Finding 8): two runs can share a classification AND
    a confidence *label* while the underlying score moves several points
    (e.g. 55.6 -> 64.8, both "Moderate") -- a real clinical-confidence
    shift with nothing in the old diff output to show it."""
    clinical = candidate_interpretation_of(variant_result) or {}
    confidence = clinical.get("confidence") or {}
    if confidence.get("pending", True):
        return None
    return confidence.get("score")


def _evidence_sources(variant_result: Dict[str, Any]) -> Set[str]:
    clinical = candidate_interpretation_of(variant_result) or {}
    return set(clinical.get("evidence_sources") or [])


def _provenance_by_source(document: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {r.get("source"): r for r in document.get("provenance") or [] if r.get("source")}


def _provenance_records_differ(record_a: Optional[Dict[str, Any]], record_b: Optional[Dict[str, Any]]) -> bool:
    if record_a is None or record_b is None:
        return record_a != record_b
    return any(record_a.get(k) != record_b.get(k) for k in _PROVENANCE_COMPARE_KEYS)


# ---------------------------------------------------------------------------
# Diff computation
# ---------------------------------------------------------------------------


def diff_reports(doc_a: Dict[str, Any], doc_b: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pure function: two already-loaded `geper_results.json` documents in,
    one diff-result dict out. Kept separate from Markdown rendering
    (`render_markdown` below) and from the CLI's argument handling
    (`main`) so this is independently testable without touching the
    filesystem.
    """
    variants_a = _index_variants(doc_a)
    variants_b = _index_variants(doc_b)
    keys_a, keys_b = set(variants_a), set(variants_b)
    common_keys = keys_a & keys_b

    provenance_a = _provenance_by_source(doc_a)
    provenance_b = _provenance_by_source(doc_b)
    all_sources = sorted(set(provenance_a) | set(provenance_b))
    provenance_diffs: List[Dict[str, Any]] = []
    for source in all_sources:
        record_a, record_b = provenance_a.get(source), provenance_b.get(source)
        if _provenance_records_differ(record_a, record_b):
            provenance_diffs.append({"source": source, "a": record_a, "b": record_b})

    code_version_a = doc_a.get("code_version")
    code_version_b = doc_b.get("code_version")
    code_version_changed = code_version_a != code_version_b

    classification_changes: List[Dict[str, Any]] = []
    unexplained_changes: List[Dict[str, Any]] = []
    evidence_changes: List[Dict[str, Any]] = []
    confidence_changes: List[Dict[str, Any]] = []

    for key in sorted(common_keys, key=lambda k: (k[0], k[1] if isinstance(k[1], int) else 0)):
        variant_a, variant_b = variants_a[key], variants_b[key]
        locus = _variant_locus(variant_a.get("variant") or {})

        cls_a, cls_b = _classification(variant_a), _classification(variant_b)
        if cls_a != cls_b:
            entry = {
                "locus": locus,
                "classification_a": cls_a,
                "classification_b": cls_b,
                "confidence_a": _confidence_label(variant_a),
                "confidence_b": _confidence_label(variant_b),
            }
            classification_changes.append(entry)

            # Scope the "did anything actually change" check to the
            # sources THIS variant's evidence cited in either run --
            # a provenance bump for a source this variant never used is
            # not an explanation for its own classification moving.
            cited = _evidence_sources(variant_a) | _evidence_sources(variant_b)
            prefixes = {
                EVIDENCE_SOURCE_TO_PROVENANCE_PREFIX[name]
                for name in cited
                if name in EVIDENCE_SOURCE_TO_PROVENANCE_PREFIX
            }
            relevant_provenance_diffs = [
                d for d in provenance_diffs if any(d["source"].startswith(p) for p in prefixes)
            ]
            if not relevant_provenance_diffs and not code_version_changed:
                unexplained_changes.append(
                    {
                        **entry,
                        "cited_sources": sorted(cited),
                    }
                )

        # Independent of whether classification changed: Finding 8 is
        # specifically the case where it DIDN'T (or the label stayed the
        # same) and the score still moved -- that must not be invisible.
        score_a, score_b = _confidence_score(variant_a), _confidence_score(variant_b)
        if score_a != score_b:
            confidence_changes.append(
                {
                    "locus": locus,
                    "classification_a": cls_a,
                    "classification_b": cls_b,
                    "confidence_score_a": score_a,
                    "confidence_score_b": score_b,
                    "confidence_label_a": _confidence_label(variant_a),
                    "confidence_label_b": _confidence_label(variant_b),
                }
            )

        sources_a, sources_b = _evidence_sources(variant_a), _evidence_sources(variant_b)
        newly_matched = sorted(sources_b - sources_a)
        newly_unmatched = sorted(sources_a - sources_b)
        if newly_matched or newly_unmatched:
            evidence_changes.append(
                {
                    "locus": locus,
                    "newly_matched": newly_matched,
                    "newly_unmatched": newly_unmatched,
                }
            )

    added_variants = [
        _variant_locus(variants_b[k].get("variant") or {})
        for k in sorted(keys_b - keys_a, key=lambda k: (k[0], k[1] if isinstance(k[1], int) else 0))
    ]
    removed_variants = [
        _variant_locus(variants_a[k].get("variant") or {})
        for k in sorted(keys_a - keys_b, key=lambda k: (k[0], k[1] if isinstance(k[1], int) else 0))
    ]

    return {
        "classification_changes": classification_changes,
        "unexplained_changes": unexplained_changes,
        "evidence_changes": evidence_changes,
        "confidence_changes": confidence_changes,
        "provenance_diffs": provenance_diffs,
        "added_variants": added_variants,
        "removed_variants": removed_variants,
        "code_version_a": code_version_a,
        "code_version_b": code_version_b,
        "code_version_changed": code_version_changed,
        "common_variant_count": len(common_keys),
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def render_markdown(diff: Dict[str, Any], doc_a: Dict[str, Any], doc_b: Dict[str, Any], dir_a: str, dir_b: str) -> str:
    lines: List[str] = ["# GEPER Report Comparison", ""]
    lines.append(
        f"- **Run A:** `{dir_a}` (generated {doc_a.get('generated_at', 'unknown')}, code version `{diff['code_version_a'] or 'unknown'}`)"
    )
    lines.append(
        f"- **Run B:** `{dir_b}` (generated {doc_b.get('generated_at', 'unknown')}, code version `{diff['code_version_b'] or 'unknown'}`)"
    )
    lines.append(f"- **Variants compared (present in both runs):** {diff['common_variant_count']}")
    lines.append(f"- **Code version changed between runs:** {'Yes' if diff['code_version_changed'] else 'No'}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Section 1 (rendered first, deliberately, ahead of the plain
    # classification-change list): the "more concerning case" the task
    # this script was built for calls out explicitly -- a classification
    # moved with no data-source version change AND no code-version
    # change to explain it.
    lines.append("## ⚠ Unexplained Classification Changes (no matching data or code version change)")
    lines.append("")
    if diff["unexplained_changes"]:
        lines.append(
            "*These changed classification between the two runs, but none of the evidence sources this "
            "variant cites show a provenance version difference, and GEPER's own code version is identical "
            "between the two runs. This suggests a code-behavior difference the version-pinning above did "
            "not capture (or non-determinism) rather than a genuine data update -- investigate before "
            "trusting either result.*"
        )
        lines.append("")
        lines.append("| Variant | Run A | Run B | Cited Evidence Sources |")
        lines.append("|---|---|---|---|")
        for c in diff["unexplained_changes"]:
            lines.append(
                f"| {c['locus']} | {c['classification_a'] or 'Not classified'} | "
                f"{c['classification_b'] or 'Not classified'} | {', '.join(c['cited_sources']) or 'none'} |"
            )
        lines.append("")
    else:
        lines.append(
            "*None -- every classification change between the two runs is explained by either a "
            "cited data-source version difference or a code version change (see below).*"
        )
        lines.append("")
    lines.append("---")
    lines.append("")

    lines.append("## Classification Changes")
    lines.append("")
    if diff["classification_changes"]:
        lines.append("| Variant | Run A Classification | Run A Confidence | Run B Classification | Run B Confidence |")
        lines.append("|---|---|---|---|---|")
        for c in diff["classification_changes"]:
            lines.append(
                f"| {c['locus']} | {c['classification_a'] or 'Not classified'} | {c['confidence_a'] or 'n/a'} | "
                f"{c['classification_b'] or 'Not classified'} | {c['confidence_b'] or 'n/a'} |"
            )
        lines.append("")
    else:
        lines.append("*No classification changes for any variant present in both runs.*")
        lines.append("")

    lines.append("## Confidence Changes")
    lines.append("")
    if diff["confidence_changes"]:
        lines.append(
            "*Every variant present in both runs whose numeric confidence score differs -- shown "
            "independently of whether the classification or confidence label also changed, since a "
            'label like "Moderate" can cover a wide score range and hide a real shift (e.g. 55.6 -> '
            "64.8, same label, same classification) that would otherwise appear nowhere in this report.*"
        )
        lines.append("")
        lines.append("| Variant | Classification (A → B) | Confidence A | Confidence B | Δ Score |")
        lines.append("|---|---|---|---|---|")
        for c in diff["confidence_changes"]:
            cls_shown = (
                f"{c['classification_a'] or 'Not classified'} → {c['classification_b'] or 'Not classified'}"
                if c["classification_a"] != c["classification_b"]
                else (c["classification_a"] or "Not classified")
            )
            score_a, score_b = c["confidence_score_a"], c["confidence_score_b"]
            delta = (
                f"{score_b - score_a:+.1f}"
                if isinstance(score_a, (int, float)) and isinstance(score_b, (int, float))
                else "n/a"
            )
            conf_a = f"{c['confidence_label_a'] or 'n/a'} ({score_a if score_a is not None else 'n/a'})"
            conf_b = f"{c['confidence_label_b'] or 'n/a'} ({score_b if score_b is not None else 'n/a'})"
            lines.append(f"| {c['locus']} | {cls_shown} | {conf_a} | {conf_b} | {delta} |")
        lines.append("")
    else:
        lines.append("*No confidence-score changes for any variant present in both runs.*")
        lines.append("")

    lines.append("## Added / Removed Variants")
    lines.append("")
    lines.append(f"**Added in Run B only ({len(diff['added_variants'])}):**")
    lines.append("")
    if diff["added_variants"]:
        lines.extend(f"- {v}" for v in diff["added_variants"])
    else:
        lines.append("*None.*")
    lines.append("")
    lines.append(f"**Present in Run A only, missing from Run B ({len(diff['removed_variants'])}):**")
    lines.append("")
    if diff["removed_variants"]:
        lines.extend(f"- {v}" for v in diff["removed_variants"])
    else:
        lines.append("*None.*")
    lines.append("")

    lines.append("## Evidence Source Changes")
    lines.append("")
    if diff["evidence_changes"]:
        lines.append("| Variant | Newly Matched (B only) | Newly Unmatched (A only) |")
        lines.append("|---|---|---|")
        for e in diff["evidence_changes"]:
            lines.append(
                f"| {e['locus']} | {', '.join(e['newly_matched']) or '—'} | {', '.join(e['newly_unmatched']) or '—'} |"
            )
        lines.append("")
    else:
        lines.append("*No evidence-source changes for any variant present in both runs.*")
        lines.append("")

    lines.append("## Data-Source Provenance Differences")
    lines.append("")
    if diff["provenance_diffs"]:
        lines.append(
            "*Every known data source (`pipeline/provenance.py`) whose recorded status/version/release date/"
            'content hash differs between the two runs -- this is what "which ClinVar/ClinGen versions '
            'differ" is answered from.*'
        )
        lines.append("")
        for d in diff["provenance_diffs"]:
            lines.append(f"### {d['source']}")
            lines.append("")
            lines.append(f"- **Run A:** {_format_provenance_record(d['a'])}")
            lines.append(f"- **Run B:** {_format_provenance_record(d['b'])}")
            lines.append("")
    else:
        lines.append("*No data-source provenance differences recorded between the two runs.*")
        lines.append("")

    return "\n".join(lines)


def _format_provenance_record(record: Optional[Dict[str, Any]]) -> str:
    if record is None:
        return "not present in this run's provenance output"
    status = record.get("status", "unknown")
    parts = [f"status={status}"]
    if record.get("version"):
        parts.append(f"version={record['version']}")
    if record.get("release_date"):
        parts.append(f"release_date={record['release_date']}")
    if record.get("content_hash"):
        parts.append(
            f"content_hash={record['content_hash'][:16]}... ({record.get('hash_algorithm') or 'unknown algorithm'})"
        )
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Diff two GEPER output directories (each containing geper_results.json) and render a Markdown report."
    )
    parser.add_argument("run_a", help="Path to the first (baseline) GEPER output directory.")
    parser.add_argument("run_b", help="Path to the second (comparison) GEPER output directory.")
    parser.add_argument(
        "--output",
        default=None,
        help="Write the Markdown diff report to this path instead of printing it to stdout.",
    )
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    try:
        doc_a = load_document(args.run_a)
        doc_b = load_document(args.run_b)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
        return 2  # unreachable -- parser.error() calls sys.exit() itself; kept for readability/type-checkers

    diff = diff_reports(doc_a, doc_b)
    report = render_markdown(diff, doc_a, doc_b, args.run_a, args.run_b)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(report)
        print(f"Wrote comparison report to '{args.output}'.")
    else:
        _print_safely(report)

    return 0


def _print_safely(text: str) -> None:
    """
    `print(text)` when stdout can represent it as-is, otherwise falls
    back to writing UTF-8 bytes with unsupported characters replaced
    rather than letting `UnicodeEncodeError` crash the whole CLI over a
    single glyph (e.g. "⚠"/em dash on a Windows console still defaulting
    to a legacy codepage like cp1252). Only affects the "print straight
    to the terminal" path -- `--output <path>` always writes the file as
    UTF-8 regardless of the console's encoding, so redirecting output
    to a file is never affected by this at all.
    """
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.buffer.write(text.encode(encoding, errors="replace"))
        sys.stdout.buffer.write(b"\n")


if __name__ == "__main__":
    sys.exit(main())
