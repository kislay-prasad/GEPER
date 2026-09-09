"""
Self-provisioning of Ensembl's official human GTF (gene -> transcript ->
exon/CDS coordinates) and CDS FASTA (per-transcript coding sequence)
downloads, converted into a gene-symbol-indexed JSON-lines dataset
`pipeline/ensembl/provider.py::LocalDatasetEnsemblProvider` reads.

Same "download once, query locally, refresh on a TTL" shape as
`pipeline/mane/bootstrap.py` and `pipeline/uniprot/bootstrap.py` -- see
`config.py::EnsemblConfig`'s docstring for the full live-verified URL
shapes, file formats, and the GRCh38-only scope decision. Two things
make this source different from either of those:

  1. Two files are fetched and cross-referenced (GTF for structure, CDS
     FASTA for sequence -- Ensembl publishes them separately), not one.
  2. The GTF is ~4.7GB decompressed (~11.2M lines) -- both files are
     streamed and parsed line-by-line via a stdlib `gzip.GzipFile`
     wrapped directly around the HTTP response's raw socket
     (`_gunzip_stream_lines`), never buffering the decompressed text in
     memory (same discipline `pipeline/uniprot/bootstrap.py` documents
     for its own large flat file). Only one transcript's structure is
     kept per gene (see `_choose_and_serialize`), so the cached JSON
     lines dataset itself ends up small even though the raw downloads
     are not.

Never raises: a failed fetch/parse is logged and reported as
"unavailable" so `TranscriptLookup`/`resolve_gene_symbol_detail` simply
fall back to their existing live-Ensembl-REST paths, exactly as they did
before this module existed.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import tempfile
import time
from threading import Lock
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import requests

from config import CONFIG
from pipeline.provenance import record_stale_fallback, write_dataset_provenance_sidecar
from utils.logger import get_logger

logger = get_logger(__name__)

_DATASET_FILENAME = "ensembl_transcripts.jsonl"

# One lock (there is only ever this one dataset) so two threads racing
# to bootstrap on first use don't both fetch/parse concurrently.
_fetch_lock = Lock()

# Matches `<a href="Homo_sapiens.GRCh38.116.gtf.gz">` specifically --
# not the sibling `.abinitio.gtf.gz`/`.chr.gtf.gz`/
# `.chr_patch_hapl_scaff.gtf.gz` files in the same directory, since none
# of those have a bare `.<version>.gtf.gz` suffix (there is always an
# extra dotted segment between the version number and `.gtf.gz` in
# their names, which this pattern's literal `\.gtf\.gz"` anchor does
# not allow).
_GTF_FILENAME_RE = re.compile(r'href="(Homo_sapiens\.GRCh38\.(\d+)\.gtf\.gz)"')

_CODING_BIOTYPE = "protein_coding"

# Ensembl GTF attribute strings are semicolon-separated `key "value"`
# pairs (see `pipeline/mane/bootstrap.py`'s docstring for the sibling
# NCBI-format precedent) -- a key can repeat (`tag` most notably), so
# this collects every match rather than the first.
_ATTR_RE = re.compile(r'(\w+) "([^"]*)"')

_FASTA_HEADER_ID_RE = re.compile(r"^>(\S+)")


def _cache_dir() -> str:
    return CONFIG.ensembl.AUTO_FETCH_DIR or os.path.join(CONFIG.CACHE_DIR, "ensembl")


def dataset_cache_path() -> str:
    """Where `ensure_dataset_file()` caches its converted JSON-lines
    dataset -- public so `pipeline/provenance.py`/`pipeline/orchestrator.py`
    can read its provenance sidecar without triggering a fetch."""
    return os.path.join(_cache_dir(), _DATASET_FILENAME)


def _is_fresh(path: str) -> bool:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    age_hours = (time.time() - os.path.getmtime(path)) / 3600.0
    return age_hours < CONFIG.ensembl.AUTO_FETCH_TTL_HOURS


def _discover_current_gtf_url(index_url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Fetches `index_url` (Ensembl's stable `current/gtf/homo_sapiens/`
    directory listing) and regex-parses it for the exact current GTF
    filename and the Ensembl release version embedded in it. Returns
    `(gtf_file_url, version_string)`, both `None` on any failure --
    never raises. Mirrors
    `pipeline/mane/bootstrap.py::_discover_current_summary_url`.
    """
    try:
        response = requests.get(index_url, timeout=CONFIG.ensembl.AUTO_FETCH_TIMEOUT_SECS)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning(f"Could not fetch Ensembl GTF directory index '{index_url}': {exc}")
        return None, None

    match = _GTF_FILENAME_RE.search(response.text)
    if not match:
        logger.warning(
            f"Ensembl GTF directory index at '{index_url}' did not contain a recognizable "
            "'Homo_sapiens.GRCh38.<N>.gtf.gz' filename; Ensembl may have changed its naming scheme."
        )
        return None, None

    filename, version = match.group(1), match.group(2)
    return index_url.rstrip("/") + "/" + filename, f"Ensembl release {version}"


