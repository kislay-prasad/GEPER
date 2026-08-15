"""
MMSplice model loader.

WHY THIS DOESN'T `import mmsplice` (read before "simplifying" this)
--------------------------------------------------------------------
See the top-of-file docstring in `pipeline/models/mmsplice/utils.py`
for the full dependency-chain analysis. Short version: the official
`mmsplice` PyPI package pins `cyvcf2<=0.30.15`, which cannot be built
on CPython 3.12+ and has no matching prebuilt wheel, and `mmsplice`'s
own `__init__.py` unconditionally imports that entire chain
(`mmsplice.mmsplice` -> `mmsplice.utils` / `mmsplice.exon_dataloader`
-> `kipoiseq`/`kipoi`/`pyranges` -> `cyvcf2`) even though GEPER never
uses any of the VCF-dataloader functionality those packages exist
for.

This loader therefore:
  1. Installs the `mmsplice` PyPI package with `--no-deps` (so pip
     never even attempts to resolve/build cyvcf2), purely to obtain
     the package's *data files* -- the five official pretrained Keras
     `.h5` weight files under `<mmsplice>/models/`.
  2. Loads `<mmsplice>/layers.py` directly **by file path** via
     `importlib.util.spec_from_file_location`, bypassing Python's
     normal package-import machinery entirely so `mmsplice/__init__.py`
     is never executed and the kipoiseq/cyvcf2 chain is never touched.
     `layers.py` itself imports only `tensorflow`/`scipy`/`numpy` (no
     kipoiseq), so this is safe and self-contained. It defines the two
     custom Keras layers (`ConvDNA`, `GlobalAveragePooling1D_Mask0`)
     the five `.h5` files need to deserialize.
  3. Loads each `.h5` file with `tensorflow.keras.models.load_model`,
     giving those two layer classes as `custom_objects`.

Every one of the five submodels loaded this way is the real, official,
unmodified MMSplice network (Cheng et al. 2019, *Genome Biology*) --
this is not a re-trained or approximated model.

This same "load once, cache, reuse" contract every other GEPER model
uses (`BaseGenomicModel` + `ModelCache`) applies here unchanged: the
five Keras models are loaded exactly once per process and reused for
every subsequent variant.
"""

import importlib.util
import os
from typing import Any, Dict, List, Optional, Tuple

from config import CONFIG
from models.base_model import BaseGenomicModel
from pipeline.models.mmsplice.models import MODEL_FILENAMES, MODULE_NAMES, ModularScores
from pipeline.models.mmsplice.utils import SeqSplitter, encode_batch
from utils.auto_install import ensure_pip_package_available, is_pip_package_installed
from utils.exceptions import ModelInferenceError, ModelLoadError
from utils.logger import get_logger

logger = get_logger(__name__)

_MMSPLICE_PIP_NAME = "mmsplice"
# Cache of the --no-deps auto-install outcome, same "cheap check,
# install-once, cache the outcome" shape as utils/auto_install.py.
_NO_DEPS_INSTALL_RESULT: Dict[str, bool] = {}


def _ensure_mmsplice_package_files_available() -> bool:
    """
    Idempotent, automatic setup: installs the `mmsplice` PyPI package
    with `--no-deps` (see module docstring) if it isn't already
    present, purely so its bundled `.h5` weight files and `layers.py`
    exist on disk. Never triggers a normal `import mmsplice` anywhere
    in this process.
    """
    if is_pip_package_installed(_MMSPLICE_PIP_NAME):
        return True
    if _MMSPLICE_PIP_NAME in _NO_DEPS_INSTALL_RESULT:
        return _NO_DEPS_INSTALL_RESULT[_MMSPLICE_PIP_NAME]

    import subprocess
    import sys

    logger.info(
        "'mmsplice' package files are not present; installing "
        "'mmsplice' with --no-deps now (its own pinned 'cyvcf2<=0.30.15' "
        "dependency cannot build on this Python and is not needed by "
        "GEPER's integration -- see pipeline/models/mmsplice/utils.py "
        "module docstring)."
    )
    base_cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--no-deps", _MMSPLICE_PIP_NAME]
    result = subprocess.run(base_cmd, capture_output=True, text=True)
    if result.returncode != 0 and "externally-managed-environment" in (result.stderr or ""):
        result = subprocess.run(base_cmd + ["--break-system-packages"], capture_output=True, text=True)
    ok = result.returncode == 0 and is_pip_package_installed(_MMSPLICE_PIP_NAME)
    if not ok:
        logger.error(f"Automatic 'mmsplice --no-deps' installation failed: {(result.stderr or '').strip()[-500:]}")
    _NO_DEPS_INSTALL_RESULT[_MMSPLICE_PIP_NAME] = ok
    return ok


