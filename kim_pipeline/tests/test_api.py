"""
tests/test_api.py
──────────────────
Unit tests for api/main.py using FastAPI TestClient.

Tests cover all endpoints:
  - GET /health
  - GET /api/v1/ready
  - GET /api/v1/config
  - POST /api/v1/upload/fastq
  - POST /api/v1/upload/fasta
  - POST /api/v1/pipeline/start (validation errors)
  - GET /api/v1/pipeline/{run_id}/status (404 case)
  - GET /api/v1/pipeline/{run_id}/progress (404 case)
  - GET /api/v1/pipeline/{run_id}/report (404 / not-completed cases)
  - GET /api/v1/pipeline/{run_id}/log (404 case)
  - DELETE /api/v1/pipeline/{run_id}
  - GET /api/v1/pipeline (list runs)

Pipeline execution itself is NOT tested here (requires real FASTQ + reference
genome data). Those are integration tests that run in a CI environment with
test fixtures. The API contract and request validation are fully covered.
"""

from __future__ import annotations

import io
import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# FastAPI TestClient requires httpx
try:
    from fastapi.testclient import TestClient
    from api.main import app, _RUNS

    _API_AVAILABLE = True
except ImportError:
    _API_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _API_AVAILABLE,
    reason="FastAPI / httpx not installed",
)


@pytest.fixture(autouse=True)
def clear_runs():
    """Reset the in-memory run registry before each test."""
    _RUNS.clear()
    yield
    _RUNS.clear()


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client_with_tmp(tmp_path):
    """TestClient with _UPLOAD_DIR and _OUTPUT_DIR patched to tmp_path.

    This is required for FIX 5 — tests that start pipelines must supply paths
    inside the approved roots, which are now validated in start_pipeline().
    """
    import api.main as main_mod

    orig_upload = main_mod._UPLOAD_DIR
    orig_output = main_mod._OUTPUT_DIR
    main_mod._UPLOAD_DIR = tmp_path
    main_mod._OUTPUT_DIR = tmp_path
    yield TestClient(app, raise_server_exceptions=False), tmp_path
    main_mod._UPLOAD_DIR = orig_upload
    main_mod._OUTPUT_DIR = orig_output


# ─── Health endpoints ─────────────────────────────────────────────────────────


class TestHealth:
    def test_health_returns_200(self, client):
        r = client.get("/health")
        assert r.status_code == 200

    def test_health_body_has_status_ok(self, client):
        r = client.get("/health")
        assert r.json()["status"] == "ok"

    def test_health_body_has_version(self, client):
        r = client.get("/health")
        assert "version" in r.json()

    def test_health_body_has_timestamp(self, client):
        r = client.get("/health")
        assert "timestamp" in r.json()

    def test_ready_returns_200(self, client):
        r = client.get("/api/v1/ready")
        assert r.status_code == 200

    def test_ready_body_has_status(self, client):
        r = client.get("/api/v1/ready")
        assert r.json()["status"] == "ready"

    def test_ready_shows_active_runs(self, client):
        r = client.get("/api/v1/ready")
        assert "active_runs" in r.json()


# ─── Config endpoint ──────────────────────────────────────────────────────────


class TestConfig:
    def test_config_returns_200(self, client):
        r = client.get("/api/v1/config")
        assert r.status_code == 200

    def test_config_body_has_config_key(self, client):
        r = client.get("/api/v1/config")
        assert "config" in r.json()

    def test_config_has_output_dir(self, client):
        r = client.get("/api/v1/config")
        assert "output_dir" in r.json()

    def test_config_has_upload_dir(self, client):
        r = client.get("/api/v1/config")
        assert "upload_dir" in r.json()


# ─── Upload endpoints ─────────────────────────────────────────────────────────


