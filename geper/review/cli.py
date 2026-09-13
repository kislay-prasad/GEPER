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

import os

from component_identity import COMPONENT_NAME, SHORT_NAME
from review.signoff import ClinicalCredentials
from review.signoff import approve as _approve
from review.signoff import list_pending as _list_pending
from review.signoff import override as _override
from review.signoff import withdraw as _withdraw
from utils.exceptions import SignoffError
from utils.logger import get_logger

logger = get_logger(__name__)


_PASSWORD_ENV_DEFAULT = "GEPER_CLINICAL_PASSWORD"


class _RefusePasswordOnTheCommandLine(argparse.Action):
    """
    `--clinical-password` exists ONLY to be refused, and it has to exist.

    Without it, argparse's prefix matching accepts `--clinical-password` as an
    unambiguous abbreviation of `--clinical-password-env` -- so an operator
    reaching for the obvious flag would have their PASSWORD silently bound to
    the variable-NAME argument and written into shell history and process
    listings, which is precisely what naming the variable instead of the
    secret was for. Declaring it explicitly removes the abbreviation and turns
    a silent leak into an error that says what to do instead.

    `nargs=0` so the password is never consumed as an argument value at all:
    argparse errors on the flag itself, before the secret is bound to
    anything.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        parser.error(
            f"{option_string} is not accepted: a password on the command line is readable by every "
            f"process on this machine and is recorded in shell history. Put the password in an "
            f"environment variable and name that variable with --clinical-password-env (default: "
            f"{_PASSWORD_ENV_DEFAULT})."
        )


def _add_clinical_arguments(subparser: argparse.ArgumentParser) -> None:
    """
    The identity a LINKED run's sign-off is recorded under (w117).

    THERE IS NO --clinical-password, deliberately and permanently. A password
    on a command line is visible to every process on the machine, lands in the
    shell's history file and is copied into any log that echoes the invocation.
    The password is named INDIRECTLY, by the environment variable that holds
    it, so the secret never becomes an argv element.

    Absent on an unlinked run's sign-off, which needs no clinical identity at
    all -- these are optional here for exactly that reason, and their absence
    on a LINKED run is refused by review/signoff.py with a message naming them.
    """
    subparser.add_argument(
        "--clinical-email",
        help="The clinical platform login of the person signing. Required for a run linked to a "
        "clinical record (one carrying clinical_link.json); ignored otherwise.",
    )
    subparser.add_argument(
        "--clinical-password-env",
        default=_PASSWORD_ENV_DEFAULT,
        help=f"NAME of the environment variable holding that login's password (default: "
        f"{_PASSWORD_ENV_DEFAULT}). The password itself is never passed on the command line, "
        "where it would be readable by every process on the machine and recorded in shell history.",
    )
    subparser.add_argument(
        "--clinical-password",
        action=_RefusePasswordOnTheCommandLine,
        nargs=0,
        help=argparse.SUPPRESS,
    )
    subparser.add_argument(
        "--clinical-totp",
        help="Current TOTP code, if the clinical account is enrolled in two-factor authentication.",
    )


def _clinical_credentials(args: argparse.Namespace) -> Optional[ClinicalCredentials]:
    """
    Credentials, or None when no --clinical-email was given.

    None is NOT "sign off without an identity": review/signoff.py refuses a
    linked run that reaches it without credentials. It only means this
    invocation supplied none, which is the correct and complete answer for an
    unlinked run.

    A named-but-empty password variable is an error rather than an empty
    password, because the overwhelmingly likely cause is a variable that was
    never exported, and attempting a login with "" would report it as a
    credential failure the operator would then debug in the wrong place.
    """
    if not getattr(args, "clinical_email", None):
        return None
    env_name = args.clinical_password_env
    password = os.getenv(env_name)
    if not password:
        raise SignoffError(
            f"--clinical-email was given but the environment variable '{env_name}' is unset or "
            f"empty, so there is no password to authenticate with. Export it (or name a different "
            f"variable with --clinical-password-env). The password is never accepted as a "
            f"command-line argument."
        )
    return ClinicalCredentials(email=args.clinical_email, password=password, totp_code=args.clinical_totp)


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
    # R10: these three are TYPED IN, with no user record behind them -- on the
    # filesystem-only path this CLI was written for, there is no clinical
    # database to read. The manifest records that (identity_source:
    # "typed_in"), so an auditor can tell a typed identity from one taken from
    # the signing account (see review/signoff.py::ClinicianIdentity).
    #
    # NOT `required=True` any more (w117 join, 2026-09-13), and the rule that
    # replaced it is in `_require_typed_identity_when_unlinked` below: with
    # --clinical-email the sign-off authenticates against the clinical
    # platform and the identity is READ FROM THAT ACCOUNT'S RECORD, so
    # demanding the operator retype it would mean retyping values that must
    # match the record exactly or be refused -- ceremony that can only
    # introduce the disagreement R10 exists to prevent. Without
    # --clinical-email nothing changes: all three are still demanded, by the
    # explicit check rather than by argparse.
    approve_parser.add_argument(
        "--clinician-name",
        help='e.g. "Dr. Rajesh Sharma". Typed in: recorded in the manifest as identity_source "typed_in". '
        "Required unless --clinical-email is given, in which case the identity is read from that account.",
    )
    approve_parser.add_argument("--reg-number", help='Medical registration number, e.g. "MCI-12345".')
    approve_parser.add_argument("--hospital", help='Hospital/lab name, e.g. "AIIMS Delhi".')
    approve_parser.add_argument(
        "--reason",
        help="The signatory's stated grounds for concurring. REQUIRED for a run linked to a "
        "clinical record (one carrying clinical_link.json), where it is recorded as the "
        "'sole_signatory' claim's reason. Ignored for an unlinked run.",
    )
    _add_clinical_arguments(approve_parser)

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
    _add_clinical_arguments(override_parser)

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
    # w119: a withdrawal on a LINKED run has to read the clinical report's
    # state before it touches a file, and reading it is an authenticated act --
    # so this subcommand takes the same clinical credentials approve/override
    # do. Absent on an unlinked run, exactly as there.
    _add_clinical_arguments(withdraw_parser)

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


def _require_typed_identity_when_unlinked(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """
    What `required=True` on --clinician-name/--reg-number/--hospital used to
    say, now said conditionally (w117 join, 2026-09-13).

    WITHOUT --clinical-email nothing has changed: all three are demanded, and
    the operator gets argparse's own usage error rather than a traceback from
    deeper in.

    WITH --clinical-email the sign-off authenticates against the clinical
    platform, and if the run is linked the identity is read from that
    account's record -- so demanding the three here would force the operator
    to retype values that must match the record exactly or be refused. That is
    ceremony whose only possible outcomes are "identical" and "refused", and
    the second one is the disagreement R10 exists to prevent, invited by the
    interface rather than by anybody's mistake.

    Values typed anyway are NOT ignored: review/signoff.py still compares them
    against the record and refuses a contradiction. This only stops the CLI
    from insisting on them.

    Kept in the CLI rather than pushed into signoff.approve because it is a
    statement about THIS command's arguments: the library function's own rule
    (all three, or an identity) is already enforced by
    _resolve_signing_identity and is not weakened here.
    """
    if getattr(args, "clinical_email", None):
        return
    missing = [
        flag
        for flag, value in (
            ("--clinician-name", args.clinician_name),
            ("--reg-number", args.reg_number),
            ("--hospital", args.hospital),
        )
        if not (value or "").strip()
    ]
    if missing:
        parser.error(
            f"approve: the following arguments are required: {', '.join(missing)} "
            "(a clinical report shows the signing clinician's name, registration number and "
            "hospital). Alternatively pass --clinical-email to sign a run that is linked to a "
            "clinical record, and the identity is read from that account."
        )


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "approve":
            _require_typed_identity_when_unlinked(parser, args)
            manifest = _approve(
                output_dir=args.output_dir,
                clinician_name=args.clinician_name,
                reg_number=args.reg_number,
                hospital=args.hospital,
                clinical_credentials=_clinical_credentials(args),
                clinical_reason=args.reason,
            )
            print(f"Approved. Manifest: {manifest}")
        elif args.command == "override":
            record = _override(
                output_dir=args.output_dir,
                variant_key=args.variant,
                new_classification=args.new_classification,
                reason=args.reason,
                clinician_id=args.clinician_id,
                clinical_credentials=_clinical_credentials(args),
            )
            print(f"Override applied: {record}")
        elif args.command == "list-pending":
            rows = _list_pending(search_root=args.search_root, show_all=args.show_all)
            _print_pending_table(rows)
        elif args.command == "withdraw":
            result = _withdraw(
                output_dir=args.output_dir,
                reason=args.reason,
                actor=args.actor,
                clinical_credentials=_clinical_credentials(args),
            )
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
