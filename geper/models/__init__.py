"""
models package.

MODEL_REGISTRY maps a short string key to its wrapper class. The
router and orchestrator look models up here by key, so adding a new
pretrained model in the future is a two-step change:

    1. Create models/<new_model>.py with a class extending
       BaseGenomicModel (see models/base_model.py).
    2. Add one line to MODEL_REGISTRY below.

No other file in the project needs to change.
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
