"""
clinical/tests/test_w172_log_redaction_and_proxy.py
───────────────────────────────────────────────────
Stage 4, increment 3. Two properties of how the service is run behind a proxy:

1. The ACCESS LOG carries no query-parameter value. Proved three ways: the pure
   functions; the logging filter on real uvicorn-shaped records through uvicorn's
   own formatter; and a REAL uvicorn process, started with the shipped
   `clinical/log_config.json`, serving real requests whose captured stdout is read.
2. Only an explicitly trusted proxy address can influence the client address that
   the service sees (uvicorn's `ProxyHeadersMiddleware`, which `FORWARDED_ALLOW_IPS`
   configures). The Docker/nginx run that confirmed the same through a real proxy
   is recorded in docs/CLINICAL_DEPLOYMENT.md.

No database and no Docker are needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from clinical import log_redaction as lr

CLINICAL = pathlib.Path(__file__).resolve().parent.parent
REPO = CLINICAL.parent
LOG_CONFIG = CLINICAL / "log_config.json"
MARK = "distinctive-secret-value-7f3a"


class TestRedactQuery:
    @pytest.mark.parametrize(
        "query, expected",
        [
            ("owner=a@b.example", "owner=<redacted>"),
            ("a=1&b=2", "a=<redacted>&b=<redacted>"),
            ("a=1&a=2&a=3", "a=<redacted>&a=<redacted>&a=<redacted>"),  # repeated
            ("owner=a%40b.example", "owner=<redacted>"),  # encoded value
            ("owner=", "owner=<redacted>"),  # empty value
            ("owner", "<redacted>"),  # no "="
            ("someone@example.org", "<redacted>"),  # a bare address
            ("a%40b=1", "<redacted>=<redacted>"),  # encoded name is not a plain token
            ("a=1&&b=2&", "a=<redacted>&b=<redacted>"),  # empty segments
            ("a=b=c", "a=<redacted>"),  # second "=" is part of the value
            ("filter[owner]=x", "filter[owner]=<redacted>"),
            ("a=1;b=2", "a=<redacted>"),  # ";" is value text, not a separator
            ("x" * 65 + "=1", "<redacted>=<redacted>"),  # over-long name
        ],
    )
    def test_cases(self, query, expected):
        assert lr.redact_query(query) == expected

    def test_no_input_value_survives(self):
        for q in (f"owner={MARK}", f"{MARK}", f"a={MARK}&b={MARK}", f"a%3D{MARK}", f"a={MARK}%00", f"a%40b={MARK}"):
            assert MARK not in lr.redact_query(q), q

    def test_plain_parameter_names_are_kept_by_policy(self):
        # Names are the API's vocabulary and are kept for diagnosis; only the values
        # are patient-bearing. A client that puts data in a NAME is putting it in the
        # URL it chose, and that is documented rather than hidden.
        assert lr.redact_query(f"{MARK}=1") == f"{MARK}=<redacted>"


class TestRedactTarget:
    def test_path_is_kept_and_query_redacted(self):
        t = "/orders/3dd25e4e-f762-4e13-8272-567942cb2735?owner=x"
        assert lr.redact_target(t) == "/orders/3dd25e4e-f762-4e13-8272-567942cb2735?owner=<redacted>"

    def test_no_query_is_unchanged(self):
        assert lr.redact_target("/readyz") == "/readyz"

    def test_question_mark_with_nothing_after_it_drops_the_mark(self):
        assert lr.redact_target("/readyz?") == "/readyz"

    @pytest.mark.parametrize("bad", ["/x?a=1\nINFO: forged line", "/x\r\n", "/x\x00", None, 5, b"/x"])
    def test_untrustworthy_targets_return_none(self, bad):
        assert lr.redact_target(bad) is None


def _record(args, msg='%s - "%s %s HTTP/%s" %d'):
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, msg, args, None)


def _format(record):
    from uvicorn.logging import AccessFormatter

    lr.AccessLogRedactionFilter().filter(record)
    return AccessFormatter(
        fmt='%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False
    ).format(record)


class TestFilterThroughUvicornFormatter:
    def test_well_formed_record(self):
        out = _format(_record(("172.29.80.10:44054", "GET", f"/exceptions/worklist?owner={MARK}", "1.1", 200)))
        assert out == 'INFO:     172.29.80.10:44054 - "GET /exceptions/worklist?owner=<redacted> HTTP/1.1" 200 OK'
        assert MARK not in out

    @pytest.mark.parametrize(
        "args",
        [
            ("a", "GET", f"/x?o={MARK}"),  # too few
            ("a", "GET", f"/x?o={MARK}", "1.1", 200, "extra"),  # too many
            ("a", "GET", f"/x?o={MARK}\nINFO: forged", "1.1", 200),  # control character
            ("a", "GET", None, "1.1", 200),
        ],
    )
    def test_malformed_records_fail_closed(self, args):
        out = _format(_record(args))
        assert MARK not in out
        assert "forged" not in out
        assert lr.UNPARSEABLE in out

    @pytest.mark.parametrize("args", [None, (), {"owner": MARK}])
    def test_odd_argument_shapes_never_reach_the_output(self, args):
        record = _record(("a", "GET", "/x", "1.1", 200))
        record.args = args  # set after construction: LogRecord itself unpacks a lone mapping
        assert lr.AccessLogRedactionFilter().filter(record) is True
        assert MARK not in repr(record.args)
        assert record.args == ("-", "-", lr.UNPARSEABLE, "-", 0)

    def test_the_filter_never_drops_a_record(self):
        assert lr.AccessLogRedactionFilter().filter(_record(("a", "GET", "/x", "1.1", 200))) is True


def test_shipped_config_attaches_the_filter_to_the_access_handler():
    cfg = json.loads(LOG_CONFIG.read_text())
    assert cfg["filters"]["redact_access"]["()"] == "clinical.log_redaction.AccessLogRedactionFilter"
    assert cfg["handlers"]["access"]["filters"] == ["redact_access"]
    assert cfg["loggers"]["uvicorn.access"]["handlers"] == ["access"]
    assert cfg["loggers"]["uvicorn.access"]["propagate"] is False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    """Start a real uvicorn process with the shipped log config. Returns
    start(app_ref) -> (port, read_output)."""
    (tmp_path / "tiny.py").write_text(
        "async def app(scope, receive, send):\n"
        "    if scope['type'] != 'http':\n"
        "        return\n"
        "    await send({'type': 'http.response.start', 'status': 200, 'headers': []})\n"
        "    await send({'type': 'http.response.body', 'body': b'ok'})\n"
    )
    procs = []

    def start(app_ref):
        port = _free_port()
        out = open(tmp_path / f"out{len(procs)}.log", "wb")
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO), str(tmp_path)]), "PYTHONUNBUFFERED": "1"}
        for k in ("CLINICAL_DSN", "CLINICAL_DSN_FILE"):
            env.pop(k, None)
        p = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                app_ref,
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-config",
                str(LOG_CONFIG),
            ],
            stdout=out,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(tmp_path),
        )
        procs.append((p, out))
        name = out.name
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            p.kill()
            pytest.fail("uvicorn did not start: " + pathlib.Path(name).read_text(errors="replace"))
        return port, lambda: pathlib.Path(name).read_text(errors="replace")

    yield start
    for p, out in procs:
        p.terminate()
        p.wait(timeout=10)
        out.close()


def _get(port, target, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{target}", headers=headers or {})
    try:
        return urllib.request.urlopen(req, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code


class TestRealUvicornProcess:
    def test_access_log_has_no_query_value_and_keeps_operational_fields(self, server):
        port, out = server("tiny:app")
        for target in (
            f"/exceptions/worklist?owner={MARK}",
            f"/x?a={MARK}&a={MARK}2",
            f"/x?owner=a%40{MARK}.example",
            f"/x?{MARK}",
            "/orders/3dd25e4e-f762-4e13-8272-567942cb2735",
        ):
            assert _get(port, target, {"Authorization": f"Bearer {MARK}", "Cookie": f"s={MARK}"}) == 200
        time.sleep(0.5)
        text = out()
        assert MARK not in text, text
        lines = [line for line in text.splitlines() if ' - "GET ' in line]
        assert len(lines) == 5, text
        assert any('"GET /exceptions/worklist?owner=<redacted> HTTP/1.1" 200 OK' in line for line in lines)
        assert any("127.0.0.1:" in line for line in lines)  # client address kept
        # path UUIDs are deliberately kept (documented as sensitive identifiers)
        assert any("/orders/3dd25e4e-f762-4e13-8272-567942cb2735 HTTP/1.1" in line for line in lines)

    def test_a_malformed_target_is_not_logged_raw(self, server):
        port, out = server("tiny:app")
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(f"GET /x?owner={MARK}%0aINFO:%20forged HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
            s.recv(4096)
        time.sleep(0.5)
        assert MARK not in out(), out()

    def test_the_real_clinical_app_is_covered_too(self, server):
        port, out = server("clinical.app:app")
        assert _get(port, f"/healthz?owner={MARK}") == 200
        time.sleep(0.5)
        text = out()
        assert MARK not in text, text
        assert "/healthz?owner=<redacted>" in text


class TestOnlyATrustedProxyInfluencesTheClientAddress:
    """uvicorn's ProxyHeadersMiddleware as FORWARDED_ALLOW_IPS configures it."""

    TRUSTED = "172.29.80.10"

    @staticmethod
    def _client_seen(trusted_hosts, peer, xff):
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        seen = {}

        async def app(scope, receive, send):
            seen["client"] = scope["client"]

        headers = [(b"x-forwarded-for", xff.encode())] if xff else []
        scope = {"type": "http", "client": (peer, 50000), "headers": headers, "scheme": "http"}
        asyncio.run(ProxyHeadersMiddleware(app, trusted_hosts=trusted_hosts)(scope, None, None))
        return seen["client"][0]

    def test_a_direct_untrusted_peer_cannot_forge_its_address(self):
        assert self._client_seen(self.TRUSTED, "172.29.80.51", "203.0.113.99") == "172.29.80.51"

    def test_the_trusted_proxy_reports_the_real_client(self):
        assert self._client_seen(self.TRUSTED, self.TRUSTED, "172.29.80.52") == "172.29.80.52"

    def test_forged_left_hand_entries_behind_the_trusted_proxy_are_discarded(self):
        # the proxy APPENDS the address it saw, so client-supplied entries sit on the left
        xff = "203.0.113.77, 198.51.100.5, 172.29.80.53"
        assert self._client_seen(self.TRUSTED, self.TRUSTED, xff) == "172.29.80.53"

    def test_default_trusts_nobody_in_a_container(self):
        # uvicorn's default is 127.0.0.1; a proxy at another address is not believed
        assert self._client_seen("127.0.0.1", self.TRUSTED, "203.0.113.200") == self.TRUSTED

    def test_a_neighbour_of_the_trusted_address_is_not_trusted(self):
        assert self._client_seen(self.TRUSTED, "172.29.80.11", "203.0.113.5") == "172.29.80.11"

    def test_the_compose_file_never_defaults_to_a_wildcard(self):
        text = (REPO / "docker-compose.clinical.yml").read_text(encoding="utf8")
        line = next(ln for ln in text.splitlines() if "FORWARDED_ALLOW_IPS:" in ln)
        assert "*" not in line


class TestImageAndDocumentWiring:
    def test_image_starts_uvicorn_with_the_redacting_config(self):
        text = (CLINICAL / "Dockerfile.slim").read_text(encoding="utf8")
        cmd = next(ln for ln in text.splitlines() if ln.startswith("CMD "))
        assert '"--log-config", "/app/clinical/log_config.json"' in cmd
        assert "--no-access-log" not in cmd
        assert "COPY clinical/log_config.json /app/clinical/log_config.json" in text

    def test_the_config_is_let_into_the_build_context(self):
        ignore = (CLINICAL / "Dockerfile.slim.dockerignore").read_text(encoding="utf8").splitlines()
        assert "!clinical/log_config.json" in ignore

    def test_document_states_the_policy_and_that_path_uuids_stay_sensitive(self):
        doc = (REPO / "docs" / "CLINICAL_DEPLOYMENT.md").read_text(encoding="utf8")
        for needle in (
            "every query-parameter value is replaced by `<redacted>`",
            "sensitive identifiers",
            "fail closed",
            "## External PostgreSQL",
            "supersede the `w125-clinical-service-up` branch",
        ):
            assert needle in doc, needle
