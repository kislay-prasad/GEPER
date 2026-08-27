"""
Registrations for the AI model plugin framework (`pipeline/models/`).

Naming note: this module predates Enformer/Borzoi/SpliceFormer having
their own real integration modules and originally also hosted an
OpenSpliceAI placeholder (removed -- GPL-3.0, never integrated; see
LICENSE_AUDIT.md for the decision record). Enformer, Borzoi, and
SpliceFormer are no longer placeholders here -- their real
integrations live in `pipeline/models/enformer_plugin.py`,
`pipeline/models/borzoi_plugin.py`, and
`pipeline/models/spliceformer_plugin.py` respectively (each
license-cleared per the project's dedicated audit); this module just
imports and registers them so `build_default_registry()` returns the
full "new models" set from one place.

SpliceFormer, SpliceBERT, and SPiP are registered here but are
deliberately NOT added to `pipeline/models/ensemble.py`'s
Enformer+Borzoi consensus (that ensemble feeds PP3/BP4 specifically --
see `pipeline/models/ensemble.py`'s own docstring). That exclusion is
the only thing all three have in common.

SpliceFormer and SpliceBERT ARE otherwise wired into a live per-variant
run: both are invoked from `pipeline/orchestrator.py::
_run_standalone_splice_plugin_stage` (called at
`pipeline/orchestrator.py:1465-1466`), both appear in the "AI Models"
status table (`pipeline/models/status.py:306-310`), and both feed BP7
directly (`pipeline/acmg_rules.py::ACMGRuleEngine._bp7`) -- whose
rationale text, caveated for these two, reaches `report/*` through the
same `supporting_evidence`/`conflicting_evidence` mechanism every
other criterion's evidence uses. SPiP alone is genuinely unwired: not
called from any per-variant orchestrator stage, absent from the status
table, and its result never reaches
`InterpretationEngine`/`acmg_rules.py`/`report/*` in any form -- see
`pipeline/models/spip_plugin.py`'s own module docstring.

All three are reachable through `ModelManager` (e.g. for
direct/programmatic use, tests, or `benchmark_spliceformer.py`)
exactly like every other plugin registered here, without altering any
existing evidence-aggregation or report-rendering behavior. SPiP is
additionally architecturally distinct from the other five plugins
here (an R script invoked as a subprocess, not an in-process PyTorch
model) -- see its own module docstring for why.
"""

from pipeline.models.borzoi_plugin import BorzoiPlugin
from pipeline.models.enformer_plugin import EnformerPlugin
from pipeline.models.spip_plugin import SpipPlugin
from pipeline.models.splicebert_plugin import SpliceBERTPlugin
from pipeline.models.spliceformer_plugin import SpliceFormerPlugin

__all__ = [
    "EnformerPlugin",
    "BorzoiPlugin",
    "SpliceFormerPlugin",
    "SpliceBERTPlugin",
    "SpipPlugin",
    "build_default_registry",
]


def build_default_registry():
    """Convenience factory: a `ModelRegistry` with all five "new
    models" registered -- the real Enformer/Borzoi/SpliceFormer/
    SpliceBERT/SPiP integrations -- used by `ModelManager` callers /
    tests that want the full set without hand-rolling the registration
    calls each time."""
    from pipeline.models.registry import ModelRegistry

    registry = ModelRegistry()
    registry.register("enformer", EnformerPlugin)
    registry.register("borzoi", BorzoiPlugin)
    registry.register("spliceformer", SpliceFormerPlugin)
    registry.register("splicebert", SpliceBERTPlugin)
    registry.register("spip", SpipPlugin)
    return registry
