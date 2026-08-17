"""
Custom exception hierarchy for GEPER.

Using specific exception types (instead of bare Exception) lets the
orchestrator apply graceful degradation policies per-failure-type
(e.g. an ExternalAPIError should not abort the whole pipeline, while a
VCFParsingError on the input file should).
"""


class GeperError(Exception):
    """Base class for all GEPER-specific exceptions."""


class VCFParsingError(GeperError):
    """Raised when the input VCF file is malformed or unreadable."""


class RawInputValidationError(GeperError):
    """
    Raised when a raw sequencing input file (FASTQ/BAM/CRAM) fails
    pre-flight structural validation (see pipeline/raw_input_validator.py).
    Fatal by design, same reasoning as VCFParsingError: a corrupt or
    wrong-format input file must stop processing before any expensive
    downstream step (alignment, variant calling) rather than degrade
    gracefully.
    """


class SequenceGenerationError(GeperError):
    """Raised when a DNA/RNA/protein sequence cannot be constructed."""


class ModelLoadError(GeperError):
    """Raised when a pretrained model fails to load into memory."""


class ModelInferenceError(GeperError):
    """Raised when a forward pass / inference call fails."""


class ExternalAPIError(GeperError):
    """Raised when a call to BLAST, ClinVar, or dbSNP fails."""


class RoutingError(GeperError):
    """Raised when the sequence router cannot determine a valid model path."""


class AssemblyMismatchError(GeperError):
    """
    Raised when the input VCF's declared genome assembly/build (parsed
    from its header) definitely conflicts with the assembly requested
    via --assembly. Fatal by design: reference-sequence lookups made
    against the wrong build silently corrupt every downstream DNA/RNA/
    protein model input, so this must stop the run rather than degrade
    gracefully like the other per-stage failures.
    """


class PipelineError(GeperError):
    """Raised for orchestration-level failures that halt processing."""


class SignoffError(GeperError):
    """
    Raised by `geper/review/signoff.py` for a clinician review-workflow
    failure that must stop the command (e.g. no `geper_results.json` in
    the given `--output-dir`, or `override`'s `--variant` not matching
    any variant in that run) -- fatal by design, same reasoning as
    `VCFParsingError`: this always means the command has nothing valid
    to act on, so it must not proceed and silently do something
    partial to a clinical record.
    """


class LIMSExportBlockedError(GeperError):
    """
    Raised by `report/export_lims.py` when a caller attempts to export
    a run whose `review_status` (see `report/json_builder.py`) is not
    `"reviewed"` -- governance control, round 30 part 2: a LIMS is an
    automated downstream consumer that never opens the PDF a human
    clinician would see the DRAFT/OVERRIDDEN status on, so this must be
    a hard, loud failure (never a silent empty/partial export) whenever
    a run has not been through `review/signoff.py::approve()`.
    """
