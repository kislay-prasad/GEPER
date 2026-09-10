"""
A real run must record WHAT IT ACTUALLY LOADED, not only what it meant to.

S1 built the capability (`resolve_model_artifact` / `verify_model_artifact`)
and S3 projected the EXISTING `model_checkpoints` field. Between them sat a
gap that was stated rather than hidden: *** NOTHING CALLED
`resolve_model_artifact` IN A REAL RUN. *** So the impact query still
answered FROM PINS -- "which runs DECLARED revision X" -- while every reader
takes it as "which runs USED weights X". That is the wrong answer with a
confident face, and it is the same shape this floor carded four times: a
capability with no production caller, which greps as done.

This file closes the deployment half. It asserts three separable things:

1. AN OBSERVATION MADE AT LOAD TIME REACHES THE FINALISED RECORD. The
   capture happens where the weights are opened; the record is assembled
   after the last variant. Anything in between that drops it is the bug.
2. A MODEL WITH NO OBSERVATION SAYS SO EXPLICITLY. Silence would be
   indistinguishable from "this model loaded nothing worth recording", which
   is the same false-silence defect `list_pending` was fixed for one level up.
3. *** THE LOAD PATHS ACTUALLY CALL IT. *** Tests 1 and 2 can both pass
   against a function nobody invokes. The source-level assertions below are
   the only ones that can fail when the wiring is removed, and they exist
   precisely because "it has tests" is what made `WeightCache.verify_checksum`
   look wired for as long as it did.

THE NEGATIVE CONTROL USES REAL BYTES ON DISK, NOT A STUBBED RESOLVER: a real
content-addressed cache entry is written, verified, then TAMPERED WITH, and
the tamper must surface as MISMATCH -- at ERROR, never reconciled, and never
promoted to HASH_ONLY, because a refuted hash is not a known one.
"""

import hashlib
import inspect
import os
import tempfile
import unittest

from pipeline.provenance import (
    HashVerification,
    VersionStatus,
    capture_hf_cache_artifact,
    finalize_model_checkpoint_provenance,
    get_loaded_model_artifacts,
    record_loaded_model_artifact,
    reset_loaded_model_artifacts,
    verify_loaded_model_artifacts,
)

_USED = {"esm2": {"status": "used", "reason": ""}}


def _write_real_cache_blob(root: str, payload: bytes) -> str:
    """
    A REAL HuggingFace-shaped cache entry: the blob file is named for the
    sha256 of its own contents, exactly as the hub stores it. Nothing here is
    stubbed -- the bytes on disk are what the verifier will read.
    """
    blobs = os.path.join(root, "models--facebook--esm2_t33_650M_UR50D", "blobs")
    os.makedirs(blobs, exist_ok=True)
    path = os.path.join(blobs, hashlib.sha256(payload).hexdigest())
    with open(path, "wb") as fh:
        fh.write(payload)
    return path


class LoadedArtifactRegistryTest(unittest.TestCase):
    def setUp(self):
        reset_loaded_model_artifacts()
        self.addCleanup(reset_loaded_model_artifacts)

    def test_an_observation_made_at_load_time_reaches_the_finalised_record(self):
        with tempfile.TemporaryDirectory() as root:
            path = _write_real_cache_blob(root, b"weights")
            record_loaded_model_artifact("esm2", path)
            (entry,) = finalize_model_checkpoint_provenance({"esm2": "facebook/esm2"}, _USED).values()
            self.assertEqual(entry["loaded_artifact"]["resolved_path"], os.path.realpath(path))
            self.assertEqual(
                entry["loaded_artifact"]["cache_declared_sha256"],
                hashlib.sha256(b"weights").hexdigest(),
            )

    def test_a_model_with_no_observation_is_explicitly_unrecorded_not_absent(self):
        """
        *** THE CENTRAL ASSERTION, and the reason this is not a bare `if`. ***
        An absent key reads as "nothing to say". A present `None` reads as
        "this run recorded no observation for this model", which is the truth
        and is actionable.
        """
        (entry,) = finalize_model_checkpoint_provenance({"esm2": "facebook/esm2"}, _USED).values()
        self.assertIn("loaded_artifact", entry)
        self.assertIsNone(entry["loaded_artifact"])

    def test_an_observation_survives_a_model_with_no_run_status(self):
        """
        A run with zero variants tracks no per-model status, and such entries
        keep their bare identifier STRING today. Dropping a real observation
        to preserve that string would be the silent-drop defect itself.
        """
        with tempfile.TemporaryDirectory() as root:
            record_loaded_model_artifact("esm2", _write_real_cache_blob(root, b"w"))
            (entry,) = finalize_model_checkpoint_provenance({"esm2": "facebook/esm2"}, {}).values()
            self.assertIsInstance(entry, dict)
            self.assertIsNotNone(entry["loaded_artifact"])

    def test_a_prior_runs_observation_cannot_leak_into_this_run(self):
        with tempfile.TemporaryDirectory() as root:
            record_loaded_model_artifact("esm2", _write_real_cache_blob(root, b"old"))
            reset_loaded_model_artifacts()
            self.assertEqual(get_loaded_model_artifacts(), {})

    def test_a_path_that_cannot_be_interpreted_still_records_something(self):
        """Never a silent absence: an uninterpretable path is UNVERIFIABLE."""
        record_loaded_model_artifact("esm2", os.path.join("nowhere", "model.bin"))
        artifact = get_loaded_model_artifacts()["esm2"]
        self.assertIs(artifact.hash_verification, HashVerification.UNVERIFIABLE)
        self.assertIsNone(artifact.cache_declared_sha256)


