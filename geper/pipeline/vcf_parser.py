"""
Plain-text VCF parser.

Deliberately avoids pysam / cyvcf2 (native-code dependencies that are
awkward in Colab and some cloud containers). VCF is a documented,
tab-delimited text format, so a dependency-free parser is both simpler
to deploy and easier to audit.

Reference: VCF 4.x spec, columns are:
CHROM  POS  ID  REF  ALT  QUAL  FILTER  INFO  [FORMAT  SAMPLE...]
"""

import gzip
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional

from utils.exceptions import VCFParsingError
from utils.logger import get_logger

logger = get_logger(__name__)

_REQUIRED_COLUMNS = ["CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO"]


@dataclass
class Variant:
    """A single, already-normalized (one ALT per record) variant call."""

    chrom: str
    pos: int
    variant_id: str
    ref: str
    alt: str
    qual: Optional[str]
    filter_status: Optional[str]
    info: Dict[str, str] = field(default_factory=dict)

    @property
    def variant_type(self) -> str:
        """Cheap classification used by the router; SNV / insertion / deletion / MNV."""
        if len(self.ref) == 1 and len(self.alt) == 1:
            return "SNV"
        if len(self.ref) < len(self.alt):
            return "insertion"
        if len(self.ref) > len(self.alt):
            return "deletion"
        return "MNV"

    def to_dict(self) -> Dict:
        return {
            "chrom": self.chrom,
            "pos": self.pos,
            "id": self.variant_id,
            "ref": self.ref,
            "alt": self.alt,
            "qual": self.qual,
            "filter": self.filter_status,
            "variant_type": self.variant_type,
            "info": self.info,
        }


