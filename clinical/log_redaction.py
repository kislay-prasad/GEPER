"""
Access-log redaction for the clinical service.

uvicorn's access log writes the request line, including the QUERY STRING, for
every request (`/exceptions/worklist?owner=<an e-mail address>`). Query values
can carry personal data, and a log is usually retained, shipped and read by
people who are not entitled to patient information. This module removes every
query-parameter value from the access log before it is formatted.

POLICY
  * Every query VALUE is replaced with `<redacted>`, whatever the parameter is
    called. There is no list of "sensitive" names to keep up to date.
  * A parameter NAME is kept only if it is a plain token (letters, digits,
    `_ . - [ ]`, at most 64 characters). Anything else -- percent-encoding, an
    "@", a bare `?someone@example.org` with no `=` -- is not a name, so it is
    replaced as well.
  * Kept: client address, method, path, HTTP version and status. The path is
    NOT redacted: record UUIDs in it (`/orders/<uuid>`) stay in the log, because
    operators need them to correlate with the audit log. They are identifiers of
    patient records and are therefore still sensitive; treat the log as
    sensitive. See docs/CLINICAL_DEPLOYMENT.md.
  * FAIL CLOSED: a record whose shape is not the one uvicorn documents, or whose
    path contains control characters, is logged as a fixed placeholder and never
    as the original text.
  * Request bodies, headers, cookies and credentials are not in uvicorn's access
    log and this module does not add them.

Wired in through clinical/log_config.json (uvicorn `--log-config`), on the
access HANDLER so that nothing the logger emits bypasses it.
"""

from __future__ import annotations

import logging
import re

REDACTED = "<redacted>"
UNPARSEABLE = "<request line redacted: unrecognised log record>"

_PLAIN_NAME = re.compile(r"[A-Za-z0-9_.\[\]-]{1,64}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def redact_query(query: str) -> str:
    """`a=1&b=2&a=3` -> `a=<redacted>&b=<redacted>&a=<redacted>`."""
    parts = []
    for segment in query.split("&"):
        if segment == "":
            continue
        name, sep, _value = segment.partition("=")
        if not sep:
            # no "=": the whole segment is a value of unknown meaning
            parts.append(REDACTED)
        elif _PLAIN_NAME.fullmatch(name):
            parts.append(f"{name}={REDACTED}")
        else:
            parts.append(f"{REDACTED}={REDACTED}")
    return "&".join(parts)


def redact_target(target: object) -> str | None:
    """The request target `/path?query` with the query redacted, or None if it
    is not something that can be trusted to be logged."""
    if not isinstance(target, str) or _CONTROL.search(target):
        return None
    path, sep, query = target.partition("?")
    if not sep:
        return path
    redacted = redact_query(query)
    return f"{path}?{redacted}" if redacted else path


class AccessLogRedactionFilter(logging.Filter):
    """Rewrites a `uvicorn.access` record in place; never drops one."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn: (client_addr, method, full_path, http_version, status_code)
        if isinstance(args, tuple) and len(args) == 5:
            target = redact_target(args[2])
            if target is not None:
                record.args = (args[0], args[1], target, args[3], args[4])
                return True
        # keep the five-argument shape uvicorn's formatter unpacks; say nothing
        # taken from the record
        record.args = ("-", "-", UNPARSEABLE, "-", 0)
        return True
