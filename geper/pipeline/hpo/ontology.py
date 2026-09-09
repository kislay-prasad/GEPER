"""
HPO ontology structure (term parent-child edges), for phenotype
*semantic similarity* -- distinct from `pipeline/hpo/provider.py`,
which only ever answers "what phenotype terms is this gene annotated
with" (a flat gene -> [term] list, no relationship between terms).

WHY THIS FILE EXISTS: HPO's official Gene-to-Phenotype annotation
download (`genes_to_phenotype.txt`, what `bootstrap.py`/`provider.py`
already load) carries no ontology structure at all -- each row is just
`(gene_symbol, hpo_id, hpo_name, ...)`, with no parent/child/`is_a`
information anywhere in the file. Confirmed by reading that file's own
column list (`ncbi_gene_id gene_symbol hpo_id hpo_name frequency
disease_id`) and every dataclass in `pipeline/hpo/models.py` -- none of
them carry a term's ancestors, and nothing elsewhere in this codebase
loads a separate ontology file either (searched for hp.obo/hp.json;
neither exists in this repo). A case-level phenotype-match score that
wants "shared ancestor terms carry partial credit" (not just exact-ID
overlap, which is all `ACMGRuleEngine._pp4` already does) therefore
needs a genuinely separate data source: HPO's own ontology release,
not the gene-annotation file.

This module is that source, self-provisioning exactly like
`pipeline/hpo/bootstrap.py` does for the gene-annotation file: fetch
HPO's official Obograph-JSON ontology release once
(https://purl.obolibrary.org/obo/hp.json -- the same purl.obolibrary.org
host and `hp/` namespace as the gene-annotation file, just the
ontology-structure release instead of the annotation release), cache
it to disk, refresh on a TTL. Deliberately optional and
never-required: `pipeline/case_prioritization.py`'s phenotype-match
scorer works correctly (falling back to exact-ID-only matching, no
partial credit) whether or not this ontology ever loads -- see that
module's docstring for the honest degrade path. This keeps the new
case-level ranking feature usable immediately (exact-match matching,
same overlap semantics PP4 already uses) without forcing a ~20MB
ontology download before it can do anything at all.

Obograph-JSON shape (HPO's own published format, not invented here):
    {"graphs": [{"nodes": [{"id": "http://purl.obolibrary.org/obo/HP_0001250", "lbl": "Seizure", ...}, ...],
                 "edges": [{"sub": "http://purl.obolibrary.org/obo/HP_0001250",
                            "pred": "is_a",
                            "obj": "http://purl.obolibrary.org/obo/HP_0012638"}, ...]}]}
Only `is_a` edges are read (the relationship "ancestor" partial-credit
scoring actually means); HPO's few other edge predicates
(`part_of`, etc.) are out of scope for this pass. IDs are converted
from the full PURL form to GEPER's own "HP:#######" convention (see
`_short_id`) so callers never need to know about the URI form.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from threading import Lock
from typing import Dict, FrozenSet, Optional, Set

import requests

from config import CONFIG
from pipeline.provenance import record_stale_fallback, write_dataset_provenance_sidecar
from utils.logger import get_logger

logger = get_logger(__name__)

_CACHE_FILENAME = "hp_ontology.json"
_HP_URI_PREFIX = "http://purl.obolibrary.org/obo/HP_"
_fetch_lock = Lock()


def _short_id(uri_or_id: str) -> Optional[str]:
    """'http://purl.obolibrary.org/obo/HP_0001250' -> 'HP:0001250'; passes an already-short 'HP:#######' id through unchanged; None for anything else (non-HPO terms the graph also references, e.g. upper ontology classes)."""
    if not uri_or_id:
        return None
    if uri_or_id.startswith("HP:"):
        return uri_or_id
    if uri_or_id.startswith(_HP_URI_PREFIX):
        return "HP:" + uri_or_id[len(_HP_URI_PREFIX) :]
    return None


def _cache_dir() -> str:
    return CONFIG.hpo.AUTO_FETCH_DIR or os.path.join(CONFIG.CACHE_DIR, "hpo")


def ontology_cache_path() -> str:
    """Where `ensure_ontology_file()` caches its download -- public for the same reason `hpo/bootstrap.py::genes_to_phenotype_cache_path` is."""
    return os.path.join(_cache_dir(), _CACHE_FILENAME)


def _is_fresh(path: str) -> bool:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    age_hours = (time.time() - os.path.getmtime(path)) / 3600.0
    return age_hours < CONFIG.hpo.AUTO_FETCH_TTL_HOURS


def _download(url: str, dest_path: str) -> bool:
    """Same atomic-write shape as `hpo/bootstrap.py::_download`; never raises."""
    try:
        response = requests.get(url, timeout=CONFIG.hpo.AUTO_FETCH_TIMEOUT_SECS)
        response.raise_for_status()
        if not response.content.strip():
            logger.warning(f"HPO ontology fetch from '{url}' returned an empty body; not caching.")
            return False
    except (requests.RequestException, ValueError) as exc:
        logger.warning(f"HPO ontology fetch from '{url}' failed: {exc}")
        return False

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(dest_path), prefix=".hpo_ontology_fetch_")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(response.content)
        os.replace(tmp_path, dest_path)
    except OSError as exc:
        logger.warning(f"Could not write HPO ontology cache to '{dest_path}': {exc}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False

    write_dataset_provenance_sidecar(dest_path, url, response_headers=response.headers)
    return True


def ensure_ontology_file() -> Optional[str]:
    """Path to a local copy of HPO's Obograph-JSON ontology release, fetching/refreshing it first if needed. None if unavailable (auto-fetch disabled, offline mode, or the fetch failed with no usable stale copy) -- exactly `hpo/bootstrap.py::ensure_genes_to_phenotype_file`'s contract, reused for this second, independent HPO download."""
    if not CONFIG.hpo.AUTO_FETCH_ENABLED or CONFIG.hpo.OFFLINE_MODE:
        return None

    dest_path = ontology_cache_path()
    if _is_fresh(dest_path):
        return dest_path

    with _fetch_lock:
        if _is_fresh(dest_path):
            return dest_path
        logger.info(
            f"Fetching HPO ontology structure from '{CONFIG.hpo.ONTOLOGY_DOWNLOAD_URL}' (cache miss or stale)..."
        )
        if _download(CONFIG.hpo.ONTOLOGY_DOWNLOAD_URL, dest_path):
            logger.info(f"Cached HPO ontology structure to '{dest_path}'.")
            return dest_path

    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        logger.warning(f"Using stale cached HPO ontology at '{dest_path}' after a failed refresh.")
        record_stale_fallback(source="HPO (ontology)", path=dest_path, reason="failed refresh")
        return dest_path
    return None


class HPOOntology:
    """
    Parent-child (`is_a`) structure over HPO terms, for ancestor-based
    partial-credit similarity in `pipeline/case_prioritization.py`.

    Two ways to construct one:
      - `HPOOntology.from_file(path)` -- parses a real Obograph-JSON
        file (what `ensure_ontology_file()` downloads).
      - `HPOOntology(graph_data=...)` -- takes an already-parsed
        Obograph-JSON dict directly, for tests that want a small,
        hand-built graph instead of the real ~19MB release (see
        `tests/test_case_prioritization.py`).

    `is_available` is False (not an exception) for every "no usable
    ontology" case -- missing file, fetch disabled, malformed JSON --
    so callers degrade to exact-match-only scoring rather than crash.
    """

    def __init__(self, graph_data: Optional[Dict] = None):
        self._parents: Dict[str, Set[str]] = {}
        self._ancestor_cache: Dict[str, FrozenSet[str]] = {}
        self._lock = Lock()
        self.is_available = False
        if graph_data is not None:
            self._parse(graph_data)

    @classmethod
    def from_file(cls, path: Optional[str]) -> "HPOOntology":
        """Never raises: a missing/unreadable/malformed file yields an ontology with `is_available=False`, not an exception -- this is an optional enhancement, not a required input (see this module's docstring)."""
        ontology = cls()
        if not path or not os.path.exists(path):
            return ontology
        try:
            with open(path, "r", encoding="utf-8") as fh:
                graph_data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                f"Could not read/parse HPO ontology file '{path}': {exc}; ontology-based partial credit unavailable this run."
            )
            return ontology
        ontology._parse(graph_data)
        return ontology

    def _parse(self, graph_data: Dict) -> None:
        try:
            graphs = graph_data.get("graphs") or []
            edge_count = 0
            for graph in graphs:
                for edge in graph.get("edges") or []:
                    if edge.get("pred") != "is_a":
                        continue
                    child = _short_id(edge.get("sub"))
                    parent = _short_id(edge.get("obj"))
                    if not child or not parent:
                        continue
                    self._parents.setdefault(child, set()).add(parent)
                    edge_count += 1
        except (AttributeError, TypeError) as exc:
            logger.warning(
                f"Malformed HPO ontology graph data ({exc}); ontology-based partial credit unavailable this run."
            )
            self._parents = {}
            return
        self.is_available = edge_count > 0
        if self.is_available:
            logger.info(
                f"Loaded HPO ontology structure: {edge_count} is_a edge(s) across {len(self._parents)} term(s)."
            )

    def ancestors(self, hpo_id: str) -> FrozenSet[str]:
        """
        Every transitive parent of `hpo_id` (via `is_a`), NOT including
        `hpo_id` itself -- callers that want "this term or an ancestor"
        add `hpo_id` to the result themselves (see
        `case_prioritization.py::_term_similarity`, which does exactly
        that so an exact match and an ancestor-only match share one
        Jaccard computation instead of two separate code paths).
        Empty frozenset for an unknown term or when the ontology never
        loaded -- never raises.
        """
        if not self.is_available or not hpo_id:
            return frozenset()
        with self._lock:
            cached = self._ancestor_cache.get(hpo_id)
            if cached is not None:
                return cached
            seen: Set[str] = set()
            frontier = list(self._parents.get(hpo_id, ()))
            while frontier:
                node = frontier.pop()
                if node in seen:
                    continue
                seen.add(node)
                frontier.extend(self._parents.get(node, ()))
            result = frozenset(seen)
            self._ancestor_cache[hpo_id] = result
            return result


_shared_ontology: Optional[HPOOntology] = None
_shared_ontology_lock = Lock()


def get_shared_ontology() -> HPOOntology:
    """
    Process-wide singleton, loaded (and its self-fetch attempted) at
    most once per process -- mirrors every other lazily-initialized
    shared resource in this codebase (e.g. `HPOLookup`'s own provider
    default). Callers that want a specific ontology instance instead
    (tests, or a caller with an explicit local file) should construct
    `HPOOntology` directly rather than going through this function.
    """
    global _shared_ontology
    if _shared_ontology is not None:
        return _shared_ontology
    with _shared_ontology_lock:
        if _shared_ontology is not None:
            return _shared_ontology
        path = CONFIG.hpo.ONTOLOGY_LOCAL_FILE or ensure_ontology_file()
        _shared_ontology = HPOOntology.from_file(path)
        return _shared_ontology
