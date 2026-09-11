"""
Clinician review workflow -- CLI entry point.

Usage (run from inside the `geper/` directory, matching `main.py`'s own
invocation convention -- see that module's docstring):

    python review/cli.py approve --output-dir ./geper_output \\
        --clinician-name "Dr. Rajesh Sharma" --reg-number "MCI-12345" \\
        --hospital "AIIMS Delhi"

    python review/cli.py override --output-dir ./geper_output \\
        --variant "17:43106534:C>A" --new-classification "Likely Pathogenic" \\
        --reason "Additional family history of early-onset breast cancer" \\
        --clinician-id "rajesh.sharma@aiims.edu"

    python review/cli.py list-pending --search-root ./geper_output

    python review/cli.py withdraw --output-dir ./geper_output \\
        --reason "Signed off in error" --actor "rajesh.sharma@aiims.edu"

This codebase has no installed `geper` console script and does not use
Click anywhere (confirmed: plain `argparse` throughout, see
`main.py::build_arg_parser`) -- this module follows that same plain-
argparse, `build_arg_parser()` + `main() -> int` +
`sys.exit(main())` shape, with one `ArgumentParser` per subcommand
(`approve`/`override`/`list-pending`/`withdraw`) rather than four
separate scripts, since they share `--output-dir` and operate on the
same review workflow.

All the actual logic lives in `review/signoff.py`; this module is a
thin argument-parsing and error-reporting layer over it, matching how
thin `main.py` itself is over `pipeline/orchestrator.py::GeperPipeline`.
"""

import argparse
import sys
from typing import Any, Dict, List, Optional

from component_identity import COMPONENT_NAME, SHORT_NAME
from review.signoff import approve as _approve
from review.signoff import list_pending as _list_pending
from review.signoff import override as _override
from review.signoff import withdraw as _withdraw
from utils.exceptions import SignoffError
from utils.logger import get_logger

logger = get_logger(__name__)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="geper-signoff",
        description=f"Clinician review workflow for {COMPONENT_NAME} runs -- approve a run, override a classification, list runs awaiting review, or withdraw a standing sign-off.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    approve_parser = subparsers.add_parser(
        "approve",
        help="Move a run from DRAFT to REVIEWED: regenerates both clinical PDFs with the reviewing clinician's identity in the footer, and writes a signed manifest.",
    )
    approve_parser.add_argument(
        "--output-dir",
        required=True,
        help=f"An existing {SHORT_NAME} --output-dir (must already contain geper_results.json from a completed run).",
    )
    approve_parser.add_argument("--clinician-name", required=True, help='e.g. "Dr. Rajesh Sharma".')
    approve_parser.add_argument("--reg-number", required=True, help='Medical registration number, e.g. "MCI-12345".')
    approve_parser.add_argument("--hospital", required=True, help='Hospital/lab name, e.g. "AIIMS Delhi".')

    override_parser = subparsers.add_parser(
        "override",
        help=f"Layer a clinician's classification override on top of one variant's {SHORT_NAME}-derived result (never replaces it), and regenerate all three report formats.",
    )
    override_parser.add_argument("--output-dir", required=True, help=f"An existing {SHORT_NAME} --output-dir.")
    override_parser.add_argument(
        "--variant",
        required=True,
        help="'chrom:pos:ref>alt', e.g. '17:43106534:C>A' (matches VCF-normalized coordinates).",
    )
    override_parser.add_argument("--new-classification", required=True, help='e.g. "Likely Pathogenic".')
    override_parser.add_argument("--reason", required=True, help="Free-text clinical justification for the override.")
    override_parser.add_argument(
        "--clinician-id", required=True, help="Identifies who made the override, e.g. an email address."
    )

    list_parser = subparsers.add_parser(
        "list-pending",
        help=f"Recursively scan a directory tree for {SHORT_NAME} runs and their DRAFT/REVIEWED review status.",
    )
    list_parser.add_argument(
        "--search-root", required=True, help="Directory to recursively search for geper_results.json files."
    )
    list_parser.add_argument(
        "--all", action="store_true", dest="show_all", help="Also list REVIEWED runs (default: DRAFT only)."
    )

    withdraw_parser = subparsers.add_parser(
        "withdraw",
        help="Retract a standing sign-off: removes the signed manifest and resets review_status to DRAFT if it was REVIEWED, then regenerates all three report formats.",
    )
    withdraw_parser.add_argument("--output-dir", required=True, help=f"An existing {SHORT_NAME} --output-dir.")
    withdraw_parser.add_argument("--reason", required=True, help="Free-text reason the sign-off is being withdrawn.")
    withdraw_parser.add_argument(
        "--actor", required=True, help="Identifies who is withdrawing the sign-off, e.g. an email address."
    )

    return parser


def _print_pending_table(rows: List[Dict[str, Any]]) -> None:
    headers = ("output_dir", "report_date", "num_variants", "has_conflicting_evidence", "status")
    if not rows:
        print("No matching runs found.")
        return
    widths = {h: max(len(h), max(len(str(row.get(h, ""))) for row in rows)) for h in headers}
    header_line = " | ".join(h.ljust(widths[h]) for h in headers)
    print(header_line)
    print("-+-".join("-" * widths[h] for h in headers))
    for row in rows:
        print(" | ".join(str(row.get(h, "")).ljust(widths[h]) for h in headers))


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "approve":
            manifest = _approve(
                output_dir=args.output_dir,
                clinician_name=args.clinician_name,
                reg_number=args.reg_number,
                hospital=args.hospital,
            )
            print(f"Approved. Manifest: {manifest}")
        elif args.command == "override":
            record = _override(
                output_dir=args.output_dir,
                variant_key=args.variant,
                new_classification=args.new_classification,
                reason=args.reason,
                clinician_id=args.clinician_id,
            )
            print(f"Override applied: {record}")
        elif args.command == "list-pending":
            rows = _list_pending(search_root=args.search_root, show_all=args.show_all)
            _print_pending_table(rows)
        elif args.command == "withdraw":
            result = _withdraw(output_dir=args.output_dir, reason=args.reason, actor=args.actor)
            print(f"Withdrawn: {result}")
        else:  # pragma: no cover - argparse's `required=True` on the subparsers already prevents this
            parser.error(f"Unknown command '{args.command}'.")
    except SignoffError as exc:
        logger.error(f"geper-signoff {args.command} failed: {exc}")
        return 1
    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
        return 130

    return 0


if __name__ == "__main__":
    sys.exit(main())
