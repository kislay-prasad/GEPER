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
from typing import Any, Dict, Iterator, List, Optional

from pipeline.stage_schemas import StageStatus
from utils.exceptions import VCFParsingError
from utils.logger import get_logger

logger = get_logger(__name__)

_REQUIRED_COLUMNS = ["CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO"]

# ---------------------------------------------------------------------------
# Per-sample variant allele fraction (VAF): within-sample read support.
#
# NOT the same quantity as gnomAD's POPULATION allele frequency (see
# `pipeline/gnomad/utils.py`, which parses an INFO `AF` key into the
# population frequency that feeds PM2/BS1). This one is "what fraction of
# THIS patient's reads carry the variant" -- used for zygosity and somatic
# mosaicism, sourced only from the per-sample FORMAT columns, never from
# INFO. The two must never be conflated in code, field names, or report
# text; they are different numbers that unfortunately share a name.
#
# Three states, not two -- the same distinction (and the same
# `{status, value, reason}` shape and `StageStatus` vocabulary) that
# `report/clinical_report_builder.py::_parse_one_qc_metric` already makes
# for run-level QC metrics, reused here rather than invented a second
# time:
#   FOUND    -- a real fraction, computed from FORMAT/AD (or read from a
#               FORMAT/AF field when AD is absent).
#   NOT_RUN  -- nothing was ever measured: the VCF carries no per-sample
#               genotype columns at all, or this record's FORMAT declares
#               neither AD nor AF. NOT zero, NOT "0.0", and deliberately
#               NOT `NOT_FOUND` -- "checked and confirmed absent" has no
#               reading for a number that was never measured.
#   ERROR    -- FORMAT is present but this sample's values are malformed
#               or unusable. An absence and a failure must never collapse
#               to the same value, which is exactly what happens when a
#               parse failure is written back as `None` alongside a field
#               that was simply never there.
# ---------------------------------------------------------------------------

VAF_NO_SAMPLE_COLUMNS_REASON = (
    "This VCF carries no per-sample genotype columns (a sites-only or "
    "annotation-only VCF), so no allele fraction was ever measured for this "
    "variant. This is an absence of measurement, not a fraction of zero."
)

_VAF_NO_AD_OR_AF_REASON = (
    "This record's FORMAT declares neither AD (allele depths) nor AF, so no "
    "allele fraction can be derived for this sample. Nothing was measured "
    "here; this is not a fraction of zero."
)


def _vaf_state(
    status: StageStatus,
    *,
    value: Optional[float] = None,
    reason: Optional[str] = None,
    source: Optional[str] = None,
    alt_depth: Optional[int] = None,
    depth: Optional[int] = None,
    dp: Optional[int] = None,
) -> Dict[str, Any]:
    """
    The single constructor for a VAF state dict, so `value`/`source`/
    `alt_depth`/`depth` can only ever be populated on a FOUND status.
    Any other status discards them here rather than trusting every call
    site to remember -- the same choke-point discipline
    `_parse_one_qc_metric` applies to run-level QC (a number that did not
    come back FOUND must not survive to a renderer that could print it).
    """
    if status is not StageStatus.FOUND:
        return {
            "status": status.value,
            "value": None,
            "reason": reason,
            "source": None,
            "alt_depth": None,
            "depth": None,
            "dp": dp,
        }
    return {
        "status": StageStatus.FOUND.value,
        "value": value,
        "reason": reason,
        "source": source,
        "alt_depth": alt_depth,
        "depth": depth,
        "dp": dp,
    }


