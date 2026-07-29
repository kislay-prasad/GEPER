"""
PS1 (established pathogenic amino acid change) and PM5 (novel amino
acid change at an established pathogenic codon) evidence rules.

Two public entry points for the rest of GEPER:

  - `ClinVarCodonLookup` (`pipeline.ps1_pm5.lookup`): the orchestrator
    stage that fetches every ClinVar record with a missense protein
    change at the query variant's codon.
  - `PS1PM5Evaluator` (`pipeline.ps1_pm5.decision`): the comparison
    logic itself, consumed by `pipeline/acmg_rules.py::ACMGRuleEngine
    ._ps1` / `._pm5`.

Implemented from ACMG/AMP 2015 (Richards et al., Genet Med 17:405):
PS1 "same amino acid change as a previously established pathogenic
variant regardless of nucleotide change"; PM5 "novel missense change
at an amino acid residue where a different missense change determined
to be pathogenic has been seen before". See `decision.py`'s docstring
for the shared evidence-gathering step and the two caveats this
package applies (a ClinVar confidence/star-rating filter, and a
splice-proximity check on the query variant's own position).
"""

from pipeline.ps1_pm5.decision import PS1PM5Evaluator, PS1PM5Thresholds
from pipeline.ps1_pm5.lookup import ClinVarCodonLookup
from pipeline.ps1_pm5.models import ClinVarCodonMatch, PS1PM5Evaluation

__all__ = [
    "ClinVarCodonLookup",
    "PS1PM5Evaluator",
    "PS1PM5Thresholds",
    "PS1PM5Evaluation",
    "ClinVarCodonMatch",
]
