"""
pipeline/annotation/gff_index.py
──────────────────────────────────
GFF3 annotation parser and chromosome-position → gene/transcript resolver.

Supports:
  * Plain GFF3 (``*.gff3``, ``*.gff``)
  * Gzip-compressed GFF3 (``*.gz``)
  * RefSeq chromosome accession aliases (``NC_000001.11`` ↔ ``chr1`` / ``1``)

The index is built once from the GFF3 file and held in memory as sorted
arrays per chromosome, enabling O(log n) binary-search lookups.  No
third-party interval-tree library is required — the standard library
``bisect`` module is used.

Usage::

    idx = GffIndex.from_file("/data/refseq/GRCh38_latest_genomic.gff.gz")
    gene, transcript = idx.lookup("chr17", 43057051)
    # → ("BRCA1", "NM_007294.4") or (None, None) if not found
"""

from __future__ import annotations

import bisect
import gzip
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("geper.pipeline.annotation.gff_index")

# ─── RefSeq chromosome name map ───────────────────────────────────────────────
# Maps RefSeq accessions (NC_xxxxxxx.x) → UCSC-style chr names.
# Only the 24 primary chromosomes for GRCh38 are listed; unrecognised
# accessions fall back to the bare accession string so lookup still works
# as long as the VCF uses the same naming as the GFF3.
_REFSEQ_TO_CHR: Dict[str, str] = {
    "NC_000001.11": "chr1",
    "NC_000002.12": "chr2",
    "NC_000003.12": "chr3",
    "NC_000004.12": "chr4",
    "NC_000005.10": "chr5",
    "NC_000006.12": "chr6",
    "NC_000007.14": "chr7",
    "NC_000008.11": "chr8",
    "NC_000009.12": "chr9",
    "NC_000010.11": "chr10",
    "NC_000011.10": "chr11",
    "NC_000012.12": "chr12",
    "NC_000013.11": "chr13",
    "NC_000014.9": "chr14",
    "NC_000015.10": "chr15",
    "NC_000016.10": "chr16",
    "NC_000017.11": "chr17",
    "NC_000018.10": "chr18",
    "NC_000019.10": "chr19",
    "NC_000020.11": "chr20",
    "NC_000021.9": "chr21",
    "NC_000022.11": "chr22",
    "NC_000023.11": "chrX",
    "NC_000024.10": "chrY",
    "NC_012920.1": "chrMT",
}

# Reverse map: chr1 → NC_000001.11, etc.
_CHR_TO_REFSEQ: Dict[str, str] = {v: k for k, v in _REFSEQ_TO_CHR.items()}

# Also alias bare number → chr
_NUM_TO_CHR: Dict[str, str] = {str(i): f"chr{i}" for i in range(1, 23)}
_NUM_TO_CHR.update({"X": "chrX", "Y": "chrY", "MT": "chrMT", "M": "chrMT"})


def _normalise_chrom(name: str) -> str:
    """Return a canonical ``chrN`` name from any common chromosome representation."""
    if name in _REFSEQ_TO_CHR:
        return _REFSEQ_TO_CHR[name]
    if name.startswith("chr"):
        return name
    return _NUM_TO_CHR.get(name, name)


# ─── Data types ───────────────────────────────────────────────────────────────


@dataclass
class GeneRecord:
    """A single gene or transcript annotation interval."""

    chrom: str
    start: int  # 1-based, inclusive
    end: int  # 1-based, inclusive
    gene_name: str  # e.g. BRCA1
    gene_id: str  # e.g. Gene:HGNC:1100 or Ensembl gene id
    transcript_id: str = ""  # e.g. NM_007294.4
    feature_type: str = "gene"  # gene | mRNA | CDS | exon | …
    # FIX 6: canonical transcript selection metadata
    is_mane_select: bool = False
    is_mane_plus_clinical: bool = False
    is_canonical: bool = False
    cds_length: int = 0  # for longest-CDS tiebreaking

    @property
    def length(self) -> int:
        return self.end - self.start + 1