def _parse_int_or_none(raw: Optional[str]) -> Optional[int]:
    """`None` for a missing or '.'-valued field, or one that isn't an integer."""
    if raw is None or raw in (".", ""):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _sample_allele_fraction(
    format_keys: List[str],
    sample_values: List[str],
    alt_index: int,
) -> Dict[str, Any]:
    """
    One sample's VAF state for one ALT allele.

    `alt_index` is the 1-based index of this ALT within the record's
    ORIGINAL comma-separated ALT list, which is how AD is indexed
    (AD = [REF_depth, ALT1_depth, ALT2_depth, ...]). It must be
    established before any ALT is skipped, or every allele after a
    skipped one silently reads the wrong depth.

    Source precedence, and why:
      1. FORMAT/AD -- gives numerator AND denominator, so the report can
         state 23/48 rather than a bare 0.48. The NABL clause this field
         exists for asks the lab to define when a low-frequency call needs
         extra verification to be told apart from instrument error, and
         that determination is impossible without a denominator: 0.03 at
         3/100 reads and 0.03 at 3/1000 reads are different claims.
      2. FORMAT/AF -- only when AD is absent. Caller-defined (some emit an
         observed alt fraction, some a posterior estimate) and carries no
         denominator, so it is recorded with `source="format_af"` and null
         depths, making the missing denominator visible rather than
         implied.
    INFO/AF is never read here at any precedence: in this codebase that
    key already means the gnomAD POPULATION frequency (pipeline/gnomad/
    utils.py), and per the VCF 4.x spec INFO/AF is a cohort estimate while
    only FORMAT is per-sample.

    The denominator is `sum(AD)`, not DP. DP is post-filter total depth and
    can legitimately differ from the summed allele depths, so AD[i]/DP can
    exceed 1 or understate the fraction. DP is still reported alongside
    (as `dp`) because a reviewer wants it, but it is never divided by. A
    DP that doesn't parse therefore does NOT invalidate the fraction -- it
    is recorded as null and the AD-derived value stands.
    """
    # VCF 4.x: "Trailing fields can be dropped." A sample column with
    # FEWER values than FORMAT declares is therefore LEGAL, and the
    # dropped trailing keys are genuinely absent for this sample -- so
    # zip()'s truncation is the correct reading, not a silent bug. More
    # values than FORMAT declares has no legal reading at all, and is the
    # case that must not be quietly truncated into an apparent absence.
    if len(sample_values) > len(format_keys):
        return _vaf_state(
            StageStatus.ERROR,
            reason=(
                f"This sample column carries {len(sample_values)} value(s) but FORMAT declares only "
                f"{len(format_keys)} field(s); the record is malformed and no allele fraction can be "
                f"trusted from it."
            ),
        )

    fmt = dict(zip(format_keys, sample_values))
    dp = _parse_int_or_none(fmt.get("DP"))

    raw_ad = fmt.get("AD")
    if raw_ad is not None and raw_ad not in (".", ""):
        try:
            ad = [int(x) for x in raw_ad.split(",")]
        except ValueError:
            return _vaf_state(
                StageStatus.ERROR,
                reason=f"FORMAT/AD value {raw_ad!r} is not a comma-separated list of integers.",
                dp=dp,
            )
        if alt_index >= len(ad):
            return _vaf_state(
                StageStatus.ERROR,
                reason=(
                    f"FORMAT/AD carries {len(ad)} value(s) (expected one per allele, REF first), so "
                    f"there is no depth at index {alt_index} for this ALT allele."
                ),
                dp=dp,
            )
        total = sum(ad)
        if total == 0:
            # A real distinction, not a rounding concern: no reads at all
            # means the fraction is undefined (0/0), which is not the same
            # claim as "0% of the reads carry it".
            return _vaf_state(
                StageStatus.ERROR,
                reason=(
                    "FORMAT/AD sums to zero -- there are no reads at this position for this sample, so "
                    "an allele fraction is undefined here rather than zero."
                ),
                dp=dp,
            )
        return _vaf_state(
            StageStatus.FOUND,
            value=ad[alt_index] / total,
            source="format_ad",
            alt_depth=ad[alt_index],
            depth=total,
            dp=dp,
        )

    raw_af = fmt.get("AF")
    if raw_af is not None and raw_af not in (".", ""):
        af_values = raw_af.split(",")
        # FORMAT/AF carries one value per ALT allele (no leading REF
        # entry, unlike AD), so this ALT's 1-based `alt_index` is at
        # 0-based position `alt_index - 1`.
        if alt_index - 1 >= len(af_values):
            return _vaf_state(
                StageStatus.ERROR,
                reason=(
                    f"FORMAT/AF carries {len(af_values)} value(s) (expected one per ALT allele), so "
                    f"there is no value for ALT allele {alt_index}."
                ),
                dp=dp,
            )
        try:
            af = float(af_values[alt_index - 1])
        except ValueError:
            return _vaf_state(
                StageStatus.ERROR,
                reason=f"FORMAT/AF value {af_values[alt_index - 1]!r} is not a number.",
                dp=dp,
            )
        return _vaf_state(StageStatus.FOUND, value=af, source="format_af", dp=dp)

    return _vaf_state(StageStatus.NOT_RUN, reason=_VAF_NO_AD_OR_AF_REASON, dp=dp)


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
    # Per-sample within-sample allele fraction, keyed by the VCF's own
    # genotype column name: {sample_name: {status, value, reason, source,
    # alt_depth, depth, dp}}. See this module's VAF comment block for the
    # three states and why an empty mapping (no genotype columns at all)
    # is a distinct, honest reading rather than a missing number.
    allele_fractions: Dict[str, Dict[str, Any]] = field(default_factory=dict)

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
            # Additive key. An empty mapping means this VCF had no
            # genotype columns at all -- read it as "never measured", the
            # reading `report/clinical_report_builder.py::
            # variant_allele_fraction_text` renders it as.
            "variant_allele_fractions": self.allele_fractions,
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
                            f"Data line encountered before '#CHROM' header at line {line_no} of '{self.filepath}'."
                        )

                    data_line_seen = True
                    yield from self._parse_data_line(line, line_no)

                if not self.column_names:
                    raise VCFParsingError(f"'{self.filepath}' has no '#CHROM' header line -- not a valid VCF.")
                if not data_line_seen:
                    logger.warning(f"'{self.filepath}' contains a header but no variant records.")

                if self.skip_counts:
                    total_skipped = sum(self.skip_counts.values())
                    breakdown = ", ".join(f"{reason}={count}" for reason, count in sorted(self.skip_counts.items()))
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
            raise VCFParsingError(f"Line {line_no}: POS '{pos_str}' is not a valid integer.") from exc

        info = self._parse_info(info_field)

        # Columns 9+ (FORMAT, then one column per sample) -- previously
        # split out of the line here and then dropped on the floor. The
        # per-sample allele fraction the report needs was always present
        # in the input and simply never read.
        format_keys = fields[8].split(":") if len(fields) > 8 else []
        sample_columns = fields[9:]
        record_error = self._sample_column_count_error(sample_columns, line_no)

        # A single VCF record may list multiple comma-separated ALT
        # alleles; normalize to one Variant per ALT allele so every
        # downstream stage only ever deals with a single ref/alt pair.
        alt_alleles = alt_field.split(",") if alt_field != "." else ["."]
        # `alt_index` is established HERE, over the original ALT list, and
        # is what FORMAT/AD is indexed by. The `continue`s below skip
        # ALTs without advancing it, which is the point: deriving the
        # index from a counter incremented after those skips would shift
        # every subsequent allele's depth by one, silently.
        for alt_index, alt in enumerate(alt_alleles, start=1):
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
                allele_fractions=self._allele_fractions(format_keys, sample_columns, alt_index, record_error),
            )

    def _sample_column_count_error(self, sample_columns: List[str], line_no: int) -> Optional[str]:
        """
        A record whose sample-column count disagrees with the `#CHROM`
        header's is malformed, and no sample column on it can be matched
        to a sample name with any confidence. Returns the reason text for
        an ERROR state, or `None` when the counts agree.

        Deliberately not a `VCFParsingError`: the eight fixed columns
        parsed fine, so the variant itself is still usable for every stage
        that doesn't need genotypes. Only the allele fraction is lost, and
        it is lost loudly rather than by falling back to an absence.
        """
        if not self.samples or len(sample_columns) == len(self.samples):
            return None
        return (
            f"Line {line_no} carries {len(sample_columns)} sample column(s) but the '#CHROM' header "
            f"declares {len(self.samples)}; the record is malformed and no sample column can be "
            f"reliably matched to a sample name."
        )

    def _allele_fractions(
        self,
        format_keys: List[str],
        sample_columns: List[str],
        alt_index: int,
        record_error: Optional[str],
    ) -> Dict[str, Dict[str, Any]]:
        """
        Per-sample VAF states for one ALT allele, keyed by sample name.

        Returns an EMPTY mapping when the VCF declares no genotype columns
        at all -- there are no samples to key by, and that emptiness is
        itself the honest reading ("never measured"), rendered as such by
        `report/clinical_report_builder.py::variant_allele_fraction_text`
        rather than as a missing or zero number.
        """
        if not self.samples:
            return {}
        if record_error is not None:
            return {name: _vaf_state(StageStatus.ERROR, reason=record_error) for name in self.samples}
        return {
            name: _sample_allele_fraction(format_keys, values.split(":"), alt_index)
            for name, values in zip(self.samples, sample_columns)
        }

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
