"""
GEPER pipeline orchestrator.

Wires every stage together in the order specified by the project spec:

    Parse VCF
      -> Extract chrom/pos/REF/ALT
      -> Generate DNA sequence context
      -> Route intelligently (HyenaDNA / Evo 2)
      -> Generate RNA sequence when required -> RNA-FM
      -> Generate protein sequence when required -> ESM-2
      -> BLAST
      -> ClinVar
      -> dbSNP
      -> Merge all outputs -> unified interpretation
      -> JSON output
      -> Human-readable report

Graceful degradation: a failure in any *external* stage (BLAST,
ClinVar, dbSNP, or an individual model) is caught, logged, and
recorded in the variant's `errors` list rather than aborting the
entire run -- one bad network call should not lose results for every
other variant. A failure in VCF parsing itself is fatal, since there
is nothing to process without it.
"""

import itertools
import dataclasses
import json
import os
from contextlib import nullcontext
from typing import Any, Dict, List, Optional, Tuple

from utils.profiling import StageProfiler

from config import CONFIG
from database.blast_client import BLASTClient
from database.clinvar_client import ClinVarClient
from database.dbsnp_client import DbSNPClient
from models import MODEL_REGISTRY as _BASE_MODEL_REGISTRY
from models.alphamissense import catalogue_cache_path as alphamissense_catalogue_cache_path
from pipeline.assembly_validator import validate_assembly
from pipeline.alphafold.lookup import AlphaFoldLookup
import pipeline.clingen.bootstrap as clingen_bootstrap
from pipeline.clingen.lookup import ClinGenLookup
import pipeline.hpo.bootstrap as hpo_bootstrap
import pipeline.mane.bootstrap as mane_bootstrap
import pipeline.orphanet.bootstrap as orphanet_bootstrap
from pipeline.functional_evidence.lookup import FunctionalEvidenceLookup
from pipeline.hpo.lookup import HPOLookup
from pipeline.orphanet.lookup import OrphanetLookup
from pipeline.conservation.lookup import ConservationLookup
from annotation.indigenomes import IndiGenomesLookup
from pipeline.gnomad.lookup import GnomadLookup
from pipeline.gnomad.provider import dataset_id_for_build as gnomad_dataset_id_for_build
from pipeline.interpretation import InterpretationEngine
from pipeline.case_prioritization import rank_case
from pipeline.hpo.ontology import get_shared_ontology
from pipeline.interpro.lookup import InterProLookup
from pipeline.models.ensemble import EnsembleManager
from pipeline.models.manager import ModelManager
from pipeline.models.mmsplice.loader import MMSpliceModel
from pipeline.models.mmsplice.service import MMSpliceService
from pipeline.models.pending_plugins import build_default_registry
from pipeline.models.status import build_ai_model_status
from pipeline.prioritization_engine import rank_batch
from pipeline.protein_translator import ProteinTranslator
from pipeline.provenance import (
    RunProvenanceCollector,
    VersionStatus,
    capture_blast_local_tool_versions,
    capture_ensembl_release,
    get_geper_code_version,
    get_model_checkpoint_identifiers,
    local_file_provenance,
    read_dataset_provenance_sidecar,
)
from pipeline.ps1_pm5.lookup import ClinVarCodonLookup
from pipeline.pvs1.lookup import TranscriptLookup
from pipeline.pvs1.utils import canonical_protein_position, transcript_from_result
from pipeline.hgvs_utils import to_hgvs_c, to_hgvs_g
from pipeline.variant_normalization import normalize_variant
from pipeline.rna_generator import RNAGenerator
from pipeline.router import HYENADNA, SequenceRouter
from pipeline.sequence_context import SequenceContextGenerator
from pipeline.uniprot.lookup import UniProtLookup
from pipeline.vcf_parser import Variant, VCFParser
from report.json_builder import JSONResultBuilder, build_variant_result
from report.summary import _parse_patient_meta, generate_pdf
from report.summary_short import generate_short_pdf
from report.report_generator import ReportGenerator
from utils.exceptions import (
    AssemblyMismatchError,
    ExternalAPIError,
    ModelInferenceError,
    ModelLoadError,
    PipelineError,
    SequenceGenerationError,
    VCFParsingError,
)
from utils.device_utils import log_environment_versions
from utils.logger import get_logger
from utils.model_cache import ModelCache

logger = get_logger(__name__)

# Extended model registry: every model in `models.MODEL_REGISTRY` plus
# MMSplice. Built here (not inside `models/__init__.py`) specifically
# to avoid a circular import between the `models` package and
# `pipeline.models.mmsplice` -- see the long comment in
# `models/__init__.py` for the exact cycle this sidesteps. Every other
# reference to `MODEL_REGISTRY` in this file (startup validation,
# `_model_availability`, `_filter_available_models`, etc.) uses *this*
# extended dict, so MMSplice participates in exactly the same
# lifecycle (availability probing, startup smoke-test, run-summary
# reporting) as every pre-existing model, with zero changes to any of
# that shared logic.
MODEL_REGISTRY: Dict[str, Any] = {**_BASE_MODEL_REGISTRY, "mmsplice": MMSpliceModel}

# Short, cheap-to-embed dummy sequences used for startup validation
# (issue: "verify every loaded model using a short dummy sequence
# before processing any variants"). DNA models get a DNA sequence,
# RNA-FM gets an RNA sequence, ESM-2 gets a tiny protein sequence --
# each is real, valid input for its model's alphabet, not a
# placeholder that would itself fail tokenization.
_DUMMY_DNA_SEQUENCE = "ACGTACGTACGTACGTACGTACGTACGTACGT"
_DUMMY_RNA_SEQUENCE = "ACGUACGUACGUACGUACGUACGUACGUACGU"
_DUMMY_PROTEIN_SEQUENCE = "MAKLVQST"
# AlphaMissense isn't a sequence model (see models/alphamissense.py) --
# its "dummy input" is a syntactically well-formed lookup key. Startup
# validation only needs the round-trip (tabix binary + catalogue
# reachable) to succeed; a "not found" result for this placeholder
# coordinate is a perfectly valid PASS, same as a real variant with no
# catalogue hit.
_DUMMY_ALPHAMISSENSE_KEY = "1:100000:A:T"

# MMSplice's startup smoke-test doesn't need a real exon -- it's purely
# proving all 5 Keras submodels loaded and can run end to end (see
# MMSpliceModel._infer_impl). Long enough (300bp) that the default
# (100, 100) overhang used when no `overhang=` kwarg is supplied (the
# generic startup path below calls `instance.predict(dummy_sequence)`
# with no kwargs) leaves a non-empty "exon" in the middle.
_DUMMY_MMSPLICE_SEQUENCE = "ACGT" * 75

_DUMMY_SEQUENCE_BY_MODEL = {
    "hyenadna": _DUMMY_DNA_SEQUENCE,
    "evo2": _DUMMY_DNA_SEQUENCE,
    "rna_fm": _DUMMY_RNA_SEQUENCE,
    "esm2": _DUMMY_PROTEIN_SEQUENCE,
    "alphamissense": _DUMMY_ALPHAMISSENSE_KEY,
    "mmsplice": _DUMMY_MMSPLICE_SEQUENCE,
}

# Friendly display names for the run summary (issue #7).
_MODEL_DISPLAY_NAMES = {
    "hyenadna": "HyenaDNA",
    "evo2": "Evo2",
    "rna_fm": "RNA-FM",
    "esm2": "ESM2",
    "alphamissense": "AlphaMissense",
    "mmsplice": "MMSplice",
    "enformer": "Enformer",
    "borzoi": "Borzoi",
    "spliceformer": "SpliceFormer",
    "splicebert": "SpliceBERT",
}


