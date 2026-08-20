"""
tests/test_dnabert2_pin_regression.py
──────────────────────────────────────
Regression test for DNABERT-2's moving-ref RCE surface (pipeline/ai/engine.py,
DnaBertEngine._load): DNABERT-2 requires trust_remote_code=True (its MosaicBERT/
ALiBi/FlashAttention architecture ships as custom Python in the HF repo, not in
the transformers library), which without a revision pin would execute whatever
code sits on the repo's default branch at load time -- something the repo owner,
or an attacker who compromises their account, could silently repoint to
something malicious after review. The fix pins both the tokenizer and model
loads to a specific, reviewed commit (see DATA_PROVENANCE.md) when loading the
default repo, while leaving an operator-supplied model_name (e.g. a local fork)
unpinned so a stale pin can't break a deliberate override.

Mirrors tests/test_torchaudio_pin_regression.py's own rationale (a moving/
unpinned dependency ref reintroducing a fixed class of bug) but not its
mechanics -- this exercises a live loader call rather than static source
analysis, so it fakes out the torch/transformers import points entirely
(pipeline.ai.engine._try_import_torch/_try_import_transformers) and inspects
the from_pretrained call kwargs. No real model download, no dependency on
torch/transformers actually being installed in the environment running this
test.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.ai.engine import DnaBertEngine

_DEFAULT_MODEL_NAME = "zhihan1996/DNABERT-2-117M"

# The reviewed, pinned commit (see pipeline/ai/engine.py's own comment and
# DATA_PROVENANCE.md) -- hardcoded rather than imported from the module under
# test so this test actually catches someone silently changing the pin, not
# just comparing the constant to itself.
_REVIEWED_PINNED_REVISION = "7bce263b15377fc15361f52cfab88f8b586abda0"


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
    """Patch DnaBertEngine._load's two import points with fakes that need
    no real torch/transformers install and never touch the network."""
    fake_transformers = MagicMock()
    fake_transformers.AutoTokenizer.from_pretrained.return_value = MagicMock()
    fake_transformers.AutoModel.from_pretrained.return_value = _fake_loaded_model()
    return (
        patch("pipeline.ai.engine._try_import_torch", return_value=_FakeTorch()),
        patch("pipeline.ai.engine._try_import_transformers", return_value=fake_transformers),
        fake_transformers,
    )


def test_default_model_name_pins_reviewed_revision():
    torch_patch, transformers_patch, fake_transformers = _patched_imports()
    with torch_patch, transformers_patch:
        engine = DnaBertEngine(model_name=_DEFAULT_MODEL_NAME)
        loaded = engine._load()

    assert loaded is True
    tok_call = fake_transformers.AutoTokenizer.from_pretrained.call_args
    model_call = fake_transformers.AutoModel.from_pretrained.call_args
    assert tok_call is not None and model_call is not None

    for call in (tok_call, model_call):
        assert call.args[0] == _DEFAULT_MODEL_NAME
        assert call.kwargs["trust_remote_code"] is True
        assert call.kwargs["revision"] == _REVIEWED_PINNED_REVISION


def test_non_default_model_name_leaves_revision_unpinned():
    torch_patch, transformers_patch, fake_transformers = _patched_imports()
    override_name = "some-operator/dnabert2-fork"
    with torch_patch, transformers_patch:
        engine = DnaBertEngine(model_name=override_name)
        loaded = engine._load()

    assert loaded is True
    tok_call = fake_transformers.AutoTokenizer.from_pretrained.call_args
    model_call = fake_transformers.AutoModel.from_pretrained.call_args
    assert tok_call is not None and model_call is not None

    for call in (tok_call, model_call):
        assert call.args[0] == override_name
        assert call.kwargs["trust_remote_code"] is True
        assert call.kwargs["revision"] is None
