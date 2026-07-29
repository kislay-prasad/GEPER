"""
HPO (Human Phenotype Ontology) evidence-source integration.

Public entry point for the rest of GEPER is `HPOLookup`
(`pipeline.hpo.lookup.HPOLookup`) -- see that module's docstring.
"""

from pipeline.hpo.lookup import HPOLookup
from pipeline.hpo.models import HPODiseaseAssociation, HPOGeneEvidence, HPOPhenotypeAssociation

__all__ = [
    "HPOLookup",
    "HPOGeneEvidence",
    "HPOPhenotypeAssociation",
    "HPODiseaseAssociation",
]
