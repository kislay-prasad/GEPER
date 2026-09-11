"""The offline acceptance verdict must be able to come back red.

`scripts/verify_offline_models.py` is what `scripts/build_bridge_ready.sh` runs
under `docker run --network none`, and its exit code is the only thing standing
between "the image built" and "the image works with no network".

Until 2026-09-11 its PASS was the literal `True` -- it meant "no exception was
raised" -- and the evidence beside it was printed, listed in the summary, and
never consulted. `params=0.0M`, `submodels=0`, and the bare string `constructed`
(the total absence of evidence) all reported PASS. `_submodel_count`'s own
docstring said what the evidence was for: it "distinguishes 'the object exists'
from 'the weights were materialised'". It was written for exactly that and then
not used to decide anything.

These tests call the REAL `verdict()` from the REAL file. The decision was
extracted out of `main()` precisely so that they could: `main()` imports the four
model packages, so while the decision lived inside it the only way to test the
verdict was to transcribe it -- and a transcription drifts.

NO TORCH, NO MODELS, NO NETWORK, NO DOCKER. The module's top-level imports are
stdlib only; the model imports are inside the loader functions. The fakes below
supply the two shapes `verdict()` knows how to read.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify_offline_models.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("verify_offline_models", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def vom():
    assert SCRIPT.is_file(), f"the acceptance verifier is missing: {SCRIPT}"
    return _load_module()


class _Param:
    def __init__(self, n: int) -> None:
        self._n = n

    def numel(self) -> int:
        return self._n


class _TorchLike:
    """Something with .parameters(), the shape `_param_count` reads."""

    def __init__(self, params) -> None:
        self._params = params

    def parameters(self):
        return self._params


class _Model:
    """A loader's return value: the thing whose `.model` holds the weights."""

    def __init__(self, inner) -> None:
        self.model = inner


# --------------------------------------------------------------------------
# IT COMES BACK RED. These three are the exact states that used to report PASS.
# --------------------------------------------------------------------------


def test_zero_parameters_is_not_a_pass(vom):
    ok, evidence = vom.verdict(_Model(_TorchLike([])))
    assert ok is False
    assert evidence == "params=0.0M"


def test_zero_submodels_is_not_a_pass(vom):
    ok, evidence = vom.verdict(_Model([]))
    assert ok is False
    assert evidence == "submodels=0"


def test_no_evidence_at_all_is_not_a_pass(vom):
    """The one that reads most like a success and is the emptiest."""
    ok, evidence = vom.verdict(_Model(object()))
    assert ok is False
    assert "NO EVIDENCE" in evidence


# --------------------------------------------------------------------------
# AND IT STILL PASSES REAL MODELS. A check that always fires is also useless,
# and these two shapes are what the four shipped models actually present.
# --------------------------------------------------------------------------


def test_a_real_torch_model_passes(vom):
    """ESM2/HyenaDNA/RNA-FM: torch modules, so the evidence is a parameter count."""
    ok, evidence = vom.verdict(_Model(_TorchLike([_Param(650_000_000)])))
    assert ok is True
    assert evidence == "params=650.0M"


def test_mmsplice_five_submodels_passes(vom):
    """MMSplice sets `self.model` to its list of five Keras submodels."""
    ok, evidence = vom.verdict(_Model([object()] * 5))
    assert ok is True
    assert evidence == "submodels=5"


# --------------------------------------------------------------------------
# The evidence functions themselves were always correct -- the defect was that
# nothing read them. Pinned so a future "simplification" cannot quietly undo it.
# --------------------------------------------------------------------------


def test_param_count_counts_and_declines(vom):
    assert vom._param_count(_Model(_TorchLike([_Param(1), _Param(2)]))) == 3
    assert vom._param_count(_Model(object())) is None, "a non-torch model is not an error"


def test_submodel_count_counts_and_declines(vom):
    assert vom._submodel_count(_Model([1, 2, 3, 4, 5])) == 5
    assert vom._submodel_count(_Model(object())) is None


def test_the_verdict_is_reachable_from_the_summary_path(vom):
    """
    The guard against the defect coming back in a different shape: `main()` must
    take its `ok` from `verdict()`, not from a literal. If someone re-inlines the
    decision, this fails and says why.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    assert "ok, evidence = verdict(model)" in source, (
        "main() no longer takes its verdict from verdict(); if the decision has been "
        "re-inlined, the evidence can stop being read again and nothing here would see it"
    )
    assert not any(line.strip() == "results.append((name, True, evidence))" for line in source.splitlines()), (
        "the literal True is back in the results append -- that IS the defect"
    )
