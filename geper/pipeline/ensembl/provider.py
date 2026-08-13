"""
Ensembl gene/transcript-structure query provider.

Reads the local (deployer-provisioned or self-fetched, see
`pipeline/ensembl/bootstrap.py`) gene-symbol-indexed JSON-lines dataset,
indexed on first use into two structures built from the same records:

  - `_by_gene_symbol`: gene symbol -> transcript-structure dict, in the
    exact shape `pipeline/pvs1/utils.py::transcript_context_from_dict`
    expects. This is what `pipeline/pvs1/lookup.py::TranscriptLookup`
    now tries first, before falling back to a live Ensembl REST call.
  - `_by_chrom`: chrom -> a start-sorted list of (gene_start, gene_end,
    gene_symbol) tuples, a simple in-memory interval index (bisect +
    linear scan over same-chromosome candidates) answering "which
    protein-coding gene(s) overlap this genomic position" without a
    live Ensembl `overlap/region` call. This is what
    `pipeline/clingen/utils.py::resolve_gene_symbol_detail`'s Ensembl
    fallback now tries first (used only when a variant's VCF record
    carries no `GENE=` INFO field).

Both indexes come from the SAME cached dataset (one JSON-lines row per
protein-coding gene, carrying both its gene-body span and its chosen
canonical transcript's structure) -- there is only one bootstrap, not
two. See `config.py::EnsemblConfig`'s docstring for why an in-memory
index was chosen over a second on-disk structure: ~20-60k protein-coding
genes is small enough (comparable in scale to `pipeline/mane/`'s
dataset) to hold entirely in memory on the project's 8GB-RAM target,
and a bisect-based interval index needs no external dependency.

Deliberately no live-API fallback inside this provider itself (like
`pipeline/mane/provider.py`): each of this module's two callers already
owns its own existing live-Ensembl-REST fallback path, and this
provider's only job is to answer "does the cache have this" honestly --
`None` (dataset unavailable) or `[]`/`None` (a clean miss), never a
guess -- so the caller can decide how to fall through.
"""

from __future__ import annotations

import bisect
import json
import os
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from config import CONFIG
from utils.logger import get_logger

logger = get_logger(__name__)


def _normalize_gene_symbol(symbol: Optional[str]) -> str:
    return (symbol or "").strip().upper()


def _normalize_chrom(chrom: Optional[str]) -> str:
    """
    Reuses `hgvs_utils.py::_strip_chr` rather than a second, independent
    prefix-strip (round 14, B1): the previous local implementation here
    (`chrom[3:] if chrom.lower().startswith("chr") else chrom`) mapped
    both `"chrM"` and bare `"M"` to `"M"` -- neither of which matches
    Ensembl's own GTF/REST seqname for the mitochondrial contig, `"MT"`
    -- while `MT`-spelled input (Ensembl's own convention) happened to
    pass through unchanged and work by coincidence. `_strip_chr` already
    canonicalizes `M`/`mt`/`Mt` (bare or `chr`-prefixed) to `MT`
    correctly and is the normalizer `pipeline/hgvs_utils.py`'s own
    RefSeq-accession lookup already relies on; duplicating its logic
    here would create two normalizers free to drift apart on the next
    edge case, exactly what this fix exists to avoid for nuclear
    chromosome names too (`chr17`/`17`/`chrX`/`X`, all unaffected --
    `_strip_chr` reduces to the same prefix strip for every non-MT
    input).

    Imported inside the function, not at module level: a module-level
    `from pipeline.hgvs_utils import _strip_chr` creates a real import
    cycle -- `hgvs_utils` -> `pipeline.ps1_pm5.utils` -> (package
    `__init__`) -> `pipeline.ps1_pm5.decision` -> `pipeline.pvs1.models`
    -> (package `__init__`) -> `pipeline.pvs1.lookup` -> back to
    `pipeline.ensembl.provider.transcript_for_gene`, which doesn't exist
    yet on this still-initializing module -- confirmed by direct import
    (`ImportError: cannot import name 'transcript_for_gene' from
    partially initialized module`). That chain runs entirely through
    eager re-exports in `pipeline/pvs1/__init__.py` and
    `pipeline/ps1_pm5/__init__.py`, not through anything this fix
    touches, so restructuring those packages is out of scope here. By
    the time `_normalize_chrom` is actually CALLED (not merely
    imported), every module in that chain has already finished loading,
    so the deferred import resolves cleanly -- the same "defer to avoid
    forcing an eager import at module-load time" idiom this codebase
    already uses for optional heavy dependencies (e.g.
    `pipeline/models/spliceformer/loader.py`'s vendored-model import).
    """
    from pipeline.hgvs_utils import _strip_chr

    return _strip_chr((chrom or "").strip())


