"""
pipeline/annotation/codon_provider.py
───────────────────────────────────────
FastaCodonContextProvider — resolves codon-level consequence for exonic SNVs.

Implements the CodonContextProvider interface defined in annotation/stage.py.

Algorithm:
  1. Load CDS feature records from the GFF3 (already parsed by RnaTranscriptAnalyser).
     Each CDS record has (chrom, start, end, strand, phase) — assembled into an
     ordered CDS chain per transcript.
  2. Given a variant (chrom, pos, ref, alt, transcript_id):
     a. Walk the transcript's CDS chain to find the codon containing pos.
     b. Read the reference codon from the genome FASTA.
     c. Apply the alt base to produce the mutant codon.
     d. Translate both codons using the standard genetic code.
     e. Compare amino acids → missense / synonymous / stop_gained / stop_lost /
        start_lost.

Requirements:
  * Reference genome FASTA (plain or bgzip-compressed; .fai index file required
    for random-access via samtools faidx — OR the whole file is loaded in memory
    for small reference chunks in test mode).
  * GFF3 with CDS features (same file already used by annotation stage).

Config::

    annotation:
      refseq_gff: /data/GRCh38.gff.gz
    alignment:
      reference_fasta: /data/GRCh38.fasta   # used for codon reading

No new downloads required — both files are already pipeline prerequisites.

KNOWN LIMITATIONS:
  * Requires either samtools faidx (preferred) or a full in-memory FASTA load.
    Without samtools, only chromosomes small enough to fit in RAM are supported.
  * Selenocysteine (TGA read-through), alternative initiation codons, and
    non-standard genetic codes (mitochondrial) are not handled — all chromosomes
    use the standard genetic code.
  * Phase-shifted CDS starts (GFF3 phase field != 0 at first exon) are
    handled, but rare transcript-specific first-codon offsets may produce
    incorrect frame assignments for a small fraction of transcripts.
"""
from __future__ import annotations

import gzip
import logging
import os
import subprocess
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("geper.pipeline.annotation.codon_provider")

# ── Standard genetic code (codon → single-letter AA) ──────────────────────────
_CODON_TABLE: Dict[str, str] = {
    "TTT": "F", "TTC": "F", "TTA": "L", "TTG": "L",
    "CTT": "L", "CTC": "L", "CTA": "L", "CTG": "L",
    "ATT": "I", "ATC": "I", "ATA": "I", "ATG": "M",
    "GTT": "V", "GTC": "V", "GTA": "V", "GTG": "V",
    "TCT": "S", "TCC": "S", "TCA": "S", "TCG": "S",
    "CCT": "P", "CCC": "P", "CCA": "P", "CCG": "P",
    "ACT": "T", "ACC": "T", "ACA": "T", "ACG": "T",
    "GCT": "A", "GCC": "A", "GCA": "A", "GCG": "A",
    "TAT": "Y", "TAC": "Y", "TAA": "*", "TAG": "*",
    "CAT": "H", "CAC": "H", "CAA": "Q", "CAG": "Q",
    "AAT": "N", "AAC": "N", "AAA": "K", "AAG": "K",
    "GAT": "D", "GAC": "D", "GAA": "E", "GAG": "E",
    "TGT": "C", "TGC": "C", "TGA": "*", "TGG": "W",
    "CGT": "R", "CGC": "R", "CGA": "R", "CGG": "R",
    "AGT": "S", "AGC": "S", "AGA": "R", "AGG": "R",
    "GGT": "G", "GGC": "G", "GGA": "G", "GGG": "G",
}

_START_CODONS = frozenset({"ATG"})
_STOP_CODONS = frozenset({"TAA", "TAG", "TGA"})


def _translate(codon: str) -> str:
    return _CODON_TABLE.get(codon.upper(), "?")


def _reverse_complement(seq: str) -> str:
    comp = {"A": "T", "T": "A", "G": "C", "C": "G", "N": "N"}
    return "".join(comp.get(b.upper(), "N") for b in reversed(seq))


# ── CDS record ────────────────────────────────────────────────────────────────

