"""
tests/test_esm2_pin_regression.py
───────────────────────────────────
Regression test for ESM-2's unpinned HuggingFace revision
(pipeline/ai/engine.py, Esm2Engine._load): without a revision pin,
from_pretrained resolves whatever is currently the repo's default-branch
tip -- a silent upstream change to facebook/esm2_t12_35M_UR50D would move
every future run's embeddings/scores with no signal that anything changed.
Unlike DNABERT-2 (see test_dnabert2_pin_regression.py), ESM-2 is a
standard transformers-library architecture -- no trust_remote_code, so the
risk here is silent drift, not remote code execution -- but the fix
mirrors the same shape: pin both the tokenizer and model loads to a
verified commit when loading the default repo, while leaving an
operator-supplied model_name (e.g. a different ESM-2 size, or a local
fork) unpinned so a stale pin can't break a deliberate override.

Mirrors test_dnabert2_pin_regression.py's mechanics exactly: fakes out
the torch/transformers import points entirely
(pipeline.ai.engine._try_import_torch/_try_import_transformers) and
inspects the from_pretrained call kwargs. No real model download, no
dependency on torch/transformers actually being installed in the
environment running this test.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.ai.engine import Esm2Engine

_DEFAULT_MODEL_NAME = "facebook/esm2_t12_35M_UR50D"

# The verified pinned commit (see pipeline/ai/engine.py's own comment) --
# hardcoded rather than imported from the module under test so this test
# actually catches someone silently changing the pin, not just comparing
# the constant to itself. Verified live via
# `git ls-remote https://huggingface.co/facebook/esm2_t12_35M_UR50D HEAD`,
# 2026-08-31.
_VERIFIED_PINNED_REVISION = "6fbf070e65b0b7291e7bbcd451118c216cff79d8"


class _FakeCuda:
    @staticmethod
    def is_available() -> bool:
        return False


class _FakeTorch:
    cuda = _FakeCuda()


def _fake_loaded_model() -> MagicMock:
    model = MagicMock()
    model.eval.return_value = model
    model.to.return_value = model
    return model


def _patched_imports():
    """Patch Esm2Engine._load's two import points with fakes that need
    no real torch/transformers install and never touch the network."""
    fake_transformers = MagicMock()
    fake_transformers.AutoTokenizer.from_pretrained.return_value = MagicMock()
    fake_transformers.AutoModel.from_pretrained.return_value = _fake_loaded_model()
    return (
        patch("pipeline.ai.engine._try_import_torch", return_value=_FakeTorch()),
        patch("pipeline.ai.engine._try_import_transformers", return_value=fake_transformers),
        fake_transformers,
    )


def test_default_model_name_pins_verified_revision():
    torch_patch, transformers_patch, fake_transformers = _patched_imports()
    with torch_patch, transformers_patch:
        engine = Esm2Engine(model_name=_DEFAULT_MODEL_NAME)
        loaded = engine._load()

    assert loaded is True
    tok_call = fake_transformers.AutoTokenizer.from_pretrained.call_args
    model_call = fake_transformers.AutoModel.from_pretrained.call_args
    assert tok_call is not None and model_call is not None

    for call in (tok_call, model_call):
        assert call.args[0] == _DEFAULT_MODEL_NAME
        assert call.kwargs["revision"] == _VERIFIED_PINNED_REVISION


def test_non_default_model_name_leaves_revision_unpinned():
    torch_patch, transformers_patch, fake_transformers = _patched_imports()
    override_name = "facebook/esm2_t33_650M_UR50D"
    with torch_patch, transformers_patch:
        engine = Esm2Engine(model_name=override_name)
        loaded = engine._load()

    assert loaded is True
    tok_call = fake_transformers.AutoTokenizer.from_pretrained.call_args
    model_call = fake_transformers.AutoModel.from_pretrained.call_args
    assert tok_call is not None and model_call is not None

    for call in (tok_call, model_call):
        assert call.args[0] == override_name
        assert call.kwargs["revision"] is None