# ─── Index ────────────────────────────────────────────────────────────────────


class GffIndex:
    """In-memory index of gene/transcript annotations from a GFF3 file.

    Build via ``GffIndex.from_file(path)``; do not instantiate directly.

    Attributes:
        source_path: Path to the GFF3 file used to build this index.
        total_features: Number of features loaded.
    """

    def __init__(self) -> None:
        # Per-chromosome: sorted list of (start, end, GeneRecord)
        self._chrom_starts: Dict[str, List[int]] = {}
        self._chrom_records: Dict[str, List[GeneRecord]] = {}
        self.source_path: str = ""
        self.total_features: int = 0

    # ── construction ─────────────────────────────────────────────────────────

    @classmethod
    def from_file(cls, gff_path: str) -> "GffIndex":
        """Parse a GFF3 file and return a populated ``GffIndex``.

        Args:
            gff_path: Path to a GFF3 (plain or .gz) annotation file.

        Raises:
            FileNotFoundError: if the path does not exist.
            ValueError: if the file is empty or has no parseable features.
        """
        path = Path(gff_path)
        if not path.exists():
            raise FileNotFoundError(
                f"GFF3 annotation file not found: {gff_path!r}.\n"
                "Download the RefSeq GFF3 for GRCh38:\n"
                "  wget https://ftp.ncbi.nlm.nih.gov/refseq/H_sapiens/annotation/"
                "GRCh38_latest/refseq_identifiers/GRCh38_latest_genomic.gff.gz\n"
                "and set 'rna_analysis.refseq_gff' in your config."
            )

        idx = cls()
        idx.source_path = gff_path

        logger.info("Loading GFF3 annotation index from %s …", gff_path)
        n_parsed = 0

        open_fn = gzip.open if str(gff_path).endswith(".gz") else open
        with open_fn(gff_path, "rt", encoding="utf-8") as fh:  # type: ignore[call-overload]
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                rec = cls._parse_gff3_line(line)
                if rec is None:
                    continue
                chrom = _normalise_chrom(rec.chrom)
                rec.chrom = chrom
                if chrom not in idx._chrom_starts:
                    idx._chrom_starts[chrom] = []
                    idx._chrom_records[chrom] = []
                idx._chrom_starts[chrom].append(rec.start)
                idx._chrom_records[chrom].append(rec)
                n_parsed += 1

        if n_parsed == 0:
            raise ValueError(
                f"GFF3 file {gff_path!r} contained no parseable gene/mRNA features. "
                "Confirm the file is a valid RefSeq or Ensembl GFF3."
            )

        # Sort each chromosome's records by start position for binary search
        for chrom in idx._chrom_starts:
            paired = sorted(
                zip(idx._chrom_starts[chrom], idx._chrom_records[chrom]),
                key=lambda x: x[0],
            )
            idx._chrom_starts[chrom] = [p[0] for p in paired]
            idx._chrom_records[chrom] = [p[1] for p in paired]

        idx.total_features = n_parsed
        logger.info(
            "GFF3 index built: %d features across %d chromosomes",
            n_parsed,
            len(idx._chrom_starts),
        )
        return idx

    @staticmethod
    def _parse_gff3_line(line: str) -> Optional[GeneRecord]:
        """Parse a single GFF3 data line.  Returns ``None`` for skipped
        feature types (UTR, repeat, etc.) that are not needed for
        position → gene resolution.
        """
        cols = line.split("\t")
        if len(cols) < 9:
            return None
        seqname, _source, feature, start_s, end_s, _score, _strand, _phase, attrs = cols

        # Only index genes and transcript-equivalent features. Besides
        # "mRNA"/"transcript", NCBI's mitochondrial-genome GFF3 (and some
        # non-coding/organellar annotations generally) models tRNA, rRNA,
        # and protein-coding genes directly via "tRNA"/"rRNA"/"CDS"
        # records with no intermediate "mRNA" feature at all (there is no
        # mitochondrial splicing to model) — without these, every MT gene
        # would resolve a gene_name but never a transcript_id, silently
        # blocking HGVS/consequence annotation for the whole contig.
        if feature not in {"gene", "mRNA", "transcript", "tRNA", "rRNA", "CDS"}:
            return None

        try:
            start = int(start_s)
            end = int(end_s)
        except ValueError:
            return None

        # Parse attributes key=value;key=value
        attr_dict: Dict[str, str] = {}
        for item in attrs.split(";"):
            item = item.strip()
            if "=" in item:
                k, _, v = item.partition("=")
                attr_dict[k.strip()] = v.strip()

        gene_name = (
            attr_dict.get("gene") or attr_dict.get("Name") or attr_dict.get("gene_name") or ""
        )
        gene_id = (
            attr_dict.get("Dbxref", "").split(",")[0]
            or attr_dict.get("gene_id")
            or attr_dict.get("ID", "")
        )
        transcript_id = attr_dict.get("transcript_id") or (
            attr_dict.get("ID", "")
            if feature in {"mRNA", "transcript", "tRNA", "rRNA", "CDS"}
            else ""
        )

        # FIX 6: detect MANE Select / MANE Plus Clinical / canonical tags
        tag_str = attr_dict.get("tag", "") or attr_dict.get("Tag", "")
        tags = {t.strip() for t in tag_str.split(",") if t.strip()}
        is_mane_select = "MANE_Select" in tags or "MANE Select" in tags
        is_mane_plus = "MANE_Plus_Clinical" in tags or "MANE Plus Clinical" in tags
        is_canonical = "Ensembl_canonical" in tags or "canonical" in tags

        return GeneRecord(
            chrom=seqname,
            start=start,
            end=end,
            gene_name=gene_name,
            gene_id=gene_id,
            transcript_id=transcript_id,
            feature_type=feature,
            is_mane_select=is_mane_select,
            is_mane_plus_clinical=is_mane_plus,
            is_canonical=is_canonical,
        )

    # ── lookup ────────────────────────────────────────────────────────────────

    def lookup(
        self,
        chrom: str,
        pos: int,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Return ``(gene_name, transcript_id)`` for a genomic position.

        FIX 6: Applies deterministic transcript selection priority:
          1. MANE Select
          2. MANE Plus Clinical
          3. Ensembl/RefSeq canonical tag
          4. Longest CDS (via feature length as proxy)
          5. First mRNA/transcript found

        FIX 11: No arbitrary window limit; supports genes > 2 Mb.

        Args:
            chrom: Chromosome name in any recognised format.
            pos:   1-based genomic position.

        Returns:
            ``(gene_name, transcript_id)`` — either may be ``None`` if
            the position is intergenic.
        """
        chrom_norm = _normalise_chrom(chrom)
        starts = self._chrom_starts.get(chrom_norm)
        records = self._chrom_records.get(chrom_norm)

        if starts is None:
            return None, None

        idx = bisect.bisect_right(starts, pos) - 1

        tx_candidates: List[GeneRecord] = []
        gene_candidates: List[GeneRecord] = []

        i = idx
        while i >= 0:
            rec = records[i]
            if rec.end < pos:
                i -= 1
                continue
            if rec.feature_type in {"mRNA", "transcript", "tRNA", "rRNA", "CDS"}:
                tx_candidates.append(rec)
            elif rec.feature_type == "gene":
                gene_candidates.append(rec)
            i -= 1

        # FIX 6: deterministic selection
        if tx_candidates:

            def _tx_priority(r: GeneRecord) -> int:
                if r.is_mane_select:
                    return 0
                if r.is_mane_plus_clinical:
                    return 1
                if r.is_canonical:
                    return 2
                return 3

            tx_candidates.sort(key=lambda r: (_tx_priority(r), -(r.cds_length or r.length)))
            best = tx_candidates[0]
            return (best.gene_name or best.gene_id or None), (best.transcript_id or None)

        if gene_candidates:
            best_gene_rec = gene_candidates[0]
            return (best_gene_rec.gene_name or best_gene_rec.gene_id or None), None

        return None, None
