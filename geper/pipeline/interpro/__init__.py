"""
InterPro/Pfam conserved-domain evidence-source integration.

Public entry point for the rest of GEPER is `InterProLookup`
(`pipeline.interpro.lookup.InterProLookup`) -- see that module's docstring.

License: InterPro and Pfam data are CC0 1.0 Universal (public domain
dedication, no restriction on commercial use) -- see
`config.py::InterProConfig`'s docstring.
"""

from pipeline.interpro.lookup import InterProLookup
from pipeline.interpro.models import InterProAnnotation, InterProDomainMatch

__all__ = ["InterProLookup", "InterProAnnotation", "InterProDomainMatch"]
