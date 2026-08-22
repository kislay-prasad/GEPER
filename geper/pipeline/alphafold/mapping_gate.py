"""
The AlphaFold residue-mapping gate: the single place that decides
whether a variant's protein position may be reported with the AlphaFold
model's per-residue pLDDT confidence.

WHY A GATE AND NOT AN INLINE CHECK. An AlphaFold entry carries two
independent coordinate claims -- the UniProt span it says it covers
(`uniprotStart`/`uniprotEnd`, from the summary API) and the residue
numbers actually present in its structure file (parsed by
`utils.parse_pdb_plddt`). Nothing guarantees they agree: AlphaFold DB
returns long proteins as multiple fragment entries, and
`provider.py::LiveAPIAlphaFoldProvider.query` selects `entries[0]`. A
residue lookup that consults only the second claim will happily return
a confident pLDDT for a residue the entry never covered, and that value
is indistinguishable downstream from a correct one. Concentrating the
decision here is what stops the next consumer from re-deriving it, and
getting it wrong differently.

THE VERDICT. `(True, None)` means all conditions hold and the pLDDT at
that position genuinely describes that residue. `(False, reason)` means
no residue-level confidence is available, and `reason` says why in
terms a report can show a reader. There is no third state and no
"probably" -- a caller that wants an approximate residue is asking for
the failure mode this gate exists to prevent.

The three conditions are named A (range), B (presence) and SPAN
CONTAINMENT. The third is deliberately not given a letter -- see the
naming note at the foot of this docstring.

WHAT THIS GATE DOES *NOT* CHECK -- named deliberately, because A, B and
span containment being sufficient is a judgement, not a proof:

  * Post-translational numbering offsets. A mature protein whose
    signal peptide or initiator methionine has been cleaved is
    numbered differently from its UniProt entry. Every condition here
    would pass while the residue is off by the length of the cleaved
    segment. Nothing in the AlphaFold summary reports this; detecting
    it needs UniProt feature annotations this module is not given.
  * Model-version drift. The declared span comes from the summary
    response and the residue numbers from a separately downloaded
    structure file. If `latestVersion` moved between the two fetches,
    or a cached structure predates the summary, the two claims are
    from different models and span containment can hold while the
    residue numbering has shifted. Comparing `model_version` across both
    fetches would be needed, and the structure file is not parsed for
    its version today.
  * Whether the protein position itself is right. Resolving a variant
    to a residue number against the correct isoform is the ISOFORM
    CHECK -- a separate stage of the wider mapping problem, living in
    `pipeline.pvs1.utils.canonical_protein_position` (pinned by
    `tests/test_alphafold_mapping_gate_isoform.py`). This gate takes
    the position as given and only asks whether the AlphaFold model
    can speak about it.

NAMING NOTE, resolved deliberately rather than left to the reader: the
dispatch that requested this gate called the containment guard
"condition C", while `test_alphafold_mapping_gate_isoform.py` had
already been using "condition C" for the ISOFORM check -- a different
check, in a different module, at a different stage. One letter meaning
two things is a collision that costs nothing to avoid and misleads
every future reader, so it is retired here: this check is "span
containment" throughout, and the isoform check is "the isoform check".
Conditions A and B keep their letters because nothing else claims
them.
"""

from typing import Dict, Optional, Tuple


def check_alphafold_mapping_gate(
    protein_position: Optional[int],
    uniprot_start: Optional[int],
    uniprot_end: Optional[int],
    residue_plddt: Dict[int, float],
) -> Tuple[bool, Optional[str]]:
    """
    Decide whether `residue_plddt[protein_position]` may be reported as
    this variant's residue-level structural confidence.

    Returns `(True, None)` only when all of the following hold:

      A. `uniprot_start <= protein_position <= uniprot_end` (inclusive).
      B. `protein_position in residue_plddt` -- membership, never the
         truthiness of the value: a pLDDT of 0.0 is a real, modelled,
         very-low-confidence residue, not an absent one.
      SPAN CONTAINMENT. Every residue number in `residue_plddt` lies
         within `[uniprot_start, uniprot_end]`. Subset, not equality --
         AlphaFold models are legitimately gapped, and equality would
         reject normal structures while catching nothing extra. Named,
         not lettered: see the module docstring's naming note.

    Never raises: a structural annotation is a rendering input, and a
    malformed one must cost the report a figure, not the interpretation.

    `uniprot_start`/`uniprot_end` are Optional despite the wider
    contract naming them `int`: the local-dataset path builds its
    summary as `record.get("summary") or {}`, so both are genuinely
    None for a record with no summary block. Answering that here is the
    point of having one gate; pushing it back to call sites would
    scatter the decision.
    """
    if protein_position is None:
        return False, "no protein position was resolved for this variant"

    if uniprot_start is None or uniprot_end is None:
        return False, "the AlphaFold entry declares no UniProt residue span (uniprotStart/uniprotEnd absent)"

    if uniprot_end < uniprot_start:
        return False, (
            f"the AlphaFold entry declares an inverted UniProt span "
            f"({uniprot_start}-{uniprot_end}); the summary is not usable"
        )

    # Condition A -- range.
    if not (uniprot_start <= protein_position <= uniprot_end):
        return False, (
            f"protein position {protein_position} lies outside the AlphaFold "
            f"entry's declared UniProt span {uniprot_start}-{uniprot_end}"
        )

    # Checked before span containment so that "we never got a structure" is
    # never reported as a numbering problem; an empty map is vacuously
    # contained and would otherwise fall through to a misleading reason.
    if not residue_plddt:
        return False, (
            "no per-residue pLDDT was parsed from the AlphaFold structure "
            "(structure file not fetched, or contained no CA records)"
        )

    # Span containment -- checked before condition B because when the
    # model's numbering is fragment-local, "position not modelled" is a
    # true but misleading statement: the residue is missing because the
    # whole numbering belongs to a different frame of reference.
    lowest = min(residue_plddt)
    highest = max(residue_plddt)
    if lowest < uniprot_start or highest > uniprot_end:
        return False, (
            f"the AlphaFold model numbers residues {lowest}-{highest}, which is not "
            f"contained in its declared UniProt span {uniprot_start}-{uniprot_end} "
            f"(fragment-local numbering, truncation, or a mismatched accession)"
        )

    # Condition B -- presence. `in`, never `.get()`: pLDDT 0.0 is falsy.
    if protein_position not in residue_plddt:
        return False, (f"protein position {protein_position} is not modelled in the AlphaFold structure")

    return True, None
