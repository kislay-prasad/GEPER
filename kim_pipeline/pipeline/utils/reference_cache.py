"""
pipeline/utils/reference_cache.py
──────────────────────────────────
Reference-genome preparation and BWA index caching.

Written to fix a specific, profiled bottleneck: BWA indexing of a large
(hundreds-of-MB to multi-GB) reference genome inside an ephemeral notebook
environment (Google Colab), where:

  1. The reference is often a plain ``.gz`` file.
  2. `/content` is wiped whenever the runtime disconnects/expires, so an
     index built there is lost even if the build itself succeeded.
  3. `bwa index` gives no visible progress for long stretches, and the
     pipeline's own subprocess helper (`pipeline/fastq/errors.py::_run`)
     buffers all stdout/stderr until the process exits — so a multi-hour
     build looks identical to a hang from the notebook's point of view.

What this module does NOT do: change which aligner is used, or guess at
speedups that weren't actually measured. See `docs/BWA_INDEXING.md` (added
alongside this module) for the profiling results that justify each design
choice below.

Two independent caches, because they solve two different problems:

  * `resolve_reference()` — decompresses a `.gz` reference exactly once
    into a local, fast working directory. This is NOT primarily a speed
    optimization (profiling showed `bwa index` runs at effectively the
    same speed against a `.gz` input as a decompressed one — gzip
    decompression is a small fraction of total index-build time). It
    exists because a downstream stage in this same pipeline
    (`variant_calling/freebayes_runner.py`, via `samtools faidx`) cannot
    open a plain-gzip FASTA at all (samtools requires either an
    uncompressed FASTA or *bgzip*-compressed, not standard gzip) — so a
    `.gz` reference must be decompressed somewhere before the pipeline
    can complete regardless of alignment-stage speed.

  * `ensure_bwa_index()` — builds the BWA FM-index into a *persistent*,
    configurable directory (e.g. a Google Drive mount) using an
    atomic build-then-publish pattern, so:
      - a completed index is *never* rebuilt on a later run (fixes the
        "1-2 hours every single time" complaint), and
      - a build interrupted by a Colab session timeout can *never* be
        mistaken for a complete, valid index on the next run (existing
        code only checked file *existence*, not completeness -- a
        session that dies while BWA is still writing `.sa` would leave
        all 5 suffix files present on disk, and the old existence-only
        check would wrongly treat that as "already indexed").
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("geper.pipeline.utils.reference_cache")

_BWA_INDEX_SUFFIXES = (".bwt", ".pac", ".sa", ".amb", ".ann")
_MANIFEST_NAME = "index_manifest.json"
_HEARTBEAT_INTERVAL_SECONDS = 30


class ReferenceCacheError(RuntimeError):
    """Raised for reference-preparation or index-caching failures."""


# ─── Decompression cache (correctness, not speed) ──────────────────────────

def _fingerprint(path: Path) -> dict:
    st = path.stat()
    return {"name": path.name, "size": st.st_size, "mtime": int(st.st_mtime)}


def _content_fingerprint(path: Path, sample_bytes: int = 8 * 1024 * 1024) -> dict:
    """Fingerprint based on file identity + content sample, NOT mtime.

    Used for the persistent BWA index manifest specifically, because mtime
    is unreliable there in exactly the scenario this feature targets: a
    `.gz` reference gets decompressed fresh into a new local cache
    directory every ephemeral session (see `resolve_reference`), which
    gives the decompressed file a brand-new mtime each time even when its
    *content* is byte-for-byte identical to the previous session's copy.
    An mtime-based fingerprint would treat that as "changed" and force an
    unnecessary rebuild every single session -- defeating the entire point
    of a persistent index cache.

    Hashing the first and last `sample_bytes` (default 8 MB) rather than
    the whole file keeps this fast even for multi-GB references, while
    still being sensitive to both truncation (a differently-sized file)
    and to different content at either end of the file.
    """
    st = path.stat()
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(sample_bytes))
        if st.st_size > sample_bytes:
            f.seek(max(0, st.st_size - sample_bytes))
            h.update(f.read(sample_bytes))
    return {"name": path.name, "size": st.st_size, "content_sha256_sample": h.hexdigest()}


def resolve_reference(reference_fasta: str, cache_dir: Optional[str] = None) -> str:
    """Return a path to a plain (uncompressed) FASTA for `reference_fasta`.

    If `reference_fasta` is not gzip-compressed, it is returned unchanged
    (no copy — avoids doubling disk usage for the common case).

    If it *is* `.gz`, it is decompressed once into `cache_dir` (default:
    a sibling `.ref_cache` directory next to the source file). A fingerprint
    (name + size + mtime of the source `.gz`) is recorded alongside the
    decompressed copy; on subsequent calls with an unchanged source file,
    the existing decompressed copy is reused instead of decompressing again.
    """
    ref = Path(reference_fasta).expanduser().resolve()

    if ref.suffix != ".gz":
        # Not gzip-compressed: pass through unchanged. Existence is
        # intentionally NOT checked here -- that has always been the
        # responsibility of the stage that actually opens the file
        # (AlignmentStage / bwa_runner, VariantCallingStage), and keeping
        # it that way preserves the exact same error path (and stage
        # label) those stages already produce for a missing reference,
        # for both real runs and stage-mocked unit tests.
        return str(ref)

    if not ref.exists():
        raise ReferenceCacheError(f"Reference FASTA not found: {ref}")

    cache = Path(cache_dir).expanduser().resolve() if cache_dir else ref.parent / ".ref_cache"
    cache.mkdir(parents=True, exist_ok=True)

    decompressed_name = ref.stem  # "ref.fna.gz" -> "ref.fna"
    decompressed_path = cache / decompressed_name
    fp_path = cache / (decompressed_name + ".source_fingerprint.json")

    current_fp = _fingerprint(ref)
    if decompressed_path.exists() and fp_path.exists():
        try:
            saved_fp = json.loads(fp_path.read_text())
        except (json.JSONDecodeError, OSError):
            saved_fp = None
        if saved_fp == current_fp and decompressed_path.stat().st_size > 0:
            logger.info(
                "Reference already decompressed and unchanged since last run "
                "(%s) — reusing %s, not re-decompressing.",
                ref.name, decompressed_path,
            )
            return str(decompressed_path)

    logger.info("Decompressing reference %s -> %s ...", ref, decompressed_path)
    t0 = time.time()
    tmp_out = decompressed_path.with_suffix(decompressed_path.suffix + ".partial")
    try:
        with gzip.open(ref, "rb") as src, open(tmp_out, "wb") as dst:
            shutil.copyfileobj(src, dst, length=64 * 1024 * 1024)
        tmp_out.rename(decompressed_path)
    except Exception as exc:
        tmp_out.unlink(missing_ok=True)
        raise ReferenceCacheError(f"Failed to decompress {ref}: {exc}") from exc

    fp_path.write_text(json.dumps(current_fp))
    logger.info(
        "Decompression complete in %.1fs (%s -> %s, %.1f MB).",
        time.time() - t0, ref.name, decompressed_path.name,
        decompressed_path.stat().st_size / (1024 * 1024),
    )
    return str(decompressed_path)


# ─── Persistent, atomically-published BWA index cache ──────────────────────

def _index_files_valid(prefix: Path, expected_sizes: Optional[dict] = None) -> bool:
    for suf in _BWA_INDEX_SUFFIXES:
        f = Path(str(prefix) + suf)
        if not f.exists() or f.stat().st_size == 0:
            return False
        if expected_sizes and expected_sizes.get(suf) is not None:
            if f.stat().st_size != expected_sizes[suf]:
                return False
    return True


def _manifest_path(index_dir: Path) -> Path:
    return index_dir / _MANIFEST_NAME


def _load_manifest(index_dir: Path) -> dict:
    p = _manifest_path(index_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _stream_subprocess_with_heartbeat(cmd: List[str], stage_label: str) -> None:
    """Run `cmd`, streaming its stderr to the logger line-by-line as it is
    produced (instead of buffering everything until the process exits), and
    logging a heartbeat every `_HEARTBEAT_INTERVAL_SECONDS` even if the
    subprocess itself has been silent for a stretch (e.g. during BWT
    construction, which prints nothing for long periods on large genomes).

    This directly fixes the "appears frozen" complaint: the existing shared
    `_run()` helper (`pipeline/fastq/errors.py`) uses
    `subprocess.run(capture_output=True)`, which produces *zero* visible
    output until the entire command completes -- for a multi-hour index
    build, that is indistinguishable from a hang in a notebook cell.
    """
    t0 = time.time()
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )

    stop_event = threading.Event()

    def _heartbeat() -> None:
        while not stop_event.wait(_HEARTBEAT_INTERVAL_SECONDS):
            logger.info(
                "[%s] still running — %.0fs elapsed (this is normal for large "
                "genomes; BWA prints no output during BWT/SA construction)",
                stage_label, time.time() - t0,
            )

    hb_thread = threading.Thread(target=_heartbeat, daemon=True)
    hb_thread.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line:
                logger.info("[%s] %s", stage_label, line)
        proc.wait()
    finally:
        stop_event.set()

    if proc.returncode != 0:
        raise ReferenceCacheError(
            f"{stage_label} exited with code {proc.returncode} (cmd={' '.join(cmd)})"
        )
    logger.info("[%s] complete in %.1fs.", stage_label, time.time() - t0)


def ensure_bwa_index(
    binary: str,
    reference_fasta: str,
    index_dir: Optional[str] = None,
) -> str:
    """Ensure a complete BWA FM-index exists for `reference_fasta`, building
    it only if necessary, and return the index prefix path to pass to
    `bwa mem` / `bwa-mem2 mem`.

    Behavior:
      * `index_dir=None` (default): index is built/expected next to
        `reference_fasta` itself, same as the original implementation.
      * `index_dir=<path>` (e.g. a Google Drive mount): index is built into
        and looked up from that persistent directory instead, so it
        survives an ephemeral runtime being wiped between sessions. A
        `index_manifest.json` records a content-based fingerprint (name +
        size + a SHA-256 sample of the file's start/end -- NOT mtime,
        which would be unreliable here; see `_content_fingerprint`) the
        index was built from, and the index for a *different* reference
        with the same filename is correctly rejected as unrelated (not
        silently reused).
      * The index is built into a temporary, uniquely-named prefix first;
        only after every one of the 5 suffix files is confirmed non-empty
        (and bwa/bwa-mem2 exited 0) is it published (renamed) to the final
        prefix and the manifest written. If the process is killed
        mid-build (e.g. a Colab session timeout), only the temp prefix is
        left behind -- the final prefix location is never left in a
        partially-written state, so a later run cannot mistake a truncated
        index for a complete one.
    """
    ref = Path(reference_fasta).expanduser().resolve()
    if not ref.exists():
        raise ReferenceCacheError(f"Reference FASTA not found: {ref}")

    if index_dir is None:
        # Original behavior: index lives right next to the reference.
        prefix = ref
        missing = [s for s in _BWA_INDEX_SUFFIXES if not Path(str(prefix) + s).exists()]
        if not missing and _index_files_valid(prefix):
            logger.info("BWA index already present next to reference: %s", ref)
            return str(prefix)
        logger.info(
            "BWA index missing/incomplete for %s (no %s) — building with '%s index'",
            reference_fasta, ", ".join(missing) or "size-0 files", binary,
        )
        _stream_subprocess_with_heartbeat([binary, "index", str(ref)], "bwa index")
        if not _index_files_valid(prefix):
            raise ReferenceCacheError(
                f"{binary} index reported success but index files are missing/empty "
                f"for prefix {prefix}"
            )
        return str(prefix)

    # Persistent, configurable index_dir path. Fingerprint by content
    # sample (not mtime) -- see _content_fingerprint's docstring for why
    # mtime specifically breaks this exact use case.
    current_fp = _content_fingerprint(ref)
    idx_root = Path(index_dir).expanduser().resolve()
    idx_root.mkdir(parents=True, exist_ok=True)
    final_prefix = idx_root / ref.name

    manifest = _load_manifest(idx_root)
    entry = manifest.get(ref.name)
    if entry == current_fp and _index_files_valid(final_prefix):
        logger.info(
            "Reusing persistent BWA index for %s from %s (unchanged since last build) "
            "— skipping indexing entirely.",
            ref.name, idx_root,
        )
        return str(final_prefix)

    if entry is not None and entry != current_fp:
        logger.warning(
            "Persistent index at %s was built from a different reference "
            "(recorded=%s, current=%s) — rebuilding rather than reusing.",
            idx_root, entry, current_fp,
        )
    elif final_prefix.exists() or any(
        Path(str(final_prefix) + s).exists() for s in _BWA_INDEX_SUFFIXES
    ):
        logger.warning(
            "Index-like files exist at %s but no valid manifest entry confirms "
            "they are complete (e.g. a previous build may have been interrupted "
            "mid-write) — rebuilding rather than risking a corrupt index.",
            final_prefix,
        )

    # Build into a private temp prefix so a killed/interrupted build never
    # contaminates final_prefix.
    build_dir = Path(tempfile.mkdtemp(prefix=f".{ref.name}.building_", dir=str(idx_root)))
    build_prefix = build_dir / ref.name
    try:
        logger.info(
            "Building BWA index for %s into persistent directory %s "
            "(temporary build location: %s) ...",
            ref.name, idx_root, build_dir,
        )
        _stream_subprocess_with_heartbeat(
            [binary, "index", "-p", str(build_prefix), str(ref)], "bwa index",
        )
        if not _index_files_valid(build_prefix):
            raise ReferenceCacheError(
                f"{binary} index reported success but index files are missing/empty "
                f"for prefix {build_prefix}"
            )

        # Publish: move each completed suffix file into place, then write
        # the manifest last -- so a crash between these renames still can't
        # produce a state where the manifest claims completeness falsely
        # (the manifest write is the final, atomic-enough step; a partial
        # set of renamed files with no manifest entry is correctly treated
        # as "no valid index" by the check above on the next run).
        for suf in _BWA_INDEX_SUFFIXES:
            src = Path(str(build_prefix) + suf)
            dst = Path(str(final_prefix) + suf)
            shutil.move(str(src), str(dst))

        manifest[ref.name] = current_fp
        _manifest_path(idx_root).write_text(json.dumps(manifest, indent=2))
        logger.info(
            "Published BWA index to persistent directory: %s "
            "(will be reused on future runs without rebuilding).",
            final_prefix,
        )
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)

    return str(final_prefix)
