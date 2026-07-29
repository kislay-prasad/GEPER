"""
pipeline.models package.

Houses model integrations that are architecturally heavier than a
single-file wrapper in the top-level `models/` package (see
`models/__init__.py` and `models/base_model.py`) -- e.g. a model that
needs its own loader/predictor/service/cache split because it
requires auxiliary annotation lookups, a bespoke dependency-avoidance
strategy, or its own result cache.

Currently: `pipeline.models.mmsplice` (splice-effect prediction).
"""
