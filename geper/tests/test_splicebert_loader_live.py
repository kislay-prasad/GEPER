"""
Real-checkpoint regression test for a genuine bug found via a live
Colab run of this project: `pipeline/models/splicebert/loader.py
::build_model_and_tokenizer` never returned (observed as an
indefinite hang, not a crash) when loading the real SpliceBERT.1024nt
checkpoint, because `transformers`' `AutoModelForMaskedLM.from_pretrained`
pathologically hangs when TensorFlow is also importable in the same
process -- which it always is here, since `pipeline/models/mmsplice/`
requires `tensorflow` unconditionally (requirements.txt). Reproduced
on plain CPU in this dev sandbox (no GPU/Colab-specific behavior
involved) by isolating the hang to that exact call via unbuffered,
step-by-step logging, and confirmed fixed by setting `USE_TF=0` before
the `transformers` import -- even when `transformers` was already
imported earlier in the process (simulating ESM2/RNA-FM loading first,
the real pipeline's actual order).

This test is intentionally NOT mocked (unlike test_splicebert_plugin.py's
existing suite) -- it exercises the real checkpoint (already downloaded
to plugin_model_cache/splicebert/ elsewhere in this project's own
testing) through a hard wall-clock timeout, so a regression of this
exact bug fails fast and loud instead of hanging the test run silently.
Skips (rather than fails) if the checkpoint isn't present on disk in
this environment -- unlike the "_live" network tests elsewhere in this
project, this doesn't re-download it (a ~208MB Zenodo archive), it only
exercises the loading step against whatever is already cached.
"""

import multiprocessing
import os
import unittest
from pathlib import Path

from config import CONFIG

_CHECKPOINT_DIR = (
    Path(CONFIG.splicing.PLUGIN_CACHE_DIR) / "splicebert" / CONFIG.splicing.SPLICEBERT_CHECKPOINT
)
_TIMEOUT_SECS = 60


def _load_in_subprocess(checkpoint_dir: str, result_queue) -> None:
    """Runs in a fresh child process so a real hang can be killed from outside rather than wedging the test runner itself."""
    try:
        from pipeline.models.splicebert.loader import build_model_and_tokenizer

        model, tokenizer = build_model_and_tokenizer(Path(checkpoint_dir))
        result_queue.put(("ok", type(model).__name__, type(tokenizer).__name__))
    except Exception as exc:  # noqa: BLE001 - report any failure back to the parent, not just success
        result_queue.put(("error", str(exc), None))


@unittest.skipUnless(
    (_CHECKPOINT_DIR / "config.json").is_file() and (_CHECKPOINT_DIR / "pytorch_model.bin").is_file(),
    f"SpliceBERT checkpoint not present at {_CHECKPOINT_DIR} in this environment (not re-downloaded by this test -- see module docstring)",
)
class TestSpliceBertLoaderDoesNotHang(unittest.TestCase):
    def test_build_model_and_tokenizer_completes_within_timeout(self):
        ctx = multiprocessing.get_context("spawn")
        result_queue = ctx.Queue()
        proc = ctx.Process(target=_load_in_subprocess, args=(str(_CHECKPOINT_DIR), result_queue))
        proc.start()
        proc.join(timeout=_TIMEOUT_SECS)

        if proc.is_alive():
            proc.terminate()
            proc.join(5)
            self.fail(
                f"build_model_and_tokenizer() did not return within {_TIMEOUT_SECS}s -- this is exactly "
                f"the real bug this test guards against (TensorFlow-backend auto-detection hang in "
                f"transformers.AutoModelForMaskedLM.from_pretrained when tensorflow is also installed). "
                f"If this fails, check that pipeline/models/splicebert/loader.py still sets USE_TF=0 "
                f"before importing transformers."
            )

        self.assertFalse(result_queue.empty(), "subprocess exited without reporting a result")
        status, a, b = result_queue.get()
        self.assertEqual(status, "ok", msg=f"loading failed with: {a}")
        self.assertEqual(a, "BertForMaskedLM")
        self.assertEqual(b, "BertTokenizerFast")


if __name__ == "__main__":
    unittest.main()