class GeperPipeline:
    """End-to-end orchestrator for the GEPER variant analysis pipeline."""

    def __init__(
        self,
        blast_mode: Optional[str] = None,
        blast_local_db: Optional[str] = None,
        blast_reference_fasta: Optional[str] = None,
        species: str = "human",
        assembly: Optional[str] = None,
        output_dir: Optional[str] = None,
        ai_only: Optional[bool] = None,
        enable_profiling: Optional[bool] = None,
        blast_disk_cache: Optional[bool] = None,
        blast_enable_prefetch: Optional[bool] = None,
        mmsplice_enable_prefetch: Optional[bool] = None,
        patient_meta_path: Optional[str] = None,
        phenotype_result: Optional[Dict[str, Any]] = None,
    ):
        log_environment_versions()

        # AI-only mode disables BLAST entirely -- no NCBI submission,
        # no local blastn subprocess, no disk-cache I/O -- and relies
        # on the DNA/RNA/protein models + ClinVar/dbSNP/AlphaMissense
        # alone. Defaults to CONFIG.AI_ONLY_MODE (env-configurable) so
        # both the CLI and programmatic callers share one source of
        # truth unless a caller explicitly overrides it.
        self.ai_only = CONFIG.AI_ONLY_MODE if ai_only is None else ai_only
        self.blast_enable_prefetch = (
            CONFIG.api.BLAST_ENABLE_PREFETCH if blast_enable_prefetch is None else blast_enable_prefetch
        )
        self.mmsplice_enable_prefetch = (
            CONFIG.mmsplice.ENABLE_PREFETCH if mmsplice_enable_prefetch is None else mmsplice_enable_prefetch
        )

        self.sequence_context_gen = SequenceContextGenerator(species=species, assembly=assembly)
        self.router = SequenceRouter()
        self.rna_generator = RNAGenerator()
        self.protein_translator = ProteinTranslator()
        self.blast_client = BLASTClient(
            mode=blast_mode,
            local_db_path=blast_local_db,
            reference_fasta=blast_reference_fasta,
            disabled=self.ai_only,
            enable_disk_cache=blast_disk_cache,
        )
        self.clinvar_client = ClinVarClient()
        self.dbsnp_client = DbSNPClient()
        self.gnomad_client = GnomadLookup()
        # India-deployment feature: IndiGenomes (~1000+ Indian genomes,
        # CSIR-IGIB) population-frequency evidence -- see
        # annotation/indigenomes.py's module docstring for why this is
        # a live-query-only integration (no local-index counterpart).
        self.indigenomes_client = IndiGenomesLookup()
        self.conservation_client = ConservationLookup()
        self.clingen_client = ClinGenLookup()
        # HPO gene-phenotype annotation (see pipeline/hpo/). Gene-level,
        # like ClinGen -- reuses the gene symbol that stage already
        # resolved (see `_run_hpo_stage`) rather than issuing a second
        # Ensembl gene lookup.
        self.hpo_client = HPOLookup()
        # Orphanet rare-disease gene-disorder annotation (see
        # pipeline/orphanet/). Gene-level, same reuse as HPO above --
        # runs after ClinGen for the same reason.
        self.orphanet_client = OrphanetLookup()
        # Transcript structure for the ACMG/AMP PVS1 rule. Runs after
        # ClinGen because it reuses the gene symbol that stage already
        # resolved, rather than issuing a second Ensembl gene lookup.
        self.transcript_client = TranscriptLookup()
        # PS1/PM5's shared evidence source: every ClinVar record with a
        # missense protein change at a variant's own codon. Runs after
        # the transcript stage since it needs that stage's structure to
        # compute the codon's genomic span.
        self.clinvar_codon_client = ClinVarCodonLookup()
        # PS3/BS3's evidence source (see pipeline/functional_evidence/):
        # ClinGen Evidence Repository (primary) + MaveDB (secondary)
        # functional-assay evidence for this exact variant. Runs after
        # the transcript stage since MaveDB matching needs the coding
        # HGVS notation that stage's structure makes possible.
        self.functional_evidence_client = FunctionalEvidenceLookup()
        # Biological evidence layer (UniProt -> InterPro/Pfam -> AlphaFold
        # DB), run in that order since InterPro/AlphaFold are both keyed
        # by the UniProt accession the UniProt stage resolves.
        self.uniprot_client = UniProtLookup()
        self.interpro_client = InterProLookup()
        self.alphafold_client = AlphaFoldLookup()

        # MMSplice's variant-level service (exon lookup, eligibility,
        # window construction, caching, interpretation) is constructed
        # lazily in `_run_mmsplice_stage`, wrapped around the *same*
        # `MMSpliceModel` instance startup validation / `_model_instances`
        # uses -- never a second, independently-loaded copy of the 5
        # Keras submodels. See `_run_mmsplice_stage`.
        self._mmsplice_service: Optional[MMSpliceService] = None

        self.interpretation_engine = InterpretationEngine()
        self.report_generator = ReportGenerator()

        # Enformer + Borzoi splicing/regulatory AI ensemble (Objectives
        # 3/4/7): one process-wide ModelManager over the "new models"
        # plugin registry (see pipeline/models/pending_plugins.py),
        # wrapped in an EnsembleManager that combines whichever of
        # Enformer/Borzoi are actually available. Both plugins are
        # config-gated off by default (CONFIG.splicing.ENABLE_ENFORMER /
        # ENABLE_BORZOI) -- when off (or their optional pip packages
        # aren't installed), `_run_ensemble_stage` below degrades to "no
        # ensemble evidence" for every variant, exactly like every other
        # optional model in this pipeline; it never raises.
        self.model_registry = build_default_registry()
        self.model_manager = ModelManager(registry=self.model_registry)
        self.ensemble_manager = EnsembleManager(manager=self.model_manager)

        # Per-stage wall-clock profiler (see utils/profiling.py). Wraps
        # every external-call / model-inference stage; never reads or
        # alters what the wrapped stage returns.
        self.enable_profiling = CONFIG.ENABLE_PROFILING if enable_profiling is None else enable_profiling
        self.profiler = StageProfiler()

        self.output_dir = output_dir or CONFIG.OUTPUT_DIR
        os.makedirs(self.output_dir, exist_ok=True)
        # Optional path to a patient-metadata JSON file for the clinical
        # PDF report's header (report/summary.py::generate_pdf). None by
        # default -- the report then renders the safe "De-identified /
        # Research Sample" fallback. A missing/corrupt file at this path
        # never crashes the pipeline (see generate_pdf's docstring); the
        # PDF stage itself is also wrapped in try/except in run() below.
        self.patient_meta_path = patient_meta_path

        # Patient-observed HPO phenotype terms (see pipeline/hpo/utils.py::
        # build_phenotype_result), the input ACMG's PP4 rule
        # (acmg_rules.py::ACMGRuleEngine._pp4) compares against each
        # variant's gene-level HPO evidence. None by default -- the same
        # "no input supplied" state every prior run was already in, so
        # PP4 continues to report "not_evaluated" exactly as before
        # unless a caller explicitly supplies phenotype terms (CLI:
        # --hpo-terms / --phenotype-file).
        self.phenotype_result = phenotype_result

        if self.ai_only:
            logger.info(
                "AI-only mode is ON: BLAST is disabled for this pipeline "
                "instance (no NCBI submission, no local blastn call, no "
                "BLAST disk-cache activity). Interpretation relies on the "
                "DNA/RNA/protein foundation models plus ClinVar/dbSNP/"
                "AlphaMissense only."
            )

        # Model instances are constructed lazily (on first routing hit)
        # and cached process-wide via ModelCache inside BaseGenomicModel.
        self._model_instances: Dict[str, Any] = {}

        # The assembly the caller explicitly asked for (may be None).
        # Preserved separately from `self.sequence_context_gen.assembly`
        # so `run()` can tell the difference between "user requested
        # this build" and "we auto-detected it from the VCF header" when
        # doing the preflight assembly validation (issue #5).
        self._cli_assembly = assembly

        # Probed once per process: which models can even be attempted
        # in this environment (e.g. is HyenaDNA's standalone package
        # installed?). Lets the router's selections be filtered
        # up-front instead of re-attempting and re-failing a known-
        # missing model on every single variant (issue #2 / #9).
        self._model_availability: Dict[str, bool] = {
            key: model_cls.is_available() for key, model_cls in MODEL_REGISTRY.items()
        }
        unavailable = [k for k, ok in self._model_availability.items() if not ok]
        if unavailable:
            details = ", ".join(
                f"{_MODEL_DISPLAY_NAMES.get(k, k)} ({MODEL_REGISTRY[k].unavailability_reason()})" for k in unavailable
            )
            logger.warning(
                f"The following model(s) are unavailable in this environment "
                f"and will be skipped wherever routed: {details}."
            )

        # Same probe as above, but for the separate plugin-model family
        # (Enformer/Borzoi/SpliceFormer/SpliceBERT/SPiP -- pipeline/models/*,
        # managed through self.model_manager rather than MODEL_REGISTRY/
        # ModelCache). Kept in its own dict rather than merged into
        # `_model_availability` above: that dict's every "unavailable"
        # entry is looked up directly in `MODEL_REGISTRY` (line above),
        # which the plugin family is deliberately not part of (see
        # `MODEL_REGISTRY`'s own module docstring on why MMSplice needed
        # a separate extended dict for exactly this kind of collision).
        self._plugin_availability: Dict[str, bool] = {
            key: self.model_registry.get(key).is_available() for key in self.model_registry.keys()
        }
        unavailable_plugins = [k for k, ok in self._plugin_availability.items() if not ok]
        if unavailable_plugins:
            details = ", ".join(
                f"{_MODEL_DISPLAY_NAMES.get(k, k)} ({self.model_registry.get(k).unavailability_reason()})"
                for k in unavailable_plugins
            )
            logger.info(
                f"The following optional AI plugin(s) are unavailable/disabled "
                f"in this environment and will be skipped wherever used: {details}."
            )

        # First load-failure message per model key, kept for the final
        # run summary (issue #7); not every failure mode is caught by
        # `_model_availability` (e.g. a network failure downloading
        # HF weights), so this is populated lazily as failures occur.
        self._model_stage_errors: Dict[str, str] = {}

        # Startup dummy-sequence validation (see _run_startup_validation)
        # runs exactly once per pipeline instance, on the first call to
        # run(). Also tracks which "unavailable" models we've already
        # logged about at the per-variant routing stage, so a model
        # missing from the environment is reported once -- not once per
        # variant that happens to route to it.
        self._startup_validated = False
        self._logged_missing_at_routing: set = set()

        # Data-source version pinning / run provenance (pipeline/provenance.py)
        # -- one collector per pipeline instance (i.e. per run), pre-seeded
        # with every known source as NOT_CONSULTED. Run-level captures
        # (code version, model checkpoints, Ensembl's release, local BLAST+
        # tool versions, bootstrapped-dataset sidecars) happen once, here,
        # rather than per-variant; per-source captures that depend on an
        # actual query response (ClinVar, dbSNP, gnomAD, UniProt, InterPro,
        # AlphaFold, functional evidence) happen in `_process_variant` the
        # first time each stage returns real data -- see
        # `_capture_stage_provenance`.
        self.provenance = RunProvenanceCollector()
        self._capture_startup_provenance()

    def _capture_startup_provenance(self) -> None:
        """
        Run-level provenance captures that don't depend on any specific
        variant: GEPER's own code version, the AI model checkpoint
        identifiers already known to config, Ensembl's current release
        (one lightweight `/info/data` call), local BLAST+ tool versions,
        and whatever's already on disk for the bootstrapped/cached
        datasets (ClinGen gene-validity/dosage, HPO, Orphanet, MANE
        Select, AlphaMissense) -- reading their provenance sidecars, never
        triggering a fresh download here. Never raises: every capture is
        independently wrapped so one failing source can't prevent the
        rest (or pipeline startup itself) from proceeding.
        """
        self.geper_code_version = get_geper_code_version()
        self.model_checkpoints = get_model_checkpoint_identifiers()

        try:
            ensembl = capture_ensembl_release()
            if ensembl.get("version"):
                self.provenance.record(
                    "Ensembl", VersionStatus.VERSION_KNOWN, version=ensembl["version"], endpoint=ensembl["endpoint"]
                )
            else:
                self.provenance.record(
                    "Ensembl",
                    VersionStatus.UNKNOWN,
                    endpoint=ensembl.get("endpoint"),
                    notes=f"Could not reach Ensembl's /info/data endpoint: {ensembl.get('error')}",
                )
        except Exception as exc:  # noqa: BLE001 -- provenance capture must never break pipeline startup
            logger.warning(f"Ensembl release provenance capture failed: {exc}")

        try:
            blast = capture_blast_local_tool_versions()
            if blast.get("version"):
                self.provenance.record("BLAST", VersionStatus.VERSION_KNOWN, version=blast["version"])
            elif not self.ai_only:
                self.provenance.record(
                    "BLAST",
                    VersionStatus.UNKNOWN,
                    notes="No local BLAST+ tools found on PATH; remote NCBI BLAST exposes no queryable database version.",
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"BLAST tool-version provenance capture failed: {exc}")

        self._capture_bootstrapped_dataset_provenance(
            "ClinGen (gene validity)",
            CONFIG.clingen.GENE_VALIDITY_LOCAL_FILE,
            clingen_bootstrap.gene_validity_cache_path(),
        )
        self._capture_bootstrapped_dataset_provenance(
            "ClinGen (dosage sensitivity)",
            CONFIG.clingen.DOSAGE_SENSITIVITY_LOCAL_FILE,
            clingen_bootstrap.dosage_sensitivity_cache_path(),
        )
        self._capture_bootstrapped_dataset_provenance(
            "HPO", CONFIG.hpo.LOCAL_FILE, hpo_bootstrap.genes_to_phenotype_cache_path()
        )
        self._capture_bootstrapped_dataset_provenance("Orphanet", "", orphanet_bootstrap.gene_disorder_cache_path())
        self._capture_bootstrapped_dataset_provenance(
            "MANE Select (NCBI)", CONFIG.mane.LOCAL_FILE, mane_bootstrap.summary_cache_path()
        )

        # AlphaMissense: keyed by build ("hg38"/"hg19", not "GRCh38"/
        # "GRCh37" -- see `models/alphamissense.py::catalogue_cache_path`),
        # and only meaningful if AlphaMissense is actually enabled --
        # otherwise this stays NOT_CONSULTED, correctly, rather than
        # reporting on a catalogue this run will never touch.
        if CONFIG.alphamissense.ENABLED:
            genome_label = "hg38" if (self.sequence_context_gen.assembly or "GRCh38") != "GRCh37" else "hg19"
            configured_local = (
                CONFIG.alphamissense.LOCAL_HG38_PATH if genome_label == "hg38" else CONFIG.alphamissense.LOCAL_HG19_PATH
            )
            self._capture_bootstrapped_dataset_provenance(
                "AlphaMissense catalogue", configured_local, alphamissense_catalogue_cache_path(genome_label)
            )

    def _capture_bootstrapped_dataset_provenance(
        self, source: str, configured_local_file: str, auto_fetch_path: str
    ) -> None:
        """
        Records whichever file this source will actually query from for
        this run: an explicitly deployer-configured local file (hashed
        directly -- no GEPER-tracked download to read a sidecar for), or
        the auto-fetch cache path's own provenance sidecar (written by
        the corresponding `pipeline/*/bootstrap.py` at download time,
        possibly in a previous run -- reproducibility needs "what's
        actually being used now", not "what did we just download").
        Records NOT_CONSULTED-preserving UNKNOWN (never raises, never
        silently skips this source) when neither exists yet -- e.g. the
        auto-fetch hasn't happened on first use yet.
        """
        if configured_local_file:
            self.provenance.record(
                **self._provenance_kwargs_from_dataclass(local_file_provenance(source, configured_local_file))
            )
            return
        sidecar = read_dataset_provenance_sidecar(auto_fetch_path)
        if sidecar is None:
            return  # stays NOT_CONSULTED -- no local file configured and nothing fetched yet
        if sidecar.get("release_date") or sidecar.get("version"):
            status = VersionStatus.VERSION_KNOWN
        elif sidecar.get("content_hash"):
            status = VersionStatus.HASH_ONLY
        else:
            status = VersionStatus.TIMESTAMP_ONLY
        self.provenance.record(
            source,
            status,
            version=sidecar.get("version"),
            release_date=sidecar.get("release_date"),
            content_hash=sidecar.get("content_hash"),
            hash_algorithm=sidecar.get("hash_algorithm"),
            query_timestamp=sidecar.get("downloaded_at"),
            endpoint=sidecar.get("url"),
            notes=None
            if status == VersionStatus.VERSION_KNOWN
            else "No release version published/parseable for this download; content hash and download timestamp recorded instead.",
        )

    @staticmethod
    def _provenance_kwargs_from_dataclass(record) -> Dict[str, Any]:
        return {
            "source": record.source,
            "status": record.status,
            "version": record.version,
            "release_date": record.release_date,
            "content_hash": record.content_hash,
            "hash_algorithm": record.hash_algorithm,
            "query_timestamp": record.query_timestamp,
            "endpoint": record.endpoint,
            "notes": record.notes,
        }

    def _timer(self, stage: str):
        """
        Returns the profiler's timing context manager for `stage`, or a
        no-op context manager when profiling is disabled -- callers
        never need an `if self.enable_profiling:` branch of their own.
        """
        if self.enable_profiling:
            return self.profiler.timer(stage)
        return nullcontext()

    def run(
        self,
        vcf_path: str,
        max_variants: Optional[int] = None,
        resume: bool = True,
    ) -> Dict[str, Any]:
        """
        Run the full pipeline on a VCF file and return the JSON document.

        Args:
            vcf_path: Path to the input VCF/VCF.gz.
            max_variants: If set, stop after this many variant records
                (issue #6 -- test-mode debugging against huge VCFs).
                Uses the streaming parser so the file isn't even read
                past that point.
            resume: If True (default) and a previous run already wrote
                geper_results.json to `output_dir`, variants already
                present there are skipped and the run continues from
                where it left off (issue #8 -- Colab-disconnect resume).
        """
        logger.info(f"Starting GEPER pipeline run for '{vcf_path}'.")
        if self.enable_profiling:
            self.profiler.mark_run_start()
        if max_variants is not None and max_variants <= 0:
            raise PipelineError("--max-variants must be a positive integer.")

        json_path = os.path.join(self.output_dir, "geper_results.json")
        report_path = os.path.join(self.output_dir, "geper_report.md")

        parser = VCFParser(vcf_path)
        try:
            variant_iter = parser.iter_variants()
            if max_variants is not None:
                variant_iter = itertools.islice(variant_iter, max_variants)
                logger.info(f"Test mode: will stop after {max_variants} variant(s).")
            variants: List[Variant] = list(variant_iter)
        except VCFParsingError as exc:
            raise PipelineError(f"Fatal error parsing input VCF: {exc}") from exc

        if not variants:
            raise PipelineError(f"'{vcf_path}' contained no usable variant records.")

        # --- Assembly preflight (issue #5) ---------------------------------
        # Runs before any variant is processed / any reference sequence
        # is fetched, using the header lines the parser already
        # collected while reading up to `variants`.
        try:
            resolved_assembly = validate_assembly(parser.header_lines, self._cli_assembly)
        except AssemblyMismatchError as exc:
            raise PipelineError(str(exc)) from exc
        if resolved_assembly and not self._cli_assembly:
            self.sequence_context_gen.assembly = resolved_assembly

        # --- Ensembl batch prefetch (Phase 2 performance pass) -------------
        # Best-effort: warms the sequence-context generator's region
        # cache with one batched POST per ~50 variants instead of one
        # GET per variant. Never fatal -- any variant this doesn't
        # manage to prefetch is fetched individually exactly as before
        # when _process_variant reaches it (see
        # SequenceContextGenerator.prefetch_regions docstring).
        try:
            with self._timer("ensembl_prefetch"):
                self.sequence_context_gen.prefetch_regions(variants, self.router.recommended_flank_size)
        except Exception as exc:  # noqa: BLE001 - optimization only, must never block a run
            logger.warning(
                f"Ensembl batch prefetch failed unexpectedly ({exc}); continuing with normal per-variant fetching."
            )

        # --- BLAST batch prefetch (Phase 3 performance pass) ---------------
        # Mirrors the Ensembl prefetch immediately above: best-effort,
        # never fatal, and strictly a cache-warming step. Builds every
        # variant's sequence context up front (cheap now that Ensembl
        # regions are already cached -- no extra network calls beyond
        # what the per-variant path would do anyway) purely to collect
        # the *distinct* alt_sequence values that will need BLASTing,
        # then submits them concurrently via BLASTClient.search_many()
        # instead of one at a time, later, serially, inside the main
        # per-variant loop. Every value it populates lands in the exact
        # same BLASTClient cache (in-memory + disk) that
        # `_run_blast_stage` already checks, so a prefetched variant's
        # BLAST stage is a pure cache hit and produces an identical
        # result to the non-prefetched per-variant path. Skipped
        # entirely in AI-only mode, where BLAST never runs at all.
        if not self.ai_only and self.blast_enable_prefetch:
            try:
                with self._timer("blast_prefetch"):
                    self._prefetch_blast_results(variants)
            except Exception as exc:  # noqa: BLE001 - optimization only, must never block a run
                logger.warning(
                    f"BLAST batch prefetch failed unexpectedly ({exc}); continuing with normal per-variant BLAST calls."
                )

        # --- MMSplice batch prefetch (execution-speed pass) -----------------
        # Mirrors the BLAST prefetch immediately above: best-effort,
        # never fatal, strictly a cache-warming step that reuses
        # `MMSpliceService.predict_batch()` -- infrastructure that
        # already existed (see loader.py/predictor.py/service.py) but
        # was never called from here before. Skipped when MMSplice
        # itself is unavailable in this environment, exactly like every
        # other MMSplice short-circuit in this file.
        if self.mmsplice_enable_prefetch and self._model_availability.get("mmsplice", True):
            try:
                with self._timer("mmsplice_prefetch"):
                    self._prefetch_mmsplice_results(variants)
            except Exception as exc:  # noqa: BLE001 - optimization only, must never block a run
                logger.warning(
                    f"MMSplice batch prefetch failed unexpectedly ({exc}); "
                    "continuing with normal per-variant MMSplice calls."
                )

        # --- Startup model validation ---------------------------------------
        # Runs once per pipeline instance, before any variant is processed.
        if not self._startup_validated:
            self._run_startup_validation()
            self._startup_validated = True

        # DPDP Act 2023 consent metadata (minimal, capture-only -- see
        # report/summary.py::_parse_consent's docstring). Parsed from
        # the same --patient-meta file/dict the PDF renderers already
        # use (`self.patient_meta_path`) -- no new input path. `None`
        # when no --patient-meta was given, or it carried no usable
        # "consent" object; `JSONResultBuilder` renders that as an
        # explicit `null` in geper_results.json, never a fabricated
        # True/False.
        patient_consent = _parse_patient_meta(self.patient_meta_path).get("consent")

        # --- Resume-from-checkpoint (issue #8) ------------------------------
        result_builder = JSONResultBuilder(
            input_vcf_path=vcf_path,
            assembly=self.sequence_context_gen.assembly,
            vcf_samples=parser.samples,
            provenance_collector=self.provenance,
            code_version=self.geper_code_version,
            model_checkpoints=self.model_checkpoints,
            patient_consent=patient_consent,
        )
        completed_keys = set()
        stats = {"processed": 0, "success": 0, "skipped": 0, "failed": 0}

        if resume and os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as fh:
                    prior_document = json.load(fh)
                for prior_result in prior_document.get("variants", []):
                    result_builder.add_variant_result(prior_result)
                    prior_variant = prior_result.get("variant", {})
                    completed_keys.add(self._variant_key_from_dict(prior_variant))
                    stats["processed"] += 1
                    stats[self._classify_variant_result(prior_result)] += 1
                if completed_keys:
                    logger.info(
                        f"Resuming previous run: {len(completed_keys)} variant(s) already completed in '{json_path}'."
                    )
            except (OSError, ValueError) as exc:
                logger.warning(
                    f"Could not read existing checkpoint '{json_path}' for "
                    f"resume ({exc}); starting this run fresh instead."
                )
                result_builder = JSONResultBuilder(
                    input_vcf_path=vcf_path,
                    assembly=self.sequence_context_gen.assembly,
                    vcf_samples=parser.samples,
                    provenance_collector=self.provenance,
                    code_version=self.geper_code_version,
                    model_checkpoints=self.model_checkpoints,
                    patient_consent=patient_consent,
                )
                completed_keys = set()
                stats = {"processed": 0, "success": 0, "skipped": 0, "failed": 0}

        total = len(variants)
        for i, variant in enumerate(variants, start=1):
            if self._variant_key(variant) in completed_keys:
                continue

            logger.info(f"Processing variant {i}/{total}: {variant.chrom}:{variant.pos}")
            variant_result = self._process_variant(variant)
            result_builder.add_variant_result(variant_result)

            stats["processed"] += 1
            stats[self._classify_variant_result(variant_result)] += 1

            if i % CONFIG.CHECKPOINT_INTERVAL == 0:
                result_builder.write(json_path)

        json_document = result_builder.build()

        # Phase 4: batch-relative priority ranking. `priority_score` is
        # per-variant (computed in `_process_variant` via
        # `InterpretationEngine.interpret`), but `priority_rank` is only
        # meaningful relative to the other variants in this run, so it
        # is assigned once here rather than per-variant. Purely additive
        # -- mutates only the `priority_rank` key already reserved for
        # this on each variant's `interpretation.interpretation_result`;
        # every other key is untouched, and a variant whose prioritization
        # engine failed (priority_score is None) simply gets rank=None
        # rather than a fabricated position.
        try:
            self._apply_priority_ranks(json_document.get("variants", []))
        except Exception:
            logger.exception("Batch priority ranking failed; priority_rank left unset for this run.")

        # Case-level, HPO-phenotype-driven ranking (pipeline/case_prioritization.py)
        # -- a SEPARATE, additive signal from Phase 4's priority_rank
        # above; never touches ACMG classification, PP4, confidence,
        # or priority_score (see that module's own docstring). Only
        # runs at all when the patient actually supplied observed HPO
        # terms this run (`--hpo-terms`/`--phenotype-file` ->
        # `self.phenotype_result`) -- with none supplied, this feature
        # simply does not run (no forced/fabricated ranking), exactly
        # as `ACMGRuleEngine._pp4` already stays "not_evaluated" for
        # the same reason.
        if self.phenotype_result and self.phenotype_result.get("hpo_term_ids"):
            try:
                self._apply_case_phenotype_ranking(json_document.get("variants", []))
            except Exception:
                logger.exception("Case-level phenotype ranking failed; case_prioritization left unset for this run.")

        result_builder.write(json_path)
        self.report_generator.write(json_document, report_path)

        # Clinical PDF report (report/summary.py) -- additive output,
        # same as the Markdown report above. Wrapped separately: a
        # rendering bug here must never invalidate an otherwise-
        # successful run that already wrote valid JSON/Markdown.
        # `generate_pdf` itself already swallows a bad --patient-meta
        # file gracefully (see its docstring); this catches genuine
        # ReportLab/layout failures instead.
        pdf_filename = "geper_report_full.pdf"
        pdf_path = os.path.join(self.output_dir, pdf_filename)
        try:
            generate_pdf(json_document, pdf_path, patient_meta=self.patient_meta_path)
        except Exception as exc:  # noqa: BLE001 - additive output, must never fail an otherwise-successful run
            logger.error(f"Clinical PDF report generation failed ({exc}); JSON/Markdown outputs are unaffected.")
            pdf_path = None

        # Short-form companion PDF (report/summary_short.py) -- a
        # second rendering of this same `json_document`, no new
        # evidence gathering, always written alongside the full report
        # (no CLI flag: both are cheap pure-ReportLab renders of data
        # already in memory, and a lab filing one usually wants both).
        # Wrapped independently of the full report above so neither
        # PDF's failure can suppress the other.
        short_pdf_path = os.path.join(self.output_dir, "geper_report_short.pdf")
        try:
            generate_short_pdf(
                json_document,
                short_pdf_path,
                patient_meta=self.patient_meta_path,
                companion_filename=pdf_filename,
            )
        except Exception as exc:  # noqa: BLE001 - additive output, must never fail an otherwise-successful run
            logger.error(f"Short-form PDF report generation failed ({exc}); other outputs are unaffected.")
            short_pdf_path = None

        logger.info(
            f"GEPER run complete. {len(json_document['variants'])} variant(s) "
            f"total in output. JSON: '{json_path}', Report: '{report_path}'"
            + (f", PDF: '{pdf_path}'" if pdf_path else " (full PDF generation failed -- see error above)")
            + (
                f", Short PDF: '{short_pdf_path}'."
                if short_pdf_path
                else " (short PDF generation failed -- see error above)."
            )
        )
        self._log_run_summary(stats)

        if self.enable_profiling:
            self.profiler.mark_run_end()
            self.profiler.log_summary(logger)
            self._write_benchmark_report()

        return json_document

    # ------------------------------------------------------------------
    # Performance profiling / benchmark report
    # ------------------------------------------------------------------
    def _write_benchmark_report(self) -> None:
        """
        Persists the per-stage timing profile collected by
        `self.profiler` for this run to `geper_benchmark.json` and
        `geper_benchmark.md` in the output directory, so a before/after
        comparison across two runs (e.g. remote BLAST vs local, or with
        vs without BLAST prefetch/caching) doesn't depend on scraping
        log output. Never fatal -- a failure to write the report is
        logged and otherwise ignored, since it must not take down an
        otherwise-successful run.
        """
        try:
            json_path = os.path.join(self.output_dir, "geper_benchmark.json")
            md_path = os.path.join(self.output_dir, "geper_benchmark.md")
            with open(json_path, "w", encoding="utf-8") as fh:
                fh.write(self.profiler.to_json())
            with open(md_path, "w", encoding="utf-8") as fh:
                fh.write(self.profiler.to_markdown())
            logger.info(f"Wrote performance profile: '{json_path}', '{md_path}'.")
        except OSError as exc:  # noqa: BLE001 - profiling output must never fail a run
            logger.warning(f"Could not write benchmark report ({exc}).")

    # ------------------------------------------------------------------
    # Startup validation
    # ------------------------------------------------------------------
    def _run_startup_validation(self) -> None:
        """
        Load and smoke-test every *available* model exactly once, with a
        short valid dummy sequence, before any real variant is
        processed. This turns "the pipeline is 40 minutes into a run
        and HyenaDNA just crashed on variant #8,412" into "the startup
        report says HyenaDNA failed, right now, before anything else
        happened."

        Design decision, stated explicitly rather than left implicit:
        a model that FAILS this validation is not allowed to take the
        whole pipeline down with it (that would break the graceful-
        degradation architecture the rest of GEPER is built on, and
        would make one broken model a hard stop for every other model
        and every other variant). Instead, a failing model is demoted
        to unavailable for the rest of *this* run -- the same
        mechanism already used for a genuinely-missing package like
        HyenaDNA's `standalone_hyenadna` -- and the routed-model
        fallback in `_filter_available_models` takes over from there.
        This is "fail fast" at the level of an individual model
        (immediately, before wasting any variant-processing time on
        it) while keeping the pipeline itself resilient, which is what
        "production-ready" requires for a multi-model system: a single
        model's failure must never be a reason to produce zero results
        for the rest of an overnight run.

        Models instantiated here are cached into `self._model_instances`
        (the same dict `_run_dna_model` / `_run_rna_stage` /
        `_run_protein_stage` use), so a model validated at startup is
        reused, not reloaded, the first time a variant actually routes
        to it -- satisfying "load each model only once" end to end.
        """
        rows: List[Dict[str, str]] = []

        for key, model_cls in MODEL_REGISTRY.items():
            display = _MODEL_DISPLAY_NAMES.get(key, key)

            if not self._model_availability.get(key, True):
                reason = model_cls.unavailability_reason()
                rows.append(
                    {
                        "name": display,
                        "device": "n/a",
                        "precision": "n/a",
                        "max_length": "n/a",
                        "status": f"SKIP ({reason})",
                    }
                )
                continue

            dummy_sequence = _DUMMY_SEQUENCE_BY_MODEL.get(key)
            try:
                instance = self._model_instances.get(key) or model_cls()
                instance.predict(dummy_sequence)
                self._model_instances[key] = instance
                max_len = instance.reported_max_length()
                rows.append(
                    {
                        "name": display,
                        "device": str(instance.device),
                        "precision": instance._report_precision(),
                        "max_length": str(max_len) if max_len is not None else "n/a",
                        "status": "PASS",
                    }
                )
            except (ModelLoadError, ModelInferenceError) as exc:
                message = str(exc)[:600]
                logger.error(
                    f"Startup validation FAILED for '{key}' ({display}): "
                    f"{message}. Demoting this model to unavailable for "
                    "the remainder of this run; variants that would have "
                    "routed to it will fall back per the router's rules."
                )
                self._model_availability[key] = False
                self._model_stage_errors.setdefault(key, message)
                rows.append(
                    {
                        "name": display,
                        "device": "n/a",
                        "precision": "n/a",
                        "max_length": "n/a",
                        "status": f"FAIL ({message[:120]})",
                    }
                )

        self._log_startup_report(rows)

    @staticmethod
    def _log_startup_report(rows: List[Dict[str, str]]) -> None:
        """
        Render the startup model-validation table.

        Column widths are computed dynamically from the actual content
        of every row (with a floor matching the original nominal
        widths), rather than hardcoded fixed widths. A hardcoded
        `Precision` width of 16, for example, silently breaks the
        moment any model reports a precision string longer than that
        -- Python's `f"{s:<16}"` does NOT truncate an already-longer
        string, it just emits it unpadded, so the very next column's
        content gets glued directly onto the end of it with zero
        separating whitespace. This is exactly what happened for
        AlphaMissense's `_report_precision()` -- `"n/a (lookup table,
        no weights)"` is 31 characters, so its row rendered as
        `...n/a (lookup table, no weights)n/a         PASS` with no
        gap before the Max Length column's own "n/a". Computing each
        column's width as `max(nominal_floor, longest value in that
        column) + gap` guarantees at least one gap's worth of
        whitespace between every column for every row, independent of
        how long any individual field happens to be -- covering not
        just this specific string but any future model name / status
        message / precision string that doesn't fit the original
        assumptions either.
        """
        gap = 2
        columns = [
            ("name", "Model", 24),
            ("device", "Device", 10),
            ("precision", "Precision", 16),
            ("max_length", "Max Length", 12),
            ("status", "Status", 0),  # last column: no padding needed
        ]

        widths = {}
        for key, label, floor in columns:
            longest_value = max((len(str(row.get(key, ""))) for row in rows), default=0)
            widths[key] = max(floor, len(label), longest_value) + gap

        def _render_row(values: Dict[str, str]) -> str:
            parts = []
            for i, (key, _label, _floor) in enumerate(columns):
                text = str(values.get(key, ""))
                if i == len(columns) - 1:
                    parts.append(text)  # last column: no trailing padding
                else:
                    parts.append(f"{text:<{widths[key]}}")
            return "".join(parts)

        header = _render_row({key: label for key, label, _floor in columns})
        lines = ["", "===== GEPER Startup Model Validation =====", "", header, "-" * len(header)]
        for row in rows:
            lines.append(_render_row(row))
        lines.append("=" * len(header))
        logger.info("\n".join(lines))

    # ------------------------------------------------------------------
    # Run-summary / bookkeeping helpers (issue #7, #8)
    # ------------------------------------------------------------------
    @staticmethod
    def _variant_key(variant: Variant) -> Tuple[str, int, str, str]:
        return (variant.chrom, variant.pos, variant.ref, variant.alt)

    @staticmethod
    def _variant_key_from_dict(variant_dict: Dict[str, Any]) -> Tuple[str, int, str, str]:
        return (
            variant_dict.get("chrom"),
            variant_dict.get("pos"),
            variant_dict.get("ref"),
            variant_dict.get("alt"),
        )

    @staticmethod
    def _apply_priority_ranks(variant_results: List[Dict[str, Any]]) -> None:
        """
        Phase 4: assigns each variant's batch-relative `priority_rank`
        (1 = highest priority in this run) via
        `prioritization_engine.rank_batch`, mutating each variant's
        `interpretation_result` in place.

        Note: `variant_result["interpretation_result"]` and
        `variant_result["interpretation"]["interpretation_result"]` are
        the same dict object (see `build_variant_result`'s passthrough
        logic), so mutating one key here updates both places a caller
        might look for it -- no double-write needed.

        Variants where prioritization failed (`priority_score` is
        `None`, or `interpretation_result` is missing/errored) get
        `priority_rank = None` rather than a fabricated position.
        """
        scores: List[Any] = []
        targets: List[Any] = []
        for vr in variant_results:
            ir = vr.get("interpretation_result")
            if isinstance(ir, dict) and "error" not in ir:
                scores.append(ir.get("priority_score"))
                targets.append(ir)
            else:
                scores.append(None)
                targets.append(None)

        ranks = rank_batch(scores)
        for target, rank in zip(targets, ranks):
            if target is not None:
                target["priority_rank"] = rank

    def _apply_case_phenotype_ranking(self, variant_results: List[Dict[str, Any]]) -> None:
        """
        Case-level, HPO-phenotype-driven ranking (see
        `pipeline/case_prioritization.py` for the full design
        rationale and why this is NOT the same thing as
        `_apply_priority_ranks` above). Only called from `run()` when
        `self.phenotype_result` actually carries patient-observed HPO
        terms this run.

        Writes each variant's result under a NEW, separate
        `case_prioritization` top-level key -- deliberately not nested
        inside `interpretation_result` (which `_apply_priority_ranks`
        mutates) -- so it is visually and structurally obvious in the
        JSON output that this signal never touched ACMG/PP4/confidence/
        priority, matching the strict separation this feature's spec
        requires.

        Per-variant `hpo` results and `priority_score` are read
        straight off each already-built `variant_results` entry
        (`vr["hpo"]`, `vr["interpretation_result"]["priority_score"]`)
        -- nothing here re-queries HPO or recomputes priority.
        """
        ontology = get_shared_ontology()
        patient_term_ids = list(self.phenotype_result.get("hpo_term_ids") or [])

        hpo_results: List[Optional[Dict[str, Any]]] = []
        priority_scores: List[Optional[float]] = []
        for vr in variant_results:
            hpo_results.append(vr.get("hpo"))
            ir = vr.get("interpretation_result")
            priority_scores.append(ir.get("priority_score") if isinstance(ir, dict) and "error" not in ir else None)

        results = rank_case(hpo_results, priority_scores, patient_term_ids, ontology)
        for vr, result in zip(variant_results, results):
            vr["case_prioritization"] = result.to_dict()

    @staticmethod
    def _classify_variant_result(variant_result: Dict[str, Any]) -> str:
        """
        Buckets one variant's result into success/skipped/failed for the
        run summary. A variant with no sequence context could never
        have been analyzed by any model, so it's "skipped". A variant
        is "failed" only when it has no usable ACMG interpretation to
        show for it (`interpretation_result` missing or itself an
        `{"error": ...}` record -- the same check `_apply_priority_ranks`
        already uses) -- NOT merely because `errors` is non-empty.
        `errors` accumulates every independently-caught, non-fatal
        per-stage failure (a single BLAST/ClinVar/gnomAD/etc. lookup
        hiccup, one AI model failing to load, ...), any one of which
        used to flip an otherwise fully-interpreted variant to "failed"
        in the summary even though a complete, valid ACMG report was
        produced. Best-effort partial evidence is still a "success"
        here; per-stage detail remains visible in `errors` on the
        variant itself for anyone who wants it.
        """
        context = variant_result.get("sequence_context", {})
        if context.get("error"):
            return "skipped"
        interpretation_result = variant_result.get("interpretation_result")
        if not isinstance(interpretation_result, dict) or "error" in interpretation_result:
            return "failed"
        return "success"

    def _log_run_summary(self, stats: Dict[str, int]) -> None:
        """Print the clear, glanceable end-of-run summary requested in issue #7."""
        lines = ["", "===== GEPER Run Summary =====", "", "Loaded:"]
        for key in MODEL_REGISTRY:
            display = _MODEL_DISPLAY_NAMES.get(key, key)
            if not self._model_availability.get(key, True):
                reason = MODEL_REGISTRY[key].unavailability_reason()
                lines.append(f"  \u2717 {display} ({reason})")
            elif ModelCache.is_cached(key):
                lines.append(f"  \u2713 {display}")
            elif key in self._model_stage_errors:
                lines.append(f"  \u2717 {display} (failed: {self._model_stage_errors[key]})")
            else:
                lines.append(f"  \u2013 {display} (not invoked -- no variant routed to it)")
        lines.append("")
        lines.append(f"Processed variants: {stats['processed']}")
        lines.append(f"Successful predictions: {stats['success']}")
        lines.append(f"Skipped: {stats['skipped']}")
        lines.append(f"Failed: {stats['failed']}")
        lines.append("==============================")
        logger.info("\n".join(lines))

    # ------------------------------------------------------------------
    # Per-variant pipeline
    # ------------------------------------------------------------------
    def _process_variant(self, variant: Variant) -> Dict[str, Any]:
        errors: List[str] = []

        # --- Variant normalization: early gate (see pipeline/variant_normalization.py) ---
        # Runs BEFORE sequence-context fetching or any provider/ACMG-rule
        # stage below -- every one of them keys off variant.pos/ref/alt,
        # so `variant` is replaced here (not just annotated) with its
        # normalized form. Multi-allelic splitting already happened in
        # `pipeline/vcf_parser.py`; this only does parsimony trimming +
        # (when enabled) reference-guided left-alignment of indels.
        normalization_result = self._run_normalization_stage(variant, errors)
        if normalization_result.get("normalized"):
            n = normalization_result["normalized"]
            variant = dataclasses.replace(variant, pos=n["pos"], ref=n["ref"], alt=n["alt"])

        sequence_context = None
        try:
            flank_size = self.router.recommended_flank_size(variant)
            with self._timer("sequence_context"):
                sequence_context = self.sequence_context_gen.build_context(variant, flank_size)
        except (SequenceGenerationError, ExternalAPIError) as exc:
            errors.append(f"Sequence context generation failed: {exc}")
            logger.error(errors[-1])

        dna_model_results: Dict[str, Any] = {}
        dna_models_used: List[str] = []
        if sequence_context is not None:
            try:
                model_keys = self.router.route(variant, sequence_context)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Routing failed: {exc}")
                model_keys = []

            model_keys = self._filter_available_models(model_keys, variant, errors)

            for model_key in model_keys:
                try:
                    result = self._run_dna_model(model_key, sequence_context.alt_sequence)
                    dna_model_results[model_key] = result
                    dna_models_used.append(model_key)
                except (ModelLoadError, ModelInferenceError) as exc:
                    errors.append(f"Model '{model_key}' failed: {exc}")
                    logger.error(errors[-1])
                    self._model_stage_errors.setdefault(model_key, str(exc)[:120])

        # ClinGen + transcript-structure resolution: moved ahead of the
        # protein/AlphaMissense stages below (they used to run first,
        # before any transcript data existed) because
        # `_run_alphamissense_stage` now needs `transcript_result` to
        # classify missense eligibility via the same transcript-CDS-frame
        # `protein_effect_flags` machinery BP7/BP1 use, rather than the
        # legacy protein-translator window (see
        # `pipeline/router.py::SequenceRouter.is_missense_eligible`'s
        # docstring). Neither stage depends on anything computed below
        # it here -- `_run_clingen_stage` only needs `variant`, and
        # `_run_transcript_stage` only needs `variant` + `clingen_result`
        # -- so this reorder changes no other stage's inputs.
        clingen_result = self._run_clingen_stage(variant, errors)
        transcript_result = self._run_transcript_stage(variant, clingen_result, errors)
        self._attach_hgvs_c(normalization_result, transcript_result)

        rna_result = self._run_rna_stage(variant, sequence_context, errors)
        protein_result = self._run_protein_stage(variant, sequence_context, errors)
        alphamissense_result = self._run_alphamissense_stage(variant, transcript_result, errors)
        mmsplice_result = self._run_mmsplice_stage(variant, errors)
        ensemble_result = self._run_ensemble_stage(variant, sequence_context, errors)
        # Standalone splice-prediction plugins for BP7 (see
        # pipeline/acmg_rules.py::ACMGRuleEngine._bp7) -- distinct from
        # the Enformer/Borzoi ensemble above (see
        # _run_standalone_splice_plugin_stage's own docstring for why).
        spliceformer_result = self._run_standalone_splice_plugin_stage("spliceformer", sequence_context, errors)
        splicebert_result = self._run_standalone_splice_plugin_stage("splicebert", sequence_context, errors)

        blast_result = self._run_blast_stage(sequence_context, errors)
        dbsnp_result = self._run_dbsnp_stage(variant, errors)
        clinvar_result = self._run_clinvar_stage(variant, dbsnp_result, errors)
        gnomad_result = self._run_gnomad_stage(variant, errors)
        indigenomes_result = self._run_indigenomes_stage(variant, errors)
        conservation_result = self._run_conservation_stage(variant, errors)
        hpo_result = self._run_hpo_stage(clingen_result, errors)
        orphanet_result = self._run_orphanet_stage(clingen_result, errors)
        clinvar_codon_result = self._run_clinvar_codon_stage(variant, transcript_result, errors)
        functional_evidence_result = self._run_functional_evidence_stage(
            variant, clingen_result, transcript_result, errors
        )

        # Biological evidence layer: UniProt -> InterPro/Pfam -> AlphaFold
        # DB, in that order -- InterPro and AlphaFold are both keyed by
        # the UniProt accession the first stage resolves, and both take
        # the same transcript-verified canonical protein position (see
        # `canonical_protein_position`'s docstring in pipeline/pvs1/utils.py
        # for exactly what it guarantees and when it is honestly `None`
        # instead of a guess).
        protein_position = canonical_protein_position(transcript_result, variant.pos)
        uniprot_result = self._run_uniprot_stage(variant, clingen_result, errors)
        interpro_result = self._run_interpro_stage(uniprot_result, protein_position, errors)
        alphafold_result = self._run_alphafold_stage(uniprot_result, protein_position, errors)

        interpretation = self.interpretation_engine.interpret(
            variant_dict=variant.to_dict(),
            dna_models_used=dna_models_used,
            clinvar_result=clinvar_result,
            dbsnp_result=dbsnp_result,
            protein_result=protein_result,
            blast_result=blast_result,
            alphamissense_result=alphamissense_result,
            mmsplice_result=mmsplice_result,
            gnomad_result=gnomad_result,
            conservation_result=conservation_result,
            clingen_result=clingen_result,
            uniprot_result=uniprot_result,
            interpro_result=interpro_result,
            alphafold_result=alphafold_result,
            rna_result=rna_result,
            ensemble_result=ensemble_result,
            transcript_result=transcript_result,
            clinvar_codon_result=clinvar_codon_result,
            hpo_result=hpo_result,
            phenotype_result=self.phenotype_result,
            functional_evidence_result=functional_evidence_result,
            spliceformer_result=spliceformer_result,
            splicebert_result=splicebert_result,
        )

        context_dict = (
            {
                "chrom": sequence_context.chrom,
                "window_start": sequence_context.window_start,
                "window_end": sequence_context.window_end,
                "flank_size": sequence_context.flank_size,
                "length": sequence_context.length,
            }
            if sequence_context is not None
            else {"error": "sequence context unavailable"}
        )

        ai_model_status = self._build_ai_model_status(
            dna_models_used=dna_models_used,
            dna_model_results=dna_model_results,
            rna_result=rna_result,
            protein_result=protein_result,
            alphamissense_result=alphamissense_result,
            mmsplice_result=mmsplice_result,
            ensemble_result=ensemble_result,
            spliceformer_result=spliceformer_result,
            splicebert_result=splicebert_result,
        )

        self._capture_stage_provenance(
            clinvar_result=clinvar_result,
            dbsnp_result=dbsnp_result,
            gnomad_result=gnomad_result,
            uniprot_result=uniprot_result,
            interpro_result=interpro_result,
            alphafold_result=alphafold_result,
            functional_evidence_result=functional_evidence_result,
        )

        return build_variant_result(
            variant_dict=variant.to_dict(),
            sequence_context=context_dict,
            dna_model_results=dna_model_results,
            rna_result=rna_result,
            protein_result=protein_result,
            alphamissense_result=alphamissense_result,
            mmsplice_result=mmsplice_result,
            blast_result=blast_result,
            clinvar_result=clinvar_result,
            dbsnp_result=dbsnp_result,
            gnomad_result=gnomad_result,
            indigenomes_result=indigenomes_result,
            conservation_result=conservation_result,
            clingen_result=clingen_result,
            uniprot_result=uniprot_result,
            interpro_result=interpro_result,
            alphafold_result=alphafold_result,
            transcript_result=transcript_result,
            clinvar_codon_result=clinvar_codon_result,
            hpo_result=hpo_result,
            orphanet_result=orphanet_result,
            functional_evidence_result=functional_evidence_result,
            normalization_result=normalization_result,
            spliceformer_result=spliceformer_result,
            splicebert_result=splicebert_result,
            interpretation=interpretation,
            errors=errors,
            ai_splicing_ensemble_result=ensemble_result,
            ai_model_status=ai_model_status,
        )

    def _capture_stage_provenance(
        self,
        *,
        clinvar_result: Dict[str, Any],
        dbsnp_result: Dict[str, Any],
        gnomad_result: Optional[Dict[str, Any]],
        uniprot_result: Optional[Dict[str, Any]],
        interpro_result: Optional[Dict[str, Any]],
        alphafold_result: Optional[Dict[str, Any]],
        functional_evidence_result: Optional[Dict[str, Any]],
    ) -> None:
        """
        Per-variant provenance capture for the sources whose version
        signal only appears in an actual query response (unlike the
        run-level captures in `_capture_startup_provenance`). Called
        once per variant; cheap and idempotent -- `RunProvenanceCollector
        .record`'s own priority ordering means calling this every
        variant, not just the first, is harmless (a later call with the
        same or better info just re-confirms/upgrades the existing
        record, never downgrades it). Never raises: each source's
        capture is independent, so one malformed result can't prevent
        the others from being recorded.
        """
        try:
            if clinvar_result:
                if clinvar_result.get("error"):
                    self.provenance.record(
                        "ClinVar", VersionStatus.UNKNOWN, notes=f"Most recent query failed: {clinvar_result['error']}"
                    )
                else:
                    self.provenance.record(
                        "ClinVar",
                        VersionStatus.TIMESTAMP_ONLY,
                        notes="ClinVar E-utilities exposes no database-wide release version; each matched record's own 'last_evaluated' date is captured in that record already.",
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"ClinVar provenance capture failed: {exc}")

        try:
            if dbsnp_result:
                build = (dbsnp_result.get("detail") or {}).get("dbsnp_build")
                if build:
                    self.provenance.record("dbSNP", VersionStatus.VERSION_KNOWN, version=f"dbSNP build {build}")
                elif dbsnp_result.get("error"):
                    self.provenance.record(
                        "dbSNP", VersionStatus.UNKNOWN, notes=f"Most recent query failed: {dbsnp_result['error']}"
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"dbSNP provenance capture failed: {exc}")

        try:
            if gnomad_result and not gnomad_result.get("skipped"):
                dataset_id = gnomad_dataset_id_for_build(self.sequence_context_gen.assembly or "GRCh38")
                if dataset_id:
                    self.provenance.record("gnomAD", VersionStatus.VERSION_KNOWN, version=dataset_id)
                elif gnomad_result.get("error"):
                    self.provenance.record(
                        "gnomAD", VersionStatus.UNKNOWN, notes=f"Query failed: {gnomad_result['error']}"
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"gnomAD provenance capture failed: {exc}")

        try:
            if uniprot_result and not uniprot_result.get("skipped"):
                if uniprot_result.get("release"):
                    self.provenance.record(
                        "UniProt",
                        VersionStatus.VERSION_KNOWN,
                        version=f"UniProt {uniprot_result['release']}",
                        release_date=uniprot_result.get("release_date"),
                    )
                elif uniprot_result.get("error"):
                    self.provenance.record(
                        "UniProt", VersionStatus.UNKNOWN, notes=f"Most recent query failed: {uniprot_result['error']}"
                    )
                elif uniprot_result.get("source") == "local_dataset":
                    self.provenance.record(
                        "UniProt",
                        VersionStatus.TIMESTAMP_ONLY,
                        notes="Served from a locally-configured dataset this run, not the live REST API; no release header available.",
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"UniProt provenance capture failed: {exc}")

        try:
            if interpro_result and not interpro_result.get("skipped"):
                if interpro_result.get("api_version"):
                    self.provenance.record(
                        "InterPro", VersionStatus.VERSION_KNOWN, version=f"InterPro {interpro_result['api_version']}"
                    )
                elif interpro_result.get("error"):
                    self.provenance.record(
                        "InterPro", VersionStatus.UNKNOWN, notes=f"Most recent query failed: {interpro_result['error']}"
                    )
                elif interpro_result.get("source") == "local_dataset":
                    self.provenance.record(
                        "InterPro",
                        VersionStatus.TIMESTAMP_ONLY,
                        notes="Served from a locally-configured dataset this run, not the live REST API; no version header available.",
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"InterPro provenance capture failed: {exc}")

        try:
            if alphafold_result and not alphafold_result.get("skipped"):
                if alphafold_result.get("model_version"):
                    self.provenance.record(
                        "AlphaFold DB",
                        VersionStatus.VERSION_KNOWN,
                        version=f"AlphaFold DB v{alphafold_result['model_version']}",
                    )
                elif alphafold_result.get("error"):
                    self.provenance.record(
                        "AlphaFold DB",
                        VersionStatus.UNKNOWN,
                        notes=f"Most recent query failed: {alphafold_result['error']}",
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"AlphaFold DB provenance capture failed: {exc}")

        try:
            if functional_evidence_result:
                source = functional_evidence_result.get("source")
                if source == "clingen_erepo":
                    self.provenance.record(
                        "Functional evidence (ClinGen ERepo)",
                        VersionStatus.TIMESTAMP_ONLY,
                        notes="No source-wide API version exposed; individual records carry their own 'publishedDate'.",
                    )
                elif source == "mavedb":
                    self.provenance.record(
                        "Functional evidence (MaveDB)",
                        VersionStatus.TIMESTAMP_ONLY,
                        notes="No source-wide API version exposed; individual score sets carry their own 'publishedDate'/'modificationDate'.",
                    )
                elif functional_evidence_result.get("error"):
                    # The composite provider doesn't disclose which of
                    # ERepo/MaveDB the failure was in -- honestly
                    # recorded against both rather than guessing one.
                    note = f"A functional-evidence query failed this run: {functional_evidence_result['error']}"
                    self.provenance.record("Functional evidence (ClinGen ERepo)", VersionStatus.UNKNOWN, notes=note)
                    self.provenance.record("Functional evidence (MaveDB)", VersionStatus.UNKNOWN, notes=note)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Functional-evidence provenance capture failed: {exc}")

    # ------------------------------------------------------------------
    # Individual stage helpers
    # ------------------------------------------------------------------
    def _filter_available_models(self, model_keys: List[str], variant: Variant, errors: List[str]) -> List[str]:
        """
        Drop any routed model that isn't available in this environment
        (issue #2: HyenaDNA missing must never crash the pipeline --
        it should be skipped, logged, and the run should continue). If
        every routed model turns out to be unavailable, fall back to
        HyenaDNA (GEPER's universal default DNA model; itself
        defensively length-capped) so the variant still gets *some*
        DNA-level analysis rather than none. HyenaDNA previously
        filled this same "last resort" role for the sub-case DNABERT-2
        used to cover -- DNABERT-2 itself has been removed from GEPER
        entirely (see LICENSE_AUDIT.md).

        Note: a graceful "model X isn't installed, skipping" is logged
        but deliberately *not* added to the variant's `errors` list
        when a fallback still produces a result -- this is expected,
        handled degradation, not a failure, and shouldn't count as one
        in the run summary (issue #7).

        Logging policy: a model's unavailability is a fixed, run-wide
        fact established once at startup (constructor-time package
        detection, then confirmed/refined by `_run_startup_validation`)
        -- it does not change from variant to variant, so re-logging a
        full warning for every single variant that happens to route to
        it is pure noise on a multi-million-variant VCF (this was
        reported as issue #2: the same "not installed; skipping"
        message repeating for every variant). Each missing model is
        therefore logged in detail exactly once per run, the first
        time routing actually needs it; every subsequent occurrence is
        a silent, free (Set membership check) skip.
        """
        available = [k for k in model_keys if self._model_availability.get(k, True)]
        missing = [k for k in model_keys if not self._model_availability.get(k, True)]

        for model_key in missing:
            if model_key in self._logged_missing_at_routing:
                continue
            self._logged_missing_at_routing.add(model_key)
            display = _MODEL_DISPLAY_NAMES.get(model_key, model_key)
            logger.warning(
                f"Model '{model_key}' ({display}) is not available "
                f"(unavailable/not installed) and will be skipped for "
                f"every variant routed to it this run -- first "
                f"encountered at {variant.chrom}:{variant.pos}. This "
                "message will not repeat for subsequent variants."
            )

        if not available and model_keys:
            if self._model_availability.get(HYENADNA, True):
                logger.warning(
                    f"{variant.chrom}:{variant.pos}: all routed models were unavailable; falling back to HyenaDNA."
                )
                available = [HYENADNA]
            else:
                errors.append("No DNA foundation model available for this variant.")

        return available

    def _run_dna_model(self, model_key: str, sequence: str) -> Dict[str, Any]:
        if model_key not in self._model_instances:
            model_class = MODEL_REGISTRY[model_key]
            self._model_instances[model_key] = model_class()
        model = self._model_instances[model_key]
        with self._timer(f"model:{model_key}"):
            return model.predict(sequence)

    def _run_rna_stage(self, variant: Variant, sequence_context, errors: List[str]) -> Dict[str, Any]:
        if sequence_context is None:
            return {"skipped": True, "reason": "no sequence context available"}
        if not self.router.requires_rna_analysis(variant):
            return {"skipped": True, "reason": "variant not flagged as transcript-relevant"}

        try:
            rna_context = self.rna_generator.generate(sequence_context)
            if "rna_fm" not in self._model_instances:
                self._model_instances["rna_fm"] = MODEL_REGISTRY["rna_fm"]()
            with self._timer("model:rna_fm"):
                rna_fm_result = self._model_instances["rna_fm"].predict(rna_context.alt_rna)
            rna_fm_result["ref_rna_preview"] = rna_context.ref_rna[:60]
            rna_fm_result["alt_rna_preview"] = rna_context.alt_rna[:60]
            return rna_fm_result
        except (SequenceGenerationError, ModelLoadError, ModelInferenceError) as exc:
            errors.append(f"RNA-FM stage failed: {exc}")
            logger.error(errors[-1])
            self._model_stage_errors.setdefault("rna_fm", str(exc)[:120])
            # `skipped: True` is kept (not flipped to False) so every
            # existing downstream reader that gates on it -- e.g.
            # `pipeline/interpretation_result.py`'s
            # `ai_context_models` check -- keeps treating a crashed
            # stage exactly like a legitimately-skipped one for
            # control-flow purposes (there is no usable RNA-FM output
            # either way). `error` is new: it's what lets
            # `pipeline/stage_schemas.py::StageEvidence.from_raw`
            # resolve this to `ERROR` instead of `NOT_RUN` -- see that
            # module's docstring for why the two were indistinguishable
            # before this key existed.
            return {"skipped": True, "reason": str(exc), "error": str(exc)}

    def _run_protein_stage(self, variant: Variant, sequence_context, errors: List[str]) -> Dict[str, Any]:
        if sequence_context is None:
            return {"skipped": True, "reason": "no sequence context available"}
        if not self.router.requires_protein_analysis(variant):
            return {"skipped": True, "reason": "variant flagged as non-coding"}

        try:
            rna_context = self.rna_generator.generate(sequence_context)
            protein_context = self.protein_translator.translate_context(rna_context)

            if not protein_context.alt_orf_found:
                return {
                    "skipped": True,
                    "reason": "no open reading frame (start codon) found in window",
                    "translation": {
                        "ref_protein": protein_context.ref_protein,
                        "alt_protein": protein_context.alt_protein,
                    },
                }

            if "esm2" not in self._model_instances:
                self._model_instances["esm2"] = MODEL_REGISTRY["esm2"]()
            with self._timer("model:esm2"):
                esm2_result = self._model_instances["esm2"].predict(protein_context.alt_protein)

            return {
                "skipped": False,
                "translation": {
                    "ref_protein": protein_context.ref_protein,
                    "alt_protein": protein_context.alt_protein,
                },
                "esm2": esm2_result,
            }
        except (SequenceGenerationError, ModelLoadError, ModelInferenceError) as exc:
            errors.append(f"Protein/ESM-2 stage failed: {exc}")
            logger.error(errors[-1])
            self._model_stage_errors.setdefault("esm2", str(exc)[:120])
            # `skipped: True` stays True (see `_run_rna_stage`'s
            # matching comment for why -- ESM-2's own downstream
            # consumers still gate on this). Note `_run_alphamissense_stage`
            # no longer reads this result at all: its eligibility gate
            # is decided from `transcript_result`, not from whether
            # this ESM-2 translation succeeded (see that method's
            # docstring). `error` is new -- see `_run_rna_stage`'s comment.
            return {"skipped": True, "reason": str(exc), "error": str(exc)}

    def _run_alphamissense_stage(
        self, variant: Variant, transcript_result: Dict[str, Any], errors: List[str]
    ) -> Dict[str, Any]:
        """
        Routes to AlphaMissense only for variants the router classifies
        as an eligible missense substitution (see
        SequenceRouter.is_missense_eligible) -- never for synonymous,
        intronic, splice, frameshift, or structural variants. Eligibility
        is decided from `transcript_result` (the transcript-CDS-frame
        `protein_effect_flags` machinery BP7/BP1 also use), not from the
        protein stage's ESM-2 translation -- AlphaMissense's own lookup
        below is keyed on genomic chrom:pos:ref:alt, never on the
        translated ref/alt protein string, so it has no dependency on
        that stage having found an ORF in its local window. Runs after
        `_run_transcript_stage` (earlier in `_process_variant`) rather
        than after the protein stage for that reason.

        Follows the exact same graceful-degradation shape as every
        other stage: a missing/failed AlphaMissense never aborts the
        variant, only records `errors` and returns a `skipped` result.
        """
        if not self.router.is_missense_eligible(variant, transcript_result):
            return {
                "skipped": True,
                "reason": "variant is not an eligible missense substitution "
                "(synonymous, nonsense, frameshift, structural, non-coding, "
                "or undeterminable-consequence variants are never routed to "
                "AlphaMissense)",
            }

        if not self._model_availability.get("alphamissense", True):
            if "alphamissense" not in self._logged_missing_at_routing:
                self._logged_missing_at_routing.add("alphamissense")
                logger.warning(
                    f"AlphaMissense is not available in this environment "
                    f"and will be skipped for every missense variant this "
                    f"run -- first encountered at {variant.chrom}:{variant.pos}. "
                    "This message will not repeat for subsequent variants."
                )
            return {"skipped": True, "reason": "AlphaMissense not available in this environment"}

        lookup_key = f"{variant.chrom}:{variant.pos}:{variant.ref}:{variant.alt}"
        assembly = self.sequence_context_gen.assembly
        try:
            if "alphamissense" not in self._model_instances:
                self._model_instances["alphamissense"] = MODEL_REGISTRY["alphamissense"]()
            with self._timer("model:alphamissense"):
                result = self._model_instances["alphamissense"].predict(lookup_key, assembly=assembly)
            result["skipped"] = False
            return result
        except (ModelLoadError, ModelInferenceError) as exc:
            errors.append(f"AlphaMissense stage failed: {exc}")
            logger.error(errors[-1])
            self._model_stage_errors.setdefault("alphamissense", str(exc)[:120])
            # `skipped: True` stays True (see `_run_rna_stage`'s
            # matching comment); `error` is new -- and, unlike
            # RNA-FM/protein, this one also flows into
            # `pipeline/stage_schemas.py::RawEvidenceBundle` directly
            # (`alphamissense` is one of its 11 required fields), so
            # this fix is what lets that boundary resolve a genuine
            # AlphaMissense crash to `ERROR` instead of `NOT_RUN`.
            return {"skipped": True, "reason": str(exc), "error": str(exc)}

    def _run_mmsplice_stage(self, variant: Variant, errors: List[str]) -> Dict[str, Any]:
        """
        Runs MMSplice for every variant (unlike AlphaMissense, eligibility
        here is a splice-window distance check MMSpliceService itself
        performs -- there's no upstream translation step to gate on
        first). Never raises: `MMSpliceService.predict` already
        implements requirement #13's "never crash the pipeline"
        contract internally, returning a well-formed
        `supported=False/predicted=False` result with a recorded reason
        for anything from "wrong variant type" to "Ensembl unreachable"
        to an unexpected internal error -- this wrapper's own try/except
        is a second, defense-in-depth layer plus the same
        model-availability short-circuit every other stage uses.
        """
        if not self._model_availability.get("mmsplice", True):
            if "mmsplice" not in self._logged_missing_at_routing:
                self._logged_missing_at_routing.add("mmsplice")
                logger.warning(
                    f"MMSplice is not available in this environment and will "
                    f"be skipped for every variant this run -- first "
                    f"encountered at {variant.chrom}:{variant.pos}. This "
                    "message will not repeat for subsequent variants."
                )
            return {
                "supported": False,
                "predicted": False,
                "skip_reason": "MMSplice not available in this environment",
                "interpretation": "MMSplice not available in this environment",
            }

        try:
            service = self._get_mmsplice_service()
            with self._timer("model:mmsplice"):
                # `predict()` checks `MMSplicePredictionCache` first
                # (see `MMSpliceService._prepare`), so any variant
                # already scored by `_prefetch_mmsplice_results`'s
                # batched pass below is a pure cache hit here -- same
                # result dict, no repeat Keras call.
                return service.predict(variant)
        except Exception as exc:  # noqa: BLE001 - final defense-in-depth; MMSpliceService.predict should already catch everything
            errors.append(f"MMSplice stage failed: {exc}")
            logger.error(errors[-1])
            self._model_stage_errors.setdefault("mmsplice", str(exc)[:120])
            # `supported`/`predicted` stay False (this stage has no
            # "skipped" key at all, so keeping both False is what
            # already-existing downstream readers of this shape
            # expect for "no usable MMSplice result"). `error` is new
            # -- MMSplice is one of `RawEvidenceBundle`'s 11 required
            # fields (see `pipeline/stage_schemas.py`), and without
            # this key a genuine crash here previously resolved to
            # `NOT_FOUND` ("checked, no splice effect"), not just
            # `NOT_RUN` -- an even more misleading label for a crash,
            # since it implied the lookup succeeded.
            return {
                "supported": False,
                "predicted": False,
                "skip_reason": str(exc)[:300],
                "interpretation": str(exc)[:300],
                "error": str(exc)[:300],
            }

    def _get_mmsplice_service(self) -> MMSpliceService:
        """
        Idempotently construct (or reuse) the single `MMSpliceService`
        for this run, wrapped around the *same* `MMSpliceModel`
        instance startup validation / `_model_instances` uses -- never
        a second, independently-loaded copy of the 5 Keras submodels.
        Shared by `_run_mmsplice_stage` (per-variant) and
        `_prefetch_mmsplice_results` (batched) so both go through
        identical construction and therefore share one
        `MMSplicePredictionCache`.
        """
        if "mmsplice" not in self._model_instances:
            self._model_instances["mmsplice"] = MODEL_REGISTRY["mmsplice"]()
        if self._mmsplice_service is None or self._mmsplice_service.model is not self._model_instances["mmsplice"]:
            self._mmsplice_service = MMSpliceService(
                model=self._model_instances["mmsplice"],
                sequence_context_generator=self.sequence_context_gen,
                species=self.sequence_context_gen.species,
                assembly=self.sequence_context_gen.assembly,
            )
        return self._mmsplice_service

    def _prefetch_blast_results(self, variants: List[Variant]) -> None:
        """
        Best-effort batch warm-up of the BLAST client's cache for an
        entire variant list. Builds each variant's sequence context
        (reusing the already-prefetched Ensembl region cache, so this
        adds no network calls of its own beyond what per-variant
        processing would do anyway), collects the distinct
        `alt_sequence` values, and submits them to
        `BLASTClient.search_many()` -- which runs them concurrently
        (remote) or in parallel local subprocesses (local) instead of
        one blocking call per variant later in the main loop.

        A variant whose sequence context can't be built here (e.g. a
        transient Ensembl error not covered by the prefetch cache) is
        simply skipped -- `_process_variant` will build it again the
        normal way when it reaches that variant, and `_run_blast_stage`
        will BLAST it individually at that point exactly as it always
        has. Nothing here is a precondition for correctness, only for
        speed.
        """
        sequences: List[str] = []
        seen = set()
        for variant in variants:
            try:
                flank_size = self.router.recommended_flank_size(variant)
                context = self.sequence_context_gen.build_context(variant, flank_size)
            except (SequenceGenerationError, ExternalAPIError) as exc:
                logger.warning(
                    f"BLAST prefetch: could not build sequence context for "
                    f"{variant.chrom}:{variant.pos} ({exc}); this variant will "
                    "get its own individual BLAST call during normal processing."
                )
                continue
            if context.alt_sequence not in seen:
                seen.add(context.alt_sequence)
                sequences.append(context.alt_sequence)

        if not sequences:
            return

        logger.info(
            f"BLAST prefetch: submitting {len(sequences)} distinct sequence(s) "
            f"(from {len(variants)} variant(s)) via '{self.blast_client.mode}' mode."
        )
        self.blast_client.search_many(sequences)

    def _prefetch_mmsplice_results(self, variants: List[Variant]) -> None:
        """
        Best-effort batch warm-up of the MMSplice prediction cache for
        an entire variant list -- same shape as
        `_prefetch_blast_results` immediately above.

        This is execution-path only: `MMSpliceService.predict_batch()`
        and the `MMSpliceModel.score_modular_batch()` /
        `MMSplicePredictor.predict_batch()` machinery it calls already
        existed (see loader.py/predictor.py/service.py) but nothing in
        the orchestrator ever invoked them -- `_run_mmsplice_stage`
        called `predict()` once per variant, so the five Keras
        submodels ran one forward pass per variant per allele instead
        of one batched forward pass per module across the whole run,
        which is the dominant per-variant AI-inference cost the
        performance audit identified. Calling the existing batch
        method here, up front, is the only change: every eligible
        variant's result is computed identically (same models, same
        `_finalize()` derivation, see
        `tests/test_mmsplice_predictor.py::test_predict_batch_matches_predict_per_item`)
        and is stored in the same `MMSplicePredictionCache`
        `_run_mmsplice_stage` -> `MMSpliceService.predict()` already
        checks first -- so when the main per-variant loop reaches each
        variant, it gets the identical result as a cache hit instead
        of recomputing it.

        Never fatal: if this fails outright, `_run_mmsplice_stage`
        falls back to its original one-call-per-variant behavior
        exactly as before this optimization existed.
        """
        service = self._get_mmsplice_service()
        logger.info(
            f"MMSplice prefetch: batch-scoring up to {len(variants)} variant(s) via MMSpliceService.predict_batch()."
        )
        service.predict_batch(variants)

    def _run_ensemble_stage(self, variant: Variant, sequence_context, errors: List[str]) -> Dict[str, Any]:
        """
        Runs the Enformer + Borzoi splicing/regulatory AI ensemble
        (Objectives 3/4/7) via `self.ensemble_manager`, which in turn
        goes through `self.model_manager` -- so a missing/disabled
        plugin, a failed weight download, or an inference error is
        already handled by `ModelManager.predict()`'s own
        never-raise contract (see pipeline/models/manager.py) and by
        `EnsembleManager.evaluate()`'s own "0/1/2 models" handling
        (see pipeline/models/ensemble.py). This wrapper's own
        try/except is defense-in-depth, matching every other stage's
        shape in this file.

        Returns the same dict `EnsembleManager.evaluate()` always
        returns (never None), so downstream callers (ACMG PP3/BP4,
        the report builders, `_build_ai_model_status`) can inspect
        `models_used` uniformly instead of null-checking a second way.
        """
        if sequence_context is None:
            return {
                "models_used": [],
                "individual_scores": {},
                "consensus_score": None,
                "confidence": None,
                "agreement_percentage": None,
                "classification": None,
                "basis": "no_sequence_context",
                "reasoning": "No sequence context was available for this variant.",
            }
        try:
            with self._timer("model:ai_splicing_ensemble"):
                return self.ensemble_manager.evaluate(sequence_context.ref_sequence, sequence_context.alt_sequence)
        except Exception as exc:  # noqa: BLE001 - never let ensemble evaluation crash a variant
            errors.append(f"AI splicing ensemble (Enformer/Borzoi) failed: {exc}")
            logger.error(errors[-1])
            self._model_stage_errors.setdefault("ai_splicing_ensemble", str(exc)[:120])
            return {
                "models_used": [],
                "individual_scores": {},
                "consensus_score": None,
                "confidence": None,
                "agreement_percentage": None,
                "classification": None,
                "basis": "error",
                "reasoning": f"AI splicing ensemble raised an unexpected error: {exc}",
            }

    def _run_standalone_splice_plugin_stage(self, key: str, sequence_context, errors: List[str]) -> Dict[str, Any]:
        """
        Runs one standalone splice-prediction plugin -- SpliceFormer or
        SpliceBERT -- directly through `self.model_manager`, for the
        ACMG/AMP BP7 rule (`pipeline/acmg_rules.py::ACMGRuleEngine._bp7`,
        `spliceformer_result`/`splicebert_result` parameters). This is
        NOT the Enformer/Borzoi ensemble `_run_ensemble_stage` runs --
        see `pipeline/models/ensemble.py::EnsembleManager
        ._ENSEMBLE_MODEL_KEYS`, which deliberately excludes both of
        these -- so it calls `self.model_manager.predict(key, ...)`
        directly for a single plugin's own result, the same object
        `ModelManager.predict()` already returns for every other plugin
        key (`{"score", "classification", "confidence", "details", ...}`,
        see `pipeline/models/spliceformer_plugin.py`/`splicebert_plugin.py`
        `_infer_impl`).

        Availability is gated by `self._plugin_availability` (populated
        once at `__init__` from each plugin's own `is_available()`,
        which already reads `CONFIG.splicing.ENABLE_SPLICEFORMER`/
        `ENABLE_SPLICEBERT`) -- the same gate `_run_ensemble_stage`
        implicitly gets via `ModelManager`/`EnsembleManager`, made
        explicit here since this method calls a single plugin directly.
        A disabled/uninstalled model reports its own
        `unavailability_reason()` in `skip_reason` rather than silently
        producing an empty result (Objective 6: every AI model must
        report an explicit status -- see `pipeline/models/status.py`).
        `ModelManager.predict()` itself never raises (see its
        docstring); this method's own try/except is the same
        defense-in-depth every other stage helper in this file has.
        """
        if sequence_context is None:
            return {
                "available": False,
                "classification": None,
                "skip_reason": "No sequence context was available for this variant.",
            }

        if not self._plugin_availability.get(key, False):
            reason = self.model_registry.get(key).unavailability_reason()
            if key not in self._logged_missing_at_routing:
                self._logged_missing_at_routing.add(key)
                logger.warning(
                    f"{_MODEL_DISPLAY_NAMES.get(key, key)} is not available in this "
                    f"environment and will be skipped for every variant this run "
                    f"({reason}). This message will not repeat for subsequent variants."
                )
            return {"available": False, "classification": None, "skip_reason": reason}

        try:
            with self._timer(f"model:{key}"):
                result = self.model_manager.predict(key, sequence_context.ref_sequence, sequence_context.alt_sequence)
            if result is None:
                # Plugin is available but this specific call produced no
                # result (e.g. a this-variant-only inference failure --
                # already recorded in ModelManager.last_inference_errors()).
                return {
                    "available": True,
                    "classification": None,
                    "skip_reason": "No result produced for this variant.",
                }
            return result
        except Exception as exc:  # noqa: BLE001 - final defense-in-depth; ModelManager.predict should already catch everything
            errors.append(f"{_MODEL_DISPLAY_NAMES.get(key, key)} stage failed: {exc}")
            logger.error(errors[-1])
            self._model_stage_errors.setdefault(key, str(exc)[:120])
            # `available: True` already distinguished "ran (but failed)"
            # from "never eligible to run" (the `available: False`
            # branches above) -- `error` is an additional, explicit
            # signal for consistency with every other stage fixed
            # alongside this one (see `_run_rna_stage`'s comment); not
            # currently read by any consumer of spliceformer/splicebert
            # results (there is no raw_evidence/clinical-report
            # rendering path for these two plugins -- see BP7 in
            # `pipeline/acmg_rules.py`), added for the same schema-
            # correctness reason regardless.
            return {"available": True, "classification": None, "skip_reason": str(exc)[:300], "error": str(exc)[:300]}

    def _build_ai_model_status(
        self,
        dna_models_used: List[str],
        dna_model_results: Dict[str, Any],
        rna_result: Dict[str, Any],
        protein_result: Dict[str, Any],
        alphamissense_result: Dict[str, Any],
        mmsplice_result: Dict[str, Any],
        ensemble_result: Dict[str, Any],
        spliceformer_result: Dict[str, Any] = None,
        splicebert_result: Dict[str, Any] = None,
    ) -> Dict[str, Dict[str, str]]:
        """
        Objective 6: every AI model GEPER knows about must explicitly
        report one of Used / Skipped / Disabled / Failed for every
        variant -- never silently absent from the report. Delegates the
        actual bucketing to `pipeline.models.status.build_ai_model_status`
        (a pure function, independently unit-tested) so this method is
        just wiring this variant's own already-computed results into it.
        """
        return build_ai_model_status(
            dna_models_used=dna_models_used,
            dna_model_results=dna_model_results,
            rna_result=rna_result,
            protein_result=protein_result,
            alphamissense_result=alphamissense_result,
            mmsplice_result=mmsplice_result,
            ensemble_result=ensemble_result,
            spliceformer_result=spliceformer_result,
            splicebert_result=splicebert_result,
            model_availability=self._model_availability,
            plugin_availability=self._plugin_availability,
            model_stage_errors=self._model_stage_errors,
            plugin_failures={
                **self.model_manager.failed_keys(),
                **self.model_manager.last_inference_errors(),
            },
        )

    def _run_blast_stage(self, sequence_context, errors: List[str]) -> Dict[str, Any]:
        if sequence_context is None:
            return {"hits": [], "hit_count": 0, "skipped": True}
        try:
            with self._timer("blast"):
                return self.blast_client.search(sequence_context.alt_sequence)
        except ExternalAPIError as exc:
            errors.append(f"BLAST stage failed: {exc}")
            logger.error(errors[-1])
            # `skipped: True` stays True (no downstream reader gates on
            # it, but this keeps the shape consistent with the
            # "no sequence context" skip above); `error` is new -- see
            # `_run_rna_stage`'s comment for why. BLAST is one of
            # `RawEvidenceBundle`'s 11 required fields, and unlike
            # protein/alphamissense/mmsplice/RNA-FM it has no separate
            # "AI Model Status" table tracking it (BLAST is a database
            # lookup, not an AI model) -- this was the one stage with
            # NO existing mechanism anywhere in the report to tell a
            # clinician "BLAST failed" apart from "BLAST found nothing"
            # before this fix; see report/clinical_report_builder.py
            # and report/report_generator.py for where this now surfaces.
            return {"hits": [], "hit_count": 0, "skipped": True, "reason": str(exc), "error": str(exc)}

    def _run_dbsnp_stage(self, variant: Variant, errors: List[str]) -> Dict[str, Any]:
        # `sequence_context_gen.assembly` holds the build resolved by the
        # run()-time preflight (CLI-supplied or auto-detected from the
        # VCF header) -- the same build already used for Ensembl
        # reference-sequence lookups. dbSNP's positional search is
        # build-specific, so the stage must pass it through rather than
        # letting the client silently assume GRCh38 (see database/
        # dbsnp_client.py for why an unqualified query mismatches
        # GRCh37 coordinates almost every time).
        assembly = self.sequence_context_gen.assembly
        try:
            with self._timer("dbsnp"):
                return self.dbsnp_client.lookup_variant(variant, assembly=assembly)
        except ExternalAPIError as exc:
            errors.append(f"dbSNP stage failed: {exc}")
            logger.error(errors[-1])
            return {"rsid": None, "found": False, "error": str(exc)}

    def _run_clinvar_stage(self, variant: Variant, dbsnp_result: Dict[str, Any], errors: List[str]) -> Dict[str, Any]:
        rsid = dbsnp_result.get("rsid") if dbsnp_result else None
        assembly = self.sequence_context_gen.assembly
        try:
            with self._timer("clinvar"):
                return self.clinvar_client.query_variant(variant, rsid=rsid, assembly=assembly)
        except ExternalAPIError as exc:
            errors.append(f"ClinVar stage failed: {exc}")
            logger.error(errors[-1])
            return {"query": None, "found": False, "error": str(exc)}

    def _run_gnomad_stage(self, variant: Variant, errors: List[str]) -> Dict[str, Any]:
        """
        gnomAD population-frequency evidence (Phase 2). `GnomadLookup`
        already never raises (see its docstring) -- it reports any
        provider failure inside the returned dict's `error` field
        instead -- so this wrapper only needs to guard against a
        genuinely unexpected bug in the gnomAD module itself, exactly
        like every other stage helper's defense-in-depth `except
        Exception` (e.g. `_run_mmsplice_stage`), never against
        `GnomadLookup.query_variant` throwing under normal operation.
        """
        assembly = self.sequence_context_gen.assembly
        try:
            with self._timer("gnomad"):
                result = self.gnomad_client.query_variant(variant, assembly=assembly)
            if result.get("error"):
                errors.append(f"gnomAD stage: {result['error']}")
            return result
        except Exception as exc:  # noqa: BLE001 - final defense-in-depth
            errors.append(f"gnomAD stage failed: {exc}")
            logger.error(errors[-1])
            return {"found": False, "skipped": False, "error": str(exc)}

    def _run_indigenomes_stage(self, variant: Variant, errors: List[str]) -> Dict[str, Any]:
        """
        IndiGenomes population-frequency evidence (India-deployment
        feature -- see `annotation/indigenomes.py`'s module docstring).
        `IndiGenomesLookup` already never raises (it reports a
        genuinely unavailable/GRCh37-run/disabled state, or a query
        failure, inside the returned dict's `skipped`/`error` fields
        instead) -- this wrapper only guards against a genuinely
        unexpected bug in the module itself, exactly like every other
        stage helper's defense-in-depth `except Exception` (e.g.
        `_run_gnomad_stage`).
        """
        assembly = self.sequence_context_gen.assembly
        try:
            with self._timer("indigenomes"):
                result = self.indigenomes_client.query_variant(variant, assembly=assembly)
            if result.get("error"):
                errors.append(f"IndiGenomes stage: {result['error']}")
            return result
        except Exception as exc:  # noqa: BLE001 - final defense-in-depth
            errors.append(f"IndiGenomes stage failed: {exc}")
            logger.error(errors[-1])
            return {"found": False, "skipped": False, "error": str(exc)}

    def _run_conservation_stage(self, variant: Variant, errors: List[str]) -> Dict[str, Any]:
        """
        Evolutionary-conservation evidence (PhyloP now; PhastCons/GERP++
        follow the same pattern -- see pipeline/conservation/).
        `ConservationLookup` already never raises (see its docstring) --
        it reports any provider failure inside the returned dict's
        `error` field instead -- so this wrapper only needs to guard
        against a genuinely unexpected bug in the module itself, exactly
        like every other stage helper's defense-in-depth `except
        Exception` (e.g. `_run_gnomad_stage`).
        """
        assembly = self.sequence_context_gen.assembly
        try:
            with self._timer("conservation"):
                result = self.conservation_client.query_variant(variant, assembly=assembly)
            if result.get("error"):
                errors.append(f"Conservation stage: {result['error']}")
            return result
        except Exception as exc:  # noqa: BLE001 - final defense-in-depth
            errors.append(f"Conservation stage failed: {exc}")
            logger.error(errors[-1])
            return {"found": False, "skipped": False, "error": str(exc)}

    def _run_clingen_stage(self, variant: Variant, errors: List[str]) -> Dict[str, Any]:
        """
        ClinGen gene-disease validity / dosage sensitivity / actionability
        evidence. `ClinGenLookup` already never raises (see its
        docstring) -- it reports any provider failure inside the
        returned dict's `error` field instead -- so this wrapper only
        needs to guard against a genuinely unexpected bug in the
        ClinGen module itself, exactly like every other stage helper's
        defense-in-depth `except Exception` (e.g. `_run_gnomad_stage`).
        """
        assembly = self.sequence_context_gen.assembly
        try:
            with self._timer("clingen"):
                result = self.clingen_client.query_variant(variant, assembly=assembly)
            if result.get("error"):
                errors.append(f"ClinGen stage: {result['error']}")
            return result
        except Exception as exc:  # noqa: BLE001 - final defense-in-depth
            errors.append(f"ClinGen stage failed: {exc}")
            logger.error(errors[-1])
            return {"found": False, "skipped": False, "error": str(exc)}

    def _run_hpo_stage(self, clingen_result: Dict[str, Any], errors: List[str]) -> Dict[str, Any]:
        """
        HPO (Human Phenotype Ontology) gene-phenotype annotation for
        this variant's gene -- annotation/report context, and PP4's
        gene-side input (see `pipeline/acmg_rules.py::ACMGRuleEngine._pp4`).
        Reuses the gene symbol the ClinGen stage already resolved
        (`clingen_result['gene_symbol']`) rather than a second Ensembl
        overlap lookup, the same reuse `_run_uniprot_stage`/
        `_run_transcript_stage` already do. `HPOLookup` never raises
        (see its docstring) -- this wrapper only guards against a
        genuinely unexpected bug in the module itself, matching every
        other stage helper's defense-in-depth `except Exception`.
        """
        gene_symbol = (clingen_result or {}).get("gene_symbol")
        try:
            with self._timer("hpo"):
                result = self.hpo_client.query_variant(gene_symbol)
            if result.get("error"):
                errors.append(f"HPO stage: {result['error']}")
            return self._with_gene_resolution_context(result, clingen_result)
        except Exception as exc:  # noqa: BLE001 - final defense-in-depth
            errors.append(f"HPO stage failed: {exc}")
            logger.error(errors[-1])
            return {"found": False, "skipped": False, "error": str(exc)}

    @staticmethod
    def _with_gene_resolution_context(result: Dict[str, Any], clingen_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        HPO/Orphanet/UniProt all reuse the gene symbol ClinGen's stage
        already resolved rather than re-resolving it themselves (see
        each stage helper's own docstring). When that resolution
        genuinely came back `AMBIGUOUS` (see
        `pipeline/clingen/utils.py::GeneResolutionStatus` -- multiple
        protein-coding genes overlap and could not be disambiguated,
        as opposed to no gene existing here at all), this replaces the
        downstream stage's generic "no gene resolved" reason with the
        specific ambiguous-candidates explanation, so a reviewer reading
        the HPO/Orphanet/UniProt section of the report sees *why*
        gene-level evidence is missing rather than a bare negative.
        Never touches `result` when a gene symbol was actually
        resolved.
        """
        if result.get("gene_symbol"):
            return result
        status = (clingen_result or {}).get("gene_resolution_status")
        if status != "ambiguous":
            return result
        result = dict(result)
        result["reason"] = (
            "gene resolution ambiguous -- ClinGen's own gene-overlap resolution could not "
            f"disambiguate this position: {(clingen_result or {}).get('reason', '')}"
        )
        result["gene_resolution_status"] = status
        result["gene_resolution_candidates"] = (clingen_result or {}).get("gene_resolution_candidates", [])
        return result

    def _run_normalization_stage(self, variant: Variant, errors: List[str]) -> Dict[str, Any]:
        """
        Variant normalization + HGVS.g generation (see
        `pipeline/variant_normalization.py`, `pipeline/hgvs_utils.py`).
        Runs first in `_process_variant`, before this variant's
        coordinates are used anywhere else. Never raises: a normalization
        bug must degrade to "use the variant as parsed" rather than
        abort the run, same policy as every other stage helper.
        """
        if not CONFIG.normalization.ENABLED:
            return {
                "skipped": True,
                "reason": "Variant normalization disabled via GEPER_ENABLE_VARIANT_NORMALIZATION=false",
            }

        fetch_base = None
        if CONFIG.normalization.LEFT_ALIGN_ENABLED:

            def fetch_base(chrom: str, pos: int) -> str:  # noqa: F811 - intentional shadow, this is the callback
                return self.sequence_context_gen.fetch_reference_sequence(chrom, pos, pos)

        try:
            with self._timer("normalization"):
                normalized = normalize_variant(
                    variant.chrom,
                    variant.pos,
                    variant.ref,
                    variant.alt,
                    fetch_base=fetch_base,
                    max_shift_bp=CONFIG.normalization.LEFT_ALIGN_MAX_SHIFT_BP,
                )
            hgvs_g = to_hgvs_g(
                normalized.chrom,
                normalized.pos,
                normalized.ref,
                normalized.alt,
                assembly=self.sequence_context_gen.assembly or "GRCh38",
            )
            return {"skipped": False, "normalized": normalized.to_dict(), "hgvs_g": hgvs_g, "hgvs_c": None}
        except Exception as exc:  # noqa: BLE001 - final defense-in-depth
            errors.append(f"Variant normalization stage failed: {exc}")
            logger.error(errors[-1])
            return {"skipped": False, "normalized": None, "hgvs_g": None, "hgvs_c": None, "error": str(exc)}

    def _attach_hgvs_c(self, normalization_result: Dict[str, Any], transcript_result: Dict[str, Any]) -> None:
        """
        Fills in `normalization_result["hgvs_c"]` once the transcript
        stage has resolved (`_run_transcript_stage`, later in
        `_process_variant` than normalization itself needs to run --
        HGVS.c generation needs real exon/CDS structure that isn't
        available yet at normalization time). Mutates `normalization_result`
        in place; a no-op if normalization was skipped/errored or no
        transcript structure resolved for this variant.
        """
        if not normalization_result.get("normalized"):
            return
        transcript_context = transcript_from_result(transcript_result)
        if transcript_context is None:
            return
        n = normalization_result["normalized"]
        try:
            normalization_result["hgvs_c"] = to_hgvs_c(transcript_context, n["pos"], n["ref"], n["alt"])
        except Exception as exc:  # noqa: BLE001 - additive annotation, must never break the run
            logger.warning(f"HGVS.c generation failed for {n['chrom']}:{n['pos']}: {exc}")

    def _run_orphanet_stage(self, clingen_result: Dict[str, Any], errors: List[str]) -> Dict[str, Any]:
        """
        Orphanet rare-disease gene-disorder annotation for this
        variant's gene -- annotation/report context alongside (not in
        place of) ClinGen's gene-disease clinical validity
        classification (see `pipeline/acmg_rules.py::ACMGRuleEngine._pp1`/
        `_bs4`'s docstrings for why Orphanet's own coarse
        "Assessed"/"Not yet assessed" association status cannot
        substitute for ClinGen's graded validity scale as those
        criteria's prerequisite check). Reuses the gene symbol the
        ClinGen stage already resolved, same reuse `_run_hpo_stage`
        does. `OrphanetLookup` never raises -- this wrapper only
        guards against a genuinely unexpected bug in the module
        itself, matching every other stage helper's defense-in-depth
        `except Exception`.
        """
        gene_symbol = (clingen_result or {}).get("gene_symbol")
        try:
            with self._timer("orphanet"):
                result = self.orphanet_client.query_variant(gene_symbol)
            if result.get("error"):
                errors.append(f"Orphanet stage: {result['error']}")
            return self._with_gene_resolution_context(result, clingen_result)
        except Exception as exc:  # noqa: BLE001 - final defense-in-depth
            errors.append(f"Orphanet stage failed: {exc}")
            logger.error(errors[-1])
            return {"found": False, "skipped": False, "error": str(exc)}

    def _run_transcript_stage(
        self, variant: Variant, clingen_result: Dict[str, Any], errors: List[str]
    ) -> Dict[str, Any]:
        """
        Canonical transcript structure (exon coordinates, CDS bounds) for
        this variant's gene, used by the ACMG/AMP PVS1 rule to answer its
        location caveats: which exon the variant is in, whether it is in
        the last exon or the last 50 bp of the penultimate one, how much
        of the protein it removes, and whether skipping the affected exon
        preserves the reading frame.

        Reuses the gene symbol the ClinGen stage already resolved rather
        than issuing a second Ensembl gene-overlap call. `TranscriptLookup`
        already never raises (see its docstring) -- it reports provider
        failures inside the returned dict -- so this wrapper only guards
        against an unexpected bug in the module itself, like every other
        stage helper here.
        """
        assembly = self.sequence_context_gen.assembly
        gene_symbol = (clingen_result or {}).get("gene_symbol")
        try:
            with self._timer("transcript_structure"):
                result = self.transcript_client.query_variant(variant, assembly=assembly, gene_symbol=gene_symbol)
            if result.get("error"):
                errors.append(f"Transcript-structure stage: {result['error']}")
            return self._with_gene_resolution_context(result, clingen_result)
        except Exception as exc:  # noqa: BLE001 - final defense-in-depth
            errors.append(f"Transcript-structure stage failed: {exc}")
            logger.error(errors[-1])
            return {"found": False, "skipped": False, "error": str(exc), "transcript": None}

    def _run_clinvar_codon_stage(
        self, variant: Variant, transcript_result: Dict[str, Any], errors: List[str]
    ) -> Dict[str, Any]:
        """
        Every ClinVar record with a missense protein change at this
        variant's own codon -- the shared evidence source for the
        ACMG/AMP PS1 (same amino acid change as an established
        pathogenic variant) and PM5 (different amino acid change at an
        established pathogenic codon) rules.

        Reuses the transcript structure `_run_transcript_stage` already
        fetched to compute the codon number and its genomic span, so
        this stage costs only the ClinVar E-utilities round trip(s),
        never a second transcript lookup. `ClinVarCodonLookup` already
        never raises -- it reports provider failures inside the
        returned dict -- so this wrapper only guards against an
        unexpected bug in the module itself, like every other stage
        helper here.
        """
        assembly = self.sequence_context_gen.assembly
        transcript = transcript_from_result(transcript_result)
        if transcript is None:
            return {
                "skipped": False,
                "found": False,
                "matches": [],
                "reason": "no transcript structure was available to determine this variant's codon.",
            }
        codon_number = transcript.codon_at(variant.pos)
        if codon_number is None:
            return {
                "skipped": False,
                "found": False,
                "matches": [],
                "reason": "variant position does not fall within a coding codon of this transcript.",
            }
        try:
            with self._timer("clinvar_codon"):
                result = self.clinvar_codon_client.query_codon(transcript, codon_number, assembly=assembly)
            if result.get("error"):
                errors.append(f"PS1/PM5 ClinVar codon stage: {result['error']}")
            return result
        except Exception as exc:  # noqa: BLE001 - final defense-in-depth
            errors.append(f"PS1/PM5 ClinVar codon stage failed: {exc}")
            logger.error(errors[-1])
            return {"found": False, "skipped": False, "error": str(exc), "matches": []}

    def _run_functional_evidence_stage(
        self,
        variant: Variant,
        clingen_result: Dict[str, Any],
        transcript_result: Dict[str, Any],
        errors: List[str],
    ) -> Dict[str, Any]:
        """
        PS3/BS3 functional-assay evidence for this exact variant (see
        `pipeline/functional_evidence/`) -- ClinGen Evidence Repository
        primary, MaveDB secondary. Unlike the gene-level stages above,
        this is variant-level: it needs this variant's own genomic
        (g.) HGVS string, to match against ClinGen ERepo's curated
        variants, and coding (c.) HGVS string, to match against
        MaveDB's transcript-relative variant scores -- both generated
        here via `pipeline/hgvs_utils.py`, reusing the gene symbol the
        ClinGen stage already resolved and the transcript structure
        the transcript stage already fetched, rather than re-deriving
        either. `FunctionalEvidenceLookup` already never raises (see
        its docstring) -- this wrapper only guards against a genuinely
        unexpected bug in the module itself, matching every other
        stage helper here.
        """
        gene_symbol = (clingen_result or {}).get("gene_symbol")
        assembly = self.sequence_context_gen.assembly or "GRCh38"
        hgvs_g = to_hgvs_g(variant.chrom, variant.pos, variant.ref, variant.alt, assembly=assembly)
        transcript_context = transcript_from_result(transcript_result)
        hgvs_c = (
            to_hgvs_c(transcript_context, variant.pos, variant.ref, variant.alt)
            if transcript_context is not None
            else None
        )
        try:
            with self._timer("functional_evidence"):
                result = self.functional_evidence_client.query_variant(
                    gene_symbol=gene_symbol,
                    hgvs_g=hgvs_g,
                    hgvs_c=hgvs_c,
                )
            if result.get("error"):
                errors.append(f"Functional-evidence (PS3/BS3) stage: {result['error']}")
            return result
        except Exception as exc:  # noqa: BLE001 - final defense-in-depth
            errors.append(f"Functional-evidence (PS3/BS3) stage failed: {exc}")
            logger.error(errors[-1])
            return {"found": False, "skipped": False, "error": str(exc), "records": []}

    # ------------------------------------------------------------------
    # Biological evidence layer: UniProt -> InterPro/Pfam -> AlphaFold DB
    # ------------------------------------------------------------------
    def _run_uniprot_stage(self, variant: Variant, clingen_result: Dict[str, Any], errors: List[str]) -> Dict[str, Any]:
        """
        UniProt reviewed-protein annotation (function, disease
        relevance, sequence features) for this variant's gene. Runs
        first in the biological-evidence layer since InterPro/AlphaFold
        below are both keyed by the UniProt accession this stage
        resolves. Reuses the gene symbol ClinGen's stage already
        resolved for this variant (`clingen_result['gene_symbol']`)
        rather than repeating the Ensembl overlap lookup a second time.
        `UniProtLookup` never raises (see its docstring) -- this
        wrapper only guards against a genuinely unexpected bug in the
        module itself, matching every other stage helper's
        defense-in-depth `except Exception`.
        """
        assembly = self.sequence_context_gen.assembly
        gene_hint = (clingen_result or {}).get("gene_symbol")
        try:
            with self._timer("uniprot"):
                result = self.uniprot_client.query_variant(variant, assembly=assembly, gene_symbol_hint=gene_hint)
            if result.get("error"):
                errors.append(f"UniProt stage: {result['error']}")
            return self._with_gene_resolution_context(result, clingen_result)
        except Exception as exc:  # noqa: BLE001 - final defense-in-depth
            errors.append(f"UniProt stage failed: {exc}")
            logger.error(errors[-1])
            return {"found": False, "skipped": False, "error": str(exc)}

    def _run_interpro_stage(
        self, uniprot_result: Dict[str, Any], protein_position: Optional[int], errors: List[str]
    ) -> Dict[str, Any]:
        """
        InterPro/Pfam conserved-domain/family/motif annotation for the
        protein UniProt resolved above, plus (when a protein position
        estimate is available) which of those domains it overlaps.
        `InterProLookup` never raises -- this wrapper only guards
        against a genuinely unexpected bug in the module itself.
        """
        try:
            with self._timer("interpro"):
                result = self.interpro_client.query_variant(uniprot_result, protein_position=protein_position)
            if result.get("error"):
                errors.append(f"InterPro stage: {result['error']}")
            return result
        except Exception as exc:  # noqa: BLE001 - final defense-in-depth
            errors.append(f"InterPro stage failed: {exc}")
            logger.error(errors[-1])
            return {"found": False, "skipped": False, "error": str(exc)}

    def _run_alphafold_stage(
        self, uniprot_result: Dict[str, Any], protein_position: Optional[int], errors: List[str]
    ) -> Dict[str, Any]:
        """
        AlphaFold DB structural reference (model URL/version, mean
        pLDDT, and -- when a protein position estimate is available --
        confidence at that specific residue) for the protein UniProt
        resolved above. `AlphaFoldLookup` never raises -- this wrapper
        only guards against a genuinely unexpected bug in the module
        itself.
        """
        try:
            with self._timer("alphafold"):
                result = self.alphafold_client.query_variant(uniprot_result, protein_position=protein_position)
            if result.get("error"):
                errors.append(f"AlphaFold stage: {result['error']}")
            return result
        except Exception as exc:  # noqa: BLE001 - final defense-in-depth
            errors.append(f"AlphaFold stage failed: {exc}")
            logger.error(errors[-1])
            return {"found": False, "skipped": False, "error": str(exc)}
