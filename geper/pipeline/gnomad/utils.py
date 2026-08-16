"""
Shared, dependency-free helpers for the gnomAD integration: build
normalization/auto-detection, variant-key construction, and INFO/JSON
field parsing. No network, no torch/tensorflow -- these are pure
functions so they're trivially unit-testable in isolation (see
tests/test_gnomad_utils.py), the same shape as
`pipeline/models/mmsplice/utils.py`.
"""

from typing import Any, Dict, Optional, Tuple

from pipeline.gnomad.models import POPULATIONS

_GRCH37_MARKERS = ("grch37", "hg19", "b37")
_GRCH38_MARKERS = ("grch38", "hg38", "b38")


def normalize_build(assembly: Optional[str]) -> str:
    """
    Map any of GEPER's accepted assembly spellings to exactly one of
    the two labels this module ever uses internally: "GRCh38" or
    "GRCh37". Mirrors `models/alphamissense.py::resolve_genome_label`
    and `pipeline/assembly_validator.py`'s marker lists, so all three
    genome-build-aware stages agree on the same input spellings.

    Defaults to GRCh38 when `assembly` is None/unrecognized -- gnomAD
    v4's primary, current release is GRCh38-based; GRCh37 is only used
    when the pipeline's resolved assembly (CLI flag or VCF-header
    auto-detection) explicitly says so, matching requirement #2
    ("Support GRCh38. Automatically detect GRCh37.").
    """
    if not assembly:
        return "GRCh38"
    label = assembly.strip().lower().replace("-", "").replace("_", "")
    if label in _GRCH37_MARKERS:
        return "GRCh37"
    if label in _GRCH38_MARKERS:
        return "GRCh38"
    return "GRCh38"


def classify_variant_type(ref: str, alt: str) -> str:
    """SNV / insertion / deletion classification, matching Variant.variant_type."""
    if len(ref) == 1 and len(alt) == 1:
        return "SNV"
    if len(ref) < len(alt):
        return "insertion"
    if len(ref) > len(alt):
        return "deletion"
    return "MNV"


def _mt_aware_bare_chrom(chrom: str) -> str:
    """
    Strip an optional 'chr' prefix, then canonicalize any M-family
    spelling ('MT', 'M', 'chrM', 'chrMT', any case) to gnomAD's own
    real token: 'M' -- never 'MT'. Round 29: gnomAD is the one resource
    GEPER integrates that consistently uses 'M', not 'MT', across every
    convention it actually has (its GRCh38 site VCFs/browser use
    'chrM' -- confirmed via gnomAD's own mtDNA-release documentation,
    which describes variants as being called "in GRCh38 chrM"; its
    GraphQL/browser dash-joined variant IDs for the mtDNA-specific
    dataset are of the form 'M-3243-A-G', bare 'M', never 'MT'). This
    is the opposite convention from Ensembl/NCBI Entrez (both want
    'MT' -- see `pipeline/hgvs_utils.py::_strip_chr`), which is exactly
    why this module keeps its own local M-family handling instead of
    reusing that helper or a shared `normalize_chrom(chrom, target=...)`
    -- round 15's own conclusion, not reopened here: the real targets
    genuinely disagree per resource, so one shared function is just
    another way to apply the wrong one.
    """
    bare = chrom[3:] if chrom.lower().startswith("chr") else chrom
    return "M" if bare.lower() in ("m", "mt") else bare


def normalize_chrom(chrom: str, *, with_chr_prefix: bool) -> str:
    """
    gnomAD's browser/GraphQL API and its GRCh38 site VCFs use a
    'chr'-prefixed contig name (chr1, chrX...); its legacy GRCh37 site
    VCFs do not. Callers pass which convention the specific data
    source in use expects; this never guesses silently.

    Round 29: the mitochondrial contig is gnomAD's own special case --
    see `_mt_aware_bare_chrom`'s docstring. Before this fix, a bare
    strip-and-pass-through mapped 'MT'/'chrMT' to 'chrMT' (never a real
    gnomAD contig) and 'M'/'chrM' to 'M' (missing the 'chr' prefix
    GRCh38 needs) -- wrong for 2 of the 4 real-world spellings in each
    mode. Currently unreachable in production (gnomAD's main GraphQL/
    tabix integration has no mtDNA dataset wired in at all -- see
    ROUND_CANDIDATES.md's round 14/29 entries), fixed anyway as
    correctness-for-its-own-sake, the same standard applied to every
    other confirmed-wrong chrom-normalization site in this codebase.
    """
    bare = _mt_aware_bare_chrom(chrom)
    return f"chr{bare}" if with_chr_prefix else bare