def _resolve_mmsplice_package_dir() -> Optional[str]:
    """
    Locate the installed `mmsplice` package directory on disk without
    ever importing it (`find_spec` only resolves the module's location;
    it does not execute `__init__.py`). Honors
    `CONFIG.mmsplice.MODEL_DIR` first, for air-gapped/custom
    deployments that vendor the `.h5` files directly.
    """
    if CONFIG.mmsplice.MODEL_DIR:
        return CONFIG.mmsplice.MODEL_DIR
    spec = importlib.util.find_spec(_MMSPLICE_PIP_NAME)
    if spec is None or not spec.submodule_search_locations:
        return None
    return list(spec.submodule_search_locations)[0]


def _load_module_from_path(module_name: str, file_path: str):
    """Load a single .py file as a standalone module, bypassing any parent package __init__."""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ModelLoadError(f"Could not create an import spec for '{file_path}'.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MMSpliceModel(BaseGenomicModel):
    """
    Loads and holds the five pretrained MMSplice Keras submodels.

    Unlike the embedding-based models (HyenaDNA, RNA-FM,
    ESM-2), MMSplice's public contract goes beyond a single
    "sequence in -> result out" call: variant-level eligibility,
    exon-annotation lookup, ref/alt window construction, caching, and
    interpretation all live in `service.py` (mirroring how
    `models/alphamissense.py` also departs from the plain embedding
    contract for a domain-specific reason). This class implements the
    required `BaseGenomicModel` contract for startup validation and
    lifecycle management, and additionally exposes `score_modular()`,
    used internally by `predictor.py`.
    """

    def __init__(self):
        super().__init__()
        # TensorFlow, not PyTorch -- self.device (from BaseGenomicModel)
        # is kept as CPU for reporting purposes; actual TF device
        # placement is handled separately via `_tf_device()`.
        import torch

        self.device = torch.device("cpu")
        self._keras_models: Dict[str, Any] = {}
        self._splitter = SeqSplitter()
        self._model_version = "mmsplice-2.4.0 (official pretrained weights)"

    def cache_key(self) -> str:
        return "mmsplice"

    @classmethod
    def is_available(cls) -> bool:
        if not CONFIG.mmsplice.ENABLED:
            return False
        if not ensure_pip_package_available("tensorflow"):
            return False
        return _ensure_mmsplice_package_files_available()

    @classmethod
    def unavailability_reason(cls) -> str:
        if not CONFIG.mmsplice.ENABLED:
            return "disabled via configuration (GEPER_ENABLE_MMSPLICE=false)"
        if not is_pip_package_installed("tensorflow"):
            return "tensorflow could not be installed automatically"
        return "mmsplice package files could not be installed automatically (--no-deps)"

    def _tf_device(self) -> str:
        cfg_device = (CONFIG.mmsplice.DEVICE or "auto").strip().lower()
        if cfg_device == "cpu":
            return "/CPU:0"
        try:
            import tensorflow as tf

            gpus = tf.config.list_physical_devices("GPU")
        except Exception:  # noqa: BLE001
            gpus = []
        if cfg_device == "cuda" and not gpus:
            self.logger.warning(
                "GEPER_MMSPLICE_DEVICE=cuda requested but no GPU is visible to "
                "TensorFlow; falling back to CPU for MMSplice."
            )
        return "/GPU:0" if gpus and cfg_device in ("auto", "cuda") else "/CPU:0"

    def _load_impl(self):
        if not CONFIG.mmsplice.ENABLED:
            raise ModelLoadError("MMSplice is disabled via configuration (GEPER_ENABLE_MMSPLICE=false).")
        if not ensure_pip_package_available("tensorflow"):
            raise ModelLoadError("MMSplice requires 'tensorflow', which could not be installed automatically.")
        if not _ensure_mmsplice_package_files_available():
            raise ModelLoadError(
                "MMSplice requires the 'mmsplice' package's bundled model files, which "
                "could not be installed automatically (pip install mmsplice --no-deps)."
            )

        # Round 22: every raise below this point used to embed a local
        # filesystem path directly into the exception it raises. This
        # method is called (via `instance.predict(dummy_sequence)`) from
        # `pipeline/orchestrator.py`'s startup validation loop, whose
        # `except (ModelLoadError, ModelInferenceError)` branch stores
        # `str(exc)[:600]` verbatim in `self._model_stage_errors["mmsplice"]`
        # -- which `pipeline/models/status.py::_mmsplice_status` then
        # returns as-is as the "reason" for a FAILED MMSplice entry in
        # `build_ai_model_status()`'s output, which
        # `report/report_generator.py::_render_ai_model_status` renders
        # into the "### AI Models" table on EVERY report unconditionally
        # (that method's own docstring: never allowed to return empty).
        # Same leak class round 20/21 fixed for SpliceBERT, traced end to
        # end here rather than assumed. Full detail (including every
        # path) now goes to `self.logger.warning(..., exc_info=True)`
        # only; what's raised keeps enough to be actionable (env var
        # names, bare filenames) without naming this machine's own
        # directory layout.
        package_dir = _resolve_mmsplice_package_dir()
        if not package_dir or not os.path.isdir(package_dir):
            self.logger.warning(
                f"Could not resolve the installed 'mmsplice' package directory (resolved: {package_dir!r})."
            )
            raise ModelLoadError(
                "Could not resolve the installed 'mmsplice' package directory. Set "
                "GEPER_MMSPLICE_MODEL_DIR to a directory containing layers.py and a "
                "models/ subdirectory with Acceptor.h5, Donor.h5, Exon.h5, Intron3.h5, "
                "Intron5.h5."
            )

        layers_path = os.path.join(package_dir, "layers.py")
        if not os.path.isfile(layers_path):
            self.logger.warning(f"'{layers_path}' not found -- cannot load MMSplice's custom Keras layers.")
            raise ModelLoadError(
                "MMSplice's custom Keras layers file ('layers.py') was not found in the resolved package directory."
            )
        layers_module = _load_module_from_path("_geper_mmsplice_layers", layers_path)

        from tensorflow.keras.models import load_model  # local import: only needed once loaded

        custom_objects = {
            "ConvDNA": layers_module.ConvDNA,
            "GlobalAveragePooling1D_Mask0": layers_module.GlobalAveragePooling1D_Mask0,
        }

        tf_device = self._tf_device()
        try:
            import tensorflow as tf

            with tf.device(tf_device):
                for module_name, filename in MODEL_FILENAMES.items():
                    h5_path = os.path.join(package_dir, "models", filename)
                    if not os.path.isfile(h5_path):
                        self.logger.warning(
                            f"MMSplice weight file '{h5_path}' not found under the "
                            f"resolved package directory '{package_dir}'."
                        )
                        raise ModelLoadError(f"MMSplice weight file '{filename}' not found in the installed package.")
                    self._keras_models[module_name] = load_model(h5_path, compile=False, custom_objects=custom_objects)
        except ModelLoadError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"Failed to load one or more MMSplice Keras submodels: {exc}", exc_info=True)
            raise ModelLoadError(
                "Failed to load one or more MMSplice Keras submodels; see server logs for detail."
            ) from exc

        self.model = self._keras_models  # satisfies BaseGenomicModel._verify_materialized's `is None` check
        self.tokenizer = None
        self._tf_device_str = tf_device
        logger.info(
            f"Loaded all 5 MMSplice submodels ({', '.join(MODULE_NAMES)}) from "
            f"'{package_dir}' onto TensorFlow device '{tf_device}'."
        )

    def _verify_materialized(self) -> None:
        # BaseGenomicModel._verify_materialized() inspects
        # `self.model.named_parameters()` / `named_buffers()`, which
        # assumes a torch.nn.Module. self.model here is a plain dict of
        # Keras models (no torch parameters at all -- see
        # models/alphamissense.py for the same kind of override, for
        # the same reason: this check is meaningless for a non-torch
        # model and must not be silently skipped nor made to crash).
        return

    def _report_precision(self) -> str:
        return f"n/a (TensorFlow/Keras models, device={getattr(self, '_tf_device_str', 'n/a')})"

    def reported_max_length(self):
        return None  # variable-length exon/intron windows; no single fixed max.

    def _infer_impl(self, sequence: str, **kwargs) -> Dict[str, Any]:
        """
        Startup-validation contract only (see class docstring for why
        real variant-level scoring lives in `score_modular` /
        `predictor.py` / `service.py` instead). `sequence` here is a
        single overhanged-exon window string; returns its 5 modular
        scores as a sanity check that every submodel is loaded and
        runnable.
        """
        overhang = kwargs.get("overhang", (100, 100))
        scores = self.score_modular(sequence, overhang)
        return {"modular_scores": scores.as_list()}

    def score_modular(self, window_sequence: str, overhang: Tuple[int, int]) -> ModularScores:
        """
        Split one overhanged-exon window into its five module inputs,
        one-hot encode each, and run every submodel -- the same
        computation `mmsplice.MMSplice.predict_on_seq` performs,
        reimplemented on top of the models loaded above (see this
        file's and utils.py's module docstrings for why we don't call
        the official package's version of this function directly).
        """
        if not self._loaded:
            self.load()
        splits = self._splitter.split(window_sequence, overhang)

        import tensorflow as tf

        raw_scores: Dict[str, float] = {}
        with tf.device(getattr(self, "_tf_device_str", "/CPU:0")):
            for module_name in MODULE_NAMES:
                encoded = encode_batch([splits[module_name]])
                prediction = self._keras_models[module_name].predict(encoded, verbose=0)
                value = float(prediction[0][0])
                # The acceptor/donor site models are trained as binary
                # classifiers (sigmoid output); MMSplice's official
                # `predict_modular_scores_on_batch` applies `logit()`
                # to exactly these two before combining (see utils.py
                # module docstring / mmsplice.mmsplice.MMSplice's own
                # `np.concatenate([..., logit(acceptorM...), ...,
                # logit(donorM...), ...])`) so all five modules end up
                # on the same logit-scale before the linear model.
                if module_name in ("acceptor", "donor"):
                    from pipeline.models.mmsplice.utils import logit
                    import numpy as np

                    value = float(logit(np.array([value]))[0])
                raw_scores[module_name] = value

        return ModularScores.from_list([raw_scores[name] for name in MODULE_NAMES])

    def score_modular_batch(self, window_sequences: List[str], overhang: Tuple[int, int]) -> List[ModularScores]:
        """
        Batch variant of `score_modular` -- splits every window first,
        then runs each of the five submodels once per batch (one Keras
        `.predict()` call per module across all variants) rather than
        once per variant, which is where MMSplice's real per-call
        overhead lives (requirement #12: batch inference support).
        """
        if not self._loaded:
            self.load()
        if not window_sequences:
            return []

        splits_per_module: Dict[str, List[str]] = {name: [] for name in MODULE_NAMES}
        for seq in window_sequences:
            splits = self._splitter.split(seq, overhang)
            for module_name in MODULE_NAMES:
                splits_per_module[module_name].append(splits[module_name])

        import tensorflow as tf

        from pipeline.models.mmsplice.utils import logit as _logit

        per_module_values: Dict[str, List[float]] = {}
        with tf.device(getattr(self, "_tf_device_str", "/CPU:0")):
            for module_name in MODULE_NAMES:
                encoded = encode_batch(splits_per_module[module_name])
                predictions = self._keras_models[module_name].predict(
                    encoded, verbose=0, batch_size=CONFIG.mmsplice.BATCH_SIZE
                )
                values = predictions[:, 0].astype(float)
                if module_name in ("acceptor", "donor"):
                    values = _logit(values)
                per_module_values[module_name] = values.tolist()

        results = []
        for i in range(len(window_sequences)):
            results.append(ModularScores.from_list([per_module_values[name][i] for name in MODULE_NAMES]))
        return results

    def score_single_module(self, module_name: str, seq: str) -> float:
        """
        Score one already-correctly-sized raw sequence fragment (e.g. an
        18bp donor-frame or 53bp acceptor-frame slice) directly with a
        single named submodel, bypassing `SeqSplitter` entirely. Used
        by `predictor.py`'s nearby-cryptic-site scan, which needs to
        score many small candidate windows that don't correspond to
        "the" annotated splice site.
        """
        return self.score_single_module_batch(module_name, [seq])[0]

    def score_single_module_batch(self, module_name: str, seqs: List[str]) -> List[float]:
        """Batched variant of `score_single_module` for a cryptic-site scan's many candidate windows."""
        if not seqs:
            return []
        if not self._loaded:
            self.load()
        if module_name not in self._keras_models:
            raise ModelInferenceError(f"Unknown MMSplice module '{module_name}'.")

        import tensorflow as tf

        from pipeline.models.mmsplice.utils import logit as _logit

        with tf.device(getattr(self, "_tf_device_str", "/CPU:0")):
            encoded = encode_batch(seqs)
            values = (
                self._keras_models[module_name]
                .predict(encoded, verbose=0, batch_size=CONFIG.mmsplice.BATCH_SIZE)[:, 0]
                .astype(float)
            )
        if module_name in ("acceptor", "donor"):
            values = _logit(values)
        return values.tolist()

    @property
    def model_version(self) -> str:
        return self._model_version
