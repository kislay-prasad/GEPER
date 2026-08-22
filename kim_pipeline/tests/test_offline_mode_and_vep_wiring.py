"""
tests/test_offline_mode_and_vep_wiring.py
───────────────────────────────────────────
Regression tests for:

  Issue 1 — `clinvar.enabled: false` / `gnomad.enabled: false` were ignored;
            lookups still issued live HTTP requests.
  Issue 2 — `python-multipart` (required for FastAPI file uploads) was
            missing from pyproject.toml's runtime dependencies.
  Issue 3 — `python main.py vcf` silently skipped VEP annotation with no
            user-visible warning, even when `vep.enabled: true`.
  Issue 4 — pyproject.toml used an invalid/legacy build backend.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ─── Issue 1: ClinVar offline mode ────────────────────────────────────────────


class TestClinVarOfflineMode:
    def test_disabled_lookup_returns_none_without_network(self):
        from pipeline.clinvar.lookup import ClinVarLookup

        lkp = ClinVarLookup(cfg={"clinvar": {"enabled": False}})
        with patch("pipeline.clinvar.lookup._api_get") as mock_get:
            result = lkp.lookup("17", 43057051, "A", "T")
            mock_get.assert_not_called()
        assert result is None

    def test_disabled_codon_scan_returns_none_tuple(self):
        from pipeline.clinvar.lookup import ClinVarLookup

        lkp = ClinVarLookup(cfg={"clinvar": {"enabled": False}})
        result = lkp.check_same_codon_pathogenic("17", 43057051, "A", "T", "Cys", "Tyr")
        assert result == (None, None)

    def test_enabled_lookup_still_calls_api(self):
        """Backward compatibility: enabled (default) behavior is unchanged."""
        from pipeline.clinvar.lookup import ClinVarLookup

        lkp = ClinVarLookup(cfg={"clinvar": {"enabled": True}})
        with patch("pipeline.clinvar.lookup._api_get") as mock_get:
            mock_get.return_value = None
            lkp.lookup("17", 43057051, "A", "T")
            assert mock_get.called

    def test_default_enabled_when_key_absent(self):
        """Omitting `clinvar.enabled` entirely must preserve legacy behavior
        (enabled), not silently disable lookups."""
        from pipeline.clinvar.lookup import ClinVarLookup

        lkp = ClinVarLookup(cfg={"clinvar": {}})
        assert lkp._enabled is True


# ─── Issue 1: gnomAD offline mode ─────────────────────────────────────────────


class TestGnomadOfflineMode:
    def test_disabled_lookup_returns_unavailable_without_network(self):
        from pipeline.gnomad.lookup import GnomadLookup, GnomadLookupOutcome

        gn = GnomadLookup(cfg={"gnomad": {"enabled": False}})
        with patch("pipeline.gnomad.lookup.requests.post") as mock_post:
            result = gn.lookup("17", 43057051, "A", "T")
            mock_post.assert_not_called()
        assert result is GnomadLookupOutcome.UNAVAILABLE

    def test_disabled_backend_is_marked_disabled(self):
        from pipeline.gnomad.lookup import GnomadLookup

        gn = GnomadLookup(cfg={"gnomad": {"enabled": False}})
        assert gn._backend == "disabled"

    def test_enabled_lookup_still_calls_api(self):
        from pipeline.gnomad.lookup import GnomadLookup, GnomadLookupOutcome

        gn = GnomadLookup(cfg={"gnomad": {"enabled": True}})
        with patch("pipeline.gnomad.lookup.requests.post") as mock_post:
            mock_post.side_effect = ConnectionError("blocked")
            result = gn.lookup("17", 43057051, "A", "T")
            assert mock_post.called
        assert result is GnomadLookupOutcome.UNAVAILABLE

    def test_default_enabled_when_key_absent(self):
        from pipeline.gnomad.lookup import GnomadLookup

        gn = GnomadLookup(cfg={"gnomad": {}})
        assert gn._enabled is True


# ─── Issue 1: end-to-end offline mode via `main.py vcf` ──────────────────────

SAMPLE_VCF = """##fileformat=VCFv4.2
##contig=<ID=chr17>
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE
chr17\t43057051\t.\tA\tT\t100\tPASS\t.\tGT\t0/1
"""

OFFLINE_CONFIG = """
clinvar:
  enabled: false
gnomad:
  enabled: false
vep:
  enabled: false