def variant_key(chrom: str, pos: int, ref: str, alt: str, build: str) -> str:
    """
    Stable cache/dedup key. Build-qualified since the same chrom/pos
    means different loci across builds.

    Round 29: confirmed still correctly bespoke, not a bug, despite
    using the same unqualified bare-strip `normalize_chrom`/
    `gnomad_variant_id` needed fixing for. This function is purely
    internal (an in-process cache/dedup key -- see this module's own
    docstring -- never sent to gnomAD or compared against any external
    resource's naming convention), so it has no "correct" external
    target to match; it only needs to be a stable, deterministic
    function of its own input, which a bare-strip already is. The one
    theoretical gap -- two different VCFs spelling the same MT locus
    differently ('MT' vs 'chrM') could produce different keys in
    `pipeline/gnomad/cache.py`'s cross-run disk-persisted cache -- is a
    cache-efficiency question, not a correctness one (no wrong data is
    ever returned), and is moot today regardless: gnomAD's compartment
    is never queried for chrM at all (round 14's compartment gate), so
    this function is never called with an MT-spelled chrom in the first
    place. Left as-is, per round 15's own verdict.
    """
    bare_chrom = chrom[3:] if chrom.lower().startswith("chr") else chrom
    return f"{build}:{bare_chrom}:{pos}:{ref.upper()}:{alt.upper()}"


def gnomad_variant_id(chrom: str, pos: int, ref: str, alt: str) -> str:
    """
    gnomAD's own '1-55516888-G-GA' dash-joined variant ID format, used
    by its GraphQL API.

    Round 29: for the mitochondrial contig, gnomAD's own real dash-ID
    format is 'M-<pos>-<ref>-<alt>' (bare 'M', never 'MT' -- see
    `_mt_aware_bare_chrom`'s docstring). Before this fix, a bare strip
    mapped 'MT'/'chrMT' input to the wrong 'MT-...' form. Currently
    unreachable in production for the same reason `normalize_chrom` is
    (no gnomAD-mtDNA dataset wired in); fixed anyway as correctness-
    for-its-own-sake.
    """
    bare_chrom = _mt_aware_bare_chrom(chrom)
    return f"{bare_chrom}-{pos}-{ref.upper()}-{alt.upper()}"


def parse_vcf_info_field(info_str: str) -> Dict[str, str]:
    """
    Parse a VCF INFO column (`key=value;key2=value2;flag`) into a flat
    dict. Flags with no '=' are stored as ``"True"``. Used by the local
    tabix-indexed provider to read AC/AN/nhomalt/AC_afr/etc. out of a
    gnomAD site VCF record without pulling in pysam/cyvcf2 (see
    `pipeline/vcf_parser.py`'s module docstring for why this project
    avoids those dependencies entirely).
    """
    fields: Dict[str, str] = {}
    for token in info_str.split(";"):
        token = token.strip()
        if not token:
            continue
        if "=" in token:
            key, _, value = token.partition("=")
            fields[key] = value
        else:
            fields[token] = "True"
    return fields


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_population_counts_from_info(
    info: Dict[str, str],
) -> Dict[str, Tuple[Optional[int], Optional[int], Optional[int], Optional[int]]]:
    """
    Given a parsed VCF INFO dict from a gnomAD site VCF, return
    ``{population: (ac, an, hom, hemi)}`` for every population in
    `pipeline.gnomad.models.POPULATIONS`, following gnomAD's own site
    VCF INFO key convention: `AC_afr`, `AN_afr`, `nhomalt_afr`,
    `AC_hemi_afr` (hemi is only meaningful/present for non-PAR X/Y
    sites; absent elsewhere, which this returns as None, not 0, so
    callers don't misreport "0 hemizygotes" for an autosomal variant).
    """
    result = {}
    for pop in POPULATIONS:
        # gnomAD's own VCFs use "oth" for the non-europop/etc bucket in
        # older releases and "remaining" in v4+; accept either INFO
        # key spelling so this works against both.
        suffixes = [pop] if pop != "remaining" else ["remaining", "oth"]
        ac = an = hom = hemi = None
        for suffix in suffixes:
            ac = ac if ac is not None else _to_int(info.get(f"AC_{suffix}"))
            an = an if an is not None else _to_int(info.get(f"AN_{suffix}"))
            hom = hom if hom is not None else _to_int(info.get(f"nhomalt_{suffix}"))
            hemi = hemi if hemi is not None else _to_int(info.get(f"AC_hemi_{suffix}"))
        result[pop] = (ac, an, hom, hemi)
    return result


def extract_global_counts_from_info(info: Dict[str, str]) -> Dict[str, Optional[float]]:
    """Global (all-population) AC/AN/AF/nhomalt/AC_hemi from a parsed gnomAD site-VCF INFO dict."""
    return {
        "ac": _to_int(info.get("AC")),
        "an": _to_int(info.get("AN")),
        "af": _to_float(info.get("AF")),
        "hom": _to_int(info.get("nhomalt")),
        "hemi": _to_int(info.get("AC_hemi")),
    }
