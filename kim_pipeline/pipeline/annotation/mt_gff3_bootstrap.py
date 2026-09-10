"""
pipeline/annotation/mt_gff3_bootstrap.py
────────────────────────────────────────
Auto-fetch bootstrap for a small, mitochondrial-genome-only GFF3
annotation source, for deployments where `rna_analysis.refseq_gff`
has not been configured with a full-genome GRCh38 GFF3 yet.

Why this exists: without ANY GFF3 configured, `AnnotationStage`
degrades to genomic (g.) coordinates only for every variant — no
gene, transcript, HGVS c./p., or consequence — even for the
mitochondrial test dataset shipped in `test_data/`, whose reference
(`NC_012920.1`, the rCRS) has a tiny, single-contig annotation. This
module fetches *just that one record's* GFF3 from NCBI Entrez eutils
(`efetch`, ~tens of KB) — never the ~1.5 GB whole-genome RefSeq GFF3 —
so gene/transcript/HGVS/consequence annotation works out of the box
for MT variants (e.g. the m.3243A>G validation variant) without
requiring any manual download.

Scope and limits (important — read before enabling):
  * This ONLY annotates the mitochondrial contig (MT / chrM /
    NC_012920.1). Nuclear-genome variants are completely unaffected
    and still require `rna_analysis.refseq_gff` to be set to a full
    GRCh38 GFF3 for gene/transcript/HGVS annotation — this bootstrap
    is not a substitute for that, and never claims to be.
  * Disabled by default (`rna_analysis.auto_fetch_mt_gff3: false` /
    unset). Opt in explicitly via config or the
    `GEPER_AUTO_FETCH_MT_GFF3=1` environment variable. This keeps
    every existing test and offline/air-gapped deployment byte-for-
    byte unchanged (see `tests/test_fix_regressions.py::
    test_vcf_command_does_not_crash_on_default_config`, which
    exercises `AnnotationStage(cfg={})` and must never make a network
    call).
  * Uses `pipeline.utils.http._api_get`, the same retry/backoff
    helper every other external lookup in this project uses (NCBI
    eutils is also ClinVar's provider — same house style).
  * Cached to disk after the first successful fetch (atomic
    write-then-rename, same pattern as `pipeline/utils/reference_cache.py`'s
    BWA index publishing) so a fresh Colab session reuses the cached
    file instead of re-fetching, *provided* `gff3_cache_dir` is
    pointed at a location that survives a session restart (e.g. a
    Google Drive mount) — exactly the same caveat that already
    applies to `alignment.index_dir`.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Optional, Tuple

from pipeline.utils.http import _api_get

logger = logging.getLogger("geper.pipeline.annotation.mt_gff3_bootstrap")

# The rCRS / human mitochondrial genome RefSeq accession — matches
# `_REFSEQ_TO_CHR["NC_012920.1"] == "chrMT"` in gff_index.py, and is
# the same accession `test_data/README_TEST_DATASET.md` documents as
# the source of `test_data/reference.fasta`.
DEFAULT_MT_ACCESSION = "NC_012920.1"

_EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"

_ENV_ENABLE = "GEPER_AUTO_FETCH_MT_GFF3"
_ENV_CACHE_DIR = "GEPER_GFF3_CACHE_DIR"


def _default_cache_dir() -> str:
    return str(Path.home() / ".cache" / "geper" / "gff3")


def _cached_path(cache_dir: str, accession: str) -> Path:
    safe_name = accession.replace("/", "_")
    return Path(cache_dir) / f"{safe_name}.gff3"


def fetch_mt_gff3(
    cache_dir: Optional[str] = None,
    accession: str = DEFAULT_MT_ACCESSION,
    timeout: int = 30,
    max_retries: int = 3,
    force: bool = False,
    ncbi_api_key: Optional[str] = None,
) -> Optional[str]:
    """Return a local path to a GFF3 file for `accession`, fetching it
    from NCBI eutils if not already cached.

    Never raises: on any failure (network, disk, malformed response)
    logs a warning and returns None, so callers can fall back to the
    existing degraded (no-annotation) mode exactly as if this
    function didn't exist.
    """
    cache_dir = cache_dir or os.environ.get(_ENV_CACHE_DIR) or _default_cache_dir()
    dest = _cached_path(cache_dir, accession)

    if dest.exists() and dest.stat().st_size > 0 and not force:
        logger.info("Using cached MT GFF3 bootstrap annotation at '%s'.", dest)
        return str(dest)

    params = {
        "db": "nuccore",
        "id": accession,
        "rettype": "gff3",
        "retmode": "text",
    }
    api_key = ncbi_api_key or os.environ.get("NCBI_API_KEY")
    if api_key:
        params["api_key"] = api_key

    logger.info(
        "No cached MT GFF3 bootstrap annotation found — fetching '%s' from NCBI eutils "
        "(this is a small, single-contig download, not the full GRCh38 GFF3).",
        accession,
    )
    resp = _api_get(
        _EUTILS_BASE + "efetch.fcgi",
        params=params,
        timeout=timeout,
        max_retries=max_retries,
        logger=logger,
    )

    if resp is None or resp.status_code >= 400:
        logger.warning(
            "MT GFF3 bootstrap fetch for '%s' failed (%s) — falling back to degraded "
            "(no gene/transcript/HGVS) annotation mode.",
            accession,
            getattr(resp, "status_code", "no response"),
        )
        return None

    text = resp.text or ""
    if "##gff-version" not in text or "\tgene\t" not in text:
        logger.warning(
            "MT GFF3 bootstrap fetch for '%s' returned an unexpected/empty payload — "
            "falling back to degraded annotation mode.",
            accession,
        )
        return None

    try:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        # Atomic write-then-rename (same pattern as reference_cache.py's
        # index publishing) so a process killed mid-write can never
        # leave a corrupt, non-empty file that a later run mistakes
        # for a valid cache hit.
        fd, tmp_path = tempfile.mkstemp(dir=cache_dir, prefix=".gff3_dl_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp_path, dest)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as exc:
        logger.warning(
            "Could not cache MT GFF3 bootstrap annotation to disk (%s) — continuing "
            "with the in-memory result for this run only.",
            exc,
        )
        # Even without a writable cache dir, we still fetched valid
        # content this run — write to a throwaway temp file so the
        # rest of the pipeline (which expects a path) still works.
        fallback_fd, fallback_path = tempfile.mkstemp(prefix="geper_mt_gff3_", suffix=".gff3")
        with os.fdopen(fallback_fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        return fallback_path

    logger.info("Cached MT GFF3 bootstrap annotation to '%s'.", dest)
    return str(dest)


def resolve_gff3_source(cfg: Optional[dict]) -> Tuple[Optional[str], str]:
    """Single source of truth for "what GFF3 file (if any) should the
    annotation stage use, and why" — used by both `AnnotationStage`
    (to actually load it) and `pipeline.orchestration.shared` (to
    explain, per-variant, why gene annotation is unavailable when it
    is). Keeping this in one place avoids the two ever disagreeing
    (the exact bug class this project's own `shared.py` docstring
    warns about for ACMG/ClinVar/gnomAD logic).

    Returns:
        (gff_path_or_None, provenance) where provenance is one of:
          "configured"    — an explicit rna_analysis.refseq_gff / annotation.refseq_gff was set
          "mt_bootstrap"  — no explicit path was set, but the MT-only auto-fetch
                             succeeded and is in use (mitochondrial variants only)
          "unavailable"   — no explicit path, and either auto-fetch is disabled or it failed
    """
    cfg = cfg or {}
    ann_cfg = cfg.get("annotation", {}) or {}
    rna_cfg = cfg.get("rna_analysis", {}) or {}

    explicit_path = ann_cfg.get("refseq_gff") or rna_cfg.get("refseq_gff") or None
    if explicit_path:
        return explicit_path, "configured"

    auto_fetch_enabled = bool(
        rna_cfg.get("auto_fetch_mt_gff3")
        or ann_cfg.get("auto_fetch_mt_gff3")
        or os.environ.get(_ENV_ENABLE, "").strip().lower() in ("1", "true", "yes", "on")
    )
    if not auto_fetch_enabled:
        return None, "unavailable"

    cache_dir = rna_cfg.get("gff3_cache_dir") or ann_cfg.get("gff3_cache_dir") or None
    fetched = fetch_mt_gff3(cache_dir=cache_dir)
    if fetched:
        return fetched, "mt_bootstrap"
    return None, "unavailable"
