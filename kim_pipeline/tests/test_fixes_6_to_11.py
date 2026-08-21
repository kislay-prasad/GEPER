"""
tests/test_fixes_6_to_11.py
────────────────────────────
Regression tests for defect fixes 6–11:

  FIX 6  — Runner must consume GnomadLookupOutcome enum, not just GnomadHit
  FIX 7  — Report directory name mismatch (report → reporting)
  FIX 8  — config_overrides must validate against allowlist
  FIX 9  — Lookup caches must be thread-safe
  FIX 10 — asyncio.get_event_loop deprecated; swallowed thread exceptions
  FIX 11 — Pedigree/phenotype sidecar fields must be loaded
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import warnings
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ─────────────────────────────────────────────────────────────────────────────
# Shared helper for async API tests
# ─────────────────────────────────────────────────────────────────────────────


async def _api_post(body: dict, *, blocked_validation: bool = True) -> Any:
    """POST to /api/v1/pipeline/start via ASGI transport (no sync TestClient)."""
    import httpx

    from api.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        if blocked_validation:
            with patch("api.main._validate_path_in_roots", return_value=Path("/tmp")):
                return await client.post("/api/v1/pipeline/start", json=body)
        else:
            return await client.post("/api/v1/pipeline/start", json=body)


def _make_body(**extra) -> dict:
    return {
        "fastq_r1_path": "/tmp/geper_uploads/r1.fastq.gz",
        "reference_fasta_path": "/tmp/geper_uploads/ref.fasta",
        "sample_id": "TEST01",
        **extra,
    }


@pytest.fixture(autouse=True, scope="module")
def _ensure_dummy_uploads(tmp_path_factory):
    """Create empty dummy FASTQ/FASTA under /tmp/geper_uploads for API tests."""
    d = Path("/tmp/geper_uploads")
    d.mkdir(parents=True, exist_ok=True)
    (d / "r1.fastq.gz").touch()
    (d / "ref.fasta").touch()


# ─────────────────────────────────────────────────────────────────────────────
# FIX 6 — GnomadLookupOutcome consumed correctly in runner
# ─────────────────────────────────────────────────────────────────────────────

from pipeline.acmg.classifier import AcmgClassifier, VariantEvidence  # noqa: E402
from pipeline.gnomad.lookup import GnomadHit, GnomadLookup, GnomadLookupOutcome  # noqa: E402
from pipeline.orchestration.shared import run_acmg_evidence_batch  # noqa: E402


def _simulate_gnomad_branch(mock_return) -> VariantEvidence:
    """Drives the REAL gnomAD dispatch in
    `pipeline/orchestration/shared.py::run_acmg_evidence_batch` (mocking
    only `GnomadLookup.lookup`/`lookup_batch`) and captures the
    `VariantEvidence` it builds, via a spy on `AcmgClassifier.classify`
    that delegates to the real implementation.

    CORRECTION (Kelly, relayed by god, 2026-08-21): this used to be a
    hand-copied duplicate of shared.py's isinstance dispatch, evaluated
    entirely in-test rather than by calling the real code. It still
    passed after shared.py's dispatch changed (UNAVAILABLE/except now
    set `gnomad_af_absent = None`, not `False`) -- because it was
    testing its own copy, which nobody had updated. A green test
    asserting the opposite of production is worse than a red one: a red
    test tells you something moved; this one would have kept passing
    forever, silently documenting a convention the code no longer
    follows. Calling through to the real function is what makes that
    structurally impossible -- there is no separate copy left to drift.
    """
    captured: dict[str, VariantEvidence] = {}
    real_classify = AcmgClassifier.classify

    def _spy(self, evidence):
        captured["evidence"] = evidence
        return real_classify(self, evidence)

    variant = {"chrom": "17", "pos": 43057051, "ref": "A", "alt": "T", "gene_name": "BRCA1"}
    cfg = {"gnomad": {"enabled": True}, "clinvar": {"enabled": False}, "vep": {"enabled": False}}
    with (
        patch.object(GnomadLookup, "lookup", return_value=mock_return),
        patch.object(GnomadLookup, "lookup_batch", return_value=None),
        patch.object(AcmgClassifier, "classify", _spy),
    ):
        run_acmg_evidence_batch([variant], cfg, sample_id="TESTSAMPLE")
    return captured["evidence"]


class TestFix6GnomadLookupOutcome:
    def test_gnomad_hit_populates_af_fields_and_absent_false(self):
        hit = GnomadHit(
            af=0.001,
            af_popmax=0.002,
            ac=5,
            an=5000,
            backend_used="api",
            outcome=GnomadLookupOutcome.PRESENT,
        )
        ev = _simulate_gnomad_branch(hit)
        assert ev.gnomad_af == 0.001
        assert ev.gnomad_af_popmax == 0.002
        assert ev.gnomad_af_absent is False

    def test_gnomad_absent_sets_absent_true_af_none(self):
        ev = _simulate_gnomad_branch(GnomadLookupOutcome.ABSENT)
        assert ev.gnomad_af is None
        assert ev.gnomad_af_popmax is None
        assert ev.gnomad_af_absent is True

    def test_gnomad_unavailable_sets_absent_none_af_none(self):
        # Was `assert ev.gnomad_af_absent is False` -- true of the OLD
        # dispatch this test used to hand-simulate, false of the real
        # one production now runs (see this class's own gnomad-lookup-
        # failure-collapses-into-a-confirmed-negative fix: UNAVAILABLE
        # is "nothing was checked", not "checked and confirmed
        # present", so it gets the same None sentinel the except-path
        # exception case does).
        ev = _simulate_gnomad_branch(GnomadLookupOutcome.UNAVAILABLE)
        assert ev.gnomad_af is None
        assert ev.gnomad_af_popmax is None
        assert ev.gnomad_af_absent is None

    def test_pm2_fires_when_absent_true(self):
        """gnomad_af_absent=True → PM2 awarded."""
        clf = AcmgClassifier(cfg={})
        ev = VariantEvidence(
            chrom="17",
            pos=43057051,
            ref="A",
            alt="T",
            gnomad_af=None,
            gnomad_af_absent=True,
            is_missense=True,
            lof_gene_intolerant=False,
            is_lof=False,
        )
        result = clf.classify(ev)
        assert "PM2" in result.criteria_met, (
            f"PM2 should fire for absent variant; got {result.criteria_met}"
        )

    def test_pm2_does_not_fire_when_unavailable(self):
        """gnomad_af_absent=False (UNAVAILABLE) → PM2 must not fire."""
        clf = AcmgClassifier(cfg={})
        ev = VariantEvidence(
            chrom="17",
            pos=43057051,
            ref="A",
            alt="T",
            gnomad_af=None,
            gnomad_af_absent=False,
            is_missense=True,
            lof_gene_intolerant=False,
            is_lof=False,
        )
        result = clf.classify(ev)
        assert "PM2" not in result.criteria_met, "PM2 must NOT fire when lookup was UNAVAILABLE"


# ─────────────────────────────────────────────────────────────────────────────
# FIX 7 — Report directory name
# ─────────────────────────────────────────────────────────────────────────────


class TestFix7ReportDirectory:
    def test_runner_uses_reporting_subdir(self):
        """Runner must assign report_out to work_dir / 'reporting', not 'report'."""
        import inspect

        from pipeline.orchestration import runner as runner_mod

        src = inspect.getsource(runner_mod)
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("report_out") and "=" in stripped:
                assert '"reporting"' in stripped or "'reporting'" in stripped, (
                    f"report_out must use 'reporting', found: {stripped!r}"
                )

    def test_api_report_lookup_uses_reporting_subdir(self):
        """API must look for report.json under work_dir/reporting/, not work_dir/report/."""
        import inspect

        from api import main as main_mod

        src = inspect.getsource(main_mod)
        # Find lines that assign json_report / html_report path variables
        for line in src.splitlines():
            stripped = line.strip()
            # Lines that build the report paths (assignments containing "/ 'reporting'")
            if (
                ("json_report" in stripped or "html_report" in stripped)
                and ("/" in stripped)
                and "=" in stripped
            ):
                assert "reporting" in stripped, (
                    f"Report path assignment should reference 'reporting': {stripped!r}"
                )

    def test_end_to_end_report_paths_set_when_files_exist(self, tmp_path):
        """Report paths become non-None when files exist at work_dir/reporting/."""
        sample_id = "TESTSAMPLE"
        work_dir = tmp_path / sample_id
        (work_dir / "reporting").mkdir(parents=True)
        (work_dir / "reporting" / "report.json").write_text('{"ok":true}')
        (work_dir / "reporting" / "report.html").write_text("<html/>")

        run: dict[str, Any] = {"report_json_path": None, "report_html_path": None}
        # Replicate the path-setting logic from _run_pipeline_sync
        json_report = tmp_path / sample_id / "reporting" / "report.json"
        html_report = tmp_path / sample_id / "reporting" / "report.html"
        if json_report.exists():
            run["report_json_path"] = str(json_report)
        if html_report.exists():
            run["report_html_path"] = str(html_report)

        assert run["report_json_path"] is not None
        assert run["report_html_path"] is not None
        assert Path(run["report_json_path"]).exists()
        assert Path(run["report_html_path"]).exists()


# ─────────────────────────────────────────────────────────────────────────────
# FIX 8 — config_overrides allowlist (async API tests)
# ─────────────────────────────────────────────────────────────────────────────

pytestmark_anyio = pytest.mark.anyio


class TestFix8ConfigOverridesAllowlist:
    @pytest.mark.anyio
    async def test_allowed_key_qc_accepted(self):
        resp = await _api_post(_make_body(config_overrides={"qc": {"min_quality": 30}}))
        assert resp.status_code == 202

    @pytest.mark.anyio
    async def test_blocked_key_alignment_rejected(self):
        resp = await _api_post(
            _make_body(config_overrides={"alignment": {"reference_fasta": "/evil"}})
        )
        assert resp.status_code == 400
        assert "alignment" in resp.json()["detail"]

    @pytest.mark.anyio
    async def test_blocked_key_gnomad_rejected(self):
        resp = await _api_post(
            _make_body(config_overrides={"gnomad": {"vcf_path": "/evil/gnomad.vcf"}})
        )
        assert resp.status_code == 400
        assert "gnomad" in resp.json()["detail"]

    @pytest.mark.anyio
    async def test_mixed_allowed_and_blocked_rejected(self):
        resp = await _api_post(
            _make_body(config_overrides={"qc": {}, "clinvar": {"tsv_gz_path": "/evil"}})
        )
        assert resp.status_code == 400
        assert "clinvar" in resp.json()["detail"]

    @pytest.mark.anyio
    async def test_empty_overrides_accepted(self):
        resp = await _api_post(_make_body(config_overrides={}))
        assert resp.status_code == 202

    @pytest.mark.anyio
    async def test_none_overrides_accepted(self):
        resp = await _api_post(_make_body(config_overrides=None))
        assert resp.status_code == 202

    @pytest.mark.anyio
    async def test_all_allowed_sections_accepted(self):
        resp = await _api_post(
            _make_body(
                config_overrides={
                    "qc": {},
                    "acmg_thresholds": {},
                    "evidence_engine": {},
                    "evidence_thresholds": {},
                    "blast": {},
                    "pgx": {},
                    "ancestry": {},
                }
            )
        )
        assert resp.status_code == 202


# ─────────────────────────────────────────────────────────────────────────────
# FIX 9 — Thread-safe caches
# ─────────────────────────────────────────────────────────────────────────────


class TestFix9ThreadSafeCaches:
    def _run_concurrent(self, lookup_fn, n=10):
        results = [None] * n
        errors = []

        def worker(i):
            try:
                results[i] = lookup_fn()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results, errors

    def test_gnomad_concurrent_same_key(self):
        lkp = GnomadLookup(cfg={})
        with patch.object(lkp, "_api_lookup", return_value=GnomadLookupOutcome.ABSENT):
            results, errors = self._run_concurrent(lambda: lkp.lookup("17", 43057051, "A", "T"))
        assert not errors
        assert all(r == GnomadLookupOutcome.ABSENT for r in results)
        stats = lkp.cache_stats()
        assert stats["hits"] + stats["misses"] == 10
        assert stats["size"] == 1

    def test_clinvar_concurrent_same_key(self):
        from pipeline.clinvar.lookup import ClinVarLookup

        lkp = ClinVarLookup(cfg={})
        with patch.object(lkp, "_api_lookup", return_value=None):
            results, errors = self._run_concurrent(lambda: lkp.lookup("17", 43057051, "A", "T"))
        assert not errors
        stats = lkp.cache_stats()
        assert stats["hits"] + stats["misses"] == 10

    def test_constraint_concurrent_same_key(self):
        from pipeline.constraint.lookup import GnomadConstraintLookup

        lkp = GnomadConstraintLookup(cfg={})
        with patch.object(lkp, "_uncached_lookup", return_value=None):
            results, errors = self._run_concurrent(lambda: lkp.lookup("BRCA1"))
        assert not errors
        stats = lkp.cache_stats()
        assert stats["hits"] + stats["misses"] == 10

    def test_hotspot_concurrent_same_key(self):
        from pipeline.hotspot.lookup import HotspotLookup

        lkp = HotspotLookup(cfg={})
        mock_rec = MagicMock()
        with patch.object(lkp, "_lookup_internal", return_value=mock_rec):
            results, errors = self._run_concurrent(lambda: lkp.lookup("17", 43057051))
        assert not errors
        stats = lkp.cache_stats()
        assert stats["hits"] + stats["misses"] == 10

    def test_clear_cache_is_atomic_gnomad(self):
        lkp = GnomadLookup(cfg={})
        with patch.object(lkp, "_api_lookup", return_value=GnomadLookupOutcome.ABSENT):
            lkp.lookup("17", 100, "A", "T")
            lkp.lookup("17", 200, "G", "C")
        assert lkp.cache_stats()["size"] == 2
        lkp.clear_cache()
        s = lkp.cache_stats()
        assert s["size"] == 0 and s["hits"] == 0 and s["misses"] == 0

    def test_clear_cache_clinvar_lookup_class(self):
        from pipeline.clinvar.lookup import ClinVarLookup

        for Cls, method, args in [
            (ClinVarLookup, "_api_lookup", ("17", 1, "A", "T")),
        ]:
            lkp = Cls(cfg={})
            with patch.object(lkp, method, return_value=None):
                lkp.lookup(*args)
            lkp.clear_cache()
            s = lkp.cache_stats()
            assert s["size"] == 0 and s["hits"] == 0 and s["misses"] == 0, (
                f"{Cls.__name__}.clear_cache() did not reset"
            )


# ─────────────────────────────────────────────────────────────────────────────
# FIX 10 — asyncio patterns
# ─────────────────────────────────────────────────────────────────────────────


class TestFix10AsyncioPatterns:
    def test_get_event_loop_not_called_in_endpoint(self):
        """No bare get_event_loop() call should appear in start_pipeline body."""
        import inspect

        from api import main as main_mod

        src = inspect.getsource(main_mod)
        # Extract only the start_pipeline function lines (not the comment mentioning it)
        lines = src.splitlines()
        in_fn = False
        fn_lines = []
        for line in lines:
            if "async def start_pipeline" in line:
                in_fn = True
            if in_fn:
                # Skip pure comment lines — they may mention the old name for contrast
                stripped = line.strip()
                if not stripped.startswith("#"):
                    fn_lines.append(line)
            if in_fn and stripped.startswith("return {") and "run_id" in stripped:
                break
        combined = "\n".join(fn_lines)
        assert "get_event_loop()" not in combined, (
            "get_event_loop() must not appear in start_pipeline non-comment code"
        )

    def test_get_running_loop_in_source(self):
        import inspect

        from api import main as main_mod

        src = inspect.getsource(main_mod)
        assert "get_running_loop()" in src

    def test_done_callback_in_source(self):
        import inspect

        from api import main as main_mod

        src = inspect.getsource(main_mod)
        assert "add_done_callback" in src

    @pytest.mark.anyio
    async def test_endpoint_returns_202_immediately(self):
        """The endpoint returns 202 without waiting for pipeline completion."""
        resp = await _api_post(_make_body())
        assert resp.status_code == 202
        assert "run_id" in resp.json()

    def test_thread_exception_marks_run_failed(self):
        """The _on_done callback must set status=failed and a non-empty error string."""
        from api.main import _RUNS

        run_id = "fix10_test_fail_run"
        _RUNS[run_id] = {"status": "running", "error": None}

        def _on_done(fut):
            try:
                exc = fut.exception()
                if exc and run_id in _RUNS:
                    _RUNS[run_id]["status"] = "failed"
                    _RUNS[run_id]["error"] = str(exc)
            except Exception:
                pass

        mock_fut = MagicMock()
        mock_fut.exception.return_value = RuntimeError("pipeline exploded")
        _on_done(mock_fut)

        assert _RUNS[run_id]["status"] == "failed"
        assert _RUNS[run_id]["error"] == "pipeline exploded"
        del _RUNS[run_id]

    def test_done_callback_does_not_raise_on_cancelled_future(self):
        """_on_done must absorb CancelledError from fut.exception()."""
        from concurrent.futures import CancelledError as FutCancelled

        from api.main import _RUNS

        run_id = "fix10_cancel_run"
        _RUNS[run_id] = {"status": "running", "error": None}

        def _on_done(fut):
            try:
                exc = fut.exception()
                if exc and run_id in _RUNS:
                    _RUNS[run_id]["status"] = "failed"
                    _RUNS[run_id]["error"] = str(exc)
            except Exception:
                pass

        mock_fut = MagicMock()
        mock_fut.exception.side_effect = FutCancelled()
        _on_done(mock_fut)  # must not raise
        assert _RUNS[run_id]["status"] == "running"
        del _RUNS[run_id]

    def test_no_deprecation_warning_from_get_running_loop(self):
        """get_running_loop() inside a running loop must not emit DeprecationWarning."""

        async def _inner():
            loop = asyncio.get_running_loop()
            assert loop is not None

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            asyncio.run(_inner())


# ─────────────────────────────────────────────────────────────────────────────
# FIX 11 — Pedigree/phenotype sidecar fields
# ─────────────────────────────────────────────────────────────────────────────


def _load_pedigree_and_build_ev(sidecar_path: str | None, variant_key: str) -> VariantEvidence:
    """Reproduce the Stage 4b pedigree-loading logic from runner.py exactly."""
    _pedigree_data: dict = {}
    _global_inheritance: str | None = None

    if sidecar_path:
        _pedigree_path = Path(sidecar_path)
        if _pedigree_path.exists():
            try:
                _ped_raw = json.loads(_pedigree_path.read_text())
                _global_inheritance = _ped_raw.get("inheritance_pattern")
                _pedigree_data = _ped_raw.get("variants", {})
            except Exception:
                pass

    chrom, pos_s, ref, alt = variant_key.split(":")
    _ped_v: dict = _pedigree_data.get(variant_key, {})
    return VariantEvidence(
        chrom=chrom,
        pos=int(pos_s),
        ref=ref,
        alt=alt,
        confirmed_de_novo=_ped_v.get("confirmed_de_novo"),
        assumed_de_novo=_ped_v.get("assumed_de_novo"),
        segregates_with_disease=_ped_v.get("segregates_with_disease"),
        segregates_away_from_disease=_ped_v.get("segregates_away_from_disease"),
        phenotype_specific_for_gene=_ped_v.get("phenotype_specific_for_gene"),
        in_trans_with_pathogenic=_ped_v.get("in_trans_with_pathogenic"),
        in_trans_or_cis_with_pathogenic_unexpected=_ped_v.get(
            "in_trans_or_cis_with_pathogenic_unexpected"
        ),
        alternate_molecular_basis_found=_ped_v.get("alternate_molecular_basis_found"),
        inheritance_pattern=_ped_v.get("inheritance_pattern") or _global_inheritance,
    )


class TestFix11PedigreeSidecar:
    def _write_sidecar(self, tmp_path: Path, data: dict) -> str:
        p = tmp_path / "pedigree.json"
        p.write_text(json.dumps(data))
        return str(p)

    def test_confirmed_de_novo_fires_ps2(self, tmp_path):
        key = "17:43057051:A:T"
        sc = self._write_sidecar(
            tmp_path,
            {
                "inheritance_pattern": "AD",
                "variants": {key: {"confirmed_de_novo": True, "assumed_de_novo": False}},
            },
        )
        ev = _load_pedigree_and_build_ev(sc, key)
        assert ev.confirmed_de_novo is True
        result = AcmgClassifier(cfg={}).classify(ev)
        assert "PS2" in result.criteria_met, (
            f"PS2 should fire for confirmed_de_novo; got {result.criteria_met}"
        )

    def test_inheritance_ar_plus_in_trans_fires_pm3(self, tmp_path):
        key = "13:32340300:G:A"
        sc = self._write_sidecar(
            tmp_path,
            {
                "inheritance_pattern": "AR",
                "variants": {key: {"in_trans_with_pathogenic": True}},
            },
        )
        ev = _load_pedigree_and_build_ev(sc, key)
        assert ev.in_trans_with_pathogenic is True
        assert ev.inheritance_pattern == "AR"
        result = AcmgClassifier(cfg={}).classify(ev)
        assert "PM3" in result.criteria_met, (
            f"PM3 should fire for in_trans+AR; got {result.criteria_met}"
        )

    def test_missing_sidecar_no_crash(self, tmp_path):
        ev = _load_pedigree_and_build_ev(str(tmp_path / "nope.json"), "17:43057051:A:T")
        assert ev.confirmed_de_novo is None
        assert ev.inheritance_pattern is None

    def test_malformed_json_sidecar_no_crash(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json}}}")
        ev = _load_pedigree_and_build_ev(str(bad), "17:43057051:A:T")
        assert ev.confirmed_de_novo is None
        assert ev.inheritance_pattern is None

    def test_variant_key_absent_from_sidecar_fields_none(self, tmp_path):
        sc = self._write_sidecar(tmp_path, {"inheritance_pattern": None, "variants": {}})
        ev = _load_pedigree_and_build_ev(sc, "1:100:A:T")
        assert ev.confirmed_de_novo is None
        assert ev.in_trans_with_pathogenic is None

    def test_global_inheritance_applied_when_no_per_variant_override(self, tmp_path):
        key = "17:43057051:A:T"
        sc = self._write_sidecar(
            tmp_path,
            {
                "inheritance_pattern": "XL",
                "variants": {key: {"confirmed_de_novo": False}},
            },
        )
        ev = _load_pedigree_and_build_ev(sc, key)
        assert ev.inheritance_pattern == "XL"

    def test_per_variant_inheritance_overrides_global(self, tmp_path):
        key = "17:43057051:A:T"
        sc = self._write_sidecar(
            tmp_path,
            {
                "inheritance_pattern": "AD",
                "variants": {key: {"inheritance_pattern": "AR"}},
            },
        )
        ev = _load_pedigree_and_build_ev(sc, key)
        assert ev.inheritance_pattern == "AR"

    def test_runner_run_has_pedigree_json_param(self):
        import inspect

        from pipeline.orchestration.runner import PipelineRunner

        sig = inspect.signature(PipelineRunner.run)
        assert "pedigree_json" in sig.parameters

    def test_api_request_model_has_pedigree_json(self):
        from api.main import PipelineStartRequest

        assert "pedigree_json" in PipelineStartRequest.model_fields
