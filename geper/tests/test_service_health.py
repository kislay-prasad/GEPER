"""
Tests for `utils/service_health.py` -- the startup external-service
health check and the runtime retry-skip registry it feeds. No live
network calls anywhere here: `requests.request`/`requests.post`/
`requests.get` are mocked throughout, and no model weights or pipeline
stages are touched (see `docs/` local-machine-constraints notes: this
repo is developed on an 8GB-RAM machine, so tests must stay this
lightweight).

Covers:
  - `_http_probe`'s status classification (200 -> healthy, 5xx ->
    degraded, timeout/connection error -> offline, other 4xx ->
    healthy/reachable).
  - `run_startup_checks` populating the registry and printing the
    status table, including the disabled (`CONFIG.health_check.ENABLED
    = False`) no-op path.
  - `note_skip`/`note_failure`/`note_success` and the final summary's
    per-service status string.
  - An end-to-end check, against a real client
    (`annotation/indigenomes.py`), that a service already marked
    offline is skipped without a single HTTP call -- the actual
    behavior the IndiGenomes-timeout incident (module docstring) this
    feature exists to fix.
"""

import unittest
from unittest import mock

import requests

from utils import service_health as sh


def _resp(status_code=200, reason="OK"):
    response = mock.Mock()
    response.status_code = status_code
    response.reason = reason
    return response


def _fake_config(enabled=True, timeout=1.0):
    cfg = mock.Mock()
    cfg.health_check.ENABLED = enabled
    cfg.health_check.TIMEOUT_SECS = timeout
    return cfg


class TestHttpProbe(unittest.TestCase):
    def test_200_is_healthy(self):
        with (
            mock.patch("utils.service_health.CONFIG", _fake_config()),
            mock.patch("utils.service_health.requests.request", return_value=_resp(200)),
        ):
            status, detail = sh._http_probe("GET", "https://example-not-real.invalid")
        self.assertEqual(status, sh.ServiceStatus.HEALTHY)
        self.assertEqual(detail, "")

    def test_404_is_still_healthy_host_is_reachable(self):
        with (
            mock.patch("utils.service_health.CONFIG", _fake_config()),
            mock.patch("utils.service_health.requests.request", return_value=_resp(404, "Not Found")),
        ):
            status, _ = sh._http_probe("GET", "https://example-not-real.invalid")
        self.assertEqual(status, sh.ServiceStatus.HEALTHY)

    def test_502_is_degraded(self):
        with (
            mock.patch("utils.service_health.CONFIG", _fake_config()),
            mock.patch("utils.service_health.requests.request", return_value=_resp(502, "Bad Gateway")),
        ):
            status, detail = sh._http_probe("GET", "https://example-not-real.invalid")
        self.assertEqual(status, sh.ServiceStatus.DEGRADED)
        self.assertIn("502", detail)
        self.assertIn("Bad Gateway", detail)

    def test_503_is_degraded(self):
        with (
            mock.patch("utils.service_health.CONFIG", _fake_config()),
            mock.patch("utils.service_health.requests.request", return_value=_resp(503, "Service Unavailable")),
        ):
            status, _ = sh._http_probe("GET", "https://example-not-real.invalid")
        self.assertEqual(status, sh.ServiceStatus.DEGRADED)

    def test_timeout_is_offline(self):
        with (
            mock.patch("utils.service_health.CONFIG", _fake_config()),
            mock.patch("utils.service_health.requests.request", side_effect=requests.Timeout()),
        ):
            status, detail = sh._http_probe("GET", "https://example-not-real.invalid")
        self.assertEqual(status, sh.ServiceStatus.OFFLINE)
        self.assertEqual(detail, "Timeout")

    def test_connection_error_is_offline(self):
        with (
            mock.patch("utils.service_health.CONFIG", _fake_config()),
            mock.patch("utils.service_health.requests.request", side_effect=requests.ConnectionError("refused")),
        ):
            status, detail = sh._http_probe("GET", "https://example-not-real.invalid")
        self.assertEqual(status, sh.ServiceStatus.OFFLINE)
        self.assertEqual(detail, "ConnectionError")


