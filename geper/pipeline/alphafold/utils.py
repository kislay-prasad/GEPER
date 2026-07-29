"""
Shared, dependency-free helpers for the AlphaFold DB integration:
UniProt-accession normalization/cache keys, and parsing a downloaded
AlphaFold PDB structure file's B-factor column (which AlphaFold DB
uses to store per-residue pLDDT, not crystallographic B-factor) into a
per-residue confidence map. No network, no torch -- pure functions,
trivially unit-testable against a small synthetic PDB fixture.
"""

from typing import Dict, Optional


def normalize_accession(accession: Optional[str]) -> Optional[str]:
    if not accession:
        return None
    return accession.strip().upper() or None


def accession_cache_key(accession: str) -> str:
    return f"accession:{normalize_accession(accession)}"


def parse_pdb_plddt(pdb_text: str) -> Dict[int, float]:
    """
    Extract one pLDDT value per residue from a PDB-format text file, by
    reading the temperature-factor column of each residue's alpha-carbon
    (`CA`) ATOM record -- exactly where AlphaFold DB stores its
    per-residue pLDDT confidence score (0-100) instead of a real
    B-factor (documented at https://alphafold.ebi.ac.uk/faq).

    Uses fixed-width PDB column parsing per the standard ATOM record
    layout (columns 23-26 = residue sequence number, 61-66 =
    temperature factor), falling back to a robust whitespace split for
    any line that doesn't conform to strict fixed-width columns (some
    third-party PDB writers pad columns loosely).
    """
    residue_plddt: Dict[int, float] = {}
    for line in pdb_text.splitlines():
        if not (line.startswith("ATOM") or line.startswith("HETATM")):
            continue
        atom_name = line[12:16].strip() if len(line) >= 16 else ""
        if atom_name != "CA":
            continue
        try:
            res_seq = int(line[22:26].strip())
            b_factor = float(line[60:66].strip())
        except (ValueError, IndexError):
            # Fixed-width slice failed (short/malformed line) -- fall
            # back to a plain whitespace split, using the standard
            # ATOM field ordering as a best effort.
            fields = line.split()
            try:
                res_seq = int(fields[5])
                b_factor = float(fields[10])
            except (ValueError, IndexError):
                continue
        residue_plddt[res_seq] = b_factor
    return residue_plddt
