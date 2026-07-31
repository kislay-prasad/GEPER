"""
Small, shared helpers for NCBI E-utilities HTTP handling, used by every
client that talks to eutils.ncbi.nlm.nih.gov (`database/clinvar_client.py`,
`database/dbsnp_client.py`, `pipeline/ps1_pm5/lookup.py`). Each of those
clients keeps its own `_request_json`/retry loop (an intentionally mirrored
shape, not organic duplication -- see `pipeline/ps1_pm5/lookup.py`'s module
docstring), but the two genuinely reusable pieces of logic -- interpreting
NCBI's `Retry-After` header, and recovering a response body that is valid
data but not strictly valid JSON -- live here once so a fix to either
applies identically everywhere instead of drifting between three copies.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

from utils.logger import get_logger

logger = get_logger(__name__)


def parse_retry_after(header_value: Optional[str]) -> Optional[float]:
    """
    Parse an HTTP `Retry-After` header value into a number of seconds
    to wait, or None if the header is absent or unparseable.

    Per RFC 9110 section 10.2.3, `Retry-After` is either a plain
    integer (delay-seconds -- what NCBI's eutils rate limiter actually
    sends, e.g. `Retry-After: 2`) or an HTTP-date. Both are handled;
    an unparseable value returns None rather than raising, so a caller
    can fall back to its own default backoff without this helper ever
    being a new source of failure.
    """
    if not header_value:
        return None
    header_value = header_value.strip()
    try:
        seconds = float(header_value)
        return seconds if seconds >= 0 else None
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(header_value)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    now = datetime.now(timezone.utc) if target.tzinfo else datetime.now()
    delta = (target - now).total_seconds()
    return delta if delta >= 0 else 0.0


def lenient_json_loads(text: str, *, source: str = "") -> Any:
    """
    Parse `text` as JSON, tolerating a stray raw control character
    (e.g. a literal tab) inside a string value -- something NCBI's
    eutils responses have been observed to contain (2026-07-31: a
    ClinVar esummary/esearch response failed strict parsing with
    "Invalid control character '\\t'..."). A raw control character
    inside a JSON string is invalid per the JSON spec's strict reading
    but is a well-known, common real-world malformation -- Python's
    `json` module supports parsing it anyway via `strict=False`. This
    is a data-layer quirk to route around, not a network failure to
    retry into oblivion: the same distinction already made for
    InterPro's HTTP 204 "no domains" response (see
    `pipeline/interpro/provider.py`) -- a response that is technically
    non-conformant but semantically complete and answerable should be
    used, not discarded.

    Raises `json.JSONDecodeError` (unchanged) when the body isn't
    recoverable even leniently -- callers should let that propagate to
    their existing retry logic, since that case genuinely might be a
    transient/malformed response worth retrying.

    `source` is an optional label (typically the request URL) included
    in the warning logged when the lenient fallback is what actually
    made parsing succeed, so this doesn't silently mask malformed
    upstream data forever.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        if "control character" not in str(exc).lower():
            raise
        recovered = json.loads(text, strict=False)
        logger.warning(
            f"Response{f' from {source}' if source else ''} contained a raw control character inside a "
            f"JSON string (strict parse failed: {exc}); recovered by parsing with strict=False rather than "
            "treating this as a network failure."
        )
        return recovered


# IANA-reserved domains (RFC 2606), guaranteed to never resolve to a
# real, deliverable inbox -- GEPER's own documented default
# (`geper.pipeline@example.com`) is one of these on purpose, precisely
# so it's easy to detect and flag here.
_RESERVED_EXAMPLE_DOMAINS = ("example.com", "example.org", "example.net")


def warn_if_placeholder_contact(email: str, *, caller: str) -> None:
    """
    Log a one-time (per client instantiation, not per request) warning
    when `email` is empty or is one of the RFC 2606 reserved "example"
    domains -- i.e. still a placeholder, not a real contact address.

    NCBI's E-utilities usage policy requires a genuine, monitorable
    contact email on every request (`docs.ncbi.nlm.nih.gov` -- "Include
    your email address... NCBI's Administrators may attempt to contact
    a user before blocking access"); GEPER's own default,
    `geper.pipeline@example.com`, satisfies the *parameter* being
    present but not the policy's actual intent, since example.com can
    never receive that contact. This never raises or blocks a
    request -- the pipeline keeps working with the placeholder in a
    dev/sandbox context exactly as it always has -- it only makes the
    gap visible so an operator sets `GEPER_NCBI_EMAIL` to a real
    address before a production run, rather than silently sending a
    dead address to NCBI on every call indefinitely.
    """
    if not email:
        logger.warning(f"{caller}: no NCBI contact email configured (GEPER_NCBI_EMAIL is empty).")
        return
    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    if domain in _RESERVED_EXAMPLE_DOMAINS:
        logger.warning(
            f"{caller}: NCBI contact email is still the placeholder default ('{email}') -- set "
            "GEPER_NCBI_EMAIL to a real, monitored address before a production run. NCBI's usage policy "
            "requires a genuine contact and may block requests sent with an unreachable one."
        )
