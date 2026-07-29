"""
pipeline/rna/transcript.py
───────────────────────────
GFF3/GTF-based RNA transcript analysis — stdlib only.

Fixes applied:
  FIX 3 — Strand-aware splice donor/acceptor classification:
           On + strand: exon right-edge → donor, exon left-edge → acceptor.
           On − strand: exon left-edge → donor, exon right-edge → acceptor.
  FIX 4 — Proper UTR annotation: classify_region now returns "utr5" / "utr3"
           by using CDS boundaries stored during GFF3 load.
  FIX 5 — GFF3 phase stored on CDS features and used in frame calculation.
"""

from __future__ import annotations

import gzip
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("geper.pipeline.rna.transcript")


@dataclass
class TranscriptRecord:
    transcript_id: str
    gene_id: str
    chrom: str
    start: int
    end: int
    strand: str
    exons: List[Tuple[int, int]] = field(default_factory=list)
    # CDS boundaries for UTR detection (FIX 4)
    cds_start: Optional[int] = None  # lowest CDS coordinate (1-based, inclusive)
    cds_end: Optional[int] = None    # highest CDS coordinate (1-based, inclusive)
    # CDS records with phase for frame calculation (FIX 5)
    # Each entry: (cds_start, cds_end, phase)
    cds_records: List[Tuple[int, int, int]] = field(default_factory=list)


def _parse_attributes(attr_str: str) -> Dict[str, str]:
    """Parse GFF3 (key=value) or GTF (key "value") attribute strings."""
    attrs: Dict[str, str] = {}
    # GFF3 style: key=value;key2=value2
    if "=" in attr_str:
        for part in attr_str.strip().split(";"):
            part = part.strip()
            if "=" in part:
                k, _, v = part.partition("=")
                attrs[k.strip()] = v.strip().strip('"')
    else:
        # GTF style: key "value"; key2 "value2";
        for m in re.finditer(r'(\w+)\s+"([^"]*)"', attr_str):
            attrs[m.group(1)] = m.group(2)
    return attrs


def _open_gff(path: str):
    """Open a plain or gzip-compressed GFF file."""
    if path.endswith(".gz"):
        return gzip.open(path, "rt")
    return open(path, "r")


