"""
NCBI MANE (Matched Annotation from NCBI and EBI) Select gene->transcript
dataset integration.

Public entry point for the rest of GEPER is `mane_select_transcript_id`
(`pipeline.mane.provider.mane_select_transcript_id`) -- see that module's
docstring. Consumed by `pipeline/clingen/utils.py`'s overlapping-gene
MANE Select tie-break.
"""

from pipeline.mane.provider import LocalDatasetMANEProvider, mane_select_transcript_id

__all__ = [
    "LocalDatasetMANEProvider",
    "mane_select_transcript_id",
]
