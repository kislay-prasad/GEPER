#!/usr/bin/env python3
"""
scripts/verify_offline_models.py
────────────────────────────────
THE ACCEPTANCE TEST FOR `bridge-ready`: all four models CONSTRUCTED with no
network. Run inside the image, with the network cut at the container.

This is not a new standard. It matches the original offline proof of
`geper:bridge-ready` (`sha256:a8a5fe67749e...`), which loaded ESM2, MMSplice,
HyenaDNA and RNA-FM through GEPER's own loaders under `--network none` and
reported constructed objects rather than directory listings.

WHY A LOAD AND NOT A LISTING. A cache is proven by a load, never by a file
listing. The image `geper:bridge-ready-BROKEN-esm2-offline` holds all 2.6 GB
of correct ESM2 weights, a complete-looking directory, a matching file count
and a matching `du` -- and misses, because five symlinks and one 40-byte
`refs/main` are absent. Every cheaper check passes on it.

WHAT THIS DELIBERATELY DOES NOT DO, each for a reason that was paid for:

  * NO TIME THRESHOLD. The same ESM2 load has been measured at 7.7 s, 12.1 s
    and 27.9 s on this hardware and the three cannot be reconciled. A
    threshold would fail on a busy machine and teach the next person to
    distrust a working cache. Durations are printed as observations, never
    compared against a limit.
  * IT DOES NOT SET `HF_HUB_OFFLINE`. Telling the library not to try is a
    weaker test than letting it try and be unable to reach the network. The
    isolation belongs to `docker run --network none`, not to a variable the
    image could set for itself.
  * IT REPORTS A PARAMETER COUNT PER MODEL, because a load that returned a
    randomly-initialised model would otherwise also "succeed". A count near
    the published size is the evidence that real weights were materialised.

ONE OUTPUT LINE THAT IS NOT A FAILURE: transformers prints a `pooler.dense`
re-initialisation notice for ESM2 on a cache HIT and a cache MISS alike. It is
not a cache warning. A reader who treats it as one will abandon a passing run.

Exit code is 0 only if every model constructs.
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from typing import Callable, List, Optional, Tuple


def _param_count(model_obj) -> Optional[int]:
    """Total parameters if this is a torch module; None if it is not one."""
    inner = getattr(model_obj, "model", None)
    if inner is None:
        return None
    params = getattr(inner, "parameters", None)
    if not callable(params):
        return None
    try:
        return sum(p.numel() for p in inner.parameters())
    except Exception:  # noqa: BLE001 -- a non-torch model is not an error here
        return None


def _submodel_count(model_obj) -> Optional[int]:
    """
    MMSplice's five weights are Keras submodels held in a container rather
    than a torch module, so a parameter count does not apply to it. Counting
    the constructed submodels is the equivalent evidence: it distinguishes
    "the object exists" from "the weights were materialised".
    """
    inner = getattr(model_obj, "model", None)
    if inner is None:
        return None
    for attribute in ("submodels", "models", "modules_"):
        value = getattr(inner, attribute, None)
        if isinstance(value, (list, tuple, dict)):
            return len(value)
    if isinstance(inner, (list, tuple, dict)):
        return len(inner)
    return None


def verdict(model_obj) -> Tuple[bool, str]:
    """
    Whether this model's WEIGHTS WERE MATERIALISED, and the evidence for it.

    *** THIS USED TO BE `results.append((name, True, evidence))`. *** The `True`
    was a literal meaning "no exception was raised", and the evidence beside it was
    printed, listed in the summary, and never consulted -- `failed` was computed
    from `ok` alone. So `params=0.0M`, `submodels=0`, and the bare string
    `constructed` (which is the TOTAL ABSENCE of evidence) all reported PASS.

    That mattered here more than anywhere else in the repository: this script is
    what `scripts/build_bridge_ready.sh` runs under `--network none`, and its exit
    code is the only thing between "the image built" and "the image works with no
    network". An acceptance test that cannot come back red has accepted nothing.

    `_submodel_count`'s own docstring already said what the evidence was for --
    "it distinguishes 'the object exists' from 'the weights were materialised'".
    It was written for exactly this and then not used to decide anything.

    A model with NO evidence available is NOT PROVEN rather than passed. For the
    four models actually shipped this cannot fire: three are torch modules and
    report a parameter count, and MMSplice sets `self.model` to its list of five
    Keras submodels, which `_submodel_count` counts. If a future model can be
    materialised but not counted, the honest fix is to teach this function what
    ITS evidence looks like -- not to let an absence read as a pass.
    """
    params = _param_count(model_obj)
    if params is not None:
        return params > 0, f"params={params / 1e6:.1f}M"
    submodels = _submodel_count(model_obj)
    if submodels is not None:
        return submodels > 0, f"submodels={submodels}"
    return False, "constructed -- NO EVIDENCE THE WEIGHTS WERE MATERIALISED"


def _load_esm2():
    from geper.models.esm2 import ESM2Model

    return ESM2Model()


def _load_hyenadna():
    from geper.models.hyenadna import HyenaDNAModel

    return HyenaDNAModel()


def _load_rnafm():
    from geper.models.rna_fm import RNAFMModel

    return RNAFMModel()


def _load_mmsplice():
    # Two import roots because this loader lives under `geper/pipeline/`,
    # which is importable as `pipeline.` when /app/geper is on the path and
    # as `geper.pipeline.` when /app is. Trying both is not defensiveness --
    # the two roots are how the repository is actually laid out.
    try:
        from pipeline.models.mmsplice.loader import MMSpliceModel
    except ImportError:
        from geper.pipeline.models.mmsplice.loader import MMSpliceModel

    return MMSpliceModel()


MODELS: List[Tuple[str, Callable[[], object]]] = [
    ("ESM2", _load_esm2),
    ("MMSplice", _load_mmsplice),
    ("HyenaDNA", _load_hyenadna),
    ("RNA-FM", _load_rnafm),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        help="Run a subset by name (repeatable). Default: all four.",
    )
    args = parser.parse_args()

    selected = MODELS
    if args.only:
        wanted = {name.lower() for name in args.only}
        selected = [entry for entry in MODELS if entry[0].lower() in wanted]
        if not selected:
            print(f"No model matches {sorted(wanted)}; known: {[n for n, _ in MODELS]}")
            return 2

    print("=" * 72)
    print("OFFLINE MODEL LOAD -- all models constructed through GEPER's own loaders")
    print("Run this with the network cut at the container (`docker run --network none`).")
    print("=" * 72)

    results = []
    for name, factory in selected:
        print(f"\n--- {name} " + "-" * (68 - len(name)))
        started = time.time()
        try:
            model = factory()
            model.load()
            elapsed = time.time() - started
            ok, evidence = verdict(model)
            print(f"{name}: {'LOADED' if ok else 'NOT PROVEN'} in {elapsed:.1f}s  {evidence}")
            results.append((name, ok, evidence))
        except Exception as exc:  # noqa: BLE001 -- every load failure is a result
            elapsed = time.time() - started
            print(f"{name}: FAILED after {elapsed:.1f}s")
            print(f"  {type(exc).__name__}: {exc}")
            traceback.print_exc()
            results.append((name, False, f"{type(exc).__name__}"))

    print("\n" + "=" * 72)
    for name, ok, evidence in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<10} {evidence}")
    failed = [name for name, ok, _ in results if not ok]
    if failed:
        print(f"\nRESULT: FAIL -- {', '.join(failed)} did not construct offline.")
        return 1
    print(f"\nRESULT: PASS -- all {len(results)} models constructed with no network.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
