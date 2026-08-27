"""
AlphaMissense wrapper.

WHY THIS ISN'T A LOADED NEURAL NETWORK (read before "fixing" this)
--------------------------------------------------------------------
Every other model in `models/` loads real weights and runs a forward
pass. AlphaMissense cannot work that way: Google DeepMind has
explicitly not released trained AlphaMissense model weights ("What we
don't provide: The trained AlphaMissense model weights" --
github.com/google-deepmind/alphamissense). What they DO provide is a
precomputed, genome-coordinate-keyed catalogue of pathogenicity scores
for essentially every possible human missense substitution (~71M rows,
hg19 and hg38), distributed as a bgzip'd, tabix-indexed TSV. This is
also exactly how every real annotation pipeline integrates
AlphaMissense in practice -- the official Ensembl VEP plugin queries
that same tabix-indexed file rather than running a model.

GEPER therefore integrates AlphaMissense as an indexed lookup against
that catalogue (matched by chrom/pos/ref/alt, via the `tabix` CLI)
instead of as a loaded network -- but it still participates in every
piece of GEPER's model lifecycle machinery (BaseGenomicModel,
ModelCache, startup validation, availability/graceful-degradation,
run summary) exactly like HyenaDNA/ESM-2/etc do, since that machinery
doesn't actually assume "model" means "torch.nn.Module with weights"
anywhere -- it only assumes cache_key()/_load_impl()/_infer_impl().

"Load once, cache" for a lookup table means: resolve which catalogue
file to use for the build actually being queried, downloading and
locally tabix-indexing it exactly once per process if it isn't cached
on disk yet already (via ModelCache for the in-process handle, plus an
on-disk cache under config.py::AlphaMissenseConfig.CACHE_SUBDIR so a
Colab restart doesn't repeat the download). This is NOT indexed HTTP
range requests against the remote file: Google's GCS bucket does not
publish a `.tbi` sidecar alongside the `.tsv.gz` files (confirmed
against the Ensembl VEP plugin docs and multiple independent reports
of the same "must download + index locally" requirement), so `tabix
<url> region` fails outright without ever downloading anything. Every
real integration of this catalogue (VEP's own plugin instructions)
downloads the file and runs `tabix -s 1 -b 2 -e 2 -f -S 1` locally
first -- GEPER does the same, once, automatically.

Device/precision: there is no tensor computation here at all (no GPU
is ever used, regardless of what's available), so `self.device` is
pinned to CPU and `_report_precision()` is overridden to say so
plainly in the startup validation table, rather than reporting a
torch dtype that doesn't apply.

LICENSING: see the long comment on config.py::AlphaMissenseConfig for
the full picture (including what CC BY 4.0 does NOT settle). Short
version: the predictions catalogue this module downloads from is
CC BY 4.0, confirmed directly against the GCS bucket's own README.pdf
at the exact URL this module downloads from (config.py's HG38_URL/
HG19_URL) -- not inferred from a third-party mirror or an old VEP
plugin tag. This does NOT by itself resolve whether CC BY 4.0's
attribution condition is triggered by extracting a single row from
the ~71M-row catalogue -- that stays a separate, still-open question.
"""

import os
import shutil
import subprocess
import threading
from typing import Any, Dict, Optional

import requests
import torch

from config import CONFIG
from models.base_model import BaseGenomicModel
from pipeline.provenance import write_dataset_provenance_sidecar
from utils.auto_install import ensure_system_binary_available
from utils.exceptions import ModelInferenceError, ModelLoadError
from utils.logger import get_logger

logger = get_logger(__name__)

# Per-process cache of the auto-install outcome, so a genuinely
# unfixable environment (no apt-get, no network) fails fast on every
# subsequent is_available()/_load_impl() call rather than
# re-attempting a doomed apt-get on every variant that routes here --
# same shape as utils/auto_install.py's own _INSTALL_RESULTS and
# models/hyenadna.py's _AUTO_INSTALL_RESULT.
_TABIX_APT_PACKAGE = "tabix"  # Debian/Ubuntu package providing the tabix + bgzip binaries


