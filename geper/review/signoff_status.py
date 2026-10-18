"""
review/signoff_status.py
────────────────────────

`status`: a READ-ONLY classification of where a run's sign-off stands, comparing what the clinical service
reports (authenticated) with what is in the run directory.

WHY. A linked sign-off is eight separate durable steps (docs/SIGNOFF_RECOVERY_DESIGN.md, section 1) and any of them can
be interrupted. Today the only tool answer to a half-finished sign-off is a refusal. This module says WHICH half-finished
state a run is in, with the evidence, so a person can decide. It does not repair anything.

WHAT IT READS
  * the run directory: clinical_link.json, geper_results.json, geper_signoff_manifest.json, the two PDFs (hashed, never
    printed), geper_signoff_audit.log (does an `approved` line exist), and whether a recovery journal file exists
    (presence only; no journal is read, created or modified here);
  * the clinical service: `GET /review/reports/{id}/signoff-state`, read TWICE and required to agree.

WHAT IT NEVER DOES
  * It issues no review write: no claim, submission, approval, override, withdrawal or release, and no PATCH/PUT/DELETE.
    The only non-GET requests that can occur are the credential exchange the existing adapter performs to obtain and end
    a session (`POST /auth/login`, and `POST /auth/logout` when the adapter opened the session). Those create and end a
    server-side session (and are audited by the service like any login); they change no report.
  * It writes no file: no journal, no log, no marker, nothing under the run directory.
  * It never says report content is verified. Content binding between the signed files and the approved server content
    does not exist yet (design doc, Decision 1: P-BIND), so every output states "Content binding: NOT VERIFIED". The local
    self-consistency check it does report (the document still hashes to the hash stored in it) proves only that the JSON was
    not edited since it was hashed.
  * It never guesses. A service it cannot reach, an answer that is malformed, a session it cannot authenticate, or a
    server state that changes between the two reads yields UNREADABLE, not a best-effort classification.

WHAT A "CONSISTENT" OUTCOME REQUIRES (and never less)
  * NOT_STARTED: the service says draft with no claims; the local files are unsigned (not 'reviewed', no manifest).
  * SIGNED_OFF_* / RELEASED_*: the service says approved/released; the operator's identity is established (a UUID from the
    authenticated session); the local JSON says reviewed, still matches its own stored hash, and has a valid manifest whose
    PDF/JSON hashes match the files, whose content hash matches, and which names THIS report and the SAME approver as the
    service; a local audit line exists. A reviewed JSON or a manifest NEVER establishes approval on its own.
  * UNLINKED_*: no clinical record is consulted at all, so nothing is claimed about the service; a manifest that names a
    clinical record with no clinical_link.json is investigated, not accepted.

OUTPUT NEVER CONTAINS claim reasons, patient data, credentials, passwords, TOTP codes, session identifiers, or any raw hash
value (hashes are compared and reported only as match / mismatch).

EXIT CODES (deterministic; 1 is the CLI's generic error and 2 is argparse's usage error, so neither is reused)
    0   CONSISTENT   nothing in progress, or a sign-off whose server state and local files agree
   10   INCOMPLETE   a sign-off is in progress or lacks only a local audit line; nothing contradicts anything
   20   INVESTIGATE  a contradiction or an unsafe state; a person must look (includes every HOLD)
   30   UNREADABLE   the state could not be established; NO classification is offered
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from report.models import verify_content_hash
from review.signoff import (
    AUDIT_LOG_FILENAME,
    FULL_PDF_FILENAME,
    MANIFEST_FILENAME,
    RESULTS_FILENAME,
    SHORT_PDF_FILENAME,
    ClinicalCredentials,
    _link_ids,
    _open_clinical,
    _sha256_file,
    read_clinical_link,
)
from utils.exceptions import SignoffError

# The recovery journal (a later change) will live here. This module only asks whether the file EXISTS.
JOURNAL_FILENAME = "geper_signoff_journal.jsonl"

EXIT_CONSISTENT = 0
EXIT_INCOMPLETE = 10
EXIT_INVESTIGATE = 20
EXIT_UNREADABLE = 30

CONSISTENT, INCOMPLETE, INVESTIGATE, UNREADABLE = "CONSISTENT", "INCOMPLETE", "INVESTIGATE", "UNREADABLE"
_EXIT = {
    CONSISTENT: EXIT_CONSISTENT,
    INCOMPLETE: EXIT_INCOMPLETE,
    INVESTIGATE: EXIT_INVESTIGATE,
    UNREADABLE: EXIT_UNREADABLE,
}

BINDING_NOTICE = (
    "Content binding: NOT VERIFIED. This tool cannot show that the local files are the content the clinical service "
    "approved (no such mechanism exists yet); 'consistent' means only that what it can compare does not contradict."
)
NO_RECOVERY_NOTICE = "No automated recovery exists yet. This command reads and reports; it changes nothing."

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


# ══ facts ═════════════════════════════════════════════════════════════════════════════════════════════════════════


@dataclass
class LocalFacts:
    """What the run directory shows. Built by reading files only."""

    readable: bool = True
    error: Optional[str] = None
    review_status: Optional[str] = None
    document_hash: str = "absent"  # "absent" | "self_consistent" | "inconsistent": the JSON against ITS OWN stored hash
    override_claim_ids: Set[str] = field(default_factory=set)
    override_count: int = 0
    override_invalid: int = 0  # override records with no usable claim id (missing, null, non-string, not a UUID)
    manifest: str = "absent"  # "absent" | "valid" | "invalid"
    manifest_problems: List[str] = field(default_factory=list)
    manifest_clinical: Dict[str, str] = field(default_factory=dict)
    full_pdf_present: bool = False
    short_pdf_present: bool = False
    audit_has_approved: bool = False
    journal_present: bool = False

    @property
    def reviewed(self) -> bool:
        return self.review_status == "reviewed"

    @property
    def hold(self) -> bool:
        """A results JSON that says `reviewed` without a valid manifest: the interim HOLD state (design Decision 3)."""
        return self.reviewed and self.manifest != "valid"


@dataclass
class ServerFacts:
    """The clinical service's answer, reduced to what classification needs. Claim reasons are never part of it."""

    state: str
    interpretation_id: uuid.UUID
    is_retracted: bool
    approver_id: Optional[uuid.UUID]
    has_content_hash: bool
    claims: List[Dict[str, Any]]

    def approver_relation(self, user_id: Optional[uuid.UUID]) -> Optional[str]:
        if self.approver_id is None:
            return None
        return "you" if self.approver_id == user_id else "another_user"