class TestIsTransientHttpError(unittest.TestCase):
    def _http_error(self, status_code):
        exc = requests.HTTPError(f"{status_code} error")
        exc.response = _resp(status_code)
        return exc

    def test_400_is_not_transient(self):
        self.assertFalse(sh.is_transient_http_error(self._http_error(400)))

    def test_404_is_not_transient(self):
        self.assertFalse(sh.is_transient_http_error(self._http_error(404)))

    def test_500_is_transient(self):
        self.assertTrue(sh.is_transient_http_error(self._http_error(500)))

    def test_502_503_504_are_transient(self):
        for code in (502, 503, 504):
            self.assertTrue(sh.is_transient_http_error(self._http_error(code)))

    def test_timeout_is_transient(self):
        self.assertTrue(sh.is_transient_http_error(requests.Timeout()))

    def test_connection_error_is_transient(self):
        self.assertTrue(sh.is_transient_http_error(requests.ConnectionError("refused")))

    def test_value_error_is_transient(self):
        # A malformed-JSON body -- retried like any other transient parse hiccup.
        self.assertTrue(sh.is_transient_http_error(ValueError("bad json")))


class TestRunStartupChecks(unittest.TestCase):
    def setUp(self):
        self.registry = sh.ServiceHealthRegistry()

    def test_mixed_statuses_populate_registry(self):
        checks = [
            sh.ServiceCheck("Ensembl", lambda: (sh.ServiceStatus.HEALTHY, "")),
            sh.ServiceCheck("ClinVar", lambda: (sh.ServiceStatus.HEALTHY, "")),
            sh.ServiceCheck("dbSNP", lambda: (sh.ServiceStatus.HEALTHY, "")),
            sh.ServiceCheck("IndiGenomes", lambda: (sh.ServiceStatus.OFFLINE, "Timeout")),
            sh.ServiceCheck("ClinGen ERepo", lambda: (sh.ServiceStatus.DEGRADED, "502 Bad Gateway")),
        ]
        with mock.patch("utils.service_health.CONFIG", _fake_config()):
            self.registry.run_startup_checks(checks)

        self.assertFalse(self.registry.is_offline("Ensembl"))
        self.assertFalse(self.registry.is_offline("ClinVar"))
        self.assertFalse(self.registry.is_offline("dbSNP"))
        self.assertTrue(self.registry.is_offline("IndiGenomes"))
        # Degraded is not offline -- retries should still proceed normally.
        self.assertFalse(self.registry.is_offline("ClinGen ERepo"))

    def test_prints_a_table_with_expected_symbols_and_labels(self):
        checks = [
            sh.ServiceCheck("Ensembl", lambda: (sh.ServiceStatus.HEALTHY, "")),
            sh.ServiceCheck("IndiGenomes", lambda: (sh.ServiceStatus.OFFLINE, "Timeout")),
            sh.ServiceCheck("ClinGen ERepo", lambda: (sh.ServiceStatus.DEGRADED, "502 Bad Gateway")),
        ]
        with mock.patch("utils.service_health.CONFIG", _fake_config()), mock.patch.object(sh.logger, "info") as info:
            self.registry.run_startup_checks(checks)

        printed = "\n".join(call.args[0] for call in info.call_args_list)
        self.assertIn("External Service Status", printed)
        self.assertIn("✓", printed)
        self.assertIn("Online", printed)
        self.assertIn("✗", printed)
        self.assertIn("Offline (Timeout)", printed)
        self.assertIn("⚠", printed)
        self.assertIn("Degraded (502 Bad Gateway)", printed)

    def test_disabled_health_check_is_a_noop(self):
        checks = [sh.ServiceCheck("Ensembl", lambda: (sh.ServiceStatus.OFFLINE, "Timeout"))]
        with mock.patch("utils.service_health.CONFIG", _fake_config(enabled=False)):
            self.registry.run_startup_checks(checks)
        # Never probed -- status stays UNKNOWN, so is_offline (which only
        # matches OFFLINE) correctly reports "not confirmed offline" and
        # runtime retries proceed as if the check never ran.
        self.assertFalse(self.registry.is_offline("Ensembl"))

    def test_no_services_configured_is_a_noop(self):
        with mock.patch("utils.service_health.CONFIG", _fake_config()), mock.patch.object(sh.logger, "info") as info:
            self.registry.run_startup_checks([])
        info.assert_not_called()

    def test_probe_exception_is_treated_as_offline_not_a_crash(self):
        def _boom():
            raise RuntimeError("probe blew up")

        checks = [sh.ServiceCheck("Ensembl", _boom)]
        with mock.patch("utils.service_health.CONFIG", _fake_config()):
            self.registry.run_startup_checks(checks)
        self.assertTrue(self.registry.is_offline("Ensembl"))