def ensure_tabix_available() -> bool:
    """
    Idempotent, automatic setup entry point used by `is_available()`
    and `_load_impl()`, mirroring `models/hyenadna.py`'s
    `ensure_hyenadna_available()` and `utils/auto_install.py`'s
    `ensure_pip_package_available()`: AlphaMissense previously
    required a manual `apt-get install tabix` (or `conda install`)
    step before this stage would ever run -- every fresh Colab
    runtime lacks `tabix` by default, so without this it silently
    skipped for the entire run (as seen in production: "AlphaMissense
    ... will be skipped ... SKIP (not installed)"), unlike every other
    optional GEPER dependency (HyenaDNA's source, RNA-FM/BLAST's pip
    packages), which all auto-install themselves. This closes that
    gap the same way, via `apt-get`, so no manual setup step is needed
    here either.

    Only attempts to auto-install the *default* 'tabix' binary name;
    if `GEPER_TABIX_BINARY` has been overridden to a custom path/name,
    apt has no way to know what that should resolve to, so auto-install
    is skipped and the configured binary is simply checked for as-is
    (the pre-existing behavior for that case is unchanged).
    """
    binary = CONFIG.alphamissense.TABIX_BINARY
    if binary != "tabix":
        return shutil.which(binary) is not None
    return ensure_system_binary_available(binary, apt_package=_TABIX_APT_PACKAGE)


# Per-build (hg19/hg38) cache of the resolved *local* catalogue path, so
# concurrent/repeated queries for the same build within one process
# don't re-check-or-re-download; keyed and guarded the same "cheap
# check, do-once, cache the outcome" shape as utils/auto_install.py.
_RESOLVED_LOCAL_PATH: Dict[str, str] = {}
_DOWNLOAD_LOCK = threading.Lock()


_EXPECTED_MIN_COLUMNS = (
    10  # CHROM POS REF ALT genome uniprot_id transcript_id protein_variant am_pathogenicity am_class
)


def _normalize_chrom_for_catalogue(chrom: str) -> str:
    """
    The AlphaMissense catalogue always uses 'chr<N>' contig names
    (chr1 .. chr22, chrX, chrY, chrM), regardless of how the input
    VCF spelled its CHROM column. Normalize defensively in both
    directions rather than assuming the VCF matches.
    """
    stripped = chrom.replace("chr", "").replace("Chr", "").replace("CHR", "")
    if stripped in ("M", "mt", "Mt", "MT"):
        stripped = "M"
    return f"chr{stripped}"


def resolve_genome_label(assembly: Optional[str]) -> str:
    """
    Map a resolved GEPER genome build string to the catalogue's file
    naming ('hg19' vs 'hg38'). Mirrors ClinVarClient._position_field /
    DbSNPClient._position_field's exact normalization and fallback
    convention (default to the current build, hg38, when unresolved)
    for consistency across every stage that's build-sensitive.
    """
    if assembly and assembly.strip().upper().replace("-", "").replace("_", "") in {
        "GRCH37",
        "HG19",
        "B37",
    }:
        return "hg19"
    return "hg38"


def _is_url(source: str) -> bool:
    return source.startswith("http://") or source.startswith("https://")