@dataclass
class StatusResult:
    group: str
    code: str
    summary: str
    reasons: List[str] = field(default_factory=list)
    hold: bool = False
    report_id: Optional[str] = None
    server: Optional[Dict[str, Any]] = None
    local: Optional[Dict[str, Any]] = None
    next_steps: List[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return _EXIT[self.group]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "group": self.group,
            "code": self.code,
            "exit_code": self.exit_code,
            "hold": self.hold,
            "summary": self.summary,
            "reasons": list(self.reasons),
            "report_id": self.report_id,
            "server": self.server,
            "local": self.local,
            "next_steps": list(self.next_steps),
            "content_binding": "NOT VERIFIED",
            "read_only": True,
        }


# ══ local inspection (file reads only) ════════════════════════════════════════════════════════════════════════════


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _inspect_manifest(output_dir: str, document: Dict[str, Any], facts: LocalFacts) -> None:
    path = os.path.join(output_dir, MANIFEST_FILENAME)
    if not os.path.exists(path):
        facts.manifest = "absent"
        return
    problems: List[str] = []
    try:
        manifest = _load_json(path)
    except (OSError, ValueError):
        facts.manifest, facts.manifest_problems = "invalid", ["manifest_unparseable"]
        return
    if not isinstance(manifest, dict):
        facts.manifest, facts.manifest_problems = "invalid", ["manifest_not_an_object"]
        return

    for key, filename, missing, differs in (
        ("pdf_sha256", FULL_PDF_FILENAME, "full_pdf_missing", "full_pdf_differs_from_manifest"),
        ("short_pdf_sha256", SHORT_PDF_FILENAME, "short_pdf_missing", "short_pdf_differs_from_manifest"),
    ):
        recorded = manifest.get(key)
        if not isinstance(recorded, str) or not _HEX64.match(recorded):
            problems.append(f"manifest_lacks_{key}")
            continue
        file_path = os.path.join(output_dir, filename)
        if not os.path.exists(file_path):
            problems.append(missing)
        elif _sha256_file(file_path) != recorded:
            problems.append(differs)
    recorded_results = manifest.get("results_json_sha256")
    if not isinstance(recorded_results, str) or not _HEX64.match(recorded_results):
        problems.append("manifest_lacks_results_json_sha256")
    elif _sha256_file(os.path.join(output_dir, RESULTS_FILENAME)) != recorded_results:
        problems.append("results_json_changed_since_signoff")
    manifest_hash, document_hash = manifest.get("content_hash"), document.get("content_hash")
    if manifest_hash and document_hash and manifest_hash != document_hash:
        problems.append("manifest_hash_differs_from_document")
    elif document_hash and not manifest_hash:
        problems.append("manifest_lacks_content_hash")

    clinical = manifest.get("clinical")
    if isinstance(clinical, dict):
        # Only real identities are kept, in canonical form. A value that is present but is not a UUID is kept as a marker
        # that can never equal an identity; a missing, null or empty one is simply absent.
        facts.manifest_clinical = {}
        for k in ("org_id", "interpretation_id", "report_id", "approver_user_id"):
            value = clinical.get(k)
            canonical = _canonical_uuid(value)
            if canonical:
                facts.manifest_clinical[k] = canonical
            elif value not in (None, ""):
                facts.manifest_clinical[k] = _MALFORMED_IDENTITY
    facts.manifest = "valid" if not problems else "invalid"
    facts.manifest_problems = problems


