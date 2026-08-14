"""
Loader for SPiP: locating an R interpreter, ensuring its CRAN package
dependencies, assembling a runtime working directory (vendored R
source + downloaded reference data, see this package's own
`__init__.py`), and invoking it as a subprocess against a one-variant
VCF.

Distinct from `pipeline.models.cache.WeightCache` (the generic
on-disk cache every plugin shares) the same way
`pipeline/models/spliceformer/loader.py` is: this module is about
*getting SPiP ready to run and running it*; `cache.py` is about
*where its downloaded reference-data files live on disk*.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)

REQUIRED_R_PACKAGES = ["foreach", "doParallel", "randomForest"]

DEFAULT_GENOME = "hg19"

_VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
_MAIN_SCRIPT_NAME = "SPiPv2.1_main.r"
# GEPER-authored driver, NOT part of the upstream vendored source --
# see its own header comment for exactly why it exists (a confirmed
# Windows-specific bug in SPiPv2.1_main.r's own %dopar% scoring loop:
# doParallel's PSOCK cluster workers don't inherit globals like
# `fileFormat` that SPiP_functions.r's scoring functions need, so
# running the upstream script as documented silently produces an
# all-NA/SPiPscore=-1 result for every variant on Windows). Reuses the
# same unmodified RefFiles/SPiP_libs/*.r functions directly, serially,
# in-process -- no parallel cluster, nothing to lose for GEPER's
# always-exactly-one-variant-per-call usage.
_DRIVER_SCRIPT_NAME = "geper_spip_driver.r"

# model.RData/RefFiles.RData are small enough to fetch straight from
# the upstream GitHub repo's own raw content host (same as any other
# source file); dataRefSeq<genome>.RData too. transcriptome_<genome>.RData
# is NOT in the git repo at all -- SPiPv2.1_main.r's own startup check
# (see its "Check transcriptome sequences..." block) points users at
# a separate SourceForge-hosted download for it, confirmed by reading
# that exact URL out of the script's own source rather than guessed.
_GITHUB_RAW_BASE = "https://raw.githubusercontent.com/LBGC-CFB/SPiP/master/"
_SOURCEFORGE_TRANSCRIPTOME_TEMPLATE = (
    "https://sourceforge.net/projects/splicing-prediction-pipeline/"
    "files/transcriptome/transcriptome_{genome}.RData/download"
)

# Filename -> where to fetch it from (relative to _GITHUB_RAW_BASE),
# always placed at runtime_dir/RefFiles/<filename>. Genome-independent.
_GITHUB_REFDATA_FILES: Dict[str, str] = {
    "model.RData": "RefFiles/model.RData",
    "RefFiles.RData": "RefFiles/RefFiles.RData",
}


def find_rscript() -> Optional[str]:
    """Locates an `Rscript` executable: PATH first, then the standard
    Windows per-machine install directories (winget/the official R
    for Windows installer both install under `Program Files`, and
    neither adds itself to PATH by default)."""
    on_path = shutil.which("Rscript")
    if on_path:
        return on_path
    for base in (r"C:\Program Files\R", r"C:\Program Files (x86)\R"):
        base_path = Path(base)
        if not base_path.is_dir():
            continue
        for version_dir in sorted(base_path.iterdir(), reverse=True):
            candidate = version_dir / "bin" / "Rscript.exe"
            if candidate.is_file():
                return str(candidate)
    return None


def _run_r_expr(rscript_path: str, expr: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run([rscript_path, "-e", expr], capture_output=True, text=True, timeout=timeout)


def is_r_package_installed(rscript_path: str, package: str) -> bool:
    result = _run_r_expr(rscript_path, f'cat(requireNamespace("{package}", quietly=TRUE))', timeout=60)
    return result.stdout.strip() == "TRUE"


def ensure_r_packages(rscript_path: str, packages: List[str] = REQUIRED_R_PACKAGES) -> bool:
    """
    Idempotent, automatic setup mirroring
    `utils.auto_install.ensure_pip_package_available`'s "cheap check,
    install if missing" shape, for R packages instead of pip ones.

    Installs into R's own per-user library directory
    (`Sys.getenv("R_LIBS_USER")`), created if it doesn't exist yet --
    NOT the default `.libPaths()[1]` (R's system-wide library under
    the R installation itself), which a non-admin install (e.g. via
    winget, as this environment's own R was installed) has no write
    access to. R only auto-adds `R_LIBS_USER` to `.libPaths()` for a
    given process if the directory already exists at that process's
    startup -- creating it once here is therefore sufficient for every
    later `Rscript` invocation (including the real SPiP run in
    `run_spip` below) to pick it up automatically, with no need to
    pass an explicit `lib=`/`.libPaths()` override at every call site.
    """
    missing = [p for p in packages if not is_r_package_installed(rscript_path, p)]
    if not missing:
        return True

    pkgs_r_vector = "c(" + ",".join(f'"{p}"' for p in missing) + ")"
    expr = (
        'userlib <- Sys.getenv("R_LIBS_USER"); '
        "dir.create(userlib, recursive=TRUE, showWarnings=FALSE); "
        f"install.packages({pkgs_r_vector}, lib=userlib, repos='https://cloud.r-project.org')"
    )
    logger.info(f"Installing missing R package(s) for SPiP: {missing}")
    _run_r_expr(rscript_path, expr, timeout=600)
    return all(is_r_package_installed(rscript_path, p) for p in missing)


def _download_file(url: str, dest_path: Path) -> None:
    import requests

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_name(dest_path.name + ".part")
    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with open(tmp_path, "wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)
    tmp_path.replace(dest_path)


def prepare_runtime_dir(cache_dir: Path, genome: str = DEFAULT_GENOME) -> Path:
    """
    Assembles `cache_dir/runtime/`: a copy of the vendored R source
    (`vendor/`, this package's own `__init__.py` explains why it's a
    copy rather than running in place -- keeps the git-tracked vendor/
    tree pristine, downloaded data never gets mixed into source
    control, same separation `pipeline.models.cache.WeightCache`
    already enforces for every other plugin) plus the downloaded
    reference-data files SPiPv2.1_main.r hardcodes as living in
    `<script_dir>/RefFiles/` relative to wherever it's invoked from
    (confirmed by reading its own `scriptPath`/`inputref` derivation --
    there is no CLI override for these, unlike `--transcriptome`).
    Idempotent: skips work already done (source copy, each downloaded
    file) so repeated calls across a run are cheap.
    """
    runtime_dir = cache_dir / "runtime"
    ref_dir = runtime_dir / "RefFiles"
    ref_dir.mkdir(parents=True, exist_ok=True)

    main_script_dest = runtime_dir / _MAIN_SCRIPT_NAME
    if not main_script_dest.is_file():
        shutil.copy2(_VENDOR_DIR / _MAIN_SCRIPT_NAME, main_script_dest)

    driver_script_dest = runtime_dir / _DRIVER_SCRIPT_NAME
    if not driver_script_dest.is_file():
        shutil.copy2(_VENDOR_DIR / _DRIVER_SCRIPT_NAME, driver_script_dest)

    vendor_ref_dir = _VENDOR_DIR / "RefFiles"
    for item in vendor_ref_dir.iterdir():
        dest = ref_dir / item.name
        if item.is_dir():
            if not dest.is_dir():
                shutil.copytree(item, dest)
        elif not dest.is_file():
            shutil.copy2(item, dest)

    for filename, relative_url in _GITHUB_REFDATA_FILES.items():
        dest = ref_dir / filename
        if not dest.is_file():
            logger.info(f"Downloading SPiP reference file '{filename}' ...")
            _download_file(_GITHUB_RAW_BASE + relative_url, dest)

    refseq_filename = f"dataRefSeq{genome}.RData"
    refseq_dest = ref_dir / refseq_filename
    if not refseq_dest.is_file():
        logger.info(f"Downloading SPiP reference file '{refseq_filename}' ...")
        _download_file(_GITHUB_RAW_BASE + f"RefFiles/{refseq_filename}", refseq_dest)

    transcriptome_filename = f"transcriptome_{genome}.RData"
    transcriptome_dest = ref_dir / transcriptome_filename
    if not transcriptome_dest.is_file():
        logger.info(f"Downloading SPiP transcriptome reference '{transcriptome_filename}' (~370-400MB, one-time) ...")
        _download_file(_SOURCEFORGE_TRANSCRIPTOME_TEMPLATE.format(genome=genome), transcriptome_dest)

    return runtime_dir


def is_runtime_ready(cache_dir: Path, genome: str = DEFAULT_GENOME) -> bool:
    ref_dir = cache_dir / "runtime" / "RefFiles"
    required = [
        "model.RData",
        "RefFiles.RData",
        f"dataRefSeq{genome}.RData",
        f"transcriptome_{genome}.RData",
        "SPiP_libs",
        "VPP_table.txt",
        "VPN_table.txt",
    ]
    runtime_dir = cache_dir / "runtime"
    return (
        (runtime_dir / _MAIN_SCRIPT_NAME).is_file()
        and (runtime_dir / _DRIVER_SCRIPT_NAME).is_file()
        and all((ref_dir / name).exists() for name in required)
    )


def build_minimal_vcf(chrom: str, pos: int, ref: str, alt: str, variant_id: str = ".") -> str:
    """
    Minimal VCF SPiP's own `readVCF` (RefFiles/SPiP_libs/SPiP_functions.r)
    accepts: at least one header line before `#CHROM...` (its own
    header-reading loop reads lines until it finds one starting with
    `#CHROM`), then the standard 8 mandatory VCF columns. Verified
    against the upstream repo's own `testVar.vcf` example, including
    its "chr17"-style (not bare "17") contig naming convention.

    The mitochondrial contig needs its own case: SPiP's underlying
    BSgenome.Hsapiens.UCSC.hg38 reference has no 'chrMT' contig, only
    'chrM' -- a bare re-prefix maps an Ensembl-style 'MT' input to the
    non-existent 'chrMT' (round 15 finding). Fixed here as defence in
    depth, but note this is currently unreachable in practice: SPiP's
    own vendored `RefFiles/getGenomeSequenceFromBSgenome.r` (line ~110)
    filters `chr=="chrM"` OUT of its transcriptome before SPiP ever
    runs, so no MT variant reaches a transcript match regardless of how
    this function spells the contig. Do not read a passing test on this
    function as proof that SPiP works end-to-end for MT -- it doesn't;
    SPiP never gets that far.
    """
    bare = chrom[3:] if chrom.lower().startswith("chr") else chrom
    chrom = "chrM" if bare.upper() in ("M", "MT") else (chrom if chrom.startswith("chr") else f"chr{chrom}")
    return (
        "##fileformat=VCFv4.0\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        f"{chrom}\t{pos}\t{variant_id}\t{ref}\t{alt}\t.\t.\t.\n"
    )


def run_spip(
    rscript_path: str,
    runtime_dir: Path,
    chrom: str,
    pos: int,
    ref: str,
    alt: str,
    genome: str = DEFAULT_GENOME,
    variant_id: str = ".",
    timeout: int = 600,
) -> str:
    """
    Writes a one-variant VCF and invokes `geper_spip_driver.r` (this
    package's own serial driver -- see `_DRIVER_SCRIPT_NAME`'s comment
    above and the driver script's own header for why it exists instead
    of the vendored `SPiPv2.1_main.r` directly) against it, returning
    the raw tab-delimited output file's text. Output column layout is
    identical to what `SPiPv2.1_main.r` itself would produce in VCF-
    input/text-output mode -- the driver reuses the exact same column
    list, just without going through that script's own broken-on-
    Windows parallel loop.

    No persistent R process: every call here reloads the ~400MB
    combined reference-data set from disk, unlike the in-process
    PyTorch plugins in this family (Enformer/Borzoi/SpliceFormer/
    SpliceBERT), which load once and reuse. Documented in SpipPlugin's
    own docstring, not silently understated.
    """
    with tempfile.TemporaryDirectory(prefix="spip_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        input_vcf = tmp_path / "input.vcf"
        output_txt = tmp_path / "output.txt"
        input_vcf.write_text(build_minimal_vcf(chrom, pos, ref, alt, variant_id), encoding="utf-8")

        cmd = [
            rscript_path,
            str(runtime_dir / _DRIVER_SCRIPT_NAME),
            "--input",
            str(input_vcf),
            "--output",
            str(output_txt),
            "--GenomeAssenbly",
            genome,
            "--runtimeDir",
            str(runtime_dir),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0 or not output_txt.is_file():
            raise RuntimeError(
                f"SPiP subprocess failed (exit {result.returncode}): "
                f"{(result.stderr or result.stdout or '').strip()[:2000]}"
            )
        return output_txt.read_text(encoding="utf-8")