class VCFParser:
    """Streams and validates variant records out of a VCF (optionally gzipped) file."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.header_lines: List[str] = []
        self.column_names: List[str] = []
        self.samples: List[str] = []
        # Counts of records skipped during parsing, keyed by reason --
        # surfaced in a single summary log line at the end of the file
        # instead of once per skipped record (which would be pure log
        # noise on a VCF with thousands of spanning-deletion '*' calls
        # from multi-sample joint genotyping).
        self.skip_counts: Dict[str, int] = defaultdict(int)

    def _open(self):
        if self.filepath.endswith(".gz"):
            return gzip.open(self.filepath, "rt")
        return open(self.filepath, "r")

    def parse(self) -> List[Variant]:
        """Parse the entire file and return a list of Variant records."""
        variants: List[Variant] = []
        for variant in self.iter_variants():
            variants.append(variant)
        logger.info(f"Parsed {len(variants)} variant record(s) from '{self.filepath}'.")
        return variants

    def iter_variants(self) -> Iterator[Variant]:
        """Memory-efficient generator over variant records for large VCFs."""
        try:
            handle = self._open()
        except OSError as exc:
            raise VCFParsingError(f"Could not open VCF file '{self.filepath}': {exc}") from exc

        try:
            with handle as fh:
                data_line_seen = False
                for line_no, raw_line in enumerate(fh, start=1):
                    line = raw_line.rstrip("\n")
                    if not line:
                        continue

                    if line.startswith("##"):
                        self.header_lines.append(line)
                        continue

                    if line.startswith("#CHROM"):
                        self._parse_column_header(line, line_no)
                        continue

                    if not self.column_names:
                        raise VCFParsingError(
                            f"Data line encountered before '#CHROM' header "
                            f"at line {line_no} of '{self.filepath}'."
                        )

                    data_line_seen = True
                    yield from self._parse_data_line(line, line_no)

                if not self.column_names:
                    raise VCFParsingError(
                        f"'{self.filepath}' has no '#CHROM' header line -- not a valid VCF."
                    )
                if not data_line_seen:
                    logger.warning(f"'{self.filepath}' contains a header but no variant records.")

                if self.skip_counts:
                    total_skipped = sum(self.skip_counts.values())
                    breakdown = ", ".join(
                        f"{reason}={count}" for reason, count in sorted(self.skip_counts.items())
                    )
                    logger.info(
                        f"'{self.filepath}': skipped {total_skipped} record(s) "
                        f"during parsing that had no usable sequence "
                        f"representation ({breakdown})."
                    )
        except UnicodeDecodeError as exc:
            raise VCFParsingError(
                f"'{self.filepath}' could not be decoded as text. Is it a valid "
                f"(optionally gzipped) VCF file? Original error: {exc}"
            ) from exc

    def _parse_column_header(self, line: str, line_no: int) -> None:
        columns = line.lstrip("#").split("\t")
        if columns[:8] != _REQUIRED_COLUMNS:
            raise VCFParsingError(
                f"Malformed VCF column header at line {line_no}: expected "
                f"columns starting with {_REQUIRED_COLUMNS}, got {columns[:8]}."
            )
        self.column_names = columns
        self.samples = columns[9:] if len(columns) > 9 else []

    def _parse_data_line(self, line: str, line_no: int) -> Iterator[Variant]:
        fields = line.split("\t")
        if len(fields) < 8:
            raise VCFParsingError(
                f"Line {line_no} has only {len(fields)} column(s); a valid VCF "
                f"data line needs at least 8 (CHROM..INFO)."
            )

        chrom, pos_str, variant_id, ref, alt_field, qual, filter_status, info_field = fields[:8]

        try:
            pos = int(pos_str)
        except ValueError as exc:
            raise VCFParsingError(
                f"Line {line_no}: POS '{pos_str}' is not a valid integer."
            ) from exc

        info = self._parse_info(info_field)

        # A single VCF record may list multiple comma-separated ALT
        # alleles; normalize to one Variant per ALT allele so every
        # downstream stage only ever deals with a single ref/alt pair.
        alt_alleles = alt_field.split(",") if alt_field != "." else ["."]
        for alt in alt_alleles:
            if alt == "." or ref == "":
                logger.warning(f"Skipping line {line_no}: missing REF/ALT allele.")
                self.skip_counts["missing_ref_alt"] += 1
                continue

            alt_upper = alt.upper()
            if alt_upper == "*":
                # VCF spec '*' ALT: this position is spanned by a
                # deletion called on a *different* allele/sample; there
                # is no actual base substitution here at all. Splicing
                # a literal '*' character into a reference window (as
                # sequence_context.build_context does for every other
                # variant) produces a DNA/RNA string containing '*',
                # which every downstream model (RNA-FM, ESM-2, and the
                # DNA foundation models) rejects as an invalid base.
                # There is no sequence-level analysis to perform for a
                # spanning-deletion placeholder, so it's skipped here,
                # before any sequence context is ever built.
                logger.warning(
                    f"Skipping line {line_no} ({chrom}:{pos}): ALT='*' is a "
                    f"spanning-deletion placeholder (VCF spec), not a real "
                    f"allele -- no sequence-level analysis is possible for it."
                )
                self.skip_counts["spanning_deletion_star"] += 1
                continue
            if alt_upper.startswith("<") and alt_upper.endswith(">"):
                # Symbolic/structural ALT alleles (e.g. '<DEL>', '<INS>',
                # '<DUP>', '<CNV>') describe a structural event, not a
                # literal base sequence. Splicing the literal text
                # "<DEL>" into a reference window is exactly as invalid
                # as splicing in '*' -- same failure mode, same fix.
                logger.warning(
                    f"Skipping line {line_no} ({chrom}:{pos}): symbolic "
                    f"structural ALT allele '{alt}' has no literal base "
                    f"sequence -- no sequence-level analysis is possible "
                    f"for it."
                )
                self.skip_counts["symbolic_structural_allele"] += 1
                continue

            yield Variant(
                chrom=chrom,
                pos=pos,
                variant_id=variant_id,
                ref=ref.upper(),
                alt=alt.upper(),
                qual=None if qual == "." else qual,
                filter_status=None if filter_status == "." else filter_status,
                info=info,
            )

    @staticmethod
    def _parse_info(info_field: str) -> Dict[str, str]:
        info: Dict[str, str] = {}
        if info_field in (".", ""):
            return info
        for entry in info_field.split(";"):
            if "=" in entry:
                key, _, value = entry.partition("=")
                info[key] = value
            else:
                info[entry] = "true"
        return info
