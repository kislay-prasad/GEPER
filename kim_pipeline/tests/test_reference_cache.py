"""
tests/test_reference_cache.py
────────────────────────────────
Tests for pipeline/utils/reference_cache.py: the .gz decompression cache
and the persistent, atomically-published BWA index cache.

Real bwa subprocess tests are skipped (not faked) if bwa isn't installed,
matching the rest of this test suite's convention. The decompression-cache
tests don't need bwa at all, so they always run.
"""

from __future__ import annotations

import gzip
import json
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.utils.reference_cache import (
    resolve_reference,
    ensure_bwa_index,
    ReferenceCacheError,
    _BWA_INDEX_SUFFIXES,
)
from pipeline.alignment import bwa_runner

HAVE_BWA = bwa_runner.is_available()


def _make_fasta(path: Path, n_bases: int = 20000, seed: int = 0) -> None:
    import random

    rng = random.Random(seed)
    seq = "".join(rng.choice("ACGT") for _ in range(n_bases))
    with open(path, "w") as f:
        f.write(">chrTest synthetic reference\n")
        for i in range(0, len(seq), 70):
            f.write(seq[i : i + 70] + "\n")


class TestResolveReferenceDecompression:
    def test_plain_fasta_returned_unchanged_no_copy(self, tmp_path):
        ref = tmp_path / "ref.fasta"
        _make_fasta(ref)
        result = resolve_reference(str(ref))
        assert result == str(ref.resolve())
        # No cache directory should have been created for a non-gz input.
        assert not (tmp_path / ".ref_cache").exists()

    def test_gz_reference_is_decompressed(self, tmp_path):
        ref = tmp_path / "ref.fasta"
        _make_fasta(ref)
        gz_path = tmp_path / "ref.fasta.gz"
        with open(ref, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

        result = resolve_reference(str(gz_path))
        assert Path(result).name == "ref.fasta"
        assert Path(result).exists()
        assert Path(result).read_text() == ref.read_text()

    def test_missing_gz_reference_raises(self, tmp_path):
        with pytest.raises(ReferenceCacheError):
            resolve_reference(str(tmp_path / "does_not_exist.fasta.gz"))

    def test_missing_plain_reference_passes_through_unchecked(self, tmp_path):
        # Existence-checking a non-.gz path is intentionally left to
        # whichever stage actually opens it (matches pre-existing
        # behavior, and is required for stage-mocked unit tests that use
        # placeholder paths that are never really opened).
        fake = tmp_path / "does_not_exist.fasta"
        result = resolve_reference(str(fake))
        assert result == str(fake.resolve())

    def test_unchanged_gz_reference_is_not_redecompressed(self, tmp_path):
        ref = tmp_path / "ref.fasta"
        _make_fasta(ref, n_bases=200000)  # big enough that decompression takes measurable time
        gz_path = tmp_path / "ref.fasta.gz"
        with open(ref, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

        first = resolve_reference(str(gz_path))
        first_mtime = Path(first).stat().st_mtime

        # Second call against the exact same (unchanged) .gz source must
        # reuse the existing decompressed copy rather than rewriting it.
        second = resolve_reference(str(gz_path))
        second_mtime = Path(second).stat().st_mtime
        assert first == second
        assert first_mtime == second_mtime

    def test_changed_gz_reference_is_redecompressed(self, tmp_path):
        ref = tmp_path / "ref.fasta"
        _make_fasta(ref, seed=1)
        gz_path = tmp_path / "ref.fasta.gz"
        with open(ref, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        first = resolve_reference(str(gz_path))
        first_content = Path(first).read_text()

        # Simulate the source .gz being replaced with different content
        # (e.g. the user swapped in a different genome build under the
        # same filename) -- must be detected via the fingerprint and
        # re-decompressed, not silently reused.
        time.sleep(0.01)
        _make_fasta(ref, seed=2)
        with open(ref, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

        second = resolve_reference(str(gz_path))
        assert Path(second).read_text() != first_content

    def test_custom_cache_dir_is_used(self, tmp_path):
        ref = tmp_path / "ref.fasta"
        _make_fasta(ref)
        gz_path = tmp_path / "ref.fasta.gz"
        with open(ref, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

        custom_cache = tmp_path / "my_persistent_cache"
        result = resolve_reference(str(gz_path), cache_dir=str(custom_cache))
        assert str(custom_cache) in result
        assert Path(result).exists()


@pytest.mark.skipif(not HAVE_BWA, reason="bwa not installed")
class TestEnsureBwaIndexNextToReference:
    def test_builds_index_when_missing(self, tmp_path):
        ref = tmp_path / "ref.fasta"
        _make_fasta(ref)
        binary = "bwa-mem2" if shutil.which("bwa-mem2") else "bwa"

        prefix = ensure_bwa_index(binary, str(ref))
        assert prefix == str(ref.resolve())
        for suf in _BWA_INDEX_SUFFIXES:
            assert Path(str(ref) + suf).exists()
            assert Path(str(ref) + suf).stat().st_size > 0

    def test_skips_rebuild_when_index_already_present(self, tmp_path):
        ref = tmp_path / "ref.fasta"
        _make_fasta(ref)
        binary = "bwa-mem2" if shutil.which("bwa-mem2") else "bwa"

        ensure_bwa_index(binary, str(ref))
        bwt_mtime_first = Path(str(ref) + ".bwt").stat().st_mtime

        time.sleep(0.05)
        ensure_bwa_index(binary, str(ref))
        bwt_mtime_second = Path(str(ref) + ".bwt").stat().st_mtime

        # Must not have rebuilt -- mtime unchanged.
        assert bwt_mtime_first == bwt_mtime_second

    def test_rebuilds_if_an_index_file_is_missing(self, tmp_path):
        ref = tmp_path / "ref.fasta"
        _make_fasta(ref)
        binary = "bwa-mem2" if shutil.which("bwa-mem2") else "bwa"

        ensure_bwa_index(binary, str(ref))
        # Simulate a partial index (e.g. one file lost/corrupted).
        Path(str(ref) + ".sa").unlink()

        ensure_bwa_index(binary, str(ref))
        for suf in _BWA_INDEX_SUFFIXES:
            assert Path(str(ref) + suf).exists()


@pytest.mark.skipif(not HAVE_BWA, reason="bwa not installed")
class TestEnsureBwaIndexPersistentDir:
    def test_builds_into_persistent_index_dir(self, tmp_path):
        ref = tmp_path / "ref.fasta"
        _make_fasta(ref)
        index_dir = tmp_path / "persistent_index"
        binary = "bwa-mem2" if shutil.which("bwa-mem2") else "bwa"

        prefix = ensure_bwa_index(binary, str(ref), index_dir=str(index_dir))
        assert str(index_dir) in prefix
        for suf in _BWA_INDEX_SUFFIXES:
            assert Path(prefix + suf).exists()
            assert Path(prefix + suf).stat().st_size > 0
        # No index files should have been left next to the reference itself.
        for suf in _BWA_INDEX_SUFFIXES:
            assert not Path(str(ref) + suf).exists()
        # No leftover temp build directories.
        leftover_build_dirs = list(index_dir.glob(".*.building_*"))
        assert leftover_build_dirs == []

    def test_second_run_skips_rebuild_entirely(self, tmp_path):
        """This is the core fix: a completed persistent index must never
        be rebuilt on a later run, even in a brand-new process/session."""
        ref = tmp_path / "ref.fasta"
        _make_fasta(ref)
        index_dir = tmp_path / "persistent_index"
        binary = "bwa-mem2" if shutil.which("bwa-mem2") else "bwa"

        prefix1 = ensure_bwa_index(binary, str(ref), index_dir=str(index_dir))
        bwt_mtime_first = Path(prefix1 + ".bwt").stat().st_mtime

        t0 = time.time()
        prefix2 = ensure_bwa_index(binary, str(ref), index_dir=str(index_dir))
        elapsed = time.time() - t0

        assert prefix1 == prefix2
        assert Path(prefix2 + ".bwt").stat().st_mtime == bwt_mtime_first
        # Skip-check must be near-instant (file stat + JSON read), not a
        # rebuild -- generous bound to avoid CI flakiness while still
        # clearly distinguishing "skipped" from "rebuilt".
        assert elapsed < 2.0

    def test_manifest_rejects_different_reference_same_filename(self, tmp_path):
        """A persistent index_dir might be reused across samples. If a
        *different* reference happens to share the same filename, the old
        index must not be silently applied to it."""
        index_dir = tmp_path / "persistent_index"
        binary = "bwa-mem2" if shutil.which("bwa-mem2") else "bwa"

        ref_dir_a = tmp_path / "a"
        ref_dir_a.mkdir()
        ref_a = ref_dir_a / "ref.fasta"
        _make_fasta(ref_a, seed=10)
        prefix_a = ensure_bwa_index(binary, str(ref_a), index_dir=str(index_dir))
        bwt_content_a = Path(prefix_a + ".bwt").read_bytes()

        ref_dir_b = tmp_path / "b"
        ref_dir_b.mkdir()
        ref_b = ref_dir_b / "ref.fasta"  # same filename, different content
        _make_fasta(ref_b, seed=20, n_bases=25000)
        prefix_b = ensure_bwa_index(binary, str(ref_b), index_dir=str(index_dir))
        bwt_content_b = Path(prefix_b + ".bwt").read_bytes()

        assert bwt_content_a != bwt_content_b

    def test_interrupted_build_leaves_no_valid_final_index(self, tmp_path):
        """Simulates a Colab session dying mid-build: a temp build
        directory with partial files exists, but nothing was published to
        the final prefix, so a subsequent run must rebuild rather than use
        a truncated index."""
        ref = tmp_path / "ref.fasta"
        _make_fasta(ref)
        index_dir = tmp_path / "persistent_index"
        index_dir.mkdir()
        binary = "bwa-mem2" if shutil.which("bwa-mem2") else "bwa"

        # Manually fabricate the "interrupted" state: a stray build dir
        # with only some suffix files, and no manifest entry.
        stray_build = index_dir / ".ref.fasta.building_abc123"
        stray_build.mkdir()
        (stray_build / "ref.fasta.amb").write_text("x")
        (stray_build / "ref.fasta.ann").write_text("x")
        # .bwt/.sa never got written -- session died before they completed.

        # A real run must succeed and produce a genuinely complete index,
        # ignoring the stray partial build directory.
        prefix = ensure_bwa_index(binary, str(ref), index_dir=str(index_dir))
        for suf in _BWA_INDEX_SUFFIXES:
            assert Path(prefix + suf).exists()
            assert Path(prefix + suf).stat().st_size > 0
        manifest = json.loads((index_dir / "index_manifest.json").read_text())
        assert "ref.fasta" in manifest

    def test_reused_across_sessions_despite_fresh_decompression_mtime(self, tmp_path):
        """Regression test for a real bug caught during live benchmarking:
        a `.gz` reference decompressed fresh in a new ephemeral session
        directory (simulating a Colab restart) produces a file with
        byte-identical content but a brand-new mtime. An mtime-based
        fingerprint wrongly treated that as "changed" and rebuilt the
        index every session, defeating the entire point of a persistent
        index cache. The fingerprint must be content-based, not mtime."""
        index_dir = tmp_path / "persistent_index"
        binary = "bwa-mem2" if shutil.which("bwa-mem2") else "bwa"

        # "Session 1": decompress into ephemeral dir A, build the index.
        session1_dir = tmp_path / "session1"
        session1_dir.mkdir()
        ref1 = session1_dir / "ref.fasta"
        _make_fasta(ref1, seed=99)
        prefix1 = ensure_bwa_index(binary, str(ref1), index_dir=str(index_dir))
        bwt_mtime_1 = Path(prefix1 + ".bwt").stat().st_mtime

        # "Session 2": ephemeral dir A is gone (session restarted); the
        # exact same source content is decompressed again into a brand
        # new ephemeral dir B, which necessarily gets a fresh mtime.
        time.sleep(0.05)
        session2_dir = tmp_path / "session2"
        session2_dir.mkdir()
        ref2 = session2_dir / "ref.fasta"
        _make_fasta(ref2, seed=99)  # identical content, different file/mtime
        assert ref1.read_bytes() == ref2.read_bytes()
        assert ref2.stat().st_mtime != ref1.stat().st_mtime

        t0 = time.time()
        prefix2 = ensure_bwa_index(binary, str(ref2), index_dir=str(index_dir))
        elapsed = time.time() - t0

        assert Path(prefix2 + ".bwt").stat().st_mtime == bwt_mtime_1, (
            "index was rebuilt even though content was unchanged -- "
            "fingerprint must be content-based, not mtime-based"
        )
        assert elapsed < 2.0