class TamperedCacheEntryTest(unittest.TestCase):
    """THE NEGATIVE CONTROL. Real bytes, really changed, on disk."""

    def setUp(self):
        reset_loaded_model_artifacts()
        self.addCleanup(reset_loaded_model_artifacts)

    def test_an_intact_cache_entry_verifies(self):
        """The other half of the control: verification must PASS on good
        bytes, or a MISMATCH below would only prove the verifier says no to
        everything."""
        with tempfile.TemporaryDirectory() as root:
            record_loaded_model_artifact("esm2", _write_real_cache_blob(root, b"genuine weights"))
            verify_loaded_model_artifacts()
            self.assertIs(get_loaded_model_artifacts()["esm2"].hash_verification, HashVerification.VERIFIED)

    def test_a_tampered_cache_entry_surfaces_as_MISMATCH(self):
        with tempfile.TemporaryDirectory() as root:
            path = _write_real_cache_blob(root, b"genuine weights")
            record_loaded_model_artifact("esm2", path)
            # Substitution, not deletion: same filename, same length, other
            # bytes -- the case a listing cannot see and only a read can.
            with open(path, "wb") as fh:
                fh.write(b"swapped weights")
            verify_loaded_model_artifacts()
            self.assertIs(get_loaded_model_artifacts()["esm2"].hash_verification, HashVerification.MISMATCH)

    def test_a_mismatch_is_never_promoted_to_HASH_ONLY(self):
        """A refuted hash is not a known one."""
        with tempfile.TemporaryDirectory() as root:
            path = _write_real_cache_blob(root, b"genuine weights")
            record_loaded_model_artifact("esm2", path)
            with open(path, "wb") as fh:
                fh.write(b"swapped weights")
            verify_loaded_model_artifacts()
            self.assertIsNot(get_loaded_model_artifacts()["esm2"].version_status, VersionStatus.HASH_ONLY)

    def test_the_mismatch_reaches_the_finalised_record(self):
        """A disagreement that never leaves the registry is not a disclosure."""
        with tempfile.TemporaryDirectory() as root:
            path = _write_real_cache_blob(root, b"genuine weights")
            record_loaded_model_artifact("esm2", path)
            with open(path, "wb") as fh:
                fh.write(b"swapped weights")
            verify_loaded_model_artifacts()
            (entry,) = finalize_model_checkpoint_provenance({"esm2": "facebook/esm2"}, _USED).values()
            self.assertEqual(entry["loaded_artifact"]["hash_verification"], HashVerification.MISMATCH.value)


class CaptureIsActuallyWiredTest(unittest.TestCase):
    """
    *** THE ONLY TESTS HERE THAT FAIL WHEN THE WIRING IS REMOVED. ***
    Everything above passes against a capability nobody calls -- which is
    exactly how `WeightCache.verify_checksum` carried five assertions and zero
    production callers. These read the load functions' own source, so they
    cannot be satisfied by an import that is never reached.
    """

    def test_capture_tolerates_an_unresolvable_cache_without_raising(self):
        """Provenance capture must never be the thing that stops a run."""
        reset_loaded_model_artifacts()
        self.addCleanup(reset_loaded_model_artifacts)
        with tempfile.TemporaryDirectory() as empty:
            self.assertIsNone(capture_hf_cache_artifact("esm2", "facebook/nope", "deadbeef", empty))
        self.assertEqual(get_loaded_model_artifacts(), {})

    def test_every_hf_backed_loader_captures_what_it_loaded(self):
        from models.esm2 import ESM2Model
        from pipeline.models.borzoi_plugin import BorzoiPlugin
        from pipeline.models.enformer_plugin import EnformerPlugin

        for loader in (ESM2Model._load_impl, EnformerPlugin._load_impl, BorzoiPlugin._load_impl):
            with self.subTest(loader=loader.__qualname__):
                self.assertIn(
                    "capture_hf_cache_artifact",
                    inspect.getsource(loader),
                    f"{loader.__qualname__} loads pinned weights from an HF cache but records "
                    "no observation of what the cache served -- the pin is not the observation",
                )

    def test_the_orchestrator_clears_observations_at_run_start(self):
        from pipeline.orchestrator import GeperPipeline

        self.assertIn(
            "reset_loaded_model_artifacts",
            inspect.getsource(GeperPipeline._capture_startup_provenance),
            "without this a resumed or second run in one process inherits the previous "
            "run's observations -- a carried-forward claim about bytes this run never opened",
        )


if __name__ == "__main__":
    unittest.main()
