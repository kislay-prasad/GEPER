"""
Test-session bootstrap for running GEPER's suite in an environment
without the real multi-GB torch/tensorflow/transformers stack
installed (see _fake_heavy_deps.py's own docstring: it exists
specifically so GEPER's real code can be imported/exercised end-to-end
without those installs). Installs the fake stubs, if needed, before
any test module is collected -- a no-op wherever the real packages ARE
installed. Not part of GEPER itself; safe to remove in any environment
with the real dependencies present.
"""
import _fake_heavy_deps

_fake_heavy_deps.install()
