"""
Orphanet gene-disorder association evidence-source integration.

Public entry point for the rest of GEPER is `OrphanetLookup`
(`pipeline.orphanet.lookup.OrphanetLookup`) -- see that module's docstring.

License: Orphadata Science's gene-disorder dataset (`en_product6.xml`)
is CC BY 4.0 (commercial use explicitly permitted with attribution) --
see `config.py::OrphanetConfig`'s docstring. This is distinct from
"Orphadata Products" (the REST API and other services), which requires
a paid Data Transfer Agreement/Service Contract and is NOT used by
this integration.
"""

from pipeline.orphanet.lookup import OrphanetLookup
from pipeline.orphanet.models import OrphanetDisorderAssociation, OrphanetGeneEvidence

__all__ = ["OrphanetLookup", "OrphanetDisorderAssociation", "OrphanetGeneEvidence"]
