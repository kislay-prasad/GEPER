"""
What a run ACTUALLY loaded, as opposed to what it MEANT to load.

WHY THIS EXISTS.
Every model identifier in `provenance.py` is read from config and loader
constants at startup. That records an INTENTION. It cannot answer "which
weights produced this report", which is the question an impact/recall
query is actually asked. The gap is not theoretical: the offline image
serves weights from a pre-seeded HuggingFace cache, and nothing verifies
the cached bytes against the pinned revision.

THE DISTINCTION THIS FILE ENFORCES, AND IT IS THE WHOLE POINT.
A HuggingFace blob is NAMED by the sha256 of its content, so the hash is
available for free by reading the filename. *** THAT FILENAME IS NOT AN
OBSERVATION OF THE BYTES. IT IS THE CACHE'S OWN ASSERTION ABOUT ITS
CONTENT, made at download time, on another machine, never rechecked. A
corrupted, truncated or substituted blob KEEPS ITS FILENAME -- the label
is the one thing that cannot change. *** So a declared hash is strictly
better than a config pin and STILL A CLAIM: a cache is proven by a load,
never by a listing, and reading a filename is a listing.

Hence three states that must never collapse into each other:
  - a hash was DECLARED by the cache and the bytes were never read
  - the bytes were READ and MATCHED
  - the bytes were READ and DID NOT MATCH  <- corrupt or substituted cache

`test_verification_detects_tampered_bytes` is the negative control. Without
it, every other test here would pass just as happily against a verifier
that returned "verified" unconditionally.

NO SYMLINKS ARE CREATED BY THESE TESTS, DELIBERATELY. This project's own
development host cannot create them without elevation (WinError 1314), so
a symlink-based test would SKIP here -- and a skipped test that reads as
coverage is the same camouflage that let an untouched `verify_checksum`
look like checksum verification. The layout parsing is therefore a pure
function over two paths, tested as one.
"""

import hashlib
import os
import tempfile
import unittest
from unittest import mock

from pipeline.provenance import (
    HashVerification,
    VersionStatus,
    _derive_cache_identity,
    resolve_model_artifact,
    verify_model_artifact,
)

_SIXTY_FOUR_HEX = "a08adabb949fa67ad3c14b509d04fd60368b35007b0095e3358f81200c4f4db0"
_FORTY_HEX = "a956a25d277f30bd870d3760b9a116f19ead885e"


class DeriveCacheIdentityTest(unittest.TestCase):
    """Pure layout parsing -- no filesystem, no symlinks."""

    def test_hf_layout_yields_served_revision_and_declared_hash(self):
        requested = "/cache/models--facebook--esm2/snapshots/08e4846e53717742/model.safetensors"
        resolved = f"/cache/models--facebook--esm2/blobs/{_SIXTY_FOUR_HEX}"
        served, declared = _derive_cache_identity(requested, resolved)
        self.assertEqual(served, "08e4846e53717742")
        self.assertEqual(declared, _SIXTY_FOUR_HEX)

    def test_forty_hex_blob_is_not_a_content_hash(self):
        """
        Only the LFS-tracked weight file is named by content sha256. The small
        config/tokenizer blobs are named by git blob SHA-1 (40 hex), which is
        NOT a hash of the bytes and must never be recorded as one.
        """
        requested = "/cache/models--facebook--esm2/snapshots/08e4846e53717742/config.json"
        resolved = f"/cache/models--facebook--esm2/blobs/{_FORTY_HEX}"
        served, declared = _derive_cache_identity(requested, resolved)
        self.assertEqual(served, "08e4846e53717742")
        self.assertIsNone(declared, "a 40-hex git blob name is not a content hash")

    def test_non_cache_layout_yields_neither(self):
        served, declared = _derive_cache_identity("/opt/weights/rna_fm.pth", "/opt/weights/rna_fm.pth")
        self.assertIsNone(served)
        self.assertIsNone(declared)


