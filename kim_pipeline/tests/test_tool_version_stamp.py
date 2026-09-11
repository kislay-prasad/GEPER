"""
Kim stamps the versions of the tools that produced its VCF into the VCF's own
header (the human's ruling, 2026-09-11; acceptance via god: "read from the
binaries kim actually resolved at run time, not the pin; `##` lines only, and
prove a downstream parser still reads the file unchanged; a stub samtools on
PATH reporting a different version -> that version in the header; a samtools
whose version cannot be read -> an honest marker in the header, never a
default").

The stub samtools below answers both calls the stamp makes: `--version`, and
`view -H <bam>` (whose @PG lines name the aligner that ran).
"""

import os
import shutil
import stat
import subprocess
import sys

import pytest

from pipeline.variant_calling import tool_versions as tv

_VCF = (
    "##fileformat=VCFv4.2\n"
    "##source=freeBayes v1.3.10\n"
    "##bcftools_viewVersion=1.16+htslib-1.16\n"
    '##INFO=<ID=DP,Number=1,Type=Integer,Description="Total read depth">\n'
    "##contig=<ID=chr1,length=1000>\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
    "chr1\t100\t.\tA\tG\t50\tPASS\tDP=30\n"
    "chr1\t200\t.\tC\tT\t60\tPASS\tDP=41\n"
)
_PG_BWA = "@PG\tID:bwa\tPN:bwa\tVN:0.7.17-r1188\tCL:bwa mem r.fa q.fq"
_PG_MM2 = "@PG\tID:minimap2\tPN:minimap2\tVN:2.24-r1122\tCL:minimap2 -ax sr r.fa q.fq"
_PG_SORT = "@PG\tID:samtools\tPN:samtools\tPP:bwa\tVN:1.16.1\tCL:samtools sort"


def _stub_samtools(directory, version_lines, pg_lines, version_exit=0):
    """A samtools that prints `version_lines` for --version and `pg_lines` for view -H."""
    v = "\n".join(version_lines)
    pg = "\n".join(pg_lines)
    if sys.platform.startswith("win"):
        # A .bat cannot echo tabs or multi-line text cleanly; delegate to Python.
        helper = os.path.join(directory, "samtools_stub.py")
        with open(helper, "w", encoding="utf-8") as fh:
            fh.write(
                "import sys\n"
                f"V = {v!r}\nPG = {pg!r}\n"
                "if sys.argv[1:2] == ['--version']:\n"
                f"    print(V)\n    sys.exit({version_exit})\n"
                "if sys.argv[1:3] == ['view', '-H']:\n"
                "    print('@HD\\tVN:1.6\\tSO:coordinate')\n    print(PG)\n    sys.exit(0)\n"
                "sys.exit(2)\n"
            )
        path = os.path.join(directory, "samtools.bat")
        with open(path, "w", encoding="ascii") as fh:
            fh.write(f'@"{sys.executable}" "{helper}" %*\r\n@exit /b %ERRORLEVEL%\r\n')
        return path
    path = os.path.join(directory, "samtools")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then\n'
            f"cat <<'EOF'\n{v}\nEOF\nexit {version_exit}\nfi\n"
            'if [ "$1" = "view" ] && [ "$2" = "-H" ]; then\n'
            f"printf '@HD\\tVN:1.6\\tSO:coordinate\\n'\ncat <<'EOF'\n{pg}\nEOF\nexit 0\nfi\n"
            "exit 2\n"
        )
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _write_vcf(tmp_path):
    p = tmp_path / "filtered_variants.vcf"
    p.write_text(_VCF, newline="")
    return str(p)


def _header(vcf_path):
    return [ln.rstrip("\n") for ln in open(vcf_path, encoding="utf-8") if ln.startswith("##")]


# --- The control the card asked for: a different samtools -> that version ---


def test_stub_samtools_is_stamped_as_its_own_version(tmp_path):
    stub = _stub_samtools(
        str(tmp_path), ["samtools 9.99-stub", "Using htslib 9.98"], [_PG_BWA, _PG_SORT]
    )
    vcf = _write_vcf(tmp_path)
    tv.stamp_tool_versions(vcf, str(tmp_path / "x.bam"), samtools=stub)
    hdr = _header(vcf)
    assert "##samtoolsVersion=9.99-stub+htslib-9.98" in hdr
    assert "##alignerVersion=bwa 0.7.17-r1188" in hdr


def test_the_aligner_is_whichever_one_the_bam_says_ran(tmp_path):
    stub = _stub_samtools(
        str(tmp_path), ["samtools 1.16.1", "Using htslib 1.16"], [_PG_MM2, _PG_SORT]
    )
    vcf = _write_vcf(tmp_path)
    tv.stamp_tool_versions(vcf, str(tmp_path / "x.bam"), samtools=stub)
    assert "##alignerVersion=minimap2 2.24-r1122" in _header(vcf)


