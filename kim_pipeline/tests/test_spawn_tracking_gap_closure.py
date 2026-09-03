"""
tests/test_spawn_tracking_gap_closure.py
───────────────────────────────────────────
Closes the enumeration gap in the original Windows/POSIX process-kill fix
(see pipeline/utils/process_control.py): the original audit's grep for
subprocess spawn sites used a bare `subprocess\\.(Popen|run|...)\\(` pattern,
which missed every site that imported the module under an alias
(`import subprocess as _sp`) or that lived in a file not touched by that
audit's file list.

Five real, undertracked spawn sites were found on a fuller pass:
  1. pipeline/variant_calling/stage.py -- bcftools --version check
  2. pipeline/variant_calling/stage.py -- bcftools norm
  3. pipeline/blast/stage.py           -- blastn -version check
  4. pipeline/reporting/stage.py       -- wkhtmltopdf HTML->PDF render
  5. pipeline/annotation/codon_provider.py -- samtools version check

Each of these is routed through spawn_tracked() below (same mechanism the
main fix uses for bwa/freebayes/BLAST-search/VEP), so a DELETE mid-run can
now reach these too. Two further sites (codon_provider.py's samtools faidx,
called once per codon lookup -- thousands of times per run) are DELIBERATELY
left on bare subprocess.run(); see the comments at those call sites for why.

These tests only verify ROUTING (spawn_tracked is the thing actually
called, not a raw subprocess.run/Popen) -- they mock spawn_tracked itself
and assert the call, same style as tests/test_run_timeout.py's coverage of
pipeline/fastq/errors.py::_run(). They do not need bcftools/samtools/BLAST/
wkhtmltopdf installed to run.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _fake_popen(returncode=0, stdout="", stderr=""):
    """A Mock standing in for the subprocess.Popen object spawn_tracked()
    returns."""
    fake_proc = mock.Mock()
    fake_proc.returncode = returncode
    fake_proc.communicate.return_value = (stdout, stderr)
    return fake_proc


class TestVariantCallingStageBcftoolsRouting:
    """pipeline/variant_calling/stage.py's bcftools --version check and
    bcftools norm call must both go through spawn_tracked, not the
    `import subprocess as _sp; _sp.run(...)` alias that defeated the
    original audit's grep."""

    def _run_stage(self, tmp_path, version_proc, norm_proc=None):
        from pipeline.variant_calling.stage import VariantCallingStage

        bam_path = str(tmp_path / "in.bam")
        reference_fasta = str(tmp_path / "ref.fa")
        output_dir = str(tmp_path / "out")

        stage = VariantCallingStage(cfg={})

        procs = [version_proc] if norm_proc is None else [version_proc, norm_proc]

        with mock.patch(
            "pipeline.variant_calling.stage.freebayes_runner.is_available",
            return_value=True,
        ):
            with mock.patch("pipeline.variant_calling.stage.freebayes_runner.run_freebayes"):
                with mock.patch("pipeline.variant_calling.stage.apply_pass_filter") as mock_filter:
                    mock_filter.return_value = mock.Mock(
                        total_input=0,
                        total_pass=0,
                        snvs_pass=0,
                        indels_pass=0,
                        thresholds_used={},
                    )
                    with mock.patch(
                        "pipeline.variant_calling.stage.spawn_tracked",
                        side_effect=procs,
                    ) as mock_spawn:
                        stage.run(bam_path, reference_fasta, output_dir, sample_id="T1")
        return mock_spawn

    def test_bcftools_version_check_routed_through_spawn_tracked(self, tmp_path):
        """RED before the fix: the version check used `_sp.run` (a local
        alias import), never spawn_tracked -- this call would not have
        been observed at all."""
        version_proc = _fake_popen(returncode=1)  # not 0 -> norm skipped
        mock_spawn = self._run_stage(tmp_path, version_proc)

        mock_spawn.assert_called_once()
        cmd = mock_spawn.call_args_list[0].args[0]
        assert cmd == ["bcftools", "--version"]

    def test_bcftools_norm_routed_through_spawn_tracked(self, tmp_path):
        """RED before the fix: bcftools norm also used `_sp.run` directly."""
        version_proc = _fake_popen(returncode=0)
        norm_proc = _fake_popen(returncode=0)
        mock_spawn = self._run_stage(tmp_path, version_proc, norm_proc)

        assert mock_spawn.call_count == 2
        norm_cmd = mock_spawn.call_args_list[1].args[0]
        assert norm_cmd[:2] == ["bcftools", "norm"]

    def test_bcftools_version_check_timeout_is_killed_via_tracked_machinery(self, tmp_path):
        """A stalled bcftools --version must be reachable by
        kill_process_tree_now, not left to hang forever."""
        from pipeline.variant_calling.stage import VariantCallingStage

        bam_path = str(tmp_path / "in.bam")
        reference_fasta = str(tmp_path / "ref.fa")
        output_dir = str(tmp_path / "out")
        stage = VariantCallingStage(cfg={})

        version_proc = mock.Mock()
        version_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd=["bcftools", "--version"], timeout=5),
            ("", ""),
        ]
        version_proc.returncode = None

        with mock.patch(
            "pipeline.variant_calling.stage.freebayes_runner.is_available",
            return_value=True,
        ):
            with mock.patch("pipeline.variant_calling.stage.freebayes_runner.run_freebayes"):
                with mock.patch("pipeline.variant_calling.stage.apply_pass_filter") as mock_filter:
                    mock_filter.return_value = mock.Mock(
                        total_input=0,
                        total_pass=0,
                        snvs_pass=0,
                        indels_pass=0,
                        thresholds_used={},
                    )
                    with mock.patch(
                        "pipeline.variant_calling.stage.spawn_tracked",
                        return_value=version_proc,
                    ):
                        with mock.patch(
                            "pipeline.variant_calling.stage.kill_process_tree_now"
                        ) as mock_kill:
                            stage.run(bam_path, reference_fasta, output_dir, sample_id="T2")

        mock_kill.assert_called_once_with(version_proc)