class TestUploadFASTQ:
    def _fastq_bytes(self, n: int = 5) -> bytes:
        lines = []
        for i in range(n):
            lines += [f"@read{i}", "ACGTACGT", "+", "IIIIIIII"]
        return "\n".join(lines).encode()

    def test_upload_fastq_returns_201(self, client):
        data = self._fastq_bytes()
        r = client.post(
            "/api/v1/upload/fastq",
            files={"file": ("sample.fastq", io.BytesIO(data), "text/plain")},
        )
        assert r.status_code == 201

    def test_upload_fastq_body_has_path(self, client):
        data = self._fastq_bytes()
        r = client.post(
            "/api/v1/upload/fastq",
            files={"file": ("sample.fastq", io.BytesIO(data), "text/plain")},
        )
        body = r.json()
        assert "path" in body
        assert Path(body["path"]).exists()

    def test_upload_fastq_gz_accepted(self, client):
        import gzip

        data = gzip.compress(self._fastq_bytes())
        r = client.post(
            "/api/v1/upload/fastq",
            files={"file": ("sample.fastq.gz", io.BytesIO(data), "application/gzip")},
        )
        assert r.status_code == 201

    def test_upload_wrong_extension_rejected(self, client):
        r = client.post(
            "/api/v1/upload/fastq",
            files={"file": ("sample.bam", io.BytesIO(b"binary"), "application/octet-stream")},
        )
        assert r.status_code == 400

    def test_upload_no_filename_rejected(self, client):
        r = client.post(
            "/api/v1/upload/fastq",
            files={"file": ("", io.BytesIO(b"data"), "text/plain")},
        )
        assert r.status_code in (400, 422)


class TestUploadFASTA:
    def _fasta_bytes(self) -> bytes:
        return b">chr1\nACGTACGTACGT\n>chr2\nTTTTGGGGAAAA\n"

    def test_upload_fasta_returns_201(self, client):
        r = client.post(
            "/api/v1/upload/fasta",
            files={"file": ("ref.fasta", io.BytesIO(self._fasta_bytes()), "text/plain")},
        )
        assert r.status_code == 201

    def test_upload_fasta_path_returned(self, client):
        r = client.post(
            "/api/v1/upload/fasta",
            files={"file": ("ref.fasta", io.BytesIO(self._fasta_bytes()), "text/plain")},
        )
        assert "path" in r.json()

    def test_upload_fa_extension_accepted(self, client):
        r = client.post(
            "/api/v1/upload/fasta",
            files={"file": ("ref.fa", io.BytesIO(self._fasta_bytes()), "text/plain")},
        )
        assert r.status_code == 201

    def test_upload_wrong_extension_rejected(self, client):
        r = client.post(
            "/api/v1/upload/fasta",
            files={"file": ("ref.vcf", io.BytesIO(b"##fileformat=VCFv4.1"), "text/plain")},
        )
        assert r.status_code == 400


# ─── Pipeline start endpoint ──────────────────────────────────────────────────


class TestPipelineStart:
    def _make_real_files(self, tmp_path: Path) -> tuple:
        r1 = tmp_path / "r1.fastq"
        ref = tmp_path / "ref.fasta"
        r1.write_text("@r1\nACGT\n+\nIIII\n")
        ref.write_text(">chr1\nACGT\n")
        return str(r1), str(ref)

    def test_missing_r1_returns_400(self, client_with_tmp):
        client, tmp_path = client_with_tmp
        ref = tmp_path / "ref.fasta"
        ref.write_text(">chr1\nACGT\n")
        r = client.post(
            "/api/v1/pipeline/start",
            json={
                "fastq_r1_path": "/nonexistent/r1.fastq",
                "reference_fasta_path": str(ref),
            },
        )
        # Either 400 (path outside root) or 400 (file not found) — both correct
        assert r.status_code == 400

    def test_missing_ref_returns_400(self, client_with_tmp):
        client, tmp_path = client_with_tmp
        r1 = tmp_path / "r1.fastq"
        r1.write_text("@r1\nACGT\n+\nIIII\n")
        r = client.post(
            "/api/v1/pipeline/start",
            json={
                "fastq_r1_path": str(r1),
                "reference_fasta_path": "/nonexistent/ref.fasta",
            },
        )
        assert r.status_code == 400

    def test_invalid_sample_id_returns_422(self, client_with_tmp):
        client, tmp_path = client_with_tmp
        r1, ref = self._make_real_files(tmp_path)
        r = client.post(
            "/api/v1/pipeline/start",
            json={
                "fastq_r1_path": r1,
                "reference_fasta_path": ref,
                "sample_id": "invalid sample id with spaces!",
            },
        )
        assert r.status_code == 422

    def test_valid_request_returns_202(self, client_with_tmp):
        client, tmp_path = client_with_tmp
        r1, ref = self._make_real_files(tmp_path)
        r = client.post(
            "/api/v1/pipeline/start",
            json={
                "fastq_r1_path": r1,
                "reference_fasta_path": ref,
                "sample_id": "SAMPLE01",
            },
        )
        assert r.status_code == 202

    def test_valid_request_returns_run_id(self, client_with_tmp):
        client, tmp_path = client_with_tmp
        r1, ref = self._make_real_files(tmp_path)
        r = client.post(
            "/api/v1/pipeline/start",
            json={
                "fastq_r1_path": r1,
                "reference_fasta_path": ref,
            },
        )
        body = r.json()
        assert "run_id" in body
        assert len(body["run_id"]) == 32  # hex UUID

    def test_valid_request_returns_status_url(self, client_with_tmp):
        client, tmp_path = client_with_tmp
        r1, ref = self._make_real_files(tmp_path)
        r = client.post(
            "/api/v1/pipeline/start",
            json={
                "fastq_r1_path": r1,
                "reference_fasta_path": ref,
            },
        )
        body = r.json()
        assert "status_url" in body

    def test_config_overrides_accepted(self, client_with_tmp):
        client, tmp_path = client_with_tmp
        r1, ref = self._make_real_files(tmp_path)
        r = client.post(
            "/api/v1/pipeline/start",
            json={
                "fastq_r1_path": r1,
                "reference_fasta_path": ref,
                "config_overrides": {"qc": {"stop_on_failure": False}},
            },
        )
        assert r.status_code == 202