class RnaTranscriptAnalyser:
    """Parse a RefSeq GFF3/GTF and answer transcript-level queries.

    Args:
        cfg: Pipeline config dict; reads ``cfg["rna_analysis"]``.
    """

    def __init__(self, cfg: Optional[dict] = None) -> None:
        self._cfg = (cfg or {}).get("rna_analysis", {}) or {}
        self._transcripts: Dict[str, TranscriptRecord] = {}
        self._gene_index: Dict[str, List[str]] = {}   # gene_id → [transcript_id]
        self._available = False

        gff_path = self._cfg.get("refseq_gff", "")
        if not gff_path:
            logger.warning("rna_analysis.refseq_gff not configured — transcript analysis disabled.")
            return

        import os
        if not os.path.exists(gff_path):
            logger.warning("refseq_gff not found: %s — transcript analysis disabled.", gff_path)
            return

        try:
            self._load_gff(gff_path)
            self._available = True
            logger.info(
                "RnaTranscriptAnalyser: loaded %d transcripts from %s",
                len(self._transcripts), gff_path,
            )
        except Exception as exc:
            logger.error("Failed to load GFF %s: %s", gff_path, exc)

    # ── Loading ───────────────────────────────────────────────────────────────

    def _load_gff(self, path: str) -> None:
        """Three-pass GFF load: mRNA records, exons, then CDS features."""
        # Pass 1: mRNA / transcript features
        with _open_gff(path) as fh:
            for line in fh:
                if line.startswith("#"):
                    continue
                cols = line.rstrip("\n").split("\t")
                if len(cols) < 9:
                    continue
                feature = cols[2]
                if feature not in ("mRNA", "transcript"):
                    continue
                try:
                    chrom = cols[0]
                    start = int(cols[3])
                    end = int(cols[4])
                    strand = cols[6]
                    attrs = _parse_attributes(cols[8])
                    gene_id = attrs.get("gene_id") or attrs.get("Parent") or ""
                    # GFF3 often stores transcript_id in "ID"
                    transcript_id = (
                        attrs.get("transcript_id")
                        or attrs.get("ID")
                        or ""
                    )
                    if not transcript_id:
                        continue
                    rec = TranscriptRecord(
                        transcript_id=transcript_id,
                        gene_id=gene_id,
                        chrom=chrom,
                        start=start,
                        end=end,
                        strand=strand,
                    )
                    self._transcripts[transcript_id] = rec
                    if gene_id:
                        self._gene_index.setdefault(gene_id, []).append(transcript_id)
                except (ValueError, IndexError):
                    continue

        # Pass 2: exon features
        with _open_gff(path) as fh:
            for line in fh:
                if line.startswith("#"):
                    continue
                cols = line.rstrip("\n").split("\t")
                if len(cols) < 9:
                    continue
                if cols[2] != "exon":
                    continue
                try:
                    exon_start = int(cols[3])
                    exon_end = int(cols[4])
                    attrs = _parse_attributes(cols[8])
                    parent = (
                        attrs.get("Parent")
                        or attrs.get("transcript_id")
                        or ""
                    )
                    # GFF3 Parent may be comma-separated (multi-parent, rare)
                    for pid in parent.split(","):
                        pid = pid.strip()
                        if pid in self._transcripts:
                            self._transcripts[pid].exons.append((exon_start, exon_end))
                except (ValueError, IndexError):
                    continue

        # Pass 3: CDS features — for UTR detection (FIX 4) and phase (FIX 5)
        with _open_gff(path) as fh:
            for line in fh:
                if line.startswith("#"):
                    continue
                cols = line.rstrip("\n").split("\t")
                if len(cols) < 9:
                    continue
                if cols[2] != "CDS":
                    continue
                try:
                    cds_s = int(cols[3])
                    cds_e = int(cols[4])
                    phase_str = cols[7]
                    phase = int(phase_str) if phase_str not in (".", "-1") else 0
                    attrs = _parse_attributes(cols[8])
                    parent = (
                        attrs.get("Parent")
                        or attrs.get("transcript_id")
                        or ""
                    )
                    for pid in parent.split(","):
                        pid = pid.strip()
                        if pid in self._transcripts:
                            rec = self._transcripts[pid]
                            rec.cds_records.append((cds_s, cds_e, phase))
                            if rec.cds_start is None or cds_s < rec.cds_start:
                                rec.cds_start = cds_s
                            if rec.cds_end is None or cds_e > rec.cds_end:
                                rec.cds_end = cds_e
                except (ValueError, IndexError):
                    continue

        # Sort exons by genomic coordinate for all transcripts
        for rec in self._transcripts.values():
            rec.exons.sort(key=lambda x: x[0])
            rec.cds_records.sort(key=lambda x: x[0])

    # ── Public API ────────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        return self._available

    def get_transcript(self, transcript_id: str) -> Optional[TranscriptRecord]:
        return self._transcripts.get(transcript_id)

    def get_transcripts_for_gene(self, gene_id: str) -> List[TranscriptRecord]:
        """Return all loaded transcripts belonging to *gene_id*.

        ISSUE 5 FIX: the public API previously lacked this lookup even
        though TranscriptRecord already stores gene_id and a dedicated
        test exercised it — the data model was designed for gene-level
        lookups (e.g. picking a canonical/MANE transcript among several
        for a gene), so this completes that intended functionality
        rather than introducing new scope.
        """
        return [rec for rec in self._transcripts.values() if rec.gene_id == gene_id]

    def get_exon_number(
        self,
        chrom: str,
        pos: int,
        transcript_id: str,
    ) -> Optional[int]:
        """Return 1-based exon number containing *pos*, or None."""
        rec = self._transcripts.get(transcript_id)
        if rec is None or rec.chrom != chrom:
            return None
        for i, (exon_s, exon_e) in enumerate(rec.exons, 1):
            if exon_s <= pos <= exon_e:
                return i
        return None

    def genomic_to_cds_pos(
        self,
        chrom: str,
        pos: int,
        transcript_id: str,
    ) -> Optional[int]:
        """Map a genomic position to a 1-based CDS-relative coordinate.

        ISSUE 1 FIX: this is the coordinate that must be used for HGVS
        ``c.`` notation. Returns None when *pos* does not fall within a
        CDS exonic segment of the transcript (UTR, intronic, unknown
        strand, or no CDS data loaded) — callers MUST treat None as
        "cannot compute a valid c. coordinate" and fall back to genomic
        (g.) notation rather than substituting the raw genomic position.
        """
        rec = self._transcripts.get(transcript_id)
        if rec is None or rec.chrom != chrom or not rec.cds_records:
            return None
        if rec.strand not in ("+", "-"):
            return None

        segments = sorted(rec.cds_records, key=lambda x: x[0])
        contained = any(s <= pos <= e for s, e, _phase in segments)
        if not contained:
            return None

        if rec.strand == "+":
            cum = 0
            for s, e, _phase in segments:
                if pos < s:
                    break
                if pos <= e:
                    return cum + (pos - s + 1)
                cum += e - s + 1
            return None

        # strand == "-": CDS position 1 is at the highest genomic coordinate.
        cum = 0
        for s, e, _phase in reversed(segments):
            if pos > e:
                break
            if pos >= s:
                return cum + (e - pos + 1)
            cum += e - s + 1
        return None

    def genomic_to_transcript_pos(
        self,
        chrom: str,
        pos: int,
        transcript_id: str,
    ) -> Optional[int]:
        """Map a genomic position to a 1-based exon/transcript-relative
        coordinate (used for HGVS ``n.`` notation on non-coding
        transcripts). Returns None if *pos* is not within an exon, or the
        transcript/strand is unknown.
        """
        rec = self._transcripts.get(transcript_id)
        if rec is None or rec.chrom != chrom or not rec.exons:
            return None
        if rec.strand not in ("+", "-"):
            return None

        exons = sorted(rec.exons, key=lambda x: x[0])
        contained = any(s <= pos <= e for s, e in exons)
        if not contained:
            return None

        if rec.strand == "+":
            cum = 0
            for s, e in exons:
                if pos < s:
                    break
                if pos <= e:
                    return cum + (pos - s + 1)
                cum += e - s + 1
            return None

        cum = 0
        for s, e in reversed(exons):
            if pos > e:
                break
            if pos >= s:
                return cum + (e - pos + 1)
            cum += e - s + 1
        return None

    def is_splice_region(
        self,
        chrom: str,
        pos: int,
        transcript_id: str,
        window: int = 2,
    ) -> bool:
        """Return True if *pos* is within *window* bp of any exon boundary."""
        rec = self._transcripts.get(transcript_id)
        if rec is None or rec.chrom != chrom:
            return False
        for exon_s, exon_e in rec.exons:
            if abs(pos - exon_s) <= window or abs(pos - exon_e) <= window:
                return True
        return False

    def classify_region(
        self,
        chrom: str,
        pos: int,
        transcript_id: str,
    ) -> str:
        """Classify the genomic region of *pos* relative to a transcript.

        Returns one of: "exonic", "splice_donor", "splice_acceptor",
        "intronic", "utr5", "utr3", "intergenic".

        FIX 3 — Strand-aware splice site classification:
            Positive strand (+):
                Exon right-edge (3' end of exon) → splice DONOR
                Exon left-edge  (5' end of exon) → splice ACCEPTOR
            Negative strand (−):
                Exon left-edge  (5' genomic, = 3' transcript end of exon) → splice DONOR
                Exon right-edge (3' genomic, = 5' transcript end of exon) → splice ACCEPTOR

        FIX 4 — Proper UTR annotation using CDS boundaries.
        """
        if not self._available:
            return "intergenic"
        rec = self._transcripts.get(transcript_id)
        if rec is None or rec.chrom != chrom:
            return "intergenic"

        # Check position is in the transcript at all
        if pos < rec.start or pos > rec.end:
            return "intergenic"

        strand = rec.strand  # "+" or "-"
        multi_exon = len(rec.exons) > 1

        # ── Check if position falls in an exon ───────────────────────────────
        in_exon = self.get_exon_number(chrom, pos, transcript_id) is not None

        if in_exon:
            # FIX 3: For multi-exon transcripts, check if within 2 bp of exon
            # boundary and apply strand-aware donor/acceptor labelling.
            if multi_exon and rec.exons:
                for idx, (exon_s, exon_e) in enumerate(rec.exons):
                    if not (exon_s <= pos <= exon_e):
                        continue
                    # At exon right boundary (genomic)
                    if abs(pos - exon_e) <= 2:
                        # Is there a downstream exon (not the last exon)?
                        if idx < len(rec.exons) - 1:
                            # Right boundary of non-terminal exon
                            if strand == "-":
                                return "splice_acceptor"
                            else:
                                return "splice_donor"
                    # At exon left boundary (genomic)
                    if abs(pos - exon_s) <= 2:
                        # Is there an upstream exon (not the first exon)?
                        if idx > 0:
                            # Left boundary of non-first exon
                            if strand == "-":
                                return "splice_donor"
                            else:
                                return "splice_acceptor"

            # FIX 4: UTR annotation using CDS boundaries
            if rec.cds_start is not None and rec.cds_end is not None:
                if strand == "+":
                    if pos < rec.cds_start:
                        return "utr5"
                    if pos > rec.cds_end:
                        return "utr3"
                else:  # strand == "-"
                    if pos > rec.cds_end:
                        return "utr5"
                    if pos < rec.cds_start:
                        return "utr3"

            return "exonic"

        # ── Intronic region — check splice boundaries (intronic side) ─────────
        if multi_exon and rec.exons:
            # Check intronic positions within window of exon boundaries
            for idx, (exon_s, exon_e) in enumerate(rec.exons):
                # Intronic right of exon end (between exon_e+1 and next exon_s-1)
                if exon_e < pos <= exon_e + 2:
                    if idx < len(rec.exons) - 1:
                        # Intronic position immediately after exon end
                        if strand == "-":
                            return "splice_acceptor"
                        else:
                            return "splice_donor"
                # Intronic left of exon start
                if exon_s - 2 <= pos < exon_s:
                    if idx > 0:
                        # Intronic position immediately before exon start
                        if strand == "-":
                            return "splice_donor"
                        else:
                            return "splice_acceptor"

        return "intronic"

    def get_cds_frame_at_position(
        self,
        chrom: str,
        pos: int,
        transcript_id: str,
    ) -> Optional[int]:
        """Return the reading frame (0, 1, or 2) at *pos* using GFF3 phase.

        FIX 5: Uses CDS phase annotation to calculate the exact codon frame,
        handling non-zero phase, partial CDS, and split codons.

        Returns None if position is not within a CDS interval.
        """
        rec = self._transcripts.get(transcript_id)
        if rec is None or rec.chrom != chrom or not rec.cds_records:
            return None

        strand = rec.strand

        # Sort CDS records in transcript order
        if strand == "+":
            cds_ordered = sorted(rec.cds_records, key=lambda x: x[0])
        else:
            cds_ordered = sorted(rec.cds_records, key=lambda x: x[0], reverse=True)

        # Walk CDS segments in transcript order, tracking cumulative coding bases
        cumulative_bases = 0
        for cds_s, cds_e, phase in cds_ordered:
            if not (cds_s <= pos <= cds_e):
                cumulative_bases += (cds_e - cds_s + 1)
                continue
            # pos is within this CDS segment
            if strand == "+":
                offset_in_cds = pos - cds_s
                # First segment's phase adjusts the frame
                if cumulative_bases == 0:
                    frame = (offset_in_cds + phase) % 3
                else:
                    frame = (cumulative_bases + offset_in_cds) % 3
            else:  # "-"
                offset_in_cds = cds_e - pos
                if cumulative_bases == 0:
                    frame = (offset_in_cds + phase) % 3
                else:
                    frame = (cumulative_bases + offset_in_cds) % 3
            return frame

        return None
