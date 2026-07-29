"""
AlphaFold Protein Structure Database structural evidence-source integration.

Public entry point for the rest of GEPER is `AlphaFoldLookup`
(`pipeline.alphafold.lookup.AlphaFoldLookup`) -- see that module's docstring.

License: AlphaFold DB structure predictions and confidence metrics
are CC-BY-4.0, explicitly for academic AND commercial use -- see
`config.py::AlphaFoldConfig`'s docstring. (Note: distinct from the
AlphaFold *model parameters*, which are CC-BY-NC-4.0 -- GEPER only
ever consumes the already-computed, separately-licensed database
entries, never the model itself.)
"""

from pipeline.alphafold.lookup import AlphaFoldLookup
from pipeline.alphafold.models import AlphaFoldAnnotation

__all__ = ["AlphaFoldLookup", "AlphaFoldAnnotation"]
