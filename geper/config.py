"""
Central configuration for GEPER.

Keeping model identifiers, API endpoints, and routing thresholds in one
place means adding a new model or changing an endpoint never requires
touching pipeline logic -- only this file.
"""

import os
from dataclasses import dataclass, field
from typing import Dict, Tuple


def _load_yaml_config_overrides() -> None:
    """
    Optional `config.yaml` overlay, applied as `os.environ` entries
    *before* any dataclass below is defined -- every field in this
    module reads its default via `os.environ.get(...)` evaluated once
    at class-definition time (module import), so this must run first,
    at the very top of the file, or a yaml value would silently arrive
    too late to affect anything.

    This is deliberately a thin convenience layer over the existing
    env-var configuration surface, not a second, divergent mechanism:
    every dotted key below maps to the exact same `GEPER_*` variable
    the corresponding field already reads, so `config.yaml` and
    exported environment variables configure the identical set of
    knobs. An explicit environment variable a user actually exported
    always wins over `config.yaml` (a checked-in yaml file should
    never silently override something the caller set on purpose) --
    this only fills in variables that are not already present in
    `os.environ`.

    Path: `GEPER_CONFIG_FILE` env var, default `./config.yaml`. Missing
    file -> silent no-op (config.yaml is optional; env vars / defaults
    still work exactly as before). Present but PyYAML isn't installed,
    or the file fails to parse -> a clear warning, then proceeds with
    defaults/env vars only -- a malformed or unloadable config.yaml
    must never prevent the pipeline from starting.
    """
    yaml_path = os.environ.get("GEPER_CONFIG_FILE", "./config.yaml")
    if not os.path.exists(yaml_path):
        return

    try:
        import yaml
    except ImportError:
        from utils.logger import get_logger

        get_logger(__name__).warning(
            f"Found '{yaml_path}' but PyYAML is not installed; skipping "
            "config.yaml overrides. Install with `pip install pyyaml` to "
            "use config.yaml, or configure GEPER via environment "
            "variables instead."
        )
        return

    try:
        with open(yaml_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as exc:
        from utils.logger import get_logger

        get_logger(__name__).warning(f"Could not parse '{yaml_path}' ({exc}); ignoring it.")
        return

    if not isinstance(data, dict):
        from utils.logger import get_logger

        get_logger(__name__).warning(f"'{yaml_path}' does not contain a top-level mapping; ignoring it.")
        return

    # Dotted config.yaml key -> the environment variable name the
    # corresponding dataclass field already reads. Add a new entry
    # here whenever a new env-var-backed field below should also be
    # settable from config.yaml.
    env_var_map = {
        "output_dir": "GEPER_OUTPUT_DIR",
        "cache_dir": "GEPER_CACHE_DIR",
        "checkpoint_interval": "GEPER_CHECKPOINT_INTERVAL",
        "ai_only": "GEPER_AI_ONLY",
        "enable_profiling": "GEPER_ENABLE_PROFILING",
        "blast.disk_cache": "GEPER_BLAST_DISK_CACHE",
        "blast.mode": "GEPER_BLAST_MODE",
        "blast.database": "GEPER_BLAST_DATABASE",
        "blast.local_db_path": "GEPER_BLAST_LOCAL_DB",
        "blast.reference_fasta": "GEPER_BLAST_REFERENCE_FASTA",
        "blast.enable_prefetch": "GEPER_BLAST_ENABLE_PREFETCH",
        "blast.max_concurrent_remote": "GEPER_BLAST_MAX_CONCURRENT_REMOTE",
        "blast.max_concurrent_local": "GEPER_BLAST_MAX_CONCURRENT_LOCAL",
        "alphamissense.enabled": "GEPER_ENABLE_ALPHAMISSENSE",
        "mmsplice.enabled": "GEPER_ENABLE_MMSPLICE",
        "mmsplice.device": "GEPER_MMSPLICE_DEVICE",
        "mmsplice.intron_window": "GEPER_MMSPLICE_INTRON_WINDOW",
        "mmsplice.exon_near_splice_window": "GEPER_MMSPLICE_EXON_WINDOW",
        "mmsplice.enable_prefetch": "GEPER_MMSPLICE_ENABLE_PREFETCH",
        "clingen.enabled": "GEPER_ENABLE_CLINGEN",
        "clingen.gene_validity_local_file": "GEPER_CLINGEN_GENE_VALIDITY_FILE",
        "clingen.dosage_sensitivity_local_file": "GEPER_CLINGEN_DOSAGE_FILE",
        "clingen.api_enabled": "GEPER_CLINGEN_API_ENABLED",
        "clingen.offline_mode": "GEPER_CLINGEN_OFFLINE",
        "uniprot.enabled": "GEPER_ENABLE_UNIPROT",
        "uniprot.offline_mode": "GEPER_UNIPROT_OFFLINE",
        "uniprot.local_dataset_file": "GEPER_UNIPROT_LOCAL_FILE",
        "interpro.enabled": "GEPER_ENABLE_INTERPRO",
        "interpro.offline_mode": "GEPER_INTERPRO_OFFLINE",
        "interpro.local_dataset_file": "GEPER_INTERPRO_LOCAL_FILE",
        "alphafold.enabled": "GEPER_ENABLE_ALPHAFOLD",
        "alphafold.offline_mode": "GEPER_ALPHAFOLD_OFFLINE",
        "alphafold.fetch_structure_file": "GEPER_ALPHAFOLD_FETCH_STRUCTURE",
        "alphafold.local_dataset_file": "GEPER_ALPHAFOLD_LOCAL_FILE",
    }

    def _flatten(prefix: str, node, out: Dict[str, object]) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                _flatten(f"{prefix}.{key}" if prefix else str(key), value, out)
        else:
            out[prefix] = node

    flat: Dict[str, object] = {}
    _flatten("", data, flat)

    applied = []
    for dotted_key, value in flat.items():
        env_var = env_var_map.get(dotted_key)
        if env_var is None or env_var in os.environ:
            continue  # unknown key, or an explicit env var already wins
        os.environ[env_var] = str(value)
        applied.append(env_var)

    if applied:
        from utils.logger import get_logger

        get_logger(__name__).info(f"Loaded {len(applied)} setting(s) from '{yaml_path}': {sorted(applied)}.")


_load_yaml_config_overrides()


@dataclass(frozen=True)
class ModelConfig:
    """HuggingFace model identifiers for each pretrained model."""

    # Official ml4bio/RNA-FM (MIT) pretrained-loader function name, called
    # as `getattr(fm.pretrained, RNA_FM)()` -- see models/rna_fm.py.
    RNA_FM: str = "rna_fm_t12"
    ESM2: str = "facebook/esm2_t33_650M_UR50D"

    # HyenaDNA is loaded from a local/downloaded checkpoint directory
    # rather than the HF hub in the reference snippet supplied.
    #
    # Redirected under GEPER_CACHE_DIR (F2, report review round 4) the
    # same way the bootstrapped datasets (ClinGen/HPO/Orphanet/UniProt/
    # Ensembl/AlphaMissense) already are -- but only when GEPER_CACHE_DIR
    # is explicitly set: an explicit GEPER_HYENADNA_CKPT_DIR always wins
    # (unchanged), and when NEITHER is set this stays the original
    # "./checkpoints" default so a local user with no Drive/persistent
    # cache configured sees no behavior change at all (a checkpoint
    # already downloaded there under the old default is still found).
    HYENADNA_CHECKPOINT_DIR: str = os.environ.get(
        "GEPER_HYENADNA_CKPT_DIR",
        os.path.join(os.environ["GEPER_CACHE_DIR"], "hyenadna")
        if os.environ.get("GEPER_CACHE_DIR")
        else "./checkpoints",
    )
    HYENADNA_MODEL_NAME: str = "hyenadna-medium-450k-seqlen"
    HYENADNA_MAX_LENGTH: int = 450_000

    # --- Safety ceilings used when a tokenizer doesn't expose a sane
    # `model_max_length` (HF's sentinel for "unset" is ~1e30, which is
    # not usable as a real bound). These are last-resort fallbacks, not
    # primary limits -- each model prefers whatever its own tokenizer
    # actually reports first (see BaseGenomicModel._resolve_safe_max_length).
    RNA_FM_MAX_SAFE_TOKENS: int = 1024
    ESM2_MAX_SAFE_TOKENS: int = 1024

    # Evo 2, loaded via the official `evo2` pip package (see
    # models/evo2.py). Which published checkpoint variant to load.
    #
    # IMPORTANT -- corrected against Arc Institute's current
    # (2026) docs and their GitHub issue tracker, not assumed from an
    # older model card: the plain 'evo2_7b' checkpoint (their
    # long-context, 1M-token-context release) ships with
    # `use_fp8_input_projections=True` baked into its config, so it
    # still requires Transformer Engine + FP8-capable hardware even on
    # the "light install" path that's documented as TE-free -- see
    # https://github.com/ArcInstitute/evo2/issues/208 (open as of
    # 2026-03-13: a user hit exactly
    # "This model requires FP8 input projections ... but TE is not
    # installed" while loading plain 'evo2_7b'). The checkpoint that
    # is genuinely usable with the light (no-TE) install is
    # 'evo2_7b_base' -- the 7B model pretrained at 8K context, listed
    # as a separate entry in Arc Institute's own model table and not
    # implicated in that issue. GEPER defaults to it for exactly that
    # reason: it's the one confirmed to actually work under the
    # documented light install (torch + flash-attn only, no conda
    # transformer-engine-torch package). Larger/longer-context variants
    # (evo2_7b_262k, the 1M-context 'evo2_7b', evo2_40b_base, etc.) can
    # still be selected via GEPER_EVO2_VARIANT on hardware where
    # Transformer Engine is installed and FP8 is supported (Hopper+).
    EVO2_VARIANT: str = os.environ.get("GEPER_EVO2_VARIANT", "evo2_7b_base")

    # Hardware floor, independent of which variant is selected above:
    # `evo2` depends directly on FlashAttention-2 for its attention
    # kernels, and FlashAttention-2's official CUDA backend requires
    # compute capability >= 8.0 (Ampere/Ada/Hopper) -- verified against
    # Dao-AILab/flash-attention's current README/PyPI page. Turing
    # GPUs, including the Tesla T4 (compute capability 7.5), are not
    # covered by the official package (only by a separate, unofficial,
    # partial-feature project Arc Institute does not use or support
    # for evo2). No variant choice or dependency pin changes this --
    # see models/evo2.py's `_unsupported_gpu_reason` for the runtime
    # check this is enforced with, and README.md section 12 for the
    # full writeup. This is not configurable via an env var because
    # it isn't a preference to override; it's a hard capability floor.

    # Right-sized to 'evo2_7b_base's real 8K (8192-token) pretraining
    # context -- unlike the 1M-context variants, this checkpoint does
    # have a genuine, model-imposed context limit, not just a GEPER-side
    # safety margin. Selecting a longer-context GEPER_EVO2_VARIANT via
    # the env var above should be paired with raising this ceiling to
    # match that variant's own published context window (e.g. 262_144
    # for evo2_7b_262k, 1_000_000 for the long-context 'evo2_7b') --
    # GEPER does not infer this automatically from EVO2_VARIANT, so the
    # two must be kept in sync by hand if you change the variant.
    EVO2_MAX_SAFE_TOKENS: int = int(os.environ.get("GEPER_EVO2_MAX_SAFE_TOKENS", "8192"))

    # Arc Institute's own guidance: intermediate-layer embeddings
    # outperform final-layer embeddings for downstream tasks (see the
    # `evo2` package README). This is the exact layer name used in
    # their own published embeddings example for the 7B model.
    EVO2_EMBEDDING_LAYER: str = "blocks.28.mlp.l3"


@dataclass(frozen=True)
class RoutingConfig:
    """
    Thresholds used by SequenceRouter to decide which DNA foundation
    model handles a given variant's sequence context.

    Rationale:
      - HyenaDNA is the default, general-purpose choice for typical
        SNV/indel flanking windows (DNABERT-2, its former fast-path
        default, has been removed from GEPER entirely -- see
        LICENSE_AUDIT.md / the AI orchestration audit for why). Its
        long-convolution architecture has no fixed context ceiling the
        way a transformer-attention model does, so it is a safe
        universal default for both short and long windows.
      - HyenaDNA is additionally reserved for long-range context (see
        HYENADNA_MIN_LEN) where the required context genuinely exceeds
        what a transformer-attention model can efficiently handle.
      - Evo 2 (7B, StripedHyena-2) is reserved for variants that need
        cross-species evolutionary context or fall in structurally
        complex / highly conserved regions, since its pretraining
        (across all domains of life) gives it stronger representations
        there at a much higher compute cost.
    """

    # At or above this length -> HyenaDNA is required (long-range
    # architecture). Below this length, HyenaDNA is still used as the
    # universal default (see rationale above) unless Evo 2's complex-
    # context rule fires instead.
    HYENADNA_MIN_LEN: int = 10_000

    # Variant classes that always warrant Evo 2, regardless of
    # length, because of their evolutionary/structural complexity.
    COMPLEX_CONTEXT_VARIANT_TYPES: tuple = (
        "structural",
        "repeat_region",
        "splice_region",
        "multi_species_conserved",
    )

    # Default flanking window (bases on each side of the variant) used
    # to build the DNA sequence context when the caller does not
    # request a specific length.
    DEFAULT_FLANK_SIZE: int = 500

    # Flank size used when a long-range HyenaDNA context is warranted.
    LONG_RANGE_FLANK_SIZE: int = 15_000


@dataclass(frozen=True)
class AlphaMissenseConfig:
    """
    Configuration for the AlphaMissense missense-pathogenicity stage.

    IMPORTANT -- how this is actually implemented, and why:
    Google DeepMind has explicitly NOT released trained AlphaMissense
    model weights ("What we don't provide: The trained AlphaMissense
    model weights" -- github.com/google-deepmind/alphamissense). What
    IS published is a precomputed, genome-coordinate-keyed catalogue of
    scores for every possible human missense substitution (~71M rows
    for hg19/hg38 canonical transcripts), distributed as a bgzip'd,
    tabix-indexed TSV. This is also exactly how every other real
    pipeline integrates AlphaMissense in practice (the official Ensembl
    VEP plugin queries the same tabix-indexed file). GEPER therefore
    integrates AlphaMissense as an indexed lookup against that
    catalogue -- via the `tabix` CLI, matched by (chrom, pos, ref, alt)
    -- rather than as a loaded neural network. See models/alphamissense.py.

    LICENSING -- read before commercial deployment:
    The AlphaMissense *code* is Apache-2.0. The *predictions catalogue*
    itself has been distributed under different terms at different
    times/venues: the current official repository (github.com/
    google-deepmind/alphamissense, archived 2025-05-16) states the
    predictions are licensed CC BY 4.0 (attribution only, commercial
    use permitted). However, the Google Cloud Storage download page,
    the Ensembl VEP plugin docs, the EBI announcement, and the
    HuggingFace dataset mirror all instead describe the predictions as
    "CC BY-NC-SA 4.0 -- non-commercial research use only", and note
    that use of the GCS-hosted files is additionally subject to the
    Google Cloud Platform Terms of Service. These sources disagree and
    this is a genuine, unresolved discrepancy -- GEPER does not know
    which currently governs the specific file your deployment
    downloads. Before relying on AlphaMissense output in a commercial
    product, confirm the license terms that apply to the exact file
    you download directly with Google DeepMind (alphamissense@google.com)
    or counsel; do not assume CC BY 4.0 from this comment alone.
    """

    ENABLED: bool = os.environ.get("GEPER_ENABLE_ALPHAMISSENSE", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    # Official precomputed-score catalogue (Google Cloud Storage, public
    # bucket listed at console.cloud.google.com/storage/browser/dm_alphamissense).
    # These files are bgzip'd, but -- unlike e.g. gnomAD's GCS-hosted VCFs --
    # Google does NOT publish a sibling `.tbi` index alongside them, so
    # `tabix` cannot do indexed HTTP range queries directly against the
    # URL (every third-party integration of this catalogue -- the
    # Ensembl VEP plugin docs, Biostars threads, etc. -- downloads the
    # file and runs `tabix -s 1 -b 2 -e 2 -f -S 1` locally first). GEPER
    # therefore downloads + locally tabix-indexes each build exactly
    # once (cached under `CACHE_SUBDIR` below, alongside the HuggingFace
    # model cache), the same "download once, cache, reuse" shape used
    # for HyenaDNA's checkpoint -- see models/alphamissense.py.
    HG38_URL: str = os.environ.get(
        "GEPER_ALPHAMISSENSE_HG38_URL",
        "https://storage.googleapis.com/dm_alphamissense/AlphaMissense_hg38.tsv.gz",
    )
    HG19_URL: str = os.environ.get(
        "GEPER_ALPHAMISSENSE_HG19_URL",
        "https://storage.googleapis.com/dm_alphamissense/AlphaMissense_hg19.tsv.gz",
    )

    # Where the auto-downloaded + locally-indexed copies are cached.
    # Defaults to a subdirectory of the main HuggingFace model cache
    # (GEPER_CACHE_DIR) so both survive a Colab disconnect together when
    # GEPER_CACHE_DIR is pointed at mounted Drive storage.
    CACHE_SUBDIR: str = os.environ.get("GEPER_ALPHAMISSENSE_CACHE_SUBDIR", "alphamissense")

    # Each build is a ~9GB download; this is therefore opt-out rather
    # than silent, but defaults to on since AlphaMissense is otherwise
    # unusable out of the box (there's no remote-index alternative).
    # Set to false to require an explicit LOCAL_HG38_PATH/LOCAL_HG19_PATH
    # instead (e.g. air-gapped deployments with an out-of-band copy).
    AUTO_DOWNLOAD: bool = os.environ.get("GEPER_ALPHAMISSENSE_AUTO_DOWNLOAD", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    # Optional local, pre-downloaded + tabix-indexed copies (path to the
    # .tsv.gz; a sibling .tsv.gz.tbi must exist alongside it). When set,
    # these take priority over auto-download -- for air-gapped / fully
    # offline deployments, or to avoid a redundant download when a copy
    # already exists elsewhere on disk. Populated once, out of band, and
    # thereafter GEPER never touches the network for AlphaMissense at all.
    LOCAL_HG38_PATH: str = os.environ.get("GEPER_ALPHAMISSENSE_HG38_LOCAL", "")
    LOCAL_HG19_PATH: str = os.environ.get("GEPER_ALPHAMISSENSE_HG19_LOCAL", "")

    # `tabix` (htslib) is a small, common bioinformatics CLI tool, not a
    # pip package. GEPER auto-installs it via `apt-get` the first time
    # it's needed (see models/alphamissense.py::ensure_tabix_available,
    # utils/auto_install.py::ensure_system_binary_available) -- the same
    # "no manual setup step" treatment every other optional dependency
    # gets (HyenaDNA's git checkout, RNA-FM/BLAST's pip packages). On a
    # non-Debian system (no apt-get), or if GEPER_TABIX_BINARY is set to
    # a custom name/path, auto-install is skipped and AlphaMissense is
    # treated exactly like a missing optional dependency (e.g. HyenaDNA
    # without git) -- skipped with one clear warning, never a hard
    # failure. Install manually with `apt-get install tabix` / `conda
    # install -c bioconda htslib` in that case.
    TABIX_BINARY: str = os.environ.get("GEPER_TABIX_BINARY", "tabix")

    QUERY_TIMEOUT_SECS: int = int(os.environ.get("GEPER_ALPHAMISSENSE_TIMEOUT", "30"))

    # Official am_class thresholds from the AlphaMissense publication /
    # database documentation (applied to am_pathogenicity, 0-1):
    #   < LIKELY_BENIGN_MAX        -> "likely_benign"
    #   > LIKELY_PATHOGENIC_MIN    -> "likely_pathogenic"
    #   otherwise                  -> "ambiguous"
    # GEPER does not recompute these -- the catalogue already ships an
    # `am_class` column -- but the thresholds are kept here (rather than
    # hardcoded) for transparency and in case a future catalogue release
    # changes them.
    LIKELY_BENIGN_MAX: float = 0.34
    LIKELY_PATHOGENIC_MIN: float = 0.564


@dataclass(frozen=True)
class MMSpliceConfig:
    """
    Configuration for the MMSplice splice-effect-prediction stage
    (see `pipeline/models/mmsplice/`).

    IMPORTANT -- how this is actually implemented, and why:
    The official `mmsplice` PyPI package pins `cyvcf2<=0.30.15`, which
    cannot be built on CPython 3.12+ (no matching prebuilt wheel, and
    the pinned version's C extension uses CPython internals removed in
    3.12) -- `pip install mmsplice` fails outright on this project's
    Python toolchain. GEPER therefore installs `mmsplice --no-deps`
    (skipping that unbuildable pin entirely) purely to obtain the
    package's bundled, official, pretrained Keras weight files, and
    loads them directly via TensorFlow/Keras plus a small, faithfully
    ported reimplementation of MMSplice's published scoring math --
    never importing the `mmsplice` package's own Python code (which
    would re-trigger the same cyvcf2 import chain). Every prediction
    still uses the real, unmodified, official MMSplice network weights
    (Cheng et al. 2019, *Genome Biology*). Full rationale in
    `pipeline/models/mmsplice/utils.py` and `loader.py` module
    docstrings.
    """

    ENABLED: bool = os.environ.get("GEPER_ENABLE_MMSPLICE", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    # "auto" prefers GPU if TensorFlow reports one visible, else CPU.
    DEVICE: str = os.environ.get("GEPER_MMSPLICE_DEVICE", "auto")

    # Override to point at a local, pre-vendored copy of the mmsplice
    # package directory (must contain layers.py and a models/
    # subdirectory with the 5 official .h5 weight files) -- e.g. for
    # air-gapped deployments. Empty string -> auto-detect the installed
    # `mmsplice` package's own directory (see loader.py).
    MODEL_DIR: str = os.environ.get("GEPER_MMSPLICE_MODEL_DIR", "")

    # Variant types this integration will attempt to score (requirement
    # #3). MNVs are deliberately excluded by default -- MMSplice's
    # published validation covers SNVs and short indels; multi-
    # nucleotide substitutions are rarer and less validated for this
    # model. Override via GEPER_MMSPLICE_VARIANT_TYPES (comma-separated)
    # if your use case needs them.
    SUPPORTED_VARIANT_TYPES: tuple = tuple(
        t.strip()
        for t in os.environ.get("GEPER_MMSPLICE_VARIANT_TYPES", "SNV,insertion,deletion").split(",")
        if t.strip()
    )

    # How far (bp) into the intron on either side of an exon a variant
    # may fall and still be considered eligible (requirement #4) --
    # covers "intronic variants" and "canonical splice variants" (which
    # sit well within this window, typically the first/last ~10bp of
    # the intron). Also used as the intronic overhang fetched on each
    # side of the exon to build the scored window (requirement #5's
    # inputs) -- must be >= 50 (the Acceptor submodel's required
    # intronic context) or short exons/introns will be zero-padded more
    # than intended.
    INTRON_WINDOW: int = int(os.environ.get("GEPER_MMSPLICE_INTRON_WINDOW", "100"))

    # How far (bp) into the exon from either boundary a variant may
    # fall and still be considered "near splice" (requirement #4's
    # "exonic splice variants" / "near-splice variants"); an exonic
    # variant farther than this from both boundaries (in an exon longer
    # than 2x this value) is classified "deep_exonic" and skipped.
    EXON_NEAR_SPLICE_WINDOW: int = int(os.environ.get("GEPER_MMSPLICE_EXON_WINDOW", "50"))

    # How many nearby candidate frames (bp, each direction) the
    # alt_donor/alt_acceptor cryptic-site scan checks around the
    # annotated boundary (see predictor.py::_cryptic_site_scan). Pure
    # runtime/sensitivity knob -- does not affect delta_logit_psi,
    # donor_score, or acceptor_score.
    CRYPTIC_SCAN_RANGE: int = int(os.environ.get("GEPER_MMSPLICE_CRYPTIC_SCAN_RANGE", "20"))

    # --- Interpretation thresholds (requirement #7) --------------------
    # All on the delta_logit_psi / per-module logit-delta scale MMSplice
    # itself operates on (not a 0-1 probability). Defaults follow the
    # commonly cited MMSplice delta_logit_psi effect-size convention
    # (|delta_logit_psi| >= 2 treated as biologically meaningful in the
    # original publication's variant-effect analyses).
    DELTA_LOGIT_PSI_MODERATE_THRESHOLD: float = float(os.environ.get("GEPER_MMSPLICE_MODERATE_THRESHOLD", "2.0"))
    DELTA_LOGIT_PSI_STRONG_THRESHOLD: float = float(os.environ.get("GEPER_MMSPLICE_STRONG_THRESHOLD", "5.0"))
    # Per-site (donor/acceptor) delta magnitude beyond which a site is
    # called "lost" outright, regardless of the overall delta_logit_psi.
    SITE_LOSS_THRESHOLD: float = float(os.environ.get("GEPER_MMSPLICE_SITE_LOSS_THRESHOLD", "2.5"))
    EXON_SKIPPING_THRESHOLD: float = float(os.environ.get("GEPER_MMSPLICE_EXON_SKIPPING_THRESHOLD", "2.0"))
    INTRON_RETENTION_THRESHOLD: float = float(os.environ.get("GEPER_MMSPLICE_INTRON_RETENTION_THRESHOLD", "2.0"))

    # --- Ensemble / ACMG-evidence contribution weight ------------------
    # How much weight MMSplice's evidence contributes to the aggregate
    # significance score in pipeline/interpretation.py, relative to the
    # other independent evidence sources already there. Set to 0 to
    # keep MMSplice visible in reports/API output while excluding it
    # from the scored evidence aggregate entirely.
    ACMG_EVIDENCE_WEIGHT: float = float(os.environ.get("GEPER_MMSPLICE_ACMG_WEIGHT", "1.0"))

    # --- Caching / batching (requirement #12) --------------------------
    CACHE_ENABLED: bool = os.environ.get("GEPER_MMSPLICE_CACHE_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    CACHE_MAX_SIZE: int = int(os.environ.get("GEPER_MMSPLICE_CACHE_MAX_SIZE", "5000"))
    BATCH_SIZE: int = int(os.environ.get("GEPER_MMSPLICE_BATCH_SIZE", "16"))

    # Mirrors `APIConfig.BLAST_ENABLE_PREFETCH`: when true (default),
    # the orchestrator runs `MMSpliceService.predict_batch()` once over
    # the whole variant list before the main per-variant loop, so the
    # five Keras submodels each see one batched `.predict()` call
    # across every eligible variant instead of one call per variant --
    # the dominant per-variant cost `score_modular_batch` exists to
    # eliminate (see loader.py). Results land in the same
    # `MMSplicePredictionCache` the per-variant path already checks
    # first, so `_run_mmsplice_stage` later gets identical results as
    # pure cache hits; this changes only *how fast* MMSplice runs, not
    # what it returns. Off reverts to one `predict()` call per variant
    # inside the main loop, as before -- useful for isolating timing.
    ENABLE_PREFETCH: bool = os.environ.get("GEPER_MMSPLICE_ENABLE_PREFETCH", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


@dataclass(frozen=True)
class GnomadConfig:
    """
    Configuration for the gnomAD population-frequency evidence source
    (see `pipeline/gnomad/`).

    Two independent, configurable query sources (requirement #4):
      - A local, tabix-indexed gnomAD "sites" VCF per build (Broad's
        own published release format, already bgzip'd + tabix-indexed
        -- GEPER never downloads or builds this itself; point these at
        a copy you've already provisioned, e.g. via `gsutil -m cp` from
        gnomAD's public GCS bucket).
      - gnomAD's public GraphQL API, used automatically when no local
        index is configured for the variant's build (or the local
        index lookup itself fails), unless OFFLINE_MODE is set.
    """

    ENABLED: bool = os.environ.get("GEPER_ENABLE_GNOMAD", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    GRCH38_LOCAL_VCF: str = os.environ.get("GEPER_GNOMAD_GRCH38_LOCAL_VCF", "")
    GRCH37_LOCAL_VCF: str = os.environ.get("GEPER_GNOMAD_GRCH37_LOCAL_VCF", "")

    TABIX_BINARY: str = os.environ.get("GEPER_GNOMAD_TABIX_BINARY", "tabix")

    GRAPHQL_ENDPOINT: str = os.environ.get("GEPER_GNOMAD_GRAPHQL_ENDPOINT", "https://gnomad.broadinstitute.org/api")
    ENABLE_GRAPHQL_FALLBACK: bool = os.environ.get(
        "GEPER_GNOMAD_ENABLE_GRAPHQL_FALLBACK", "true"
    ).strip().lower() not in ("0", "false", "no", "off")

    OFFLINE_MODE: bool = os.environ.get("GEPER_GNOMAD_OFFLINE", "false").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    # 45s, not the 30s most other API integrations in this file use:
    # gnomAD's public GraphQL endpoint has observed, real read timeouts
    # around 30s under load (absorbed by the retry below without
    # failing the run, but each occurrence wastes a full 30s attempt
    # before the retry). A slightly longer timeout reduces how often
    # that wasted-attempt-then-retry cycle fires on a large VCF with
    # many variants, without changing worst-case behavior when the
    # endpoint is genuinely down (still MAX_RETRIES attempts, each
    # bounded by this timeout, before the variant falls back to a
    # not-evaluated annotation).
    QUERY_TIMEOUT_SECS: int = int(os.environ.get("GEPER_GNOMAD_TIMEOUT", "45"))
    MAX_RETRIES: int = int(os.environ.get("GEPER_GNOMAD_MAX_RETRIES", "3"))
    RETRY_BACKOFF_SECS: float = float(os.environ.get("GEPER_GNOMAD_RETRY_BACKOFF", "1.5"))

    CACHE_ENABLED: bool = os.environ.get("GEPER_GNOMAD_CACHE_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    CACHE_MAX_SIZE: int = int(os.environ.get("GEPER_GNOMAD_CACHE_MAX_SIZE", "20000"))
    CACHE_TTL_SECS: float = float(os.environ.get("GEPER_GNOMAD_CACHE_TTL_HOURS", "6")) * 3600
    CACHE_DISK_PATH: str = os.environ.get("GEPER_GNOMAD_CACHE_DISK_PATH", "")
    MAX_CONCURRENT_ASYNC: int = int(os.environ.get("GEPER_GNOMAD_MAX_CONCURRENT", "8"))

    BA1_AF_THRESHOLD: float = float(os.environ.get("GEPER_GNOMAD_BA1_AF", "0.05"))
    BS1_AF_THRESHOLD: float = float(os.environ.get("GEPER_GNOMAD_BS1_AF", "0.01"))
    PM2_AF_THRESHOLD: float = float(os.environ.get("GEPER_GNOMAD_PM2_AF", "0.0001"))
    USE_POPMAX_FOR_BA1_BS1: bool = os.environ.get("GEPER_GNOMAD_USE_POPMAX", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    # India-deployment feature: when set to one of
    # `pipeline.gnomad.models.POPULATIONS` (e.g. "sas" for South
    # Asian), `ACMGRuleEngine._pm2`/`_ba1_bs1` check that population's
    # own gnomAD allele frequency FIRST, before falling back to global
    # (or, for BA1/BS1, popmax) AF -- so a variant that looks rare
    # globally but is actually common within the deployment's target
    # ancestry (frequency diluted by every other population gnomAD
    # pools into "global") is not miscalled PM2-moderate-pathogenic
    # just because the global figure looked rare. Empty string (the
    # default) disables this entirely -- every rule falls back to its
    # pre-existing global/popmax-only behavior, unchanged. Never
    # silently substitutes: when this is set but the priority
    # population's AF is genuinely unavailable for a given variant
    # (not every gnomAD release/endpoint exposes subpopulation data),
    # both rules fall back to global AF and say so explicitly in the
    # rationale -- see `pipeline/acmg_rules.py::ACMGRuleEngine
    # ._population_priority_context`'s docstring.
    POPULATION_PRIORITY: str = os.environ.get("GEPER_GNOMAD_POPULATION_PRIORITY", "").strip().lower()


@dataclass(frozen=True)
class IndiGenomesConfig:
    """
    Configuration for the IndiGenomes population-frequency evidence
    source (see `geper/annotation/indigenomes.py`) -- CSIR-IGIB's
    public resource of genetic variants from 1000+ Indian genomes
    (Jain et al. 2020, NAR, PMID 33095885,
    https://clingen.igib.res.in/indigen/).

    Unlike `GnomadConfig`, there is no local-index option here: this
    integration was originally scoped as "download a frequency file and
    tabix-index it", matching gnomAD's own site-VCF distribution model,
    but that assumption was checked against the live site before
    writing any code and turned out to be false -- IndiGenomes' only
    bulk download (`IndiGenomes_Variants.vcf.gz`, confirmed live: 200
    OK, ~137MB, plain gzip) carries variant positions and type only, no
    AC/AF/AN. The only place this resource actually exposes allele
    frequency is a live per-variant JSON endpoint its own web frontend
    calls (confirmed live and documented in
    `geper/annotation/indigenomes.py`'s module docstring), so this is a
    live-query-only integration, matching `GraphQLGnomadProvider`'s
    retry/backoff/timeout shape without a local-index counterpart.

    RETIRED FROM GEPER'S ACTIVE QUERY PATH as of 2026-08-08 -- see
    `DATA_SOURCE_LICENSE_AUDIT.md`: IndiGenomes' own terms state it "is
    intended for purely research purposes" and that "Commercial use of
    the resource would require licensing," which GEPER has not
    obtained. `ENABLED` therefore now defaults to `false`;
    `pipeline/orchestrator.py` no longer calls
    `IndiGenomesLookup.query_variant` at all (not just internally
    no-oping on this flag), and `annotation/thousand_genomes_sas.py`'s
    1000 Genomes SAS lookup is the sole Indian/South-Asian
    population-frequency source in the "Indian Population Frequency"
    report section now. This module and its config are kept, not
    deleted, so the integration can be reinstated (flip `ENABLED` back
    to `true`, or `GEPER_ENABLE_INDIGENOMES=true`, and restore the call
    in `pipeline/orchestrator.py::process_variant`) if a commercial
    license is obtained later.
    """

    ENABLED: bool = os.environ.get("GEPER_ENABLE_INDIGENOMES", "false").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    ENDPOINT: str = os.environ.get("GEPER_INDIGENOMES_ENDPOINT", "https://clingen.igib.res.in/indigen/data.php")

    QUERY_TIMEOUT_SECS: int = int(os.environ.get("GEPER_INDIGENOMES_TIMEOUT", "30"))
    MAX_RETRIES: int = int(os.environ.get("GEPER_INDIGENOMES_MAX_RETRIES", "3"))
    RETRY_BACKOFF_SECS: float = float(os.environ.get("GEPER_INDIGENOMES_RETRY_BACKOFF", "1.5"))

    # In-process, in-memory only (no disk tier, unlike GnomadCache) --
    # IndiGenomes is a single small public research server, not a
    # production API with its own CDN/caching; this only exists to
    # avoid re-querying the same variant twice within one run.
    CACHE_MAX_SIZE: int = int(os.environ.get("GEPER_INDIGENOMES_CACHE_MAX_SIZE", "20000"))

    # "Common in Indian populations" report-flag threshold (task point:
    # "a 'Common in Indian populations' flag when either exceeds 1%").
    # Originally shared between the gnomAD-SAS and IndiGenomes AF
    # figures; since IndiGenomes' retirement (see this class's own
    # docstring) it's shared between gnomAD-SAS and the 1000 Genomes
    # SAS pooled AF instead -- see
    # `report/clinical_report_builder.py::_indian_population_frequency`.
    # Left under this config class (not moved to
    # `ThousandGenomesSASConfig`) so `GEPER_INDIAN_POPULATION_COMMON_AF`
    # keeps meaning the same thing it always has, regardless of which
    # source(s) currently feed the flag.
    COMMON_AF_THRESHOLD: float = float(os.environ.get("GEPER_INDIAN_POPULATION_COMMON_AF", "0.01"))


@dataclass(frozen=True)
class ThousandGenomesSASConfig:
    """
    Configuration for the 1000 Genomes Project South Asian (SAS)
    sub-population frequency lookup (see
    `annotation/thousand_genomes_sas.py`) -- the SOLE source for the
    "Indian Population Frequency" report section as of 2026-08-08.

    Originally built as a fallback shown only when IndiGenomes was
    confirmed offline for a given run; IndiGenomes itself was retired
    from GEPER's active query path entirely on 2026-08-08 (see
    `IndiGenomesConfig`'s own docstring and `DATA_SOURCE_LICENSE_AUDIT.md`
    -- a commercial-use licensing restriction, not a reliability
    concern), so this now runs unconditionally for every variant rather
    than gated on `utils/service_health.py`'s IndiGenomes entry. See
    `annotation/thousand_genomes_sas.py`'s own module docstring for the
    full investigation this is built from (2026-08-08): real,
    live-confirmed per-sub-population (GIH/PJL/BEB/STU/ITU) allele
    frequencies are reachable via Ensembl's own `/variation/human/
    {rsID}?pops=1` REST endpoint -- `CONFIG.api.ENSEMBL_REST_BASE`, an
    existing GEPER dependency, not a new external service.

    Two disclosures are mandatory wherever this source's data is shown
    (never softened or omitted -- see `_POPULATION_LABELS` and
    `TOTAL_SAMPLE_SIZE` below): the total sample size (n=494 across all
    five sub-populations, phase_3/2015 -- much smaller than
    IndiGenomes' 1000+ India-resident genomes would have been) and that
    every sub-population is a DIASPORA cohort sampled outside India
    (e.g. GIH = Gujarati Indian in Houston, TX; ITU = Indian Telugu in
    the UK), not India-resident individuals. These disclosures matter
    even more now that this is the only source shown, not an
    occasional fallback -- they are unconditional in the report layer,
    never gated on anything.

    Honesty note on reliability: this reuses the same `rest.ensembl.org`
    host and the same "Ensembl" `utils/service_health.py` HEALTH entry
    every other Ensembl-dependent stage already shares (PVS1 transcript
    lookup, ClinGen gene resolution) -- `service_health.py`'s own module
    docstring documents Ensembl throwing intermittent 500/502/503s in
    this exact session. This source is NOT presented as more reliable
    than IndiGenomes was; it inherits Ensembl's own reliability profile,
    whatever that is for a given run.
    """

    ENABLED: bool = os.environ.get("GEPER_ENABLE_1000GENOMES_SAS", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    QUERY_TIMEOUT_SECS: int = int(os.environ.get("GEPER_1000GENOMES_SAS_TIMEOUT", "30"))
    MAX_RETRIES: int = int(os.environ.get("GEPER_1000GENOMES_SAS_MAX_RETRIES", "3"))
    RETRY_BACKOFF_SECS: float = float(os.environ.get("GEPER_1000GENOMES_SAS_RETRY_BACKOFF", "1.5"))


@dataclass(frozen=True)
class ConservationConfig:
    """
    Configuration for the evolutionary-conservation evidence source
    (see `pipeline/conservation/`): PhyloP, PhastCons, and GERP++ all
    integrated.

    Two independent, configurable query sources per score type,
    mirroring `GnomadConfig`'s local-first/API-fallback shape:
      - A local bigWig file per (score type, build) -- UCSC's/Ensembl's
        own published format, e.g. `hg38.phyloP100way.bw` /
        `hg38.phastCons100way.bw` / a GERP bigWig from Ensembl's FTP
        site -- GEPER never downloads these itself; point these at a
        copy you've already provisioned), queried via the
        `bigWigSummary` CLI.
      - An API fallback, used automatically when no local track is
        configured for that score type/build, unless OFFLINE_MODE is
        set. PhyloP/PhastCons: UCSC's public Genome Browser REST API
        (https://api.genome.ucsc.edu/getData/track). GERP++: NOT
        available from UCSC (confirmed: no "gerp"-named track exists
        for either build) or Ensembl's REST API (confirmed: no
        conservation-score endpoint exists there either, only bigWig/
        Compara-API access) -- MyVariant.info
        (https://myvariant.info), which re-publishes dbNSFP's
        precomputed GERP++ RS score, is the one live source verified
        to actually work (see pipeline/conservation/provider.py::
        MyVariantGerpProvider's own docstring). SNV-only (dbNSFP's own
        coverage; MyVariant.info's `chrN:g.POSREF>ALT` id format also
        doesn't extend cleanly to indels).

    PP3/BP4 threshold rationale: phyloP100way scores are approximately
    on a per-track scale reported directly by UCSC (this track's own
    metadata: range roughly -20 to +7.5); there is no single official
    ACMG-endorsed cutoff, so -- exactly like GnomadConfig's own
    BA1_AF_THRESHOLD/BS1_AF_THRESHOLD -- these are deployer-adjustable,
    defaulted to widely-cited conventions (e.g. dbNSFP/InterVar-style
    computational-evidence aggregation commonly treats phyloP > ~2 as
    "conserved" and <= 0 as "not conserved"; the band between is
    deliberately ambiguous -- neither threshold fires) rather than a
    single hardcoded "clinical" number.
    """

    ENABLED: bool = os.environ.get("GEPER_ENABLE_CONSERVATION", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    PHYLOP_GRCH38_LOCAL_BIGWIG: str = os.environ.get("GEPER_PHYLOP_GRCH38_LOCAL_BIGWIG", "")
    PHYLOP_GRCH37_LOCAL_BIGWIG: str = os.environ.get("GEPER_PHYLOP_GRCH37_LOCAL_BIGWIG", "")

    PHASTCONS_GRCH38_LOCAL_BIGWIG: str = os.environ.get("GEPER_PHASTCONS_GRCH38_LOCAL_BIGWIG", "")
    PHASTCONS_GRCH37_LOCAL_BIGWIG: str = os.environ.get("GEPER_PHASTCONS_GRCH37_LOCAL_BIGWIG", "")

    GERP_GRCH38_LOCAL_BIGWIG: str = os.environ.get("GEPER_GERP_GRCH38_LOCAL_BIGWIG", "")
    GERP_GRCH37_LOCAL_BIGWIG: str = os.environ.get("GEPER_GERP_GRCH37_LOCAL_BIGWIG", "")

    BIGWIGSUMMARY_BINARY: str = os.environ.get("GEPER_BIGWIGSUMMARY_BINARY", "bigWigSummary")

    UCSC_API_ENDPOINT: str = os.environ.get(
        "GEPER_CONSERVATION_UCSC_API_ENDPOINT", "https://api.genome.ucsc.edu/getData/track"
    )
    ENABLE_UCSC_API_FALLBACK: bool = os.environ.get(
        "GEPER_CONSERVATION_ENABLE_UCSC_API_FALLBACK", "true"
    ).strip().lower() not in ("0", "false", "no", "off")

    # GERP++'s own API fallback -- see this class's own docstring for
    # why MyVariant.info, not UCSC/Ensembl.
    MYVARIANT_API_ENDPOINT: str = os.environ.get(
        "GEPER_CONSERVATION_MYVARIANT_API_ENDPOINT", "https://myvariant.info/v1/variant"
    )
    ENABLE_MYVARIANT_API_FALLBACK: bool = os.environ.get(
        "GEPER_CONSERVATION_ENABLE_MYVARIANT_API_FALLBACK", "true"
    ).strip().lower() not in ("0", "false", "no", "off")

    OFFLINE_MODE: bool = os.environ.get("GEPER_CONSERVATION_OFFLINE", "false").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    QUERY_TIMEOUT_SECS: int = int(os.environ.get("GEPER_CONSERVATION_TIMEOUT", "30"))
    MAX_RETRIES: int = int(os.environ.get("GEPER_CONSERVATION_MAX_RETRIES", "3"))
    RETRY_BACKOFF_SECS: float = float(os.environ.get("GEPER_CONSERVATION_RETRY_BACKOFF", "1.5"))

    CACHE_ENABLED: bool = os.environ.get("GEPER_CONSERVATION_CACHE_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    CACHE_MAX_SIZE: int = int(os.environ.get("GEPER_CONSERVATION_CACHE_MAX_SIZE", "20000"))
    CACHE_TTL_SECS: float = float(os.environ.get("GEPER_CONSERVATION_CACHE_TTL_HOURS", "6")) * 3600
    CACHE_DISK_PATH: str = os.environ.get("GEPER_CONSERVATION_CACHE_DISK_PATH", "")
    MAX_CONCURRENT_ASYNC: int = int(os.environ.get("GEPER_CONSERVATION_MAX_CONCURRENT", "8"))

    PHYLOP_CONSERVED_THRESHOLD: float = float(os.environ.get("GEPER_PHYLOP_CONSERVED_THRESHOLD", "2.0"))
    PHYLOP_NOT_CONSERVED_THRESHOLD: float = float(os.environ.get("GEPER_PHYLOP_NOT_CONSERVED_THRESHOLD", "0.0"))

    # PhastCons is a 0..1 probability ("this base is part of a
    # conserved element"), a fundamentally different scale from
    # PhyloP's -- so it gets its own pair of thresholds, not PhyloP's
    # reused. 0.8/0.2 mirror the same widely-cited dbNSFP/InterVar-
    # style convention PHYLOP_*_THRESHOLD's own docstring describes,
    # applied to PhastCons's own published scale.
    PHASTCONS_CONSERVED_THRESHOLD: float = float(os.environ.get("GEPER_PHASTCONS_CONSERVED_THRESHOLD", "0.8"))
    PHASTCONS_NOT_CONSERVED_THRESHOLD: float = float(os.environ.get("GEPER_PHASTCONS_NOT_CONSERVED_THRESHOLD", "0.2"))

    # GERP++ RS scores (Davydov et al. 2010's own "rejected
    # substitutions" scale) typically range roughly -12 to +6.17;
    # RS > 2 is a widely-cited "constrained" cutoff in the same
    # dbNSFP/InterVar-style literature PHYLOP_*_THRESHOLD's own
    # docstring cites, reused here (not a new invented number) since
    # it lands on a comparable "elevated = conserved" scale to
    # PhyloP's own default, even though the two scores aren't
    # numerically equivalent.
    GERP_CONSERVED_THRESHOLD: float = float(os.environ.get("GEPER_GERP_CONSERVED_THRESHOLD", "2.0"))
    GERP_NOT_CONSERVED_THRESHOLD: float = float(os.environ.get("GEPER_GERP_NOT_CONSERVED_THRESHOLD", "0.0"))


@dataclass(frozen=True)
class ClinGenConfig:
    """
    Configuration for the ClinGen clinical evidence source (see
    `pipeline/clingen/`): gene-disease clinical validity, dosage
    sensitivity (haploinsufficiency/triplosensitivity), and clinical
    actionability.

    Two independent, configurable query sources, mirroring
    `GnomadConfig`'s local-first/API-fallback shape:
      - ClinGen's own published Gene-Disease Validity and Dosage
        Sensitivity flat-file downloads (stable, versioned TSV/CSV --
        see https://search.clinicalgenome.org/kb/gene-validity and
        .../kb/dosage/download), provisioned locally by the deployer.
        This is the primary, most reliable source.
      - ClinGen's live gene-curation API, used automatically as a
        fallback for genes not present in the local files (or when no
        local file is configured), unless OFFLINE_MODE is set. See
        `pipeline/clingen/provider.py::LiveAPIClinGenProvider`'s
        docstring for an important caveat: this environment could not
        exercise a live call to clinicalgenome.org during development,
        so the endpoint/response-schema assumptions below should be
        re-verified before production use.
    """

    ENABLED: bool = os.environ.get("GEPER_ENABLE_CLINGEN", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    GENE_VALIDITY_LOCAL_FILE: str = os.environ.get("GEPER_CLINGEN_GENE_VALIDITY_FILE", "")
    DOSAGE_SENSITIVITY_LOCAL_FILE: str = os.environ.get("GEPER_CLINGEN_DOSAGE_FILE", "")

    API_ENDPOINT: str = os.environ.get("GEPER_CLINGEN_API_ENDPOINT", "https://search.clinicalgenome.org/api")
    API_ENABLED: bool = os.environ.get("GEPER_CLINGEN_API_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    OFFLINE_MODE: bool = os.environ.get("GEPER_CLINGEN_OFFLINE", "false").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    QUERY_TIMEOUT_SECS: int = int(os.environ.get("GEPER_CLINGEN_TIMEOUT", "30"))
    MAX_RETRIES: int = int(os.environ.get("GEPER_CLINGEN_MAX_RETRIES", "3"))
    RETRY_BACKOFF_SECS: float = float(os.environ.get("GEPER_CLINGEN_RETRY_BACKOFF", "1.5"))

    CACHE_ENABLED: bool = os.environ.get("GEPER_CLINGEN_CACHE_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    # Gene-level curation changes far less often than population
    # frequencies, so this defaults to a much longer TTL than
    # gnomAD's cache (24h vs 6h).
    CACHE_MAX_SIZE: int = int(os.environ.get("GEPER_CLINGEN_CACHE_MAX_SIZE", "5000"))
    CACHE_TTL_SECS: float = float(os.environ.get("GEPER_CLINGEN_CACHE_TTL_HOURS", "24")) * 3600
    CACHE_DISK_PATH: str = os.environ.get("GEPER_CLINGEN_CACHE_DISK_PATH", "")
    MAX_CONCURRENT: int = int(os.environ.get("GEPER_CLINGEN_MAX_CONCURRENT", "8"))

    # ACMG-integration thresholds (requirement #5). Kept configurable
    # rather than hard-coded, matching every other ACMG threshold in
    # this codebase (e.g. CONFIG.gnomad.BA1_AF_THRESHOLD).
    DOSAGE_SUFFICIENT_EVIDENCE_SCORE: int = int(os.environ.get("GEPER_CLINGEN_DOSAGE_SUFFICIENT_SCORE", "3"))
    DOSAGE_UNLIKELY_SCORE: int = int(os.environ.get("GEPER_CLINGEN_DOSAGE_UNLIKELY_SCORE", "40"))

    # -- self-provisioning local dataset (see pipeline/clingen/bootstrap.py) --
    #
    # `GENE_VALIDITY_LOCAL_FILE`/`DOSAGE_SENSITIVITY_LOCAL_FILE` above
    # are for a deployer who wants to pin an exact file. Most
    # deployments won't set either, so without this, GEPER's own
    # `LocalDatasetClinGenProvider.is_available()` is always False and
    # every gene-disease validity / dosage-sensitivity lookup silently
    # returns "not found" -- `LiveAPIClinGenProvider`'s per-gene
    # endpoint template is unverified (see that provider's docstring)
    # and does not answer real requests, so nothing else fills the
    # gap. When no explicit local file is configured, GEPER instead
    # fetches ClinGen's own official downloads once and caches them to
    # disk, refreshed on the TTL below -- the same "download once,
    # query locally" shape `GENE_VALIDITY_LOCAL_FILE` already commits
    # to, just self-provisioned instead of requiring a manual step.
    AUTO_FETCH_ENABLED: bool = os.environ.get("GEPER_CLINGEN_AUTO_FETCH", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    # ClinGen's Gene-Disease Clinical Validity curated download (the
    # KB export backing https://search.clinicalgenome.org/kb/gene-validity).
    GENE_VALIDITY_DOWNLOAD_URL: str = os.environ.get(
        "GEPER_CLINGEN_GENE_VALIDITY_URL",
        "https://search.clinicalgenome.org/kb/gene-validity/download",
    )
    # ClinGen's Dosage Sensitivity curated download, numeric-score
    # format (the `ftp.clinicalgenome.org` mirror -- NOT
    # `/kb/gene-dosage/download`, which as of this writing collapses
    # haploinsufficiency into a text label rather than the numeric
    # 0/1/2/3/30/40 score `pipeline/clingen/models.py::
    # DOSAGE_SCORE_LABELS` and the PVS1 mechanism gate both key off).
    DOSAGE_SENSITIVITY_DOWNLOAD_URL: str = os.environ.get(
        "GEPER_CLINGEN_DOSAGE_URL",
        "https://ftp.clinicalgenome.org/ClinGen_gene_curation_list_GRCh38.tsv",
    )
    # Curation changes on the order of weeks/months (see this class's
    # own docstring), so a daily refresh is already generous; default
    # errs toward not hammering ClinGen's download endpoint.
    AUTO_FETCH_TTL_HOURS: float = float(os.environ.get("GEPER_CLINGEN_AUTO_FETCH_TTL_HOURS", "24"))
    AUTO_FETCH_DIR: str = os.environ.get("GEPER_CLINGEN_AUTO_FETCH_DIR", "")  # "" -> "<CACHE_DIR>/clingen"
    AUTO_FETCH_TIMEOUT_SECS: int = int(os.environ.get("GEPER_CLINGEN_AUTO_FETCH_TIMEOUT", "60"))


@dataclass(frozen=True)
class MANEConfig:
    """
    Configuration for NCBI's MANE (Matched Annotation from NCBI and
    EBI) Select gene->transcript dataset (see `pipeline/mane/`): which
    single Ensembl transcript NCBI and EMBL-EBI jointly designate as
    "the" representative transcript for a protein-coding gene. Used
    solely as the second-stage tie-break in
    `pipeline/clingen/utils.py::_disambiguate_overlapping_genes`, when
    two genes' gene bodies (or even coding sequences) genuinely overlap
    a queried position -- Ensembl's own `lookup/id` REST response
    never populates a MANE field (verified live), so without this,
    that tie-break step was always inert and every such case fell
    straight through to AMBIGUOUS.

    Same "download once, query locally, refresh on a TTL" self-
    provisioning shape as `ClinGenConfig`/`HPOConfig` above, not a new
    live per-request API dependency -- see
    `pipeline/mane/bootstrap.py`. NCBI's MANE `current/` directory
    (https://ftp.ncbi.nlm.nih.gov/refseq/MANE/MANE_human/current/) is a
    stable alias, but the actual filenames inside it embed the release
    version (e.g. `MANE.GRCh38.v1.5.summary.txt.gz`, confirmed live
    2026-08-08) -- `INDEX_URL` below is that stable directory, fetched
    and parsed at bootstrap time to discover the exact current
    filename, rather than hardcoding a version number that goes stale
    at the next MANE release. The `.summary.txt.gz` file (~1.1MB
    compressed, ~3.6MB decompressed, ~19,400 rows, one per MANE Select/
    MANE Plus Clinical transcript) is the lightest-weight file that
    carries the gene-symbol -> transcript mapping GEPER needs; the
    alternative GFF3/GTF/FASTA files in the same directory are 7-83MB
    each and carry full genomic-feature annotation this integration
    has no use for.

    Mitochondrial genes (MT-*) and a small number of other genes have
    no MANE Select transcript at all (confirmed live: neither MT-ATP8
    nor MT-ATP6 appear in the dataset) -- this is a genuine, disclosed
    NCBI/EBI scope limitation, not a GEPER gap; such genes simply
    aren't in `_by_gene_symbol`, and the tie-break degrades honestly to
    its existing AMBIGUOUS fallback exactly as it did before this
    dataset was wired in.
    """

    ENABLED: bool = os.environ.get("GEPER_ENABLE_MANE", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    LOCAL_FILE: str = os.environ.get("GEPER_MANE_LOCAL_FILE", "")

    OFFLINE_MODE: bool = os.environ.get("GEPER_MANE_OFFLINE", "false").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    # -- self-provisioning local dataset (see pipeline/mane/bootstrap.py) --
    AUTO_FETCH_ENABLED: bool = os.environ.get("GEPER_MANE_AUTO_FETCH", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    # NCBI's stable "current release" directory index -- parsed at
    # bootstrap time to discover the exact current summary filename
    # (see this class's own docstring for why).
    INDEX_URL: str = os.environ.get(
        "GEPER_MANE_INDEX_URL",
        "https://ftp.ncbi.nlm.nih.gov/refseq/MANE/MANE_human/current/",
    )
    # MANE releases roughly every few months (v1.4 -> v1.5 was ~4
    # months apart, per NCBI's own release history) -- a weekly refresh
    # check is already generous, same reasoning as
    # ClinGenConfig.AUTO_FETCH_TTL_HOURS's daily check for a
    # faster-moving source.
    AUTO_FETCH_TTL_HOURS: float = float(os.environ.get("GEPER_MANE_AUTO_FETCH_TTL_HOURS", "168"))
    AUTO_FETCH_DIR: str = os.environ.get("GEPER_MANE_AUTO_FETCH_DIR", "")  # "" -> "<CACHE_DIR>/mane"
    AUTO_FETCH_TIMEOUT_SECS: int = int(os.environ.get("GEPER_MANE_AUTO_FETCH_TIMEOUT", "60"))


@dataclass(frozen=True)
class EnsemblConfig:
    """
    Configuration for GEPER's self-provisioning Ensembl gene/transcript-
    structure cache (see `pipeline/ensembl/`): the human GTF (gene ->
    transcript -> exon/CDS coordinates) and CDS FASTA (per-transcript
    coding sequence) downloads that replace two previously-live,
    per-request Ensembl REST dependencies with a bootstrap-and-cache
    dataset, the same shape `pipeline/mane/` and `pipeline/uniprot/`
    already established:
      - `pipeline/pvs1/lookup.py::TranscriptLookup` -- transcript exon/
        CDS structure for the PVS1 null-variant decision tree. The
        cache is tried first; a genuine Ensembl REST call
        (`_fetch_live`) remains as the fallback for GRCh37 requests
        (out of this cache's scope, see below) and for any gene the
        cache doesn't have an answer for.
      - `pipeline/clingen/utils.py::resolve_gene_symbol_detail`'s
        Ensembl overlap-region fallback, used only when a variant's VCF
        record carries no `GENE=` INFO field.

    Deliberately GRCh38 only: Ensembl's GRCh37 mirror
    (`https://ftp.ensembl.org/pub/grch37/current/gtf/homo_sapiens/`)
    exists but was not folded into this cache -- GRCh37 lookups already
    have a dedicated, build-routed live REST path
    (`TranscriptLookup._rest_base`/`_GRCH37_REST_BASE`) that this
    integration leaves untouched, and doubling the download/parse cost
    for a legacy build was judged not worth it for GEPER's primary
    GRCh38 use case. A GRCh37 request simply skips the local cache
    entirely and falls straight through to the existing live path --
    an honest scope limitation, not a silent gap.

    Two files, both confirmed live 2026-08-08 against Ensembl's stable
    `current`/`current_fasta` FTP aliases (release 116 at the time):
      - GTF: `https://ftp.ensembl.org/pub/current/gtf/homo_sapiens/` --
        NOT `current_gtf/homo_sapiens/` (that path 404s; only
        `current_fasta/`, `current_gff3/`, and `current_variation/` exist
        as top-level aliases, GTF instead lives under the general
        `current/` release alias). Like NCBI's MANE directory, the
        filename itself embeds the release version
        (`Homo_sapiens.GRCh38.116.gtf.gz`, ~141MB compressed,
        ~4.7GB decompressed/~11.2M lines) -- `GTF_INDEX_URL` below is
        the stable directory, regex-parsed at bootstrap time to find
        the current filename (mirroring
        `pipeline/mane/bootstrap.py::_discover_current_summary_url`),
        specifically excluding the sibling `.abinitio.gtf.gz`/
        `.chr.gtf.gz`/`.chr_patch_hapl_scaff.gtf.gz` files in the same
        directory (ab-initio gene predictions and alternate-scaffold
        variants of the same annotation, neither useful here).
      - CDS FASTA: `https://ftp.ensembl.org/pub/current_fasta/
        homo_sapiens/cds/Homo_sapiens.GRCh38.cds.all.fa.gz` (~37MB
        compressed) -- unlike the GTF, this filename is NOT
        version-suffixed (same stable-alias shape as UniProt's
        reference-proteome download), so `CDS_FASTA_URL` is hit
        directly with no directory-listing discovery step.

    Both files are streamed and parsed line-by-line without ever
    holding the decompressed GTF's ~4.7GB in memory at once (same
    "never buffer the whole decompressed file" discipline
    `pipeline/uniprot/bootstrap.py` documents for its own ~600MB+
    flat file) -- important given this codebase's 8GB-RAM target
    deployment profile. Only one transcript per gene is kept (the
    protein-coding transcript tagged `Ensembl_canonical` in the GTF,
    i.e. the same transcript `TranscriptLookup._choose_transcript`
    would pick from a live REST response for the same gene, falling
    back to the longest-CDS protein-coding transcript when no
    transcript carries that tag), so the cached dataset itself is
    small (~one row per protein-coding gene, comparable in size to
    `pipeline/mane/`'s dataset) even though the raw downloads are not.

    `Ensembl_canonical`/`MANE_Select` are GTF `tag` attribute values,
    confirmed live against BRCA1's own canonical transcript
    (`ENST00000357654`, tagged with both). Neither appears in
    Ensembl's own GTF README's documented tag list (which predates
    their introduction) -- confirmed by direct inspection of the live
    file rather than trusting the README, since the README is
    demonstrably stale on this exact point.
    """

    ENABLED: bool = os.environ.get("GEPER_ENABLE_ENSEMBL_CACHE", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    LOCAL_FILE: str = os.environ.get("GEPER_ENSEMBL_LOCAL_FILE", "")

    OFFLINE_MODE: bool = os.environ.get("GEPER_ENSEMBL_OFFLINE", "false").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    # -- self-provisioning local dataset (see pipeline/ensembl/bootstrap.py) --
    AUTO_FETCH_ENABLED: bool = os.environ.get("GEPER_ENSEMBL_AUTO_FETCH", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    GTF_INDEX_URL: str = os.environ.get(
        "GEPER_ENSEMBL_GTF_INDEX_URL",
        "https://ftp.ensembl.org/pub/current/gtf/homo_sapiens/",
    )
    CDS_FASTA_URL: str = os.environ.get(
        "GEPER_ENSEMBL_CDS_FASTA_URL",
        "https://ftp.ensembl.org/pub/current_fasta/homo_sapiens/cds/Homo_sapiens.GRCh38.cds.all.fa.gz",
    )
    # Ensembl releases roughly every 2-3 months (release 116 as of
    # 2026-08-08) -- a weekly refresh check is already generous, same
    # reasoning as MANEConfig/UniProtConfig's identical TTL.
    AUTO_FETCH_TTL_HOURS: float = float(os.environ.get("GEPER_ENSEMBL_AUTO_FETCH_TTL_HOURS", "168"))
    AUTO_FETCH_DIR: str = os.environ.get("GEPER_ENSEMBL_AUTO_FETCH_DIR", "")  # "" -> "<CACHE_DIR>/ensembl"
    # Much larger downloads than any other bootstrapped dataset in this
    # codebase (~180MB combined) -- a longer default timeout than
    # MANE/UniProt's, since a slow connection genuinely needs more wall
    # time here, not because the server itself is slower to respond.
    AUTO_FETCH_TIMEOUT_SECS: int = int(os.environ.get("GEPER_ENSEMBL_AUTO_FETCH_TIMEOUT", "600"))


@dataclass(frozen=True)
class HPOConfig:
    """
    Configuration for the Human Phenotype Ontology (HPO) gene-phenotype
    evidence source (see `pipeline/hpo/`): which HPO phenotype terms
    are associated with a gene's disease(s), used for annotation/report
    context and as PP4's gene-side input (see
    `pipeline/acmg_rules.py::ACMGRuleEngine._pp4`).

    Two independent, configurable query sources, the same local-first/
    API-fallback shape as `ClinGenConfig` above:
      - HPO's own official Gene-to-Phenotype annotation bulk download
        (stable, versioned TSV -- obophenotype/human-phenotype-ontology's
        `hp/hpoa/genes_to_phenotype.txt` release artifact), auto-fetched
        and cached locally exactly like ClinGen's datasets. This is the
        primary source: richer per-row detail (a specific disease_id and
        observed-frequency fraction per phenotype, not just the term)
        and no live round trip per gene.
      - The Monarch/JAX-hosted HPO live API
        (`https://ontology.jax.org/api/network/...`), used as a fallback
        for a gene not present in the local file (or when none is
        configured). Unlike `ClinGenConfig.API_ENDPOINT`, this endpoint
        *was* exercised against real live requests during development
        (gene search + per-gene annotation, verified against FBN1/
        NCBIGene:2200 and CFTR/NCBIGene:1080) -- no "unverified, re-check
        before production" caveat applies here.
    """

    ENABLED: bool = os.environ.get("GEPER_ENABLE_HPO", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    LOCAL_FILE: str = os.environ.get("GEPER_HPO_LOCAL_FILE", "")

    API_ENDPOINT: str = os.environ.get("GEPER_HPO_API_ENDPOINT", "https://ontology.jax.org/api")
    API_ENABLED: bool = os.environ.get("GEPER_HPO_API_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    OFFLINE_MODE: bool = os.environ.get("GEPER_HPO_OFFLINE", "false").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    QUERY_TIMEOUT_SECS: int = int(os.environ.get("GEPER_HPO_TIMEOUT", "30"))
    MAX_RETRIES: int = int(os.environ.get("GEPER_HPO_MAX_RETRIES", "3"))
    RETRY_BACKOFF_SECS: float = float(os.environ.get("GEPER_HPO_RETRY_BACKOFF", "1.5"))

    CACHE_ENABLED: bool = os.environ.get("GEPER_HPO_CACHE_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    # Gene-phenotype annotation changes on the order of HPO's periodic
    # releases (roughly monthly), far less often than population
    # frequencies -- same long-TTL reasoning as ClinGenConfig.CACHE_TTL_SECS.
    CACHE_MAX_SIZE: int = int(os.environ.get("GEPER_HPO_CACHE_MAX_SIZE", "5000"))
    CACHE_TTL_SECS: float = float(os.environ.get("GEPER_HPO_CACHE_TTL_HOURS", "24")) * 3600
    CACHE_DISK_PATH: str = os.environ.get("GEPER_HPO_CACHE_DISK_PATH", "")
    MAX_CONCURRENT: int = int(os.environ.get("GEPER_HPO_MAX_CONCURRENT", "8"))

    # -- self-provisioning local dataset (see pipeline/hpo/bootstrap.py) --
    AUTO_FETCH_ENABLED: bool = os.environ.get("GEPER_HPO_AUTO_FETCH", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    # HPO's official Gene-to-Phenotype annotation release file (part of
    # the same `hp/hpoa/` release directory as `phenotype.hpoa`), a
    # plain TSV with a single header row and no preamble to skip --
    # verified live: `ncbi_gene_id  gene_symbol  hpo_id  hpo_name  frequency  disease_id`.
    DOWNLOAD_URL: str = os.environ.get(
        "GEPER_HPO_DOWNLOAD_URL",
        "http://purl.obolibrary.org/obo/hp/hpoa/genes_to_phenotype.txt",
    )
    # HPO releases roughly monthly; a daily refresh check is already
    # generous (same reasoning as ClinGenConfig.AUTO_FETCH_TTL_HOURS).
    AUTO_FETCH_TTL_HOURS: float = float(os.environ.get("GEPER_HPO_AUTO_FETCH_TTL_HOURS", "24"))
    AUTO_FETCH_DIR: str = os.environ.get("GEPER_HPO_AUTO_FETCH_DIR", "")  # "" -> "<CACHE_DIR>/hpo"
    AUTO_FETCH_TIMEOUT_SECS: int = int(os.environ.get("GEPER_HPO_AUTO_FETCH_TIMEOUT", "120"))

    # PP4-integration thresholds (see
    # `pipeline/acmg_rules.py::ACMGRuleEngine._pp4`). ACMG/AMP's own PP4
    # wording ("phenotype... highly specific for a disease with a
    # single genetic etiology") is inherently a qualitative clinical
    # judgment call, not a single numeric test -- these two thresholds
    # are GEPER's explicit, disclosed approximation of it (a patient/
    # gene HPO-term overlap fraction, and a cap on how many distinct
    # HPO-curated disease entries a gene may have and still count as
    # "single etiology"), kept configurable rather than hard-coded to
    # match this codebase's policy for every other ACMG threshold.
    PP4_OVERLAP_THRESHOLD: float = float(os.environ.get("GEPER_HPO_PP4_OVERLAP_THRESHOLD", "0.5"))
    PP4_MAX_DISTINCT_DISEASES: int = int(os.environ.get("GEPER_HPO_PP4_MAX_DISTINCT_DISEASES", "3"))

    # -- ontology structure, for case-level phenotype-match semantic
    # similarity only (see pipeline/hpo/ontology.py and
    # pipeline/case_prioritization.py) -- entirely separate from the
    # gene-annotation download above, and from PP4, which never
    # consults this. `genes_to_phenotype.txt` (DOWNLOAD_URL above)
    # carries no term parent-child structure at all -- confirmed by
    # reading its own column list -- so ancestor-based partial-credit
    # similarity needs HPO's own ontology release, not that file.
    ONTOLOGY_LOCAL_FILE: str = os.environ.get("GEPER_HPO_ONTOLOGY_LOCAL_FILE", "")
    # HPO's official Obograph-JSON ontology release -- same
    # purl.obolibrary.org/obo/hp/ host and namespace as DOWNLOAD_URL
    # above, the ontology-structure release rather than the
    # gene-annotation one.
    ONTOLOGY_DOWNLOAD_URL: str = os.environ.get(
        "GEPER_HPO_ONTOLOGY_DOWNLOAD_URL",
        "https://purl.obolibrary.org/obo/hp.json",
    )


@dataclass(frozen=True)
class OrphanetConfig:
    """
    Configuration for the Orphanet rare-disease gene-disorder evidence
    source (see `pipeline/orphanet/`): which specific rare disorders
    (by ORPHAcode) a gene is tied to, and by what kind of relationship
    (disease-causing germline mutation, major susceptibility factor,
    candidate-gene-tested, etc.) -- used for annotation/report context,
    alongside (not in place of) ClinGen's gene-disease clinical
    validity classification (see `ClinGenConfig` above, and
    `pipeline/acmg_rules.py::ACMGRuleEngine._pp1`/`_bs4`).

    License: Orphanet distributes two materially different things
    under two different terms (verified live against
    https://www.orphadata.com's legal notice and pricing pages before
    this integration was added):
      - "Orphadata Science" (what this integration uses): free,
        no-account, bi-annual (July/December) bulk XML downloads,
        under CC BY 4.0 -- "free to copy, distribute, display and make
        commercial use of this data in all legislations, provided you
        cite the provenance." Fully commercial-use-compatible.
      - "Orphadata Products" (the REST API at api.orphadata.com, and
        other services): requires a paid Data Transfer Agreement or
        Service Contract -- verified live (api.orphadata.com returns
        HTTP 404 for a guessed query, i.e. it is a real, working,
        contract-gated service, not merely undocumented). This
        integration deliberately does NOT use it; see
        `pipeline/orphanet/provider.py`'s docstring.

    Single query source: the CC BY 4.0 bulk download
    (`en_product6.xml`, "genes associated with rare diseases"),
    self-fetched by `pipeline/orphanet/bootstrap.py` on the same
    "download once, query locally, refresh on a TTL" shape as
    `HPOConfig` above. There is no live-API tier to fall back to (see
    `pipeline/orphanet/provider.py`'s docstring for why) -- a gene
    absent from this dataset is genuinely not covered, not a case
    where a live call would find more.
    """

    ENABLED: bool = os.environ.get("GEPER_ENABLE_ORPHANET", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    LOCAL_FILE: str = os.environ.get("GEPER_ORPHANET_LOCAL_FILE", "")

    OFFLINE_MODE: bool = os.environ.get("GEPER_ORPHANET_OFFLINE", "false").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    CACHE_ENABLED: bool = os.environ.get("GEPER_ORPHANET_CACHE_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    # Gene-disorder association data changes on the order of Orphanet's
    # bi-annual releases -- same long-TTL reasoning as
    # HPOConfig.CACHE_TTL_SECS/ClinGenConfig.CACHE_TTL_SECS.
    CACHE_MAX_SIZE: int = int(os.environ.get("GEPER_ORPHANET_CACHE_MAX_SIZE", "5000"))
    CACHE_TTL_SECS: float = float(os.environ.get("GEPER_ORPHANET_CACHE_TTL_HOURS", "24")) * 3600
    CACHE_DISK_PATH: str = os.environ.get("GEPER_ORPHANET_CACHE_DISK_PATH", "")

    # -- self-provisioning local dataset (see pipeline/orphanet/bootstrap.py) --
    AUTO_FETCH_ENABLED: bool = os.environ.get("GEPER_ORPHANET_AUTO_FETCH", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    # Orphadata Science's official "genes associated with rare
    # diseases" release file -- verified live: root <JDBOR> with a
    # `date` attribute (the release's "Data version", used in the
    # required citation), 4,245 <Disorder> entries as of the July 2026
    # release.
    DOWNLOAD_URL: str = os.environ.get(
        "GEPER_ORPHANET_DOWNLOAD_URL",
        "https://www.orphadata.com/data/xml/en_product6.xml",
    )
    # Orphanet releases bi-annually (July/December); a daily refresh
    # check is already generous (same reasoning as
    # HPOConfig.AUTO_FETCH_TTL_HOURS).
    AUTO_FETCH_TTL_HOURS: float = float(os.environ.get("GEPER_ORPHANET_AUTO_FETCH_TTL_HOURS", "24"))
    AUTO_FETCH_DIR: str = os.environ.get("GEPER_ORPHANET_AUTO_FETCH_DIR", "")  # "" -> "<CACHE_DIR>/orphanet"
    # Longer default than HPO's/ClinGen's (120s): the real file is
    # ~22MB, larger than either of those downloads.
    AUTO_FETCH_TIMEOUT_SECS: int = int(os.environ.get("GEPER_ORPHANET_AUTO_FETCH_TIMEOUT", "180"))


@dataclass(frozen=True)
class FunctionalEvidenceConfig:
    """
    Configuration for the PS3/BS3 functional-evidence sources (see
    `pipeline/functional_evidence/`): published functional assay
    results (saturation genome editing, deep mutational scans, MAVE
    reporter assays, ...) supporting or refuting a damaging effect for
    a specific variant.

    Two sources, primary/secondary rather than local/API-fallback
    (both are live APIs; there is no bulk-download tier for either
    today):
      - **ClinGen Evidence Repository** (erepo.clinicalgenome.org) --
        primary source. Expert-panel (VCEP)-curated PS3/BS3
        Met/Not-Met calls, already assigned a strength and traceable
        to the panel's own published specification. Verified live
        against real BRCA1/TP53 data during development (unlike
        `ClinGenConfig.API_ENDPOINT` above, this endpoint *was*
        reachable and exercised from this environment). CC0 licensed,
        same as every other ClinGen curated resource.
      - **MaveDB** (api.mavedb.org) -- secondary source, used only for
        a variant ClinGen ERepo has no curation for. Raw multiplexed
        functional-assay scores, bucketed into
        functional/intermediate/non-functional using each score-set's
        own investigator-provided `scoreCalibrations` thresholds
        (never a threshold GEPER invents itself). CC0/CC-BY licensed
        per score-set (verified live: every BRCA1/TP53 score-set
        checked during development was CC0 or CC BY 4.0).

    Neither source covers every gene -- e.g. CFTR has no BRCA1/TP53-
    style saturation-genome-editing coverage in either source as of
    this integration (confirmed live: both a ClinGen ERepo `gene=CFTR`
    query and a MaveDB `CFTR` search returned zero results). PS3/BS3
    report "not_evaluated" for such a gene, the same honest-gap pattern
    already used for PS4/BS4/PP4-before-patient-input.
    """

    ENABLED: bool = os.environ.get("GEPER_ENABLE_FUNCTIONAL_EVIDENCE", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    # -- ClinGen Evidence Repository (primary) ------------------------
    EREPO_ENABLED: bool = os.environ.get("GEPER_FUNCTIONAL_EVIDENCE_EREPO_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    EREPO_API_ENDPOINT: str = os.environ.get(
        "GEPER_FUNCTIONAL_EVIDENCE_EREPO_ENDPOINT",
        "https://erepo.clinicalgenome.org/evrepo/api",
    )

    # -- MaveDB (secondary) --------------------------------------------
    MAVEDB_ENABLED: bool = os.environ.get("GEPER_FUNCTIONAL_EVIDENCE_MAVEDB_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    MAVEDB_API_ENDPOINT: str = os.environ.get(
        "GEPER_FUNCTIONAL_EVIDENCE_MAVEDB_ENDPOINT",
        "https://api.mavedb.org/api/v1",
    )
    # A gene like BRCA1 can have 60+ score sets (many are per-exon
    # replicate splits of the same underlying assay); fetching every
    # one's full variant-data CSV per gene would be a lot of network
    # calls for one secondary, best-effort source. Capped and sorted by
    # `numVariants` descending (largest, most-complete assays first) --
    # a deliberate coverage/performance tradeoff, disclosed rather than
    # silent.
    MAVEDB_MAX_SCORE_SETS_PER_GENE: int = int(os.environ.get("GEPER_FUNCTIONAL_EVIDENCE_MAVEDB_MAX_SCORE_SETS", "10"))

    OFFLINE_MODE: bool = os.environ.get("GEPER_FUNCTIONAL_EVIDENCE_OFFLINE", "false").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    QUERY_TIMEOUT_SECS: int = int(os.environ.get("GEPER_FUNCTIONAL_EVIDENCE_TIMEOUT", "30"))
    MAX_RETRIES: int = int(os.environ.get("GEPER_FUNCTIONAL_EVIDENCE_MAX_RETRIES", "3"))
    RETRY_BACKOFF_SECS: float = float(os.environ.get("GEPER_FUNCTIONAL_EVIDENCE_RETRY_BACKOFF", "1.5"))

    # Gene-level cache: both sources are queried once per *gene* (not
    # per variant -- see `pipeline/functional_evidence/lookup.py`), so
    # every subsequent variant in an already-seen gene costs zero
    # additional network calls for the rest of the run. Same long TTL
    # rationale as ClinGen/HPO/Orphanet: published functional-assay
    # curation changes on the order of weeks/months, not per-request.
    CACHE_ENABLED: bool = os.environ.get("GEPER_FUNCTIONAL_EVIDENCE_CACHE_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    CACHE_MAX_SIZE: int = int(os.environ.get("GEPER_FUNCTIONAL_EVIDENCE_CACHE_MAX_SIZE", "5000"))
    CACHE_TTL_SECS: float = float(os.environ.get("GEPER_FUNCTIONAL_EVIDENCE_CACHE_TTL_HOURS", "24")) * 3600
    CACHE_DISK_PATH: str = os.environ.get("GEPER_FUNCTIONAL_EVIDENCE_CACHE_DISK_PATH", "")

    # -- ACMG/SVI (Brnich et al. 2019) integration thresholds ---------
    # A VCEP-assigned strength (e.g. an ERepo evidence code literally
    # labeled "PS3_Moderate") is always honored verbatim over this
    # default -- these only apply when a source gives us a bare
    # PS3/BS3 Met call with no explicit strength of its own.
    EREPO_DEFAULT_STRENGTH: str = os.environ.get("GEPER_FUNCTIONAL_EVIDENCE_EREPO_DEFAULT_STRENGTH", "strong")
    # A raw MaveDB score, bucketed only against a score-set's own
    # calibration (never a GEPER-invented threshold), is one step
    # below an already-adjudicated VCEP call in the Brnich et al.
    # framework's validation hierarchy -- default one strength tier
    # down from ERepo's, capped further to "supporting" when the
    # calibration is itself flagged research-use-only (MaveDB's own
    # `scoreCalibrations[].researchUseOnly` field).
    MAVEDB_CLINICAL_GRADE_STRENGTH: str = os.environ.get(
        "GEPER_FUNCTIONAL_EVIDENCE_MAVEDB_CLINICAL_STRENGTH", "moderate"
    )
    MAVEDB_RESEARCH_USE_ONLY_STRENGTH: str = os.environ.get(
        "GEPER_FUNCTIONAL_EVIDENCE_MAVEDB_RUO_STRENGTH", "supporting"
    )


@dataclass(frozen=True)
class PVS1Config:
    """
    Configuration for the PVS1 (null variant) ACMG/AMP rule -- see
    `pipeline/pvs1/`.

    PVS1 is the only ACMG/AMP criterion whose answer depends on
    transcript structure (which exon the variant lands in, where the
    last exon-exon junction is, how much coding sequence lies 3' of it),
    so this config covers both the Ensembl transcript-structure lookup
    that supplies that structure and the numeric thresholds of the
    ClinGen SVI decision tree.

    Every threshold below is the value published in the source
    guideline, kept configurable rather than hard-coded to match this
    codebase's existing policy for ACMG thresholds (e.g.
    `CONFIG.gnomad.BA1_AF_THRESHOLD`). Changing them changes clinical
    output: they are not tuning knobs, they are the published rule.
    """

    ENABLED: bool = os.environ.get("GEPER_ENABLE_PVS1", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    # -- transcript-structure lookup (Ensembl REST) -------------------
    OFFLINE_MODE: bool = os.environ.get("GEPER_PVS1_OFFLINE", "false").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    QUERY_TIMEOUT_SECS: int = int(os.environ.get("GEPER_PVS1_TIMEOUT", "30"))
    MAX_RETRIES: int = int(os.environ.get("GEPER_PVS1_MAX_RETRIES", "3"))
    RETRY_BACKOFF_SECS: float = float(os.environ.get("GEPER_PVS1_RETRY_BACKOFF", "1.5"))

    CACHE_ENABLED: bool = os.environ.get("GEPER_PVS1_CACHE_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    CACHE_MAX_SIZE: int = int(os.environ.get("GEPER_PVS1_CACHE_MAX_SIZE", "5000"))
    CACHE_TTL_SECS: float = float(os.environ.get("GEPER_PVS1_CACHE_TTL_HOURS", "24")) * 3600
    CACHE_DISK_PATH: str = os.environ.get("GEPER_PVS1_CACHE_DISK_PATH", "")

    # -- ClinGen SVI decision-tree thresholds -------------------------
    # "NMD is not predicted to occur if the premature termination codon
    # occurs in the 3'-most exon or within the 3'-most 50 nucleotides of
    # the penultimate exon" (Abou Tayoun et al. 2018).
    NMD_PENULTIMATE_WINDOW_BP: int = int(os.environ.get("GEPER_PVS1_NMD_WINDOW_BP", "50"))

    # "Removing >10% of the protein product is more likely to have a
    # loss of function effect (PVS1_Strong) compared to variants that
    # remove <10% of the protein (PVS1_Moderate)" (ibid).
    PROTEIN_LOSS_STRONG_FRACTION: float = float(os.environ.get("GEPER_PVS1_PROTEIN_LOSS_FRACTION", "0.10"))

    # Ceiling on the population frequency of a variant that is still
    # allowed to carry PVS1-strength evidence. Approximates the SVI
    # tree's "LoF variants in this exon are frequent in the general
    # population" node at the variant level (GEPER integrates gnomAD
    # variant frequencies, not a per-exon LoF-tolerance track).
    LOF_POPULATION_AF_MAX: float = float(os.environ.get("GEPER_PVS1_LOF_AF_MAX", "0.001"))

    # For a canonical splice-site variant the exact position of the new
    # stop codon is not computed, only bounded. When the bound sits
    # within this many codons of the NMD boundary the NMD call is
    # reported as uncertain rather than asserted. A random frameshift
    # meets a stop after ~20 codons on average, so 50 is a comfortable
    # margin.
    NMD_UNCERTAINTY_MARGIN_CODONS: float = float(os.environ.get("GEPER_PVS1_NMD_MARGIN_CODONS", "50"))

    # The SVI recommendation explicitly forbids PVS1 (Very Strong) and
    # PVS1_Strong for initiation-codon variants, because translation
    # frequently reinitiates at a downstream in-frame methionine.
    INITIATION_CODON_MAX_STRENGTH: str = os.environ.get("GEPER_PVS1_INIT_CODON_MAX_STRENGTH", "moderate")


@dataclass(frozen=True)
class PS1PM5Config:
    """
    Configuration for the PS1 (established pathogenic amino acid
    change) and PM5 (novel amino acid change at an established
    pathogenic codon) ACMG/AMP rules -- see `pipeline/ps1_pm5/`.

    Both rules need a codon-neighborhood ClinVar search (does ANY
    other variant at this exact codon already have an established
    pathogenic classification?), which is the one piece of
    infrastructure this config governs; the confidence and
    splice-proximity thresholds below are the two published caveats
    the task requires ("filter for high-confidence classifications";
    "PS1 should not apply if the novel variant could have a different
    splicing consequence").
    """

    ENABLED: bool = os.environ.get("GEPER_ENABLE_PS1_PM5", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    QUERY_TIMEOUT_SECS: int = int(os.environ.get("GEPER_PS1_PM5_TIMEOUT", "30"))
    MAX_RETRIES: int = int(os.environ.get("GEPER_PS1_PM5_MAX_RETRIES", "3"))
    RETRY_BACKOFF_SECS: float = float(os.environ.get("GEPER_PS1_PM5_RETRY_BACKOFF", "1.5"))

    CACHE_ENABLED: bool = os.environ.get("GEPER_PS1_PM5_CACHE_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    CACHE_MAX_SIZE: int = int(os.environ.get("GEPER_PS1_PM5_CACHE_MAX_SIZE", "5000"))
    # ClinVar submissions accrue continuously (unlike ClinGen's
    # weeks/months-scale gene curation), so this defaults to a much
    # shorter TTL than pipeline/clingen's or pipeline/pvs1's caches.
    CACHE_TTL_SECS: float = float(os.environ.get("GEPER_PS1_PM5_CACHE_TTL_HOURS", "6")) * 3600
    CACHE_DISK_PATH: str = os.environ.get("GEPER_PS1_PM5_CACHE_DISK_PATH", "")

    # Minimum ClinVar review-status star rating (see
    # `pipeline/ps1_pm5/models.py::star_rating`) an anchor record must
    # reach to count as "established pathogenic". Default of 2
    # ("criteria provided, multiple submitters, no conflicts") matches
    # the task's own guidance ("reviewed with multiple submitters, not
    # conflicting") -- a single-submitter or conflicting record is
    # real ClinVar data but not yet a settled classification, so it is
    # surfaced as a rejected/considered-but-insufficient anchor in the
    # rationale rather than silently dropped.
    MIN_STAR_RATING: int = int(os.environ.get("GEPER_PS1_PM5_MIN_STAR_RATING", "2"))

    # PS1/PM5 splice-proximity caveat: an exonic substitution within
    # this many bp of an exon-intron junction is "splice-region"
    # territory (matching VEP's own `splice_region_variant` convention
    # of 3 exonic bp) even though it is not one of the canonical
    # +-1/+-2 intronic bases `pipeline/pvs1` already tracks separately
    # -- it could plausibly disrupt splicing *in addition to* changing
    # the amino acid, a mechanism the established pathogenic anchor at
    # the same codon may not share. PS1/PM5 are withheld (not
    # downgraded) when the *query* variant itself falls in this
    # window, per the task's explicit "should NOT apply" wording.
    SPLICE_PROXIMITY_EXON_BP: int = int(os.environ.get("GEPER_PS1_PM5_SPLICE_PROXIMITY_BP", "3"))


@dataclass(frozen=True)
class UniProtConfig:
    """
    Configuration for the UniProt protein-annotation evidence source
    (see `pipeline/uniprot/`): reviewed (Swiss-Prot) protein function,
    disease relevance, and sequence features for a variant's gene.

    License: UniProt data is distributed under CC-BY-4.0
    (https://www.uniprot.org/help/license), which permits commercial
    use with attribution -- verified before this integration was
    added, per the same "verify the license before integrating"
    policy this project already documents for gnomAD/ClinGen data.

    Two independent, configurable query sources, mirroring
    `GnomadConfig`/`ClinGenConfig`'s local-first/API-fallback shape:
      - A local JSON-lines dataset (`gene_symbol` -> UniProt entry
        JSON), for offline/deterministic use -- either a deployer-
        provisioned `LOCAL_DATASET_FILE`, or (when none is set) self-
        fetched and cached by `pipeline/uniprot/bootstrap.py`, the
        same "download once, query locally" shape `pipeline/mane/`
        and `pipeline/hpo/` already use.
      - UniProt's public REST API (`rest.uniprot.org`), used
        automatically when no local file is available (or a specific
        gene isn't in it), unless OFFLINE_MODE is set.

    Bootstrap source: UniProt's human reference proteome (`UP000005640`)
    Swiss-Prot flat file, reachable via UniProt's own stable
    `current_release` alias (confirmed live 2026-08-08:
    `.../current_release/knowledgebase/reference_proteomes/Eukaryota/
    UP000005640/UP000005640_9606.dat.gz`, ~128MB, release version
    "2026_02" -- unlike NCBI's MANE directory, UniProt also publishes a
    clean `RELEASE.metalink` XML manifest right alongside the data file
    itself, with an explicit `<version>` tag, so `bootstrap.py` reads
    that for the version signal rather than parsing directory-listing
    HTML). The reference proteome's own `.gene2acc` mapping file was
    considered and NOT used -- it carries only accession<->external-ID
    cross-references, no function/disease/feature annotation, so it
    cannot answer this integration's actual queries; the gene symbol
    needed to index each entry is already present in the `.dat` file's
    own GN (gene name) line, making a second download unnecessary.
    """

    ENABLED: bool = os.environ.get("GEPER_ENABLE_UNIPROT", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    LOCAL_DATASET_FILE: str = os.environ.get("GEPER_UNIPROT_LOCAL_FILE", "")

    # -- self-provisioning local dataset (see pipeline/uniprot/bootstrap.py) --
    AUTO_FETCH_ENABLED: bool = os.environ.get("GEPER_UNIPROT_AUTO_FETCH", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    # UniProt's stable "current release" directory for the human
    # reference proteome -- filenames here are NOT version-suffixed
    # (unlike NCBI's MANE directory), so no directory-listing discovery
    # step is needed; only the RELEASE.metalink manifest is read, for
    # the version string.
    AUTO_FETCH_DIR_URL: str = os.environ.get(
        "GEPER_UNIPROT_AUTO_FETCH_DIR_URL",
        "https://ftp.uniprot.org/pub/databases/uniprot/current_release/"
        "knowledgebase/reference_proteomes/Eukaryota/UP000005640/",
    )
    # UniProt's own release cadence is ~8 weeks (confirmed live: 6
    # releases/year via ftp.uniprot.org/.../previous_releases/) -- a
    # weekly refresh check is already generous, same reasoning as
    # `MANEConfig.AUTO_FETCH_TTL_HOURS`.
    AUTO_FETCH_TTL_HOURS: float = float(os.environ.get("GEPER_UNIPROT_AUTO_FETCH_TTL_HOURS", "168"))
    AUTO_FETCH_LOCAL_DIR: str = os.environ.get("GEPER_UNIPROT_AUTO_FETCH_DIR", "")  # "" -> "<CACHE_DIR>/uniprot"
    AUTO_FETCH_TIMEOUT_SECS: int = int(os.environ.get("GEPER_UNIPROT_AUTO_FETCH_TIMEOUT", "180"))

    API_BASE: str = os.environ.get("GEPER_UNIPROT_API_BASE", "https://rest.uniprot.org/uniprotkb")
    ORGANISM_ID: str = os.environ.get("GEPER_UNIPROT_ORGANISM_ID", "9606")  # Homo sapiens
    REVIEWED_ONLY: bool = os.environ.get("GEPER_UNIPROT_REVIEWED_ONLY", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    OFFLINE_MODE: bool = os.environ.get("GEPER_UNIPROT_OFFLINE", "false").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    QUERY_TIMEOUT_SECS: int = int(os.environ.get("GEPER_UNIPROT_TIMEOUT", "30"))
    MAX_RETRIES: int = int(os.environ.get("GEPER_UNIPROT_MAX_RETRIES", "3"))
    RETRY_BACKOFF_SECS: float = float(os.environ.get("GEPER_UNIPROT_RETRY_BACKOFF", "1.5"))

    CACHE_ENABLED: bool = os.environ.get("GEPER_UNIPROT_CACHE_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    # Reviewed protein annotations change infrequently -- a long TTL,
    # same order of magnitude as ClinGen's gene-level cache.
    CACHE_MAX_SIZE: int = int(os.environ.get("GEPER_UNIPROT_CACHE_MAX_SIZE", "5000"))
    CACHE_TTL_SECS: float = float(os.environ.get("GEPER_UNIPROT_CACHE_TTL_HOURS", "24")) * 3600
    CACHE_DISK_PATH: str = os.environ.get("GEPER_UNIPROT_CACHE_DISK_PATH", "")


@dataclass(frozen=True)
class InterProConfig:
    """
    Configuration for the InterPro/Pfam conserved-domain evidence
    source (see `pipeline/interpro/`): domain/family/motif annotations
    for a variant's protein, keyed by UniProt accession (resolved by
    the UniProt stage that runs immediately before this one).

    License: InterPro (and Pfam, now hosted within InterPro) data is
    distributed under CC0 1.0 Universal (public domain dedication;
    https://interpro-documentation.readthedocs.io/en/latest/license.html),
    with no restriction on commercial use whatsoever -- verified
    before this integration was added.

    Single query source (InterPro's public REST API), since InterPro
    itself is the actively maintained, canonical distribution point
    for Pfam domain calls (the standalone Pfam website now redirects
    here) -- no separate local-dataset layer is needed the way
    gnomAD/ClinGen have one for their much larger flat-file downloads.
    """

    ENABLED: bool = os.environ.get("GEPER_ENABLE_INTERPRO", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    LOCAL_DATASET_FILE: str = os.environ.get("GEPER_INTERPRO_LOCAL_FILE", "")

    API_BASE: str = os.environ.get("GEPER_INTERPRO_API_BASE", "https://www.ebi.ac.uk/interpro/api")
    PAGE_SIZE: int = int(os.environ.get("GEPER_INTERPRO_PAGE_SIZE", "200"))

    OFFLINE_MODE: bool = os.environ.get("GEPER_INTERPRO_OFFLINE", "false").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    QUERY_TIMEOUT_SECS: int = int(os.environ.get("GEPER_INTERPRO_TIMEOUT", "30"))
    MAX_RETRIES: int = int(os.environ.get("GEPER_INTERPRO_MAX_RETRIES", "3"))
    RETRY_BACKOFF_SECS: float = float(os.environ.get("GEPER_INTERPRO_RETRY_BACKOFF", "1.5"))

    CACHE_ENABLED: bool = os.environ.get("GEPER_INTERPRO_CACHE_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    CACHE_MAX_SIZE: int = int(os.environ.get("GEPER_INTERPRO_CACHE_MAX_SIZE", "5000"))
    CACHE_TTL_SECS: float = float(os.environ.get("GEPER_INTERPRO_CACHE_TTL_HOURS", "24")) * 3600
    CACHE_DISK_PATH: str = os.environ.get("GEPER_INTERPRO_CACHE_DISK_PATH", "")


@dataclass(frozen=True)
class AlphaFoldConfig:
    """
    Configuration for the AlphaFold Protein Structure Database
    evidence source (see `pipeline/alphafold/`): structural reference,
    per-residue confidence (pLDDT), and structural context for a
    variant's protein, keyed by UniProt accession.

    License: AlphaFold DB data (structures + confidence metrics) is
    distributed under CC-BY-4.0, explicitly for "academic and
    commercial use" (https://alphafold.ebi.ac.uk/faq,
    https://alphafold.ebi.ac.uk/assets/License-Disclaimer.pdf) --
    verified before this integration was added. Note this is distinct
    from the AlphaFold *model parameters* (CC-BY-NC-4.0,
    non-commercial) -- GEPER never downloads or runs the AlphaFold
    model itself, only the already-computed, separately-licensed
    prediction database entries.

    Single query source (AlphaFold DB's public prediction API +
    structure-file download for per-residue pLDDT), matching
    InterPro's shape above.
    """

    ENABLED: bool = os.environ.get("GEPER_ENABLE_ALPHAFOLD", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    LOCAL_DATASET_FILE: str = os.environ.get("GEPER_ALPHAFOLD_LOCAL_FILE", "")

    API_BASE: str = os.environ.get("GEPER_ALPHAFOLD_API_BASE", "https://alphafold.ebi.ac.uk/api/prediction")

    # Per-residue pLDDT requires downloading the actual structure file
    # (AlphaFold's summary API only returns URLs + version metadata,
    # not the per-residue confidence array -- that lives in the
    # B-factor column of the PDB/mmCIF file itself). This is a bigger
    # network/time cost than a plain JSON lookup, so it's independently
    # toggleable; with it off, GEPER still reports the structural
    # reference (model URL, version) without per-residue confidence.
    FETCH_STRUCTURE_FILE: bool = os.environ.get("GEPER_ALPHAFOLD_FETCH_STRUCTURE", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    MAX_STRUCTURE_FILE_BYTES: int = int(os.environ.get("GEPER_ALPHAFOLD_MAX_STRUCTURE_BYTES", str(20 * 1024 * 1024)))

    OFFLINE_MODE: bool = os.environ.get("GEPER_ALPHAFOLD_OFFLINE", "false").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    QUERY_TIMEOUT_SECS: int = int(os.environ.get("GEPER_ALPHAFOLD_TIMEOUT", "30"))
    MAX_RETRIES: int = int(os.environ.get("GEPER_ALPHAFOLD_MAX_RETRIES", "3"))
    RETRY_BACKOFF_SECS: float = float(os.environ.get("GEPER_ALPHAFOLD_RETRY_BACKOFF", "1.5"))

    CACHE_ENABLED: bool = os.environ.get("GEPER_ALPHAFOLD_CACHE_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    CACHE_MAX_SIZE: int = int(os.environ.get("GEPER_ALPHAFOLD_CACHE_MAX_SIZE", "2000"))
    CACHE_TTL_SECS: float = float(os.environ.get("GEPER_ALPHAFOLD_CACHE_TTL_HOURS", "24")) * 3600
    CACHE_DISK_PATH: str = os.environ.get("GEPER_ALPHAFOLD_CACHE_DISK_PATH", "")

    # pLDDT confidence bands, matching AlphaFold DB's own published
    # thresholds exactly (https://alphafold.ebi.ac.uk/faq).
    PLDDT_VERY_HIGH_THRESHOLD: float = float(os.environ.get("GEPER_ALPHAFOLD_PLDDT_VERY_HIGH", "90"))
    PLDDT_CONFIDENT_THRESHOLD: float = float(os.environ.get("GEPER_ALPHAFOLD_PLDDT_CONFIDENT", "70"))
    PLDDT_LOW_THRESHOLD: float = float(os.environ.get("GEPER_ALPHAFOLD_PLDDT_LOW", "50"))


@dataclass(frozen=True)
class APIConfig:
    """External data source endpoints."""

    ENSEMBL_REST_BASE: str = "https://rest.ensembl.org"
    NCBI_EUTILS_BASE: str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    NCBI_VARIATION_BASE: str = "https://api.ncbi.nlm.nih.gov/variation/v0"
    CLINVAR_DB: str = "clinvar"
    DBSNP_DB: str = "snp"

    REQUEST_TIMEOUT_SECS: int = 30
    MAX_RETRIES: int = 3
    RETRY_BACKOFF_SECS: float = 1.5

    # Runtime-optimization knobs (Phase 2 performance pass). None of
    # these change what a fetch returns -- only how many network round
    # trips it takes to get there. Every optimization gated by these
    # falls back transparently to the original one-request-per-variant
    # GET path on any failure, so flipping either of these off (or the
    # sandbox/deployment simply never hitting the batch path) reproduces
    # the exact pre-optimization behavior.
    ENSEMBL_ENABLE_BATCH_PREFETCH: bool = True
    # Ensembl's own guidance for POST batch endpoints is up to ~1000
    # items per request; 50 is a conservative default that keeps any
    # single failed batch small (a failed batch of 50 falls back to 50
    # individual GETs, not 1000).
    ENSEMBL_BATCH_SIZE: int = 50

    # NCBI E-utilities requires an email / tool for polite usage and
    # allows a higher rate limit with an API key.
    NCBI_TOOL_NAME: str = "GEPER"
    NCBI_EMAIL: str = os.environ.get("GEPER_NCBI_EMAIL", "geper.pipeline@example.com")
    NCBI_API_KEY: str = os.environ.get("GEPER_NCBI_API_KEY", "")

    # --- BLAST performance knobs (Phase 3 performance pass) ------------
    # Remote BLAST (NCBI-hosted qblast) is, by a wide margin, the
    # slowest external call GEPER makes -- often minutes per submission,
    # almost all of it spent waiting in NCBI's shared queue rather than
    # doing local work. These knobs control the layers that address
    # that (see database/blast_client.py for the full rationale):
    #   - disk caching across runs
    #   - concurrent submission of distinct sequences
    #   - automatic preference for a local blastn database when present
    # None of them change what a BLAST result contains.
    BLAST_DISK_CACHE_ENABLED: bool = os.environ.get("GEPER_BLAST_DISK_CACHE", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    # Backend priority for BLASTClient when a caller doesn't explicitly
    # pass mode=... (e.g. the orchestrator/CLI default, or any direct
    # `BLASTClient()` construction with no argument):
    #   "auto"   (default) -- local BLAST+ (blastn + a usable database)
    #             first, remote NCBI BLAST second, graceful skip if
    #             neither is available.
    #   "local"  -- require local BLAST+; raises if no database is
    #             configured.
    #   "remote" -- always use NCBI's hosted BLAST service.
    BLAST_MODE: str = os.environ.get("GEPER_BLAST_MODE", "auto").strip().lower()

    # Local BLAST+ database path/prefix (default production BLAST
    # backend). `GEPER_BLAST_DATABASE` is the primary, documented
    # variable for this; `GEPER_BLAST_LOCAL_DB` and the standard NCBI
    # `BLASTDB` env var are still honored, in that priority order, for
    # backward compatibility with existing deployments/scripts.
    BLAST_LOCAL_DB_PATH: str = os.environ.get("GEPER_BLAST_DATABASE") or os.environ.get("GEPER_BLAST_LOCAL_DB") or ""

    # Optional FASTA reference. If set and no prebuilt BLAST database
    # is found at BLAST_LOCAL_DB_PATH (or alongside the FASTA itself),
    # BLASTClient automatically builds one with `makeblastdb` the first
    # time it's needed, and never rebuilds an already-existing database.
    BLAST_REFERENCE_FASTA: str = os.environ.get("GEPER_BLAST_REFERENCE_FASTA", "")

    # How many *distinct* sequences BLASTClient.search_many() will
    # submit concurrently. Remote is capped low and conservative (NCBI
    # is a shared public service -- this is "don't hammer it", not a
    # throughput dial); local has no such courtesy concern, since it's
    # our own machine, so it defaults to the CPU count instead.
    BLAST_MAX_CONCURRENT_REMOTE: int = int(os.environ.get("GEPER_BLAST_MAX_CONCURRENT_REMOTE", "3"))
    BLAST_MAX_CONCURRENT_LOCAL: int = int(os.environ.get("GEPER_BLAST_MAX_CONCURRENT_LOCAL", str(os.cpu_count() or 4)))

    # Mirrors ENSEMBL_ENABLE_BATCH_PREFETCH above: gates the
    # orchestrator's pre-loop batch/concurrent BLAST submission (see
    # GeperPipeline._prefetch_blast_results). Purely an optimization
    # toggle -- with this off, BLAST reverts to one blocking search()
    # call per variant inside the main loop (still benefiting from
    # in-memory + disk caching, just without the up-front concurrent
    # submission), which is also useful for isolating before/after
    # timing comparisons.
    BLAST_ENABLE_PREFETCH: bool = os.environ.get("GEPER_BLAST_ENABLE_PREFETCH", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


@dataclass(frozen=True)
class HealthCheckConfig:
    """
    Startup reachability probe for external services (Ensembl, NCBI
    eutils/dbSNP, IndiGenomes, ClinGen ERepo, MaveDB) -- see
    `utils/service_health.py`. Purely a fast up-front signal (printed
    as a table before the first variant) plus a runtime skip hint for
    services already confirmed offline; it never changes what a client
    ultimately returns, only how long it spends finding that out.
    """

    ENABLED: bool = os.environ.get("GEPER_HEALTH_CHECK_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    # Deliberately much shorter than any client's own REQUEST_TIMEOUT_SECS
    # (30s) -- this only needs to tell "up" from "down/slow", not complete
    # a real query. Run in parallel across services (see
    # `service_health.run_startup_checks`), so total startup overhead is
    # bounded by this single timeout, not the sum of all services.
    TIMEOUT_SECS: float = float(os.environ.get("GEPER_HEALTH_CHECK_TIMEOUT", "4.0"))


@dataclass(frozen=True)
class ConfidenceConfig:
    """
    Configuration for the Phase 3 Confidence Scoring Engine
    (see `pipeline/confidence_engine.py`).

    This engine is independent of, and does not replace, ACMG
    classification (Phase 1) -- it estimates how much of GEPER's
    *evidence surface* was actually available and internally consistent
    for a given variant, not whether the variant is pathogenic.

    Every category weight below is a documented, environment-overridable
    number (never a bare literal buried in code), and the seven weights
    are normalized against each other at scoring time -- so e.g. doubling
    CLINICAL_WEIGHT always means "clinical evidence matters twice as
    much relative to the others," regardless of what the other six
    weights are set to.

    Rationale for the relative defaults chosen: clinical (ClinVar/
    ClinGen) and population (gnomAD/dbSNP) evidence are curated/
    empirically observed facts, so they default highest; AI predictors
    (AlphaMissense/MMSplice) and protein/structural annotation
    (UniProt/InterPro/AlphaFold) are informative but model-derived or
    positionally-estimated, so they default to roughly half that; raw
    sequence-context models (HyenaDNA/Evo2/RNA-FM/ESM2) and
    "additional" evidence (BLAST; Ensembl's contribution here is
    indirect -- see `confidence_engine._additional_evidence_quality`)
    default lowest, since in this pipeline they inform routing/context
    rather than emitting a per-variant verdict.
    """

    CLINICAL_WEIGHT: float = float(os.environ.get("GEPER_CONFIDENCE_CLINICAL_WEIGHT", "3.0"))
    POPULATION_WEIGHT: float = float(os.environ.get("GEPER_CONFIDENCE_POPULATION_WEIGHT", "2.5"))
    AI_WEIGHT: float = float(os.environ.get("GEPER_CONFIDENCE_AI_WEIGHT", "2.0"))
    PROTEIN_WEIGHT: float = float(os.environ.get("GEPER_CONFIDENCE_PROTEIN_WEIGHT", "1.5"))
    STRUCTURAL_WEIGHT: float = float(os.environ.get("GEPER_CONFIDENCE_STRUCTURAL_WEIGHT", "1.0"))
    SEQUENCE_CONTEXT_WEIGHT: float = float(os.environ.get("GEPER_CONFIDENCE_SEQUENCE_CONTEXT_WEIGHT", "1.0"))
    ADDITIONAL_WEIGHT: float = float(os.environ.get("GEPER_CONFIDENCE_ADDITIONAL_WEIGHT", "1.0"))

    # Multiplicative penalty applied per unit of detected conflict (see
    # `confidence_engine._conflict_penalty`), capped at
    # MAX_CONFLICT_PENALTY so heavily-conflicting evidence lowers
    # confidence sharply without ever being able to push the score
    # negative.
    CONFLICT_PENALTY_PER_CONFLICT: float = float(os.environ.get("GEPER_CONFIDENCE_CONFLICT_PENALTY", "0.15"))
    MAX_CONFLICT_PENALTY: float = float(os.environ.get("GEPER_CONFIDENCE_MAX_CONFLICT_PENALTY", "0.6"))

    # Label thresholds against the final 0-100 score. Configurable so a
    # deployment can retune labels without touching scoring code.
    VERY_HIGH_THRESHOLD: float = float(os.environ.get("GEPER_CONFIDENCE_VERY_HIGH_THRESHOLD", "85"))
    HIGH_THRESHOLD: float = float(os.environ.get("GEPER_CONFIDENCE_HIGH_THRESHOLD", "65"))
    MODERATE_THRESHOLD: float = float(os.environ.get("GEPER_CONFIDENCE_MODERATE_THRESHOLD", "40"))
    # Below MODERATE_THRESHOLD -> "Low".


@dataclass(frozen=True)
class PrioritizationConfig:
    """
    Configuration for the Phase 4 Variant Prioritization Engine
    (see `pipeline/prioritization_engine.py`).

    Priority answers a different question than ACMG classification
    (Phase 1) or confidence (Phase 3): not "is this variant pathogenic"
    or "how much/how consistent is the evidence", but "how urgently
    does this variant deserve human review, given everything GEPER
    found." It therefore *uses* the ACMG classification and confidence
    score as two of its inputs (via ACMG_WEIGHT / CONFIDENCE_WEIGHT)
    rather than recomputing either.

    As with `ConfidenceConfig`, every weight is a documented,
    environment-overridable number, normalized against the others at
    scoring time -- never a bare literal in `prioritization_engine.py`.

    Rationale for the relative defaults: the ACMG classification itself
    (a Phase-1-vetted, criterion-traced conclusion) and clinical
    curation (ClinVar/ClinGen) default highest, since they are the most
    directly clinically-actionable signals. Confidence, population
    rarity, and AI agreement default to the next tier -- each modulates
    how much to trust/act on the classification. Protein-impact,
    conserved-domain, structural, and splicing evidence default lower
    individually since each is one mechanistic clue among several, not
    a verdict on its own. Sequence-context and "additional" evidence
    default lowest for the same reason given in `ConfidenceConfig`:
    they contribute completeness/context in this pipeline, not a
    per-variant verdict.
    """

    ACMG_WEIGHT: float = float(os.environ.get("GEPER_PRIORITY_ACMG_WEIGHT", "3.0"))
    CONFIDENCE_WEIGHT: float = float(os.environ.get("GEPER_PRIORITY_CONFIDENCE_WEIGHT", "1.5"))
    CLINICAL_WEIGHT: float = float(os.environ.get("GEPER_PRIORITY_CLINICAL_WEIGHT", "3.0"))
    POPULATION_RARITY_WEIGHT: float = float(os.environ.get("GEPER_PRIORITY_POPULATION_WEIGHT", "2.0"))
    AI_AGREEMENT_WEIGHT: float = float(os.environ.get("GEPER_PRIORITY_AI_WEIGHT", "1.5"))
    PROTEIN_IMPACT_WEIGHT: float = float(os.environ.get("GEPER_PRIORITY_PROTEIN_IMPACT_WEIGHT", "1.5"))
    CONSERVED_DOMAIN_WEIGHT: float = float(os.environ.get("GEPER_PRIORITY_DOMAIN_WEIGHT", "1.0"))
    STRUCTURAL_WEIGHT: float = float(os.environ.get("GEPER_PRIORITY_STRUCTURAL_WEIGHT", "1.0"))
    SPLICING_WEIGHT: float = float(os.environ.get("GEPER_PRIORITY_SPLICING_WEIGHT", "1.5"))
    SEQUENCE_CONTEXT_WEIGHT: float = float(os.environ.get("GEPER_PRIORITY_SEQUENCE_CONTEXT_WEIGHT", "0.5"))
    ADDITIONAL_WEIGHT: float = float(os.environ.get("GEPER_PRIORITY_ADDITIONAL_WEIGHT", "0.5"))

    # Multiplicative penalty per detected conflict unit, capped, same
    # pattern as `ConfidenceConfig` but tuned/configured independently
    # since priority conflicts (e.g. AI disagreement lowering how
    # urgently actionable a call is) needn't move the same amount as
    # they do for confidence.
    CONFLICT_PENALTY_PER_CONFLICT: float = float(os.environ.get("GEPER_PRIORITY_CONFLICT_PENALTY", "0.10"))
    MAX_CONFLICT_PENALTY: float = float(os.environ.get("GEPER_PRIORITY_MAX_CONFLICT_PENALTY", "0.5"))

    # Priority-category thresholds against the final 0-100 score.
    CRITICAL_THRESHOLD: float = float(os.environ.get("GEPER_PRIORITY_CRITICAL_THRESHOLD", "80"))
    HIGH_THRESHOLD: float = float(os.environ.get("GEPER_PRIORITY_HIGH_THRESHOLD", "60"))
    MODERATE_THRESHOLD: float = float(os.environ.get("GEPER_PRIORITY_MODERATE_THRESHOLD", "35"))
    # Below MODERATE_THRESHOLD -> "Low".


@dataclass(frozen=True)
class CasePrioritizationConfig:
    """
    Configuration for case-level, HPO-phenotype-driven variant ranking
    (see `pipeline/case_prioritization.py`).

    Answers a different question than Phase 4's `PrioritizationEngine`
    above: not "how urgently does THIS variant deserve review" in
    isolation, but "given the whole batch of variants from one VCF and
    the PATIENT's observed symptoms, which ones best explain them" --
    only meaningful case-wide, only runs when `--hpo-terms`/
    `--phenotype-file` supplied patient terms (see
    `pipeline/hpo/utils.py::build_phenotype_result`), and NEVER
    influences ACMG classification or PP4 (see
    `pipeline/acmg_rules.py::ACMGRuleEngine._pp4`'s own strict,
    single-etiology-gated binary criterion, which this does not touch).

    The combined case-rank score below deliberately reuses
    `priority_score` as its non-phenotype input rather than ACMG
    classification and confidence separately: `PrioritizationEngine`
    already folds both of those in (`ACMG_WEIGHT`/`CONFIDENCE_WEIGHT`
    above, among its 11 factors), so re-including them raw here would
    double-count exactly those two signals. `priority_score` is
    therefore read as "how strong/urgent is the evidence, independent
    of phenotype" and combined with the new "does this variant explain
    the patient's symptoms" signal -- two genuinely independent axes.
    """

    # Both normalized 0-1 against each other at scoring time (same
    # discipline as PrioritizationConfig's own weights) -- equal
    # default weight: neither axis is assumed more informative than
    # the other without case-specific evidence one way or the other.
    PHENOTYPE_MATCH_WEIGHT: float = float(os.environ.get("GEPER_CASE_RANK_PHENOTYPE_WEIGHT", "0.5"))
    PRIORITY_WEIGHT: float = float(os.environ.get("GEPER_CASE_RANK_PRIORITY_WEIGHT", "0.5"))

    # Minimum term-pair similarity (see
    # `case_prioritization.py::_term_similarity`) for an ontology
    # ancestor-based match to count as a genuine partial-credit hit
    # rather than noise from two nearly-unrelated terms sharing only a
    # very high-level common ancestor (e.g. "Phenotypic abnormality"
    # itself, which is a parent of nearly everything in HPO and would
    # otherwise give every term pair a nonzero score).
    MIN_ANCESTOR_SIMILARITY: float = float(os.environ.get("GEPER_CASE_RANK_MIN_ANCESTOR_SIMILARITY", "0.15"))


@dataclass(frozen=True)
class ConflictConfig:
    """
    Configuration for the Phase 6 Conflict Resolution Engine (see
    `pipeline/conflict_resolution_engine.py`).

    This engine never changes ACMG classification (Phase 1) -- it
    identifies, documents, and rates the severity of disagreements
    between evidence sources GEPER already collected, so a reviewer can
    see exactly what disagrees and why, without the pipeline silently
    picking a winner.

    Every severity weight and threshold below is documented and
    environment-overridable, same discipline as Phase 3/4.
    """

    # Numeric weight assigned to each severity tier when computing the
    # overall `conflict_score` (0-100). Higher = counts for more.
    MINOR_WEIGHT: float = float(os.environ.get("GEPER_CONFLICT_MINOR_WEIGHT", "1.0"))
    MODERATE_WEIGHT: float = float(os.environ.get("GEPER_CONFLICT_MODERATE_WEIGHT", "2.0"))
    MAJOR_WEIGHT: float = float(os.environ.get("GEPER_CONFLICT_MAJOR_WEIGHT", "3.0"))
    # "Critical" is a distinct, higher tier reserved for exactly one
    # detector (`ConflictResolutionEngine._expert_panel_disagreement_
    # conflict`): GEPER's own ACMG classification disagreeing with a
    # ClinVar record reviewed by an expert panel or practice guideline
    # for this exact (variant-matched) variant. Its presence alone
    # forces the overall `conflict_severity` to "Critical" (see
    # `_overall_severity`) rather than only contributing to the
    # aggregate score like the other tiers.
    CRITICAL_WEIGHT: float = float(os.environ.get("GEPER_CONFLICT_CRITICAL_WEIGHT", "5.0"))

    # A run with this many "major-equivalent" weight units (or more) is
    # treated as saturating the 0-100 conflict_score scale. Configurable
    # rather than hardcoded so a deployment with typically noisier
    # evidence can retune what counts as "maximally conflicting."
    SATURATION_WEIGHT_UNITS: float = float(os.environ.get("GEPER_CONFLICT_SATURATION_UNITS", "6.0"))

    # Overall conflict_severity label thresholds against the 0-100
    # conflict_score.
    MAJOR_THRESHOLD: float = float(os.environ.get("GEPER_CONFLICT_MAJOR_THRESHOLD", "60"))
    MODERATE_THRESHOLD: float = float(os.environ.get("GEPER_CONFLICT_MODERATE_THRESHOLD", "30"))
    # Below MODERATE_THRESHOLD but > 0 -> "Minor"; exactly 0 -> "None".
    # "Critical" is not score-thresholded -- see CRITICAL_WEIGHT above.

    # AlphaFold pLDDT bands treated as "low structural confidence" for
    # the structural-vs-functional-prediction conflict check. Matches
    # AlphaFold DB's own published band names
    # (pipeline/alphafold/models.py's confidence_band()).
    LOW_STRUCTURAL_CONFIDENCE_BANDS: Tuple[str, ...] = ("low", "very_low")


@dataclass(frozen=True)
class VariantNormalizationConfig:
    """
    Configuration for variant normalization (see
    `pipeline/variant_normalization.py`) and HGVS notation (see
    `pipeline/hgvs_utils.py`) -- the early gate that runs immediately
    after VCF parsing, before any provider stage or ACMG rule sees a
    variant's coordinates (`pipeline/orchestrator.py::_process_variant`).

    Reference-free parsimony trimming always runs (no config needed --
    it's pure string manipulation with no cost or failure mode).
    Reference-guided left-alignment additionally needs one or more
    Ensembl fetches per indel (reusing
    `SequenceContextGenerator.fetch_reference_sequence`, the same
    source/cache every other stage already uses) -- `LEFT_ALIGN_ENABLED`
    lets a deployment disable that extra network dependency and fall
    back to trim-only normalization (still correct, just unable to
    resolve the homopolymer/repeat-run ambiguity left-alignment exists
    for).
    """

    ENABLED: bool = os.environ.get("GEPER_ENABLE_VARIANT_NORMALIZATION", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    LEFT_ALIGN_ENABLED: bool = os.environ.get("GEPER_NORMALIZATION_LEFT_ALIGN", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    # Safety bound on how many bases left-alignment will walk left
    # through the reference before giving up -- guards against an
    # absurdly long homopolymer/repeat run (or a reference-fetch bug)
    # turning one indel's normalization into an unbounded number of
    # network round trips.
    LEFT_ALIGN_MAX_SHIFT_BP: int = int(os.environ.get("GEPER_NORMALIZATION_MAX_SHIFT_BP", "200"))


@dataclass(frozen=True)
class VariantClusteringConfig:
    """
    Configuration for the report's gene- and genomic-window clustering
    observation (C7, report review round 2): a multi-finding-per-gene
    or multi-finding-in-a-short-window summary that works without any
    phenotype input (`case_prioritization.py`'s HPO-driven ranking only
    activates with --hpo-terms/--phenotype-file, which leaves every
    run without them -- the common case for a raw VCF -- with no
    cross-finding view at all). Deliberately reports proximity only:
    it never infers phase, compound heterozygosity, or any
    cis/trans relationship, because GEPER has no phase data to support
    that inference.
    """

    # Two findings on the same chromosome at or within this many bases
    # of each other are flagged as a genomic-proximity cluster. 1kb is
    # comfortably wider than a single gene's typical exon spacing
    # (catches multiple findings in the same or adjacent exons) while
    # staying well short of "same gene" for most genes, so it adds
    # signal beyond the gene-grouping observation rather than
    # duplicating it.
    WINDOW_BP: int = int(os.environ.get("GEPER_CLUSTER_WINDOW_BP", "1000"))


@dataclass(frozen=True)
class QCReportConfig:
    """
    Configuration for the sequencing/alignment QC metrics table drawn
    by `report/summary.py`'s clinical PDF generator.

    IMPORTANT -- these are PLACEHOLDER thresholds, not clinically
    validated ones. GEPER's own pipeline consumes an already-called
    VCF, not raw FASTQ/BAM, so it cannot itself compute mean coverage
    depth, %>20x, or Q30 from its own inputs. When GEPER is run from
    the bridge's combined FASTQ->Report workflow (`bridge/
    combined_pipeline.py`), these are supplied by kim_pipeline's own
    alignment/QC stages via a `--qc-metrics-json` sidecar file (see
    `report/summary.py::_parse_qc_metrics` for the validated
    found/not-applicable/failed shape each metric is parsed into --
    never a bare number, so a placeholder value can never reach the
    PASS/WARNING comparison below by accident). When GEPER is invoked
    directly against a VCF (`main.py --vcf`, with no `--qc-metrics-json`),
    there genuinely is no run-level QC to report -- `report/summary.py`
    renders each metric "Not applicable" rather than a mock value, and
    this must never be mistaken for a real QC result either way.
    Before any diagnostic/production use, GEPER's own validation
    studies against a truth set (e.g. GIAB) must determine what PASS/
    WARNING actually means for this lab's assay and instrument -- these
    numbers are placeholders for that work, not a substitute for it.
    """

    MEAN_COVERAGE_DEPTH_PASS_MIN: float = float(os.environ.get("GEPER_QC_MEAN_COVERAGE_DEPTH_PASS_MIN", "30.0"))
    BASES_AT_20X_PASS_MIN: float = float(os.environ.get("GEPER_QC_BASES_AT_20X_PASS_MIN", "90.0"))
    Q30_SCORE_PASS_MIN: float = float(os.environ.get("GEPER_QC_Q30_SCORE_PASS_MIN", "85.0"))


@dataclass(frozen=True)
class ReportBrandingConfig:
    """
    Branding configuration for the clinical PDF report's header logo
    (`report/summary.py`). Configurable rather than hardcoded so a
    white-label deployment (e.g. a hospital supplying its own mark) can
    point at a different image, or disable the logo entirely, without a
    code change.

    LOGO_PATH defaults to GEPER's own mark (`geper/assets/logo.png`,
    resolved relative to this file's own directory so it works
    regardless of the process's current working directory). A missing,
    unreadable, or corrupt file at this path is never fatal -- see
    `report/summary.py::_build_report_header`'s docstring -- report
    generation falls back to the existing text-only header rather than
    raising.
    """

    ENABLED: bool = os.environ.get("GEPER_REPORT_LOGO_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    LOGO_PATH: str = os.environ.get(
        "GEPER_REPORT_LOGO_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "logo.png"),
    )


@dataclass(frozen=True)
class SplicingConfig:
    """
    Configuration for the new AI-model plugin manager
    (`pipeline/models/manager.py`) and its splice/regulatory model
    plugins.

    Enformer and Borzoi are both commercially-cleared (see the license
    status below), so -- exactly like every other plugin in this
    codebase (RNA-FM, HyenaDNA, MMSplice, ...) -- they default to
    ENABLED and are gated purely on whether their environment is
    actually ready (their optional pip package installed or
    auto-installable; see `is_available()` in each plugin module,
    which now mirrors `models/rna_fm.py`'s "auto-install, then check"
    pattern instead of a passive already-installed check). The flags
    below remain as an explicit *opt-out* (e.g.
    `GEPER_ENABLE_ENFORMER=false`) for a deployment that wants to
    force one of them off regardless of environment -- same knob,
    inverted default.

    License status, per model (see each plugin module for the full
    verification writeup):
      - Enformer: cleared for commercial use. Both the wrapper code
        (`enformer-pytorch`, MIT) and the official DeepMind weights
        (Apache-2.0, per Google's own Kaggle Models license field) are
        commercially usable.
      - Borzoi: cleared for commercial use ONLY via the MIT-licensed
        `johahi/borzoi-pytorch` + HuggingFace-mirrored weights path
        (`BORZOI_HF_REPO` below). Calico's original GCS `.h5`
        checkpoints have no equivalent explicit weight license and
        are never used, regardless of this flag.
      - SpliceFormer: cleared for commercial use. Both the official
        model source (vendored unmodified under
        `pipeline/models/spliceformer/vendor/`) and its pretrained
        weights are MIT licensed, per the upstream repository's own
        `LICENSE` file. See pipeline/models/spliceformer_plugin.py for
        the full verification writeup, including the note on the
        Zenodo archive's separate, archive-level CC-BY-4.0 tag.
      - SpliceBERT: cleared for commercial use. Code is BSD-3-Clause
        (repo's own LICENSE file); pretrained weights are published
        only on Zenodo and are CC-BY-4.0 there (attribution required,
        unlike SpliceFormer's weights) -- see
        pipeline/models/splicebert_plugin.py for the full
        verification writeup and why this attribution requirement is
        real here (Zenodo is the sole distribution channel for these
        weights, not just an archival mirror).
      - SPiP: cleared for commercial use (MIT, repo's own LICENSE
        file). Architecturally different from every other plugin here
        -- an R script (github.com/LBGC-CFB/SPiP), not a PyTorch
        model -- invoked as a subprocess rather than in-process; see
        pipeline/models/spip_plugin.py for the full writeup.
    """

    ENABLE_ENFORMER: bool = os.environ.get("GEPER_ENABLE_ENFORMER", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    ENABLE_BORZOI: bool = os.environ.get("GEPER_ENABLE_BORZOI", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    ENABLE_SPLICEFORMER: bool = os.environ.get("GEPER_ENABLE_SPLICEFORMER", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    ENABLE_SPLICEBERT: bool = os.environ.get("GEPER_ENABLE_SPLICEBERT", "false").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    ENABLE_SPIP: bool = os.environ.get("GEPER_ENABLE_SPIP", "true").strip().lower() not in ("0", "false", "no", "off")

    # -- Enformer --
    # `EleutherAI/enformer-official-rough` mirrors DeepMind's official
    # weights (Apache-2.0, per Google's own Kaggle Models license
    # field for the `deepmind/enformer` model -- corroborated
    # independently by third-party model documentation) in a PyTorch-
    # loadable HuggingFace repo. See pipeline/models/enformer_plugin.py
    # for the full license verification writeup.
    ENFORMER_HF_REPO: str = os.environ.get("GEPER_ENFORMER_HF_REPO", "EleutherAI/enformer-official-rough")

    # -- Borzoi --
    # MUST stay under the `johahi/` HuggingFace namespace: those are
    # the MIT-licensed PyTorch-ported weights ("ported with
    # permission" from Calico, per the peer-reviewed Flashzoi paper).
    # Calico's own original TensorFlow `.h5` checkpoints (hosted on
    # GCS) carry no equivalent explicit weight license and must NOT be
    # used here -- see pipeline/models/borzoi_plugin.py, which
    # enforces this at load time, not just by convention.
    BORZOI_HF_REPO: str = os.environ.get("GEPER_BORZOI_HF_REPO", "johahi/borzoi-replicate-0")

    # -- SpliceFormer --
    # Pinned to the v1.0.0 GitHub release tag (== the Zenodo-archived
    # release, DOI 10.5281/zenodo.14019451), not "main", so the exact
    # source/weights GEPER downloads never silently change underneath
    # a running deployment -- same rationale as ENFORMER_HF_REPO/
    # BORZOI_HF_REPO pinning a specific repo id above. See
    # pipeline/models/spliceformer/loader.py.
    SPLICEFORMER_SOURCE_REF: str = os.environ.get("GEPER_SPLICEFORMER_SOURCE_REF", "v1.0.0")
    # One of the ten official "transformer_encoder_45k_171022_*"
    # replicate checkpoints (replicate 0) shipped in the upstream
    # repository's Results/PyTorch_Models/ directory. See
    # pipeline/models/spliceformer/loader.py's module docstring for
    # why a single replicate (not the paper's full ten-replicate
    # average) is used by default, and pipeline/models/
    # spliceformer_plugin.py::SpliceFormerPlugin.metadata() for how
    # that's surfaced to a report/audit reader.
    SPLICEFORMER_CHECKPOINT: str = os.environ.get("GEPER_SPLICEFORMER_CHECKPOINT", "transformer_encoder_40k_171022_0")

    # -- SpliceBERT --
    # Weights are published only on Zenodo (no GitHub release asset),
    # pinned to a specific immutable record id rather than "latest"
    # for the same never-silently-change-underneath-a-deployment
    # reason ENFORMER_HF_REPO/SPLICEFORMER_SOURCE_REF are pinned. See
    # pipeline/models/splicebert/loader.py.
    SPLICEBERT_ZENODO_RECORD: str = os.environ.get("GEPER_SPLICEBERT_ZENODO_RECORD", "7995778")
    # One of the three checkpoints bundled in that record's
    # models.tar.gz (SpliceBERT.510nt / SpliceBERT-human.510nt /
    # SpliceBERT.1024nt) -- the 1024nt, all-vertebrate checkpoint is
    # the flagship/most general of the three. See
    # pipeline/models/splicebert/loader.py's module docstring.
    SPLICEBERT_CHECKPOINT: str = os.environ.get("GEPER_SPLICEBERT_CHECKPOINT", "SpliceBERT.1024nt")
    # Bounds `AutoModelForMaskedLM.from_pretrained`/`AutoTokenizer
    # .from_pretrained` (pipeline/models/splicebert/loader.py::
    # build_model_and_tokenizer) so a pathological `transformers`
    # TensorFlow-backend-detection path (see that function's docstring
    # -- confirmed via a real hang report to trigger whenever ANY
    # transformers-based model loads before SpliceBERT in the same
    # process, e.g. ESM2 at startup validation, which permanently caches
    # TF as "available" since tensorflow is a required MMSplice
    # dependency) fails loudly with a clear, actionable error instead of
    # hanging silently -- observed as long as 2+ hours in one real run.
    SPLICEBERT_LOAD_TIMEOUT_SECS: float = float(os.environ.get("GEPER_SPLICEBERT_LOAD_TIMEOUT_SECS", "180"))

    # -- SPiP --
    # Genome build SPiP resolves variants/transcripts against --
    # SPiPv2.1_main.r itself defaults to hg19 and supports only hg19
    # or hg38 (see pipeline/models/spip/loader.py).
    SPIP_GENOME_ASSEMBLY: str = os.environ.get("GEPER_SPIP_GENOME_ASSEMBLY", "hg19")
    # SPiP has no persistent process to warm up -- every call is a
    # fresh Rscript invocation that reloads its ~400MB combined
    # reference-data set from disk, so this needs real headroom (well
    # beyond every other plugin's own per-call cost). See
    # pipeline/models/spip/loader.py::run_spip's own docstring.
    SPIP_TIMEOUT_SECONDS: int = int(os.environ.get("GEPER_SPIP_TIMEOUT_SECONDS", "600"))

    # Directory the new plugin weight cache (pipeline/models/cache.py)
    # uses -- kept as its own named subdirectory rather than merged
    # flat into CONFIG.CACHE_DIR (which predates this and is used by
    # BLAST/gnomAD/ClinGen result caches) so plugin weight files
    # (Enformer/Borzoi/SpliceFormer/SpliceBERT, multiple GB) are still
    # easy to find/clear independently of the smaller bootstrapped-
    # dataset caches -- while still living *under* GEPER_CACHE_DIR by
    # default (F2, report review round 4) so pointing GEPER_CACHE_DIR
    # at persistent storage (e.g. a mounted Drive) carries plugin
    # weights along with everything else, instead of leaving ~multi-GB
    # downloads stranded in container-local storage every fresh
    # session. An explicit GEPER_PLUGIN_CACHE_DIR always wins
    # (unchanged); when NEITHER env var is set this stays the original
    # "./plugin_model_cache" default, so a local user with no Drive
    # sees no behavior change and already-downloaded weights there are
    # still found.
    PLUGIN_CACHE_DIR: str = os.environ.get(
        "GEPER_PLUGIN_CACHE_DIR",
        os.path.join(os.environ["GEPER_CACHE_DIR"], "plugin_model_cache")
        if os.environ.get("GEPER_CACHE_DIR")
        else "./plugin_model_cache",
    )


@dataclass(frozen=True)
class GeperConfig:
    models: ModelConfig = field(default_factory=ModelConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    api: APIConfig = field(default_factory=APIConfig)
    alphamissense: AlphaMissenseConfig = field(default_factory=AlphaMissenseConfig)
    mmsplice: MMSpliceConfig = field(default_factory=MMSpliceConfig)
    gnomad: GnomadConfig = field(default_factory=GnomadConfig)
    indigenomes: IndiGenomesConfig = field(default_factory=IndiGenomesConfig)
    thousand_genomes_sas: ThousandGenomesSASConfig = field(default_factory=ThousandGenomesSASConfig)
    conservation: ConservationConfig = field(default_factory=ConservationConfig)
    clingen: ClinGenConfig = field(default_factory=ClinGenConfig)
    mane: MANEConfig = field(default_factory=MANEConfig)
    ensembl: EnsemblConfig = field(default_factory=EnsemblConfig)
    hpo: HPOConfig = field(default_factory=HPOConfig)
    orphanet: OrphanetConfig = field(default_factory=OrphanetConfig)
    functional_evidence: FunctionalEvidenceConfig = field(default_factory=FunctionalEvidenceConfig)
    pvs1: PVS1Config = field(default_factory=PVS1Config)
    ps1_pm5: PS1PM5Config = field(default_factory=PS1PM5Config)
    uniprot: UniProtConfig = field(default_factory=UniProtConfig)
    interpro: InterProConfig = field(default_factory=InterProConfig)
    alphafold: AlphaFoldConfig = field(default_factory=AlphaFoldConfig)
    confidence: ConfidenceConfig = field(default_factory=ConfidenceConfig)
    prioritization: PrioritizationConfig = field(default_factory=PrioritizationConfig)
    case_prioritization: CasePrioritizationConfig = field(default_factory=CasePrioritizationConfig)
    conflict: ConflictConfig = field(default_factory=ConflictConfig)
    splicing: SplicingConfig = field(default_factory=SplicingConfig)
    qc_report: QCReportConfig = field(default_factory=QCReportConfig)
    variant_clustering: VariantClusteringConfig = field(default_factory=VariantClusteringConfig)
    report_branding: ReportBrandingConfig = field(default_factory=ReportBrandingConfig)
    normalization: VariantNormalizationConfig = field(default_factory=VariantNormalizationConfig)
    health_check: HealthCheckConfig = field(default_factory=HealthCheckConfig)

    OUTPUT_DIR: str = os.environ.get("GEPER_OUTPUT_DIR", "./geper_output")
    CACHE_DIR: str = os.environ.get("GEPER_CACHE_DIR", "./model_cache")

    # How often (in variants) the orchestrator flushes geper_results.json
    # to disk mid-run. Small enough to survive a Colab disconnect without
    # losing much work, large enough not to dominate runtime with I/O.
    CHECKPOINT_INTERVAL: int = int(os.environ.get("GEPER_CHECKPOINT_INTERVAL", "25"))

    # AI-only mode: skip BLAST entirely (no NCBI submission, no local
    # blastn subprocess, no disk-cache lookups) and rely solely on the
    # DNA/RNA/protein foundation models + ClinVar/dbSNP/AlphaMissense
    # for interpretation. Useful when BLAST's remote-queue latency
    # isn't acceptable and no local BLAST+ database is available.
    AI_ONLY_MODE: bool = os.environ.get("GEPER_AI_ONLY", "false").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    # Per-stage wall-clock profiling (sequence context, each model,
    # BLAST, ClinVar, dbSNP). Cheap (a handful of time.perf_counter()
    # calls per variant) and on by default; the resulting benchmark is
    # written to `geper_benchmark.md` / `.json` in the output directory
    # at the end of every run. Never changes any prediction output.
    ENABLE_PROFILING: bool = os.environ.get("GEPER_ENABLE_PROFILING", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    # Codon table used by ProteinTranslator (standard genetic code).
    CODON_TABLE: Dict[str, str] = field(default_factory=lambda: _STANDARD_CODON_TABLE)


_STANDARD_CODON_TABLE: Dict[str, str] = {
    "UUU": "F",
    "UUC": "F",
    "UUA": "L",
    "UUG": "L",
    "CUU": "L",
    "CUC": "L",
    "CUA": "L",
    "CUG": "L",
    "AUU": "I",
    "AUC": "I",
    "AUA": "I",
    "AUG": "M",
    "GUU": "V",
    "GUC": "V",
    "GUA": "V",
    "GUG": "V",
    "UCU": "S",
    "UCC": "S",
    "UCA": "S",
    "UCG": "S",
    "CCU": "P",
    "CCC": "P",
    "CCA": "P",
    "CCG": "P",
    "ACU": "T",
    "ACC": "T",
    "ACA": "T",
    "ACG": "T",
    "GCU": "A",
    "GCC": "A",
    "GCA": "A",
    "GCG": "A",
    "UAU": "Y",
    "UAC": "Y",
    "UAA": "*",
    "UAG": "*",
    "CAU": "H",
    "CAC": "H",
    "CAA": "Q",
    "CAG": "Q",
    "AAU": "N",
    "AAC": "N",
    "AAA": "K",
    "AAG": "K",
    "GAU": "D",
    "GAC": "D",
    "GAA": "E",
    "GAG": "E",
    "UGU": "C",
    "UGC": "C",
    "UGA": "*",
    "UGG": "W",
    "CGU": "R",
    "CGC": "R",
    "CGA": "R",
    "CGG": "R",
    "AGU": "S",
    "AGC": "S",
    "AGA": "R",
    "AGG": "R",
    "GGU": "G",
    "GGC": "G",
    "GGA": "G",
    "GGG": "G",
}

CONFIG = GeperConfig()

# RNA-FM (models/rna_fm.py) caches its checkpoint under `torch.hub`'s
# own directory (`torch.hub.get_dir()`), which PyTorch resolves from
# the `TORCH_HOME` env var (falling back to `~/.cache/torch`) --
# entirely independent of GEPER_CACHE_DIR, unlike every other
# bootstrapped/model cache in this file. Setting `TORCH_HOME` here
# (module import time, before anything can call `torch.hub.get_dir()`)
# is the only way to redirect it, since RNA-FM never reads a
# GEPER-specific config value for its cache location (F2, report
# review round 4). Same guard as HYENADNA_CHECKPOINT_DIR/
# PLUGIN_CACHE_DIR above: only takes effect when GEPER_CACHE_DIR is
# explicitly set, and never overrides an explicit TORCH_HOME the
# deployer already set for their own reasons -- a local user with
# neither set keeps PyTorch's own default, unchanged.
if os.environ.get("GEPER_CACHE_DIR") and not os.environ.get("TORCH_HOME"):
    os.environ["TORCH_HOME"] = os.path.join(os.environ["GEPER_CACHE_DIR"], "torch")
