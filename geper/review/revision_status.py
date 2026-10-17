"""
review/revision_status.py
─────────────────────────

THE AUTHORITATIVE CLINICAL STATUS OF A LINKED REPORT -- retracted, amended,
superseded, re-analysed -- fetched from the clinical platform and VALIDATED,
so that the things GEPER writes (a current-status copy of a report, a LIMS
export) can state it.

WHY THIS EXISTS. A report's signed artefacts (`geper_results.json`, the two PDFs,
the manifest) are written once, at sign-off, and are never touched again: the
manifest holds their SHA-256s, and "the approved report stays byte-identical"
is the whole point of how retraction was modelled (migration 0003, ruling E-series).
So a retraction, an amendment or a re-analysis made AFTER sign-off cannot appear
in them. The only authoritative source of those facts is the clinical database,
reached through `DataAccess.get_report_revision`. This module is the one place
that asks it, checks the answer, and refuses to guess.

THE RULES IT ENFORCES, each one a refusal rather than a default:

  * The session must belong to the link's organisation, the report must exist
    in that organisation, and the report must belong to the link's
    interpretation. A report id alone does not establish that this run is that
    report's run.
  * `get_report_revision` answers `{}` when it LOOKED and found nothing to
    say, and the renderer's own `None` means "never looked". An answer that is
    not a mapping is therefore an error, never "no revision".
  * Every failure -- unknown report, a record that cannot be read into a true
    sentence (BrokenRevisionRecordError), an expired session, a refused role, a
    malformed block -- is a `RevisionStatusError`. None of them is turned into
    "not retracted": a failed lookup must never produce a document that looks
    like a clean one.
  * `get_report`'s `is_retracted` flag and the revision block's own
    `retracted` entry are two reads of the same fact and must agree.

An empty `{}` means there is nothing to render. It does not, on its own, prove
the report was never retracted; it proves that a successful, authenticated,
organisation-scoped lookup at `checked_at` returned nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from report.clinical_report_builder import normalize_report_revision
from utils.exceptions import SignoffError


class RevisionStatusError(SignoffError):
    """The clinical status of a linked report could not be established and validated."""


@dataclass(frozen=True)
class ReportRevisionStatus:
    """A validated answer to "what is this report's clinical status right now?"."""

    org_id: str
    interpretation_id: str
    report_id: str
    report_state: str
    checked_at: str
    # The NORMALISED revision block (`normalize_report_revision` output): never
    # None -- a successful lookup that found nothing is an all-false block.
    revision: Dict[str, Any]

    @property
    def is_retracted(self) -> bool:
        return bool(self.revision.get("is_retracted"))

    @property
    def retraction(self) -> Optional[Dict[str, Any]]:
        block = self.revision.get("retracted")
        return dict(block) if block else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "org_id": self.org_id,
            "interpretation_id": self.interpretation_id,
            "report_id": self.report_id,
            "report_state": self.report_state,
            "checked_at": self.checked_at,
            "revision": self.revision,
        }


def fetch_report_revision_status(
    data_access: Any,
    session: Any,
    *,
    org_id: uuid.UUID,
    interpretation_id: uuid.UUID,
    report_id: uuid.UUID,
) -> ReportRevisionStatus:
    """
    Ask the clinical platform for `report_id`'s revision status as `session`, and
    validate it. `data_access` and `session` are the caller's already-authenticated
    pair (the sign-off tooling's `_clinical_login`); this function never logs in.

    Raises `RevisionStatusError` on ANY failure; returns only a validated status.
    """
    try:
        from clinical.data_access import (
            AuthorizationError,
            BrokenRevisionRecordError,
            NotFoundError,
            SessionExpired,
        )
    except ImportError as exc:  # pragma: no cover -- a broken install, not a code path
        raise RevisionStatusError(f"The clinical layer is unavailable, so the report's status cannot be read: {exc}")

    if getattr(session, "org_id", None) != org_id:
        raise RevisionStatusError(
            f"The authenticated session belongs to organisation {getattr(session, 'org_id', None)}, "
            f"not to the link's organisation {org_id}. Refusing to read another organisation's report."
        )

    try:
        report = data_access.get_report(session, report_id)
    except (AuthorizationError, SessionExpired) as exc:
        raise RevisionStatusError(f"The clinical platform refused to show report {report_id}: {type(exc).__name__}.")
    except Exception as exc:
        raise RevisionStatusError(f"Reading report {report_id} failed ({type(exc).__name__}: {exc}).") from exc
    if report is None:
        raise RevisionStatusError(
            f"Report {report_id} does not exist in organisation {org_id}. The run's link names a record that "
            "cannot be found there, so its status cannot be established."
        )
    if str(report.get("interpretation_id")) != str(interpretation_id):
        raise RevisionStatusError(
            f"Report {report_id} belongs to interpretation {report.get('interpretation_id')}, not to "
            f"interpretation {interpretation_id} named by this run's link. The link and the clinical record "
            "disagree about which report this run is; refusing to attach one report's status to another."
        )

    try:
        raw = data_access.get_report_revision(session, report_id)
    except NotFoundError as exc:
        raise RevisionStatusError(f"Report {report_id} was not found when its revision was read: {exc}") from exc
    except BrokenRevisionRecordError as exc:
        raise RevisionStatusError(
            f"The revision record of report {report_id} cannot be read into a true statement ({exc}). "
            "Refusing to produce an unflagged representation of a report whose status is unreadable."
        ) from exc
    except (AuthorizationError, SessionExpired) as exc:
        raise RevisionStatusError(
            f"The clinical platform refused to show the revision of report {report_id}: {type(exc).__name__}."
        )
    except Exception as exc:
        raise RevisionStatusError(
            f"Reading the revision of report {report_id} failed ({type(exc).__name__}: {exc})."
        ) from exc

    if not isinstance(raw, dict):
        raise RevisionStatusError(
            f"The revision lookup for report {report_id} returned {type(raw).__name__}, not a mapping. "
            "Only a mapping (possibly empty) is a successful answer."
        )
    try:
        normalised = normalize_report_revision(raw)
    except ValueError as exc:
        raise RevisionStatusError(
            f"The revision of report {report_id} is incomplete or inconsistent ({exc}). "
            "Refusing to render a report from facts that could not be validated."
        ) from exc
    if normalised is None:  # pragma: no cover -- normalize returns None only for None input
        raise RevisionStatusError(f"The revision of report {report_id} normalised to nothing.")

    flagged = report.get("is_retracted")
    if flagged is not None and bool(flagged) != bool(normalised["is_retracted"]):
        raise RevisionStatusError(
            f"Report {report_id}: the report record says is_retracted={bool(flagged)} but its revision says "
            f"is_retracted={bool(normalised['is_retracted'])}. The two reads of one fact disagree; refusing to "
            "choose between them."
        )

    return ReportRevisionStatus(
        org_id=str(org_id),
        interpretation_id=str(interpretation_id),
        report_id=str(report_id),
        report_state=str(report.get("state")),
        checked_at=datetime.now(timezone.utc).isoformat(),
        revision=normalised,
    )
