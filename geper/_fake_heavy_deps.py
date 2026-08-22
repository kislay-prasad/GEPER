"""
Test/benchmark-only helper: installs minimal fake `torch` and
`transformers` modules into sys.modules when the real packages aren't
installed, so benchmark_blast.py (and similar local harnesses) can
import GEPER's `models` / `pipeline.orchestrator` modules -- which
declare `import torch` / `from transformers import ...` at module
level -- without needing a multi-hundred-MB real install in this
sandbox.

This is NOT part of GEPER itself and is never imported by any
production code path (main.py, pipeline/, database/, models/). It
exists purely so this benchmark can exercise the real orchestrator
code end-to-end. Every model's actual `_load_impl`/`_infer_impl` is
separately replaced with a fake in benchmark_blast.py, so the only
thing these stubs need to support is what runs *around* that: device
detection, the `torch.inference_mode()` context manager, and a few
type-hint attribute lookups -- never real tensor math.

If the real `torch` is already importable (e.g. this is run somewhere
with it installed), this is a no-op and the real package is used.
"""

import contextlib
import importlib.machinery
import sys
import types

from utils.logger import get_logger

logger = get_logger(__name__)


def _stub_module(name: str) -> types.ModuleType:
    """
    Create a stub module that behaves like a real one under
    `importlib.util.find_spec`.

    A bare `types.ModuleType` has `__spec__ = None`, and `find_spec` on an
    already-imported module with no spec raises
    `ValueError: <name>.__spec__ is None` rather than returning None. GEPER
    probes optional dependencies exactly that way
    (`utils/auto_install.py::is_pip_package_installed`, called from every
    plugin's `is_available()` during pipeline construction), so a
    spec-less stub turns a clean "package absent" into a crash.
    """
    module = types.ModuleType(name)
    module.__spec__ = importlib.machinery.ModuleSpec(name, loader=None)
    return module


def install() -> None:
    """
    Install whichever of the `torch` / `transformers` stubs this
    environment actually needs.

    The two are checked independently on purpose. They used to share a
    single guard -- an early `return` as soon as real `torch` imported --
    which silently skipped the `transformers` stub in the (real, and
    common) case of a machine with torch installed but not transformers.
    The symptom was that `import pipeline.orchestrator` still raised
    `ModuleNotFoundError: No module named 'transformers'` even after
    calling `install()`, because `models/esm2.py` imports it at module
    level. Each stub now guards only on its own package.
    """
    _install_torch_stub()
    _install_transformers_stub()


def _install_torch_stub() -> None:
    try:
        import torch  # noqa: F401

        return  # real torch already available -- nothing to stub
    except Exception:
        # A partially-broken local install (e.g. missing shared
        # libraries) can leave a half-initialized module behind in
        # sys.modules even though the import raised -- clear it so
        # our stub is used instead of that broken partial module.
        for name in list(sys.modules):
            if name == "torch" or name.startswith("torch."):
                del sys.modules[name]

    # [[conftest-silently-substitutes-fake-heavy-deps-with-no-signal]]:
    # this used to be silent -- a stub activating and real torch loading
    # produced identical output, so nothing in a test run's own log
    # would tell anyone this session is exercising fake tensor math
    # rather than the real library.
    logger.warning(
        "Real 'torch' is not importable in this environment -- installing the "
        "fake torch stub (_fake_heavy_deps.py). Any test relying on real "
        "tensor computation, not just module presence, is NOT exercising "
        "real behavior in this run."
    )

    torch_mod = _stub_module("torch")
    torch_mod.__version__ = "0.0.0-fake-stub"

    class _FakeDevice:
        def __init__(self, kind: str):
            self.type = kind.split(":")[0]
            self._repr = kind

        def __str__(self) -> str:
            return self._repr

        def __repr__(self) -> str:
            return f"device(type='{self.type}')"

    torch_mod.device = _FakeDevice

    class _FakeTensor:
        """Exists only so `x: torch.Tensor` type hints resolve."""

    torch_mod.Tensor = _FakeTensor

    torch_mod.cuda = types.SimpleNamespace(
        is_available=lambda: False,
        get_device_name=lambda i=0: "fake-gpu",
        get_device_properties=lambda i=0: types.SimpleNamespace(total_memory=0),
        get_device_capability=lambda device=None: (0, 0),
        memory_allocated=lambda: 0,
        memory_reserved=lambda: 0,
        empty_cache=lambda: None,
    )
    # No `mps` attribute on purpose: device_utils.py does
    # `getattr(torch.backends, "mps", None) is not None` and correctly
    # falls through to CPU when it's absent.
    torch_mod.backends = types.SimpleNamespace()

    @contextlib.contextmanager
    def _noop_ctx(*_args, **_kwargs):
        yield

    torch_mod.inference_mode = _noop_ctx
    torch_mod.no_grad = _noop_ctx
    torch_mod.float16 = "float16"
    torch_mod.float32 = "float32"
    # Added for Evo2 (models/evo2.py sets self._dtype = torch.bfloat16 in
    # _load_impl, and tests/test_evo2.py exercises that same path) --
    # missing originally because this stub predates Evo2's integration.
    torch_mod.bfloat16 = "bfloat16"

    def _fake_tensor_fn(*_args, **_kwargs):
        return _FakeTensor()

    for name in ("tensor", "stack", "ones_like", "ones", "clamp", "sum", "max", "linspace", "arange", "load"):
        setattr(torch_mod, name, _fake_tensor_fn)

    nn_mod = _stub_module("torch.nn")

    class _Module:
        def __init__(self, *args, **kwargs):
            pass

    nn_mod.Module = _Module
    torch_mod.nn = nn_mod

    sys.modules["torch"] = torch_mod
    sys.modules["torch.nn"] = nn_mod


def _install_transformers_stub() -> None:
    """Stub only what GEPER's model modules import at module level."""
    try:
        import transformers  # noqa: F401

        return  # real transformers already available -- nothing to stub
    except Exception:
        for name in list(sys.modules):
            if name == "transformers" or name.startswith("transformers."):
                del sys.modules[name]

    # See _install_torch_stub's identical comment --
    # [[conftest-silently-substitutes-fake-heavy-deps-with-no-signal]].
    logger.warning(
        "Real 'transformers' is not importable in this environment -- "
        "installing the fake transformers stub (_fake_heavy_deps.py). Any "
        "test relying on real model loading, not just module presence, is "
        "NOT exercising real behavior in this run."
    )

    transformers_mod = _stub_module("transformers")
    transformers_mod.__version__ = "0.0.0-fake-stub"

    class _FakeAutoClass:
        @classmethod
        def from_pretrained(cls, *args, **kwargs):
            return object()

    for name in ("AutoConfig", "AutoModel", "AutoTokenizer", "AutoModelForMaskedLM", "EsmModel"):
        setattr(transformers_mod, name, _FakeAutoClass)

    utils_mod = _stub_module("transformers.utils")
    utils_mod.cached_file = lambda *args, **kwargs: None
    transformers_mod.utils = utils_mod

    sys.modules["transformers"] = transformers_mod
    sys.modules["transformers.utils"] = utils_mod
