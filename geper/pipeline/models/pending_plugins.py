"""
Registrations for the AI model plugin framework (`pipeline/models/`).

Naming note: this module is still called "pending_plugins" because it
still hosts OpenSpliceAI's placeholder. Enformer, Borzoi, and
SpliceFormer are no longer placeholders here -- their real
integrations live in `pipeline/models/enformer_plugin.py`,
`pipeline/models/borzoi_plugin.py`, and
`pipeline/models/spliceformer_plugin.py` respectively (each
license-cleared per the project's dedicated audit); this module just
imports and registers them alongside OpenSpliceAI's still-disabled
placeholder so `build_default_registry()` continues to return the
full "new models" set from one place.

SpliceFormer, SpliceBERT, and SPiP are registered here but are
deliberately NOT added to `pipeline/models/ensemble.py`'s
Enformer+Borzoi consensus, and their results are never passed to ACMG
evaluation (`pipeline/interpretation.py`/`pipeline/acmg_rules.py`) or
to report generation (`report/*`) -- see
`pipeline/models/spliceformer_plugin.py`/
`pipeline/models/splicebert_plugin.py`/`pipeline/models/spip_plugin.py`'s
own module docstrings. All three are reachable through `ModelManager`
(e.g. for direct/programmatic use, tests, or `benchmark_spliceformer.py`)
exactly like every other plugin registered here, without altering any
existing evidence-aggregation or report-rendering behavior. SPiP is
additionally architecturally distinct from the other five plugins
here (an R script invoked as a subprocess, not an in-process PyTorch
model) -- see its own module docstring for why.

OpenSpliceAI is NOT integrated. Its code and pretrained weights are
both GPL-3.0 (confirmed via the repo's own LICENSE file/license
badge) -- a strong copyleft license. Linking GPL-3.0 code directly
into GEPER's proprietary, commercially-distributed codebase would
require the combined work to also be distributed under GPL-3.0, which
is not something this change can decide unilaterally. The
`OpenSpliceAIPlugin` placeholder below stays hard-disabled
(`is_available()` always False) regardless of
`CONFIG.splicing.ENABLE_OPENSPLICEAI` until GEPER's leadership makes
an explicit, informed decision about that tradeoff -- e.g. isolating
OpenSpliceAI as a separate subprocess/CLI invocation rather than an
in-process library (a common way projects incorporate GPL tools
without GPL-licensing the whole product), which is a legal/architectural
judgment call outside what this change makes for you.
"""

from typing import Any, Dict

from config import CONFIG
from pipeline.models.base import ModelMetadata, PluginModel
from pipeline.models.borzoi_plugin import BorzoiPlugin
from pipeline.models.enformer_plugin import EnformerPlugin
from pipeline.models.spip_plugin import SpipPlugin
from pipeline.models.splicebert_plugin import SpliceBERTPlugin
from pipeline.models.spliceformer_plugin import SpliceFormerPlugin

__all__ = [
    "OpenSpliceAIPlugin",
    "EnformerPlugin",
    "BorzoiPlugin",
    "SpliceFormerPlugin",
    "SpliceBERTPlugin",
    "SpipPlugin",
    "build_default_registry",
]


class OpenSpliceAIPlugin(PluginModel):
    """
    Placeholder for JHU's OpenSpliceAI (Kuanhao-Chao/OpenSpliceAI).
    GPL-3.0 licensed (both code and bundled pretrained models,
    confirmed via the repo's own LICENSE file/GitHub license badge) --
    NOT integrated. See this module's docstring for why, and what an
    isolated-subprocess integration path would need to look like if
    GEPER's leadership decides to pursue it later.
    """

    @classmethod
    def metadata(cls) -> ModelMetadata:
        return ModelMetadata(
            name="openspliceai",
            version="not integrated",
            source="https://github.com/Kuanhao-Chao/OpenSpliceAI",
            license_name="GPL-3.0 (code and bundled pretrained models)",
            license_url="https://github.com/Kuanhao-Chao/OpenSpliceAI/blob/main/LICENSE",
            commercial_use_allowed=False,
            license_notes=(
                "GPL-3.0 confirmed via the repository's own LICENSE file and "
                "GitHub license badge. This is a copyleft license: linking it "
                "directly into GEPER's proprietary codebase would obligate "
                "the combined work to also be distributed under GPL-3.0. Not "
                "integrated. A subprocess/CLI-isolation approach (avoiding "
                "in-process linking) may be viable but is a legal/"
                "architectural decision for GEPER's leadership, not something "
                "this change makes unilaterally."
            ),
        )

    @classmethod
    def is_available(cls) -> bool:
        # Hard-disabled regardless of CONFIG.splicing.ENABLE_OPENSPLICEAI:
        # this isn't a "not verified yet" gate (unlike Enformer/Borzoi
        # before their audit) -- the license IS verified, and it's
        # GPL-3.0, which is why this plugin is not integrated.
        return CONFIG.splicing.ENABLE_OPENSPLICEAI and False

    @classmethod
    def unavailability_reason(cls) -> str:
        return (
            "not integrated -- GPL-3.0 licensed (code and weights), "
            "incompatible with linking into GEPER's proprietary codebase; "
            "see OpenSpliceAIPlugin's docstring"
        )

    def _load_impl(self) -> None:
        raise NotImplementedError(
            "OpenSpliceAIPlugin is intentionally not integrated (GPL-3.0); "
            "see module docstring."
        )

    def _infer_impl(self, *args, **kwargs) -> Dict[str, Any]:
        raise NotImplementedError(
            "OpenSpliceAIPlugin is intentionally not integrated (GPL-3.0); "
            "see module docstring."
        )


def build_default_registry():
    """Convenience factory: a `ModelRegistry` with all six "new
    models" registered -- the real Enformer/Borzoi/SpliceFormer/
    SpliceBERT/SPiP integrations plus OpenSpliceAI's still-disabled
    placeholder -- used by `ModelManager` callers / tests that want
    the full set without hand-rolling the registration calls each
    time."""
    from pipeline.models.registry import ModelRegistry

    registry = ModelRegistry()
    registry.register("openspliceai", OpenSpliceAIPlugin)
    registry.register("enformer", EnformerPlugin)
    registry.register("borzoi", BorzoiPlugin)
    registry.register("spliceformer", SpliceFormerPlugin)
    registry.register("splicebert", SpliceBERTPlugin)
    registry.register("spip", SpipPlugin)
    return registry