# ─── Status / progress endpoints ─────────────────────────────────────────────


class TestStatusAndProgress:
    def _queue_run(self, client_with_tmp) -> str:
        client, tmp_path = client_with_tmp
        r1 = tmp_path / "r1.fastq"
        ref = tmp_path / "ref.fasta"
        r1.write_text("@r1\nACGT\n+\nIIII\n")
        ref.write_text(">chr1\nACGT\n")
        r = client.post(
            "/api/v1/pipeline/start",
            json={
                "fastq_r1_path": str(r1),
                "reference_fasta_path": str(ref),
                "sample_id": "TESTRUN",
            },
        )
        return r.json()["run_id"], client

    def test_status_unknown_run_returns_404(self, client):
        r = client.get("/api/v1/pipeline/deadbeef/status")
        assert r.status_code == 404

    def test_progress_unknown_run_returns_404(self, client):
        r = client.get("/api/v1/pipeline/deadbeef/progress")
        assert r.status_code == 404

    def test_status_known_run_returns_200(self, client_with_tmp):
        run_id, client = self._queue_run(client_with_tmp)
        r = client.get(f"/api/v1/pipeline/{run_id}/status")
        assert r.status_code == 200

    def test_status_body_has_run_id(self, client_with_tmp):
        run_id, client = self._queue_run(client_with_tmp)
        r = client.get(f"/api/v1/pipeline/{run_id}/status")
        assert r.json()["run_id"] == run_id

    def test_status_body_has_status_field(self, client_with_tmp):
        run_id, client = self._queue_run(client_with_tmp)
        r = client.get(f"/api/v1/pipeline/{run_id}/status")
        assert r.json()["status"] in ("pending", "running", "completed", "failed")

    def test_progress_known_run_returns_200(self, client_with_tmp):
        run_id, client = self._queue_run(client_with_tmp)
        r = client.get(f"/api/v1/pipeline/{run_id}/progress")
        assert r.status_code == 200

    def test_progress_body_has_progress_pct(self, client_with_tmp):
        run_id, client = self._queue_run(client_with_tmp)
        r = client.get(f"/api/v1/pipeline/{run_id}/progress")
        assert "progress_pct" in r.json()


# ─── Report download endpoint ─────────────────────────────────────────────────


