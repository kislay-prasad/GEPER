"""F3: kim_pipeline derives a variant's protein consequence twice for the
same record -- once via its own GFF3 + codon_provider path (`var.consequence`,
set in `AnnotationStage.run()` from `_map_consequence`), and once from VEP's
own CSQ output (`var.vep_consequence`, `_extract_csq_consequence`). Each
picks its transcript independently: kim's own via `GffIndex.lookup()`,
VEP's via its own most-severe-across-transcripts ranking. Nothing
reconciles the two.

This is not hypothetical: a variant that is intronic relative to the
transcript kim's own lookup resolves for a locus can be missense relative
to a different, overlapping transcript VEP's own ranking picks as most
severe -- alternative splicing alone is enough, no bug required on either
side. `consequence_classes_disagree` (pipeline/annotation/stage.py) is a
detector for exactly this: it does not run inside `AnnotationStage.run()`
and changes no pipeline behaviour -- whether a disagreement should warn,
block, or simply be visible is a policy call left to whoever wires it in.
"""

from pipeline.annotation.stage import (
    _extract_csq_consequence,
    _map_consequence,
    _parse_csq_header,
    consequence_classes_disagree,
)


_CSQ_HEADER = (
    '##INFO=<ID=CSQ,Number=.,Type=String,Description="Consequence annotations '
    'from Ensembl VEP. Format: Allele|Consequence|Gene|Feature|HGVSc|HGVSp">'
)


def _csq_fields():
    return _parse_csq_header(_CSQ_HEADER)


class TestConsequenceClassesDisagree:
    def test_real_divergent_transcripts_are_detected(self):
        """The exact reproduction: identical chrom:pos:ref:alt, kim's own
        GffIndex-picked transcript says intronic, VEP's independently-picked
        most-severe transcript says missense."""
        kim_consequence = _map_consequence(
            region="intronic",
            ref="G",
            alt="A",
            codon_provider=None,
            chrom="chr17",
            pos=43094000,
            transcript_id="NM_007294.4",
        )
        assert kim_consequence == "intron_variant"

        info = (
            "CSQ=A|intron_variant|BRCA1|NM_007294.4|NM_007294.4:c.123+50G>A|,"
            "A|missense_variant|BRCA1|NM_007298.4|NM_007298.4:c.150G>A|NP_009229.2:p.Arg50His"
        )
        vep_consequence = _extract_csq_consequence(info, _csq_fields())
        assert vep_consequence == "missense_variant"

        assert consequence_classes_disagree(kim_consequence, vep_consequence) is True

    def test_agreement_is_not_flagged(self):
        """Control: when both derivations land in the same class, the
        detector must not raise a false positive."""
        assert consequence_classes_disagree("missense_variant", "missense_variant") is False
        # Different exact SO terms, same coarse class (both LOF) — the
        # detector is deliberately class-level, not string-exact.
        assert consequence_classes_disagree("stop_gained", "frameshift_variant") is False

    def test_either_side_empty_is_not_a_disagreement(self):
        """No CSQ available (VEP skipped/failed) or consequence not yet
        computed — nothing to compare, so no disagreement to report."""
        assert consequence_classes_disagree("", "missense_variant") is False
        assert consequence_classes_disagree("missense_variant", "") is False
        assert consequence_classes_disagree("", "") is False

    def test_multi_allele_csq_consequence_uses_class_membership(self):
        """VEP's own Consequence for one entry can itself be '&'-joined
        (multiple SO terms for one transcript, e.g. a variant that is both
        missense and splice-region). Disagreement is class-membership, not
        exact string equality."""
        assert (
            consequence_classes_disagree(
                "missense_variant", "missense_variant&splice_region_variant"
            )
            is False
        )
