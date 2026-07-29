"""
SpliceFormer plugin support package.

- `vendor/` -- the official, unmodified Spliceformer model source
  (MIT license; see `vendor/__init__.py`).
- `loader.py` -- GEPER-side loader: builds a `SpliceFormer` instance
  with the same hyperparameters the official repo's own inference
  notebook uses, and downloads/loads one official pretrained
  checkpoint into it.

The actual `PluginModel` subclass (`SpliceFormerPlugin`) lives at
`pipeline/models/spliceformer_plugin.py`, one level up -- matching
where `EnformerPlugin`/`BorzoiPlugin` live -- so registry/import
conventions stay identical across all three plugins. This package
exists only to hold the vendored source and the loader so
`spliceformer_plugin.py` itself stays focused on the `PluginModel`
lifecycle contract, the same division of labor `pipeline/models/
mmsplice/` already uses (`loader.py` + `service.py` there).
"""
