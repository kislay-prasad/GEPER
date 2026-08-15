"""Tests for pipeline/interpro/provider.py, with `requests` mocked -- see that module's docstring for the sandbox network caveat."""

import unittest
from unittest import mock

from pipeline.interpro.provider import CompositeInterProProvider, LiveAPIInterProProvider, LocalDatasetInterProProvider


def _cfg(fake_config, enabled=True, offline=False):
    fake_config.interpro.ENABLED = enabled
    fake_config.interpro.OFFLINE_MODE = offline
    fake_config.interpro.PAGE_SIZE = 200
    fake_config.interpro.QUERY_TIMEOUT_SECS = 30
    fake_config.interpro.MAX_RETRIES = 3
    fake_config.interpro.RETRY_BACKOFF_SECS = 0


class TestLiveAPIInterProProvider(unittest.TestCase):
    def test_query_returns_annotation_on_success(self):
        provider = LiveAPIInterProProvider()
        fake_response = mock.Mock(status_code=200)
        fake_response.json.return_value = {
            "results": [
                {
                    "metadata": {
                        "accession": "IPR000001",
                        "name": "Kringle",
                        "type": "domain",
                        "source_database": "interpro",
                    },
                    "proteins": [{"entry_protein_locations": [{"fragments": [{"start": 1, "end": 50}]}]}],
                }
            ]
        }
        fake_response.raise_for_status.return_value = None

        with (
            mock.patch("pipeline.interpro.provider.requests.get", return_value=fake_response) as fake_get,
            mock.patch("pipeline.interpro.provider.CONFIG") as fake_config,
        ):
            _cfg(fake_config)
            result = provider.query("P04637")

        fake_get.assert_called_once()
        self.assertTrue(result.found)
        self.assertEqual(len(result.domains), 1)

    def test_404_treated_as_not_found_not_error(self):
        provider = LiveAPIInterProProvider()
        fake_response = mock.Mock(status_code=404)

        with (
            mock.patch("pipeline.interpro.provider.requests.get", return_value=fake_response),
            mock.patch("pipeline.interpro.provider.CONFIG") as fake_config,
        ):
            _cfg(fake_config)
            result = provider.query("P99999")

        self.assertFalse(result.found)
        self.assertIsNone(result.error)

    def test_204_no_content_treated_as_not_found_not_error_and_not_retried(self):
        """
        Regression test: confirmed live against the real API
        (2026-07-30, accession A0A3G1DJQ2 from a real Colab run) that
        InterPro returns HTTP 204 No Content -- not 404 -- for a
        syntactically valid accession with zero domain matches. The
        response has `Content-Type: application/json` but an empty
        body, so `response.json()` used to raise `JSONDecodeError`
        (caught by the retry loop's `except ValueError`), burning all
        `MAX_RETRIES` attempts on a deterministic response and then
        reporting a false "lookup failed" for what was actually a
        successful, informative answer.
        """
        provider = LiveAPIInterProProvider()
        fake_response = mock.Mock(status_code=204, content=b"")
        fake_response.raise_for_status.return_value = None
        fake_response.json.side_effect = ValueError("Expecting value: line 1 column 1 (char 0)")

        with (
            mock.patch("pipeline.interpro.provider.requests.get", return_value=fake_response) as fake_get,
            mock.patch("pipeline.interpro.provider.CONFIG") as fake_config,
        ):
            _cfg(fake_config)
            result = provider.query("A0A3G1DJQ2")

        fake_get.assert_called_once()  # must not retry a deterministic 204
        self.assertFalse(result.found)
        self.assertIsNone(result.error)
        self.assertEqual(result.domains, [])

    def test_repeated_real_failure_still_retries_and_reports_error(self):
        """Regression guard alongside the 204 fix above: a genuinely
        transient/repeated failure must still retry MAX_RETRIES times
        and end up as an error, not silently swallowed."""
        provider = LiveAPIInterProProvider()
        fake_response = mock.Mock(status_code=500)
        fake_response.raise_for_status.side_effect = Exception("boom")

        import requests as real_requests

        with (
            mock.patch(
                "pipeline.interpro.provider.requests.get",
                side_effect=real_requests.exceptions.ConnectionError("connection reset"),
            ) as fake_get,
            mock.patch("pipeline.interpro.provider.CONFIG") as fake_config,
            mock.patch("pipeline.interpro.provider.time.sleep"),
        ):
            _cfg(fake_config)
            result = provider.query("P04637")

        self.assertEqual(fake_get.call_count, 3)  # MAX_RETRIES
        self.assertFalse(result.found)
        self.assertIsNotNone(result.error)
        # Round 24: `error` used to embed the full request URL and raw
        # `ConnectionError` text (`_get`'s own f-string) -- rendered
        # unconditionally into `interpro_error` by
        # `report/report_generator.py` and embedded verbatim in
        # geper_results.json. Full detail still reaches the log; what's
        # returned to callers/reports must not.
        self.assertNotIn("http", result.error)
        self.assertNotIn("connection reset", result.error)
        self.assertEqual(result.error, "InterPro REST API request failed after 3 attempts")

    def test_network_failure_returns_error_annotation_not_raise(self):
        import requests as real_requests

        provider = LiveAPIInterProProvider()
        with (
            mock.patch(
                "pipeline.interpro.provider.requests.get", side_effect=real_requests.ConnectionError("no route")
            ),
            mock.patch("pipeline.interpro.provider.CONFIG") as fake_config,
            mock.patch("pipeline.interpro.provider.time.sleep"),
        ):
            _cfg(fake_config)
            fake_config.interpro.MAX_RETRIES = 2
            result = provider.query("P04637")

        self.assertFalse(result.found)
        self.assertIsNotNone(result.error)
        self.assertNotIn("http", result.error)
        self.assertNotIn("no route", result.error)
        self.assertEqual(result.error, "InterPro REST API request failed after 2 attempts")

    def test_disabled_or_offline_returns_none(self):
        provider = LiveAPIInterProProvider()
        with mock.patch("pipeline.interpro.provider.CONFIG") as fake_config:
            _cfg(fake_config, enabled=False)
            self.assertIsNone(provider.query("P04637"))
            _cfg(fake_config, enabled=True, offline=True)
            self.assertIsNone(provider.query("P04637"))


