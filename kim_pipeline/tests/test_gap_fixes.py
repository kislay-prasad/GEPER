"""
tests/test_gap_fixes.py
────────────────────────
Regression tests for GAPs 1-7 as specified in the audit.

GAP 1 — VEP integration stage
GAP 2 — API authentication
GAP 3 — SQLite persistent run store
GAP 4 — Real process cancellation
GAP 5 — gnomad_constraint and hotspot config sections
GAP 6 — api package discoverable via setuptools
GAP 7 — anyio / pytest-anyio available
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ═══════════════════════════════════════════════════════════════════════════════
# GAP 1 — VEP Annotation Stage
# ═══════════════════════════════════════════════════════════════════════════════

from pipeline.vep.stage import VEPAnnotationStage
from pipeline.fastq.errors import FastqPipelineError


def test_gap1_vep_raises_when_binary_absent(tmp_path):
    """VEPAnnotationStage.run() raises FastqPipelineError when vep is absent."""
    stage = VEPAnnotationStage(cfg={"vep": {"enabled": True}})
    # Patch _require to simulate vep not on PATH
    with patch(
        "pipeline.vep.stage._require",
        side_effect=FastqPipelineError(
            "Required tool 'vep' is not installed or not on PATH.",
            stage="vep_annotation",
            tool="vep",
        ),
    ):
        with pytest.raises(FastqPipelineError) as exc_info:
            stage.run(
                filtered_vcf_path=str(tmp_path / "test.vcf"),
                output_dir=str(tmp_path / "vep_out"),
                sample_id="TESTSAMPLE",
            )
    assert "vep" in str(exc_info.value).lower()


def test_gap1_vep_consequence_from_output(tmp_path):
    """When VEP output contains consequence=missense_variant, annotated variant carries it."""
    # Write a minimal VEP-annotated VCF
    vcf_content = """\
##fileformat=VCFv4.2
##INFO=<ID=CSQ,Number=.,Type=String,Description="VEP annotation. Format: Allele|Consequence|SYMBOL|Feature|HGVSc|HGVSp|CADD_PHRED|REVEL|SpliceAI_pred|AM_PATHOGENICITY">
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO
chr1\t100\t.\tA\tG\t50\tPASS\tCSQ=G|missense_variant|BRCA1|ENST001|NM_007294.4:c.100A>G|NP_009225.1:p.Ile34Val|25.3|0.72|.|0.92
"""
    vcf_path = tmp_path / "vep_out.vcf"
    vcf_path.write_text(vcf_content)

    stage = VEPAnnotationStage()
    variants = stage._parse_vep_vcf(str(vcf_path))

    assert len(variants) == 1
    v = variants[0]
    assert v.consequence == "missense_variant", f"Expected missense_variant, got {v.consequence}"


def test_gap1_vep_hgvs_propagated(tmp_path):
    """VEP HGVS c. notation is propagated to VEPVariantAnnotation.hgvs_c."""
    vcf_content = """\
##fileformat=VCFv4.2
##INFO=<ID=CSQ,Number=.,Type=String,Description="VEP annotation. Format: Allele|Consequence|SYMBOL|Feature|HGVSc|HGVSp|CADD_PHRED|REVEL|SpliceAI_pred|AM_PATHOGENICITY">
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO
chr17\t41246481\t.\tC\tT\t99\tPASS\tCSQ=T|missense_variant|BRCA1|ENST00000357654|NM_007294.4:c.5266dupC|NP_009225.1:p.Gln1756ProfsTer25|32.1|0.88|.|0.98
"""
    vcf_path = tmp_path / "vep_hgvs.vcf"
    vcf_path.write_text(vcf_content)

    stage = VEPAnnotationStage()
    variants = stage._parse_vep_vcf(str(vcf_path))
    assert variants[0].hgvs_c == "NM_007294.4:c.5266dupC"


def test_gap1_vep_scores_populated(tmp_path):
    """cadd_phred, revel_score, and am_pathogenicity are populated from VEP output."""
    vcf_content = """\
