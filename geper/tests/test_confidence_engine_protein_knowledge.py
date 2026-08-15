"""
Regression test for `pipeline/confidence_engine.py::
ConfidenceEngine._protein_quality` (round 23).

Found via a real report (GEPER-RUN-20260815T11442, MT-TL1): when a gene
has no protein-coding transcript at all, `pipeline/orchestrator.py`
computes a single `non_protein_coding_gene_reason` string and reuses it
verbatim for `uniprot_result["reason"]`, `interpro_result["reason"]`,
and `alphafold_result["reason"]` (see orchestrator.py's own comment on
that assignment). `_protein_quality` appends BOTH the UniProt-branch
and InterPro-branch `reason` to its `notes` list and joins them with a
space -- so when the two reasons are the identical shared string, the
same sentence appeared twice, back to back, in the rendered "Protein
Knowledge" evidence-completeness cell. `_structural_quality` only has
one source (AlphaFold) contributing a `reason`, so the same underlying
shared-string setup never duplicated there -- matching what the report
actually showed (the Structural Biology cell printed the sentence once).
"""

import unittest

from pipeline.confidence_engine import ConfidenceEngine


class TestProteinQualityReasonDedup(unittest.TestCase):
    def test_shared_skip_reason_is_stated_once(self):
        reason = (
            "MT-TL1 is annotated by Ensembl as biotype 'Mt_tRNA', not protein_coding -- "
            "this gene has no protein product, so there is no UniProt entry, InterPro/Pfam "
            "domain annotation, or AlphaFold DB structure to resolve. This is gene biology, "
            "not a failed lookup."
        )
        uniprot_result = {"found": False, "skipped": True, "reason": reason}
        interpro_result = {"found": False, "skipped": True, "reason": reason}

        score = ConfidenceEngine._protein_quality(uniprot_result, interpro_result, weight=1.0)

        self.assertEqual(score.rationale.count(reason), 1)

    def test_distinct_reasons_are_both_kept(self):
        uniprot_result = {"found": False, "skipped": False, "error": "UniProt REST API request failed"}
        interpro_result = {"found": False, "skipped": False, "reason": "no InterPro annotation for this accession"}

        score = ConfidenceEngine._protein_quality(uniprot_result, interpro_result, weight=1.0)

        self.assertIn("No UniProt entry resolved for this gene/protein.", score.rationale)
        self.assertIn("no InterPro annotation for this accession", score.rationale)


if __name__ == "__main__":
    unittest.main()