def _run_tabix_index(tabix_binary: str, local_tsv_gz: str) -> None:
    """
    Build a local `.tbi` for a freshly-downloaded AlphaMissense catalogue
    file, using the exact indexing invocation documented by the Ensembl
    VEP AlphaMissense plugin (genomic coordinate in column 1, begin/end
    both in column 2, one header line to skip): `tabix -s 1 -b 2 -e 2
    -f -S 1 <file>`. Raises ModelInferenceError on failure.
    """
    command = [tabix_binary, "-s", "1", "-b", "2", "-e", "2", "-f", "-S", "1", local_tsv_gz]
    proc = subprocess.run(command, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ModelInferenceError(
            f"Building a local tabix index for '{local_tsv_gz}' failed "
            f"(tabix exit {proc.returncode}): {(proc.stderr or '').strip()[:1000]}"
        )


def catalogue_cache_path(build: str) -> str:
    """Where the auto-downloaded catalogue for `build` ("hg38"/"hg19")
    is cached -- public so `pipeline/provenance.py`/
    `pipeline/orchestrator.py` can read its provenance sidecar without
    triggering the (~9GB) download."""
    cache_dir = os.path.join(CONFIG.CACHE_DIR, CONFIG.alphamissense.CACHE_SUBDIR)
    return os.path.join(cache_dir, f"AlphaMissense_{build}.tsv.gz")


def _download_catalogue(url: str, dest_path: str) -> None:
    """
    Stream-download the (~9GB) catalogue file to `dest_path`, via a
    `.part` temp file that's only renamed into place on full success --
    so a Colab disconnect or Ctrl-C mid-download leaves no file that a
    later run could mistake for a complete, valid catalogue.
    """
    part_path = dest_path + ".part"
    try:
        with requests.get(url, stream=True, timeout=60) as response:
            response.raise_for_status()
            headers = dict(response.headers)
            total_bytes = int(response.headers.get("Content-Length", 0))
            downloaded = 0
            next_log_at = 0
            with open(part_path, "wb") as fh:
                for chunk in response.iter_content(chunk_size=8 * 1024 * 1024):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if total_bytes and downloaded >= next_log_at:
                        pct = 100 * downloaded / total_bytes
                        logger.info(
                            f"Downloading '{url}': {downloaded / 1e9:.2f}GB / {total_bytes / 1e9:.2f}GB ({pct:.0f}%)."
                        )
                        next_log_at = downloaded + max(total_bytes // 20, 200 * 1024 * 1024)
        os.replace(part_path, dest_path)
        # Provenance sidecar (pipeline/provenance.py). The GCS-hosted
        # catalogue's `ETag` is literally the object's own MD5 (verified
        # live: a 32-hex-char value, same shape as `x-goog-hash`'s `md5=`
        # component) -- a precise, server-authoritative content
        # identifier used directly rather than sha256-hashing this ~9GB
        # file ourselves, which would add real, avoidable cost on every
        # (re)download for no extra confidence.
        write_dataset_provenance_sidecar(
            dest_path,
            url,
            response_headers=headers,
            content_hash=(headers.get("ETag") or "").strip('"') or None,
            hash_algorithm="gcs-etag-md5" if headers.get("ETag") else None,
            compute_hash_from_file=False,
        )
    except Exception as exc:
        if os.path.exists(part_path):
            os.remove(part_path)
        raise ModelInferenceError(
            f"Downloading the AlphaMissense catalogue from '{url}' to '{dest_path}' failed: {exc}"
        ) from exc


def _ensure_local_catalogue(configured_source: str, build: str, tabix_binary: str) -> str:
    """
    Resolve `configured_source` (either a local path from
    GEPER_ALPHAMISSENSE_{HG38,HG19}_LOCAL, or the default remote GCS
    URL) to a local `.tsv.gz` path with a sibling `.tbi` guaranteed to
    exist, downloading + indexing it once if necessary.

    IMPORTANT: Google's GCS bucket does not publish a `.tbi` sidecar
    next to the raw `.tsv.gz` files, so `tabix <url> region` cannot do
    indexed HTTP range queries the way e.g. gnomAD's GCS-hosted VCFs
    can -- it fails immediately, before touching the network for the
    actual data. This mirrors what every other real integration of
    this catalogue does (the Ensembl VEP plugin instructions, several
    independent bioinformatics forum threads): download the file, then
    build the index locally with `tabix -s 1 -b 2 -e 2 -f -S 1`.
    """
    if not _is_url(configured_source):
        # A user-provided local path -- trust it's already indexed
        # (that's the documented contract for GEPER_ALPHAMISSENSE_*_LOCAL),
        # but fail with a clear, actionable message rather than a
        # confusing tabix error if it isn't.
        if not os.path.exists(configured_source):
            raise ModelInferenceError(f"Configured local AlphaMissense catalogue '{configured_source}' does not exist.")
        if not os.path.exists(configured_source + ".tbi"):
            raise ModelInferenceError(
                f"Configured local AlphaMissense catalogue "
                f"'{configured_source}' has no sibling '.tbi' index. Build "
                f"one with: {tabix_binary} -s 1 -b 2 -e 2 -f -S 1 "
                f"'{configured_source}'"
            )
        return configured_source

    cached = _RESOLVED_LOCAL_PATH.get(build)
    if cached is not None:
        return cached

    if not CONFIG.alphamissense.AUTO_DOWNLOAD:
        raise ModelInferenceError(
            "AlphaMissense has no local catalogue configured and "
            "GEPER_ALPHAMISSENSE_AUTO_DOWNLOAD=false, so there is no "
            "'.tbi'-indexed file to query (the remote GCS URL alone "
            "cannot be queried directly -- see models/alphamissense.py "
            "module docstring). Either unset "
            "GEPER_ALPHAMISSENSE_AUTO_DOWNLOAD or set "
            f"GEPER_ALPHAMISSENSE_{build.upper()}_LOCAL to a pre-downloaded, "
            "pre-indexed copy."
        )

    cache_dir = os.path.join(CONFIG.CACHE_DIR, CONFIG.alphamissense.CACHE_SUBDIR)
    os.makedirs(cache_dir, exist_ok=True)
    local_path = catalogue_cache_path(build)

    with _DOWNLOAD_LOCK:
        # Re-check after acquiring the lock: another thread may have
        # just finished downloading this same build while we waited.
        cached = _RESOLVED_LOCAL_PATH.get(build)
        if cached is not None:
            return cached

        if os.path.exists(local_path) and os.path.exists(local_path + ".tbi"):
            logger.info(f"Found existing cached AlphaMissense catalogue at '{local_path}'.")
        else:
            logger.info(
                f"No local, tabix-indexed AlphaMissense '{build}' catalogue "
                f"found. Downloading it once now from '{configured_source}' "
                f"to '{local_path}' (~9GB; this is a one-time cost -- cached "
                "under GEPER_CACHE_DIR for subsequent runs)."
            )
            _download_catalogue(configured_source, local_path)
            logger.info(f"Download complete. Building local tabix index for '{local_path}'...")
            _run_tabix_index(tabix_binary, local_path)
            logger.info(f"AlphaMissense '{build}' catalogue ready at '{local_path}'.")

        _RESOLVED_LOCAL_PATH[build] = local_path
        return local_path


class _AlphaMissenseCatalogue:
    """
    Thin handle around the (possibly remote) tabix-indexed AlphaMissense
    catalogue files. This -- not a torch.nn.Module -- is what
    AlphaMissenseModel stores as `self.model`, since there are no
    learned weights to hold. It exposes `named_parameters()` /
    `named_buffers()` as empty iterators purely so
    BaseGenomicModel._verify_materialized() (which walks every loaded
    model's parameters/buffers looking for stray meta-device tensors)
    continues to work unmodified against this model too, without
    needing any special-casing in base_model.py.
    """

    def __init__(self, hg38_source: str, hg19_source: str, tabix_binary: str):
        self.hg38_source = hg38_source
        self.hg19_source = hg19_source
        self.tabix_binary = tabix_binary

    # -- duck-typing for BaseGenomicModel._verify_materialized() -----
    @staticmethod
    def named_parameters():
        return iter(())

    @staticmethod
    def named_buffers():
        return iter(())

    def _source_for(self, genome_label: str) -> str:
        configured = self.hg38_source if genome_label == "hg38" else self.hg19_source
        return _ensure_local_catalogue(configured, genome_label, self.tabix_binary)

    def query(self, chrom: str, pos: int, ref: str, alt: str, genome_label: str) -> Optional[Dict[str, Any]]:
        """
        Look up (chrom, pos, ref, alt) in the catalogue and return the
        matching row's fields, or None if the position is present but
        no row matches this exact ref/alt (e.g. a different allele at
        the same site), or the position simply isn't in the catalogue
        (e.g. it's outside any canonical transcript's coding region).

        Raises ModelInferenceError for genuine query failures (tabix
        missing/crashed, the one-time download/index-build failing) --
        the orchestrator's AlphaMissense stage catches this and degrades
        gracefully per variant, exactly like a failed ClinVar/dbSNP call.
        """
        source = self._source_for(genome_label)
        region = f"{_normalize_chrom_for_catalogue(chrom)}:{pos}-{pos}"
        command = [self.tabix_binary, source, region]
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=CONFIG.alphamissense.QUERY_TIMEOUT_SECS,
            )
        except FileNotFoundError as exc:
            raise ModelInferenceError(
                f"'{self.tabix_binary}' is not available on PATH (it was present at load time but is missing now)."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ModelInferenceError(
                f"AlphaMissense catalogue query for '{region}' against the "
                f"local file '{source}' timed out after "
                f"{CONFIG.alphamissense.QUERY_TIMEOUT_SECS}s (unexpected for "
                "a local-file query -- check disk I/O)."
            ) from exc

        if proc.returncode != 0:
            raise ModelInferenceError(
                f"AlphaMissense catalogue query for '{region}' against "
                f"'{source}' failed (tabix exit {proc.returncode}): "
                f"{(proc.stderr or '').strip()[:1000]}"
            )

        for line in proc.stdout.splitlines():
            if not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) < _EXPECTED_MIN_COLUMNS:
                continue
            (
                f_chrom,
                f_pos,
                f_ref,
                f_alt,
                f_genome,
                f_uniprot,
                f_transcript,
                f_protein_variant,
                f_am_path,
                f_am_class,
            ) = fields[:_EXPECTED_MIN_COLUMNS]
            if f_ref.upper() == ref.upper() and f_alt.upper() == alt.upper():
                try:
                    pathogenicity = float(f_am_path)
                except ValueError:
                    pathogenicity = None
                return {
                    "chrom": f_chrom,
                    "pos": int(f_pos) if f_pos.isdigit() else pos,
                    "ref": f_ref,
                    "alt": f_alt,
                    "genome": f_genome,
                    "uniprot_id": f_uniprot,
                    "transcript_id": f_transcript,
                    "protein_variant": f_protein_variant,
                    "am_pathogenicity": pathogenicity,
                    "am_class": f_am_class,
                }
        return None


class AlphaMissenseModel(BaseGenomicModel):
    """
    Missense-variant pathogenicity lookup against the AlphaMissense
    precomputed catalogue. See the module docstring for why this is an
    indexed lookup rather than a loaded network.

    Inference contract (unlike the embedding models): `predict()` is
    called with a synthetic lookup key string `"chrom:pos:ref:alt"`
    (not a biological sequence) plus an optional `assembly=` kwarg --
    reusing BaseGenomicModel.predict()'s existing `sequence: str`
    signature exactly as-is (see `_infer_impl` below) rather than
    changing that shared contract for one model.
    """

    def __init__(self):
        super().__init__()
        # No GPU work happens here under any circumstances -- pin to
        # CPU explicitly rather than reporting whatever get_device()
        # resolved for the tensor-based models, which would be
        # misleading in the startup validation table.
        self.device = torch.device("cpu")

    def cache_key(self) -> str:
        return "alphamissense"

    @classmethod
    def is_available(cls) -> bool:
        """
        Confirms the stage *could* run: config enabled, and the
        `tabix` binary is on PATH -- auto-installing it via `apt-get`
        first if it isn't (see `ensure_tabix_available()` above), the
        same "auto-install, then check" shape every other optional
        GEPER dependency already uses (HyenaDNA's git checkout,
        RNA-FM/BLAST's pip packages). This does touch the network the
        first time it's called in a process (an `apt-get` run) if
        `tabix` is genuinely missing -- but only once, ever, per
        process (cached in `utils.auto_install`'s install-result
        cache), not per variant/call. Catalogue reachability itself is
        still confirmed once at load time (see `_load_impl`), and a
        failure there demotes availability for the rest of the run
        through the same mechanism already used for every other model.
        """
        if not CONFIG.alphamissense.ENABLED:
            return False
        return ensure_tabix_available()

    def _load_impl(self):
        if not CONFIG.alphamissense.ENABLED:
            # Reachable if _load_impl is ever called directly, bypassing
            # is_available() -- keep the failure mode explicit rather
            # than silently proceeding.
            raise ModelLoadError("AlphaMissense is disabled via configuration (GEPER_ENABLE_ALPHAMISSENSE=false).")

        tabix_binary = CONFIG.alphamissense.TABIX_BINARY
        if not ensure_tabix_available():
            raise ModelLoadError(
                f"AlphaMissense requires the '{tabix_binary}' CLI tool "
                "(htslib) to query the precomputed score catalogue, and "
                "it could not be found or automatically installed on "
                "this system. Install it with 'apt-get install tabix' "
                "or 'conda install -c bioconda htslib', or set "
                "GEPER_TABIX_BINARY to its full path."
            )

        hg38_source = self._resolve_source("hg38")
        hg19_source = self._resolve_source("hg19")
        self.model = _AlphaMissenseCatalogue(
            hg38_source=hg38_source, hg19_source=hg19_source, tabix_binary=tabix_binary
        )
        self.tokenizer = None
        logger.info(
            f"AlphaMissense catalogue resolved -- hg38: '{hg38_source}', "
            f"hg19: '{hg19_source}' (tabix binary: '{tabix_binary}')."
        )

    @staticmethod
    def _resolve_source(build: str) -> str:
        """
        Local pre-downloaded copy takes priority over the remote URL
        (see config.py::AlphaMissenseConfig -- air-gapped/high-throughput
        deployments). Falls back to the remote GCS URL, which `tabix`
        queries via indexed HTTP range requests rather than a full
        download.
        """
        cfg = CONFIG.alphamissense
        if build == "hg38":
            return cfg.LOCAL_HG38_PATH if cfg.LOCAL_HG38_PATH else cfg.HG38_URL
        return cfg.LOCAL_HG19_PATH if cfg.LOCAL_HG19_PATH else cfg.HG19_URL

    def _report_precision(self) -> str:
        # Overrides BaseGenomicModel._report_precision(), which reports
        # a torch dtype -- meaningless here since no tensor/model
        # weights are ever loaded. Explicit "n/a" reads more honestly
        # in the startup validation table than a fabricated dtype.
        return "n/a (lookup table, no weights)"

    def _infer_impl(self, sequence: str, **kwargs) -> Dict[str, Any]:
        """
        `sequence` here is the lookup key "chrom:pos:ref:alt" (see the
        class docstring for why this reuses BaseGenomicModel's string
        contract rather than a new one). `assembly` (e.g. "GRCh38" /
        "GRCh37") is accepted as a kwarg the same way the ClinVar/dbSNP
        stages already thread it through from the orchestrator.
        """
        parts = sequence.split(":")
        if len(parts) != 4:
            raise ModelInferenceError(f"AlphaMissense expected a 'chrom:pos:ref:alt' lookup key, got '{sequence}'.")
        chrom, pos_str, ref, alt = parts
        try:
            pos = int(pos_str)
        except ValueError as exc:
            raise ModelInferenceError(f"AlphaMissense lookup key '{sequence}' has a non-integer position.") from exc

        genome_label = resolve_genome_label(kwargs.get("assembly"))
        match = self.model.query(chrom, pos, ref, alt, genome_label)

        if match is None:
            return {
                "found": False,
                "genome": genome_label,
                "am_pathogenicity": None,
                "am_class": None,
                "uniprot_id": None,
                "transcript_id": None,
                "protein_variant": None,
            }

        return {
            "found": True,
            "genome": match["genome"] or genome_label,
            "am_pathogenicity": match["am_pathogenicity"],
            "am_class": match["am_class"],
            "uniprot_id": match["uniprot_id"],
            "transcript_id": match["transcript_id"],
            "protein_variant": match["protein_variant"],
        }