class TestBlastStageVersionCheckRouting:
    """pipeline/blast/stage.py::_get_version used a bare subprocess.run
    even though the main BLAST search call in the same file already used
    spawn_tracked -- the version check was the missed second site."""

    def test_get_version_routed_through_spawn_tracked(self):
        from pipeline.blast.stage import BLASTStage

        stage = BLASTStage(cfg={})
        fake_proc = _fake_popen(returncode=0, stdout="blastn: 2.13.0+\n")

        with mock.patch("pipeline.blast.stage.spawn_tracked", return_value=fake_proc) as mock_spawn:
            version = stage._get_version("blastn")

        mock_spawn.assert_called_once()
        cmd = mock_spawn.call_args.args[0]
        assert cmd == ["blastn", "-version"]
        assert "blastn" in version.lower()

    def test_get_version_timeout_is_killed_via_tracked_machinery(self):
        from pipeline.blast.stage import BLASTStage

        stage = BLASTStage(cfg={})
        fake_proc = mock.Mock()
        fake_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd=["blastn", "-version"], timeout=10),
            ("", ""),
        ]

        with mock.patch("pipeline.blast.stage.spawn_tracked", return_value=fake_proc):
            with mock.patch("pipeline.blast.stage.kill_process_tree_now") as mock_kill:
                version = stage._get_version("blastn")

        mock_kill.assert_called_once_with(fake_proc)
        assert version == "unknown" or version == ""


class TestReportingStageWkhtmltopdfRouting:
    """pipeline/reporting/stage.py's wkhtmltopdf fallback (the third PDF
    rendering path, after ReportLab and WeasyPrint) used a bare
    subprocess.run(check=True) -- routed to spawn_tracked below."""

    def test_wkhtmltopdf_routed_through_spawn_tracked(self, tmp_path):
        from pipeline.reporting.stage import ReportingStage, _pdf_report_mod

        stage = ReportingStage(cfg={})
        html_path = str(tmp_path / "report.html")
        pdf_path = str(tmp_path / "report.pdf")
        Path(html_path).write_text("<html></html>")

        fake_proc = _fake_popen(returncode=0)

        with mock.patch.object(
            _pdf_report_mod,
            "render_clinical_pdf",
            side_effect=_pdf_report_mod.ReportLabUnavailableError("no reportlab"),
        ):
            with mock.patch(
                "pipeline.reporting.stage.shutil.which", return_value="/usr/bin/wkhtmltopdf"
            ):
                with mock.patch(
                    "pipeline.reporting.stage.spawn_tracked", return_value=fake_proc
                ) as mock_spawn:
                    result = stage._try_render_pdf(html_path, pdf_path, sample_id="T1")

        mock_spawn.assert_called_once()
        cmd = mock_spawn.call_args.args[0]
        assert cmd == ["wkhtmltopdf", html_path, pdf_path]
        assert result == pdf_path