class TestReportDownload:
    def test_report_unknown_run_returns_404(self, client):
        r = client.get("/api/v1/pipeline/deadbeef/report")
        assert r.status_code == 404

    def test_report_pending_run_returns_409(self, client_with_tmp):
        client, tmp_path = client_with_tmp
        r1 = tmp_path / "r1.fastq"
        ref = tmp_path / "ref.fasta"
        r1.write_text("@r1\nACGT\n+\nIIII\n")
        ref.write_text(">chr1\nACGT\n")
        start_r = client.post(
            "/api/v1/pipeline/start",
            json={
                "fastq_r1_path": str(r1),
                "reference_fasta_path": str(ref),
            },
        )
        run_id = start_r.json()["run_id"]
        # Manually set status to pending so we can test 409
        _RUNS[run_id]["status"] = "pending"
        r = client.get(f"/api/v1/pipeline/{run_id}/report")
        assert r.status_code == 409

    def test_report_completed_run_serves_file(self, client, tmp_path):
        """Inject a fake completed run with a real HTML file."""
        html_file = tmp_path / "report.html"
        html_file.write_text("<html><body>Test Report</body></html>")
        run_id = "abc123"
        _RUNS[run_id] = {
            "run_id": run_id,
            "sample_id": "S01",
            "status": "completed",
            "stage": "reporting",
            "progress_pct": 100.0,
            "stages_completed": ["reporting"],
            "stages_failed": [],
            "started_at": None,
            "finished_at": None,
            "elapsed_seconds": 1.0,
            "error": None,
            "report_json_path": None,
            "report_html_path": str(html_file),
        }
        r = client.get(f"/api/v1/pipeline/{run_id}/report?format=html")
        assert r.status_code == 200
        assert b"Test Report" in r.content


# ─── Log endpoint ─────────────────────────────────────────────────────────────


class TestLogDownload:
    def test_log_unknown_run_returns_404(self, client):
        r = client.get("/api/v1/pipeline/deadbeef/log")
        assert r.status_code == 404

    def test_log_known_run_returns_200(self, client_with_tmp):
        client, tmp_path = client_with_tmp
        r1 = tmp_path / "r1.fastq"
        ref = tmp_path / "ref.fasta"
        r1.write_text("@r1\nACGT\n+\nIIII\n")
        ref.write_text(">chr1\nACGT\n")
        start_r = client.post(
            "/api/v1/pipeline/start",
            json={
                "fastq_r1_path": str(r1),
                "reference_fasta_path": str(ref),
            },
        )
        run_id = start_r.json()["run_id"]
        r = client.get(f"/api/v1/pipeline/{run_id}/log")
        assert r.status_code == 200
        assert "log" in r.json()


# ─── Delete endpoint ──────────────────────────────────────────────────────────


class TestDeleteRun:
    def test_delete_unknown_run_returns_404(self, client):
        r = client.delete("/api/v1/pipeline/deadbeef")
        assert r.status_code == 404

    def test_delete_known_run_returns_200(self, client_with_tmp):
        client, tmp_path = client_with_tmp
        r1 = tmp_path / "r1.fastq"
        ref = tmp_path / "ref.fasta"
        r1.write_text("@r1\nACGT\n+\nIIII\n")
        ref.write_text(">chr1\nACGT\n")
        start_r = client.post(
            "/api/v1/pipeline/start",
            json={
                "fastq_r1_path": str(r1),
                "reference_fasta_path": str(ref),
            },
        )
        run_id = start_r.json()["run_id"]
        r = client.delete(f"/api/v1/pipeline/{run_id}")
        assert r.status_code == 200

    def test_delete_removes_run_from_registry(self, client_with_tmp):
        client, tmp_path = client_with_tmp
        r1 = tmp_path / "r1.fastq"
        ref = tmp_path / "ref.fasta"
        r1.write_text("@r1\nACGT\n+\nIIII\n")
        ref.write_text(">chr1\nACGT\n")
        start_r = client.post(
            "/api/v1/pipeline/start",
            json={
                "fastq_r1_path": str(r1),
                "reference_fasta_path": str(ref),
            },
        )
        run_id = start_r.json()["run_id"]
        client.delete(f"/api/v1/pipeline/{run_id}")
        assert run_id not in _RUNS

    def test_delete_preserves_other_runs_sharing_sample_id(self, client_with_tmp):
        """Two runs sharing the same sample_id (e.g. both left at the
        "SAMPLE" default) share one work_dir. Deleting run_id_a must not
        destroy run_id_b's still-live artefacts or registry entry."""
        client, tmp_path = client_with_tmp

        sample_id = "SAMPLE"
        work_dir = tmp_path / sample_id
        (work_dir / "reporting").mkdir(parents=True)
        report_a = work_dir / "reporting" / "report_a.json"
        report_b = work_dir / "reporting" / "report_b.json"
        report_a.write_text('{"run": "a"}')
        report_b.write_text('{"run": "b"}')

        run_id_a, run_id_b = "run-a", "run-b"
        for rid, report_path in ((run_id_a, report_a), (run_id_b, report_b)):
            _RUNS[rid] = {
                "run_id": rid,
                "sample_id": sample_id,
                "status": "completed",
                "stage": "reporting",
                "progress_pct": 100.0,
                "stages_completed": ["reporting"],
                "stages_failed": [],
                "started_at": None,
                "finished_at": None,
                "elapsed_seconds": 1.0,
                "error": None,
                "report_json_path": str(report_path),
                "report_html_path": None,
            }

        r = client.delete(f"/api/v1/pipeline/{run_id_a}")
        assert r.status_code == 200

        # run_id_b's artefacts and shared work_dir must survive.
        assert work_dir.exists()
        assert report_b.exists()

        # run_id_b must still be queryable and its report still fetchable.
        status_r = client.get(f"/api/v1/pipeline/{run_id_b}/status")
        assert status_r.status_code == 200
        report_r = client.get(f"/api/v1/pipeline/{run_id_b}/report?format=json")
        assert report_r.status_code == 200