class TestRuntimeTrackingAndSummary(unittest.TestCase):
    def setUp(self):
        self.registry = sh.ServiceHealthRegistry()

    def _mark(self, name, status, detail=""):
        with mock.patch("utils.service_health.CONFIG", _fake_config()):
            self.registry.run_startup_checks([sh.ServiceCheck(name, lambda: (status, detail))])

    def test_note_skip_logs_once_but_counts_every_call(self):
        self._mark("IndiGenomes", sh.ServiceStatus.OFFLINE, "Timeout")
        with mock.patch.object(sh.logger, "warning") as warn:
            self.registry.note_skip("IndiGenomes")
            self.registry.note_skip("IndiGenomes")
            self.registry.note_skip("IndiGenomes")
        self.assertEqual(warn.call_count, 1)
        self.assertEqual(self.registry._get("IndiGenomes").skipped_count, 3)

    def test_summary_healthy_service_with_no_failures(self):
        self._mark("Ensembl", sh.ServiceStatus.HEALTHY)
        self.registry.note_success("Ensembl")
        self.registry.note_success("Ensembl")
        record = self.registry._get("Ensembl")
        self.assertEqual(self.registry._summary_status(record), "Healthy")

    def test_summary_intermittent_counts_failures(self):
        self._mark("ClinGen ERepo", sh.ServiceStatus.DEGRADED, "502 Bad Gateway")
        for _ in range(5):
            self.registry.note_failure("ClinGen ERepo")
        record = self.registry._get("ClinGen ERepo")
        self.assertEqual(self.registry._summary_status(record), "Intermittent (5 failures)")

    def test_summary_offline_service_shows_skip_count(self):
        self._mark("IndiGenomes", sh.ServiceStatus.OFFLINE, "Timeout")
        self.registry.note_skip("IndiGenomes")
        self.registry.note_skip("IndiGenomes")
        record = self.registry._get("IndiGenomes")
        self.assertEqual(self.registry._summary_status(record), "Offline (Skipped x2)")

    def test_print_summary_renders_all_tracked_services(self):
        self._mark("Ensembl", sh.ServiceStatus.HEALTHY)
        self._mark("IndiGenomes", sh.ServiceStatus.OFFLINE, "Timeout")
        self.registry.note_skip("IndiGenomes")
        with mock.patch.object(sh.logger, "info") as info:
            self.registry.print_summary()
        printed = "\n".join(call.args[0] for call in info.call_args_list)
        self.assertIn("External Services Summary", printed)
        self.assertIn("Ensembl", printed)
        self.assertIn("Healthy", printed)
        self.assertIn("IndiGenomes", printed)
        self.assertIn("Offline (Skipped x1)", printed)

    def test_print_summary_with_no_tracked_services_is_a_noop(self):
        with mock.patch.object(sh.logger, "info") as info:
            self.registry.print_summary()
        info.assert_not_called()

    def test_reset_clears_all_state(self):
        self._mark("Ensembl", sh.ServiceStatus.OFFLINE, "Timeout")
        self.registry.note_skip("Ensembl")
        self.registry.reset()
        self.assertFalse(self.registry.is_offline("Ensembl"))
        self.assertEqual(self.registry._get("Ensembl").skipped_count, 0)


