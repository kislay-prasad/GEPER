"""
tests/test_http_utils.py
────────────────────────
Unit tests for pipeline/utils/http.py _api_get() utility.

Covers:
- 429 triggers exactly 3 retries then returns None
- 404 does NOT retry (returns on first attempt)
- Successful response on 2nd attempt returns correctly
- ConnectionError triggers retries
"""

from __future__ import annotations

import logging
import unittest
from unittest.mock import MagicMock, patch, call

import requests

# Ensure project root is in path
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.utils.http import _api_get


def _make_response(status_code: int, json_data=None) -> MagicMock:
    """Create a mock requests.Response with given status code."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    return resp


class TestApiGetRetryOn429(unittest.TestCase):
    """429 response should trigger exactly 3 retries then return None."""

    @patch("pipeline.utils.http.time.sleep")
    @patch("pipeline.utils.http.requests.get")
    def test_429_triggers_3_retries_returns_none(self, mock_get, mock_sleep):
        """HTTP 429 on every attempt → 3 total attempts → None."""
        mock_get.return_value = _make_response(429)

        result = _api_get("https://example.com", params={}, max_retries=3)

        self.assertIsNone(result)
        self.assertEqual(mock_get.call_count, 3, "Should attempt exactly 3 times")
        # Backoff: sleep called after attempt 1 and 2 (not after final)
        self.assertEqual(mock_sleep.call_count, 2)
        self.assertEqual(mock_sleep.call_args_list[0], call(1.0))
        self.assertEqual(mock_sleep.call_args_list[1], call(2.0))

    @patch("pipeline.utils.http.time.sleep")
    @patch("pipeline.utils.http.requests.get")
    def test_500_triggers_retries(self, mock_get, mock_sleep):
        """HTTP 500 on every attempt → 3 total attempts → None."""
        mock_get.return_value = _make_response(500)

        result = _api_get("https://example.com", params={}, max_retries=3)

        self.assertIsNone(result)
        self.assertEqual(mock_get.call_count, 3)

    @patch("pipeline.utils.http.time.sleep")
    @patch("pipeline.utils.http.requests.get")
    def test_503_triggers_retries(self, mock_get, mock_sleep):
        """HTTP 503 triggers retries."""
        mock_get.return_value = _make_response(503)
        result = _api_get("https://example.com", params={}, max_retries=3)
        self.assertIsNone(result)
        self.assertEqual(mock_get.call_count, 3)


class TestApiGetNoRetryOn404(unittest.TestCase):
    """404 response should NOT retry — return on first attempt."""

    @patch("pipeline.utils.http.time.sleep")
    @patch("pipeline.utils.http.requests.get")
    def test_404_no_retry(self, mock_get, mock_sleep):
        """HTTP 404 → single attempt, sleep never called, response returned."""
        mock_404 = _make_response(404)
        mock_get.return_value = mock_404

        result = _api_get("https://example.com", params={}, max_retries=3)

        self.assertEqual(mock_get.call_count, 1, "Should only make 1 attempt on 404")
        mock_sleep.assert_not_called()
        self.assertIs(result, mock_404)

    @patch("pipeline.utils.http.time.sleep")
    @patch("pipeline.utils.http.requests.get")
    def test_400_no_retry(self, mock_get, mock_sleep):
        mock_400 = _make_response(400)
        mock_get.return_value = mock_400
        result = _api_get("https://example.com", params={}, max_retries=3)
        self.assertEqual(mock_get.call_count, 1)
        mock_sleep.assert_not_called()
        self.assertIs(result, mock_400)

    @patch("pipeline.utils.http.time.sleep")
    @patch("pipeline.utils.http.requests.get")
    def test_401_no_retry(self, mock_get, mock_sleep):
        mock_401 = _make_response(401)
        mock_get.return_value = mock_401
        result = _api_get("https://example.com", params={}, max_retries=3)
        self.assertEqual(mock_get.call_count, 1)
        mock_sleep.assert_not_called()
        self.assertIs(result, mock_401)

    @patch("pipeline.utils.http.time.sleep")
    @patch("pipeline.utils.http.requests.get")
    def test_403_no_retry(self, mock_get, mock_sleep):
        mock_403 = _make_response(403)
        mock_get.return_value = mock_403
        result = _api_get("https://example.com", params={}, max_retries=3)
        self.assertEqual(mock_get.call_count, 1)
        mock_sleep.assert_not_called()
        self.assertIs(result, mock_403)


class TestApiGetSuccessOnSecondAttempt(unittest.TestCase):
    """Successful response on 2nd attempt should be returned correctly."""

    @patch("pipeline.utils.http.time.sleep")
    @patch("pipeline.utils.http.requests.get")
    def test_success_on_second_attempt(self, mock_get, mock_sleep):
        """First attempt → 429, second attempt → 200 → return response."""
        ok_resp = _make_response(200, {"data": "value"})
        mock_get.side_effect = [_make_response(429), ok_resp]

        result = _api_get("https://example.com", params={"key": "val"}, max_retries=3)

        self.assertIs(result, ok_resp)
        self.assertEqual(mock_get.call_count, 2)
        mock_sleep.assert_called_once_with(1.0)

    @patch("pipeline.utils.http.time.sleep")
    @patch("pipeline.utils.http.requests.get")
    def test_success_on_third_attempt(self, mock_get, mock_sleep):
        """Two failures then success."""
        ok_resp = _make_response(200)
        mock_get.side_effect = [_make_response(502), _make_response(503), ok_resp]

        result = _api_get("https://example.com", params={}, max_retries=3)

        self.assertIs(result, ok_resp)
        self.assertEqual(mock_get.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("pipeline.utils.http.time.sleep")
    @patch("pipeline.utils.http.requests.get")
    def test_immediate_success_no_sleep(self, mock_get, mock_sleep):
        """First attempt succeeds — no sleep, single call."""
        ok_resp = _make_response(200)
        mock_get.return_value = ok_resp

        result = _api_get("https://example.com", params={})
        self.assertIs(result, ok_resp)
        mock_sleep.assert_not_called()
        self.assertEqual(mock_get.call_count, 1)


class TestApiGetConnectionError(unittest.TestCase):
    """ConnectionError should trigger retries with backoff."""

    @patch("pipeline.utils.http.time.sleep")
    @patch("pipeline.utils.http.requests.get")
    def test_connection_error_triggers_retries(self, mock_get, mock_sleep):
        """ConnectionError on every attempt → 3 total attempts → None."""
        mock_get.side_effect = requests.exceptions.ConnectionError("Network unreachable")

        result = _api_get("https://example.com", params={}, max_retries=3)

        self.assertIsNone(result)
        self.assertEqual(mock_get.call_count, 3)
        # Sleep called after attempt 1 and 2
        self.assertEqual(mock_sleep.call_count, 2)
        self.assertEqual(mock_sleep.call_args_list[0], call(1.0))
        self.assertEqual(mock_sleep.call_args_list[1], call(2.0))

    @patch("pipeline.utils.http.time.sleep")
    @patch("pipeline.utils.http.requests.get")
    def test_timeout_triggers_retries(self, mock_get, mock_sleep):
        """Timeout on every attempt → 3 total attempts → None."""
        mock_get.side_effect = requests.exceptions.Timeout("Timed out")

        result = _api_get("https://example.com", params={}, max_retries=3)

        self.assertIsNone(result)
        self.assertEqual(mock_get.call_count, 3)

    @patch("pipeline.utils.http.time.sleep")
    @patch("pipeline.utils.http.requests.get")
    def test_connection_error_then_success(self, mock_get, mock_sleep):
        """ConnectionError then success returns the successful response."""
        ok_resp = _make_response(200)
        mock_get.side_effect = [
            requests.exceptions.ConnectionError("fail"),
            ok_resp,
        ]

        result = _api_get("https://example.com", params={}, max_retries=3)

        self.assertIs(result, ok_resp)
        self.assertEqual(mock_get.call_count, 2)
        mock_sleep.assert_called_once_with(1.0)


class TestApiGetBackoffSequence(unittest.TestCase):
    """Verify exponential backoff: 1s → 2s → 4s."""

    @patch("pipeline.utils.http.time.sleep")
    @patch("pipeline.utils.http.requests.get")
    def test_backoff_sequence_with_4_retries(self, mock_get, mock_sleep):
        """4 retries → sleep(1), sleep(2), sleep(4)."""
        mock_get.return_value = _make_response(429)

        result = _api_get("https://example.com", params={}, max_retries=4)

        self.assertIsNone(result)
        self.assertEqual(mock_get.call_count, 4)
        sleep_delays = [c.args[0] for c in mock_sleep.call_args_list]
        self.assertEqual(sleep_delays, [1.0, 2.0, 4.0])


class TestApiGetLogging(unittest.TestCase):
    """Verify that logging occurs at appropriate levels."""

    @patch("pipeline.utils.http.time.sleep")
    @patch("pipeline.utils.http.requests.get")
    def test_warning_logged_on_retry(self, mock_get, mock_sleep):
        mock_get.side_effect = [_make_response(429), _make_response(200)]
        test_logger = MagicMock(spec=logging.Logger)

        _api_get("https://example.com", params={}, max_retries=3, logger=test_logger)

        test_logger.warning.assert_called()

    @patch("pipeline.utils.http.time.sleep")
    @patch("pipeline.utils.http.requests.get")
    def test_error_logged_on_final_failure(self, mock_get, mock_sleep):
        mock_get.return_value = _make_response(429)
        test_logger = MagicMock(spec=logging.Logger)

        result = _api_get("https://example.com", params={}, max_retries=3, logger=test_logger)

        self.assertIsNone(result)
        test_logger.error.assert_called()

    @patch("pipeline.utils.http.time.sleep")
    @patch("pipeline.utils.http.requests.get")
    def test_custom_logger_used(self, mock_get, mock_sleep):
        """Custom logger receives all log messages instead of default."""
        mock_get.return_value = _make_response(200)
        custom_logger = MagicMock(spec=logging.Logger)

        _api_get("https://example.com", params={}, logger=custom_logger)

        # No error should be logged on success
        custom_logger.error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
