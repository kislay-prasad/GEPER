"""
Stamp the versions of the tools that produced this VCF into its own header.

The human's ruling, 2026-09-11: "Reports not recording which htslib version
produced them is the same gap as the model-hash -- the thing that determines
the output isn't captured alongside the output." The VCF Kim hands on IS the
artefact alongside the output, so the versions go in its header, where GEPER
(and anyone else) reads them.

freebayes (`##source=freeBayes ...`) and bcftools (`##bcftools_*Version=
...+htslib-...`) already stamp themselves. The two that ran upstream of the
VCF and do not are added here, each MEASURED, never copied from a pin:

  ##samtoolsVersion=<version>+htslib-<version>
      `--version` of the samtools this run resolves -- `shutil.which`, the
      same resolution `fastq.errors._require` uses for every samtools call.
  ##alignerVersion=<program> <version>
      read from the BAM's own `@PG` header, which the aligner wrote as it
      ran (`@PG ID:bwa PN:bwa VN:0.7.17-r1188`). So it names whichever
      aligner actually produced the alignment -- bwa, bwa-mem2 or minimap2
      -- rather than assuming one.

A version that cannot be read is written as `NOT READ: <reason>`, never a
default. Only `##` lines are added, directly before `#CHROM`; every other
byte of the file is unchanged.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("geper.pipeline.variant_calling.tool_versions")

NOT_READ = "NOT READ"
_ALIGNERS = ("bwa", "bwa-mem2", "minimap2")


def _run(argv: List[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def read_samtools_version(samtools: Optional[str]) -> str:
    """`1.16.1+htslib-1.16` from `samtools --version`, or `NOT READ: <why>`."""
    if not samtools:
        return f"{NOT_READ}: samtools not found on PATH"
    try:
        proc = _run([samtools, "--version"])
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{NOT_READ}: {exc or type(exc).__name__}"
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if proc.returncode != 0 or not lines or not lines[0].startswith("samtools "):
        return f"{NOT_READ}: `samtools --version` exited {proc.returncode}: {lines[0] if lines else 'no output'}"
    version = lines[0][len("samtools ") :].strip()
    htslib = next(
        (ln[len("Using htslib ") :].strip() for ln in lines if ln.startswith("Using htslib ")), None
    )
    return f"{version}+htslib-{htslib}" if htslib else version


def read_aligner_from_bam(samtools: Optional[str], bam_path: str) -> str:
    """`bwa 0.7.17-r1188` from the BAM's own `@PG` header, or `NOT READ: <why>`."""
    if not samtools:
        return f"{NOT_READ}: samtools not found on PATH, so the BAM header could not be read"
    try:
        proc = _run([samtools, "view", "-H", bam_path])
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{NOT_READ}: {exc or type(exc).__name__}"
    if proc.returncode != 0:
        return f"{NOT_READ}: `samtools view -H` exited {proc.returncode}"
    found: List[str] = []
    for line in (proc.stdout or "").splitlines():
        if not line.startswith("@PG"):
            continue
        fields = dict(f.split(":", 1) for f in line.split("\t")[1:] if ":" in f)
        if fields.get("PN") in _ALIGNERS:
            entry = (
                f"{fields['PN']} {fields['VN']}"
                if fields.get("VN")
                else f"{fields['PN']} (no VN in @PG)"
            )
            if entry not in found:
                found.append(entry)
    if not found:
        return f"{NOT_READ}: no aligner @PG line ({', '.join(_ALIGNERS)}) in the BAM header"
    return "; ".join(found)


def stamp_tool_versions(vcf_path: str, bam_path: str, samtools: Optional[str] = None) -> List[str]:
    """
    Inserts the two lines before `#CHROM` in `vcf_path` (plain-text VCF),
    via a temporary file and an atomic replace, so a failure leaves the
    original untouched. Returns the lines inserted.
    """
    samtools = samtools if samtools is not None else shutil.which("samtools")
    stamps = [
        f"##samtoolsVersion={read_samtools_version(samtools)}",
        f"##alignerVersion={read_aligner_from_bam(samtools, bam_path)}",
    ]
    src = Path(vcf_path)
    tmp = src.with_name(src.name + ".stamping")
    inserted = False
    with src.open("r", newline="") as fin, tmp.open("w", newline="") as fout:
        for line in fin:
            if not inserted and line.startswith("#CHROM"):
                eol = "\r\n" if line.endswith("\r\n") else "\n"
                fout.write("".join(s + eol for s in stamps))
                inserted = True
            fout.write(line)
    if not inserted:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"no #CHROM header line in {vcf_path!r}; nothing stamped")
    os.replace(tmp, src)
    for s in stamps:
        logger.info("Stamped %s: %s", vcf_path, s)
    return stamps
