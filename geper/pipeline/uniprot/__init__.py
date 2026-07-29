"""
UniProt reviewed-protein-annotation evidence-source integration.

Public entry point for the rest of GEPER is `UniProtLookup`
(`pipeline.uniprot.lookup.UniProtLookup`) -- see that module's docstring.

License: UniProt data is CC-BY-4.0 (commercial use permitted with
attribution) -- see `config.py::UniProtConfig`'s docstring.
"""

from pipeline.uniprot.lookup import UniProtLookup
from pipeline.uniprot.models import UniProtAnnotation, UniProtFeature

__all__ = ["UniProtLookup", "UniProtAnnotation", "UniProtFeature"]
