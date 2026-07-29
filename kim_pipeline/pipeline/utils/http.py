"""
pipeline/utils/http.py
──────────────────────
Shared HTTP GET utility with exponential backoff retry logic.

All API lookup modules use _api_get() instead of requests.get() directly.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests
from requests.exceptions import ConnectionError as ReqConnectionError, Timeout

_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
_NO_RETRY_STATUS_CODES = {400, 401, 403, 404}

_DEFAULT_LOGGER = logging.getLogger("geper.utils.http")

# NOTE: a connection-pooled requests.Session() was tried here to avoid a
# fresh TCP+TLS handshake per API call, but it broke the existing test
# suite's mocking contract (tests patch module-level requests.get/post via
# @patch(...)). Reverted — see performance report. Left as a documented,
# not-yet-applied recommendation rather than risking a silent behavior
# change that the tests can no longer verify.


def _api_get(
    url: str,
    params: dict,
    timeout: int = 15,
    max_retries: int = 3,
    logger: Optional[logging.Logger] = None,
) -> Optional[requests.Response]:
    """Perform an HTTP GET with exponential backoff retry.

    Retries on: ConnectionError, Timeout, HTTP 429/500/502/503/504.
    Does NOT retry on: HTTP 400/401/403/404.
    Backoff: 1s → 2s → 4s (exponential, no jitter).

    Args:
        url:         Full URL to request.
        params:      Query parameters dict.
        timeout:     Per-attempt timeout in seconds.
        max_retries: Maximum number of attempts (first attempt + retries).
        logger:      Optional logger; defaults to geper.utils.http.

    Returns:
        requests.Response on success, None on final failure.  Never raises.
    """
    log = logger or _DEFAULT_LOGGER
    delay = 1.0

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)

            # Non-retryable client errors
            if resp.status_code in _NO_RETRY_STATUS_CODES:
                return resp

            # Success
            if resp.status_code < 400:
                return resp

            # Retryable server/rate-limit error
            if resp.status_code in _RETRY_STATUS_CODES:
                if attempt < max_retries:
                    log.warning(
                        "[http] HTTP %d on attempt %d/%d for %s — retrying in %.0fs",
                        resp.status_code, attempt, max_retries, url, delay,
                    )
                    time.sleep(delay)
                    delay *= 2
                    continue
                else:
                    log.error(
                        "[http] HTTP %d on final attempt %d/%d for %s — giving up",
                        resp.status_code, attempt, max_retries, url,
                    )
                    return None

            # Other unexpected status codes — return as-is
            return resp

        except (ReqConnectionError, Timeout) as exc:
            if attempt < max_retries:
                log.warning(
                    "[http] %s on attempt %d/%d for %s — retrying in %.0fs",
                    type(exc).__name__, attempt, max_retries, url, delay,
                )
                time.sleep(delay)
                delay *= 2
            else:
                log.error(
                    "[http] %s on final attempt %d/%d for %s — giving up: %s",
                    type(exc).__name__, attempt, max_retries, url, exc,
                )
                return None

        except Exception as exc:
            log.error("[http] Unexpected error on attempt %d/%d for %s: %s", attempt, max_retries, url, exc)
            return None

    return None