class LocalDatasetEnsemblProvider:
    """
    Reads GEPER's self-fetched (or deployer-provisioned) Ensembl
    GTF+CDS-derived dataset from disk, indexed by gene symbol and by
    genomic interval on first use. `auto_fetch=False` exists for tests
    that want a fully offline, deterministic provider (same reasoning as
    `LocalDatasetMANEProvider`/`LocalDatasetUniProtProvider`).
    """

    name = "local_dataset"

    def __init__(self, local_file_path: Optional[str] = None, auto_fetch: bool = True):
        self.local_file_path = local_file_path or CONFIG.ensembl.LOCAL_FILE or None
        self._auto_fetch = auto_fetch and local_file_path is None
        self._lock = Lock()
        self._loaded = False
        self._by_gene_symbol: Dict[str, Dict[str, Any]] = {}
        # chrom -> sorted list of (gene_start, gene_end, gene_symbol), sorted by gene_start
        self._by_chrom: Dict[str, List[Tuple[int, int, str]]] = {}
        self._chrom_starts: Dict[str, List[int]] = {}  # parallel start-only lists, for bisect

    def is_available(self) -> bool:
        if not CONFIG.ensembl.ENABLED:
            return False
        if self.local_file_path:
            return True
        return self._auto_fetch and CONFIG.ensembl.AUTO_FETCH_ENABLED and not CONFIG.ensembl.OFFLINE_MODE

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:  # re-check inside the lock
                return
            path = self.local_file_path
            if self._auto_fetch and CONFIG.ensembl.AUTO_FETCH_ENABLED and not CONFIG.ensembl.OFFLINE_MODE and not path:
                from pipeline.ensembl import bootstrap as ensembl_bootstrap

                path = ensembl_bootstrap.ensure_dataset_file()
            if path:
                self._load(path)
            self._loaded = True

    def _load(self, path: str) -> None:
        if not os.path.exists(path):
            logger.warning(f"Ensembl local dataset file not found at '{path}'; Ensembl-cache lookups will be empty.")
            return

        by_chrom_raw: Dict[str, List[Tuple[int, int, str]]] = {}
        count = 0
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    gene = _normalize_gene_symbol(record.get("gene_symbol"))
                    if not gene:
                        continue
                    transcript = record.get("transcript")
                    if transcript:
                        self._by_gene_symbol[gene] = transcript

                    # Indexed for `genes_overlapping` regardless of
                    # whether a usable transcript was chosen -- see
                    # `pipeline/ensembl/bootstrap.py::_choose_and_serialize`'s
                    # docstring for why a gene's span must survive even
                    # when it has no `transcript` entry.
                    chrom = _normalize_chrom(record.get("chrom"))
                    start, end = record.get("gene_start"), record.get("gene_end")
                    if chrom and isinstance(start, int) and isinstance(end, int):
                        by_chrom_raw.setdefault(chrom, []).append((start, end, gene))
                    count += 1
        except OSError as exc:
            logger.warning(f"Could not read local Ensembl dataset '{path}': {exc}")
            return

        for chrom, entries in by_chrom_raw.items():
            entries.sort(key=lambda e: e[0])
            self._by_chrom[chrom] = entries
            self._chrom_starts[chrom] = [e[0] for e in entries]

        logger.info(f"Loaded {count} Ensembl gene->transcript structure(s) from '{path}'.")

    # ------------------------------------------------------------------
    # TranscriptLookup's query: gene symbol -> transcript structure
    # ------------------------------------------------------------------

    def transcript_for_gene(self, gene_symbol: str) -> Optional[Dict[str, Any]]:
        """The cached canonical transcript structure for `gene_symbol`, or
        None if the cache is unavailable or has no entry for this gene."""
        if not self.is_available():
            return None
        self._ensure_loaded()
        return self._by_gene_symbol.get(_normalize_gene_symbol(gene_symbol))

    # ------------------------------------------------------------------
    # resolve_gene_symbol_detail's query: position -> overlapping genes
    # ------------------------------------------------------------------

    def genes_overlapping(self, chrom: str, pos: int) -> Optional[List[Dict[str, Any]]]:
        """
        Protein-coding genes whose gene-body span (start-end, inclusive)
        contains `pos` on `chrom`, in the same feature-list shape
        `pipeline/clingen/utils.py::_fetch_overlapping_genes`'s live
        Ensembl `overlap/region` response already provides
        (`{"external_name": symbol, "biotype": "protein_coding"}`) --
        so `resolve_gene_symbol_detail`'s existing downstream filtering/
        disambiguation code needs zero changes to consume either source.

        Returns `None` (distinct from an empty list) only when the
        dataset itself is unavailable this run -- a clean "no gene
        overlaps this position" is `[]`, and is just as authoritative as
        a live overlap/region answer would be, since this cache carries
        every protein-coding gene from the same annotation Ensembl's
        live API itself serves (unlike `pipeline/uniprot/`'s reference-
        proteome cache, which does not claim 100% gene coverage and so
        must fall through to the live API even on a clean local miss --
        see `CompositeUniProtProvider`'s docstring for that contrast).
        """
        if not self.is_available():
            return None
        self._ensure_loaded()
        chrom = _normalize_chrom(chrom)
        entries = self._by_chrom.get(chrom)
        if not entries:
            return []

        starts = self._chrom_starts[chrom]
        # Every entry at index < the insertion point for `pos` has
        # start <= pos; entries further to the right cannot overlap
        # (their start is already > pos). This does NOT prune on `end`,
        # so genes upstream of `pos` whose own end already fell short of
        # it are still scanned and rejected below -- acceptable here
        # since a single chromosome's gene count is small (low
        # thousands at most) and this runs once per unresolved variant,
        # not in a hot inner loop.
        cutoff = bisect.bisect_right(starts, pos)
        matches = [
            {"external_name": symbol, "biotype": "protein_coding"}
            for start, end, symbol in entries[:cutoff]
            if start <= pos <= end
        ]
        return matches


# ---------------------------------------------------------------------------
# Default, process-wide instance -- lazily constructed on first use so
# importing this module never triggers a fetch, mirroring
# `pipeline/mane/provider.py`'s identical singleton pattern.
# ---------------------------------------------------------------------------

_default_provider: Optional[LocalDatasetEnsemblProvider] = None
_default_provider_lock = Lock()


def _get_default_provider() -> LocalDatasetEnsemblProvider:
    global _default_provider
    if _default_provider is None:
        with _default_provider_lock:
            if _default_provider is None:
                _default_provider = LocalDatasetEnsemblProvider()
    return _default_provider


def transcript_for_gene(gene_symbol: str) -> Optional[Dict[str, Any]]:
    """Module-level convenience wrapper over the default provider instance."""
    return _get_default_provider().transcript_for_gene(gene_symbol)


def genes_overlapping(chrom: str, pos: int) -> Optional[List[Dict[str, Any]]]:
    """Module-level convenience wrapper over the default provider instance."""
    return _get_default_provider().genes_overlapping(chrom, pos)