class TestIndiGenomesSkipsRetriesWhenOffline(unittest.TestCase):
    """
    End-to-end check against the actual regression this feature fixes:
    IndiGenomes confirmed offline at startup must not spend the usual
    3-attempt / ~90s retry budget on every subsequent variant.
    """

    def setUp(self):
        from annotation import indigenomes as ind_mod

        self.ind_mod = ind_mod
        self.offline_registry = sh.ServiceHealthRegistry()
        with mock.patch("utils.service_health.CONFIG", _fake_config()):
            self.offline_registry.run_startup_checks(
                [sh.ServiceCheck("IndiGenomes", lambda: (sh.ServiceStatus.OFFLINE, "Timeout"))]
            )

    def test_post_raises_immediately_without_any_http_call(self):
        fake_cfg = mock.Mock()
        fake_cfg.indigenomes.ENDPOINT = "https://example-not-real.invalid/indigen/data.php"
        fake_cfg.indigenomes.MAX_RETRIES = 3
        fake_cfg.indigenomes.RETRY_BACKOFF_SECS = 0.001
        fake_cfg.indigenomes.QUERY_TIMEOUT_SECS = 5

        with (
            mock.patch.object(self.ind_mod, "HEALTH", self.offline_registry),
            mock.patch.object(self.ind_mod, "CONFIG", fake_cfg),
            mock.patch.object(self.ind_mod.requests, "post") as post,
        ):
            with self.assertRaises(self.ind_mod.ExternalAPIError):
                self.ind_mod._post("chr1-1-A-G")
            with self.assertRaises(self.ind_mod.ExternalAPIError):
                self.ind_mod._post("chr2-2-C-T")

        post.assert_not_called()
        self.assertEqual(self.offline_registry._get("IndiGenomes").skipped_count, 2)

    def test_when_healthy_retries_proceed_normally(self):
        healthy_registry = sh.ServiceHealthRegistry()
        with mock.patch("utils.service_health.CONFIG", _fake_config()):
            healthy_registry.run_startup_checks(
                [sh.ServiceCheck("IndiGenomes", lambda: (sh.ServiceStatus.HEALTHY, ""))]
            )

        fake_cfg = mock.Mock()
        fake_cfg.indigenomes.ENDPOINT = "https://example-not-real.invalid/indigen/data.php"
        fake_cfg.indigenomes.MAX_RETRIES = 3
        fake_cfg.indigenomes.RETRY_BACKOFF_SECS = 0.001
        fake_cfg.indigenomes.QUERY_TIMEOUT_SECS = 5

        ok_response = mock.Mock()
        ok_response.raise_for_status.return_value = None
        ok_response.json.return_value = {"mydata": []}

        with (
            mock.patch.object(self.ind_mod, "HEALTH", healthy_registry),
            mock.patch.object(self.ind_mod, "CONFIG", fake_cfg),
            mock.patch.object(self.ind_mod.requests, "post", return_value=ok_response) as post,
        ):
            result = self.ind_mod._post("chr1-1-A-G")

        self.assertEqual(result, {"mydata": []})
        post.assert_called_once()
        self.assertEqual(healthy_registry._get("IndiGenomes").success_count, 1)


class TestDbSnpSkipsRetriesWhenOffline(unittest.TestCase):
    def test_request_json_raises_immediately_without_any_http_call(self):
        from database import dbsnp_client as db_mod

        offline_registry = sh.ServiceHealthRegistry()
        with mock.patch("utils.service_health.CONFIG", _fake_config()):
            offline_registry.run_startup_checks(
                [sh.ServiceCheck("dbSNP", lambda: (sh.ServiceStatus.OFFLINE, "Timeout"))]
            )

        fake_cfg = mock.Mock()
        fake_cfg.api.MAX_RETRIES = 3
        fake_cfg.api.RETRY_BACKOFF_SECS = 0.001
        fake_cfg.api.REQUEST_TIMEOUT_SECS = 5

        client = db_mod.DbSNPClient.__new__(db_mod.DbSNPClient)

        with (
            mock.patch.object(db_mod, "HEALTH", offline_registry),
            mock.patch.object(db_mod, "CONFIG", fake_cfg),
            mock.patch.object(db_mod.requests, "get") as get,
        ):
            with self.assertRaises(db_mod.ExternalAPIError):
                client._request_json("https://example-not-real.invalid/esearch", {})

        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