def _gunzip_stream_lines(response: requests.Response) -> Iterable[str]:
    """
    Text-line iterator over a streaming HTTP response whose body is raw
    gzip bytes (confirmed live: Ensembl serves both the GTF and CDS
    FASTA as `Content-Type: application/x-gzip`, not a transparently
    `requests`-decoded `Content-Encoding: gzip`) -- never materializes
    the decompressed text in memory beyond one line at a time.
    """
    raw = response.raw
    raw.decode_content = False
    with gzip.GzipFile(fileobj=raw) as gz:
        yield from io.TextIOWrapper(gz, encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# GTF parsing
# ---------------------------------------------------------------------------


def _parse_attributes(attr_str: str) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {}
    for key, value in _ATTR_RE.findall(attr_str):
        result.setdefault(key, []).append(value)
    return result


class _GeneAccumulator:
    """One gene's in-progress state while streaming through its block of GTF lines."""

    __slots__ = ("gene_symbol", "chrom", "strand", "gene_start", "gene_end", "transcripts")

    def __init__(self, gene_symbol: str, chrom: str, strand: str, start: int, end: int):
        self.gene_symbol = gene_symbol
        self.chrom = chrom
        self.strand = strand
        self.gene_start = start
        self.gene_end = end
        self.transcripts: Dict[str, Dict[str, Any]] = {}


def _choose_and_serialize(gene: _GeneAccumulator) -> Optional[Dict[str, Any]]:
    """
    Picks the same transcript
    `pipeline/pvs1/lookup.py::TranscriptLookup._choose_transcript` would
    pick from a live Ensembl REST response for this gene (the
    protein-coding transcript tagged `Ensembl_canonical`, falling back
    to the protein-coding transcript with the longest CDS when none
    carries that tag), then serializes it into the exact dict shape
    `pipeline/pvs1/utils.py::transcript_context_from_dict` expects.

    `cds_genomic_start`/`cds_genomic_end` are widened to include the
    stop codon's 3 genomic bases (unioned with the transcript's own
    `stop_codon` GTF feature, when one was annotated) -- see this
    module's docstring and `config.py::EnsemblConfig`'s for why: GTF's
    `CDS` feature itself excludes the stop codon, but
    `TranscriptContext`'s coordinate arithmetic was built against
    Ensembl REST's `Translation.start/end`, which is stop-inclusive.
    Getting this wrong would silently disable stop-loss detection for
    every GTF-cache-sourced transcript.

    A gene with no usable protein-coding transcript (no exons, or no
    CDS at all -- rare, but a `protein_coding`-biotype gene is not
    guaranteed to have a transcript that itself qualifies) still gets a
    record here, with `"transcript": None`: this dataset also backs the
    gene-*overlap* index (`pipeline/ensembl/provider.py`'s `_by_chrom`),
    and dropping such a gene's span entirely would silently make
    `genes_overlapping` incomplete relative to what a live Ensembl
    `overlap/region` call would report for the same position --
    `LocalDatasetEnsemblProvider.transcript_for_gene` simply has nothing
    to return for it, exactly like a gene truly absent from the cache.
    """
    candidates = [t for t in gene.transcripts.values() if t["biotype"] == _CODING_BIOTYPE and t["exons"] and t["cds"]]
    if not candidates:
        return {
            "gene_symbol": gene.gene_symbol,
            "chrom": gene.chrom,
            "gene_start": gene.gene_start,
            "gene_end": gene.gene_end,
            "strand": 1 if gene.strand != "-" else -1,
            "transcript": None,
        }

    canonical = [t for t in candidates if t["is_canonical"]]
    chosen = canonical[0] if canonical else max(candidates, key=lambda t: sum(e - s + 1 for s, e in t["cds"]))

    cds_bounds = list(chosen["cds"]) + list(chosen["stop"])
    cds_start = min(s for s, e in cds_bounds)
    cds_end = max(e for s, e in cds_bounds)
    cds_len = sum(e - s + 1 for s, e in cds_bounds)
    # `chosen["stop"]` non-empty means the stop codon's 3 bases are
    # already folded into `cds_len` above, matching REST's convention
    # (aa count = (stop-inclusive CDS length // 3) - 1). A transcript
    # annotated `cds_end_NF`/`mRNA_end_NF` (incomplete 3' CDS -- no
    # `stop_codon` feature at all) has no stop bases to subtract; such
    # transcripts are rare among canonical/MANE Select picks specifically
    # (the canonical model requires a defined start and stop), so this
    # is a disclosed, narrow edge case rather than a routine one.
    protein_length = (cds_len // 3) - 1 if chosen["stop"] else cds_len // 3

    strand = 1 if gene.strand != "-" else -1
    return {
        "gene_symbol": gene.gene_symbol,
        "chrom": gene.chrom,
        "gene_start": gene.gene_start,
        "gene_end": gene.gene_end,
        "strand": strand,
        "transcript": {
            "transcript_id": chosen["transcript_id"],
            "gene_symbol": gene.gene_symbol,
            "chrom": gene.chrom,
            "strand": strand,
            "exons": [{"start": s, "end": e} for s, e in chosen["exons"]],
            "cds_genomic_start": cds_start,
            "cds_genomic_end": cds_end,
            "protein_length": protein_length,
            "is_mane_select": chosen["is_mane_select"],
            "is_canonical": chosen["is_canonical"],
            "source": "ensembl_gtf_cache",
            "cds_sequence": None,  # filled in from the CDS FASTA pass, see `_build_dataset`
        },
    }


def _parse_gtf_lines(lines: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """
    Streams Ensembl's GTF line by line and returns one entry per
    protein-coding gene, keyed by (upper-cased) gene symbol -- see
    `_choose_and_serialize` for the per-gene shape. Relies on Ensembl's
    own file ordering (confirmed live: every gene's `gene`/`transcript`/
    `exon`/`CDS`/`stop_codon` lines form one contiguous block before the
    next gene's block begins) to flush one gene's accumulated state as
    soon as the next `gene` line is seen, rather than buffering the
    whole file.

    Never raises on a malformed line -- skips it and continues, since
    one bad line out of ~11 million lines must not abort the whole
    bootstrap.
    """
    genes: Dict[str, Dict[str, Any]] = {}
    current: Optional[_GeneAccumulator] = None

    def flush() -> None:
        if current is None:
            return
        entry = _choose_and_serialize(current)
        if entry is not None:
            genes[entry["gene_symbol"]] = entry

    for line in lines:
        if not line or line.startswith("#"):
            continue
        fields = line.rstrip("\n").split("\t")
        if len(fields) != 9:
            continue
        chrom, _source, feature, start, end, _score, strand, _frame, attr_str = fields
        try:
            start_i, end_i = int(start), int(end)
        except ValueError:
            continue
        attrs = _parse_attributes(attr_str)

        if feature == "gene":
            flush()
            gene_symbol = (attrs.get("gene_name") or [None])[0]
            biotype = (attrs.get("gene_biotype") or [None])[0]
            if not gene_symbol or biotype != _CODING_BIOTYPE:
                current = None
                continue
            current = _GeneAccumulator(gene_symbol.strip().upper(), chrom, strand, start_i, end_i)
            continue

        if current is None:
            continue  # inside a non-protein-coding gene's block, or before the first gene line

        transcript_id = (attrs.get("transcript_id") or [None])[0]
        if not transcript_id:
            continue

        if feature == "transcript":
            tags = attrs.get("tag") or []
            current.transcripts[transcript_id] = {
                "transcript_id": transcript_id,
                "biotype": (attrs.get("transcript_biotype") or [None])[0],
                "is_canonical": "Ensembl_canonical" in tags,
                "is_mane_select": "MANE_Select" in tags,
                "exons": [],
                "cds": [],
                "stop": [],
            }
            continue

        transcript = current.transcripts.get(transcript_id)
        if transcript is None:
            continue  # a feature line for a transcript whose own `transcript` line wasn't kept

        if feature == "exon":
            transcript["exons"].append((start_i, end_i))
        elif feature == "CDS":
            transcript["cds"].append((start_i, end_i))
        elif feature == "stop_codon":
            transcript["stop"].append((start_i, end_i))

    flush()
    return genes


# ---------------------------------------------------------------------------
# CDS FASTA parsing
# ---------------------------------------------------------------------------


def _parse_cds_fasta_lines(lines: Iterable[str], wanted_bare_transcript_ids: Set[str]) -> Dict[str, str]:
    """
    Streams Ensembl's CDS FASTA and returns `{bare_transcript_id: cds_sequence}`
    for only the transcript IDs in `wanted_bare_transcript_ids` -- every
    other record is skipped without ever holding its sequence in memory,
    since the full file covers every transcript of every gene (~250k
    records) while this integration only needs the ~20k genes' single
    chosen canonical transcript each.

    Confirmed live (2026-08-08, BRCA1's `ENST00000357654`): the FASTA
    sequence includes the trailing in-frame stop codon (5592nt = 1864
    codons = 1863 aa + stop), matching the stop-inclusive
    `cds_genomic_start`/`cds_genomic_end` convention
    `_choose_and_serialize` reconstructs from the GTF.
    """
    sequences: Dict[str, str] = {}
    current_id: Optional[str] = None
    chunks: List[str] = []

    def flush() -> None:
        if current_id and chunks:
            sequences[current_id] = "".join(chunks).upper()

    for line in lines:
        line = line.rstrip("\n")
        if not line:
            continue
        if line.startswith(">"):
            flush()
            match = _FASTA_HEADER_ID_RE.match(line)
            versioned_id = match.group(1) if match else ""
            bare_id = versioned_id.split(".")[0]
            current_id = bare_id if bare_id in wanted_bare_transcript_ids else None
            chunks = []
            continue
        if current_id is not None:
            chunks.append(line)

    flush()
    return sequences


# ---------------------------------------------------------------------------
# Fetch orchestration
# ---------------------------------------------------------------------------


def _build_dataset(gtf_url: str, cds_url: str, dest_path: str, version: Optional[str]) -> bool:
    """
    Streams and parses both source files, merges the CDS sequences into
    the GTF-derived transcript structures, and writes the result as
    JSON-lines -- atomically (temp file + rename), same pattern as
    every other bootstrap module in this codebase. Never raises.

    A CDS FASTA failure degrades gracefully (the transcript structure is
    still cached, just without `cds_sequence` -- PVS1 already falls back
    to the protein-translation window for substitutions when no CDS
    sequence is available, see `pipeline/pvs1/lookup.py::_fetch_cds_sequence`'s
    equivalent live-API caveat); a GTF failure aborts the whole build,
    since there is nothing useful to cache without it.
    """
    timeout = CONFIG.ensembl.AUTO_FETCH_TIMEOUT_SECS
    try:
        gtf_response = requests.get(gtf_url, stream=True, timeout=timeout)
        gtf_response.raise_for_status()
        genes = _parse_gtf_lines(_gunzip_stream_lines(gtf_response))
    except (requests.RequestException, OSError) as exc:
        logger.warning(f"Ensembl GTF download/parse from '{gtf_url}' failed: {exc}")
        return False

    if not genes:
        logger.warning(f"Ensembl GTF fetch from '{gtf_url}' produced zero usable gene entries; not caching.")
        return False

    wanted_ids = {entry["transcript"]["transcript_id"] for entry in genes.values() if entry["transcript"]}

    try:
        cds_response = requests.get(cds_url, stream=True, timeout=timeout)
        cds_response.raise_for_status()
        sequences = _parse_cds_fasta_lines(_gunzip_stream_lines(cds_response), wanted_ids)
    except (requests.RequestException, OSError) as exc:
        logger.warning(
            f"Ensembl CDS FASTA download/parse from '{cds_url}' failed: {exc}. Caching transcript "
            "structure without CDS sequences (CDS-frame substitution calls will be unavailable for "
            "these genes until the next successful refresh)."
        )
        sequences = {}

    for entry in genes.values():
        if entry["transcript"]:
            entry["transcript"]["cds_sequence"] = sequences.get(entry["transcript"]["transcript_id"])

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(dest_path), prefix=".ensembl_fetch_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            for gene_symbol, entry in sorted(genes.items()):
                fh.write(json.dumps(entry) + "\n")
        os.replace(tmp_path, dest_path)
    except OSError as exc:
        logger.warning(f"Could not write Ensembl local dataset cache to '{dest_path}': {exc}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False

    with_sequence = sum(1 for e in genes.values() if e["transcript"] and e["transcript"]["cds_sequence"])
    write_dataset_provenance_sidecar(dest_path, gtf_url, version=version, compute_hash_from_file=True)
    logger.info(
        f"Cached {len(genes)} Ensembl gene->transcript structure(s) ({with_sequence} with CDS sequence, "
        f"{version or 'version unknown'}) to '{dest_path}'."
    )
    return True


def ensure_dataset_file() -> Optional[str]:
    """Path to a local, gene-symbol-indexed JSON-lines Ensembl transcript-structure
    dataset, fetching/refreshing it first if needed. None if unavailable."""
    if not CONFIG.ensembl.AUTO_FETCH_ENABLED or CONFIG.ensembl.OFFLINE_MODE:
        return None

    dest_path = dataset_cache_path()
    if _is_fresh(dest_path):
        return dest_path

    with _fetch_lock:
        if _is_fresh(dest_path):  # re-check inside the lock
            return dest_path
        gtf_url, version = _discover_current_gtf_url(CONFIG.ensembl.GTF_INDEX_URL)
        if gtf_url:
            logger.info(f"Fetching Ensembl GTF from '{gtf_url}' and CDS FASTA (cache miss or stale)...")
            if _build_dataset(gtf_url, CONFIG.ensembl.CDS_FASTA_URL, dest_path, version):
                return dest_path

    # Fetch failed -- an existing stale copy is still better than
    # nothing (Ensembl releases roughly every 2-3 months; a stale file
    # is far more useful than falling back to a live per-gene call for
    # every single lookup this run).
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        logger.warning(f"Using stale cached Ensembl dataset at '{dest_path}' after a failed refresh.")
        record_stale_fallback(source="Ensembl (GTF+CDS gene/transcript cache)", path=dest_path, reason="failed refresh")
        return dest_path
    return None
