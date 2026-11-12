"""
clinical/genome_analysis/reference.py
──────────────────────────────────
Static chromosome reference metadata for GRCh37 and GRCh38.

SOURCE, EXACT. Both tables were generated from the NCBI Datasets "assembly
report" files for the two RefSeq assemblies below, restricted to the 25
primary sequences (chromosomes 1-22, X, Y and the mitochondrion):

  GRCh37  GRCh37.p13   RefSeq GCF_000001405.25   GenBank GCA_000001405.14
          report dated 2013-06-28
          https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/001/405/
              GCF_000001405.25_GRCh37.p13/GCF_000001405.25_GRCh37.p13_assembly_report.txt
          sha256 0097e6b24cc9ae5cb4fb5c075dffbae36b4ec0c90a61d428f039eb82e64e4a17
  GRCh38  GRCh38.p14   RefSeq GCF_000001405.40   GenBank GCA_000001405.29
          report dated 2022-02-03
          https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/001/405/
              GCF_000001405.40_GRCh38.p14/GCF_000001405.40_GRCh38.p14_assembly_report.txt
          sha256 04e40091e427dae4f0c7dcc1b67792b8d4d5413903ea6b1d171840664e2da989

Retrieved 2026-11-10. Patch releases change only patch/alt-locus sequences, not
the primary chromosome lengths used here. The mitochondrial record (rCRS,
NC_012920.1 / J01415.2, 16,569 bp) is identical in both assemblies.

LICENCE. NCBI places no restriction on the use of its data beyond the notice at
https://www.ncbi.nlm.nih.gov/home/about/policies/ ; the GRC assembly data is
released without use restrictions. Recorded in THIRD_PARTY_NOTICES.md.

DELIBERATELY NOT HERE: cytogenetic bands and centromere coordinates. NCBI
assembly reports do not carry them, and UCSC's cytoBand table was not adopted
because its redistribution terms were not established. Nothing in this module
may be read as banding information. Add them only with the licence recorded.

Pure data. No I/O, no third-party imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

ASSEMBLY_GRCH37 = "GRCh37"
ASSEMBLY_GRCH38 = "GRCh38"
SUPPORTED_ASSEMBLIES: Tuple[str, ...] = (ASSEMBLY_GRCH37, ASSEMBLY_GRCH38)

#: Canonical chromosome names, in display order. "MT" is the canonical
#: mitochondrial name (NCBI's), chosen once so that chrM / M / MT all collapse.
CANONICAL_CHROMOSOMES: Tuple[str, ...] = tuple(str(n) for n in range(1, 23)) + ("X", "Y", "MT")

REFERENCE_SOURCES: Dict[str, Dict[str, str]] = {
    ASSEMBLY_GRCH37: {
        "assembly_name": "GRCh37.p13",
        "refseq_assembly": "GCF_000001405.25",
        "genbank_assembly": "GCA_000001405.14",
        "report_date": "2013-06-28",
        "report_sha256": "0097e6b24cc9ae5cb4fb5c075dffbae36b4ec0c90a61d428f039eb82e64e4a17",
        "provider": "NCBI / Genome Reference Consortium",
    },
    ASSEMBLY_GRCH38: {
        "assembly_name": "GRCh38.p14",
        "refseq_assembly": "GCF_000001405.40",
        "genbank_assembly": "GCA_000001405.29",
        "report_date": "2022-02-03",
        "report_sha256": "04e40091e427dae4f0c7dcc1b67792b8d4d5413903ea6b1d171840664e2da989",
        "provider": "NCBI / Genome Reference Consortium",
    },
}


@dataclass(frozen=True)
class Chromosome:
    assembly: str
    name: str  # canonical: "1".."22", "X", "Y", "MT"
    length_bp: int
    refseq_accession: str
    genbank_accession: str

    @property
    def kind(self) -> str:
        if self.name == "MT":
            return "mitochondrial"
        if self.name in ("X", "Y"):
            return "sex_chromosome"
        return "autosome"


# (name, RefSeq accession, GenBank accession, length in bp)
_GRCH37 = (
    ("1", "NC_000001.10", "CM000663.1", 249250621),
    ("2", "NC_000002.11", "CM000664.1", 243199373),
    ("3", "NC_000003.11", "CM000665.1", 198022430),
    ("4", "NC_000004.11", "CM000666.1", 191154276),
    ("5", "NC_000005.9", "CM000667.1", 180915260),
    ("6", "NC_000006.11", "CM000668.1", 171115067),
    ("7", "NC_000007.13", "CM000669.1", 159138663),
    ("8", "NC_000008.10", "CM000670.1", 146364022),
    ("9", "NC_000009.11", "CM000671.1", 141213431),
    ("10", "NC_000010.10", "CM000672.1", 135534747),
    ("11", "NC_000011.9", "CM000673.1", 135006516),
    ("12", "NC_000012.11", "CM000674.1", 133851895),
    ("13", "NC_000013.10", "CM000675.1", 115169878),
    ("14", "NC_000014.8", "CM000676.1", 107349540),
    ("15", "NC_000015.9", "CM000677.1", 102531392),
    ("16", "NC_000016.9", "CM000678.1", 90354753),
    ("17", "NC_000017.10", "CM000679.1", 81195210),
    ("18", "NC_000018.9", "CM000680.1", 78077248),
    ("19", "NC_000019.9", "CM000681.1", 59128983),
    ("20", "NC_000020.10", "CM000682.1", 63025520),
    ("21", "NC_000021.8", "CM000683.1", 48129895),
    ("22", "NC_000022.10", "CM000684.1", 51304566),
    ("X", "NC_000023.10", "CM000685.1", 155270560),
    ("Y", "NC_000024.9", "CM000686.1", 59373566),
    ("MT", "NC_012920.1", "J01415.2", 16569),
)

_GRCH38 = (
    ("1", "NC_000001.11", "CM000663.2", 248956422),
    ("2", "NC_000002.12", "CM000664.2", 242193529),
    ("3", "NC_000003.12", "CM000665.2", 198295559),
    ("4", "NC_000004.12", "CM000666.2", 190214555),
    ("5", "NC_000005.10", "CM000667.2", 181538259),
    ("6", "NC_000006.12", "CM000668.2", 170805979),
    ("7", "NC_000007.14", "CM000669.2", 159345973),
    ("8", "NC_000008.11", "CM000670.2", 145138636),
    ("9", "NC_000009.12", "CM000671.2", 138394717),
    ("10", "NC_000010.11", "CM000672.2", 133797422),
    ("11", "NC_000011.10", "CM000673.2", 135086622),
    ("12", "NC_000012.12", "CM000674.2", 133275309),
    ("13", "NC_000013.11", "CM000675.2", 114364328),
    ("14", "NC_000014.9", "CM000676.2", 107043718),
    ("15", "NC_000015.10", "CM000677.2", 101991189),
    ("16", "NC_000016.10", "CM000678.2", 90338345),
    ("17", "NC_000017.11", "CM000679.2", 83257441),
    ("18", "NC_000018.10", "CM000680.2", 80373285),
    ("19", "NC_000019.10", "CM000681.2", 58617616),
    ("20", "NC_000020.11", "CM000682.2", 64444167),
    ("21", "NC_000021.9", "CM000683.2", 46709983),
    ("22", "NC_000022.11", "CM000684.2", 50818468),
    ("X", "NC_000023.11", "CM000685.2", 156040895),
    ("Y", "NC_000024.10", "CM000686.2", 57227415),
    ("MT", "NC_012920.1", "J01415.2", 16569),
)

_REFERENCE: Dict[str, Dict[str, Chromosome]] = {
    assembly: {name: Chromosome(assembly, name, length, refseq, genbank) for name, refseq, genbank, length in rows}
    for assembly, rows in ((ASSEMBLY_GRCH37, _GRCH37), (ASSEMBLY_GRCH38, _GRCH38))
}

# Accepted spellings of the two supported builds. EXACT matches only (after
# lower-casing and dropping '-', '_', '.', ' '): unlike
# pipeline/assembly_validator._normalize_build, which treats any token containing
# "37" or "38" as a build, a value that is not recognisably one of these is
# UNSUPPORTED here rather than guessed -- a wrong build silently mislabels every
# length and accession in the response.
_ASSEMBLY_ALIASES = {
    "grch37": ASSEMBLY_GRCH37,
    "hg19": ASSEMBLY_GRCH37,
    "b37": ASSEMBLY_GRCH37,
    "grch38": ASSEMBLY_GRCH38,
    "hg38": ASSEMBLY_GRCH38,
    "b38": ASSEMBLY_GRCH38,
}


def canonical_assembly(value: object) -> Optional[str]:
    """'hg19' / 'GRCh37' / 'b37' -> 'GRCh37' (likewise 38); anything else -> None."""
    if not isinstance(value, str):
        return None
    token = "".join(ch for ch in value.lower() if ch not in "-_. ")
    return _ASSEMBLY_ALIASES.get(token)


def chromosomes_for(assembly: str) -> Tuple[Chromosome, ...]:
    """All 25 chromosomes of a SUPPORTED assembly, in canonical display order."""
    table = _REFERENCE.get(assembly)
    if table is None:
        raise ValueError(f"Unsupported assembly {assembly!r}; supported: {', '.join(SUPPORTED_ASSEMBLIES)}")
    return tuple(table[name] for name in CANONICAL_CHROMOSOMES)


def get_chromosome(assembly: str, name: str) -> Optional[Chromosome]:
    """One chromosome by CANONICAL name, or None. Raises on an unsupported assembly."""
    table = _REFERENCE.get(assembly)
    if table is None:
        raise ValueError(f"Unsupported assembly {assembly!r}; supported: {', '.join(SUPPORTED_ASSEMBLIES)}")
    return table.get(name)