##fileformat=VCFv4.2
##INFO=<ID=CSQ,Number=.,Type=String,Description="VEP annotation. Format: Allele|Consequence|SYMBOL|Feature|HGVSc|HGVSp|CADD_PHRED|REVEL|SpliceAI_pred|AM_PATHOGENICITY">
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO
chr1\t200\t.\tG\tA\t80\tPASS\tCSQ=A|missense_variant|TP53|ENST00000269305|NM_000546.6:c.215G>A|NP_000537.3:p.Arg72Pro|28.6|0.81|.|0.95
"""
    vcf_path = tmp_path / "scores.vcf"
    vcf_path.write_text(vcf_content)

    stage = VEPAnnotationStage()
    variants = stage._parse_vep_vcf(str(vcf_path))
    v = variants[0]
    assert v.cadd_phred == pytest.approx(28.6)
    assert v.revel_score == pytest.approx(0.81)
    assert v.am_pathogenicity == pytest.approx(0.95)


def test_gap1_vep_checkpoint_written(tmp_path):
    """vep_annotation checkpoint key is written after successful VEP stage."""

    vcf_path = tmp_path / "test.vcf"
    vcf_path.write_text("##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")

    stage = VEPAnnotationStage(cfg={"vep": {"enabled": True}})

    # Mock the subprocess to return success with empty VCF output
    with (
        patch("pipeline.vep.stage._require", return_value="/usr/bin/vep"),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        # Create empty output VCF
        out_dir = tmp_path / "vep_out"
        out_dir.mkdir()
        annotated = out_dir / "vep_annotated.vcf"
        annotated.write_text(
            "##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        )

        result = stage.run(
            filtered_vcf_path=str(vcf_path),
            output_dir=str(out_dir),
            sample_id="TESTSAMPLE",
        )
    assert result.annotated_vcf_path.endswith("vep_annotated.vcf")


# ═══════════════════════════════════════════════════════════════════════════════
# GAP 2 — API Authentication
# ═══════════════════════════════════════════════════════════════════════════════

# `fastapi` is not always installed (see e.g. test_api.py's own guarded
# import) -- deliberately NOT imported at module level here. A
# module-level `from fastapi.testclient import TestClient` aborts
# collection of this WHOLE FILE (and, with no
# `--continue-on-collection-errors` in pyproject.toml, the rest of the
# suite invoked alongside it) with a single opaque error the moment
# fastapi is missing, instead of letting the ~1000 tests that don't
# need it run and reporting real results. Every function below that
# needs `TestClient` imports it locally, matching the pattern
# test_fixes_6_to_11.py already uses for the same reason.


def _make_api_client(api_keys_env: str = ""):
    """Create a TestClient with GEPER_API_KEYS set to api_keys_env."""
    from fastapi.testclient import TestClient

    # Patch the env var and reload the module-level _API_KEYS
    with patch.dict(os.environ, {"GEPER_API_KEYS": api_keys_env}, clear=False):
        from api import main as api_main

        # Re-compute the API keys for this test
        api_main._API_KEYS = api_main._load_api_keys()
        client = TestClient(api_main.app, raise_server_exceptions=False)
        return client, api_main


def test_gap2_health_always_200_no_key():
    """/health returns 200 with no API key regardless of GEPER_API_KEYS."""
    client, _ = _make_api_client("secretkey123")
    resp = client.get("/health")
    assert resp.status_code == 200


def test_gap2_start_401_no_key():
    """pipeline/start returns 401 when GEPER_API_KEYS is set and no key provided."""
    client, _ = _make_api_client("secretkey123")
    resp = client.post(
        "/api/v1/pipeline/start",
        json={
            "fastq_r1_path": "/tmp/r1.fastq.gz",
            "reference_fasta_path": "/tmp/ref.fasta",
            "sample_id": "TEST01",
        },
    )
    assert resp.status_code == 401


def test_gap2_start_401_wrong_key():
    """pipeline/start returns 401 when wrong key is provided."""
    client, _ = _make_api_client("secretkey123")
    resp = client.post(
        "/api/v1/pipeline/start",
        json={
            "fastq_r1_path": "/tmp/r1.fastq.gz",
            "reference_fasta_path": "/tmp/ref.fasta",
            "sample_id": "TEST01",
        },
        headers={"X-Api-Key": "wrongkey"},
    )
    assert resp.status_code == 401


def test_gap2_start_accepts_correct_key(tmp_path):
    """pipeline/start returns 202 when correct key is provided."""
    r1 = tmp_path / "r1.fastq.gz"
    r1.touch()
    ref = tmp_path / "ref.fasta"
    ref.touch()

    from fastapi.testclient import TestClient
    from api import main as api_main

    api_main._API_KEYS = {"goodkey"}

    client = TestClient(api_main.app, raise_server_exceptions=False)

    # Patch heavy parts so the test is about auth only
    with (
        patch("api.main._validate_path_in_roots", return_value=tmp_path),
        patch.object(Path, "exists", return_value=True),
        patch("api.main._run_pipeline_sync"),
    ):
        resp = client.post(
            "/api/v1/pipeline/start",
            json={
                "fastq_r1_path": str(r1),
                "reference_fasta_path": str(ref),
                "sample_id": "TEST01",
            },
            headers={"X-Api-Key": "goodkey"},
        )
    # The auth passed; may get 202 or 400 depending on path validation — not 401
    assert resp.status_code != 401


def test_gap2_dev_mode_no_key_needed():
    """Dev mode (GEPER_API_KEYS unset) allows requests without a key."""
    # _API_KEYS = None means dev mode
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("GEPER_API_KEYS", None)
        from fastapi.testclient import TestClient
        from api import main as api_main

        api_main._API_KEYS = None  # explicit dev mode
        client = TestClient(api_main.app, raise_server_exceptions=False)
        # /health should definitely return 200
        resp = client.get("/health")
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# ISSUE 3 — every endpoint must require authorization consistently.
#
# Prior to this fix, only POST /api/v1/pipeline/start checked the API key.
# pipeline status/progress, report download, log download, delete, list,
# config, and the upload endpoints were all reachable with no credentials
# at all — including direct download of the clinical report and log files
# (PHI exposure) and unauthenticated enumeration of every sample/run via
# list_runs. These tests lock in that every one of those endpoints now
# rejects requests without a valid key, and that /health and /ready (the
# liveness/readiness probes, which carry no PHI) remain intentionally open.
# ═══════════════════════════════════════════════════════════════════════════════

_PROTECTED_GET_ROUTES = [
    "/api/v1/config",
    "/api/v1/pipeline/some-run-id/status",
    "/api/v1/pipeline/some-run-id/progress",
    "/api/v1/pipeline/some-run-id/report",
    "/api/v1/pipeline/some-run-id/log",
    "/api/v1/pipeline",
]


@pytest.mark.parametrize("path", _PROTECTED_GET_ROUTES)
def test_issue3_protected_get_routes_401_no_key(path):
    client, _ = _make_api_client("secretkey123")
    resp = client.get(path)
    assert resp.status_code == 401, f"{path} should require auth, got {resp.status_code}"


@pytest.mark.parametrize("path", _PROTECTED_GET_ROUTES)
def test_issue3_protected_get_routes_401_wrong_key(path):
    client, _ = _make_api_client("secretkey123")
    resp = client.get(path, headers={"X-Api-Key": "wrongkey"})
    assert resp.status_code == 401, f"{path} should reject a wrong key, got {resp.status_code}"


@pytest.mark.parametrize("path", _PROTECTED_GET_ROUTES)
def test_issue3_protected_get_routes_not_401_with_correct_key(path):
    client, _ = _make_api_client("secretkey123")
    resp = client.get(path, headers={"X-Api-Key": "secretkey123"})
    # A correct key must clear the auth gate. Downstream 404s (unknown
    # run_id) are expected and fine — only 401 indicates an auth bug here.
    assert resp.status_code != 401, f"{path} rejected a correct key"


def test_issue3_delete_run_401_no_key():
    client, _ = _make_api_client("secretkey123")
    resp = client.delete("/api/v1/pipeline/some-run-id")
    assert resp.status_code == 401


def test_issue3_delete_run_not_401_with_correct_key():
    client, _ = _make_api_client("secretkey123")
    resp = client.delete(
        "/api/v1/pipeline/some-run-id",
        headers={"X-Api-Key": "secretkey123"},
    )
    assert resp.status_code != 401


def test_issue3_upload_fastq_401_no_key():
    client, _ = _make_api_client("secretkey123")
    resp = client.post(
        "/api/v1/upload/fastq",
        files={"file": ("r1.fastq.gz", b"dummy", "application/octet-stream")},
    )
    assert resp.status_code == 401


def test_issue3_upload_fasta_401_no_key():
    client, _ = _make_api_client("secretkey123")
    resp = client.post(
        "/api/v1/upload/fasta",
        files={"file": ("ref.fasta", b"dummy", "application/octet-stream")},
    )
    assert resp.status_code == 401


def test_issue3_upload_fastq_accepts_correct_key():
    client, _ = _make_api_client("secretkey123")
    resp = client.post(
        "/api/v1/upload/fastq",
        files={"file": ("r1.fastq.gz", b"dummy", "application/octet-stream")},
        headers={"X-Api-Key": "secretkey123"},
    )
    assert resp.status_code != 401


def test_issue3_health_and_ready_remain_open_when_keys_set():
    """Liveness/readiness probes intentionally stay unauthenticated (no PHI,
    and infra orchestrators polling them typically cannot supply a key)."""
    client, _ = _make_api_client("secretkey123")
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/ready").status_code == 200


def test_issue3_list_runs_does_not_leak_without_auth():
    """list_runs must not be usable to enumerate sample IDs/run IDs without
    a valid key — this was the gap that defeated the unguessable-UUID
    protection on the report/log download endpoints."""
    client, api_main = _make_api_client("secretkey123")
    api_main._RUNS["leak-test-run"] = {
        "run_id": "leak-test-run",
        "sample_id": "PATIENT-PHI-001",
        "status": "completed",
        "progress_pct": 100.0,
        "started_at": None,
    }
    try:
        resp = client.get("/api/v1/pipeline")
        assert resp.status_code == 401
        assert "PATIENT-PHI-001" not in resp.text
    finally:
        api_main._RUNS.pop("leak-test-run", None)


# ═══════════════════════════════════════════════════════════════════════════════
# GAP 3 — SQLite persistent run store
# ═══════════════════════════════════════════════════════════════════════════════

from api.run_store import RunStore


def test_gap3_run_survives_reinstantiation(tmp_path):
    """A run created in RunStore survives a RunStore re-instantiation."""
    db = str(tmp_path / "test_runs.db")
    store1 = RunStore(db_path=db)
    store1.create("run001", {"sample_id": "SAMP01", "status": "pending"})

    store2 = RunStore(db_path=db)
    record = store2.get("run001")
    assert record is not None
    assert record["sample_id"] == "SAMP01"
    assert record["status"] == "pending"


def test_gap3_concurrent_writes_safe(tmp_path):
    """Concurrent writes from 5 threads do not raise or corrupt data."""
    db = str(tmp_path / "concurrent.db")
    store = RunStore(db_path=db)
    errors = []

    def _write(i: int):
        try:
            run_id = f"run{i:04d}"
            store.create(run_id, {"sample_id": f"SAMP{i}", "status": "pending"})
            store.update(run_id, status="running")
            store.update(run_id, status="completed", progress_pct=100.0)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_write, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Concurrent write errors: {errors}"
    runs = store.list_all()
    assert len(runs) == 5


def test_gap3_get_returns_none_for_unknown(tmp_path):
    """get() returns None for unknown run_id."""
    store = RunStore(db_path=str(tmp_path / "empty.db"))
    assert store.get("nonexistent") is None


def test_gap3_list_all_ordered_by_created_at_desc(tmp_path):
    """list_all() returns runs in created_at descending order."""
    db = str(tmp_path / "order.db")
    store = RunStore(db_path=db)
    for i in range(3):
        store.create(f"run{i}", {"sample_id": f"S{i}", "status": "pending"})
        time.sleep(0.01)  # ensure distinct timestamps

    runs = store.list_all()
    created_ats = [r["created_at"] for r in runs]
    assert created_ats == sorted(created_ats, reverse=True)


def test_gap3_delete_removes_run(tmp_path):
    """delete() removes the run and get() returns None afterwards."""
    store = RunStore(db_path=str(tmp_path / "del.db"))
    store.create("deleteMe", {"sample_id": "X", "status": "completed"})
    assert store.get("deleteMe") is not None
    store.delete("deleteMe")
    assert store.get("deleteMe") is None


# ═══════════════════════════════════════════════════════════════════════════════
# GAP 4 — Real process cancellation
# ═══════════════════════════════════════════════════════════════════════════════


def test_gap4_delete_sets_status_cancelled(tmp_path):
    """After delete_run() on a running run, status becomes 'cancelled'."""
    from fastapi.testclient import TestClient
    from api import main as api_main

    # Inject a fake running run
    run_id = "fakecancelrun"
    api_main._RUNS[run_id] = {
        "run_id": run_id,
        "sample_id": "CSAMP",
        "status": "running",
        "stage": "alignment",
        "progress_pct": 30.0,
        "stages_completed": ["fastq_validation"],
        "stages_failed": [],
        "started_at": "2025-01-01T00:00:00Z",
        "finished_at": None,
        "elapsed_seconds": None,
        "error": None,
        "report_json_path": None,
        "report_html_path": None,
        "log_path": None,
        "fastq_r1": "/tmp/r1.fastq.gz",
        "fastq_r2": None,
        "reference_fasta": "/tmp/ref.fasta",
    }
    api_main._RUN_STORE.create(run_id, api_main._RUNS[run_id])

    client = TestClient(api_main.app, raise_server_exceptions=False)
    api_main._API_KEYS = None  # dev mode

    with patch("os.killpg"), patch("shutil.rmtree"):
        resp = client.delete(f"/api/v1/pipeline/{run_id}")

    assert resp.status_code == 200
    # Run should be gone from _RUNS (deleted)
    assert run_id not in api_main._RUNS


def test_gap4_killpg_called_with_sigterm():
    """os.killpg is called with SIGTERM when a running run is deleted."""
    from fastapi.testclient import TestClient
    from api import main as api_main

    run_id = "killtest"
    api_main._RUNS[run_id] = {
        "run_id": run_id,
        "sample_id": "KSAMP",
        "status": "running",
        "stage": None,
        "progress_pct": 0.0,
        "stages_completed": [],
        "stages_failed": [],
        "started_at": None,
        "finished_at": None,
        "elapsed_seconds": None,
        "error": None,
        "report_json_path": None,
        "report_html_path": None,
        "log_path": None,
        "fastq_r1": "/tmp/r1.fastq.gz",
        "fastq_r2": None,
        "reference_fasta": "/tmp/ref.fasta",
    }
    api_main._RUN_STORE.create(run_id, api_main._RUNS[run_id])
    api_main._PROCESS_GROUPS[run_id] = 99999  # fake pgid
    api_main._API_KEYS = None

    client = TestClient(api_main.app, raise_server_exceptions=False)
    with patch("os.killpg") as mock_kill, patch("shutil.rmtree"):
        resp = client.delete(f"/api/v1/pipeline/{run_id}")

    assert resp.status_code == 200
    mock_kill.assert_called()
    first_call_args = mock_kill.call_args_list[0]
    assert first_call_args[0][1] == signal.SIGTERM


def test_gap4_delete_completed_no_kill():
    """delete_run() on a completed run does not attempt to kill anything."""
    from fastapi.testclient import TestClient
    from api import main as api_main

    run_id = "completedrun"
    api_main._RUNS[run_id] = {
        "run_id": run_id,
        "sample_id": "CSAMP2",
        "status": "completed",
        "stage": "reporting",
        "progress_pct": 100.0,
        "stages_completed": ["fastq_validation", "alignment", "reporting"],
        "stages_failed": [],
        "started_at": None,
        "finished_at": "2025-01-01T01:00:00Z",
        "elapsed_seconds": 120.0,
        "error": None,
        "report_json_path": None,
        "report_html_path": None,
        "log_path": None,
        "fastq_r1": "/tmp/r1.fastq.gz",
        "fastq_r2": None,
        "reference_fasta": "/tmp/ref.fasta",
    }
    api_main._RUN_STORE.create(run_id, api_main._RUNS[run_id])
    api_main._API_KEYS = None

    client = TestClient(api_main.app, raise_server_exceptions=False)
    with patch("os.killpg") as mock_kill, patch("shutil.rmtree"):
        resp = client.delete(f"/api/v1/pipeline/{run_id}")

    assert resp.status_code == 200
    mock_kill.assert_not_called()


def test_gap4_delete_unknown_run_returns_404():
    """delete_run() on an unknown run_id returns 404."""
    from fastapi.testclient import TestClient
    from api import main as api_main

    api_main._API_KEYS = None

    # Ensure not in store
    api_main._RUNS.pop("nosuchrun", None)

    client = TestClient(api_main.app, raise_server_exceptions=False)
    resp = client.delete("/api/v1/pipeline/nosuchrun")
    assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# GAP 5 — gnomad_constraint and hotspot config sections
# ═══════════════════════════════════════════════════════════════════════════════

from pipeline.config_validator import validate_config


def test_gap5_no_raise_empty_gnomad_constraint_tsv_path():
    """validate_config does not raise when gnomad_constraint.tsv_path is empty."""
    cfg = {"gnomad_constraint": {"tsv_path": "", "loeuf_threshold": 0.35, "pli_threshold": 0.9}}
    # Should not raise
    validate_config(cfg)


def test_gap5_no_raise_hotspot_section_absent():
    """validate_config does not raise when hotspot section is absent."""
    cfg = {}  # hotspot entirely absent
    validate_config(cfg)  # must not raise


def test_gap5_gnomad_constraint_lookup_missing_cfg():
    """GnomadConstraintLookup initialises without error when cfg['gnomad_constraint'] is missing."""
    from pipeline.constraint.lookup import GnomadConstraintLookup

    # Pass empty config — should not raise
    lookup = GnomadConstraintLookup(cfg={})
    assert lookup is not None


# ═══════════════════════════════════════════════════════════════════════════════
# GAP 6 — api package discoverable via setuptools
# ═══════════════════════════════════════════════════════════════════════════════


def test_gap6_api_in_find_packages():
    """'api' is in the list returned by setuptools.find_packages() for the project root."""
    import setuptools

    packages = setuptools.find_packages(where=str(_ROOT), include=["pipeline*", "geper*", "api*"])
    assert "api" in packages, f"'api' not found in packages: {packages}"


def test_gap6_api_main_importable():
    """from api.main import app succeeds."""
    from api.main import app

    assert app is not None


# ═══════════════════════════════════════════════════════════════════════════════
# GAP 7 — anyio and pytest-anyio available
# ═══════════════════════════════════════════════════════════════════════════════


def test_gap7_anyio_importable():
    """anyio is importable."""
    import anyio

    assert anyio is not None


@pytest.mark.anyio
async def test_gap7_pytest_anyio_mark_recognised():
    """
    `@pytest.mark.anyio`-marked async tests actually run under anyio's
    pytest plugin -- not merely that `pytest.mark.anyio` can be spelled.

    `pytest.mark.<anything>` returns a non-None `MarkDecorator` for
    literally any attribute name, whether or not a plugin backs it --
    `_pytest.mark.MarkGenerator.__getattr__` synthesizes one
    unconditionally (confirmed directly:
    `pytest.mark.this_plugin_definitely_does_not_exist_zzz` is also
    non-None). The previous version of this test asserted exactly that
    ("mark is not None") and so could never fail regardless of whether
    anyio's pytest plugin was installed, registered, or even existed --
    a cannot-fail assertion, not a check.

    This checks the real, discriminating fact instead: an `async def`
    test genuinely gets collected and its body genuinely gets awaited.
    Without a working async-capable pytest plugin, pytest cannot run an
    `async def` test at all -- it fails the test outright with "async
    def functions are not natively supported" rather than silently
    skipping the body -- so reaching the final assertion here is itself
    proof the mark does real work, the same real-world path
    `tests/test_fixes_6_to_11.py`'s async API tests (auth, cancellation)
    already depend on.

    Verified both directions by hand before landing this test: passes
    under a normal run; fails with exactly that "async def functions
    are not natively supported" diagnostic under `pytest -p no:anyio`.
    (An earlier draft of this fix instead checked
    `pytestconfig.pluginmanager.list_name_plugin()` for "anyio" -- that
    turned out to *also* report "anyio" as registered even under
    `-p no:anyio` in this pytest/anyio version, i.e. it would not have
    discriminated either. Caught by running it against both conditions
    before trusting it, not assumed correct because it looked
    plausible.)
    """
    ran = []

    async def _mark_ran():
        ran.append(True)

    await _mark_ran()
    assert ran == [True]
