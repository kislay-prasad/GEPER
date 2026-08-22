"""
Audit script for C1 (report review round 2): find stored GEPER outputs
whose classification was computed under the pre-fix combining-rule bug
(`pipeline/acmg_rules.py::ACMGRuleEngine._combine` thresholded
`path_points`/`benign_points` independently instead of on `net =
path_points - benign_points` -- see the fix committed on
fix/report-review-A1-B7).

No `geper_results.json` or other run artifacts exist anywhere in this
repository (working tree or git history) to audit directly -- this
script is the deliverable for C1 instead: point it at wherever your
real archived outputs live (a single geper_results.json, or a
directory of them from multiple runs) and it will report every
affected variant.

How detection works: every `combining_rule_trace` (old code and new
code alike) ends with a line of the exact form
    "Pathogenic points = <P>, benign points = <B>, net = <N>."
because that line has been unchanged since before this fix -- only
which of (path_points, benign_points) vs net drove the *decision*
changed. So this script re-derives what each of the old and new
threshold logic would output from those same printed points, and flags
any stored variant where the *stored* classification matches the old
logic's answer but disagrees with the new logic's answer. It does not
need to know which code version produced the file.

Usage:
    python audit_net_points_blast_radius.py path/to/geper_results.json
    python audit_net_points_blast_radius.py path/to/output_dir/         # globs **/*.json
"""

import glob
import json
import os
import re
import sys
from report.clinical_report_builder import candidate_interpretation_of
from typing import Any, Dict, List, Tuple

_TRACE_RE = re.compile(r"Pathogenic points = ([\d.]+), benign points = ([\d.]+), net = (-?[\d.]+)\.")


def _old_classification(path_points: float, benign_points: float) -> str:
    if benign_points >= 8:
        return "Benign"
    if benign_points >= 4 and path_points < 4:
        return "Likely Benign"
    if path_points >= 10:
        return "Pathogenic"
    if path_points >= 6:
        return "Likely Pathogenic"
    return "Uncertain Significance"


def _new_classification(net: float) -> str:
    if net <= -7:
        return "Benign"
    if net <= -1:
        return "Likely Benign"
    if net >= 10:
        return "Pathogenic"
    if net >= 6:
        return "Likely Pathogenic"
    return "Uncertain Significance"


def _iter_variant_records(document: Dict[str, Any]):
    """Yields (locus_label, acmg_classification_dict) for every variant
    in one geper_results.json document, tolerant of either the
    `variants: [...]` (multi-variant run) or single-`variant` shape
    `report/summary.py::generate_pdf` also accepts."""
    if "variants" in document:
        records = document.get("variants") or []
    elif "variant" in document:
        records = [document]
    else:
        return
    for rec in records:
        variant = rec.get("variant") or {}
        locus = f"{variant.get('chrom')}:{variant.get('pos')} {variant.get('ref')}>{variant.get('alt')}"
        gene = (rec.get("interpretation_result") or {}).get("gene_symbol")
        label = f"{locus} ({gene})" if gene else locus
        clinical = candidate_interpretation_of(rec) or {}
        acmg = clinical.get("acmg_classification") or {}
        yield label, acmg


def audit_file(path: str) -> List[Tuple[str, str, str, float, float, float]]:
    """Returns a list of (label, stored_classification, new_classification,
    path_points, benign_points, net) for every affected variant in this file."""
    with open(path, "r", encoding="utf-8") as fh:
        document = json.load(fh)

    affected = []
    for label, acmg in _iter_variant_records(document):
        stored_classification = acmg.get("classification")
        trace = acmg.get("combining_rule_trace") or []
        points_line = next((line for line in trace if _TRACE_RE.search(line)), None)
        if not points_line or not stored_classification:
            continue
        m = _TRACE_RE.search(points_line)
        path_points, benign_points, net = (float(g) for g in m.groups())

        old_answer = _old_classification(path_points, benign_points)
        new_answer = _new_classification(net)
        if old_answer == new_answer:
            continue  # this record's points don't cross the bug's divergence condition at all

        # If the file's own stored classification matches what the OLD
        # (buggy) logic would have produced -- and disagrees with what
        # the fixed logic produces -- this record was filed under the
        # bug and its classification is now stale.
        if stored_classification == old_answer and stored_classification != new_answer:
            affected.append((label, stored_classification, new_answer, path_points, benign_points, net))
    return affected


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1

    paths: List[str] = []
    for arg in argv[1:]:
        if arg.endswith(".json"):
            paths.append(arg)
        else:
            paths.extend(glob.glob(os.path.join(arg, "**", "*.json"), recursive=True))

    if not paths:
        print("No .json files found at the given path(s).")
        return 1

    total_affected = 0
    for path in paths:
        try:
            affected = audit_file(path)
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            print(f"SKIP {path}: could not read as a GEPER results document ({exc}).")
            continue
        if not affected:
            continue
        print(f"\n{path}:")
        for label, stored, corrected, p, b, n in affected:
            total_affected += 1
            print(
                f"  {label}: stored classification = '{stored}' (from path_points={p}, "
                f"benign_points={b}) -- corrected (net={n}) classification = '{corrected}'"
            )

    print(f"\n{total_affected} affected variant record(s) found across {len(paths)} file(s) checked.")
    print(
        "Note: 'affected' means the stored classification would read differently once re-run "
        "under the fixed combiner -- it does NOT mean the underlying evidence changed, only the "
        "arithmetic that turned already-triggered criteria into a final label."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
