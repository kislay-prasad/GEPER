"""
clinical/genome_analysis/summary.py
────────────────────────────────
Pure variant-to-chromosome aggregation over ONE interpretation `run_document`.

INPUT. The JSON document `interpretations.run_document` holds, as written by
geper/report/json_builder.py: `assembly`, `run_complete`, and
`variants[].variant.{chrom,pos,ref,alt,variant_type}`. Those variants are the
small variants the existing VCF parser (pipeline/vcf_parser.py) produced; this
module parses no VCF. The `assembly` is the value pipeline/assembly_validator.py
settled on before any variant was processed.

OUTPUT. An `AnalysisSummary`: per-chromosome records for the 25 primary
sequences of the document's assembly, each carrying one of three statuses --

    variants_present      >= 1 mapped small variant on this chromosome
    no_variants_reported  the run completed and reported none here
    not_assessed          the run did not complete (or the document is unusable)

-- and NEVER a fourth meaning. "no_variants_reported" is a statement about the
interpretation's output, not about the genome: the document does not record
which regions the sequencing covered, so absence of a variant is not evidence
that the chromosome is normal. That is stated in `LIMITATIONS` on every summary.

BUILD IS MANDATORY. A document whose `assembly` is missing or not recognisably
GRCh37/GRCh38 yields status `assembly_unavailable` and NO chromosome table: a
guessed build would mislabel every length and accession. Nothing is merged
across builds; one summary describes one document in one build.

NOTHING IS SILENTLY DROPPED. A variant on a contig outside chromosomes 1-22/X/Y/
MT (alt loci, decoys) is counted in `unmapped_variant_count`; a malformed one,
or one whose position lies beyond the end of its chromosome in the stated build
(the signature of a wrong-build label), in `invalid_variant_count`. Either raises
a warning.

NOT HERE, ON PURPOSE: ACMG classifications, scores, gene names, file paths --
the run_document is pipeline output, and a released report may differ from it
after human review. Only coordinates and alleles are surfaced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .normalization import normalize_contig
from .reference import (
    REFERENCE_SOURCES,
    Chromosome,
    canonical_assembly,
    chromosomes_for,
)

STATUS_VARIANTS_PRESENT = "variants_present"
STATUS_NO_VARIANTS = "no_variants_reported"
STATUS_NOT_ASSESSED = "not_assessed"

ANALYSIS_OK = "ok"
ANALYSIS_ASSEMBLY_UNAVAILABLE = "assembly_unavailable"
ANALYSIS_DOCUMENT_UNREADABLE = "document_unreadable"

_VARIANT_TYPES = frozenset({"SNV", "insertion", "deletion", "MNV"})

LIMITATIONS: Tuple[str, ...] = (
    "Only small variants from the interpreted VCF are summarised here.",
    "No variants reported for a chromosome is not evidence that it is normal: the regions the sequencing covered are not recorded.",
    "Copy-number variants and structural variants are not assessed; symbolic structural records in a VCF are not analysed.",
    "No karyotype is determined or implied. A karyotype requires a validated cytogenetic test; it cannot be derived from variant calls.",
    "Biological sex and chromosome complement are not inferred from X or Y variants.",
    "Coordinates are only valid in the stated genome assembly.",
)

_NOT_ASSESSED_EVIDENCE: Dict[str, Dict[str, str]] = {
    "copy_number_variants": {"status": "not_assessed", "reason": "Copy-number analysis is not part of this analysis."},
    "structural_variants": {
        "status": "not_assessed",
        "reason": "Structural-variant analysis is not part of this analysis.",
    },
    "karyotype": {"status": "not_assessed", "reason": "No validated cytogenetic result is attached."},
}


@dataclass(frozen=True)
class ChromosomeSummary:
    chromosome: Chromosome
    status: str
    variants: Tuple[Dict[str, Any], ...] = ()

    @property
    def variant_count(self) -> int:
        return len(self.variants)

    def to_dict(self, *, include_variants: bool = False) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.chromosome.name,
            "kind": self.chromosome.kind,
            "assembly": self.chromosome.assembly,
            "length_bp": self.chromosome.length_bp,
            "refseq_accession": self.chromosome.refseq_accession,
            "genbank_accession": self.chromosome.genbank_accession,
            "small_variants": {"status": self.status, "count": self.variant_count},
        }
        if include_variants:
            out["small_variants"]["variants"] = [dict(v) for v in self.variants]
        return out


@dataclass(frozen=True)
class AnalysisSummary:
    status: str
    assembly: Optional[str]
    reason: Optional[str] = None
    chromosomes: Tuple[ChromosomeSummary, ...] = ()
    unmapped_variant_count: int = 0
    invalid_variant_count: int = 0
    warnings: Tuple[str, ...] = ()
    provenance: Dict[str, Any] = field(default_factory=dict)

    def chromosome(self, canonical_name: str) -> Optional[ChromosomeSummary]:
        for entry in self.chromosomes:
            if entry.chromosome.name == canonical_name:
                return entry
        return None

    def _evidence(self) -> Dict[str, Any]:
        small = (
            {"status": "assessed_from_interpreted_vcf"}
            if self.status == ANALYSIS_OK
            else {"status": STATUS_NOT_ASSESSED, "reason": self.reason}
        )
        return {"small_variants": small, **{k: dict(v) for k, v in _NOT_ASSESSED_EVIDENCE.items()}}

    def _base(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "assembly": self.assembly,
            "reason": self.reason,
            "evidence": self._evidence(),
            "unmapped_variant_count": self.unmapped_variant_count,
            "invalid_variant_count": self.invalid_variant_count,
            "warnings": list(self.warnings),
            "limitations": list(LIMITATIONS),
            "provenance": dict(self.provenance),
        }

    def to_dict(self) -> Dict[str, Any]:
        out = self._base()
        out["chromosomes"] = [c.to_dict() for c in self.chromosomes]
        return out

    def chromosome_to_dict(self, canonical_name: str) -> Dict[str, Any]:
        """The analysis envelope plus ONE chromosome with its variants listed."""
        out = self._base()
        entry = self.chromosome(canonical_name)
        out["chromosome"] = entry.to_dict(include_variants=True) if entry is not None else None
        return out


#: Sex chromosomes are withheld from the patient projection entirely (PCPNDT
#: and counselling policy are unresolved; see clinical/genome_endpoints.py).
PATIENT_WITHHELD_CHROMOSOMES = frozenset({"X", "Y"})

PATIENT_LIMITATIONS: Tuple[str, ...] = (
    "This is reference information about human chromosomes, not a result for you.",
    "No karyotype is determined or implied, and biological sex is not inferred.",
)

PATIENT_WITHHELD_NOTICE = (
    "Your genomic findings are not shown here. Please speak with the clinician who ordered your test."
)


def patient_projection(summary: "AnalysisSummary") -> Dict[str, Any]:
    """
    The ONLY view of an analysis a patient may be given, built by ALLOW-LIST from
    a summary and never by removing fields from one: static reference metadata for
    the build (names, kinds, lengths, accessions) and a statement that findings
    are withheld. It carries no variant, no coordinate, no allele, no count, no
    per-chromosome status, no provenance and nothing X/Y.

    Report release authorises a report document; this module's input is raw
    pipeline output, which release does not authorise. Until the release-content,
    consent and counselling policies are resolved, patient genomic data stays
    unavailable.
    """
    assembly = summary.assembly if summary.status == ANALYSIS_OK else None
    reference = []
    if assembly is not None:
        reference = [
            {
                "name": c.name,
                "kind": c.kind,
                "length_bp": c.length_bp,
                "refseq_accession": c.refseq_accession,
                "genbank_accession": c.genbank_accession,
            }
            for c in chromosomes_for(assembly)
            if c.name not in PATIENT_WITHHELD_CHROMOSOMES
        ]
    return {
        "assembly": assembly,
        "findings": {"status": "withheld", "reason": PATIENT_WITHHELD_NOTICE},
        "reference_chromosomes": reference,
        "limitations": list(PATIENT_LIMITATIONS),
    }


def _provenance(document: Mapping[str, Any], assembly: Optional[str]) -> Dict[str, Any]:
    raw = document.get("assembly")
    out: Dict[str, Any] = {
        "assembly_as_recorded": raw if isinstance(raw, str) else None,
        "assembly_source": "interpretation run document (assembly validated before the run)",
        "variant_source": "small variants parsed from the interpreted VCF",
    }
    for key in ("geper_version", "generated_at"):
        value = document.get(key)
        out[key] = value if isinstance(value, str) else None
    if assembly is not None:
        out["reference"] = dict(REFERENCE_SOURCES[assembly])
    return out


def _unusable(status: str, assembly: Optional[str], reason: str, document: Mapping[str, Any]) -> AnalysisSummary:
    return AnalysisSummary(
        status=status,
        assembly=assembly,
        reason=reason,
        warnings=(reason,),
        provenance=_provenance(document, assembly),
    )


def summarise_run_document(run_document: object) -> AnalysisSummary:
    """Never raises on a malformed document: it returns an unusable summary that says why."""
    if not isinstance(run_document, Mapping):
        return _unusable(ANALYSIS_DOCUMENT_UNREADABLE, None, "The interpretation record is not readable.", {})
    document: Mapping[str, Any] = run_document

    assembly = canonical_assembly(document.get("assembly"))
    if assembly is None:
        return _unusable(
            ANALYSIS_ASSEMBLY_UNAVAILABLE,
            None,
            "The genome assembly is missing or is not GRCh37/GRCh38, so coordinates cannot be placed on chromosomes.",
            document,
        )

    raw_variants = document.get("variants")
    if not isinstance(raw_variants, list):
        return _unusable(
            ANALYSIS_DOCUMENT_UNREADABLE, assembly, "The interpretation record has no readable variant list.", document
        )

    reference = {c.name: c for c in chromosomes_for(assembly)}
    by_chrom: Dict[str, List[Dict[str, Any]]] = {name: [] for name in reference}
    unmapped = invalid = 0

    for entry in raw_variants:
        variant = entry.get("variant") if isinstance(entry, Mapping) else None
        if not isinstance(variant, Mapping):
            invalid += 1
            continue
        name = normalize_contig(variant.get("chrom"))
        pos, ref, alt = variant.get("pos"), variant.get("ref"), variant.get("alt")
        if name is None:
            if isinstance(variant.get("chrom"), str) and variant.get("chrom"):
                unmapped += 1
            else:
                invalid += 1
            continue
        if (
            not isinstance(pos, int)
            or isinstance(pos, bool)
            or pos < 1
            or pos > reference[name].length_bp
            or not isinstance(ref, str)
            or not ref
            or not isinstance(alt, str)
            or not alt
        ):
            invalid += 1
            continue
        vtype = variant.get("variant_type")
        by_chrom[name].append(
            {
                "variant_key": f"{name}:{pos}:{ref}>{alt}",
                "chrom": name,
                "pos": pos,
                "ref": ref,
                "alt": alt,
                "variant_type": vtype if vtype in _VARIANT_TYPES else None,
            }
        )

    complete = document.get("run_complete") is True
    summaries: List[ChromosomeSummary] = []
    for name, chrom in reference.items():
        found = tuple(sorted(by_chrom[name], key=lambda v: (v["pos"], v["ref"], v["alt"])))
        if found:
            status = STATUS_VARIANTS_PRESENT
        else:
            status = STATUS_NO_VARIANTS if complete else STATUS_NOT_ASSESSED
        summaries.append(ChromosomeSummary(chrom, status, found))

    warnings: List[str] = []
    if not complete:
        warnings.append("The analysis run did not complete; chromosomes without variants are shown as not assessed.")
    if unmapped:
        warnings.append(
            f"{unmapped} variant(s) lie on contigs other than chromosomes 1-22, X, Y and MT and are not shown by chromosome."
        )
    if invalid:
        warnings.append(
            f"{invalid} variant(s) could not be placed (malformed, or a position beyond the end of the chromosome in {assembly})."
        )

    return AnalysisSummary(
        status=ANALYSIS_OK,
        assembly=assembly,
        chromosomes=tuple(summaries),
        unmapped_variant_count=unmapped,
        invalid_variant_count=invalid,
        warnings=tuple(warnings),
        provenance=_provenance(document, assembly),
    )