class ResolveModelArtifactTest(unittest.TestCase):
    def _blob(self, tmp, content: bytes):
        blobs = os.path.join(tmp, "models--x--y", "blobs")
        os.makedirs(blobs)
        name = hashlib.sha256(content).hexdigest()
        path = os.path.join(blobs, name)
        with open(path, "wb") as fh:
            fh.write(content)
        return path, name

    def test_declared_hash_is_recorded_but_never_claimed_as_verified(self):
        """
        *** THE CENTRAL ASSERTION. *** Reading the filename must produce a
        DECLARED hash in a NOT_VERIFIED state, and must NOT claim HASH_ONLY --
        whose own definition in this module is "a content hash of the actual
        bytes used is known". The bytes have not been read.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path, name = self._blob(tmp, b"weights-go-here")
            art = resolve_model_artifact(path)
            self.assertEqual(art.cache_declared_sha256, name)
            self.assertIs(art.hash_verification, HashVerification.NOT_VERIFIED)
            self.assertIsNot(
                art.version_status,
                VersionStatus.HASH_ONLY,
                "HASH_ONLY means the actual bytes were hashed; a declared filename is not that",
            )

    def test_a_path_outside_any_cache_is_explicit_not_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plain.pth")
            with open(path, "wb") as fh:
                fh.write(b"x")
            art = resolve_model_artifact(path)
            self.assertIsNone(art.cache_declared_sha256)
            self.assertIs(art.hash_verification, HashVerification.UNVERIFIABLE)


class VerifyModelArtifactTest(unittest.TestCase):
    def _blob(self, tmp, content: bytes):
        blobs = os.path.join(tmp, "models--x--y", "blobs")
        os.makedirs(blobs)
        name = hashlib.sha256(content).hexdigest()
        path = os.path.join(blobs, name)
        with open(path, "wb") as fh:
            fh.write(content)
        return path, name

    def test_matching_bytes_verify_and_only_then_is_hash_only_claimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, name = self._blob(tmp, b"the real weights")
            verified = verify_model_artifact(resolve_model_artifact(path))
            self.assertIs(verified.hash_verification, HashVerification.VERIFIED)
            self.assertIs(verified.version_status, VersionStatus.HASH_ONLY)
            self.assertEqual(verified.observed_sha256, name)

    def test_mismatch_still_persists_the_observed_hash(self):
        # THE CARD: the refusal cannot name what was never kept. A MISMATCH
        # must carry the actual bytes-on-disk hash, not just the declared one.
        with tempfile.TemporaryDirectory() as tmp:
            path, name = self._blob(tmp, b"the real weights")
            art = resolve_model_artifact(path)
            with open(path, "wb") as fh:
                fh.write(b"substituted weights")
            tampered = verify_model_artifact(art)
            self.assertIs(tampered.hash_verification, HashVerification.MISMATCH)
            self.assertEqual(tampered.observed_sha256, hashlib.sha256(b"substituted weights").hexdigest())
            self.assertNotEqual(tampered.observed_sha256, name, "the observed hash must differ from the declared one")

    def test_no_declared_hash_verify_leaves_observed_hash_none(self):
        # UNVERIFIABLE short-circuits before any bytes are read -- no
        # `verify_checksum_detailed` call happens, so nothing computed.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plain.pth")
            with open(path, "wb") as fh:
                fh.write(b"x")
            art = resolve_model_artifact(path)
            verified = verify_model_artifact(art)
            self.assertIs(verified.hash_verification, HashVerification.UNVERIFIABLE)
            self.assertIsNone(verified.observed_sha256)

    def test_verification_detects_tampered_bytes(self):
        """
        NEGATIVE CONTROL. A substituted blob keeps its filename, so the
        declared hash still looks right; only reading the bytes finds it.
        Without this test every other assertion here would pass against a
        verifier that returned VERIFIED unconditionally.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path, name = self._blob(tmp, b"the real weights")
            art = resolve_model_artifact(path)
            self.assertEqual(art.cache_declared_sha256, name)
            with open(path, "wb") as fh:  # same filename, different bytes
                fh.write(b"substituted weights")
            tampered = verify_model_artifact(art)
            self.assertIs(
                tampered.hash_verification,
                HashVerification.MISMATCH,
                "a substituted blob keeps its name; only the bytes reveal it",
            )
            self.assertIsNot(
                tampered.version_status,
                VersionStatus.HASH_ONLY,
                "a mismatched artifact must never be recorded as a known content hash",
            )

    def test_verification_routes_through_weightcache_verify_checksum(self):
        """
        The on-demand verifier is deliberately the production caller of
        `WeightCache.verify_checksum` (via `verify_checksum_detailed`, its
        bool-plus-actual-hash sibling -- see that method's docstring), which
        until now had none -- and it must pass a NON-None expected hash,
        since that function returns True for None and would otherwise pass
        by default.
        """
        from pipeline.models.cache import ChecksumResult, WeightCache

        with tempfile.TemporaryDirectory() as tmp:
            path, name = self._blob(tmp, b"the real weights")
            art = resolve_model_artifact(path)
            with mock.patch.object(
                WeightCache,
                "verify_checksum_detailed",
                return_value=ChecksumResult(matches=True, actual_sha256="a" * 64),
            ) as spy:
                verify_model_artifact(art)
            self.assertTrue(spy.called, "verification must go through WeightCache.verify_checksum_detailed")
            self.assertIsNotNone(spy.call_args.args[1] or spy.call_args.kwargs.get("expected_sha256"))


if __name__ == "__main__":
    unittest.main()