class TestCodonProviderSamtoolsVersionCheckRouting:
    """codon_provider.py::_FastaReader._check_samtools used a bare
    subprocess.run -- routed below. The two samtools faidx sites in the
    same class (_samtools_fetch) are DELIBERATELY left untracked; see the
    comments at those call sites for the fixed rationale, not re-asserted
    here."""

    def test_check_samtools_routed_through_spawn_tracked(self):
        from pipeline.annotation.codon_provider import _FastaReader

        fake_proc = _fake_popen(returncode=0)
        with mock.patch(
            "pipeline.annotation.codon_provider.spawn_tracked",
            return_value=fake_proc,
        ) as mock_spawn:
            result = _FastaReader._check_samtools()

        mock_spawn.assert_called_once()
        cmd = mock_spawn.call_args.args[0]
        assert cmd == ["samtools", "version"]
        assert result is True

    def test_check_samtools_timeout_is_killed_via_tracked_machinery(self):
        from pipeline.annotation.codon_provider import _FastaReader

        fake_proc = mock.Mock()
        fake_proc.communicate.side_effect = subprocess.TimeoutExpired(
            cmd=["samtools", "version"], timeout=5
        )

        with mock.patch(
            "pipeline.annotation.codon_provider.spawn_tracked",
            return_value=fake_proc,
        ):
            with mock.patch(
                "pipeline.annotation.codon_provider.kill_process_tree_now"
            ) as mock_kill:
                result = _FastaReader._check_samtools()

        mock_kill.assert_called_once_with(fake_proc)
        assert result is False

    def test_check_samtools_missing_binary_returns_false(self):
        """FileNotFoundError (samtools not installed) must still be
        handled gracefully -- same external behavior as before routing."""
        from pipeline.annotation.codon_provider import _FastaReader

        with mock.patch(
            "pipeline.annotation.codon_provider.spawn_tracked",
            side_effect=FileNotFoundError("samtools"),
        ):
            result = _FastaReader._check_samtools()

        assert result is False


class TestDeliberatelyUntrackedFaidxSitesAreDocumented:
    """The two skip-with-comment sites (codon_provider.py:~305,~318,
    samtools faidx in _samtools_fetch) must still work exactly as before
    (bare subprocess.run, no spawn_tracked) AND must carry an explicit
    comment explaining why -- this is a documentation requirement, not a
    behavior test, but it's cheap to assert the source carries the
    required comment so the exclusion can't silently regress into
    'nobody remembers why this one's different'."""

    def test_faidx_call_sites_are_not_routed_through_spawn_tracked(self):
        """Confirms the deliberate exclusion: spawn_tracked must NOT be
        called when _samtools_fetch runs (only bare subprocess.run)."""
        from pipeline.annotation.codon_provider import _FastaReader

        reader = _FastaReader.__new__(_FastaReader)
        reader._path = "/fake/ref.fa"
        reader._in_memory = None
        reader._has_samtools = True

        fake_result = mock.Mock(returncode=0, stdout=">chr1\nACGT\n")
        with mock.patch(
            "pipeline.annotation.codon_provider.subprocess.run",
            return_value=fake_result,
        ) as mock_run:
            with mock.patch("pipeline.annotation.codon_provider.spawn_tracked") as mock_spawn:
                reader._samtools_fetch("chr1", 1, 4)

        mock_run.assert_called_once()
        mock_spawn.assert_not_called()

    def test_source_documents_the_exclusion_rationale(self):
        source = Path(
            Path(__file__).resolve().parents[1] / "pipeline" / "annotation" / "codon_provider.py"
        ).read_text(encoding="utf-8")
        assert "Deliberately NOT routed through spawn_tracked" in source
        assert "faidx" in source
