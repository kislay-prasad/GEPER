"""
PVS1 (null variant) evidence rule.

Two public entry points for the rest of GEPER:

  - `TranscriptLookup` (`pipeline.pvs1.lookup`): the orchestrator stage
    that fetches the transcript structure PVS1's location caveats need.
  - `PVS1DecisionTree` / `build_pvs1_input` : the ClinGen SVI decision
    tree itself, consumed by `pipeline/acmg_rules.py::ACMGRuleEngine
    ._pvs1`.

The tree is implemented from ACMG/AMP 2015 (Richards et al., Genet Med
17:405) as refined by the ClinGen Sequence Variant Interpretation
Working Group (Abou Tayoun et al., Hum Mutat 2018;39:1517, PMID
30192042). See `decision_tree.py`'s docstring for the exact mapping,
including which nodes GEPER can answer today and which are reported as
gaps.
"""

from pipeline.pvs1.decision_tree import PVS1DecisionTree, PVS1Input
from pipeline.pvs1.lookup import TranscriptLookup
from pipeline.pvs1.models import (
    ExonSpan,
    PVS1Evaluation,
    STRENGTH_MODERATE,
    STRENGTH_NOT_APPLICABLE,
    STRENGTH_STRONG,
    STRENGTH_SUPPORTING,
    STRENGTH_VERY_STRONG,
    TranscriptContext,
)
from pipeline.pvs1.utils import build_pvs1_input, classify_null_variant

__all__ = [
    "TranscriptLookup",
    "PVS1DecisionTree",
    "PVS1Input",
    "PVS1Evaluation",
    "TranscriptContext",
    "ExonSpan",
    "build_pvs1_input",
    "classify_null_variant",
    "STRENGTH_VERY_STRONG",
    "STRENGTH_STRONG",
    "STRENGTH_MODERATE",
    "STRENGTH_SUPPORTING",
    "STRENGTH_NOT_APPLICABLE",
]