class CdsRecord:
    """One CDS exon segment from a GFF3 CDS feature."""
    __slots__ = ("chrom", "start", "end", "strand", "phase", "transcript_id")

    def __init__(
        self,
        chrom: str,
        start: int,
        end: int,
        strand: str,
        phase: int,
        transcript_id: str,
    ) -> None:
        self.chrom = chrom
        self.start = start      # 1-based, inclusive
        self.end = end          # 1-based, inclusive
        self.strand = strand
        self.phase = phase      # GFF3 phase (0/1/2) — bases to skip at start
        self.transcript_id = transcript_id


# ── FASTA reader ──────────────────────────────────────────────────────────────

class _FastaReader:
    """Minimal FASTA sequence extractor.

    Prefers samtools faidx for O(1) random access.  Falls back to loading
    the entire FASTA into memory (suitable for small/test references).
    """

    def __init__(self, fasta_path: str) -> None:
        self._path = fasta_path
        self._in_memory: Optional[Dict[str, str]] = None
        self._has_samtools = self._check_samtools()

        if not self._has_samtools:
            logger.info(
                "[CodonProvider] samtools not found — loading FASTA into memory: %s",
                fasta_path,
            )
            self._in_memory = self._load_fasta(fasta_path)

    @staticmethod
    def _check_samtools() -> bool:
        try:
            result = subprocess.run(
                ["samtools", "version"],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    @staticmethod
    def _load_fasta(path: str) -> Dict[str, str]:
        """Load entire FASTA into a dict of chrom → sequence (uppercase)."""
        seqs: Dict[str, str] = {}
        open_fn = gzip.open if path.endswith((".gz", ".bgz")) else open
        current: Optional[str] = None
        buf: List[str] = []

        with open_fn(path, "rt", encoding="ascii", errors="replace") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if line.startswith(">"):
                    if current is not None:
                        seqs[current] = "".join(buf).upper()
                    current = line[1:].split()[0]
                    buf = []
                else:
                    buf.append(line)
            if current is not None:
                seqs[current] = "".join(buf).upper()

        return seqs

    def fetch(self, chrom: str, start: int, end: int) -> str:
        """Fetch sequence [start, end] (1-based, inclusive).

        Args:
            chrom: Chromosome name as stored in FASTA header.
            start: 1-based start position.
            end:   1-based end position (inclusive).

        Returns:
            Uppercase sequence string.  Returns empty string on any error.
        """
        if self._has_samtools:
            return self._samtools_fetch(chrom, start, end)
        if self._in_memory is not None:
            seq = self._in_memory.get(chrom, "")
            if not seq:
                # Try with/without chr prefix
                alt = chrom.lstrip("chr") if chrom.startswith("chr") else f"chr{chrom}"
                seq = self._in_memory.get(alt, "")
            if not seq:
                logger.debug("[CodonProvider] Chrom %s not in in-memory FASTA", chrom)
                return ""
            s = start - 1  # convert to 0-based
            return seq[s:end].upper()
        return ""

    def _samtools_fetch(self, chrom: str, start: int, end: int) -> str:
        region = f"{chrom}:{start}-{end}"
        try:
            result = subprocess.run(
                ["samtools", "faidx", self._path, region],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                # Try alternate chrom name
                alt = chrom.lstrip("chr") if chrom.startswith("chr") else f"chr{chrom}"
                region2 = f"{alt}:{start}-{end}"
                result = subprocess.run(
                    ["samtools", "faidx", self._path, region2],
                    capture_output=True, text=True, timeout=15,
                )
            if result.returncode != 0:
                return ""
            lines = result.stdout.strip().splitlines()
            seq = "".join(l for l in lines if not l.startswith(">"))
            return seq.upper()
        except Exception as exc:
            logger.debug("[CodonProvider] samtools faidx failed for %s: %s", region, exc)
            return ""


# ── GFF3 CDS loader ───────────────────────────────────────────────────────────

def _load_cds_from_gff(gff_path: str) -> Dict[str, List[CdsRecord]]:
    """Parse CDS features from a GFF3 file.

    Returns: transcript_id → [CdsRecord, ...] sorted by start position.
    """
    cds_map: Dict[str, List[CdsRecord]] = {}

    open_fn = gzip.open if gff_path.endswith((".gz", ".bgz")) else open

    with open_fn(gff_path, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 9:
                continue
            if cols[2] != "CDS":
                continue
            try:
                chrom = cols[0]
                start = int(cols[3])
                end = int(cols[4])
                strand = cols[6]
                phase_raw = cols[7]
                phase = int(phase_raw) if phase_raw in ("0", "1", "2") else 0
                attrs_str = cols[8]
            except (ValueError, IndexError):
                continue

            # Parse attributes
            attrs: Dict[str, str] = {}
            if "=" in attrs_str:
                for part in attrs_str.strip().split(";"):
                    part = part.strip()
                    if "=" in part:
                        k, _, v = part.partition("=")
                        attrs[k.strip()] = v.strip().strip('"')
            else:
                import re
                for m in re.finditer(r'(\w+)\s+"([^"]*)"', attrs_str):
                    attrs[m.group(1)] = m.group(2)

            # GFF3 Parent = transcript; GTF transcript_id attr
            parent = (
                attrs.get("Parent")
                or attrs.get("transcript_id")
                or ""
            )
            if not parent:
                continue

            rec = CdsRecord(
                chrom=chrom,
                start=start,
                end=end,
                strand=strand,
                phase=phase,
                transcript_id=parent,
            )
            cds_map.setdefault(parent, []).append(rec)

    # Sort each transcript's CDS segments
    for tx_id in cds_map:
        cds_map[tx_id].sort(key=lambda r: r.start)

    logger.info("[CodonProvider] Loaded CDS for %d transcripts from %s", len(cds_map), gff_path)
    return cds_map


# ── Main provider ─────────────────────────────────────────────────────────────

class FastaCodonContextProvider:
    """Resolve codon-level consequence for exonic SNVs.

    Implements the CodonContextProvider interface from annotation/stage.py.

    Args:
        gff_path:    Path to GFF3 annotation file (plain or gzipped).
        fasta_path:  Path to reference genome FASTA.
    """

    def __init__(self, gff_path: str, fasta_path: str) -> None:
        self._gff_path = gff_path
        self._fasta_path = fasta_path
        self._cds_map: Optional[Dict[str, List[CdsRecord]]] = None
        self._fasta: Optional[_FastaReader] = None
        self._available = False

        if not os.path.isfile(gff_path):
            logger.warning("[CodonProvider] GFF3 not found: %s", gff_path)
            return
        if not os.path.isfile(fasta_path):
            logger.warning("[CodonProvider] FASTA not found: %s", fasta_path)
            return

        try:
            self._cds_map = _load_cds_from_gff(gff_path)
            self._fasta = _FastaReader(fasta_path)
            self._available = True
            logger.info(
                "[CodonProvider] Ready: %d transcripts with CDS from %s",
                len(self._cds_map), gff_path,
            )
        except Exception as exc:
            logger.error("[CodonProvider] Initialisation failed: %s", exc)

    # ── CodonContextProvider interface ────────────────────────────────────────

    def get_codon_change(
        self,
        chrom: str,
        pos: int,
        ref: str,
        alt: str,
        transcript_id: str,
    ) -> Optional[str]:
        """Determine the codon-level consequence of an exonic SNV."""
        result = self.get_codon_and_aa(chrom, pos, ref, alt, transcript_id)
        return result[0] if result else None

    def get_codon_and_aa(
        self,
        chrom: str,
        pos: int,
        ref: str,
        alt: str,
        transcript_id: str,
    ):
        """Return (consequence, ref_codon, alt_codon, ref_aa, alt_aa) or None tuple.

        Used by annotation stage to populate AI engine sequence fields.
        Returns (consequence_str, ref_codon_str, alt_codon_str, ref_aa_str, alt_aa_str)
        or (None, None, None, None, None) if undetermined.
        """
        if not self._available:
            raise NotImplementedError(
                "CodonContextProvider not available (GFF3 or FASTA not loaded)"
            )

        if len(ref) != 1 or len(alt) != 1:
            raise NotImplementedError(
                "FastaCodonContextProvider only handles SNVs; "
                f"got ref={ref!r} alt={alt!r}"
            )

        if ref == alt:
            return ("synonymous", None, None, None, None)

        try:
            return self._classify_snv_full(chrom, pos, ref.upper(), alt.upper(), transcript_id)
        except Exception as exc:
            logger.debug(
                "[CodonProvider] classify_snv failed for %s:%d %s>%s tx=%s: %s",
                chrom, pos, ref, alt, transcript_id, exc,
            )
            return (None, None, None, None, None)

    # ── Internal logic ────────────────────────────────────────────────────────

    def _classify_snv_full(
        self,
        chrom: str,
        pos: int,
        ref: str,
        alt: str,
        transcript_id: str,
    ):
        """Core codon-change classification; returns (consequence, ref_codon, alt_codon, ref_aa, alt_aa)."""
        cds_chain = self._cds_map.get(transcript_id)  # type: ignore[union-attr]
        if not cds_chain:
            logger.debug(
                "[CodonProvider] No CDS for transcript %s", transcript_id
            )
            return (None, None, None, None, None)

        # Normalise chrom for FASTA lookup
        strand = cds_chain[0].strand

        # Walk exons in transcript order to compute CDS offsets correctly.
        # Plus-strand: ascending genomic order (same as stored sorted order).
        # Minus-strand: descending genomic order (reversed) — this MUST match
        # the order used by _fetch_codon(), otherwise the offset is wrong and
        # multi-exon codons are assembled incorrectly.
        transcript_ordered_chain = (
            cds_chain if strand == "+" else list(reversed(cds_chain))
        )

        # FIX 4: Apply GFF3 phase of the first CDS exon.
        # phase=N means the first N bases of this exon belong to the tail of a
        # split codon whose first bases are upstream (or are a 5' partial CDS).
        # Subtracting _phase_offset from cds_pos aligns position 0 with the first
        # complete in-frame codon, giving correct frame for all downstream positions.
        _phase_offset = transcript_ordered_chain[0].phase if transcript_ordered_chain else 0

        # Find the CDS exon containing pos
        cds_exon: Optional[CdsRecord] = None
        cds_offset = 0  # number of CDS bases before this exon (in transcript order)
        for exon in transcript_ordered_chain:
            exon_len = exon.end - exon.start + 1
            if exon.chrom == chrom and exon.start <= pos <= exon.end:
                cds_exon = exon
                break
            cds_offset += exon_len
        else:
            # Try normalised chrom names
            norm_chrom = chrom.lstrip("chr")
            cds_offset = 0  # reset for second pass
            for exon in transcript_ordered_chain:
                exon_chrom = exon.chrom.lstrip("chr")
                exon_len = exon.end - exon.start + 1
                if exon_chrom == norm_chrom and exon.start <= pos <= exon.end:
                    cds_exon = exon
                    chrom = exon.chrom  # use the stored chrom for FASTA
                    break
                cds_offset += exon_len

        if cds_exon is None:
            logger.debug(
                "[CodonProvider] pos %s:%d not in any CDS exon of %s",
                chrom, pos, transcript_id,
            )
            return (None, None, None, None, None)

        # Position within this CDS exon (0-based from exon start)
        if strand == "+":
            pos_in_exon = pos - cds_exon.start
        else:
            pos_in_exon = cds_exon.end - pos

        # Absolute CDS position (0-based from transcript CDS start).
        # FIX 4: subtract phase_offset so frame aligns correctly for phase != 0.
        cds_pos = cds_offset + pos_in_exon - _phase_offset

        # Codon position within the codon (0, 1, or 2)
        codon_index = cds_pos % 3
        # Start of this codon in CDS coordinates (0-based, phase-adjusted)
        codon_cds_start = cds_pos - codon_index

        # Fetch reference codon from genomic FASTA
        ref_codon = self._fetch_codon(
            chrom, transcript_id, cds_chain, strand, codon_cds_start
        )
        if not ref_codon or len(ref_codon) != 3:
            logger.debug(
                "[CodonProvider] Could not fetch codon at CDS pos %d for %s",
                codon_cds_start, transcript_id,
            )
            return (None, None, None, None, None)

        # Validate reference base matches FASTA
        actual_ref_base = ref_codon[codon_index]
        if actual_ref_base != ref:
            logger.debug(
                "[CodonProvider] Ref mismatch at %s:%d: VCF=%s FASTA=%s (codon=%s idx=%d)",
                chrom, pos, ref, actual_ref_base, ref_codon, codon_index,
            )

        # Build mutant codon
        mut_codon = list(ref_codon)
        if strand == "+":
            mut_codon[codon_index] = alt
        else:
            # On minus strand the alt in VCF is on the + strand; we need its complement
            _comp = {"A": "T", "T": "A", "G": "C", "C": "G"}
            mut_codon[codon_index] = _comp.get(alt, alt)
        mut_codon_str = "".join(mut_codon)

        ref_aa = _translate(ref_codon)
        alt_aa = _translate(mut_codon_str)

        # Classify
        if ref_aa == alt_aa:
            consequence = "synonymous"
        elif alt_aa == "*":
            consequence = "stop_gained"
        elif ref_aa == "*":
            consequence = "stop_lost"
        elif codon_cds_start == 0 and ref_aa == "M":
            consequence = "start_lost"
        else:
            consequence = "missense"

        return (consequence, ref_codon, mut_codon_str, ref_aa, alt_aa)

    def _fetch_codon(
        self,
        chrom: str,
        transcript_id: str,
        cds_chain: List[CdsRecord],
        strand: str,
        codon_cds_start: int,
    ) -> Optional[str]:
        """Assemble three bases of the codon from the CDS chain.

        Args:
            codon_cds_start: 0-based CDS coordinate of codon start.

        Returns:
            3-character reference codon string (sense strand), or None.
        """
        # Walk CDS chain collecting bases starting at codon_cds_start
        bases: List[str] = []
        cds_cursor = 0

        chain = cds_chain if strand == "+" else list(reversed(cds_chain))

        for exon in chain:
            exon_len = exon.end - exon.start + 1
            exon_end_in_cds = cds_cursor + exon_len

            if exon_end_in_cds <= codon_cds_start:
                cds_cursor += exon_len
                continue

            # This exon contributes to our codon
            if strand == "+":
                # Genomic start of needed bases
                skip = max(0, codon_cds_start - cds_cursor)
                gstart = exon.start + skip
                gend = min(exon.end, gstart + (3 - len(bases)))
                if gend < gstart:
                    cds_cursor += exon_len
                    continue
                seq = self._fasta.fetch(exon.chrom, gstart, gend)  # type: ignore[union-attr]
                bases.extend(seq)
            else:
                # Minus strand: exons in the chain are reversed above.
                # Genomic coords: high end first.
                skip = max(0, codon_cds_start - cds_cursor)
                gend = exon.end - skip
                gstart = max(exon.start, gend - (3 - len(bases)) + 1)
                if gstart > gend:
                    cds_cursor += exon_len
                    continue
                seq = self._fasta.fetch(exon.chrom, gstart, gend)  # type: ignore[union-attr]
                # Reverse-complement this segment
                bases.extend(_reverse_complement(seq))

            cds_cursor += exon_len
            if len(bases) >= 3:
                break

        if len(bases) < 3:
            return None

        return "".join(bases[:3]).upper()


# ── Factory function ──────────────────────────────────────────────────────────

def make_codon_provider_from_cfg(cfg: Optional[dict]) -> "CodonContextProvider":  # noqa: F821
    """Create the best available CodonContextProvider from pipeline config.

    Returns FastaCodonContextProvider when both GFF3 and reference FASTA are
    configured; otherwise returns NoCdsCodonContextProvider.
    """
    from pipeline.annotation.stage import NoCdsCodonContextProvider

    ann_cfg = (cfg or {}).get("annotation", {}) or {}
    rna_cfg = (cfg or {}).get("rna_analysis", {}) or {}
    align_cfg = (cfg or {}).get("alignment", {}) or {}

    gff_path = ann_cfg.get("refseq_gff") or rna_cfg.get("refseq_gff") or ""
    fasta_path = (
        align_cfg.get("reference_fasta")
        or (cfg or {}).get("reference_fasta")
        or ""
    )

    if gff_path and fasta_path and os.path.isfile(gff_path) and os.path.isfile(fasta_path):
        try:
            provider = FastaCodonContextProvider(gff_path, fasta_path)
            if provider._available:
                logger.info("[CodonProvider] Using FastaCodonContextProvider")
                return provider
        except Exception as exc:
            logger.warning("[CodonProvider] FastaCodonContextProvider init failed: %s", exc)

    logger.info(
        "[CodonProvider] Using NoCdsCodonContextProvider "
        "(set alignment.reference_fasta + rna_analysis.refseq_gff to enable codon resolution)"
    )
    return NoCdsCodonContextProvider()