# ─── List runs endpoint ───────────────────────────────────────────────────────


class TestListRuns:
    def test_list_returns_200(self, client):
        r = client.get("/api/v1/pipeline")
        assert r.status_code == 200

    def test_list_empty_when_no_runs(self, client):
        r = client.get("/api/v1/pipeline")
        assert r.json()["total"] == 0
        assert r.json()["runs"] == []

    def test_list_shows_queued_run(self, client_with_tmp):
        client, tmp_path = client_with_tmp
        r1 = tmp_path / "r1.fastq"
        ref = tmp_path / "ref.fasta"
        r1.write_text("@r1\nACGT\n+\nIIII\n")
        ref.write_text(">chr1\nACGT\n")
        client.post(
            "/api/v1/pipeline/start",
            json={
                "fastq_r1_path": str(r1),
                "reference_fasta_path": str(ref),
            },
        )
        r = client.get("/api/v1/pipeline")
        assert r.json()["total"] == 1

    def test_list_run_has_expected_fields(self, client_with_tmp):
        client, tmp_path = client_with_tmp
        r1 = tmp_path / "r1.fastq"
        ref = tmp_path / "ref.fasta"
        r1.write_text("@r1\nACGT\n+\nIIII\n")
        ref.write_text(">chr1\nACGT\n")
        client.post(
            "/api/v1/pipeline/start",
            json={
                "fastq_r1_path": str(r1),
                "reference_fasta_path": str(ref),
                "sample_id": "LISTSAMPLE",
            },
        )
        r = client.get("/api/v1/pipeline")
        run = r.json()["runs"][0]
        assert "run_id" in run
        assert "status" in run
        assert "progress_pct" in run
        assert run["sample_id"] == "LISTSAMPLE"


# ─── OpenAPI docs sanity check ────────────────────────────────────────────────


class TestOpenAPI:
    def test_openapi_json_available(self, client):
        r = client.get("/openapi.json")
        assert r.status_code == 200

    def test_openapi_has_paths(self, client):
        schema = client.get("/openapi.json").json()
        assert "/health" in schema["paths"]
        assert "/api/v1/pipeline/start" in schema["paths"]

    def test_swagger_ui_available(self, client):
        r = client.get("/docs")
        assert r.status_code == 200


# ─── Generic exception handler: information disclosure ───────────────────────


class TestExceptionHandlerDoesNotLeakDetails:
    """The catch-all 500 handler must not leak exception text or request URLs.

    str(exc) can carry filesystem paths, model names, config values, or
    third-party API errors. request.url can carry internal deployment
    structure. Neither belongs in a response body sent to a client.
    """

    def test_500_does_not_leak_exception_detail_or_request_url(self, client, monkeypatch):
        leaked_path = "/var/secrets/geper/db_credentials.yaml"

        class _ExplodingConfig(dict):
            def items(self):
                raise RuntimeError(f"failed to read config file at {leaked_path}")

        import api.main as main_mod

        monkeypatch.setattr(main_mod, "_PIPELINE_CONFIG", _ExplodingConfig({"a": 1}))

        r = client.get("/api/v1/config")

        assert r.status_code == 500
        body = r.json()
        raw = json.dumps(body)

        assert body["error"] == "InternalServerError"
        assert "error_id" in body
        uuid.UUID(body["error_id"])  # must be a valid uuid4, raises ValueError otherwise

        assert leaked_path not in raw
        assert "/api/v1/config" not in raw