_MALFORMED_IDENTITY = "<not-a-uuid>"


def _canonical_uuid(value: Any) -> Optional[str]:
    """The canonical lower-case spelling of a UUID given as a UUID or a UUID string; None for anything else."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, str):
        try:
            return str(uuid.UUID(value))
        except ValueError:
            return None
    return None


def _override_claim_ids(document: Dict[str, Any]) -> Tuple[Set[str], int, int]:
    ids: Set[str] = set()
    count = 0
    invalid = 0
    for variant in document.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        for override in variant.get("overrides") or []:
            count += 1
            clinical = override.get("clinical") if isinstance(override, dict) else None
            claim_id = _canonical_uuid(clinical.get("claim_id")) if isinstance(clinical, dict) else None
            if claim_id:
                ids.add(claim_id)
            else:
                invalid += 1
    return ids, count, invalid


def _audit_has_approved(output_dir: str) -> bool:
    path = os.path.join(output_dir, AUDIT_LOG_FILENAME)
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue  # a torn or foreign line is not an `approved` entry
                if isinstance(entry, dict) and entry.get("action") == "approved":
                    return True
    except OSError:
        return False
    return False


def inspect_local(output_dir: str) -> LocalFacts:
    """Read the run directory. Reads only; creates nothing."""
    facts = LocalFacts()
    results_path = os.path.join(output_dir, RESULTS_FILENAME)
    if not os.path.isdir(output_dir):
        return LocalFacts(readable=False, error="the run directory does not exist")
    if not os.path.exists(results_path):
        return LocalFacts(readable=False, error=f"{RESULTS_FILENAME} is missing")
    try:
        document = _load_json(results_path)
    except (OSError, ValueError):
        return LocalFacts(readable=False, error=f"{RESULTS_FILENAME} cannot be read as JSON")
    if not isinstance(document, dict):
        return LocalFacts(readable=False, error=f"{RESULTS_FILENAME} is not a JSON object")

    status = document.get("review_status")
    facts.review_status = status if isinstance(status, str) else None
    stored = document.get("content_hash")
    if not stored:
        facts.document_hash = "absent"
    else:
        try:
            facts.document_hash = "self_consistent" if verify_content_hash(document, stored) else "inconsistent"
        except Exception:  # noqa: BLE001 - an uncomputable hash is "inconsistent", never "consistent"
            facts.document_hash = "inconsistent"
    facts.override_claim_ids, facts.override_count, facts.override_invalid = _override_claim_ids(document)
    facts.full_pdf_present = os.path.exists(os.path.join(output_dir, FULL_PDF_FILENAME))
    facts.short_pdf_present = os.path.exists(os.path.join(output_dir, SHORT_PDF_FILENAME))
    facts.audit_has_approved = _audit_has_approved(output_dir)
    facts.journal_present = os.path.exists(os.path.join(output_dir, JOURNAL_FILENAME))
    _inspect_manifest(output_dir, document, facts)
    return facts


# ══ classification (pure) ═════════════════════════════════════════════════════════════════════════════════════════


def _local_summary(local: LocalFacts) -> Dict[str, Any]:
    return {
        "review_status": local.review_status,
        "document_hash_against_its_own_stored_hash": local.document_hash,
        "manifest": local.manifest,
        "manifest_problems": list(local.manifest_problems),
        "full_pdf_present": local.full_pdf_present,
        "short_pdf_present": local.short_pdf_present,
        "local_audit_has_approved_entry": local.audit_has_approved,
        "override_records": local.override_count,
        "recovery_journal_present": local.journal_present,
    }


def _server_summary(server: ServerFacts, user_id: Optional[uuid.UUID]) -> Dict[str, Any]:
    by_type: Dict[str, int] = {}
    mine = others = 0
    for claim in server.claims:
        by_type[claim["claim_type"]] = by_type.get(claim["claim_type"], 0) + 1
        if claim["actor_id"] == user_id:
            mine += 1
        else:
            others += 1
    return {
        "state": server.state,
        "retracted": server.is_retracted,
        "approval": server.approver_relation(user_id) or "none",
        "has_content_hash": server.has_content_hash,
        "claims_total": len(server.claims),
        "claims_by_you": mine,
        "claims_by_other_users": others,
        "claims_by_type": dict(sorted(by_type.items())),
    }


_NEXT = {
    CONSISTENT: [],
    INCOMPLETE: ["A sign-off is in progress. " + NO_RECOVERY_NOTICE],
    INVESTIGATE: [
        "Stop and escalate to the platform owner and the laboratory's quality owner.",
        "Do not export, deliver or release this run until it has been looked at.",
        "Do not 'fix' it with approve or withdraw (the linked-run tools refuse by design) and do not edit files by hand.",
    ],
    UNREADABLE: ["Fix the cause above and run status again. " + NO_RECOVERY_NOTICE],
}


def _result(
    group: str,
    code: str,
    summary: str,
    *,
    reasons: Sequence[str] = (),
    hold: bool = False,
    report_id: Optional[str] = None,
    server: Optional[Dict[str, Any]] = None,
    local: Optional[Dict[str, Any]] = None,
) -> StatusResult:
    steps = list(_NEXT[group])
    if hold:
        steps.insert(
            0, "HOLD: a results JSON marked 'reviewed' has no valid manifest. It must not be exported or delivered."
        )
    return StatusResult(group, code, summary, list(reasons), hold, report_id, server, local, steps)


def classify_unlinked(local: LocalFacts) -> StatusResult:
    """A run with no clinical_link.json: the local files are all there is, and the server is not consulted."""
    summary = _local_summary(local)
    base: Dict[str, Any] = {"local": summary}
    reasons: List[str] = []
    if local.manifest_clinical:
        # A manifest's `clinical` block exists only for a sign-off recorded through the clinical record. If it is here but
        # clinical_link.json is not, the link file was removed (or never copied): the run is not safely "unlinked".
        return _result(
            INVESTIGATE,
            "UNLINKED_BUT_MANIFEST_NAMES_A_CLINICAL_RECORD",
            "There is no clinical_link.json, but the sign-off manifest records a clinical report. The link file may have been removed.",
            hold=local.hold,
            **base,
        )
    if local.reviewed:
        if local.document_hash == "inconsistent":
            return _result(
                INVESTIGATE,
                "UNLINKED_LOCAL_HASH_INCONSISTENT",
                "The reviewed results JSON no longer matches its own stored hash.",
                hold=local.hold,
                **base,
            )
        if local.document_hash == "absent":
            return _result(
                INVESTIGATE,
                "UNLINKED_LOCAL_HASH_ABSENT",
                "The reviewed results JSON carries no content hash (a run signed before hashing existed, or one that lost it), so its integrity cannot be checked.",
                hold=local.hold,
                **base,
            )
        if local.manifest != "valid":
            reasons = [f"manifest: {local.manifest}"] + local.manifest_problems
            return _result(
                INVESTIGATE,
                "UNLINKED_REVIEWED_WITHOUT_VALID_MANIFEST",
                "The run says 'reviewed' but has no valid sign-off manifest.",
                reasons=reasons,
                hold=True,
                **base,
            )
        return _result(
            CONSISTENT,
            "UNLINKED_SIGNED_OFF",
            "An unlinked run, signed off on the filesystem; its files agree with its manifest. No clinical record was consulted.",
            **base,
        )
    if local.manifest != "absent":
        return _result(
            INVESTIGATE,
            "UNLINKED_MANIFEST_WITHOUT_REVIEW",
            "A sign-off manifest exists but the results JSON is not marked 'reviewed'.",
            reasons=local.manifest_problems,
            **base,
        )
    return _result(
        CONSISTENT,
        "UNLINKED_DRAFT",
        "An unlinked run that has not been signed off. No clinical record was consulted.",
        **base,
    )


def classify(
    link_ids: Tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    server: Optional[ServerFacts],
    local: LocalFacts,
    user_id: Optional[uuid.UUID],
) -> StatusResult:
    """
    The decision. PURE: its inputs are facts already read, it performs no I/O and it is deterministic. Rules are applied in
    a fixed order; the first that applies decides, so the most serious contradiction is the one reported.
    """
    org_id, interpretation_id, report_id = link_ids
    rid = str(report_id)
    lsum = _local_summary(local)
    if server is None:
        return _result(
            INVESTIGATE,
            "REPORT_NOT_FOUND_IN_ORGANISATION",
            "The clinical service has no such report in your organisation (it does not distinguish 'absent' from 'someone else's').",
            report_id=rid,
            local=lsum,
            hold=local.hold,
        )
    if not isinstance(user_id, uuid.UUID):
        # "Approved by you" and "approved by another operator" are both claims about WHO is asking. With no established
        # identity (none given, or one that is not a UUID such as a plain string) neither may be made, and the type is
        # not coerced: a string that happens to look right is still not the service's own answer.
        return _result(
            UNREADABLE,
            "OPERATOR_IDENTITY_UNAVAILABLE",
            "The authenticated operator's identity could not be established, so nothing can be attributed to 'you' or to 'another operator'.",
            report_id=rid,
            local=lsum,
            hold=local.hold,
        )
    ssum = _server_summary(server, user_id)
    common: Dict[str, Any] = {"report_id": rid, "server": ssum, "local": lsum}
    hold = local.hold

    # 1. Is this the report the run says it is?
    mc = local.manifest_clinical
    if server.interpretation_id != interpretation_id or any(
        mc.get(k) and mc[k] != str(v)
        for k, v in (("org_id", org_id), ("interpretation_id", interpretation_id), ("report_id", report_id))
    ):
        return _result(
            INVESTIGATE,
            "WRONG_TARGET",
            "The link, the manifest and the clinical service disagree about which report or interpretation this run is.",
            hold=hold,
            **common,
        )
    # 2. Retraction and unsupported states are never classified as a normal sign-off.
    if server.is_retracted:
        return _result(
            INVESTIGATE,
            "RETRACTED",
            "The report is retracted on the clinical service. Nothing here should be finalised or exported.",
            hold=hold,
            **common,
        )
    if server.state == "returned":
        return _result(
            INVESTIGATE,
            "UNSUPPORTED_STATE_RETURNED",
            "The report is in state 'returned', which this tool does not handle.",
            hold=hold,
            **common,
        )
    # 3. Local document integrity.
    if local.reviewed and local.document_hash == "inconsistent":
        return _result(
            INVESTIGATE,
            "LOCAL_HASH_INCONSISTENT",
            "The reviewed results JSON no longer matches its own stored hash: it was changed after it was hashed.",
            hold=hold,
            **common,
        )
    if local.reviewed and local.document_hash == "absent":
        return _result(
            INVESTIGATE,
            "LOCAL_HASH_ABSENT",
            "The reviewed results JSON carries no content hash, so even its self-consistency cannot be checked.",
            hold=hold,
            **common,
        )
    # 4. Override records against the claims the service holds (ids only; variant keys are not exposed).
    server_ids = {_canonical_uuid(c["id"]) or str(c["id"]) for c in server.claims}
    my_disagreements = {
        _canonical_uuid(c["id"]) or str(c["id"])
        for c in server.claims
        if c["claim_type"] == "disagree" and c["actor_id"] == user_id
    }
    unknown_to_server = local.override_claim_ids - server_ids
    unreferenced_mine = my_disagreements - local.override_claim_ids
    status_without_records = local.review_status == "overridden" and local.override_count == 0
    records_without_status = local.override_count > 0 and local.review_status not in ("overridden", "reviewed")
    if (
        unknown_to_server
        or unreferenced_mine
        or local.override_invalid
        or status_without_records
        or records_without_status
    ):
        reasons = []
        if local.override_invalid:
            reasons.append(
                f"{local.override_invalid} override record(s) in the results JSON carry no valid claim id, so they cannot be matched to the service"
            )
        if status_without_records:
            reasons.append("the results JSON says 'overridden' but holds no override record")
        if records_without_status:
            reasons.append(
                f"the results JSON holds override records but its review status is {local.review_status or 'absent'}"
            )
        if unknown_to_server:
            reasons.append(
                f"{len(unknown_to_server)} override record(s) in the results JSON name a claim the service does not have"
            )
        if unreferenced_mine:
            reasons.append(
                f"{len(unreferenced_mine)} disagreement claim(s) by you on the service have no override record in the results JSON"
            )
        return _result(
            INVESTIGATE,
            "OVERRIDE_CLAIM_MISMATCH",
            "The results JSON's override records and the service's disagreement claims do not correspond.",
            reasons=reasons,
            hold=hold,
            **common,
        )

    my_sole = sum(1 for c in server.claims if c["claim_type"] == "sole_signatory" and c["actor_id"] == user_id)

    # 5. By server state.
    if server.state in ("approved", "released"):
        who = server.approver_relation(user_id)
        suffix = "BY_YOU" if who == "you" else "BY_ANOTHER_OPERATOR"
        who_text = "you" if who == "you" else "another operator"
        released = server.state == "released"
        if local.reviewed and local.manifest == "valid":
            if not all(mc.get(k) for k in ("org_id", "interpretation_id", "report_id", "approver_user_id")):
                return _result(
                    INVESTIGATE,
                    "MANIFEST_NOT_LINKED_TO_THIS_REPORT",
                    "The manifest does not name the organisation, interpretation, report and approver, so it was not recorded through this clinical record.",
                    hold=hold,
                    **common,
                )
            if mc["approver_user_id"] != (_canonical_uuid(server.approver_id) or str(server.approver_id)):
                return _result(
                    INVESTIGATE,
                    "MANIFEST_APPROVER_DIFFERS",
                    "The manifest names a different approver than the clinical service.",
                    hold=hold,
                    **common,
                )
            if not local.audit_has_approved:
                return _result(
                    INCOMPLETE,
                    "SIGNED_OFF_AUDIT_ENTRY_MISSING",
                    f"Approved by {who_text}; files and manifest agree; the local audit log lacks its 'approved' line.",
                    **common,
                )
            code = ("RELEASED_" if released else "SIGNED_OFF_") + ("CONSISTENT_" if released else "") + suffix
            return _result(
                CONSISTENT,
                code,
                f"{'Released' if released else 'Approved'} by {who_text}; the local files and manifest are consistent with the clinical service.",
                **common,
            )
        if local.reviewed:
            reasons = [f"manifest: {local.manifest}"] + local.manifest_problems
            return _result(
                INVESTIGATE,
                f"APPROVED_FILES_INCOMPLETE_{suffix}",
                f"Approved by {who_text} on the service, but the local sign-off is incomplete or inconsistent (HOLD).",
                reasons=reasons,
                hold=True,
                **common,
            )
        reasons = [f"local review status: {local.review_status or 'absent'}"]
        return _result(
            INVESTIGATE,
            f"APPROVED_NO_LOCAL_SIGNOFF_{suffix}",
            f"Approved by {who_text} on the service, but the local files were never signed off.",
            reasons=reasons,
            hold=hold,
            **common,
        )

    # draft or under_review: nothing is approved, so a reviewed local file contradicts the service.
    if local.reviewed:
        reasons = [f"server state: {server.state}", f"manifest: {local.manifest}"] + local.manifest_problems
        return _result(
            INVESTIGATE,
            "LOCAL_REVIEWED_SERVER_NOT_APPROVED",
            "The results JSON says 'reviewed' but the clinical service has not approved the report.",
            reasons=reasons,
            hold=hold,
            **common,
        )
    if local.manifest != "absent":
        return _result(
            INVESTIGATE,
            "MANIFEST_WITHOUT_REVIEW",
            "A sign-off manifest exists although the results JSON is not marked 'reviewed' and the service has not approved.",
            reasons=local.manifest_problems,
            **common,
        )
    if local.audit_has_approved:
        return _result(
            INVESTIGATE,
            "LOCAL_AUDIT_RECORDS_AN_APPROVAL_THE_SERVER_LACKS",
            "The local audit log records an approval, but the clinical service has not approved this report.",
            hold=hold,
            **common,
        )
    if my_sole > 1:
        return _result(
            INVESTIGATE,
            "DUPLICATE_SOLE_SIGNATORY_CLAIMS",
            f"You have {my_sole} 'sole_signatory' claims on this report. Claims are append-only; a person must decide what they mean.",
            **common,
        )
    if server.state == "under_review":
        return _result(
            INCOMPLETE,
            "UNDER_REVIEW",
            "Submitted for review; not yet approved. The local files are unsigned. This is a stage of a sign-off in progress.",
            **common,
        )
    if not server.claims:
        if local.review_status not in (None, "draft") or local.override_count:
            return _result(
                INVESTIGATE,
                "LOCAL_STATUS_NOT_COMPATIBLE_WITH_NOT_STARTED",
                f"The service holds a draft with no claims, but the results JSON's review status is {local.review_status}.",
                hold=hold,
                **common,
            )
        return _result(
            CONSISTENT,
            "NOT_STARTED",
            "A draft with no claims and unsigned local files: no sign-off is in progress.",
            **common,
        )
    note = "a 'sole_signatory' claim by you exists" if my_sole else "claims exist"
    return _result(
        INCOMPLETE,
        "DRAFT_WITH_CLAIMS",
        f"A draft with claims ({note}), not submitted and not approved. Which attempt a claim belongs to cannot be established without a recovery journal.",
        **common,
    )


# ══ orchestration (authenticated reads) ═══════════════════════════════════════════════════════════════════════════


def _unreadable(
    code: str, summary: str, local: Optional[LocalFacts] = None, report_id: Optional[str] = None
) -> StatusResult:
    return _result(
        UNREADABLE,
        code,
        summary,
        report_id=report_id,
        local=_local_summary(local) if local and local.readable else None,
    )


def _server_facts(raw: Dict[str, Any]) -> ServerFacts:
    return ServerFacts(
        state=raw["state"],
        interpretation_id=raw["interpretation_id"],
        is_retracted=raw["is_retracted"],
        approver_id=raw["approver_id"],
        has_content_hash=raw["has_content_hash"],
        claims=list(raw["claims"]),
    )


def run_status(
    output_dir: str,
    clinical_credentials: Optional[ClinicalCredentials] = None,
    clinical_url: Optional[str] = None,
    data_access: Any = None,
) -> StatusResult:
    """
    Classify one run directory. READ-ONLY (see the module docstring for the one caveat about the adapter's own
    login/logout). `data_access` is the injection point the other tools use; without it the clinical service is reached
    over HTTPS at `clinical_url` / $GEPER_CLINICAL_URL, never through a database connection.
    """
    local = inspect_local(output_dir)
    if not local.readable:
        return _unreadable("LOCAL_UNREADABLE", f"The run directory cannot be read: {local.error}.")

    try:
        link = read_clinical_link(output_dir)
    except SignoffError:
        return _unreadable(
            "LINK_UNREADABLE",
            "clinical_link.json exists but cannot be read; the run is not treated as unlinked.",
            local,
        )
    if link is None:
        return classify_unlinked(local)
    try:
        link_ids = _link_ids(link)
    except SignoffError:
        return _unreadable(
            "LINK_INCOMPLETE", "clinical_link.json does not name the organisation, interpretation and report.", local
        )
    report_id = str(link_ids[2])
    if clinical_credentials is None:
        return _unreadable(
            "CREDENTIALS_NOT_SUPPLIED",
            "This run is linked to a clinical record, so its state can only be read as an authenticated user. Supply "
            "--clinical-email (and the password in the environment variable it names).",
            local,
            report_id,
        )

    from clinical import data_access as da  # the exception types the adapter raises

    close = None
    try:
        dao, session, close = _open_clinical(link, clinical_credentials, data_access, clinical_url)
        reader = getattr(dao, "get_signoff_state", None)
        if reader is None:
            return _unreadable(
                "SERVICE_UNREADABLE", "The clinical access in use cannot read the sign-off state.", local, report_id
            )
        first = reader(session, link_ids[2])
        second = reader(session, link_ids[2])
        user_id = getattr(session, "user_id", None)
        session_org = getattr(session, "org_id", None)
    except da.SessionExpired:  # a subclass of AuthenticationError, so it must be caught first
        return _unreadable("SESSION_EXPIRED", "The clinical session expired during the read.", local, report_id)
    except da.AuthenticationError:
        return _unreadable("AUTHENTICATION_FAILED", "The clinical service refused the login.", local, report_id)
    except da.AuthorizationError:
        return _unreadable(
            "PERMISSION_DENIED",
            "The clinical service refused this user permission to read the sign-off state.",
            local,
            report_id,
        )
    except SignoffError:
        return _unreadable(
            "SERVICE_UNREADABLE",
            "The clinical service could not be reached or gave an unusable (malformed or inconsistent) answer.",
            local,
            report_id,
        )
    except Exception as exc:  # noqa: BLE001 - never a guess; only the TYPE is reported (messages can carry request detail)
        return _unreadable(
            "UNEXPECTED_ERROR", f"The read failed unexpectedly ({type(exc).__name__}).", local, report_id
        )
    finally:
        if close is not None:
            try:
                close()
            except Exception:  # noqa: BLE001 - failing to log out must not mask the result
                pass

    # Defence in depth: the adapter already refuses a login that lands in another organisation, but an injected access
    # object need not. A session whose organisation is not the link's is a contradiction, not a read to classify from.
    if session_org != link_ids[0]:
        return _result(
            INVESTIGATE,
            "ORGANISATION_MISMATCH",
            "The authenticated session belongs to a different organisation than the run's clinical link.",
            report_id=report_id,
            local=_local_summary(local),
            hold=local.hold,
        )
    if first != second:
        return _unreadable(
            "SERVER_STATE_CHANGED_DURING_READ",
            "The sign-off state changed between two reads, so no classification is offered. Run status again.",
            local,
            report_id,
        )
    return classify(link_ids, None if first is None else _server_facts(first), local, user_id)


# ══ presentation ══════════════════════════════════════════════════════════════════════════════════════════════════


def format_text(result: StatusResult) -> str:
    lines = [f"{result.group}: {result.code}" + ("   [HOLD]" if result.hold else ""), result.summary]
    if result.report_id:
        lines.append(f"Report: {result.report_id}")
    for reason in result.reasons:
        lines.append(f"  - {reason}")
    if result.server:
        s = result.server
        lines.append(
            f"Server: state={s['state']} retracted={s['retracted']} approval={s['approval']} "
            f"claims={s['claims_total']} (yours {s['claims_by_you']}, others {s['claims_by_other_users']}) "
            f"by type {s['claims_by_type']}"
        )
    if result.local:
        loc = result.local
        lines.append(
            f"Local: review_status={loc['review_status']} manifest={loc['manifest']} document_hash={loc['document_hash_against_its_own_stored_hash']} "
            f"pdfs(full={loc['full_pdf_present']}, short={loc['short_pdf_present']}) audit_approved_entry={loc['local_audit_has_approved_entry']} "
            f"journal_present={loc['recovery_journal_present']}"
        )
        if loc["manifest_problems"]:
            lines.append(f"  manifest problems: {', '.join(loc['manifest_problems'])}")
    for step in result.next_steps:
        lines.append(f"Note: {step}")
    lines.append(BINDING_NOTICE)
    lines.append(f"Exit code {result.exit_code}.")
    return "\n".join(lines)