"""


def test_cli_offline_mode_makes_no_outbound_network_calls(tmp_path):
    """With clinvar/gnomad/vep all disabled, `main.py vcf` must complete
    without attempting any outbound HTTP request (no 403/connection-error
    noise from blocked sandboxed network), and must log that lookups were
    skipped because the service is disabled."""
    vcf = tmp_path / "sample.vcf"
    vcf.write_text(SAMPLE_VCF)
    cfg = tmp_path / "offline.yaml"
    cfg.write_text(OFFLINE_CONFIG)
    out_dir = tmp_path / "out"

    proc = subprocess.run(
        [
            sys.executable,
            "main.py",
            "vcf",
            "--input",
            str(vcf),
            "--output-dir",
            str(out_dir),
            "--config",
            str(cfg),
            "--log-level",
            "INFO",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    combined = proc.stdout + proc.stderr

    assert "clinvar.enabled is false" in combined or "[ClinVar] Lookup disabled" in combined
    assert "[gnomAD] Lookup disabled" in combined or "gnomad.enabled is false" in combined
    # No live-network failure noise should appear when disabled.
    assert "403 Client Error" not in combined
    assert "Connection" not in combined or "ConnectionError" not in combined


# ─── Issue 3: VEP wiring from `python main.py vcf` ───────────────────────────


class TestVepWiringFromVcfCommand:
    def test_vep_stage_invoked_when_enabled(self, tmp_path, monkeypatch):
        """VEPAnnotationStage.run() must actually be called by cmd_vcf
        whenever vep.enabled is true (default)."""
        monkeypatch.chdir(REPO_ROOT)
        sys.path.insert(0, str(REPO_ROOT))
        import importlib
        import main as main_mod

        importlib.reload(main_mod)

        vcf = tmp_path / "sample.vcf"
        vcf.write_text(SAMPLE_VCF)
        out_dir = tmp_path / "out"

        calls = {}

        class _FakeVepStage:
            def __init__(self, cfg):
                calls["constructed"] = True

            def run(self, filtered_vcf_path, output_dir, sample_id="SAMPLE"):
                calls["ran"] = True
                from pipeline.vep.stage import VEPAnnotationResult

                return VEPAnnotationResult(
                    annotated_vcf_path=filtered_vcf_path,
                    variants=[],
                    variant_count=0,
                )

        with patch("pipeline.vep.stage.VEPAnnotationStage", _FakeVepStage):
            args = (
                main_mod.parser.parse_args(
                    ["vcf", "--input", str(vcf), "--output-dir", str(out_dir)]
                )
                if hasattr(main_mod, "parser")
                else None
            )
            if args is None:
                # Fall back to calling cmd_vcf directly via argparse Namespace
                import argparse

                args = argparse.Namespace(
                    input=str(vcf),
                    output_dir=str(out_dir),
                    sample_id=None,
                    config=None,
                    log_level="INFO",
                )
            rc = main_mod.cmd_vcf(args)

        assert rc == 0
        assert calls.get("ran") is True, "VEPAnnotationStage.run() was never invoked"

    def test_vep_explicitly_disabled_prints_clear_warning(self, tmp_path):
        """When vep.enabled is false, the CLI must print a clear warning
        instead of silently continuing."""
        vcf = tmp_path / "sample.vcf"
        vcf.write_text(SAMPLE_VCF)
        cfg = tmp_path / "vep_off.yaml"
        cfg.write_text("vep:\n  enabled: false\n")
        out_dir = tmp_path / "out"

        proc = subprocess.run(
            [
                sys.executable,
                "main.py",
                "vcf",
                "--input",
                str(vcf),
                "--output-dir",
                str(out_dir),
                "--config",
                str(cfg),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        combined = proc.stdout + proc.stderr
        assert "VEP annotation SKIPPED" in combined
        assert "vep.enabled is false" in combined

    def test_vep_attempted_and_skip_reason_visible_when_binary_missing(self, tmp_path):
        """If vep.enabled is true but the `vep` binary is unavailable (as in
        this sandboxed test environment), the stage must still be attempted
        and the skip must be clearly visible to the user, not silent."""
        vcf = tmp_path / "sample.vcf"
        vcf.write_text(SAMPLE_VCF)
        out_dir = tmp_path / "out"

        proc = subprocess.run(
            [sys.executable, "main.py", "vcf", "--input", str(vcf), "--output-dir", str(out_dir)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        combined = proc.stdout + proc.stderr
        assert "Annotating VCF" in combined
        # Either VEP ran successfully, or its absence is clearly surfaced.
        assert ("VEP annotation:" in combined) or ("VEP annotation SKIPPED" in combined)


# ─── Issue 5: VEP plugin data must be configured, not assumed present ────────


class TestVepPluginDataGating:
    """A literal-docs operator who follows INSTALL_DEPENDENCIES.md installs
    the `vep` binary and a cache dir, but is never told to fetch CADD/REVEL/
    AlphaMissense plugin data files -- and there was previously no config
    knob to supply them even if they did. `_build_vep_cmd` requested
    `--plugin CADD` / `--plugin REVEL` / `--plugin AlphaMissense` bare (no
    data-file argument) unconditionally, which VEP rejects immediately
    (these three plugins require a data file argument), turning first run
    into a hard failure regardless of cache/binary setup. Plugins must be
    requested only when their data file is configured, and omitted
    otherwise -- mirroring the graceful degradation VEP-as-a-whole already
    documents and the ACMG classifier already tolerates (cadd_phred /
    revel_score / am_pathogenicity are `Optional[float]`, checked
    `is not None` before voting)."""

    def _cmd(self, vep_cfg):
        from pipeline.vep.stage import VEPAnnotationStage

        stage = VEPAnnotationStage(cfg={"vep": vep_cfg})
        return stage._build_vep_cmd("vep", "in.vcf", "out.vcf")

    def test_no_bare_plugin_flags_by_default(self):
        """With no plugin data configured (the out-of-the-box default),
        CADD/REVEL/AlphaMissense must not be requested at all -- a bare
        `--plugin CADD` with no data file is a guaranteed VEP failure."""
        cmd = self._cmd({})
        plugin_args = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--plugin"]
        assert plugin_args == [], (
            f"plugin requested without data file, which VEP rejects: {plugin_args}"
        )

    def test_cadd_plugin_requested_with_data_path_when_configured(self):
        cmd = self._cmd({"cadd_data": "/data/cadd/whole_genome_SNVs.tsv.gz"})
        assert cmd[cmd.index("--plugin") : cmd.index("--plugin") + 2] == (
            ["--plugin", "CADD,/data/cadd/whole_genome_SNVs.tsv.gz"]
        )

    def test_revel_plugin_requested_with_data_path_when_configured(self):
        cmd = self._cmd({"revel_data": "/data/revel/revel.tsv.gz"})
        assert cmd[cmd.index("--plugin") : cmd.index("--plugin") + 2] == (
            ["--plugin", "REVEL,/data/revel/revel.tsv.gz"]
        )

    def test_alphamissense_plugin_requested_with_data_path_when_configured(self):
        cmd = self._cmd({"alphamissense_data": "/data/am/AlphaMissense_hg38.tsv.gz"})
        assert cmd[cmd.index("--plugin") : cmd.index("--plugin") + 2] == (
            ["--plugin", "AlphaMissense,file=/data/am/AlphaMissense_hg38.tsv.gz"]
        )

    def test_dir_plugins_passed_through_when_configured(self):
        cmd = self._cmd({"dir_plugins": "/opt/vep/Plugins"})
        assert "--dir_plugins" in cmd
        assert cmd[cmd.index("--dir_plugins") + 1] == "/opt/vep/Plugins"

    def test_all_three_configured_together(self):
        cmd = self._cmd(
            {
                "cadd_data": "/data/cadd.tsv.gz",
                "revel_data": "/data/revel.tsv.gz",
                "alphamissense_data": "/data/am.tsv.gz",
            }
        )
        plugin_args = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--plugin"]
        assert plugin_args == [
            "CADD,/data/cadd.tsv.gz",
            "REVEL,/data/revel.tsv.gz",
            "AlphaMissense,file=/data/am.tsv.gz",
        ]


# ─── Issue 2 / Issue 4: dependency & build-backend hygiene ───────────────────


class TestDependencyAndBuildBackend:
    def test_python_multipart_in_pyproject_dependencies(self):
        # Use tomllib (stdlib in 3.11+) or tomli fallback for reliable parsing.
        try:
            import tomllib  # Python 3.11+
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ImportError:
                # Fall back to raw-text scan with a pattern that is not
                # confused by inner brackets such as uvicorn[standard].
                import re

                content = (REPO_ROOT / "pyproject.toml").read_text()
                # Capture the full dependencies = [ ... ] block
                m = re.search(
                    r"^dependencies\s*=\s*\[(.*?)\]",
                    content,
                    re.DOTALL | re.MULTILINE,
                )
                assert m, "Could not locate [dependencies] block in pyproject.toml"
                assert "python-multipart" in m.group(1)
                return

        content = (REPO_ROOT / "pyproject.toml").read_bytes()
        data = tomllib.loads(content.decode())
        deps = data.get("project", {}).get("dependencies", [])
        assert any("python-multipart" in d for d in deps), (
            "python-multipart not found in [project.dependencies] in pyproject.toml"
        )

    def test_python_multipart_in_requirements_txt(self):
        content = (REPO_ROOT / "requirements.txt").read_text()
        assert "python-multipart" in content

    def test_build_backend_is_modern_setuptools(self):
        content = (REPO_ROOT / "pyproject.toml").read_text()
        assert 'build-backend = "setuptools.build_meta"' in content
        assert "setuptools.backends.legacy" not in content

    def test_package_builds_successfully(self, tmp_path):
        """`python -m build --no-isolation --wheel` (or sdist) must succeed
        with the corrected build backend."""
        import importlib.util

        if importlib.util.find_spec("build") is None:
            pytest.skip("`build` package not installed in this environment")

        proc = subprocess.run(
            [sys.executable, "-m", "build", "--no-isolation", "--wheel", "--outdir", str(tmp_path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert list(tmp_path.glob("*.whl"))


# ─── Upload endpoints work end-to-end (Issue 2 regression) ───────────────────


def test_api_module_imports_with_multipart_available():
    """api/main.py wires UploadFile-based endpoints; importing it must not
    raise `python-multipart is not installed` errors."""
    import importlib

    api_main = importlib.import_module("api.main")
    assert hasattr(api_main, "app")
