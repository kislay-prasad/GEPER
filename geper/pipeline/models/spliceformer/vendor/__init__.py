"""
Vendored, unmodified copy of the official Spliceformer model-definition
source (github.com/benniatli/Spliceformer, MIT license -- see
`LICENSE_HEADER.txt` in this directory and
`pipeline/models/spliceformer_plugin.py` for the full license
verification writeup).

Only two files are vendored here: `model.py` (the `SpliceFormer` /
`SpliceAI` `nn.Module` definitions) and `weight_init.py` (the
`keras_init` initializer `model.py` imports and applies to its
internal `SpliceAI` encoder). These are copied byte-for-byte from
`Code/src/model.py` and `Code/src/weight_init.py` in the upstream
repository (commit tagged `v1.0.0`) -- nothing in either file has been
rewritten, simplified, or otherwise altered; the only change anywhere
in this package is this docstring/header commentary. Every other file
in the upstream `Code/src/` directory (`create_dataset.py`,
`dataloader.py`, `evaluation_metrics.py`, `gpu_metrics.py`,
`losses.py`, `train.py`) is training/evaluation-only infrastructure
that GEPER's inference-only plugin has no use for and does not vendor.

Do not edit `model.py` or `weight_init.py` in this directory by hand.
If the upstream architecture ever changes, re-vendor both files from
the new upstream commit/tag in full, rather than hand-patching them,
so this directory always reflects a real, verifiable upstream state.
"""