def test_resolution_is_the_samtools_on_path_when_none_is_passed(tmp_path, monkeypatch):
    stub = _stub_samtools(str(tmp_path), ["samtools 9.99-stub", "Using htslib 9.98"], [_PG_BWA])
    monkeypatch.setattr(tv.shutil, "which", lambda name: stub if name == "samtools" else None)
    vcf = _write_vcf(tmp_path)
    tv.stamp_tool_versions(vcf, str(tmp_path / "x.bam"))
    assert "##samtoolsVersion=9.99-stub+htslib-9.98" in _header(vcf)


# --- Honest markers, never defaults ---


def test_no_samtools_is_an_honest_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(tv.shutil, "which", lambda name: None)
    vcf = _write_vcf(tmp_path)
    tv.stamp_tool_versions(vcf, str(tmp_path / "x.bam"))
    hdr = _header(vcf)
    assert "##samtoolsVersion=NOT READ: samtools not found on PATH" in hdr
    assert any(h.startswith("##alignerVersion=NOT READ: ") for h in hdr)
    assert not any(
        "1.16" in h for h in hdr if h.startswith(("##samtoolsVersion", "##alignerVersion"))
    )


def test_samtools_that_fails_version_is_an_honest_marker(tmp_path):
    stub = _stub_samtools(
        str(tmp_path), ["samtools: unrecognized option '--version'"], [_PG_BWA], version_exit=1
    )
    vcf = _write_vcf(tmp_path)
    tv.stamp_tool_versions(vcf, str(tmp_path / "x.bam"), samtools=stub)
    line = next(h for h in _header(vcf) if h.startswith("##samtoolsVersion="))
    assert line.startswith("##samtoolsVersion=NOT READ: ")
    assert "exited 1" in line


def test_bam_with_no_aligner_pg_is_an_honest_marker(tmp_path):
    stub = _stub_samtools(str(tmp_path), ["samtools 1.16.1", "Using htslib 1.16"], [_PG_SORT])
    vcf = _write_vcf(tmp_path)
    tv.stamp_tool_versions(vcf, str(tmp_path / "x.bam"), samtools=stub)
    line = next(h for h in _header(vcf) if h.startswith("##alignerVersion="))
    assert line.startswith("##alignerVersion=NOT READ: no aligner @PG line")


# --- The file is otherwise unchanged, and downstream parsers still read it ---


def test_only_two_hash_hash_lines_are_added_directly_before_chrom(tmp_path):
    stub = _stub_samtools(str(tmp_path), ["samtools 1.16.1", "Using htslib 1.16"], [_PG_BWA])
    vcf = _write_vcf(tmp_path)
    added = tv.stamp_tool_versions(vcf, str(tmp_path / "x.bam"), samtools=stub)
    after = open(vcf, encoding="utf-8", newline="").read().splitlines(keepends=True)
    before = _VCF.splitlines(keepends=True)
    chrom = next(i for i, ln in enumerate(after) if ln.startswith("#CHROM"))
    assert [ln.rstrip("\n") for ln in after[chrom - 2 : chrom]] == added
    assert all(a.startswith("##") for a in added)
    assert after[: chrom - 2] + after[chrom:] == before


def test_bcftools_still_reads_the_stamped_file_and_its_records_are_unchanged(tmp_path):
    bcftools = shutil.which("bcftools")
    if not bcftools:
        pytest.skip("bcftools not installed (CI's kim job installs the pinned one)")
    stub = _stub_samtools(str(tmp_path), ["samtools 1.16.1", "Using htslib 1.16"], [_PG_BWA])
    vcf = _write_vcf(tmp_path)
    tv.stamp_tool_versions(vcf, str(tmp_path / "x.bam"), samtools=stub)
    out = subprocess.run([bcftools, "view", "-H", vcf], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == [ln for ln in _VCF.splitlines() if not ln.startswith("#")]
    hdr = subprocess.run([bcftools, "view", "-h", vcf], capture_output=True, text=True).stdout
    assert "##samtoolsVersion=1.16.1+htslib-1.16" in hdr


def test_the_stage_wires_the_stamp_after_the_pass_filter():
    # Structural, and stated as such: running the stage needs freebayes.
    import ast
    import inspect
    import textwrap

    from pipeline.variant_calling.stage import VariantCallingStage

    tree = ast.parse(textwrap.dedent(inspect.getsource(VariantCallingStage.run)))
    # ast.walk is breadth-first, not source order -- sort by line.
    order = [
        name
        for _, name in sorted(
            (n.lineno, n.func.id)
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id in ("apply_pass_filter", "stamp_tool_versions")
        )
    ]
    assert order == ["apply_pass_filter", "stamp_tool_versions"]
    call = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "stamp_tool_versions"
    )
    assert [ast.unparse(a) for a in call.args] == ["filtered_vcf_path", "bam_path"]
