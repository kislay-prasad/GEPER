"""
models package.

MODEL_REGISTRY maps a short string key to its wrapper class. The
router and orchestrator look models up here by key.

CORRECTED 2026-08-22 -- this docstring used to end here with "adding a
new pretrained model in the future is a two-step change: 1. Create
models/<new_model>.py with a class extending BaseGenomicModel. 2. Add
one line to MODEL_REGISTRY below. No other file in the project needs
to change." That was TRUE WHEN WRITTEN, for the five models this
package registered at the time (hyenadna/evo2/rna_fm/esm2/
alphamissense, all below). It stopped being true as GEPER correctly
added two more ways to add a model, neither of which this file was
ever updated to describe:

  - MMSpliceModel (`pipeline/models/mmsplice/loader.py`) DOES extend
    BaseGenomicModel, but is NOT registered in MODEL_REGISTRY below --
    doing so from this file creates a real import cycle (this
    package's own `__init__.py` -> `pipeline.models.mmsplice.loader`
    -> `models.base_model`, which needs this package, still mid-
    import). It is instead added to an *extended* registry built in
    `pipeline/orchestrator.py` (see the comment on MODEL_REGISTRY
    below for the full mechanism). So even the original two-step
    contract already had a third file, for one model, before this
    correction -- that exception was documented in this file's own
    comment directly above MODEL_REGISTRY below, but was never folded
    back up into the claim at the top of this docstring.
  - Enformer, Borzoi, SpliceFormer, SpliceBERT, and SPiP -- 5 more
    models -- use a DIFFERENT contract entirely and are not in
    MODEL_REGISTRY at all. Each subclasses `PluginModel` (`pipeline/
    models/base.py`), not `BaseGenomicModel`, and registers through
    `pipeline/models/pending_plugins.py::build_default_registry()`
    (which calls `ModelRegistry.register()` for each, consumed by
    `pipeline/models/manager.py::ModelManager`), plus its own
    `config.py` `ENABLE_<MODEL>` flag (`ENABLE_ENFORMER`,
    `ENABLE_BORZOI`, `ENABLE_SPLICEFORMER`, `ENABLE_SPLICEBERT`,
    `ENABLE_SPIP`) so it can be disabled per-deployment. Three files
    change for one of these, not one.

So, as of this correction, "how do I add a model" has two right
answers depending on which contract the new model fits:

  1. BaseGenomicModel + MODEL_REGISTRY (this file) -- for a model like
     the five already here: always-on, no config-gated enable/disable
     needed. Create models/<new_model>.py with a class extending
     BaseGenomicModel (see models/base_model.py), add one line to
     MODEL_REGISTRY below. Watch for the MMSplice-style import-cycle
     case: if the new model's module needs to import something that
     itself (transitively) imports this `models` package, it cannot be
     registered here either, and needs the same orchestrator.py-
     extended-registry treatment MMSplice got.
  2. PluginModel + ModelManager -- for an optional/heavier model that
     needs its own enable/disable flag, lazy loading, or the shared
     network-error handling `pipeline/models/base.py`'s `PluginModel`
     already provides (see any of the five `*_plugin.py` files under
     `pipeline/models/`). Subclass `PluginModel`, register it in
     `pipeline/models/pending_plugins.py::build_default_registry()`,
     add a `config.py` `ENABLE_<MODEL>` flag. This package
     (`models/__init__.py`, `models/base_model.py`) is not touched at
     all for this path -- which is exactly why this docstring never
     went stale-and-caught: nothing here breaks when a model is added
     this way, it just stops being a complete answer to "how do I add
     a model".
"""

from models.alphamissense import AlphaMissenseModel
from models.esm2 import ESM2Model
from models.evo2 import Evo2Model
from models.hyenadna import HyenaDNAModel
from models.rna_fm import RNAFMModel

# NOTE: MMSpliceModel (pipeline/models/mmsplice/loader.py) is
# deliberately NOT imported/registered here. It subclasses
# BaseGenomicModel (`from models.base_model import BaseGenomicModel`),
# and this file is the `models` package's own `__init__.py` -- adding
# `from pipeline.models.mmsplice.loader import MMSpliceModel` at this
# module's top level creates a real two-way import cycle:
# `models` (this file) -> `pipeline.models.mmsplice.loader` ->
# `models.base_model` -> (needs the `models` package, which is still
# mid-import) -> back to this file. Whenever some other module imports
# `pipeline.models.mmsplice.*` *before* anything has imported `models`,
# Python hits exactly that cycle from the other direction and raises
# "cannot import name 'MMSpliceModel' from partially initialized
# module" (reproduced and confirmed while building this integration).
#
# MMSpliceModel is instead added to an *extended* registry built in
# `pipeline/orchestrator.py` (see `MODEL_REGISTRY` there, which merges
# this package's `MODEL_REGISTRY` with `{"mmsplice": MMSpliceModel}`)
# -- the orchestrator is not part of the `models` package, so no cycle
# is created, and every other model here is completely unaffected.
MODEL_REGISTRY = {
    "hyenadna": HyenaDNAModel,
    "evo2": Evo2Model,
    "rna_fm": RNAFMModel,
    "esm2": ESM2Model,
    "alphamissense": AlphaMissenseModel,
}

__all__ = [
    "HyenaDNAModel",
    "Evo2Model",
    "RNAFMModel",
    "ESM2Model",
    "AlphaMissenseModel",
    "MODEL_REGISTRY",
]