class TestLocalDatasetInterProProvider(unittest.TestCase):
    def test_reads_jsonl_dataset(self):
        import json
        import os
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(
                json.dumps(
                    {
                        "accession": "P04637",
                        "payload": {
                            "results": [
                                {
                                    "metadata": {
                                        "accession": "IPR1",
                                        "name": "x",
                                        "type": "domain",
                                        "source_database": "interpro",
                                    },
                                    "proteins": [],
                                }
                            ]
                        },
                    }
                )
                + "\n"
            )
            path = fh.name

        try:
            provider = LocalDatasetInterProProvider(dataset_path=path)
            self.assertTrue(provider.is_available())
            result = provider.query("P04637")
            self.assertTrue(result.found)

            missing = provider.query("Q99999")
            self.assertFalse(missing.found)
        finally:
            os.unlink(path)


class TestCompositeInterProProvider(unittest.TestCase):
    def test_falls_back_to_api_when_local_unavailable(self):
        local = mock.Mock()
        local.query.return_value = None
        api = mock.Mock()
        api.query.return_value = mock.Mock(found=True, error=None)

        composite = CompositeInterProProvider(local_provider=local, api_provider=api)
        with mock.patch("pipeline.interpro.provider.CONFIG") as fake_config:
            fake_config.interpro.OFFLINE_MODE = False
            result = composite.query("P04637")

        api.query.assert_called_once()
        self.assertTrue(result.found)

    def test_offline_mode_never_calls_api(self):
        local = mock.Mock()
        local.query.return_value = None
        api = mock.Mock()

        composite = CompositeInterProProvider(local_provider=local, api_provider=api)
        with mock.patch("pipeline.interpro.provider.CONFIG") as fake_config:
            fake_config.interpro.OFFLINE_MODE = True
            result = composite.query("P04637")

        api.query.assert_not_called()
        self.assertIsNotNone(result.error)


if __name__ == "__main__":
    unittest.main()
