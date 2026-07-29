"""
tests/test_omim.py
──────────────────
Unit tests for pipeline.omim.lookup.OmimLookup.
"""
from pathlib import Path

import pytest

from pipeline.omim.lookup import OmimLookup, OmimGeneEntry


# ── Helpers ───────────────────────────────────────────────────────────────────

GENEMAP_HEADER = (
    "# Chromosome\tGenomic Position Start\tGenomic Position End\t"
    "Cytoband\tComputed Cytoband\tMIM Number\tGene Symbols\t"
    "Approved Gene Symbol\tEntrez Gene ID\tEnsembl Gene ID\t"
    "Comments\tPhenotypes\tMouse Gene Symbol/ID\n"
)

GENEMAP_BRCA1 = (
    "17\t43044295\t43125370\t17q21.31\t17q21.31\t113705\tBRCA1\t"
    "BRCA1\t672\tENSG00000012048\t\t"
    "Breast-ovarian cancer, familial 1, 604370 (3), Autosomal dominant\t"
    "Brca1\n"
)

GENEMAP_TP53 = (
    "17\t7661779\t7687550\t17p13.1\t17p13.1\t191170\tTP53\t"
    "TP53\t7157\tENSG00000141510\t\t"
    "Li-Fraumeni syndrome, 151623 (3), Autosomal dominant\t"
    "Trp53\n"
)

GENEMAP_RECESSIVE = (
    "7\t117480025\t117668665\t7q31.2\t7q31.2\t602421\tCFTR\t"
    "CFTR\t1080\tENSG00000001626\t\t"
    "Cystic fibrosis, 219700 (3), Autosomal recessive\t"
    "Cftr\n"
)

GENEMAP_HAPLOINSUFFICIENCY = (
    "1\t156084621\t156140088\t1q23.3\t1q23.3\t107400\tHAPLO1\t"
    "HAPLO1\t9999\tENSG00000999999\t\t"
    "Haploinsufficiency syndrome, 999999 (3)\t"
    "Haplo1\n"
)


def _write_genemap(tmp_path: Path, rows: list[str]) -> Path:
    gm_path = tmp_path / "genemap2.txt"
    gm_path.write_text(GENEMAP_HEADER + "".join(rows), encoding="utf-8")
    return gm_path


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_genemap_parsing(tmp_path):
    """Parsing a minimal genemap2.txt returns the correct MIM number for BRCA1."""
    gm = _write_genemap(tmp_path, [GENEMAP_BRCA1])
    omim = OmimLookup(cfg={"omim": {"genemap_path": str(gm)}})
    entry = omim.lookup_gene("BRCA1")
    assert entry is not None
    assert entry.mim_number == "113705"
    assert entry.symbol == "BRCA1"


def test_genemap_case_insensitive(tmp_path):
    """Gene symbol lookup is case-insensitive."""
    gm = _write_genemap(tmp_path, [GENEMAP_BRCA1])
    omim = OmimLookup(cfg={"omim": {"genemap_path": str(gm)}})
    assert omim.lookup_gene("brca1") is not None
    assert omim.lookup_gene("Brca1") is not None


def test_lof_intolerant_dominant(tmp_path):
    """Autosomal dominant in Phenotypes → is_lof_intolerant returns True."""
    gm = _write_genemap(tmp_path, [GENEMAP_TP53])
    omim = OmimLookup(cfg={"omim": {"genemap_path": str(gm)}})
    assert omim.is_lof_intolerant("TP53") is True


def test_lof_intolerant_haploinsufficiency(tmp_path):
    """Haploinsufficiency in Phenotypes → is_lof_intolerant returns True."""
    gm = _write_genemap(tmp_path, [GENEMAP_HAPLOINSUFFICIENCY])
    omim = OmimLookup(cfg={"omim": {"genemap_path": str(gm)}})
    assert omim.is_lof_intolerant("HAPLO1") is True


def test_lof_intolerant_negative(tmp_path):
    """Autosomal recessive gene (no dominant) → is_lof_intolerant returns False."""
    gm = _write_genemap(tmp_path, [GENEMAP_RECESSIVE])
    omim = OmimLookup(cfg={"omim": {"genemap_path": str(gm)}})
    assert omim.is_lof_intolerant("CFTR") is False


def test_lof_intolerant_unknown_gene(tmp_path):
    """Unknown gene → is_lof_intolerant returns False (not an error)."""
    gm = _write_genemap(tmp_path, [GENEMAP_BRCA1])
    omim = OmimLookup(cfg={"omim": {"genemap_path": str(gm)}})
    assert omim.is_lof_intolerant("FAKEGENE999") is False


def test_missense_mechanism_hardcoded():
    """is_missense_mechanism returns True for hardcoded genes without any file."""
    omim = OmimLookup(cfg={})
    assert omim.is_missense_mechanism("BRCA1") is True
    assert omim.is_missense_mechanism("MYBPC3") is True
    assert omim.is_missense_mechanism("LDLR") is True


def test_missense_mechanism_not_hardcoded_unknown():
    """is_missense_mechanism returns False for a totally unknown gene."""
    omim = OmimLookup(cfg={})
    assert omim.is_missense_mechanism("FAKEGENE999") is False


def test_lookup_unknown_gene_returns_none(tmp_path):
    """lookup_gene for a gene not in the file returns None."""
    gm = _write_genemap(tmp_path, [GENEMAP_BRCA1])
    omim = OmimLookup(cfg={"omim": {"genemap_path": str(gm)}})
    assert omim.lookup_gene("FAKEGENE999") is None


def test_multiple_genes_all_found(tmp_path):
    """Multiple genes in the genemap are all individually retrievable."""
    gm = _write_genemap(tmp_path, [GENEMAP_BRCA1, GENEMAP_TP53, GENEMAP_RECESSIVE])
    omim = OmimLookup(cfg={"omim": {"genemap_path": str(gm)}})
    assert omim.lookup_gene("BRCA1") is not None
    assert omim.lookup_gene("TP53") is not None
    assert omim.lookup_gene("CFTR") is not None


def test_api_fallback_no_key_returns_none():
    """Without an API key, API backend returns None and logs a warning."""
    omim = OmimLookup(cfg={"omim": {}})  # no genemap_path, no api_key
    result = omim.lookup_gene("BRCA1")
    # No key → should return None gracefully
    assert result is None
