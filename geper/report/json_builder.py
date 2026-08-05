"""
JSON output builder.

Assembles the per-variant results produced by every pipeline stage
(DNA model embeddings, RNA-FM, ESM-2, BLAST, ClinVar, dbSNP, and the
unified interpretation) into a single, stable JSON structure.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List

from utils.logger import get_logger
from report.clinical_report_builder import build_clinical_report
from pipeline.stage_schemas import build_raw_evidence_bundle, validate_interpretation_result_for_report

logger = get_logger(__name__)


class JSONResultBuilder:
    """Builds the unified JSON output document for a GEPER run."""

    def __init__(
        self,
        input_vcf_path: str,
        assembly: str = None,
        vcf_samples: List[str] = None,
        provenance_collector: Any = None,
        code_version: str = None,
        model_checkpoints: Dict[str, str] = None,
    ):
        self.input_vcf_path = input_vcf_path
        # Genome reference build resolved by the orchestrator's assembly
        # preflight (`validate_assembly`, from the VCF header and/or
        # --assembly) -- previously computed and used internally for
        # provider lookups but never actually surfaced in the JSON
        # output. Additive key: None here (same as every other
        # not-yet-known field in this codebase) renders as "not
        # specified" rather than a fabricated default downstream (see
        # `report/summary.py`), never guessed as "GRCh38".
        self.assembly = assembly
        # The VCF's own genotype sample column names (`VCFParser.samples`,
        # from its header line), if any -- also previously computed but
        # never surfaced. Used by `report/summary.py` to derive a real
        # Sample ID instead of a fabricated placeholder.
        self.vcf_samples = vcf_samples or []
        self.variant_results: List[Dict[str, Any]] = []
        # New, additive: data-source version pinning / run provenance
        # (pipeline/provenance.py) -- what makes a report reproducible
        # later. `provenance_collector` is the orchestrator's live
        # `RunProvenanceCollector` (a *reference*, not a snapshot taken
        # here): `.to_list()` is only called inside `build()`, so a
        # `write()` invoked mid-run (the periodic checkpoint write in
        # `pipeline/orchestrator.py::run()`) reflects whatever's been
        # captured so far, and the final `write()` reflects the
        # complete run. `None` (the default, for backward compatibility
        # with any caller that doesn't pass one) renders as an empty
        # list, not a fabricated one.
        self.provenance_collector = provenance_collector
        self.code_version = code_version
        self.model_checkpoints = model_checkpoints or {}

    def add_variant_result(self, variant_result: Dict[str, Any]) -> None:
        self.variant_results.append(variant_result)

    def build(self) -> Dict[str, Any]:
        return {
            "geper_version": "1.0.0",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "input_vcf": self.input_vcf_path,
            "assembly": self.assembly,
            "vcf_samples": self.vcf_samples,
            "variant_count": len(self.variant_results),
            "variants": self.variant_results,
            # Code version + AI model checkpoints (task point 5) --
            # reproducibility of the CODE, alongside the data-source
            # provenance list below (which covers reproducibility of
            # the DATA each variant's evidence came from).
            "code_version": self.code_version,
            "model_checkpoints": self.model_checkpoints,
            # Data-source provenance (task points 1-4, 6): one entry
            # per known external source, always present (never omitted)
            # -- a source this run never consulted still appears, with
            # `status: "not_consulted"`, structurally distinct from a
            # source that was consulted but yielded no version
            # (`status: "unknown"`). See pipeline/provenance.py.
            "provenance": self.provenance_collector.to_list() if self.provenance_collector is not None else [],
        }

    def write(self, output_path: str) -> str:
        document = self.build()
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(document, fh, indent=2, default=_json_default)
        logger.info(f"Wrote JSON output to '{output_path}'.")
        return output_path


def _json_default(obj: Any) -> Any:
    """Fallback serializer for numpy scalars / torch tensors that slip through."""
    if hasattr(obj, "item"):
        return obj.item()
    if hasattr(obj, "tolist"):
        return obj.tolist()
    return str(obj)


def build_variant_result(
    variant_dict: Dict[str, Any],
    sequence_context: Dict[str, Any],
    dna_model_results: Dict[str, Any],
    rna_result: Dict[str, Any],
    protein_result: Dict[str, Any],
    blast_result: Dict[str, Any],
    clinvar_result: Dict[str, Any],
    dbsnp_result: Dict[str, Any],
    interpretation: Dict[str, Any],
    errors: List[str],
    alphamissense_result: Dict[str, Any] = None,
    mmsplice_result: Dict[str, Any] = None,
    gnomad_result: Dict[str, Any] = None,
    indigenomes_result: Dict[str, Any] = None,
    conservation_result: Dict[str, Any] = None,
    clingen_result: Dict[str, Any] = None,
    uniprot_result: Dict[str, Any] = None,
    interpro_result: Dict[str, Any] = None,
    alphafold_result: Dict[str, Any] = None,
    transcript_result: Dict[str, Any] = None,
    clinvar_codon_result: Dict[str, Any] = None,
    hpo_result: Dict[str, Any] = None,
    orphanet_result: Dict[str, Any] = None,
    functional_evidence_result: Dict[str, Any] = None,
    normalization_result: Dict[str, Any] = None,
    spliceformer_result: Dict[str, Any] = None,
    splicebert_result: Dict[str, Any] = None,
    interpretation_result: Dict[str, Any] = None,
    ai_splicing_ensemble_result: Dict[str, Any] = None,
    ai_model_status: Dict[str, Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Assemble a single variant's full multi-stage result into one record."""
    resolved_interpretation_result = (
        interpretation_result
        if interpretation_result is not None
        else (interpretation or {}).get("interpretation_result")
    )
    # Built from the same raw provider dicts this function already
    # received (and is about to emit verbatim under the top-level
    # `uniprot`/`interpro`/`gnomad`/etc. keys below) so that
    # `build_clinical_report`'s evidence-dependent sections (protein/
    # structural/population/clinical knowledge, sequence context) can
    # never drift from what those top-level keys actually say --
    # passed explicitly rather than left to fall back to
    # `resolved_interpretation_result.get("raw_evidence")`, which is
    # always empty: `InterpretationResult.to_dict()`
    # (pipeline/interpretation_result.py) deliberately omits
    # `raw_evidence` from its serialized form. Before this fix, that
    # meant `build_clinical_report` always saw an empty `raw` dict, so
    # its protein/structural/population/clinical-evidence sections
    # unconditionally reported "not found" regardless of what the
    # corresponding stage actually returned -- see
    # `build_clinical_report`'s docstring for the real Colab report
    # this was caught from (a resolved UniProt entry contradicted two
    # sections later by "no entry resolved" here).
    raw_evidence_for_report = {
        "clinvar": clinvar_result,
        "dbsnp": dbsnp_result,
        "protein": protein_result,
        "blast": blast_result,
        "alphamissense": alphamissense_result,
        "mmsplice": mmsplice_result,
        "gnomad": gnomad_result,
        "clingen": clingen_result,
        "uniprot": uniprot_result,
        "interpro": interpro_result,
        "alphafold": alphafold_result,
    }
    # Schema validation at this exact boundary (pipeline/stage_schemas.py)
    # -- the site of the bug above. Deliberately does NOT replace
    # `raw_evidence_for_report`/`resolved_interpretation_result` below:
    # a validation failure is logged loudly (see
    # `build_raw_evidence_bundle`/`validate_interpretation_result_for_report`)
    # and this function still proceeds with the original, unvalidated
    # dicts -- falling back to a validated-but-blank result on failure
    # would silently reintroduce the exact "empty raw_evidence" bug
    # this schema exists to catch, just one layer further in.
    _variant_ref = (
        f"{variant_dict.get('chrom')}:{variant_dict.get('pos')}{variant_dict.get('ref')}>{variant_dict.get('alt')}"
    )
    build_raw_evidence_bundle(
        variant_ref=_variant_ref, **{f"{k}_result": v for k, v in raw_evidence_for_report.items()}
    )
    validate_interpretation_result_for_report(resolved_interpretation_result, variant_ref=_variant_ref)
    result = {
        "variant": variant_dict,
        "sequence_context": sequence_context,
        "dna_model_results": dna_model_results,
        "rna_analysis": rna_result,
        "protein_analysis": protein_result,
        "alphamissense": alphamissense_result if alphamissense_result is not None else {"skipped": True},
        # New, additive key -- both `alphamissense_result` and
        # `mmsplice_result` default to None, so any existing caller of
        # `build_variant_result` that doesn't pass this kwarg still
        # gets a fully backward-compatible result dict (requirement
        # #10: "Maintain backward compatibility").
        "mmsplice": mmsplice_result if mmsplice_result is not None else {"supported": False, "predicted": False},
        # New, additive key (Phase 2) -- defaults to None exactly like
        # `alphamissense_result`/`mmsplice_result` above, so any
        # existing caller of `build_variant_result` that doesn't pass
        # this kwarg still gets a fully backward-compatible result dict.
        "gnomad": gnomad_result if gnomad_result is not None else {"skipped": True, "found": False},
        # New, additive key (India-deployment feature: IndiGenomes
        # population-frequency evidence -- see annotation/indigenomes.py).
        # Defaults to None exactly like `gnomad_result` above, so any
        # existing caller of `build_variant_result` that doesn't pass
        # this kwarg still gets a fully backward-compatible result dict.
        # Deliberately NOT routed through `raw_evidence_for_report`/
        # `RawEvidenceBundle` below (that schema's 11 fields are fixed
        # and validated as a whole -- see pipeline/stage_schemas.py);
        # passed to `build_clinical_report` as its own explicit
        # parameter instead.
        "indigenomes": indigenomes_result if indigenomes_result is not None else {"skipped": True, "found": False},
        # New, additive key (evolutionary-conservation evidence:
        # PhyloP now, PhastCons/GERP++ to follow the same pattern --
        # see pipeline/conservation/) -- defaults to None exactly like
        # `gnomad_result` above, so any existing caller of
        # `build_variant_result` that doesn't pass this kwarg still
        # gets a fully backward-compatible result dict.
        "conservation": conservation_result if conservation_result is not None else {"skipped": True, "found": False},
        # New, additive key (ClinGen integration) -- defaults to None
        # exactly like `gnomad_result` above, so any existing caller
        # of `build_variant_result` that doesn't pass this kwarg still
        # gets a fully backward-compatible result dict (requirement
        # #13: "Do NOT modify or break ... Reports. API.").
        "clingen": clingen_result if clingen_result is not None else {"skipped": True, "found": False},
        # New, additive keys (biological evidence layer: UniProt ->
        # InterPro/Pfam -> AlphaFold DB) -- default to None exactly
        # like `gnomad_result`/`clingen_result` above, so any existing
        # caller of `build_variant_result` that doesn't pass these
        # kwargs still gets a fully backward-compatible result dict.
        "uniprot": uniprot_result if uniprot_result is not None else {"skipped": True, "found": False},
        "interpro": interpro_result if interpro_result is not None else {"skipped": True, "found": False},
        "alphafold": alphafold_result if alphafold_result is not None else {"skipped": True, "found": False},
        # New, additive key (transcript structure for the ACMG/AMP PVS1
        # rule -- see pipeline/pvs1/). Defaults to None exactly like
        # `clingen_result` above, so any existing caller that doesn't
        # pass this kwarg still gets a fully backward-compatible result
        # dict. Surfaced rather than kept internal because PVS1's
        # strength is only auditable against the exon structure it was
        # derived from: which transcript, how many coding exons, and
        # where the NMD boundary fell.
        "transcript": transcript_result if transcript_result is not None else {"skipped": True, "found": False},
        # New, additive key (PS1/PM5 shared evidence source -- see
        # pipeline/ps1_pm5/). Defaults to None exactly like
        # `transcript_result` above, so any existing caller that
        # doesn't pass this kwarg still gets a fully backward-
        # compatible result dict.
        "clinvar_codon_matches": clinvar_codon_result
        if clinvar_codon_result is not None
        else {"skipped": True, "found": False},
        # New, additive key (HPO gene-phenotype annotation -- see
        # pipeline/hpo/). Defaults to None exactly like
        # `clinvar_codon_result` above, so any existing caller that
        # doesn't pass this kwarg still gets a fully backward-
        # compatible result dict.
        "hpo": hpo_result if hpo_result is not None else {"skipped": True, "found": False},
        # New, additive key (Orphanet rare-disease gene-disorder
        # annotation -- see pipeline/orphanet/). Defaults to None
        # exactly like `hpo_result` above, so any existing caller that
        # doesn't pass this kwarg still gets a fully backward-
        # compatible result dict.
        "orphanet": orphanet_result if orphanet_result is not None else {"skipped": True, "found": False},
        # New, additive key (PS3/BS3 functional-evidence integration --
        # see pipeline/functional_evidence/). Defaults to None exactly
        # like `orphanet_result` above, so any existing caller that
        # doesn't pass this kwarg still gets a fully backward-
        # compatible result dict.
        "functional_evidence": functional_evidence_result
        if functional_evidence_result is not None
        else {"skipped": True, "found": False},
        # New, additive key (variant normalization + HGVS notation --
        # see pipeline/variant_normalization.py, pipeline/hgvs_utils.py).
        # Defaults to None exactly like `orphanet_result` above, so any
        # existing caller that doesn't pass this kwarg still gets a
        # fully backward-compatible result dict.
        "normalization": normalization_result if normalization_result is not None else {"skipped": True},
        # New, additive keys (standalone SpliceFormer/SpliceBERT
        # splice-prediction plugins for BP7 -- see pipeline/acmg_rules.py::
        # ACMGRuleEngine._bp7 and pipeline/orchestrator.py::
        # _run_standalone_splice_plugin_stage). Default to an explicit
        # "unavailable" shape rather than None so a report reader always
        # sees why, matching every other additive key's backward-
        # compatible default above.
        "spliceformer": spliceformer_result
        if spliceformer_result is not None
        else {"available": False, "classification": None},
        "splicebert": splicebert_result
        if splicebert_result is not None
        else {"available": False, "classification": None},
        "blast": blast_result,
        "clinvar": clinvar_result,
        "dbsnp": dbsnp_result,
        "interpretation": interpretation,
        # New, additive key (Phase 2: Evidence Aggregation Engine) --
        # defaults to None exactly like `gnomad_result`/`clingen_result`
        # above, so any existing caller of `build_variant_result` that
        # doesn't pass this kwarg still gets a fully backward-compatible
        # result dict. When `interpretation` already carries its own
        # `interpretation_result` key (the normal case, since
        # `InterpretationEngine.interpret()` now populates it), that is
        # used automatically if the caller didn't pass this kwarg
        # explicitly, so orchestrator call sites don't have to thread it
        # through twice.
        "interpretation_result": resolved_interpretation_result,
        # New, additive key (Phase 5: Clinical Report Upgrade). Built
        # entirely from `resolved_interpretation_result` above (the same
        # canonical InterpretationResult dict Phase 2-4 already
        # produced) -- reuses that logic rather than re-deriving
        # anything from the raw provider dicts a second time. `None`
        # when interpretation_result itself is missing/errored, never
        # fabricated.
        "clinical_report": build_clinical_report(
            resolved_interpretation_result,
            variant_dict,
            raw_evidence=raw_evidence_for_report,
            indigenomes_result=indigenomes_result,
        ),
        # New, additive key (Objective 6: AI model status reporting).
        # Unlike `ai_splicing_ensemble` below, this key is ALWAYS
        # present -- never conditionally omitted -- because the whole
        # point of this table is that no AI model may silently vanish
        # from a report. Defaults to `{}` only for legacy callers that
        # don't pass it at all; every current orchestrator call site
        # passes a complete dict covering every model in
        # `pipeline.models.status.DISPLAY_ORDER`.
        "ai_model_status": ai_model_status if ai_model_status is not None else {},
        "errors": errors,
    }

    # AI Splicing/Regulatory Ensemble (Enformer + Borzoi via
    # pipeline/models/ensemble.py::EnsembleManager) -- additive,
    # schema-preserving key. Only added to the result dict when real
    # ensemble evidence exists (i.e. `ai_splicing_ensemble_result` was
    # actually passed in AND at least one model produced a result).
    # This means:
    #   - Every existing/legacy caller of `build_variant_result` (which
    #     never passes this kwarg) gets a result dict with EXACTLY the
    #     same set of keys as before this change -- the key is not
    #     merely empty, it is entirely absent, which is the strongest
    #     form of schema backward compatibility.
    #   - A caller that wires the ensemble in but for a variant where
    #     both Enformer and Borzoi were unavailable also gets no key
    #     here (there is nothing to report), matching the Markdown
    #     renderer's "hide the section entirely" behavior for the same
    #     case -- see report/report_generator.py::_render_ai_splicing_ensemble.
    if ai_splicing_ensemble_result and ai_splicing_ensemble_result.get("models_used"):
        result["ai_splicing_ensemble"] = ai_splicing_ensemble_result

    return result
